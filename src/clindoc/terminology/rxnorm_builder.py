"""RxNorm Current Prescribable Content (CPC) ingestion builder for M-TERMINOLOGY-SCALE.

Parses official NLM RxNorm Current Prescribable Content (CPC) files (RXNCONSO.RRF and RXNREL.RRF),
enforcing strict inclusion of pure prescribable NLM concepts (SAB in ('RXNORM', 'MTHSPL'))
and supported prescribable Term Types, while excluding proprietary third-party commercial databases
and non-drug CMS formulary files (MTHCMSFRF).
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
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Official NLM RxNorm attribution notice
RXNORM_ATTRIBUTION_NOTICE = (
    "This product uses publicly available data courtesy of the U.S. National Library of Medicine (NLM), "
    "National Institutes of Health, Department of Health and Human Services. NLM is not responsible "
    "for the product and does not endorse or recommend this or any other product."
)

# Supported Prescribable Term Types (TTY) per NLM RxNorm documentation
SUPPORTED_CPC_TTYS = {
    "IN",     # Ingredient
    "PIN",    # Precise Ingredient
    "MIN",    # Multi-Ingredient
    "SCDC",   # Semantic Clinical Drug Component (Ingredient + Strength)
    "SCD",    # Semantic Clinical Drug (Ingredient + Strength + Dose Form)
    "SBD",    # Semantic Branded Drug (Brand Name + Strength + Dose Form)
    "DF",     # Dose Form
    "GPCK",   # Generic Pack
    "BPCK",   # Brand Pack
}

# Supported Source Vocabularies in Prescribable Content (Strict CPC)
SUPPORTED_CPC_SABS = {"RXNORM", "MTHSPL"}


def normalize_rx_text(text: str) -> str:
    """Normalizes medication string (lowercased, clean spaces, stripped)."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[\s\t\n]+", " ", text)
    return text


@dataclass
class ParsedRxNormConcept:
    """Represents an ingested RxNorm prescribable concept."""
    rxcui: str
    canonical_name: str
    tty: str
    sab: str
    suppress: str = "N"
    attributes: Dict[str, Any] = field(default_factory=dict)
    aliases: List[Tuple[str, str, bool]] = field(default_factory=list)  # (alias_text, source, is_abbr)
    rules: List[Tuple[str, str, List[str]]] = field(default_factory=list)  # (dimension, operator, allowed_values)


class RxNormIngestionBuilder:
    """Ingests RxNorm Current Prescribable Content (CPC) into the terminology database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.cursor = connection.cursor()
        self.total_processed = 0
        self.concepts_inserted = 0
        self.aliases_inserted = 0
        self.rules_inserted = 0
        self.relationships_inserted = 0
        self.excluded_count = 0
        self.exclusion_reasons: Dict[str, int] = {}
        self.concepts_by_tty: Dict[str, int] = {}

    def _record_exclusion(self, reason: str) -> None:
        self.excluded_count += 1
        self.exclusion_reasons[reason] = self.exclusion_reasons.get(reason, 0) + 1

    def parse_rxnconso_line(self, line: str) -> Optional[Tuple[str, str, str, str, str, str]]:
        """Parses a single line from official RXNCONSO.RRF.

        RXNCONSO columns (pipe-delimited):
        0: RXCUI, 1: LAT, 2: TS, 3: LUI, 4: STT, 5: SUI, 6: ISPREF, 7: RXAUI,
        8: SAUI, 9: SCUI, 10: SDUI, 11: SAB, 12: TTY, 13: CODE, 14: STR,
        15: SRL, 16: SUPPRESS, 17: CVF
        """
        parts = line.strip().split("|")
        if len(parts) < 15:
            self._record_exclusion("MALFORMED_RRF_LINE")
            return None

        rxcui = parts[0].strip()
        lat = parts[1].strip()
        sab = parts[11].strip().upper()
        tty = parts[12].strip().upper()
        str_name = parts[14].strip()
        suppress = parts[16].strip().upper() if len(parts) > 16 else "N"

        # Filter for English language
        if lat != "ENG":
            self._record_exclusion("NON_ENGLISH")
            return None

        # Filter for supported non-proprietary SABs
        if sab not in SUPPORTED_CPC_SABS:
            self._record_exclusion(f"PROPRIETARY_OR_UNSUPPORTED_SAB_{sab}")
            return None

        # Filter for supported prescribable Term Types
        if tty not in SUPPORTED_CPC_TTYS:
            self._record_exclusion(f"UNSUPPORTED_TTY_{tty}")
            return None

        # Filter suppressed rows
        if suppress in ("O", "E"):
            self._record_exclusion(f"SUPPRESSED_ROW_{suppress}")
            return None

        return rxcui, sab, tty, str_name, suppress, parts[6].strip()

    def ingest_full_release_zip(self, rxnorm_zip_path: Path) -> Dict[str, Any]:
        """High-performance batch ingestion of official RxNorm CPC distribution zip."""
        start_time = time.perf_counter()
        rxnorm_zip_path = Path(rxnorm_zip_path)

        with zipfile.ZipFile(rxnorm_zip_path, "r") as zf:
            # Step 1: Parse RXNCONSO.RRF
            conso_entry = None
            rel_entry = None
            for name in zf.namelist():
                if name.endswith("RXNCONSO.RRF"):
                    conso_entry = name
                elif name.endswith("RXNREL.RRF"):
                    rel_entry = name

            if not conso_entry:
                raise FileNotFoundError("Could not find RXNCONSO.RRF in RxNorm zip archive.")

            logger.info(f"Opening RxNorm RXNCONSO: {conso_entry}")
            
            # Map rxcui -> (canonical_name, primary_tty, sab, aliases_dict)
            # aliases_dict: search_text -> (raw_display_text, source, rxaui, is_pref)
            concepts_data: Dict[str, Dict[str, Any]] = {}

            with zf.open(conso_entry) as f:
                wrapper = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                for line in wrapper:
                    self.total_processed += 1
                    parts = line.strip().split("|")
                    if len(parts) < 15:
                        self._record_exclusion("MALFORMED_RRF_LINE")
                        continue

                    rxcui = parts[0].strip()
                    lat = parts[1].strip()
                    is_pref = parts[6].strip().upper()
                    rxaui = parts[7].strip()
                    sab = parts[11].strip().upper()
                    tty = parts[12].strip().upper()
                    str_name = parts[14].strip()
                    suppress = parts[16].strip().upper() if len(parts) > 16 else "N"

                    if lat != "ENG":
                        self._record_exclusion("NON_ENGLISH")
                        continue

                    if sab not in SUPPORTED_CPC_SABS:
                        self._record_exclusion(f"PROPRIETARY_OR_UNSUPPORTED_SAB_{sab}")
                        continue

                    if suppress in ("O", "E"):
                        self._record_exclusion(f"SUPPRESSED_ROW_{suppress}")
                        continue

                    norm_str = normalize_rx_text(str_name)
                    if not norm_str:
                        continue

                    if rxcui not in concepts_data:
                        concepts_data[rxcui] = {
                            "canonical_name": str_name,
                            "tty": tty,
                            "sab": sab,
                            "aliases": {},
                        }

                    entry = concepts_data[rxcui]

                    # Prefer canonical name from RXNORM SAB with ISPREF=Y
                    if is_pref == "Y" and sab == "RXNORM":
                        entry["canonical_name"] = str_name
                        entry["tty"] = tty

                    alias_source = f"{sab}_{tty}"
                    pref_flag = 1 if (is_pref == "Y" and sab == "RXNORM") else 0
                    
                    if norm_str not in entry["aliases"] or pref_flag == 1:
                        entry["aliases"][norm_str] = (str_name, alias_source, rxaui, pref_flag)

            logger.info(f"Ingesting {len(concepts_data)} RxNorm concepts into SQLite...")
            
            # Batch insert concepts
            concepts_batch = []
            for rxcui, c_info in concepts_data.items():
                tty_val = c_info["tty"]
                self.concepts_by_tty[tty_val] = self.concepts_by_tty.get(tty_val, 0) + 1
                concepts_batch.append((
                    "RxNorm",
                    rxcui,
                    c_info["canonical_name"],
                    tty_val,
                    None,
                    None,
                    "ACTIVE",
                    "{}",
                ))

            batch_size = 10000
            for i in range(0, len(concepts_batch), batch_size):
                chunk = concepts_batch[i : i + batch_size]
                self.cursor.executemany(
                    """
                    INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    chunk,
                )
            self.conn.commit()
            self.concepts_inserted += len(concepts_batch)

            # Retrieve concept IDs
            self.cursor.execute("SELECT code, concept_id FROM concepts WHERE system = 'RxNorm'")
            concept_id_map: Dict[str, int] = {row[0]: row[1] for row in self.cursor.fetchall()}

            # Batch insert aliases
            aliases_batch = []
            for rxcui, c_info in concepts_data.items():
                cid = concept_id_map.get(rxcui)
                if not cid:
                    continue

                for norm_str, (raw_str, source, rxaui, pref_flag) in c_info["aliases"].items():
                    is_abbr = 1 if len(norm_str) <= 4 else 0
                    aliases_batch.append((
                        cid,
                        norm_str,
                        raw_str,
                        norm_str,
                        "en",
                        source,
                        rxaui,
                        is_abbr,
                        pref_flag,
                    ))

            for i in range(0, len(aliases_batch), batch_size):
                chunk = aliases_batch[i : i + batch_size]
                self.cursor.executemany(
                    """
                    INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    chunk,
                )
            self.conn.commit()
            self.aliases_inserted += len(aliases_batch)

            # Step 2: Parse and ingest RXNREL.RRF
            if rel_entry:
                logger.info(f"Opening RxNorm RXNREL: {rel_entry}")
                rel_batch = []
                with zf.open(rel_entry) as f:
                    wrapper = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                    for line in wrapper:
                        parts = line.strip().split("|")
                        if len(parts) <= 10:
                            continue
                        sab = parts[10].strip().upper()
                        if sab not in SUPPORTED_CPC_SABS:
                            continue

                        rxcui1 = parts[0].strip()
                        rxcui2 = parts[4].strip()
                        rel = parts[3].strip()
                        rela = parts[7].strip()
                        rui = parts[8].strip() if len(parts) > 8 else None

                        cid1 = concept_id_map.get(rxcui1)
                        cid2 = concept_id_map.get(rxcui2)

                        if cid1 and cid2:
                            rel_type = (rela or rel).upper()
                            rel_batch.append((
                                cid1,
                                cid2,
                                rel_type,
                                "RXNORM_RXNREL",
                                rui,
                                "{}",
                            ))

                        if len(rel_batch) >= batch_size:
                            self.cursor.executemany(
                                """
                                INSERT INTO concept_relationships (concept_id_from, concept_id_to, relationship_type, relationship_source, source_relationship_id, attributes_json)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                rel_batch,
                            )
                            self.conn.commit()
                            self.relationships_inserted += len(rel_batch)
                            rel_batch = []

                if rel_batch:
                    self.cursor.executemany(
                        """
                        INSERT INTO concept_relationships (concept_id_from, concept_id_to, relationship_type, relationship_source, source_relationship_id, attributes_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rel_batch,
                    )
                    self.conn.commit()
                    self.relationships_inserted += len(rel_batch)

        duration = time.perf_counter() - start_time
        logger.info(
            f"RxNorm Full Release Ingestion Complete: {self.concepts_inserted} concepts, "
            f"{self.aliases_inserted} aliases, {self.relationships_inserted} relationships in {duration:.2f}s"
        )

        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "concepts_by_tty": self.concepts_by_tty,
            "aliases_inserted": self.aliases_inserted,
            "relationships_inserted": self.relationships_inserted,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
            "duration_sec": duration,
        }

    def ingest_rxnconso_file(self, rxnconso_path: Path) -> Dict[str, Any]:
        """Ingests RXNCONSO.RRF from an official RxNorm CPC distribution file."""
        concepts_map: Dict[str, ParsedRxNormConcept] = {}

        with open(rxnconso_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                self.total_processed += 1
                parsed = self.parse_rxnconso_line(line)
                if not parsed:
                    continue

                rxcui, sab, tty, str_name, suppress, is_pref = parsed
                norm_str = normalize_rx_text(str_name)
                if not norm_str:
                    continue

                if rxcui not in concepts_map:
                    concepts_map[rxcui] = ParsedRxNormConcept(
                        rxcui=rxcui,
                        canonical_name=str_name,
                        tty=tty,
                        sab=sab,
                        suppress=suppress,
                    )

                concept = concepts_map[rxcui]

                if is_pref == "Y" and sab == "RXNORM":
                    concept.canonical_name = str_name

                alias_source = f"{sab}_{tty}"
                concept.aliases.append((norm_str, alias_source, len(norm_str) <= 4))

        for rxcui, concept in concepts_map.items():
            self._insert_rx_concept(concept)

        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "aliases_inserted": self.aliases_inserted,
            "rules_inserted": self.rules_inserted,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
        }

    def ingest_medication_subset_json(self, json_path: Path) -> Dict[str, Any]:
        """Ingests from a curated medication subset JSON file (e.g. configs/medication_subset.json)."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for surface_name, record in data.items():
            self.total_processed += 1
            rxcui = str(record.get("rxcui") or record.get("code") or "")
            if not rxcui:
                self._record_exclusion("MISSING_RXCUI")
                continue

            canon = record.get("canonical_name", surface_name)
            drug_class = record.get("drug_class")
            default_route = record.get("default_route")
            common_forms = record.get("common_forms", [])

            concept = ParsedRxNormConcept(
                rxcui=rxcui,
                canonical_name=canon,
                tty="IN",
                sab="RXNORM",
                attributes={
                    "drug_class": drug_class,
                    "default_route": default_route,
                    "common_forms": common_forms,
                },
            )

            norm_canon = normalize_rx_text(canon)
            concept.aliases.append((norm_canon, "RXNORM_CANONICAL", False))

            norm_surface = normalize_rx_text(surface_name)
            if norm_surface != norm_canon:
                concept.aliases.append((norm_surface, "RXNORM_SURFACE", False))

            if default_route:
                concept.rules.append(("ROUTE", "IN", [default_route]))

            self._insert_rx_concept(concept)

        return {
            "total_processed": self.total_processed,
            "concepts_inserted": self.concepts_inserted,
            "aliases_inserted": self.aliases_inserted,
            "rules_inserted": self.rules_inserted,
            "excluded_count": self.excluded_count,
            "exclusion_reasons": self.exclusion_reasons,
        }

    def _insert_rx_concept(self, concept: ParsedRxNormConcept) -> int:
        """Inserts a single RxNorm concept and its aliases into the database."""
        self.cursor.execute(
            "SELECT concept_id FROM concepts WHERE system = ? AND code = ?",
            ("RxNorm", concept.rxcui),
        )
        row = self.cursor.fetchone()
        if row:
            concept_id = row[0]
        else:
            self.cursor.execute(
                """
                INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
                VALUES (?, ?, ?, ?, NULL, NULL, 'ACTIVE', ?)
                """,
                ("RxNorm", concept.rxcui, concept.canonical_name, concept.tty, json.dumps(concept.attributes)),
            )
            concept_id = self.cursor.lastrowid
            self.concepts_inserted += 1

        unique_aliases: Dict[str, Tuple[str, bool]] = {}
        for alias_text, source, is_abbr in concept.aliases:
            if alias_text not in unique_aliases:
                unique_aliases[alias_text] = (source, is_abbr)

        for alias_text, (source, is_abbr) in unique_aliases.items():
            self.cursor.execute(
                "SELECT alias_id FROM concept_aliases WHERE concept_id = ? AND alias_text = ?",
                (concept_id, alias_text),
            )
            alias_row = self.cursor.fetchone()
            if alias_row:
                continue

            is_pref = 1 if alias_text == normalize_rx_text(concept.canonical_name) else 0
            self.cursor.execute(
                """
                INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
                VALUES (?, ?, ?, ?, 'en', ?, ?, ?, ?)
                """,
                (concept_id, alias_text, alias_text, alias_text, source, concept.rxcui, 1 if is_abbr else 0, is_pref),
            )
            alias_id = self.cursor.lastrowid
            self.aliases_inserted += 1

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
