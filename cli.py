"""Command-line entry point.

    python cli.py init            # create database + load seed knowledge sources
    python cli.py pipeline        # run all agents once (offline unless TMD_CDE_ONLINE=1)
    python cli.py import FILE     # add a HEAL/NLM/TMD IMPACT/REDCap/dbGaP/i2b2/text file as a knowledge source
    python cli.py serve           # start the web platform on http://127.0.0.1:8000
    python cli.py demo-reviews    # add synthetic Delphi ratings (for trying the review screens)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.db import SessionLocal, init_db


def cmd_init(_args) -> None:
    from app.knowledge.seed_loader import load_seed_documents

    init_db()
    with SessionLocal() as db:
        print("Seed knowledge:", load_seed_documents(db))


def cmd_pipeline(args) -> None:
    from app.agents.orchestrator import run_pipeline
    from app.models import PipelineRun

    init_db()
    run_id = run_pipeline({"use_llm": not args.no_llm, "online": not args.offline})
    with SessionLocal() as db:
        run = db.get(PipelineRun, run_id)
        print(f"Run #{run_id}: {run.status}")
        for log in run.logs:
            print(f"  [{log.agent}] {log.message}")


def cmd_import(args) -> None:
    from app.knowledge.base import ensure_sources, upsert_document
    from app.knowledge.importers import import_file

    init_db()
    path = Path(args.file)
    with SessionLocal() as db:
        sources = ensure_sources(db)
        for p in import_file(path.name, path.read_bytes()):
            doc, created = upsert_document(db, sources[args.source], p)
            print(f"{'Added' if created else 'Updated'}: {doc.title} ({doc.doc_type}, {len(p.records)} records)")
        db.commit()


def cmd_demo_reviews(args) -> None:
    from app.services.demo import add_demo_reviews, apply_demo_decisions

    with SessionLocal() as db:
        print(f"Added synthetic demo ratings for {add_demo_reviews(db)} CDEs")
        if args.decide:
            print("Demo decisions:", apply_demo_decisions(db))


def cmd_serve(args) -> None:
    import uvicorn

    init_db()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("pipeline")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(fn=cmd_pipeline)
    p = sub.add_parser("import")
    p.add_argument("file")
    p.add_argument("--source", default="upload",
                   help="knowledge source key: nih_heal | nlm_cde | dc_tmd | tmd_impact | study_dictionary | ehr_cdm | upload")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("demo-reviews")
    p.add_argument("--decide", action="store_true", help="also move rated CDEs to review / approve consensus core CDEs")
    p.set_defaults(fn=cmd_demo_reviews)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
