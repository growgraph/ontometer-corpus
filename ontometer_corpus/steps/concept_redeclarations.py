"""
Class labels declared in several home ontologies, with their cover.

One row per normalised label declared as an own class in at least three home
ontologies, with the largest share of those declarations one common ancestor
covers (see ``ontometer_corpus.network.redeclarations``).

Reads:  ``$ONTOMETER_STORE/cache/entity_abstractness.parquet``,
        ``$ONTOMETER_STORE/cache/entity_{edges,labels}.parquet``.
Writes: ``<corpus>/concept_redeclarations.parquet``.
Runs after: ``ontometer-corpus entity-network`` and ``ontometer-corpus abstractness``.

Usage:
    ontometer-corpus redeclarations --corpus /path/to/corpus --store /path/to/store
"""

import click
import pandas as pd

from ontometer_corpus.network.redeclarations import redeclared_labels
from ontometer_corpus.paths import CorpusPaths, corpus_paths


@click.command()
@corpus_paths
def main(paths: CorpusPaths) -> None:
    """Write the redeclared-label table."""
    d = paths.cache_dir
    terms = pd.read_parquet(
        d / "entity_abstractness.parquet",
        columns=["term", "iri", "home", "df_class", "is_property"],
    ).sort_values("term", ignore_index=True)
    table = redeclared_labels(
        terms,
        pd.read_parquet(d / "entity_labels.parquet"),
        pd.read_parquet(d / "entity_edges.parquet", columns=["child", "parent", "kind"]),
    )
    out = paths.concept_redeclarations_path
    table.to_parquet(out, index=False, compression="zstd")
    print(f"Wrote {len(table)} labels to {out}")


if __name__ == "__main__":
    main()
