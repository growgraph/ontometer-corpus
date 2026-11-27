"""The public copy of a tagged corpus version, and recovery of withheld files.

``publish`` uploads only the objects the redistribution policy allows. Every
object of the version stays listed in ``releases/<tag>/index/objects.parquet``
with its licence verdict and source URL, so the public copy is a complete
record even where it cannot carry the file. Release files are add-only; the
dataset card (``README.md``) is the one file a release replaces.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.release import RELEASE_FILE, release_at_tag
from ontometer_corpus.store.index import ObjectEntry, index_at_rev, local_objects
from ontometer_corpus.store.licence import Policy, Verdict, load_policy, verdicts
from ontometer_corpus.store.ops import StoreError, move_into_place, push
from ontometer_corpus.store.remote import Remote
from ontometer_corpus.versions import family_key

#: Tracked files a release carries beside the index, besides every Parquet table.
_RELEASE_FILES = (
    "corpus.db",
    "objects.tsv",
    "manifest.jsonl",
    "redistribution.yaml",
    RELEASE_FILE,
)


@dataclass(frozen=True, slots=True)
class PublishReport:
    uploaded: list[ObjectEntry]
    withheld: list[ObjectEntry]


@dataclass(frozen=True, slots=True)
class FetchReport:
    fetched: list[ObjectEntry] = field(default_factory=list)
    changed: list[ObjectEntry] = field(default_factory=list)
    failed: list[ObjectEntry] = field(default_factory=list)


def data_at_rev(paths: CorpusPaths, rev: str, dest: Path) -> CorpusPaths:
    """Copy the tracked data files of git revision *rev* into *dest*."""
    corpus_dir = paths.corpus_dir.resolve()
    top = subprocess.run(
        ["git", "-C", str(corpus_dir), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rel_dir = corpus_dir.relative_to(Path(top).resolve()).as_posix()
    listing = subprocess.run(
        ["git", "-C", top, "ls-tree", "--name-only", rev, f"{rel_dir}/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    dest.mkdir(parents=True, exist_ok=True)
    for name in listing:
        base = name.rsplit("/", 1)[-1]
        if base in _RELEASE_FILES or base.endswith(".parquet"):
            blob = subprocess.run(
                ["git", "-C", top, "show", f"{rev}:{name}"], capture_output=True, check=True
            ).stdout
            (dest / base).write_bytes(blob)
    return CorpusPaths(corpus_dir=dest, store_dir=paths.store_dir)


def _first_rows(paths: CorpusPaths) -> dict[str, dict[str, Any]]:
    """Registry facts of the first corpus row referencing each object."""
    conn = sqlite3.connect(f"file:{paths.db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT file_hash, source_registry, source_id, ontology_iri, canonical_url "
            "FROM corpus_registry WHERE file_hash IS NOT NULL AND file_hash != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    first: dict[str, dict[str, Any]] = {}
    for digest, registry, source_id, ontology_iri, url in rows:
        first.setdefault(
            digest,
            {
                "registry": registry,
                "source_id": source_id,
                "ontology_iri": ontology_iri,
                "family": family_key(source_id or ""),
                "canonical_url": url,
            },
        )
    return first


def _index_frame(
    paths: CorpusPaths, entries: list[ObjectEntry], by_hash: dict[str, Verdict]
) -> pd.DataFrame:
    facts = _first_rows(paths)
    records = []
    for entry in entries:
        verdict = by_hash.get(entry.file_hash)
        records.append(
            {
                "file_hash": entry.file_hash,
                "path": entry.path,
                "size": entry.size,
                **facts.get(entry.file_hash, {}),
                "licence_ids": list(verdict.licence_ids) if verdict else [],
                "evidence": verdict.evidence if verdict else "none",
                "redistributed": bool(verdict and verdict.redistribute),
                "reason": verdict.reason if verdict else "not in corpus.db",
            }
        )
    return pd.DataFrame.from_records(records)


def dataset_card(current: str, releases: list[str], frame: pd.DataFrame) -> str:
    """The Hugging Face dataset card for a release, listing every release as a config."""
    configs = [
        {
            "config_name": name,
            "data_files": f"releases/{name}/index/objects.parquet",
            **({"default": True} if name == current else {}),
        }
        for name in releases
    ]
    header = yaml.safe_dump(
        {
            "license": "other",
            "license_name": "per-file",
            "license_link": f"releases/{current}/tables/redistribution.yaml",
            "pretty_name": "OntoMeter ontology corpus",
            "tags": ["ontology", "rdf", "owl", "skos", "semantic-web"],
            "configs": configs,
        },
        sort_keys=False,
    ).rstrip()
    published = int(frame["redistributed"].sum())
    return f"""---
{header}
---

# OntoMeter ontology corpus

Ontology files collected from public registries, kept byte for byte and named by the SHA-256
of their content: `objects/<hh>/<sha256><ext>`. Each release is a tagged revision of this
repository; its index lists every object of that corpus version.

Release `{current}`: {len(frame)} objects, {published} included here, {len(frame) - published}
withheld.

## Files

- `objects/` — the ontology files this dataset may redistribute.
- `releases/<tag>/index/objects.parquet` — every object of the release: hash, path, size,
  registry, source id, ontology IRI, release family, declared licences, whether it is
  included, why, and the URL it was fetched from.
- `releases/<tag>/tables/` — the corpus database and the per-ontology tables of the release.

## Licences

The tables under `releases/<tag>/tables/` are released under CC BY 4.0; the registry metadata
text they carry (names, descriptions, notes) keeps its registry's terms. Each ontology file
under `objects/` keeps the licence its authors gave it, so there is no single licence for the
dataset. A file is included when the licence it declares (or, failing that, its registry's licence
field) is on the redistribution list in `releases/<tag>/tables/redistribution.yaml`. Files
that declare no licence, declare a restrictive one, or are not ontology files are withheld:
they stay in the index with their source URL and SHA-256, so anyone can fetch them from the
source and check they have the same bytes.

## Getting the files

The `ontometer-corpus store` command downloads a release and checks it. It reads the
release's index from the corpus repository, which can be cloned anywhere:

```bash
pip install "ontometer-corpus[store]"
git clone https://github.com/growgraph/ontometer-corpus ontometer-corpus
export ONTOMETER_CORPUS=$PWD/ontometer-corpus/data
export ONTOMETER_STORE=./ontometer-store
ontometer-corpus store pull --store-remote hf://datasets/<this repository> --rev {current}
ontometer-corpus store fetch-withheld --rev {current}   # what the sources still serve unchanged
ontometer-corpus store verify --deep --rev {current}
```
"""


def _tagged_policy(at_rev: CorpusPaths, policy: Policy | None) -> Policy:
    """The policy the revision records, which its release publishes beside the files."""
    recorded = at_rev.corpus_dir / "redistribution.yaml"
    if not recorded.exists():
        raise StoreError("the revision has no corpus/redistribution.yaml to publish under")
    tagged = load_policy(recorded)
    if policy is not None and policy != tagged:
        raise StoreError(
            "the given policy differs from the revision's corpus/redistribution.yaml, "
            "which the release publishes; tag a revision with the policy you mean"
        )
    return tagged


def publish(
    paths: CorpusPaths, remote: Remote, rev: str, policy: Policy | None = None
) -> PublishReport:
    """Publish git tag *rev* to *remote*: allowed objects, index, tables, card, tag.

    The policy is the one recorded at *rev*; a different *policy* is refused.

    Raises:
        ReleaseError: the tag's ``release.toml`` is missing or names another release.
        StoreError: a redistributable object is missing locally or corrupt, or the
            policy does not match the revision's.
    """
    release_at_tag(paths.corpus_dir, rev)
    entries = index_at_rev(paths.corpus_dir, rev)
    with tempfile.TemporaryDirectory() as tmp:
        at_rev = data_at_rev(paths, rev, Path(tmp) / "data")
        policy = _tagged_policy(at_rev, policy)
        by_hash = {v.file_hash: v for v in verdicts(at_rev, policy)}
        public = [
            e for e in entries if e.file_hash in by_hash and by_hash[e.file_hash].redistribute
        ]
        public_hashes = {e.file_hash for e in public}
        withheld = [e for e in entries if e.file_hash not in public_hashes]
        assert public_hashes.isdisjoint(e.file_hash for e in withheld)

        pushed = push(paths, remote, public, allow_public=True)
        if pushed.corrupt or pushed.missing_locally:
            raise StoreError(
                f"{len(pushed.corrupt)} corrupt and {len(pushed.missing_locally)} missing "
                "redistributable objects; run `store pull` and `store verify --deep`"
            )
        frame = _index_frame(at_rev, entries, by_hash)
        index_file = Path(tmp) / "objects.parquet"
        frame.to_parquet(index_file, index=False)
        prefix = f"releases/{rev}/"
        files = {prefix + "index/objects.parquet": index_file}
        files |= {prefix + "tables/" + f.name: f for f in sorted(at_rev.corpus_dir.iterdir())}
        remote.put(files, f"Release {rev}")

        earlier = {p.split("/")[1] for p in remote.list("releases/")} - {rev}
        card = dataset_card(rev, [rev, *sorted(earlier, reverse=True)], frame)
        remote.write("README.md", card.encode(), f"Dataset card for {rev}")
    remote.tag(rev)
    return PublishReport(uploaded=public, withheld=withheld)


def fetch_withheld(
    paths: CorpusPaths,
    entries: list[ObjectEntry],
    policy: Policy | None = None,
    *,
    session: Any = None,
    rev: str | None = None,
) -> FetchReport:
    """Download withheld objects from their source URLs, keeping only identical bytes.

    Verdicts and URLs come from the corpus at *rev* (the working tree when None),
    under its recorded policy unless *policy* is given. Downloads stream to the
    staging directory and are hashed on the way.
    """
    if session is None:
        import requests

        session = requests.Session()
    with contextlib.ExitStack() as stack:
        source = paths
        if rev is not None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            source = data_at_rev(paths, rev, Path(tmp) / "data")
        policy = policy or load_policy(source.corpus_dir / "redistribution.yaml")
        by_hash = {v.file_hash: v for v in verdicts(source, policy)}
        urls = {h: facts["canonical_url"] for h, facts in _first_rows(source).items()}
    local = local_objects(paths)
    store = paths.require_store_dir()
    report = FetchReport()
    for entry in entries:
        verdict = by_hash.get(entry.file_hash)
        if entry.file_hash in local or (verdict is not None and verdict.redistribute):
            continue
        url = urls.get(entry.file_hash)
        if verdict is None or not url:
            report.failed.append(entry)
            continue
        staged = paths.staging_dir / entry.path
        try:
            response = session.get(url, timeout=120, stream=True)
            if response.status_code != 200:
                report.failed.append(entry)
                continue
            staged.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with staged.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
            response.close()
        except Exception:  # network errors are reported per file, not raised
            staged.unlink(missing_ok=True)
            report.failed.append(entry)
            continue
        if digest.hexdigest() != entry.file_hash:
            staged.unlink()
            report.changed.append(entry)
            continue
        move_into_place(staged, store / entry.path)
        report.fetched.append(entry)
    return report
