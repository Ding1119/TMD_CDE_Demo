"""Public EHR data-model dictionaries (no patient data).

Used by the Phenotype / EHR-readiness Agent to choose binding targets for the
implementation layer. Only the tables/fields relevant to TMD CDEs are listed.
OMOP CDM v5.4: https://ohdsi.github.io/CommonDataModel/  PCORnet CDM: https://pcornet.org/data/
"""
from __future__ import annotations

from .base import DocPayload

# system of a terminology mapping -> (OMOP table, OMOP field, PCORnet table, PCORnet field)
OMOP_TARGETS = {
    "ICD10CM": ("condition_occurrence", "condition_source_value", "DIAGNOSIS", "DX"),
    "SNOMEDCT": ("condition_occurrence", "condition_concept_id", "CONDITION", "CONDITION"),
    "LOINC": ("measurement", "measurement_source_value", "OBS_CLIN", "OBSCLIN_CODE"),
    "CDT": ("procedure_occurrence", "procedure_source_value", "PROCEDURES", "PX"),
    "CPT": ("procedure_occurrence", "procedure_source_value", "PROCEDURES", "PX"),
    "ATC": ("drug_exposure", "drug_concept_id (via ATC ancestor)", "PRESCRIBING", "RXNORM_CUI"),
    "RxNorm": ("drug_exposure", "drug_source_value", "PRESCRIBING", "RXNORM_CUI"),
}

DEMOGRAPHIC_TARGETS = {
    "age_enroll": ("person", "year_of_birth / birth_datetime", "DEMOGRAPHIC", "BIRTH_DATE"),
    "sex_at_birth": ("person", "gender_source_value", "DEMOGRAPHIC", "SEX"),
    "gender_identity": ("observation", "observation_source_value", "DEMOGRAPHIC", "GENDER_IDENTITY"),
    "race_ethnicity": ("person", "race_source_value / ethnicity_source_value", "DEMOGRAPHIC", "RACE / HISPANIC"),
}

OMOP_TABLES = {
    "person": ["person_id", "gender_concept_id", "year_of_birth", "birth_datetime", "race_concept_id",
               "ethnicity_concept_id", "gender_source_value", "race_source_value", "ethnicity_source_value"],
    "condition_occurrence": ["condition_occurrence_id", "person_id", "condition_concept_id", "condition_start_date",
                             "condition_source_value", "condition_source_concept_id"],
    "procedure_occurrence": ["procedure_occurrence_id", "person_id", "procedure_concept_id", "procedure_date",
                             "procedure_source_value", "procedure_source_concept_id"],
    "drug_exposure": ["drug_exposure_id", "person_id", "drug_concept_id", "drug_exposure_start_date",
                      "drug_source_value", "quantity", "days_supply"],
    "measurement": ["measurement_id", "person_id", "measurement_concept_id", "measurement_date", "value_as_number",
                    "unit_concept_id", "measurement_source_value"],
    "observation": ["observation_id", "person_id", "observation_concept_id", "observation_date", "value_as_number",
                    "value_as_string", "value_as_concept_id", "observation_source_value"],
    "note": ["note_id", "person_id", "note_date", "note_type_concept_id", "note_title", "note_text"],
    "note_nlp": ["note_nlp_id", "note_id", "lexical_variant", "note_nlp_concept_id", "snippet", "term_exists",
                 "term_temporal", "term_modifiers"],
}

PCORNET_TABLES = {
    "DEMOGRAPHIC": ["PATID", "BIRTH_DATE", "SEX", "GENDER_IDENTITY", "HISPANIC", "RACE"],
    "DIAGNOSIS": ["DIAGNOSISID", "PATID", "ENCOUNTERID", "DX", "DX_TYPE", "DX_DATE", "DX_SOURCE"],
    "PROCEDURES": ["PROCEDURESID", "PATID", "PX", "PX_TYPE", "PX_DATE"],
    "PRESCRIBING": ["PRESCRIBINGID", "PATID", "RXNORM_CUI", "RX_START_DATE", "RX_DOSE_ORDERED"],
    "OBS_CLIN": ["OBSCLINID", "PATID", "OBSCLIN_CODE", "OBSCLIN_TYPE", "OBSCLIN_RESULT_NUM", "OBSCLIN_START_DATE"],
    "PRO_CM": ["PRO_CM_ID", "PATID", "PRO_CODE", "PRO_TYPE", "PRO_RESPONSE_NUM", "PRO_DATE"],
    "CONDITION": ["CONDITIONID", "PATID", "CONDITION", "CONDITION_TYPE", "ONSET_DATE"],
}


def ehr_cdm_payloads() -> list[DocPayload]:
    omop_records = [{"table": t, "fields": f} for t, f in OMOP_TABLES.items()]
    pcornet_records = [{"table": t, "fields": f} for t, f in PCORNET_TABLES.items()]
    return [
        DocPayload(external_id="omop-cdm-v5.4", title="OMOP CDM v5.4 - tables used for TMD CDE bindings",
                   uri="https://ohdsi.github.io/CommonDataModel/cdm54.html", doc_type="ehr_dictionary",
                   records=omop_records, meta={"model": "OMOP"}),
        DocPayload(external_id="pcornet-cdm", title="PCORnet CDM - tables used for TMD CDE bindings",
                   uri="https://pcornet.org/data/", doc_type="ehr_dictionary", records=pcornet_records,
                   meta={"model": "PCORnet"}),
    ]
