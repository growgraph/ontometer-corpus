"""Term roles: whether each IRI is used as a class or as an instance in a file.

The TBox/ABox split is binary inside one well-formed OWL file, but across a
corpus the same IRI is a class here, an individual there, and sometimes both
in one file (OWL 2 punning). Recording, per file, which *roles* each term plays
lets class-likeness be measured as a share of documents rather than asserted.

Roles, with the sense each supports:

=================  =========  ===============================================
role               sense      evidence in the file
=================  =========  ===============================================
``decl_class``     class      typed ``owl:Class`` or ``rdfs:Class``
``sub_child``      class      subject of a named ``rdfs:subClassOf``
``sub_parent``     class      object of a named ``rdfs:subClassOf``
``has_instance``   class      object of ``rdf:type`` (something is typed by it)
``axiom_ref``      class      domain/range, restriction filler, equivalence,
                              disjointness or complement operand
``decl_individual`` instance  typed ``owl:NamedIndividual``
``typed``          instance   subject of ``rdf:type`` with a non-meta type
                              or ``owl:Thing``
``skos_concept``   marker     directly typed ``skos:Concept``; retained so
                              namespace ownership uses concept-local evidence
``fact_subj``      instance   subject of a fact (see below)
``fact_obj``       instance   object of a fact
=================  =========  ===============================================

A *fact* is a triple between two IRIs whose predicate is not a language term
(RDF, RDFS, OWL, ...) and which :class:`ontometer_corpus.usage.UsageContext`
reads as a property assertion rather than an annotation: not about the
ontology header or an axiom annotation, not through a property the file
declares ``owl:AnnotationProperty``, and, for a predicate the file never
declares, not about a declared class or property (OWL 2 DL allows only
annotations there). No vocabulary is excluded by name: ``dcterms:creator`` on
the header is metadata, between two individuals it is a fact. Without these
exclusions, metadata would make ordinary classes look like instances.
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("build", "rdflib")


import re
from collections import defaultdict
from typing import TYPE_CHECKING

import rdflib
from rdflib import OWL, RDF, RDFS, SKOS, URIRef
from rdflib.term import Literal

from ontometer_corpus.namespaces import LANGUAGE_NAMESPACES
from ontometer_corpus.role_names import SKOS_CONCEPT_ROLE
from ontometer_corpus.usage import LOGICAL, UsageContext

if TYPE_CHECKING:
    import pandas as pd

CLASS_ROLES: frozenset[str] = frozenset(
    {"decl_class", "sub_child", "sub_parent", "has_instance", "axiom_ref"}
)
INSTANCE_ROLES: frozenset[str] = frozenset({"decl_individual", "typed", "fact_subj", "fact_obj"})

#: Namespaces whose terms describe the modelling language or the editing
#: tools, not the domain: :data:`ontometer_corpus.namespaces.LANGUAGE_NAMESPACES`.
#: ``oboInOwl:ObsoleteClass`` is the telling case: ontologies file deprecated
#: classes under it, so as a "superclass" it would rank among the most
#: specialised terms in the corpus while meaning nothing about it. SWRL rule
#: variables are typed individuals of the rule language.
META_NAMESPACES: tuple[str, ...] = tuple(sorted(LANGUAGE_NAMESPACES))

_AXIOM_PREDICATES = frozenset(
    {
        RDFS.domain,
        RDFS.range,
        OWL.someValuesFrom,
        OWL.allValuesFrom,
        OWL.onClass,
        OWL.equivalentClass,
        OWL.disjointWith,
        OWL.complementOf,
    }
)
_CLASS_TYPES = frozenset({OWL.Class, RDFS.Class})
_LABEL_PREDICATES = (RDFS.label, SKOS.prefLabel)


def is_meta(iri: str) -> bool:
    """Whether *iri* lies in one of :data:`META_NAMESPACES` (modelling language or tooling).

    Such terms are never recorded as domain terms and never count as ends of
    a specialisation.
    """
    return iri.startswith(META_NAMESPACES)


def _pick_label(candidates: list[Literal]) -> str | None:
    """English or untagged first, then anything; deterministic among equals."""
    ranked = sorted(
        candidates,
        key=lambda lit: (
            0 if (lit.language or "").lower().startswith("en") else 1 if not lit.language else 2,
            str(lit),
        ),
    )
    for lit in ranked:
        text = str(lit).strip()
        if text:
            return text
    return None


def roles_from_graph(graph: rdflib.Graph) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Roles and preferred labels of every domain IRI in one parsed file.

    Returns:
        ``(roles, labels)``: ``roles[iri]`` is the set of role names the IRI
        plays; ``labels[iri]`` its preferred ``rdfs:label``/``skos:prefLabel``.
    """
    roles: dict[str, set[str]] = defaultdict(set)
    label_candidates: dict[str, list[Literal]] = defaultdict(list)

    usage = UsageContext(graph)
    headers = {s for s in graph.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef)}

    def add(term, role: str) -> None:
        if isinstance(term, URIRef):
            iri = str(term)
            if not is_meta(iri):
                roles[iri].add(role)

    for s, p, o in graph:
        if p == RDF.type:
            if not isinstance(o, URIRef):
                continue
            if o in _CLASS_TYPES:
                add(s, "decl_class")
            elif o == OWL.NamedIndividual:
                add(s, "decl_individual")
            elif o == OWL.Thing:
                # Older OWL (early Protégé) types individuals directly as
                # owl:Thing; like the corpus individual_count, that makes the
                # subject an instance. owl:Thing itself is no domain term.
                if s not in headers:
                    add(s, "typed")
            elif not is_meta(str(o)):
                add(o, "has_instance")
                if s not in headers:
                    add(s, "typed")
                    if o == SKOS.Concept:
                        add(s, SKOS_CONCEPT_ROLE)
        elif p == RDFS.subClassOf:
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                add(s, "sub_child")
                add(o, "sub_parent")
        elif p in _AXIOM_PREDICATES:
            add(o, "axiom_ref")
            if p in (OWL.equivalentClass, OWL.disjointWith):
                add(s, "axiom_ref")
        elif p in _LABEL_PREDICATES:
            if isinstance(o, Literal) and isinstance(s, URIRef):
                label_candidates[str(s)].append(o)
        elif (
            isinstance(s, URIRef)
            and isinstance(o, URIRef)
            and not is_meta(str(p))
            and usage.assertion_mode(s, p, o) == LOGICAL
        ):
            add(s, "fact_subj")
            add(o, "fact_obj")

    labels: dict[str, str] = {}
    for iri, cands in label_candidates.items():
        if is_meta(iri):
            continue
        label = _pick_label(cands)
        if label:
            labels[iri] = label
    return dict(roles), labels


# --- OBO flat files ------------------------------------------------------------

_OBO_ID_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_]+$")
_OBO_AXIOM_TAGS = ("intersection_of", "union_of", "disjoint_from", "equivalent_to")
_OBO_BASE = "http://purl.obolibrary.org/obo/"


def obo_to_iri(obo_id: str, ontology_name: str | None) -> str:
    """The OBO-id to IRI expansion the corpus parser uses."""
    if obo_id.startswith(("http://", "https://")):
        return obo_id
    if ":" in obo_id:
        prefix, local = obo_id.split(":", 1)
        return f"{_OBO_BASE}{prefix}_{local}"
    return f"{_OBO_BASE}{ontology_name or 'obo'}_{obo_id}"


def roles_from_obo(
    text: str, fallback_name: str | None = None
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Roles and ``name:`` labels from an OBO flat file.

    ``[Term]`` stanzas are classes; ``is_a``, ``relationship`` and the
    class-expression tags reference classes; ``[Instance]`` stanzas are
    individuals, typed by ``instance_of`` and linked by ``property_value``.
    ``[Typedef]`` stanzas declare properties and play no role here.
    """
    ontology_name: str | None = None
    stanza: str | None = None
    current: str | None = None
    raw_roles: list[tuple[str, str]] = []
    raw_labels: dict[str, str] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        if line.startswith("[") and line.endswith("]"):
            stanza = line[1:-1].strip().lower()
            current = None
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.split(" ! ")[0].strip()
        if stanza is None:
            if key == "ontology":
                ontology_name = val
            continue
        if stanza not in ("term", "instance"):
            continue
        if key == "id":
            current = val
            raw_roles.append((val, "decl_class" if stanza == "term" else "decl_individual"))
        elif current is None:
            continue
        elif key == "name":
            raw_labels.setdefault(current, val)
        elif stanza == "term" and key == "is_a":
            target = val.split()[0]
            raw_roles += [(current, "sub_child"), (target, "sub_parent")]
        elif stanza == "term" and key == "relationship":
            parts = val.split()
            if len(parts) >= 2 and _OBO_ID_TOKEN.match(parts[1]):
                raw_roles.append((parts[1], "axiom_ref"))
        elif stanza == "term" and key in _OBO_AXIOM_TAGS:
            for tok in val.split():
                if _OBO_ID_TOKEN.match(tok):
                    raw_roles.append((tok, "axiom_ref"))
        elif stanza == "instance" and key == "instance_of":
            target = val.split()[0]
            raw_roles += [(current, "typed"), (target, "has_instance")]
        elif stanza == "instance" and key == "property_value":
            parts = val.split()
            if len(parts) >= 2 and _OBO_ID_TOKEN.match(parts[1]):
                raw_roles += [(current, "fact_subj"), (parts[1], "fact_obj")]

    name = ontology_name or fallback_name
    roles: dict[str, set[str]] = defaultdict(set)
    for obo_id, role in raw_roles:
        roles[obo_to_iri(obo_id, name)].add(role)
    labels = {obo_to_iri(k, name): v for k, v in raw_labels.items()}
    return dict(roles), labels


def summarise(roles: dict[str, set[str]]) -> dict[str, tuple[bool, bool]]:
    """``iri -> (plays a class role, plays an instance role)``."""
    return {iri: (bool(rs & CLASS_ROLES), bool(rs & INSTANCE_ROLES)) for iri, rs in roles.items()}


def kb_individuals(
    individual_count: pd.Series, skos_concept_count: pd.Series | None = None
) -> pd.Series:
    """Individuals that make a source knowledge-base-like: SKOS concepts excluded.

    The corpus' ``individual_count`` follows the RDF semantics, in which a
    ``skos:Concept`` is an individual. A concept scheme's concepts are its
    vocabulary rather than facts about a domain, so for the ontology versus
    knowledge-base split they count as terms. Missing counts read as zero; a
    missing ``skos_concept_count`` column (rows parsed before it existed)
    subtracts nothing.
    """
    ind = individual_count.fillna(0)
    if skos_concept_count is None:
        return ind
    return ind - skos_concept_count.fillna(0)
