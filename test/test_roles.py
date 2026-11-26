"""Class and instance roles, including the cases metadata would otherwise fake."""

from __future__ import annotations

import rdflib

from ontometer_corpus.roles import SKOS_CONCEPT_ROLE, roles_from_graph, roles_from_obo, summarise

EX = "http://example.org/zoo#"

TTL = f"""
@prefix : <{EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

<http://example.org/zoo> a owl:Ontology ; dcterms:creator :alice .

:Animal a owl:Class ; rdfs:label "animal"@en, "Tier"@de .
:Dog a owl:Class ; rdfs:subClassOf :Animal .
:Eagle a owl:Class ; rdfs:subClassOf :Animal .
:hasKeeper a owl:ObjectProperty ; rdfs:range :Keeper .
:seeAlsoLink a owl:AnnotationProperty .

:rex a owl:NamedIndividual, :Dog ; :hasKeeper :bob .
:Dog :seeAlsoLink :Eagle .

# Punning: the class Eagle is also asserted to be an instance of Species.
:Eagle a :Species .
:mammal a skos:Concept .
"""


def _roles():
    g = rdflib.Graph()
    g.parse(data=TTL, format="turtle")
    return roles_from_graph(g)


def test_class_roles():
    roles, _ = _roles()
    assert {"decl_class", "sub_parent"} <= roles[EX + "Animal"]
    assert "sub_child" in roles[EX + "Dog"]
    assert "has_instance" in roles[EX + "Dog"]  # rex is a Dog
    assert "axiom_ref" in roles[EX + "Keeper"]  # range filler


def test_instance_roles():
    roles, _ = _roles()
    assert {"decl_individual", "typed", "fact_subj"} <= roles[EX + "rex"]
    assert "fact_obj" in roles[EX + "bob"]


def test_skos_concept_role_is_recorded_explicitly():
    roles, _ = _roles()
    assert SKOS_CONCEPT_ROLE in roles[EX + "mammal"]


def test_header_and_annotation_links_are_not_facts():
    roles, _ = _roles()
    # alice appears only as the header's dcterms:creator.
    assert EX + "alice" not in roles
    # A declared annotation property does not make Dog or Eagle an instance.
    assert "fact_subj" not in roles[EX + "Dog"]


def test_punned_class_plays_both_roles():
    roles, _ = _roles()
    is_class, is_instance = summarise(roles)[EX + "Eagle"]
    assert is_class and is_instance


def test_meta_vocabulary_is_never_a_term():
    roles, _ = _roles()
    assert not any(iri.startswith("http://www.w3.org/2002/07/owl#") for iri in roles)


def test_english_label_preferred():
    _, labels = _roles()
    assert labels[EX + "Animal"] == "animal"


OBO = """format-version: 1.2
ontology: zoo

[Term]
id: ZOO:0000001
name: animal

[Term]
id: ZOO:0000002
name: dog
is_a: ZOO:0000001 ! animal
relationship: part_of ZOO:0000009

[Instance]
id: ZOO:0000100
name: rex
instance_of: ZOO:0000002
property_value: kept_by ZOO:0000200

[Typedef]
id: part_of
"""


def test_obo_roles_and_labels():
    roles, labels = roles_from_obo(OBO)
    base = "http://purl.obolibrary.org/obo/ZOO_"
    assert {"decl_class", "sub_parent"} <= roles[base + "0000001"]
    assert "axiom_ref" in roles[base + "0000009"]
    assert {"decl_individual", "typed", "fact_subj"} <= roles[base + "0000100"]
    assert "has_instance" in roles[base + "0000002"]
    assert labels[base + "0000002"] == "dog"
    # Typedefs declare properties; they play no class or instance role.
    assert not any(k.endswith("part_of") for k in roles)


def test_owl_thing_typing_is_an_instance_role():
    g = rdflib.Graph().parse(
        data=f"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        <{EX}johnSmith> a owl:Thing .
        """,
        format="turtle",
    )
    roles, _ = roles_from_graph(g)
    assert roles == {f"{EX}johnSmith": {"typed"}}


def test_kb_individuals_subtracts_concepts_and_reads_missing_as_zero():
    import pandas as pd

    from ontometer_corpus.roles import kb_individuals

    ind = pd.Series([5, None, 3])
    assert kb_individuals(ind, pd.Series([2, None, None])).tolist() == [3.0, 0.0, 3.0]
    assert kb_individuals(ind).tolist() == [5.0, 0.0, 3.0]
