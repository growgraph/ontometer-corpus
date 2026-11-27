# Object store

The raw ontology files live outside the repository, in the object store
(`--store`, `ONTOMETER_STORE`). This page says how to get a store, check it, add to it,
and publish a release of it. After reading it you can work on a store that matches any tracked
version of the corpus, and cut a public release.

The commands are subcommands of `ontometer-corpus store`, installed with the `store` extra
(`pip install "ontometer-corpus[store]"`).

## The rule: version the index, not the store

Objects are named by the SHA-256 of their bytes (`objects/<hh>/<sha256><ext>`), so the store is
a set that only grows, and two copies of it never conflict. A **corpus version is a git commit
of the corpus directory** (`--corpus`, `data/` in a clone of `growgraph/ontometer-corpus`), written
`corpus/` below. `corpus/objects.tsv` lists the hash, size and path of every object that version
references. A store *satisfies* a version when every listed hash is present with the listed
size (with `--deep`, the listed hash). Presence is decided by hash, not by path.

A **remote** is a Hugging Face dataset (`hf://datasets/<org>/<name>`) or a directory with the
store's layout (`file:///path`). Remotes are add-only: no command deletes or overwrites an
object. `push` refuses a remote that holds public releases; only `publish` writes there, and
only the objects the licence policy allows.

| Remote | Holds |
|---|---|
| `growgraph/ontometer-corpus` (public) | the objects whose licence allows redistribution, one index and table set per release, the dataset card |
| a private working remote | every object; collaborators who fetch upload here |
| a cache remote | the versioned per-file caches |

## Getting a store

```bash
export ONTOMETER_STORE="$HOME/ontometer-store"
ontometer-corpus store pull --store-remote hf://datasets/growgraph/ontometer-corpus \
    [--rev corpus-YYYY-MM]                               # objects the index lists and the store lacks
ontometer-corpus store fetch-withheld                # the rest, from their sources
ontometer-corpus store verify --deep                 # exit 1 if anything is missing or wrong
```

Collaborators with access to the private working remote, which holds every object, set
`ONTOMETER_STORE_REMOTE` to it instead and need no `fetch-withheld`.

Downloads land in `_store_staging/` beside the store and are moved into `objects/` only once
their hash matches, so an interrupted pull leaves nothing half-written; run it again to resume.
`fetch-withheld` keeps a file only if its source still serves the same bytes.

## Adding to the corpus

With `ONTOMETER_STORE_REMOTE` set to the private working remote:

1. `pull` and `verify`: start from a store that satisfies `main`.
2. Fetch and process (the [pipeline](pipeline.md)). New objects land in the store.
3. `ontometer-corpus store index` writes `corpus/objects.tsv` from `corpus.db`.
4. `ontometer-corpus store push` uploads what the index lists and the remote lacks, hash-checking each
   object first; a corrupt one is named and skipped. Then `status` shows the remote holds every
   listed object. Objects go up **before** the change to `corpus/` that references them enters
   version control.
5. For a version a paper or release uses, name it in `data/release.toml` (`corpus-YYYY-MM` for a
   new crawl, `corpus-YYYY-MM.N` for a re-derivation of one), commit, tag it in git and run
   `ontometer-corpus store tag corpus-YYYY-MM`, which refuses until the remote holds the whole version
   with the right sizes and hashes, and refuses a tag whose `release.toml` names another release.

`corpus.db` is binary and cannot be combined line by line, so one person fetches per refresh.
Objects from parallel fetches combine on their own; `manifest.jsonl` is the text record to
reconcile, and re-seeding `corpus.db` from it is idempotent.

`verify --orphans-to DIR` moves objects no index lists out of the store. `gc` lists objects no
`corpus-*` tag references, locally and on the remote; it deletes nothing.

## Caches

Structural sidecars (`cache/structure/v<N>/`) and role tables (`cache/roles/v<N>/`) are per-file
caches of the parse and role passes. Their version is part of the path: bump `STRUCTURE_VERSION`
or `ROLES_VERSION` in `ontometer_corpus.paths` when the writer's output changes, and the next
run writes into an empty directory instead of reusing old output.
`push --caches` and `pull --caches` exchange the current versions with the cache remote
(`--cache-remote`, `ONTOMETER_CACHE_REMOTE`).

## Publishing a release

Each file keeps its own licence. The parser records the licence statements a file makes about
itself (`corpus_registry.licence`); `corpus/redistribution.yaml` maps licence ids to
`redistribute` or `withhold`, with defaults for files that declare nothing or something
unrecognised. A registry `viewingRestriction` other than `public` withholds a file, and so does
a capture that is not an ontology file.

```bash
ontometer-corpus store licenses --out licences.tsv [--emails]    # review before publishing
ontometer-corpus store publish --rev corpus-YYYY-MM \
    --corpus-remote hf://datasets/growgraph/ontometer-corpus
```

`publish` applies the `redistribution.yaml` recorded at the tag, the one the release carries,
and refuses a `--policy` that differs from it. It uploads the redistributable objects, writes
`releases/<tag>/index/objects.parquet` (every object, with licence, verdict and source URL) and
`releases/<tag>/tables/`, rewrites the dataset card, and tags the dataset.

For an archived, citable copy:

```bash
ontometer-corpus store bundle --rev corpus-YYYY-MM --public --out bundles/
ontometer-corpus store zenodo --rev corpus-YYYY-MM --out bundles/ [--sandbox]
# a later version adds a delta to the same record:
ontometer-corpus store zenodo --rev corpus-YYYY-MM --base corpus-YYYY-MM \
    --record <id> --out bundles/
```

Bundles are deterministic `tar.zst` archives, readable with `tar --zstd -xf`, with a
`sha256sum -c` list beside them. `zenodo` creates a draft and never publishes it: review the
draft in the Zenodo web interface and publish it there.

## Commands

| Command | Does |
|---|---|
| `index` | Write `corpus/objects.tsv` from `corpus.db` and the store |
| `verify [--deep] [--rev] [--orphans-to]` | Check the store against an index |
| `status [--rev]` | What the remote and the store lack; exit 1 if anything |
| `push [--caches] [--limit]` | Upload what the remote lacks |
| `pull [--rev] [--caches] [--limit]` | Download what the store lacks |
| `tag NAME` | Tag the remote once it holds git tag `NAME`'s objects |
| `gc` | List objects no `corpus-*` tag references |
| `licenses [--out] [--emails]` | Redistribution verdict per object |
| `publish --rev` | Public release of a tagged version |
| `fetch-withheld [--rev]` | Withheld objects from their sources |
| `bundle --rev [--base] [--public]` | Solid `tar.zst` of a version or a delta |
| `zenodo --rev [--base --record] [--sandbox]` | Zenodo draft with the public bundle |
