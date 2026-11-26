"""The abstractness step: reuse communities do not depend on the order of the term-usage rows."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ontometer_corpus.network.entities import EXTERNAL
from ontometer_corpus.steps.abstractness import _communities


def _docs(seed: int) -> tuple[pd.DataFrame, np.ndarray, int]:
    """Two tight groups of ontologies, linked by a few shared terms."""
    rng = np.random.default_rng(seed)
    n_onto, n_terms = 12, 240
    term_home = np.repeat(np.arange(n_onto), n_terms // n_onto)
    term_home[:6] = EXTERNAL
    rows = []
    for onto in range(n_onto):
        group = range(0, 6) if onto < 6 else range(6, 12)
        for other in group:
            for t in np.flatnonzero(term_home == other)[:8]:
                rows.append((onto, int(t)))
    rows += [
        (0, int(np.flatnonzero(term_home == 7)[0])),
        (11, int(np.flatnonzero(term_home == 2)[0])),
    ]
    docs = pd.DataFrame(rows, columns=pd.Index(["onto", "term"]))
    return docs.sample(frac=1, random_state=int(rng.integers(1 << 31))), term_home, n_onto


def test_communities_ignore_row_order():
    first = _communities(*_docs(0))
    for seed in (1, 2, 3):
        assert (_communities(*_docs(seed)) == first).all()


def test_communities_are_numbered_by_size_then_smallest_member():
    comm = _communities(*_docs(0))
    assert comm[0] == 0
    assert set(comm[:6]) == {0}
    assert set(comm[6:]) == {1}
