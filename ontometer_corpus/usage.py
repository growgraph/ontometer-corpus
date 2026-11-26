"""How a file uses each term it references: for modelling, or as metadata.

A namespace alone cannot say whether a vocabulary is reused. The same Dublin
Core property is metadata on an ontology header and a modelling relation in a
bibliographic knowledge base; FOAF is reused when a class specialises
``foaf:Person`` and is metadata when ``foaf:page`` points at a homepage. So
every occurrence is classified by its position in the file, with no list of
vocabularies:

* **logical**: the term takes part in the model. It is an end of a
  subclass/subproperty, equivalence or disjointness axiom, a domain or range,
  a restriction's property or filler, a member of a class expression, the type
  of a non-header subject, or a predicate (or its value) in a statement that
  is an object or data property assertion.
* **annotation**: the term describes rather than models. It is a predicate the
  file declares ``owl:AnnotationProperty``, any statement about the ontology
  header or an axiom annotation node, a value of a language annotation
  predicate (``rdfs:seeAlso``, ``rdfs:isDefinedBy``), or a predicate the file
  never declares whose value is a literal or whose subject is a declared class
  or property. OWL 2 DL allows only annotations there, and the OWL API reads
  undeclared predicates in those positions the same way.

A term used both ways counts as logical. A term that only appears in a
declaration (``foaf:Person a owl:Class``) is neither. Terms of the
representation languages themselves (:data:`ontometer_corpus.namespaces.LANGUAGE_NAMESPACES`)
are never counted: they are the language, not a vocabulary written in it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterable, Iterator

from ontometer_corpus.namespaces import LANGUAGE_NAMESPACES, iri_namespace

LOGICAL = "logical"
ANNOTATION = "annotation"
DECLARED = "declared"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL = "http://www.w3.org/2002/07/owl#"

_RDF_TYPE = _RDF + "type"
_RDF_FIRST = _RDF + "first"
_RDF_REST = _RDF + "rest"
_RDF_NIL = _RDF + "nil"

#: Predicates whose subject and object are ends of a logical axiom.
_STRUCTURAL = frozenset(
    {
        _RDFS + "subClassOf",
        _RDFS + "subPropertyOf",
        _RDFS + "domain",
        _RDFS + "range",
        _OWL + "equivalentClass",
        _OWL + "equivalentProperty",
        _OWL + "disjointWith",
        _OWL + "propertyDisjointWith",
        _OWL + "complementOf",
        _OWL + "inverseOf",
        _OWL + "someValuesFrom",
        _OWL + "allValuesFrom",
        _OWL + "hasValue",
        _OWL + "hasSelf",
        _OWL + "onClass",
        _OWL + "onProperty",
        _OWL + "onDataRange",
        _OWL + "onDatatype",
        _OWL + "sameAs",
        _OWL + "differentFrom",
        _OWL + "sourceIndividual",
        _OWL + "assertionProperty",
        _OWL + "targetIndividual",
    }
)
#: Predicates whose object is an RDF list of class/property/individual terms.
_STRUCTURAL_LISTS = frozenset(
    {
        _OWL + "unionOf",
        _OWL + "intersectionOf",
        _OWL + "oneOf",
        _OWL + "disjointUnionOf",
        _OWL + "propertyChainAxiom",
        _OWL + "hasKey",
        _OWL + "members",
        _OWL + "distinctMembers",
        _OWL + "withRestrictions",
    }
)
#: Language predicates that link a document to another, not a term to a term.
_DOCUMENT_LINKS = frozenset(
    {
        _OWL + "imports",
        _OWL + "versionIRI",
        _OWL + "priorVersion",
        _OWL + "backwardCompatibleWith",
        _OWL + "incompatibleWith",
    }
)
_OBJECT_OR_DATA_PROPERTY_TYPES = frozenset(
    _OWL + t
    for t in (
        "ObjectProperty",
        "DatatypeProperty",
        "TransitiveProperty",
        "SymmetricProperty",
        "AsymmetricProperty",
        "ReflexiveProperty",
        "IrreflexiveProperty",
        "InverseFunctionalProperty",
    )
)
_SCHEMA_TYPES = frozenset(
    {
        _OWL + "Class",
        _RDFS + "Class",
        _RDF + "Property",
        _OWL + "AnnotationProperty",
        _OWL + "FunctionalProperty",
        _RDFS + "Datatype",
        *_OBJECT_OR_DATA_PROPERTY_TYPES,
    }
)
#: Reification nodes that carry axiom or annotation annotations.
_ANNOTATION_NODE_TYPES = frozenset({_OWL + "Axiom", _OWL + "Annotation"})


def is_language_term(iri: str) -> bool:
    """Whether *iri* belongs to a representation language (RDF, OWL, ...)."""
    return iri_namespace(iri) in LANGUAGE_NAMESPACES


#: Stand-in for any literal in normalised triples (its value is never needed).
LITERAL = object()

_NT_LINE = re.compile(r"^(<[^>]*>|_:\S+)\s+<([^>]*)>\s+(.+?)\s*\.\s*$")


def _norm(term):
    """One term representation for rdflib and N-Triples input: an IRI is its
    string, a blank node is ``_:<id>``, a literal is :data:`LITERAL`."""
    kind = type(term).__name__
    if kind == "URIRef":
        return str(term)
    if kind == "BNode":
        return f"_:{term}"
    if kind == "Literal":
        return LITERAL
    return term


def _is_iri(term) -> bool:
    return isinstance(term, str) and not term.startswith("_:")


def rdflib_triples(graph) -> Iterator[tuple]:
    """Normalised (s, p, o) triples of an rdflib graph."""
    for s, p, o in graph:
        yield _norm(s), str(p), _norm(o)


def ntriples(lines: Iterable[str]) -> Iterator[tuple]:
    """Normalised (s, p, o) triples of canonical N-Triples lines (as written by
    the parse phase's pyoxigraph reader). Lines that are not one triple are skipped."""
    for line in lines:
        m = _NT_LINE.match(line)
        if m is None:
            continue
        s, p, o = m.groups()
        if s[0] == "<":
            s = s[1:-1]
        if o[0] == "<":
            o = o[1:-1]
        elif o.startswith("_:"):
            o = o.split()[0]
        else:
            o = LITERAL
        yield s, p, o


class UsageContext:
    """Declarations of one RDF source that decide how its statements read.

    Built in one pass over the triples; :meth:`assertion_mode` then classifies
    any statement whose predicate is not a language term. Terms are held in
    the normalised form of :func:`rdflib_triples` / :func:`ntriples`.
    """

    def __init__(self, graph=None) -> None:
        self.headers: set = set()
        self.annotation_nodes: set = set()
        self.annotation_properties: set = set()
        self.logical_properties: set = set()
        self.schema_entities: set = set()
        self._first: dict = {}
        self._rest: dict = {}
        if graph is not None:
            self._build(rdflib_triples(graph))

    @classmethod
    def from_triples(cls, triples: Iterable[tuple]) -> "UsageContext":
        ctx = cls()
        ctx._build(triples)
        return ctx

    def _build(self, triples: Iterable[tuple]) -> None:
        for s, p, o in triples:
            if p == _RDF_TYPE:
                if not _is_iri(o):
                    continue
                if o == _OWL + "Ontology":
                    self.headers.add(s)
                if o in _ANNOTATION_NODE_TYPES:
                    self.annotation_nodes.add(s)
                if o == _OWL + "AnnotationProperty":
                    self.annotation_properties.add(s)
                if o in _OBJECT_OR_DATA_PROPERTY_TYPES:
                    self.logical_properties.add(s)
                if o in _SCHEMA_TYPES:
                    self.schema_entities.add(s)
            elif p == _RDFS + "subClassOf":
                self.schema_entities.add(s)
            elif p == _RDF_FIRST and not _is_iri(s):
                self._first[s] = o
            elif p == _RDF_REST and not _is_iri(s):
                self._rest[s] = o

    def list_members(self, head) -> list:
        """Items of the RDF list starting at blank node *head* (cycle-safe)."""
        items, seen, node = [], set(), head
        while node in self._first and node not in seen:
            seen.add(node)
            items.append(self._first[node])
            node = self._rest.get(node)
        return items

    def assertion_mode(self, s, p, o) -> str:
        """``logical`` or ``annotation`` for a statement with a non-language predicate."""
        s, p, o = _norm(s), _norm(p), _norm(o)
        if s in self.headers or s in self.annotation_nodes or p in self.annotation_properties:
            return ANNOTATION
        if p in self.logical_properties:
            return LOGICAL
        if o is LITERAL or s in self.schema_entities:
            return ANNOTATION
        return LOGICAL


def classify_triples(triples: Callable[[], Iterable[tuple]]) -> dict[str, str]:
    """Usage mode of every non-language IRI in a source of normalised triples.

    *triples* is called twice (declarations first, then classification), so
    a file can be streamed rather than held in memory.
    """
    ctx = UsageContext.from_triples(triples())
    modes: dict[str, str] = {}

    def mark(term, mode: str) -> None:
        if not _is_iri(term) or is_language_term(term):
            return
        current = modes.get(term)
        if current == LOGICAL or (current == ANNOTATION and mode == DECLARED):
            return
        modes[term] = mode

    for s, p, o in triples():
        if p in _STRUCTURAL:
            annotation_axiom = (
                p == _RDFS + "subPropertyOf"
                and (s in ctx.annotation_properties or o in ctx.annotation_properties)
            ) or (p in (_RDFS + "domain", _RDFS + "range") and s in ctx.annotation_properties)
            mode = ANNOTATION if annotation_axiom else LOGICAL
            mark(s, mode)
            mark(o, mode)
        elif p in _STRUCTURAL_LISTS:
            mark(s, LOGICAL)
            for item in ctx.list_members(o):
                mark(item, LOGICAL)
        elif p == _RDF_TYPE:
            if not _is_iri(o) or is_language_term(o):
                mark(s, DECLARED)
            elif s in ctx.headers or s in ctx.annotation_nodes:
                mark(o, ANNOTATION)
            else:
                mark(s, LOGICAL)
                mark(o, LOGICAL)
        elif p in (_RDF_FIRST, _RDF_REST) or p in _DOCUMENT_LINKS:
            continue
        elif is_language_term(p):
            # rdfs:label, rdfs:seeAlso, owl:deprecated, oboInOwl:*: annotation
            # by definition. owl:annotatedSource/Property/Target only point an
            # axiom annotation at the axiom it annotates; they use nothing.
            if p.startswith(_OWL + "annotated"):
                continue
            mark(s, ANNOTATION)
            mark(o, ANNOTATION)
        else:
            mode = ctx.assertion_mode(s, p, o)
            mark(s, mode)
            mark(p, mode)
            mark(o, mode)
    return modes


def classify_graph(graph) -> dict[str, str]:
    """Usage mode of every non-language IRI in an rdflib graph."""
    return classify_triples(lambda: rdflib_triples(graph))


# --- OWL/XML (functional-style XML) --------------------------------------------

_OWX_ANNOTATION_AXIOMS = frozenset(
    {
        "AnnotationAssertion",
        "SubAnnotationPropertyOf",
        "AnnotationPropertyDomain",
        "AnnotationPropertyRange",
    }
)
_OWX_SKIP = frozenset({"Prefix", "Import"})


def classify_owl_xml(root, expand: Callable[[str], str | None]) -> dict[str, str]:
    """Usage mode of every non-language IRI in a parsed OWL/XML document.

    OWL/XML states the role of each element, so no inference is needed:
    annotation axioms and ``<Annotation>`` elements (on the ontology or on an
    axiom) are annotation, declarations are declarations, and every other
    axiom is logical.
    """
    modes: dict[str, str] = {}

    def mark(iri: str | None, mode: str) -> None:
        if not iri or is_language_term(iri):
            return
        current = modes.get(iri)
        if current == LOGICAL or (current == ANNOTATION and mode == DECLARED):
            return
        modes[iri] = mode

    def walk(el, mode: str) -> None:
        tag = el.tag.split("}")[-1]
        if tag in _OWX_SKIP:
            return
        if tag == "Annotation":
            mode = ANNOTATION
        for attr in ("IRI", "abbreviatedIRI"):
            value = el.attrib.get(attr)
            if value:
                mark(expand(value), mode)
        if tag == "IRI" and el.text and mode == ANNOTATION:
            mark(expand(el.text.strip()), mode)
        for child in el:
            walk(child, mode)

    for axiom in root:
        tag = axiom.tag.split("}")[-1]
        if tag == "Declaration":
            walk(axiom, DECLARED)
        elif tag in _OWX_ANNOTATION_AXIOMS or tag == "Annotation":
            walk(axiom, ANNOTATION)
        else:
            walk(axiom, LOGICAL)
    return modes


def namespace_counts(modes: dict[str, str], extra: Iterable[tuple[str, str]] = ()) -> dict:
    """Distinct terms per namespace and usage mode.

    Returns ``{namespace: {"logical": n, "annotation": n, "declared": n}}``.
    *extra* adds (iri, mode) pairs from another source (OBO flat files).
    """
    merged = dict(modes)
    for iri, mode in extra:
        current = merged.get(iri)
        if current == LOGICAL or (current == ANNOTATION and mode == DECLARED):
            continue
        merged[iri] = mode
    out: dict[str, dict[str, int]] = defaultdict(lambda: {LOGICAL: 0, ANNOTATION: 0, DECLARED: 0})
    for iri, mode in merged.items():
        ns = iri_namespace(iri)
        if ns:
            out[ns][mode] += 1
    return dict(out)
