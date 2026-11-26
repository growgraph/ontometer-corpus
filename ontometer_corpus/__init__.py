"""The OntoMeter Corpus: the pipeline that derives it, loaders for its tables, and the
analysis of the resource paper that describes it.

The base install is light: the namespace rules (:mod:`ontometer_corpus.namespaces`) and
path resolution (:mod:`ontometer_corpus.paths`). Everything else sits behind an extra
and says which one when it is missing:

``tables``
    read the tables and the dependency network (pandas, pyarrow, networkx);
``build``
    derive the tables from the raw files: fetch, parse, dedup, networks, dates;
``store``
    exchange and publish the object store (Hugging Face, zstd bundles);
``validation``
    the abstractness validation step;
``paper``
    the figures, tables and numbers of the resource paper.
"""
