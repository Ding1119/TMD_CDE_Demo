"""CDE snapshots and semantic versioning (patch = edit, minor = approval, major = breaking change)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import CDE, CDEVersion

DEFINITION_FIELDS = [
    "cde_code", "variable_name", "name", "definition", "question_text", "data_type", "unit", "min_value", "max_value",
    "instrument", "domain", "tier", "recall_period", "expected_source", "ehr_capturability", "ehr_validation_status",
    "status", "version", "copyright_note", "notes",
]


def snapshot(cde: CDE) -> dict:
    return {
        **{f: getattr(cde, f) for f in DEFINITION_FIELDS},
        "permissible_values": [
            {"code": p.code, "label": p.label, "concept_system": p.concept_system, "concept_code": p.concept_code}
            for p in cde.permissible_values
        ],
        "mappings": [
            {"system": m.system, "code": m.code, "display": m.display, "relation": m.relation, "status": m.status}
            for m in cde.mappings
        ],
    }


def bump(version: str, part: str) -> str:
    try:
        major, minor, patch = (int(x) for x in version.split("."))
    except ValueError:
        major, minor, patch = 0, 1, 0
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def record_version(db: Session, cde: CDE, note: str, author: str = "system", part: Optional[str] = "patch") -> CDEVersion:
    if part:
        cde.version = bump(cde.version, part)
    db.flush()
    v = CDEVersion(cde=cde, version=cde.version, snapshot=snapshot(cde), change_note=note, author=author)
    db.add(v)
    return v
