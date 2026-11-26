"""Growth of a specialisation network: survival of leaves and attachment nulls.

When a network of ontologies grows, an ontology that nothing specialises (a
*terminal* ontology, a leaf of the network) becomes a branch the first time a
newcomer specialises one of its classes. Specialiser counts only grow, so
growth of one ontology is not informative by itself; these functions measure
the rate and the concentration of that growth: how fast leaves turn into
branches (survival), and whether newcomers spread over the existing
ontologies or concentrate on a few (attachment nulls).

Needs the ``corpus`` extra (numpy).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def kaplan_meier(durations: np.ndarray, observed: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Kaplan–Meier survival estimate.

    Args:
        durations: time to the event, or to censoring when the event was not
            observed.
        observed: True where the event happened at ``durations``.

    Returns:
        ``(times, survival)``: the distinct event times and the estimated
        probability of surviving past each of them.
    """
    d = np.asarray(durations, dtype=float)
    e = np.asarray(observed, dtype=bool)
    times = np.unique(d[e])
    surv = np.empty(len(times))
    s = 1.0
    for i, t in enumerate(times):
        at_risk = int((d >= t).sum())
        events = int(((d == t) & e).sum())
        s *= 1.0 - events / at_risk
        surv[i] = s
    return times, surv


def survival_at(times: np.ndarray, surv: np.ndarray, t: float) -> float:
    """Value of a Kaplan–Meier step function at ``t``.

    Args:
        times: event times, as :func:`kaplan_meier` returns them.
        surv: survival after each event time.
        t: time at which to read the step function.

    Returns:
        The survival probability at ``t``; 1.0 before the first event.
    """
    i = np.searchsorted(times, t, side="right")
    return 1.0 if i == 0 else float(surv[i - 1])


@dataclass(frozen=True, slots=True)
class AttachmentNull:
    """Outcome of re-targeting every specialisation at random.

    Attributes:
        n_targets: per replicate, the number of distinct ontologies specialised
            at least once — the number of leaves that became branches.
        top_share: per replicate, the share of all specialisations received by
            the most specialised tenth of targets.
    """

    n_targets: np.ndarray
    top_share: np.ndarray


def top_decile_share(in_degree: np.ndarray) -> float:
    """Share of the total received by the top tenth of nodes with any in-degree.

    Args:
        in_degree: in-degree (or any non-negative count) per node.

    Returns:
        The share in ``(0, 1]``, or NaN when no node has positive in-degree.
    """
    x = np.sort(in_degree[in_degree > 0])[::-1]
    if not len(x):
        return float("nan")
    k = max(1, int(np.ceil(0.1 * len(x))))
    return float(x[:k].sum() / x.sum())


def attachment_null(
    edge_time: np.ndarray,
    edge_child: np.ndarray,
    birth: np.ndarray,
    *,
    preferential: bool,
    reps: int,
    seed: int = 0,
) -> AttachmentNull:
    """Replay the arrival of specialisations with random targets.

    Each specialisation ``i`` arrives at ``edge_time[i]`` from ``edge_child[i]``
    and, in the null, lands on an ontology already born at that time other
    than the child itself and other than a target the child already chose —
    uniformly, or with probability proportional to one plus the number of
    specialisations the target has received so far (preferential attachment).

    Args:
        edge_time: arrival time of each specialisation (distinct child–target pairs).
        edge_child: node index of the specialising ontology.
        birth: birth time per node; NaN nodes are never targets.
        preferential: attach in proportion to ``1 + received`` instead of uniformly.
        reps: number of replicates.
        seed: random seed.

    Returns:
        Per replicate, the number of distinct targets and the top-decile share
        of the specialisations received. A specialisation with no eligible
        target at its arrival time is dropped.
    """
    rng = np.random.default_rng(seed)
    order = np.argsort(edge_time, kind="stable")
    t_sorted = edge_time[order]
    c_sorted = edge_child[order]
    dated = np.flatnonzero(~np.isnan(birth))
    by_birth = dated[np.argsort(birth[dated], kind="stable")]
    born = birth[by_birth]
    avail = np.searchsorted(born, t_sorted, side="right")
    pos = np.empty(len(birth), dtype=np.int64)
    pos[by_birth] = np.arange(len(by_birth))
    n_targets = np.empty(reps, dtype=np.int64)
    top = np.empty(reps)
    for r in range(reps):
        received = np.zeros(len(by_birth))
        chosen: dict[int, set[int]] = {}
        for i in range(len(t_sorted)):
            k = int(avail[i])
            child = int(c_sorted[i])
            taken = chosen.setdefault(child, set())
            excluded = {int(pos[child])} if not np.isnan(birth[child]) else set()
            excluded |= taken
            if k - len([x for x in excluded if x < k]) <= 0:
                continue
            if preferential:
                w = received[:k] + 1.0
                for x in excluded:
                    if x < k:
                        w[x] = 0.0
                j = int(rng.choice(k, p=w / w.sum()))
            else:
                while True:
                    j = int(rng.integers(k))
                    if j not in excluded:
                        break
            received[j] += 1
            taken.add(j)
        n_targets[r] = int((received > 0).sum())
        top[r] = top_decile_share(received)
    return AttachmentNull(n_targets=n_targets, top_share=top)
