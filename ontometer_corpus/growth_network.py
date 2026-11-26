"""The ontology specialisation network placed in time, from tables in the corpus directory.

Three small tables in the corpus directory are enough to rebuild G(t), the
specialisation network of ontologies at any time t, without the object store:

``ontology_abstractness.parquet``
    one row per ontology (index ``onto``): release family, registry, captured
    release date, reuse community, population flags (``subject`` marks the
    measured ontologies).
``ontology_dates.parquet``
    first-evidence date per release family (``ontometer-corpus dates``).
``ontology_specialisations.parquet``
    one row per (child, parent) pair of ontologies where a class homed in the
    child is asserted, by the child, under a class homed in the parent; with
    ``n_terms`` (term-level specialisations behind the pair) and
    ``panel_first`` (the first release of the child asserting it, where the
    child's releases are in the release panel). Written by
    ``ontometer-corpus growth``, which derives it from the entity network.

:func:`load_tracked` reads them and places ontologies and pairs in time
(:func:`ontometer_corpus.dating.time_pairs`); :func:`viz_payload` turns the
result into the data behind the interactive G(t) view, with the Poincaré-disk
layout of :mod:`ontometer_corpus.network.hyperbolic`.
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("tables")


import ast
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ontometer_corpus.dating import time_pairs
from ontometer_corpus.metrics.abstractness import dag_closure
from ontometer_corpus.network.hyperbolic import DEFAULT_R_MAX, primary_parents, radial_angles
from ontometer_corpus.paths import CorpusPaths

EPOCH = pd.Timestamp("1970-01-01")
YEAR = 365.25
#: Birth cohorts followed forward in time: (first year, last year, label).
COHORTS = [(0, 2009, "before 2010"), (2010, 2014, "2010-14"), (2015, 2019, "2015-19")]


def days(values: pd.Series) -> np.ndarray:
    """Days since the epoch of date-like values.

    Args:
        values: dates, date strings or missing values.

    Returns:
        A float array of days since 1970-01-01; NaN where missing or unparseable.
    """
    return ((pd.to_datetime(values, errors="coerce") - EPOCH).dt.days).to_numpy(dtype=float)


def decimal_year(d: np.ndarray | float) -> np.ndarray:
    """Decimal year of days since the epoch.

    Args:
        d: days since 1970-01-01.

    Returns:
        The decimal year, as an array (NaN stays NaN).
    """
    return 1970.0 + np.asarray(d, dtype=float) / YEAR


@dataclass
class TimedNetwork:
    """Ontologies and specialisation pairs with their times, in days since the epoch.

    Attributes:
        onto: one row per ontology, positionally indexed by ``onto``.
        dep: one row per (child, parent) pair: ``child``, ``parent``, ``n_terms``.
        b_obs: first-evidence birth per ontology.
        b_bnd: birth bounded by the first observed specialisation of its classes.
        eps: arrival per pair, before the rule that a pair never predates its parent.
        as_of: capture date, in days since the epoch.
        as_of_label: capture date as given in the dates table (``YYYY-MM-DD``).
    """

    onto: pd.DataFrame
    dep: pd.DataFrame
    b_obs: np.ndarray
    b_bnd: np.ndarray
    eps: np.ndarray
    as_of: float
    as_of_label: str


def assemble(onto: pd.DataFrame, dep: pd.DataFrame, dates: pd.DataFrame) -> TimedNetwork:
    """Place ontologies and pairs in time.

    Args:
        onto: ``onto`` (0..n-1), ``family``, ``release_date`` and any other columns to keep.
        dep: ``child``, ``parent``, ``n_terms``, ``panel_first`` (date or missing).
        dates: ``family``, ``birth``, ``as_of`` (the same capture date on every row).

    Returns:
        The network with first-evidence and bounded births per ontology and
        arrival times per pair.

    Raises:
        ValueError: if ``onto`` does not run 0..n-1 after sorting.
    """
    onto = onto.merge(dates[["family", "birth"]], on="family", how="left")
    onto = onto.sort_values("onto").reset_index(drop=True)
    if not (onto.onto.to_numpy() == np.arange(len(onto))).all():
        raise ValueError("ontology index must run 0..n-1")
    b_obs = days(onto.birth)
    b_rep = days(onto.release_date)
    child, parent = dep.child.to_numpy(), dep.parent.to_numpy()
    eps, b_bnd = time_pairs(b_obs, b_rep, child, parent, days(dep.panel_first))
    as_of_label = str(dates.as_of.iloc[0])
    return TimedNetwork(
        onto=onto,
        dep=dep[["child", "parent", "n_terms"]].reset_index(drop=True),
        b_obs=b_obs,
        b_bnd=b_bnd,
        eps=eps,
        as_of=float((pd.Timestamp(as_of_label) - EPOCH).days),
        as_of_label=as_of_label,
    )


def load_tracked(paths: CorpusPaths) -> TimedNetwork:
    """Load G(t) from the three tables in the corpus directory (see the module docstring).

    Args:
        paths: corpus locations; only the corpus tables are read.

    Returns:
        The network placed in time, as :func:`assemble` returns it.
    """
    onto = pd.read_parquet(paths.ontology_abstractness_path)
    dates = pd.read_parquet(paths.ontology_dates_path)
    dep = pd.read_parquet(paths.ontology_specialisations_path)
    return assemble(onto, dep, dates)


def labels(db_path: Path, onto: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Short label and longer name per ontology, from registry metadata.

    The label is the registry acronym or vocabulary prefix where there is one;
    the name is the registry's title, else the ontology IRI, else the family.

    Args:
        db_path: the corpus SQLite database (``corpus.db``).
        onto: needs ``id`` (the registry row), ``family`` and ``ontology_iri``.

    Returns:
        ``(short, long)``, one entry per row of ``onto``: labels capped at 40
        characters and names capped at 120.
    """
    conn = sqlite3.connect(str(db_path))
    meta = dict(conn.execute("SELECT id, source_registry_metadata FROM corpus_registry"))
    conn.close()
    short, long = [], []
    for row_id, family, iri_value in zip(onto.id, onto.family, onto.ontology_iri):
        try:
            m = json.loads(meta.get(row_id) or "{}")
        except ValueError:
            m = {}
        rec = m.get("ontology")
        if isinstance(rec, str):
            try:
                rec = ast.literal_eval(rec)
            except (ValueError, SyntaxError):
                rec = None
        name = rec.get("name") if isinstance(rec, dict) else None
        tail = family.split(":", 1)[-1].rstrip("/#").rsplit("/", 1)[-1]
        label = (
            m.get("_acronym")
            or m.get("_prefix")
            or (str(m["id"]).upper() if m.get("id") else None)
            or m.get("title")
            or tail.removesuffix(".owl")
        )
        short.append(str(label)[:40])
        iri = iri_value if isinstance(iri_value, str) else None
        long.append(str(name or m.get("title") or iri or family)[:120])
    return short, long


def viz_payload(
    net: TimedNetwork,
    short: list[str],
    long: list[str],
    step: float = 0.25,
    series_from: float = 2005.0,
) -> dict:
    """The data behind the interactive G(t) view.

    Ontologies are shown from their bounded birth; undated ones are left out
    and counted. Each ontology's angle is fixed by a radial tree over its
    primary parents at capture; the page computes its radius at time t from
    ``steps``, the times at which its number of transitive specialisers
    changes. ``series`` holds the share of ontologies in G(t) that some
    ontology specialises, for the whole network and for birth cohorts.

    Args:
        net: the network placed in time.
        short: short label per ontology (see :func:`labels`).
        long: longer name per ontology.
        step: spacing of the time grid, in years; radii and the series are
            evaluated at these times and at the capture date.
        series_from: first decimal year included in ``series``; earlier grid
            points are still used for ``steps``.

    Returns:
        A JSON-serialisable dict with ``meta``, ``nodes`` (label, name,
        registry, ``subject`` flag, birth, angle, first specialisation and
        radius steps), ``edges`` (``[child, parent, arrival year, n_terms]``
        over node indices) and ``series``.
    """
    onto, dep, b = net.onto, net.dep, net.b_bnd
    n = len(onto)
    c_all, p_all = dep.child.to_numpy(), dep.parent.to_numpy()
    timed = ~np.isnan(net.eps) & ~np.isnan(b[c_all]) & ~np.isnan(b[p_all])
    c, p = c_all[timed], p_all[timed]
    tau_y = decimal_year(np.maximum(net.eps[timed], b[p]))
    w = dep.n_terms.to_numpy()[timed]
    shown = ~np.isnan(b)
    b_y = decimal_year(b)

    pp = primary_parents(n, c, p, w)
    pp[~shown] = -1
    angle = radial_angles(pp, onto.community.fillna(-1).to_numpy(dtype=np.int64))

    end = float(decimal_year(net.as_of))
    times = np.arange(float(np.floor(np.nanmin(b_y))), end + step, step)
    times[-1] = end
    counts = np.zeros((len(times), n), dtype=np.int64)
    alive = np.zeros((len(times), n), dtype=bool)
    branch = np.zeros((len(times), n), dtype=bool)
    for k, t in enumerate(times):
        e = tau_y <= t
        counts[k] = dag_closure(n, c[e], p[e]).n_desc
        alive[k] = b_y <= t
        branch[k] = np.bincount(p[e], minlength=n) > 0

    first = np.full(n, np.nan)
    np.fmin.at(first, p, tau_y)
    nodes = []
    index = np.full(n, -1, dtype=np.int64)
    for v in np.flatnonzero(shown):
        index[v] = len(nodes)
        col = counts[:, v]
        change = np.flatnonzero(np.r_[True, np.diff(col) != 0])
        nodes.append(
            {
                "label": short[v],
                "name": long[v],
                "registry": onto.registry.iloc[v],
                "subject": bool(onto.subject.iloc[v]),
                "birth": round(float(b_y[v]), 3),
                "angle": round(float(angle[v]), 5),
                "first": None if np.isnan(first[v]) else round(float(first[v]), 3),
                "steps": [[round(float(times[k]), 3), int(col[k])] for k in change if col[k] > 0],
            }
        )
    edges = [
        [int(index[ci]), int(index[pi]), round(float(ti), 3), int(wi)]
        for ci, pi, ti, wi in zip(c, p, tau_y, w)
        if index[ci] >= 0 and index[pi] >= 0
    ]

    by = np.floor(b_y)
    series: dict = {"years": [], "corpus": [], "cohorts": {lab: [] for _, _, lab in COHORTS}}
    for k, t in enumerate(times):
        if t < series_from:
            continue
        a = alive[k] & shown
        series["years"].append(round(float(t), 3))
        series["corpus"].append(round(float(branch[k][a].mean()), 4) if a.any() else None)
        for lo, hi, lab in COHORTS:
            coh = shown & (by >= lo) & (by <= hi) & alive[k]
            ok = coh.any() and t >= hi + 1
            series["cohorts"][lab].append(round(float(branch[k][coh].mean()), 4) if ok else None)

    return {
        "meta": {
            "as_of": net.as_of_label,
            "as_of_year": round(end, 3),
            "n_ontologies": n,
            "n_shown": int(shown.sum()),
            "n_undated": int((~shown).sum()),
            "n_edges": len(edges),
            # Radius normaliser: the most specialised ontology at capture sits at the centre.
            "n_final": int(counts[-1].max()) + 1,
            "r_max": DEFAULT_R_MAX,
            "start_year": round(float(times[0]), 3),
        },
        "nodes": nodes,
        "edges": edges,
        "series": series,
    }


def build_viz(paths: CorpusPaths, step: float = 0.25) -> dict:
    """:func:`viz_payload` for the corpus: tables in the corpus directory plus ``corpus.db``.

    Args:
        paths: corpus locations.
        step: spacing of the time grid, in years.

    Returns:
        The payload of :func:`viz_payload`.
    """
    net = load_tracked(paths)
    short, long = labels(paths.db_path, net.onto)
    return viz_payload(net, short, long, step=step)
