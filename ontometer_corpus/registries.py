"""Which registry an ontology belongs to.

A corpus row's ``source_registry`` is the registry that row was fetched from.
For an ontology held by several registries, dedup keeps one latest copy, and
which registry that copy came from depends on registration dates: the newest
copy of FOAF sits on BioPortal, that of BFO on OKG. Grouping ontologies by
``source_registry`` therefore credits them to registries that merely mirror
them.

The *home registry* is the answer used for any per-registry statistic. It is

* ``OBO`` for an ontology whose IRI is an OBO PURL, whichever registry the
  corpus got it from; otherwise
* the first registry in :data:`REGISTRY_PRIORITY` among those holding a copy of
  it -- the same file, or the same ontology IRI (:func:`iri_key`).

The priority puts the curated, focused libraries before the portals and the
portals before the general-purpose collections.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from ontometer_corpus.namespaces import OBO_BASE_NS

#: Registries from most to least specific; an ontology's home is the first one
#: that holds a copy. A registry not listed ranks after all of these.
REGISTRY_PRIORITY: tuple[str, ...] = (
    "OBO",
    "LOV",
    "BioPortal",
    "AgroPortal",
    "BiodivPortal",
    "MatPortal",
    "TechnoPortal",
    "OntoPortal-Astro",
    "Ontohub",
    "OKG",
)
_RANK = {registry: i for i, registry in enumerate(REGISTRY_PRIORITY)}


def iri_key(iri: str) -> str:
    """Match key for an ontology IRI: its scheme and trailing ``/`` or ``#``
    removed, so ``http://schema.org/`` and ``https://schema.org`` are one
    ontology."""
    return iri.split("://", 1)[-1].rstrip("/#")


def is_obo_purl(iri: str | None) -> bool:
    return isinstance(iri, str) and iri.lower().startswith(OBO_BASE_NS)


def home_registry(ontology_iri: str | None, hosts: set[str] | frozenset[str]) -> str:
    """The home registry of an ontology held by the registries in *hosts*."""
    if is_obo_purl(ontology_iri):
        return "OBO"
    return min(hosts, key=lambda r: (_RANK.get(r, len(_RANK)), r))


def home_registry_sql(conn: sqlite3.Connection) -> str:
    """SQL expression for a row's home registry: the column dedup writes, or
    ``source_registry`` in a database dedup has not yet given one."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(corpus_registry)")}
    if "home_registry" in columns:
        return "COALESCE(home_registry, source_registry)"
    return "source_registry"


def hosted_on(conn: sqlite3.Connection) -> dict[int, frozenset[str]]:
    """Per corpus row id, every registry holding a copy of that ontology.

    Copies are rows with the same file, or with the same ontology IRI. An IRI
    that dedup marked ``shared_iri`` names several distinct ontologies, so
    such a row is matched by its file only.
    """
    rows = conn.execute(
        "SELECT id, source_registry, ontology_iri, file_hash, shared_iri FROM corpus_registry"
    ).fetchall()
    by_file: dict[str, set[str]] = defaultdict(set)
    by_iri: dict[str, set[str]] = defaultdict(set)
    for _, registry, iri, file_hash, shared in rows:
        if file_hash:
            by_file[file_hash].add(registry)
        if iri and not shared:
            by_iri[iri_key(iri)].add(registry)
    hosts: dict[int, frozenset[str]] = {}
    for row_id, registry, iri, file_hash, shared in rows:
        found = {registry}
        if file_hash:
            found |= by_file[file_hash]
        if iri and not shared:
            found |= by_iri[iri_key(iri)]
        hosts[row_id] = frozenset(found)
    return hosts


def assign_home_registries(conn: sqlite3.Connection) -> int:
    """Write ``corpus_registry.home_registry`` for every row. Returns how many
    latest rows have a home registry other than their ``source_registry``."""
    hosts = hosted_on(conn)
    rows = conn.execute(
        "SELECT id, source_registry, ontology_iri, is_latest_version FROM corpus_registry"
    ).fetchall()
    updates, moved = [], 0
    for row_id, registry, iri, is_latest in rows:
        home = home_registry(iri, hosts[row_id])
        updates.append((home, row_id))
        moved += bool(is_latest and home != registry)
    conn.executemany("UPDATE corpus_registry SET home_registry = ? WHERE id = ?", updates)
    conn.commit()
    return moved
