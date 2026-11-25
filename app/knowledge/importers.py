"""File importers for knowledge sources uploaded by users.

Supported inputs
----------------
* HEAL CDE / NINDS-style CDE spreadsheets (.xlsx/.csv) - e.g. files from the HEAL CDE Repository
* TMD IMPACT CDE exports (.xlsx/.csv, same tabular logic)
* REDCap data dictionaries (.csv)
* NIH CDE Repository (NLM) exports (.json or .csv)
* dbGaP data dictionaries (.xml), e.g. OPPERA variable dictionaries
* i2b2 ontology metadata (.csv/.xlsx with c_fullname / c_basecode) -> EHR dictionary
* Protocol / methods text (.txt, .md, and .pdf when pypdf is installed) -> text for LLM/rule extraction
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

from .base import DocPayload

# ---------------------------------------------------------------------------
# Column synonyms (lower-case, whitespace collapsed)
# ---------------------------------------------------------------------------
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "variable_hint": ["variable name", "variable / field name", "field name", "variable", "var name", "element name (short)"],
    "name": ["title", "cde name", "data element name", "element name", "long name", "name", "field label", "variable label"],
    "definition": ["definition", "data element definition", "description", "short description", "variable description"],
    "question_text": ["preferred question text", "question text", "question texts", "question", "field label", "item text"],
    "data_type": ["data type", "datatype", "field type", "value type", "type"],
    "validation": ["text validation type or show slider number", "text validation type", "input restriction"],
    "permissible_values": ["permissible values", "choices, calculations, or slider labels", "choices", "permissible value",
                           "responses", "response options", "values"],
    "pv_descriptions": ["permissible value descriptions", "pv descriptions", "permissible value labels",
                        "permissible value output codes"],
    "min_value": ["minimum value", "min value", "min", "text validation min"],
    "max_value": ["maximum value", "max value", "max", "text validation max"],
    "unit": ["unit of measure", "units", "unit", "uom"],
    "instrument": ["crf name", "form name", "instrument", "questionnaire", "crf", "form"],
    "domain": ["domain", "subdomain", "category", "classification"],
    "recall_period": ["recall period", "time frame"],
}

I2B2_COLUMNS = {"c_fullname", "c_basecode"}


def _norm(h: Any) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def _strip_html(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", " ", str(s))
    return re.sub(r"\s+", " ", s).strip() or None


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).strip())
    except ValueError:
        return None


def normalize_data_type(raw: Optional[str], validation: Optional[str] = None, has_pvs: bool = False) -> str:
    t = _norm(raw)
    v = _norm(validation)
    if v in {"integer"}:
        return "integer"
    if v in {"number", "number_1dp", "number_2dp", "float"}:
        return "float"
    if v.startswith("date"):
        return "date"
    mapping = [
        (("checkbox", "multi", "select all"), "multi_choice"),
        (("radio", "dropdown", "value list", "choice", "enum", "list"), "choice"),
        (("yesno", "truefalse", "boolean", "bool"), "boolean"),
        (("integer", "int"), "integer"),
        (("float", "decimal", "number", "numeric", "calc", "slider", "real"), "float"),
        (("date", "time"), "date"),
        (("code",), "code"),
    ]
    for keys, out in mapping:
        if any(k in t for k in keys):
            return "choice" if (out in {"code", "text"} and has_pvs) else out
    return "choice" if has_pvs else "text"


def parse_permissible_values(values: Any, descriptions: Any = None) -> list[dict]:
    """Handle REDCap ('1, Yes | 0, No'), NINDS/HEAL ('0;1' + 'No;Yes') and 'code=label' lines."""
    if values is None or str(values).strip() == "":
        return []
    s = str(values).strip()
    descs = [d.strip() for d in re.split(r"[;\n]", str(descriptions))] if descriptions not in (None, "") else []
    out: list[dict] = []
    if "|" in s:
        for i, seg in enumerate(x.strip() for x in s.split("|") if x.strip()):
            code, _, label = seg.partition(",")
            out.append({"code": code.strip(), "label": (label.strip() or code.strip())})
        return out
    parts = [p.strip() for p in re.split(r"[;\n]", s) if p.strip()]
    for i, p in enumerate(parts):
        if "=" in p:
            code, _, label = p.partition("=")
        elif re.match(r"^-?\d+\s*,\s*\S", p):
            code, _, label = p.partition(",")
        else:
            code, label = p, descs[i] if i < len(descs) and descs[i] else p
        out.append({"code": code.strip(), "label": label.strip()})
    return out


def _header_index(header: list[str]) -> dict[str, int]:
    idx: dict[str, int] = {}
    normed = [_norm(h) for h in header]
    for field, syns in COLUMN_SYNONYMS.items():
        for syn in syns:
            if syn in normed:
                idx[field] = normed.index(syn)
                break
    return idx


def _rows_to_records(header: list[str], rows: list[list[Any]], default_instrument: Optional[str]) -> list[dict]:
    idx = _header_index(header)
    get = lambda row, f: row[idx[f]] if f in idx and idx[f] < len(row) else None  # noqa: E731
    records = []
    for row in rows:
        if not any(str(c or "").strip() for c in row):
            continue
        raw_type = get(row, "data_type")
        if _norm(raw_type) == "descriptive":  # REDCap descriptive text, not a data element
            continue
        pvs = parse_permissible_values(get(row, "permissible_values"), get(row, "pv_descriptions"))
        if _norm(raw_type) == "calc":
            pvs = []  # REDCap calc formulas live in the choices column
        elif _norm(raw_type) == "yesno" and not pvs:
            pvs = [{"code": "1", "label": "Yes"}, {"code": "0", "label": "No"}]
        elif _norm(raw_type) == "truefalse" and not pvs:
            pvs = [{"code": "1", "label": "True"}, {"code": "0", "label": "False"}]
        label = _strip_html(get(row, "name")) or _strip_html(get(row, "question_text")) or _strip_html(get(row, "variable_hint"))
        if not label:
            continue
        rec = {
            "name": label[:300],
            "variable_hint": _strip_html(get(row, "variable_hint")),
            "definition": _strip_html(get(row, "definition")),
            "question_text": _strip_html(get(row, "question_text")),
            "data_type": normalize_data_type(raw_type, get(row, "validation"), bool(pvs)),
            "unit": _strip_html(get(row, "unit")),
            "min_value": _to_float(get(row, "min_value")),
            "max_value": _to_float(get(row, "max_value")),
            "permissible_values": pvs,
            "instrument": _strip_html(get(row, "instrument")) or default_instrument,
            "domain": _strip_html(get(row, "domain")),
            "recall_period": _strip_html(get(row, "recall_period")),
        }
        records.append(rec)
    return records


def _find_header_row(rows: list[list[Any]]) -> int:
    """Return the index of the first row that looks like a header (has >=2 known column names)."""
    known = {s for syns in COLUMN_SYNONYMS.values() for s in syns} | I2B2_COLUMNS
    for i, row in enumerate(rows[:30]):
        if sum(_norm(c) in known for c in row) >= 2:
            return i
    return 0


def _tabular_to_payloads(rows: list[list[Any]], stem: str, sheet: Optional[str] = None) -> list[DocPayload]:
    if not rows:
        return []
    h = _find_header_row(rows)
    header = [str(c or "") for c in rows[h]]
    body = rows[h + 1:]
    normed = {_norm(c) for c in header}
    ext = f"{stem}:{sheet}" if sheet else stem
    title = f"{stem} ({sheet})" if sheet else stem
    if I2B2_COLUMNS <= normed:  # i2b2 ontology metadata -> EHR dictionary
        col = {_norm(c): i for i, c in enumerate(header)}
        recs = []
        for r in body:
            if len(r) <= col["c_basecode"]:
                continue
            recs.append({
                "path": r[col["c_fullname"]],
                "name": r[col["c_name"]] if "c_name" in col else None,
                "basecode": r[col["c_basecode"]],
            })
        return [DocPayload(external_id=ext, title=f"i2b2 ontology: {title}", doc_type="ehr_dictionary",
                           records=recs, meta={"model": "i2b2"})]
    is_redcap = "variable / field name" in normed
    records = _rows_to_records(header, body, default_instrument=None if is_redcap else stem)
    kind = "REDCap data dictionary" if is_redcap else "CDE spreadsheet"
    doc_type = "dictionary" if is_redcap else "structured_cde"
    return [DocPayload(external_id=ext, title=f"{kind}: {title}", doc_type=doc_type, records=records,
                       meta={"format": kind, "rows": len(records)})]


def _read_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;") if text.strip() else csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _read_xlsx(data: bytes) -> dict[str, list[list[Any]]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        out[ws.title] = [list(r) for r in ws.iter_rows(values_only=True)]
    return out


def parse_nlm_cde_json(objs: list[dict]) -> list[dict]:
    """Normalize NIH CDE Repository (NLM) JSON export objects."""
    records = []
    for o in objs:
        designations = o.get("designations") or []
        name = next((d.get("designation") for d in designations if d.get("designation")), None) or o.get("name")
        question = next(
            (d.get("designation") for d in designations if "Question Text" in (d.get("tags") or [])), None
        )
        definition = next((d.get("definition") for d in (o.get("definitions") or []) if d.get("definition")), None)
        vd = o.get("valueDomain") or {}
        pvs = [
            {
                "code": str(p.get("permissibleValue", "")),
                "label": p.get("valueMeaningName") or str(p.get("permissibleValue", "")),
                **({"concept_system": p.get("codeSystemName"), "concept_code": p.get("valueMeaningCode")}
                   if p.get("valueMeaningCode") else {}),
            }
            for p in vd.get("permissibleValues") or []
        ]
        num = vd.get("datatypeNumber") or {}
        if not name:
            continue
        records.append({
            "name": name,
            "definition": definition,
            "question_text": question,
            "data_type": normalize_data_type(vd.get("datatype"), None, bool(pvs)),
            "unit": vd.get("uom"),
            "min_value": _to_float(num.get("minValue")),
            "max_value": _to_float(num.get("maxValue")),
            "permissible_values": pvs,
            "instrument": (o.get("stewardOrg") or {}).get("name"),
            "external_ref": f"https://cde.nlm.nih.gov/deView?tinyId={o.get('tinyId')}" if o.get("tinyId") else None,
        })
    return records


def _parse_dbgap_xml(data: bytes, stem: str) -> list[DocPayload]:
    root = ElementTree.fromstring(data)
    recs = []
    for var in root.iter("variable"):
        vname = (var.findtext("name") or "").strip()
        desc = (var.findtext("description") or "").strip()
        values = [{"code": v.get("code", ""), "label": (v.text or "").strip()} for v in var.findall("value")]
        recs.append({
            "name": desc or vname,
            "variable_hint": vname,
            "definition": desc or None,
            "data_type": normalize_data_type(var.findtext("type"), None, bool(values)),
            "unit": (var.findtext("unit") or "").strip() or None,
            "min_value": _to_float(var.findtext("logical_min")),
            "max_value": _to_float(var.findtext("logical_max")),
            "permissible_values": values,
            "instrument": root.get("study_id") or stem,
        })
    return [DocPayload(external_id=stem, title=f"dbGaP data dictionary: {stem}", doc_type="dictionary",
                       records=recs, meta={"format": "dbGaP XML", "rows": len(recs)})]


def _read_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError as exc:  # pragma: no cover
        raise ValueError("PDF import needs `pip install pypdf`; or upload the text (.txt) instead.") from exc
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def import_file(filename: str, data: bytes) -> list[DocPayload]:
    """Parse an uploaded file into one or more DocPayloads."""
    stem, ext = Path(filename).stem, Path(filename).suffix.lower()
    if ext in {".xlsx", ".xlsm"}:
        payloads = []
        for sheet, rows in _read_xlsx(data).items():
            payloads += [p for p in _tabular_to_payloads(rows, stem, sheet) if p.records]
        return payloads
    if ext in {".csv", ".tsv"}:
        return _tabular_to_payloads(_read_csv(data), stem)
    if ext == ".json":
        obj = json.loads(data.decode("utf-8-sig"))
        objs = obj if isinstance(obj, list) else obj.get("cdes") or obj.get("elements") or [obj]
        if objs and isinstance(objs[0], dict) and ("designations" in objs[0] or "valueDomain" in objs[0]):
            return [DocPayload(external_id=stem, title=f"NIH CDE Repository export: {stem}", doc_type="structured_cde",
                               records=parse_nlm_cde_json(objs), meta={"format": "NLM CDE JSON"})]
        return [DocPayload(external_id=stem, title=f"CDE JSON: {stem}", doc_type="structured_cde",
                           records=[o for o in objs if isinstance(o, dict) and o.get("name")], meta={"format": "JSON"})]
    if ext == ".xml":
        return _parse_dbgap_xml(data, stem)
    if ext in {".txt", ".md"}:
        return [DocPayload(external_id=stem, title=f"Text: {stem}", doc_type="text",
                           text=data.decode("utf-8", errors="replace"), meta={"format": ext})]
    if ext == ".pdf":
        return [DocPayload(external_id=stem, title=f"PDF: {stem}", doc_type="text", text=_read_pdf_text(data),
                           meta={"format": "pdf"})]
    raise ValueError(f"Unsupported file type: {ext}")
