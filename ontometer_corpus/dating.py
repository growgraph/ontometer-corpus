"""When an ontology first existed: the earliest date any source attests to it.

No single source dates an ontology's birth. A registry records when *it* took a
submission, so a mirror portal that loaded a long-established ontology last
year dates it last year; a web archive records its first crawl of the IRI,
which lags publication and misses IRIs that moved; knowledge bases record an
inception date for a minority of ontologies. Each is therefore an *upper
bound* on the birth date, and the earliest of them is the tightest bound
available — the *first-evidence date*.

A second bound comes from the dependency structure itself: a class cannot be
specialised before it exists, so an ontology existed no later than the first
observed specialisation of one of its classes. :func:`bound_births` applies
that bound.
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("tables", "numpy")


import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as calendar_date

import numpy as np

_EXT = re.compile(r"\.(owl|rdf|ttl|obo|n3|nt|jsonld|xml|owx|ofn)$")
_OBO = re.compile(r"^purl\.obolibrary\.org/obo/([a-z0-9_]+)$")
_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})")

#: Dates earlier than this are rejected as noise: the web-ontology languages
#: and the first biomedical ontology releases postdate it.
EARLIEST_PLAUSIBLE = "1995-01-01"


def iri_match_key(iri: str | None) -> str | None:
    """Matching key of an ontology IRI: scheme-less, lower-cased, extension-stripped.

    ``https://example.org/onto.owl#`` and ``http://example.org/onto`` get the
    same key; used to link rows of one ontology across registries. The key is
    not an IRI and is not meant to be dereferenced.

    Args:
        iri: an ontology IRI, or None.

    Returns:
        The key without scheme, trailing ``#``/``/`` or a known ontology file
        extension; None for a missing, non-string or empty input.
    """
    if not iri or not isinstance(iri, str):
        return None
    s = iri.strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.rstrip("#/")
    s = _EXT.sub("", s)
    return s or None


def archive_candidates(iri: str | None) -> list[str]:
    """URLs under which a web archive may have captured an ontology IRI.

    The IRI itself (scheme-less, separators stripped), and for OBO PURLs also
    the ``.owl`` download, which is what crawlers usually saw. IRIs without a
    host — local file paths an author left in place — have none.

    Args:
        iri: an ontology IRI, or None.

    Returns:
        Scheme-less candidate URLs, possibly empty.
    """
    base = iri_match_key(iri)
    if not base:
        return []
    host = base.split("/", 1)[0]
    if "." not in host or ":" in host:
        return []
    out = [base]
    if _OBO.match(base):
        out.append(f"{base}.owl")
    return out


def archive_timestamp_date(ts: str | None) -> str | None:
    """ISO date of a ``YYYYMMDDhhmmss`` capture timestamp.

    Args:
        ts: a web-archive capture timestamp, or None.

    Returns:
        ``YYYY-MM-DD``, or None when ``ts`` is missing or does not start with
        eight digits.
    """
    if not ts:
        return None
    m = _TS.match(ts.strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def plausible(date: str | None, latest: str) -> str | None:
    """``date`` truncated to ``YYYY-MM-DD`` if it lies in the plausible range.

    Args:
        date: an ISO date or datetime string; a bare year becomes 1 January.
        latest: the latest acceptable ``YYYY-MM-DD`` (usually the capture date).

    Returns:
        The truncated date if it lies in ``[EARLIEST_PLAUSIBLE, latest]``,
        otherwise None.
    """
    if not date or not isinstance(date, str):
        return None
    text = date.strip()
    if re.fullmatch(r"\d{4}", text):
        d = f"{text}-01-01"
    elif len(text) >= 10:
        d = text[:10]
    else:
        return None
    try:
        calendar_date.fromisoformat(d)
        calendar_date.fromisoformat(latest)
    except ValueError:
        return None
    return d if EARLIEST_PLAUSIBLE <= d <= latest else None


def earliest(dates: Iterable[str | float | None]) -> str | None:
    """The minimum of the ISO dates given, ignoring missing ones.

    Args:
        dates: ISO date strings; None, NaN and empty strings are skipped.

    Returns:
        The earliest date, or None when none is present.
    """
    present = [d for d in dates if isinstance(d, str) and d]
    return min(present) if present else None


def bound_births(births: np.ndarray, parent: np.ndarray, edge_time: np.ndarray) -> np.ndarray:
    """Bound each ontology's birth by the time it was first observed being specialised.

    ``births`` holds days since an epoch (NaN when unknown); specialisation
    ``i`` of a class of ``parent[i]`` was observed at ``edge_time[i]`` (NaN when
    unknown). A class exists before anything specialises it, so the result is
    ``min(births[p], edge_time[i] for every i with parent[i] == p)``; an undated
    parent with a dated specialisation becomes dated.

    The bound is applied once, not chained: an ontology observed to exist
    early is not thereby observed to have made its own specialisations early.

    Args:
        births: birth per ontology, in days since an epoch.
        parent: parent ontology index of each specialisation.
        edge_time: observation time of each specialisation.

    Returns:
        The bounded birth per ontology, same shape as ``births``.
    """
    out = births.astype(float).copy()
    known = ~np.isnan(edge_time)
    cand = np.full(len(out), np.inf)
    np.minimum.at(cand, parent[known], edge_time[known])
    cand[np.isinf(cand)] = np.nan
    return np.fmin(out, cand)


def time_pairs(
    birth: np.ndarray,
    release: np.ndarray,
    child: np.ndarray,
    parent: np.ndarray,
    panel_first: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Arrival time of each specialisation pair, and the bounded births.

    All times are days since an epoch, NaN when unknown. Pair ``i`` says
    ``child[i]`` specialises a class of ``parent[i]``; ``panel_first[i]`` is the
    first release of the child that asserts it, where the child's release
    history is known.

    * Arrival: ``panel_first`` where known, otherwise the child's first
      evidence ``birth[child]`` — an assumption that the pair dates from the
      child's first release.
    * Bounded birth: :func:`bound_births` over *observed* times only — the
      first release asserting the pair, or the child's captured release
      ``release[child]``. The assumed arrival never moves a parent's birth.

    Args:
        birth: first-evidence birth per ontology.
        release: captured release date per ontology.
        child: child ontology index of each pair.
        parent: parent ontology index of each pair.
        panel_first: first release of the child asserting each pair.

    Returns:
        ``(arrival, bounded_birth)``. Callers still place a pair no earlier
        than its parent's birth.
    """
    known = ~np.isnan(panel_first)
    arrival = np.where(known, panel_first, birth[child])
    observed = np.where(known, panel_first, release[child])
    return arrival, bound_births(birth, parent, observed)


@dataclass(frozen=True)
class DatingConsistency:
    """How often specialisations point from a younger ontology to an older one.

    Attributes:
        n_pairs: specialisation pairs with both ends dated.
        share: share of those pairs whose child is born no earlier than its parent.
        null_mean: the same share, averaged over permutations of the birth dates.
        null_q975: 97.5th percentile of the permuted shares.
    """

    n_pairs: int
    share: float
    null_mean: float
    null_q975: float


def dating_consistency(
    births: np.ndarray, child: np.ndarray, parent: np.ndarray, reps: int = 500, seed: int = 0
) -> DatingConsistency:
    """Check dates against the specialisation network.

    A child that specialises a parent's class should not be born before the
    parent. Correct dates make that the rule; dates unrelated to the network
    leave it at the share a random assignment gives, which the permutation
    null estimates by shuffling the births among the dated ontologies.

    Args:
        births: birth per ontology, in days since an epoch (NaN when unknown).
        child: child ontology index of each specialisation pair.
        parent: parent ontology index of each specialisation pair.
        reps: number of permutations.
        seed: seed of the permutation generator.

    Returns:
        The observed share and its permutation null.
    """
    m = ~np.isnan(births[child]) & ~np.isnan(births[parent])
    c, p = child[m], parent[m]
    obs = float((births[c] >= births[p]).mean()) if m.any() else float("nan")
    rng = np.random.default_rng(seed)
    dated = np.flatnonzero(~np.isnan(births))
    null = np.empty(reps)
    shuffled = births.astype(float).copy()
    for i in range(reps):
        shuffled[dated] = rng.permutation(births[dated])
        null[i] = (shuffled[c] >= shuffled[p]).mean() if m.any() else np.nan
    return DatingConsistency(
        n_pairs=int(m.sum()),
        share=obs,
        null_mean=float(null.mean()),
        null_q975=float(np.quantile(null, 0.975)),
    )
