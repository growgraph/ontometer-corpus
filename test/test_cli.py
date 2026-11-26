"""The ``ontometer-corpus`` command and the light base install."""

from __future__ import annotations

import subprocess
import sys

from click.testing import CliRunner

from ontometer_corpus.cli import COMMANDS, PAPER_COMMANDS, cli

#: What the base install must not pull in on import.
HEAVY = ("pandas", "numpy", "networkx", "pyarrow", "requests", "bs4", "pyoxigraph", "rdflib")


def test_help_lists_every_command() -> None:
    res = CliRunner().invoke(cli, ["--help"])
    assert res.exit_code == 0
    for name in COMMANDS:
        assert name in res.output


def test_every_command_resolves() -> None:
    runner = CliRunner()
    for name in COMMANDS:
        assert runner.invoke(cli, [name, "--help"]).exit_code == 0, name
    for name in PAPER_COMMANDS:
        assert runner.invoke(cli, ["paper", name, "--help"]).exit_code == 0, name


def test_base_modules_import_without_the_heavy_stack() -> None:
    code = (
        "import sys, ontometer_corpus, ontometer_corpus.namespaces, ontometer_corpus.paths, "
        "ontometer_corpus.cli; "
        f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


#: What the `tables` extra must not need: the rebuild stack of the `build` extra.
BUILD_ONLY = ("rdflib", "pyoxigraph", "owlready2", "requests", "bs4", "suthing", "matplotlib")

#: The modules a `tables` install uses: the loaders and the G(t) view the docs build.
TABLES_MODULES = (
    "ontometer_corpus.network.construction",
    "ontometer_corpus.ontology_metrics",
    "ontometer_corpus.growth_network",
)


def test_table_loaders_import_without_the_build_stack() -> None:
    code = (
        f"import sys; import {', '.join(TABLES_MODULES)}; "
        f"print(sorted(m for m in {BUILD_ONLY!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
