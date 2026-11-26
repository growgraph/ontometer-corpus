"""Redeclared class labels and the cover one common ancestor gives them."""

from __future__ import annotations

import pandas as pd
import pytest

from ontometer_corpus.network.entities import EDGE_COPY, EDGE_OWN_FOREIGN, EXTERNAL
from ontometer_corpus.network.redeclarations import norm_label, redeclared_labels

# term: (iri, home, class?, property?)
TERMS = [
    ("a:Person", 0, 1, 0),
    ("b:Person", 1, 1, 0),
    ("c:Person", 2, 1, 0),
    ("a:Event", 0, 1, 0),
    ("b:Event", 1, 1, 0),
    ("c:Event", 2, 1, 0),
    ("a:Role", 0, 1, 0),
    ("b:Role", 1, 1, 0),
    ("c:Role", 2, 1, 0),
    ("u:Agent", 3, 1, 0),
    ("a:Unit", 0, 1, 0),
    ("b:Unit", 1, 1, 0),
    ("x:Thing", EXTERNAL, 1, 0),
    ("y:Thing", EXTERNAL, 1, 0),
    ("z:Thing", EXTERNAL, 1, 0),
    ("a:hasPart", 0, 0, 1),
    ("b:hasPart", 1, 0, 1),
    ("c:hasPart", 2, 0, 1),
]
LABELS = [
    "person",
    "Person",
    "PERSON ",
    "event",
    "Event!",
    "event",
    "role",
    "role",
    "role",
    "agent",
    "unit",
    "unit",
    "thing",
    "thing",
    "thing",
    "has part",
    "has part",
    "has part",
]


def _tables(edges: list[tuple[int, int, int]]):
    terms = pd.DataFrame(
        [
            {"term": i, "iri": iri, "home": home, "df_class": cls, "is_property": prop}
            for i, (iri, home, cls, prop) in enumerate(TERMS)
        ]
    )
    labels = pd.DataFrame(
        {"onto": [max(h, 0) for _, h, _, _ in TERMS], "term": range(len(TERMS)), "label": LABELS}
    )
    edge_df = pd.DataFrame(edges, columns=pd.Index(["child", "parent", "kind"]))
    return terms, labels, edge_df


def _by_label(edges: list[tuple[int, int, int]]) -> pd.DataFrame:
    return redeclared_labels(*_tables(edges)).set_index("label")


def test_norm_label_folds_case_space_and_punctuation():
    assert norm_label("  Person_Name ") == "person name"
    assert norm_label("Event!") == "event"
    assert norm_label("well-formed  term") == "well-formed term"


def test_groups_need_three_homes_and_own_classes():
    table = _by_label([])
    assert set(table.index) == {"person", "event", "role"}
    assert table.loc["person", "n_homes"] == 3
    assert table.loc["person", "n_terms"] == 3


def test_cover_is_one_when_a_shared_parent_unites_every_declaration():
    edges = [(0, 9, EDGE_OWN_FOREIGN), (1, 9, EDGE_OWN_FOREIGN), (2, 9, EDGE_OWN_FOREIGN)]
    row = _by_label(edges).loc["person"]
    assert row.cover == 1.0
    assert row.best_common_ancestor == "u:Agent"


def test_cover_is_one_over_n_when_nothing_is_shared():
    row = _by_label([]).loc["event"]
    assert row.cover == pytest.approx(1 / 3, abs=1e-3)
    assert row.best_common_ancestor == ""


def test_copied_edges_are_not_a_shared_abstraction():
    edges = [(6, 9, EDGE_COPY), (7, 9, EDGE_COPY), (8, 9, EDGE_COPY)]
    row = _by_label(edges).loc["role"]
    assert row.cover == pytest.approx(1 / 3, abs=1e-3)


def test_terms_must_be_positioned_by_id():
    terms, labels, edges = _tables([])
    with pytest.raises(ValueError):
        redeclared_labels(terms.iloc[::-1], labels, edges)
