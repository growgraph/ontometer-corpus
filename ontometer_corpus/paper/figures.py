"""
Publication figures and headline numbers of the corpus, from the corpus tables.

Part of the resource paper's analysis. Every figure is drawn from the
corpus tables and the per-ontology metric table
(``ontometer_corpus.ontology_metrics``), sized for a single-column LNCS page
(4.8 in text width) in the shared style of ``figstyle``, and without
titles, which the captions carry. The numbers the text quotes are computed by
the code that draws the figure they belong to and written as LaTeX macros.

Reads:  ``<corpus>/corpus.db``, the network tables, ``namespace_usage``,
        ``ontology_metrics`` and the three G(t) tables; with ``--store``, the
        term-graph tables in the store's cache, for the size of T; with
        ``--comparators DIR``, the reuse networks of other kinds that
        ``ontometer-corpus paper comparators fetch`` stored there.
Writes: ``<out>/<name>.pdf`` and ``<out>/<name>.png`` per figure, and
        ``<out>/stats.tex`` with one ``\\statdef{key}{value}`` line per number,
        ``<out>/hubs.tex``, the table of the most reused ontologies,
        ``<out>/registries.tex``, the funnel from listed records to unique
        ontologies per registry, and with ``--comparators``
        ``<out>/comparators.tex``, reuse inequality beside the other networks.
        The power-law goodness-of-fit bootstrap is cached in
        ``CACHE_DIR/powerlaw_gof.json`` (see ``figstyle``).

Usage:
    ontometer-corpus paper figures --out /path/to/figures --comparators DIR
    ontometer-corpus paper figures --out /path/to/figures --only poincare_gt
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
import warnings
from collections import defaultdict
from collections.abc import Callable
from functools import cached_property
from pathlib import Path

import click
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import networkx as nx
import numpy
import pandas as pd
import powerlaw
import seaborn as sns
from matplotlib.collections import LineCollection
from matplotlib.colors import LogNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon, Rectangle
from matplotlib.ticker import FixedFormatter, FixedLocator, NullFormatter
from scipy.spatial import ConvexHull

from ontometer_corpus.dating import dating_consistency
from ontometer_corpus.growth_network import build_viz, load_tracked
from ontometer_corpus.network.entities import EDGE_COPY, EXTERNAL
from ontometer_corpus.network.hyperbolic import poincare_radius
from ontometer_corpus.ontology_metrics import (
    SKOS_DOMINATED_KINDS,
    NetworkViews,
    external_ratio,
    latest_files,
    load_ontology_metrics,
    load_views,
    logical_namespaces,
    structural_population,
)
from ontometer_corpus.paper.comparators import LABELS, ReuseNetwork
from ontometer_corpus.paper.comparators import load as load_comparators
from ontometer_corpus.paper.figstyle import (
    BRAND_BLUE,
    CACHE_DIR,
    DIVERGING_CMAP,
    FULL,
    HALF,
    INK,
    KIND_PALETTE,
    NEUTRAL,
    NEUTRAL_LIGHT,
    OTHER,
    SCATTER_SLOTS,
    SEQUENTIAL_CMAP,
    STATUS_CRITICAL,
    STATUS_SERIOUS,
    STATUS_WARNING,
    SURFACE,
    TEXT_MUTED,
    fmt_dec,
    fmt_int,
    fmt_p,
    fmt_pct,
    ink_on,
    panel,
    reference_line,
    registry_colors,
    registry_groups,
    save,
    style,
    tint,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.versions import family_key

GOF_CACHE = CACHE_DIR / "powerlaw_gof.json"
N_BOOTSTRAP = 500
BOOTSTRAP_SEED = 0

# Years at which G(t) is drawn, besides the capture date.
GT_YEARS = (2010.0, 2018.0)
# The two registries whose ontologies reuse almost only each other.
COMMUNITIES = ("LOV", "OBO")


# ---------------------------------------------------------------- formatting


def _gini(values) -> float:
    """Gini coefficient of non-negative values: 0 equal, 1 maximally concentrated."""
    arr = numpy.sort(numpy.asarray(values, dtype=float))
    n = len(arr)
    if n == 0 or arr.sum() == 0:
        return 0.0
    cum = numpy.cumsum(arr)
    return float((n + 1 - 2 * numpy.sum(cum) / cum[-1]) / n)


def _short_label(iri: str, maxlen: int = 40) -> str:
    """Last informative path segment(s) of an IRI (``.../foaf/0.1/`` -> ``foaf/0.1``)."""
    uninformative = {
        "core",
        "terms",
        "ns",
        "schema",
        "ontology",
        "ontologies",
        "vocab",
        "vocabulary",
        "owl",
        "rdf",
        "elements",
        "def",
        "index",
    }
    s = iri.removeprefix("external:").rstrip("/#")
    if "://" not in s:
        return s[:maxlen]
    host, _, path = s.split("://", 1)[1].partition("/")
    parts = [p for p in path.replace("#", "/").split("/") if p]
    if not parts:
        return host[:maxlen]
    keep = [parts.pop()]
    while parts and (
        keep[0].replace(".", "").replace("v", "").isdigit() or keep[0].lower() in uninformative
    ):
        keep.insert(0, parts.pop())
    return "/".join(keep)[:maxlen]


def registry_order(db_path: Path) -> list[str]:
    """Home registries by the number of unique ontologies (latest versions) they hold."""
    conn = sqlite3.connect(db_path)
    try:
        latest = pd.read_sql_query(
            "SELECT source_registry, home_registry FROM corpus_registry WHERE is_latest_version = 1",
            conn,
        )
    finally:
        conn.close()
    return _registry_counts(latest).index.tolist()


def _registry_counts(latest: pd.DataFrame) -> pd.DataFrame:
    return (
        pd.DataFrame(
            {
                "n_ontologies": latest["home_registry"].value_counts(),
                "latest_copy_fetched_from": latest["source_registry"].value_counts(),
            }
        )
        .fillna(0)
        .astype(int)
        .sort_values("n_ontologies", ascending=False)
    )


# ---------------------------------------------------------------- data
class Corpus:
    """The populations the figures draw on, loaded once and shared.

    Registry rows and fetch outcomes cover the whole inventory; the
    logical-reuse network ``g`` and the structural population ``p4`` leave
    SKOS-dominated sources out (``ontometer_corpus.ontology_metrics``).
    """

    def __init__(
        self,
        paths: CorpusPaths,
        comparators_dir: Path | None = None,
        github_dir: Path | None = None,
    ):
        self.paths = paths
        self.comparators_dir = comparators_dir
        self.github_dir = github_dir
        self.stats: dict[str, str] = {}

    @cached_property
    def github(self) -> pd.DataFrame | None:
        """Sampled GitHub ontologies matched to the registries (``ontometer-corpus paper github``)."""
        if not self.github_dir:
            return None
        return pd.read_parquet(self.github_dir / "github_ontologies.parquet")

    @cached_property
    def comparators(self) -> dict[str, ReuseNetwork]:
        """Reuse networks of other kinds (``ontometer-corpus paper comparators``), empty without a directory."""
        return load_comparators(self.comparators_dir) if self.comparators_dir else {}

    @property
    def comparator_date(self) -> str:
        """The latest retrieval date of the comparator snapshots."""
        if not self.comparators_dir:
            return ""
        manifest = json.loads((self.comparators_dir / "manifest.json").read_text())
        return max(str(m.get("retrieved") or "") for m in manifest.values())

    def _sql(self, query: str) -> pd.DataFrame:
        conn = sqlite3.connect(self.paths.db_path)
        try:
            return pd.read_sql_query(query, conn)
        finally:
            conn.close()

    # --- registries --------------------------------------------------------
    @cached_property
    def counts(self) -> pd.DataFrame:
        """Unique ontologies per home registry and per registry of the kept copy."""
        return _registry_counts(
            self._sql(
                "SELECT source_registry, home_registry FROM corpus_registry "
                "WHERE is_latest_version = 1"
            )
        )

    @cached_property
    def source_order(self) -> list[str]:
        return self.counts.index.tolist()

    @cached_property
    def group(self) -> dict[str, str]:
        """Registry -> the group it is drawn as."""
        return registry_groups(self.source_order)

    @cached_property
    def group_color(self) -> dict[str, str]:
        return registry_colors(self.source_order)

    def color(self, registry: str) -> str:
        return self.group_color[self.group.get(registry, OTHER)]

    @cached_property
    def scatter_group(self) -> dict[str, str]:
        """Registry -> group in a scatter, where only the first few keep a hue."""
        return registry_groups(self.source_order, SCATTER_SLOTS)

    def scatter_color(self, registry: str) -> str:
        return self.group_color[self.scatter_group.get(registry, OTHER)]

    @cached_property
    def outcomes(self) -> pd.DataFrame:
        """One terminal outcome per source record, per registry."""
        out = self._sql(
            """
            WITH attempts AS (
                SELECT source_registry, source_id, MAX(status = 'ok') AS fetched_ok
                FROM fetch_attempts
                GROUP BY source_registry, source_id
            ), terminal AS (
                SELECT a.source_registry,
                       CASE
                           WHEN a.fetched_ok = 0 THEN 'Fetch failed'
                           WHEN c.id IS NULL THEN 'Fetched, not registered'
                           WHEN c.parse_status = 'ok' THEN 'Parsed'
                           WHEN c.parse_status = 'fail' THEN 'Parse failed'
                           WHEN c.parse_status IN ('feed', 'unsupported', 'missing')
                               THEN 'Skipped'
                           ELSE 'Awaiting parse'
                       END AS outcome
                FROM attempts AS a
                LEFT JOIN corpus_registry AS c
                  ON c.source_registry = a.source_registry AND c.source_id = a.source_id
            )
            SELECT source_registry, outcome, COUNT(*) AS n
            FROM terminal
            GROUP BY source_registry, outcome
            """
        )
        return out.pivot(index="source_registry", columns="outcome", values="n").fillna(0)

    @cached_property
    def version_groups(self) -> pd.DataFrame:
        """Parsed versions per release family holding a latest copy."""
        rows = self._sql(
            "SELECT source_registry, source_id, is_latest_version FROM corpus_registry "
            "WHERE is_duplicate = 0 AND parse_status = 'ok'"
        )
        rows["group_key"] = rows["source_id"].map(family_key)
        holds_latest = rows.groupby(["source_registry", "group_key"])[
            "is_latest_version"
        ].transform("max")
        rows = rows[holds_latest == 1]
        return (
            rows.groupby(["source_registry", "group_key"]).size().rename("n_versions").reset_index()
        )

    # --- network -------------------------------------------------------------
    @cached_property
    def views(self) -> NetworkViews:
        views = load_views(self.paths, latest_files(self.paths))
        self.stats["skosdropped"] = fmt_int(views.skos_dropped)
        return views

    @property
    def g_all(self) -> nx.MultiDiGraph:
        """Every usage and import edge, SKOS-dominated sources removed."""
        return self.views.g_all

    @property
    def g(self) -> nx.MultiDiGraph:
        """The logical-reuse view: imports plus usage edges with a logically used term."""
        return self.views.g

    @property
    def corpus_nodes(self) -> list[str]:
        return self.views.corpus_nodes

    @cached_property
    def home(self) -> dict[str, str]:
        return {n: self.g_all.nodes[n]["home_registry"] for n in self.corpus_nodes}

    @cached_property
    def corpus_links(self) -> nx.DiGraph:
        """One link per ordered pair of corpus ontologies, no self-loops."""
        links = nx.DiGraph(self.g.subgraph(self.corpus_nodes))
        links.remove_edges_from(nx.selfloop_edges(links))
        return links

    @cached_property
    def logical_ns_by_file(self) -> dict[str, set[str]]:
        return logical_namespaces(self.paths)

    def external_ratio(self, n: str) -> float:
        """Share of the vocabularies an ontology uses logically that ``g`` resolves elsewhere."""
        return external_ratio(n, self.g, self.logical_ns_by_file, self.views.no_iri_nodes)

    # --- structural population ---------------------------------------------
    @cached_property
    def p4(self) -> pd.DataFrame:
        """Latest parsed ontologies with structural and coupling metrics, SKOS-dominated left out."""
        return structural_population(load_ontology_metrics(self.paths))


# ---------------------------------------------------------------- figures
def _registry_axis(ax, order: list[str]) -> None:
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order)
    ax.set_ylim(len(order) - 0.5, -0.5)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)


def fig_corpus_composition(c: Corpus) -> plt.Figure:
    """Per registry: ontologies captured, how each record failed, version history."""
    order = c.source_order
    counts = c.counts
    # Outcome states, failures first; "parsed" is the light remainder.
    failure_colors = {
        "Fetch failed": STATUS_CRITICAL,
        "Fetched, not registered": STATUS_CRITICAL,
        "Parse failed": STATUS_SERIOUS,
        "Skipped": STATUS_WARNING,
        "Awaiting parse": TEXT_MUTED,
    }
    columns = [*failure_colors, "Parsed"]
    funnel = c.outcomes.reindex(columns=columns, fill_value=0)
    funnel = funnel.loc[:, funnel.sum() > 0].reindex(order).fillna(0)
    funnel_pct = funnel.div(funnel.sum(axis=1), axis=0) * 100

    groups = c.version_groups
    groups = groups.assign(has_history=groups["n_versions"] >= 2)
    share = groups.groupby("source_registry")["has_history"].mean().mul(100).reindex(order)
    median = (
        groups[groups["has_history"]].groupby("source_registry")["n_versions"].median()
    ).reindex(order)

    fig, axes = plt.subplots(
        1, 3, figsize=(FULL, 2.25), sharey=True, gridspec_kw={"width_ratios": [1.2, 1, 1]}
    )
    y = numpy.arange(len(order))

    ax = axes[0]
    # The kept copies are the wide light bar behind; the ontologies homed in the
    # registry are the narrow full bar in front.
    ax.barh(
        y,
        counts["latest_copy_fetched_from"],
        height=0.72,
        color=[tint(c.color(s), 0.68) for s in order],
    )
    ax.barh(y, counts["n_ontologies"], height=0.36, color=[c.color(s) for s in order])
    for yi, (v, kept) in zip(y, counts[["n_ontologies", "latest_copy_fetched_from"]].to_numpy()):
        ax.text(max(v, kept), yi, f" {v:,}", va="center", fontsize=5.5, color=INK)
    ax.set_xlim(0, counts.to_numpy().max() * 1.3)
    ax.set_xlabel("ontologies (latest version)")
    ax.legend(
        handles=[
            Patch(facecolor=TEXT_MUTED, label="homed in registry"),
            Patch(facecolor=tint(TEXT_MUTED, 0.68), label="copy kept from it"),
        ],
        loc="lower right",
    )
    _registry_axis(ax, order)
    panel(ax, "a", "unique ontologies")

    ax = axes[1]
    left = numpy.zeros(len(order))
    for col in funnel_pct.columns:
        vals = funnel_pct[col].to_numpy()
        ax.barh(
            y,
            vals,
            left=left,
            height=0.66,
            color=failure_colors.get(col, NEUTRAL_LIGHT),
            edgecolor=SURFACE,
            linewidth=0.4,
            label=col.lower(),
        )
        left += vals
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of source records")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=2, columnspacing=0.8)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    panel(ax, "b", "outcome per record")

    ax = axes[2]
    ax.barh(y, share.fillna(0), height=0.66, color=[c.color(s) for s in order])
    for yi, (s, m) in enumerate(zip(share.fillna(0), median)):
        label = f" {s:.0f}%" + (f" (median {m:.0f})" if pd.notna(m) else "")
        ax.text(s, yi, label, va="center", fontsize=5.5, color=TEXT_MUTED)
    ax.set_xlim(0, 100)
    ax.set_xlabel("% with 2+ parsed versions")
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    panel(ax, "c", "version history")
    fig.tight_layout(w_pad=0.6)

    total = funnel.to_numpy().sum()
    c.stats["registries"] = fmt_int(c.outcomes.shape[0])
    c.stats["homeregistries"] = fmt_int((counts["n_ontologies"] > 0).sum())
    c.stats["records"] = fmt_int(total)
    c.stats["parseok"] = fmt_pct(funnel["Parsed"].sum() / total * 100)
    c.stats["parsefail"] = fmt_pct(funnel.get("Parse failed", pd.Series(0)).sum() / total * 100)
    c.stats["fetchfail"] = fmt_pct(funnel.get("Fetch failed", pd.Series(0)).sum() / total * 100)
    c.stats["versions"] = fmt_int(groups["n_versions"].sum())
    c.stats["families"] = fmt_int(counts["n_ontologies"].sum())
    c.stats["withhistory"] = fmt_pct(groups["has_history"].mean() * 100)
    c.stats["medianversions"] = fmt_dec(groups.loc[groups["has_history"], "n_versions"].median(), 0)
    return fig


def _scatter_legend(c: Corpus, in_view: pd.Series) -> list[Line2D]:
    """One handle per scatter group, with the number of ontologies it holds in the view."""
    sizes = in_view.groupby(in_view.map(lambda r: c.scatter_group.get(r, OTHER))).size()
    groups = [*dict.fromkeys(c.scatter_group[r] for r in c.source_order)]
    return [
        Line2D(
            [],
            [],
            marker="o",
            linestyle="",
            markersize=3.5,
            markerfacecolor=c.group_color[g],
            markeredgecolor=SURFACE,
            markeredgewidth=0.3,
            label=f"{g} ({int(sizes.get(g, 0)):,})",
        )
        for g in groups
    ]


def bridges(links: nx.DiGraph, home: dict[str, str]) -> set[str]:
    """Ontologies that reuse an ontology of each closed community."""
    return {
        n for n in links if set(COMMUNITIES) <= {home[v] for v in links.successors(n) if v in home}
    }


def fig_reuse_network(c: Corpus) -> plt.Figure:
    """Largest connected component of the logical-reuse network among corpus ontologies."""
    net = c.corpus_links.subgraph(max(nx.weakly_connected_components(c.corpus_links), key=len))
    reused_by = pd.Series(dict(net.in_degree())).sort_values()
    skeleton = nx.Graph()
    skeleton.add_nodes_from(sorted(net))
    skeleton.add_edges_from(sorted(net.edges()))
    pos = nx.forceatlas2_layout(skeleton, max_iter=200, distributed_action=True, seed=7)
    xy = numpy.array([pos[n] for n in reused_by.index])
    xy = xy - xy.mean(axis=0)
    xy = xy @ numpy.linalg.svd(xy, full_matrices=False)[2].T
    pos = dict(zip(reused_by.index, xy))
    area = 0.6
    sizes = 1 + reused_by.to_numpy() * area
    home = {n: net.nodes[n]["home_registry"] for n in net}

    fig, ax = plt.subplots(figsize=(FULL, 2.55))
    # The two closed communities, as soft hulls around their central members.
    hull_labels = []
    for registry in COMMUNITIES:
        pts = numpy.array([pos[n] for n in reused_by.index if home[n] == registry])
        dist = numpy.linalg.norm(pts - numpy.median(pts, axis=0), axis=1)
        core = pts[dist <= numpy.quantile(dist, 0.85)]
        hull = core[ConvexHull(core).vertices]
        color = c.scatter_color(registry)
        ax.add_patch(
            Polygon(
                hull,
                closed=True,
                facecolor=tint(color, 0.9),
                edgecolor=tint(color, 0.45),
                linewidth=0.6,
                linestyle=(0, (3, 2)),
                joinstyle="round",
                zorder=0,
            )
        )
        hull_labels.append((registry, numpy.median(pts[:, 0])))
    segments = [(pos[u], pos[v]) for u, v in net.edges()]
    edge_color = [c.scatter_color(home[v]) for _, v in net.edges()]
    ax.add_collection(
        LineCollection(segments, colors=edge_color, linewidths=0.15, alpha=0.12, zorder=1)
    )
    bridge = numpy.array([n in bridges(net, home) for n in reused_by.index])
    ax.scatter(
        xy[:, 0],
        xy[:, 1],
        s=sizes,
        c=[c.scatter_color(home[n]) for n in reused_by.index],
        edgecolors=numpy.where(bridge, INK, SURFACE).tolist(),
        linewidths=numpy.where(bridge, 0.45, 0.2).tolist(),
        alpha=0.92,
        zorder=2,
    )
    ax.autoscale()
    ax.margins(0.02)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.tight_layout(rect=(0, 0, 0.82, 0.93))
    placed = []
    for n in reused_by.index[-16:][::-1]:
        gap = math.sqrt(1 + reused_by[n] * area) / 2 + 1
        label = ax.annotate(
            _short_label(n).removesuffix(".owl"),
            pos[n],
            xytext=(0, gap),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=4.8 + 2 * math.sqrt(reused_by[n] / reused_by.max()),
            fontweight="bold",
            color=INK,
            path_effects=[pe.withStroke(linewidth=1.2, foreground=SURFACE)],
            zorder=3,
        )
        if any(label.get_window_extent().overlaps(box) for box in placed):
            label.set(position=(0, -gap), va="top")
        placed.append(label.get_window_extent())

    fig.legend(
        handles=[
            *_scatter_legend(c, pd.Series(home)),
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                markersize=3.5,
                markerfacecolor=NEUTRAL,
                markeredgecolor=INK,
                markeredgewidth=0.6,
                label=f"reuses both ({int(bridge.sum()):,})",
            ),
        ],
        title="home registry",
        loc="upper left",
        bbox_to_anchor=(0.82, 0.95),
        alignment="left",
    )
    fig.legend(
        handles=[
            ax.scatter([], [], s=1 + k * area, color=NEUTRAL, edgecolors=SURFACE, label=str(k))
            for k in (1, 50, 200, 400)
        ],
        title="reused by",
        loc="upper left",
        bbox_to_anchor=(0.82, 0.52),
        labelspacing=1.1,
        alignment="left",
    )
    for registry, x in hull_labels:
        ax.annotate(
            f"{registry} community",
            (x, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=7,
            fontstyle="italic",
            color=INK,
            annotation_clip=False,
        )
    c.stats["netshown"] = fmt_int(net.number_of_nodes())
    c.stats["netlinks"] = fmt_int(net.number_of_edges())
    return fig


def _geodesic(p: numpy.ndarray, q: numpy.ndarray, k: int = 24) -> numpy.ndarray:
    """Points along the Poincaré-disk geodesic from ``p`` to ``q``."""
    cross = p[0] * q[1] - p[1] * q[0]
    if abs(cross) < 1e-9:
        return numpy.array([p, q])
    # The geodesic lies on the circle through p, q and p's inversion in the unit circle.
    pp, qq = p @ p, q @ q
    a = numpy.array([[p[0], p[1]], [q[0], q[1]]]) * 2
    b = numpy.array([pp + 1, qq + 1])
    centre = numpy.linalg.solve(a, b)
    r = numpy.linalg.norm(p - centre)
    t0 = math.atan2(p[1] - centre[1], p[0] - centre[0])
    t1 = math.atan2(q[1] - centre[1], q[0] - centre[0])
    dt = (t1 - t0 + math.pi) % (2 * math.pi) - math.pi  # the short way round stays in the disk
    ts = t0 + numpy.linspace(0, 1, k) * dt
    return numpy.c_[centre[0] + r * numpy.cos(ts), centre[1] + r * numpy.sin(ts)]


#: Ontologies named in each panel of the G(t) figure: the most specialised at that date.
GT_LABELS = 5


def _gt_label(node: dict) -> str:
    """A short name: the registry label, or the initials of a long multi-word name."""
    label = str(node.get("label") or node.get("name") or "")
    words = label.split()
    if len(label) > 10 and len(words) > 1:
        return "".join(w[0] for w in words if w[0].isalpha()).upper()
    return label


#: Label offsets (points) tried in turn, nearest first, until a label overlaps none placed.
_LABEL_OFFSETS = [
    (dx * r, dy * r)
    for r in (3, 7, 11)
    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1), (1.4, 0), (-1.4, 0), (0, 1.3), (0, -1.3))
]


def _place_labels(fig, ax, points: list[tuple[str, numpy.ndarray]]) -> None:
    """Annotate points so that no two labels overlap, trying offsets in turn."""
    renderer = fig.canvas.get_renderer()
    placed = []
    for text, xy in points:
        ann = None
        for dx, dy in _LABEL_OFFSETS:
            ann = ax.annotate(
                text,
                xy,
                xytext=(dx, dy),
                textcoords="offset points",
                ha="left" if dx > 0 else "right" if dx < 0 else "center",
                va="bottom" if dy > 0 else "top" if dy < 0 else "center",
                fontsize=5,
                color=INK,
                zorder=4,
                path_effects=[pe.withStroke(linewidth=1.4, foreground=SURFACE)],
            )
            box = ann.get_window_extent(renderer).expanded(1.05, 1.1)
            if not any(box.overlaps(b) for b in placed):
                placed.append(box)
                break
            ann.remove()
            ann = None
        if ann is None:  # nowhere free: the label is dropped rather than drawn over another
            continue


def fig_poincare_gt(c: Corpus) -> plt.Figure:
    """G(t) in the Poincaré disk at two years and at capture, as the explorer draws it."""
    data = build_viz(c.paths)
    meta, nodes, edges = data["meta"], data["nodes"], data["edges"]
    times = [*GT_YEARS, meta["as_of_year"]]
    angle = numpy.array([n["angle"] for n in nodes])
    birth = numpy.array([n["birth"] for n in nodes])

    def spec_at(t: float) -> numpy.ndarray:
        s = numpy.zeros(len(nodes))
        for i, n in enumerate(nodes):
            for ts, cnt in n["steps"]:
                if ts <= t:
                    s[i] = cnt
                else:
                    break
        return s

    fig, axes = plt.subplots(1, 3, figsize=(FULL, 2.2))
    fig.subplots_adjust(left=0.005, right=0.995, top=0.86, bottom=0.17, wspace=0.03)
    previous = -math.inf
    for ax, t in zip(axes, times):
        alive = birth <= t
        s = spec_at(t)
        rho = poincare_radius(s, meta["n_final"], meta["r_max"])
        xy = numpy.c_[rho * numpy.cos(angle), rho * numpy.sin(angle)]
        ax.add_patch(
            plt.Circle(
                (0, 0), 1, facecolor=tint(BRAND_BLUE, 0.96), edgecolor=NEUTRAL, linewidth=0.6
            )
        )
        live = [e for e in edges if e[2] <= t and alive[e[0]] and alive[e[1]]]
        old = [e for e in live if e[2] <= previous]
        new = [e for e in live if e[2] > previous]
        for group, color, width, alpha in ((old, NEUTRAL, 0.2, 0.55), (new, INK, 0.22, 0.4)):
            ax.add_collection(
                LineCollection(
                    [_geodesic(xy[e[0]], xy[e[1]]) for e in group],
                    colors=color,
                    linewidths=width,
                    alpha=alpha,
                    zorder=1,
                )
            )
        idx = numpy.flatnonzero(alive)
        idx = idx[numpy.argsort(s[idx])]
        ax.scatter(
            xy[idx, 0],
            xy[idx, 1],
            s=0.9 + 4.2 * numpy.log1p(s[idx]),
            c=[c.scatter_color(nodes[j]["registry"]) for j in idx],
            edgecolors=SURFACE,
            linewidths=0.15,
            alpha=0.95,
            zorder=2,
        )
        ax.set_xlim(-1.02, 1.02)
        ax.set_ylim(-1.02, 1.02)
        ax.set_aspect("equal")
        top = [j for j in idx[::-1][:GT_LABELS] if s[j] > 0]
        _place_labels(fig, ax, [(_gt_label(nodes[j]), xy[j]) for j in top])
        label = meta["as_of"] if t == meta["as_of_year"] else f"{int(t)}"
        ax.set_title(label, loc="center", fontsize=8.5, fontweight="bold", pad=9)
        ax.text(
            0.5,
            1.0,
            f"{int(alive.sum()):,} ontologies \u00b7 {len(live):,} links",
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=5.5,
            color=TEXT_MUTED,
        )
        ax.axis("off")
        previous = t
    in_view = pd.Series([n["registry"] for n in nodes])
    links = [
        Line2D([], [], color=INK, linewidth=0.8, alpha=0.7, label="link new in this panel"),
        Line2D([], [], color=NEUTRAL, linewidth=0.8, label="earlier link"),
    ]
    fig.legend(
        handles=[*_scatter_legend(c, in_view), *links],
        loc="lower center",
        ncol=3,
        bbox_to_anchor=(0.5, 0.0),
        columnspacing=1.0,
    )
    c.stats["gtshown"] = fmt_int(meta["n_shown"])
    c.stats["gtundated"] = fmt_int(meta["n_undated"])
    c.stats["gtedges"] = fmt_int(meta["n_edges"])
    c.stats["gtasof"] = str(meta["as_of"])
    return fig


def fig_size_distributions(c: Corpus) -> plt.Figure:
    """Triples, classes, properties and individuals per ontology, by home registry."""
    size = c._sql(
        "SELECT home_registry, triple_count, class_count, property_count, individual_count, "
        "skos_concept_count FROM corpus_registry "
        "WHERE is_latest_version = 1 AND parse_status = 'ok'"
    )
    size["instance_count"] = size["individual_count"] - size["skos_concept_count"].fillna(0)
    size["group"] = size["home_registry"].map(lambda r: c.group.get(r, OTHER))
    metrics = [
        ("triple_count", "triples"),
        ("class_count", "classes"),
        ("property_count", "properties"),
        ("instance_count", "individuals"),
    ]
    groups = [*dict.fromkeys(c.group[r] for r in c.source_order)]
    fig, axes = plt.subplots(1, 4, figsize=(FULL, 1.75), sharey=True)
    rng = numpy.random.default_rng(BOOTSTRAP_SEED)
    for i, (ax, (col, label)) in enumerate(zip(axes, metrics)):
        sub = size.dropna(subset=[col])
        data = [sub.loc[sub["group"] == g, col].to_numpy() + 1 for g in groups]
        # Every ontology as a faint point behind its box, jittered across the row.
        for yi, (g, vals) in enumerate(zip(groups, data)):
            ax.scatter(
                vals,
                yi + rng.uniform(-0.28, 0.28, len(vals)),
                s=0.6,
                color=c.group_color[g],
                alpha=0.25,
                linewidths=0,
                rasterized=True,
                zorder=1,
            )
        bp = ax.boxplot(
            data,
            positions=range(len(groups)),
            orientation="horizontal",
            patch_artist=True,
            widths=0.5,
            showfliers=False,
            medianprops=dict(color=INK, linewidth=1.0),
            whiskerprops=dict(color=TEXT_MUTED, linewidth=0.5),
            capprops=dict(linewidth=0),
            zorder=2,
        )
        for patch, g in zip(bp["boxes"], groups):
            patch.set(facecolor=tint(c.group_color[g], 0.45), edgecolor=c.group_color[g])
            patch.set_linewidth(0.6)
        ax.set_xscale("log")
        ax.xaxis.set_minor_formatter(NullFormatter())
        zero = numpy.mean(numpy.concatenate(data) == 1) * 100
        panel(ax, "abcd"[i], label)
        ax.set_xlabel(f"count + 1 ({zero:.0f}% zero)")
        ax.tick_params(axis="y", length=0)
        if i > 0:
            ax.spines["left"].set_visible(False)
    _registry_axis(axes[0], groups)
    fig.tight_layout(w_pad=0.4)
    c.stats["medtriples"] = fmt_int(size["triple_count"].median())
    c.stats["medclasses"] = fmt_int(size["class_count"].median())
    c.stats["zeroindividuals"] = fmt_pct((size["instance_count"].dropna() == 0).mean() * 100)
    c.stats["zeroproperties"] = fmt_pct((size["property_count"].dropna() == 0).mean() * 100)
    # How much of the corpus total the largest 1% of ontologies hold, per count.
    for col, key in (("triple_count", "triples"), ("class_count", "classes")):
        s = size[col].dropna()
        c.stats[f"sizetop{key}"] = fmt_pct(s[s >= s.quantile(0.99)].sum() / s.sum() * 100)
    c.stats["ninetyninthclasses"] = fmt_int(size["class_count"].quantile(0.99))
    c.stats["ninetyninthproperties"] = fmt_int(size["property_count"].quantile(0.99))
    return fig


def _gof_bootstrap(data, fit, discrete: bool, n_boot: int, seed: int) -> float:
    """Clauset et al.'s goodness-of-fit p-value for the fitted power law."""
    data = numpy.asarray(data, dtype=float)
    xmin = fit.power_law.xmin
    below = data[data < xmin]
    p_tail = (data >= xmin).mean()
    observed = fit.power_law.D
    rng = numpy.random.default_rng(seed)
    numpy.random.seed(seed)  # powerlaw's generate_random draws from numpy's global state
    hits = 0
    for _ in range(n_boot):
        n_tail = rng.binomial(len(data), p_tail)
        synthetic = numpy.concatenate(
            [
                fit.power_law.generate_random(n_tail),
                rng.choice(below, len(data) - n_tail) if len(below) else [],
            ]
        )
        hits += powerlaw.Fit(synthetic, discrete=discrete, verbose=False).power_law.D >= observed
    return hits / n_boot


def _cached_gof(data, fit, discrete: bool) -> float:
    """The bootstrap p-value, cached under (data, discrete, n_boot, seed)."""
    key = hashlib.sha256(
        json.dumps([sorted(map(float, data)), discrete, N_BOOTSTRAP, BOOTSTRAP_SEED]).encode()
    ).hexdigest()
    try:
        cache = json.loads(GOF_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if key not in cache:
        cache[key] = _gof_bootstrap(data, fit, discrete, N_BOOTSTRAP, BOOTSTRAP_SEED)
        GOF_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GOF_CACHE.write_text(json.dumps(cache, indent=1))
    return cache[key]


def _reuse_degrees(c: Corpus) -> tuple[list[int], list[int]]:
    """Per corpus ontology: distinct corpus ontologies reusing it, and distinct targets it reuses.

    Reuse received counts corpus ontologies only, as the hub table does; reuse made
    counts every namespace in ``g``, external ones included. An import and a usage of
    the same target count once.
    """
    links = c.corpus_links
    ins = [links.in_degree(n) if links.has_node(n) else 0 for n in c.corpus_nodes]
    outs = [len(set(c.g.successors(n)) - {n}) for n in c.corpus_nodes]
    return ins, outs


class TailFit:
    """A discrete power-law fit to the positive values, with the alternatives compared."""

    def __init__(self, values):
        self.pos = [v for v in values if v > 0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.fit = powerlaw.Fit(self.pos, discrete=True, verbose=False)
            self.comparisons = {
                "log-normal": self.fit.distribution_compare("power_law", "lognormal"),
                "exponential": self.fit.distribution_compare("power_law", "exponential"),
                "truncated power law": self.fit.distribution_compare(
                    "power_law", "truncated_power_law", nested=True
                ),
            }

    @property
    def alpha(self) -> float:
        return float(self.fit.power_law.alpha)

    @property
    def xmin(self) -> float:
        return float(self.fit.power_law.xmin)

    @property
    def n_tail(self) -> int:
        return sum(v >= self.xmin for v in self.pos)

    @property
    def better(self) -> list[str]:
        """Alternatives that fit the tail significantly better than the power law (p < 0.1)."""
        return [name for name, (r, p) in self.comparisons.items() if r < 0 and p < 0.1]

    def ccdf(self, dist: str, ks: numpy.ndarray) -> numpy.ndarray:
        """P(K >= k) over all positive values, for k in the tail, under a fitted distribution."""
        share = self.n_tail / len(self.pos)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return share * getattr(self.fit, dist).ccdf(ks)


#: Colours of the networks compared in the inequality figure: the corpus first.
NETWORK_COLORS = {
    # The corpus is the emphasised series, in the brand ink-blue; the three comparators
    # separate from it and from each other for normal and colour-deficient vision, and are
    # also labelled directly in panel (a).
    "corpus": BRAND_BLUE,
    "cran": "#E67E22",
    "debian": "#1baf7a",
    "lod": "#6250d6",
}
#: Where, in reusers k, the CCDF names a curve that ends among others.
CURVE_LABEL_AT = {"lod": 30, "cran": 1500}
#: Short names for the direct labels at the end of each curve.
NETWORK_SHORT = {"corpus": "corpus", "cran": "CRAN", "debian": "Debian", "lod": "LOD cloud"}


def _empirical_ccdf(values) -> tuple[numpy.ndarray, numpy.ndarray]:
    """P(K >= k) over the positive values, at each distinct k."""
    pos = numpy.sort(numpy.asarray([v for v in values if v > 0]))
    ks, first = numpy.unique(pos, return_index=True)
    return ks, 1 - first / len(pos)


@plt.FuncFormatter
def _percent_axis(v: float, _pos: int) -> str:
    """A share on a log axis as a percentage: 100%, 10%, ..., 0.01%."""
    return f"{v * 100:g}%"


def _top_share(values) -> tuple[numpy.ndarray, numpy.ndarray]:
    """Share of all reuse received by the top fraction of nodes, most reused first."""
    arr = numpy.sort(numpy.asarray(values, dtype=float))[::-1]
    n = len(arr)
    return numpy.arange(1, n + 1) / n, numpy.cumsum(arr) / arr.sum()


def _share_of_top(values, fraction: float) -> float:
    """Share of all reuse received by the given top fraction of nodes."""
    arr = numpy.sort(numpy.asarray(values, dtype=float))[::-1]
    return arr[: max(1, round(fraction * len(arr)))].sum() / arr.sum()


#: The top fraction of nodes whose share of reuse panel (b) of the inequality figure marks.
TOP_FRACTION = 0.01


def fig_inequality(c: Corpus) -> plt.Figure:
    """Reuse received by the corpus against other reuse networks: in-degree tail and top shares."""
    ins, outs = _reuse_degrees(c)
    tail = TailFit(ins)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gof_p = _cached_gof(tail.pos, tail.fit, True)
    corpus_color = NETWORK_COLORS["corpus"]
    other = {"linewidth": 0.9}
    main = {"color": corpus_color, "linewidth": 1.5, "zorder": 4}

    fig = plt.figure(figsize=(FULL, 3.35))
    gs = fig.add_gridspec(
        2,
        2,
        height_ratios=[1, 1.12],
        wspace=0.28,
        hspace=0.42,
        left=0.085,
        right=0.985,
        bottom=0.1,
        top=0.95,
    )
    made = {"color": corpus_color, "linewidth": 0.9, "linestyle": (0, (5, 1.5, 1, 1.5))}
    fit_style = {"color": INK, "linewidth": 0.8, "zorder": 5}
    # Only the better tail fit is drawn; the power law's exponent is in the comparator table.
    truncated = {"linestyle": (0, (3, 1.5)), **fit_style}

    ax = fig.add_subplot(gs[0, 0])
    for name, net in c.comparators.items():
        ks, cc = _empirical_ccdf(net.in_degrees())
        ax.step(ks, cc, where="post", color=NETWORK_COLORS[name], **other)
    ks, cc = _empirical_ccdf(ins)
    ax.step(ks, cc, where="post", **main)
    xs = numpy.unique(numpy.geomspace(tail.xmin, max(ins), 60).round())
    ax.plot(xs, tail.ccdf("truncated_power_law", xs), **truncated)
    for name, degrees in [
        ("corpus", ins),
        *((n, net.in_degrees()) for n, net in c.comparators.items()),
    ]:
        ks, cc = _empirical_ccdf(degrees)
        # Each name sits where its curve runs clear of the others: the corpus below its end,
        # Debian above its end, CRAN and the LOD cloud above a stretch where they lead.
        at, offset, ha, va = (ks[-1], cc[-1]), (2, -2), "left", "top"
        if name == "debian":
            offset, ha, va = (-6, 7), "right", "bottom"
        elif name in CURVE_LABEL_AT:
            i = int(numpy.searchsorted(ks, CURVE_LABEL_AT[name]))
            at, offset, ha, va = (ks[i], cc[i]), (0, 4), "center", "bottom"
        ax.annotate(
            NETWORK_SHORT[name],
            at,
            xytext=offset,
            textcoords="offset points",
            fontsize=5.5,
            color=NETWORK_COLORS[name],
            ha=ha,
            va=va,
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.yaxis.set_major_formatter(_percent_axis)
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("reused by at least $k$ others")
    ax.set_ylabel("share of the reused nodes")
    panel(ax, "a", "how widely nodes are reused")

    ax = fig.add_subplot(gs[0, 1])
    for name, net in c.comparators.items():
        ax.plot(*_top_share(net.in_degrees()), color=NETWORK_COLORS[name], **other)
    ax.plot(*_top_share(ins), **main)
    ax.plot(*_top_share(outs), zorder=4, **made)
    top = _share_of_top(ins, TOP_FRACTION)
    ax.axvline(TOP_FRACTION, color=TEXT_MUTED, linestyle=(0, (1, 1.5)), linewidth=0.7, zorder=1)
    ax.text(TOP_FRACTION * 1.15, 0.02, f"top {TOP_FRACTION:.0%}", fontsize=5.5, color=TEXT_MUTED)
    ax.plot(
        [TOP_FRACTION],
        [top],
        marker="o",
        markersize=3.2,
        color=corpus_color,
        markeredgecolor=SURFACE,
        markeredgewidth=0.6,
        zorder=6,
    )
    ax.text(TOP_FRACTION * 1.35, top - 0.05, f"{top:.0%}", fontsize=6, color=INK, va="top")
    ax.set_xscale("log")
    ax.set_xlim(1e-4, 1)
    ax.set_ylim(0, 1.02)
    ax.xaxis.set_major_formatter(_percent_axis)
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_xlabel("the most reused nodes, as a share of all")
    ax.set_ylabel("their share of all reuse")
    panel(ax, "b", "how much the most reused take")

    nets = [
        Line2D([], [], color=NETWORK_COLORS[name], linewidth=0.9, label=LABELS[name])
        for name in c.comparators
    ]
    # Column-major fill: the networks form the first row, the corpus's extra lines the second.
    handles = [
        Line2D([], [], label="ontology corpus", **main),
        Line2D([], [], label="corpus, reuse made (b)", **made),
        nets[0],
        Line2D([], [], label="truncated power-law fit (a)", **truncated),
        *nets[1:],
    ]
    ax = fig.add_subplot(gs[1, 0])
    _mixing_heatmap(ax, c, fontsize=6)
    ax.set_ylabel("reusing registry", fontsize=6.5)
    ax.set_xlabel("reused registry", fontsize=6.5)
    panel(ax, "c", "where reuse goes (%)")
    legend_ax = fig.add_subplot(gs[1, 1])
    legend_ax.axis("off")
    legend_ax.legend(handles=handles, loc="center left", ncol=1, handlelength=2.6, frameon=False)

    if gof_p < 0.1:
        verdict = "ruled out by the goodness-of-fit test"
    elif tail.better:
        verb = "fits" if len(tail.better) == 1 else "fit"
        verdict = f"plausible, but the {' and the '.join(tail.better)} {verb} significantly better"
    else:
        verdict = "plausible, and no alternative fits significantly better"
    c.stats["plalpha"] = fmt_dec(tail.alpha)
    c.stats["plxmin"] = fmt_int(tail.xmin)
    c.stats["pltail"] = fmt_int(tail.n_tail)
    c.stats["plgofp"] = fmt_dec(gof_p)
    c.stats["plverdict"] = verdict
    c.stats["reusetopfraction"] = f"{TOP_FRACTION * 100:g}\\%"
    c.stats["reusetopshare"] = fmt_pct(top * 100)
    c.stats["reusetopmadeshare"] = fmt_pct(_share_of_top(outs, TOP_FRACTION) * 100)
    c.stats["giniin"] = fmt_dec(_gini(ins))
    c.stats["giniout"] = fmt_dec(_gini(outs))
    c.stats["giniinreused"] = fmt_dec(_gini([v for v in ins if v > 0]))
    c.stats["neverreused"] = fmt_pct(numpy.mean([v == 0 for v in ins]) * 100)
    c.stats["noreuse"] = fmt_pct(numpy.mean([v == 0 for v in outs]) * 100)
    c.stats["ncorpus"] = fmt_int(len(c.corpus_nodes))
    c.stats["nsnodes"] = fmt_int(c.g.number_of_nodes())
    c.stats["nsedges"] = fmt_int(c.g.number_of_edges())
    c.stats["nsexternal"] = fmt_int(c.g.number_of_nodes() - len(c.corpus_nodes))
    return fig


#: Ontologies placed in the population, by ontology IRI, with the name shown: widely reused
#: ones from the two curated libraries and from the other registries.
PLACED = {
    "http://xmlns.com/foaf/0.1/": "FOAF",
    "http://www.w3.org/ns/prov#": "PROV-O",
    "http://purl.obolibrary.org/obo/bfo.owl": "BFO",
    "http://purl.obolibrary.org/obo/go.owl": "GO",
    "http://edamontology.org": "EDAM",
    "http://www.loa-cnr.it/ontologies/DUL.owl": "DUL",
}
#: Measures on which they are placed: metric column, column heading.
PLACED_MEASURES = (
    ("class_count", "classes"),
    ("property_count", "properties"),
    ("max_depth_long", "hierarchy\ndepth"),
    ("multiple_inheritance_rate", "multiple\ninheritance"),
    ("restriction_density", "restrictions\nper class"),
    ("coupling_ratio", "coupling\nratio"),
)


def _placed_percentiles(c: Corpus) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per placed ontology: its percentile in the analysed corpus on each measure (ties take
    their mean rank), and its home registry and the corpus ontologies reusing it."""
    p4 = c.p4
    links = c.corpus_links
    pct, info = {}, {}
    for iri, name in PLACED.items():
        hit = p4.index[p4["ontology_iri"] == iri]
        if len(hit) != 1:
            raise ValueError(f"{name}: {len(hit)} analysed ontologies with IRI {iri}")
        node = hit[0]
        pct[name] = {
            col: p4[col].dropna().rank(pct=True, method="average").get(node, numpy.nan) * 100
            for col, _ in PLACED_MEASURES
        }
        info[name] = {
            "registry": p4.at[node, "home_registry"],
            "reused_by": links.in_degree(node) if links.has_node(node) else 0,
        }
    return pd.DataFrame(pct).T, pd.DataFrame(info).T


def fig_placing(c: Corpus) -> plt.Figure:
    """Widely used ontologies as percentiles of the corpus on structural measures."""
    pct, info = _placed_percentiles(c)
    cols = [col for col, _ in PLACED_MEASURES]
    n_rows, n_cols = pct.shape
    row, head = 0.165, 0.27
    height = head + row * n_rows + 0.02

    fig = plt.figure(figsize=(FULL, height))
    ax = fig.add_axes((0.215, 0.02 / height, 0.78, row * n_rows / height))
    norm = plt.Normalize(0, 100)
    for i, name in enumerate(pct.index):
        for j, col in enumerate(cols):
            v = pct.at[name, col]
            fill = DIVERGING_CMAP(norm(v))
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=fill, edgecolor=SURFACE, linewidth=1.2))
            ax.text(
                j + 0.5,
                i + 0.5,
                f"{v:.0f}",
                ha="center",
                va="center",
                fontsize=7,
                color=ink_on(fill),
            )
    ax.set_xlim(0, n_cols)
    ax.set_ylim(n_rows, 0)
    ax.xaxis.tick_top()
    ax.set_xticks(numpy.arange(n_cols) + 0.5, [label for _, label in PLACED_MEASURES], fontsize=6.5)
    ax.set_yticks(
        numpy.arange(n_rows) + 0.5,
        [f"{n} ({info.at[n, 'registry']}, {info.at[n, 'reused_by']})" for n in pct.index],
        fontsize=7,
    )
    ax.tick_params(length=0, pad=2)
    for side in ("top", "bottom", "left", "right"):
        ax.spines[side].set_visible(False)

    c.stats["placedn"] = fmt_int(n_rows)
    reused = info["reused_by"].astype(int)
    c.stats["placedreusedmin"] = fmt_int(reused.min())
    c.stats["placedreusedmax"] = fmt_int(reused.max())
    for col, _ in PLACED_MEASURES:
        tag = "".join(ch for ch in col.lower() if ch.isalpha())
        c.stats[f"placed{tag}min"] = fmt_int(pct[col].min())
        c.stats[f"placed{tag}max"] = fmt_int(pct[col].max())
        for name in pct.index:
            key = name.lower().replace("-", "")
            c.stats[f"placed{key}{tag}"] = fmt_int(pct.at[name, col])
    for name in pct.index:
        c.stats[f"placed{name.lower().replace('-', '')}reusedby"] = fmt_int(
            info.at[name, "reused_by"]
        )
    return fig


def comparator_table(c: Corpus) -> str:
    """The corpus and the other reuse networks side by side, as a LaTeX table."""
    ins, _ = _reuse_degrees(c)
    rows = [
        ("ontology corpus", "corpus", len(c.corpus_nodes), c.corpus_links.number_of_edges(), ins)
    ]
    rows += [
        (LABELS[name], name, len(net.nodes), len(net.edges), net.in_degrees())
        for name, net in c.comparators.items()
    ]
    lines = []
    for label, key, n_nodes, n_edges, degrees in rows:
        tail = TailFit(degrees)
        never = numpy.mean([v == 0 for v in degrees]) * 100
        gini = _gini(degrees)
        gini_reused = _gini([v for v in degrees if v > 0])
        best = "truncated" if "truncated power law" in tail.better else "power law"
        lines.append(
            f"  {label} & {fmt_int(n_nodes)} & {fmt_int(n_edges)} & {fmt_pct(never)} & {gini:.2f}"
            f" & {gini_reused:.2f} & {tail.alpha:.2f} & {fmt_int(tail.xmin)} & {best} \\\\"
        )
        c.stats[f"cmp{key}gini"] = fmt_dec(gini)
        c.stats[f"cmp{key}ginireused"] = fmt_dec(gini_reused)
        c.stats[f"cmp{key}alpha"] = fmt_dec(tail.alpha)
        c.stats[f"cmp{key}never"] = fmt_pct(never)
        c.stats[f"cmp{key}nodes"] = fmt_int(n_nodes)
    c.stats["cmpdate"] = c.comparator_date
    return "\n".join(
        [
            "% Generated from the corpus tables and the comparator snapshots by `ontometer-corpus"
            " paper figures`; do not edit.",
            "\\begin{tabular}{@{}lrrrrrrrl@{}}",
            "  \\toprule",
            "  & & & & \\multicolumn{2}{c}{Gini} & & & \\\\",
            "  \\cmidrule(lr){5-6}",
            "  Network & Nodes & Links & Never reused & all & reused & $\\alpha$ & $k_{\\min}$"
            " & Better fit \\\\",
            "  \\midrule",
            *lines,
            "  \\bottomrule",
            "\\end{tabular}",
        ]
    )


def fig_declared_vs_actual(c: Corpus) -> plt.Figure:
    """(ontology, corpus ontology) reuse pairs: declared and used, silent, vestigial."""
    declared: dict[str, set[str]] = defaultdict(set)
    used: dict[str, set[str]] = defaultdict(set)
    imports_anything: dict[str, bool] = defaultdict(bool)
    for u, v, d in c.g.edges(data=True):
        if d["edge_type"] == "import":
            imports_anything[u] = True
        if c.g.nodes[v].get("is_external"):
            continue
        (declared if d["edge_type"] == "import" else used)[u].add(v)
    totals = {"declared & used": 0, "silent": 0, "vestigial": 0}
    importers = dict(totals)
    for n in c.corpus_nodes:
        d_set, u_set = declared.get(n, set()), used.get(n, set())
        row = {
            "declared & used": len(d_set & u_set),
            "silent": len(u_set - d_set),
            "vestigial": len(d_set - u_set),
        }
        for k, v in row.items():
            totals[k] += v
            if imports_anything[n]:
                importers[k] += v
    scopes = {"all ontologies": totals, "ontologies that\nimport anything": importers}

    fig, ax = plt.subplots(figsize=(HALF, 1.9))
    for i, (scope, table) in enumerate(scopes.items()):
        n = sum(table.values())
        left = 0.0
        for (kind, v), color in zip(table.items(), KIND_PALETTE):
            share = v / n * 100
            ax.barh(
                i,
                share,
                left=left,
                height=0.6,
                color=color,
                edgecolor=SURFACE,
                linewidth=0.5,
                label=kind if i == 0 else None,
            )
            if share >= 6:
                ax.text(
                    left + share / 2,
                    i,
                    f"{share:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=6 if share >= 12 else 5,
                    color=ink_on(color),
                )
            left += share
    ax.set_yticks(range(len(scopes)))
    ax.set_yticklabels([f"{s}\n({sum(t.values()):,} pairs)" for s, t in scopes.items()])
    ax.set_ylim(len(scopes) - 0.5, -0.5)
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of (ontology, corpus target) pairs")
    ax.legend(loc="upper center", bbox_to_anchor=(0.35, -0.3), ncol=3, columnspacing=0.8)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    fig.tight_layout()
    n = sum(totals.values())
    c.stats["reusepairs"] = fmt_int(n)
    c.stats["consistentshare"] = fmt_pct(totals["declared & used"] / n * 100)
    c.stats["silentshare"] = fmt_pct(totals["silent"] / n * 100)
    c.stats["vestigialshare"] = fmt_pct(totals["vestigial"] / n * 100)
    c.stats["importersilentshare"] = fmt_pct(importers["silent"] / sum(importers.values()) * 100)

    # The same split within LOV alone, the population of the earlier LOV studies.
    lov = {n for n in c.corpus_nodes if c.home[n] == "LOV"}
    lov_totals = {"declared & used": 0, "silent": 0, "vestigial": 0}
    lov_importers = dict(lov_totals)
    for node in lov:
        d_set, u_set = declared.get(node, set()) & lov, used.get(node, set()) & lov
        row = {
            "declared & used": len(d_set & u_set),
            "silent": len(u_set - d_set),
            "vestigial": len(d_set - u_set),
        }
        for k, v in row.items():
            lov_totals[k] += v
            if imports_anything[node]:
                lov_importers[k] += v
    lov_n = sum(lov_totals.values())
    c.stats["lovpairs"] = fmt_int(lov_n)
    c.stats["lovsilentshare"] = fmt_pct(lov_totals["silent"] / lov_n * 100)
    c.stats["lovimportersilentshare"] = fmt_pct(
        lov_importers["silent"] / sum(lov_importers.values()) * 100
    )
    c.stats["lovnoimportshare"] = fmt_pct(
        sum(not imports_anything[n] for n in lov) / len(lov) * 100
    )
    return fig


def _mixing_heatmap(ax, c: Corpus, fontsize: float = 5.5) -> tuple[pd.DataFrame, list[str]]:
    """Row-normalised reuse between registries (percent of a registry's links reaching each),
    with the within-registry cells boxed; registries with at least 20 corpus ontologies."""
    members = pd.Series(c.home).value_counts()
    registries = [r for r in c.source_order if members.get(r, 0) >= 20]
    links = pd.DataFrame(
        [(c.home[u], c.home[v]) for u, v in c.corpus_links.edges()],
        columns=pd.Index(["source", "target"]),
    )
    mixing = (
        pd.crosstab(links["source"], links["target"], normalize="index")
        .mul(100)
        .reindex(index=registries, columns=registries, fill_value=0)
    )
    sns.heatmap(
        mixing,
        ax=ax,
        cmap=SEQUENTIAL_CMAP,
        vmin=0,
        vmax=100,
        annot=True,
        fmt=".0f",
        annot_kws={"fontsize": fontsize},
        linewidths=0.4,
        linecolor=SURFACE,
        cbar=False,
        square=True,
    )
    for i in range(len(registries)):
        ax.add_patch(Rectangle((i, i), 1, 1, fill=False, edgecolor=INK, linewidth=0.7))
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="both", length=0, labelsize=6)
    plt.setp(ax.get_xticklabels(), rotation=40, ha="right", rotation_mode="anchor")
    return mixing, registries


def fig_registry_reuse(c: Corpus) -> plt.Figure:
    """Where each registry's ontologies find the corpus ontologies they reuse."""
    fig, ax = plt.subplots(figsize=(2.15, 1.85))
    # The caption names the axes: rows reuse, columns are reused.
    mixing, registries = _mixing_heatmap(ax, c)
    fig.tight_layout(pad=0.1)
    within = pd.Series({r: mixing.loc[r, r] for r in registries})
    c.stats["withinmax"] = fmt_pct(within.max())
    c.stats["withinmaxregistry"] = str(within.idxmax())
    c.stats["mixingregistries"] = fmt_int(len(registries))
    for r in COMMUNITIES:
        c.stats[f"within{r.lower()}"] = fmt_pct(within[r])
    # Where the reuse made by every other registry's ontologies goes.
    links = pd.DataFrame(
        [(c.home[u], c.home[v]) for u, v in c.corpus_links.edges()],
        columns=pd.Index(["source", "target"]),
    )
    others = links[~links["source"].isin(COMMUNITIES)]
    c.stats["otherstocommunities"] = fmt_pct(others["target"].isin(COMMUNITIES).mean() * 100)
    return fig


def fig_coupling_ratio(c: Corpus) -> plt.Figure:
    """Term-level coupling ratio, and against the namespace-level external dependency ratio."""
    p4 = c.p4
    cr = p4.loc[p4["ontology_iri"].notna(), "coupling_ratio"].dropna()
    ns = cr.index.to_series().map(c.external_ratio)

    fig, axes = plt.subplots(1, 2, figsize=(FULL, 1.85))
    ax = axes[0]
    ax.hist(
        cr, bins=numpy.linspace(0, 1, 26), color=KIND_PALETTE[0], edgecolor=SURFACE, linewidth=0.4
    )
    reference_line(ax, cr.median())
    ax.set_xlabel("coupling ratio (0 self-contained, 1 pure bridge)")
    ax.set_ylabel("ontologies")
    panel(ax, "a", "term-level coupling")

    ax = axes[1]
    # Namespace ratios are fractions with small denominators, so a scatter shows
    # stripes; a hexbin shows where the ontologies are.
    hb = ax.hexbin(
        ns,
        cr,
        gridsize=22,
        extent=(0, 1, 0, 1),
        mincnt=1,
        bins="log",
        vmin=0.3,
        cmap=SEQUENTIAL_CMAP,
        linewidths=0.15,
        edgecolors=SURFACE,
    )
    ax.plot([0, 1], [0, 1], color=INK, linestyle=":", linewidth=0.8)
    cb = fig.colorbar(hb, ax=ax, fraction=0.05, pad=0.02)
    cb.set_label("ontologies", fontsize=6)
    cb.ax.tick_params(labelsize=5, length=1.5)
    cb.outline.set_visible(False)
    ax.set_xlabel("namespace-level external dependency ratio")
    ax.set_ylabel("term-level coupling ratio")
    panel(ax, "b", "terms against namespaces")
    fig.tight_layout(w_pad=1.5)
    c.stats["couplingn"] = fmt_int(len(cr))
    c.stats["selfcontained"] = fmt_pct((cr == 0).mean() * 100)
    c.stats["purebridge"] = fmt_pct((cr == 1).mean() * 100)
    c.stats["couplingmedian"] = fmt_dec(cr.median())
    return fig


def fig_structure_shape(c: Corpus) -> plt.Figure:
    """Schema density against instance volume, and the depth and branching of hierarchies."""
    sd = c.p4["schema_density"].dropna()
    inst = c.p4.loc[sd.index, "instance_count"]
    n, m, s = len(sd), sd.mean(), sd.std()
    skew = ((sd - m) ** 3).mean() / s**3
    kurt = ((sd - m) ** 4).mean() / s**4 - 3
    bc = (skew**2 + 1) / (kurt + 3 * (n - 1) ** 2 / ((n - 2) * (n - 3)))
    hier = c.p4[c.p4["n_nodes"] > 0]
    max_d = hier["max_depth_long"].dropna()
    bf = hier["branching"].dropna()
    bf_pos = bf[bf > 0]

    fig = plt.figure(figsize=(FULL, 1.75))
    outer = fig.add_gridspec(
        1, 2, width_ratios=[2.6, 3.2], wspace=0.22, left=0.08, right=0.99, bottom=0.22, top=0.88
    )
    left = outer[0].subgridspec(1, 2, width_ratios=[2.1, 0.55], wspace=0.06)
    right = outer[1].subgridspec(1, 2, wspace=0.3)
    ax = fig.add_subplot(left[0])
    side = fig.add_subplot(left[1], sharey=ax)
    ax.hexbin(
        numpy.log10(inst + 1),
        sd,
        gridsize=(18, 12),
        extent=(0, numpy.log10(inst.max() + 1), -0.02, 1.02),
        mincnt=1,
        bins="log",
        vmin=0.3,
        cmap=SEQUENTIAL_CMAP,
        linewidths=0.15,
        edgecolors=SURFACE,
    )
    ticks = range(0, int(numpy.log10(inst.max() + 1)) + 1, 2)
    ax.set_xticks(list(ticks), [f"$10^{{{t}}}$" for t in ticks])
    ax.set_xlabel("individuals + 1")
    ax.set_ylabel("schema density")
    ax.set_ylim(-0.02, 1.02)
    panel(ax, "a", "ontology or knowledge base")
    side.hist(
        sd.to_numpy(),
        bins=numpy.linspace(0, 1, 41).tolist(),
        orientation="horizontal",
        color=KIND_PALETTE[0],
        edgecolor=SURFACE,
        linewidth=0.2,
    )
    side.set_xscale("log")
    side.xaxis.set_minor_formatter(NullFormatter())
    side.set_xlabel("ontologies")
    side.tick_params(axis="y", labelleft=False, length=0)
    side.spines["left"].set_visible(False)
    side.set_title(f"$b = {bc:.2f}$", loc="center", fontsize=6)

    ax = fig.add_subplot(right[0])
    ax.hist(
        max_d,
        bins=range(0, int(max_d.max()) + 2),
        color=KIND_PALETTE[0],
        edgecolor=SURFACE,
        linewidth=0.2,
    )
    reference_line(ax, max_d.median())
    ax.set_xlabel("longest chain below a root")
    ax.set_ylabel("ontologies")
    panel(ax, "b", "depth")
    ax = fig.add_subplot(right[1])
    ax.hist(
        bf_pos,
        bins=numpy.logspace(numpy.log10(bf_pos.min()), numpy.log10(bf_pos.max()), 30),
        color=KIND_PALETTE[0],
        edgecolor=SURFACE,
        linewidth=0.2,
    )
    reference_line(ax, bf.median())
    ax.set_xscale("log")
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("direct subclasses per parent")
    panel(ax, "c", "branching")

    c.stats["bimodality"] = fmt_dec(bc)
    c.stats["pureschema"] = fmt_pct((sd > 0.99).mean() * 100)
    c.stats["instanceheavy"] = fmt_pct((sd < 0.5).mean() * 100)
    density_stats(c, sd)
    c.stats["structn"] = fmt_int(len(c.p4))
    c.stats["hierarchyn"] = fmt_int(len(hier))
    c.stats["medmaxdepth"] = fmt_dec(max_d.median(), 0)
    c.stats["maxdepth"] = fmt_int(max_d.max())
    c.stats["medbranching"] = fmt_dec(bf.median(), 1)
    # Depth grows with size, slowly: the smaller half against the largest 1% by classes.
    cc = hier["class_count"]
    large = hier[cc >= cc.quantile(0.99)]
    c.stats["depthsmallhalf"] = fmt_dec(hier.loc[cc <= cc.median(), "max_depth_long"].median(), 0)
    c.stats["depthlarge"] = fmt_dec(large["max_depth_long"].median(), 0)
    c.stats["depthlargeclasses"] = fmt_int(large["class_count"].min())
    return fig


#: Registries whose schema-density profile the text describes.
DENSITY_REGISTRIES = ("OKG", "OBO", "LOV")


def density_stats(c: Corpus, sd: pd.Series) -> None:
    """Schema density: its two point masses, the shape between them, and three registries.

    The dip test is run on the full sample and on the values strictly between the point
    masses at 0 and 1, with tied values spread over half the gap to their neighbours, so
    that neither a point mass nor ties can make a single-peaked distribution look bimodal.
    """
    from diptest import diptest

    from ontometer_corpus.paper.stats import dequantise

    x = sd.to_numpy(dtype=float)
    mid = x[(x > 0) & (x < 1)]
    rng = numpy.random.default_rng(BOOTSTRAP_SEED)
    c.stats["densityone"] = fmt_pct((x == 1).mean() * 100)
    c.stats["densityzero"] = fmt_int((x == 0).sum())
    c.stats["densityzeroshare"] = fmt_pct((x == 0).mean() * 100)
    c.stats["densitymidn"] = fmt_int(len(mid))
    c.stats["densitydipall"] = fmt_p(diptest(x)[1])
    c.stats["densitydipmid"] = fmt_p(diptest(dequantise(mid, rng))[1])

    registry = c.p4.loc[sd.index, "home_registry"]
    for r in DENSITY_REGISTRIES:
        s = sd[registry == r]
        key = r.lower()
        c.stats[f"density{key}pure"] = fmt_pct((s > 0.99).mean() * 100)
        c.stats[f"density{key}instances"] = fmt_pct((s < 0.5).mean() * 100)
        c.stats[f"density{key}mixed"] = fmt_pct(((s >= 0.5) & (s <= 0.99)).mean() * 100)


#: Positions in the specialisation network of an ontology that owns a class and has a hierarchy.
GAP_POSITIONS = [
    ("both", "builds on others, built on"),
    ("builds", "builds on others only"),
    ("built", "built on only"),
    ("isolated", "isolated"),
]
#: Most redeclared labels named in the gaps figure.
GAP_LABELS_NAMED = 2
#: Labels declared by at least this many ontologies count as widely declared in the text.
REDECLARED_WIDE = 20
#: Labels with more classes than this per declaring ontology are repeats within ontologies
#: (value lists such as "yes" or "absent", stage-specific copies of one anatomical class), whose
#: groups under one parent count classes of one ontology rather than alignment across ontologies.
REPEATED_PER_ONTOLOGY = 2


def _repeated_within(labels: pd.DataFrame) -> pd.Series:
    """Labels repeated within the ontologies that declare them, beyond REPEATED_PER_ONTOLOGY."""
    return labels["n_terms"] > REPEATED_PER_ONTOLOGY * labels["n_homes"]


def _aligned_groups(labels: pd.DataFrame) -> pd.Series:
    """Per label, the largest number of its declarations under one shared parent.

    ``cover`` is that number over the declarations, rounded to three decimals, so rounding
    back recovers it for any label with fewer than 500 declarations.
    """
    return (labels["cover"] * labels["n_terms"]).round().astype(int)


def _hierarchy_positions(c: Corpus) -> pd.DataFrame:
    """Ontologies that own a class and have a subclass hierarchy, by registry and position in G.

    An ontology that owns no class can take no position in G, so it is left out
    rather than counted as isolated.
    """
    onts = pd.read_parquet(c.paths.ontology_abstractness_path)
    pairs = pd.read_parquet(c.paths.ontology_specialisations_path)
    onts = onts[(onts["hierarchy_nodes"] > 0) & (onts["n_home_classes"] > 0)]
    child = onts["onto"].isin(pairs["child"])
    parent = onts["onto"].isin(pairs["parent"])
    position = numpy.select(
        [child & parent, child, parent], ["both", "builds", "built"], default="isolated"
    )
    return pd.DataFrame(
        {
            "registry": onts["registry"],
            "position": position,
            "node_id": onts["node_id"],
            "family": onts["family"],
            "external_parents": onts["n_external_parents"].fillna(0),
        }
    )


def fig_gaps(c: Corpus) -> plt.Figure:
    """Where ontologies fail to connect: isolated hierarchies, and labels modelled again and again."""
    pos = _hierarchy_positions(c)
    order = [r for r in c.source_order if (pos["registry"] == r).sum() >= 20]
    share = (
        pd.crosstab(pos["registry"], pos["position"], normalize="index")
        .reindex(index=order, columns=[k for k, _ in GAP_POSITIONS], fill_value=0)
        .mul(100)
        .sort_values("isolated")
    )
    order = list(share.index)  # least isolated first
    labels = pd.read_parquet(c.paths.concept_redeclarations_path)
    labels = labels[~_repeated_within(labels)]
    labels = labels.assign(group=_aligned_groups(labels))

    fig = plt.figure(figsize=(FULL, 1.95))
    outer = fig.add_gridspec(
        1, 2, width_ratios=[1.55, 1.1], wspace=0.3, left=0.15, right=0.98, bottom=0.26, top=0.88
    )
    ax = fig.add_subplot(outer[0])
    y = numpy.arange(len(order))
    left = numpy.zeros(len(order))
    colors = [KIND_PALETTE[0], tint(KIND_PALETTE[0], 0.55), KIND_PALETTE[1], KIND_PALETTE[2]]
    for (key, label), color in zip(GAP_POSITIONS, colors):
        vals = share[key].to_numpy()
        ax.barh(y, vals, left=left, height=0.66, color=color, edgecolor=SURFACE, linewidth=0.5)
        if key == "isolated":
            for yi, (lft, v) in enumerate(zip(left, vals)):
                if v >= 8:
                    ax.text(
                        lft + v / 2,
                        yi,
                        f"{v:.0f}%",
                        ha="center",
                        va="center",
                        fontsize=5,
                        color=ink_on(color),
                    )
        left += vals
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of ontologies with a class hierarchy")
    _registry_axis(ax, order)
    ax.legend(
        handles=[Patch(facecolor=col, label=lab) for col, (_, lab) in zip(colors, GAP_POSITIONS)],
        loc="upper center",
        bbox_to_anchor=(0.42, -0.3),
        ncol=2,
        columnspacing=0.8,
    )
    panel(ax, "a", "position in $\\mathcal{G}$")

    # (b) Every redeclared label: its declarations against the largest group of them under one
    # shared parent. Labels share integer positions, so each position is one mark shaded by count.
    ax = fig.add_subplot(outer[1])
    cells = labels.groupby(["n_terms", "group"]).size().rename("n").reset_index()
    hi_x, hi_y = labels["n_terms"].max() * 1.6, 60
    norm = LogNorm(1, cells["n"].max())

    def shade(n):
        return SEQUENTIAL_CMAP(0.3 + 0.7 * norm(n))

    ax.plot([3, hi_y], [3, hi_y], color=NEUTRAL, linewidth=0.8, zorder=1)
    ax.axhline(1, color=NEUTRAL, linewidth=0.8, linestyle=(0, (3, 2)), zorder=1)
    order = cells.sort_values("n")
    ax.scatter(
        order["n_terms"], order["group"], s=3.5, marker="s", color=shade(order["n"]), linewidths=0
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(2.4, hi_x)
    ax.set_ylim(0.7, hi_y)
    for axis, ticks in ((ax.xaxis, [3, 10, 30, 100, 300]), (ax.yaxis, [1, 3, 10, 30])):
        axis.set_major_locator(FixedLocator(ticks))
        axis.set_major_formatter(FixedFormatter([str(t) for t in ticks]))
        axis.set_minor_formatter(NullFormatter())
    # The diagonal's label runs along it, so its angle comes from the axes' aspect.
    (x0, y0), (x1, y1) = ax.transData.transform([(4, 4), (40, 40)])
    ax.text(
        4.5,
        5.4,
        "all under one parent",
        fontsize=5,
        color=INK,
        rotation=math.degrees(math.atan2(y1 - y0, x1 - x0)),
        rotation_mode="anchor",
        ha="left",
        va="bottom",
    )
    ax.text(hi_x / 1.1, 1.12, "no two share a parent", fontsize=5, color=INK, ha="right")
    offsets = [((-3, 3), "right"), ((4, -1), "left")]
    named = labels.nlargest(GAP_LABELS_NAMED, ["n_homes", "n_terms"])
    for (_, r), (xy, ha) in zip(named.iterrows(), offsets):
        ax.scatter(
            r["n_terms"], r["group"], s=14, facecolors="none", edgecolors=INK, linewidths=0.5
        )
        ax.annotate(
            r["label"],
            (r["n_terms"], r["group"]),
            xytext=xy,
            textcoords="offset points",
            ha=ha,
            va="center",
            fontsize=5,
            color=INK,
        )
    ax.legend(
        handles=[
            Line2D([], [], marker="s", linestyle="", markersize=3, color=shade(n), label=f"{n:,}")
            for n in (1, 100, 10_000)
        ],
        title="labels",
        loc="upper left",
        bbox_to_anchor=(0.0, 1.0),
        frameon=False,
        fontsize=5,
        title_fontsize=5,
        handletextpad=0.1,
        labelspacing=0.3,
    )
    ax.set_xlabel("classes declared with the label")
    ax.set_ylabel("largest group under\none shared parent")
    panel(ax, "b", "redeclared labels")
    return fig


#: Redeclared labels the text names, with how many ontologies declare each.
REDECLARED_LISTED = 8


def gap_stats(c: Corpus) -> None:
    """The numbers of the gaps paragraphs: isolation in G, bridges in N, redeclared labels."""
    pos = _hierarchy_positions(c)
    isolated = pos["position"] == "isolated"
    unanchored = pos["position"].isin(["built", "isolated"])
    by_registry = isolated.groupby(pos["registry"]).mean()
    big = pos["registry"].value_counts()
    by_registry = by_registry[big.reindex(by_registry.index) >= 20]
    c.stats["gaphierarchies"] = fmt_int(len(pos))
    c.stats["gapisolated"] = fmt_int(isolated.sum())
    c.stats["gapisolatedshare"] = fmt_pct(isolated.mean() * 100)
    c.stats["gapunanchored"] = fmt_int(unanchored.sum())
    c.stats["gapunanchoredshare"] = fmt_pct(unanchored.mean() * 100)
    c.stats["gapisolatedmaxregistry"] = str(by_registry.idxmax())
    c.stats["gapisolatedmax"] = fmt_pct(by_registry.max() * 100)
    c.stats["gapisolatedminregistry"] = str(by_registry.idxmin())
    c.stats["gapisolatedmin"] = fmt_pct(by_registry.min() * 100)
    builds = pos["position"].isin(["both", "builds"])
    c.stats["specchildrenshare"] = fmt_pct(builds.mean() * 100)

    # Files with a subclass edge (the structural population) that take no position in G:
    # those owning none of their classes, and those whose edges touch only language terms.
    onts = pd.read_parquet(c.paths.ontology_abstractness_path)
    with_edges = set(c.p4.loc[c.p4["n_nodes"] > 0, "file_hash"])
    in_t = onts["hierarchy_nodes"] > 0
    c.stats["gapownerless"] = fmt_int((in_t & (onts["n_home_classes"] == 0)).sum())
    c.stats["gaplangonly"] = fmt_int(len(with_edges - set(onts.loc[in_t, "file_hash"])))

    # How the isolated are still connected, each counted under the first that applies.
    links = c.corpus_links
    iso = pos[isolated]
    external_parent = iso["external_parents"] > 0
    linked = iso["node_id"].map(lambda n: links.has_node(n) and links.degree(n) > 0)
    uses_any = iso["node_id"].map(lambda n: c.g.has_node(n) and c.g.out_degree(n) > 0)
    unlinked = ~external_parent & ~linked & ~uses_any
    c.stats["gapexternal"] = fmt_int(external_parent.sum())
    c.stats["gapotheraxioms"] = fmt_int((~external_parent & linked).sum())
    c.stats["gapexternalvocab"] = fmt_int((~external_parent & ~linked & uses_any).sum())
    c.stats["gapunlinked"] = fmt_int(unlinked.sum())
    c.stats["gapunlinkedshare"] = fmt_pct(unlinked.sum() / len(pos) * 100)

    across = sum({c.home.get(u), c.home.get(v)} == set(COMMUNITIES) for u, v in links.edges())
    c.stats["bridgen"] = fmt_int(len(bridges(links, c.home)))
    c.stats["bridgeacross"] = fmt_int(across)
    c.stats["bridgeacrossshare"] = fmt_pct(across / links.number_of_edges() * 100)

    labels = pd.read_parquet(c.paths.concept_redeclarations_path)
    top = labels.nlargest(2, ["n_homes", "n_terms"])
    wide = labels[labels["n_homes"] >= 10]
    c.stats["redeclaredn"] = fmt_int(len(labels))
    c.stats["redeclarednone"] = fmt_pct((labels["best_common_ancestor"] == "").mean() * 100)
    c.stats["redeclaredten"] = fmt_int(len(wide))
    c.stats["redeclaredtencover"] = fmt_dec(wide["cover"].median())
    c.stats["redeclaredtenlow"] = fmt_pct((wide["cover"] < 0.25).mean() * 100)
    repeated = _repeated_within(labels)
    widest = labels["n_homes"] >= REDECLARED_WIDE
    c.stats["redeclaredrepeated"] = fmt_int(repeated.sum())
    c.stats["redeclaredrepeatedmax"] = fmt_int(REPEATED_PER_ONTOLOGY)
    c.stats["redeclaredwide"] = fmt_int(REDECLARED_WIDE)
    c.stats["redeclaredwiden"] = fmt_int((widest & ~repeated).sum())
    c.stats["redeclaredwidegroup"] = fmt_int(_aligned_groups(labels[widest & ~repeated]).median())
    listed = labels.nlargest(REDECLARED_LISTED, ["n_homes", "n_terms"])
    named = [
        f"\\emph{{{_tex_escape(str(r['label']))}}} ({r['n_homes']})" for _, r in listed.iterrows()
    ]
    c.stats["redeclaredtop"] = ", ".join(named[:-1]) + " and " + named[-1]
    for i, (_, row) in enumerate(top.iterrows()):
        c.stats[f"redeclared{'ab'[i]}"] = _tex_escape(str(row["label"]))
        c.stats[f"redeclared{'ab'[i]}n"] = fmt_int(row["n_homes"])
        c.stats[f"redeclared{'ab'[i]}cover"] = fmt_dec(row["cover"])


#: First year of the recent period whose births and growth the text summarises.
BIRTHS_RECENT_FROM = 2018
#: The years of fastest growth, whose yearly births the text sets against the recent period.
BIRTHS_EARLY = (2010, 2015)
#: Starts of the periods of first evidence in the cohort panel, three years each; an open
#: period comes before the first, and the last runs to the capture year.
COHORT_BREAKS = (2011, 2014, 2017, 2020, 2023)


def births_stats(c: Corpus) -> None:
    """New ontologies per year by first-evidence birth: the recent rate and the peak."""
    d = pd.read_parquet(c.paths.ontology_dates_path)
    years = pd.to_datetime(d["birth"], errors="coerce").dt.year.dropna().astype(int)
    per_year = years.value_counts().sort_index()
    recent = per_year.reindex(range(BIRTHS_RECENT_FROM, 2026), fill_value=0)
    c.stats["birthsrecent"] = fmt_int(recent.median())
    c.stats["birthsrecentmin"] = fmt_int(recent.min())
    c.stats["birthsrecentmax"] = fmt_int(recent.max())
    c.stats["birthspeakyear"] = str(int(per_year.idxmax()))
    c.stats["birthspeak"] = fmt_int(per_year.max())


def _extremes_hist(ax, values: pd.Series, xlabel: str) -> None:
    """A histogram of a ratio in [0, 1], with its point masses at exactly 0 and 1 set apart."""
    x = values.to_numpy(dtype=float)
    inner = x[(x > 0) & (x < 1)]
    width = 0.05
    ax.hist(
        inner,
        bins=numpy.linspace(0, 1, 21),
        color=KIND_PALETTE[0],
        edgecolor=SURFACE,
        linewidth=0.4,
    )
    for at, mass in ((-1.6 * width, (x == 0).sum()), (1 + 1.6 * width, (x == 1).sum())):
        ax.bar(at, mass, width=width * 1.4, color=KIND_PALETTE[2], edgecolor=SURFACE, linewidth=0.4)
        ax.text(
            at,
            mass * 1.15,
            f"{mass / len(x):.0%}",
            ha="center",
            va="bottom",
            fontsize=5.5,
            color=INK,
        )
    ax.set_yscale("log")
    ax.set_ylim(top=len(x) * 1.2)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xticks([-1.6 * width, 0.25, 0.5, 0.75, 1 + 1.6 * width], ["0", ".25", ".5", ".75", "1"])
    ax.set_xlim(-0.16, 1.16)
    ax.set_xlabel(xlabel)


def fig_extremes(c: Corpus) -> plt.Figure:
    """Ontologies sit at the extremes: schema density, coupling, and coupling by namespace."""
    p4 = c.p4
    sd = p4["schema_density"].dropna()
    cr = p4.loc[p4["ontology_iri"].notna(), "coupling_ratio"].dropna()
    ns = cr.index.to_series().map(c.external_ratio)

    fig = plt.figure(figsize=(FULL, 1.8))
    gs = fig.add_gridspec(
        1, 3, width_ratios=[1, 1, 1.12], wspace=0.42, left=0.075, right=0.95, bottom=0.24, top=0.88
    )
    ax = fig.add_subplot(gs[0])
    _extremes_hist(ax, sd, "schema density")
    ax.set_ylabel("ontologies")
    panel(ax, "a", "data (0) or schema (1)")
    ax = fig.add_subplot(gs[1])
    _extremes_hist(ax, cr, "coupling ratio")
    panel(ax, "b", "own (0) or borrowed (1)")

    ax = fig.add_subplot(gs[2])
    hb = ax.hexbin(
        ns,
        cr,
        gridsize=18,
        extent=(0, 1, 0, 1),
        mincnt=1,
        bins="log",
        vmin=0.3,
        cmap=SEQUENTIAL_CMAP,
        linewidths=0.15,
        edgecolors=SURFACE,
    )
    ax.plot([0, 1], [0, 1], color=INK, linestyle=":", linewidth=0.8)
    cb = fig.colorbar(hb, ax=ax, fraction=0.06, pad=0.03)
    cb.set_label("ontologies", fontsize=6)
    cb.ax.tick_params(labelsize=5, length=1.5)
    cb.outline.set_visible(False)
    ax.set_xlabel("namespace coupling")
    ax.set_ylabel("term coupling")
    panel(ax, "c", "terms against namespaces")
    below = (cr < ns).mean() * 100
    c.stats["couplingbelowdiag"] = fmt_pct(below)
    return fig


def fig_births(c: Corpus) -> plt.Figure:
    """New ontologies per year by first-evidence date, by home registry, and the relative growth."""
    d = pd.read_parquet(c.paths.ontology_dates_path)
    d["year"] = pd.to_datetime(d["birth"], errors="coerce").dt.year
    dated = d.dropna(subset=["year"]).copy()
    first, last = 2000, int(dated["year"].max())
    dated["bin"] = dated["year"].clip(lower=first).astype(int)
    dated["group"] = dated["registry"].map(lambda r: c.group.get(r, OTHER))
    table = dated.pivot_table(index="bin", columns="group", aggfunc="size", fill_value=0)
    groups = [g for g in dict.fromkeys(c.group[r] for r in c.source_order) if g in table.columns]
    bins = list(range(first, last + 1))
    table = table.reindex(index=bins, columns=groups, fill_value=0)
    per_year = table.sum(axis=1)
    total = per_year.cumsum()
    as_of = pd.Timestamp(str(d["as_of"].dropna().iloc[0]))
    partial = as_of.year == last

    github = c.github
    fig = plt.figure(figsize=(FULL, 1.95))
    ratios = [2.3, 1] if github is None else [1.6, 1.05, 1]
    gs = fig.add_gridspec(
        1,
        len(ratios),
        width_ratios=ratios,
        wspace=0.5 if github is None else 0.72,
        left=0.08,
        right=0.985,
        bottom=0.3,
        top=0.88,
    )
    ax = fig.add_subplot(gs[0])
    bottom = numpy.zeros(len(bins))
    for g in groups:
        vals = table[g].to_numpy()
        ax.bar(
            bins,
            vals,
            bottom=bottom,
            width=0.8,
            color=c.group_color[g],
            label=g,
            edgecolor=SURFACE,
            linewidth=0.2,
        )
        bottom += vals
    if partial:
        ax.bar(
            last,
            per_year[last],
            width=0.8,
            fill=False,
            hatch="//////",
            edgecolor=INK,
            linewidth=0,
            alpha=0.55,
        )
    ticks = bins[::5]
    ax.set_xticks(ticks, [f"\u2264{first}", *map(str, ticks[1:])])
    ax.set_xlim(bins[0] - 0.7, bins[-1] + 0.7)
    ax.set_ylabel("new ontologies")
    twin = ax.twinx()
    twin.plot(bins, total.to_numpy(), color=INK, linewidth=1.0)
    twin.set_ylim(0, total.iloc[-1] * 1.08)
    twin.annotate(
        "cumulative",
        (bins[-1], total.iloc[-1]),
        xytext=(-3, 5),
        textcoords="offset points",
        ha="right",
        fontsize=5.5,
        color=INK,
    )
    twin.spines["right"].set_visible(True)
    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=4, columnspacing=0.8, handlelength=1.0
    )
    panel(ax, "a", "new ontologies per year")

    if github is None:
        _cohort_panel(fig.add_subplot(gs[1]), c, dated, last, "b")
    else:
        _divergence_panel(fig.add_subplot(gs[1]), c, dated, github, as_of)
        _coverage_panel(fig.add_subplot(gs[2]), c, github, dated, as_of)

    years = [y for y in bins if y >= 2005 and not (partial and y == last)]
    rate = [per_year[y] / total[y - 1] * 100 for y in years]
    recent = [y for y in years if y >= BIRTHS_RECENT_FROM]
    # Mean new ontologies a year in the early peak and the recent period, overall and by the
    # registries that carry the change; the capture year is incomplete and left out.
    families = {
        "all": dated["registry"].notna(),
        "lov": dated["registry"] == "LOV",
        "obo": dated["registry"] == "OBO",
        "portal": dated["registry"].str.contains("Portal"),
    }
    for period, (lo, hi) in (("early", BIRTHS_EARLY), ("recent", (BIRTHS_RECENT_FROM, last - 1))):
        span = dated["year"].between(lo, hi)
        for key, mask in families.items():
            c.stats[f"births{key}{period}"] = fmt_int((span & mask).sum() / (hi - lo + 1))
    # The same means from each source of evidence on its own, whichever is earliest, so the
    # fall cannot come from how the earliest date is chosen.
    for key, col in (("registry", "d_registry"), ("archive", "d_archive")):
        year = pd.to_datetime(d[col], errors="coerce").dt.year
        for period, (lo, hi) in (
            ("early", BIRTHS_EARLY),
            ("recent", (BIRTHS_RECENT_FROM, last - 1)),
        ):
            c.stats[f"births{key}{period}"] = fmt_int(year.between(lo, hi).sum() / (hi - lo + 1))
    c.stats["birthsearlyyears"] = f"{BIRTHS_EARLY[0]}--{BIRTHS_EARLY[1]}"
    c.stats["birthsrecentyears"] = f"{BIRTHS_RECENT_FROM}--{last - 1}"
    c.stats["birthsgrowthpeak"] = fmt_pct(max(rate))
    c.stats["birthsgrowthpeakyear"] = str(years[int(numpy.argmax(rate))])
    c.stats["birthsgrowthrecent"] = fmt_pct(numpy.median([rate[years.index(y)] for y in recent]))
    return fig


#: Years after its first commit within which a GitHub ontology counts as registered promptly.
REGISTRATION_WINDOW = 3


def _periods(last: int) -> tuple[list[float], list[str], list[str]]:
    """Bin edges, axis labels and LaTeX labels of the periods of first appearance."""
    breaks = list(COHORT_BREAKS)
    ends = [b - 1 for b in breaks[1:]] + [last]
    labels = [f"\u2264{breaks[0] - 1}"] + [f"{b}\u2013{e % 100:02d}" for b, e in zip(breaks, ends)]
    tex = [f"up to {breaks[0] - 1}"] + [f"{b}--{e}" for b, e in zip(breaks, ends)]
    return [-numpy.inf, *breaks, numpy.inf], labels, tex


def _coverage_panel(
    ax, c: Corpus, github: pd.DataFrame, dated: pd.DataFrame, as_of: pd.Timestamp
) -> None:
    """Share of the sampled authored GitHub ontologies, by period of first commit, that a
    registry holds, with a 95% band from resampling the sampling windows.

    Each distinct ontology IRI counts once; generated files, test and example files and shapes
    that declare ``owl:Ontology`` are left out. The prompt share counts only registrations
    within ``REGISTRATION_WINDOW`` years of the first commit, so it is drawn only for periods
    that ended at least that long before the capture.
    """
    g = github[github["kind"] == "ontology"].copy()
    g["year"] = g["first_commit"].dt.year
    edges, labels, tex = _periods(as_of.year)
    g["period"] = pd.cut(g["year"], edges, right=False, labels=range(len(labels)))
    window = pd.Timedelta(days=round(365.25 * REGISTRATION_WINDOW))
    g["prompt"] = g["registered"] & (g["registry_date"] <= g["first_commit"] + window)
    shares = g.groupby("period", observed=False)[["registered", "prompt"]].mean().mul(100)
    counts = g.groupby("period", observed=False).size()
    xs = numpy.arange(len(labels))
    closed = numpy.array(
        [e <= as_of.year - REGISTRATION_WINDOW for e in [*edges[1:-1], as_of.year + 1]]
    )
    band = _window_bootstrap(g, len(labels))
    ax.fill_between(xs, band["lo"], band["hi"], color=NEUTRAL_LIGHT, linewidth=0, zorder=0)
    ax.plot(xs, shares["registered"], marker="o", markersize=2.2, color=INK, linewidth=1.2)
    ax.plot(
        xs[closed],
        shares["prompt"][closed],
        marker="o",
        markersize=2.2,
        color=TEXT_MUTED,
        linewidth=1.0,
        linestyle=(0, (3, 1.5)),
    )
    peak = int(numpy.argmax(shares["registered"].to_numpy()))
    ax.annotate(
        "held by a registry",
        (xs[peak], shares["registered"].iloc[peak]),
        xytext=(3, 4),
        textcoords="offset points",
        fontsize=5.5,
        color=INK,
    )
    ax.annotate(
        f"within {REGISTRATION_WINDOW} years",
        (xs[0], shares["prompt"].iloc[0]),
        xytext=(-2, -9),
        textcoords="offset points",
        fontsize=5.5,
        color=TEXT_MUTED,
    )
    top = max(band["hi"].max() * 1.1, 10)
    ax.set_ylim(0, top)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}%"))
    ax.set_xticks(xs, labels, rotation=40, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", labelsize=5.5)
    ax.set_ylabel("of GitHub ontologies")
    panel(ax, "c", "registered")

    c.stats["githubn"] = fmt_int(len(github))
    c.stats["githubauthored"] = fmt_int(len(g))
    c.stats["githubartifactshare"] = fmt_pct((github["kind"] != "ontology").mean() * 100)
    capture = github[github["first_commit"].dt.year == as_of.year]
    c.stats["githubartifactcapture"] = fmt_pct((capture["kind"] != "ontology").mean() * 100)
    c.stats["githubcaptureyear"] = str(as_of.year)
    if c.github_dir is not None:
        plan = pd.read_csv(c.github_dir / "plan.csv")
        sampled = pd.read_csv(c.github_dir / "sample_enriched.csv")
        c.stats["githubindexed"] = fmt_int(plan["files"].sum())
        c.stats["githubsampled"] = fmt_int(len(sampled))
        c.stats["githubwithiri"] = fmt_int(sampled["iri"].notna().sum())
        c.stats["githubbins"] = fmt_int((plan["files"] > 0).sum())
    c.stats["githubregistered"] = fmt_pct(g["registered"].mean() * 100)
    c.stats["githubcoveragefirst"] = fmt_pct(shares["registered"].iloc[0])
    c.stats["githubcoveragelast"] = fmt_pct(shares["registered"].iloc[-1])
    prompt = shares["prompt"][closed].dropna()
    c.stats["githubpromptfirst"] = fmt_pct(prompt.iloc[0])
    c.stats["githubpromptlast"] = fmt_pct(prompt.iloc[-1])
    c.stats["githubpromptlastperiod"] = tex[int(prompt.index[-1])]
    c.stats["githubperiodfirst"] = tex[0]
    c.stats["githubperiodlast"] = tex[-1]
    c.stats["githubnfirst"] = fmt_int(counts.iloc[0])
    c.stats["githubnlast"] = fmt_int(counts.iloc[-1])
    c.stats["githubwindow"] = str(REGISTRATION_WINDOW)
    c.stats["githubcoveragepeak"] = fmt_pct(shares["registered"].iloc[peak])
    c.stats["githubcoveragepeakperiod"] = tex[peak]
    for at, name in ((peak, "peak"), (len(labels) - 1, "last")):
        c.stats[f"githubcoverage{name}ci"] = (
            f"{band['lo'].iloc[at]:.1f}--{band['hi'].iloc[at]:.1f}\\%"
        )
    c.stats["githubbootstrapn"] = fmt_int(N_WINDOW_BOOTSTRAP)
    _recall_stats(c, shares["registered"].iloc[peak] / 100, as_of)
    _signal_stats(c, as_of)
    c.stats["githubpromptpeak"] = fmt_pct(shares["prompt"].iloc[peak])
    # New GitHub ontologies a year in the sample, from the peak period to the last one.
    span = [3] * len(labels)
    span[-1] = as_of.year - COHORT_BREAKS[-1] + 1
    yearly = counts / pd.Series(span, index=counts.index)
    c.stats["githubfold"] = f"{yearly.iloc[-1] / yearly.iloc[peak]:.0f}"
    # Two-list (Lincoln-Petersen) estimate per period: registry births over the registries'
    # coverage of GitHub ontologies. The lists are not independent, so it is a lower bound.
    births = pd.cut(dated["year"], edges, right=False, labels=range(len(labels)))
    n_reg = births.value_counts().sort_index()
    estimate = n_reg / shares["registered"].div(100)
    c.stats["githubestimatefirst"] = fmt_int(estimate.iloc[0])
    c.stats["githubestimatelast"] = fmt_int(estimate.iloc[-1])


#: Resamples of the sampling windows behind the coverage band.
N_WINDOW_BOOTSTRAP = 2000


def _window_bootstrap(g: pd.DataFrame, n_periods: int) -> pd.DataFrame:
    """95% interval of the registered share per period, resampling whole sampling windows."""
    rng = numpy.random.default_rng(BOOTSTRAP_SEED)
    groups = [d for _, d in g.groupby("window")]
    draws = []
    for _ in range(N_WINDOW_BOOTSTRAP):
        pick = pd.concat([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        draws.append(
            pick.groupby("period", observed=False)["registered"].mean().reindex(range(n_periods))
        )
    boot = pd.DataFrame(draws).mul(100)
    return pd.DataFrame({"lo": boot.quantile(0.025), "hi": boot.quantile(0.975)})


#: Years before the capture within which a repository's last push counts as recent activity.
ACTIVE_YEARS = 2


def _recall_stats(c: Corpus, coverage: float, as_of: pd.Timestamp) -> None:
    """How often code search finds the ontologies of repositories the registries name, for
    recently active and inactive repositories, and the coverage a worst case would leave.

    The worst case puts every unregistered ontology in an inactive repository and every
    registered one in an active repository, and corrects the observed coverage for both recalls.
    """
    recall = pd.read_csv(c.github_dir / "recall.csv").dropna(subset=["found", "pushed"])
    recall["found"] = recall["found"].astype(bool)
    active = pd.to_datetime(recall["pushed"], utc=True).dt.year >= as_of.year - ACTIVE_YEARS
    found_active = recall.loc[active, "found"].mean()
    found_inactive = recall.loc[~active, "found"].mean()
    worst = (coverage / found_active) / (coverage / found_active + (1 - coverage) / found_inactive)
    c.stats["githubrecalln"] = fmt_int(len(recall))
    c.stats["githubrecall"] = fmt_pct(recall["found"].mean() * 100)
    c.stats["githubrecallactive"] = fmt_pct(found_active * 100)
    c.stats["githubrecallinactive"] = fmt_pct(found_inactive * 100)
    c.stats["githubactivefrom"] = str(as_of.year - ACTIVE_YEARS)
    c.stats["githubcoverageworst"] = fmt_pct(worst * 100)


def _signal_stats(c: Corpus, as_of: pd.Timestamp) -> None:
    """Signs of LLM assistance among authored GitHub ontologies, in the capture year and before."""
    sig = pd.read_csv(c.github_dir / "signals.csv")
    now, before = sig[sig["year"] == as_of.year], sig[sig["year"] < as_of.year - 1]
    c.stats["githubaicommit"] = fmt_pct(now["commit_ai"].mean() * 100)
    c.stats["githubaicommitn"] = fmt_int(now["commit_ai"].sum())
    c.stats["githubaicommitbefore"] = fmt_int(before["commit_ai"].sum())
    c.stats["githubaibeforeyears"] = f"{int(sig['year'].min())}--{as_of.year - 2}"
    c.stats["githubaifile"] = fmt_int(sig["file_ai"].sum())


def _cohort_panel(ax, c: Corpus, dated: pd.DataFrame, last: int, letter: str | None) -> None:
    """Share of each period's new class-owning ontologies that specialise, or are specialised by, another.

    Positions are those of the capture, so a recent cohort has had less time to be built on.
    """
    pos = _hierarchy_positions(c).merge(dated[["family", "year"]], on="family")
    edges, labels, tex = _periods(last)
    pos["cohort"] = pd.cut(pos["year"], edges, right=False, labels=range(len(labels)))
    pos["builds"] = pos["position"].isin(["builds", "both"])
    pos["built"] = pos["position"].isin(["built", "both"])
    shares = pos.groupby("cohort", observed=False)[["builds", "built"]].mean().mul(100)
    xs = numpy.arange(len(labels))
    lines = (
        ("builds", "builds on another", {"color": INK, "linewidth": 1.2}),
        (
            "built",
            "built on by another",
            {"color": TEXT_MUTED, "linewidth": 1.0, "linestyle": (0, (3, 1.5))},
        ),
    )
    for key, label, kw in lines:
        ax.plot(xs, shares[key], marker="o", markersize=2.2, **kw)
        ax.annotate(
            label,
            (xs[-1], shares[key].iloc[-1]),
            xytext=(0, 4 if key == "builds" else -4),
            textcoords="offset points",
            ha="right",
            va="bottom" if key == "builds" else "top",
            fontsize=5.5,
            color=kw["color"],
        )
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}%"))
    ax.set_xticks(xs, labels, rotation=40, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", labelsize=5.5)
    ax.set_ylabel("of the period's ontologies")
    panel(ax, letter, "specialisation by period")

    first, final = shares.iloc[0], shares.iloc[-1]
    c.stats["cohortfirst"] = tex[0]
    c.stats["cohortlast"] = tex[-1]
    c.stats["cohortn"] = fmt_int(len(pos))
    c.stats["cohortbuildsfirst"] = fmt_pct(first["builds"])
    c.stats["cohortbuildslast"] = fmt_pct(final["builds"])
    c.stats["cohortbuiltfirst"] = fmt_pct(first["built"])
    c.stats["cohortbuiltlast"] = fmt_pct(final["built"])
    obo = pos[pos["registry"] == "OBO"]
    obo_builds = obo.groupby("cohort", observed=False)["builds"].mean().mul(100).dropna()
    c.stats["cohortobobuildsfirst"] = fmt_pct(obo_builds.iloc[0])
    c.stats["cohortobobuildslast"] = fmt_pct(obo_builds.iloc[-1])
    c.stats["cohortobofirst"] = tex[int(obo_builds.index[0])]


def fig_cohorts(c: Corpus) -> plt.Figure:
    """Specialisation by period of first evidence, on its own (the extended version's figure)."""
    d = pd.read_parquet(c.paths.ontology_dates_path)
    d["year"] = pd.to_datetime(d["birth"], errors="coerce").dt.year
    dated = d.dropna(subset=["year"]).copy()
    fig, ax = plt.subplots(figsize=(HALF, 1.9))
    _cohort_panel(ax, c, dated, int(dated["year"].max()), None)
    fig.tight_layout()
    return fig


#: First years of the three-year periods the divergence panel compares; the last ends the
#: year before the capture, so that no period is partial.
DIVERGENCE_STARTS = (2008, 2011, 2014, 2017, 2020, 2023)
#: The period every series is indexed to.
DIVERGENCE_BASE = 2011


def _divergence_panel(
    ax, c: Corpus, dated: pd.DataFrame, github: pd.DataFrame, as_of: pd.Timestamp
) -> None:
    """New ontologies a year in the registries and on GitHub, and new GitHub repositories,
    each indexed to its rate in the base period, on one log axis.

    GitHub ontologies are the authored ones in the sample, each weighted by its sampling
    weight; repositories are GitHub's own yearly counts (``ontometer-corpus paper github baseline``).
    """
    starts = [s for s in DIVERGENCE_STARTS if s + 2 < as_of.year]
    labels = [f"{s}\u2013{(s + 2) % 100:02d}" for s in starts]

    def per_period(years: pd.Series, weights: pd.Series | None = None) -> pd.Series:
        w = pd.Series(1.0, index=years.index) if weights is None else weights
        return pd.Series(
            [w[(years >= s) & (years <= s + 2)].sum() / 3 for s in starts], index=starts
        )

    authored = github[github["kind"] == "ontology"]
    repos = pd.read_csv(c.github_dir / "baseline.csv").set_index("year")["repositories"]
    series = {
        "registries": per_period(dated["year"]),
        "GitHub ontologies": per_period(authored["first_commit"].dt.year, authored["weight"]),
        "GitHub repositories": pd.Series(
            [repos.loc[s : s + 2].mean() for s in starts], index=starts
        ),
    }
    styles = {
        "registries": {"color": INK, "linewidth": 1.3, "marker": "o", "markersize": 2.2},
        "GitHub ontologies": {
            "color": INK,
            "linewidth": 1.0,
            "linestyle": (0, (3, 1.5)),
            "marker": "o",
            "markersize": 2.2,
        },
        "GitHub repositories": {"color": TEXT_MUTED, "linewidth": 0.9, "linestyle": ":"},
    }
    xs = numpy.arange(len(starts))
    index = {}
    for name, values in series.items():
        index[name] = values / values.loc[DIVERGENCE_BASE] * 100
        ax.plot(xs, index[name].to_numpy(), **styles[name])
        # Repositories are named mid-way, where their curve runs clear above the others.
        at = len(xs) - 2 if name == "GitHub repositories" else len(xs) - 1
        ax.annotate(
            name,
            (xs[at], index[name].iloc[at]),
            xytext=(-4, 2) if name != "registries" else (-2, -3),
            textcoords="offset points",
            ha="right",
            va="bottom" if name != "registries" else "top",
            fontsize=5.5,
            color=styles[name]["color"],
        )
    ax.set_yscale("log")
    ax.axhline(100, color=NEUTRAL, linewidth=0.6, zorder=0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xticks(xs, labels, rotation=40, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="x", labelsize=5.5)
    ax.set_ylabel(f"per year, {labels[starts.index(DIVERGENCE_BASE)]} = 100")
    panel(ax, "b", "registries and GitHub")

    c.stats["divbase"] = f"{DIVERGENCE_BASE}--{DIVERGENCE_BASE + 2}"
    c.stats["divlast"] = f"{starts[-1]}--{starts[-1] + 2}"
    for key, name in (
        ("registries", "registries"),
        ("github", "GitHub ontologies"),
        ("repos", "GitHub repositories"),
    ):
        c.stats[f"div{key}"] = fmt_int(index[name].iloc[-1])
        c.stats[f"div{key}fold"] = f"{index[name].iloc[-1] / 100:.1f}"


def fig_release_years(c: Corpus) -> plt.Figure:
    """Distinct parsed versions per release year, stacked by the registry that published them."""
    rows = c._sql(
        "SELECT source_registry, source_version_date FROM corpus_registry "
        "WHERE parse_status = 'ok' AND is_duplicate = 0"
    )
    year = pd.to_numeric(rows["source_version_date"].str[:4], errors="coerce")
    dated = rows.assign(year=year).dropna(subset=["year"])
    # Dates before 2000 are registry placeholders or legacy stamps; one bar holds them all.
    first = 2000
    dated["bin"] = dated["year"].clip(lower=first - 1).astype(int)
    dated["group"] = dated["source_registry"].map(lambda r: c.group.get(r, OTHER))
    table = dated.pivot_table(index="bin", columns="group", aggfunc="size", fill_value=0)
    groups = [g for g in dict.fromkeys(c.group[r] for r in c.source_order) if g in table.columns]
    bins = list(range(first - 1, int(dated["bin"].max()) + 1))
    table = table.reindex(index=bins, columns=groups, fill_value=0)

    fig, ax = plt.subplots(figsize=(FULL, 1.9))
    bottom = numpy.zeros(len(bins))
    for g in groups:
        vals = table[g].to_numpy()
        ax.bar(bins, vals, bottom=bottom, width=0.8, color=c.group_color[g], label=g)
        bottom += vals
    ticks = bins[::2]
    labels = [f"<{first}" if b < first else str(b) for b in ticks]
    ax.set_xticks(ticks, labels, rotation=45, ha="right", rotation_mode="anchor")
    ax.set_xlim(bins[0] - 0.7, bins[-1] + 0.7)
    ax.set_ylabel("distinct versions")
    ax.legend(loc="upper left", ncol=2)
    fig.tight_layout()

    c.stats["versionsdated"] = fmt_int(len(dated))
    c.stats["versionsundated"] = fmt_int(len(rows) - len(dated))
    c.stats["versionssince2010"] = fmt_pct((dated["year"] >= 2010).mean() * 100)
    c.stats["versionspeakyear"] = str(int(table.sum(axis=1).idxmax()))
    return fig


def fig_dating_sources(c: Corpus) -> plt.Figure:
    """Which source dates each ontology, by registry, and how far first evidence moves a date."""
    d = pd.read_parquet(c.paths.ontology_dates_path)
    sources = [("registry", "registry date"), ("archive", "web archive"), ("wikidata", "Wikidata")]
    d["source"] = d["birth_source"].fillna("undated")
    order = [r for r in c.source_order if r in set(d["registry"])]
    share = (
        pd.crosstab(d["registry"], d["source"], normalize="index")
        .reindex(index=order, columns=[s for s, _ in sources] + ["undated"], fill_value=0)
        .mul(100)
    )
    reg = pd.to_datetime(d["d_registry"], errors="coerce")
    birth = pd.to_datetime(d["birth"], errors="coerce")
    gap = ((reg - birth).dt.days / 365.25)[lambda g: g > 0]

    fig, axes = plt.subplots(1, 2, figsize=(FULL, 1.9), gridspec_kw={"width_ratios": [1.3, 1]})
    ax = axes[0]
    y = numpy.arange(len(order))
    left = numpy.zeros(len(order))
    colors = [*KIND_PALETTE, NEUTRAL_LIGHT]
    labels = [label for _, label in sources] + ["undated"]
    for col, color, label in zip(share.columns, colors, labels):
        vals = share[col].to_numpy()
        ax.barh(y, vals, left=left, height=0.66, color=color, edgecolor=SURFACE, linewidth=0.4)
        left += vals
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of ontologies")
    _registry_axis(ax, order)
    ax.legend(
        handles=[Patch(facecolor=col, label=lab) for col, lab in zip(colors, labels)],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.3),
        ncol=4,
        columnspacing=0.8,
    )
    panel(ax, "a", "source of the first-evidence date")

    ax = axes[1]
    ax.hist(gap, bins=numpy.arange(0, gap.max() + 1, 1), color=KIND_PALETTE[1])
    reference_line(ax, gap.median())
    ax.set_xlabel("years before the registry date")
    ax.set_ylabel("ontologies")
    panel(ax, "b", "dates moved earlier")
    fig.tight_layout(w_pad=1.0)

    by_registry = d.groupby("registry")["birth"].apply(lambda s: s.notna().mean() * 100)
    weakest = by_registry.sort_values().head(2)
    c.stats["datedfull"] = fmt_int((by_registry == 100).sum())
    c.stats["datedweakest"] = " and ".join(f"{r} ({fmt_pct(v)})" for r, v in weakest.items())
    c.stats["movedearlier"] = fmt_int(len(gap))
    c.stats["withregistrydate"] = fmt_int(reg.notna().sum())
    c.stats["movedmedianyears"] = fmt_dec(gap.median(), 1)
    return fig


def time_stats(c: Corpus) -> None:
    """The numbers of the dating section: coverage, link timing and the dating check."""
    d = pd.read_parquet(c.paths.ontology_dates_path)
    src = d["birth_source"]
    c.stats["datingn"] = fmt_int(len(d))
    c.stats["dated"] = fmt_int(d["birth"].notna().sum())
    c.stats["datedshare"] = fmt_pct(d["birth"].notna().mean() * 100)
    c.stats["undated"] = fmt_int(d["birth"].isna().sum())
    for source in ("registry", "archive", "wikidata"):
        c.stats[f"birthby{source}"] = fmt_int((src == source).sum())

    net = load_tracked(c.paths)
    child, parent = net.dep["child"].to_numpy(), net.dep["parent"].to_numpy()
    pairs = pd.read_parquet(c.paths.ontology_specialisations_path)
    panel_timed = pairs["panel_first"].notna()
    c.stats["specpairs"] = fmt_int(len(pairs))
    c.stats["panelpairs"] = fmt_int(panel_timed.sum())
    c.stats["panelpairshare"] = fmt_pct(panel_timed.mean() * 100)
    c.stats["fallbackpairshare"] = fmt_pct((~panel_timed).mean() * 100)

    moved = ~numpy.isnan(net.b_bnd) & (numpy.isnan(net.b_obs) | (net.b_bnd < net.b_obs))
    c.stats["boundmoved"] = fmt_int(moved.sum())
    c.stats["bounddated"] = fmt_int((numpy.isnan(net.b_obs) & ~numpy.isnan(net.b_bnd)).sum())

    c.stats["nperm"] = fmt_int(N_BOOTSTRAP)
    for key, births in (("bounded", net.b_bnd), ("first", net.b_obs)):
        check = dating_consistency(births, child, parent, reps=N_BOOTSTRAP, seed=BOOTSTRAP_SEED)
        c.stats[f"cons{key}"] = fmt_pct(check.share * 100)
        c.stats[f"cons{key}null"] = fmt_pct(check.null_mean * 100)
        c.stats[f"cons{key}q"] = fmt_pct(check.null_q975 * 100)
        c.stats[f"cons{key}pairs"] = fmt_int(check.n_pairs)

    # A parent whose first specialiser arrives no later than its own birth was
    # already specialised when first attested: its leaf phase is not observed.
    arrival = numpy.fmax(net.eps, net.b_bnd[parent])
    first = pd.Series(arrival).groupby(parent).min()
    born = pd.Series(net.b_bnd[first.index.to_numpy()], index=first.index)
    known = first.notna() & born.notna()
    c.stats["specialisedparents"] = fmt_int(known.sum())
    c.stats["bornspecialised"] = fmt_pct((first[known] <= born[known]).mean() * 100)


def construction_stats(c: Corpus) -> None:
    """The numbers of the construction section: funnel, capture window, parse paths, kinds."""
    attempts = c._sql(
        "SELECT source_registry, source_id, MAX(status = 'ok') AS ok, "
        "MIN(retrieval_date) AS first, MAX(retrieval_date) AS last "
        "FROM fetch_attempts GROUP BY source_registry, source_id"
    )
    rows = c._sql(
        "SELECT source_id, source_registry, parse_status, parse_format, is_duplicate, "
        "is_latest_version, "
        "shared_iri, kind, iri_source, licence FROM corpus_registry"
    )
    parsed = rows[rows["parse_status"] == "ok"]
    canonical = parsed[parsed["is_duplicate"] == 0]
    latest = rows[rows["is_latest_version"] == 1]

    c.stats["nrecords"] = fmt_int(len(attempts))
    c.stats["nfetched"] = fmt_int(attempts["ok"].sum())
    c.stats["nregistered"] = fmt_int(len(rows))
    c.stats["nparsed"] = fmt_int(len(parsed))
    c.stats["nparsefail"] = fmt_int((rows["parse_status"] == "fail").sum())
    c.stats["nunsupported"] = fmt_int((rows["parse_status"].isin(["unsupported", "feed"])).sum())
    c.stats["nduplicates"] = fmt_int(len(parsed) - len(canonical))
    c.stats["ndistinct"] = fmt_int(len(canonical))
    c.stats["releasefamilies"] = fmt_int(canonical["source_id"].map(family_key).nunique())
    c.stats["nlatest"] = fmt_int(len(latest))
    c.stats["nsharediri"] = fmt_int(rows["shared_iri"].fillna(0).astype(bool).sum())
    c.stats["capturestart"] = str(attempts["first"].min())[:10]
    c.stats["captureend"] = str(attempts["last"].max())[:10]

    fmt = parsed["parse_format"].fillna("unknown")
    paths = {
        "xml": fmt == "xml",
        "turtle": fmt == "turtle",
        "jsonld": fmt == "json-ld",
        "obo": fmt == "obo",
        "owlxml": fmt == "owl_xml",
        "owlready": fmt == "owlready2",
        "streamxml": fmt == "stream_xml_cvt",
        "streamnt": fmt == "stream_nt_cvt",
    }
    for key, mask in paths.items():
        c.stats[f"parse{key}"] = fmt_int(mask.sum())
        c.stats[f"parse{key}share"] = fmt_pct(mask.mean() * 100)
    streamed = fmt.str.startswith("stream_")
    c.stats["parsestream"] = fmt_int(streamed.sum())
    c.stats["parsestreamshare"] = fmt_pct(streamed.mean() * 100)
    c.stats["parseother"] = fmt_int((~numpy.logical_or.reduce(list(paths.values()))).sum())

    for kind in ("no_skos", "mostly_owl", "mostly_skos", "thesaurus"):
        c.stats[f"kind{kind.replace('_', '')}"] = fmt_int((latest["kind"] == kind).sum())
    for source, key in (("declared", "declared"), ("registry", "registry")):
        c.stats[f"iri{key}"] = fmt_int((latest["iri_source"] == source).sum())
    c.stats["irinone"] = fmt_int(latest["iri_source"].isna().sum())
    declares = latest["licence"].fillna("[]").map(lambda v: v not in ("[]", ""))
    c.stats["licencedeclared"] = fmt_pct(declares.mean() * 100)
    c.stats["structn"] = fmt_int(len(c.p4))

    # Parse failures among downloaded files, in the two registries where they are commonest.
    failed = rows.assign(fail=rows["parse_status"] == "fail").groupby("source_registry")["fail"]
    for rank, (registry, share) in zip("ab", failed.mean().nlargest(2).items()):
        c.stats[f"parsefail{rank}registry"] = str(registry)
        c.stats[f"parsefail{rank}"] = fmt_pct(share * 100)
    unique = latest[latest["parse_status"] == "ok"]
    c.stats["uniqueoboowlxml"] = fmt_int(unique["parse_format"].isin(["obo", "owl_xml"]).sum())

    # The kept copy of an ontology may come from a registry other than its home registry.
    homes = c._sql(
        "SELECT source_registry, home_registry FROM corpus_registry WHERE is_latest_version = 1"
    )
    elsewhere = homes.loc[homes["source_registry"] != homes["home_registry"], "source_registry"]
    c.stats["homeelsewhere"] = fmt_int(len(elsewhere))
    c.stats["homeelsewheretopregistry"] = str(elsewhere.value_counts().index[0])
    c.stats["homeelsewheretop"] = fmt_int(elsewhere.value_counts().iloc[0])
    c.stats["multiregistry"] = fmt_int(_multi_registry_ontologies(c))

    metrics = load_ontology_metrics(c.paths)
    skos = metrics[metrics["kind"].isin(SKOS_DOMINATED_KINDS)]
    c.stats["skosnoclasses"] = fmt_int((skos["class_count"].fillna(0) == 0).sum())
    c.stats["skoshierarchy"] = fmt_int((skos["n_nodes"].fillna(0) > 0).sum())


def _multi_registry_ontologies(c: Corpus) -> int:
    """Unique ontologies whose merged copies come from two or more registries."""
    rows = c._sql(
        "SELECT id, source_registry, canonical_entry_id, is_latest_version FROM corpus_registry"
    )
    parent = dict(zip(rows["id"], rows["canonical_entry_id"].fillna(rows["id"]).astype(int)))
    root = {}
    for i in parent:
        j, seen = i, set()
        while parent.get(j, j) != j and j not in seen:
            seen.add(j)
            j = parent[j]
        root[i] = j
    rows["root"] = rows["id"].map(root)
    registries = rows.groupby("root")["source_registry"].nunique()
    latest = rows.loc[rows["is_latest_version"] == 1, "id"]
    return int((registries.reindex(latest).fillna(1) >= 2).sum())


def term_graph_stats(c: Corpus) -> None:
    """Nodes and edges of T, built as ``ontometer-corpus abstractness`` builds it, when the store is given.

    T keeps own and bridge edges, and copies whose child no corpus ontology owns, each
    (child, parent) pair once. Its entity tables live in the store's cache, not the corpus.
    """
    if c.paths.store_dir is None:
        return
    cache = c.paths.cache_dir
    home = pd.read_parquet(cache / "entity_terms.parquet", columns=["home"])["home"].to_numpy()
    edges = pd.read_parquet(cache / "entity_edges.parquet", columns=["child", "parent", "kind"])
    keep = (edges["kind"].to_numpy() != EDGE_COPY) | (home[edges["child"].to_numpy()] == EXTERNAL)
    kept = edges.loc[keep, ["child", "parent"]].drop_duplicates()
    nodes = numpy.unique(numpy.concatenate([kept["child"].to_numpy(), kept["parent"].to_numpy()]))
    c.stats["tdagnodes"] = fmt_int(len(nodes))
    c.stats["tdagedges"] = fmt_int(len(kept))


def network_stats(c: Corpus) -> None:
    """The numbers of the networks section: nodes and edges per layer."""
    c.stats["nsalledges"] = fmt_int(c.g_all.number_of_edges())
    c.stats["nsallnodes"] = fmt_int(c.g_all.number_of_nodes())

    owners = pd.read_parquet(c.paths.ontology_abstractness_path)
    asserted = {kind: owners[f"n_{kind}_edges"].sum() for kind in ("own", "bridge", "copy")}
    for kind, n in asserted.items():
        c.stats[f"tdag{kind}"] = fmt_int(n)
    c.stats["tdagasserted"] = fmt_int(sum(asserted.values()))
    c.stats["tdagcopyshare"] = fmt_pct(asserted["copy"] / sum(asserted.values()) * 100)
    c.stats["tdagcopiers"] = fmt_int((owners["n_copy_edges"] > 0).sum())
    c.stats["tdagclasses"] = fmt_int(owners["n_home_classes"].sum())
    c.stats["tdagowners"] = fmt_int((owners["n_home_classes"] > 0).sum())

    term_graph_stats(c)

    pairs = pd.read_parquet(c.paths.ontology_specialisations_path)
    spec = nx.DiGraph()
    spec.add_edges_from(zip(pairs["child"], pairs["parent"]))
    cyclic = [s for s in nx.strongly_connected_components(spec) if len(s) > 1]
    c.stats["specnodes"] = fmt_int(spec.number_of_nodes())
    c.stats["specchildren"] = fmt_int(pairs["child"].nunique())
    c.stats["specparents"] = fmt_int(pairs["parent"].nunique())
    c.stats["speccycles"] = fmt_int(len(cyclic))
    c.stats["speccyclenodes"] = fmt_int(sum(len(s) for s in cyclic))

    # Usage edges to domain vocabularies (not languages or identifier schemes): share logical.
    domain = [
        d
        for _, _, d in c.g_all.edges(data=True)
        if d["edge_type"] == "usage" and d.get("reference_kind") == "domain_vocabulary"
    ]
    logical = sum((d.get("n_logical_terms") or 0) > 0 for d in domain)
    c.stats["logicalshare"] = fmt_pct(logical / len(domain) * 100)


def specialisation_stats(c: Corpus) -> None:
    """Specialisation pairs of G: overlap with reuse, how thin they are, who they gather on."""
    pairs = pd.read_parquet(c.paths.ontology_specialisations_path)
    onts = pd.read_parquet(c.paths.ontology_abstractness_path)
    node = dict(zip(onts["onto"], onts["node_id"]))
    child, parent = pairs["child"].map(node), pairs["parent"].map(node)
    links = c.corpus_links
    in_reuse = numpy.array([links.has_edge(u, v) for u, v in zip(child, parent)])
    c.stats["specinreuse"] = fmt_pct(in_reuse.mean() * 100)
    c.stats["reusespecshare"] = fmt_pct(in_reuse.sum() / links.number_of_edges() * 100)
    c.stats["specmedianparents"] = fmt_int(pairs.groupby("child").size().median())
    c.stats["specmedianedges"] = fmt_int(pairs["n_terms"].median())
    c.stats["specsingleedge"] = fmt_pct((pairs["n_terms"] == 1).mean() * 100)
    top = pairs["parent"].value_counts().nlargest(10).index
    c.stats["spectopshare"] = fmt_pct(pairs["parent"].isin(top).mean() * 100)
    across = numpy.array(
        [{c.home.get(u), c.home.get(v)} == set(COMMUNITIES) for u, v in zip(child, parent)]
    )
    c.stats["specacross"] = fmt_int(across.sum())
    c.stats["specacrossshare"] = fmt_pct(across.mean() * 100)


def mixing_stats(c: Corpus) -> None:
    """Class/instance mixing: own classes that some file uses as a class and an individual at once."""
    owners = pd.read_parquet(c.paths.ontology_abstractness_path)
    share = owners["punned_share"].dropna()
    mixed = share > 0
    c.stats["punnedn"] = fmt_int(len(share))
    c.stats["punnedany"] = fmt_pct(mixed.mean() * 100)
    c.stats["punnedmedian"] = fmt_pct(share[mixed].median() * 100)
    c.stats["punnedall"] = fmt_int((share == 1).sum())
    c.stats["punnedregistries"] = fmt_int(owners.loc[share[mixed].index, "registry"].nunique())


def _tex_escape(s: str) -> str:
    return "".join("\\" + ch if ch in "&%$#_{}" else ch for ch in s)


def registry_table(c: Corpus) -> str:
    """The funnel from listed records to unique ontologies, per registry, as a LaTeX table.

    File-level stages count under the registry that published the file; unique ontologies
    and their subsets count under the home registry. The totals row is the corpus funnel.
    """
    attempts = c._sql(
        "SELECT source_registry, source_id, MAX(status = 'ok') AS ok "
        "FROM fetch_attempts GROUP BY source_registry, source_id"
    )
    rows = c._sql(
        "SELECT source_registry, source_id, parse_status, is_duplicate, is_latest_version, "
        "home_registry FROM corpus_registry"
    )
    parsed = rows[rows["parse_status"] == "ok"]
    canonical = parsed[parsed["is_duplicate"] == 0].assign(
        family=lambda d: d["source_id"].map(family_key)
    )
    latest = rows[rows["is_latest_version"] == 1]
    hier = c.p4[c.p4["n_nodes"] > 0]
    columns = {
        "listed": attempts.groupby("source_registry").size(),
        "downloaded": attempts.groupby("source_registry")["ok"].sum(),
        "parsed": parsed.groupby("source_registry").size(),
        "versions": canonical.groupby("source_registry").size(),
        "families": canonical.groupby("source_registry")["family"].nunique(),
        "unique": latest.groupby("home_registry").size(),
        "analysed": c.p4.groupby("home_registry").size(),
        "hierarchy": hier.groupby("home_registry").size(),
    }
    table = pd.DataFrame(columns).fillna(0).astype(int)
    dates = pd.read_parquet(c.paths.ontology_dates_path)
    dated = dates.groupby("registry")["birth"].apply(lambda s: s.notna().mean() * 100)
    order = [r for r in c.source_order if r in table.index]
    order += sorted(set(table.index) - set(order))
    table = table.reindex(order)
    totals = {
        "listed": len(attempts),
        "downloaded": int(attempts["ok"].sum()),
        "parsed": len(parsed),
        "versions": len(canonical),
        "families": canonical["family"].nunique(),
        "unique": len(latest),
        "analysed": len(c.p4),
        "hierarchy": len(hier),
    }
    for key, total in totals.items():
        if table[key].sum() != total:
            raise ValueError(f"registry table: {key} sums to {table[key].sum()}, not {total}")

    def line(name: str, values, dated_share: float) -> str:
        cells = [fmt_int(v) for v in values] + [f"{dated_share:.0f}\\%"]
        return f"  {name} & " + " & ".join(cells) + " \\\\"

    body = [
        line(_tex_escape(r), table.loc[r].tolist(), dated.get(r, float("nan"))) for r in table.index
    ]
    return "\n".join(
        [
            "% Generated from the corpus tables by `ontometer-corpus paper figures`; do not edit.",
            "\\begin{tabular}{@{}l@{\\hspace{0.6em}}rrrrr@{\\hspace{0.8em}}rrrr@{}}",
            "  \\toprule",
            "  & \\multicolumn{5}{c}{Files, by publishing registry}"
            " & \\multicolumn{4}{c}{Ontologies, by home registry} \\\\",
            "  \\cmidrule(lr){2-6}\\cmidrule(l){7-10}",
            "  Registry & Listed & Fetched & Parsed & Versions & Families"
            " & Unique & Analysed & Hierarchy & Dated \\\\",
            "  \\midrule",
            *body,
            "  \\midrule",
            line("Total", list(totals.values()), dates["birth"].notna().mean() * 100),
            "  \\bottomrule",
            "\\end{tabular}",
        ]
    )


def hub_table(c: Corpus, n: int = 10) -> str:
    """The most reused corpus ontologies as a LaTeX table: reusing ontologies and PageRank."""
    reused_by: dict[str, int] = dict(c.corpus_links.in_degree())
    hubs = sorted(reused_by, key=lambda node: -reused_by[node])[:n]
    pagerank = load_ontology_metrics(c.paths)["pagerank"]
    cells = [
        f"\\texttt{{{_tex_escape(_short_label(node).removesuffix('.owl'))}}}"
        f" & {_tex_escape(c.home[node])}"
        f" & {fmt_int(reused_by[node])} & {pagerank.get(node, float('nan')):.3f}"
        for node in hubs
    ]
    # Two blocks side by side, ranks 1..n/2 on the left and the rest on the right.
    half = (len(cells) + 1) // 2
    left, right = cells[:half], cells[half:] + [" & & & "] * (2 * half - len(cells))
    rows = [f"  {a} && {b} \\\\" for a, b in zip(left, right)]
    c.stats["hubtop"] = _tex_escape(_short_label(hubs[0]).removesuffix(".owl"))
    c.stats["hubtopn"] = fmt_int(reused_by[hubs[0]])
    c.stats["hubn"] = fmt_int(n)
    c.stats["hubobo"] = fmt_int(sum(c.home[node] == "OBO" for node in hubs))
    ranked = pagerank.dropna().nlargest(n)
    c.stats["prtoplov"] = fmt_int(sum(c.home.get(node) == "LOV" for node in ranked.index))
    c.stats["prtopshare"] = fmt_pct(ranked.sum() / pagerank.dropna().sum() * 100)
    return "\n".join(
        [
            "% Generated from the corpus tables by `ontometer-corpus paper figures`; do not edit.",
            "\\begin{tabular}{@{}llrr@{\\hspace{0.9em}}c@{\\hspace{0.9em}}llrr@{}}",
            "  \\toprule",
            "  Ontology & Registry & Reusers & PageRank &&"
            " Ontology & Registry & Reusers & PageRank \\\\",
            "  \\cmidrule(r){1-4}\\cmidrule(l){6-9}",
            *rows,
            "  \\bottomrule",
            "\\end{tabular}",
        ]
    )


FIGURES: dict[str, Callable[[Corpus], plt.Figure]] = {
    "corpus_composition": fig_corpus_composition,
    "reuse_network": fig_reuse_network,
    "poincare_gt": fig_poincare_gt,
    "size_distributions": fig_size_distributions,
    "inequality": fig_inequality,
    "declared_vs_actual": fig_declared_vs_actual,
    "registry_reuse": fig_registry_reuse,
    "placing": fig_placing,
    "coupling_ratio": fig_coupling_ratio,
    "structure_shape": fig_structure_shape,
    "gaps": fig_gaps,
    "release_years": fig_release_years,
    "dating_sources": fig_dating_sources,
    "extremes": fig_extremes,
    "births": fig_births,
    "cohorts": fig_cohorts,
}


#: The figures the paper includes. Every function in FIGURES still runs, because most
#: compute numbers the text quotes, but only these are written unless --only names others.
PUBLISHED = (
    "births",
    "cohorts",
    "poincare_gt",
    "extremes",
    "gaps",
    "inequality",
)


def _corpus_commit(corpus_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(corpus_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _write_stats(path: Path, stats: dict[str, str]) -> None:
    lines = ["% Generated from the corpus tables by `ontometer-corpus paper figures`; do not edit."]
    lines += [f"\\statdef{{{k}}}{{{v}}}" for k, v in sorted(stats.items())]
    path.write_text("\n".join(lines) + "\n")


@click.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for the figures and stats.tex.",
)
@click.option(
    "--only",
    multiple=True,
    type=click.Choice(sorted(FIGURES)),
    help="Draw and write only these figures (repeatable), published or not. stats.tex is "
    "then written only with --stats. Without it, every figure is drawn for its numbers and "
    "the published ones are written.",
)
@click.option(
    "--stats/--no-stats",
    "write_stats",
    default=None,
    help="Write stats.tex (default: when every figure is drawn).",
)
@click.option(
    "--comparators",
    "comparators_dir",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=None,
    help="Directory of comparator snapshots (``ontometer-corpus paper comparators fetch``); without it the "
    "inequality figure shows the corpus alone and no comparator table is written.",
)
@click.option(
    "--github",
    "github_dir",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=None,
    help="Directory of the GitHub ontology sample (``ontometer-corpus paper github analyse``); without "
    "it the births figure has no registry-coverage panel.",
)
@corpus_paths
def main(
    out: Path,
    only: tuple[str, ...],
    write_stats: bool | None,
    comparators_dir: Path | None,
    github_dir: Path | None,
    paths: CorpusPaths,
) -> None:
    """Draw the publication figures and write their numbers as LaTeX macros."""
    style()
    out.mkdir(parents=True, exist_ok=True)
    corpus = Corpus(paths, comparators_dir, github_dir)
    names = list(only) or list(FIGURES)
    written = set(only) or set(PUBLISHED)
    for name in names:
        fig = FIGURES[name](corpus)
        if name in written:
            save(fig, out, name)
            print(f"  {name}")
        plt.close(fig)
    # Drawn figures may not have touched the network; its one number belongs in stats.tex.
    _ = corpus.views
    construction_stats(corpus)
    time_stats(corpus)
    network_stats(corpus)
    specialisation_stats(corpus)
    mixing_stats(corpus)
    gap_stats(corpus)
    births_stats(corpus)
    hubs = hub_table(corpus)
    networks = comparator_table(corpus) if corpus.comparators else None
    registries = registry_table(corpus)
    commit = _corpus_commit(paths.corpus_dir)
    if commit:
        corpus.stats["corpuscommit"] = commit
    if write_stats if write_stats is not None else not only:
        _write_stats(out / "stats.tex", corpus.stats)
        (out / "hubs.tex").write_text(hubs + "\n")
        (out / "registries.tex").write_text(registries + "\n")
        if networks:
            (out / "comparators.tex").write_text(networks + "\n")
        print(f"  stats.tex ({len(corpus.stats)} numbers), hubs.tex")
    print(
        f"Corpus: {paths.corpus_dir} @ {commit or 'unknown revision'}; wrote {len(written)} figures to {out}"
    )


if __name__ == "__main__":
    main()
