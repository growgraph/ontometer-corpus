# Install and use

## Install

The base install is small: the namespace rules and path resolution. Each part of the work is
an extra; name every extra you need in one install.

| Install | For |
|---|---|
| `pip install ontometer-corpus` | the namespace rules ([`namespaces`](reference/namespaces.md)) and path resolution ([`paths`](reference/paths.md)) |
| `pip install "ontometer-corpus[tables]"` | loading the tables and the networks |
| `pip install "ontometer-corpus[build]"` | rebuilding the tables from the raw files |
| `pip install "ontometer-corpus[store]"` | getting, checking and publishing the object store |
| `pip install "ontometer-corpus[validation]"` | the abstractness validation step |
| `pip install "ontometer-corpus[paper]"` | the figures, tables and numbers of the paper |
| `pip install "ontometer-corpus[all]"` | everything above |

A module that needs a missing extra says which one when it is imported.

## Two locations

Every command reads two locations, and neither has a default:

| Location | Flag | Environment | Needed by |
|---|---|---|---|
| Corpus directory: `data/` in a clone of this repository | `--corpus` | `ONTOMETER_CORPUS` | every command |
| Object store of raw files and caches | `--store` | `ONTOMETER_STORE` | commands that read raw files |

```bash
git clone https://github.com/growgraph/ontometer-corpus
export ONTOMETER_CORPUS=$PWD/ontometer-corpus/data
export ONTOMETER_STORE=$HOME/ontometer-store     # see Object store
```

## Load the tables

```python
from ontometer_corpus.network.construction import load_network
from ontometer_corpus.ontology_metrics import load_ontology_metrics
from ontometer_corpus.paths import CorpusPaths

paths = CorpusPaths.resolve("ontometer-corpus/data")
G = load_network(paths.corpus_dir)          # the dependency network, networkx.MultiDiGraph
metrics = load_ontology_metrics(paths)      # one row per ontology, indexed by network node
```

[The tables](tables.md) describes every file and how they join; the tables are plain Parquet,
SQLite, JSON Lines and TSV, so the package is optional for reading them.

## The command

`ontometer-corpus` has one subcommand per derivation step, plus:

| Command | Does |
|---|---|
| `ontometer-corpus --help` | lists every step, in derivation order |
| `ontometer-corpus rebuild --out DIR` | runs every step from parse to the G(t) export ([Rebuilding](rebuild.md)) |
| `ontometer-corpus store …` | the object store ([Object store](store.md)) |
| `ontometer-corpus paper figures --out DIR` | the paper's figures, tables and `\stat` numbers ([The paper](paper.md)) |

`python -m ontometer_corpus` is the same command.
