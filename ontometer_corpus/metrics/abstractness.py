"""Abstractness of terms and ontologies, in two senses that are kept apart.

**Generality** (sense G) is taxonomic: how much of the world a class subsumes.
It is read off a subsumption DAG — depth from the roots, and intrinsic
information content (IC), which falls as a class gathers descendants.

**Class-likeness** (sense C) is terminological: whether a term is *used* as a
class (it has instances or subclasses, it constrains a property) or as an
instance (it is typed, it takes part in facts). Across many ontologies one IRI
can play both roles, so the share of documents using it as a class is a
continuous quantity rather than the TBox/ABox binary.

The two are different constructs — a leaf class is maximally specific yet
wholly class-like — so nothing here blends them.

The ontology-level quantities transpose Martin's package metrics: instability
``I = Ce / (Ca + Ce)`` from efferent and afferent specialisation couplings, and
the distance ``D = |A + I - 1|`` from the line on which stability and
abstractness balance.

The DAG routines work on integer node ids. A subsumption DAG merged across
many ontologies is large and has multiple inheritance; the naive
dynamic-programming count of descendants sums *paths*, which over-counts at
every diamond. :func:`dag_closure` counts exactly by propagating ancestor sets
in topological order, releasing each set once its last child has consumed it.

Needs the ``corpus`` extra (numpy, scipy).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

#: Accumulate this many ancestor ids before folding them into the counters.
_FLUSH_AT = 20_000_000


@dataclass(slots=True)
class DagClosure:
    """Per-node closure statistics of a subsumption DAG.

    Nodes in a cycle are collapsed to their strongly connected component and
    share its values; ``cyclic`` marks them.
    """

    #: Longest / shortest path from a root (a node with no parent).
    depth_long: np.ndarray
    depth_short: np.ndarray
    #: Distinct transitive ancestors, excluding the node itself.
    n_anc: np.ndarray
    #: Distinct transitive descendants, excluding the node itself.
    n_desc: np.ndarray
    #: Distinct leaf descendants; a leaf counts itself.
    n_leaves: np.ndarray
    is_leaf: np.ndarray
    cyclic: np.ndarray

    @property
    def n_nodes(self) -> int:
        """Number of nodes in the DAG, including those collapsed into a cycle."""
        return len(self.n_desc)


def _condense(
    n: int, child: np.ndarray, parent: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Collapse strongly connected components.

    Returns ``(comp_of_node, comp_child, comp_parent, comp_size)`` with
    self-loops and duplicate component edges removed.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if len(child) == 0:
        return np.arange(n), child, parent, np.ones(n, dtype=np.int64)
    graph = coo_matrix((np.ones(len(child), dtype=np.int8), (child, parent)), shape=(n, n)).tocsr()
    n_comp, comp = connected_components(graph, directed=True, connection="strong")
    c_child, c_parent = comp[child], comp[parent]
    keep = c_child != c_parent
    pairs = np.unique(np.stack([c_child[keep], c_parent[keep]], axis=1), axis=0)
    size = np.bincount(comp, minlength=n_comp)
    return comp, pairs[:, 0], pairs[:, 1], size


def dag_closure(n: int, child: np.ndarray, parent: np.ndarray) -> DagClosure:
    """Exact closure counts for a subsumption graph over nodes ``0..n-1``.

    Args:
        n: Number of nodes; ids must lie in ``range(n)``.
        child: Child id of each ``(child, parent)`` edge.
        parent: Parent id of each edge.

    Returns:
        A :class:`DagClosure` indexed by node id. Isolated nodes are roots and
        leaves at depth 0.
    """
    child = np.asarray(child, dtype=np.int64)
    parent = np.asarray(parent, dtype=np.int64)
    comp, cc, cp, size = _condense(n, child, parent)
    m = len(size)

    # CSR adjacency in both directions over components.
    order = np.argsort(cp, kind="stable")
    kids_ptr = np.zeros(m + 1, dtype=np.int64)
    np.add.at(kids_ptr, cp + 1, 1)
    kids_ptr = np.cumsum(kids_ptr)
    kids = cc[order]
    order = np.argsort(cc, kind="stable")
    par_ptr = np.zeros(m + 1, dtype=np.int64)
    np.add.at(par_ptr, cc + 1, 1)
    par_ptr = np.cumsum(par_ptr)
    pars = cp[order]

    n_parents = np.diff(par_ptr)
    n_kids = np.diff(kids_ptr)
    is_leaf_c = n_kids == 0

    depth_long = np.zeros(m, dtype=np.int64)
    depth_short = np.full(m, -1, dtype=np.int64)
    n_anc = np.zeros(m, dtype=np.int64)
    desc = np.zeros(m, dtype=np.int64)
    leaves = np.zeros(m, dtype=np.int64)

    remaining_parents = n_parents.copy()
    remaining_kids = n_kids.copy()
    queue = deque(np.flatnonzero(n_parents == 0).tolist())
    for r in queue:
        depth_short[r] = 0
    anc: dict[int, np.ndarray] = {}
    empty = np.empty(0, dtype=np.int64)
    # Pending (ancestor ids, weight) pairs: a component of k members adds k to
    # each ancestor's descendant count, and k to its leaf count if it is a leaf.
    buf_desc: list[tuple[np.ndarray, int]] = []
    buf_leaf: list[tuple[np.ndarray, int]] = []
    buffered = 0

    def fold(target: np.ndarray, buf: list[tuple[np.ndarray, int]]) -> None:
        if not buf:
            return
        ids = np.concatenate([a for a, _ in buf])
        weights = np.concatenate([np.full(len(a), w, dtype=np.int64) for a, w in buf])
        target += np.bincount(ids, weights=weights, minlength=m).astype(np.int64)
        buf.clear()

    def flush() -> None:
        nonlocal buffered
        fold(desc, buf_desc)
        fold(leaves, buf_leaf)
        buffered = 0

    while queue:
        node = queue.popleft()
        ps = pars[par_ptr[node] : par_ptr[node + 1]]
        if len(ps) == 0:
            a = empty
        elif len(ps) == 1:
            p = int(ps[0])
            a = np.append(anc[p], p)
        else:
            a = np.unique(np.concatenate([anc[int(p)] for p in ps] + [ps]))
        n_anc[node] = int(size[a].sum()) if len(a) else 0
        if len(a):
            buf_desc.append((a, int(size[node])))
            if is_leaf_c[node]:
                buf_leaf.append((a, int(size[node])))
            buffered += len(a)
        # Release parents whose every child has now been settled.
        for p in ps:
            p = int(p)
            remaining_kids[p] -= 1
            if remaining_kids[p] == 0:
                anc.pop(p, None)
        if n_kids[node]:
            anc[node] = a
        for k in kids[kids_ptr[node] : kids_ptr[node + 1]]:
            k = int(k)
            depth_long[k] = max(depth_long[k], depth_long[node] + 1)
            d = depth_short[node] + 1
            if depth_short[k] < 0 or d < depth_short[k]:
                depth_short[k] = d
            remaining_parents[k] -= 1
            if remaining_parents[k] == 0:
                queue.append(k)
        if buffered >= _FLUSH_AT:
            flush()
    flush()

    # Leaves count themselves (Sánchez et al. 2011); a cyclic leaf component
    # counts its members.
    leaves = leaves + np.where(is_leaf_c, size, 0)
    # Members of a cycle are each other's ancestors and descendants.
    desc = desc + (size - 1)
    n_anc = n_anc + (size - 1)

    return DagClosure(
        depth_long=depth_long[comp],
        depth_short=depth_short[comp],
        n_anc=n_anc[comp],
        n_desc=desc[comp],
        n_leaves=leaves[comp],
        is_leaf=is_leaf_c[comp],
        cyclic=(size > 1)[comp],
    )


def encode_edges(
    edges: list[tuple[str, str]] | set[tuple[str, str]],
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Map string ``(child, parent)`` edges to integer ids; self-loops dropped.

    Returns:
        ``(names, child_ids, parent_ids)``, where ``names[i]`` is node ``i``.
    """
    index: dict[str, int] = {}
    ch: list[int] = []
    pa: list[int] = []
    for c, p in edges:
        if c == p:
            continue
        ch.append(index.setdefault(c, len(index)))
        pa.append(index.setdefault(p, len(index)))
    names = [""] * len(index)
    for name, i in index.items():
        names[i] = name
    return names, np.asarray(ch, dtype=np.int64), np.asarray(pa, dtype=np.int64)


# --- sense G: generality from information content ---------------------------


def ic_seco(n_desc: np.ndarray, n_nodes: int) -> np.ndarray:
    """Intrinsic IC after Seco et al. (2004): ``1 - log(desc + 1) / log(N)``.

    1 for a leaf, falling towards 0 for a class that subsumes everything.
    """
    if n_nodes <= 1:
        return np.ones(len(n_desc))
    return 1.0 - np.log1p(n_desc) / math.log(n_nodes)


def ic_sanchez(n_leaves: np.ndarray, n_anc: np.ndarray) -> np.ndarray:
    """Intrinsic IC after Sánchez et al. (2011), normalised to ``[0, 1]``.

    ``-log((leaves / subsumers + 1) / (max_leaves + 1))`` divided by its
    maximum ``log(max_leaves + 1)``. Subsumers include the class itself, which
    lets two classes with the same leaves differ by how specialised they are.
    """
    n_leaves = np.asarray(n_leaves, dtype=float)
    max_leaves = float(n_leaves.max()) if len(n_leaves) else 1.0
    if max_leaves <= 0:
        return np.ones(len(n_leaves))
    raw = -np.log((n_leaves / (np.asarray(n_anc, dtype=float) + 1.0) + 1.0) / (max_leaves + 1.0))
    return np.clip(raw / math.log(max_leaves + 1.0), 0.0, 1.0)


# --- sense C: class-likeness from usage roles -------------------------------


def role_share(df_class: np.ndarray, df_inst: np.ndarray) -> np.ndarray:
    """Share of the documents using a term that use it as a class.

    NaN where the term is observed in neither role.
    """
    df_class = np.asarray(df_class, dtype=float)
    total = df_class + np.asarray(df_inst, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, df_class / total, np.nan)


# --- distributional diversity -----------------------------------------------


def entropy(counts: np.ndarray) -> float:
    """Shannon entropy (nats) of a count vector; 0 for an empty one."""
    counts = np.asarray(counts, dtype=float)
    counts = counts[counts > 0]
    total = counts.sum()
    if total <= 0:
        return 0.0
    p = counts / total
    return float(-(p * np.log(p)).sum())


def effective_number(counts: np.ndarray) -> float:
    """``exp(H)``: how many equally used categories the spread amounts to."""
    return math.exp(entropy(counts)) if np.asarray(counts).sum() > 0 else 0.0


# --- ontology level: Martin's metrics transposed -------------------------------


def martin_instability(ca: np.ndarray, ce: np.ndarray) -> np.ndarray:
    """``I = Ce / (Ca + Ce)``; NaN for an ontology coupled in neither direction.

    ``Ca`` (afferent) counts ontologies that specialise its classes, ``Ce``
    (efferent) the ontologies whose classes it specialises. 0 is maximally
    stable — depended on, depending on nothing.
    """
    ca = np.asarray(ca, dtype=float)
    ce = np.asarray(ce, dtype=float)
    total = ca + ce
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, ce / total, np.nan)


def main_sequence_distance(abstractness: np.ndarray, instability: np.ndarray) -> np.ndarray:
    """``D = |A + I - 1|``, the distance from the main sequence.

    Near 0, an ontology is as abstract as it is stable. Stable and concrete
    (``A`` and ``I`` both low) is the zone of pain: much depends on it and it
    is hard to change. Abstract and unstable is the zone of uselessness:
    general classes that nobody specialises.
    """
    return np.abs(
        np.asarray(abstractness, dtype=float) + np.asarray(instability, dtype=float) - 1.0
    )
