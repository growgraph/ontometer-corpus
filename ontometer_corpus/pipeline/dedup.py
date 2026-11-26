"""
Phase 2: Dedup.

About:
    Identifies duplicate ontologies across registries and marks non-canonical
    rows in corpus_registry (is_duplicate=1, canonical_entry_id=<winner.id>),
    then flags one latest canonical representative per release family.

    Version tracking:
        Different versions of the same ontology are preserved as separate
        canonical rows. Only rows that represent the SAME version fetched
        from multiple registries are collapsed as duplicates.

    Passes, in order:

    Pass 0 — Exact-byte dedup (file_hash)
        Groups all parse_status='ok' rows by file_hash (SHA-256 of the raw
        fetched bytes). Byte-identical files are unambiguously the same
        content regardless of any parsed metadata, so this needs no guard —
        it is the strongest, cheapest identity signal and collapses mirrors
        serving the exact same file before either semantic pass runs.

    Pass A — Version-aware IRI dedup
        Groups all remaining is_duplicate=0 rows by (ontology_iri, ontology_version_iri).
        Rows with the same IRI but DIFFERENT version IRI are distinct versions
        and are NEVER marked as duplicates.
        Rows where ontology_version_iri IS NULL are preserved: a stable ontology
        IRI and class/property signature do not prove that two files are the same
        release, because relationships, individuals and annotations may change.

    Pass B — Content-hash dedup
        Groups remaining rows by content_hash (the coarse class+property IRI
        set), but only an identical, non-null ontology_version_iri is strong
        enough to mark a duplicate. Rows with no classes or
        properties are excluded (they would all share the empty-set hash) and
        logged to dedup_exclusions.log.

    Pass C — Release-family selection
        Groups the remaining canonical rows by release family (the fetch
        identifier before its final ``::``), then sets is_latest_version=1 on
        the most recent representative in each family. Earlier releases stay
        canonical so longitudinal analyses retain their full history.

    Pass D — One latest row per ontology
        Release families are per registry, so Pass C keeps one latest row per
        registry: RO arrives as obo:ro, agroportal:RO and okg:Q28729320, each
        a different release. Two latest rows are one ontology when either
        holds:
          * they share an ontology IRI (``ontology_key``: scheme, trailing
            separator and OBO build artifacts such as ``x/x-full.owl`` or
            ``x-base.owl`` ignored) and their declared terms (structural
            sidecars) overlap: across registries, at least CONTAINMENT_SAME
            of the smaller copy's terms are in the larger, since a registry
            often holds an older, smaller release; within one registry, which
            lists them as separate entries, the two sets must overlap by
            CONTAINMENT_SAME of their union, so a module or extract published
            under its parent's IRI stays its own ontology. Two releases under
            the ontology's own OBO PURL that share a term, and a copy
            declaring nothing, also join;
          * one of them, or an earlier canonical release of its family,
            declares the identical non-empty class and property set
            (``content_hash``) as the other or one of its family's releases,
            with or without an IRI (``_same_terms``): a copy of an old
            release is that ontology even after a release renamed its terms.
        Only the newest of each such group keeps is_latest_version (see
        _newness_key). When an IRI covers several distinct ontologies (a
        placeholder such as http://iri/, or a tool's default IRI reused by
        several projects), the IRI is no identity: those rows get
        shared_iri=1 and become their own network nodes (row:<id>). The IRI
        evidence needs the sidecar store; without it the pass is skipped with
        a warning.

    Pass E — An ontology declares something
        A latest row declaring no class, property, individual or SKOS concept
        (an import-only aggregator, an empty submission, a failed conversion)
        loses is_latest_version: it is a pointer, not an ontology.

    Winner election within a group (no registry is treated as inherently more
    authoritative than another):
        1. Highest triple_count
        2. Tie-break: latest retrieval_date (lexicographic ISO-8601)
        3. Tie-break: lowest row id (first one seeded, purely for determinism)

    Raw-byte, declared-IRI, content-hash, and release-family checks avoid a
    graph-isomorphism digest, whose rdflib canonicalisation can run unboundedly
    long on blank-node-dense graphs.
"""

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from ontometer_corpus.namespaces import OBO_BASE_NS

from ..registries import assign_home_registries, iri_key
from ..versions import family_key, release_date
from .db import get_connection, log_dedup_exclusion, update_duplicate

# content_hash of an ontology that contributed no class/property IRIs at all:
# SHA-256 of the empty canonical string. Every entity-less ontology produces
# this same value (SKOS thesauri, instance dumps, pre-OWL vocabularies), so
# grouping on it would collapse all of those unrelated rows into one.
#
# Pass B also excludes rows via `(class_count + property_count) = 0`, but
# that is an INDIRECT proxy: it assumes the counters and compute_content_hash
# count exactly the same entity types. If the two definitions drift apart,
# empty-hash rows would enter Pass B and collapse falsely. This constant checks
# the hash itself, so the guard holds regardless of the counters.
_EMPTY_CONTENT_HASH = hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Election helpers
# ---------------------------------------------------------------------------


def _elect_winner(rows: list) -> int:
    """
    Return the id of the canonical winner from a list of sqlite3.Row objects.

    Preference order: highest triple_count -> latest retrieval_date -> lowest
    row id (a stable, arbitrary-but-deterministic final tie-break). No
    registry/source is treated as inherently more authoritative than another.

    Implemented as three successive stable sorts, least-significant criterion
    first, so each later sort's ties are broken by the previous pass's order.
    """
    ordered = sorted(rows, key=lambda r: r["id"])
    ordered = sorted(ordered, key=lambda r: r["retrieval_date"] or "", reverse=True)
    ordered = sorted(ordered, key=lambda r: r["triple_count"] or 0, reverse=True)
    return ordered[0]["id"]


def _dup_key(row) -> str | None:
    """Content-identity key used to group duplicates: the coarse
    class+property content_hash."""
    return row["content_hash"]


_TERM_COUNTS = (
    "class_count",
    "property_count",
    "individual_count",
    "declared_individual_count",
    "skos_concept_count",
)


def _is_stub(row) -> bool:
    """A parsed copy with no classes, properties, individuals or SKOS concepts
    (a failed conversion, an empty submission): never the copy to measure
    while a populated one exists."""
    return not any(row[c] for c in _TERM_COUNTS)


def _flatten_canonical_chains(conn) -> int:
    """Point every duplicate at its final canonical row; returns rows repointed.

    Passes run in sequence, so a row marked duplicate of X can see X marked
    duplicate of Y later. A cycle (never written by these passes) stops the walk.
    """
    parent = dict(
        conn.execute(
            "SELECT id, canonical_entry_id FROM corpus_registry "
            "WHERE is_duplicate = 1 AND canonical_entry_id IS NOT NULL"
        ).fetchall()
    )
    moved = 0
    for row_id, target in parent.items():
        root, seen = target, {row_id}
        while root in parent and root not in seen:
            seen.add(root)
            root = parent[root]
        if root != target:
            conn.execute(
                "UPDATE corpus_registry SET canonical_entry_id = ? WHERE id = ?", (root, row_id)
            )
            moved += 1
    conn.commit()
    return moved


def _newest_duplicate_dates(conn) -> dict[int, str]:
    """Canonical row id -> newest ``source_version_date`` among its duplicates in
    the same release family."""
    newest: dict[int, str] = {}
    rows = conn.execute(
        """
        SELECT d.source_id, c.source_id, c.id, d.source_version_date
        FROM corpus_registry d JOIN corpus_registry c ON c.id = d.canonical_entry_id
        WHERE d.is_duplicate = 1 AND d.source_version_date IS NOT NULL
        """
    ).fetchall()
    for dup_source, canon_source, canon_id, date in rows:
        if family_key(dup_source) == family_key(canon_source) and date > newest.get(canon_id, ""):
            newest[canon_id] = date
    return newest


# ---------------------------------------------------------------------------
# Phase 2 entry point
# ---------------------------------------------------------------------------


def run_dedup(
    db_path: Path,
    exclusion_log: Path | None = None,
    sidecar_path: Callable[[str], Path] | None = None,
) -> dict:
    """
    Run all deduplication passes on the corpus_registry.

    Args:
        db_path: Path to corpus.db.
        exclusion_log: Where rows excluded from Pass B are logged. Defaults to
            ``dedup_exclusions.log`` next to corpus.db.
        sidecar_path: Maps a content hash to its structural sidecar
            (``CorpusPaths.sidecar_path``). Pass D needs it; without it the
            pass is skipped.

    Returns:
        Summary counts keyed ``pass_0_marked``, ``pass_a_marked``,
        ``pass_b_marked``, ``latest_flagged`` (after Passes D and E),
        ``latest_unflagged``, ``shared_iris``, ``iris_undecided`` and
        ``stubs_dropped``.
    """
    conn = get_connection(db_path)
    exclusion_log_path = exclusion_log or db_path.parent / "dedup_exclusions.log"
    counts = {"pass_0_marked": 0, "pass_a_marked": 0, "pass_b_marked": 0}

    # These flags are wholly derived by this phase. Recompute them from the
    # parsed evidence so a rule correction can restore rows that an earlier
    # run classified as duplicates.
    conn.execute(
        """
        UPDATE corpus_registry SET
            canonical_entry_id = NULL,
            is_duplicate = 0,
            shared_iri = 0,
            is_latest_version = 0
        """
    )
    conn.commit()

    # ---- Pass 0: Exact-byte dedup (file_hash) --------------------------
    print("\n  Pass 0: Exact-byte dedup (file_hash)")
    print("  (identical raw bytes = duplicate, unconditionally — no guard needed)")

    rows_with_file_hash = conn.execute(
        """
        SELECT id, source_registry, file_hash, triple_count, retrieval_date
        FROM corpus_registry
        WHERE parse_status = 'ok'
          AND file_hash IS NOT NULL
          AND is_duplicate = 0
        ORDER BY file_hash
        """
    ).fetchall()

    file_hash_groups: dict[str, list] = defaultdict(list)
    for row in rows_with_file_hash:
        file_hash_groups[row["file_hash"]].append(row)

    dup_file_hash = {h: rows for h, rows in file_hash_groups.items() if len(rows) > 1}
    print(f"  Found {len(dup_file_hash)} file_hash groups with duplicates")

    for h, rows in dup_file_hash.items():
        winner_id = _elect_winner(rows)
        for row in rows:
            if row["id"] != winner_id:
                update_duplicate(conn, row["id"], winner_id)
                counts["pass_0_marked"] += 1
                print(
                    f"    [dup] {row['source_registry']}#{row['id']} "
                    f"-> canonical #{winner_id} | file_hash={h[:16]}..."
                )

    # ---- Pass A: Version-aware IRI dedup ------------------------------
    print("\n  Pass A: Version-aware IRI dedup")
    print("  (same IRI + same version_iri = duplicate; different version = separate canonical row)")

    rows_with_iri = conn.execute(
        """
        SELECT id, source_registry, ontology_iri, ontology_version_iri,
               content_hash, triple_count, retrieval_date
        FROM corpus_registry
        WHERE parse_status = 'ok'
          AND ontology_iri IS NOT NULL
          AND is_duplicate = 0
        ORDER BY ontology_iri, ontology_version_iri
        """
    ).fetchall()

    # Group A1: rows where version_iri is known — key = (iri, version_iri)
    versioned_groups: dict[tuple, list] = defaultdict(list)
    # Group A2: rows where version_iri is NULL — key = iri, further guarded by content hash
    unversioned_by_iri: dict[str, list] = defaultdict(list)

    for row in rows_with_iri:
        if row["ontology_version_iri"] is not None:
            key = (row["ontology_iri"], row["ontology_version_iri"])
            versioned_groups[key].append(row)
        else:
            unversioned_by_iri[row["ontology_iri"]].append(row)

    # A1: deduplicate versioned groups
    dup_versioned = {k: rows for k, rows in versioned_groups.items() if len(rows) > 1}
    print(f"  A1 (IRI+version): {len(dup_versioned)} groups with cross-registry duplicates")
    for (iri, ver), rows in dup_versioned.items():
        winner_id = _elect_winner(rows)
        for row in rows:
            if row["id"] != winner_id:
                update_duplicate(conn, row["id"], winner_id)
                counts["pass_a_marked"] += 1
                print(
                    f"    [dup] {row['source_registry']}#{row['id']} "
                    f"-> canonical #{winner_id} | version={ver}"
                )

    # A2: preserve unversioned rows. A matching ontology IRI and coarse
    # class/property signature is not release identity: relationships,
    # individuals, concepts and annotations may all have changed.
    dup_unversioned = {iri: rows for iri, rows in unversioned_by_iri.items() if len(rows) > 1}
    print(f"  A2 (IRI, no version): {len(dup_unversioned)} IRIs with multiple unversioned entries")
    for iri, rows in dup_unversioned.items():
        print(f"    [keep] {iri[:60]}: {len(rows)} unversioned rows require release selection")

    # ---- Pass B: Content-hash dedup ------------------------------------
    print("\n  Pass B: Content-hash dedup")

    hash_groups: dict[str, list] = defaultdict(list)

    rows_with_hash = conn.execute(
        """
        SELECT id, source_id, source_registry, ontology_iri, ontology_version_iri,
               content_hash, triple_count, retrieval_date
        FROM corpus_registry
        WHERE parse_status = 'ok'
          AND content_hash IS NOT NULL
          AND content_hash != ?
          AND is_duplicate = 0
          AND (class_count + property_count) > 0
        ORDER BY content_hash
        """,
        (_EMPTY_CONTENT_HASH,),
    ).fetchall()
    # Grouping key is the coarse class+property content_hash — see the
    # version-IRI guard below for how distinct versions with an identical
    # class/property set stay separate despite the coarse hash colliding.

    for row in rows_with_hash:
        key = _dup_key(row)
        if key is not None:  # SQL excludes NULL; this also satisfies the typed boundary.
            hash_groups[key].append(row)

    # Rows excluded from Pass B entirely: zero classes/properties would all
    # hash to the SHA-256 of an empty string and form a false duplicate group.
    # Logged (not silently dropped) so there's a record of what Pass B never
    # got to consider.
    excluded_empty = conn.execute(
        """
        SELECT id, source_id, triple_count
        FROM corpus_registry
        WHERE parse_status = 'ok'
          AND content_hash IS NOT NULL
          AND is_duplicate = 0
          AND ((class_count + property_count) = 0 OR content_hash = ?)
        """,
        (_EMPTY_CONTENT_HASH,),
    ).fetchall()
    for row in excluded_empty:
        log_dedup_exclusion(
            exclusion_log_path,
            row["source_id"],
            "pass_b_excluded_empty",
            f"zero classes/properties (triple_count={row['triple_count']}) — "
            "would collide with every other empty ontology on the SHA-256 of "
            "an empty string, so excluded from content-hash grouping",
        )

    duplicate_groups_b = {h: rows for h, rows in hash_groups.items() if len(rows) > 1}
    print(f"  Found {len(duplicate_groups_b)} content-hash groups with duplicates")

    for h, rows in duplicate_groups_b.items():
        # A coarse signature is only corroborating evidence. Without an
        # explicit version IRI, matching hashes may be genuine releases (or
        # unrelated ontologies), so leave them canonical for Pass C/D.
        version_subgroups: dict[str | None, list] = defaultdict(list)
        for row in rows:
            version_subgroups[row["ontology_version_iri"]].append(row)

        for ver, sub in version_subgroups.items():
            if ver is None or len(sub) <= 1:
                continue
            winner_id = _elect_winner(sub)
            for row in sub:
                if row["id"] != winner_id:
                    update_duplicate(conn, row["id"], winner_id)
                    counts["pass_b_marked"] += 1
                    print(
                        f"    [dup] {row['source_registry']}#{row['id']} "
                        f"-> canonical #{winner_id} | hash={h[:16]}..."
                    )

    counts["chains_flattened"] = _flatten_canonical_chains(conn)

    # ---- Pass C: Flag the latest version per ontology ------------------
    counts["latest_flagged"] = mark_latest_versions(conn)

    # ---- Pass D: One latest row per ontology ---------------------------
    print("\n  Pass D: One latest row per ontology")
    if sidecar_path is None:
        print("  [warn] no sidecar store (--store): skipped, so copies of one")
        print("         ontology from several registries all stay latest")
        counts["latest_unflagged"] = counts["shared_iris"] = counts["iris_undecided"] = 0
    else:
        unflagged, shared, undecided = unify_latest_by_iri(
            conn, _sidecar_entities(sidecar_path), _newest_duplicate_dates(conn)
        )
        counts["latest_unflagged"], counts["shared_iris"] = unflagged, shared
        counts["iris_undecided"] = undecided
        counts["latest_flagged"] -= unflagged
        print(
            f"  {unflagged} older copies unflagged; {shared} IRIs shared by distinct "
            f"ontologies; {undecided} IRIs undecided (sidecar missing or unreadable)"
        )

    # ---- Pass E: An ontology declares something -----------------------
    counts["stubs_dropped"] = drop_stub_latest(conn, exclusion_log_path)
    counts["latest_flagged"] -= counts["stubs_dropped"]
    print(f"\n  Pass E: {counts['stubs_dropped']} latest rows declaring nothing unflagged")

    # ---- Home registry -------------------------------------------------
    counts["home_registry_moved"] = assign_home_registries(conn)
    print(
        f"\n  Home registry: {counts['home_registry_moved']} latest ontologies belong to a "
        "registry other than the one their latest copy came from"
    )

    conn.close()

    total = counts["pass_0_marked"] + counts["pass_a_marked"] + counts["pass_b_marked"]
    print("\nDedup summary:")
    print(f"  Pass 0 (exact-byte):         {counts['pass_0_marked']} rows marked")
    print(f"  Pass A (IRI-based):          {counts['pass_a_marked']} rows marked")
    print(f"  Pass B (content-hash-based): {counts['pass_b_marked']} rows marked")
    print(f"  Total duplicates marked:     {total}")
    print(f"  Duplicate chains flattened:  {counts['chains_flattened']}")
    print(f"  Latest versions flagged:     {counts['latest_flagged']}")

    return counts


# ---------------------------------------------------------------------------
# Latest-version flagging
# ---------------------------------------------------------------------------


def _ontology_group_key(row) -> str:
    """Return the registry release family used for latest-version selection.

    Declared ontology IRIs and class/property content hashes have already been
    applied by Passes A and B to identify cross-registry duplicates. The final
    selection must instead use the registry's version family: some submissions
    omit an ontology IRI, and converted submissions can assign a different IRI
    to every release. ``family_key`` handles both while keeping unversioned
    identifiers as one-row families.
    """
    return family_key(row["source_id"])


def _recency_key(row, inherited_date: str = "") -> tuple:
    """
    Sort key for 'most recent version'; higher = newer.

    Order: populated before stub (``_is_stub``) -> publication date, or the
    newest date among the row's duplicates when later (``inherited_date``;
    version labels are not reliably sortable) -> numeric source_id suffix
    (e.g. an OntoPortal submission id; submission ids order releases whose
    dates coincide) -> retrieval date. Missing dates/non-numeric suffixes
    sort earliest.
    """
    suffix = row["source_id"].rsplit("::", 1)[-1] if "::" in row["source_id"] else ""
    numeric_suffix = int(suffix) if suffix.isdigit() else -1
    date = max(row["source_version_date"] or "", inherited_date)
    return (not _is_stub(row), date, numeric_suffix, row["retrieval_date"] or "")


def mark_latest_versions(conn) -> int:
    """
    Flag the most-recent canonical release in each release family with
    is_latest_version=1; every other row is reset to 0, including rows that
    were flagged earlier and have since become duplicates or failed to parse.

    Only successfully-parsed, non-duplicate rows (parse_status='ok',
    is_duplicate=0) are considered — those are the usable canonical versions.
    Experiments that don't care about history can then simply filter
    ``WHERE is_latest_version=1``. Idempotent: safe to re-run.

    A stub release is flagged only when every release of its family is a
    stub; a canonical row competes with the newest date of its in-family
    duplicates.

    Returns the number of ontologies flagged (one winner per group).
    """
    from .db import _now

    now = _now()

    # Reset the flag on every row, not only the eligible ones, so a row that a
    # later run marks as a duplicate does not keep a stale flag.
    conn.execute("UPDATE corpus_registry SET is_latest_version = 0 WHERE is_latest_version != 0")

    rows = conn.execute(
        """
        SELECT id, source_id, source_version_date, retrieval_date,
               class_count, property_count, individual_count, declared_individual_count,
               skos_concept_count
        FROM corpus_registry
        WHERE parse_status = 'ok' AND is_duplicate = 0
        """
    ).fetchall()
    inherited = _newest_duplicate_dates(conn)

    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[_ontology_group_key(row)].append(row)

    flagged = 0
    for _key, group in groups.items():
        winner = max(group, key=lambda r: _recency_key(r, inherited.get(r["id"], "")))
        conn.execute(
            "UPDATE corpus_registry SET is_latest_version = 1, updated_at = ? WHERE id = ?",
            (now, winner["id"]),
        )
        flagged += 1

    conn.commit()
    return flagged


# ---------------------------------------------------------------------------
# Pass D: one latest row per ontology IRI
# ---------------------------------------------------------------------------

#: Share of declared terms two copies must have in common to be one ontology:
#: of the smaller copy across registries, of their union within one registry.
#: Successive releases keep most of their terms; distinct ontologies behind one
#: IRI share few.
CONTAINMENT_SAME = 0.5

#: Fewest declared classes and properties for an identical set to make two
#: copies with different IRIs one ontology. Small sets (an ``About`` metadata
#: file, an alignment) recur across a project's modules.
MIN_IDENTICAL_TERMS = 20

_OBO_PURL = OBO_BASE_NS.split("://", 1)[1]
# Build artifacts of an OBO ontology ``x``: ``x-full``, ``x-base``, ``x_merged``, …
_OBO_ARTIFACT = re.compile(r"[-_](?:full|base|simple|basic|plus|non-classified|edit|merged)$")
# Folders that hold build stages of the ontology named by the first folder.
_OBO_STAGE_DIRS = frozenset({"dev", "subsets", "extensions"})

# A release date in a version IRI: a full date anywhere, or a year (or
# year-month) as a whole path segment (``…/obo/bfo/2020/bfo-core.ttl``).
# Partial dates compare correctly as strings against full ones.
_ISO_DATE = re.compile(r"(?:19|20)\d\d-\d\d-\d\d|(?<=/)(?:19|20)\d\d(?:-\d\d)?(?=/)")


def ontology_key(iri: str) -> str:
    """Match key for the ontology an IRI names.

    ``iri_key`` (scheme and trailing separator ignored), with an OBO build
    artifact mapped to its ontology's PURL: ``…/obo/pco/pco-full.owl``,
    ``…/obo/oba/oba-base.owl``, ``…/obo/go/extensions/go-plus.owl`` and
    ``…/obo/subsets/envo-basic.obo.owl`` all read as ``…/obo/<id>.owl``, with
    the id lower-cased as OBO file names are. Other files under an OBO folder
    (``…/obo/uberon/chebi_import.owl``) keep their own key.
    """
    key = iri_key(iri).lstrip("/")
    if not key.lower().startswith(_OBO_PURL):
        return key
    *dirs, name = key[len(_OBO_PURL) :].lower().split("/")
    for ext in (".obo.owl", ".owl", ".obo", ".ttl"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    else:
        return key
    stem = _OBO_ARTIFACT.sub("", name)
    if not stem or (dirs and (dirs[0] != stem and dirs != ["subsets"])):
        return key
    if any(d not in _OBO_STAGE_DIRS for d in dirs[1:]):
        return key
    return f"{_OBO_PURL}{stem}.owl"


def _term_key(term: str) -> str:
    """A declared term compared across copies: without its scheme, and with
    ``#`` read as ``/`` (``…/APTO#X`` and ``…/APTO/X`` name one term)."""
    return term.split("://", 1)[-1].replace("#", "/")


def _version_iri_date(ontology_iri: str | None, version_iri: str | None) -> str | None:
    """The release date a version IRI adds to its ontology IRI.

    Only the part after the two IRIs' shared prefix is searched, so a year that
    belongs to the namespace itself (an editor's ``…/ontologies/2020/7/…``
    default, W3C's ``/2006/time``) is never read as a release date.
    """
    if not version_iri:
        return None
    iri = ontology_iri or ""
    shared = os.path.commonprefix([iri, version_iri])
    n = len(shared)
    # Cut after the whole shared segment when both IRIs part at a separator
    # (``…/onto/2019`` vs ``…/onto/2019/1.1/…``), else at the last ``/``.
    at_boundary = (
        n > 0 and version_iri[n : n + 1] in ("/", "#") and iri[n : n + 1] in ("", "/", "#")
    )
    cut = n if at_boundary else shared.rfind("/")
    match = _ISO_DATE.search(version_iri[cut:] if cut >= 0 else version_iri)
    return match.group(0) if match else None


def _sidecar_entities(
    sidecar_path: Callable[[str], Path],
) -> Callable[[str | None], set[str] | None]:
    """Declared entity IRIs of a raw-byte file hash, read from its sidecar;
    None when the sidecar is absent or unreadable (no evidence either way)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    def entities(file_hash: str | None) -> set[str] | None:
        if not file_hash:
            return None
        side = sidecar_path(file_hash)
        if not side.exists():
            return None
        try:
            table = pq.read_table(side, columns=["record_type", "subject"]).to_pandas()
        except (OSError, pa.ArrowException) as exc:
            print(f"    [warn] unreadable sidecar {side}: {exc}")
            return None
        return set(table.subject[table.record_type == "entity"])

    return entities


def _obo_release_of(iri: str | None, version_iri: str | None) -> bool:
    """Whether *version_iri* is a release IRI of OBO ontology *iri*
    (``…/obo/ogms/releases/2025-07-02/ogms.owl`` of ``…/obo/ogms.owl``)."""
    if not iri or not version_iri or not iri.startswith(OBO_BASE_NS):
        return False
    stem = iri[len(OBO_BASE_NS) :].split(".", 1)[0]
    return bool(stem) and version_iri.startswith(f"{OBO_BASE_NS}{stem}/")


def _comparable_concepts(a, b) -> bool:
    """Whether two copies carry comparable numbers of SKOS concepts.

    Sidecars and content hashes record declared classes and properties, not
    SKOS concepts, so thesauri published under one schema declare the same
    schema properties; only copies with comparable concept counts can be one
    ontology.
    """
    ca, cb = a["skos_concept_count"] or 0, b["skos_concept_count"] or 0
    return not max(ca, cb) or min(ca, cb) / max(ca, cb) >= CONTAINMENT_SAME


def _same_ontology(a, b, sets: dict[int, set[str]]) -> bool:
    """Whether rows *a* and *b*, which share an ontology key, are one ontology.

    Across registries, most of the smaller copy's terms are in the larger
    (CONTAINMENT_SAME). Within one registry, which lists the two as separate
    entries, the overlap is taken over their union, so a module or extract is
    not folded into the ontology containing it. Either way, two releases
    under the ontology's own OBO PURL that share at least one term are one
    ontology (a restructured release keeps few terms; the overlap guard keeps
    apart distinct files that both declare a tool's placeholder such as
    ``…/obo/TEMP.owl``).
    """
    if not _comparable_concepts(a, b):
        return False
    sa, sb = sets[a["id"]], sets[b["id"]]
    small, large = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if not small:
        return False
    common = len(small & large)
    base = len(sa | sb) if a["source_registry"] == b["source_registry"] else len(small)
    if common / base >= CONTAINMENT_SAME:
        return True
    return (
        common > 0
        and _obo_release_of(a["ontology_iri"], a["ontology_version_iri"])
        and _obo_release_of(b["ontology_iri"], b["ontology_version_iri"])
    )


def _declared_terms(row) -> int:
    return (row["class_count"] or 0) + (row["property_count"] or 0)


def _same_terms(a, b) -> bool:
    """Whether rows *a* and *b*, which declare the identical non-empty class
    and property set, are one ontology.

    An identical set is identity even without an IRI (a copy whose file
    declares none) or under a moved IRI; only small sets under two different
    IRIs are kept apart (MIN_IDENTICAL_TERMS), as are thesauri whose concept
    counts differ.
    """
    if not _comparable_concepts(a, b):
        return False
    ia, ib = a["ontology_iri"], b["ontology_iri"]
    if ia and ib and ontology_key(ia) != ontology_key(ib):
        return _declared_terms(a) >= MIN_IDENTICAL_TERMS
    return True


def release_day(row) -> str | None:
    """The release date of a row: the date its version IRI adds to its
    ontology IRI, which the ontology itself publishes, else the registry's
    release date."""
    day = _version_iri_date(row["ontology_iri"], row["ontology_version_iri"])
    if day:
        return day
    return release_date(row["source_registry_metadata"]) or (
        (row["source_version_date"] or "")[:10] or None
    )


def _newness_key(row, inherited_date: str = "") -> tuple:
    """Sort key for the newest copy of one ontology across registries.

    Populated copies before stubs (``_is_stub``); then dated copies before
    undated ones, newest date first, a canonical copy taking the newest date
    of its in-family duplicates when later (``inherited_date``); then the registry
    that curates the IRI (OBO for OBO PURLs, else LOV); then the copy
    declaring the most entities; then the lowest id.
    """
    day = max(filter(None, (release_day(row), inherited_date[:10])), default=None)
    from ontometer_corpus.network.entities import curating_registry

    curator = curating_registry(row["ontology_iri"] or "")
    size = sum(row[c] or 0 for c in ("class_count", "property_count", "declared_individual_count"))
    return (
        not _is_stub(row),
        day is not None,
        day or "",
        row["source_registry"] == curator,
        size,
        -row["id"],
    )


def unify_latest_by_iri(
    conn,
    entities: Callable[[str | None], set[str] | None],
    inherited: dict[int, str] | None = None,
    folded: dict[int, int] | None = None,
) -> tuple[int, int, int]:
    """Pass D. Returns (rows unflagged as latest, IRIs marked shared, IRIs left
    undecided for want of a readable sidecar). ``inherited`` maps a canonical
    row id to the newest date among its in-family duplicates; ``folded``, when
    given, receives each unflagged row id mapped to the row id kept for it.

    An undecided IRI contributes no IRI evidence and is never marked shared;
    its copies can still join others through an identical term set.
    """
    inherited = inherited or {}
    from .db import _now

    now = _now()
    conn.execute("UPDATE corpus_registry SET shared_iri = 0 WHERE shared_iri != 0")
    rows = conn.execute(
        """
        SELECT id, source_id, source_registry, ontology_iri, ontology_version_iri,
               source_version_date, source_registry_metadata, file_hash, content_hash,
               class_count, property_count, individual_count, declared_individual_count,
               skos_concept_count
        FROM corpus_registry
        WHERE is_latest_version = 1
        """
    ).fetchall()
    parent = {row["id"]: row["id"] for row in rows}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    # IRI evidence: one group per ontology key, whatever the IRI's spelling.
    by_iri: dict[str, list] = defaultdict(list)
    for row in rows:
        if row["ontology_iri"]:
            by_iri[ontology_key(row["ontology_iri"])].append(row)
    decided: list[list] = []
    undecided = 0
    for group in by_iri.values():
        if len(group) < 2:
            continue
        iri = min(row["ontology_iri"] for row in group)
        found = {row["id"]: entities(row["file_hash"]) for row in group}
        if any(s is None for s in found.values()):
            # Without every copy's terms, merging could drop a distinct
            # ontology and splitting could mark the IRI shared.
            undecided += 1
            print(f"    [undecided] {iri}: sidecar missing or unreadable")
            continue
        sets = {i: {_term_key(t) for t in s} for i, s in found.items() if s is not None}
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if _same_ontology(a, b, sets):
                    union(a["id"], b["id"])
        # A copy declaring nothing and carrying no concepts (a failed
        # conversion, an empty submission) is no evidence of a different
        # ontology: it joins the largest copy.
        largest = max(group, key=lambda r: len(sets[r["id"]]))
        for row in group:
            if not sets[row["id"]] and not row["skos_concept_count"]:
                union(row["id"], largest["id"])
        decided.append(group)

    # Term evidence: the identical non-empty class and property set, in any
    # canonical release. A copy equal to an earlier release of another family
    # is that family's ontology even when its latest release renamed every term.
    latest_of = {family_key(row["source_id"]): row["id"] for row in rows}
    by_terms: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT id, source_id, ontology_iri, content_hash, class_count, property_count,
               skos_concept_count
        FROM corpus_registry
        WHERE parse_status = 'ok' AND is_duplicate = 0
          AND content_hash IS NOT NULL AND content_hash != ?
        """,
        (_EMPTY_CONTENT_HASH,),
    ):
        if _declared_terms(row) and family_key(row["source_id"]) in latest_of:
            by_terms[row["content_hash"]].append(row)
    for group in by_terms.values():
        for i, a in enumerate(group):
            la = latest_of[family_key(a["source_id"])]
            for b in group[i + 1 :]:
                lb = latest_of[family_key(b["source_id"])]
                if find(la) != find(lb) and _same_terms(a, b):
                    union(la, lb)

    components: dict[int, list] = defaultdict(list)
    for row in rows:
        components[find(row["id"])].append(row)
    unflagged = 0
    for members in components.values():
        winner = max(members, key=lambda r: _newness_key(r, inherited.get(r["id"], "")))
        for row in members:
            if row["id"] != winner["id"]:
                conn.execute(
                    "UPDATE corpus_registry SET is_latest_version = 0, updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                unflagged += 1
                if folded is not None:
                    folded[row["id"]] = winner["id"]

    shared = 0
    for group in decided:
        n = len({find(row["id"]) for row in group})
        if n > 1:
            # Every row declaring any spelling of the IRI, latest or not.
            for spelling in {row["ontology_iri"] for row in group}:
                conn.execute(
                    "UPDATE corpus_registry SET shared_iri = 1, updated_at = ? "
                    "WHERE ontology_iri = ?",
                    (now, spelling),
                )
            shared += 1
            print(
                f"    [shared] {min(row['ontology_iri'] for row in group)}: {n} distinct ontologies"
            )
    conn.commit()
    return unflagged, shared, undecided


# ---------------------------------------------------------------------------
# Pass E: an ontology declares something
# ---------------------------------------------------------------------------


def drop_stub_latest(conn, exclusion_log: Path | None = None) -> int:
    """Pass E. Unflag every latest row that declares no class, property,
    individual or SKOS concept (``_is_stub``); returns the rows unflagged.
    Each is logged to *exclusion_log* when given."""
    from .db import _now

    now = _now()
    rows = conn.execute(
        """
        SELECT id, source_id, triple_count, class_count, property_count, individual_count,
               declared_individual_count, skos_concept_count
        FROM corpus_registry
        WHERE is_latest_version = 1
        """
    ).fetchall()
    stubs = [row for row in rows if _is_stub(row)]
    for row in stubs:
        conn.execute(
            "UPDATE corpus_registry SET is_latest_version = 0, updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        if exclusion_log is not None:
            log_dedup_exclusion(
                exclusion_log,
                row["source_id"],
                "pass_e_declares_nothing",
                f"no class, property, individual or concept (triple_count="
                f"{row['triple_count']}), so not counted as an ontology",
            )
    conn.commit()
    return len(stubs)
