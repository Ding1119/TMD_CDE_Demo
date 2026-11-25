"""Server-rendered web UI."""
from __future__ import annotations

from typing import Optional

import json
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..agents.harmonization import promote_candidate
from ..agents.llm import LLMClient
from ..agents.orchestrator import AGENTS, create_run, execute_run
from ..config import BASE_DIR, settings
from ..db import get_db
from ..knowledge.base import ensure_sources, upsert_document
from ..knowledge.importers import import_file
from ..knowledge.online import DEFAULT_PUBMED_QUERY, fetch_pubmed, search_nlm_cde
from ..models import (CDE, CDE_STATUSES, CORE_DOMAINS, DATA_TYPES, DOMAINS, EHR_CAPTURABILITY, EXPECTED_SOURCES,
                      MODULE_DOMAINS, TIERS, Candidate, Implementation, KnowledgeSource, PermissibleValue,
                      Phenotype, PipelineRun, QAIssue, Review, SourceDocument, TerminologyMapping)
from ..services import exporters
from ..services.consensus import delphi_summary_for, delphi_table
from ..services.versioning import record_version

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))
templates.env.globals.update(
    DOMAINS=DOMAINS, CORE_DOMAINS=CORE_DOMAINS, MODULE_DOMAINS=MODULE_DOMAINS, TIERS=TIERS, DATA_TYPES=DATA_TYPES,
    EXPECTED_SOURCES=EXPECTED_SOURCES, EHR_CAPTURABILITY=EHR_CAPTURABILITY, CDE_STATUSES=CDE_STATUSES,
    settings=settings, AGENTS=AGENTS,
)
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)
templates.env.filters["num"] = lambda v: "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))
templates.env.filters["dt"] = lambda d: d.strftime("%Y-%m-%d %H:%M") if d else ""


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: Optional[str] = None, err: Optional[str] = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def _cde_or_404(db: Session, cde_id: int) -> CDE:
    cde = db.get(CDE, cde_id)
    if cde is None:
        raise HTTPException(404, "CDE not found")
    return cde


# ---------------------------------------------------------------------------- dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    report = exporters.coverage_report(db)
    counts = {
        "sources": db.scalar(select(func.count(KnowledgeSource.id))),
        "documents": db.scalar(select(func.count(SourceDocument.id))),
        "candidates": db.scalar(select(func.count(Candidate.id))),
        "reviews": db.scalar(select(func.count(Review.id))),
    }
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(5)).all()
    return page(request, "dashboard.html", report=report, counts=counts, runs=runs,
                llm_on=LLMClient().enabled)


# ---------------------------------------------------------------------------- sources
@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request, db: Session = Depends(get_db)):
    ensure_sources(db)
    srcs = db.scalars(select(KnowledgeSource).order_by(KnowledgeSource.weight.desc())).all()
    docs = db.scalars(select(SourceDocument).order_by(SourceDocument.relevance.desc(), SourceDocument.id)).all()
    return page(request, "sources.html", sources=srcs, docs=docs, default_query=DEFAULT_PUBMED_QUERY)


@router.post("/sources/upload")
async def upload(file: UploadFile = File(...), source_key: str = Form("upload"), db: Session = Depends(get_db)):
    sources_ = ensure_sources(db)
    if source_key not in sources_:
        return back("/sources", err=f"Unknown source {source_key}")
    try:
        payloads = import_file(file.filename or "upload.txt", await file.read())
    except Exception as exc:  # noqa: BLE001
        return back("/sources", err=f"Import failed: {exc}")
    n = 0
    for p in payloads:
        upsert_document(db, sources_[source_key], p)
        n += len(p.records) or 1
    db.commit()
    return back("/sources", msg=f"Imported {len(payloads)} document(s), {n} element(s)/text. Run the pipeline to process them.")


@router.post("/sources/fetch")
def fetch_online(kind: str = Form(...), query: str = Form(""), max_results: int = Form(20), db: Session = Depends(get_db)):
    if not settings.online:
        return back("/sources", err="Online mode is off (set TMD_CDE_ONLINE=1 and restart).")
    sources_ = ensure_sources(db)
    try:
        if kind == "pubmed":
            payloads, key = fetch_pubmed(query or DEFAULT_PUBMED_QUERY, max_results), "pubmed"
        else:
            payloads, key = search_nlm_cde(query or "temporomandibular", max_results), "nlm_cde"
    except Exception as exc:  # noqa: BLE001
        return back("/sources", err=f"Fetch failed: {exc}")
    for p in payloads:
        upsert_document(db, sources_[key], p)
    db.commit()
    return back("/sources", msg=f"Fetched {len(payloads)} document(s) from {key}.")


@router.get("/sources/documents/{doc_id}", response_class=HTMLResponse)
def document_view(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = db.get(SourceDocument, doc_id)
    if doc is None:
        raise HTTPException(404)
    try:
        pretty = json.dumps(json.loads(doc.content), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pretty = doc.content
    return page(request, "document.html", doc=doc, pretty=pretty[:60000])


# ---------------------------------------------------------------------------- pipeline
@router.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, db: Session = Depends(get_db)):
    runs = db.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(30)).all()
    return page(request, "pipeline.html", runs=runs, llm_on=LLMClient().enabled)


@router.post("/pipeline/run")
async def pipeline_run(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    form = await request.form()
    options = {
        "agents": form.getlist("agents") or None,
        "use_llm": form.get("use_llm") == "on",
        "online": form.get("online") == "on",
        "min_relevance": float(form.get("min_relevance") or 0.35),
        "pubmed_query": form.get("pubmed_query") or None,
        "pubmed_max": int(form.get("pubmed_max") or 20),
    }
    run = create_run(db, {k: v for k, v in options.items() if v is not None})
    background.add_task(execute_run, run.id)
    return back(f"/runs/{run.id}", msg="Pipeline started")


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(404)
    return page(request, "run.html", run=run)


# ---------------------------------------------------------------------------- candidates
@router.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, status: Optional[str] = None, db: Session = Depends(get_db)):
    stmt = select(Candidate).order_by(Candidate.id.desc()).limit(500)
    if status:
        stmt = stmt.where(Candidate.status == status)
    rows = db.scalars(stmt).all()
    counts = dict(db.execute(select(Candidate.status, func.count()).group_by(Candidate.status)).all())
    return page(request, "candidates.html", rows=rows, counts=counts, status=status)


@router.post("/candidates/{cand_id}/promote")
def candidate_promote(cand_id: int, db: Session = Depends(get_db)):
    cand = db.get(Candidate, cand_id)
    if cand is None or cand.cde_id:
        return back("/candidates", err="Candidate not found or already linked")
    cde = promote_candidate(db, cand)
    return back(f"/cdes/{cde.id}", msg="Candidate promoted to a draft CDE")


@router.post("/candidates/{cand_id}/reject")
def candidate_reject(cand_id: int, db: Session = Depends(get_db)):
    cand = db.get(Candidate, cand_id)
    if cand:
        cand.status = "rejected"
        db.commit()
    return back("/candidates?status=unmatched", msg="Candidate rejected")


# ---------------------------------------------------------------------------- CDE repository
@router.get("/cdes", response_class=HTMLResponse)
def cdes(request: Request, q: str = "", domain: str = "", tier: str = "", status: str = "", capt: str = "",
         db: Session = Depends(get_db)):
    stmt = select(CDE).order_by(CDE.cde_code)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(CDE.name.ilike(like), CDE.definition.ilike(like), CDE.variable_name.ilike(like),
                              CDE.instrument.ilike(like), CDE.cde_code.ilike(like)))
    for col, val in ((CDE.domain, domain), (CDE.tier, tier), (CDE.status, status), (CDE.ehr_capturability, capt)):
        if val:
            stmt = stmt.where(col == val)
    rows = db.scalars(stmt).all()
    return page(request, "cdes.html", rows=rows, f={"q": q, "domain": domain, "tier": tier, "status": status, "capt": capt})


@router.get("/cdes/{cde_id}", response_class=HTMLResponse)
def cde_detail(cde_id: int, request: Request, db: Session = Depends(get_db)):
    cde = _cde_or_404(db, cde_id)
    rounds = sorted({r.round for r in cde.reviews}) or [1]
    return page(request, "cde_detail.html", c=cde, delphi=delphi_summary_for(cde), next_round=max(rounds),
                open_issues=[q for q in cde.qa_issues if not q.resolved],
                pv_text="\n".join(f"{p.code}={p.label}" for p in cde.permissible_values))


def _num(v) -> Optional[float]:
    try:
        return float(v) if str(v).strip() != "" else None
    except ValueError:
        return None


@router.post("/cdes/{cde_id}/edit")
async def cde_edit(cde_id: int, request: Request, db: Session = Depends(get_db)):
    cde = _cde_or_404(db, cde_id)
    if cde.status == "approved":
        return back(f"/cdes/{cde_id}", err="Approved CDEs are locked; move back to 'under_review' to edit.")
    form = await request.form()
    for field in ("name", "definition", "question_text", "data_type", "unit", "domain", "tier", "expected_source",
                  "ehr_capturability", "recall_period", "instrument", "copyright_note", "notes"):
        if field in form:
            setattr(cde, field, (form.get(field) or "").strip() or None)
    cde.min_value, cde.max_value = _num(form.get("min_value")), _num(form.get("max_value"))
    if not cde.name or not cde.data_type or not cde.domain:
        return back(f"/cdes/{cde_id}", err="Name, data type and domain are required.")
    new_pvs = []
    for i, line in enumerate((form.get("pvs") or "").splitlines()):
        if line.strip():
            code, _, label = line.partition("=")
            new_pvs.append(PermissibleValue(code=code.strip(), label=(label or code).strip(), sort_order=i))
    old = {p.code: p for p in cde.permissible_values}
    for p in new_pvs:  # keep value-level concept codes for unchanged codes
        if p.code in old:
            p.concept_system, p.concept_code = old[p.code].concept_system, old[p.code].concept_code
    cde.permissible_values = new_pvs
    record_version(db, cde, form.get("change_note") or "Edited in UI", author=form.get("author") or "reviewer")
    db.commit()
    return back(f"/cdes/{cde_id}", msg=f"Saved as v{cde.version}")


@router.post("/cdes/{cde_id}/status")
def cde_status(cde_id: int, status: str = Form(...), author: str = Form("reviewer"), db: Session = Depends(get_db)):
    cde = _cde_or_404(db, cde_id)
    if status not in CDE_STATUSES:
        return back(f"/cdes/{cde_id}", err="Unknown status")
    if status == "approved":
        if any(q.severity == "error" and not q.resolved for q in cde.qa_issues):
            return back(f"/cdes/{cde_id}", err="Resolve QA errors before approval.")
        part, note = "minor", "Approved"
    else:
        part, note = "patch", f"Status -> {status}"
    cde.status = status
    record_version(db, cde, note, author=author, part=part)
    db.commit()
    return back(f"/cdes/{cde_id}", msg=f"Status set to {status} (v{cde.version})")


@router.post("/cdes/{cde_id}/review")
def cde_review(cde_id: int, reviewer: str = Form(...), role: str = Form(""), round: int = Form(1),
               importance: int = Form(...), feasibility: int = Form(...), comment: str = Form(""),
               db: Session = Depends(get_db)):
    cde = _cde_or_404(db, cde_id)
    if not (1 <= importance <= 9 and 1 <= feasibility <= 9):
        return back(f"/cdes/{cde_id}", err="Ratings must be 1-9")
    db.add(Review(cde=cde, reviewer=reviewer.strip(), role=role or None, round=round, importance=importance,
                  feasibility=feasibility, comment=comment or None))
    if cde.status == "draft":
        cde.status = "under_review"
    db.commit()
    return back(f"/cdes/{cde_id}", msg="Rating saved")


@router.post("/cdes/{cde_id}/mappings")
def mapping_add(cde_id: int, system: str = Form(...), code: str = Form(...), display: str = Form(""),
                relation: str = Form("exact"), db: Session = Depends(get_db)):
    cde = _cde_or_404(db, cde_id)
    db.add(TerminologyMapping(cde=cde, system=system.strip(), code=code.strip(), display=display or None,
                              relation=relation, confidence=1.0, method="manual", status="accepted"))
    for m in list(cde.mappings):
        if m.relation == "gap" and m.system == system.strip():
            cde.mappings.remove(m)
    db.commit()
    return back(f"/cdes/{cde_id}", msg="Mapping added")


@router.post("/mappings/{map_id}/{action}")
def mapping_action(map_id: int, action: str, db: Session = Depends(get_db)):
    m = db.get(TerminologyMapping, map_id)
    if m is None or action not in {"accept", "reject"}:
        raise HTTPException(404)
    m.status = "accepted" if action == "accept" else "rejected"
    db.commit()
    return back(f"/cdes/{m.cde_id}", msg=f"Mapping {m.status}")


@router.post("/implementations/{impl_id}/validate")
def impl_validate(impl_id: int, db: Session = Depends(get_db)):
    impl = db.get(Implementation, impl_id)
    if impl is None:
        raise HTTPException(404)
    impl.status = "validated"
    cde = impl.cde
    ehr = [i for i in cde.implementations if i.target in {"OMOP", "PCORnet", "i2b2", "NLP"}]
    if ehr and all(i.status == "validated" for i in ehr):
        cde.ehr_validation_status = "validated"
    db.commit()
    return back(f"/cdes/{cde.id}", msg=f"{impl.target} binding marked validated")


@router.post("/qa/{issue_id}/resolve")
def qa_resolve(issue_id: int, db: Session = Depends(get_db)):
    q = db.get(QAIssue, issue_id)
    if q is None:
        raise HTTPException(404)
    q.resolved = True
    db.commit()
    return back(f"/cdes/{q.cde_id}", msg="Issue marked resolved")


# ---------------------------------------------------------------------------- review, phenotype, exports
@router.get("/review", response_class=HTMLResponse)
def review(request: Request, round: Optional[int] = None, decision: str = "", db: Session = Depends(get_db)):
    rows = delphi_table(db, round)
    if decision:
        rows = [r for r in rows if r[1]["decision"] == decision]
    rounds = sorted(set(db.scalars(select(Review.round).distinct()))) or [1]
    return page(request, "review.html", rows=rows, rounds=rounds, round=round, decision=decision)


@router.get("/phenotype", response_class=HTMLResponse)
def phenotype(request: Request, db: Session = Depends(get_db)):
    return page(request, "phenotype.html", phenotypes=db.scalars(select(Phenotype)).all())


@router.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request):
    return page(request, "exports.html")


@router.get("/export/{kind}")
def export(kind: str, status: Optional[str] = None, db: Session = Depends(get_db)) -> Response:
    def attach(content: str, name: str, media: str) -> Response:
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    if kind == "cdes.json":
        return JSONResponse(exporters.export_json(db, status),
                            headers={"Content-Disposition": 'attachment; filename="tmd_cdes.json"'})
    if kind == "cdes.csv":
        return attach(exporters.export_csv(db, status), "tmd_cdes.csv", "text/csv")
    if kind == "redcap_core.csv":
        return attach(exporters.export_redcap(db, status, tier="core"), "tmd_cde_core_redcap_dictionary.csv", "text/csv")
    if kind == "redcap_all.csv":
        return attach(exporters.export_redcap(db, status), "tmd_cde_all_redcap_dictionary.csv", "text/csv")
    if kind == "omop_concept_sets.csv":
        return attach(exporters.export_concept_sets(db), "tmd_omop_concept_sets.csv", "text/csv")
    if kind == "omop_resolve.sql":
        return PlainTextResponse(exporters.concept_set_sql(),
                                 headers={"Content-Disposition": 'attachment; filename="tmd_omop_resolve.sql"'})
    if kind == "nlp_lexicon.json":
        return JSONResponse(exporters.export_nlp_lexicon(db))
    if kind == "phenotype.json":
        return JSONResponse(exporters.export_phenotype(db))
    if kind == "coverage_report.json":
        return JSONResponse(exporters.coverage_report(db))
    raise HTTPException(404, "Unknown export")
