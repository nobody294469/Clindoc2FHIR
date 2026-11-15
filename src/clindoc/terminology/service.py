"""Runtime Terminology Retrieval Service for M-TERMINOLOGY-SCALE Phase 2.

Provides deterministic exact alias lookup, in-memory caching, multi-tiered FTS5
lexical (Unicode) and substring (Trigram) retrieval, multi-dimensional clinical
context-rule evaluation, and deterministic ranking without arbitrary probability thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from clindoc.terminology.loinc_builder import normalize_alias_text
from clindoc.terminology.compiler import load_active_terminology_pointer

logger = logging.getLogger(__name__)


class ContextStatus(str, Enum):
    """Evaluation outcome for clinical context rules."""
    PASSED = "PASSED"
    NO_RULES = "NO_RULES"
    FAILED_CONSTRAINT = "FAILED_CONSTRAINT"
    ABSTAINED_MISSING_CONTEXT = "ABSTAINED_MISSING_CONTEXT"
    ABSTAINED_CONFLICTING_CONTEXT = "ABSTAINED_CONFLICTING_CONTEXT"


class RetrievalMethod(str, Enum):
    """Method used to retrieve candidate from terminology store."""
    EXACT_CACHE = "EXACT_CACHE"
    EXACT_DB = "EXACT_DB"
    FTS_UNICODE = "FTS_UNICODE"
    FTS_TRIGRAM = "FTS_TRIGRAM"


@dataclass(frozen=True)
class ContextRule:
    """Normalized contextual restriction associated with an alias."""
    rule_id: int
    alias_id: int
    dimension: str           # 'SPECIMEN', 'SCALE', 'ROUTE', 'DOMAIN', 'DISALLOWED_CONTEXT'
    operator: str            # 'IN', 'EQUALS', 'NOT_IN'
    allowed_values: Tuple[str, ...]


@dataclass
class QueryContext:
    """Query-time clinical context passed to constrain terminology retrieval."""
    specimen: Optional[Union[str, Sequence[str]]] = None
    scale: Optional[Union[str, Sequence[str]]] = None
    route: Optional[Union[str, Sequence[str]]] = None
    domain: Optional[str] = None
    disallowed_context: List[str] = field(default_factory=list)
    raw_context: Dict[str, Any] = field(default_factory=dict)
    has_conflict: bool = False
    conflict_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> QueryContext:
        if not data:
            return cls()
        return cls(
            specimen=data.get("specimen"),
            scale=data.get("scale"),
            route=data.get("route"),
            domain=data.get("domain"),
            disallowed_context=list(data.get("disallowed_context", [])),
            raw_context=dict(data),
            has_conflict=bool(data.get("has_conflict", False)),
            conflict_reason=data.get("conflict_reason"),
        )


@dataclass
class RetrievedCandidate:
    """A retrieved terminology concept candidate with ranking and context evaluation.
    
    Note on Score Semantics:
      - `rank_score` is an ordinal retrieval-ranking priority signal (Tier 1: 1.0 exact,
        Tier 2: 0.8 prefix, Tier 3: 0.4..0.9 unicode, Tier 4: 0.2..0.5 trigram).
        It is strictly a ranking signal, NOT a probability or clinical confidence.
      - `is_preferred` indicates whether the matched string is the official terminology
        release's canonical display string, NOT a preferred clinical treatment or drug-of-choice.
    """
    concept_id: int
    system: str                          # 'LOINC' or 'RxNorm'
    code: str                            # e.g. '2823-3', '723'
    canonical_name: str
    tty: Optional[str]
    attributes: Dict[str, Any]
    alias_id: int
    matched_alias: str
    alias_source: str
    is_abbreviation: bool
    is_preferred: bool                   # Terminology display preference flag, not clinical preference
    retrieval_method: RetrievalMethod
    rank_score: float                   # Ordinal ranking signal (Tier 1: 1.0 exact > FTS), not probability
    raw_bm25_score: Optional[float] = None
    context_status: ContextStatus = ContextStatus.NO_RULES
    context_reasons: List[str] = field(default_factory=list)


@dataclass
class RetrievalResponse:
    """Authoritative response contract for terminology retrieval queries."""
    query: str
    normalized_query: str
    candidates: List[RetrievedCandidate]
    is_ambiguous: bool                  # True if multiple top-ranked candidates or multiple distinct concepts
    is_abstained: bool                  # True if candidates were suppressed due to missing/conflicting context
    abstention_reasons: List[str]
    database_version: str
    source_releases: Dict[str, str]
    query_duration_ms: float


def sanitize_fts5_query(query: str) -> str:
    """Sanitizes text query for SQLite FTS5 MATCH clause, stripping control punctuation."""
    if not query:
        return ""
    # Remove quotes, colons, asterisks, parentheses, brackets, and logical keywords
    cleaned = re.sub(r'["*^:()\[\]{}]', " ", query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    # Filter out bare boolean operators
    tokens = [f'"{t}"' for t in tokens if t.upper() not in ("AND", "OR", "NOT", "NEAR")]
    return " ".join(tokens)


def normalize_context_token(tok: Any) -> str:
    """Normalizes context token for case-insensitive matching."""
    if not tok:
        return ""
    return str(tok).strip().lower()


def evaluate_context_rules(
    rules: Sequence[ContextRule],
    context: Optional[QueryContext],
) -> Tuple[ContextStatus, List[str]]:
    """Evaluates context rules against provided QueryContext.

    Semantics:
    1. Conflicting query context -> ABSTAINED_CONFLICTING_CONTEXT (fail closed).
    2. Zero rules defined -> NO_RULES.
    3. AND across distinct dimensions (all dimensions must pass).
    4. OR across allowed values in a dimension.
    5. NOT_IN / DISALLOWED_CONTEXT -> FAILED_CONSTRAINT if forbidden token present.
    6. Missing required context for a dimension -> ABSTAINED_MISSING_CONTEXT (fail closed).
    """
    if not rules:
        return ContextStatus.NO_RULES, ["No contextual restrictions defined for alias"]

    if context is None:
        context = QueryContext()

    # Rule 1: Conflicting context evidence results in immediate abstention
    if context.has_conflict:
        reason = context.conflict_reason or "Conflicting clinical context evidence in query"
        return ContextStatus.ABSTAINED_CONFLICTING_CONTEXT, [reason]

    # Inspect context provided by dimension
    ctx_by_dimension: Dict[str, Set[str]] = {}

    def extract_tokens(val: Any) -> Set[str]:
        if val is None:
            return set()
        if isinstance(val, (list, tuple, set)):
            return {normalize_context_token(item) for item in val if item}
        norm = normalize_context_token(val)
        return {norm} if norm else set()

    ctx_by_dimension["SPECIMEN"] = extract_tokens(context.specimen)
    ctx_by_dimension["SCALE"] = extract_tokens(context.scale)
    ctx_by_dimension["ROUTE"] = extract_tokens(context.route)
    ctx_by_dimension["DOMAIN"] = extract_tokens(context.domain)
    ctx_by_dimension["DISALLOWED_CONTEXT"] = extract_tokens(context.disallowed_context)

    # Check for multi-value conflict within dimensions where a slot expects exactly one value
    for dim in ("SPECIMEN", "SCALE", "ROUTE"):
        if len(ctx_by_dimension[dim]) > 1:
            return (
                ContextStatus.ABSTAINED_CONFLICTING_CONTEXT,
                [f"Conflicting multiple values provided for single dimension '{dim}': {ctx_by_dimension[dim]}"],
            )

    # Group rules by dimension
    rules_by_dim: Dict[str, List[ContextRule]] = {}
    for r in rules:
        dim = r.dimension.strip().upper()
        rules_by_dim.setdefault(dim, []).append(r)

    reasons: List[str] = []
    has_missing_context = False
    has_failed_constraint = False

    # AND across dimensions: every dimension must be satisfied
    for dim, dim_rules in rules_by_dim.items():
        provided_tokens = ctx_by_dimension.get(dim, set())

        # Disallowed Context dimension
        if dim == "DISALLOWED_CONTEXT":
            for r in dim_rules:
                forbidden = {normalize_context_token(x) for x in r.allowed_values}
                clash = provided_tokens.intersection(forbidden)
                if clash:
                    has_failed_constraint = True
                    reasons.append(f"Disallowed context token detected: {clash}")
            continue

        # Standard context dimensions (SPECIMEN, SCALE, ROUTE, DOMAIN)
        for r in dim_rules:
            allowed = {normalize_context_token(x) for x in r.allowed_values}
            op = r.operator.strip().upper()

            if op in ("IN", "EQUALS"):
                if not provided_tokens or "unknown" in provided_tokens:
                    # Missing required context: candidate requires specific evidence, but none provided
                    has_missing_context = True
                    reasons.append(
                        f"Missing required context for dimension '{dim}'; requires one of {list(r.allowed_values)}"
                    )
                else:
                    # OR semantics: at least one provided token must match allowed values
                    if not provided_tokens.intersection(allowed):
                        has_failed_constraint = True
                        reasons.append(
                            f"Dimension '{dim}' value {list(provided_tokens)} not in allowed set {list(r.allowed_values)}"
                        )

            elif op == "NOT_IN":
                if provided_tokens.intersection(allowed):
                    has_failed_constraint = True
                    reasons.append(f"Dimension '{dim}' contains forbidden value in {list(r.allowed_values)}")

    if has_failed_constraint:
        return ContextStatus.FAILED_CONSTRAINT, reasons
    if has_missing_context:
        return ContextStatus.ABSTAINED_MISSING_CONTEXT, reasons

    return ContextStatus.PASSED, ["All contextual constraints satisfied"]


class TerminologyRetrievalService:
    """Standalone runtime retrieval service providing exact cache, FTS5, and rule evaluation.

    Concurrency Model:
      - Thread-safety is enforced via a reentrant lock (`threading.RLock`).
      - All public query methods (`retrieve`, `get_exact_alias`, `search_unicode`, `search_trigram`)
        and lifecycle methods (`reload_if_updated`, `close`) acquire `self._lock`.
      - Reentrant locking allows composite calls (e.g. `retrieve` invoking `get_exact_alias`)
        without risking self-deadlock.
      - During database reload, candidate connection creation and cache compilation occur in
        isolated local variables. State publication occurs atomically under `self._lock`,
        ensuring readers never observe partially initialized state or closed database handles.
    """

    def __init__(
        self,
        db_path_or_dir: Union[Path, str],
        pointer_name: str = "active_terminology.json",
        preload_cache: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self.input_path = Path(db_path_or_dir)
        self.pointer_name = pointer_name
        self.db_path = self._resolve_db_path(self.input_path)
        self.conn = self._connect_db(self.db_path)
        self.metadata: Dict[str, str] = self._load_metadata(self.conn)
        self.db_version: str = self.metadata.get("version_tag") or self.metadata.get("schema_version", "1.0.0")

        # In-memory exact alias cache: alias_text -> list of raw candidate dicts
        self._exact_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Alias context rules cache: alias_id -> tuple of ContextRule
        self._rules_cache: Dict[int, Tuple[ContextRule, ...]] = {}

        if preload_cache:
            self._exact_cache, self._rules_cache = self._build_cache_from_conn(self.conn)

    def _resolve_db_path(self, path: Path) -> Path:
        """Resolves target .db file directly or from pointer in directory."""
        if path.is_file():
            return path
        if path.is_dir():
            ptr = load_active_terminology_pointer(path)
            if ptr and ptr.get("active_database"):
                db_file = path / ptr["active_database"]
                if db_file.exists():
                    return db_file
                p_path = Path(ptr.get("active_database_path", ""))
                if p_path.exists():
                    return p_path
            # Fall back to finding any .db in dir
            db_files = sorted(path.glob("terminology_v*.db"), reverse=True)
            if db_files:
                return db_files[0]
            any_dbs = sorted(path.glob("*.db"), reverse=True)
            if any_dbs:
                return any_dbs[0]
        raise FileNotFoundError(f"Cannot resolve terminology SQLite database from '{path}'")

    def _connect_db(self, path: Path) -> sqlite3.Connection:
        """Opens read-only SQLite connection configured for concurrent queries."""
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    @classmethod
    def _load_metadata(cls, conn: sqlite3.Connection) -> Dict[str, str]:
        """Loads metadata table from specified connection."""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM terminology_metadata")
            return {row["key"]: row["value"] for row in cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to read terminology_metadata: {e}")
            return {}

    @classmethod
    def _validate_fts_operational(cls, conn: sqlite3.Connection) -> None:
        """Executes harmless representative MATCH queries to verify virtual tables are operational.

        Performs read-only validation without modifying the candidate database.
        """
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT rowid FROM fts_aliases_unicode WHERE fts_aliases_unicode MATCH 'test' LIMIT 1")
            cursor.fetchall()
        except Exception as e:
            raise sqlite3.DatabaseError(f"Virtual table 'fts_aliases_unicode' failed operational MATCH check: {e}") from e

        try:
            # Trigram query requires token length >= 3
            cursor.execute("SELECT rowid FROM fts_aliases_trigram WHERE fts_aliases_trigram MATCH 'tes' LIMIT 1")
            cursor.fetchall()
        except Exception as e:
            raise sqlite3.DatabaseError(f"Virtual table 'fts_aliases_trigram' failed operational MATCH check: {e}") from e

    @classmethod
    def _build_cache_from_conn(
        cls, conn: sqlite3.Connection
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[int, Tuple[ContextRule, ...]]]:
        """Builds exact alias and context rules caches in temporary structures from connection."""
        cursor = conn.cursor()

        # Validate schema tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cursor.fetchall()}
        required = {"concepts", "concept_aliases", "alias_context_rules", "terminology_metadata", "fts_aliases_unicode", "fts_aliases_trigram"}
        missing = required - tables
        if missing:
            raise sqlite3.DatabaseError(f"Candidate database missing required tables: {missing}")

        # Operational check for FTS virtual tables
        cls._validate_fts_operational(conn)

        # Load rules
        cursor.execute(
            """
            SELECT rule_id, alias_id, dimension, operator, allowed_values_json
            FROM alias_context_rules
            ORDER BY rule_id
            """
        )
        temp_rules: Dict[int, List[ContextRule]] = {}
        for row in cursor.fetchall():
            try:
                allowed = tuple(json.loads(row["allowed_values_json"]))
            except Exception:
                allowed = ()
            r = ContextRule(
                rule_id=row["rule_id"],
                alias_id=row["alias_id"],
                dimension=row["dimension"],
                operator=row["operator"],
                allowed_values=allowed,
            )
            temp_rules.setdefault(row["alias_id"], []).append(r)
        rules_cache = {aid: tuple(rl) for aid, rl in temp_rules.items()}

        # Load concepts and aliases
        cursor.execute(
            """
            SELECT 
                ca.alias_id, ca.alias_text, ca.alias_source, ca.is_abbreviation, ca.is_preferred,
                c.concept_id, c.system, c.code, c.canonical_name, c.tty, c.attributes_json
            FROM concept_aliases ca
            JOIN concepts c ON ca.concept_id = c.concept_id
            ORDER BY ca.alias_text, c.concept_id
            """
        )
        exact_cache: Dict[str, List[Dict[str, Any]]] = {}
        for row in cursor.fetchall():
            norm_alias = normalize_alias_text(row["alias_text"])
            record = dict(row)
            exact_cache.setdefault(norm_alias, []).append(record)

        return exact_cache, rules_cache

    def reload_if_updated(self) -> bool:
        """Transactionally checks and reloads database if active pointer changed.

        Concurrency & Transactional Invariants:
        1. Reload Serialization (Per Service Instance):
           - An internal `self._reload_lock` serializes concurrent reload calls within a single
             service instance. Only one thread executes candidate connection opening, FTS validation,
             and cache building at any time.
           - Scope Limitation: This lock is strictly per Python service instance and does NOT serialize
             across separate operating system processes or independent service instances.
        2. Non-blocking Readers:
           - Reader queries acquire `self._lock`, which is NOT held during expensive candidate
             connection opening or cache building. Readers continue using the existing active connection
             uninterrupted until the brief state publication swap.
        3. Double-Checked State Verification:
           - Fast pre-check: If `active_database` matches `self.db_path`, returns False immediately.
           - Pre-publication check: Inside the critical section `with self._lock:`, re-reads the active
             pointer from disk. If the active pointer changed again to a different release while candidate
             compilation was underway, the candidate is discarded as stale and the reload aborts safely.
           - Invariance check: If `self.db_path` already equals `new_db_file`, returns False without mutating.
           - Scope Limitation: This final pointer check cannot prevent an external pointer change occurring
             immediately AFTER the critical section completes. In such cases, the service publishes the
             database that was active at the time of the check. Subsequent calls to `reload_if_updated()`
             reliably converge the service to the latest pointer on disk, but no automatic convergence
             is promised without another explicit invocation.
        4. Atomic State Handover & Post-Publication Safety:
           - Under `self._lock`, atomically replaces `self.conn`, `self.db_path`, `self.metadata`,
             `self.db_version`, `self._exact_cache`, and `self._rules_cache`.
           - Once publication succeeds, `published = True` is marked. Any subsequent exception (e.g.
             during superseded connection close or logging) will NEVER close the newly installed active
             connection `self.conn`.
           - The superseded `old_conn` is extracted, and closed safely AFTER releasing `self._lock`.
        5. Fail-Closed Cleanup:
           - Any exception occurring prior to successful publication triggers immediate closure of
             un-published `candidate_conn`, leaves active state 100% untouched, and returns False.
        """
        if not self.input_path.is_dir():
            return False

        # Fast non-blocking check before acquiring reload lock
        ptr = load_active_terminology_pointer(self.input_path)
        if not ptr:
            return False

        target_rel_db = ptr.get("active_database", "")
        new_db_file = self.input_path / target_rel_db
        if not new_db_file.exists():
            return False

        with self._lock:
            if new_db_file == self.db_path:
                return False

        # Serialize reload attempts to prevent race conditions and duplicate compilation
        with self._reload_lock:
            # Re-check under reload lock in case another thread just completed the reload
            with self._lock:
                if new_db_file == self.db_path:
                    return False

            candidate_conn: Optional[sqlite3.Connection] = None
            published = False
            try:
                candidate_conn = self._connect_db(new_db_file)
                candidate_metadata = self._load_metadata(candidate_conn)
                if not candidate_metadata:
                    raise sqlite3.DatabaseError(f"Candidate database '{new_db_file}' has empty metadata")

                candidate_version = candidate_metadata.get("version_tag") or ptr.get("version_tag", "1.0.0")

                # Validate operational status of FTS5 virtual tables before cache build
                self._validate_fts_operational(candidate_conn)

                # Build candidate caches in temporary variables outside reader lock
                candidate_exact, candidate_rules = self._build_cache_from_conn(candidate_conn)

                # Atomic publication critical section
                old_conn: Optional[sqlite3.Connection] = None
                with self._lock:
                    # Final freshness check: re-verify the active pointer on disk
                    latest_ptr = load_active_terminology_pointer(self.input_path)
                    if not latest_ptr:
                        raise sqlite3.DatabaseError("Active pointer disappeared during candidate compilation")

                    latest_target = self.input_path / latest_ptr.get("active_database", "")
                    if latest_target != new_db_file:
                        logger.warning(
                            f"Active pointer changed to '{latest_target.name}' while validating '{new_db_file.name}'. "
                            f"Discarding stale candidate."
                        )
                        raise RuntimeError(f"Pointer moved to '{latest_target.name}' during compilation")

                    if self.db_path == new_db_file:
                        # State was already published
                        candidate_conn.close()
                        return False

                    # Atomically publish new state
                    old_conn = self.conn
                    self.conn = candidate_conn
                    self.db_path = new_db_file
                    self.metadata = candidate_metadata
                    self.db_version = candidate_version
                    self._exact_cache = candidate_exact
                    self._rules_cache = candidate_rules
                    published = True

                # Safely close superseded connection outside of reader lock
                if old_conn is not None:
                    try:
                        old_conn.close()
                    except Exception as close_err:
                        logger.warning(f"Error closing superseded connection: {close_err}")

                logger.info(f"Terminology successfully reloaded to {new_db_file.name} (version {candidate_version})")
                return True

            except Exception as e:
                logger.error(f"Failed to transactionally reload candidate database '{new_db_file}': {e}. Preserving active state.")
                if candidate_conn is not None and not published:
                    try:
                        candidate_conn.close()
                    except Exception:
                        pass
                return published

    def get_exact_alias(
        self,
        query: str,
        context: Optional[QueryContext] = None,
    ) -> List[RetrievedCandidate]:
        """Performs exact alias lookup via in-memory cache or DB query."""
        with self._lock:
            norm_query = normalize_alias_text(query)
            if not norm_query:
                return []

            # 1. In-memory cache
            records = self._exact_cache.get(norm_query)
            method = RetrievalMethod.EXACT_CACHE

            # 2. Database fallback if not in cache
            if records is None:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT 
                        ca.alias_id, ca.alias_text, ca.alias_source, ca.is_abbreviation, ca.is_preferred,
                        c.concept_id, c.system, c.code, c.canonical_name, c.tty, c.attributes_json
                    FROM concept_aliases ca
                    JOIN concepts c ON ca.concept_id = c.concept_id
                    WHERE ca.alias_text = ?
                    ORDER BY ca.is_preferred DESC, c.concept_id ASC
                    """,
                    (norm_query,),
                )
                rows = cursor.fetchall()
                records = [dict(r) for r in rows]
                method = RetrievalMethod.EXACT_DB

            candidates: List[RetrievedCandidate] = []
            for rec in records:
                alias_id = rec["alias_id"]
                rules = self._rules_cache.get(alias_id)
                if rules is None:
                    # Query DB for rules if not in cache
                    cursor = self.conn.cursor()
                    cursor.execute(
                        "SELECT rule_id, alias_id, dimension, operator, allowed_values_json FROM alias_context_rules WHERE alias_id = ?",
                        (alias_id,),
                    )
                    rl = []
                    for r_row in cursor.fetchall():
                        try:
                            allw = tuple(json.loads(r_row["allowed_values_json"]))
                        except Exception:
                            allw = ()
                        rl.append(ContextRule(r_row["rule_id"], r_row["alias_id"], r_row["dimension"], r_row["operator"], allw))
                    rules = tuple(rl)

                ctx_status, ctx_reasons = evaluate_context_rules(rules, context)

                try:
                    attribs = json.loads(rec["attributes_json"])
                except Exception:
                    attribs = {}

                c = RetrievedCandidate(
                    concept_id=rec["concept_id"],
                    system=rec["system"],
                    code=rec["code"],
                    canonical_name=rec["canonical_name"],
                    tty=rec["tty"],
                    attributes=attribs,
                    alias_id=alias_id,
                    matched_alias=rec["alias_text"],
                    alias_source=rec["alias_source"],
                    is_abbreviation=bool(rec["is_abbreviation"]),
                    is_preferred=bool(rec["is_preferred"]),
                    retrieval_method=method,
                    rank_score=1.0,  # Top tier
                    raw_bm25_score=None,
                    context_status=ctx_status,
                    context_reasons=ctx_reasons,
                )
                candidates.append(c)

            return candidates

    def search_unicode(
        self,
        query: str,
        context: Optional[QueryContext] = None,
        max_candidates: int = 10,
    ) -> List[RetrievedCandidate]:
        """Performs word-token BM25 retrieval over fts_aliases_unicode."""
        with self._lock:
            clean_fts = sanitize_fts5_query(query)
            if not clean_fts:
                return []

            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT 
                        f.rowid AS alias_id,
                        bm25(fts_aliases_unicode) AS bm25_score,
                        ca.alias_text, ca.alias_source, ca.is_abbreviation, ca.is_preferred,
                        c.concept_id, c.system, c.code, c.canonical_name, c.tty, c.attributes_json
                    FROM fts_aliases_unicode f
                    JOIN concept_aliases ca ON f.rowid = ca.alias_id
                    JOIN concepts c ON ca.concept_id = c.concept_id
                    WHERE fts_aliases_unicode MATCH ?
                    ORDER BY bm25(fts_aliases_unicode) ASC, ca.is_preferred DESC, c.concept_id ASC
                    LIMIT ?
                    """,
                    (clean_fts, max_candidates),
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 unicode query '{clean_fts}' failed: {e}")
                return []

            candidates: List[RetrievedCandidate] = []
            for row in rows:
                alias_id = row["alias_id"]
                rules = self._rules_cache.get(alias_id, ())
                ctx_status, ctx_reasons = evaluate_context_rules(rules, context)

                try:
                    attribs = json.loads(row["attributes_json"])
                except Exception:
                    attribs = {}

                bm25_val = float(row["bm25_score"])
                # Normalize BM25 strictly as a relative rank signal in [0.4, 0.9]
                rank_score = max(0.4, min(0.9, 0.9 - abs(bm25_val) * 0.05))

                c = RetrievedCandidate(
                    concept_id=row["concept_id"],
                    system=row["system"],
                    code=row["code"],
                    canonical_name=row["canonical_name"],
                    tty=row["tty"],
                    attributes=attribs,
                    alias_id=alias_id,
                    matched_alias=row["alias_text"],
                    alias_source=row["alias_source"],
                    is_abbreviation=bool(row["is_abbreviation"]),
                    is_preferred=bool(row["is_preferred"]),
                    retrieval_method=RetrievalMethod.FTS_UNICODE,
                    rank_score=rank_score,
                    raw_bm25_score=bm25_val,
                    context_status=ctx_status,
                    context_reasons=ctx_reasons,
                )
                candidates.append(c)

            return candidates

    def search_trigram(
        self,
        query: str,
        context: Optional[QueryContext] = None,
        max_candidates: int = 10,
    ) -> List[RetrievedCandidate]:
        """Performs character trigram substring retrieval over fts_aliases_trigram."""
        with self._lock:
            norm = normalize_alias_text(query)
            # SQLite trigram MATCH requires string length >= 3
            if len(norm) < 3:
                return []

            clean_fts = sanitize_fts5_query(norm)
            if not clean_fts:
                return []

            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT 
                        f.rowid AS alias_id,
                        bm25(fts_aliases_trigram) AS bm25_score,
                        ca.alias_text, ca.alias_source, ca.is_abbreviation, ca.is_preferred,
                        c.concept_id, c.system, c.code, c.canonical_name, c.tty, c.attributes_json
                    FROM fts_aliases_trigram f
                    JOIN concept_aliases ca ON f.rowid = ca.alias_id
                    JOIN concepts c ON ca.concept_id = c.concept_id
                    WHERE fts_aliases_trigram MATCH ?
                    ORDER BY bm25(fts_aliases_trigram) ASC, ca.is_preferred DESC, c.concept_id ASC
                    LIMIT ?
                    """,
                    (clean_fts, max_candidates),
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 trigram query '{clean_fts}' failed: {e}")
                return []

            candidates: List[RetrievedCandidate] = []
            for row in rows:
                alias_id = row["alias_id"]
                rules = self._rules_cache.get(alias_id, ())
                ctx_status, ctx_reasons = evaluate_context_rules(rules, context)

                try:
                    attribs = json.loads(row["attributes_json"])
                except Exception:
                    attribs = {}

                bm25_val = float(row["bm25_score"])
                # Normalize trigram relative rank signal in [0.2, 0.5]
                rank_score = max(0.2, min(0.5, 0.5 - abs(bm25_val) * 0.05))

                c = RetrievedCandidate(
                    concept_id=row["concept_id"],
                    system=row["system"],
                    code=row["code"],
                    canonical_name=row["canonical_name"],
                    tty=row["tty"],
                    attributes=attribs,
                    alias_id=alias_id,
                    matched_alias=row["alias_text"],
                    alias_source=row["alias_source"],
                    is_abbreviation=bool(row["is_abbreviation"]),
                    is_preferred=bool(row["is_preferred"]),
                    retrieval_method=RetrievalMethod.FTS_TRIGRAM,
                    rank_score=rank_score,
                    raw_bm25_score=bm25_val,
                    context_status=ctx_status,
                    context_reasons=ctx_reasons,
                )
                candidates.append(c)

            return candidates

    def retrieve(
        self,
        query: str,
        context: Optional[Union[QueryContext, Dict[str, Any]]] = None,
        max_candidates: int = 10,
    ) -> RetrievalResponse:
        """Executes multi-tiered deterministic retrieval:

        Tier 1: Exact in-memory cache lookup.
        Tier 2: Unicode word-level BM25 search.
        Tier 3: Trigram substring fallback (queries >= 3 chars).
        Short-query fallback: Prefix search for queries < 3 chars when exact match misses.
        """
        with self._lock:
            start_time = time.perf_counter()
            norm_query = normalize_alias_text(query)

            if isinstance(context, dict):
                ctx_obj = QueryContext.from_dict(context)
            elif isinstance(context, QueryContext):
                ctx_obj = context
            else:
                ctx_obj = QueryContext()

            candidates_map: Dict[Tuple[int, int], RetrievedCandidate] = {}

            # Tier 1: Exact alias lookup
            exact_matches = self.get_exact_alias(norm_query, context=ctx_obj)
            for c in exact_matches:
                candidates_map[(c.concept_id, c.alias_id)] = c

            # Tier 2: Unicode FTS retrieval if more candidates needed
            if len(candidates_map) < max_candidates:
                u_matches = self.search_unicode(norm_query, context=ctx_obj, max_candidates=max_candidates)
                for c in u_matches:
                    key = (c.concept_id, c.alias_id)
                    if key not in candidates_map:
                        candidates_map[key] = c

            # Tier 3: Trigram substring fallback (queries >= 3 chars)
            if len(candidates_map) < max_candidates and len(norm_query) >= 3:
                t_matches = self.search_trigram(norm_query, context=ctx_obj, max_candidates=max_candidates)
                for c in t_matches:
                    key = (c.concept_id, c.alias_id)
                    if key not in candidates_map:
                        candidates_map[key] = c

            # Short query fallback (< 3 chars): prefix lookup in concept_aliases
            if not candidates_map and 0 < len(norm_query) < 3:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT 
                        ca.alias_id, ca.alias_text, ca.alias_source, ca.is_abbreviation, ca.is_preferred,
                        c.concept_id, c.system, c.code, c.canonical_name, c.tty, c.attributes_json
                    FROM concept_aliases ca
                    JOIN concepts c ON ca.concept_id = c.concept_id
                    WHERE ca.alias_text LIKE ?
                    ORDER BY ca.is_preferred DESC, c.concept_id ASC
                    LIMIT ?
                    """,
                    (f"{norm_query}%", max_candidates),
                )
                for row in cursor.fetchall():
                    alias_id = row["alias_id"]
                    rules = self._rules_cache.get(alias_id, ())
                    ctx_status, ctx_reasons = evaluate_context_rules(rules, ctx_obj)
                    try:
                        attribs = json.loads(row["attributes_json"])
                    except Exception:
                        attribs = {}
                    c = RetrievedCandidate(
                        concept_id=row["concept_id"],
                        system=row["system"],
                        code=row["code"],
                        canonical_name=row["canonical_name"],
                        tty=row["tty"],
                        attributes=attribs,
                        alias_id=alias_id,
                        matched_alias=row["alias_text"],
                        alias_source=row["alias_source"],
                        is_abbreviation=bool(row["is_abbreviation"]),
                        is_preferred=bool(row["is_preferred"]),
                        retrieval_method=RetrievalMethod.EXACT_DB,
                        rank_score=0.8,
                        raw_bm25_score=None,
                        context_status=ctx_status,
                        context_reasons=ctx_reasons,
                    )
                    candidates_map[(c.concept_id, c.alias_id)] = c

            all_candidates = list(candidates_map.values())

            # Deterministic ranking sort key:
            # 1. context_status: PASSED or NO_RULES before ABSTAINED or FAILED
            # 2. rank_score: descending (1.0 exact > FTS)
            # 3. is_preferred: descending (preferred canonical aliases first)
            # 4. concept_id: ascending (lowest ID first)
            # 5. alias_id: ascending
            def sort_key(c: RetrievedCandidate) -> tuple:
                # Context tier: valid context first
                ctx_tier = 0 if c.context_status in (ContextStatus.PASSED, ContextStatus.NO_RULES) else 1
                return (ctx_tier, -c.rank_score, -int(c.is_preferred), c.concept_id, c.alias_id)

            all_candidates.sort(key=sort_key)
            final_candidates = all_candidates[:max_candidates]

            # Determine ambiguity and abstention status
            valid_candidates = [c for c in final_candidates if c.context_status in (ContextStatus.PASSED, ContextStatus.NO_RULES)]
            abstained_candidates = [
                c for c in final_candidates
                if c.context_status in (ContextStatus.ABSTAINED_MISSING_CONTEXT, ContextStatus.ABSTAINED_CONFLICTING_CONTEXT)
            ]

            distinct_valid_concepts = {c.concept_id for c in valid_candidates}
            is_ambiguous = len(distinct_valid_concepts) > 1

            is_abstained = (len(valid_candidates) == 0 and len(abstained_candidates) > 0)
            abstention_reasons: List[str] = []
            if is_abstained:
                for c in abstained_candidates:
                    abstention_reasons.extend(c.context_reasons)
                abstention_reasons = list(dict.fromkeys(abstention_reasons))

            duration_ms = (time.perf_counter() - start_time) * 1000.0

            return RetrievalResponse(
                query=query,
                normalized_query=norm_query,
                candidates=final_candidates,
                is_ambiguous=is_ambiguous,
                is_abstained=is_abstained,
                abstention_reasons=abstention_reasons,
                database_version=self.db_version,
                source_releases={
                    "LOINC": self.metadata.get("loinc_release_id", "UNKNOWN"),
                    "RxNorm": self.metadata.get("rxnorm_release_id", "UNKNOWN"),
                },
                query_duration_ms=duration_ms,
            )

    def close(self) -> None:
        """Closes SQLite connection under lock."""
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass


# ─── Process-Local Retrieval Service Lifecycle (Phase 3D.3) ───────────────────

_PROCESS_TERMINOLOGY_SERVICE: Optional[TerminologyRetrievalService] = None
_PROCESS_SERVICE_LOCK = threading.Lock()
DEFAULT_TERMINOLOGY_DIR = Path("data/terminology")


def get_process_terminology_service(
    data_dir: Optional[Union[Path, str]] = None,
    pointer_name: str = "active_terminology.json",
    preload_cache: bool = False,
) -> TerminologyRetrievalService:
    """Retrieve or lazily initialize the process-local TerminologyRetrievalService.

    Thread-safe and process-isolated. Reuses the open SQLite connection and in-memory
    caches across jobs within the same OS process without redundant database reloads.
    Fails fast with FileNotFoundError or DatabaseError if the pointer or database is invalid.
    If the service is already initialized and the effective requested directory or pointer_name
    conflicts with the existing instance, raises ValueError without mutating the existing service.
    """
    global _PROCESS_TERMINOLOGY_SERVICE

    import os
    raw_dir = data_dir or os.environ.get("CLINDOC_TERMINOLOGY_DIR")
    target_dir = Path(raw_dir) if raw_dir else DEFAULT_TERMINOLOGY_DIR
    target_path = target_dir.resolve()

    def _validate_effective_config(svc: TerminologyRetrievalService) -> None:
        existing_path = getattr(svc, "input_path", None)
        if existing_path is not None:
            if existing_path.resolve() != target_path:
                raise ValueError(
                    f"Process-local terminology service is already initialized with directory '{svc.input_path}' "
                    f"(resolved: '{existing_path.resolve()}'). Cannot return service for conflicting effective directory '{target_dir}' "
                    f"(resolved: '{target_path}'). Call reset_process_terminology_service() first if directory switch is intended."
                )
        existing_pointer = getattr(svc, "pointer_name", None)
        if existing_pointer is not None and existing_pointer != pointer_name:
            raise ValueError(
                f"Process-local terminology service is already initialized with pointer_name '{svc.pointer_name}'. "
                f"Cannot return service for conflicting pointer_name '{pointer_name}'. "
                f"Call reset_process_terminology_service() first if pointer switch is intended."
            )

    # Fast path check outside lock
    if _PROCESS_TERMINOLOGY_SERVICE is not None:
        _validate_effective_config(_PROCESS_TERMINOLOGY_SERVICE)
        return _PROCESS_TERMINOLOGY_SERVICE

    with _PROCESS_SERVICE_LOCK:
        if _PROCESS_TERMINOLOGY_SERVICE is not None:
            _validate_effective_config(_PROCESS_TERMINOLOGY_SERVICE)
            return _PROCESS_TERMINOLOGY_SERVICE

        _PROCESS_TERMINOLOGY_SERVICE = TerminologyRetrievalService(
            db_path_or_dir=target_dir,
            pointer_name=pointer_name,
            preload_cache=preload_cache,
        )
        return _PROCESS_TERMINOLOGY_SERVICE


def set_process_terminology_service(
    service: Optional[TerminologyRetrievalService],
    force: bool = False,
) -> None:
    """Set or override the process-local TerminologyRetrievalService (for testing/lifecycle).

    Safety Rules:
      - Default replacement (force=False) strictly rejects replacing an active, live service
        to avoid closing or orphaning a connection while concurrent workers/requests are active.
      - If force=True is passed, the caller explicitly guarantees that the process has quiesced
        all users of the old service. Only then is the superseded service closed and replaced.
      - To register a new service cleanly, callers should invoke reset_process_terminology_service()
        first, ensuring explicit teardown before new registration.
    """
    global _PROCESS_TERMINOLOGY_SERVICE
    with _PROCESS_SERVICE_LOCK:
        if _PROCESS_TERMINOLOGY_SERVICE is not None and service is not _PROCESS_TERMINOLOGY_SERVICE:
            if not force:
                raise ValueError(
                    "Cannot overwrite an active process-local terminology service while in use. "
                    "Call reset_process_terminology_service() after quiescing all active workers/requests, "
                    "or pass force=True only when quiescence is guaranteed."
                )
            # Confirmed force replacement: close previous connection
            try:
                _PROCESS_TERMINOLOGY_SERVICE.close()
            except Exception as e:
                logger.warning("Error closing superseded process terminology service: %s", e)

        _PROCESS_TERMINOLOGY_SERVICE = service
        import sys
        if "clindoc.api.deps" in sys.modules:
            sys.modules["clindoc.api.deps"]._GLOBAL_TERMINOLOGY_SERVICE = service


def reset_process_terminology_service() -> None:
    """Safely close and clear the process-local TerminologyRetrievalService."""
    global _PROCESS_TERMINOLOGY_SERVICE
    with _PROCESS_SERVICE_LOCK:
        if _PROCESS_TERMINOLOGY_SERVICE is not None:
            try:
                _PROCESS_TERMINOLOGY_SERVICE.close()
            except Exception as e:
                logger.warning(f"Error closing process terminology service: {e}")
            _PROCESS_TERMINOLOGY_SERVICE = None
        import sys
        if "clindoc.api.deps" in sys.modules:
            sys.modules["clindoc.api.deps"]._GLOBAL_TERMINOLOGY_SERVICE = None

