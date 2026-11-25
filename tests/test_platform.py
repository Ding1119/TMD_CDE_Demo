"""End-to-end tests: seed -> agents -> repository -> review -> exports -> web pages (offline, no LLM)."""
import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.harmonization import can_merge
from app.agents.heuristics import name_similarity
from app.agents.orchestrator import run_pipeline
from app.db import SessionLocal, init_db
from app.knowledge.base import ensure_sources, upsert_document
from app.knowledge.importers import import_file, parse_permissible_values
from app.main import app
from app.models import CDE, Candidate, Phenotype, PipelineRun, Review
from app.services import exporters
from app.services.consensus import delphi_summary_for

SAMPLES = Path(__file__).resolve().parent.parent / "sample_inputs"


@pytest.fixture(scope="module")
def seeded():
    init_db()
    run_id = run_pipeline({"use_llm": False, "online": False})
    with SessionLocal() as db:
        sources = ensure_sources(db)
        for f in ["example_protocol_methods.txt", "example_redcap_dictionary.csv", "example_dbgap_data_dict.xml",
                  "example_heal_style_cde.xlsx"]:
            for p in import_file(f, (SAMPLES / f).read_bytes()):
                upsert_document(db, sources["upload"], p)
        for p in import_file("example_i2b2_ontology.csv", (SAMPLES / "example_i2b2_ontology.csv").read_bytes()):
            upsert_document(db, sources["ehr_cdm"], p)
        db.commit()
    run2 = run_pipeline({"use_llm": False, "online": False})
    return run_id, run2


def test_pipeline_builds_repository(seeded):
    run1, run2 = seeded
    with SessionLocal() as db:
        assert db.get(PipelineRun, run1).status == "completed"
        assert db.get(PipelineRun, run2).status in {"completed", "needs_review"}
        cdes = db.scalars(select(CDE)).all()
        assert len(cdes) >= 60
        names = {c.name for c in cdes}
        assert {"PEG total score", "Maximum unassisted opening", "TMD diagnosis code", "JFLS-8 global score"} <= names
        assert len({c.variable_name for c in cdes}) == len(cdes)
        assert all(c.provenance for c in cdes)
        assert db.scalar(select(Phenotype)) is not None


def test_harmonization_merges_and_keeps_apart(seeded):
    with SessionLocal() as db:
        jfls = db.scalar(select(CDE).where(CDE.variable_name == "jfls20_global"))
        assert {p.source_key for p in jfls.provenance} >= {"nih_heal", "dc_tmd"}  # same element from two sources
        muo = db.scalar(select(CDE).where(CDE.variable_name == "exam_muo"))
        assert {p.source_key for p in muo.provenance} >= {"nih_heal", "upload"}  # REDCap + dbGaP + text evidence
        assert db.scalar(select(CDE).where(CDE.variable_name == "exam_mao")) is not None  # not merged into MUO
        assert db.scalar(select(CDE).where(CDE.variable_name == "phq2_total")).id != \
            db.scalar(select(CDE).where(CDE.variable_name == "phq9_total")).id
        fav = db.scalar(select(Candidate).where(Candidate.name == "Favorite color"))
        assert fav.status == "unmatched"


def test_similarity_guards():
    assert name_similarity("Maximum unassisted opening", "Maximum assisted opening") < 0.78
    assert name_similarity("JFLS-20 global score", "Jaw Functional Limitation Scale-20 global score") == 1.0


def test_terminology_and_ehr_layer(seeded):
    with SessionLocal() as db:
        phq9 = db.scalar(select(CDE).where(CDE.variable_name == "phq9_total"))
        assert any(m.system == "LOINC" and m.code == "44261-6" for m in phq9.mappings)
        dx = db.scalar(select(CDE).where(CDE.variable_name == "tmd_dx_icd10"))
        omop = next(i for i in dx.implementations if i.target == "OMOP")
        codes = {v["code"] for v in omop.value_set}
        assert {"M26.621", "M79.11", "M26.69"} <= codes
        assert any(i.target == "i2b2" for i in dx.implementations)  # from uploaded i2b2 metadata
        noise = db.scalar(select(CDE).where(CDE.variable_name == "exam_tmj_noise"))
        nlp = next(i for i in noise.implementations if i.target == "NLP")
        assert "crepitus" in nlp.nlp_terms
        obc = db.scalar(select(CDE).where(CDE.variable_name == "obc_total"))
        assert any(m.relation == "gap" and m.system == "LOINC" for m in obc.mappings)


def test_delphi_consensus(seeded):
    with SessionLocal() as db:
        cde = db.scalar(select(CDE).where(CDE.variable_name == "peg_total"))
        for who, imp in [("A", 8), ("B", 9), ("C", 7), ("D", 5)]:
            db.add(Review(cde=cde, reviewer=who, importance=imp, feasibility=4))
        db.commit()
        db.refresh(cde)
        s = delphi_summary_for(cde)
        assert s["n"] == 4 and s["decision"] == "include" and s["imp_high"] == 0.75


def test_exports(seeded):
    with SessionLocal() as db:
        rows = list(csv.reader(io.StringIO(exporters.export_redcap(db))))
        assert rows[0] == exporters.REDCAP_HEADER and len(rows[0]) == 18
        assert all(len(r) == 18 for r in rows)
        var_names = [r[0] for r in rows[1:]]
        assert len(var_names) == len(set(var_names))
        data = exporters.export_json(db)
        assert data["cdes"] and "implementation" in data["cdes"][0]
        assert "ICD10CM" in exporters.export_concept_sets(db)
        rep = exporters.coverage_report(db)
        assert rep["n_cdes"] >= 60 and 0 <= rep["terminology_coverage_pct"] <= 100


def test_pv_parsing():
    assert parse_permissible_values("1, Yes | 0, No") == [{"code": "1", "label": "Yes"}, {"code": "0", "label": "No"}]
    assert parse_permissible_values("0;1", "No;Yes") == [{"code": "0", "label": "No"}, {"code": "1", "label": "Yes"}]


def test_web_pages(seeded):
    with TestClient(app) as client:
        with SessionLocal() as db:
            cde_id = db.scalar(select(CDE.id).where(CDE.variable_name == "exam_muo"))
            cand_id = db.scalar(select(Candidate.id).where(Candidate.status == "unmatched"))
        for url in ["/", "/sources", "/pipeline", "/candidates", "/cdes", "/cdes?domain=Jaw+Function", f"/cdes/{cde_id}",
                    "/review", "/phenotype", "/exports", "/runs/1", "/sources/documents/1", "/api/stats", "/docs"]:
            r = client.get(url)
            assert r.status_code == 200, url
        for kind in ["cdes.json", "cdes.csv", "redcap_core.csv", "redcap_all.csv", "omop_concept_sets.csv",
                     "omop_resolve.sql", "nlp_lexicon.json", "phenotype.json", "coverage_report.json"]:
            assert client.get(f"/export/{kind}").status_code == 200, kind
        # human-in-the-loop actions
        r = client.post(f"/cdes/{cde_id}/review", data={"reviewer": "Dr X", "importance": 8, "feasibility": 6, "round": 1},
                        follow_redirects=False)
        assert r.status_code == 303
        r = client.post(f"/cdes/{cde_id}/edit", data={"name": "Maximum unassisted opening", "definition": "Largest "
                        "interincisal opening achieved without assistance, measured in millimetres.", "data_type": "integer",
                        "domain": "Jaw Function", "tier": "core", "min_value": "0", "max_value": "90", "pvs": "",
                        "change_note": "test edit"}, follow_redirects=False)
        assert r.status_code == 303
        r = client.post(f"/cdes/{cde_id}/status", data={"status": "approved"}, follow_redirects=False)
        assert r.status_code == 303
        assert client.get(f"/api/cdes/exam_muo").json()["status"] == "approved"
        r = client.post(f"/candidates/{cand_id}/promote", follow_redirects=False)
        assert r.status_code == 303
        # upload through the UI
        files = {"file": ("example_redcap_dictionary.csv", (SAMPLES / "example_redcap_dictionary.csv").read_bytes(), "text/csv")}
        assert client.post("/sources/upload", files=files, data={"source_key": "study_dictionary"},
                           follow_redirects=False).status_code == 303


class FakeLLM:
    """Stands in for Claude: returns fixed JSON so the LLM code paths run without an API key."""
    enabled = True
    model = "fake"

    def complete_json(self, system, prompt, max_tokens=4000):
        if "data-standards curator" in system:
            assert "DOCUMENT:" in prompt
            return [{"name": "Masticatory endurance time", "definition": None, "data_type": "float", "unit": "s",
                     "domain": "Jaw Function", "expected_source": "exam", "evidence": "chewing endurance was timed"}]
        if "ISO/IEC 11179" in system:
            return {"definition": "Time in seconds a participant can chew a standard bolus before stopping due to fatigue or pain."}
        if "terminology specialist" in system:
            return [{"system": "NCIT", "code": "C00000", "display": "placeholder", "relation": "related"}]
        return None


def test_llm_paths(seeded):
    from app.agents.base import AgentContext
    from app.agents.extraction import ExtractionAgent
    from app.agents.harmonization import HarmonizationAgent
    from app.agents.terminology_mapping import TerminologyMappingAgent
    from app.knowledge.base import DocPayload

    with SessionLocal() as db:
        src = ensure_sources(db)["upload"]
        doc, _ = upsert_document(db, src, DocPayload(external_id="llm-test", title="LLM test protocol", doc_type="text",
                                                     text="Masticatory endurance was timed during gum chewing in TMD patients."))
        doc.status = "prioritized"
        run = PipelineRun(status="running", config={})
        db.add(run)
        db.commit()
        ctx = AgentContext(db=db, run=run, llm=FakeLLM(), options={"use_llm": True})
        assert ExtractionAgent().run(ctx).stats["llm"] == 1
        assert TerminologyMappingAgent().run(ctx).stats["llm"] == 1
        HarmonizationAgent().run(ctx)
        cde = db.scalar(select(CDE).where(CDE.name == "Masticatory endurance time"))
        assert cde is not None and cde.definition.startswith("Time in seconds")
        assert any(m.method == "llm" and m.status == "proposed" for m in cde.mappings)
