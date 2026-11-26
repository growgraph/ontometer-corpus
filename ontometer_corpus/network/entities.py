"""Term-level ownership and edge authority across the corpus.

The ontology network resolves reuse by *namespace*; entity-level metrics need
the finer question of who is entitled to assert what about a term.

**Home.** A term's home is the ontology that owns its namespace. Every
ontology that declares terms in a namespace claims it, and claims collide: a
file that copies GO terms into itself (MIREOT, or a baked import closure)
declares them just as GO does. Collisions are settled in order by

1. the ontology whose own IRI names the namespace (``…/obo/go.owl`` owns
   ``…/obo/GO_`` and OBO sub-namespaces such as ``…/obo/go/subsets#``;
   ``http://xmlns.com/foaf/0.1/`` owns ``…/foaf/0.1/``), the one declaring
   most if several do. A narrow exception lets a dominant SKOS concept scheme
   win only when that named claimant declares nothing in the namespace;
2. otherwise a *dominant* declarer: the claimant declaring at least
   ``DOMINANCE`` times as many terms in the namespace as any other claimant.
   An ontology whose file declares no IRI still owns its ID prefix when
   copiers hold only a small share of its terms.

A namespace with no claimant, contested with no dominant claimant, or classified
as a representation language or metadata/identifier infrastructure
(``reference_kind``) is homed outside the corpus. That is the right answer for namespaces that are no
ontology's (ORCID iDs and DOIs declared as individuals, licence URLs) and for
vocabularies outside the corpus that several files copy from (DBpedia
resources): giving them to the largest copier would credit it with every use.
Registry provenance plays no part: a copier listed by the "right" registry
would otherwise beat the real owner listed elsewhere.

**Edge authority.** A subclass edge ``(c, p)`` asserted in ontology ``O`` is

* *own* when ``O`` is ``c``'s home — the ontology is speaking about its own
  class. If ``p`` is homed elsewhere this is a *foreign specialisation*: ``O``
  builds under someone else's class, the signal that ``p`` is general enough
  to be reused.
* *bridge* when ``p`` is homed in ``O`` but ``c`` is not — ``O`` files a
  foreign class under one of its own (typical of alignment modules).
* *copy* otherwise — ``O`` repeats an edge between terms it owns neither end
  of, which is what an import closure looks like on disk. Copies are never
  counted as reuse.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from ontometer_corpus.namespaces import OBO_BASE_NS, reference_kind
from ontometer_corpus.network.construction import _ns_key
from ontometer_corpus.roles import is_meta

#: Own edge with both ends homed in the asserting ontology.
EDGE_OWN_INTERNAL = 0
#: Own edge whose parent is homed elsewhere: a foreign specialisation.
EDGE_OWN_FOREIGN = 1
#: Parent homed in the asserting ontology, child homed elsewhere.
EDGE_BRIDGE = 2
#: Neither end homed in the asserting ontology: a copied edge, never reuse.
EDGE_COPY = 3
#: Names of the edge codes, indexed by code.
EDGE_KIND_NAMES = ("own_internal", "own_foreign", "bridge", "copy")

#: Home index for a term whose namespace no corpus ontology claims.
EXTERNAL = -1


@dataclass(frozen=True, slots=True)
class Claimant:
    """One ontology's claim on a namespace."""

    onto: int
    registry: str
    ontology_iri: str | float | None  # NaN when it comes from a pandas column
    n_declared: int
    n_skos_concepts: int = 0


#: How many times more terms than any other claimant a claimant must declare
#: in a namespace to own it when no claimant's IRI names it (rule 2 above).
DOMINANCE = 10


@dataclass(frozen=True, slots=True)
class NamespaceOwnerRecord:
    """One corpus row participating in namespace-home resolution."""

    owner: int
    ontology_iri: str | None
    source_registry: str
    own_namespaces: tuple[str, ...]
    declarations: dict[str, int]
    skos_concepts: dict[str, int]


# First segment of an OBO namespace after the PURL base: the ID prefix of
# ``…/obo/GO_``, or the ontology folder of ``…/obo/ro/subsets#`` and
# ``…/obo/ncbitaxon#``.
_OBO_SEGMENT = re.compile(r"([A-Za-z][A-Za-z0-9]*)(?:_|[/#]|$)")


def iri_names_namespace(ontology_iri: str | float | None, ns: str) -> bool:
    """Whether an ontology's own IRI identifies *ns* as its namespace.

    Besides an exact match, an OBO ontology (``…/obo/ro.owl``) names its ID
    prefix (``…/obo/RO_``) and every namespace under its PURL folder
    (``…/obo/ro/subsets#``, ``…/obo/ro#``). A missing IRI may arrive as None
    or, from a pandas column, as NaN.
    """
    if not isinstance(ontology_iri, str) or not ontology_iri:  # None, or NaN from pandas
        return False
    if _ns_key(ontology_iri) == _ns_key(ns):
        return True
    if ns.startswith(OBO_BASE_NS) and ontology_iri.lower().startswith(OBO_BASE_NS):
        match = _OBO_SEGMENT.match(ns[len(OBO_BASE_NS) :])
        stem = ontology_iri.rstrip("/").rsplit("/", 1)[-1].lower().split(".", 1)[0]
        return bool(match) and match.group(1).lower() == stem
    return False


def curating_registry(ns: str) -> str:
    """The registry that curates namespace *ns*: ``"OBO"`` for OBO PURL prefixes, else ``"LOV"``.

    Used to break exact ties between copies of one ontology from several
    registries (dedup Pass D); it plays no part in namespace ownership.
    """
    return "OBO" if ns.startswith(OBO_BASE_NS) else "LOV"


def pick_home(ns: str, claimants: list[Claimant]) -> int:
    """The home ontology of namespace *ns* among its claimants, or ``EXTERNAL``."""
    if reference_kind(ns) != "domain_vocabulary":
        return EXTERNAL
    named = [c for c in claimants if iri_names_namespace(c.ontology_iri, ns)]
    # A SKOS concept scheme can use one namespace for its concepts and name
    # another in its ontology IRI. A smaller vocabulary may then happen to
    # name the concept namespace. Compare the scheme only with that *named*
    # claimant: other thesauri mentioning the namespace are irrelevant to
    # whether its IRI names the scheme's vocabulary.
    if named:
        named_home = max(named, key=lambda c: (c.n_declared, -c.onto))
        # A SKOS scheme occasionally names a schema IRI while a small subset
        # names the concept namespace. Only override an otherwise empty named
        # claim, and only with concept counts from this exact namespace.
        if named_home.n_declared == 0:
            concepts = sorted(claimants, key=lambda c: (-c.n_skos_concepts, c.onto))
            if (
                concepts
                and concepts[0].n_skos_concepts >= 10
                and concepts[0].n_skos_concepts >= DOMINANCE * max(1, named_home.n_skos_concepts)
            ):
                return concepts[0].onto
        return named_home.onto
    ranked = sorted(claimants, key=lambda c: (-c.n_declared, c.onto))
    if not ranked or ranked[0].n_declared == 0:
        return EXTERNAL
    if len(ranked) > 1 and ranked[0].n_declared < DOMINANCE * ranked[1].n_declared:
        return EXTERNAL
    return ranked[0].onto


def resolve_namespace_owners(records: Iterable[NamespaceOwnerRecord]) -> dict[str, int]:
    """Resolve namespace keys to corpus-row owners using :func:`pick_home`.

    This is the single ownership authority for dependency edges and term-level
    reuse measures. A result omits unresolved namespaces, which consumers
    should treat as external.
    """
    # One claim per (namespace, ontology): spellings that reduce to one key
    # (``…/a#`` and ``…/a/``) add up, so an ontology is never its own rival
    # under DOMINANCE.
    declared: dict[str, dict[int, int]] = defaultdict(dict)
    by_owner: dict[int, NamespaceOwnerRecord] = {}

    def claim(key: str, owner: int, n_declared: int) -> None:
        declared[key][owner] = declared[key].get(owner, 0) + n_declared

    for record in records:
        by_owner[record.owner] = record
        for ns in dict.fromkeys(record.own_namespaces):
            if is_meta(ns):
                continue
            n = record.declarations.get(ns, 0)
            claim(ns, record.owner, n)
            key = _ns_key(ns)
            if key != ns:
                claim(key, record.owner, n)
        if record.ontology_iri:
            claim(_ns_key(record.ontology_iri), record.owner, 0)

    owners: dict[str, int] = {}
    for key, counts in declared.items():
        claimants = [
            Claimant(
                owner,
                by_owner[owner].source_registry,
                by_owner[owner].ontology_iri,
                n,
                by_owner[owner].skos_concepts.get(key, 0),
            )
            for owner, n in counts.items()
        ]
        home = pick_home(key, claimants)
        if home != EXTERNAL:
            owners[key] = home
    return owners


def classify_edges(onto: int, home_child: np.ndarray, home_parent: np.ndarray) -> np.ndarray:
    """Edge-authority code per ``(child, parent)`` edge asserted in *onto*."""
    own = home_child == onto
    kind = np.full(len(home_child), EDGE_COPY, dtype=np.int8)
    kind[own & (home_parent == onto)] = EDGE_OWN_INTERNAL
    kind[own & (home_parent != onto)] = EDGE_OWN_FOREIGN
    kind[~own & (home_parent == onto)] = EDGE_BRIDGE
    return kind
