"""Rebuild every derived corpus table, from parse to the G(t) export.

Steps, in order: verify parse dedup term_coupling network structural_metrics
ontology_metrics roles entity_network abstractness redeclarations panel_select
panel_roles panel_measure dates growth validation growth_viz index.

``verify`` checks the object store against ``objects.tsv`` by size and stops the run
if an object is missing (skipped when there is no index yet); ``index`` rewrites
``objects.tsv`` from the rebuilt ``corpus.db``. Both are offline.

Fetch is never run: a new crawl is a different corpus. Each step runs in its own
process (``python -m ontometer_corpus <command>``) with ``--corpus`` / ``--store``
passed through. ``dates`` exits 75 when the web archive leaves lookups unanswered; the
lookups made so far are cached, so ``--from dates`` resumes, and ``--offline`` rebuilds
from the cache alone, failing if a lookup is missing.

``--local`` rebuilds from the corpus directory and object store with no network at
all: every step runs in a bubblewrap sandbox without a network, and dates come from
the cache, a missing lookup counting as no evidence. Files that load a remote JSON-LD
context fail to read and are reported by their step. ``validation`` runs only with
``--validation NORMS.tsv``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import click

from ontometer_corpus.paths import CORPUS_ENV, STORE_ENV

STEPS: tuple[str, ...] = (
    "verify",
    "parse",
    "dedup",
    "term_coupling",
    "network",
    "structural_metrics",
    "ontology_metrics",
    "roles",
    "entity_network",
    "abstractness",
    "redeclarations",
    "panel_select",
    "panel_roles",
    "panel_measure",
    "dates",
    "growth",
    "validation",
    "growth_viz",
    "index",
)

#: Exit status of the ``dates`` step when archive lookups are incomplete.
INCOMPLETE = 75


class Rebuild:
    """One rebuild run: the selected steps and how each is invoked."""

    def __init__(
        self,
        out: Path,
        reparse: bool,
        offline: bool,
        local: bool,
        norms: Path | None,
        path_args: list[str],
        dry_run: bool,
        bwrap: str,
    ) -> None:
        self.out = out
        self.reparse = reparse
        self.offline = offline
        self.local = local
        self.norms = norms
        self.path_args = path_args
        self.dry_run = dry_run
        self.sandbox = [bwrap, "--dev-bind", "/", "/", "--unshare-net"] if local else []

    def command(self, *args: str) -> list[str]:
        return [*self.sandbox, sys.executable, "-m", "ontometer_corpus", *args, *self.path_args]

    def run(self, *args: str) -> int:
        cmd = self.command(*args)
        if self.dry_run:
            click.echo("+ " + shlex.join(cmd))
            return 0
        return subprocess.run(cmd).returncode

    def check(self, *args: str) -> None:
        rc = self.run(*args)
        if rc:
            sys.exit(rc)

    def step(self, name: str) -> None:
        out = str(self.out)
        match name:
            case "verify":
                self.check("store", "verify", "--if-indexed")
            case "index":
                self.check("store", "index")
            case "parse":
                reparse = ("--reparse",) if self.reparse else ()
                self.check("process", "--phases", "seed,parse", *reparse)
            case "dedup":
                self.check("process", "--phases", "dedup")
            case "term_coupling":
                self.check("term-coupling")
            case "network":
                self.check("network")
            case "structural_metrics":
                self.check("structural-metrics")
            case "ontology_metrics":
                self.check("ontology-metrics")
            case "roles":
                self.check("entity-roles")
            case "entity_network":
                self.check("entity-network")
            case "abstractness":
                self.check("abstractness")
            case "redeclarations":
                self.check("redeclarations")
            case "panel_select":
                self.check("panel", "select")
            case "panel_roles":
                self.check("entity-roles", "--panel")
            case "panel_measure":
                self.check("panel", "measure")
            case "dates":
                if self.local:
                    rc = self.run("dates", "--offline", "--allow-uncached")
                elif self.offline:
                    rc = self.run("dates", "--offline")
                else:
                    rc = self.run("dates")
                if rc == INCOMPLETE:
                    click.echo("rebuild: archive lookups incomplete (cached so far).", err=True)
                    click.echo(
                        "rebuild: rerun with --from dates to resume, or use --local to build"
                        " with the cached lookups only (absent ones count as no evidence).",
                        err=True,
                    )
                    sys.exit(INCOMPLETE)
                if rc:
                    sys.exit(rc)
            case "growth":
                self.check("growth", "--out", out)
            case "validation":
                if self.norms is None:
                    click.echo("validation: skipped (pass --validation NORMS.tsv to run it)")
                else:
                    self.check("validation", "--out", out, "--concreteness", str(self.norms))
            case "growth_viz":
                self.check("growth-viz", "--out", out)
            case _:
                raise AssertionError(name)


def _absolute(path: Path | None) -> Path | None:
    return None if path is None else Path(os.path.abspath(path))


@click.command()
@click.option("--out", type=click.Path(path_type=Path), required=True, help="Output directory.")
@click.option("--from", "first", type=click.Choice(STEPS), default=STEPS[0], show_default=True)
@click.option("--to", "last", type=click.Choice(STEPS), default=STEPS[-1], show_default=True)
@click.option("--reparse", is_flag=True, help="Parse every file again (needs the parse step).")
@click.option("--offline", is_flag=True, help="Date from the cached lookups; fail on a miss.")
@click.option("--local", is_flag=True, help="Run every step in a sandbox with no network.")
@click.option(
    "--validation",
    "norms",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Concreteness norms (TSV) for the validation step; without it the step is skipped.",
)
@click.option("--corpus", type=click.Path(file_okay=False, path_type=Path), envvar=CORPUS_ENV)
@click.option("--store", type=click.Path(file_okay=False, path_type=Path), envvar=STORE_ENV)
@click.option("--dry-run", is_flag=True, help="Print the step commands without running them.")
def main(
    out: Path,
    first: str,
    last: str,
    reparse: bool,
    offline: bool,
    local: bool,
    norms: Path | None,
    corpus: Path | None,
    store: Path | None,
    dry_run: bool,
) -> None:
    """Rebuild every derived corpus table, from parse to the G(t) export."""
    lo, hi = STEPS.index(first), STEPS.index(last)
    if lo > hi:
        raise click.UsageError(f"--from {first} comes after --to {last}")
    if reparse and lo > STEPS.index("parse"):
        raise click.UsageError("--reparse needs the parse step (--from parse)")
    if offline and local:
        raise click.UsageError(
            "--offline and --local are exclusive (--local implies offline dates)"
        )
    path_args: list[str] = []
    for flag, value in (("--corpus", corpus), ("--store", store)):
        if value is not None:
            path_args += [flag, str(_absolute(value))]
    bwrap = os.environ.get("BWRAP", "bwrap")
    run = Rebuild(
        out=Path(os.path.abspath(out)),
        reparse=reparse,
        offline=offline,
        local=local,
        norms=_absolute(norms),
        path_args=path_args,
        dry_run=dry_run,
        bwrap=bwrap,
    )
    if local and not dry_run:
        try:
            ok = subprocess.run([*run.sandbox, "true"], capture_output=True).returncode == 0
        except OSError:
            ok = False
        if not ok:
            raise click.UsageError(
                f"--local needs bubblewrap ({bwrap}) able to create a network namespace"
            )
    for name in STEPS[lo : hi + 1]:
        click.echo(f"=== [{time.strftime('%H:%M:%S')}] {name}")
        run.step(name)
    click.echo(f"=== [{time.strftime('%H:%M:%S')}] done (outputs in {run.out})")
