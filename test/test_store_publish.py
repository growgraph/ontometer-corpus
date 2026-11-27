"""The public copy: only redistributable objects, a full index, a dataset card."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from ontometer_corpus.store.index import ObjectEntry, build_index, write_index
from ontometer_corpus.store.licence import Policy
from ontometer_corpus.store.ops import StoreError
from ontometer_corpus.store.publish import fetch_withheld, publish
from ontometer_corpus.store.remote import DirRemote

from .test_release import write_release
from .test_store_licence import _corpus

CC_BY = "http://creativecommons.org/licenses/by/4.0/"
POLICY = Policy(
    default="withhold", unrecognised="withhold", redistribute=("CC-BY*",), withhold=("CC-BY-NC*",)
)


def _mixed(tmp_path: Path):
    paths = _corpus(
        tmp_path,
        [
            {"content": b"open", "licence": [CC_BY]},
            {"content": b"closed", "licence": []},
            {"content": b"nc", "licence": ["http://creativecommons.org/licenses/by-nc/4.0/"]},
        ],
    )
    write_index(build_index(paths), paths.objects_index_path)
    _write_policy(paths.corpus_dir / "redistribution.yaml", POLICY)
    return paths


def _write_policy(path: Path, policy: Policy) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "default": policy.default,
                "unrecognised": policy.unrecognised,
                "redistribute": list(policy.redistribute),
                "withhold": list(policy.withhold),
            }
        )
    )


def _git_tag(repo: Path, name: str) -> None:
    """Commit ``data/`` as release *name* and tag it."""
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e"]
    if not (repo / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    write_release(repo / "data", name)
    subprocess.run([*git, "add", "data"], check=True)
    subprocess.run([*git, "commit", "-qm", "data"], check=True)
    subprocess.run([*git, "tag", name], check=True)


def test_publish_never_uploads_withheld(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    remote = DirRemote(tmp_path / "public")

    report = publish(paths, remote, "corpus-2026-01", POLICY)

    uploaded = {Path(p).name[:64] for p in remote.list("objects/")}
    assert uploaded == {hashlib.sha256(b"open").hexdigest()}
    assert len(report.withheld) == 2
    assert "corpus-2026-01" in remote.tags()


def test_index_lists_withheld_with_url(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    remote = DirRemote(tmp_path / "public")
    publish(paths, remote, "corpus-2026-01", POLICY)

    index = pd.read_parquet(tmp_path / "public" / "releases/corpus-2026-01/index/objects.parquet")
    assert len(index) == 3
    withheld = index[~index.redistributed]
    assert set(withheld.canonical_url.str.startswith("https://example.org/")) == {True}
    assert set(index.columns) >= {
        "file_hash",
        "path",
        "size",
        "registry",
        "source_id",
        "ontology_iri",
        "family",
        "licence_ids",
        "evidence",
        "redistributed",
        "canonical_url",
    }


def test_card_yaml_parses_and_points_at_index(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    remote = DirRemote(tmp_path / "public")
    publish(paths, remote, "corpus-2026-01", POLICY)

    card = (tmp_path / "public" / "README.md").read_text()
    header = yaml.safe_load(card.split("---")[1])
    (config,) = header["configs"]
    assert config["data_files"] == "releases/corpus-2026-01/index/objects.parquet"
    assert header["license"] == "other"
    assert (tmp_path / "public" / "releases/corpus-2026-01/tables/objects.tsv").exists()


def test_republishing_a_later_tag_keeps_earlier_release(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    remote = DirRemote(tmp_path / "public")
    publish(paths, remote, "corpus-2026-01", POLICY)
    _git_tag(tmp_path, "corpus-2026-01.1")
    publish(paths, remote, "corpus-2026-01.1", POLICY)

    header = yaml.safe_load((tmp_path / "public" / "README.md").read_text().split("---")[1])
    names = [c["config_name"] for c in header["configs"]]
    assert names == ["corpus-2026-01.1", "corpus-2026-01"]
    assert header["configs"][0].get("default") is True


class _Session:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies

    def get(self, url: str, timeout: float, stream: bool = False):
        assert stream, "downloads must stream: objects can be larger than memory"
        body = self.bodies.get(url)
        return SimpleNamespace(
            status_code=200 if body is not None else 404,
            iter_content=lambda chunk_size: [body[:3], body[3:]] if body else [],
            close=lambda: None,
        )


def test_fetch_withheld_keeps_only_hash_matches(tmp_path):
    paths = _mixed(tmp_path)
    entries = build_index(paths)
    store = paths.objects_dir.parent
    for entry in entries:
        (store / entry.path).unlink()
    session = _Session({"https://example.org/o1": b"closed", "https://example.org/o2": b"changed"})

    report = fetch_withheld(paths, entries, POLICY, session=session)

    assert [e.file_hash for e in report.fetched] == [hashlib.sha256(b"closed").hexdigest()]
    assert len(report.changed) == 1
    assert report.failed == []  # the redistributable object is pulled from the dataset, not here


def test_publish_uses_the_policy_recorded_at_the_tag(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    loosened = Policy(
        default="redistribute", unrecognised="redistribute", redistribute=("*",), withhold=()
    )
    _write_policy(paths.corpus_dir / "redistribution.yaml", loosened)
    remote = DirRemote(tmp_path / "public")

    report = publish(paths, remote, "corpus-2026-01")

    assert len(report.uploaded) == 1


def test_publish_refuses_a_policy_other_than_the_tagged_one(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    loosened = Policy(
        default="redistribute", unrecognised="redistribute", redistribute=("*",), withhold=()
    )
    with pytest.raises(StoreError):
        publish(paths, DirRemote(tmp_path / "public"), "corpus-2026-01", loosened)


def test_card_installs_the_package_and_clones_the_index(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    publish(paths, DirRemote(tmp_path / "public"), "corpus-2026-01")
    card = (tmp_path / "public" / "README.md").read_text()
    assert 'pip install "ontometer-corpus[store]"' in card
    assert "git clone" in card


def test_card_clones_the_corpus_repository_that_holds_the_index(tmp_path):
    paths = _mixed(tmp_path)
    _git_tag(tmp_path, "corpus-2026-01")
    publish(paths, DirRemote(tmp_path / "public"), "corpus-2026-01")
    card = (tmp_path / "public" / "README.md").read_text()
    assert "git clone https://github.com/growgraph/ontometer-corpus ontometer-corpus" in card
    assert "export ONTOMETER_CORPUS=$PWD/ontometer-corpus/data" in card


def test_fetch_withheld_counts_objects_missing_from_corpus_as_failed(tmp_path):
    paths = _mixed(tmp_path)
    entries = build_index(paths)
    stranger = ObjectEntry("f" * 64, 3, "objects/ff/" + "f" * 64 + ".owl")
    report = fetch_withheld(paths, [*entries, stranger], session=_Session({}))
    assert stranger in report.failed
