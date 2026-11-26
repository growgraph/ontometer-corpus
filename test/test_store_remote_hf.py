"""The Hugging Face remote, against an in-memory stand-in for ``HfApi`` (no network)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from huggingface_hub.hf_api import GitRefInfo, RepoFile

from ontometer_corpus.store import remote as remote_module
from ontometer_corpus.store.index import build_index, verify
from ontometer_corpus.store.ops import pull, push, status
from ontometer_corpus.store.remote import DirRemote, HfRemote, open_remote

from .test_store_index import make_corpus


class _HttpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.response = SimpleNamespace(status_code=code)


class FakeHfApi:
    """Files, commits and tags of one dataset repository, held in memory."""

    def __init__(self, fail_downloads: int = 0) -> None:
        self.files: dict[str, bytes] = {}
        self.commits: list[list[str]] = []
        self.tag_names: list[str] = []
        self.fail_downloads = fail_downloads
        self.snapshots: dict[str, dict[str, bytes]] = {}

    def list_repo_tree(
        self, repo_id, path_in_repo=None, *, recursive=False, revision=None, repo_type=None, **_
    ):
        files = self.snapshots[revision] if revision else self.files
        prefix = (path_in_repo or "").rstrip("/")
        for path, data in sorted(files.items()):
            if prefix and not path.startswith(prefix + "/"):
                continue
            lfs = None
            if path.startswith("objects/"):
                lfs = {"size": len(data), "oid": hashlib.sha256(data).hexdigest(), "pointerSize": 1}
            yield RepoFile(path=path, size=len(data), oid="git", lfs=lfs)

    def create_commit(self, repo_id, operations, *, commit_message, repo_type=None, **_):
        ops = list(operations)
        assert len(ops) <= 100, "a commit must stay under the per-commit operation limit"
        for op in ops:
            source = op.path_or_fileobj
            self.files[op.path_in_repo] = (
                source if isinstance(source, bytes) else Path(source).read_bytes()
            )
        self.commits.append([op.path_in_repo for op in ops])

    def hf_hub_download(
        self, repo_id, filename, *, repo_type=None, revision=None, local_dir=None, **_
    ):
        if self.fail_downloads:
            self.fail_downloads -= 1
            raise _HttpError(429)
        assert local_dir is not None, (
            "downloads must go to an explicit directory, never the hub cache"
        )
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[filename])
        meta = Path(local_dir) / ".cache" / "huggingface" / "download" / f"{filename}.metadata"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text("etag")
        return str(target)

    def create_tag(self, repo_id, *, tag, repo_type=None, **_):
        self.tag_names.append(tag)
        self.snapshots[tag] = dict(self.files)

    def list_repo_refs(self, repo_id, *, repo_type=None, **_):
        return SimpleNamespace(
            tags=[
                GitRefInfo(name=t, ref=f"refs/tags/{t}", target_commit="c") for t in self.tag_names
            ]
        )


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(remote_module.time, "sleep", lambda _: None)


def _backends(tmp_path: Path):
    return {
        "dir": DirRemote(tmp_path / "remote"),
        "hf": HfRemote("growgraph/test", api=FakeHfApi()),
    }


@pytest.mark.parametrize("kind", ["dir", "hf"])
def test_remote_contract_round_trip(tmp_path, kind):
    remote = _backends(tmp_path)[kind]
    local = tmp_path / "a.owl"
    local.write_bytes(b"content")
    remote.put({"objects/aa/a.owl": local}, "add")
    remote.put({"objects/aa/a.owl": local}, "again")

    listed = remote.list("objects/")
    assert list(listed) == ["objects/aa/a.owl"]
    assert listed["objects/aa/a.owl"].size == 7

    staging = tmp_path / "staging"
    remote.fetch(["objects/aa/a.owl"], staging)
    assert (staging / "objects/aa/a.owl").read_bytes() == b"content"

    other = tmp_path / "b.owl"
    other.write_bytes(b"different")
    remote.put({"objects/aa/a.owl": other}, "overwrite attempt")
    remote.fetch(["objects/aa/a.owl"], tmp_path / "staging2")
    assert (tmp_path / "staging2/objects/aa/a.owl").read_bytes() == b"content"

    remote.tag("corpus-x")
    assert "corpus-x" in remote.tags()


def test_hf_put_batches_at_most_100_ops(tmp_path):
    api = FakeHfApi()
    remote = HfRemote("growgraph/test", api=api)
    files = {}
    for i in range(250):
        f = tmp_path / f"f{i}"
        f.write_bytes(str(i).encode())
        files[f"objects/{i:02x}/f{i}"] = f
    remote.put(files, "many")
    assert all(len(c) <= 100 for c in api.commits)
    assert sum(len(c) for c in api.commits) == 250 + 1  # plus .gitattributes


def test_hf_put_tracks_objects_through_lfs(tmp_path):
    api = FakeHfApi()
    remote = HfRemote("growgraph/test", api=api)
    f = tmp_path / "x"
    f.write_bytes(b"x")
    remote.put({"objects/aa/x": f}, "one")
    assert b"objects/** filter=lfs" in api.files[".gitattributes"]
    remote.put({"objects/aa/y": f}, "two")
    assert sum(c.count(".gitattributes") for c in api.commits) == 1


def test_hf_fetch_keeps_hub_metadata_out_of_store(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = HfRemote("growgraph/test", api=FakeHfApi())
    push(paths, remote, entries)
    other = paths.__class__(corpus_dir=paths.corpus_dir, store_dir=tmp_path / "other")
    pull(other, remote, entries)
    assert verify(other, entries, deep=True).ok()
    assert not list((tmp_path / "other" / "objects").rglob(".cache"))


def test_hf_fetch_retries_on_429(tmp_path):
    api = FakeHfApi()
    remote = HfRemote("growgraph/test", api=api)
    f = tmp_path / "x"
    f.write_bytes(b"x")
    remote.put({"objects/aa/x": f}, "one")
    api.fail_downloads = 2
    remote.fetch(["objects/aa/x"], tmp_path / "staging")
    assert (tmp_path / "staging/objects/aa/x").read_bytes() == b"x"


def test_hf_status_uses_recorded_sha256(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    api = FakeHfApi()
    remote = HfRemote("growgraph/test", api=api)
    push(paths, remote, entries)
    victim = next(p for p in api.files if p.startswith("objects/"))
    api.files[victim] = b"z" * len(api.files[victim])
    assert len(status(paths, remote, entries).remote_bad) == 1


def test_open_remote_parses_hf_urls():
    remote = open_remote("hf://datasets/growgraph/ontometer-store", api=FakeHfApi())
    assert isinstance(remote, HfRemote)
    assert remote.repo_id == "growgraph/ontometer-store"
    with pytest.raises(ValueError):
        open_remote("hf://models/growgraph/x")
