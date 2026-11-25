"""ORM models.

The CDE record is split into two layers, as recommended for building CDEs before
TMD EHR data exist:

* Definition layer  – CDE, PermissibleValue, TerminologyMapping, Provenance.
  Needs no EHR; can be released (v1.0) on its own.
* Implementation layer – Implementation (OMOP / PCORnet / i2b2 / REDCap / NLP bindings)
  plus the TMD computable Phenotype. Filled in and validated once EHR data arrive.
"""
from __future__ import annotations

from typing import Optional

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Controlled vocabularies (kept as plain strings for database portability)
# ---------------------------------------------------------------------------
# Domains follow slide 3 of the TMD CDE framework deck.
CORE_DOMAINS = [
    "Demographics",
    "Diagnoses & Comorbidities",
    "Pain Characteristics",
    "TMD Clinical Features",
    "Functional Outcomes",
]
MODULE_DOMAINS = [
    "Jaw Function",
    "Psychosocial Factors",
    "Sleep",
    "Imaging",
    "Biomarkers",
    "Treatments",
    "Longitudinal Outcomes",
]
DOMAINS = CORE_DOMAINS + MODULE_DOMAINS

TIERS = ["core", "extension"]
CDE_STATUSES = ["draft", "under_review", "approved", "retired"]
DATA_TYPES = ["integer", "float", "text", "date", "boolean", "choice", "multi_choice", "code"]

# How the element is collected in a prospective study
EXPECTED_SOURCES = {
    "pro": "Patient-reported (questionnaire)",
    "exam": "Clinician examination",
    "structured_ehr": "Structured EHR field / code",
    "clinical_note": "Free-text clinical note",
    "imaging": "Imaging (MRI / CBCT / CT)",
    "biomarker": "Biospecimen / QST",
    "derived": "Derived / computed",
}
# How (if at all) the element can be found in routine EHR data
EHR_CAPTURABILITY = {
    "structured": "Structured EHR (codes / fields)",
    "note_nlp": "Clinical notes (needs NLP)",
    "unlikely": "Unlikely in routine EHR",
}
EHR_VALIDATION = ["pending", "validated", "not_applicable"]


class KnowledgeSource(Base):
    __tablename__ = "knowledge_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(64))  # cde_repository | instrument | literature | ...
    url: Mapped[Optional[str]] = mapped_column(String(500))
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    description: Mapped[Optional[str]] = mapped_column(Text)

    documents: Mapped[list["SourceDocument"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class SourceDocument(Base):
    __tablename__ = "source_document"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_source.id"))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    uri: Mapped[Optional[str]] = mapped_column(String(500))
    # structured_cde | dictionary | ehr_dictionary | text
    doc_type: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    relevance: Mapped[float] = mapped_column(Float, default=0.0)
    # new | prioritized | skipped | extracted
    status: Mapped[str] = mapped_column(String(32), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[KnowledgeSource] = relationship(back_populates="documents")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Candidate(Base):
    """A raw CDE candidate produced by the Extraction Agent, before harmonization."""

    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_document.id"))
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pipeline_run.id"))
    name: Mapped[str] = mapped_column(String(300))
    definition: Mapped[Optional[str]] = mapped_column(Text)
    question_text: Mapped[Optional[str]] = mapped_column(Text)
    data_type: Mapped[Optional[str]] = mapped_column(String(32))
    unit: Mapped[Optional[str]] = mapped_column(String(64))
    min_value: Mapped[Optional[float]] = mapped_column(Float)
    max_value: Mapped[Optional[float]] = mapped_column(Float)
    permissible_values: Mapped[list] = mapped_column(JSON, default=list)
    instrument: Mapped[Optional[str]] = mapped_column(String(200))
    domain: Mapped[Optional[str]] = mapped_column(String(64))
    tier_hint: Mapped[Optional[str]] = mapped_column(String(16))
    expected_source: Mapped[Optional[str]] = mapped_column(String(32))
    ehr_capturability: Mapped[Optional[str]] = mapped_column(String(32))
    recall_period: Mapped[Optional[str]] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    method: Mapped[str] = mapped_column(String(16), default="structured")  # structured | llm | rule
    # new | mapped | merged | rejected
    status: Mapped[str] = mapped_column(String(16), default="new")
    cde_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cde.id"))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # mappings, evidence, copyright, variable hint
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[SourceDocument] = relationship(back_populates="candidates")


class CDE(Base):
    """Definition layer of a Common Data Element."""

    __tablename__ = "cde"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_code: Mapped[str] = mapped_column(String(32), unique=True)
    variable_name: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    definition: Mapped[Optional[str]] = mapped_column(Text)
    question_text: Mapped[Optional[str]] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(String(32), default="text")
    unit: Mapped[Optional[str]] = mapped_column(String(64))
    min_value: Mapped[Optional[float]] = mapped_column(Float)
    max_value: Mapped[Optional[float]] = mapped_column(Float)
    instrument: Mapped[Optional[str]] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16), default="extension")
    recall_period: Mapped[Optional[str]] = mapped_column(String(64))
    expected_source: Mapped[Optional[str]] = mapped_column(String(32))
    ehr_capturability: Mapped[Optional[str]] = mapped_column(String(32))
    ehr_validation_status: Mapped[str] = mapped_column(String(32), default="pending")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    version: Mapped[str] = mapped_column(String(16), default="0.1.0")
    qa_score: Mapped[Optional[float]] = mapped_column(Float)
    copyright_note: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    permissible_values: Mapped[list["PermissibleValue"]] = relationship(
        back_populates="cde", cascade="all, delete-orphan", order_by="PermissibleValue.sort_order"
    )
    mappings: Mapped[list["TerminologyMapping"]] = relationship(back_populates="cde", cascade="all, delete-orphan")
    provenance: Mapped[list["Provenance"]] = relationship(back_populates="cde", cascade="all, delete-orphan")
    implementations: Mapped[list["Implementation"]] = relationship(back_populates="cde", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="cde", cascade="all, delete-orphan")
    versions: Mapped[list["CDEVersion"]] = relationship(
        back_populates="cde", cascade="all, delete-orphan", order_by="CDEVersion.id"
    )
    qa_issues: Mapped[list["QAIssue"]] = relationship(back_populates="cde", cascade="all, delete-orphan")


class PermissibleValue(Base):
    __tablename__ = "permissible_value"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    code: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # optional value-level concept (e.g. ICD-10-CM code for one diagnosis option)
    concept_system: Mapped[Optional[str]] = mapped_column(String(32))
    concept_code: Mapped[Optional[str]] = mapped_column(String(64))

    cde: Mapped[CDE] = relationship(back_populates="permissible_values")


class TerminologyMapping(Base):
    __tablename__ = "terminology_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    system: Mapped[str] = mapped_column(String(32))  # ICD10CM | LOINC | SNOMEDCT | HPO | NCIT | CDT | CPT | ATC | RxNorm
    code: Mapped[str] = mapped_column(String(64))  # "" when relation == gap
    display: Mapped[Optional[str]] = mapped_column(String(300))
    relation: Mapped[str] = mapped_column(String(16), default="related")  # exact | broad | narrow | related | gap
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    method: Mapped[str] = mapped_column(String(16), default="seed")  # seed | lexicon | api | llm | manual
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | accepted | rejected
    note: Mapped[Optional[str]] = mapped_column(Text)

    cde: Mapped[CDE] = relationship(back_populates="mappings")


class Provenance(Base):
    __tablename__ = "provenance"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    source_key: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("source_document.id"))
    candidate_id: Mapped[Optional[int]] = mapped_column(ForeignKey("candidate.id"))
    citation: Mapped[Optional[str]] = mapped_column(Text)
    uri: Mapped[Optional[str]] = mapped_column(String(500))

    cde: Mapped[CDE] = relationship(back_populates="provenance")


class Implementation(Base):
    """Implementation (EHR binding) layer: how a CDE is found in a specific data model."""

    __tablename__ = "implementation"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    target: Mapped[str] = mapped_column(String(16))  # OMOP | PCORnet | i2b2 | REDCap | NLP
    table_name: Mapped[Optional[str]] = mapped_column(String(64))
    field_name: Mapped[Optional[str]] = mapped_column(String(128))
    value_set: Mapped[list] = mapped_column(JSON, default=list)  # [{system, code, display}]
    nlp_terms: Mapped[list] = mapped_column(JSON, default=list)
    logic: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | validated
    note: Mapped[Optional[str]] = mapped_column(Text)

    cde: Mapped[CDE] = relationship(back_populates="implementations")


class Review(Base):
    """One expert rating in a Delphi round (1-9 scale for importance and feasibility)."""

    __tablename__ = "review"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    round: Mapped[int] = mapped_column(Integer, default=1)
    reviewer: Mapped[str] = mapped_column(String(120))
    role: Mapped[Optional[str]] = mapped_column(String(120))
    importance: Mapped[int] = mapped_column(Integer)
    feasibility: Mapped[int] = mapped_column(Integer)  # feasible to capture in routine care / EHR
    comment: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cde: Mapped[CDE] = relationship(back_populates="reviews")


class CDEVersion(Base):
    __tablename__ = "cde_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    version: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    change_note: Mapped[Optional[str]] = mapped_column(Text)
    author: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cde: Mapped[CDE] = relationship(back_populates="versions")


class QAIssue(Base):
    __tablename__ = "qa_issue"

    id: Mapped[int] = mapped_column(primary_key=True)
    cde_id: Mapped[int] = mapped_column(ForeignKey("cde.id"))
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pipeline_run.id"))
    severity: Mapped[str] = mapped_column(String(16))  # error | warning | info
    rule: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    cde: Mapped[CDE] = relationship(back_populates="qa_issues")


class Phenotype(Base):
    """Draft computable phenotype (e.g. TMD cohort definition) prepared before EHR access."""

    __tablename__ = "phenotype"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[str] = mapped_column(String(16), default="0.1.0")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued | running | completed | needs_review | failed
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)

    logs: Mapped[list["AgentLog"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="AgentLog.id")


class AgentLog(Base):
    __tablename__ = "agent_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_run.id"))
    agent: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[PipelineRun] = relationship(back_populates="logs")
