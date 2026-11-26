"""
Validation and analysis of the abstractness measures.

Reads:  ``$ONTOMETER_STORE/cache/entity_abstractness.parquet``,
        ``$ONTOMETER_STORE/cache/entity_{terms,edges}.parquet`` and, when
        present, ``$ONTOMETER_STORE/cache/entity_labels.parquet``;
        ``<corpus>/ontology_abstractness.parquet``,
        ``<corpus>/structural_metrics.parquet``,
        ``<corpus>/term_coupling.parquet``,
        ``<corpus>/network_edges.parquet`` and, when present,
        ``<corpus>/abstractness_panel.parquet``.
Runs after: ``ontometer-corpus abstractness``, ``ontometer-corpus structural-metrics``,
``ontometer-corpus term-coupling`` and the network construction; optionally
``ontometer-corpus panel measure`` (section 7 then covers release families).
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Writes, to --out:

    summary.json          every reported quantity, keyed by section
    population_sweep.csv  the population threshold sweep
    anchors_*.csv         anchor ontologies and anchor terms with their ranks
    terms_*.csv           term-level rank correlations
    ontology_*.csv        ontology-level correlations and disagreement lists
    panel_family_slopes.csv  per-family trends (only with the release panel)
    gaps_*.csv            the four candidate gap lists
    *.png                 figures

Sections (``summary.json`` keys in brackets where they differ from the title):

1. Population: measured ontologies (``subject``: a class hierarchy and no more
   individuals than classes) vs KB-like and flat sources, with a threshold sweep.
2. Anchors: foundational ontologies and terms should rank general and
   class-like; taxa, chemicals and units should rank specific even though they
   are heavily reused.
3. Convergent validity: rank correlations among the term signals and the
   principal components they load on — do the two senses separate?
4. Ontology level: abstractness from a term average versus abstractness from
   position in the dependency structure; Martin's main sequence; whether
   ontologies built on upper ontologies are less coupled and more reused
   [``upper_vs_reuse``].
5. Lexical check: generality against WordNet hypernym depth (the same
   construct) and against human concreteness ratings (a different one).
   Needs NLTK's WordNet (``NLTK_DATA``) and the concreteness norms file given
   by --concreteness; skipped otherwise.
6. Reuse distribution: heavy-tail fit of the number of foreign specialisers.
7. Abstractness over time [``over_time``]: against release date across
   ontologies [``across_ontologies``], and within release families against a
   growth-only null model [``within_families``].
8. Candidate gap lists [``gaps``].

Usage:
    NLTK_DATA=... ontometer-corpus validation \\
        --store /path/to/ontologies --out /path/to/out \\
        --concreteness /path/to/norms.txt
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("validation", "powerlaw")


import importlib
import json
from pathlib import Path

import click
import numpy as np
import pandas as pd
from scipy import stats

from ontometer_corpus.network.entities import (
    EDGE_OWN_FOREIGN,
    EDGE_OWN_INTERNAL,
)
from ontometer_corpus.network.redeclarations import (
    redeclared_labels,
    term_labels,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.roles import kb_individuals

# Chart colours on a light surface: ink, secondary ink, grid, three series.
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

# Anchors are namespaces, so they are found wherever their terms are homed.
UPPER_ANCHORS = {
    "BFO": r"^http://purl\.obolibrary\.org/obo/BFO_$",
    "DUL": r"ontologydesignpatterns\.org/ont/dul/DUL\.owl#$",
    "DOLCE-Lite": r"DOLCE-Lite\.owl#$",
    "GFO": r"onto-med\.de/ontologies/gfo",
    "SUMO": r"SUMO\.owl#$",
    "gist": r"semanticarts.*gist",
    "COB": r"^http://purl\.obolibrary\.org/obo/COB_$",
    "IAO": r"^http://purl\.obolibrary\.org/obo/IAO_$",
    "PROV-O": r"^http://www\.w3\.org/ns/prov#$",
    "schema.org": r"^https?://schema\.org/$",
    "SKOS": r"skos/core#$",
    "FOAF": r"^http://xmlns\.com/foaf/0\.1/$",
    "OWL-Time": r"^http://www\.w3\.org/2006/time#$",
    "gUFO": r"purl\.org/nemo/gufo#$",
}
CONCRETE_ANCHORS = {
    "NCBITaxon": r"^http://purl\.obolibrary\.org/obo/NCBITaxon_$",
    "ChEBI": r"^http://purl\.obolibrary\.org/obo/CHEBI_$",
    "UO (units)": r"^http://purl\.obolibrary\.org/obo/UO_$",
    "Crop Ontology traits": r"cropontology\.org",
}
ANCHOR_TERMS = {
    "http://purl.obolibrary.org/obo/BFO_0000001": "BFO entity",
    "http://purl.obolibrary.org/obo/BFO_0000002": "BFO continuant",
    "http://purl.obolibrary.org/obo/BFO_0000003": "BFO occurrent",
    "http://purl.obolibrary.org/obo/BFO_0000040": "BFO material entity",
    "http://purl.obolibrary.org/obo/IAO_0000030": "IAO information content entity",
    "http://www.ontologydesignpatterns.org/ont/dul/DUL.owl#Object": "DUL Object",
    "http://www.ontologydesignpatterns.org/ont/dul/DUL.owl#Event": "DUL Event",
    "http://www.w3.org/ns/prov#Entity": "prov:Entity",
    "http://www.w3.org/ns/prov#Agent": "prov:Agent",
    "http://www.w3.org/ns/prov#Activity": "prov:Activity",
    "http://xmlns.com/foaf/0.1/Person": "foaf:Person",
    "http://xmlns.com/foaf/0.1/Agent": "foaf:Agent",
    "http://schema.org/Thing": "schema:Thing",
    "http://schema.org/Person": "schema:Person",
    "http://www.w3.org/2004/02/skos/core#Concept": "skos:Concept",
    "http://purl.obolibrary.org/obo/GO_0008150": "GO biological_process",
    "http://purl.obolibrary.org/obo/CL_0000000": "CL cell",
    "http://purl.obolibrary.org/obo/UBERON_0000061": "UBERON anatomical structure",
    "http://purl.obolibrary.org/obo/NCBITaxon_9606": "NCBITaxon Homo sapiens",
    "http://purl.obolibrary.org/obo/NCBITaxon_10090": "NCBITaxon Mus musculus",
    "http://purl.obolibrary.org/obo/CHEBI_15377": "ChEBI water",
    "http://purl.obolibrary.org/obo/UO_0000008": "UO meter",
    "http://purl.obolibrary.org/obo/UO_0000010": "UO second",
}

TERM_SIGNALS = {
    "generality": "generality (1-IC Seco, G)",
    "neg_depth_global": "-depth in G",
    "sanchez_generality": "1-IC Sanchez (G)",
    "generality_local": "generality, home hierarchy",
    "df_spec": "foreign specialisers",
    "div_comm": "specialiser diversity",
    "role_share": "class-role share",
    "df_decl_foreign": "foreign declarations",
    "df_ref_foreign": "foreign references",
}


def _spearman_matrix(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df[cols].rank().corr(method="pearson")


def _rho(a: pd.Series, b: pd.Series) -> dict:
    m = a.notna() & b.notna()
    if m.sum() < 10:
        return {"rho": None, "p": None, "n": int(m.sum())}
    r = stats.spearmanr(a[m], b[m])
    return {"rho": round(float(r.statistic), 3), "p": float(r.pvalue), "n": int(m.sum())}


def _pct(series: pd.Series) -> pd.Series:
    return series.rank(pct=True)


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def build(paths: CorpusPaths, out: Path, concreteness: Path | None, seed: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out.mkdir(parents=True, exist_ok=True)
    d = paths.cache_dir
    ent = pd.read_parquet(d / "entity_abstractness.parquet")
    ent = ent.merge(pd.read_parquet(d / "entity_terms.parquet", columns=["term", "ns"]), on="term")
    onts = pd.read_parquet(paths.ontology_abstractness_path)
    edges = pd.read_parquet(d / "entity_edges.parquet")
    labels_path = d / "entity_labels.parquet"
    summary: dict = {}

    # --- 1. population --------------------------------------------------------
    summary["population"] = {
        "ontologies": len(onts),
        "subjects": int(onts.subject.sum()),
        "kb_like": int(onts.kb_like.sum()),
        "flat": int(onts.flat.sum()),
        "kb_like_and_flat": int((onts.kb_like & onts.flat).sum()),
        "by_registry": onts.groupby("registry")
        .subject.agg(["size", "sum"])
        .rename(columns={"size": "n", "sum": "subjects"})
        .to_dict("index"),
        "rich_roles_share": round(float(onts.rich_roles.mean()), 3),
        "terms": len(ent),
        "terms_in_G": int(ent.in_g.sum()),
        "g_cyclic_terms": int(ent.cyclic.eq(True).sum()),
    }
    sweep = []
    for min_nodes in (3, 5, 10, 20):
        for min_depth in (1, 2, 3):
            for kb_ratio in (0.5, 1.0, 2.0):
                subj = (
                    (onts.hierarchy_nodes >= min_nodes)
                    & (onts.max_depth_local >= min_depth)
                    & (
                        kb_individuals(onts.individual_count, onts.get("skos_concept_count"))
                        <= kb_ratio * onts.class_count.fillna(0)
                    )
                )
                s = onts[subj & ((onts.Ca + onts.Ce) > 0)]
                sweep.append(
                    {
                        "min_nodes": min_nodes,
                        "min_depth": min_depth,
                        "kb_ratio": kb_ratio,
                        "subjects": int(subj.sum()),
                        "rho_Alocal_stability": _rho(s.A_local, 1 - s.I)["rho"],
                        "rho_Aglobal_stability": _rho(s.A_global, 1 - s.I)["rho"],
                    }
                )
    pd.DataFrame(sweep).to_csv(out / "population_sweep.csv", index=False)
    summary["population"]["sweep_range_rho_Alocal_stability"] = [
        min(x["rho_Alocal_stability"] for x in sweep if x["rho_Alocal_stability"] is not None),
        max(x["rho_Alocal_stability"] for x in sweep if x["rho_Alocal_stability"] is not None),
    ]

    # --- 2. anchors -------------------------------------------------------------
    g = ent[ent.in_g].copy()
    g["gen_pct"] = _pct(g.generality)
    g["spec_pct"] = _pct(g.df_spec)
    g["decl_pct"] = _pct(g.df_decl_foreign)
    anchor_rows = []
    top_decile = g.generality.quantile(0.9)
    by_onto = onts.set_index("onto")
    for group, table in (("upper", UPPER_ANCHORS), ("concrete", CONCRETE_ANCHORS)):
        for name, pattern in table.items():
            ns_hit = g.ns.str.contains(pattern, regex=True)
            home = g[ns_hit & (g.df_class > 0)]
            if home.empty:
                anchor_rows.append({"group": group, "anchor": name, "found": False})
                continue
            owner = int(home.home.mode().iloc[0])
            row = {
                "group": group,
                "anchor": name,
                "found": True,
                "n_classes_in_G": len(home),
                "share_nonleaf": round(float((home.n_desc > 0).mean()), 3),
                "share_top_decile_generality": round(
                    float((home.generality >= top_decile).mean()), 3
                ),
                "median_generality_pct": round(float(home.gen_pct.median()), 3),
                "share_specialised_by_foreign": round(float((home.df_spec > 0).mean()), 3),
                "max_df_spec": int(home.df_spec.max()),
                "mean_role_share": round(float(home.role_share.mean()), 3),
                "home_family": by_onto.family.get(owner) if owner >= 0 else "external",
            }
            if owner >= 0:
                oo = by_onto.loc[owner]
                row.update(
                    A_local=round(float(oo.A_local), 3) if pd.notna(oo.A_local) else None,
                    A_global=round(float(oo.A_global), 3) if pd.notna(oo.A_global) else None,
                    Ca=int(oo.Ca),
                    Ce=int(oo.Ce),
                    I=round(float(oo.I), 3) if pd.notna(oo.I) else None,
                    layer=int(oo.layer),
                )
            anchor_rows.append(row)
    anchors = pd.DataFrame(anchor_rows)
    anchors.to_csv(out / "anchors_ontologies.csv", index=False)
    summary["anchors_ontologies"] = anchors.to_dict("records")
    at = ent[ent.iri.isin(ANCHOR_TERMS)].copy()
    at = at.merge(g[["term", "gen_pct", "spec_pct", "decl_pct"]], on="term", how="left")
    at["name"] = at.iri.map(ANCHOR_TERMS)
    cols = [
        "name",
        "gen_pct",
        "spec_pct",
        "decl_pct",
        "df_spec",
        "df_decl_foreign",
        "df_class",
        "df_inst",
        "role_share",
        "n_desc",
        "depth_global_long",
    ]
    at = at[cols].round(3)
    at.to_csv(out / "anchors_terms.csv", index=False)
    summary["anchors_terms"] = at.to_dict("records")

    # --- 3. convergent validity (terms) ------------------------------------------
    used = g[(g.df_class + g.df_inst) > 0].copy()
    used["neg_depth_global"] = -used.depth_global_long
    used["sanchez_generality"] = 1 - used.ic_sanchez
    used["div_comm"] = used.div_comm.fillna(0)
    sample = used.sample(min(len(used), 300_000), random_state=seed)
    corr = _spearman_matrix(sample, list(TERM_SIGNALS))
    corr.round(3).to_csv(out / "terms_spearman.csv")
    summary["terms_spearman"] = corr.round(3).to_dict()
    # Over all terms the never-reused leaves dominate the ties; the signals can
    # only disagree where a term is used outside its home.
    reused = used[used.df_ref_foreign > 0]
    rcorr = _spearman_matrix(reused, list(TERM_SIGNALS))
    rcorr.round(3).to_csv(out / "terms_spearman_reused.csv")
    summary["terms_spearman_reused"] = {"n": len(reused), "matrix": rcorr.round(3).to_dict()}
    # Principal components of the rank-transformed signals.
    z = sample[list(TERM_SIGNALS)].rank().fillna(sample[list(TERM_SIGNALS)].rank().mean())
    z = (z - z.mean()) / z.std(ddof=0).replace(0, 1)
    cov = np.cov(z.to_numpy(), rowvar=False)
    w, v = np.linalg.eigh(cov)
    order = np.argsort(w)[::-1]
    w, v = w[order], v[:, order]
    summary["terms_pca"] = {
        "explained": [round(float(x), 3) for x in (w / w.sum())[:4]],
        "loadings": {
            f"PC{k + 1}": {
                s: round(float(v[i, k] * np.sqrt(w[k])), 3) for i, s in enumerate(TERM_SIGNALS)
            }
            for k in range(3)
        },
    }

    # --- 4. ontology level --------------------------------------------------------
    sm = pd.read_parquet(paths.structural_metrics_path)
    tc = pd.read_parquet(paths.term_coupling_path)
    tc["coupling"] = tc.n_ext_domain / tc.n_terms_domain.replace(0, np.nan)
    net = pd.read_parquet(paths.network_edges_path)
    indeg = net[net.edge_type == "usage"].groupby("target").source.nunique().rename("ns_in_degree")
    o = onts.merge(sm[["file_hash", "mean_depth_long", "branching"]], on="file_hash", how="left")
    o = o.merge(tc[["file_hash", "coupling"]], on="file_hash", how="left")
    o = o.merge(indeg, left_on="node_id", right_index=True, how="left")
    o["ns_in_degree"] = o.ns_in_degree.fillna(0)
    o["stability"] = 1 - o.I
    o["log_Ca"] = np.log1p(o.Ca)
    o["neg_layer"] = -o.layer
    subj = o[o.subject].copy()
    oc = [
        "A_local",
        "A_global",
        "A_class",
        "upper_share",
        "stability",
        "log_Ca",
        "Ce",
        "neg_layer",
        "mean_depth_long",
        "branching",
        "coupling",
        "ns_in_degree",
    ]
    ocorr = _spearman_matrix(subj, oc)
    ocorr.round(3).to_csv(out / "ontology_spearman.csv")
    summary["ontology_spearman"] = ocorr.round(3).to_dict()
    coupled = subj[(subj.Ca + subj.Ce) > 0]
    summary["entity_vs_network"] = {
        "n_subjects_coupled": len(coupled),
        "n_subjects_uncoupled": int(((subj.Ca + subj.Ce) == 0).sum()),
        "A_local_vs_stability": _rho(coupled.A_local, coupled.stability),
        "A_global_vs_stability": _rho(coupled.A_global, coupled.stability),
        "A_class_vs_stability": _rho(coupled.A_class, coupled.stability),
        "A_global_vs_log_Ca": _rho(coupled.A_global, coupled.log_Ca),
        "A_local_vs_log_Ca": _rho(coupled.A_local, coupled.log_Ca),
        "upper_share_vs_neg_layer": _rho(coupled.upper_share, coupled.neg_layer),
        "D_global_median": round(float(coupled.D_global.median()), 3),
        "zone_pain_global": int(((coupled.A_global < 0.3) & (coupled.I < 0.3)).sum()),
        "zone_useless_global": int(((coupled.A_global > 0.7) & (coupled.I > 0.7)).sum()),
        "on_sequence_global_D_lt_0.2": int((coupled.D_global < 0.2).sum()),
        "A_local_vs_branching": _rho(subj.A_local, subj.branching),
        "D_median": round(float(coupled.D.median()), 3),
        "D_mean": round(float(coupled.D.mean()), 3),
        "zone_pain": int(((coupled.A_local < 0.3) & (coupled.I < 0.3)).sum()),
        "zone_useless": int(((coupled.A_local > 0.7) & (coupled.I > 0.7)).sum()),
        "on_sequence_D_lt_0.2": int((coupled.D < 0.2).sum()),
    }
    # Disagreements: rank difference between entity-based and network-based abstractness.
    coupled = coupled.assign(
        r_ent=coupled.A_global.rank(pct=True),
        r_net=coupled.stability.rank(pct=True) * 0.5 + coupled.log_Ca.rank(pct=True) * 0.5,
    )
    coupled["gap"] = coupled.r_ent - coupled.r_net
    keep = [
        "family",
        "registry",
        "ontology_iri",
        "n_home_classes",
        "A_local",
        "A_global",
        "Ca",
        "Ce",
        "I",
        "layer",
        "gap",
    ]
    coupled.sort_values("gap", ascending=False)[keep].head(20).round(3).to_csv(
        out / "ontology_abstract_but_unused.csv", index=False
    )
    coupled.sort_values("gap")[keep].head(20).round(3).to_csv(
        out / "ontology_concrete_but_depended.csv", index=False
    )
    # Upper ontologies: are ontologies built on them less coupled and more reused?
    summary["upper_vs_reuse"] = {
        "upper_share_vs_coupling": _rho(subj.upper_share, subj.coupling),
        "upper_share_vs_log_Ca": _rho(subj.upper_share, subj.log_Ca),
        "log_Ca_vs_coupling": _rho(subj.log_Ca, subj.coupling),
        "ns_in_degree_vs_log_Ca": _rho(subj.ns_in_degree, subj.log_Ca),
    }

    fig, ax = plt.subplots(figsize=(6.8, 5.4), dpi=150, facecolor=SURFACE)
    _style(ax)
    ax.fill_between([0, 0.3], 0, 0.3, color=ORANGE, alpha=0.08, linewidth=0)
    ax.fill_between([0.7, 1], 0.7, 1, color=AQUA, alpha=0.08, linewidth=0)
    ax.plot([0, 1], [1, 0], color=INK2, linewidth=1, linestyle="--")
    size = 8 + 10 * np.log1p(coupled.Ca)
    ax.scatter(
        coupled.I,
        coupled.A_global,
        s=size,
        color=BLUE,
        alpha=0.45,
        edgecolor=SURFACE,
        linewidth=0.6,
    )
    # Anchor labels, spread vertically so neighbours do not collide.
    marks = []
    for _, r in anchors[anchors.found & anchors.group.eq("upper")].iterrows():
        row = o[o.family == r.home_family]
        if len(row) and pd.notna(row.I.iloc[0]) and pd.notna(row.A_global.iloc[0]):
            marks.append((float(row.I.iloc[0]), float(row.A_global.iloc[0]), r.anchor))
    marks.sort(key=lambda m: m[1])
    placed: list[float] = []
    for x0, y0, name in marks:
        y = y0
        while any(abs(y - q) < 0.035 for q in placed):
            y += 0.035
        placed.append(y)
        ax.annotate(
            name,
            (x0, y0),
            xytext=(x0 + 0.08, y),
            textcoords="data",
            fontsize=8,
            color=INK,
            va="center",
            arrowprops={"arrowstyle": "-", "color": INK2, "linewidth": 0.5},
        )
    ax.text(0.31, 0.02, "zone of pain", color=INK2, fontsize=8)
    ax.text(0.72, 0.95, "zone of uselessness", color=INK2, fontsize=8)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("instability I = Ce / (Ca + Ce)", color=INK, fontsize=10)
    ax.set_ylabel("A_global (mean generality of own classes in G)", color=INK, fontsize=10)
    ax.set_title(
        "Ontologies against the main sequence (marker size ~ log Ca)",
        color=INK,
        fontsize=10,
        loc="left",
    )
    fig.tight_layout()
    fig.savefig(out / "main_sequence.png", facecolor=SURFACE)
    plt.close(fig)

    # --- 5. lexical check ---------------------------------------------------------
    lexical: dict[str, object] = {"skipped": True}
    if labels_path.exists():
        raw_labels = pd.read_parquet(labels_path)
        lab = term_labels(raw_labels, ent.set_index("term").home)
        lx = g.merge(lab, on="term")
        lexical = {"skipped": False, "labelled_terms_in_G": len(lx)}
        try:
            wn = importlib.import_module("nltk.corpus.wordnet")

            wn.synsets("dog")
            depth_cache: dict[str, float] = {}

            def wn_depth(text: str) -> tuple[float, bool]:
                whole = text.replace(" ", "_")
                syn = wn.synsets(whole, pos="n")
                if syn:
                    return float(syn[0].min_depth()), True
                head = text.split(" ")[-1] if text else ""
                if head in depth_cache:
                    return depth_cache[head], False
                syn = wn.synsets(head, pos="n") if head else []
                val = float(syn[0].min_depth()) if syn else np.nan
                depth_cache[head] = val
                return val, False

            res = [wn_depth(t) for t in lx.norm]
            lx["wn_depth"] = [r[0] for r in res]
            lx["wn_whole"] = [r[1] for r in res]
            whole = lx[lx.wn_whole]
            lexical["wordnet"] = {
                "matched": int(lx.wn_depth.notna().sum()),
                "matched_whole_label": len(whole),
                "generality_vs_wn_depth": _rho(lx.generality, lx.wn_depth),
                "generality_vs_wn_depth_whole": _rho(whole.generality, whole.wn_depth),
                "depth_global_vs_wn_depth_whole": _rho(whole.depth_global_long, whole.wn_depth),
                "generality_local_vs_wn_depth_whole": _rho(whole.generality_local, whole.wn_depth),
                "df_spec_vs_wn_depth_whole": _rho(whole.df_spec, whole.wn_depth),
                "role_share_vs_wn_depth_whole": _rho(whole.role_share, whole.wn_depth),
            }
        except LookupError:
            lexical["wordnet"] = "unavailable"
        except ImportError:
            lexical["wordnet"] = "nltk not installed"
        if concreteness and concreteness.exists():
            norms = pd.read_csv(concreteness, sep="\t")
            conc = dict(zip(norms.Word.str.lower(), norms["Conc.M"]))
            lx["conc"] = [conc.get(t, conc.get(t.split(" ")[-1]) if t else None) for t in lx.norm]
            lx["conc_whole"] = [t in conc for t in lx.norm]
            cw = lx[lx.conc_whole]
            lexical["concreteness"] = {
                "matched": int(lx.conc.notna().sum()),
                "matched_whole_label": len(cw),
                "generality_vs_conc_whole": _rho(cw.generality, cw.conc),
                "role_share_vs_conc_whole": _rho(cw.role_share, cw.conc),
                "df_spec_vs_conc_whole": _rho(cw.df_spec, cw.conc),
                "generality_vs_conc": _rho(lx.generality, lx.conc),
            }
    summary["lexical"] = lexical

    # --- 6. reuse distribution --------------------------------------------------
    import powerlaw

    x = ent.df_spec[ent.df_spec > 0].to_numpy()
    fit = powerlaw.Fit(x, discrete=True, verbose=False)
    r_ln, p_ln = fit.distribution_compare("power_law", "lognormal", normalized_ratio=True)
    r_ex, p_ex = fit.distribution_compare("power_law", "exponential", normalized_ratio=True)
    xs = np.sort(x)
    gini = float((2 * np.arange(1, len(xs) + 1) - len(xs) - 1).dot(xs) / (len(xs) * xs.sum()))
    summary["reuse_tail"] = {
        "n_terms_specialised": len(x),
        "max_df_spec": int(x.max()),
        "alpha": round(float(fit.power_law.alpha), 3),
        "xmin": int(fit.power_law.xmin),
        "powerlaw_vs_lognormal_R": round(float(r_ln), 3),
        "p": round(float(p_ln), 4),
        "powerlaw_vs_exponential_R": round(float(r_ex), 3),
        "p_exp": round(float(p_ex), 4),
        "gini": round(gini, 3),
        "top_specialised": ent.sort_values("df_spec", ascending=False)[
            ["iri", "df_spec", "div_comm", "n_registries_spec"]
        ]
        .head(25)
        .round(2)
        .to_dict("records"),
    }
    fig, ax = plt.subplots(figsize=(6.4, 4.2), dpi=150, facecolor=SURFACE)
    _style(ax)
    vals, counts = np.unique(x, return_counts=True)
    ccdf = 1 - np.cumsum(counts) / counts.sum() + counts / counts.sum()
    ax.loglog(vals, ccdf, marker="o", markersize=4, linestyle="none", color=BLUE)
    ax.set_xlabel("foreign ontologies specialising the term (df_spec)", color=INK, fontsize=10)
    ax.set_ylabel("P(df_spec >= x)", color=INK, fontsize=10)
    ax.set_title("Foreign specialisers per term (CCDF)", color=INK, fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out / "df_spec_ccdf.png", facecolor=SURFACE)
    plt.close(fig)

    # --- 7. abstractness over time ---------------------------------------------------
    subj["year"] = (
        pd.to_datetime(subj.release_date, errors="coerce").dt.year
        + pd.to_datetime(subj.release_date, errors="coerce").dt.dayofyear / 365.25
    )
    dated = subj[subj.year.notna()]
    ha: dict = {
        "across_ontologies": {
            "n_dated_subjects": len(dated),
            "A_local_vs_year": _rho(dated.A_local, dated.year),
            "A_global_vs_year": _rho(dated.A_global, dated.year),
            "A_class_vs_year": _rho(dated.A_class, dated.year),
            "upper_share_vs_year": _rho(dated.upper_share, dated.year),
            "stability_vs_year": _rho(dated.stability, dated.year),
            "within_registry": {
                reg: {
                    "n": len(sub),
                    "A_local_vs_year": _rho(sub.A_local, sub.year)["rho"],
                    "A_class_vs_year": _rho(sub.A_class, sub.year)["rho"],
                }
                for reg, sub in dated.groupby("registry")
                if len(sub) >= 30
            },
        }
    }
    panel_path = paths.corpus_dir / "abstractness_panel.parquet"
    if panel_path.exists():
        pn = pd.read_parquet(panel_path)
        pn = pn[pn.ok & pn.n_home_classes.fillna(0).gt(0)].copy()
        pn["t"] = pd.to_datetime(pn.release_date, errors="coerce")
        pn["year"] = pn.t.dt.year + pn.t.dt.dayofyear / 365.25
        pn["excess"] = pn.A_local - pn.A_null
        slopes = []
        for fam, f in pn.groupby("family"):
            f = f.sort_values("release_index")
            if len(f) < 2 or f.year.nunique() < 2:
                continue
            span = f.year.max() - f.year.min()
            if span < 0.25:
                continue
            rec = {
                "family": fam,
                "n": len(f),
                "span_years": round(span, 2),
                "n_classes_first": f.n_home_classes.iloc[0],
                "n_classes_last": f.n_home_classes.iloc[-1],
            }
            for col in ("A_local", "class_share", "instance_share", "punned_share"):
                ok = f[col].notna()
                rec[f"slope_{col}"] = (
                    stats.linregress(f.year[ok], f[col][ok]).slope
                    if ok.sum() >= 2 and f.year[ok].nunique() >= 2
                    else np.nan
                )
            ex = f.excess.dropna()
            rec["mean_excess"] = float(ex.mean()) if len(ex) else np.nan
            rec["promotions"] = f.promotions.fillna(0).sum()
            rec["demotions"] = f.demotions.fillna(0).sum()
            rec["leaf_to_parent"] = f.leaf_to_parent.fillna(0).sum()
            rec["new_above"] = f.new_above.fillna(0).sum()
            rec["new_classes"] = f.n_new_classes.fillna(0).sum()
            slopes.append(rec)
        sl = pd.DataFrame(slopes)
        sl.to_csv(out / "panel_family_slopes.csv", index=False)
        rng = np.random.default_rng(seed)

        def boot_median(v: np.ndarray) -> list[float]:
            v = v[~np.isnan(v)]
            if len(v) < 5:
                return [np.nan, np.nan]
            meds = np.median(rng.choice(v, size=(2000, len(v)), replace=True), axis=1)
            return [
                round(float(np.quantile(meds, 0.025)), 5),
                round(float(np.quantile(meds, 0.975)), 5),
            ]

        def wil(v: np.ndarray) -> dict:
            v = v[~np.isnan(v)]
            v = v[v != 0]
            if len(v) < 10:
                return {"n": len(v)}
            r = stats.wilcoxon(v)
            return {
                "n": len(v),
                "median": round(float(np.median(v)), 5),
                "share_positive": round(float((v > 0).mean()), 3),
                "p": float(r.pvalue),
                "ci95_median": boot_median(v),
            }

        total_prom, total_dem = int(sl.promotions.sum()), int(sl.demotions.sum())
        fam_up = int((sl.promotions > sl.demotions).sum())
        fam_down = int((sl.promotions < sl.demotions).sum())
        ha["within_families"] = {
            "families": len(sl),
            "releases": len(pn),
            "median_span_years": round(float(sl.span_years.median()), 2),
            "slope_A_local": wil(sl.slope_A_local.to_numpy(dtype=float)),
            "mean_excess_over_null": wil(sl.mean_excess.to_numpy(dtype=float)),
            "slope_class_share": wil(sl.slope_class_share.to_numpy(dtype=float)),
            "slope_instance_share": wil(sl.slope_instance_share.to_numpy(dtype=float)),
            "promotions_total": total_prom,
            "demotions_total": total_dem,
            "families_more_promotions": fam_up,
            "families_more_demotions": fam_down,
            "sign_test_p": float(stats.binomtest(fam_up, fam_up + fam_down).pvalue)
            if fam_up + fam_down
            else None,
            "leaf_to_parent_per_100_new": round(
                100 * float(sl.leaf_to_parent.sum() / max(sl.new_classes.sum(), 1)), 2
            ),
            "new_above_per_100_new": round(
                100 * float(sl.new_above.sum() / max(sl.new_classes.sum(), 1)), 2
            ),
        }
        fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8), dpi=150, facecolor=SURFACE)
        for ax, col, title in (
            (axes[0], "mean_excess", "A_local minus growth-only null, per family"),
            (axes[1], "slope_class_share", "slope of class share per year, per family"),
        ):
            _style(ax)
            v = sl[col].dropna().to_numpy(dtype=float)
            if len(v):
                lim = np.quantile(np.abs(v), 0.97) or 1e-3
                ax.hist(
                    np.clip(v, -lim, lim), bins=40, color=BLUE, edgecolor=SURFACE, linewidth=0.8
                )
            ax.axvline(0, color=INK2, linewidth=1, linestyle="--")
            ax.set_title(title, color=INK, fontsize=10, loc="left")
            ax.set_ylabel("families", color=INK, fontsize=9)
        fig.tight_layout()
        fig.savefig(out / "panel_slopes.png", facecolor=SURFACE)
        plt.close(fig)
    summary["over_time"] = ha

    # --- 8. candidate gap lists ------------------------------------------------------
    gb: dict = {}
    if labels_path.exists():
        # 1. independent re-declaration without a shared abstraction.
        g1 = redeclared_labels(
            ent[["term", "iri", "home", "df_class", "is_property"]].sort_values(
                "term", ignore_index=True
            ),
            raw_labels,
            edges,
        )
        g1["gap_score"] = (1 - g1.cover) * np.log1p(g1.n_homes)
        g1.to_csv(out / "gaps_1_missing_shared_abstraction_all.csv", index=False)
        g1.sort_values("gap_score", ascending=False).head(40).to_csv(
            out / "gaps_1_missing_shared_abstraction.csv", index=False
        )
        gb["missing_shared_abstraction"] = {
            "label_groups_with_3plus_homes": len(g1),
            "median_cover": round(float(g1.cover.median()), 3),
            "share_cover_below_0.5": round(float((g1.cover < 0.5).mean()), 3),
            "share_cover_below_0.25": round(float((g1.cover < 0.25).mean()), 3),
            "top": g1.sort_values("gap_score", ascending=False)
            .head(15)[["label", "n_homes", "cover", "best_common_ancestor"]]
            .to_dict("records"),
            "best_covered": g1[g1.n_homes >= 10]
            .sort_values("cover", ascending=False)
            .head(10)[["label", "n_homes", "cover", "best_common_ancestor"]]
            .to_dict("records"),
        }
    # 2. missing middle: foreign specialisation leaps far beyond internal ones.
    iri = ent.iri.to_numpy()
    gen = ent.set_index("term").generality
    fe = edges[edges.kind == EDGE_OWN_FOREIGN].copy()
    ie = edges[edges.kind == EDGE_OWN_INTERNAL].copy()
    for e in (fe, ie):
        e["jump"] = gen.reindex(e.parent).to_numpy() - gen.reindex(e.child).to_numpy()
    q99 = float(ie.jump.quantile(0.99))
    leaps = fe[fe.jump > q99].copy()
    leaps["child_iri"] = iri[leaps.child.to_numpy()]
    leaps["parent_iri"] = iri[leaps.parent.to_numpy()]
    leaps = leaps.merge(onts[["onto", "family"]], on="onto")
    leaps.sort_values("jump", ascending=False)[["family", "child_iri", "parent_iri", "jump"]].head(
        60
    ).round(3).to_csv(out / "gaps_2_missing_middle.csv", index=False)
    by_parent = (
        leaps.groupby("parent_iri")
        .agg(n_leaps=("child", "size"), n_ontologies=("onto", "nunique"))
        .sort_values("n_ontologies", ascending=False)
    )
    gb["missing_middle"] = {
        "internal_jump_q99": round(q99, 3),
        "foreign_edges": len(fe),
        "median_jump_foreign": round(float(fe.jump.median()), 3),
        "median_jump_internal": round(float(ie.jump.median()), 3),
        "leaps": len(leaps),
        "ontologies_with_leaps": int(leaps.onto.nunique()),
        "top_parents": by_parent.head(12).reset_index().to_dict("records"),
    }
    # 3. unanchored ontologies.
    un = subj[(subj.Ce == 0) & (subj.n_external_parents == 0)]
    iso = un[un.Ca == 0]
    # Reference taxonomies specialise nothing foreign but are anchors
    # themselves; the gap list is the fully isolated ontologies.
    iso.sort_values("n_home_classes", ascending=False)[
        ["family", "registry", "ontology_iri", "n_home_classes", "A_local"]
    ].head(40).to_csv(out / "gaps_3_unanchored.csv", index=False)
    gb["unanchored"] = {
        "subjects": len(subj),
        "specialise_nothing_foreign": len(un),
        "share": round(len(un) / max(len(subj), 1), 3),
        "fully_isolated": len(iso),
        "by_registry": un.registry.value_counts().to_dict(),
        "isolated_by_size": un[un.Ca == 0].n_home_classes.describe().round(1).to_dict(),
    }
    # 4. concrete without cover: widely used as an instance, never as a class.
    inst = ent[(ent.df_inst >= 3) & (ent.df_class == 0)].sort_values("df_inst", ascending=False)
    inst[["iri", "df_inst", "df_ref_foreign", "home"]].head(60).to_csv(
        out / "gaps_4_instances_never_classes.csv", index=False
    )
    conflict = ent[(ent.df_class >= 2) & (ent.df_inst >= 2)].copy()
    conflict["balance"] = conflict[["df_class", "df_inst"]].min(axis=1)
    conflict.sort_values("balance", ascending=False)[
        ["iri", "df_class", "df_inst", "df_punned", "role_share"]
    ].head(60).to_csv(out / "gaps_4_role_conflicts.csv", index=False)
    gb["instance_without_class"] = {
        "terms_inst_3plus_never_class": len(inst),
        "role_conflict_terms_2plus_each": len(conflict),
        "punned_in_some_document": int((ent.df_punned > 0).sum()),
        "top_instances": inst.head(12)[["iri", "df_inst"]].to_dict("records"),
        "top_conflicts": conflict.sort_values("balance", ascending=False)
        .head(12)[["iri", "df_class", "df_inst"]]
        .to_dict("records"),
    }
    summary["gaps"] = gb

    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {out}/summary.json and tables")


@click.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for tables, figures and summary.json.",
)
@click.option(
    "--concreteness",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Brysbaert et al. (2014) concreteness norms (TSV). Lexical checks are skipped without it.",
)
@click.option(
    "--seed",
    type=int,
    default=0,
    show_default=True,
    help="Random seed of the term sample and the bootstrap intervals.",
)
@corpus_paths
def main(out: Path, concreteness: Path | None, seed: int, paths: CorpusPaths) -> None:
    """Validate and analyse the abstractness measures."""
    paths.require_store_dir()
    build(paths, out, concreteness, seed)


if __name__ == "__main__":
    main()
