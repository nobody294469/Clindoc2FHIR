"""LOINC distribution parser and SQLite compiler for M-TERMINOLOGY-SCALE.

Parses official LOINC distribution tables (Loinc.csv, LoincUniversalLabOrdersValueSet.csv,
or curated LOINC distribution subsets) and NRCeS Common Lab Codes for India (CLCI),
preserving all 6 axes, component, system/specimen, scale, method, related aliases,
domain classifications, source statuses, and EXTERNAL_COPYRIGHT_NOTICE metadata.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Required attribution statement per Regenstrief Institute LOINC License
LOINC_COPYRIGHT_NOTICE = (
    "This material contains content from LOINC (http://loinc.org). "
    "LOINC is copyright © 1995-2026, Regenstrief Institute, Inc. and the Logical "
    "Observation Identifiers Names and Codes (LOINC) Committee and is available "
    "at no cost under the license at http://loinc.org/license. "
    "LOINC® is a registered United States trademark of Regenstrief Institute, Inc."
)

# Official NRCeS CLCI Attribution Notice
CLCI_ATTRIBUTION_NOTICE = (
    "Common Lab Codes for India (CLCI) copyright © 2026, Centre for Development of Advanced Computing (C-DAC), Pune. "
    "Released by National Resource Centre for EHR Standards (NRCeS) at C-DAC Pune. "
    "Contains suggested mappings to LOINC. LOINC is copyright © Regenstrief Institute, Inc."
)


@dataclass
class ParsedLoincConcept:
    """Represents a validated LOINC concept and its associated aliases and axes."""
    code: str
    canonical_name: str
    component: str
    property: str
    time_aspect: str
    system: str
    scale: str
    method: Optional[str]
    status: str
    class_type: Optional[int] = None
    class_name: Optional[str] = None
    default_unit: Optional[str] = None
    example_ucum_units: Optional[str] = None
    external_copyright_notice: Optional[str] = None
    aliases: List[Tuple[str, str, bool]] = field(default_factory=list)  # (alias_text, source, is_abbr)
    rules: List[Tuple[str, str, List[str]]] = field(default_factory=list)  # (dimension, operator, allowed_values)


def normalize_alias_text(text: str) -> str:
    """Normalizes an alias string for indexing (lowercased, standardized spaces, stripped)."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[\s\t\n]+", " ", text)
    return text


class LoincIngestionBuilder:
    """Ingests LOINC distribution records and CLCI overlays into the ClinicalDoc terminology database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.cursor = connection.cursor()
        self.total_processed = 0
        self.concepts_inserted = 0
        self.aliases_inserted = 0
        self.rules_inserted = 0
        self.excluded_count = 0
        self.exclusion_reasons: Dict[str, int] = {}
        self.concepts_by_status: Dict[str, int] = {}
        self.concepts_by_classtype: Dict[int, int] = {}

    def _record_exclusion(self, reason: str) -> None:
        self.excluded_count += 1
        self.exclusion_reasons[reason] = self.exclusion_reasons.get(reason, 0) + 1

    def parse_loinc_csv_row(self, row: Dict[str, str], filter_inactive: bool = True) -> Optional[ParsedLoincConcept]:
        """Parses a single row from official Loinc.csv or a LOINC extract."""
        code = row.get("LOINC_NUM") or row.get("loinc_num") or row.get("loinc_code") or row.get("code")
        if not code or not code.strip():
            self._record_exclusion("MISSING_LOINC_CODE")
            return None

        code = code.strip()

        status = (row.get("STATUS") or row.get("status") or "ACTIVE").strip().upper()
        if filter_inactive and status in ("DEPRECATED", "DISCOURAGED", "DELETED"):
            self._record_exclusion(f"INACTIVE_STATUS_{status}")
            return None

        # Extract axes
        component = (row.get("COMPONENT") or row.get("component") or "").strip()
        prop = (row.get("PROPERTY") or row.get("property") or "").strip()
        time_asp = (row.get("TIME_ASPCT") or row.get("time_aspect") or "Pt").strip()
        system = (row.get("SYSTEM") or row.get("system") or "").strip()
        scale = (row.get("SCALE_TYP") or row.get("scale") or "").strip()
        method = (row.get("METHOD_TYP") or row.get("method") or "").strip() or None

        long_common_name = (row.get("LONG_COMMON_NAME") or row.get("long_common_name") or "").strip()
        short_name = (row.get("SHORTNAME") or row.get("shortname") or "").strip()
        display_name = (row.get("DisplayName") or "").strip()
        canonical_name = long_common_name or (row.get("canonical_name") or component or code)

        ext_copyright = (row.get("EXTERNAL_COPYRIGHT_NOTICE") or row.get("external_copyright_notice") or "").strip() or None
        default_unit = (row.get("EXAMPLE_UNITS") or row.get("default_unit") or "").strip() or None
        ucum_unit = (row.get("EXAMPLE_UCUM_UNITS") or "").strip() or None

        class_name = (row.get("CLASS") or row.get("class_name") or "").strip() or None
        raw_classtype = (row.get("CLASSTYPE") or row.get("class_type") or "").strip()
        class_type_int = None
        if raw_classtype.isdigit():
            class_type_int = int(raw_classtype)

        concept = ParsedLoincConcept(
            code=code,
            canonical_name=canonical_name,
            component=component,
            property=prop,
            time_aspect=time_asp,
            system=system,
            scale=scale,
            method=method,
            status=status,
            class_type=class_type_int,
            class_name=class_name,
            default_unit=default_unit,
            example_ucum_units=ucum_unit,
            external_copyright_notice=ext_copyright,
        )

        # Collect unique aliases
        aliases_seen: Set[str] = set()

        def add_alias(raw_alias: str, source: str, is_abbr: bool = False):
            norm = normalize_alias_text(raw_alias)
            if norm and len(norm) >= 1 and norm not in aliases_seen:
                aliases_seen.add(norm)
                concept.aliases.append((norm, source, is_abbr, raw_alias.strip()))

        # 1. Canonical and component aliases
        add_alias(canonical_name, "LOINC_LONG_COMMON_NAME", is_abbr=False)
        if component and component != canonical_name:
            add_alias(component, "LOINC_COMPONENT", is_abbr=False)

        # 2. Shortname alias
        if short_name and short_name != canonical_name:
            is_abbr = len(short_name) <= 6
            add_alias(short_name, "LOINC_SHORTNAME", is_abbr=is_abbr)

        # 3. Display name
        if display_name and display_name != canonical_name:
            add_alias(display_name, "LOINC_DISPLAY_NAME", is_abbr=False)

        # 4. RelatedNames2 pipe-delimited synonyms (Official LOINC column)
        related_names = row.get("RELATEDNAMES2") or row.get("related_names") or ""
        if related_names:
            for item in related_names.split(";"):
                for subitem in item.split("|"):
                    s = subitem.strip()
                    if s:
                        add_alias(s, "LOINC_RELATEDNAMES2", is_abbr=(len(s) <= 4 and s.isupper()))

        # 5. Consumer name if present
        consumer_name = (row.get("CONSUMER_NAME") or "").strip()
        if consumer_name:
            add_alias(consumer_name, "LOINC_CONSUMER_NAME", is_abbr=False)

        # Generate Context Rules based on Axis Metadata
        if system:
            concept.rules.append(("SPECIMEN", "IN", [system]))
        if scale:
            concept.rules.append(("SCALE", "EQUALS", [scale]))

        return concept

    def insert_concept(self, concept: ParsedLoincConcept) -> int:
        """Inserts a single parsed LOINC concept, its aliases, and rules into the database."""
        attributes = {
            "component": concept.component,
            "property": concept.property,
            "time_aspect": concept.time_aspect,
            "system": concept.system,
            "scale": concept.scale,
            "method": concept.method,
            "status": concept.status,
            "class_type": concept.class_type,
            "class": concept.class_name,
            "default_unit": concept.default_unit,
            "example_ucum_units": concept.example_ucum_units,
            "external_copyright_notice": concept.external_copyright_notice,
        }

        self.cursor.execute(
            "SELECT concept_id FROM concepts WHERE system = ? AND code = ?",
            ("LOINC", concept.code),
        )
        row = self.cursor.fetchone()
        if row:
            concept_id = row[0]
        else:
            self.cursor.execute(
                """
                INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "LOINC",
                    concept.code,
                    concept.canonical_name,
                    concept.scale or "Qn",
                    concept.class_type,
                    concept.class_name,
                    concept.status,
                    json.dumps(attributes),
                ),
            )
            concept_id = self.cursor.lastrowid
            self.concepts_inserted += 1

        # Insert aliases and attach rules
        for item in concept.aliases:
            norm_alias = item[0]
            source = item[1]
            is_abbr = item[2]
            raw_alias = item[3] if len(item) > 3 else norm_alias
            self.cursor.execute(
                "SELECT alias_id FROM concept_aliases WHERE concept_id = ? AND alias_text = ?",
                (concept_id, norm_alias),
            )
            alias_row = self.cursor.fetchone()
            if alias_row:
                continue

            is_pref = 1 if norm_alias == normalize_alias_text(concept.canonical_name) else 0
            self.cursor.execute(
                """
                INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
                VALUES (?, ?, ?, ?, 'en', ?, ?, ?, ?)
                """,
                (concept_id, norm_alias, raw_alias, norm_alias, source, concept.code, 1 if is_abbr else 0, is_pref),
            )
            alias_id = self.cursor.lastrowid
            self.aliases_inserted += 1

            # Insert rules for this alias if concept has constraints
            for dim, op, vals in concept.rules:
                self.cursor.execute(
                    """
                    INSERT INTO alias_context_rules (alias_id, dimension, operator, allowed_values_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (alias_id, dim, op, json.dumps(vals)),
                )
                self.rules_inserted += 1

        return concept_id

    def ingest_full_release_zip(self, loinc_zip_path: Path) -> Dict[str, Any]:
        """High-performance batch ingestion of official Loinc_2.83.zip distribution."""
        start_time = time.perf_counter()
        loinc_zip_path = Path(loinc_zip_path)

        with zipfile.ZipFile(loinc_zip_path, "r") as zf:
            # Locate LoincTable/Loinc.csv
            csv_entry = None
            for name in zf.namelist():
                if name.replace("\\", "/").endswith("LoincTable/Loinc.csv") or name == "LoincTable/Loinc.csv":
                    csv_entry = name
                    break
            if not csv_entry:
                # Fallback to any Loinc.csv
                for name in zf.namelist():
                    if name.endswith("Loinc.csv"):
                        csv_entry = name
                        break
            if not csv_entry:
                raise FileNotFoundError("Could not find Loinc.csv inside LOINC zip archive.")

            logger.info(f"Opening LOINC release table: {csv_entry}")
            with zf.open(csv_entry) as f:
                wrapper = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                reader = csv.DictReader(wrapper)
                self._batch_ingest_loinc_rows(reader)

        duration = time.perf_counter() - start_time
        logger.info(
            f"LOINC Full Release Ingestion Complete: {self.concepts_inserted} concepts, "
            f"{self.aliases_inserted} aliases, {self.rules_inserted} rules in {duration:.2f}s"
        )
        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "aliases_inserted": self.aliases_inserted,
            "rules_inserted": self.rules_inserted,
            "concepts_by_status": self.concepts_by_status,
            "concepts_by_classtype": self.concepts_by_classtype,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
            "duration_sec": duration,
        }

    def _batch_ingest_loinc_rows(self, reader: Iterator[Dict[str, str]]) -> None:
        """Processes LOINC rows using chunked batch transactions for high throughput."""
        batch_size = 10000
        concepts_batch = []
        parsed_concepts: List[ParsedLoincConcept] = []

        for row in reader:
            self.total_processed += 1
            concept = self.parse_loinc_csv_row(row, filter_inactive=False)
            if not concept:
                continue

            self.concepts_by_status[concept.status] = self.concepts_by_status.get(concept.status, 0) + 1
            if concept.class_type is not None:
                self.concepts_by_classtype[concept.class_type] = (
                    self.concepts_by_classtype.get(concept.class_type, 0) + 1
                )

            attribs = {
                "component": concept.component,
                "property": concept.property,
                "time_aspect": concept.time_aspect,
                "system": concept.system,
                "scale": concept.scale,
                "method": concept.method,
                "status": concept.status,
                "class_type": concept.class_type,
                "class": concept.class_name,
                "default_unit": concept.default_unit,
                "example_ucum_units": concept.example_ucum_units,
                "external_copyright_notice": concept.external_copyright_notice,
            }

            concepts_batch.append((
                "LOINC",
                concept.code,
                concept.canonical_name,
                concept.scale or "Qn",
                concept.class_type,
                concept.class_name,
                concept.status,
                json.dumps(attribs),
            ))
            parsed_concepts.append(concept)

            if len(concepts_batch) >= batch_size:
                self._flush_concepts_batch(concepts_batch)
                concepts_batch = []

        if concepts_batch:
            self._flush_concepts_batch(concepts_batch)

        # Retrieve concept_ids for all LOINC codes
        self.cursor.execute("SELECT code, concept_id FROM concepts WHERE system = 'LOINC'")
        code_to_id = {row[0]: row[1] for row in self.cursor.fetchall()}

        # Now batch insert aliases and rules
        aliases_batch = []
        rules_batch = []
        alias_id_counter = 0

        # Query existing max alias_id
        self.cursor.execute("SELECT COALESCE(MAX(alias_id), 0) FROM concept_aliases")
        alias_id_counter = self.cursor.fetchone()[0]

        for concept in parsed_concepts:
            cid = code_to_id.get(concept.code)
            if not cid:
                continue

            for item in concept.aliases:
                norm_alias = item[0]
                source = item[1]
                is_abbr = item[2]
                raw_alias = item[3] if len(item) > 3 else norm_alias
                is_pref = 1 if norm_alias == normalize_alias_text(concept.canonical_name) else 0
                alias_id_counter += 1

                aliases_batch.append((
                    alias_id_counter,
                    cid,
                    norm_alias,
                    raw_alias,
                    norm_alias,
                    "en",
                    source,
                    concept.code,
                    1 if is_abbr else 0,
                    is_pref,
                ))

                # Attach rules to abbreviations and short acronyms requiring contextual disambiguation
                if is_abbr or len(norm_alias) <= 8 or source == "LOINC_SHORTNAME":
                    for dim, op, vals in concept.rules:
                        rules_batch.append((
                            alias_id_counter,
                            dim,
                            op,
                            json.dumps(vals),
                        ))

            if len(aliases_batch) >= 50000:
                self._flush_aliases_batch(aliases_batch)
                aliases_batch = []
                if rules_batch:
                    self._flush_rules_batch(rules_batch)
                    rules_batch = []

        if aliases_batch:
            self._flush_aliases_batch(aliases_batch)
        if rules_batch:
            self._flush_rules_batch(rules_batch)
        self.conn.commit()

    def _flush_concepts_batch(self, batch: List[Tuple]) -> None:
        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        self.concepts_inserted += len(batch)

    def _flush_aliases_batch(self, batch: List[Tuple]) -> None:
        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (alias_id, concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        self.aliases_inserted += len(batch)

    def _flush_rules_batch(self, batch: List[Tuple]) -> None:
        self.cursor.executemany(
            """
            INSERT INTO alias_context_rules (alias_id, dimension, operator, allowed_values_json)
            VALUES (?, ?, ?, ?)
            """,
            batch,
        )
        self.rules_inserted += len(batch)

    def ingest_clci(self, clci_zip_or_csv: Path) -> Dict[str, Any]:
        """Ingests NRCeS Common Lab Codes for India (CLCI) as an Indian curated overlay."""
        start_time = time.perf_counter()
        clci_zip_or_csv = Path(clci_zip_or_csv)

        # Get existing LOINC concepts mapping
        self.cursor.execute("SELECT code, concept_id, attributes_json FROM concepts WHERE system = 'LOINC'")
        loinc_map = {row[0]: (row[1], row[2]) for row in self.cursor.fetchall()}

        mappings_to_insert = []
        aliases_to_insert = []
        concepts_to_update = []
        clci_rows_count = 0
        mapped_count = 0
        unmapped_count = 0

        created_timestamp = datetime.now(timezone.utc).isoformat()

        def process_reader(reader):
            nonlocal clci_rows_count, mapped_count, unmapped_count
            for row in reader:
                if not row or not row.get("LOINC Code"):
                    continue
                clci_rows_count += 1
                loinc_code = row["LOINC Code"].strip()
                gen_name = (row.get("General Name") or "").strip()
                fsn = (row.get("Fully-Specified Name (FSN)") or "").strip()
                lcn = (row.get("Long Common Name") or "").strip()

                if not loinc_code:
                    unmapped_count += 1
                    continue

                loinc_entry = loinc_map.get(loinc_code)
                if not loinc_entry:
                    unmapped_count += 1
                    continue

                mapped_count += 1
                cid, orig_attribs_json = loinc_entry

                # 1. Concept Mapping record
                evidence = {
                    "general_name": gen_name,
                    "fsn": fsn,
                    "long_common_name": lcn,
                }
                mappings_to_insert.append((
                    "CLCI",
                    loinc_code,
                    "LOINC",
                    loinc_code,
                    "EQUIVALENT",
                    "NRCES_CLCI_20260629",
                    json.dumps(evidence),
                    "20260629",
                    "OFFICIAL",
                    created_timestamp,
                ))

                # 2. General Name alias
                if gen_name:
                    norm_gen = normalize_alias_text(gen_name)
                    aliases_to_insert.append((
                        cid,
                        norm_gen,
                        gen_name,
                        norm_gen,
                        "en",
                        "CLCI_GENERAL_NAME",
                        loinc_code,
                        0,
                        0,
                    ))

                # 3. Update attributes_json to include in_clci: True
                try:
                    attribs = json.loads(orig_attribs_json)
                except Exception:
                    attribs = {}
                attribs["in_clci"] = True
                concepts_to_update.append((json.dumps(attribs), cid))

        if clci_zip_or_csv.is_file() and clci_zip_or_csv.suffix.lower() == ".zip":
            with zipfile.ZipFile(clci_zip_or_csv, "r") as zf:
                csv_entry = None
                for name in zf.namelist():
                    if name.endswith("common-lab-codes-for-india.csv"):
                        csv_entry = name
                        break
                if not csv_entry:
                    raise FileNotFoundError("Could not find common-lab-codes-for-india.csv in CLCI zip.")
                with zf.open(csv_entry) as f:
                    wrapper = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                    reader = csv.DictReader(wrapper)
                    process_reader(reader)
        else:
            with open(clci_zip_or_csv, "r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                process_reader(reader)

        # Batch insert mappings
        self.cursor.executemany(
            """
            INSERT INTO concept_mappings (source_system, source_code, target_system, target_code, relationship_type, mapping_source, evidence_json, release_id, review_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            mappings_to_insert,
        )

        # Batch insert aliases
        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            aliases_to_insert,
        )

        # Update concept attributes
        self.cursor.executemany(
            "UPDATE concepts SET attributes_json = ? WHERE concept_id = ?",
            concepts_to_update,
        )
        self.conn.commit()

        self.aliases_inserted += len(aliases_to_insert)
        duration = time.perf_counter() - start_time
        logger.info(
            f"CLCI Ingestion Complete: {clci_rows_count} rows, {mapped_count} mapped to LOINC, "
            f"{len(aliases_to_insert)} aliases in {duration:.2f}s"
        )

        return {
            "clci_rows_count": clci_rows_count,
            "mapped_count": mapped_count,
            "unmapped_count": unmapped_count,
            "mappings_inserted": len(mappings_to_insert),
            "aliases_inserted": len(aliases_to_insert),
            "duration_sec": duration,
        }

    def ingest_csv(self, csv_file_or_path: Any, delimiter: str = ",") -> Dict[str, Any]:
        """Ingests rows from a CSV reader or file path."""
        if isinstance(csv_file_or_path, (str, Path)):
            with open(csv_file_or_path, "r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                for row in reader:
                    self.total_processed += 1
                    concept = self.parse_loinc_csv_row(row, filter_inactive=True)
                    if concept:
                        self.insert_concept(concept)
        else:
            reader = csv.DictReader(csv_file_or_path, delimiter=delimiter)
            for row in reader:
                self.total_processed += 1
                concept = self.parse_loinc_csv_row(row, filter_inactive=True)
                if concept:
                    self.insert_concept(concept)

        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "aliases_inserted": self.aliases_inserted,
            "rules_inserted": self.rules_inserted,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
        }

    def ingest_loinc_subset_json(self, json_path: Path) -> Dict[str, Any]:
        """Ingests from a curated LOINC subset JSON file (e.g. configs/loinc_subset.json)."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for surface_name, record in data.items():
            self.total_processed += 1
            code = record.get("loinc_code") or record.get("code")
            if not code:
                self._record_exclusion("MISSING_LOINC_CODE")
                continue

            canon = record.get("canonical_name", surface_name)
            system = record.get("system") or record.get("specimen") or "Ser/Plas"
            scale = record.get("scale") or "Qn"
            default_unit = record.get("default_unit")

            concept = ParsedLoincConcept(
                code=code,
                canonical_name=canon,
                component=surface_name,
                property=record.get("property", "MCnc"),
                time_aspect=record.get("time_aspect", "Pt"),
                system=system,
                scale=scale,
                method=record.get("method"),
                status="ACTIVE",
                default_unit=default_unit,
            )

            # Add surface name and canonical name aliases
            concept.aliases.append((canon, "LOINC_CANONICAL", False))
            if surface_name != canon:
                concept.aliases.append((surface_name, "LOINC_SURFACE", len(surface_name) <= 4))

            # Context rules
            concept.rules.append(("SPECIMEN", "IN", [system]))
            concept.rules.append(("SCALE", "EQUALS", [scale]))

            self.insert_concept(concept)

        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "aliases_inserted": self.aliases_inserted,
            "rules_inserted": self.rules_inserted,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
        }
