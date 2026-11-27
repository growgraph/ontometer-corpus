# Changelog

All notable changes to the `ontometer-corpus` package are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[Semantic Versioning](https://semver.org/). Releases of the tables are tagged
`corpus-YYYY-MM[.N]` and are not listed here; each package release names the table format it
reads.

## [Unreleased]

## [0.1.0]

Reads table format 1 (`corpus-2026-08.1` and later).

### Added

- First release of the package: the namespace rules and path resolution (base install), table
  loaders (`tables`), the derivation pipeline (`build`), object-store exchange (`store`), the
  abstractness validation step (`validation`) and the resource paper's analysis (`paper`).
- One command, `ontometer-corpus`, with a subcommand per derivation step, `rebuild` for the whole
  chain, `store` for the object store and `paper` for the paper's figures, tables and numbers.
- `data/release.toml` names a table release and its format; the table loaders refuse a corpus
  directory of another format, and `store tag` / `store publish` refuse a tag whose
  `release.toml` names another release.

### Removed

- The OQuaRE table (`oquare_metrics.parquet`) and the `oq_*` columns of `ontology_metrics.parquet`
  are no longer derived; the `corpus-2026-08` tables keep them.
