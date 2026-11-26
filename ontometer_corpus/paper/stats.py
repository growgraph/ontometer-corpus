"""Small statistics shared by the figure modules."""

from __future__ import annotations

import numpy


def dequantise(x, rng: numpy.random.Generator) -> numpy.ndarray:
    """Jitter tied values: spread each tied value uniformly over half the gap to its
    neighbouring distinct values; untied values are kept. The order of distinct values is
    preserved, so a dip test sees a continuous sample instead of point masses."""
    x = numpy.asarray(x, dtype=float)
    u, inv_, cnt = numpy.unique(x, return_inverse=True, return_counts=True)
    if len(u) < 2 or cnt.max() == 1:
        return x.copy()
    gaps = numpy.diff(u)
    left = numpy.r_[gaps[0], gaps] / 2
    right = numpy.r_[gaps, gaps[-1]] / 2
    tied = cnt[inv_] > 1
    out = x.copy()
    out[tied] = x[tied] + rng.uniform(-left[inv_][tied], right[inv_][tied])
    return out
