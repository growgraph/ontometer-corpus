"""``ontometer-corpus``: every derivation step, the rebuild, the object store and the paper.

Commands are imported only when run, so ``--help`` works on the base install and a
missing extra is reported by the command that needs it.
"""

from __future__ import annotations

import importlib

import click

#: Command name -> (module, attribute, one-line help). Order is the derivation order.
COMMANDS: dict[str, tuple[str, str, str]] = {
    "fetch": (
        "ontometer_corpus.steps.fetch_ontologies",
        "main",
        "Fetch ontologies from the registries into the store and the manifest.",
    ),
    "process": (
        "ontometer_corpus.steps.process",
        "main",
        "Seed, parse and deduplicate the fetched files into corpus.db.",
    ),
    "term-coupling": (
        "ontometer_corpus.steps.term_coupling",
        "main",
        "Term-level coupling per ontology (needs the store).",
    ),
    "network": (
        "ontometer_corpus.network.construction",
        "main",
        "Build the dependency network from corpus.db and the sidecars.",
    ),
    "structural-metrics": (
        "ontometer_corpus.steps.structural_metrics",
        "main",
        "Hierarchy metrics per ontology from the structural sidecars (needs the store).",
    ),
    "ontology-metrics": (
        "ontometer_corpus.steps.ontology_metrics",
        "main",
        "Write the per-ontology metric table.",
    ),
    "entity-roles": (
        "ontometer_corpus.steps.entity_roles",
        "main",
        "Per-term class/instance roles and labels (needs the store).",
    ),
    "entity-network": (
        "ontometer_corpus.steps.entity_network",
        "main",
        "Term-level homes, edge authority and the term-document table.",
    ),
    "abstractness": (
        "ontometer_corpus.steps.abstractness",
        "main",
        "Term- and ontology-level abstractness from the entity network.",
    ),
    "redeclarations": (
        "ontometer_corpus.steps.concept_redeclarations",
        "main",
        "Write the redeclared-label table.",
    ),
    "panel": (
        "ontometer_corpus.steps.abstractness_panel",
        "main",
        "Abstractness across releases of the same ontology (select, measure).",
    ),
    "dates": (
        "ontometer_corpus.steps.ontology_dates",
        "main",
        "First-evidence date of each ontology family.",
    ),
    "growth": (
        "ontometer_corpus.steps.abstractness_growth",
        "main",
        "Abstractness as the corpus grows: G(t).",
    ),
    "validation": (
        "ontometer_corpus.steps.abstractness_validation",
        "main",
        "Validate and analyse the abstractness measures.",
    ),
    "growth-viz": (
        "ontometer_corpus.steps.abstractness_growth_viz",
        "main",
        "Export G(t) with a Poincaré-disk layout.",
    ),
    "rebuild": (
        "ontometer_corpus.rebuild",
        "main",
        "Rebuild every derived table, from parse to the G(t) export.",
    ),
    "store": (
        "ontometer_corpus.store.cli",
        "cli",
        "Index, verify and exchange the object store.",
    ),
    "paper": (
        "ontometer_corpus.cli",
        "paper",
        "Figures, tables and numbers of the resource paper.",
    ),
}

#: ``paper`` subcommands, same shape as :data:`COMMANDS`.
PAPER_COMMANDS: dict[str, tuple[str, str, str]] = {
    "figures": (
        "ontometer_corpus.paper.figures",
        "main",
        "Figures, tables and \\stat macros of the paper.",
    ),
    "dedup-audit": (
        "ontometer_corpus.paper.dedup_audit",
        "main",
        "Score deduplication against the hand verdicts.",
    ),
    "comparators": (
        "ontometer_corpus.paper.comparators",
        "main",
        "Fetch and summarise the comparator reuse networks.",
    ),
    "github": (
        "ontometer_corpus.paper.github_ontologies",
        "main",
        "Sample, date and match the ontologies published on GitHub.",
    ),
}


class LazyGroup(click.Group):
    """A group whose commands are imported on first use."""

    def __init__(self, *args, table: dict[str, tuple[str, str, str]], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.table = table

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.table)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        entry = self.table.get(cmd_name)
        if entry is None:
            return None
        module, attr, _ = entry
        try:
            command = getattr(importlib.import_module(module), attr)
        except ImportError as exc:
            raise click.ClickException(str(exc)) from exc
        command.name = cmd_name
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = [(name, short) for name, (_, _, short) in self.table.items()]
        with formatter.section("Commands"):
            formatter.write_dl(rows)


@click.group(cls=LazyGroup, table=PAPER_COMMANDS)
def paper() -> None:
    """Figures, tables and numbers of the resource paper (the 'paper' extra)."""


@click.group(cls=LazyGroup, table=COMMANDS)
@click.version_option(package_name="ontometer-corpus")
def cli() -> None:
    """Derive the OntoMeter Corpus tables and the numbers of the paper that describes them.

    Every command takes --corpus (the tables directory) and, where it reads raw files,
    --store; ONTOMETER_CORPUS and ONTOMETER_STORE are the fallbacks.
    """
