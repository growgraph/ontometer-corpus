"""
Data for the interactive view of G(t) in the Poincaré disk.

Reads only tables in the corpus directory (no object store):
``<corpus>/ontology_abstractness.parquet``, ``<corpus>/ontology_dates.parquet``,
``<corpus>/ontology_specialisations.parquet``, and ``<corpus>/corpus.db``
for labels.
Writes: ``growth_graph.json`` to ``--out``.
Runs after: ``ontometer-corpus growth`` (which writes
``ontology_specialisations.parquet``); once the three tables are in the data
directory, nothing else is needed.
Does not need ``--store``.

The payload and the layout are defined in ``ontometer_corpus.growth_network``;
the documentation build generates the same file, so this script is for
viewing it elsewhere.

Usage:
    ontometer-corpus growth-viz --out /path/to/out
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ontometer_corpus.growth_network import build_viz
from ontometer_corpus.paths import CorpusPaths, corpus_paths


@click.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for growth_graph.json.",
)
@click.option("--step", type=float, default=0.25, show_default=True, help="Time step in years.")
@corpus_paths
def main(out: Path, step: float, paths: CorpusPaths) -> None:
    """Export G(t) with a Poincaré-disk layout."""
    data = build_viz(paths, step=step)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "growth_graph.json"
    target.write_text(json.dumps(data, separators=(",", ":")))
    meta = data["meta"]
    print(
        f"Wrote {target}  ({meta['n_shown']} ontologies, {meta['n_edges']} links, "
        f"{meta['n_undated']} undated left out)"
    )


if __name__ == "__main__":
    main()
