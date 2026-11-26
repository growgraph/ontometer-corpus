"""First-evidence dating and the growth statistics of a specialisation network."""

from __future__ import annotations

import numpy as np
import pytest
from click import ClickException

from ontometer_corpus.dating import (
    archive_candidates,
    archive_timestamp_date,
    bound_births,
    dating_consistency,
    earliest,
    iri_match_key,
    plausible,
)
from ontometer_corpus.metrics.growth import (
    attachment_null,
    kaplan_meier,
    survival_at,
    top_decile_share,
)
from ontometer_corpus.steps.ontology_dates import _Cache, _require_cached


def test_iri_normalisation_ignores_scheme_separator_and_extension():
    assert iri_match_key("https://Example.org/onto.owl#") == "example.org/onto"
    assert iri_match_key("http://example.org/onto/") == "example.org/onto"
    assert iri_match_key(None) is None


def test_obo_purl_is_also_looked_up_as_its_owl_download():
    urls = archive_candidates("http://purl.obolibrary.org/obo/bfo.owl")
    assert "purl.obolibrary.org/obo/bfo" in urls
    assert "purl.obolibrary.org/obo/bfo.owl" in urls
    assert archive_candidates("http://example.org/x/") == ["example.org/x"]
    assert archive_candidates("/c:/work/onto.owl") == []


def test_dates_are_bounded_and_missing_values_ignored():
    assert archive_timestamp_date("20111009044834") == "2011-10-09"
    assert archive_timestamp_date("") is None
    assert plausible("1980-01-01", "2026-09-27") is None
    assert plausible("2030-01-01", "2026-09-27") is None
    assert plausible("2012-05-02T00:00:00Z", "2026-09-27") == "2012-05-02"
    assert plausible("2012", "2026-09-27") == "2012-01-01"
    assert plausible("2012-99-99", "2026-09-27") is None
    assert plausible("2012-05", "2026-09-27") is None
    assert earliest(["2014-01-01", None, float("nan"), "2011-03-01"]) == "2011-03-01"
    assert earliest([None]) is None


def test_dating_consistency_counts_children_not_older_than_parents():
    births = np.array([0.0, 10.0, 20.0, np.nan])
    child = np.array([1, 2, 0, 3])
    parent = np.array([0, 1, 2, 0])
    result = dating_consistency(births, child, parent, reps=50, seed=1)
    # The pair with an undated end is skipped; two of the three dated pairs agree.
    assert result.n_pairs == 3
    assert result.share == pytest.approx(2 / 3)
    assert 0.0 <= result.null_mean <= result.null_q975 <= 1.0
    again = dating_consistency(births, child, parent, reps=50, seed=1)
    assert again == result


def test_offline_date_rebuild_requires_every_lookup_key(tmp_path):
    cache = _Cache(tmp_path / "dating_cache.jsonl")
    cache.put("cdx:example.org/one", None)
    with pytest.raises(ClickException, match="1 required lookup"):
        _require_cached(cache, ["cdx:example.org/one", "wd:Q2"])


def test_cached_null_is_a_completed_lookup(tmp_path):
    cache = _Cache(tmp_path / "dating_cache.jsonl")
    cache.put("cdx:example.org/one", None)
    _require_cached(cache, ["cdx:example.org/one"])


def test_parent_is_born_no_later_than_its_first_observed_specialisation():
    nan = np.nan
    births = np.array([500.0, 300.0, nan, nan])
    parent = np.array([0, 0, 2, 3])
    edge_time = np.array([400.0, 450.0, 120.0, nan])
    out = bound_births(births, parent, edge_time)
    assert out[:3].tolist() == [400.0, 300.0, 120.0]
    # No dated specialisation, no bound.
    assert np.isnan(out[3])


def test_bound_is_not_chained_through_intermediate_ontologies():
    # 2 specialises 1 at t=10; 1 specialises 0 at t=50. 1 exists by 10, but that
    # says nothing about when it specialised 0.
    out = bound_births(np.array([100.0, 100.0, 5.0]), np.array([1, 0]), np.array([10.0, 50.0]))
    assert out.tolist() == [50.0, 10.0, 5.0]


def test_kaplan_meier_with_censoring():
    # Events at 1 and 3; one censored at 2; one censored at 4.
    t, s = kaplan_meier(np.array([1, 2, 3, 4]), np.array([True, False, True, False]))
    assert t.tolist() == [1.0, 3.0]
    assert np.allclose(s, [0.75, 0.75 * 0.5])
    assert survival_at(t, s, 0.5) == 1.0
    assert survival_at(t, s, 2.0) == 0.75


def test_top_decile_share():
    assert top_decile_share(np.array([0, 10, 0])) == 1.0
    x = np.array([10] + [1] * 9)
    assert np.isclose(top_decile_share(x), 10 / 19)


def test_attachment_null_never_targets_self_unborn_or_the_same_target_twice():
    birth = np.array([0.0, 0.0, 5.0, np.nan])
    # Node 2 arrives at t=5 and specialises two ontologies: only 0 and 1 exist.
    res = attachment_null(
        np.array([5.0, 5.0]), np.array([2, 2]), birth, preferential=False, reps=20
    )
    assert (res.n_targets == 2).all()
    # A single early specialisation can only land on the one other born node.
    res = attachment_null(np.array([0.0]), np.array([0]), birth, preferential=True, reps=5)
    assert (res.n_targets == 1).all()


def test_preferential_null_concentrates_more_than_uniform():
    n = 60
    birth = np.zeros(n)
    t = np.arange(200, dtype=float)
    child = np.random.default_rng(1).integers(0, n, size=200)
    uni = attachment_null(t, child, birth, preferential=False, reps=30, seed=2)
    pref = attachment_null(t, child, birth, preferential=True, reps=30, seed=2)
    assert pref.n_targets.mean() < uni.n_targets.mean()
