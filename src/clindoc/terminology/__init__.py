"""Terminology package for M-TERMINOLOGY-SCALE.

Provides offline database schemas, ingestion builders for LOINC and RxNorm Current Prescribable
Content (CPC), FTS5 virtual indexing, and reproducible terminology database compilation.
"""

from clindoc.terminology.compiler import (
    BuildResult,
    TerminologyDatabaseCompiler,
    compute_file_sha256,
    load_active_terminology_pointer,
)
from clindoc.terminology.loinc_builder import (
    LOINC_COPYRIGHT_NOTICE,
    LoincIngestionBuilder,
    ParsedLoincConcept,
    normalize_alias_text,
)
from clindoc.terminology.rxnorm_builder import (
    RXNORM_ATTRIBUTION_NOTICE,
    SUPPORTED_CPC_SABS,
    SUPPORTED_CPC_TTYS,
    ParsedRxNormConcept,
    RxNormIngestionBuilder,
    normalize_rx_text,
)
from clindoc.terminology.schema import (
    FTS5_DDL,
    SCHEMA_DDL,
    TERMINOLOGY_SCHEMA_VERSION,
)
from clindoc.terminology.service import (
    ContextRule,
    ContextStatus,
    QueryContext,
    RetrievalMethod,
    RetrievalResponse,
    RetrievedCandidate,
    TerminologyRetrievalService,
    evaluate_context_rules,
    sanitize_fts5_query,
)

__all__ = [
    "BuildResult",
    "TerminologyDatabaseCompiler",
    "compute_file_sha256",
    "load_active_terminology_pointer",
    "LoincIngestionBuilder",
    "ParsedLoincConcept",
    "normalize_alias_text",
    "LOINC_COPYRIGHT_NOTICE",
    "RxNormIngestionBuilder",
    "ParsedRxNormConcept",
    "normalize_rx_text",
    "RXNORM_ATTRIBUTION_NOTICE",
    "SUPPORTED_CPC_SABS",
    "SUPPORTED_CPC_TTYS",
    "SCHEMA_DDL",
    "FTS5_DDL",
    "TERMINOLOGY_SCHEMA_VERSION",
    "TerminologyRetrievalService",
    "QueryContext",
    "ContextRule",
    "ContextStatus",
    "RetrievalMethod",
    "RetrievedCandidate",
    "RetrievalResponse",
    "evaluate_context_rules",
    "sanitize_fts5_query",
]

