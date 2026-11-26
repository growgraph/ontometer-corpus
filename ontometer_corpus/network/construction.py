"""
Ontology dependency network construction.

Builds a directed multigraph of the corpus from corpus.db plus the structural
sidecars written by the Phase 1 parse (see ontometer_corpus/pipeline/parse.py),
and writes it as two Parquet tables (``corpus/network_nodes.parquet`` and
``corpus/network_edges.parquet``, see save_network) for downstream analysis. No
raw ontology is parsed here: the sidecars supply declaration counts used to
settle namespace ownership.

Two edge types, both derived straight from corpus_registry:
  - "import": declared owl:imports targets (column: imports)
  - "usage":  namespaces an ontology actually references but doesn't own
              (column: referenced_namespaces, minus the namespaces it owns)
A pair of ontologies connected by both gets both edges (hence MultiDiGraph)
— the gap between "declared" and "actually used" is itself a signal.

Nodes are the corpus at its latest, canonical, successfully-parsed version
per ontology (parse_status='ok' AND is_duplicate=0 AND is_latest_version=1
— the Phase 2 dedup only sets is_latest_version on parsed, non-duplicate
rows, so it already implies the first two).
Import/usage targets that don't resolve unambiguously to another row in the
corpus become external leaf nodes (is_external=True, no row behind them)
rather than being dropped — that is itself useful signal (what is referenced
outside the corpus).

Usage edges resolve a namespace to the ontology that owns it. Several rows can
claim one namespace (a file that copies BFO terms declares them just as BFO
does), so claims are settled by the same rule the term network uses
(``ontometer_corpus.network.entities.pick_home``; modelling-language
namespaces such as SWRL are never owned): the ontology whose IRI names the
namespace, else a claimant declaring at least ten times as many terms in it
as any other (counted from the structural sidecars when the object store is
available); otherwise it stays external.

A node is identified by its ontology IRI, or by ``row:<id>`` when the row has
none or dedup found the IRI shared by distinct ontologies (``node_id_of``).

Every relation preserves all original evidence in ``reference_iris`` and its
cardinality in ``evidence_count``. Usage edges also carry coarse
``reference_kinds`` (``language`` for RDF/OWL and the other representation
languages, ``metadata_or_identifier``, ``malformed_or_local``,
``domain_vocabulary``) and, when ``ontometer-corpus term-coupling`` has written
``namespace_usage.parquet``, how many distinct terms the source uses from the
target logically, as annotation, or only in declarations
(``n_logical_terms``, ``n_annotation_terms``, ``n_declared_terms``; see
``ontometer_corpus.usage``). Whether a vocabulary is reused for modelling is
read from those counts, not from a list of vocabularies.

Usage:
    uv run ontometer-corpus network --store /path/to/ontologies
    # or:
    from ontometer_corpus.network.construction import build_network, load_network
    G = build_network(paths)
"""

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import click
import networkx as nx

from ontometer_corpus.namespaces import reference_kind
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.registries import home_registry_sql
from ontometer_corpus.roles import SKOS_CONCEPT_ROLE


def _as_paths(value: CorpusPaths | Path | str | None) -> CorpusPaths:
    """Accept a CorpusPaths, a corpus directory, or nothing.

    The directory form is for interactive use, where `load_network("data")`
    reads better than constructing a paths object for a one-line call.
    """
    if isinstance(value, CorpusPaths):
        return value
    if value is None:
        return CorpusPaths.resolve()
    return CorpusPaths.resolve(corpus_dir=value)


_NODE_QUERY = """
    SELECT id, ontology_iri, ontology_version_iri, source_registry,
           {home_registry} AS home_registry,
           file_hash, content_hash, triple_count, class_count, property_count,
           individual_count, skos_concept_count, imports, referenced_namespaces,
           own_namespaces, shared_iri
    FROM corpus_registry
    WHERE is_latest_version = 1 AND parse_status = 'ok'
    ORDER BY id
"""

# Path segments that mark a URL as pointing at an import *module* or a
# specific *version* of an ontology, rather than its canonical identity —
# stripped before comparing two IRIs for "same ontology." Raw import targets
# rarely match an ontology IRI verbatim.
_VERSION_SEGMENT_RE = re.compile(r"/(releases|\d{4}-\d{2}-\d{2})/[^/]+/")
_IMPORT_MODULE_RE = re.compile(r"/imports/[^/]+_import\.owl$")

# Unexpanded XML entity refs occasionally seen in dirty OBO/OWL-XML import
# targets (the source file never resolved its own <!ENTITY> declaration).
_XML_ENTITIES = {"&obo;": "http://purl.obolibrary.org/obo/"}

# An OBO import module is an extract of another ontology's terms, so the
# dependency is on that ontology: ``…/obo/foodon/imports/chebi_import.owl`` (the
# ODK layout), ``…/obo/ino/BFO_import.owl`` and ``…/obo/bcgo/import_NCBITaxon.owl``
# all depend on the module's source ontology, ``…/obo/<name>.owl``. A dated
# OBO release (``…/obo/iao/2017-03-24/iao.owl``) is that ontology too. Only used
# when the named ontology is a corpus node other than the importer.
_OBO_MODULE_NAME_RES = (
    re.compile(r"/([A-Za-z]+)_import\.owl$"),
    re.compile(r"/import_([A-Za-z]+)\.owl$"),
)
_OBO_DATED_RELEASE_RE = re.compile(
    r"^http://purl\.obolibrary\.org/obo/([a-z0-9]+)/(?:releases/)?\d{4}-\d{2}-\d{2}/\1\.owl$"
)


def normalize_iri(iri: str) -> str:
    """Normalise an IRI so two different-looking URLs for the same ontology
    compare equal: expands known XML entities, strips import-module
    suffixes, collapses version-specific path segments, and reads https as
    http. The result is a match key, not an IRI to store or display."""
    if not iri:
        return iri
    for entity, expansion in _XML_ENTITIES.items():
        iri = iri.replace(entity, expansion)
    iri = _IMPORT_MODULE_RE.sub("/", iri)
    iri = _VERSION_SEGMENT_RE.sub("/", iri)
    # http and https spellings name the same resource.
    if iri.startswith("https://"):
        iri = "http://" + iri[len("https://") :]
    return iri.rstrip("/")


def _ns_key(iri: str) -> str:
    """
    Match key for comparing a namespace against an ontology_iri. Usage edges
    resolve on the referenced *namespace*, which routinely carries a trailing
    '#' or '/' that the owning ontology's IRI does not. `normalize_iri` strips
    only `/`; this also strips `#`, so a namespace such as `.../cube#` matches
    an ontology IRI `.../cube`.
    """
    return normalize_iri(iri).rstrip("/#")


def _obo_dependency(target_iri: str) -> str | None:
    """The OBO ontology an import module or dated release stands for, if any."""
    iri = target_iri
    for entity, expansion in _XML_ENTITIES.items():
        iri = iri.replace(entity, expansion)
    if not iri.startswith(("http://purl.obolibrary.org/obo/", "file:")):
        return None
    if match := _OBO_DATED_RELEASE_RE.match(iri):
        return f"http://purl.obolibrary.org/obo/{match.group(1)}.owl"
    for pattern in _OBO_MODULE_NAME_RES:
        if match := pattern.search(iri):
            return f"http://purl.obolibrary.org/obo/{match.group(1).lower()}.owl"
    return None


def _external_key(target_iri: str) -> str:
    """Identity of an unresolved target: spellings that differ only by a
    trailing ``/`` or ``#`` (``…/foaf/0.1/``, ``…/foaf/0.1#``) are one target,
    so an unresolved import and a usage of the same namespace meet on one node."""
    return target_iri.rstrip("/#") or target_iri


def _namespace_usage(paths: CorpusPaths) -> dict[str, dict[str, tuple[int, int, int]]] | None:
    """``{file_hash: {namespace: (logical, annotation, declared)}}`` from
    ``namespace_usage.parquet``, or None when that table has not been built."""
    if not paths.namespace_usage_path.exists():
        return None
    import pyarrow.parquet as pq

    table = pq.read_table(paths.namespace_usage_path).to_pydict()
    out: dict[str, dict[str, tuple[int, int, int]]] = {}
    for fh, ns, n_log, n_ann, n_decl in zip(
        table["file_hash"],
        table["namespace"],
        table["n_logical_terms"],
        table["n_annotation_terms"],
        table["n_declared_terms"],
    ):
        out.setdefault(fh, {})[ns] = (n_log, n_ann, n_decl)
    return out


def _external_kind(target_id: str) -> str:
    """Classify an unresolved target for the exported node table."""
    kind = reference_kind(target_id)
    return {
        "language": "language",
        "metadata_or_identifier": "metadata_or_identifier",
        "malformed_or_local": "malformed_or_local",
    }.get(kind, "unclassified")


def _count_by_namespace(iris: Iterable[str]) -> dict[str, int]:
    """Number of IRIs per namespace."""
    from ontometer_corpus.namespaces import iri_namespace

    counts: dict[str, int] = {}
    for iri in iris:
        ns = iri_namespace(iri)
        if ns:
            counts[ns] = counts.get(ns, 0) + 1
    return counts


def _namespace_counts(
    paths: CorpusPaths, file_hash: str | None, skos_concept_count: int | None
) -> tuple[dict[str, int], dict[str, int]]:
    """Declared entities and directly typed SKOS concepts per namespace, from one sidecar read.

    The dominance evidence for namespace ownership. Both are empty without the
    object store or a sidecar, which leaves only IRI-based ownership; a
    reproducible full-corpus build therefore requires the sidecar store. The
    roles artifact is read only for a row the parser counted concepts in, or
    one parsed before that count existed, whose sidecar lacks SKOS records.
    """
    if not file_hash or paths.store_dir is None:
        return {}, {}
    import pyarrow.parquet as pq

    declared: list[str] = []
    concepts: list[str] = []
    side = paths.sidecar_path(file_hash)
    if side.exists():
        table = pq.read_table(side, columns=["record_type", "subject"]).to_pandas()
        declared = table.subject[table.record_type == "entity"].tolist()
        concepts = table.subject[table.record_type == "skos_concept"].tolist()
    if not concepts and skos_concept_count != 0:
        roles = paths.roles_path(file_hash)
        if roles.exists():
            table = pq.read_table(roles, columns=["record_type", "subject", "object"]).to_pandas()
            concepts = table.subject[
                (table.record_type == "role") & (table.object == SKOS_CONCEPT_ROLE)
            ].tolist()
    return _count_by_namespace(declared), _count_by_namespace(concepts)


def identity_iri(row) -> str | None:
    """The row's ontology IRI if it identifies one ontology, else None.

    Dedup sets ``shared_iri`` where distinct ontologies declare the same IRI
    (placeholders such as ``http://iri/``, a family of crop ontologies sharing
    ``https://cropontology.org/rdf/``); such an IRI names none of them.
    """
    shared = row["shared_iri"] if "shared_iri" in row.keys() else 0
    return None if shared else row["ontology_iri"]


def node_id_of(row) -> str:
    """Network node id of a corpus row: its identity IRI, else ``row:<id>``."""
    return identity_iri(row) or f"row:{row['id']}"


def build_network(paths: CorpusPaths | Path | str | None = None) -> nx.MultiDiGraph:
    """Build the ontology dependency graph from corpus.db (see module
    docstring for the node/edge model)."""
    # entities.py imports _ns_key from this module, so this import cannot be
    # at the top.
    from ontometer_corpus.network.entities import NamespaceOwnerRecord, resolve_namespace_owners

    paths = _as_paths(paths)
    conn = sqlite3.connect(str(paths.db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_NODE_QUERY.format(home_registry=home_registry_sql(conn))).fetchall()
    conn.close()

    G = nx.MultiDiGraph()

    # Lookup tables built once so each edge resolves in O(1), not O(n).
    # Import IRIs are often not byte-for-byte equal to the declaring
    # ontology's IRI. Resolution is deliberately limited to normalised exact
    # matches: a bare final filename (``core.owl``) is not identity evidence.
    iri_to_nodes: dict[str, set[str]] = {}
    node_of_row: dict[int, str] = {}
    ownership_records: list[NamespaceOwnerRecord] = []

    for row in rows:
        node_id = node_id_of(row)
        node_of_row[row["id"]] = node_id
        own_ns = json.loads(row["own_namespaces"] or "[]")
        declared, skos_concepts = _namespace_counts(
            paths, row["file_hash"], row["skos_concept_count"]
        )
        ownership_records.append(
            NamespaceOwnerRecord(
                owner=row["id"],
                ontology_iri=identity_iri(row),
                source_registry=row["source_registry"],
                own_namespaces=tuple(own_ns),
                declarations=declared,
                skos_concepts=skos_concepts,
            )
        )

        G.add_node(
            node_id,
            source_registry=row["source_registry"],
            home_registry=row["home_registry"],
            file_hash=row["file_hash"],
            content_hash=row["content_hash"],
            triple_count=row["triple_count"],
            class_count=row["class_count"],
            property_count=row["property_count"],
            individual_count=row["individual_count"],
            own_namespaces=own_ns,
            referenced_namespaces=json.loads(row["referenced_namespaces"] or "[]"),
            imports=json.loads(row["imports"] or "[]"),
            is_external=False,
        )

        for iri in (identity_iri(row), row["ontology_version_iri"]):
            if iri:
                iri_to_nodes.setdefault(normalize_iri(iri), set()).add(node_id)
    namespace_owner_rows = resolve_namespace_owners(ownership_records)
    namespace_to_node = {key: node_of_row[owner] for key, owner in namespace_owner_rows.items()}
    usage_by_file = _namespace_usage(paths)

    def _resolve_external(target_id: str) -> str:
        """
        Add (once) and return an external leaf node for an unresolved target.

        Prefixed into its own id-space so it can never collide with a real
        ontology_iri node — without this, a corpus row with a degenerate
        ontology_iri (e.g. a malformed file whose detected identity is just
        the bare "http://purl.obolibrary.org/obo/" namespace) would silently
        absorb every unrelated ontology's unresolved reference to that same
        string as if it were the real target.
        """
        ext_id = f"external:{_external_key(target_id)}"
        if ext_id not in G:
            G.add_node(
                ext_id,
                is_external=True,
                external_iri=target_id,
                external_kind=_external_kind(target_id),
            )
        return ext_id

    def _add_edge(
        source: str,
        target: str,
        edge_type: str,
        reference_iri: str,
        *,
        resolution: str | None = None,
        kind: str,
        usage: tuple[int, int, int] | None = None,
    ) -> None:
        if G.has_edge(source, target, key=edge_type):
            attrs = G[source][target][edge_type]
            if usage is not None:
                for name, n in zip(_USAGE_ATTRS, usage):
                    attrs[name] = (attrs.get(name) or 0) + n
            attrs["reference_iris"] = sorted({*attrs["reference_iris"], reference_iri})
            attrs["reference_kinds"] = sorted({*attrs["reference_kinds"], kind})
            if resolution is not None:
                attrs["resolutions"] = sorted({*attrs.get("resolutions", []), resolution})
            attrs["evidence_count"] = len(attrs["reference_iris"])
            attrs["reference_iri"] = attrs["reference_iris"][0]
            attrs["reference_kind"] = (
                attrs["reference_kinds"][0] if len(attrs["reference_kinds"]) == 1 else "mixed"
            )
            if attrs.get("resolutions"):
                attrs["resolution"] = (
                    attrs["resolutions"][0] if len(attrs["resolutions"]) == 1 else "mixed"
                )
            return
        attrs: dict[str, object] = {
            "edge_type": edge_type,
            "reference_iri": reference_iri,
            "reference_kind": kind,
            "reference_iris": [reference_iri],
            "reference_kinds": [kind],
            "evidence_count": 1,
        }
        if resolution is not None:
            attrs["resolution"] = resolution
            attrs["resolutions"] = [resolution]
        else:
            attrs["resolutions"] = []
        if usage is not None:
            attrs.update(zip(_USAGE_ATTRS, usage))
        G.add_edge(source, target, key=edge_type, **attrs)

    for row in rows:
        source = node_id_of(row)
        # None: no usage table, or this file failed the coupling re-parse.
        file_usage = (usage_by_file or {}).get(row["file_hash"])

        for target_iri in json.loads(row["imports"] or "[]"):
            normalized = normalize_iri(target_iri)
            exact_candidates = iri_to_nodes.get(normalized, set())
            dependency = _obo_dependency(target_iri)
            module_candidates = (
                iri_to_nodes.get(normalize_iri(dependency), set()) if dependency else set()
            )
            if module_candidates == {source}:
                continue  # the ontology's own module: packaging, not a dependency
            if len(module_candidates) == 1:
                target = next(iter(module_candidates))
                resolution = "obo_module"
            elif len(exact_candidates) == 1:
                target = next(iter(exact_candidates))
                resolution = "exact_iri"
            elif len(exact_candidates) > 1:
                target = _resolve_external(target_iri)
                resolution = "ambiguous_iri"
            else:
                target = _resolve_external(target_iri)
                resolution = "external"
            # Module imports normalise to their containing ontology. They are
            # internal packaging, not a dependency from an ontology to itself.
            if target != source:
                _add_edge(
                    source,
                    target,
                    "import",
                    target_iri,
                    resolution=resolution,
                    kind="declared_import",
                )

        for ns in json.loads(row["referenced_namespaces"] or "[]"):
            node = namespace_to_node.get(ns) or namespace_to_node.get(_ns_key(ns))
            # Only a namespace the ontology OWNS is skipped. One it merely
            # claims (it declares copied terms, as OBO ontologies do with IAO
            # and BFO) resolves to its owner: copying a vocabulary's terms is
            # using it, and skipping every claimed namespace hid that reuse.
            # This also drops an ontology referencing its own generic
            # namespace (LOV's FOAF row using foaf: terms): a self-loop is
            # meaningless here and would inflate its in-degree.
            if node == source:
                continue
            _add_edge(
                source,
                node or _resolve_external(ns),
                "usage",
                ns,
                kind=reference_kind(ns),
                usage=None if file_usage is None else file_usage.get(ns, (0, 0, 0)),
            )

    return G


#: Node attributes that are lists of IRIs. Stored as JSON strings in the node
#: table — Parquet has no ragged-list column that survives a round trip through
#: pandas cleanly, and the strings compress well because namespaces repeat.
_LIST_ATTRS = ("own_namespaces", "referenced_namespaces", "imports")
_EDGE_LIST_ATTRS = ("reference_iris", "reference_kinds", "resolutions")
#: Usage-edge term counts by usage mode; absent when no evidence was measured.
_USAGE_ATTRS = ("n_logical_terms", "n_annotation_terms", "n_declared_terms")

#: Scalar node attributes.
_SCALAR_ATTRS = (
    "source_registry",
    "home_registry",
    "file_hash",
    "content_hash",
    "triple_count",
    "class_count",
    "property_count",
    "individual_count",
    "external_iri",
    "external_kind",
)


def save_network(
    G: nx.MultiDiGraph, paths: CorpusPaths | Path | str | None = None
) -> tuple[Path, Path]:
    """Write the graph as two Parquet tables: nodes and edges.

    Parquet rather than a pickle, because loading a pickle executes arbitrary
    code; dictionary encoding keeps repeated namespace strings small.

    Returns the ``(nodes, edges)`` paths.
    """
    import pandas as pd

    paths = _as_paths(paths)
    paths.ensure_corpus_dir()

    node_rows = []
    for node_id, attrs in G.nodes(data=True):
        row: dict[str, object] = {"node_id": node_id, "is_external": bool(attrs.get("is_external"))}
        for key in _SCALAR_ATTRS:
            row[key] = attrs.get(key)
        for key in _LIST_ATTRS:
            value = attrs.get(key)
            row[key] = json.dumps(value, separators=(",", ":")) if value else None
        node_rows.append(row)

    edge_rows = []
    for s, t, d in G.edges(data=True):
        row = {
            "source": s,
            "target": t,
            "edge_type": d.get("edge_type"),
            "resolution": d.get("resolution"),
            "reference_iri": d.get("reference_iri"),
            "reference_kind": d.get("reference_kind"),
            "evidence_count": d.get("evidence_count", 1),
        }
        for key in _USAGE_ATTRS:
            row[key] = d.get(key)
        for key in _EDGE_LIST_ATTRS:
            row[key] = json.dumps(d.get(key, []), separators=(",", ":"))
        edge_rows.append(row)

    nodes_path = paths.network_nodes_path
    edges_path = paths.network_edges_path
    frame = pd.DataFrame(node_rows)
    # External nodes carry no counts, so a plain int column would be coerced to
    # float by the NaNs. Nullable integers keep them integral on the round trip.
    for column in ("triple_count", "class_count", "property_count", "individual_count"):
        if column in frame:
            frame[column] = frame[column].astype("Int64")
    frame.to_parquet(nodes_path, index=False, compression="zstd")
    edge_frame = pd.DataFrame(edge_rows)
    for column in _USAGE_ATTRS:
        edge_frame[column] = edge_frame[column].astype("Int64")
    edge_frame.to_parquet(edges_path, index=False, compression="zstd")
    return nodes_path, edges_path


def load_network(paths: CorpusPaths | Path | str | None = None) -> nx.MultiDiGraph:
    """Rebuild the graph written by :func:`save_network`."""
    import pandas as pd

    paths = _as_paths(paths)
    nodes = pd.read_parquet(paths.network_nodes_path)
    edges = pd.read_parquet(paths.network_edges_path)

    G = nx.MultiDiGraph()
    for row in nodes.to_dict("records"):
        node_id = row.pop("node_id")
        for key in _LIST_ATTRS:
            raw = row.get(key)
            row[key] = json.loads(raw) if isinstance(raw, str) else []
        row["is_external"] = bool(row["is_external"])
        for count in ("triple_count", "class_count", "property_count", "individual_count"):
            value = row.get(count)
            row[count] = int(value) if value is not None and value == value else None
        G.add_node(node_id, **{k: v for k, v in row.items() if v is not None or k in _LIST_ATTRS})
    for row in edges.to_dict("records"):
        attrs = {
            "edge_type": row["edge_type"],
            "evidence_count": int(row.get("evidence_count", 1)),
        }
        for key in ("resolution", "reference_iri", "reference_kind"):
            value = row.get(key)
            if value is not None and value == value:
                attrs[key] = value
        for key in _USAGE_ATTRS:
            value = row.get(key)
            if value is not None and value is not pd.NA and value == value:
                attrs[key] = int(value)
        for key in _EDGE_LIST_ATTRS:
            raw = row.get(key)
            if isinstance(raw, str):
                attrs[key] = json.loads(raw)
            elif key == "reference_iris" and "reference_iri" in attrs:
                attrs[key] = [attrs["reference_iri"]]
            elif key == "reference_kinds" and "reference_kind" in attrs:
                attrs[key] = [attrs["reference_kind"]]
            elif key == "resolutions" and "resolution" in attrs:
                attrs[key] = [attrs["resolution"]]
            else:
                attrs[key] = []
        # Backward-compatible loading: older exports simply lack the new
        # evidence fields, so retain the edge with the attributes they carry.
        G.add_edge(row["source"], row["target"], key=row["edge_type"], **attrs)
    return G


@click.command()
@corpus_paths
def main(paths: CorpusPaths) -> None:
    """Build the ontology dependency network from corpus.db and sidecars."""

    paths.require_store_dir()

    print("OntoMeter — dependency network")
    print(f"  corpus db : {paths.db_path}")

    graph = build_network(paths)

    n_import = sum(1 for *_, d in graph.edges(data=True) if d["edge_type"] == "import")
    n_usage = sum(1 for *_, d in graph.edges(data=True) if d["edge_type"] == "usage")
    n_external = sum(1 for _, d in graph.nodes(data=True) if d.get("is_external"))

    print(f"  nodes     : {graph.number_of_nodes()} ({n_external} external)")
    print(f"  edges     : {graph.number_of_edges()} (import={n_import}, usage={n_usage})")

    usage = [d for *_, d in graph.edges(data=True) if d["edge_type"] == "usage"]
    measured = [d for d in usage if "n_logical_terms" in d]
    logical = sum(1 for d in measured if d["n_logical_terms"] > 0)
    annotation = sum(
        1 for d in measured if d["n_logical_terms"] == 0 and d["n_annotation_terms"] > 0
    )
    print(
        f"  usage     : {len(measured)} of {len(usage)} edges with term evidence; "
        f"{logical} logical, {annotation} annotation only, "
        f"{len(measured) - logical - annotation} declaration only or no terms seen"
    )
    by_resolution: dict[str, int] = {}
    for *_, d in graph.edges(data=True):
        if d["edge_type"] == "import":
            key = d.get("resolution", "?")
            by_resolution[key] = by_resolution.get(key, 0) + 1
    print(f"  imports   : {dict(sorted(by_resolution.items()))}")

    nodes_path, edges_path = save_network(graph, paths)
    print(f"\n  wrote     : {nodes_path}")
    print(f"              {edges_path}")


if __name__ == "__main__":
    main()
