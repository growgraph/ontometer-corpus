# OntoMeter Corpus

A corpus of public ontologies, processed into tables: what each ontology is, how ontologies
depend on one another, and how abstract and how reused they are. The ontologies come from ten
public registries (six OntoPortal Alliance instances, OBO Foundry, Ontohub, Open Knowledge
Graphs and LOV), parsed and deduplicated across registries and versions.

![The abstractness race: the specialisation network of ontologies growing over time](assets/abstractness-race.gif){ width="420" }

The repository holds two things:

- **the tables**, in `data/`, versioned in git and released as `corpus-YYYY-MM` tags;
- **the `ontometer-corpus` package**, which loads the tables, derives them from the raw files,
  and regenerates the figures and numbers of the paper that describes the corpus.

The raw ontology files are not in the repository; they live in an [object store](store.md).

## I want to...

| I want to... | Read |
|---|---|
| Install the package and run a command | [Install and use](getting_started.md) |
| Know what each table holds and how the tables join | [The tables](tables.md) |
| See which command writes which table | [Overview](overview.md) |
| Fetch, parse and deduplicate ontologies | [Fetch, parse, dedup](pipeline.md) |
| Get, check, extend or publish the raw files | [Object store](store.md) |
| Know what a node and an edge of the dependency network mean | [Dependency network](network.md) |
| Rebuild every table, and know what a rebuild reproduces | [Rebuilding](rebuild.md) |
| Measure how general or how class-like a term is | [Abstractness](abstractness.md) |
| Know where the dates come from | [Dates](dates.md) |
| Watch the network of ontologies grow | [Abstractness race](growth.md) |
| Regenerate the paper's figures and numbers | [The paper](paper.md) |
| Call the code from Python | [API reference](reference/index.md) |
