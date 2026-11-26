"""Class labels declared in many ontologies, and how far one parent unites them.

A label such as *person* or *event* that several ontologies each declare as a
class of their own is a candidate gap in the fabric: either one shared
abstraction already covers those declarations, or each ontology modelled the
concept again on its own. For every normalised label declared as an own class
in at least ``min_homes`` home ontologies, :func:`redeclared_labels` reports the
best *cover*: the largest share of those declarations that sit under one
common ancestor in the term DAG (a term covers itself). 1 means one class
already unites every declaration; 1/n means none does.

Matching is lexical, so one label is not proven to be one concept. Copy edges
are left out of the ancestry unless the copied child is homed outside the
corpus, as everywhere else: a copied hierarchy is not a shared abstraction.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque

import pandas as pd

from ontometer_corpus.network.entities import EDGE_COPY, EXTERNAL

#: Labels shorter than this after normalisation are too generic to group.
MIN_LABEL_LENGTH = 3
#: Ancestor walks stop after this many terms; deep OBO closures are bounded.
ANCESTOR_CAP = 5000
#: Example IRIs kept per label group.
EXAMPLES = 4


def norm_label(label: str) -> str:
    """Lower case, underscores and runs of space folded to one space, punctuation dropped."""
    label = re.sub(r"[_\s]+", " ", label.strip().lower())
    return re.sub(r"[^\w\s-]", "", label).strip()


def term_labels(labels: pd.DataFrame, home: pd.Series) -> pd.DataFrame:
    """One normalised label per term, preferring the label its home ontology gives.

    ``labels`` has columns ``onto``, ``term``, ``label``; ``home`` maps term to
    home ontology. Returns columns ``term``, ``norm``.
    """
    lab = labels.copy()
    lab["is_home"] = lab.onto.to_numpy() == home.reindex(lab.term).to_numpy()
    lab = lab.sort_values(["term", "is_home"], ascending=[True, False]).drop_duplicates("term")
    lab["norm"] = lab.label.map(norm_label)
    return lab[["term", "norm"]]


def redeclared_labels(
    terms: pd.DataFrame,
    labels: pd.DataFrame,
    edges: pd.DataFrame,
    min_homes: int = 3,
) -> pd.DataFrame:
    """Label groups declared as an own class in at least ``min_homes`` homes, with their cover.

    Args:
        terms: one row per term, positioned by term id (``terms.term[i] == i``),
            with columns ``term``, ``iri``, ``home``, ``df_class``, ``is_property``.
        labels: ``onto``, ``term``, ``label`` as the entity network writes them.
        edges: ``child``, ``parent``, ``kind`` per asserted subclass edge.
        min_homes: the smallest number of distinct home ontologies in a group.

    Returns:
        One row per label, most homes first: ``label``, ``n_homes``, ``n_terms``,
        ``cover``, ``best_common_ancestor`` (empty when no ancestor covers two
        declarations) and ``example_iris``.
    """
    if not (terms.term.to_numpy() == range(len(terms))).all():
        raise ValueError("terms must be positioned by term id")
    home = terms.home.to_numpy()
    iri = terms.iri.to_numpy()

    kept = edges[(edges.kind != EDGE_COPY) | (home[edges.child.to_numpy(dtype=int)] == EXTERNAL)]
    parents: dict[int, list[int]] = defaultdict(list)
    for c, p in kept[["child", "parent"]].drop_duplicates().itertuples(index=False):
        parents[int(c)].append(int(p))

    def ancestors(t: int) -> set[int]:
        seen = {t}
        queue = deque([t])
        while queue and len(seen) < ANCESTOR_CAP:
            for p in parents.get(queue.popleft(), ()):
                if p not in seen:
                    seen.add(p)
                    queue.append(p)
        return seen

    own_classes = terms[(terms.home != EXTERNAL) & (terms.df_class > 0) & (terms.is_property == 0)][
        ["term", "home"]
    ]
    named = term_labels(labels, terms.set_index("term").home).merge(own_classes, on="term")
    named = named[named.norm.str.len() >= MIN_LABEL_LENGTH]
    groups = named.groupby("norm").agg(
        n_homes=("home", "nunique"), n_terms=("term", "nunique"), terms=("term", list)
    )
    groups = groups[groups.n_homes >= min_homes].sort_values(
        ["n_homes", "n_terms"], ascending=False, kind="stable"
    )

    rows = []
    for label, g in groups.iterrows():
        members = sorted(set(g.terms))
        counts: dict[int, int] = defaultdict(int)
        for t in members:
            for a in ancestors(t):
                counts[a] += 1
        best, best_n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
        rows.append(
            {
                "label": label,
                "n_homes": int(g.n_homes),
                "n_terms": int(g.n_terms),
                "cover": round(best_n / len(members), 3),
                "best_common_ancestor": iri[best] if best_n > 1 else "",
                "example_iris": "; ".join(iri[t] for t in members[:EXAMPLES]),
            }
        )
    columns = ["label", "n_homes", "n_terms", "cover", "best_common_ancestor", "example_iris"]
    return pd.DataFrame(rows, columns=pd.Index(columns))
