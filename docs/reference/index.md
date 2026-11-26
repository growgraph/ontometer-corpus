# API reference

The per-module pages in this section are generated at build time from the docstrings in the
source tree: `docs/_build/gen_pages.py` walks `ontometer_corpus/**/*.py` and renders one page
per module. To document a module, write its docstrings.

This page documents the top-level package:

::: ontometer_corpus
    options:
      members: false

## Base install

- [`namespaces`](namespaces.md): which namespace an IRI belongs to, and which no ontology can
  own.
- [`paths`](paths.md): the corpus directory and the object store, and every path derived from
  them.
- [`cli`](cli.md): the `ontometer-corpus` command.

## Tables (`tables`)

- [`network`](network.md): the dependency network
  ([`construction`](network/construction.md)), term ownership and edge authority
  ([`entities`](network/entities.md)), redeclared labels
  ([`redeclarations`](network/redeclarations.md)) and the Poincaré layout
  ([`hyperbolic`](network/hyperbolic.md)).
- [`ontology_metrics`](ontology_metrics.md): the per-ontology metric table and the network
  views it is measured on.
- [`growth_network`](growth_network.md): G(t) rebuilt from the tracked tables, and the
  explorer's payload.
- [`dating`](dating.md): first-evidence dates and bounded births.
- [`metrics`](metrics.md): term and ontology abstractness
  ([`abstractness`](metrics/abstractness.md)) and the growth statistics
  ([`growth`](metrics/growth.md)).

## Derivation (`build`)

- [`fetchers`](fetchers.md): one module per registry family
  ([`ontoportal`](fetchers/ontoportal.md), [`obo`](fetchers/obo.md),
  [`ontohub`](fetchers/ontohub.md), [`okg`](fetchers/okg.md), [`lov`](fetchers/lov.md)).
- [`pipeline`](pipeline.md): the corpus database ([`db`](pipeline/db.md)), parsing
  ([`parse`](pipeline/parse.md)) and deduplication ([`dedup`](pipeline/dedup.md)).
- [`metadata`](metadata.md), [`registries`](registries.md), [`versions`](versions.md): registry
  metadata projected to an allowlist, home registries, release families.
- [`roles`](roles.md) and [`usage`](usage.md): the roles a term plays in a file, and how a
  vocabulary is used.
- [`supervisor`](supervisor.md): one process per task under a wall-clock budget.
- [`steps`](steps.md): one command per derivation step; [`rebuild`](rebuild.md) runs them in
  order.
- [`store`](store.md): the object store's index, remotes and published copies (`store`).

## The paper (`paper`)

- [`paper`](paper.md): the figures and numbers ([`figures`](paper/figures.md)), the shared
  style ([`figstyle`](paper/figstyle.md)), small statistics ([`stats`](paper/stats.md)), the
  deduplication audit ([`dedup_audit`](paper/dedup_audit.md)), the comparator networks
  ([`comparators`](paper/comparators.md)) and the GitHub sample
  ([`github_ontologies`](paper/github_ontologies.md)).
