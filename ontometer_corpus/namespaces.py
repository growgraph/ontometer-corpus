"""Namespace rules: which namespace an IRI belongs to, and which no ontology can own.

Coupling — how much of an ontology's vocabulary it borrows rather than declares —
depends on these rules, so every coupling measure reads this one definition. The
module needs only the standard library, so any package that measures coupling the
same way can import it without the rest of the corpus stack.

:data:`LANGUAGE_NAMESPACES` is the one closed list: the namespaces of RDF, OWL and
the other representation languages. Popular vocabularies are not listed as
"generic"; how a file uses them is measured instead.
"""

from __future__ import annotations

import re

#: The generic OBO Foundry path. Every OBO term sits directly under it, so
#: treating it as one namespace would collapse GO, CHEBI and every other OBO
#: ontology into a single dependency. :func:`iri_namespace` splits it by prefix.
OBO_BASE_NS = "http://purl.obolibrary.org/obo/"

_OBO_ID_RE = re.compile(r"^([A-Za-z]+)_[0-9]+$")

#: Namespaces of the representation languages and formats themselves, not of
#: any vocabulary written in them. Membership is fixed by specification, not by
#: how popular a vocabulary is: RDF, RDFS and XSD (RDF 1.1), the XML namespace,
#: OWL 2, SWRL and the SWRL annotations Protégé writes, the OBO-format-to-OWL
#: mapping (oboInOwl, which encodes OBO flat-file constructs such as synonyms
#: and subsets), and Protégé's own editor bookkeeping. Using ``rdf:type`` or
#: ``oboInOwl:hasExactSynonym`` is writing in a language, not reusing an
#: ontology. Every other vocabulary — Dublin Core, SKOS, FOAF, PROV — is an
#: ontology like any other; whether a file uses it for modelling or only for
#: metadata is decided per use (:mod:`ontometer_corpus.usage`), not here.
LANGUAGE_NAMESPACES: frozenset[str] = frozenset(
    {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/2001/XMLSchema#",
        "http://www.w3.org/XML/1998/namespace",
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2003/11/swrl#",
        "http://www.w3.org/2003/11/swrlb#",
        "http://swrl.stanford.edu/ontologies/3.3/swrla.owl#",
        "urn:swrl#",
        "urn:swrl:var#",
        "http://www.geneontology.org/formats/oboInOwl#",
        "http://protege.stanford.edu/plugins/owl/protege#",
    }
)

#: Namespaces no ontology can own: the languages, and the bare OBO PURL root,
#: which every OBO ontology shares and :func:`iri_namespace` only returns for
#: malformed OBO local names. Removed from an ontology's own namespaces and
#: from its external references.
UNOWNABLE_NAMESPACES: frozenset[str] = LANGUAGE_NAMESPACES | {OBO_BASE_NS}

# Clear identifier, citation, licence, or web-reference namespaces. These may
# legitimately occur in ontology annotations, but are not domain-vocabulary
# reuse. The list is deliberately narrow: an unrecognised HTTP(S) namespace
# stays a possible domain vocabulary rather than being discarded by guesswork.
_METADATA_OR_IDENTIFIER_PREFIXES = (
    "doi:",
    "http://doi.org/",
    "https://doi.org/",
    "http://dx.doi.org/",
    "http://orcid.org/",
    "https://orcid.org/",
    "http://creativecommons.org/",
    "https://creativecommons.org/",
    "http://en.wikipedia.org/",
    "https://en.wikipedia.org/",
    "https://commons.wikimedia.org/",
    "http://commons.wikimedia.org/",
    "http://upload.wikimedia.org/",
    "https://upload.wikimedia.org/",
    "http://species.wikimedia.org/",
    "https://species.wikimedia.org/",
    "http://www.ncbi.nlm.nih.gov/pubmed/",
    "http://identifiers.org/",
    "https://identifiers.org/",
)


def _normalised_namespace(iri: str) -> str:
    """Case-insensitive namespace key with trailing separators removed."""
    return iri.strip().lower().rstrip("/#")


_LANGUAGE_KEYS = frozenset(_normalised_namespace(ns) for ns in LANGUAGE_NAMESPACES)


def reference_kind(reference_iri: str) -> str:
    """Coarse research role of a referenced namespace or IRI.

    ``language`` for the representation languages (:data:`LANGUAGE_NAMESPACES`),
    ``metadata_or_identifier`` for identifier and web-reference namespaces,
    ``malformed_or_local`` for strings that are not resolvable namespaces
    (including the bare OBO PURL root, which only arises from malformed OBO
    local names), else ``domain_vocabulary``. A namespace alone cannot say
    whether a file uses it for modelling or for metadata; that is measured per
    use by :mod:`ontometer_corpus.usage`.
    """
    text = reference_iri.strip().lower()
    if _normalised_namespace(text) in _LANGUAGE_KEYS:
        return "language"
    if _normalised_namespace(text) == _normalised_namespace(OBO_BASE_NS):
        return "malformed_or_local"
    if text in {"http://", "https://"} or not text.startswith(("http://", "https://", "doi:")):
        return "malformed_or_local"
    if any(
        text == prefix.rstrip("/") or text.startswith(prefix)
        for prefix in _METADATA_OR_IDENTIFIER_PREFIXES
    ):
        return "metadata_or_identifier"
    return "domain_vocabulary"


def iri_namespace(iri: str | None) -> str | None:
    """Return an IRI's namespace prefix.

    Everything up to and including the last ``#`` or, failing that, the last
    ``/``. Returns ``None`` for empty input or a string with neither delimiter,
    which is not a hierarchical IRI.

    Special case: under the generic OBO Foundry path, a well-formed
    ``PREFIX_localid`` local name (``GO_0008150``, ``CHEBI_36357``) yields
    ``.../obo/PREFIX_`` rather than the bare ``.../obo/``, so that OBO
    ontologies are distinguishable from one another. Malformed or non-standard
    local names fall back to the bare generic path.
    """
    if not iri:
        return None
    hash_idx = iri.rfind("#")
    if hash_idx != -1:
        return iri[: hash_idx + 1]
    slash_idx = iri.rfind("/")
    if slash_idx != -1:
        base = iri[: slash_idx + 1]
        if base == OBO_BASE_NS:
            match = _OBO_ID_RE.match(iri[slash_idx + 1 :])
            if match:
                return f"{base}{match.group(1)}_"
        return base
    return None
