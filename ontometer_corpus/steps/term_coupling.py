"""
Term-level coupling per ontology, from a re-parse of each file.

The parse phase keeps, per ontology, only the *set of namespaces* it
references (referenced_namespaces / own_namespaces); it discards the
individual terms to stay memory-bounded on very large files. A dependency
ratio computed from those sets is therefore a *namespace-breadth* measure:
FOAF counts once whether an ontology borrows one term from it or five hundred.

The term-level coupling ratio asks the finer question — of the domain terms
an ontology actually uses, what fraction are borrowed from other vocabularies
rather than its own — which is only answerable by re-reading each file. This
script does that re-parse once and writes the per-ontology term tallies;
consumers load that small table instead of re-parsing the corpus.

Reads:  ``<corpus>/corpus.db`` (latest-version, parsed rows with their
        namespace claims and parse_format), structural sidecars, and the raw files under
        ``$ONTOMETER_STORE/objects/``.
Writes: ``<corpus>/term_coupling.parquet``, one row per file hash, and
        ``<corpus>/namespace_usage.parquet``, one row per (file hash,
        namespace) with the distinct terms used logically, as annotation, or
        only declared (``ontometer_corpus.usage``). Network construction reads
        the second table, so this script runs before it.
Runs after: ``ontometer-corpus process`` (parse and dedup phases).
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Keyed by raw-byte file_hash (the same key the structural sidecars use), so
files with the same coarse class/property signature remain independently
measured; join the result back to latest-version rows on file_hash.

Method, per ontology:
  - Collect every distinct referenced term (URIRef) via the same parse path
    that succeeded for this file in the parse phase (parse_format says which):
      obo                      -> flat-file scan of logical-axiom lines
      turtle / json-ld / nt    -> rdflib
      xml / stream_xml_cvt     -> rdflib (RDF/XML)
      owl_xml / owlready2      -> owlready2 (via the pipeline's try_owlready2)
  - Bucket each term by namespace with ``ontometer_corpus.namespaces.iri_namespace``
    (OBO-prefix-aware: .../obo/GO_ , not the bare .../obo/), so buckets line
    up exactly with the stored own_namespaces.
  - A term is "external" if its namespace resolves to another ontology (or
    outside the corpus) under the shared ``pick_home`` ownership rule; "domain"
    if ``reference_kind`` does not identify its namespace as a representation
    language, metadata/identifier, or malformed. The headline coupling ratio is
    external-domain / total-domain, so language terms and citation metadata
    are excluded from both sides.
  - Each term is also classified by how the file uses it (logical, annotation,
    declaration only; ``ontometer_corpus.usage``). ``n_terms_logical`` and
    ``n_ext_logical`` restrict the domain tallies to terms used logically, so
    a vocabulary used only for metadata (Dublin Core on the header) does not
    count as coupling while the same vocabulary used for modelling does.

Large files: a file above the 50 MB in-memory guard that the parse phase
streamed (parse_format ``stream_xml_cvt`` / ``stream_nt_cvt``) is converted to
N-Triples with the same pyoxigraph reader and read line by line, so it
gets the same measurements. ``--only-failed`` re-reads just the files the
existing tables record as failed and splices them in.

OBO caveat: OBO cannot be converted to RDF by any tool available here
(pyoxigraph does not read OBO; owlready2 fails on these files), so OBO term
capture is limited to IDs appearing in logical-axiom lines (is_a,
relationship, intersection_of, union_of, disjoint_from, equivalent_to) plus
the ontology's own id: stanzas — it does not see IRIs buried in annotation
values the way the rdflib path does. OBO rows also leave
``n_logical_statements`` empty, so they fall outside that comparison.

Usage:
    ontometer-corpus term-coupling --store /path/to/ontologies
    ontometer-corpus term-coupling --store /path/to/ontologies --limit 50
    ontometer-corpus term-coupling --store /path/to/ontologies --only-failed --workers 3
"""

import json
import re
import sqlite3
import warnings
from pathlib import Path

import click

from ontometer_corpus.namespaces import iri_namespace, reference_kind
from ontometer_corpus.network.construction import (
    _namespace_counts,
    _ns_key,
    identity_iri,
)
from ontometer_corpus.network.entities import (
    NamespaceOwnerRecord,
    resolve_namespace_owners,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.pipeline.parse import (
    STREAM_FORMATS,
    _resolve_xml_entities,
    configure_paths,
    estimate_real_size,
    ntriples_lines,
    open_content_stream,
    scratch_dir,
    try_owlready2,
    try_rdflib,
)
from ontometer_corpus.usage import (
    ANNOTATION,
    LITERAL,
    LOGICAL,
    classify_graph,
    classify_owl_xml,
    classify_triples,
    namespace_counts,
    ntriples,
)

warnings.filterwarnings("ignore")

# Per-file wall-clock budget, enforced by build()'s subprocess supervisor
# (SIGTERM/SIGKILL from outside the process) rather than signal.alarm: alarm()
# only fires between Python bytecodes, so it can never interrupt a hang inside
# a C extension (lxml/libxml2 chewing on a malformed XML file), which can
# hold the process indefinitely. An external kill works regardless of what
# the process is doing internally.
_PER_FILE_TIMEOUT_S = 90
# Streamed files (see below) get a longer budget: a multi-GB dump is
# converted and read three times.
_STREAM_TIMEOUT_S = 3 * 3600

# Files above this decompressed size cannot be loaded into an in-memory
# rdflib graph in bounded time or memory. Those the parse phase streamed
# (parse_format stream_*_cvt) are re-read the same way: pyoxigraph streams
# them to N-Triples on disk, which is then read line by line (_stream_stats).
# Any other file above the guard is skipped (ok=False, reported in coverage).
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# OBO logical-axiom tags whose values carry cross-references to other terms.
# xref is deliberately excluded — it usually points at external databases
# (PMID, DOI, MeSH), not ontology terms, and would overstate coupling.
_OBO_REF_TAGS = (
    "is_a",
    "relationship",
    "intersection_of",
    "union_of",
    "disjoint_from",
    "equivalent_to",
)
_OBO_ID_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:\d+$")


def _obo_to_iri(obo_id: str, ontology_name: str | None) -> str:
    """Mirror _parse_obo's OBO-id -> IRI expansion exactly, so derived term
    namespaces match the stored own_namespaces."""
    if obo_id.startswith("http://") or obo_id.startswith("https://"):
        return obo_id
    if ":" in obo_id:
        part1, part2 = obo_id.split(":", 1)
        return f"http://purl.obolibrary.org/obo/{part1}_{part2}"
    return f"http://purl.obolibrary.org/obo/{ontology_name or 'obo'}_{obo_id}"


def _obo_term_iris(content: bytes, filename: str) -> tuple[set[str], set[str], set[str]]:
    """(own_iris, referenced_iris, annotation_iris) for an OBO flat file — own
    ids from every stanza, referenced ids from the logical-axiom lines, and
    the predicate and IRI value of every ``property_value`` line."""
    text = content.decode("utf-8", errors="ignore")
    ontology_name = None
    own_ids: set[str] = set()
    ref_ids: set[str] = set()
    ann_ids: set[str] = set()
    stanza: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            stanza = line[1:-1].strip().lower()
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.split("!")[0].strip()  # drop trailing "! human label"

        if key == "property_value":
            # "<predicate> <value> [datatype]": the predicate always, the
            # value when it is an ID or IRI rather than a quoted literal.
            raw_val = line.partition(":")[2].strip()
            tokens = raw_val.split()
            for tok in tokens[:2]:
                if _OBO_ID_TOKEN.match(tok) or tok.startswith(("http://", "https://")):
                    ann_ids.add(tok)
            continue
        if stanza is None:  # header
            if key == "ontology":
                ontology_name = val
            continue
        if key == "id":
            if stanza in ("term", "typedef", "instance"):
                own_ids.add(val)
        elif key in _OBO_REF_TAGS:
            # relationship lines are "<rel> <target>"; the others are "<target>".
            for tok in val.split():
                if _OBO_ID_TOKEN.match(tok):
                    ref_ids.add(tok)

    if not ontology_name:
        stem = Path(filename).stem
        ontology_name = (stem[3:] if stem.startswith("bp_") else stem).lower()

    own_iris = {_obo_to_iri(i, ontology_name) for i in own_ids}
    ref_iris = {_obo_to_iri(i, ontology_name) for i in ref_ids}
    ann_iris = {_obo_to_iri(i, ontology_name) for i in ann_ids}
    return own_iris, ref_iris, ann_iris


# Predicates used for a selected logical-statement proxy, as opposed to
# annotations (rdfs:label, rdfs:comment, dc:*, skos:*) and metadata. This still
# counts RDF triples, not OWL axioms: one restriction axiom normally spans
# several triples. rdf:type is included only for selected OWL entity kinds.
_AXIOM_PREDICATES = frozenset(
    {
        "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "http://www.w3.org/2000/01/rdf-schema#subPropertyOf",
        "http://www.w3.org/2000/01/rdf-schema#domain",
        "http://www.w3.org/2000/01/rdf-schema#range",
        "http://www.w3.org/2002/07/owl#equivalentClass",
        "http://www.w3.org/2002/07/owl#equivalentProperty",
        "http://www.w3.org/2002/07/owl#disjointWith",
        "http://www.w3.org/2002/07/owl#inverseOf",
        "http://www.w3.org/2002/07/owl#unionOf",
        "http://www.w3.org/2002/07/owl#intersectionOf",
        "http://www.w3.org/2002/07/owl#complementOf",
        "http://www.w3.org/2002/07/owl#oneOf",
        "http://www.w3.org/2002/07/owl#onProperty",
        "http://www.w3.org/2002/07/owl#someValuesFrom",
        "http://www.w3.org/2002/07/owl#allValuesFrom",
        "http://www.w3.org/2002/07/owl#hasValue",
        "http://www.w3.org/2002/07/owl#cardinality",
        "http://www.w3.org/2002/07/owl#minCardinality",
        "http://www.w3.org/2002/07/owl#maxCardinality",
        "http://www.w3.org/2002/07/owl#minQualifiedCardinality",
        "http://www.w3.org/2002/07/owl#maxQualifiedCardinality",
        "http://www.w3.org/2002/07/owl#propertyChainAxiom",
        "http://www.w3.org/2002/07/owl#hasKey",
        "http://www.w3.org/2002/07/owl#sameAs",
        "http://www.w3.org/2002/07/owl#differentFrom",
    }
)
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
# rdf:type objects that make the assertion a logical axiom (entity declaration
# or class-membership), rather than mere annotation-property typing.
_AXIOM_TYPE_OBJECTS = frozenset(
    {
        "http://www.w3.org/2002/07/owl#Class",
        "http://www.w3.org/2002/07/owl#ObjectProperty",
        "http://www.w3.org/2002/07/owl#DatatypeProperty",
        "http://www.w3.org/2002/07/owl#FunctionalProperty",
        "http://www.w3.org/2002/07/owl#InverseFunctionalProperty",
        "http://www.w3.org/2002/07/owl#TransitiveProperty",
        "http://www.w3.org/2002/07/owl#SymmetricProperty",
        "http://www.w3.org/2002/07/owl#NamedIndividual",
        "http://www.w3.org/2002/07/owl#Restriction",
    }
)


def _stats_from_graph(graph) -> tuple[set[str], int, int, dict[str, str]]:
    """Return coupling statistics and per-term usage modes from a parsed RDF graph."""
    import rdflib

    terms: set[str] = set()
    n_statements = 0
    restriction_nodes: set[str] = set()
    for s, p, o in graph:
        for t in (s, p, o):
            if isinstance(t, rdflib.URIRef):
                terms.add(str(t))
        ps = str(p)
        if ps in _AXIOM_PREDICATES or (ps == _RDF_TYPE and str(o) in _AXIOM_TYPE_OBJECTS):
            n_statements += 1
        if ps == _RDF_TYPE and str(o) == "http://www.w3.org/2002/07/owl#Restriction":
            restriction_nodes.add(str(s))
    return terms, n_statements, len(restriction_nodes), classify_graph(graph)


def _owl_xml_stats(content: bytes) -> tuple[set[str], int, int, dict[str, str]]:
    """Extract terms from OWL/XML accepted by the pipeline's XML parser."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(_resolve_xml_entities(content.decode("utf-8", errors="ignore")))
    if root.tag.split("}")[-1] != "Ontology":
        raise ValueError("not an OWL/XML Ontology document")
    prefixes = {
        el.attrib["name"].rstrip(":"): el.attrib["IRI"]
        for el in root.iter()
        if el.tag.split("}")[-1] == "Prefix" and "name" in el.attrib and "IRI" in el.attrib
    }
    base = root.attrib.get("ontologyIRI") or root.attrib.get(
        "{http://www.w3.org/XML/1998/namespace}base"
    )

    def expand(value: str) -> str | None:
        if value.startswith("#") and base:
            return base + value
        if ":" in value and not value.startswith(("http:", "https:", "urn:")):
            prefix, local = value.split(":", 1)
            if prefix in prefixes:
                return prefixes[prefix] + local
        return value if ":" in value else None

    terms: set[str] = set()
    logical_tags = {
        "Declaration",
        "SubClassOf",
        "EquivalentClasses",
        "DisjointClasses",
        "DisjointUnion",
        "SubObjectPropertyOf",
        "EquivalentObjectProperties",
        "DisjointObjectProperties",
        "ObjectPropertyDomain",
        "ObjectPropertyRange",
        "InverseObjectProperties",
        "FunctionalObjectProperty",
        "InverseFunctionalObjectProperty",
        "ReflexiveObjectProperty",
        "IrreflexiveObjectProperty",
        "SymmetricObjectProperty",
        "AsymmetricObjectProperty",
        "TransitiveObjectProperty",
        "SubDataPropertyOf",
        "EquivalentDataProperties",
        "DisjointDataProperties",
        "DataPropertyDomain",
        "DataPropertyRange",
        "FunctionalDataProperty",
        "DatatypeDefinition",
        "HasKey",
        "SameIndividual",
        "DifferentIndividuals",
        "ClassAssertion",
        "ObjectPropertyAssertion",
        "NegativeObjectPropertyAssertion",
        "DataPropertyAssertion",
        "NegativeDataPropertyAssertion",
    }
    n_statements = 0
    restrictions = 0
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in logical_tags:
            n_statements += 1
        if tag in {"ObjectSomeValuesFrom", "ObjectAllValuesFrom"}:
            restrictions += 1
        for attr in ("IRI", "abbreviatedIRI"):
            value = el.attrib.get(attr)
            if value and (iri := expand(value)):
                terms.add(iri)
        if tag == "Import" and el.text and (iri := expand(el.text.strip())):
            terms.add(iri)
    return terms, n_statements, restrictions, classify_owl_xml(root, expand)


def _rdf_stats(
    content: bytes, suffix: str, parse_format: str, xml_only: bool = False
) -> tuple[set[str], int, int, dict[str, str]]:
    """Term, selected logical-statement, and distinct OWL restriction counts.

    The counts are reported only for the OWL/RDF-parseable subset. Restriction
    nodes are deduplicated because one restriction normally spans several RDF
    triples and must contribute exactly once to restriction density.
    """
    import rdflib

    if parse_format in ("owl_xml", "owlready2"):
        # xml_only: owlready2 hung on this file in an earlier attempt (see
        # build()), so go straight to the direct OWL/XML reader.
        if xml_only:
            g, err = None, "skipped after a timeout"
        else:
            g, _, err = try_owlready2(content, suffix or ".owl")
        if g is None:
            try:
                return _owl_xml_stats(content)
            except Exception as xml_exc:
                raise RuntimeError(
                    f"owlready2 failed: {err}; OWL/XML fallback failed: {xml_exc}"
                ) from xml_exc
    else:
        fmt = {
            "turtle": "turtle",
            "json-ld": "json-ld",
            "stream_nt_cvt": "nt",
        }.get(parse_format, "xml")  # xml / stream_xml_cvt -> RDF/XML
        g = rdflib.Graph()
        try:
            g.parse(data=content, format=fmt)
        except Exception:
            g, _, err = try_rdflib(content)
            if g is None:
                raise RuntimeError(f"rdflib fallback failed: {err}")
    return _stats_from_graph(g)


def _stream_stats(
    path: Path, kind: str, zip_member: str | None, parse_format: str
) -> tuple[set[str], int, int, dict[str, str]]:
    """The statistics of :func:`_stats_from_graph` for a file too large for an
    in-memory graph: stream it to N-Triples with the parse phase's pyoxigraph
    reader, spill that to a scratch file, and read it line by line -- twice
    for the usage classification (declarations, then statements) and once for
    the counts."""
    import tempfile

    fmt_guess = "xml" if parse_format == "stream_xml_cvt" else "turtle_like"
    last_error = ""
    with tempfile.NamedTemporaryFile(
        "w+", suffix=".nt", dir=scratch_dir(), encoding="utf-8"
    ) as nt_file:
        for rdf_format in STREAM_FORMATS[fmt_guess]:
            nt_file.seek(0)
            nt_file.truncate()
            stream, handle = open_content_stream(path, kind, zip_member)
            try:
                nt_file.writelines(ntriples_lines(stream, rdf_format))
                break
            except SyntaxError as exc:
                last_error = str(exc)
            finally:
                stream.close()
                if handle is not None:
                    handle.close()
        else:
            tried = ", ".join(f.name for f in STREAM_FORMATS[fmt_guess])
            raise RuntimeError(f"streamed parse failed (tried: {tried}): {last_error}")
        nt_file.flush()

        def triples():
            nt_file.seek(0)
            return ntriples(nt_file)

        modes = classify_triples(triples)
        terms: set[str] = set()
        n_statements = 0
        restriction_nodes: set[str] = set()
        for s, p, o in triples():
            terms.add(p)
            for t in (s, o):
                if t is not LITERAL and not t.startswith("_:"):
                    terms.add(t)
            if p in _AXIOM_PREDICATES or (p == _RDF_TYPE and o in _AXIOM_TYPE_OBJECTS):
                n_statements += 1
            if p == _RDF_TYPE and o == "http://www.w3.org/2002/07/owl#Restriction":
                restriction_nodes.add(s)
    return terms, n_statements, len(restriction_nodes), modes


def _tally(
    resolved_own_ns: set[str],
    term_iris: set[str],
    modes: dict[str, str],
    extra_own_iris: set[str] | None = None,
) -> dict:
    """Count total/external terms at the all-terms, domain-only (language,
    metadata and malformed namespaces excluded) and logically-used domain
    granularities, using the pipeline's namespace logic."""
    own_terms = extra_own_iris or set()
    ns_by_term: dict[str, str] = {}
    for iri in term_iris | own_terms:
        ns = iri_namespace(iri)
        if ns:
            ns_by_term[iri] = ns

    def is_local(ns: str) -> bool:
        return ns in resolved_own_ns or _ns_key(ns) in resolved_own_ns

    n_all = len(ns_by_term)
    n_ext_all = sum(1 for ns in ns_by_term.values() if not is_local(ns))

    domain = {
        iri: ns for iri, ns in ns_by_term.items() if reference_kind(ns) == "domain_vocabulary"
    }
    n_dom = len(domain)
    n_ext_dom = sum(1 for ns in domain.values() if not is_local(ns))
    logical = {iri: ns for iri, ns in domain.items() if modes.get(iri) == LOGICAL}

    return {
        "n_terms_all": n_all,
        "n_ext_all": n_ext_all,
        "n_terms_domain": n_dom,
        "n_ext_domain": n_ext_dom,
        "n_terms_logical": len(logical),
        "n_ext_logical": sum(1 for ns in logical.values() if not is_local(ns)),
    }


def _usage_rows(modes: dict[str, str], extra=()) -> list[tuple[str, int, int, int]]:
    """(namespace, logical, annotation, declared) distinct-term counts."""
    return [
        (ns, c["logical"], c["annotation"], c["declared"])
        for ns, c in sorted(namespace_counts(modes, extra).items())
    ]


def _read_archive_content(path: Path, kind: str, zip_member: str | None) -> tuple[bytes, str, str]:
    """Read the selected, decompressed ontology payload from *path*."""
    stream, handle = open_content_stream(path, kind, zip_member)
    try:
        content = stream.read()
    finally:
        stream.close()
        if handle is not None:
            handle.close()
    filename = zip_member or path.name
    suffix = Path(filename).suffix.lower()
    if kind in {"gzip", "bzip2"} and suffix in {".gz", ".bz2"}:
        suffix = Path(filename).with_suffix("").suffix.lower()
    return content, suffix or ".owl", filename


def _process_one(
    path: Path,
    resolved_own_ns: set[str],
    parse_format: str,
    kind: str = "plain",
    zip_member: str | None = None,
    streamed: bool = False,
    xml_only: bool = False,
) -> dict:
    if streamed:
        term_iris, n_statements, n_restrictions, modes = _stream_stats(
            path, kind, zip_member, parse_format
        )
        rec = _tally(resolved_own_ns, term_iris, modes)
        rec["n_logical_statements"] = n_statements
        rec["n_restrictions"] = n_restrictions
        rec["namespace_usage"] = _usage_rows(modes)
        return rec
    content, suffix, filename = _read_archive_content(path, kind, zip_member)
    if parse_format == "obo":
        # OBO cannot be OWL-parsed here, so it is outside this RDF statement subset.
        own_iris, ref_iris, ann_iris = _obo_term_iris(content, filename)
        modes = {iri: ANNOTATION for iri in ann_iris} | {iri: LOGICAL for iri in ref_iris}
        rec = _tally(resolved_own_ns, ref_iris, modes, extra_own_iris=own_iris)
        rec["n_logical_statements"] = None
        rec["n_restrictions"] = None
        rec["namespace_usage"] = _usage_rows(modes)
        return rec
    term_iris, n_statements, n_restrictions, modes = _rdf_stats(
        content, suffix, parse_format, xml_only
    )
    rec = _tally(resolved_own_ns, term_iris, modes)
    rec["n_logical_statements"] = n_statements
    rec["n_restrictions"] = n_restrictions
    rec["namespace_usage"] = _usage_rows(modes)
    return rec


def _empty_rec(file_hash: str, content_hash: str, parse_format: str, error: str) -> dict:
    return {
        "file_hash": file_hash,
        "content_hash": content_hash,
        "parse_format": parse_format,
        "ok": False,
        "n_terms_all": 0,
        "n_ext_all": 0,
        "n_terms_domain": 0,
        "n_ext_domain": 0,
        "n_terms_logical": 0,
        "n_ext_logical": 0,
        "namespace_usage": [],
        "n_logical_statements": None,
        "n_restrictions": None,
        "error": error,
    }


def _is_streamed(parse_format: str | None) -> bool:
    """Whether the parse phase read this file with the pyoxigraph streaming path."""
    return parse_format in ("stream_xml_cvt", "stream_nt_cvt")


def _subprocess_target(task: tuple, conn) -> None:
    """Runs in its own OS process (see build()). Isolated per-task so a
    pathological file can be killed from the OUTSIDE via SIGTERM/SIGKILL --
    unlike signal.alarm, which only fires between Python bytecodes and can
    never interrupt a hang inside a C extension (lxml/libxml2 chewing on a
    malformed XML file can hold the process indefinitely)."""
    file_hash, content_hash, retrieved_file, own_ns_json, parse_format, xml_only = task
    path = Path(retrieved_file)
    resolved_own_ns = set(json.loads(own_ns_json or "[]"))
    try:
        if not path.exists():
            raise FileNotFoundError(str(path))
        size, kind, zip_member = estimate_real_size(path)
        if kind == "zip" and zip_member is None:
            raise RuntimeError("ZIP contains no supported ontology member")
        streamed = size > _MAX_FILE_BYTES and _is_streamed(parse_format)
        if size > _MAX_FILE_BYTES and not streamed:
            raise RuntimeError(f"skipped: {size / 1e6:.0f}MB > size guard")
        rec = {
            "file_hash": file_hash,
            "content_hash": content_hash,
            "parse_format": parse_format,
            "ok": False,
            "error": None,
        }
        rec.update(
            _process_one(path, resolved_own_ns, parse_format, kind, zip_member, streamed, xml_only)
        )
        rec["ok"] = True
    except BaseException as exc:  # incl. MemoryError
        rec = _empty_rec(
            file_hash, content_hash, parse_format, f"{type(exc).__name__}: {str(exc)[:120]}"
        )
    try:
        conn.send(rec)
    except (BrokenPipeError, OSError):
        pass  # parent already gave up and closed its end
    finally:
        conn.close()


def _owner_record(paths: CorpusPaths, row: sqlite3.Row) -> NamespaceOwnerRecord:
    """Namespace-ownership evidence of one latest corpus row."""
    declared, skos_concepts = _namespace_counts(paths, row["file_hash"], row["skos_concept_count"])
    return NamespaceOwnerRecord(
        owner=row["id"],
        ontology_iri=identity_iri(row),
        source_registry=row["source_registry"],
        own_namespaces=tuple(json.loads(row["own_namespaces"] or "[]")),
        declarations=declared,
        skos_concepts=skos_concepts,
    )


def build(
    paths: CorpusPaths,
    limit: int | None = None,
    workers: int = 8,
    only_failed: bool = False,
    timeout: int = _PER_FILE_TIMEOUT_S,
) -> None:
    """Re-read every latest parsed file, or with *only_failed* just those the
    existing ``term_coupling.parquet`` records as failed, replacing their rows
    in both output tables."""
    import multiprocessing as mp
    import time

    # The streamed path spills to the parse phase's scratch dir; forked
    # workers inherit this.
    configure_paths(paths)
    conn = sqlite3.connect(str(paths.db_path))
    conn.row_factory = sqlite3.Row
    ownership_rows = conn.execute(
        """
        SELECT id, ontology_iri, source_registry, file_hash, triple_count,
               own_namespaces, shared_iri, skos_concept_count
        FROM corpus_registry
        WHERE is_latest_version = 1 AND parse_status = 'ok' AND file_hash IS NOT NULL
        """
    ).fetchall()
    owners = resolve_namespace_owners(_owner_record(paths, row) for row in ownership_rows)
    resolved_own_by_row: dict[int, set[str]] = {}
    for namespace, owner in owners.items():
        resolved_own_by_row.setdefault(owner, set()).add(namespace)

    # One representative row per exact raw file among latest parsed rows.
    rows = conn.execute(
        """
        SELECT id, file_hash, content_hash, retrieved_file, parse_format
        FROM corpus_registry
        WHERE id IN (
            SELECT MIN(id) FROM corpus_registry
            WHERE is_latest_version = 1 AND parse_status = 'ok'
                  AND file_hash IS NOT NULL
            GROUP BY file_hash
        )
        """
    ).fetchall()
    conn.close()
    previous_records: list[dict] = []
    previous_usage: list[dict] = []
    if only_failed:
        import pyarrow.parquet as pq

        previous = pq.read_table(paths.term_coupling_path).to_pylist()
        failed = {r["file_hash"] for r in previous if not r["ok"]}
        previous_records = [r for r in previous if r["file_hash"] not in failed]
        previous_usage = [
            r
            for r in pq.read_table(paths.namespace_usage_path).to_pylist()
            if r["file_hash"] not in failed
        ]
        rows = [r for r in rows if r["file_hash"] in failed]
        print(f"Re-reading {len(rows)} previously failed files")
    if limit:
        rows = rows[:limit]

    from collections import deque

    # Stored paths are relative to the object store, so they are resolved here
    # in the parent and the workers receive something absolute.
    tasks = deque(
        (
            r["file_hash"],
            r["content_hash"],
            str(paths.resolve_object(r["retrieved_file"])),
            json.dumps(sorted(resolved_own_by_row.get(r["id"], set()))),
            r["parse_format"],
            False,
        )
        for r in rows
    )
    total = len(tasks)

    ctx = mp.get_context("fork")
    running: dict[int, list] = {}  # pid -> [process, conn, task, start_time]
    records = []
    n_ok = n_fail = 0

    def _spawn(task):
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        p = ctx.Process(target=_subprocess_target, args=(task, child_conn))
        p.start()
        child_conn.close()  # only the child needs its end open
        if p.pid is None:
            raise RuntimeError("term-coupling worker did not receive a PID")
        running[p.pid] = [p, parent_conn, task, time.monotonic()]

    while tasks or running:
        while len(running) < workers and tasks:
            _spawn(tasks.popleft())

        for pid, (p, pconn, task, start) in list(running.items()):
            budget = _STREAM_TIMEOUT_S if _is_streamed(task[4]) else timeout
            timed_out = time.monotonic() - start > budget
            if pconn.poll(0.2):
                try:
                    rec = pconn.recv()
                except EOFError:
                    rec = _empty_rec(
                        task[0], task[1], task[4], "EOFError: worker died without a result"
                    )
                p.join(2)
            elif timed_out:
                p.terminate()
                p.join(3)
                if p.is_alive():
                    p.kill()
                    p.join(3)
                if task[4] in ("owl_xml", "owlready2") and not task[5]:
                    # owlready2 can hang inside C code on some OWL/XML files;
                    # retry once with the direct OWL/XML reader.
                    pconn.close()
                    del running[pid]
                    tasks.append((*task[:5], True))
                    continue
                rec = _empty_rec(
                    task[0],
                    task[1],
                    task[4],
                    f"HardTimeout: exceeded {budget}s wall clock",
                )
            else:
                continue  # still running, within budget

            pconn.close()
            records.append(rec)
            n_ok += rec["ok"]
            n_fail += not rec["ok"]
            del running[pid]
            if len(records) % 100 == 0 or len(records) == total:
                print(f"  {len(records)}/{total} parsed  (ok={n_ok} fail={n_fail})", flush=True)

    import pyarrow as pa
    import pyarrow.parquet as pq

    usage_rows = [
        {
            "file_hash": rec["file_hash"],
            "namespace": ns,
            "n_logical_terms": n_log,
            "n_annotation_terms": n_ann,
            "n_declared_terms": n_decl,
        }
        for rec in records
        for ns, n_log, n_ann, n_decl in rec.pop("namespace_usage")
    ]
    records = previous_records + records
    usage_rows = previous_usage + usage_rows
    n_ok = sum(1 for r in records if r["ok"])
    n_fail = len(records) - n_ok
    out_path = paths.term_coupling_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records), out_path)
    print(f"Wrote {len(records)} rows to {out_path}  (ok={n_ok} fail={n_fail})")
    pq.write_table(pa.Table.from_pylist(usage_rows), paths.namespace_usage_path)
    print(f"Wrote {len(usage_rows)} rows to {paths.namespace_usage_path}")


@click.command()
@click.option("--limit", type=int, default=None, help="Process at most N ontologies.")
@click.option(
    "--workers", type=int, default=8, show_default=True, help="Concurrent worker processes."
)
@click.option(
    "--only-failed",
    is_flag=True,
    help="Re-read only the files the existing tables record as failed.",
)
@click.option(
    "--timeout",
    type=int,
    default=_PER_FILE_TIMEOUT_S,
    show_default=True,
    help="Per-file wall-clock budget in seconds (streamed files get longer).",
)
@corpus_paths
def main(
    limit: int | None, workers: int, only_failed: bool, timeout: int, paths: CorpusPaths
) -> None:
    """Term-level coupling per ontology. Needs the object store."""
    paths.require_store_dir()
    build(paths, limit=limit, workers=workers, only_failed=only_failed, timeout=timeout)


if __name__ == "__main__":
    main()
