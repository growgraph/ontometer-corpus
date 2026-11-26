"""A radial layout of the ontology specialisation network in the Poincaré disk.

A drawing, not a learned embedding. Each ontology gets a fixed *angle* from a
radial tree over the network at capture, so that it never moves sideways as
time passes, and a *radius* from how much it is specialised at a given time,
so that it moves inward exactly when it gains a specialiser.

* The tree is each ontology's *primary parent* — the ontology whose classes it
  specialises most — with cycles cut. Every root gets a wedge of the circle
  proportional to its subtree, roots are ordered by group (a community), and
  a node's children share its wedge in proportion to their own subtrees.
* The radius is a hyperbolic distance from the centre,
  ``r = r_max * (1 - log(1 + s) / log(n))`` for ``s`` transitive specialisers,
  mapped to the disk as ``tanh(r / 2)``, with ``n`` a fixed normaliser — one
  more than the largest ``s`` at capture puts the most specialised ontology at
  the centre. An ontology nothing specialises sits near the rim, and a fixed
  ``n`` keeps each ontology's radius monotone in time.
"""

from __future__ import annotations

import math

import numpy as np

#: Hyperbolic radius at which a leaf ontology (one nothing specialises) is drawn: disk radius 0.95.
DEFAULT_R_MAX = 2.0 * math.atanh(0.95)


def primary_parents(
    n: int, child: np.ndarray, parent: np.ndarray, weight: np.ndarray
) -> np.ndarray:
    """The parent each node specialises most, or -1; cycles are cut.

    Ties go to the lower parent index. A cycle in the chosen pointers — two
    ontologies specialising each other most — is broken by making its
    lowest-index member a root.

    Args:
        n: number of nodes; indices run 0..n-1.
        child: child node of each edge.
        parent: parent node of each edge.
        weight: weight of each edge (e.g. the number of term-level
            specialisations behind it); self-loops are ignored.

    Returns:
        The primary parent per node, -1 for a root.
    """
    out = np.full(n, -1, dtype=np.int64)
    best = np.full(n, -np.inf)
    order = np.lexsort((parent, -weight.astype(float)))
    for i in order:
        c, p, w = int(child[i]), int(parent[i]), float(weight[i])
        if c != p and w > best[c]:
            best[c] = w
            out[c] = p
    # Functional-graph cycle detection: walk each unvisited chain.
    state = np.zeros(n, dtype=np.int8)  # 0 new, 1 on the current path, 2 done
    for start in range(n):
        path = []
        v = start
        while v != -1 and state[v] == 0:
            state[v] = 1
            path.append(v)
            v = int(out[v])
        if v != -1 and state[v] == 1:
            cycle = path[path.index(v) :]
            out[min(cycle)] = -1
        for u in path:
            state[u] = 2
    return out


def radial_angles(primary_parent: np.ndarray, group: np.ndarray) -> np.ndarray:
    """Angle in ``[0, 2π)`` per node from a radial tree over ``primary_parent``.

    Roots are ordered by ``group`` and then by subtree size, largest first;
    each takes a wedge proportional to its subtree and is drawn at the
    wedge's centre, and its children split the wedge the same way.

    Args:
        primary_parent: parent per node, -1 for a root; must be acyclic (as
            :func:`primary_parents` returns it).
        group: integer group per node (e.g. a community); only roots' groups
            are used, to order them around the circle.

    Returns:
        The angle per node, in radians.
    """
    n = len(primary_parent)
    children: list[list[int]] = [[] for _ in range(n)]
    for v, p in enumerate(primary_parent):
        if p >= 0:
            children[int(p)].append(v)
    size = np.ones(n, dtype=np.int64)
    # Accumulate subtree sizes leaves-first: order nodes by depth, deepest first.
    depth = np.zeros(n, dtype=np.int64)
    for v in range(n):
        d, u = 0, int(primary_parent[v])
        while u >= 0:
            d += 1
            u = int(primary_parent[u])
        depth[v] = d
    for v in np.argsort(-depth, kind="stable"):
        p = int(primary_parent[v])
        if p >= 0:
            size[p] += size[v]
    roots = [v for v in range(n) if primary_parent[v] < 0]
    roots.sort(key=lambda v: (int(group[v]), -int(size[v]), v))
    angle = np.zeros(n)
    total = float(size[roots].sum()) if roots else 1.0
    stack: list[tuple[int, float, float]] = []
    start = 0.0
    for r in roots:
        width = 2.0 * math.pi * size[r] / total
        stack.append((r, start, width))
        start += width
    while stack:
        v, a0, w = stack.pop()
        angle[v] = a0 + w / 2.0
        kids = sorted(children[v], key=lambda u: (-int(size[u]), u))
        inner = float(size[v] - 1)
        a = a0
        for u in kids:
            wu = w * size[u] / inner
            stack.append((u, a, wu))
            a += wu
    return angle % (2.0 * math.pi)


def poincare_radius(specialisers: np.ndarray, n: int, r_max: float = DEFAULT_R_MAX) -> np.ndarray:
    """Disk radius for ``specialisers`` transitive specialisers, normalised by ``n``.

    Args:
        specialisers: number of transitive specialisers per node.
        n: the normaliser; a node with ``n - 1`` specialisers sits at the centre.
        r_max: hyperbolic radius of a node with no specialiser.

    Returns:
        The Euclidean radius in the unit disk, ``tanh(r / 2)``, per node.
    """
    s = np.asarray(specialisers, dtype=float)
    frac = np.log1p(s) / math.log(max(n, 2))
    r = r_max * np.clip(1.0 - frac, 0.0, 1.0)
    return np.tanh(r / 2.0)
