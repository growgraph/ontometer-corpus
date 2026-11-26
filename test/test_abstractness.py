"""Closure counts, IC and the ontology-level couplings, on hand-checkable graphs."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ontometer_corpus.metrics.abstractness import (
    dag_closure,
    effective_number,
    encode_edges,
    entropy,
    ic_sanchez,
    ic_seco,
    main_sequence_distance,
    martin_instability,
    role_share,
)


def _closure(edges: list[tuple[str, str]]):
    names, ch, pa = encode_edges(edges)
    cl = dag_closure(len(names), ch, pa)
    idx = {n: i for i, n in enumerate(names)}
    return cl, idx


def test_chain_counts():
    cl, i = _closure([("b", "a"), ("c", "b")])
    assert cl.n_desc[i["a"]] == 2
    assert cl.n_desc[i["b"]] == 1
    assert cl.n_desc[i["c"]] == 0
    assert cl.n_anc[i["c"]] == 2
    assert cl.depth_long[i["c"]] == 2
    # Only c is a leaf; it counts itself.
    assert cl.n_leaves[i["a"]] == 1
    assert cl.n_leaves[i["c"]] == 1
    assert not cl.cyclic.any()


def test_diamond_counts_nodes_not_paths():
    # bottom reaches top by two paths; a path count would give top 4 descendants.
    cl, i = _closure([("l", "top"), ("r", "top"), ("bottom", "l"), ("bottom", "r")])
    assert cl.n_desc[i["top"]] == 3
    assert cl.n_leaves[i["top"]] == 1
    assert cl.n_anc[i["bottom"]] == 3
    assert cl.depth_long[i["bottom"]] == 2
    assert cl.depth_short[i["bottom"]] == 2


def test_shortcut_edge_separates_long_and_short_depth():
    cl, i = _closure([("mid", "top"), ("bottom", "mid"), ("bottom", "top")])
    assert cl.depth_long[i["bottom"]] == 2
    assert cl.depth_short[i["bottom"]] == 1
    assert cl.n_desc[i["top"]] == 2


def test_cycle_is_condensed_and_flagged():
    # A and B subsume each other (a defect); C sits under A.
    cl, i = _closure([("A", "B"), ("B", "A"), ("C", "A"), ("D", "C")])
    assert cl.cyclic[i["A"]] and cl.cyclic[i["B"]]
    assert not cl.cyclic[i["C"]]
    # A's descendants: its cycle-mate B, plus C and D.
    assert cl.n_desc[i["A"]] == 3
    assert cl.n_desc[i["B"]] == 3
    assert cl.n_anc[i["D"]] == 3


def test_multiple_roots_and_leaf_count():
    cl, i = _closure([("x", "r1"), ("y", "r1"), ("y", "r2")])
    assert cl.n_leaves[i["r1"]] == 2
    assert cl.n_leaves[i["r2"]] == 1
    assert cl.depth_long[i["r2"]] == 0


def test_self_loops_are_dropped():
    names, ch, _ = encode_edges([("a", "a"), ("b", "a")])
    assert len(ch) == 1
    assert sorted(names) == ["a", "b"]


def test_ic_seco_bounds():
    ic = ic_seco(np.array([0, 3]), n_nodes=4)
    assert ic[0] == pytest.approx(1.0)  # leaf
    assert ic[1] == pytest.approx(0.0)  # subsumes everything else


def test_ic_sanchez_separates_equal_leaves_by_subsumers():
    # Same single leaf below, but the second class is more specialised.
    ic = ic_sanchez(np.array([1, 1, 4]), np.array([0, 3, 0]))
    assert ic[1] > ic[0]
    assert ic[2] < ic[0]
    assert ((ic >= 0) & (ic <= 1)).all()


def test_role_share():
    rs = role_share(np.array([3, 0, 0]), np.array([1, 2, 0]))
    assert rs[0] == pytest.approx(0.75)
    assert rs[1] == 0.0
    assert math.isnan(rs[2])


def test_entropy_and_effective_number():
    assert entropy(np.array([5])) == 0.0
    assert effective_number(np.array([2, 2, 2, 2])) == pytest.approx(4.0)
    assert effective_number(np.array([0, 0])) == 0.0


def test_martin_instability_and_distance():
    inst = martin_instability(np.array([4, 0, 0]), np.array([0, 3, 0]))
    assert inst[0] == 0.0  # depended on, depends on nothing: stable
    assert inst[1] == 1.0
    assert math.isnan(inst[2])
    d = main_sequence_distance(np.array([1.0, 0.0]), np.array([0.0, 0.0]))
    assert d[0] == pytest.approx(0.0)  # abstract and stable: on the line
    assert d[1] == pytest.approx(1.0)  # concrete and stable: zone of pain
