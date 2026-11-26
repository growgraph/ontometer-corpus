# Contributing to ontometer-corpus

This repository holds the corpus tables (`data/`) and the `ontometer-corpus` package that
derives them. Welcome as issues or pull requests:

- corrections to the data, such as a wrong registry, licence or date for an ontology;
- licence and redistribution decisions in `data/redistribution.yaml`;
- changes to the package: the pipeline, the loaders, the paper's analysis, the documentation.

Name the corpus version or tag a data problem was seen in. A change to how a table is computed
lands as code first; the rebuilt tables follow as a new corpus version, never in an unrelated
change.

Setting up a working copy, running the checks and building the documentation:
[Contributing](https://growgraph.github.io/ontometer-corpus/contributing/).

Code is published under the [Apache License 2.0](LICENSE), and changes to the tables under
[CC BY 4.0](data/LICENSE). Before your first pull request is merged, you accept the
[Growgraph Contributor License Agreement](CLA.md) once, by commenting on the pull request:

    I have read the Growgraph CLA and I accept it.
