"""
Abstractness as the corpus grows: G(t).

The specialisation network of ontologies is a function of time. An ontology
that nothing specialises is *terminal* — a leaf of the network. When a later
ontology specialises one of its classes it becomes a branch, and in the
network sense it has become more abstract. This script reconstructs G(t) and
measures that process.

Reconstruction. The network is the one measured at capture: ``child`` →
``parent`` when a class homed in ``child`` is asserted, by ``child``, under a
class homed in ``parent`` (own-foreign edges, ``ontometer-corpus entity-network``). Each
ontology is placed in time by its first-evidence date (``ontometer-corpus dates``).
A specialisation pair (child, parent) is placed at the first release of the
child that asserts it when the child's releases are in the release panel
(``ontometer-corpus panel``, used only if
``$ONTOMETER_STORE/cache/panel_releases.parquet`` exists), and otherwise at the
child's first evidence —
an assumption the panel measures (the share of pairs already asserted in a
family's first panel release). It never predates its parent. Two birth
variants are measured:

* ``bounded`` (primary): the first-evidence date, further bounded by the rule
  that a class exists before anything specialises it, so an ontology is born no
  later than the first *observed* specialisation of one of its classes — the
  first panel release asserting it, or the captured release that does
  (``bound_births``); an undated parent with a dated observation becomes dated.
* ``first_evidence``: the first-evidence date alone.

Measured, per variant:

* dating consistency — the share of specialisations whose child is born no
  earlier than its parent, against a permutation of the dates;
* leaf survival — time from birth to the first specialiser (Kaplan–Meier,
  censored at capture). Ontologies already specialised when first attested are
  counted apart: the data cannot say when they stopped being leaves;
* the corpus over calendar time — how many ontologies exist, the share that are
  branches, normalised network generality ``log(1 + specialisers) / log N(t)``
  and height (longest chain of specialisers beneath), for all ontologies,
  for the measured ontologies (``subject``), and for fixed cohorts, so that change within existing ontologies is separated
  from the arrival of new, terminal ones;
* attachment — whether specialisations spread over existing ontologies or
  concentrate: the number of leaves that became branches against the same
  arrivals re-targeted uniformly and preferentially (``attachment_null``);
* who becomes a branch — content abstractness (``A_local``) of ontologies
  specialised within five years of birth against those that were not, net of
  ontology size;
* terms — home classes gaining a first foreign specialiser, over time;
* sense C over time — for terms used by other ontologies, whether later users
  use them as classes more than earlier users do.

Reads:  ``$ONTOMETER_STORE/cache/entity_{ontologies,terms,edges,docs}.parquet``,
        ``<corpus>/ontology_abstractness.parquet``,
        ``<corpus>/ontology_dates.parquet``, and, when
        ``$ONTOMETER_STORE/cache/panel_releases.parquet`` exists, the panel
        releases' sidecars under ``$ONTOMETER_STORE/cache/structure/v<N>/``.
Writes: ``<corpus>/ontology_specialisations.parquet`` (the ontology-level
        pairs with ``n_terms`` and ``panel_first``; with
        ``ontology_abstractness`` and ``ontology_dates`` it is all
        ``ontometer_corpus.growth_network`` needs to rebuild G(t) without the
        object store), ``<corpus>/abstractness_growth.parquet`` (per
        ontology), and, to ``--out``,
        ``growth_{yearly,cohorts,leaf_survival,attachment_null,terms_per_year,sense_c_by_lag}.csv``,
        ``growth_summary.json`` and the figures ``growth_branch_share.png``,
        ``growth_leaf_survival.png`` and ``growth_attachment_null.png``.
Runs after: ``ontometer-corpus abstractness`` and ``ontometer-corpus dates``; optionally
``ontometer-corpus panel select``.
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Usage:
    ontometer-corpus growth --store /path/to/ontologies --out /path/to/out
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import click
import numpy as np
import pandas as pd
from scipy import stats

from ontometer_corpus.dating import DatingConsistency, dating_consistency
from ontometer_corpus.growth_network import COHORTS, EPOCH, YEAR, assemble, days
from ontometer_corpus.metrics.abstractness import dag_closure
from ontometer_corpus.metrics.growth import (
    attachment_null,
    kaplan_meier,
    survival_at,
    top_decile_share,
)
from ontometer_corpus.network.entities import EDGE_OWN_FOREIGN, EXTERNAL
from ontometer_corpus.paths import CorpusPaths, corpus_paths

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
F_CLASS, F_INST = 8, 16
BASE_YEARS = (2010, 2015, 2020)


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _r(x: float | None, k: int = 3) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), k)


def _consistency(c: DatingConsistency) -> dict:
    """The dating check as the summary JSON reports it."""
    return {
        "n_edges_dated": c.n_pairs,
        "share_child_not_earlier": _r(c.share),
        "null_mean": _r(c.null_mean),
        "null_q975": _r(c.null_q975),
    }


def _perm_p(obs: float, null: np.ndarray) -> float:
    """Two-sided permutation p-value: distance from the null mean, either direction."""
    dev = np.abs(null - null.mean())
    return float((np.sum(dev >= abs(obs - null.mean())) + 1) / (len(null) + 1))


def _snapshot(n: int, b: np.ndarray, tau: np.ndarray, c: np.ndarray, p: np.ndarray, t: float):
    """Branch flag, normalised generality and height per ontology in G(t)."""
    alive = b <= t
    e = tau <= t
    ec, ep = c[e], p[e]
    ca = np.bincount(ep, minlength=n)
    down = dag_closure(n, ec, ep)  # n_desc: transitive specialisers
    up = dag_closure(n, ep, ec)  # depth_long from terminals: height
    n_alive = int(alive.sum())
    gen = np.log1p(down.n_desc) / np.log(max(n_alive, 2))
    return alive, ca > 0, gen, up.depth_long


def _variant(
    name: str,
    b: np.ndarray,
    eps: np.ndarray,
    dep: pd.DataFrame,
    onto: pd.DataFrame,
    as_of: float,
    reps: int,
    seed: int,
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Every measurement for one birth variant; see the module docstring."""
    n = len(b)
    c_all, p_all = dep.child.to_numpy(), dep.parent.to_numpy()
    m = ~np.isnan(eps) & ~np.isnan(b[c_all]) & ~np.isnan(b[p_all])
    c, p = c_all[m], p_all[m]
    # A specialisation arrives when it is first observed, and not before its target exists.
    tau = np.maximum(eps[m], b[p])
    dated = ~np.isnan(b)
    subj = onto.subject.to_numpy()

    first = np.full(n, np.nan)
    np.fmin.at(first, p, tau)
    at_birth = first == b
    ever = ~np.isnan(first)
    res: dict = {
        "n_dated": int(dated.sum()),
        "n_dated_subjects": int((dated & subj).sum()),
        "n_specialisations": int(m.sum()),
        "n_specialisations_undatable": int((~m).sum()),
        "n_branches_at_capture": int((dated & ever).sum()),
        "n_branch_at_first_evidence": int((dated & at_birth).sum()),
    }

    # --- leaf survival -----------------------------------------------------------
    km_rows = []
    for label, pop in (("all", dated), ("subjects", dated & subj)):
        risk = pop & ~at_birth
        dur = np.where(ever, first - b, as_of - b)[risk] / YEAR
        ev = ever[risk]
        t_km, s_km = kaplan_meier(dur, ev)
        km_rows.append(pd.DataFrame({"population": label, "years": t_km, "survival": s_km}))
        med = t_km[np.argmax(s_km <= 0.5)] if (s_km <= 0.5).any() else None
        res[f"leaf_survival_{label}"] = {
            "n_at_risk": int(risk.sum()),
            "n_became_branch": int(ev.sum()),
            "S_1y": _r(survival_at(t_km, s_km, 1)),
            "S_5y": _r(survival_at(t_km, s_km, 5)),
            "S_10y": _r(survival_at(t_km, s_km, 10)),
            "median_years": _r(med, 2) if med is not None else None,
        }
    km = pd.concat(km_rows, ignore_index=True)

    # --- calendar time: population and cohorts --------------------------------
    birth_year = pd.Series(pd.to_datetime(b, unit="D", origin=EPOCH, errors="coerce")).dt.year
    by = birth_year.to_numpy()
    y0 = int(np.nanmin(by)) if dated.any() else 2000
    years = list(range(max(y0, 2000), pd.Timestamp.today().year))
    ts = [(y, (pd.Timestamp(f"{y}-12-31") - EPOCH).days) for y in years] + [(years[-1] + 1, as_of)]
    yearly, cohort_rows, snaps = [], [], {}
    for y, t in ts:
        alive, branch, gen, height = _snapshot(n, b, tau, c, p, float(t))
        snaps[y] = (alive, branch, gen)
        for label, pop in (("all", alive), ("subjects", alive & subj)):
            yearly.append(
                {
                    "year": y,
                    "population": label,
                    "n": int(pop.sum()),
                    "n_specialisations": int((tau <= t).sum()),
                    "branch_share": _r(branch[pop].mean()) if pop.any() else None,
                    "mean_generality": _r(gen[pop].mean()) if pop.any() else None,
                    "mean_height": _r(height[pop].mean()) if pop.any() else None,
                    "max_height": int(height[pop].max()) if pop.any() else 0,
                }
            )
        for lo, hi, lab in COHORTS:
            coh = dated & (by >= lo) & (by <= hi) & (b <= t)
            if coh.sum() and y >= hi:
                cohort_rows.append(
                    {
                        "year": y,
                        "cohort": lab,
                        "n": int(coh.sum()),
                        "branch_share": _r(branch[coh].mean()),
                        "mean_generality": _r(gen[coh].mean()),
                    }
                )
    yearly_df, cohorts_df = pd.DataFrame(yearly), pd.DataFrame(cohort_rows)

    # Shift-share: change in the population mean = change within the cohort
    # already present + the effect of the ontologies that arrived since.
    last = ts[-1][0]
    shift = []
    for y0_ in BASE_YEARS:
        if y0_ not in snaps:
            continue
        a0, br0, g0 = snaps[y0_]
        a1, br1, g1 = snaps[last]
        coh = a0
        new = a1 & ~a0
        for metric, v0, v1 in (("branch_share", br0, br1), ("mean_generality", g0, g1)):
            pop0, pop1 = v0[a0].mean(), v1[a1].mean()
            within = v1[coh].mean() - v0[coh].mean()
            shift.append(
                {
                    "base_year": y0_,
                    "metric": metric,
                    "population_at_base": _r(float(pop0)),
                    "population_at_capture": _r(float(pop1)),
                    "cohort_at_base": _r(float(v0[coh].mean())),
                    "cohort_at_capture": _r(float(v1[coh].mean())),
                    "cohort_change": _r(float(within)),
                    "newcomers_at_capture": _r(float(v1[new].mean())) if new.any() else None,
                    "n_cohort": int(coh.sum()),
                    "n_newcomers": int(new.sum()),
                }
            )
    res["shift_share"] = shift

    # --- attachment null ----------------------------------------------------------
    received = np.bincount(p, minlength=n)
    obs_targets = int((received > 0).sum())
    uni = attachment_null(tau, c, b, preferential=False, reps=reps, seed=seed)
    pref = attachment_null(tau, c, b, preferential=True, reps=reps, seed=seed)
    res["attachment"] = {
        "observed_branches": obs_targets,
        "observed_top_decile_share": _r(top_decile_share(received)),
        "uniform_branches_mean": _r(float(uni.n_targets.mean()), 1),
        "uniform_branches_q025_q975": [
            int(np.quantile(uni.n_targets, 0.025)),
            int(np.quantile(uni.n_targets, 0.975)),
        ],
        "uniform_top_decile_share": _r(float(uni.top_share.mean())),
        "preferential_branches_mean": _r(float(pref.n_targets.mean()), 1),
        "preferential_branches_q025_q975": [
            int(np.quantile(pref.n_targets, 0.025)),
            int(np.quantile(pref.n_targets, 0.975)),
        ],
        "preferential_top_decile_share": _r(float(pref.top_share.mean())),
        "reps": reps,
    }
    null_df = pd.DataFrame(
        {
            "model": ["uniform"] * reps + ["preferential"] * reps,
            "branches": np.concatenate([uni.n_targets, pref.n_targets]),
            "top_decile_share": np.concatenate([uni.top_share, pref.top_share]),
        }
    )

    # --- who becomes a branch -----------------------------------------------------
    exposure = (as_of - b) / YEAR
    elig = dated & ~at_birth & (exposure >= 5)
    within5 = ever & ((first - b) / YEAR <= 5)
    who = {}
    size = np.log1p(onto.n_home_classes.to_numpy(dtype=float))
    x = onto.A_local.to_numpy(dtype=float)
    for label, pop in (("all", elig), ("subjects", elig & subj)):
        mm = pop & ~np.isnan(x)
        yes, no = x[mm & within5], x[mm & ~within5]
        if len(yes) < 5 or len(no) < 5:
            continue
        u = stats.mannwhitneyu(yes, no)
        frame = pd.DataFrame({"x": x[mm], "size": size[mm], "y": within5[mm]})
        frame["x_t"] = pd.qcut(frame.x.rank(method="first"), 3, labels=False)
        frame["size_t"] = pd.qcut(frame["size"].rank(method="first"), 3, labels=False)
        # Rank correlation of A_local with branching, net of size: correlate
        # the residuals of both ranks after regressing each on the size rank.
        rs = frame["size"].rank().to_numpy()
        rx = frame.x.rank().to_numpy()
        ry = frame.y.rank().to_numpy()
        res_x = rx - np.polyval(np.polyfit(rs, rx, 1), rs)
        res_y = ry - np.polyval(np.polyfit(rs, ry, 1), rs)
        partial = stats.pearsonr(res_x, res_y)
        who[label] = {
            "n_branched_5y": len(yes),
            "n_not": len(no),
            "A_local_median_branched": _r(float(np.median(yes))),
            "A_local_median_not": _r(float(np.median(no))),
            "mannwhitney_p": float(u.pvalue),
            "branch_rate_by_A_local_tercile": [
                _r(float(v)) for v in frame.groupby("x_t").y.mean().to_numpy()
            ],
            "branch_rate_by_size_tercile": [
                _r(float(v)) for v in frame.groupby("size_t").y.mean().to_numpy()
            ],
            "branch_rate_A_local_tercile_within_size_tercile": frame.groupby(["size_t", "x_t"])
            .y.mean()
            .unstack()
            .round(3)
            .to_numpy()
            .tolist(),
            "partial_rho_A_local_given_size": _r(float(partial.statistic)),
            "partial_p": float(partial.pvalue),
            "rho_size": _r(float(stats.spearmanr(frame["size"], frame.y).statistic)),
        }
    res["who_becomes_branch"] = who
    res["branch_rate_5y_by_registry"] = (
        pd.DataFrame({"registry": onto.registry[elig], "y": within5[elig]})
        .groupby("registry")
        .y.agg(["size", "mean"])
        .round(3)
        .reset_index()
        .to_dict("records")
    )

    per_onto = pd.DataFrame(
        {
            f"birth_{name}": pd.to_datetime(b, unit="D", origin=EPOCH, errors="coerce"),
            f"first_specialised_{name}": pd.to_datetime(
                first, unit="D", origin=EPOCH, errors="coerce"
            ),
            f"branch_at_first_evidence_{name}": dated & at_birth,
            f"Ca_dated_{name}": received,
        }
    )
    km["variant"] = name
    yearly_df["variant"] = name
    cohorts_df["variant"] = name
    null_df["variant"] = name
    return res, per_onto, yearly_df, km, cohorts_df, null_df


def _term_growth(
    edges: pd.DataFrame,
    term_home: np.ndarray,
    b: np.ndarray,
    b_first: np.ndarray,
    pair_time: dict[tuple[int, int], float],
    onto: pd.DataFrame,
) -> tuple[dict, pd.DataFrame]:
    """First foreign specialisation of each home class, over calendar time.

    A term-level specialisation is timed like the ontology-level pair it
    belongs to (``pair_time``: child ontology, home of the parent). Lags are
    measured from the home's first evidence ``b_first``, not from the bounded
    birth ``b``, which the first specialisation itself may have set.
    """
    of = edges[edges.kind == EDGE_OWN_FOREIGN]
    ph = term_home[of.parent.to_numpy()]
    keep = ph != EXTERNAL
    ev = pd.DataFrame(
        {
            "term": of.parent.to_numpy()[keep],
            "home": ph[keep],
            "child": of.onto.to_numpy()[keep].astype(np.int64),
        }
    )
    ev = ev[ev.child != ev.home]
    eps = np.array(
        [pair_time.get((c, h), np.nan) for c, h in zip(ev.child.to_numpy(), ev.home.to_numpy())]
    )
    ev["tau"] = np.maximum(eps, b[ev.home.to_numpy()])
    ev = ev.dropna(subset=["tau"])
    first = ev.groupby("term").agg(tau=("tau", "min"), home=("home", "first"))
    first["year"] = pd.to_datetime(first.tau, unit="D", origin=EPOCH).dt.year
    first["lag_years"] = (first.tau - b_first[first.home.to_numpy()]) / YEAR
    per_year = first.groupby("year").size().rename("terms_first_specialised").reset_index()
    per_year["cumulative"] = per_year.terms_first_specialised.cumsum()
    n_home = onto.n_home_classes.to_numpy(dtype=float)
    lag = first.lag_years.dropna()
    res: dict = {
        "n_terms_ever_specialised_dated": int(len(first)),
        "n_with_home_first_evidence": int(len(lag)),
        "share_specialised_by_home_first_evidence": _r(float((lag <= 0).mean())),
        "median_lag_years_when_later": _r(float(lag[lag > 0].median()), 2),
    }
    for y0 in BASE_YEARS:
        t0 = (pd.Timestamp(f"{y0}-12-31") - EPOCH).days
        coh = (b <= t0) & (n_home > 0)
        if not coh.any():
            continue
        c0 = first[first.tau <= t0].groupby("home").size().reindex(range(len(b)), fill_value=0)
        c1 = first.groupby("home").size().reindex(range(len(b)), fill_value=0)
        us0 = (c0.to_numpy() / np.maximum(n_home, 1))[coh]
        us1 = (c1.to_numpy() / np.maximum(n_home, 1))[coh]
        res[f"cohort_{y0}_upper_share"] = {
            "n": int(coh.sum()),
            "mean_at_base": _r(float(us0.mean()), 4),
            "mean_at_capture": _r(float(us1.mean()), 4),
            "share_gaining_any_term": _r(float((us1 > us0).mean())),
        }
    return res, per_year


def _role_order_terms(term: np.ndarray, tu: np.ndarray, cls: np.ndarray) -> pd.DataFrame:
    """Per term: promotion, demotion, and the sign of the later-minus-earlier class share.

    Rows must be sorted by term. A *promotion*: every earliest user of a term
    uses it as an instance and some later user as a class; a *demotion* the
    reverse. The within-term comparison splits each term's users at the
    median arrival and compares the class share of the two halves.
    """
    u = pd.DataFrame({"term": term, "tu": tu, "cls": cls}).sort_values(
        ["term", "tu"], kind="stable"
    )
    g = u.groupby("term", sort=False)
    t_min = g.tu.transform("min")
    first = u[u.tu == t_min].groupby("term").cls.agg(["min", "max"])
    later = u[u.tu > t_min].groupby("term").cls.agg(["min", "max"])
    j = first.join(later, rsuffix="_later", how="left")
    out = pd.DataFrame(index=first.index)
    out["promotion"] = (j["max"] == 0) & (j["max_later"] == 1)
    out["demotion"] = (j["min"] == 1) & (j["min_later"] == 0)
    n_use = g.cls.transform("size").to_numpy()
    late = g.cumcount().to_numpy() >= n_use / 2
    per = u.assign(late=late).groupby(["term", "late"]).cls.mean().unstack()
    out["shift"] = np.sign((per.get(True) - per.get(False)).reindex(out.index)).fillna(0)
    return out


def _role_order(term: np.ndarray, tu: np.ndarray, cls: np.ndarray) -> tuple[int, int, int, int]:
    """Totals of :func:`_role_order_terms`: promotions, demotions, later more / less class."""
    t = _role_order_terms(term, tu, cls)
    return (
        int(t.promotion.sum()),
        int(t.demotion.sum()),
        int((t["shift"] > 0).sum()),
        int((t["shift"] < 0).sum()),
    )


def _sense_c(
    docs: pd.DataFrame,
    term_home: np.ndarray,
    b_user: np.ndarray,
    b_home: np.ndarray,
    reps: int,
    seed: int,
) -> tuple[dict, pd.DataFrame]:
    """Class-role share of foreign uses, by how late the using ontology arrived.

    A use is one ontology using a term homed elsewhere in exactly one role
    (class or instance). Users are placed in time by their first evidence.
    The order test compares promotions with demotions, and later with
    earlier users, against the same uses with arrival order permuted within
    each term: under that null the roles of a term carry no time trend. The
    terms of one home ontology share their users, so the sign test that
    counts home ontologies is the one to read; the term-level permutation
    treats terms as independent.
    """
    home = term_home[docs.term.to_numpy()]
    user = docs.onto.to_numpy()
    fl = docs["flags"].to_numpy()
    is_c, is_i = (fl & F_CLASS) > 0, (fl & F_INST) > 0
    m = (home != EXTERNAL) & (home != user) & (is_c ^ is_i)
    u = pd.DataFrame(
        {"term": docs.term.to_numpy()[m], "home": home[m], "user": user[m], "cls": is_c[m]}
    )
    u["tu"] = b_user[u.user.to_numpy()]
    u["th"] = b_home[u.home.to_numpy()]
    u = u.dropna(subset=["tu", "th"])
    u["lag"] = (u.tu - u.th) / YEAR
    bins = [-np.inf, 0, 2, 5, 10, np.inf]
    labels = ["user first attested earlier", "0-2y", "2-5y", "5-10y", ">10y"]
    u["lag_bin"] = pd.cut(u.lag, bins=bins, labels=labels, right=True)
    by_lag = (
        u.groupby("lag_bin", observed=True)
        .agg(uses=("cls", "size"), class_share=("cls", "mean"), terms=("term", "nunique"))
        .reset_index()
    )
    # Only terms used in both roles can move either way.
    mixed = u.groupby("term").cls.transform(lambda s: s.any() and not s.all()).astype(bool)
    mx = u[mixed].sort_values("term", kind="stable")
    term, tu, cls = mx.term.to_numpy(), mx.tu.to_numpy(), mx.cls.to_numpy()
    per_term = _role_order_terms(term, tu, cls)
    promo, demo = int(per_term.promotion.sum()), int(per_term.demotion.sum())
    up, down = int((per_term["shift"] > 0).sum()), int((per_term["shift"] < 0).sum())
    # Terms of one home ontology, used by the same few ontologies, are not
    # independent: the home is the unit of the sign test.
    by_home = (
        per_term.join(mx.groupby("term").home.first())
        .groupby("home")
        .agg(p=("promotion", "sum"), d=("demotion", "sum"), n=("promotion", "size"))
    )
    h_up, h_down = int((by_home.p > by_home.d).sum()), int((by_home.p < by_home.d).sum())
    rng = np.random.default_rng(seed)
    null = []
    for _ in range(reps):
        perm = np.lexsort((rng.random(len(term)), term))
        null.append(_role_order(term, tu[perm], cls))
    null_a = np.array(null, dtype=float)
    stat_obs = promo - demo
    stat_null = null_a[:, 0] - null_a[:, 1]
    sw_obs = up - down
    sw_null = null_a[:, 2] - null_a[:, 3]
    res = {
        "n_foreign_role_uses_dated": int(len(u)),
        "n_terms": int(u.term.nunique()),
        "n_terms_used_in_both_roles": int(mx.term.nunique()),
        "class_share_all": _r(float(u.cls.mean())),
        "promotions": promo,
        "demotions": demo,
        "null_promotions_mean": _r(float(null_a[:, 0].mean()), 1),
        "null_demotions_mean": _r(float(null_a[:, 1].mean()), 1),
        "promotion_minus_demotion_p": _perm_p(stat_obs, stat_null),
        "within_term_later_more_class": up,
        "within_term_later_less_class": down,
        "null_later_more_class_mean": _r(float(null_a[:, 2].mean()), 1),
        "null_later_less_class_mean": _r(float(null_a[:, 3].mean()), 1),
        "within_term_p": _perm_p(sw_obs, sw_null),
        "n_home_ontologies": int(len(by_home)),
        "largest_home_share_of_terms": _r(float(by_home.n.max() / by_home.n.sum())),
        "homes_more_promotions": h_up,
        "homes_more_demotions": h_down,
        "homes_sign_p": float(stats.binomtest(h_up, h_up + h_down).pvalue)
        if h_up + h_down
        else None,
        "reps": reps,
    }
    return res, by_lag


def _panel_pair_times(
    paths: CorpusPaths, terms: pd.DataFrame, dep: pd.DataFrame
) -> tuple[dict[tuple[int, int], float], dict]:
    """When each specialisation pair first appears across the release panel.

    For release families in the panel, every selected release is read and the
    pairs (family, parent ontology) it asserts are recorded with the release
    date; a pair seen at capture gets the date of the first release asserting
    it. Also returns how many pairs seen at capture were already asserted in
    the family's first panel release — the share for which dating a
    specialisation by its child's birth is right.
    """
    import pyarrow.parquet as pq

    panel_file = paths.cache_dir / "panel_releases.parquet"
    if not panel_file.exists():
        return {}, {"available": False}
    panel = pd.read_parquet(panel_file).sort_values(["family_onto", "release_index"])
    panel["t"] = days(panel.release_date)
    later_parents = dep.groupby("child").parent.agg(set)
    panel = panel[panel.family_onto.isin(later_parents.index)]
    home_of = pd.Series(terms.home.to_numpy(), index=terms.iri.to_numpy())
    home_of = home_of[~home_of.index.duplicated()]
    times: dict[tuple[int, int], float] = {}
    in_first = total = fams = 0
    for o, rel in panel.groupby("family_onto"):
        o = int(o)
        seen_first: set[int] | None = None
        for r in rel.itertuples():
            side = paths.sidecar_path(r.file_hash)
            if not side.exists() or np.isnan(r.t):
                continue
            t = pq.read_table(side).to_pandas()
            e = t[t.record_type == "subclass_edge"]
            ch = home_of.reindex(e.subject.to_numpy()).to_numpy()
            ph = home_of.reindex(e.object.to_numpy()).to_numpy()
            ok = (ch == o) & ~np.isnan(ph) & (ph != o) & (ph != EXTERNAL)
            parents = {int(x) for x in ph[ok]} & later_parents[o]
            if seen_first is None:
                seen_first = parents
            for pp in parents:
                times.setdefault((o, pp), float(r.t))
        if seen_first is None:
            continue
        fams += 1
        total += len(later_parents[o])
        in_first += len(seen_first)
    span = panel.groupby("family_onto").t.agg(lambda s: (s.max() - s.min()) / YEAR)
    check = {
        "available": True,
        "families": fams,
        "pairs_at_capture": total,
        "pairs_in_first_release": in_first,
        "share_in_first_release": _r(in_first / total) if total else None,
        "pairs_timed_by_panel": len(times),
        "median_years_first_to_last_release": _r(float(span.median()), 2) if len(span) else None,
    }
    return times, check


@dataclass
class GrowthInputs:
    """The ontology specialisation network at capture, placed in time.

    ``dep`` holds one row per (child, parent) pair with ``n_terms``, the
    number of term-level specialisations behind it. ``eps`` is each pair's
    arrival time (first panel release asserting it, else the child's first
    evidence); ``b_obs`` and ``b_bnd`` are first-evidence and bounded births,
    in days since the epoch, indexed by ontology.
    """

    onts: pd.DataFrame
    onto: pd.DataFrame
    terms: pd.DataFrame
    edges: pd.DataFrame
    dates: pd.DataFrame
    dep: pd.DataFrame
    b_obs: np.ndarray
    b_bnd: np.ndarray
    eps: np.ndarray
    by_panel: np.ndarray
    panel_check: dict
    as_of: float

    @property
    def term_home(self) -> np.ndarray:
        return self.terms.home.to_numpy()

    @property
    def pair_time(self) -> dict[tuple[int, int], float]:
        return {
            (int(c), int(p)): float(e)
            for c, p, e in zip(self.dep.child, self.dep.parent, self.eps)
            if not np.isnan(e)
        }


def load_timed_network(paths: CorpusPaths) -> GrowthInputs:
    """Load the entity network and date its ontologies and specialisation pairs."""
    d = paths.cache_dir
    onts = pd.read_parquet(d / "entity_ontologies.parquet")
    oa = pd.read_parquet(paths.ontology_abstractness_path)
    dates = pd.read_parquet(paths.ontology_dates_path)
    terms = pd.read_parquet(d / "entity_terms.parquet", columns=["term", "iri", "home"])
    edges = pd.read_parquet(d / "entity_edges.parquet")
    term_home = terms.home.to_numpy()

    base = onts[["onto", "family", "registry", "source_id", "release_date"]].merge(
        oa[["onto", "subject", "A_local", "n_home_classes", "community"]], on="onto"
    )

    of = edges[edges.kind == EDGE_OWN_FOREIGN]
    ph = term_home[of.parent.to_numpy()]
    dep = (
        pd.DataFrame({"child": of.onto.to_numpy()[ph != EXTERNAL], "parent": ph[ph != EXTERNAL]})
        .value_counts()
        .rename("n_terms")
        .reset_index()
        .sort_values(["child", "parent"])
    )
    dep = dep[dep.child != dep.parent].reset_index(drop=True)

    # The first panel release asserting each pair, where the child's releases
    # are in the panel. Written with the pairs as a small table in the data
    # directory, from which G(t) can be rebuilt without the object store.
    panel_times, panel_check = _panel_pair_times(paths, terms, dep)
    pairs = list(zip(dep.child.to_numpy(), dep.parent.to_numpy()))
    by_panel = np.array([pair in panel_times for pair in pairs])
    first_days = pd.Series([panel_times.get(pair, np.nan) for pair in pairs], dtype=float)
    family = base.set_index("onto").family
    spec = dep.assign(
        child_family=family.reindex(dep.child).to_numpy(),
        parent_family=family.reindex(dep.parent).to_numpy(),
        panel_first=(EPOCH + pd.to_timedelta(first_days, unit="D")).dt.strftime("%Y-%m-%d"),
    )
    spec.to_parquet(paths.ontology_specialisations_path, index=False)

    net = assemble(base, spec, dates)
    onto = net.onto.merge(dates[["family", "birth_source", "as_of"]], on="family", how="left")
    b_obs, b_bnd, eps, as_of = net.b_obs, net.b_bnd, net.eps, net.as_of
    return GrowthInputs(
        onts=onts,
        onto=onto,
        terms=terms,
        edges=edges,
        dates=dates,
        dep=dep,
        b_obs=b_obs,
        b_bnd=b_bnd,
        eps=eps,
        by_panel=by_panel,
        panel_check=panel_check,
        as_of=as_of,
    )


def build(paths: CorpusPaths, out: Path, reps: int, seed: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out.mkdir(parents=True, exist_ok=True)
    net = load_timed_network(paths)
    onto, dep, dates, edges = net.onto, net.dep, net.dates, net.edges
    b_obs, b_bnd, eps, as_of = net.b_obs, net.b_bnd, net.eps, net.as_of
    term_home, pair_time = net.term_home, net.pair_time
    by_panel, panel_check = net.by_panel, net.panel_check
    d = paths.cache_dir

    summary: dict = {
        "as_of": str(dates.as_of.iloc[0]),
        "n_ontologies": len(onto),
        "n_specialisation_pairs": len(dep),
        "edge_timing": {**panel_check, "pairs_timed_by_panel_in_network": int(by_panel.sum())},
        "dating": {
            "dated_first_evidence": int((~np.isnan(b_obs)).sum()),
            "dated_bounded": int((~np.isnan(b_bnd)).sum()),
            "moved_earlier_by_bound": int((b_bnd < b_obs).sum()),
            "by_source": onto.birth_source.value_counts().to_dict(),
            "by_registry": onto.assign(d=~np.isnan(b_obs))
            .groupby("registry")
            .d.mean()
            .round(3)
            .to_dict(),
            "consistency_first_evidence": _consistency(
                dating_consistency(b_obs, dep.child.to_numpy(), dep.parent.to_numpy(), seed=seed)
            ),
        },
    }
    print("edge timing", summary["edge_timing"])
    print(json.dumps(summary["dating"], indent=1, default=str))

    per_onto = onto[["onto", "family", "registry", "subject"]].copy()
    yearly_all, km_all, coh_all, null_all = [], [], [], []
    for name, b in (("bounded", b_bnd), ("first_evidence", b_obs)):
        res, po, yearly, km, coh, null = _variant(name, b, eps, dep, onto, as_of, reps, seed)
        summary[name] = res
        per_onto = pd.concat([per_onto, po], axis=1)
        yearly_all.append(yearly)
        km_all.append(km)
        coh_all.append(coh)
        null_all.append(null)
        print(name, json.dumps(res, default=str)[:2500])

    summary["terms"], term_years = _term_growth(edges, term_home, b_bnd, b_obs, pair_time, onto)
    print("terms", summary["terms"])
    docs = pd.read_parquet(d / "entity_docs.parquet")
    summary["sense_c"], lag = _sense_c(docs, term_home, b_obs, b_bnd, reps, seed)
    print("sense C", summary["sense_c"])
    del docs

    per_onto.to_parquet(paths.corpus_dir / "abstractness_growth.parquet", index=False)
    yearly = pd.concat(yearly_all, ignore_index=True)
    km = pd.concat(km_all, ignore_index=True)
    coh = pd.concat(coh_all, ignore_index=True)
    null = pd.concat(null_all, ignore_index=True)
    yearly.to_csv(out / "growth_yearly.csv", index=False)
    coh.to_csv(out / "growth_cohorts.csv", index=False)
    km.to_csv(out / "growth_leaf_survival.csv", index=False)
    null.to_csv(out / "growth_attachment_null.csv", index=False)
    term_years.to_csv(out / "growth_terms_per_year.csv", index=False)
    lag.to_csv(out / "growth_sense_c_by_lag.csv", index=False)
    (out / "growth_summary.json").write_text(json.dumps(summary, indent=1, default=str))

    # --- figures -----------------------------------------------------------------
    v = "bounded"
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9), dpi=150, facecolor=SURFACE)
    ax = axes[0]
    _style(ax)
    ys = yearly[(yearly.variant == v) & (yearly.population == "all") & (yearly.year >= 2008)]
    ax.plot(ys.year, ys.branch_share, color=INK2, linewidth=2.2, label="whole corpus at t")
    for (lab, g), col in zip(
        coh[coh.variant == v].groupby("cohort", sort=False), (BLUE, ORANGE, AQUA)
    ):
        ax.plot(g.year, g.branch_share, color=col, linewidth=1.6, label=f"born {lab}")
    ax.set_ylabel("share specialised by some ontology", color=INK, fontsize=10)
    ax.set_xlabel("year", color=INK, fontsize=10)
    ax.set_ylim(0, None)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2)
    ax.set_title(
        "Share of ontologies that are branches in G(t)", color=INK, fontsize=10, loc="left"
    )
    ax = axes[1]
    _style(ax)
    ax2 = ax.twinx()
    ax.bar(ys.year, ys.n, color=GRID, width=0.8, label="ontologies in G(t)")
    ax2.plot(ys.year, ys.mean_height, color=BLUE, linewidth=2, label="mean height")
    ax2.plot(ys.year, ys.max_height, color=ORANGE, linewidth=1.6, label="max height")
    ax.set_ylabel("ontologies in G(t)", color=INK, fontsize=10)
    ax2.set_ylabel("specialisers beneath (chain length)", color=INK, fontsize=10)
    ax2.tick_params(colors=INK2, labelsize=9)
    for s in ("top",):
        ax2.spines[s].set_visible(False)
    ax.set_xlabel("year", color=INK, fontsize=10)
    ax2.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    ax.set_title("Size and height of G(t)", color=INK, fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out / "growth_branch_share.png", facecolor=SURFACE)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=150, facecolor=SURFACE)
    _style(ax)
    for (lab, g), col in zip(km[km.variant == v].groupby("population", sort=False), (BLUE, ORANGE)):
        ax.step(
            np.r_[0, g.years],
            np.r_[1, g.survival],
            where="post",
            color=col,
            linewidth=1.8,
            label=lab,
        )
    ax.set_xlim(0, 15)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("years since first evidence", color=INK, fontsize=10)
    ax.set_ylabel("P(still terminal)", color=INK, fontsize=10)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK2)
    ax.set_title(
        "Time from first evidence to first specialiser", color=INK, fontsize=10, loc="left"
    )
    fig.tight_layout()
    fig.savefig(out / "growth_leaf_survival.png", facecolor=SURFACE)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 3.8), dpi=150, facecolor=SURFACE)
    _style(ax)
    nv = null[null.variant == v]
    for (lab, g), col in zip(nv.groupby("model", sort=False), (BLUE, AQUA)):
        ax.hist(
            g.branches, bins=25, color=col, alpha=0.75, label=f"{lab} attachment", edgecolor=SURFACE
        )
    ax.axvline(
        summary[v]["attachment"]["observed_branches"], color=ORANGE, linewidth=2.2, label="observed"
    )
    ax.set_xlabel("ontologies ever specialised (leaves turned branches)", color=INK, fontsize=10)
    ax.set_ylabel("replicates", color=INK, fontsize=10)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2)
    ax.set_title(
        "Leaves turned branches: observed against re-targeted arrivals",
        color=INK,
        fontsize=10,
        loc="left",
    )
    fig.tight_layout()
    fig.savefig(out / "growth_attachment_null.png", facecolor=SURFACE)
    plt.close(fig)
    print(f"Wrote {out}")


@click.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for tables and figures.",
)
@click.option("--reps", type=int, default=200, show_default=True, help="Null-model replicates.")
@click.option(
    "--seed", type=int, default=0, show_default=True, help="Random seed of the null models."
)
@corpus_paths
def main(out: Path, reps: int, seed: int, paths: CorpusPaths) -> None:
    """Abstractness as the corpus grows: G(t)."""
    paths.require_store_dir()
    build(paths, out, reps, seed)


if __name__ == "__main__":
    main()
