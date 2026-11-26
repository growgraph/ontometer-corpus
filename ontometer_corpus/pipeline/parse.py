"""
Phase 1: Parse.

About:
    Processes all corpus_registry rows with parse_status IS NULL.
    For each row, attempts to parse the raw ontology file and extract:

      - Ontology IRI (declared by the file, else recovered from the registry's
        source_id — see _registry_declared_iri), version IRI, version info
      - Triple count, class count, property count, individual count (with
        its declared and SKOS-concept subsets — see _individual_counts)
      - owl:imports list
      - Content hash (SHA-256 of sorted class+property IRIs, blank nodes excluded)
      - Namespace footprint (referenced_namespaces / own_namespaces), which
        feeds the dependency network's namespace-usage edges
      - A per-file structural sidecar (subclass edges + entities), written
        to ONTOMETER_STORE/cache/structure/v<N>/<hh>/<file_hash>.parquet

    Parse strategy (in order):
      0. files whose estimated decompressed size exceeds _STREAM_THRESHOLD_BYTES
         go to a bounded-memory streaming path (pyoxigraph, read as N-Triples)
      1. rdflib — tries 'xml', 'turtle', 'n3', 'json-ld', 'nt', 'trig'
      2. custom parsers — offline, lightweight parsers for OBO and OWL/XML
      3. owlready2 — fallback helper (strips imports to prevent network requests)

    Results are written back to corpus_registry via db.py helpers.
    Failures are recorded as parse_status='fail' with parse_error populated.

    Workers run in a bounded ProcessPoolExecutor (capped max_workers,
    max_tasks_per_child=1) over tasks sorted by estimated decompressed size.
    Structural sidecars are written inside each worker, so large hierarchies
    never cross the process-pool boundary — only scalar metadata does. See
    `ontometer_corpus/network/README.md` for how the sidecars are consumed.

    No graph-isomorphism digest is computed: rdflib's canonicalisation can run
    unboundedly long on blank-node-dense graphs. Dedup relies on content_hash
    plus the raw-bytes file_hash instead.
"""

import bz2
import codecs
import contextlib
import gzip
import io
import os
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pyoxigraph
import rdflib
from pyoxigraph import RdfFormat
from rdflib import OWL, RDF, RDFS, URIRef
from rdflib.term import BNode, Node
from suthing import atomic_open, text_hash

from ontometer_corpus.fetchers._base import looks_like_feed
from ontometer_corpus.namespaces import OBO_BASE_NS, UNOWNABLE_NAMESPACES, iri_namespace
from ontometer_corpus.paths import CorpusPaths, MissingStore
from ontometer_corpus.roles import is_meta

from .db import (
    get_connection,
    update_parse_fail,
    update_parse_feed,
    update_parse_ok,
    update_parse_unsupported,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# rdflib format strings to try in order
_PARSE_FORMATS = ["xml", "turtle", "n3", "json-ld", "nt", "trig"]

# Cap ProcessPoolExecutor concurrency instead of defaulting to os.cpu_count():
# several large ontologies landing on workers at the same moment multiplies
# peak RAM by the worker count, so we bound it explicitly.
_DEFAULT_MAX_WORKERS = min(os.cpu_count() or 4, 4)

# OWL vocabulary helpers
_OWL_CLASS = OWL.Class
_RDFS_CLASS = RDFS.Class
_OWL_OBJ_PROP = OWL.ObjectProperty
_OWL_DATA_PROP = OWL.DatatypeProperty
_OWL_ANN_PROP = OWL.AnnotationProperty
_OWL_NAMED_IND = OWL.NamedIndividual
_RDF_PROPERTY = RDF.Property

# The entity types that define an ontology's "content" for the rdflib path.
#
# These two tuples are load-bearing and MUST stay in sync with each other's
# use sites: compute_content_hash() fingerprints exactly their union, while
# class_count/property_count count each half. dedup.py's Pass B relies on that
# equivalence — it gates on `(class_count + property_count) > 0` as a proxy for
# "this row contributed IRIs to the hash", because a row that contributed none
# hashes to SHA-256("") and would collide with every other empty ontology.
# Sharing the constants keeps the counters and the hash from diverging.
#
# Changing either tuple changes content_hash for affected rows, so the
# structural sidecars and the derived `structural_metrics.parquet` and
# `term_coupling.parquet` must be regenerated.
_CLASS_TYPES = (_OWL_CLASS, _RDFS_CLASS)
_PROPERTY_TYPES = (_OWL_OBJ_PROP, _OWL_DATA_PROP, _OWL_ANN_PROP, _RDF_PROPERTY)
_OWL_IMPORTS = OWL.imports
_OWL_VERSION_IRI = OWL.versionIRI
_OWL_VERSION_INFO = OWL.versionInfo
_OWL_ONTOLOGY = OWL.Ontology

# Individuals: what a file uses as an instance, not only what it declares.
#
# Counting `owl:NamedIndividual` declarations alone measures the authoring
# tool more than the content: `ex:lab1 a ex:Laboratory` is an individual
# whether or not Protégé also wrote `a owl:NamedIndividual`, and SKOS concept
# schemes, plain RDF data and hand-written Turtle almost never declare one.
# An individual is therefore a named subject that is
#   - declared `owl:NamedIndividual`, or
#   - typed by a domain type: anything outside the modelling-language
#     namespaces (`roles.is_meta`, shared with the role pass so the two agree
#     on what an instance is), plus `owl:Thing`, which marks an untyped
#     individual in older OWL;
# and is not itself a declared class or property, nor the ontology header.
# SKOS concepts are included — a thesaurus' concepts are its content — and
# also counted on their own (`skos_concept_count`, direct `skos:Concept`
# typing only) so analyses can tell a concept scheme from a knowledge base.
# The declared-only count is kept as `declared_individual_count`.
#
# None of this touches content_hash, the sidecar entities or own_namespaces:
# they stay declaration-based, so the dependency network is unchanged.
_OWL_THING_IRI = str(OWL.Thing)
_SKOS_CONCEPT_IRI = "http://www.w3.org/2004/02/skos/core#Concept"


def _is_instance_type(type_iri: str) -> bool:
    """Whether `?s rdf:type <type_iri>` makes ?s an individual."""
    return type_iri == _OWL_THING_IRI or not is_meta(type_iri)


def _individual_counts(
    declared: set[str],
    typed: set[str],
    skos_concepts: set[str],
    schema: set[str],
    headers: set[str],
) -> dict[str, int]:
    """The three individual counts every parser path reports.

    `typed` and `skos_concepts` are raw typed subjects; schema terms and
    ontology headers are removed here, so callers can collect them in one pass
    before the declarations are complete.
    """
    excluded = schema | headers
    individuals = declared | (typed - excluded)
    return {
        "individual_count": len(individuals),
        "declared_individual_count": len(declared),
        "skos_concept_count": len(skos_concepts & individuals),
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _guess_formats(head_str: str) -> list[str]:
    """
    Guess the likely rdflib format(s) from a small decoded prefix of the
    (post-normalisation) content, so the common case only needs one parse
    attempt instead of blindly cycling through every format in
    _PARSE_FORMATS. The full list is still appended as a safety net for
    ambiguous/malformed content, so this never reduces correctness — only
    the wasted work of building large partial graphs on wrong-format guesses.
    """
    stripped = head_str.lstrip()
    if stripped.startswith("<?xml") or stripped.startswith("<"):
        guessed = ["xml"]
    elif stripped.startswith(("@prefix", "@base", "PREFIX", "BASE")) or "owl:imports" in stripped:
        guessed = ["turtle", "n3"]
    elif stripped.startswith(("{", "[")):
        guessed = ["json-ld"]
    else:
        guessed = []
    return guessed + [fmt for fmt in _PARSE_FORMATS if fmt not in guessed]


def _apply_syntax_repairs(normalised: bytes) -> bytes:
    """
    Speculative regex-based repairs for malformed files (invalid xml:lang,
    unbracketed Turtle prefixes, dots instead of semicolons, stray spaces in
    prefixed names, invalid Turtle language tags). Returns the input
    unchanged (same object) if nothing needed fixing, so callers can cheaply
    tell whether it's worth retrying a parse with the result.

    This is deliberately only invoked *after* a plain parse attempt fails
    (see try_rdflib) — most files don't need it, and skipping it on the
    common path avoids holding several extra full-content string copies in
    memory for every file, large ones especially.
    """
    try:
        content_str = normalised.decode("utf-8", errors="ignore")
        modified = False

        # 1. Clean invalid xml:lang attributes in XML/RDF files
        if "xml:lang=" in content_str or "xml:lang='" in content_str:

            def clean_lang_tag(match):
                val = match.group(1).strip()
                # BCP 47 language tag validation check
                if not re.match(r"^[a-zA-Z]{1,8}(-[a-zA-Z0-9]{1,8})*$", val) or len(val) > 10:
                    return ""  # strip invalid xml:lang attribute
                return match.group(0)

            content_str = re.sub(r'\bxml:lang=["\']([^"\']+)["\']', clean_lang_tag, content_str)
            modified = True

        # 2. Fix Turtle prefix declarations and common syntax typos
        if "@prefix" in content_str or "\n@prefix" in content_str or "owl:imports" in content_str:
            # Fix unbracketed prefixes
            content_str = re.sub(
                r"@prefix\s+([a-zA-Z0-9_\-]+:)\s*([^<>\s]+)>?\s*\.",
                r"@prefix \1 <\2> .",
                content_str,
            )

            # Fix dots instead of semicolons before common properties
            content_str = re.sub(
                r"\.\s*(#.*)?\n\s*(owl:imports|owl:versionInfo|owl:versionIRI|skos:prefLabel|rdfs:label|skos:hasTopConcept)\b",
                r"; \1\n  \2",
                content_str,
            )

            # Fix spaces after colons in prefixed names (e.g. shv: reference_event -> shv:reference_event)
            content_str = re.sub(
                r"\b([a-zA-Z0-9_\-]+):\s+([a-zA-Z0-9_\-]+)\b", r"\1:\2", content_str
            )

            # Fix invalid language tags in Turtle (like @gmail.com or @something.else)
            if "@" in content_str:

                def clean_turtle_lang(match):
                    tag = match.group(1).lower()
                    if tag in ("prefix", "base"):
                        return match.group(0)
                    if "." in tag or len(tag) > 10:
                        return ""  # strip the invalid language tag
                    return match.group(0)

                content_str = re.compile(r"@([a-zA-Z0-9\-\.]+)").sub(clean_turtle_lang, content_str)

            modified = True

        if modified:
            return content_str.encode("utf-8")
    except Exception:
        pass
    return normalised


def _attempt_parse(content: bytes) -> tuple[rdflib.Graph | None, str | None, str | None]:
    head_str = content[:2000].decode("utf-8", errors="ignore")
    last_error: str | None = None
    for fmt in _guess_formats(head_str):
        g = rdflib.Graph()
        try:
            g.parse(data=content, format=fmt)
            if len(g) > 0:
                return g, fmt, None
        except Exception as exc:
            last_error = str(exc)
    return None, None, last_error


def try_rdflib(raw: bytes) -> tuple[rdflib.Graph | None, str | None, str | None]:
    """
    Try rdflib formats against the file content, cheapest path first.

    Returns
    -------
    (graph, format_used, error)
        graph       — populated Graph on success, None on failure
        format_used — format string that worked, None on failure
        error       — last exception message on failure, None on success
    """
    # Normalise Windows CRLF and bare CR line endings to LF before parsing.
    # rdflib's Turtle parser treats \r as part of prefixed names, which breaks
    # files saved by Windows tools.
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    graph, fmt, error = _attempt_parse(normalised)
    if graph is not None:
        return graph, fmt, None

    # Only pay for the speculative regex-based syntax repairs (multiple extra
    # full-content string copies) once the cheap raw attempt has failed for
    # every candidate format — most files parse fine unmodified.
    repaired = _apply_syntax_repairs(normalised)
    if repaired is normalised:
        return None, None, error
    return _attempt_parse(repaired)


def _strip_imports(content: bytes) -> bytes:
    """
    Remove import declarations from ontology content to prevent owlready2
    from making network requests during load.
    Handles:
      - OWL/XML: <Import>...</Import>
      - RDF/XML: <owl:imports rdf:resource="..."/>
      - OBO: import: ...
    """
    try:
        content_str = content.decode("utf-8", errors="ignore")
        # Remove OWL/XML <Import>...</Import>
        content_str = re.sub(r"<Import\b[^>]*>.*?</Import>", "", content_str, flags=re.DOTALL)
        # Remove RDF/XML <owl:imports ... />
        content_str = re.sub(r"<owl:imports\b[^>]*/>", "", content_str)
        # Remove RDF/XML <owl:imports ... > ... </owl:imports>
        content_str = re.sub(
            r"<owl:imports\b[^>]*>.*?</owl:imports>", "", content_str, flags=re.DOTALL
        )
        # Remove OBO import: ... lines
        lines = content_str.splitlines()
        cleaned_lines = [line for line in lines if not line.strip().startswith("import:")]
        content_str = "\n".join(cleaned_lines)
        return content_str.encode("utf-8")
    except Exception:
        return content


def try_owlready2(
    content: bytes, orig_suffix: str
) -> tuple[rdflib.Graph | None, str | None, str | None]:
    """
    Fall back to owlready2 (handles OWL/XML and OBO).
    Converts the ontology to N-Triples, then loads into rdflib.

    `content` is the already-read file bytes (reused from the caller to
    avoid a redundant full-file read) and `orig_suffix` is the source file's
    extension, used to pick the temp-file suffix owlready2 infers format from.

    Returns (graph, 'owlready2', None) on success, (None, None, error) on failure.
    """
    try:
        import owlready2  # optional dependency — imported lazily

        is_obo = content.lstrip().startswith(b"format-version:")

        # Strip imports to prevent network calls and local import resolution crashes
        cleaned_content = _strip_imports(content)

        # owlready2 infers format from extension. OBO files MUST end with .obo
        suffix = ".obo" if is_obo else orig_suffix
        if not suffix or suffix.lower() not in (".obo", ".owl", ".rdf", ".xml"):
            suffix = ".owl"

        tmp_onto_path: str | None = None
        tmp_nt_path: str | None = None

        try:
            # Create a cleaned version of the ontology file locally
            with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, dir=scratch_dir()
            ) as tmp_onto:
                tmp_onto.write(cleaned_content)
                tmp_onto_path = tmp_onto.name

            with tempfile.NamedTemporaryFile(
                suffix=".nt", delete=False, dir=scratch_dir()
            ) as tmp_nt:
                tmp_nt_path = tmp_nt.name

            with contextlib.redirect_stderr(io.StringIO()):
                onto = owlready2.get_ontology(
                    f"file://{Path(tmp_onto_path).resolve().as_posix()}"
                ).load()
                onto.save(file=tmp_nt_path, format="ntriples")

            g = rdflib.Graph()
            g.parse(tmp_nt_path, format="nt")
            return g, "owlready2", None

        finally:
            if tmp_onto_path and os.path.exists(tmp_onto_path):
                os.remove(tmp_onto_path)
            if tmp_nt_path and os.path.exists(tmp_nt_path):
                os.remove(tmp_nt_path)

    except Exception as exc:
        return None, None, str(exc)


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


#: Header predicates that declare a licence or rights statement, as full IRIs.
LICENCE_PREDICATES: frozenset[str] = frozenset(
    {
        "http://purl.org/dc/terms/license",
        "http://purl.org/dc/terms/rights",
        "http://purl.org/dc/elements/1.1/rights",
        "http://creativecommons.org/ns#license",
        "http://schema.org/license",
        "https://schema.org/license",
        "http://www.w3.org/1999/xhtml/vocab#license",
    }
)
#: The same predicates as OBO headers spell them.
_OBO_LICENCE_PROPERTIES: frozenset[str] = LICENCE_PREDICATES | frozenset(
    {
        "dcterms:license",
        "dcterms-license",
        "terms:license",
        "dcterms:rights",
        "dcterms-rights",
        "terms:rights",
        "dc:rights",
        "dc-rights",
        "cc:license",
        "schema:license",
    }
)
#: Classes whose instances are an ontology header: licence statements on any
#: other subject (a term, a dataset item) are ignored.
_HEADER_TYPES: frozenset[str] = frozenset(
    {"http://www.w3.org/2002/07/owl#Ontology", "http://www.w3.org/2004/02/skos/core#ConceptScheme"}
)
MAX_LICENCE_VALUES = 5
MAX_LICENCE_CHARS = 500


def _licence_values(values) -> list[str]:
    """Raw licence values, stripped, truncated, deduplicated and capped, in order."""
    out: list[str] = []
    for value in values:
        text = str(value).strip()[:MAX_LICENCE_CHARS]
        if text and text not in out:
            out.append(text)
            if len(out) == MAX_LICENCE_VALUES:
                break
    return out


def _uri_or_none(term) -> str | None:
    """Return the string form of a URIRef, or None for BNodes/Literals."""
    return str(term) if isinstance(term, URIRef) else None


# LOV source_ids embed the vocabulary's own URI: "lov:<uri>::<version_label>".
# That's the registry's declaration of the artifact's identity, and it's the
# only registry whose source_id carries one (OBO uses a short id, OntoPortal an
# acronym + submission id, Ontohub a repo path, OKG a Wikidata Q-number).
_REGISTRY_IRI_RE = re.compile(r"^lov:(?P<iri>https?://.+?)::[^:]*$")


def _registry_declared_iri(source_id: str | None) -> str | None:
    """
    The ontology IRI the *registry* claims for this artifact, when it can be
    recovered from source_id.

    Used only as a fallback: many plain RDF/RDFS vocabularies (Dublin Core,
    RSS, WGS84, SWRL, ...) never declare `?s rdf:type owl:Ontology`, so
    _detect_ontology_iri finds nothing and the row lands in the graph as an
    anonymous "row:<id>" node that nothing can ever reference — silently
    zeroing its in-degree, PageRank and authority scores. The registry knew
    the identity all along; this reads it back.
    """
    if not source_id:
        return None
    m = _REGISTRY_IRI_RE.match(source_id)
    return m.group("iri") if m else None


def _first_literal(graph: rdflib.Graph, subject, predicate) -> str | None:
    """Return the string value of the first matching literal triple, or None."""
    for obj in graph.objects(subject, predicate):
        if not isinstance(obj, BNode):
            return str(obj)
    return None


def _detect_ontology_iri(graph: rdflib.Graph) -> Node | None:
    """
    Detect the subject of the owl:Ontology declaration.
    Prefers URIRef subjects; falls back to the first BNode subject.
    """
    uri_subject: URIRef | None = None
    bnode_subject: Node | None = None

    for s in graph.subjects(RDF.type, _OWL_ONTOLOGY):
        if isinstance(s, URIRef):
            # A file: IRI is the path the parser read (a relative `<>` header
            # resolved against it, or owlready2's scratch copy), not an
            # identity the ontology declares, and it changes on every parse.
            if str(s).startswith("file:"):
                continue
            uri_subject = s
            break
        elif bnode_subject is None:
            bnode_subject = s

    return uri_subject or bnode_subject


def compute_content_hash(graph: rdflib.Graph) -> str:
    """
    Compute a stable content fingerprint for deduplication.

    Algorithm:
      1. Collect all non-blank-node subjects declared as one of
         _CLASS_TYPES + _PROPERTY_TYPES.
      2. Sort the IRI strings lexicographically.
      3. Join with newlines, encode UTF-8, SHA-256.

    Blank nodes are excluded because they vary across serialisations of the
    same logical ontology.

    The type list is shared with class_count/property_count via _CLASS_TYPES /
    _PROPERTY_TYPES — see the note there for why that coupling is mandatory.
    """
    iris: set[str] = set()

    for rdf_type in _CLASS_TYPES + _PROPERTY_TYPES:
        for s in graph.subjects(RDF.type, rdf_type):
            if isinstance(s, URIRef):
                iris.add(str(s))

    canonical = "\n".join(sorted(iris))
    return text_hash(canonical)


# ---------------------------------------------------------------------------
# Namespace + structural (subclass/entity) collection
# ---------------------------------------------------------------------------
#
# These feed two downstream consumers of the parse phase:
#   - corpus_registry.referenced_namespaces / own_namespaces columns, used to
#     build the ontology dependency network's namespace-usage edges (a
#     referenced namespace not in own_namespaces is an external dependency).
#   - per-ontology structural sidecar parquet files (subclass_edges, entities)
#     for future hierarchy/entity metrics (taxonomic depth, branching factor,
#     schema/axiom density) with no further re-parsing.
#
# Every metadata-producing parser path populates the same five keys in its
# returned meta dict: referenced_namespaces, own_namespaces (both list[str]),
# subclass_edges (list[(child, parent)]), entities (list[(iri, kind)]), and
# skos_concepts (list[iri]). parse_worker strips the three structural keys out
# to the sidecar before
# the (small) scalar meta is sent back over the process pool.

# Entity kinds recorded in the sidecar, keyed by rdf:type (rdflib paths).
_ENTITY_KIND_BY_TYPE = [
    (_OWL_CLASS, "class"),
    (_RDFS_CLASS, "class"),
    (_OWL_OBJ_PROP, "object_property"),
    (_OWL_DATA_PROP, "datatype_property"),
    (_OWL_ANN_PROP, "annotation_property"),
    (_OWL_NAMED_IND, "individual"),
]


# OBO Foundry term IDs (GO_0008150, CHEBI_36357, CL_0000000, ...) all share one
# generic path, http://purl.obolibrary.org/obo/ — the real identity is the
# PREFIX_ in the local name, not the shared path. Without this, every OBO
# ontology's own_namespaces would claim to "own" that bare generic path, and
# whichever one is processed first in a query silently absorbs every other
# OBO ontology's term references as if they all pointed at it specifically.
# These live in ontometer_corpus.namespaces: one definition of the namespaces no
# ontology can own (the representation languages and the bare OBO root) keeps
# every coupling number comparable.
_OBO_BASE_NS = OBO_BASE_NS
_UNOWNABLE_NAMESPACES = UNOWNABLE_NAMESPACES
_iri_namespace = iri_namespace


def _namespaces_of(iris) -> set[str]:
    """Distinct namespace prefixes of an iterable of IRI strings."""
    out: set[str] = set()
    for iri in iris:
        ns = _iri_namespace(iri)
        if ns:
            out.add(ns)
    return out


def _dedup_entities(entities: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order-independent de-duplication of (iri, kind) entity records."""
    return sorted(set(entities))


def _collect_subclass_edges(graph: rdflib.Graph) -> list[tuple[str, str]]:
    """Named-class rdfs:subClassOf edges (child_iri, parent_iri); BNodes skipped."""
    edges: list[tuple[str, str]] = []
    for child, parent in graph.subject_objects(RDFS.subClassOf):
        if isinstance(child, URIRef) and isinstance(parent, URIRef):
            edges.append((str(child), str(parent)))
    return edges


def _collect_entities_rdflib(graph: rdflib.Graph) -> list[tuple[str, str]]:
    """(iri, kind) for every named class/property/individual declaration."""
    entities: list[tuple[str, str]] = []
    for rdf_type, kind in _ENTITY_KIND_BY_TYPE:
        for s in graph.subjects(RDF.type, rdf_type):
            if isinstance(s, URIRef):
                entities.append((str(s), kind))
    return _dedup_entities(entities)


def _collect_referenced_namespaces_rdflib(graph: rdflib.Graph) -> set[str]:
    """Namespaces of every URIRef term appearing as subject/predicate/object."""
    ns: set[str] = set()
    for s, p, o in graph:
        for term in (s, p, o):
            if isinstance(term, URIRef):
                n = _iri_namespace(str(term))
                if n:
                    ns.add(n)
    return ns


def extract_metadata(graph: rdflib.Graph, source_id: str | None = None) -> dict:
    """
    Extract all parse (Phase 1) metadata fields from a parsed rdflib Graph.

    Returns a dict with keys matching corpus_registry columns, plus the
    transient structural keys (subclass_edges, entities) that parse_worker
    diverts to the sidecar rather than the DB.

    source_id is optional and used only to recover an ontology IRI the file
    itself never declares — see _registry_declared_iri.
    """
    onto_subject = _detect_ontology_iri(graph)

    ontology_iri: str | None = _uri_or_none(onto_subject) if onto_subject else None

    # Fall back to the registry's declared IRI when the file declares none of
    # its own. Recorded as 'registry' rather than 'declared' so a derived
    # identity is never mistaken for one the artifact actually asserts.
    iri_source = "declared" if ontology_iri else None
    if not ontology_iri:
        ontology_iri = _registry_declared_iri(source_id)
        if ontology_iri:
            iri_source = "registry"
    ontology_version_iri: str | None = None
    ontology_version_info: str | None = None

    if onto_subject is not None:
        # owl:versionIRI
        for obj in graph.objects(onto_subject, _OWL_VERSION_IRI):
            if isinstance(obj, URIRef):
                ontology_version_iri = str(obj)
                break
        # owl:versionInfo
        ontology_version_info = _first_literal(graph, onto_subject, _OWL_VERSION_INFO)

    # Licences come from every header subject, not only onto_subject, which
    # skips the file: IRI that `<> a owl:Ontology` resolves to.
    headers = sorted(
        {s for t in _HEADER_TYPES for s in graph.subjects(RDF.type, URIRef(t))}, key=str
    )
    licence = _licence_values(
        o
        for s in headers
        for p in sorted(LICENCE_PREDICATES)
        for o in sorted(graph.objects(s, URIRef(p)), key=str)
        if not isinstance(o, BNode)  # a blank node's label differs on every parse
    )

    # Metrics
    triple_count = len(graph)

    class_iris: set[str] = set()
    for rdf_type in _CLASS_TYPES:
        for s in graph.subjects(RDF.type, rdf_type):
            if isinstance(s, URIRef):
                class_iris.add(str(s))

    # _PROPERTY_TYPES includes rdf:Property: plain RDF/RDFS vocabularies
    # predating OWL conventions declare their terms `a rdf:Property` only, so
    # counting owl:* property types alone would report zero properties for
    # them. compute_content_hash counts the same set, so the dedup guard stays
    # valid.
    property_iris: set[str] = set()
    for rdf_type in _PROPERTY_TYPES:
        for s in graph.subjects(RDF.type, rdf_type):
            if isinstance(s, URIRef):
                property_iris.add(str(s))

    individual_iris: set[str] = set()
    for s in graph.subjects(RDF.type, _OWL_NAMED_IND):
        if isinstance(s, URIRef):
            individual_iris.add(str(s))

    typed_iris: set[str] = set()
    skos_concept_iris: set[str] = set()
    for s, o in graph.subject_objects(RDF.type):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and _is_instance_type(str(o)):
            typed_iris.add(str(s))
            if str(o) == _SKOS_CONCEPT_IRI:
                skos_concept_iris.add(str(s))
    headers = {str(s) for s in graph.subjects(RDF.type, _OWL_ONTOLOGY) if isinstance(s, URIRef)}

    imports: list[str] = []
    if onto_subject is not None:
        for obj in graph.objects(onto_subject, _OWL_IMPORTS):
            iri = _uri_or_none(obj)
            if iri:
                imports.append(iri)

    own_ns = _namespaces_of(class_iris | property_iris | individual_iris)
    own_iri_ns = _iri_namespace(ontology_iri)
    if own_iri_ns:
        own_ns.add(own_iri_ns)
    own_ns -= _UNOWNABLE_NAMESPACES
    referenced_ns = _collect_referenced_namespaces_rdflib(graph)

    return {
        "ontology_iri": ontology_iri,
        "iri_source": iri_source,
        "ontology_version_iri": ontology_version_iri,
        "ontology_version_info": ontology_version_info,
        "licence": licence,
        "triple_count": triple_count,
        "class_count": len(class_iris),
        "property_count": len(property_iris),
        **_individual_counts(
            individual_iris, typed_iris, skos_concept_iris, class_iris | property_iris, headers
        ),
        "imports": sorted(imports),
        "content_hash": compute_content_hash(graph),
        "referenced_namespaces": sorted(referenced_ns),
        "own_namespaces": sorted(own_ns),
        "subclass_edges": _collect_subclass_edges(graph),
        "entities": _collect_entities_rdflib(graph),
        "skos_concepts": sorted(skos_concept_iris),
    }


# ---------------------------------------------------------------------------
# Phase 1 entry point (Parallel Worker & Orchestrator)
# ---------------------------------------------------------------------------


def _extract_imports(content: bytes) -> list[str]:
    """
    Statically extract import IRIs from the raw file content to ensure they
    are captured. Handles:
      - OWL/XML and RDF/XML: <Import>...</Import> or <owl:imports rdf:resource="..."/>
      - Turtle: owl:imports <...>
      - OBO: import: ...
    """
    imports = set()
    try:
        content_str = content.decode("utf-8", errors="ignore")

        # Match <Import>...</Import>
        for m in re.finditer(r"<Import\b[^>]*>([^<]+)</Import>", content_str):
            imports.add(m.group(1).strip())

        # Match <owl:imports rdf:resource="..."/>
        for m in re.finditer(r'<owl:imports\s+[^>]*rdf:resource=["\']([^"\']+)["\']', content_str):
            imports.add(m.group(1).strip())

        # Match owl:imports <...> in Turtle
        for m in re.finditer(r"owl:imports\s+<([^>]+)>", content_str):
            imports.add(m.group(1).strip())

        # Match OBO import: ...
        for line in content_str.splitlines():
            if line.strip().startswith("import:"):
                val = line.split(":", 1)[1].strip()
                if "!" in val:
                    val = val.split("!", 1)[0].strip()
                if val:
                    imports.add(val)
    except Exception:
        pass
    return sorted(list(imports))


def _resolve_xml_entities(xml_content: str) -> str:
    entities = {}
    for m in re.finditer(r"<!ENTITY\s+([a-zA-Z0-9_\-]+)\s+[\'\"]([^\'\"]+)[\'\"]\s*>", xml_content):
        entities[m.group(1)] = m.group(2)

    cleaned = re.sub(r"<!DOCTYPE\s+[^\[>]+(?:\[[^\]]*\])?\s*>", "", xml_content, flags=re.DOTALL)

    for _ in range(3):
        replaced = False
        for k, v in entities.items():
            pattern = f"&{k};"
            if pattern in cleaned:
                cleaned = cleaned.replace(pattern, v)
                replaced = True
        if not replaced:
            break
    return cleaned


def _parse_owl_xml(
    content_bytes: bytes, filestem: str
) -> tuple[rdflib.Graph | None, str | None, str | None, dict | None]:
    """
    Custom offline parser for OWL/XML format using xml.etree.ElementTree.
    Returns (None, 'owl_xml', None, meta) on success, or (None, None, error, None) on failure.
    """
    try:
        import xml.etree.ElementTree as ET

        content = content_bytes.decode("utf-8", errors="ignore")

        cleaned = _resolve_xml_entities(content)
        root = ET.fromstring(cleaned)

        root_tag = root.tag.split("}")[-1]
        if root_tag != "Ontology":
            return None, None, "Root element is not Ontology", None

        ontology_iri = root.attrib.get("ontologyIRI")
        version_iri = root.attrib.get("versionIRI")
        base_iri = root.attrib.get("{http://www.w3.org/XML/1998/namespace}base") or ontology_iri

        # No ontologyIRI attribute: left None for parse_worker's registry
        # fallback (_settle_ontology_iri). The file stem is the object hash, so
        # an IRI built from it would be an invented OBO-looking identity.

        imports = []
        classes = set()
        object_props = set()
        datatype_props = set()
        annotation_props = set()
        individuals = set()
        subclass_edges: list[tuple[str, str]] = []
        referenced_iris: set[str] = set()
        version_info = None
        triple_count = 0

        # OWL/XML makes every individual reference an explicit NamedIndividual
        # element, so `individuals` already covers typed ones; only the SKOS
        # subset needs ClassAssertion(skos:Concept, x), with prefixes resolved.
        prefixes: dict[str, str] = {}
        skos_concepts: set[str] = set()

        def _named_iri(e):
            v = e.attrib.get("IRI") or e.attrib.get("abbreviatedIRI")
            if v and v.startswith("#") and base_iri:
                v = base_iri + v
            return v

        def _full_iri(e) -> str | None:
            if e.attrib.get("IRI"):
                return _named_iri(e)
            abbrev = e.attrib.get("abbreviatedIRI")
            if abbrev and ":" in abbrev:
                prefix, local_name = abbrev.split(":", 1)
                if prefix in prefixes:
                    return prefixes[prefix] + local_name
            return abbrev

        for el in root.iter():
            triple_count += 1
            tag_name = el.tag.split("}")[-1]

            if tag_name == "Prefix":
                if el.attrib.get("name") is not None and el.attrib.get("IRI"):
                    prefixes[el.attrib["name"]] = el.attrib["IRI"]
            elif tag_name == "ClassAssertion":
                kids = {sub.tag.split("}")[-1]: sub for sub in el}
                if "Class" in kids and "NamedIndividual" in kids:
                    if _full_iri(kids["Class"]) == _SKOS_CONCEPT_IRI:
                        ind = _named_iri(kids["NamedIndividual"])
                        if ind:
                            skos_concepts.add(ind)

            if tag_name == "Import":
                imp = el.text.strip() if el.text else ""
                imports.append(imp)
                if imp:
                    referenced_iris.add(imp)
            elif tag_name == "SubClassOf":
                # OWL/XML: <SubClassOf><Class IRI="child"/><Class IRI="parent"/>…
                # First two named Class children are (subclass, superclass).
                named = [_named_iri(sub) for sub in el if sub.tag.split("}")[-1] == "Class"]
                named = [n for n in named if n]
                if len(named) >= 2:
                    subclass_edges.append((named[0], named[1]))
                    referenced_iris.update(named[:2])
            elif tag_name == "Annotation":
                prop_el = el.find(".//{http://www.w3.org/2002/07/owl#}AnnotationProperty")
                if prop_el is None:
                    for sub in el:
                        if sub.tag.split("}")[-1] == "AnnotationProperty":
                            prop_el = sub
                            break
                if prop_el is not None:
                    abbrev = prop_el.attrib.get("abbreviatedIRI")
                    iri = prop_el.attrib.get("IRI")
                    if (
                        abbrev == "owl:versionInfo"
                        or iri == "http://www.w3.org/2002/07/owl#versionInfo"
                    ):
                        lit_el = el.find(".//Literal")
                        if lit_el is None:
                            for sub in el:
                                if sub.tag.split("}")[-1] == "Literal":
                                    lit_el = sub
                                    break
                        if lit_el is not None and lit_el.text:
                            version_info = lit_el.text.strip()
            else:
                iri = el.attrib.get("IRI")
                abbrev = el.attrib.get("abbreviatedIRI")
                val = iri or abbrev
                if val:
                    if val.startswith("#") and base_iri:
                        val = base_iri + val

                    referenced_iris.add(val)
                    if tag_name == "Class":
                        classes.add(val)
                    elif tag_name == "ObjectProperty":
                        object_props.add(val)
                    elif tag_name == "DatatypeProperty":
                        datatype_props.add(val)
                    elif tag_name == "AnnotationProperty":
                        annotation_props.add(val)
                    elif tag_name == "NamedIndividual":
                        individuals.add(val)

        # Direct children only: a nested Annotation belongs to an axiom.
        header_licences: list[str] = []
        for el in root:
            if el.tag.split("}")[-1] != "Annotation":
                continue
            kids = {sub.tag.split("}")[-1]: sub for sub in el}
            prop_el = kids.get("AnnotationProperty")
            if prop_el is None or _full_iri(prop_el) not in LICENCE_PREDICATES:
                continue
            for kind in ("IRI", "AbbreviatedIRI", "Literal"):
                value_el = kids.get(kind)
                if value_el is None or not value_el.text:
                    continue
                value = value_el.text.strip()
                if kind == "AbbreviatedIRI":
                    prefix, _, local_name = value.partition(":")
                    value = prefixes[prefix] + local_name if prefix in prefixes else value
                header_licences.append(value)
                break
        licence = _licence_values(header_licences)

        properties = object_props | datatype_props | annotation_props
        all_iris = classes.union(properties)
        canonical = "\n".join(sorted(all_iris))
        content_hash = text_hash(canonical)

        entities = (
            [(c, "class") for c in classes]
            + [(p, "object_property") for p in object_props]
            + [(p, "datatype_property") for p in datatype_props]
            + [(p, "annotation_property") for p in annotation_props]
            + [(i, "individual") for i in individuals]
        )
        own_ns = _namespaces_of(classes | properties | individuals)
        oiri_ns = _iri_namespace(ontology_iri)
        if oiri_ns:
            own_ns.add(oiri_ns)
        own_ns -= _UNOWNABLE_NAMESPACES
        referenced_ns = _namespaces_of(referenced_iris) | own_ns

        meta = {
            "ontology_iri": ontology_iri,
            "ontology_version_iri": version_iri,
            "ontology_version_info": version_info,
            "licence": licence,
            "triple_count": triple_count,
            "class_count": len(classes),
            "property_count": len(properties),
            **_individual_counts(individuals, set(), skos_concepts, set(), set()),
            "imports": sorted(imports),
            "content_hash": content_hash,
            "referenced_namespaces": sorted(referenced_ns),
            "own_namespaces": sorted(own_ns),
            "subclass_edges": subclass_edges,
            "entities": _dedup_entities(entities),
            "skos_concepts": sorted(skos_concepts),
        }
        return None, "owl_xml", None, meta
    except Exception as exc:
        return None, None, str(exc), None


def _parse_obo(
    content_bytes: bytes, filestem: str
) -> tuple[rdflib.Graph | None, str | None, str | None, dict | None]:
    """
    Custom offline parser for OBO format.
    Returns (None, 'obo', None, meta) on success, or (None, None, error, None) on failure.
    """
    try:
        content = content_bytes.decode("utf-8", errors="ignore")
        lines = content.splitlines()

        ontology_name = None
        version_info = None
        version_iri = None
        imports = []
        header_licences: list[str] = []

        class_ids = set()
        property_ids = set()
        individual_ids = set()
        subclass_id_edges: list[tuple[str, str]] = []
        current_id = None

        in_header = True
        current_stanza_type = None
        triple_count = 0

        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            if line_stripped.startswith("[") and line_stripped.endswith("]"):
                in_header = False
                current_stanza_type = line_stripped[1:-1].strip().lower()
                current_id = None
                continue

            if ":" in line_stripped:
                parts = line_stripped.split(":", 1)
                key = parts[0].strip().lower()
                val = parts[1].strip()

                if "!" in val:
                    val_clean = []
                    in_quotes = False
                    for char in val:
                        if char == '"':
                            in_quotes = not in_quotes
                        elif char == "!" and not in_quotes:
                            break
                        val_clean.append(char)
                    val = "".join(val_clean).strip()

                triple_count += 1

                if in_header:
                    if key == "ontology":
                        ontology_name = val
                    elif key == "data-version":
                        version_info = val
                        if val.startswith("http://") or val.startswith("https://"):
                            version_iri = val
                    elif key == "date" and not version_info:
                        version_info = val
                    elif key == "import":
                        imports.append(val)
                    elif key == "license":
                        header_licences.append(val.strip('"'))
                    elif key == "property_value":
                        prop, _, value = val.partition(" ")
                        if prop.lower() in _OBO_LICENCE_PROPERTIES:
                            value = value.strip()
                            if value.startswith('"'):
                                # "text" xsd:string -> text
                                value = value[1:].rsplit('"', 1)[0]
                            header_licences.append(value)
                else:
                    if key == "id":
                        current_id = val
                        if current_stanza_type == "term":
                            class_ids.add(val)
                        elif current_stanza_type == "typedef":
                            property_ids.add(val)
                        elif current_stanza_type == "instance":
                            individual_ids.add(val)
                    elif key == "is_a" and current_stanza_type == "term" and current_id and val:
                        subclass_id_edges.append((current_id, val))

        # Without an `ontology:` header there is no OBO identity: the IRI is
        # left None for parse_worker's registry fallback (_settle_ontology_iri).
        # The file stem (the object hash) still prefixes unprefixed term ids.
        ontology_iri = (
            f"http://purl.obolibrary.org/obo/{ontology_name}.owl" if ontology_name else None
        )
        if not ontology_name:
            stem = filestem
            if stem.startswith("bp_"):
                stem = stem[3:]
            ontology_name = stem.lower()

        if version_info and ontology_iri and not version_iri:
            clean_v = re.sub(r"[^a-zA-Z0-9\-\./:_]", "_", version_info)
            version_iri = f"http://purl.obolibrary.org/obo/{ontology_name}/releases/{clean_v}/{ontology_name}.owl"

        def to_iri(obo_id):
            if obo_id.startswith("http://") or obo_id.startswith("https://"):
                return obo_id
            if ":" in obo_id:
                part1, part2 = obo_id.split(":", 1)
                return f"http://purl.obolibrary.org/obo/{part1}_{part2}"
            else:
                return f"http://purl.obolibrary.org/obo/{ontology_name or 'obo'}_{obo_id}"

        class_iris = {to_iri(cid) for cid in class_ids}
        property_iris = {to_iri(pid) for pid in property_ids}
        individual_iris = {to_iri(iid) for iid in individual_ids}

        all_iris = class_iris.union(property_iris)
        canonical = "\n".join(sorted(all_iris))
        content_hash = text_hash(canonical)

        # is_a targets are class IRIs; OBO typedefs are relations → object props.
        subclass_edges = [(to_iri(c), to_iri(p)) for c, p in subclass_id_edges]
        entities = (
            [(c, "class") for c in class_iris]
            + [(p, "object_property") for p in property_iris]
            + [(i, "individual") for i in individual_iris]
        )
        own_ns = _namespaces_of(class_iris | property_iris | individual_iris)
        oiri_ns = _iri_namespace(ontology_iri)
        if oiri_ns:
            own_ns.add(oiri_ns)
        own_ns -= _UNOWNABLE_NAMESPACES
        referenced_iris = class_iris | property_iris | individual_iris
        referenced_iris.update(p for _, p in subclass_edges)
        referenced_ns = _namespaces_of(referenced_iris) | own_ns

        meta = {
            "ontology_iri": ontology_iri,
            "ontology_version_iri": version_iri,
            "ontology_version_info": version_info,
            "licence": _licence_values(header_licences),
            "triple_count": triple_count,
            "class_count": len(class_ids),
            "property_count": len(property_ids),
            **_individual_counts(individual_iris, set(), set(), set(), set()),
            "imports": sorted(imports),
            "content_hash": content_hash,
            "referenced_namespaces": sorted(referenced_ns),
            "own_namespaces": sorted(own_ns),
            "subclass_edges": subclass_edges,
            "entities": _dedup_entities(entities),
            "skos_concepts": [],
        }
        return None, "obo", None, meta
    except Exception as exc:
        return None, None, str(exc), None


# ---------------------------------------------------------------------------
# Bounded-memory streaming path for very large files
# ---------------------------------------------------------------------------
#
# rdflib is a pure-Python, in-memory graph library: parsing a file fully
# materialises every triple as Python objects, so its memory use is a multiple
# of the decompressed file size. Ontologies can be ZIP/GZIP-wrapped ("small"
# on disk, huge once decompressed) or multi-GB uncompressed dumps, for which
# that approach is impractically slow or does not fit in memory. Above
# _STREAM_THRESHOLD_BYTES (decompressed), skip rdflib/DOM parsing entirely
# and stream-extract the same metadata fields with bounded memory instead.

_STREAM_THRESHOLD_BYTES = 50 * 1024 * 1024

# Decompression temp files must land on real disk, not RAM. tempfile's default
# directory follows $TMPDIR/TMP/TEMP (falling back to /tmp), which on many
# Linux setups is a tmpfs — writing a decompressed multi-hundred-MB or
# multi-GB ontology there consumes RAM, not disk space. Temp files are instead
# created in a scratch directory next to the object store
# (CorpusPaths.scratch_dir), which is on the same disk as ONTOMETER_STORE.
# Paths are configured once per process by configure_paths(). Workers are
# separate processes, so each one reconfigures itself from the value carried in
# its task rather than relying on inherited module state — that inheritance
# holds under fork and silently does not under spawn.
_PATHS: CorpusPaths | None = None
_SCRATCH_DIR_CACHE: str | None = None
_SCRATCH_DIR_RESOLVED = False


def configure_paths(paths: CorpusPaths | None) -> None:
    """Point this module's path helpers at *paths* for the current process."""
    global _PATHS, _SCRATCH_DIR_CACHE, _SCRATCH_DIR_RESOLVED
    _PATHS = paths
    _SCRATCH_DIR_CACHE = None
    _SCRATCH_DIR_RESOLVED = False


def _current_paths() -> CorpusPaths:
    """Configured paths, falling back to the environment."""
    global _PATHS
    if _PATHS is None:
        _PATHS = CorpusPaths.resolve()
    return _PATHS


def scratch_dir() -> str | None:
    """
    Return a disk-backed directory for decompression temp files, or None to
    fall back to tempfile's platform default (only relevant when no object
    store is configured, e.g. when parse.py is used outside the normal
    pipeline).
    """
    global _SCRATCH_DIR_CACHE, _SCRATCH_DIR_RESOLVED
    if _SCRATCH_DIR_RESOLVED:
        return _SCRATCH_DIR_CACHE

    try:
        scratch = _current_paths().scratch_dir
        scratch.mkdir(parents=True, exist_ok=True)
        _SCRATCH_DIR_CACHE = str(scratch)
    except (MissingStore, OSError):
        _SCRATCH_DIR_CACHE = None

    _SCRATCH_DIR_RESOLVED = True
    return _SCRATCH_DIR_CACHE


_OWL_NS_URI = "http://www.w3.org/2002/07/owl#"
_RDFS_NS_URI = "http://www.w3.org/2000/01/rdf-schema#"
_RDF_NS_TAG = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"


_ONTOLOGY_ARCHIVE_SUFFIXES = (
    ".owl",
    ".rdf",
    ".ttl",
    ".obo",
    ".nt",
    ".nq",
    ".n3",
    ".xml",
    ".owx",
    ".ofn",
    ".jsonld",
)

# ZIP-based office documents contain many XML files.  rdflib will interpret
# their package metadata as generic RDF/XML even though it is not an ontology,
# so these members must never be candidates merely because they end in .xml.
_ZIP_PACKAGE_PREFIXES = ("_rels/", "docprops/", "xl/", "word/", "ppt/")


def _pick_zip_member(names: list[str]) -> str | None:
    """Select an ontology-like file from a ZIP, never container metadata."""
    normalised_names = [name.replace("\\", "/").lstrip("./") for name in names]
    office_package = "[content_types].xml" in {name.lower() for name in normalised_names} and any(
        name.lower().startswith(("xl/", "word/", "ppt/")) for name in normalised_names
    )
    candidates = []
    for name, normalised in zip(names, normalised_names):
        lower = normalised.lower()
        if not normalised or normalised.endswith("/"):
            continue
        if lower == "[content_types].xml" or lower.startswith(_ZIP_PACKAGE_PREFIXES):
            continue
        if office_package and lower.endswith(".xml"):
            continue
        if lower.endswith(_ONTOLOGY_ARCHIVE_SUFFIXES):
            candidates.append(name)
    return candidates[0] if candidates else None


def _gzip_real_size(path: Path) -> int | None:
    """
    Read gzip's trailing ISIZE field (uncompressed size mod 2**32) instead of
    decompressing — O(1) rather than O(decompressed size).

    Two known failure modes, both handled by falling back to a conservative
    compression-ratio estimate rather than trusting a bogus small value:
      - multi-member (concatenated) gzip streams, common from pigz/bgzip bulk
        exports — the trailer only reflects the size of the *last* member,
        which can be tiny even when the full stream is many GB;
      - files whose true uncompressed size exceeds 4GB, where the field wraps.
    A decompressed size smaller than the on-disk compressed size is a strong
    signal one of these happened, since real content essentially never
    compresses at a negative ratio, so an implausible ISIZE falls back to 4x
    the on-disk size.
    """
    try:
        on_disk = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            isize = int.from_bytes(f.read(4), "little")
    except OSError:
        return None

    if isize < on_disk:
        return on_disk * 4  # conservative lower bound on the compression ratio

    return isize


def estimate_real_size(filepath: Path) -> tuple[int, str, str | None]:
    """
    Cheaply estimate a file's *decompressed* size without fully decompressing
    it, so oversized archives route to the streaming parser and run_parse's
    scheduling sort reflects actual parse cost rather than on-disk size.
    Returns (estimated_bytes, kind, zip_member_name_or_None).
    """
    try:
        with open(filepath, "rb") as f:
            sig = f.read(4)
    except OSError:
        return 0, "plain", None

    if sig.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(filepath, "r") as z:
                member = _pick_zip_member(z.namelist())
                if member is None:
                    return 0, "zip", None
                return z.getinfo(member).file_size, "zip", member
        except zipfile.BadZipFile:
            return filepath.stat().st_size, "plain", None
    elif sig.startswith(b"\x1f\x8b"):
        size = _gzip_real_size(filepath)
        return (size if size is not None else filepath.stat().st_size), "gzip", None
    elif sig.startswith(b"BZh"):
        # No cheap trailer for bzip2 — on-disk size underestimates the real
        # size, but it's the best O(1) signal available.
        return filepath.stat().st_size, "bzip2", None

    try:
        return filepath.stat().st_size, "plain", None
    except OSError:
        return 0, "plain", None


def open_content_stream(filepath: Path, kind: str, zip_member: str | None):
    """
    Open a fresh, from-byte-zero binary stream over the actual (decompressed)
    content. Returns (stream, extra_handle_to_close_or_None) — the zip case
    needs the ZipFile itself kept open and closed alongside its member stream.
    """
    if kind == "zip":
        if zip_member is None:
            raise ValueError("ZIP content requires a selected member")
        z = zipfile.ZipFile(filepath, "r")
        return z.open(zip_member), z
    elif kind == "gzip":
        return gzip.open(filepath, "rb"), None
    elif kind == "bzip2":
        return bz2.open(filepath, "rb"), None
    return open(filepath, "rb"), None


_STREAM_BASE_IRI = "http://ontometer.invalid/base#"

# Formats to try, in order, for each sniffed kind. TriG reads Turtle and
# N-Triples as well as named-graph blocks; N-Quads lines carry a fourth (graph)
# term TriG rejects. Graph names are dropped either way.
STREAM_FORMATS = {
    "xml": (RdfFormat.RDF_XML,),
    "turtle_like": (RdfFormat.TRIG, RdfFormat.N_QUADS),
}

_XML_ENCODING_RE = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


class _ChunkReader(io.RawIOBase):
    """A readable binary stream over an iterator of byte chunks."""

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self._pending:
            self._pending = next(self._chunks, b"")
            if not self._pending:
                return 0
        n = min(len(buffer), len(self._pending))
        buffer[:n] = self._pending[:n]
        self._pending = self._pending[n:]
        return n


def _utf8_xml_chunks(byte_stream, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """
    Yield an XML document as UTF-8, transcoding it (declaration included) when
    its XML declaration names another encoding — the RDF/XML parser reads
    UTF-8 only. Raises LookupError for an encoding Python does not know.
    """
    head = byte_stream.read(1024)
    match = _XML_ENCODING_RE.match(head)
    encoding = codecs.lookup(match.group(1).decode("ascii")).name if match else "utf-8"
    if match is None or encoding == "utf-8":
        yield head
        yield from iter(lambda: byte_stream.read(chunk_size), b"")
        return
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    yield head[: match.start(1)] + b"UTF-8" + decoder.decode(head[match.end(1) :]).encode()
    for chunk in iter(lambda: byte_stream.read(chunk_size), b""):
        yield decoder.decode(chunk).encode()
    yield decoder.decode(b"", final=True).encode()


def ntriples_lines(byte_stream, rdf_format: RdfFormat) -> Iterator[str]:
    """
    Stream `byte_stream` as canonical N-Triples lines, one triple at a time,
    so memory stays bounded at any file size. Raises SyntaxError where the
    input stops being valid `rdf_format`. Parsing is lenient about malformed
    IRIs and language tags, which published files carry and the in-memory
    rdflib path also accepts.
    """
    if rdf_format == RdfFormat.RDF_XML:
        byte_stream = io.BufferedReader(_ChunkReader(_utf8_xml_chunks(byte_stream)))
    for quad in pyoxigraph.parse(
        input=byte_stream, format=rdf_format, base_iri=_STREAM_BASE_IRI, lenient=True
    ):
        yield f"{quad.subject} {quad.predicate} {quad.object} .\n"


_NT_PREFIX_RE = re.compile(r"^@prefix\s+([A-Za-z0-9_\-]*):\s*<([^>]*)>\s*\.")
_NT_TYPE_RE = re.compile(
    r"^<([^>]+)>\s+(?:a|<http://www\.w3\.org/1999/02/22-rdf-syntax-ns#type>)\s+"
    r"(?:<([^>]+)>|([A-Za-z0-9_\-]*):([A-Za-z0-9_\-]+))\s*[;,.]"
)
_NT_VERSION_IRI_RE = re.compile(
    r"^<([^>]+)>\s+(?:owl:versionIRI|<http://www\.w3\.org/2002/07/owl#versionIRI>)\s+<([^>]+)>"
)
_NT_VERSION_INFO_RE = re.compile(
    r'^<([^>]+)>\s+(?:owl:versionInfo|<http://www\.w3\.org/2002/07/owl#versionInfo>)\s+"((?:[^"\\]|\\.)*)"'
)
# Licence capture over canonical N-Triples: subjects stay in their N-Triples
# spelling (<iri> or _:label) so header and licence lines compare directly.
_NT_HEADER_RE = re.compile(
    r"^(<[^>]+>|_:\S+)\s+<http://www\.w3\.org/1999/02/22-rdf-syntax-ns#type>\s+"
    r"<(?:http://www\.w3\.org/2002/07/owl#Ontology|http://www\.w3\.org/2004/02/skos/core#ConceptScheme)>"
)
_NT_LICENCE_RE = re.compile(r'^(<[^>]+>|_:\S+)\s+<([^>]+)>\s+(?:<([^>]*)>|"((?:[^"\\]|\\.)*)")')
_NT_ESCAPE_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)")
_NT_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}
#: Licence statements held for subjects not yet known to be headers. Bounded,
#: because a dump may give every item its own licence.
_MAX_PENDING_LICENCES = 1000


def _unescape_nt(text: str) -> str:
    """Undo N-Triples string escapes."""

    def _one(m: re.Match) -> str:
        code = m.group(1)
        if len(code) > 1:
            return chr(int(code[1:], 16))
        return _NT_ESCAPES.get(code, code)

    return _NT_ESCAPE_RE.sub(_one, text)


_NT_IMPORTS_RE = re.compile(
    r"^<([^>]+)>\s+(?:owl:imports|<http://www\.w3\.org/2002/07/owl#imports>)\s+<([^>]+)>"
)
_NT_SUBCLASS_RE = re.compile(
    r"^<([^>]+)>\s+(?:rdfs:subClassOf|<http://www\.w3\.org/2000/01/rdf-schema#subClassOf>)\s+<([^>]+)>"
)


def _stream_parse_ntriples_like(text_stream, filestem: str) -> dict | None:
    """
    Bounded-memory metadata extraction from one-statement-per-line RDF with
    fully-spelled-out IRIs, such as the canonical N-Triples ntriples_lines
    emits. Reads line by line, so a multi-GB dump costs O(1) memory rather
    than rdflib's full in-memory triple store.

    Does not implement general Turtle grammar: statements that don't fit the
    one-line-per-triple pattern are skipped, so feed it N-Triples.
    """
    owl_prefixes = {"owl"}
    rdfs_prefixes = {"rdfs"}

    ontology_iri: str | None = None
    version_iri: str | None = None
    version_info: str | None = None
    imports: set[str] = set()
    classes: set[str] = set()
    object_props: set[str] = set()
    datatype_props: set[str] = set()
    annotation_props: set[str] = set()
    individuals: set[str] = set()
    typed: set[str] = set()
    skos_concepts: set[str] = set()
    prefixes: dict[str, str] = {}
    subclass_edges: list[tuple[str, str]] = []
    referenced_ns: set[str] = set()
    triple_count = 0
    header_subjects: set[str] = set()
    header_licences: list[str] = []
    pending_licences: list[tuple[str, str]] = []

    def _add_ns(iri: str | None) -> None:
        n = _iri_namespace(iri)
        if n:
            referenced_ns.add(n)

    for raw_line in text_stream:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        m = _NT_PREFIX_RE.match(line)
        if m:
            prefix, ns = m.group(1), m.group(2).rstrip("#/")
            prefixes[prefix] = m.group(2)
            _add_ns(m.group(2))
            if ns == _OWL_NS_URI.rstrip("#/"):
                owl_prefixes.add(prefix)
            elif ns == _RDFS_NS_URI.rstrip("#/"):
                rdfs_prefixes.add(prefix)
            continue

        if line.endswith("."):
            triple_count += 1

        if "#Ontology>" in line or "#ConceptScheme>" in line:
            m = _NT_HEADER_RE.match(line)
            if m:
                header_subjects.add(m.group(1))
        if "license" in line or "rights" in line:
            m = _NT_LICENCE_RE.match(line)
            if m and m.group(2) in LICENCE_PREDICATES:
                subject = m.group(1)
                value = m.group(3) if m.group(3) is not None else _unescape_nt(m.group(4))
                if subject in header_subjects:
                    header_licences.append(value)
                elif len(pending_licences) < _MAX_PENDING_LICENCES:
                    pending_licences.append((subject, value))

        m = _NT_TYPE_RE.match(line)
        if m:
            subject, full_type, type_prefix, type_local = m.groups()
            kind: str | None = None
            is_onto = False
            if full_type:
                if full_type in (f"{_OWL_NS_URI}Class", f"{_RDFS_NS_URI}Class"):
                    kind = "class"
                elif full_type == f"{_OWL_NS_URI}ObjectProperty":
                    kind = "object_property"
                elif full_type == f"{_OWL_NS_URI}DatatypeProperty":
                    kind = "datatype_property"
                elif full_type == f"{_OWL_NS_URI}AnnotationProperty":
                    kind = "annotation_property"
                elif full_type == f"{_OWL_NS_URI}NamedIndividual":
                    kind = "individual"
                elif full_type == f"{_OWL_NS_URI}Ontology":
                    is_onto = True
            elif type_prefix in owl_prefixes:
                if type_local == "Class":
                    kind = "class"
                elif type_local == "ObjectProperty":
                    kind = "object_property"
                elif type_local == "DatatypeProperty":
                    kind = "datatype_property"
                elif type_local == "AnnotationProperty":
                    kind = "annotation_property"
                elif type_local == "NamedIndividual":
                    kind = "individual"
                elif type_local == "Ontology":
                    is_onto = True
            elif type_prefix in rdfs_prefixes and type_local == "Class":
                kind = "class"

            if kind == "class":
                classes.add(subject)
                _add_ns(subject)
            elif kind == "object_property":
                object_props.add(subject)
                _add_ns(subject)
            elif kind == "datatype_property":
                datatype_props.add(subject)
                _add_ns(subject)
            elif kind == "annotation_property":
                annotation_props.add(subject)
                _add_ns(subject)
            elif kind == "individual":
                individuals.add(subject)
                _add_ns(subject)
            elif is_onto:
                ontology_iri = subject
                _add_ns(subject)
            else:
                type_iri = full_type or (
                    prefixes[type_prefix] + type_local if type_prefix in prefixes else None
                )
                if type_iri and _is_instance_type(type_iri):
                    typed.add(subject)
                    if type_iri == _SKOS_CONCEPT_IRI:
                        skos_concepts.add(subject)
            continue

        m = _NT_SUBCLASS_RE.match(line)
        if m:
            child, parent = m.group(1), m.group(2)
            subclass_edges.append((child, parent))
            _add_ns(child)
            _add_ns(parent)
            continue

        m = _NT_VERSION_IRI_RE.match(line)
        if m:
            version_iri = m.group(2)
            continue

        m = _NT_VERSION_INFO_RE.match(line)
        if m:
            version_info = m.group(2)
            continue

        m = _NT_IMPORTS_RE.match(line)
        if m:
            imports.add(m.group(2))

    # The stream was fully iterated above — a large dump with no matched
    # owl:Class/Property lines (e.g. a pure SKOS/instance-data dump with no
    # formal OWL schema) is a legitimate result, not a sign the format went
    # unrecognised, so it must NOT trigger a fall-back to the classic
    # in-memory path, which would materialise a multi-GB file in memory.
    # An undeclared IRI stays None for parse_worker's registry fallback
    # (_settle_ontology_iri); the file stem is only the object hash.

    properties = object_props | datatype_props | annotation_props
    all_iris = classes.union(properties)
    canonical = "\n".join(sorted(all_iris))
    content_hash = text_hash(canonical)

    entities = (
        [(c, "class") for c in classes]
        + [(p, "object_property") for p in object_props]
        + [(p, "datatype_property") for p in datatype_props]
        + [(p, "annotation_property") for p in annotation_props]
        + [(i, "individual") for i in individuals]
    )
    own_ns = _namespaces_of(classes | properties | individuals)
    oiri_ns = _iri_namespace(ontology_iri)
    if oiri_ns:
        own_ns.add(oiri_ns)
    own_ns -= _UNOWNABLE_NAMESPACES
    referenced_ns |= own_ns

    licence = _licence_values(
        header_licences + [v for subject, v in pending_licences if subject in header_subjects]
    )

    return {
        "ontology_iri": ontology_iri,
        "ontology_version_iri": version_iri,
        "ontology_version_info": version_info,
        "licence": licence,
        "triple_count": triple_count,
        "class_count": len(classes),
        "property_count": len(properties),
        **_individual_counts(
            individuals,
            typed,
            skos_concepts,
            classes | properties,
            {ontology_iri} if ontology_iri else set(),
        ),
        "imports": sorted(imports),
        "content_hash": content_hash,
        "referenced_namespaces": sorted(referenced_ns),
        "own_namespaces": sorted(own_ns),
        "subclass_edges": subclass_edges,
        "entities": _dedup_entities(entities),
        "skos_concepts": sorted(skos_concepts),
    }


def _parse_worker_streaming(
    row_id: int, filepath: Path, kind: str, zip_member: str | None
) -> tuple[int, str, str | None, dict | None, str | None] | None:
    """
    Bounded-memory path for files whose decompressed size exceeds
    _STREAM_THRESHOLD_BYTES: sniff the format from a small head peek, stream
    the content through pyoxigraph as N-Triples lines (ntriples_lines) and
    extract exact metadata with _stream_parse_ntriples_like. A Turtle-like
    file that TriG rejects is retried as N-Quads on the same bytes.

    Returns None (not a failure tuple) if the format is neither RDF/XML nor
    Turtle-like, so the caller falls back to the classic path.
    """
    try:
        peek_stream, peek_handle = open_content_stream(filepath, kind, zip_member)
        try:
            head = peek_stream.read(4096)
        finally:
            peek_stream.close()
            if peek_handle is not None:
                peek_handle.close()
    except Exception as exc:
        return row_id, "fail", None, None, f"Error peeking large-file stream: {exc}"

    if looks_like_feed(head):
        return row_id, "feed", None, None, "Detected XML RSS/Atom feed instead of ontology"

    head_stripped = head.lstrip()
    if head_stripped.startswith((b"@prefix", b"PREFIX", b"<http://", b"<https://")):
        fmt_guess = "turtle_like"
    elif head_stripped.startswith(b"<?xml") or head_stripped.startswith(b"<"):
        fmt_guess = "xml"
    else:
        return None  # unrecognised — let the classic path have a go

    formats = STREAM_FORMATS[fmt_guess]
    last_error = ""
    for rdf_format in formats:
        try:
            stream, handle = open_content_stream(filepath, kind, zip_member)
        except Exception as exc:
            return row_id, "fail", None, None, f"Error opening large-file stream: {exc}"
        try:
            meta = _stream_parse_ntriples_like(ntriples_lines(stream, rdf_format), filepath.stem)
        except SyntaxError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            return row_id, "fail", None, None, f"Streaming parse error: {exc}"
        finally:
            stream.close()
            if handle is not None:
                handle.close()

        if meta is None or not meta.get("triple_count"):
            # Well-formed but empty, e.g. XML whose root is a bare <rdf:RDF/>.
            return row_id, "unsupported", None, None, "0 triples parsed -- not valid RDF"
        fmt = "stream_xml_cvt" if fmt_guess == "xml" else "stream_nt_cvt"
        return row_id, "ok", fmt, meta, None

    tried = ", ".join(f.name for f in formats)
    return row_id, "fail", None, None, f"Large-file parse failed (tried: {tried}): {last_error}"


# ---------------------------------------------------------------------------
# Per-ontology structural sidecars
# ---------------------------------------------------------------------------
#
# subclass_edges and entities can be huge (millions of rows for large
# ontologies), so they are NOT carried in the scalar meta dict that crosses the
# process-pool boundary and lands in corpus_registry. Instead each worker
# writes them to a content-addressed parquet sidecar, mirroring the object
# store's <hh>/<hash> sharding and keyed by the raw-byte file_hash. Exact file
# copies share a sidecar, while files with the same coarse class/property set
# keep their distinct structures. The network
# layer later reads these lazily for hierarchy/entity metrics — see
# `ontometer_corpus/network/README.md`.


def _structure_dir() -> Path | None:
    """Root of the structural sidecar store, or None if no object store is set."""
    try:
        return _current_paths().structure_dir
    except MissingStore:
        return None


def _sidecar_path(file_hash: str) -> Path | None:
    """The sidecar for a file_hash (``CorpusPaths.sidecar_path``), or None without a store."""
    if _structure_dir() is None or not file_hash:
        return None
    return _current_paths().sidecar_path(file_hash)


def _write_sidecar(
    file_hash: str,
    subclass_edges: list[tuple[str, str]],
    entities: list[tuple[str, str]],
    skos_concepts: list[str],
) -> None:
    """
    Write a structural sidecar keyed by the complete raw-byte file_hash.
    Unified long schema so the two record kinds share one file:
      record_type='subclass_edge' → (subject=child_iri,  object=parent_iri)
      record_type='entity'        → (subject=entity_iri, object=kind)
      record_type='skos_concept'  → (subject=concept_iri, object='skos:Concept')
    """
    path = _sidecar_path(file_hash)
    if path is None:
        return

    import pyarrow as pa
    import pyarrow.parquet as pq

    if path.exists():
        if not skos_concepts:
            return
        previous = pq.read_table(path)
        if "skos_concept" in previous["record_type"].to_pylist():
            return
        old = previous.to_pydict()
        table = pa.table(
            {
                "record_type": old["record_type"] + ["skos_concept"] * len(skos_concepts),
                "subject": old["subject"] + skos_concepts,
                "object": old["object"] + ["skos:Concept"] * len(skos_concepts),
            }
        )
        with atomic_open(path) as f:
            pq.write_table(table, f)
        return

    record_type: list[str] = []
    subject: list[str] = []
    obj: list[str] = []
    for child, parent in subclass_edges:
        record_type.append("subclass_edge")
        subject.append(child)
        obj.append(parent)
    for iri, kind in entities:
        record_type.append("entity")
        subject.append(iri)
        obj.append(kind)
    for iri in skos_concepts:
        record_type.append("skos_concept")
        subject.append(iri)
        obj.append("skos:Concept")

    table = pa.table({"record_type": record_type, "subject": subject, "object": obj})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publish via a unique temp name, so a concurrent worker writing
    # the same file_hash can never expose a half-written parquet.
    with atomic_open(path) as f:
        pq.write_table(table, f)


def _settle_ontology_iri(meta: dict, source_id: str | None) -> None:
    """Give every parser path the IRI provenance extract_metadata gives rdflib.

    The custom and streaming parsers report only an IRI the file declares;
    an undeclared one is recovered from the registry here, as extract_metadata
    does, and `iri_source` records which it was.
    """
    if (meta.get("ontology_iri") or "").startswith("file:"):
        # A local path (the author's, or a parser's scratch copy) is not an
        # identity; see _detect_ontology_iri.
        meta["ontology_iri"] = None
        meta.pop("iri_source", None)
    if "iri_source" in meta:
        return
    if meta.get("ontology_iri"):
        meta["iri_source"] = "declared"
        return
    meta["ontology_iri"] = _registry_declared_iri(source_id)
    meta["iri_source"] = "registry" if meta["ontology_iri"] else None


def _finalize_result(
    result: tuple[int, str, str | None, dict | None, str | None],
    source_id: str | None = None,
    file_hash: str | None = None,
) -> tuple[int, str, str | None, dict | None, str | None]:
    """
    Divert the transient structural keys (subclass_edges, entities) out of an
    'ok' result into the sidecar store before the (now scalar-only) meta is
    returned across the process pool, and settle the ontology IRI's
    provenance. No-op for non-ok results.
    """
    row_id, status, fmt, meta, error = result
    if status == "ok" and isinstance(meta, dict):
        _settle_ontology_iri(meta, source_id)
        subclass_edges = meta.pop("subclass_edges", None) or []
        entities = meta.pop("entities", None) or []
        skos_concepts = meta.pop("skos_concepts", None) or []
        if file_hash:
            try:
                _write_sidecar(file_hash, subclass_edges, entities, skos_concepts)
            except Exception as exc:
                # Sidecars are derived, rebuildable data — never fail an
                # otherwise-successful parse just because one couldn't be
                # written. But DO say so: a swallowed failure silently drops
                # that ontology from the structural metrics (depth, branching,
                # depth histogram), which read the sidecar store and cannot
                # tell "no hierarchy" apart from "sidecar never written".
                # Large ontologies are the likeliest to hit this, and the gap
                # is invisible unless the store is diffed against corpus.db.
                print(
                    f"  [warn] sidecar write failed for row {row_id} "
                    f"(file_hash={file_hash[:12]}…, "
                    f"{len(subclass_edges)} edges, {len(entities)} entities): "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
    return result


def parse_worker(
    task_info: tuple[int, str, str | None, str | None, str | None, str],
) -> tuple[int, str, str | None, dict | None, str | None]:
    """
    Multiprocessing worker function to parse a single ontology file.
    Does not use/share database connections directly.

    The task is (row_id, retrieved_file, store_dir, source_id, file_hash, corpus_dir).
    It carries both roots alongside the already-resolved file path, because a
    worker is a separate process: under `spawn` it re-imports this module with
    no inherited state, so it configures its own paths from the task rather
    than assuming the parent's or the environment's. source_id is only consulted to recover an
    ontology IRI the file itself never declares (see _registry_declared_iri).
    """
    row_id, retrieved_file, store_dir, source_id, file_hash, corpus_dir = task_info
    if store_dir:
        configure_paths(CorpusPaths.resolve(corpus_dir=corpus_dir, store_dir=store_dir))
    filepath = Path(retrieved_file)

    if not filepath.exists():
        return row_id, "missing", None, None, "File not found on disk"
    if filepath.stat().st_size == 0:
        return row_id, "fail", None, None, "Empty file"

    real_size, size_kind, zip_member = estimate_real_size(filepath)
    if real_size >= _STREAM_THRESHOLD_BYTES:
        streamed = _parse_worker_streaming(row_id, filepath, size_kind, zip_member)
        if streamed is not None:
            return _finalize_result(streamed, source_id, file_hash)
        # Streaming path didn't recognise the format — fall through to the
        # classic rdflib/custom-parser path below as a last resort.

    temp_filepath = None
    try:
        # Detect archive signatures (ZIP or GZIP)
        with open(filepath, "rb") as f:
            sig = f.read(4)

        # ZIP file
        if sig.startswith(b"PK\x03\x04"):
            import zipfile

            with zipfile.ZipFile(filepath, "r") as z:
                names = z.namelist()
                chosen = _pick_zip_member(names)
                if not chosen:
                    error = (
                        "Zip file is empty"
                        if not names
                        else "Zip file contains no supported ontology member"
                    )
                    return row_id, "unsupported", None, None, error

                suffix = Path(chosen).suffix or ".owl"
                with tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False, dir=scratch_dir()
                ) as tmpf:
                    tmpf.write(z.read(chosen))
                    temp_filepath = Path(tmpf.name)
                filepath = temp_filepath

        # GZIP file (starts with \x1f\x8b)
        elif sig.startswith(b"\x1f\x8b"):
            import gzip

            suffix = filepath.suffix.lower() or ".owl"
            if suffix == ".gz":
                suffix = filepath.with_suffix("").suffix or ".owl"
            with gzip.open(filepath, "rb") as gf:
                decomp_bytes = gf.read()
            with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, dir=scratch_dir()
            ) as tmpf:
                tmpf.write(decomp_bytes)
                temp_filepath = Path(tmpf.name)
            filepath = temp_filepath

        # BZIP2 file (starts with 'BZh')
        elif sig.startswith(b"BZh"):
            import bz2

            suffix = filepath.suffix.lower() or ".owl"
            if suffix == ".bz2":
                suffix = filepath.with_suffix("").suffix or ".owl"
            try:
                decomp_bytes = bz2.decompress(filepath.read_bytes())
            except OSError as exc:
                return row_id, "fail", None, None, f"Error processing bzip2 archive: {exc}"
            with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, dir=scratch_dir()
            ) as tmpf:
                tmpf.write(decomp_bytes)
                temp_filepath = Path(tmpf.name)
            filepath = temp_filepath

    except Exception as exc:
        return row_id, "fail", None, None, f"Error processing compressed archive: {exc}"

    # Everything from here on operates on `filepath` (the decompressed temp
    # file, if one was created) — wrapped in try/finally so temp_filepath is
    # always removed, even on an exception type none of the branches below
    # anticipated, and an interrupted parse leaves no decompressed file
    # behind.
    try:
        try:
            raw_bytes = filepath.read_bytes()
        except OSError as exc:
            return row_id, "fail", None, None, f"Error reading file: {exc}"

        # Check if the file is an RSS/Atom feed or Manchester OWL Syntax before attempting RDF parsing
        head = raw_bytes[:10000]  # 10k to check past long license comment headers

        if looks_like_feed(head):
            return row_id, "feed", None, None, "Detected XML RSS/Atom feed instead of ontology"

        # Check for Manchester / Functional OWL Syntax
        head_lower = head.lower()
        is_manchester = False
        is_functional = False

        # If it has Prefix: but no @prefix (Turtle) and is not XML, it's Manchester OWL syntax
        if b"prefix:" in head_lower and b"@prefix" not in head_lower and b"xmlns" not in head_lower:
            is_manchester = True
        elif b"ontology:" in head_lower and b"prefix:" in head_lower:
            is_manchester = True
        elif b"prefix(" in head_lower or head_lower.strip().startswith(b"prefix("):
            is_functional = True

        if is_manchester or is_functional:
            fmt_desc = "Manchester OWL Syntax" if is_manchester else "Functional OWL Syntax"
            return row_id, "unsupported", None, None, f"{fmt_desc} is not natively supported"

        static_imports = _extract_imports(raw_bytes)
        meta = None
        graph = None
        fmt = None
        error = None
        status = "fail"

        # 1. Try rdflib first
        graph, fmt, error = try_rdflib(raw_bytes)
        if graph is not None:
            try:
                meta = extract_metadata(graph, source_id)
                meta["imports"] = sorted(list(set(meta["imports"]).union(static_imports)))
                status = "ok"
            except Exception as exc:
                graph = None
                error = f"Metadata extraction error: {exc}"

        # 2. Try custom parsers
        if graph is None:
            is_obo = (
                b"format-version:" in head_lower
                or b"default-namespace:" in head_lower
                or b"[term]" in head_lower
            )

            if is_obo:
                _, fmt, error, meta = _parse_obo(raw_bytes, filepath.stem)
                if meta is not None:
                    meta["imports"] = sorted(list(set(meta["imports"]).union(static_imports)))
                    status = "ok"
                    graph = True

            else:
                is_owl_xml = (
                    b"<ontology" in head_lower
                    and b"xmlns=" in head_lower
                    and b"rdf:rdf" not in head_lower
                )

                if is_owl_xml:
                    _, fmt, error, meta = _parse_owl_xml(raw_bytes, filepath.stem)
                    if meta is not None:
                        meta["imports"] = sorted(list(set(meta["imports"]).union(static_imports)))
                        status = "ok"
                        graph = True

        # 3. Fall back to owlready2
        if graph is None:
            graph, fmt, error = try_owlready2(raw_bytes, filepath.suffix)
            if graph is not None:
                try:
                    meta = extract_metadata(graph, source_id)
                    meta["imports"] = static_imports
                    status = "ok"
                except Exception as exc:
                    graph = None
                    error = f"Metadata extraction error under owlready2 fallback: {exc}"

        if status != "ok":
            return row_id, "fail", None, None, error or "Unknown parse error"

        return _finalize_result((row_id, status, fmt, meta, None), source_id, file_hash)

    except Exception as exc:
        # Safety net: an unanticipated exception anywhere above must still
        # yield a normal "fail" result rather than propagating out of the
        # worker (which, under ProcessPoolExecutor, would surface from
        # future.result() and crash the entire run_parse batch).
        return row_id, "fail", None, None, f"Unexpected parse error: {exc}"

    finally:
        if temp_filepath and temp_filepath.exists():
            try:
                os.remove(temp_filepath)
            except OSError:
                pass


def _record_result(
    conn: sqlite3.Connection,
    counts: dict,
    row_id: int,
    status: str,
    fmt: str | None,
    meta: dict | None,
    error: str | None,
) -> None:
    """Persist a single parse_worker result to corpus_registry and tally it."""
    error_text = error or "Parser returned no error detail"
    if status == "missing":
        update_parse_fail(conn, row_id, error_text)
        counts["missing"] += 1
    elif status == "feed":
        update_parse_feed(conn, row_id, error_text)
        counts["feed"] += 1
    elif status == "unsupported":
        update_parse_unsupported(conn, row_id, error_text)
        counts["unsupported"] += 1
    elif status == "fail":
        update_parse_fail(conn, row_id, error_text)
        counts["fail"] += 1
    elif status == "ok":
        if fmt is None or meta is None:
            update_parse_fail(conn, row_id, "Successful parse omitted metadata or format")
            counts["fail"] += 1
            return
        update_parse_ok(conn, row_id, parse_format=fmt, **meta)
        counts["ok"] += 1


def run_parse(
    db_path: Path,
    batch_size: int = 500,
    max_workers: int | None = None,
    parallel: bool = True,
    paths: CorpusPaths | None = None,
) -> dict:
    """
    Parse all unparsed rows in corpus_registry.

    parallel=True (default) uses a ProcessPoolExecutor with the concurrency
    and memory-safety measures below. parallel=False processes rows one at a
    time in the current process instead — useful when a file is suspected of
    causing runaway CPU/memory (e.g. a ZIP-wrapped ontology that decompresses
    to hundreds of MB), since it isolates that cost to a single, easily
    Ctrl+C-able process instead of spreading it across worker processes whose
    shutdown blocks on in-flight tasks.

    Concurrency and memory-safety measures (parallel=True only):
      - max_workers is capped (see _DEFAULT_MAX_WORKERS) instead of defaulting
        to os.cpu_count(), bounding peak concurrent RAM.
      - max_tasks_per_child=1 recycles each worker process after a single
        file, so memory that the interpreter/native extensions don't return
        to the OS (common with lxml/owlready2 on large files) can't
        accumulate across the whole run.
      - Tasks are sorted by estimated size ascending and submitted in batch_size
        chunks (each chunk fully drains before the next is submitted), so
        large ontologies are less likely to land on workers simultaneously
        and a crash only loses progress within the current chunk. The sort
        key is the estimated decompressed size (ZIP member size, gzip ISIZE);
        bzip2 has no cheap size trailer, so a bzip2 file sorts by its
        on-disk size and can still decompress to something far larger.
    """
    paths = paths or CorpusPaths.resolve()
    configure_paths(paths)
    print(f"Mode: {'concurrent' if parallel else 'sequential'}")

    conn = get_connection(db_path)
    counts = {"ok": 0, "fail": 0, "missing": 0, "feed": 0, "unsupported": 0}

    pending = conn.execute(
        "SELECT id, retrieved_file, source_id, file_hash FROM corpus_registry "
        "WHERE parse_status IS NULL ORDER BY id"
    ).fetchall()

    total = len(pending)
    print(f"Rows to parse: {total}")

    if total == 0:
        conn.close()
        return counts

    # Resolve each stored path against the object store here, in the parent,
    # so a worker receives an absolute path and never has to know the layout.
    # `retrieved_file` is stored relative to the store root precisely so a
    # corpus captured on one machine can be parsed on another.
    store = str(paths.store_dir) if paths.store_dir else None
    tasks = [
        (
            row["id"],
            str(paths.resolve_object(row["retrieved_file"])),
            store,
            row["source_id"],
            row["file_hash"],
            str(paths.corpus_dir),
        )
        for row in pending
    ]

    def _real_size(task: tuple[int, str, str | None, str | None, str | None, str]) -> int:
        # Estimated *decompressed* size, not on-disk size — a small ZIP/GZIP
        # file can unpack to something far larger than it looks, and sorting
        # by on-disk size would schedule it among the small files.
        try:
            return estimate_real_size(Path(task[1]))[0]
        except OSError:
            return 0

    tasks.sort(key=_real_size)

    if not parallel:
        for completed, task in enumerate(tasks, start=1):
            row_id, status, fmt, meta, error = parse_worker(task)
            filepath = Path(task[1])
            print(f"  [{completed}/{total}] {filepath.name} -> [{status}]", flush=True)
            _record_result(conn, counts, row_id, status, fmt, meta, error)

        conn.close()
        print("\nParse summary:")
        print(f"  ok={counts['ok']}")
        print(f"  fail={counts['fail']}")
        print(f"  feed={counts['feed']}")
        print(f"  unsupported={counts['unsupported']}")
        print(f"  missing={counts['missing']}")
        return counts

    from concurrent.futures import ProcessPoolExecutor, as_completed

    workers = max_workers or _DEFAULT_MAX_WORKERS
    completed = 0

    with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1) as executor:
        for batch_start in range(0, total, batch_size):
            batch = tasks[batch_start : batch_start + batch_size]
            futures = {executor.submit(parse_worker, task): task for task in batch}

            for future in as_completed(futures):
                row_id, status, fmt, meta, error = future.result()
                task = futures[future]
                filepath = Path(task[1])
                completed += 1

                print(f"  [{completed}/{total}] {filepath.name} -> [{status}]", flush=True)
                _record_result(conn, counts, row_id, status, fmt, meta, error)

    conn.close()

    print("\nParse summary:")
    print(f"  ok={counts['ok']}")
    print(f"  fail={counts['fail']}")
    print(f"  feed={counts['feed']}")
    print(f"  unsupported={counts['unsupported']}")
    print(f"  missing={counts['missing']}")

    return counts
