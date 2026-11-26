# Contributing

This page sets up a working copy, runs its checks, builds this site, and says how a release is
cut.

## Licensing your contribution

Code is released under the Apache License 2.0 and the tables under CC BY 4.0. Before your first
pull request is merged, you accept the
[Growgraph Contributor License Agreement](https://github.com/growgraph/ontometer-corpus/blob/main/CLA.md)
(CLA) once, by replying on the pull request:

```
I have read the Growgraph CLA and I accept it.
```

The `cla` check then turns green. Every commit must be authored with an e-mail address linked
to your GitHub account, so the check can match it to your acceptance. Contributions written at
work need your employer's permission, or a Corporate CLA (write to team@growgraph.dev). Do not
submit code you did not write without saying where it comes from and under which licence.

## Set up a working copy

The repository uses [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/growgraph/ontometer-corpus.git
cd ontometer-corpus
uv sync --group dev --all-extras
uv run pre-commit install
```

## Run the checks

```bash
uv run pytest
uv run pre-commit run --all-files
```

Pre-commit runs `ruff` (check and format), `ty` over the package and the tests, and the file
hygiene hooks. CI runs the same hooks and `pytest` on every pull request and every push to
`main`.

## Build the documentation

```bash
uv sync --group dev --all-extras
uv run properdocs serve        # http://127.0.0.1:8000
uv run properdocs build --strict
```

The API reference is generated from docstrings at build time; to document a module, write its
docstring. The G(t) explorer's data is built from the tables in `data/` (or
`ONTOMETER_CORPUS`). CI builds with `--strict`, so a broken link or a missing anchor fails the
build.

## The tables

The tables are tracked in git on purpose: they cannot be fetched into existence again, because
the registries are live services and a crawl run today produces a different corpus. Do not
regenerate them as part of an unrelated change. [Rebuilding](rebuild.md) says what a rebuild
does and does not reproduce.

## Releases

Two kinds of tag share this repository:

- **package releases**: a GitHub Release whose tag is `v` followed by `version` in
  `pyproject.toml`; the publish workflow uploads it to PyPI and refuses a tag that does not
  match. Record the change under `[Unreleased]` in `CHANGELOG.md` with your pull request.
- **table releases**: `corpus-YYYY-MM` tags, archived on Zenodo; they publish nothing to PyPI.
