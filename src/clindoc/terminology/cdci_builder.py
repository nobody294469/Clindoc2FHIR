"""NRCeS Common Drug Codes for India (CDCI) Flat Files Ingestion Builder.

Parses official NRCeS CDCI Flat Files Package (SubstanceMaster, GenericMaster,
BrandMaster, ProductMaster, SupplierMaster, DrugFormMaster, RouteOfAdministrationMaster),
populating normalized concepts, multi-tier aliases, and authoritative concept relationships
according to the M-TERMINOLOGY-SCALE Phase 3A.2 architecture.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Official NRCeS CDCI Attribution Statement
CDCI_ATTRIBUTION_NOTICE = (
    "Common Drug Codes for India (Flat Files Package) copyright © 2019, Centre for Development of Advanced Computing (C-DAC), Pune. "
    "Produced by National Resource Centre for EHR Standards (NRCeS) at C-DAC Pune under the terms of Creative Commons Attribution 4.0 "
    "International Public License (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/. "
    "Redistribution and derivative product creation licensed within India."
)


def normalize_alias_text(text: str) -> str:
    """Normalizes an alias string for indexing (lowercased, standardized spaces, stripped)."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[\s\t\n]+", " ", text)
    return text


class CdciIngestionBuilder:
    """Ingests NRCeS CDCI Flat Files Package into the terminology database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.cursor = connection.cursor()
        self.concepts_inserted = 0
        self.aliases_inserted = 0
        self.relationships_inserted = 0
        self.concepts_by_tty: Dict[str, int] = {}
        self.relationships_by_type: Dict[str, int] = {}
        self.orphaned_brands_count = 0
        self.exclusion_reasons: Dict[str, int] = {}

    def ingest_flat_files_package(self, zip_or_dir_path: Path) -> Dict[str, Any]:
        """Ingests all 7 CDCI master tables from zip package or directory.

        Execution stages:
        1. Ingest SubstanceMaster.txt (tty='SUBSTANCE')
        2. Ingest DrugFormMaster.txt (tty='DOSE_FORM')
        3. Ingest RouteOfAdministrationMaster.txt (tty='ROUTE')
        4. Ingest SupplierMaster.txt (tty='SUPPLIER')
        5. Ingest GenericMaster.txt (tty='GENERIC') & map HAS_SUBSTANCE, HAS_ROUTE, HAS_DOSE_FORM
        6. Index ProductMaster.txt (Product Identifier -> Product Name)
        7. Ingest BrandMaster.txt (tty='BRAND') & map MANUFACTURED_BY, TRADENAME_OF (handling 14 orphans)
        """
        start_time = time.perf_counter()
        zip_or_dir_path = Path(zip_or_dir_path)

        if zip_or_dir_path.is_file() and zip_or_dir_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(zip_or_dir_path, "r") as zf:
                return self._ingest_from_zip(zf, start_time)
        elif zip_or_dir_path.is_dir():
            return self._ingest_from_directory(zip_or_dir_path, start_time)
        else:
            raise FileNotFoundError(f"CDCI source '{zip_or_dir_path}' must be a zip file or directory.")

    def _open_tsv_from_zip(self, zf: zipfile.ZipFile, filename: str) -> csv.reader:
        # Find path within zip
        target = None
        for name in zf.namelist():
            if name.endswith(filename) and not name.startswith("__MACOSX"):
                target = name
                break
        if not target:
            raise FileNotFoundError(f"Could not find '{filename}' inside CDCI zip archive.")

        f = zf.open(target)
        wrapper = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
        return csv.reader(wrapper, delimiter="\t")

    def _ingest_from_zip(self, zf: zipfile.ZipFile, start_time: float) -> Dict[str, Any]:
        logger.info("Ingesting CDCI master tables from zip...")
        
        # In-memory mapping from code to concept_id
        concept_id_map: Dict[str, int] = {}

        # 1. SubstanceMaster.txt
        # Header: Identifier, Substance Name, CAS Number, UNII, Substance Description, Molecular Weight, Toxicity, SMILE, InChI, IUPAC Name, Molecular Formula, last_updated_on
        substance_reader = self._open_tsv_from_zip(zf, "SubstanceMaster.txt")
        sub_hdr = next(substance_reader)
        sub_concepts_data = []
        sub_aliases_data = []

        for row in substance_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue

            cas = row[2].strip() if len(row) > 2 else ""
            unii = row[3].strip() if len(row) > 3 else ""
            desc = row[4].strip() if len(row) > 4 else ""
            mw = row[5].strip() if len(row) > 5 else ""
            toxicity = row[6].strip() if len(row) > 6 else ""
            smiles = row[7].strip() if len(row) > 7 else ""
            inchi = row[8].strip() if len(row) > 8 else ""
            iupac = row[9].strip() if len(row) > 9 else ""
            formula = row[10].strip() if len(row) > 10 else ""
            updated = row[11].strip() if len(row) > 11 else ""

            attribs = {
                "cas_number": cas or None,
                "unii": unii or None,
                "description": desc or None,
                "molecular_weight": mw or None,
                "toxicity": toxicity or None,
                "smiles": smiles or None,
                "inchi": inchi or None,
                "iupac_name": iupac or None,
                "formula": formula or None,
                "last_updated_on": updated or None,
            }

            sub_concepts_data.append((
                "CDCI", code, name, "SUBSTANCE", None, None, "ACTIVE", json.dumps(attribs)
            ))

        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sub_concepts_data,
        )
        self.conn.commit()
        self.concepts_inserted += len(sub_concepts_data)
        self.concepts_by_tty["SUBSTANCE"] = len(sub_concepts_data)

        # Retrieve inserted IDs
        self.cursor.execute("SELECT code, concept_id, canonical_name FROM concepts WHERE system = 'CDCI' AND tty = 'SUBSTANCE'")
        substance_codes: Set[str] = set()
        for r_code, cid, cname in self.cursor.fetchall():
            concept_id_map[r_code] = cid
            substance_codes.add(r_code)
            norm = normalize_alias_text(cname)
            sub_aliases_data.append((cid, norm, cname, norm, "en", "CDCI_SUBSTANCE_NAME", r_code, 0, 1))

        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sub_aliases_data,
        )
        self.aliases_inserted += len(sub_aliases_data)

        # 2. DrugFormMaster.txt
        # Header: Id, Dose Form
        form_reader = self._open_tsv_from_zip(zf, "DrugFormMaster.txt")
        form_hdr = next(form_reader)
        form_concepts = []
        form_aliases = []
        form_codes: Set[str] = set()

        for row in form_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue
            form_concepts.append(("CDCI", code, name, "DOSE_FORM", None, None, "ACTIVE", "{}"))

        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            form_concepts,
        )
        self.conn.commit()
        self.concepts_inserted += len(form_concepts)
        self.concepts_by_tty["DOSE_FORM"] = len(form_concepts)

        self.cursor.execute("SELECT code, concept_id, canonical_name FROM concepts WHERE system = 'CDCI' AND tty = 'DOSE_FORM'")
        for r_code, cid, cname in self.cursor.fetchall():
            concept_id_map[r_code] = cid
            form_codes.add(r_code)
            norm = normalize_alias_text(cname)
            form_aliases.append((cid, norm, cname, norm, "en", "CDCI_DOSE_FORM", r_code, 0, 1))

        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            form_aliases,
        )
        self.aliases_inserted += len(form_aliases)

        # 3. RouteOfAdministrationMaster.txt
        # Header: Identifier, RouteOfAdministration
        route_reader = self._open_tsv_from_zip(zf, "RouteOfAdministrationMaster.txt")
        route_hdr = next(route_reader)
        route_concepts = []
        route_aliases = []
        route_codes: Set[str] = set()

        for row in route_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue
            route_concepts.append(("CDCI", code, name, "ROUTE", None, None, "ACTIVE", "{}"))

        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            route_concepts,
        )
        self.conn.commit()
        self.concepts_inserted += len(route_concepts)
        self.concepts_by_tty["ROUTE"] = len(route_concepts)

        self.cursor.execute("SELECT code, concept_id, canonical_name FROM concepts WHERE system = 'CDCI' AND tty = 'ROUTE'")
        for r_code, cid, cname in self.cursor.fetchall():
            concept_id_map[r_code] = cid
            route_codes.add(r_code)
            norm = normalize_alias_text(cname)
            route_aliases.append((cid, norm, cname, norm, "en", "CDCI_ROUTE", r_code, 0, 1))

        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            route_aliases,
        )
        self.aliases_inserted += len(route_aliases)

        # 4. SupplierMaster.txt
        # Header: Identifier, Supplier Name, Country
        sup_reader = self._open_tsv_from_zip(zf, "SupplierMaster.txt")
        sup_hdr = next(sup_reader)
        sup_concepts = []
        sup_aliases = []
        supplier_codes: Set[str] = set()

        for row in sup_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            country = row[2].strip() if len(row) > 2 else ""
            if not name:
                continue
            attribs = {"country": country or None}
            sup_concepts.append(("CDCI", code, name, "SUPPLIER", None, None, "ACTIVE", json.dumps(attribs)))

        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sup_concepts,
        )
        self.conn.commit()
        self.concepts_inserted += len(sup_concepts)
        self.concepts_by_tty["SUPPLIER"] = len(sup_concepts)

        self.cursor.execute("SELECT code, concept_id, canonical_name FROM concepts WHERE system = 'CDCI' AND tty = 'SUPPLIER'")
        for r_code, cid, cname in self.cursor.fetchall():
            concept_id_map[r_code] = cid
            supplier_codes.add(r_code)
            norm = normalize_alias_text(cname)
            sup_aliases.append((cid, norm, cname, norm, "en", "CDCI_SUPPLIER_NAME", r_code, 0, 1))

        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sup_aliases,
        )
        self.aliases_inserted += len(sup_aliases)

        # 5. GenericMaster.txt
        # Header: Identifier, Generic Name, Substance Identifier, Route of Administration, Dose Form, Therapeutic Role, Indication, Contra Indication, Interaction with Drugs, Classification of Drugs, Source/ Regulatory, last_updated_on
        gen_reader = self._open_tsv_from_zip(zf, "GenericMaster.txt")
        gen_hdr = next(gen_reader)
        gen_concepts = []
        gen_relationships_raw = []  # (gen_code, sub_id_str, route_id_str, form_id_str)
        generic_codes: Set[str] = set()

        for row in gen_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue

            sub_ids = row[2].strip() if len(row) > 2 else ""
            route_ids = row[3].strip() if len(row) > 3 else ""
            form_id = row[4].strip() if len(row) > 4 else ""
            role = row[5].strip() if len(row) > 5 else ""
            indication = row[6].strip() if len(row) > 6 else ""
            contra = row[7].strip() if len(row) > 7 else ""
            interaction = row[8].strip() if len(row) > 8 else ""
            classification = row[9].strip() if len(row) > 9 else ""
            regulatory = row[10].strip() if len(row) > 10 else ""
            updated = row[11].strip() if len(row) > 11 else ""

            attribs = {
                "therapeutic_role": role or None,
                "indication": indication or None,
                "contraindication": contra or None,
                "interaction": interaction or None,
                "classification": classification or None,
                "source_regulatory": regulatory or None,
                "last_updated_on": updated or None,
            }
            gen_concepts.append(("CDCI", code, name, "GENERIC", None, None, "ACTIVE", json.dumps(attribs)))
            gen_relationships_raw.append((code, sub_ids, route_ids, form_id))

        self.cursor.executemany(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            gen_concepts,
        )
        self.conn.commit()
        self.concepts_inserted += len(gen_concepts)
        self.concepts_by_tty["GENERIC"] = len(gen_concepts)

        gen_aliases = []
        self.cursor.execute("SELECT code, concept_id, canonical_name FROM concepts WHERE system = 'CDCI' AND tty = 'GENERIC'")
        for r_code, cid, cname in self.cursor.fetchall():
            concept_id_map[r_code] = cid
            generic_codes.add(r_code)
            norm = normalize_alias_text(cname)
            gen_aliases.append((cid, norm, cname, norm, "en", "CDCI_GENERIC_NAME", r_code, 0, 1))

        self.cursor.executemany(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            gen_aliases,
        )
        self.aliases_inserted += len(gen_aliases)

        # Build generic relationships
        gen_rel_data = []
        for g_code, sub_ids, r_ids, f_id in gen_relationships_raw:
            g_cid = concept_id_map.get(g_code)
            if not g_cid:
                continue

            # HAS_SUBSTANCE
            if sub_ids:
                for sid in sub_ids.split("+"):
                    sid = sid.strip()
                    s_cid = concept_id_map.get(sid)
                    if s_cid:
                        gen_rel_data.append((g_cid, s_cid, "HAS_SUBSTANCE", "CDCI_FLAT_FILES", None, "{}"))
                        self.relationships_by_type["HAS_SUBSTANCE"] = self.relationships_by_type.get("HAS_SUBSTANCE", 0) + 1

            # HAS_ROUTE
            if r_ids:
                for rid in r_ids.split("+"):
                    rid = rid.strip()
                    r_cid = concept_id_map.get(rid)
                    if r_cid:
                        gen_rel_data.append((g_cid, r_cid, "HAS_ROUTE", "CDCI_FLAT_FILES", None, "{}"))
                        self.relationships_by_type["HAS_ROUTE"] = self.relationships_by_type.get("HAS_ROUTE", 0) + 1

            # HAS_DOSE_FORM
            if f_id:
                f_cid = concept_id_map.get(f_id.strip())
                if f_cid:
                    gen_rel_data.append((g_cid, f_cid, "HAS_DOSE_FORM", "CDCI_FLAT_FILES", None, "{}"))
                    self.relationships_by_type["HAS_DOSE_FORM"] = self.relationships_by_type.get("HAS_DOSE_FORM", 0) + 1

        self.cursor.executemany(
            """
            INSERT INTO concept_relationships (concept_id_from, concept_id_to, relationship_type, relationship_source, source_relationship_id, attributes_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            gen_rel_data,
        )
        self.conn.commit()
        self.relationships_inserted += len(gen_rel_data)

        # 6. ProductMaster.txt
        # Header: Identifier, Product Name
        prod_reader = self._open_tsv_from_zip(zf, "ProductMaster.txt")
        prod_hdr = next(prod_reader)
        product_names: Dict[str, str] = {}
        for row in prod_reader:
            if not row or not row[0].strip():
                continue
            pid = row[0].strip()
            pname = row[1].strip() if len(row) > 1 else ""
            if pname:
                product_names[pid] = pname

        # 7. BrandMaster.txt
        # Header: Identifier, Brand Name, Product Identifier, Supplier Identifier, Generic Identifier, License Number, License Status, Excipient, last_updated_on
        brand_reader = self._open_tsv_from_zip(zf, "BrandMaster.txt")
        brand_hdr = next(brand_reader)
        brand_concepts = []
        brand_relationships_raw = []  # (brand_code, prod_id, sup_id, gen_id, brand_name)

        for row in brand_reader:
            if not row or not row[0].strip():
                continue
            code = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ""
            if not name:
                continue

            pid = row[2].strip() if len(row) > 2 else ""
            sid = row[3].strip() if len(row) > 3 else ""
            gid = row[4].strip() if len(row) > 4 else ""
            lic_num = row[5].strip() if len(row) > 5 else ""
            lic_stat = row[6].strip() if len(row) > 6 else ""
            excipient = row[7].strip() if len(row) > 7 else ""
            updated = row[8].strip() if len(row) > 8 else ""

            # Check orphan status: generic ID missing or not in generic_codes
            is_orphan = not gid or gid not in generic_codes
            if is_orphan:
                self.orphaned_brands_count += 1

            attribs = {
                "product_identifier": pid or None,
                "supplier_identifier": sid or None,
                "generic_identifier": gid or None,
                "license_number": lic_num or None,
                "license_status": lic_stat or None,
                "excipient": excipient or None,
                "last_updated_on": updated or None,
            }
            if is_orphan:
                attribs["unresolved_generic_parent"] = gid or "UNSPECIFIED"

            brand_concepts.append(("CDCI", code, name, "BRAND", None, None, "ACTIVE", json.dumps(attribs)))
            brand_relationships_raw.append((code, pid, sid, gid, name))

        # Chunked batch insert of brand concepts
        batch_size = 10000
        for i in range(0, len(brand_concepts), batch_size):
            chunk = brand_concepts[i : i + batch_size]
            self.cursor.executemany(
                """
                INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                chunk,
            )
        self.conn.commit()
        self.concepts_inserted += len(brand_concepts)
        self.concepts_by_tty["BRAND"] = len(brand_concepts)

        # Retrieve brand concept IDs
        self.cursor.execute("SELECT code, concept_id FROM concepts WHERE system = 'CDCI' AND tty = 'BRAND'")
        for r_code, cid in self.cursor.fetchall():
            concept_id_map[r_code] = cid

        # Prepare brand aliases and relationships
        brand_aliases = []
        brand_rels = []

        for b_code, pid, sid, gid, b_name in brand_relationships_raw:
            b_cid = concept_id_map.get(b_code)
            if not b_cid:
                continue

            # Primary Brand Name alias
            norm_bname = normalize_alias_text(b_name)
            brand_aliases.append((b_cid, norm_bname, b_name, norm_bname, "en", "CDCI_BRAND_NAME", b_code, 0, 1))

            # Product Name alias if present and distinct
            p_name = product_names.get(pid)
            if p_name and p_name.strip():
                norm_pname = normalize_alias_text(p_name)
                if norm_pname and norm_pname != norm_bname:
                    brand_aliases.append((b_cid, norm_pname, p_name, norm_pname, "en", "CDCI_PRODUCT_NAME", pid, 0, 0))

            # MANUFACTURED_BY edge
            if sid:
                s_cid = concept_id_map.get(sid)
                if s_cid:
                    brand_rels.append((b_cid, s_cid, "MANUFACTURED_BY", "CDCI_FLAT_FILES", None, "{}"))
                    self.relationships_by_type["MANUFACTURED_BY"] = self.relationships_by_type.get("MANUFACTURED_BY", 0) + 1

            # TRADENAME_OF edge (strictly for non-orphaned brands)
            if gid and gid in generic_codes:
                g_cid = concept_id_map.get(gid)
                if g_cid:
                    brand_rels.append((b_cid, g_cid, "TRADENAME_OF", "CDCI_FLAT_FILES", None, "{}"))
                    self.relationships_by_type["TRADENAME_OF"] = self.relationships_by_type.get("TRADENAME_OF", 0) + 1

        # Chunked insert of aliases
        for i in range(0, len(brand_aliases), batch_size):
            chunk = brand_aliases[i : i + batch_size]
            self.cursor.executemany(
                """
                INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, language, alias_source, source_identifier, is_abbreviation, is_preferred)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                chunk,
            )
        self.conn.commit()
        self.aliases_inserted += len(brand_aliases)

        # Chunked insert of relationships
        for i in range(0, len(brand_rels), batch_size):
            chunk = brand_rels[i : i + batch_size]
            self.cursor.executemany(
                """
                INSERT INTO concept_relationships (concept_id_from, concept_id_to, relationship_type, relationship_source, source_relationship_id, attributes_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                chunk,
            )
        self.conn.commit()
        self.relationships_inserted += len(brand_rels)

        duration = time.perf_counter() - start_time
        logger.info(
            f"CDCI Ingestion Complete: {self.concepts_inserted} concepts, {self.aliases_inserted} aliases, "
            f"{self.relationships_inserted} relationships in {duration:.2f}s"
        )

        return {
            "concepts_inserted": self.concepts_inserted,
            "concepts_by_tty": self.concepts_by_tty,
            "aliases_inserted": self.aliases_inserted,
            "relationships_inserted": self.relationships_inserted,
            "relationships_by_type": self.relationships_by_type,
            "orphaned_brands_count": self.orphaned_brands_count,
            "duration_sec": duration,
        }

    def _ingest_from_directory(self, dir_path: Path, start_time: float) -> Dict[str, Any]:
        # For directories, could be implemented if needed; primary packaging is the zip file
        raise NotImplementedError("Directory ingestion not required; zip ingestion is authoritative.")
