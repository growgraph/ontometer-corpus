"""Generate build-time pages: the API reference, and the data of the G(t) view.

* One ``mkdocstrings`` page per module under ``reference/`` from the package's
  docstrings, plus a literate-nav ``SUMMARY.md``. Private modules (a leading
  underscore, other than ``__init__``) and ``__main__`` are skipped.
* ``g_t/growth_graph.json``, rebuilt from the tables in the corpus directory
  (``ONTOMETER_CORPUS``, else this repository's ``data/``) by
  ``ontometer_corpus.growth_network.build_viz``. It needs the ``tables`` extra;
  without it, or without the tables, the build continues and the view reports
  that its data is missing (logged as a warning).

Nothing here is committed output: the files exist only inside the build. A file
committed under ``docs/reference/`` at a generated path is inert, because the
plugin opens each path in ``"w"`` mode and replaces the entry before any page
renders; document a module by writing its docstrings.

Two hand-written pages are honoured. ``reference/index.md`` documents the
top-level package, so ``ontometer_corpus/__init__.py`` is skipped. A section
overview at ``reference/<pkg>/index.md`` shares its URL with the page a package
``__init__`` would generate, so a package whose overview exists on disk is
skipped too; that overview must then carry its own ``::: ontometer_corpus.<pkg>``
block, or the package is documented nowhere.
"""

import json
import logging
import os
from pathlib import Path

import mkdocs_gen_files

PACKAGE = "ontometer_corpus"
REFERENCE = Path("docs/reference")
#: The tables tracked in this repository, used when ONTOMETER_CORPUS is unset.
DEFAULT_CORPUS = Path("data")

nav = mkdocs_gen_files.Nav()
# The hand-written overview leads the section; it documents the top-level package.
nav[("Overview",)] = "index.md"

for path in sorted(Path(PACKAGE).rglob("*.py")):
    is_pkg_init = path.name == "__init__.py"
    if is_pkg_init:
        if path.parent == Path(PACKAGE):
            continue
        pkg_dir = path.parent.relative_to(PACKAGE)
        # A hand-written overview owns this URL -- see the module docstring.
        if (REFERENCE / pkg_dir / "index.md").exists():
            continue
        doc_path = pkg_dir.with_suffix(".md")
    else:
        doc_path = path.relative_to(PACKAGE).with_suffix(".md")
    full_doc_path = Path("reference", doc_path)

    parts = list(doc_path.with_suffix("").parts)
    if not parts or any(p.startswith("_") for p in parts):
        continue
    ident = ".".join([PACKAGE, *parts])
    nav[tuple(parts)] = doc_path.as_posix()

    with mkdocs_gen_files.open(full_doc_path, "w") as f:
        # The sidebar labels a page by its title, and a column of dotted paths
        # truncates to the same prefix on every row. The title is the module's
        # own name -- the sidebar's nesting supplies the rest -- while the
        # heading, and with it the search result, keeps the full path.
        f.write(f'---\ntitle: "{parts[-1]}"\n---\n\n')
        if is_pkg_init:
            # `show_submodules: false` keeps a package page from inlining its
            # whole subtree; the summary table indexes that subtree instead,
            # with each submodule's one-line docstring summary.
            f.write(
                f"# `{ident}`\n\n"
                f"::: {ident}\n"
                f"    options:\n"
                f"      show_submodules: false\n"
                f"      summary:\n"
                f"        modules: true\n"
            )
        else:
            f.write(f"# `{ident}`\n\n::: {ident}\n")

    # Edit paths resolve against the docs directory; the module lives above it.
    mkdocs_gen_files.set_edit_path(full_doc_path, Path("..", path))

with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())


def _growth_graph() -> None:
    log = logging.getLogger("mkdocs.plugins.gen_pages")
    try:
        from ontometer_corpus.growth_network import build_viz
        from ontometer_corpus.paths import CORPUS_ENV, CorpusPaths
    except ImportError as exc:
        log.warning("G(t) view: data not generated, the tables extra is missing (%s)", exc)
        return
    corpus_dir = Path(os.environ.get(CORPUS_ENV) or DEFAULT_CORPUS)
    try:
        data = build_viz(CorpusPaths.resolve(corpus_dir=corpus_dir))
    except FileNotFoundError as exc:
        log.warning("G(t) view: data not generated, a tracked table is missing (%s)", exc)
        return
    with mkdocs_gen_files.open("g_t/growth_graph.json", "w") as f:
        f.write(json.dumps(data, separators=(",", ":")))


_growth_graph()
