"""Radial layout of the ontology specialisation network in the Poincaré disk."""

from __future__ import annotations

import math

import numpy as np

from ontometer_corpus.network.hyperbolic import (
    DEFAULT_R_MAX,
    poincare_radius,
    primary_parents,
    radial_angles,
)


def test_primary_parent_is_the_most_specialised_one_with_low_index_tie_break():
    child = np.array([0, 0, 1, 1])
    parent = np.array([2, 3, 2, 3])
    weight = np.array([5, 9, 4, 4])
    assert primary_parents(4, child, parent, weight).tolist() == [3, 2, -1, -1]


def test_mutual_specialisation_is_cut_at_the_lowest_index():
    # 1 and 2 specialise each other most; 0 hangs under 1.
    out = primary_parents(3, np.array([1, 2, 0]), np.array([2, 1, 1]), np.array([3, 3, 1]))
    assert out.tolist() == [1, -1, 1]


def test_children_sit_inside_their_parent_wedge():
    # Root 0 with children 1, 2; 1 has children 3, 4. Root 5 alone, in another group.
    pp = np.array([-1, 0, 0, 1, 1, -1])
    group = np.array([0, 0, 0, 0, 0, 1])
    a = radial_angles(pp, group)
    # Root 0 has 5 of 6 nodes: wedge [0, 5/6 * 2π); root 5 the rest.
    w0 = 2 * math.pi * 5 / 6
    assert math.isclose(a[0], w0 / 2)
    assert math.isclose(a[5], w0 + (2 * math.pi - w0) / 2)
    # Node 1's subtree (3 nodes) takes 3/4 of root 0's wedge, node 2 the rest.
    w1 = w0 * 3 / 4
    assert 0 <= a[3] < w1 and 0 <= a[4] < w1
    assert w1 <= a[2] < w0
    assert len(set(np.round(a, 9))) == 6


def test_radius_moves_inward_as_specialisers_grow():
    r = poincare_radius(np.array([0, 1, 10, 100, 999]), 1000)
    assert math.isclose(r[0], math.tanh(DEFAULT_R_MAX / 2))
    assert (np.diff(r) < 0).all()
    assert r[-1] < 1e-9
    assert (r >= 0).all() and (r < 1).all()
