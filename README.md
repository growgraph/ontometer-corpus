# ontometer-corpus

A corpus of public ontologies, processed into tables: what each ontology is, how ontologies
depend on one another, and how abstract and how reused they are. The ontologies were collected
from public registries (the OntoPortal instances BioPortal, AgroPortal, BiodivPortal, MatPortal,
TechnoPortal and OntoPortal-Astro, plus OBO Foundry, Ontohub, Open Knowledge Graphs and LOV),
then parsed and deduplicated across registries and versions.

Everything here is produced by [**OntoMeter**](https://github.com/growgraph/OntoMeter), the
ontology-quality and ontology-population toolkit. You do not need OntoMeter to *read* the data:
every table is Parquet, SQLite, JSON Lines or TSV, and the network recipes below need only
`pandas` and `networkx`. OntoMeter adds ready-made loaders and the pipeline that rebuilds the
tables.

The raw ontology files are **not** in this repository (see [The raw files](#the-raw-files)).

## Contents

- [Quick start: the ontology network in memory](#quick-start-the-ontology-network-in-memory)
- [What is in `data/`](#what-is-in-data)
- [How the tables join](#how-the-tables-join)
- [The dependency network](#the-dependency-network)
- [The specialisation network, and G(t)](#the-specialisation-network-and-gt)
- [The registry database, `corpus.db`](#the-registry-database-corpusdb)
- [The raw files](#the-raw-files)
- [Working with OntoMeter](#working-with-ontometer)
- [Versions, citing, licence](#versions-citing-licence)

## Quick start: the ontology network in memory

```bash
git clone https://github.com/growgraph/ontometer-corpus
pip install pandas pyarrow networkx
```

```python
import json

import networkx as nx
import pandas as pd

nodes = pd.read_parquet("ontometer-corpus/data/network_nodes.parquet")
edges = pd.read_parquet("ontometer-corpus/data/network_edges.parquet")

G = nx.MultiDiGraph()
for row in nodes.to_dict("records"):
    for key in ("own_namespaces", "referenced_namespaces", "imports"):
        row[key] = json.loads(row[key]) if row[key] else []
    G.add_node(row.pop("node_id"), **row)
for row in edges.to_dict("records"):
    for key in ("reference_iris", "reference_kinds", "resolutions"):
        row[key] = json.loads(row[key]) if row[key] else []
    G.add_edge(row.pop("source"), row.pop("target"), key=row["edge_type"], **row)

corpus = [n for n, d in G.nodes(data=True) if not d["is_external"]]
G.number_of_nodes(), G.number_of_edges(), len(corpus)
```

`G` is the **dependency network**: one node per ontology in the corpus plus one per external
target it references, and directed edges `ontology → what it depends on`, keyed by
`edge_type` (`import` or `usage`). Each (source, target, edge type) occurs once, so the key is
unique. The whole graph fits comfortably in memory (tens of thousands of edges). With OntoMeter
installed, the same graph is one call:

```python
from ontometer.corpus.network.construction import load_network

G = load_network("ontometer-corpus/data")  # networkx.MultiDiGraph
```

## What is in `data/`

| File | One row per | What it holds |
|---|---|---|
| `manifest.jsonl` | fetch attempt | The capture record: registry, `source_id`, `canonical_url`, retrieval date, `status` (`ok`/`fail`), `error`, the `file_hash` and store path of what was downloaded, and the registry's own metadata entry (projected to a field allowlist, contact details removed). The primary record: everything else is derived from it and the raw files |
| `corpus.db` | registry entry | SQLite. `corpus_registry`: every fetched file with its parse result, ontology identity (IRI, version IRI), counts, namespaces, imports, licence, and the dedup outcome. `fetch_attempts`: every attempt, successes and failures. See [below](#the-registry-database-corpusdb) |
| `objects.tsv` | raw file | `file_hash`, `size`, `path` of every raw file the corpus references, to check an object store against |
| `network_nodes.parquet` | network node | Dependency-network nodes: corpus ontologies (counts, namespaces, imports, registry) and external targets (`external_iri`, `external_kind`) |
| `network_edges.parquet` | (source, target, edge type) | Dependency-network edges: `import` (`owl:imports`) or `usage` (terms used from a namespace the target owns), with the evidence behind each |
| `namespace_usage.parquet` | (file, namespace) | Distinct terms a file uses from a namespace, by how: logically, as annotation, or only declared |
| `term_coupling.parquet` | file | How many of a file's terms are external, over all terms, domain terms and logically used terms; counts of logical statements and restrictions |
| `structural_metrics.parquet` | file | Shape of the class hierarchy: roots, depth (mean, max, histogram), branching, multiple inheritance, orphans, cycles |
| `oquare_metrics.parquet` | file | The OQuaRE metrics (`ANOnto`, `DITOnto`, `NOCOnto`, …) as OntoMeter computes them |
| `ontology_metrics.parquet` | ontology | One wide table joining the above: counts, hierarchy shape, coupling, OQuaRE (`oq_*`) and position in the reuse network (degrees, PageRank, betweenness, IDF-weighted reuse). Indexed by network `node` |
| `ontology_abstractness.parquet` | ontology (release family) | Term-based abstractness: `A_local`, `A_global`, `A_class`; dependency metrics `Ca`, `Ce`, `I`, `D`; `layer`, reuse `community`; population flags `subject`, `kb_like`, `flat`. Index `onto` (0..n-1) |
| `ontology_specialisations.parquet` | (child, parent) ontology pair | The **specialisation network**: the child asserts one of its classes under a class of the parent; `n_terms` such assertions; `panel_first`, the first release that asserts it where known |
| `concept_redeclarations.parquet` | normalised class label | Labels declared as an own class in at least three home ontologies: `n_homes`, `n_terms`, and `cover`, the largest share of those declarations one common ancestor unites (copied edges left out), with that `best_common_ancestor` and `example_iris`. Matching is lexical |
| `abstractness_panel.parquet` | release | Abstractness per release, for families with several dated releases, and how the hierarchy changed from the previous release |
| `ontology_dates.parquet` | release family | First-evidence birth date: from the registry, the web archive or Wikidata (`birth_source`), and `as_of`, the capture date |
| `abstractness_growth.parquet` | ontology | Birth, first specialisation and dependency counts under two dating rules (`*_bounded`, `*_first_evidence`) |
| `dating_cache.jsonl` | lookup | Cached web-archive and Wikidata answers behind `ontology_dates`, so dating reruns offline |
| `redistribution.yaml` | — | Which declared licences allow the raw files to be republished |
| `zenodo.yaml` | — | Metadata for the archived release of the raw files |

Definitions of the abstractness and dependency measures:
[Abstractness](https://growgraph.github.io/OntoMeter/corpus/abstractness/). Dates:
[Dates](https://growgraph.github.io/OntoMeter/corpus/dates/). Node and edge model:
[Network](https://growgraph.github.io/OntoMeter/corpus/network/).

## How the tables join

Different tables count different things, so they carry different keys:

| Key | Meaning | Found in |
|---|---|---|
| `file_hash` | SHA-256 of the raw file bytes; the identity of a downloaded file | everywhere: `manifest.jsonl`, `corpus.db`, `objects.tsv`, network nodes, every per-file table |
| `content_hash` | SHA-256 of the sorted class and property IRIs; equal for two serialisations of the same ontology | `corpus.db`, network nodes, `term_coupling`, `ontology_abstractness`, `abstractness_panel` |
| `source_id` | The registry's identifier of one submission, `<registry>:<acronym>::<submission>` | `manifest.jsonl`, `corpus.db`, `ontology_abstractness`, `abstractness_panel` |
| family | `source_id` before the last `::` (`bp:PTO::355` → `bp:PTO`): all releases of one ontology | `family` columns of the abstractness, dates and growth tables |
| `node_id` | Dependency-network node: the ontology IRI, `row:<id>` for a file without a usable IRI, `external:<iri>` for an external target | network tables, `ontology_metrics` (as `node`), `ontology_abstractness` (as `node_id`) |
| `onto` | Position 0..n-1 in `ontology_abstractness`; `child`/`parent` in `ontology_specialisations` refer to it | abstractness and growth tables |

**Which rows are "the corpus".** `corpus.db` holds every file fetched. The analysis population
is the rows with `is_latest_version = 1 AND parse_status = 'ok'`: one latest, parsed,
non-duplicate file per ontology, with copies of the same ontology in several registries counted
once. These are the corpus nodes of the dependency network and the rows of the per-file
tables. The abstractness and growth tables further leave out SKOS-dominated sources
(`kind` `thesaurus` or `mostly_skos`), which have no class hierarchy to measure.

`registry` and `home_registry` are the registry an ontology *belongs to* (OBO for an OBO PURL,
otherwise the first registry that holds a copy); `source_registry` is only where the kept copy
was downloaded from.

## The dependency network

Built in the [quick start](#quick-start-the-ontology-network-in-memory).

**Nodes.** Corpus ontologies have `is_external = False`, the counts `triple_count`,
`class_count`, `property_count`, `individual_count`, the JSON lists `own_namespaces`,
`referenced_namespaces`, `imports`, and `file_hash` / `content_hash` to join on. External nodes
(`is_external = True`) are targets that resolve to no corpus ontology; `external_kind` says what
they are where that is clear (`language` for RDF, RDFS, OWL, XSD and the like).

**Edges.** `import` edges come from `owl:imports`; `resolution` records how the target was
matched. `usage` edges mean the source uses terms from a namespace the target owns, whether or
not it imports it. A usage edge carries `reference_kind` (`domain_vocabulary`, `language`,
`metadata_or_identifier`, `malformed_or_local`) and term counts by how the terms are used:
`n_logical_terms` (in axioms: subclass, domain and range, restrictions, typing),
`n_annotation_terms`, `n_declared_terms` (null where a file was too large to re-read).
`reference_iris` lists the raw IRIs or namespaces behind the edge.

Common views:

```python
# Undeclared reuse: an ontology uses another's terms without importing it.
undeclared = [
    (u, v) for u, v, k in G.edges(keys=True)
    if k == "usage" and not G.has_edge(u, v, key="import")
]

# Logical reuse: imports, plus usage of domain vocabulary inside axioms
# (drops metadata-only reuse such as dcterms:creator on the header).
logical = nx.DiGraph()
for u, v, k, d in G.edges(keys=True, data=True):
    if k == "import" or (
        d.get("reference_kind") == "domain_vocabulary" and (d.get("n_logical_terms") or 0) > 0
    ):
        logical.add_edge(u, v)

# Most reused corpus ontologies.
sorted(logical.subgraph(corpus).in_degree, key=lambda x: -x[1])[:10]
```

Attach any per-file table through `file_hash`:

```python
metrics = pd.read_parquet("ontometer-corpus/data/structural_metrics.parquet").set_index("file_hash")
for n in corpus:
    fh = G.nodes[n]["file_hash"]
    if fh in metrics.index:
        G.nodes[n]["max_depth"] = metrics.at[fh, "max_depth_long"]
```

or read `ontology_metrics.parquet`, which already joins them per node. OntoMeter's
`ontometer.corpus.ontology_metrics.load_views` builds the network as those metrics are measured
on it: SKOS-dominated sources removed, a full and a logical-reuse view, every edge weighted by
how rare its target is (`idf_weight`).

## The specialisation network, and G(t)

A second, finer network: ontology A → ontology B when A places one of its own classes under a
class that B owns. It is built from term-level hierarchies, so it says *who builds on whom*,
where the dependency network says *who uses whom*. It may contain cycles (two ontologies
specialising each other's classes).

```python
onto = pd.read_parquet("ontometer-corpus/data/ontology_abstractness.parquet")
spec = pd.read_parquet("ontometer-corpus/data/ontology_specialisations.parquet")

S = nx.DiGraph()
S.add_nodes_from(onto.set_index("onto")[["family", "registry", "A_local"]].to_dict("index").items())
S.add_weighted_edges_from(spec[["child", "parent", "n_terms"]].itertuples(index=False), weight="n_terms")
```

Nodes are `onto` indices (join `family`, `node_id` or `source_id` from `onto` for names).

**Over time.** With `ontology_dates.parquet`, these two tables are enough to rebuild G(t), the
specialisation network as it stood at any date, without the raw files. Each ontology is placed
at its first-evidence birth and each pair at its first observed assertion (or, when that is
unknown, the child's birth, and never before the parent's). OntoMeter does the placing:

```python
import numpy as np
import pandas as pd
from ontometer.corpus.growth_network import load_tracked
from ontometer.corpus.paths import CorpusPaths

net = load_tracked(CorpusPaths.resolve("ontometer-corpus/data"))
t = (pd.Timestamp("2015-01-01") - pd.Timestamp("1970-01-01")).days   # times are days since 1970

parent = net.dep.parent.to_numpy()
arrival = np.maximum(net.eps, net.b_bnd[parent])
alive = net.b_bnd <= t                       # ontologies that existed at t (undated: NaN, never)
G_t = nx.DiGraph()
G_t.add_nodes_from(np.flatnonzero(alive))
G_t.add_edges_from(net.dep.loc[arrival <= t, ["child", "parent"]].itertuples(index=False))
```

Dated coverage is incomplete and uneven across registries (an undated ontology has a `NaN`
birth); report it alongside any result over time. The interactive version is the
[abstractness race](https://growgraph.github.io/OntoMeter/corpus/growth/).

## The registry database, `corpus.db`

Open it read-only with any SQLite client:

```python
import sqlite3

con = sqlite3.connect("file:ontometer-corpus/data/corpus.db?mode=ro", uri=True)
latest = pd.read_sql_query(
    """SELECT source_id, home_registry, ontology_iri, file_hash, kind,
              triple_count, class_count, property_count, licence
       FROM corpus_registry
       WHERE is_latest_version = 1 AND parse_status = 'ok'""",
    con,
)
```

`corpus_registry`, one row per fetched file:

| Columns | Meaning |
|---|---|
| `source_id`, `source_registry`, `home_registry`, `canonical_url`, `retrieval_date`, `source_version`, `source_version_date` | where and when it was fetched |
| `source_registry_metadata` | the registry's entry (JSON, allowlisted fields) |
| `retrieved_file`, `file_hash` | the raw file in the object store |
| `parse_status`, `parse_error`, `parse_format` | `ok`, `fail` or `unsupported`, and the format that parsed |
| `ontology_iri`, `ontology_version_iri`, `ontology_version_info`, `iri_source`, `shared_iri` | identity declared inside the file; `shared_iri = 1` when distinct ontologies claim one IRI |
| `triple_count`, `class_count`, `property_count`, `individual_count`, `declared_individual_count`, `skos_concept_count`, `kind` | size; `kind` is `no_skos`, `mostly_owl`, `mostly_skos` or `thesaurus` |
| `imports`, `own_namespaces`, `referenced_namespaces`, `licence` | JSON lists |
| `content_hash`, `is_duplicate`, `canonical_entry_id`, `is_latest_version` | dedup: a duplicate points at the row that replaced it |

`fetch_attempts` lists every fetch, `ok` or `fail` with its `error`.

## The raw files

The ontology files themselves live in an **object store**, outside this repository: each file
named by the SHA-256 of its bytes, `objects/<first two hex>/<file_hash><ext>`, the path recorded
as `retrieved_file`. `objects.tsv` lists every file this corpus version refers to, so a store can
be checked against it.

- The files whose declared licence allows redistribution (`redistribution.yaml`) are published
  as the Hugging Face dataset
  [`growgraph/ontometer-corpus`](https://huggingface.co/datasets/growgraph/ontometer-corpus).
- The others are fetched again from their `canonical_url`, and kept only if the bytes still
  match `file_hash`.

OntoMeter does both (`run/store.py pull`, `fetch-withheld`, `verify`); see
[Object store](https://growgraph.github.io/OntoMeter/corpus/store/).

The tables here are enough to study the networks and metrics; the raw files are needed only to
recompute them or to go down to individual terms.

## Working with OntoMeter

```bash
pip install "ontometer[corpus]"          # the loaders
git clone https://github.com/growgraph/OntoMeter   # the pipeline (run/ scripts)
```

OntoMeter reads this directory as its *corpus directory*, passed as `--corpus` or
`ONTOMETER_CORPUS`, and the object store as `--store` or `ONTOMETER_STORE`:

```bash
export ONTOMETER_CORPUS=/path/to/ontometer-corpus/data
export ONTOMETER_STORE=/path/to/ontometer-store
run/rebuild.sh --out /tmp/figures          # every derived table, from parse to G(t); never fetches
```

| Python | Returns |
|---|---|
| `ontometer.corpus.network.construction.load_network(dir)` | the dependency network, `networkx.MultiDiGraph` |
| `ontometer.corpus.ontology_metrics.load_views(paths)` | the network as the metrics see it (full and logical-reuse views) |
| `ontometer.corpus.growth_network.load_tracked(paths)` | the specialisation network placed in time |
| `ontometer.corpus.paths.CorpusPaths.resolve(dir, store)` | `paths` for the calls above |

The pipeline, stage by stage, and what can and cannot be reproduced:
[Corpus](https://growgraph.github.io/OntoMeter/corpus/).

**This is a snapshot.** Registries change, so a new crawl gives a different corpus, not this
one. That is why these tables are versioned rather than regenerated: `manifest.jsonl` records
exactly what was captured, and when.

## Versions, citing, licence

A corpus version is a commit of this repository; releases are tags (`git tag`). Name the
commit or tag when you report results, and the OntoMeter version that read it.

Authors: Alexander Belikov (GrowGraph) and David Bareham (CESDIP, CY Cergy Paris Université).

### Licence

The tables in `data/` are © 2025–2026 Alexander Belikov and David Bareham, released under the
[Creative Commons Attribution 4.0 International licence](LICENSE) (CC BY 4.0). You may share
and adapt them for any purpose if you credit the corpus: cite it as in
[CITATION.cff](CITATION.cff) and name the commit or tag you used.

**Registry metadata.** `manifest.jsonl` and `corpus.db` (`source_registry_metadata`) also carry
text the registries publish about each ontology: names, acronyms, descriptions, notes and
titles. That text keeps its registry's terms, so credit the registry when you reuse it:

| Registry | Terms for its metadata |
|---|---|
| OBO Foundry, OKG | CC0 (OKG's text comes from Wikidata; its `category` is OKG's own, Apache-2.0) |
| LOV | CC BY 4.0 |
| AgroPortal, TechnoPortal, OntoPortal-Astro | an open licence the portals call equivalent to CC BY |
| BioPortal | use allowed with credit to the original provider |
| BiodivPortal, MatPortal | no terms stated |
| Ontohub, LOD Cloud | only URLs are kept |

Links to each registry's terms and the citation it asks for are in
[`data/redistribution.yaml`](data/redistribution.yaml) under `registry_metadata`.

The ontology files the tables describe are not included here and keep their own licences (see
[The raw files](#the-raw-files)). The code that builds the tables,
[OntoMeter](https://github.com/growgraph/OntoMeter), is Apache-2.0.

### Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are accepted under the
[Growgraph Contributor License Agreement](CLA.md).
