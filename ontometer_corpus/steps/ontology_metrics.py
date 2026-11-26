"""
The per-ontology metric table, from the corpus tables.

Joins the registry counts, the structural and term-coupling tables and
the reuse-network measures into one row per latest, parsed ontology (see
``ontometer_corpus.ontology_metrics`` for every column and its rules). Analyses
and figures read this table instead of recomputing it.

Reads:  ``<corpus>/corpus.db``, ``structural_metrics.parquet``,
        ``term_coupling.parquet``,
        ``namespace_usage.parquet`` and the network tables; never the store.
Writes: ``<corpus>/ontology_metrics.parquet``, indexed by node id.
Runs after: ``ontometer-corpus structural-metrics``, ``ontometer-corpus term-coupling``, the
network step.

Usage:
    ontometer-corpus ontology-metrics --corpus /path/to/corpus
"""

import click

from ontometer_corpus.ontology_metrics import build_ontology_metrics
from ontometer_corpus.paths import CorpusPaths, corpus_paths


@click.command()
@corpus_paths
def main(paths: CorpusPaths) -> None:
    """Write the per-ontology metric table."""
    table = build_ontology_metrics(paths)
    out = paths.ontology_metrics_path
    table.to_parquet(out, compression="zstd")
    print(f"Wrote {len(table)} ontologies x {table.shape[1]} columns to {out}")


if __name__ == "__main__":
    main()
