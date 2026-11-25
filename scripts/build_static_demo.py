"""Build a read-only static demo of the platform, e.g. for GitHub Pages.

    python scripts/build_static_demo.py --out site [--repo-url https://github.com/you/tmd-cde-platform]

It creates a throw-away database, runs the full agent pipeline on the bundled NIH HEAL / DC/TMD seed plus
the synthetic files in sample_inputs/, adds a synthetic Delphi round, renders every page to static HTML with
relative links, and writes all export files. Forms are hidden; a banner explains the demo.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r'(href|src|action)="/(?!/)([^"#]*)(#[^"]*)?"')
EXPORTS = ["cdes.json", "cdes.csv", "redcap_core.csv", "redcap_all.csv", "omop_concept_sets.csv", "omop_resolve.sql",
           "nlp_lexicon.json", "phenotype.json", "coverage_report.json"]

STATIC_HEAD = """<style>
form, .interactive { display: none !important; }
.static-banner { background: #0f766e; color: #fff; font-size: 13px; padding: 8px 24px; text-align: center; }
.static-banner a { color: #fff; text-decoration: underline; }
.static-search { width: 100%; max-width: 420px; margin: 0 0 12px; padding: 7px 9px; border: 1px solid #d1d5db; border-radius: 7px; font: inherit; }
</style>"""

STATIC_SCRIPT = """<script>
(function () {
  var filt = document.querySelector('form.filters[method="get"]');
  var tbl = document.querySelector('.card table.tbl');
  if (!filt || !tbl) return;
  var box = document.createElement('input');
  box.type = 'search'; box.className = 'static-search'; box.placeholder = 'Filter rows (static demo)';
  tbl.parentNode.insertBefore(box, tbl);
  box.addEventListener('input', function () {
    var q = box.value.toLowerCase();
    tbl.querySelectorAll('tr').forEach(function (tr, i) {
      if (i > 0) tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    });
  });
})();
</script>"""


def banner(repo_url: str | None) -> str:
    install = f'<a href="{repo_url}">Install it</a>' if repo_url else "Install it"
    return ('<div class="static-banner">Read-only demo &middot; built from the bundled NIH HEAL / DC/TMD seed plus '
            'synthetic example uploads; expert ratings are synthetic placeholders. '
            f'{install} to run the agents, upload sources and review CDEs.</div>')


def rewrite_links(html: str, depth: int) -> str:
    prefix = "../" * depth

    def repl(m: re.Match) -> str:
        attr, target, frag = m.group(1), m.group(2), m.group(3) or ""
        path, _, query = target.partition("?")
        path = path.rstrip("/")
        if path.startswith(("static/", "export/")):
            if path == "export/cdes.json" and "status=approved" in query:
                path = "export/cdes_approved.json"
            return f'{attr}="{prefix}{path}{frag}"'
        if path == "docs":
            path = "exports"
        return f'{attr}="{prefix}{path + "/" if path else ""}index.html{frag}"'

    return LINK_RE.sub(repl, html)


def build_data() -> None:
    from app.agents.orchestrator import run_pipeline
    from app.db import SessionLocal, init_db
    from app.knowledge.base import ensure_sources, upsert_document
    from app.knowledge.importers import import_file
    from app.services.demo import add_demo_reviews, apply_demo_decisions

    init_db()
    run_pipeline({"use_llm": False, "online": False})
    samples = ROOT / "sample_inputs"
    with SessionLocal() as db:
        sources = ensure_sources(db)
        for f in sorted(samples.iterdir()):
            key = "ehr_cdm" if "i2b2" in f.name else "upload"
            for payload in import_file(f.name, f.read_bytes()):
                upsert_document(db, sources[key], payload)
        db.commit()
    run_pipeline({"use_llm": False, "online": False})
    with SessionLocal() as db:
        add_demo_reviews(db)
        print("Demo decisions:", apply_demo_decisions(db))
    run_pipeline({"use_llm": False, "online": False, "agents": ["quality_assurance"]})


def render(out: Path, repo_url: str | None) -> int:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.main import app
    from app.models import CDE, PipelineRun, SourceDocument

    with SessionLocal() as db:
        paths = ["/", "/sources", "/pipeline", "/candidates", "/cdes", "/review", "/phenotype", "/exports"]
        paths += [f"/cdes/{i}" for i in db.scalars(select(CDE.id))]
        paths += [f"/sources/documents/{i}" for i in db.scalars(select(SourceDocument.id))]
        paths += [f"/runs/{i}" for i in db.scalars(select(PipelineRun.id))]

    if out.exists():
        shutil.rmtree(out)
    (out / "export").mkdir(parents=True)
    shutil.copytree(ROOT / "app" / "web" / "static", out / "static")
    (out / ".nojekyll").write_text("")
    with TestClient(app) as client:
        for path in paths:
            r = client.get(path)
            r.raise_for_status()
            depth = len([p for p in path.strip("/").split("/") if p])
            html = r.text.replace("http://testserver/", "/")
            html = rewrite_links(html, depth)
            html = html.replace("</head>", STATIC_HEAD + "\n</head>", 1)
            html = html.replace("<body>", "<body>\n" + banner(repo_url), 1)
            html = html.replace("</body>", STATIC_SCRIPT + "\n</body>", 1)
            target = out / path.strip("/") / "index.html" if depth else out / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html, encoding="utf-8")
        for kind in EXPORTS:
            (out / "export" / kind).write_bytes(client.get(f"/export/{kind}").content)
        (out / "export" / "cdes_approved.json").write_bytes(client.get("/export/cdes.json?status=approved").content)
    return len(paths)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="site")
    ap.add_argument("--repo-url", default=None, help="link shown in the demo banner")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="tmdcde_site_")
    os.environ.update({"TMD_CDE_DB_URL": f"sqlite:///{Path(tmp) / 'demo.db'}", "TMD_CDE_DATA_DIR": tmp,
                       "TMD_CDE_ONLINE": "0", "TMD_CDE_LLM_PROVIDER": "none"})
    sys.path.insert(0, str(ROOT))
    build_data()
    n = render(Path(args.out).resolve(), args.repo_url)
    print(f"Static demo written to {args.out}/ ({n} pages). Open {args.out}/index.html in a browser.")


if __name__ == "__main__":
    main()
