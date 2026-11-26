"""G(t) rebuilt from the tracked ontology-level tables, and the payload of its view."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ontometer_corpus.dating import time_pairs
from ontometer_corpus.growth_network import assemble, days, decimal_year, viz_payload


def test_pair_arrival_uses_the_panel_where_known_else_the_child_birth():
    nan = np.nan
    birth = np.array([100.0, 300.0, 500.0])
    release = np.array([900.0, 950.0, 990.0])
    child = np.array([1, 2])
    parent = np.array([0, 0])
    arrival, bounded = time_pairs(birth, release, child, parent, np.array([400.0, nan]))
    assert arrival.tolist() == [400.0, 500.0]
    # Parent 0 is bounded by observations only: min(own 100, panel 400, release of 2 at 990).
    assert bounded[0] == 100.0


def test_assumed_arrival_never_moves_a_parent_birth():
    nan = np.nan
    # Child 1 was first seen early (birth 50), but the pair is only observed in its
    # captured release (800). The parent, first seen at 600, must not move to 50.
    arrival, bounded = time_pairs(
        np.array([600.0, 50.0]),
        np.array([900.0, 800.0]),
        np.array([1]),
        np.array([0]),
        np.array([nan]),
    )
    assert arrival.tolist() == [50.0]
    assert bounded.tolist() == [600.0, 50.0]


def _toy():
    onto = pd.DataFrame(
        {
            "onto": [0, 1, 2, 3],
            "family": ["a", "b", "c", "d"],
            "release_date": ["2015-01-01", "2016-01-01", "2018-01-01", None],
            "registry": ["OBO", "LOV", "LOV", "OKG"],
            "subject": [True, True, False, True],
            "community": [0, 0, 1, 1],
        }
    )
    dates = pd.DataFrame(
        {
            "family": ["a", "b", "c", "d"],
            "birth": ["2010-06-01", "2012-06-01", "2014-06-01", None],
            "as_of": ["2020-01-01"] * 4,
        }
    )
    dep = pd.DataFrame(
        {"child": [1, 2], "parent": [0, 1], "n_terms": [5, 2], "panel_first": ["2013-06-01", None]}
    )
    return assemble(onto, dep, dates)


def test_assemble_places_pairs_and_leaves_undated_ontologies_out():
    net = _toy()
    assert np.isnan(net.b_bnd[3])
    assert net.eps[0] == days(pd.Series(["2013-06-01"]))[0]
    assert net.eps[1] == net.b_obs[2]
    assert net.as_of_label == "2020-01-01"


def test_viz_payload_counts_transitive_specialisers_over_time():
    net = _toy()
    data = viz_payload(net, ["A", "B", "C", "D"], ["a", "b", "c", "d"], step=1.0, series_from=2010)
    meta = data["meta"]
    assert (meta["n_shown"], meta["n_undated"], meta["n_edges"]) == (3, 1, 2)
    # The most specialised ontology (A: specialised by B, and by C through B) sets the normaliser.
    assert meta["n_final"] == 3
    a = next(n for n in data["nodes"] if n["label"] == "A")
    # One specialiser once B's link arrives (mid-2013), two once C's does (mid-2014).
    assert [s[1] for s in a["steps"]] == [1, 2]
    assert a["steps"][0][0] >= float(decimal_year(net.eps[0]))
    c = next(n for n in data["nodes"] if n["label"] == "C")
    assert c["steps"] == [] and c["first"] is None
    assert data["series"]["corpus"][-1] == round(2 / 3, 4)
