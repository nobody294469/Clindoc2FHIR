"""Terminology database schema definitions and constants for M-TERMINOLOGY-SCALE.

Defines the normalized relational tables, FTS5 external-content virtual tables,
and metadata tables for offline LOINC, CLCI, CDCI, and RxNorm terminology.
"""

from __future__ import annotations

# Current schema version
TERMINOLOGY_SCHEMA_VERSION = "2.0.0"

# DDL for Authoritative Relational Tables and Metadata
SCHEMA_DDL = """
-- 1. Concepts Master Table (One row per unique code in standard system)
CREATE TABLE IF NOT EXISTS concepts (
    concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL,              -- 'LOINC', 'CDCI', 'RxNorm'
    code TEXT NOT NULL,                -- e.g. '2823-3', '723', CDCI ID
    canonical_name TEXT NOT NULL,      -- Formal release display string
    tty TEXT,                          -- RxNorm TTY, CDCI TTY ('SUBSTANCE', 'GENERIC', 'BRAND'), or LOINC Scale
    class_type INTEGER,                -- LOINC: 1=Lab, 2=Clinical, 3=Claims, 4=Surveys; NULL for medications
    class TEXT,                        -- LOINC: 'CHEM', 'HEM/BC', 'MICRO', 'DOC', etc.
    status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'TRIAL', 'DISCOURAGED', 'DEPRECATED'
    attributes_json TEXT NOT NULL      -- Structured JSON representing LOINC 6 axes or CDCI/RxNorm descriptors
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_concepts_system_code ON concepts(system, code);
CREATE INDEX IF NOT EXISTS idx_concepts_system_tty ON concepts(system, tty);
CREATE INDEX IF NOT EXISTS idx_concepts_system_domain ON concepts(system, class_type);
CREATE INDEX IF NOT EXISTS idx_concepts_system_status ON concepts(system, status);
CREATE INDEX IF NOT EXISTS idx_concepts_system_class ON concepts(system, class);

-- 2. Master Aliases Table (Normalized alias strings, provenance, and flags)
CREATE TABLE IF NOT EXISTS concept_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    alias_text TEXT NOT NULL,          -- Normalized search token (for backwards compat with existing queries)
    raw_display_text TEXT NOT NULL DEFAULT '',    -- Verbatim source string ("Augmentin® 625 Duo")
    search_text TEXT NOT NULL DEFAULT '',         -- Normalized search token ("augmentin 625 duo")
    language TEXT NOT NULL DEFAULT 'en', -- ISO language code
    alias_source TEXT NOT NULL,        -- 'LOINC_CORE', 'CDCI_BRAND_NAME', etc.
    source_identifier TEXT,            -- Source atom ID (e.g. RXAUI, CDCI Trade ID)
    is_abbreviation BOOLEAN NOT NULL DEFAULT 0,
    is_preferred BOOLEAN NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_aliases_concept_id ON concept_aliases(concept_id);
CREATE INDEX IF NOT EXISTS idx_aliases_text ON concept_aliases(alias_text);
CREATE INDEX IF NOT EXISTS idx_aliases_search_text ON concept_aliases(search_text);

-- 3. Normalized Contextual Restrictions Table (0..N restrictions per alias)
CREATE TABLE IF NOT EXISTS alias_context_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_id INTEGER NOT NULL REFERENCES concept_aliases(alias_id) ON DELETE CASCADE,
    dimension TEXT NOT NULL,           -- 'SPECIMEN', 'SCALE', 'ROUTE', 'DOMAIN', 'DISALLOWED_CONTEXT'
    operator TEXT NOT NULL,            -- 'IN', 'EQUALS', 'NOT_IN'
    allowed_values_json TEXT NOT NULL  -- JSON array of allowed/forbidden string tokens
);

CREATE INDEX IF NOT EXISTS idx_rules_alias_id ON alias_context_rules(alias_id);

-- 4. Authoritative Concept Relationships Table (Structural Graph)
CREATE TABLE IF NOT EXISTS concept_relationships (
    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id_from INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    concept_id_to INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,    -- 'HAS_SUBSTANCE', 'HAS_DOSE_FORM', 'HAS_ROUTE', 'TRADENAME_OF', 'MANUFACTURED_BY'
    relationship_source TEXT NOT NULL,  -- 'CDCI_FLAT_FILES', 'RXNORM_RXNREL', etc.
    source_relationship_id TEXT,        -- Original identifier from source archive
    attributes_json TEXT                -- Edge metadata (strength numerator/denominator, etc.)
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON concept_relationships(concept_id_from, relationship_type);
CREATE INDEX IF NOT EXISTS idx_rel_to ON concept_relationships(concept_id_to, relationship_type);

-- 5. Cross-Source Concept Mappings Table
CREATE TABLE IF NOT EXISTS concept_mappings (
    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,          -- 'CLCI', 'CDCI'
    source_code TEXT NOT NULL,            -- '6025-1' or CDCI ID
    target_system TEXT NOT NULL,          -- 'LOINC', 'RxNorm'
    target_code TEXT NOT NULL,            -- LOINC code or RxCUI
    relationship_type TEXT NOT NULL,      -- 'EQUIVALENT', 'BROADER', 'NARROWER', 'RELATED', 'UNRESOLVED'
    mapping_source TEXT NOT NULL,         -- 'NRCES_OFFICIAL_MAP', 'CLCI_RELEASE_20260629', etc.
    evidence_json TEXT NOT NULL,          -- Audit trail
    release_id TEXT NOT NULL,             -- Mapping release identifier
    review_status TEXT NOT NULL,          -- 'OFFICIAL', 'HUMAN_VERIFIED', 'PROVISIONAL'
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_src ON concept_mappings(source_system, source_code);
CREATE INDEX IF NOT EXISTS idx_map_tgt ON concept_mappings(target_system, target_code);

-- 6. Publication and Provenance Metadata Table
CREATE TABLE IF NOT EXISTS terminology_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# DDL for FTS5 External Content Virtual Tables
FTS5_DDL = """
-- Primary Word-Level BM25 External-Content Table over concept_aliases
CREATE VIRTUAL TABLE IF NOT EXISTS fts_aliases_unicode USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Secondary Substring / OCR Trigram External-Content Table over concept_aliases
CREATE VIRTUAL TABLE IF NOT EXISTS fts_aliases_trigram USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='trigram'
);
"""
