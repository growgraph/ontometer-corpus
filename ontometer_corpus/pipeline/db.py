"""
SQLite corpus registry.

About:
    Defines the corpus_registry and fetch_attempts table schemas, provides a
    connection factory, and handles idempotent seeding from manifest.jsonl
    (Phase 0).

    The corpus_registry is the single source of truth for the processing
    pipeline.  One row per ontology file version, covering:
      - Raw file provenance (from manifest; Phase 0 seed)
      - Parse status and extracted metadata (Phase 1 parse)
      - Deduplication decisions (Phase 2 dedup)

    The database lives in the corpus directory as ``<corpus>/corpus.db``
    (see ``CorpusPaths.db_path``).

    Columns added after the initial schema are applied to existing databases
    by _migrate_schema (guarded ALTER TABLE), so additive schema changes never
    need a re-seed. Registry metadata is projected to a field allowlist before
    storage, and parse errors are bounded (see MAX_PARSE_ERROR_CHARS).
"""

import json
import sqlite3
from pathlib import Path

from suthing import iter_jsonl, utc_now_iso

from ontometer_corpus.metadata import project_registry_metadata, redact_secrets

# Registries excluded from seeding. manifest.jsonl is append-only and never
# rewritten (see seed_from_manifest), so entries from these registries can
# remain in it; this set keeps them out of fetch_attempts/corpus_registry.
_EXCLUDED_REGISTRIES: set[str] = {"LODCloud"}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS corpus_registry (

    -- Primary key
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Provenance from manifest.jsonl (FetchRecord fields)
    source_id             TEXT NOT NULL,
    source_registry       TEXT NOT NULL,
    canonical_url         TEXT NOT NULL,
    retrieved_file        TEXT NOT NULL,     -- FK → content-addressed object (many rows may share one)
    file_hash             TEXT,              -- SHA-256 of raw bytes (object-store key)
    source_version        TEXT,              -- registry-declared version label (e.g. LOV "v0.99")
    source_version_date   TEXT,              -- ISO publication date of the version (for recency ordering)
    retrieval_date        TEXT NOT NULL,
    source_registry_metadata TEXT,           -- JSON blob (raw registry entry)

    -- Phase 1: Parse
    parse_status          TEXT,              -- NULL | 'ok' | 'fail' | 'feed' | 'unsupported'
    parse_error           TEXT,
    parse_date            TEXT,              -- ISO-8601 UTC
    parse_format          TEXT,              -- rdflib format string that succeeded

    -- Phase 1: Ontology identity (extracted from inside the file)
    ontology_iri          TEXT,              -- owl:Ontology subject IRI
    ontology_version_iri  TEXT,              -- owl:versionIRI value
    ontology_version_info TEXT,              -- owl:versionInfo literal

    -- Phase 1: Structural metrics
    triple_count          INTEGER,
    class_count           INTEGER,
    property_count        INTEGER,
    individual_count      INTEGER,           -- named subjects declared owl:NamedIndividual or typed by a domain type (SKOS concepts included)
    declared_individual_count INTEGER,       -- the owl:NamedIndividual-declared subset
    skos_concept_count    INTEGER,           -- the subset typed skos:Concept
    imports               TEXT,              -- JSON array of owl:imports IRIs

    -- Phase 1: Dedup keys
    content_hash          TEXT,              -- SHA-256(sorted class+property IRIs) — coarse, serialisation-independent

    -- Phase 1: Namespace footprint (for the dependency-network usage edges)
    referenced_namespaces TEXT,              -- JSON array: namespaces of every referenced URIRef term (s/p/o)
    own_namespaces        TEXT,              -- JSON array: namespaces of the ontology's own declared entities + its IRI
    iri_source            TEXT,              -- 'declared' | 'registry' | NULL — how ontology_iri was established
    licence               TEXT,              -- JSON list of raw licence declarations on the header

    -- Phase 2: Deduplication
    canonical_entry_id    INTEGER,           -- FK → id of the winning row (NULL = this IS canonical)
    is_duplicate          INTEGER DEFAULT 0, -- 1 = superseded by canonical_entry_id
    shared_iri            INTEGER DEFAULT 0, -- 1 = ontology_iri is declared by several distinct ontologies, so it is no identity (set by dedup)
    is_latest_version     INTEGER DEFAULT 0, -- 1 = most-recent canonical version of its ontology (filter target for version-agnostic experiments)
    home_registry         TEXT,              -- registry the ontology belongs to for per-registry statistics (set by dedup; see ontometer_corpus.registries)


    -- Bookkeeping
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cr_source_id
    ON corpus_registry(source_id);
CREATE INDEX IF NOT EXISTS idx_cr_ontology_iri
    ON corpus_registry(ontology_iri);
CREATE INDEX IF NOT EXISTS idx_cr_content_hash
    ON corpus_registry(content_hash);
CREATE INDEX IF NOT EXISTS idx_cr_file_hash
    ON corpus_registry(file_hash);
CREATE INDEX IF NOT EXISTS idx_cr_parse_status
    ON corpus_registry(parse_status);
CREATE INDEX IF NOT EXISTS idx_cr_is_duplicate
    ON corpus_registry(is_duplicate);
CREATE INDEX IF NOT EXISTS idx_cr_is_latest_version
    ON corpus_registry(is_latest_version);
CREATE INDEX IF NOT EXISTS idx_cr_source_registry
    ON corpus_registry(source_registry);

-- ---------------------------------------------------------------------------
-- fetch_attempts: one row per fetch ATTEMPT (success or failure).
--
-- corpus_registry only holds ontologies we successfully retrieved. To measure
-- what fraction of each registry we actually captured we also need the misses,
-- so every attempt (including dead URLs, HTML-instead-of-RDF, etc.) is recorded
-- here. This is the source of the per-registry capture-rate statistic.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL,
    source_registry   TEXT NOT NULL,
    canonical_url     TEXT,
    status            TEXT NOT NULL,         -- 'ok' | 'fail'
    file_hash         TEXT,                  -- raw-bytes SHA-256 (ok only)
    retrieved_file    TEXT,                  -- object-store path (ok only)
    source_version    TEXT,                  -- registry-declared version label
    error             TEXT,                  -- failure reason (fail only)
    retrieval_date    TEXT NOT NULL,
    created_at        TEXT NOT NULL,

    -- Idempotent seeding: re-seeding the same manifest never duplicates rows.
    UNIQUE(source_id, retrieval_date, status)
);

CREATE INDEX IF NOT EXISTS idx_fa_source_registry
    ON fetch_attempts(source_registry);
CREATE INDEX IF NOT EXISTS idx_fa_status
    ON fetch_attempts(status);
"""


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the SQLite corpus database.

    Returns a connection with:
    - WAL journal for concurrent read safety
    - Row factory set to sqlite3.Row (column-name access)
    - Foreign keys enabled
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# The `kind` of a parsed row:
#   'no_skos'      no SKOS concepts;
#   'thesaurus'    SKOS concepts and no OWL/RDFS classes;
#   'mostly_skos'  at least as many SKOS concepts as classes;
#   'mostly_owl'   fewer SKOS concepts than classes (an OWL ontology with a
#                  code list).
# The first two SKOS kinds are SKOS-dominated (role_names.SKOS_DOMINATED_KINDS):
# included in the corpus inventory and the dependency network, excluded from
# the hierarchy-based analyses, which assume an OWL class hierarchy.
_KIND_SQL = (
    "CASE WHEN parse_status IS NOT 'ok' THEN NULL"
    " WHEN COALESCE(skos_concept_count, 0) = 0 THEN 'no_skos'"
    " WHEN COALESCE(class_count, 0) = 0 THEN 'thesaurus'"
    " WHEN skos_concept_count >= class_count THEN 'mostly_skos'"
    " ELSE 'mostly_owl' END"
)

# Columns added after the initial schema shipped. CREATE TABLE IF NOT EXISTS
# never alters an existing table, so these are applied to already-populated
# databases via guarded ALTER TABLE (see _migrate_schema). Each entry is
# (column_name, column_definition). `kind` exists only here: a generated
# column needs _KIND_SQL, and fresh databases get it from the same migration.
_ADDED_COLUMNS: list[tuple[str, str]] = [
    ("referenced_namespaces", "TEXT"),
    ("own_namespaces", "TEXT"),
    # How ontology_iri was established: 'declared' (the file asserts it via
    # owl:Ontology), 'registry' (recovered from the registry's source_id
    # because the file declares none — see parse._registry_declared_iri), or
    # NULL for rows written by a parser path that doesn't report provenance.
    ("iri_source", "TEXT"),
    # Subsets of individual_count, which counts typed as well as declared
    # individuals (see parse._individual_counts). NULL for rows parsed before
    # the typed count existed — their individual_count is the declared count.
    ("declared_individual_count", "INTEGER"),
    ("skos_concept_count", "INTEGER"),
    # What kind of source a parsed row is, from its class and SKOS concept
    # counts (see _KIND_SQL). Generated, so it can never disagree with them.
    ("kind", f"TEXT GENERATED ALWAYS AS ({_KIND_SQL}) VIRTUAL"),
    # Set by dedup's latest-version pass: the row's ontology_iri is shared by
    # distinct ontologies, so network nodes use row:<id> instead.
    ("shared_iri", "INTEGER DEFAULT 0"),
    # Set by dedup: the registry an ontology belongs to for per-registry
    # statistics, which is not always the registry this copy was fetched from
    # (see ontometer_corpus.registries).
    ("home_registry", "TEXT"),
    # Raw licence declarations on the ontology header (JSON list of IRIs or
    # literals; [] = none declared, NULL = parsed before capture existed).
    ("licence", "TEXT"),
]


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Additively bring an existing corpus_registry up to the current schema.

    SQLite's CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so
    columns introduced after the DB was first created must be added with
    ALTER TABLE. This is guarded against PRAGMA table_info so it's idempotent
    and safe to run on every startup (fresh DBs already have the columns from
    _DDL and skip every ALTER).
    """
    # table_xinfo, not table_info: only it lists generated columns (`kind`).
    existing = {row["name"] for row in conn.execute("PRAGMA table_xinfo(corpus_registry)")}
    for name, definition in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE corpus_registry ADD COLUMN {name} {definition}")
    conn.commit()


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes (idempotent — safe to call on every run)."""
    conn.executescript(_DDL)
    conn.commit()
    _migrate_schema(conn)


# ---------------------------------------------------------------------------
# Manifest seeder
# ---------------------------------------------------------------------------


def seed_from_manifest(conn: sqlite3.Connection, manifest_path: Path) -> dict:
    """
    Read manifest.jsonl and seed both tables from it:

      * fetch_attempts — EVERY attempt (status ok and fail), so capture rates
        are queryable. Idempotent via UNIQUE(source_id, retrieval_date, status).
      * corpus_registry — only successful attempts (status == 'ok'), one row
        per source_id. Idempotent on source_id (re-runs never duplicate rows).

    A record without a ``status`` field is treated as successful ('ok').

    Args:
        conn: Open SQLite connection with schema already created.
        manifest_path: Path to the manifest.jsonl written by the fetcher
            orchestrator.

    Returns:
        Counts keyed ``corpus_inserted``, ``corpus_skipped`` and
        ``attempts_inserted``.
    """
    counts = {"corpus_inserted": 0, "corpus_skipped": 0, "attempts_inserted": 0}

    if not manifest_path.exists():
        print(f"  [warn] manifest not found: {manifest_path}")
        return counts

    now = _now()

    # Bad lines are logged with their line number and skipped.
    for rec in iter_jsonl(manifest_path, strict=False):
        source_id = rec.get("source_id", "")
        if not source_id:
            continue

        if rec.get("source_registry", "") in _EXCLUDED_REGISTRIES:
            # Registries excluded from seeding: manifest.jsonl is never
            # truncated or rewritten, so their entries can remain in it.
            # This skip keeps them out of fetch_attempts / corpus_registry.
            continue

        status = rec.get("status", "ok")
        retrieval_date = rec.get("retrieval_date", now)

        # ---- fetch_attempts: record every attempt (ok + fail) ----------
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO fetch_attempts (
                source_id, source_registry, canonical_url, status,
                file_hash, retrieved_file, source_version, error,
                retrieval_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                rec.get("source_registry", ""),
                redact_secrets(rec.get("canonical_url", "")),
                status,
                rec.get("file_hash") or None,
                rec.get("retrieved_file") or None,
                rec.get("source_version") or None,
                # A manifest written before redaction can hold keys in URLs.
                redact_secrets(rec.get("error")) or None,
                retrieval_date,
                now,
            ),
        )
        if cur.rowcount:
            counts["attempts_inserted"] += 1

        # ---- corpus_registry: successful fetches only ------------------
        if status != "ok":
            continue

        retrieved_file = rec.get("retrieved_file", "")
        if not retrieved_file:
            continue

        existing = conn.execute(
            "SELECT id FROM corpus_registry WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if existing:
            counts["corpus_skipped"] += 1
            continue

        conn.execute(
            """
            INSERT INTO corpus_registry (
                source_id, source_registry, canonical_url,
                retrieved_file, file_hash, source_version, source_version_date,
                retrieval_date,
                source_registry_metadata,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                rec.get("source_registry", ""),
                rec.get("canonical_url", ""),
                retrieved_file,
                rec.get("file_hash") or None,
                rec.get("source_version") or None,
                rec.get("source_version_date") or None,
                retrieval_date,
                # Projected here as well as at fetch time: a manifest may
                # hold the full registry response, and seeding from it must
                # not put that response into the database.
                json.dumps(
                    project_registry_metadata(rec.get("source_registry_metadata", {})),
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        counts["corpus_inserted"] += 1

    conn.commit()
    print(
        f"  Manifest seed: corpus +{counts['corpus_inserted']} "
        f"({counts['corpus_skipped']} already present), "
        f"attempts +{counts['attempts_inserted']}"
    )
    return counts


def fetch_capture_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return per-registry fetch capture statistics from fetch_attempts.

    Each row has: source_registry, captured, failed, attempts, pct_captured.
    'pct_captured' is captured / attempts as a percentage (one decimal place).
    """
    return conn.execute(
        """
        SELECT
            source_registry,
            SUM(status = 'ok')                                  AS captured,
            SUM(status = 'fail')                                AS failed,
            COUNT(*)                                            AS attempts,
            ROUND(100.0 * SUM(status = 'ok') / COUNT(*), 1)     AS pct_captured
        FROM fetch_attempts
        GROUP BY source_registry
        ORDER BY source_registry
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Write-back helpers
# ---------------------------------------------------------------------------


def update_parse_ok(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    parse_format: str,
    ontology_iri: str | None,
    ontology_version_iri: str | None,
    ontology_version_info: str | None,
    triple_count: int,
    class_count: int,
    property_count: int,
    individual_count: int,
    imports: list[str],
    content_hash: str | None,
    referenced_namespaces: list[str] | None = None,
    own_namespaces: list[str] | None = None,
    iri_source: str | None = None,
    declared_individual_count: int | None = None,
    skos_concept_count: int | None = None,
    licence: list[str] | None = None,
) -> None:
    """Write a successful parse result back to corpus_registry."""
    now = _now()
    conn.execute(
        """
        UPDATE corpus_registry SET
            parse_status          = 'ok',
            parse_error           = NULL,
            parse_date            = ?,
            parse_format          = ?,
            ontology_iri          = ?,
            ontology_version_iri  = ?,
            ontology_version_info = ?,
            triple_count          = ?,
            class_count           = ?,
            property_count        = ?,
            individual_count      = ?,
            declared_individual_count = ?,
            skos_concept_count    = ?,
            imports               = ?,
            content_hash          = ?,
            referenced_namespaces = ?,
            own_namespaces        = ?,
            iri_source            = ?,
            licence               = ?,
            updated_at            = ?
        WHERE id = ?
        """,
        (
            now,
            parse_format,
            ontology_iri,
            ontology_version_iri,
            ontology_version_info,
            triple_count,
            class_count,
            property_count,
            individual_count,
            declared_individual_count,
            skos_concept_count,
            json.dumps(imports),
            content_hash,
            json.dumps(referenced_namespaces) if referenced_namespaces is not None else None,
            json.dumps(own_namespaces) if own_namespaces is not None else None,
            iri_source,
            json.dumps(licence) if licence is not None else None,
            now,
            row_id,
        ),
    )
    conn.commit()


#: Longest parse error retained. Parser messages (e.g. rdflib's "Invalid
#: line:" errors) can echo the offending input back into the message, so an
#: unbounded column grows with the size of the file that failed.
MAX_PARSE_ERROR_CHARS = 2000

_TRUNCATION_NOTE = "... [truncated]"


def _clamp_error(error: str | None) -> str | None:
    """Bound a parser error message before it is stored.

    Parser messages can echo the whole input; the first 2,000 characters carry
    the diagnosis.
    """
    if error is None or len(error) <= MAX_PARSE_ERROR_CHARS:
        return error
    return error[:MAX_PARSE_ERROR_CHARS] + _TRUNCATION_NOTE


def update_parse_fail(
    conn: sqlite3.Connection,
    row_id: int,
    error: str,
) -> None:
    """Write a parse failure back to corpus_registry."""
    now = _now()
    conn.execute(
        """
        UPDATE corpus_registry SET
            parse_status = 'fail',
            parse_error  = ?,
            parse_date   = ?,
            updated_at   = ?
        WHERE id = ?
        """,
        (_clamp_error(error), now, now, row_id),
    )
    conn.commit()


def update_parse_feed(
    conn: sqlite3.Connection,
    row_id: int,
    error: str,
) -> None:
    """Write a feed parse status back to corpus_registry."""
    now = _now()
    conn.execute(
        """
        UPDATE corpus_registry SET
            parse_status = 'feed',
            parse_error  = ?,
            parse_date   = ?,
            updated_at   = ?
        WHERE id = ?
        """,
        (_clamp_error(error), now, now, row_id),
    )
    conn.commit()


def update_parse_unsupported(
    conn: sqlite3.Connection,
    row_id: int,
    error: str,
) -> None:
    """Write an unsupported parse status back to corpus_registry."""
    now = _now()
    conn.execute(
        """
        UPDATE corpus_registry SET
            parse_status = 'unsupported',
            parse_error  = ?,
            parse_date   = ?,
            updated_at   = ?
        WHERE id = ?
        """,
        (_clamp_error(error), now, now, row_id),
    )
    conn.commit()


def update_duplicate(
    conn: sqlite3.Connection,
    row_id: int,
    canonical_entry_id: int,
) -> None:
    """Mark a row as a duplicate pointing to the canonical winner."""
    now = _now()
    conn.execute(
        """
        UPDATE corpus_registry SET
            is_duplicate       = 1,
            canonical_entry_id = ?,
            updated_at         = ?
        WHERE id = ?
        """,
        (canonical_entry_id, now, row_id),
    )
    conn.commit()


#: Columns the parse and dedup phases fill; clearing them makes every row parse again.
_PARSE_COLUMNS = (
    "parse_status",
    "parse_error",
    "parse_date",
    "parse_format",
    "ontology_iri",
    "ontology_version_iri",
    "ontology_version_info",
    "triple_count",
    "class_count",
    "property_count",
    "individual_count",
    "declared_individual_count",
    "skos_concept_count",
    "imports",
    "content_hash",
    "iri_source",
    "referenced_namespaces",
    "own_namespaces",
    "canonical_entry_id",
    "licence",
)


def reset_parse_state(conn: sqlite3.Connection) -> int:
    """Clear every parse- and dedup-derived column so the next parse re-reads all rows.

    Seeded columns and structural sidecars are kept; sidecars are write-once per
    file hash. Returns the number of rows reset.
    """
    assignments = ", ".join(f"{c} = NULL" for c in _PARSE_COLUMNS)
    cur = conn.execute(
        f"UPDATE corpus_registry SET {assignments}, "
        "is_duplicate = 0, is_latest_version = 0, shared_iri = 0"
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Dedup exclusion log
# ---------------------------------------------------------------------------


def log_dedup_exclusion(log_path: Path, source_id: str, reason: str, detail: str = "") -> None:
    """
    Append a record of a row that was excluded (or only partially covered) by
    a dedup identity check, so there's a durable trail even though the
    exclusion itself only shows as an absence in corpus_registry.

    Used for: rows with zero classes/properties excluded from Pass B's
    content-hash grouping (dedup.py).
    """
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{_now()}\t{source_id}\t{reason}\t{detail}\n")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    # "auto" matches the timestamps already stored in corpus.db.
    return utc_now_iso(timespec="auto")
