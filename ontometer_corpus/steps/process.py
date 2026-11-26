"""
Corpus processing: seed, parse, deduplicate.

Runs after the fetch step (`ontometer-corpus fetch`), which writes the object
store and manifest.jsonl. Three phases over the corpus registry, each
idempotent and resume-safe, so running a subset of phases is a normal
operation:

  Phase 0  seed   manifest.jsonl -> `corpus_registry` + `fetch_attempts`
                  tables in `<corpus>/corpus.db`
  Phase 1  parse  raw files -> parse columns of `corpus_registry` (triple/
                  class/property counts, identity, imports, content hash,
                  namespaces) plus one structural sidecar per raw file hash
                  under `<store>/cache/structure/v<N>/`
  Phase 2  dedup  Pass 0 (file hash), Pass A (IRI + explicit version IRI),
                  and corroborated Pass B matches set `is_duplicate` /
                  `canonical_entry_id`; unversioned content-hash matches are
                  retained as possible distinct releases.
                  Pass C sets `is_latest_version` per release family; Pass D
                  keeps one latest row per ontology IRI across registries and
                  sets `shared_iri` where an IRI covers several ontologies.
                  Rows excluded from Pass B are logged to
                  `<corpus>/dedup_exclusions.log`

Phase 1 reads the raw files and Pass D the structural sidecars, so both need
`--store`; without it, dedup skips Pass D with a warning.

The dependency network is built downstream from corpus.db and the structural
sidecars — see ``ontometer-corpus network``.

Usage:
    ontometer-corpus process
    ontometer-corpus process --phases parse --sequential
    ontometer-corpus process --corpus /tmp/run1 --store /path/to/store
"""

from __future__ import annotations

import sys

import click

from ontometer_corpus.paths import CorpusPaths, MissingStore, corpus_paths
from ontometer_corpus.pipeline.db import (
    create_schema,
    fetch_capture_stats,
    get_connection,
    reset_parse_state,
    seed_from_manifest,
)
from ontometer_corpus.pipeline.dedup import run_dedup
from ontometer_corpus.pipeline.parse import run_parse

PHASES = ("seed", "parse", "dedup")


def _phase_set(ctx: click.Context, param: click.Parameter, value: str) -> set[str]:
    """Parse ``--phases`` into a set, rejecting unknown names."""
    requested = {p.strip() for p in value.split(",") if p.strip()}
    unknown = requested - set(PHASES)
    if unknown:
        raise click.BadParameter(f"unknown phase(s): {', '.join(sorted(unknown))}")
    return requested


@click.command()
@click.option(
    "--phases",
    default=",".join(PHASES),
    show_default=True,
    callback=_phase_set,
    help=f"Comma-separated phases to run, always in the order {', '.join(PHASES)}.",
)
@click.option(
    "--sequential",
    is_flag=True,
    help=(
        "Parse one file at a time in-process instead of via a process pool. "
        "Useful to isolate a file that hangs or exhausts memory."
    ),
)
@click.option(
    "--batch-size",
    type=int,
    default=500,
    show_default=True,
    help="Rows per parse chunk; each chunk drains before the next is submitted.",
)
@click.option(
    "--max-workers",
    type=int,
    default=None,
    help="Parse worker processes. Default is a capped value, not os.cpu_count().",
)
@click.option(
    "--reparse",
    is_flag=True,
    help="Clear parse and dedup results first so every row is parsed again (sidecars are kept).",
)
@corpus_paths
def main(
    phases: set[str],
    sequential: bool,
    batch_size: int,
    max_workers: int | None,
    reparse: bool,
    paths: CorpusPaths,
) -> None:
    """Seed, parse and deduplicate the ontology corpus."""
    if reparse and "parse" not in phases:
        raise click.UsageError("--reparse needs the parse phase")
    paths.ensure_corpus_dir()

    print("OntoMeter — processing pipeline")
    print(f"  phases       : {', '.join(p for p in PHASES if p in phases)}")
    print(paths.describe())
    print()

    if "parse" in phases and paths.store_dir is None:
        print(
            "Error: the parse phase reads the raw ontology files. Pass "
            "--store, or set ONTOMETER_STORE, to the object-store root."
        )
        sys.exit(1)

    conn = get_connection(paths.db_path)
    create_schema(conn)
    if reparse:
        print(f"Reset parse state on {reset_parse_state(conn)} rows")

    if "seed" in phases:
        print("=== Phase 0: Seed ===")
        if not paths.manifest_path.exists():
            print(f"  no manifest at {paths.manifest_path} — nothing to seed")
        else:
            seed_from_manifest(conn, paths.manifest_path)

            stats = fetch_capture_stats(conn)
            if stats:
                print("\n  Fetch capture rate by registry:")
                print(
                    f"    {'registry':<14} {'captured':>8} {'failed':>7} "
                    f"{'attempts':>9} {'captured%':>10}"
                )
                for row in stats:
                    print(
                        f"    {row['source_registry']:<14} {row['captured']:>8} "
                        f"{row['failed']:>7} {row['attempts']:>9} {row['pct_captured']:>9}%"
                    )
    conn.close()

    if "parse" in phases:
        print("\n=== Phase 1: Parse ===")
        try:
            run_parse(
                paths.db_path,
                batch_size=batch_size,
                max_workers=max_workers,
                parallel=not sequential,
                paths=paths,
            )
        except MissingStore as exc:
            print(f"Error: {exc}")
            sys.exit(1)

    if "dedup" in phases:
        print("\n=== Phase 2: Dedup ===")
        run_dedup(
            paths.db_path,
            exclusion_log=paths.dedup_log,
            sidecar_path=paths.sidecar_path if paths.store_dir else None,
        )

    print(f"\n{'=' * 60}")
    print("Pipeline complete.")
    print(f"  corpus db : {paths.db_path}")
    print(f"  manifest  : {paths.manifest_path}")
    print("\nNext: build the dependency network from the processed corpus —")
    print("  uv run ontometer-corpus network")
    print()


if __name__ == "__main__":
    main()
