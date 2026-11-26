"""Directive errors for an import that needs an optional extra.

The base install is the namespace rules and the path resolution only. Every
module that needs a heavier stack checks for it on import, so a missing extra
reads as one sentence naming the extra rather than whichever transitive
module happened to be missed first.
"""

from __future__ import annotations

import importlib.util

#: Import names each extra provides (the ones a module checks for).
EXTRAS: dict[str, tuple[str, ...]] = {
    "tables": ("numpy", "pandas", "pyarrow", "networkx"),
    "build": (
        "numpy",
        "pandas",
        "pyarrow",
        "networkx",
        "scipy",
        "matplotlib",
        "requests",
        "bs4",
        "rdflib",
        "pyoxigraph",
        "suthing",
        "yaml",
    ),
    "store": ("huggingface_hub", "zstandard"),
    "validation": ("nltk", "powerlaw"),
    "paper": ("seaborn", "powerlaw", "diptest"),
}


def require(extra: str, *modules: str) -> None:
    """Raise ``ImportError`` naming ``extra`` if any of ``modules`` (default: all of the
    extra's) is not importable."""
    names = modules or EXTRAS[extra]
    missing = [name for name in names if importlib.util.find_spec(name) is None]
    if missing:
        raise ImportError(
            f"this part of ontometer_corpus needs the '{extra}' extra. Install it with:\n"
            f'    pip install "ontometer-corpus[{extra}]"\n'
            f"(missing: {', '.join(missing)})"
        )
