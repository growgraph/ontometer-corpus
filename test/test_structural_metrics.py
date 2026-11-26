"""The structural-metrics step: per-ontology hierarchy metrics for the corpus."""

from __future__ import annotations

import pytest

from ontometer_corpus.steps.structural_metrics import _OWL_THING, _metrics_from_structure

ANIMAL, MAMMAL, DOG, CAT = "ex:Animal", "ex:Mammal", "ex:Dog", "ex:Cat"
CHAIN = [(MAMMAL, ANIMAL), (DOG, MAMMAL), (CAT, MAMMAL)]
CLASSES = {ANIMAL, MAMMAL, DOG, CAT}

COMPARED = [
    "n_nodes",
    "n_roots",
    "n_classes",
    "n_multiple_inheritance",
    "n_orphans",
    "mean_depth_long",
    "max_depth_long",
    "mean_depth_short",
    "branching",
    "depth_hist",
]


def test_an_explicit_owl_thing_parent_changes_nothing():
    # The same hierarchy, saved by a tool that writes `Animal subClassOf
    # owl:Thing` and by one that does not, must measure the same.
    without = _metrics_from_structure(CHAIN, CLASSES)
    with_thing = _metrics_from_structure([*CHAIN, (ANIMAL, _OWL_THING)], CLASSES | {_OWL_THING})
    assert {k: with_thing[k] for k in COMPARED} == {k: without[k] for k in COMPARED}


def test_hierarchy_values_on_a_small_chain():
    m = _metrics_from_structure(CHAIN, CLASSES)
    assert m["max_depth_long"] == 2
    assert m["n_classes"] == 4
    assert m["n_roots"] == 1
    # Animal has one child, Mammal two.
    assert m["branching"] == pytest.approx(1.5)


def test_owl_thing_is_never_a_class():
    m = _metrics_from_structure([(ANIMAL, _OWL_THING)], {ANIMAL, _OWL_THING})
    assert m["n_classes"] == 1
    assert m["n_nodes"] == 0


def test_a_class_only_under_owl_thing_is_an_orphan():
    # With the Thing line ignored it sits in no subclass edge, as in the
    # evaluator, where it is a root with no named parent.
    m = _metrics_from_structure([*CHAIN, ("ex:Lonely", _OWL_THING)], CLASSES)
    assert m["n_orphans"] == 1
    assert m["n_classes"] == 5


def test_an_explicit_owl_thing_still_counts_toward_multiple_inheritance():
    # OQuaRE's TMOnto counts asserted parents, owl:Thing included, so a class
    # under Mammal that also asserts owl:Thing has two.
    m = _metrics_from_structure([*CHAIN, (DOG, _OWL_THING)], CLASSES)
    assert m["n_multiple_inheritance"] == 1
    assert m["multiple_inheritance_rate"] == pytest.approx(1 / 4)


def test_branching_is_noconto_without_the_owl_thing_term():
    # |E| / (|C| - |L|): three edges over the two classes with children.
    m = _metrics_from_structure(CHAIN, CLASSES)
    n_leaves = 2
    assert m["branching"] == pytest.approx(len(CHAIN) / (len(CLASSES) - n_leaves))
