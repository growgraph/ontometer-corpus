"""Role and source-kind names that the tables carry.

Plain constants, kept apart from :mod:`ontometer_corpus.roles` (which needs rdflib) so
that the table loaders can use them with the ``tables`` extra alone.
"""

from __future__ import annotations

#: Role recorded for a term typed ``skos:Concept``.
SKOS_CONCEPT_ROLE = "skos_concept"

#: Values of corpus_registry.kind for sources whose vocabulary is mostly SKOS
#: concepts rather than OWL/RDFS classes (pipeline.db._KIND_SQL). They stay in
#: the corpus inventory and the dependency network, and are excluded from the
#: hierarchy-based analyses (term network, G(t), structural metrics,
#: clustering), which assume an OWL class hierarchy.
SKOS_DOMINATED_KINDS: tuple[str, ...] = ("thesaurus", "mostly_skos")
