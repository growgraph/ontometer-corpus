"""Store operations against a directory remote: status, push, pull, tag, gc."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.store import cli as store_cli
from ontometer_corpus.store.cli import cli
from ontometer_corpus.store.index import build_index, local_objects, verify, write_index
from ontometer_corpus.store.ops import (
    StoreError,
    gc,
    pull,
    pull_caches,
    push,
    push_caches,
    status,
    tag,
)
from ontometer_corpus.store.remote import DirRemote, RemoteFile, open_remote

from .test_release import write_release
from .test_store_index import make_corpus


def _empty_store(tmp_path: Path, paths: CorpusPaths, name: str = "other") -> CorpusPaths:
    """A second checkout: same corpus dir, empty store."""
    return CorpusPaths(corpus_dir=paths.corpus_dir, store_dir=tmp_path / name)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        capture_output=True,
    )


def _commit_index(tmp_path: Path, paths: CorpusPaths, tag_name: str | None = None) -> None:
    if not (tmp_path / ".git").exists():
        _git(tmp_path, "init", "-q")
    write_index(build_index(paths), paths.objects_index_path)
    if tag_name:
        write_release(paths.corpus_dir, tag_name)
    _git(tmp_path, "add", "data")
    _git(tmp_path, "commit", "-qm", "index")
    if tag_name:
        _git(tmp_path, "tag", tag_name)


def test_push_then_pull_into_empty_store_verifies(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")

    assert len(push(paths, remote, entries).uploaded) == len(entries)
    other = _empty_store(tmp_path, paths)
    report = pull(other, remote, entries)

    assert len(report.downloaded) == len(entries)
    assert verify(other, entries, deep=True).ok()
    assert not other.staging_dir.exists() or not any(other.staging_dir.rglob("*.*"))


def test_push_is_idempotent(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries)
    assert push(paths, remote, entries).uploaded == []
    assert status(paths, remote, entries).remote_missing == []


def test_put_never_overwrites_existing_path(tmp_path):
    remote = DirRemote(tmp_path / "remote")
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    remote.put({"objects/aa/x": first}, "one")
    remote.put({"objects/aa/x": second}, "two")
    assert (tmp_path / "remote" / "objects" / "aa" / "x").read_bytes() == b"first"


def test_push_skips_and_reports_corrupt_local_object(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    bad = paths.objects_dir.parent / entries[0].path
    bad.write_bytes(b"x" * entries[0].size)
    remote = DirRemote(tmp_path / "remote")

    report = push(paths, remote, entries)

    assert [e.file_hash for e in report.corrupt] == [entries[0].file_hash]
    assert len(report.uploaded) == len(entries) - 1
    assert [e.file_hash for e in status(paths, remote, entries).remote_missing] == [
        entries[0].file_hash
    ]


class _LyingRemote(DirRemote):
    """Reports a recorded sha256 that disagrees with the file name, as a damaged remote would."""

    def list(self, prefix: str, revision: str | None = None) -> dict[str, RemoteFile]:
        return {
            p: RemoteFile(f.path, f.size, "0" * 64)
            for p, f in super().list(prefix, revision).items()
        }


def test_status_flags_remote_hash_mismatch(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = _LyingRemote(tmp_path / "remote")
    push(paths, DirRemote(tmp_path / "remote"), entries)
    assert len(status(paths, remote, entries).remote_bad) == len(entries)


def test_pull_rejects_remote_hash_mismatch(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries)
    damaged = tmp_path / "remote" / entries[0].path
    damaged.write_bytes(b"y" * entries[0].size)
    other = _empty_store(tmp_path, paths)

    report = pull(other, remote, entries)

    assert [e.file_hash for e in report.rejected] == [entries[0].file_hash]
    assert entries[0].file_hash not in local_objects(other)


class _FailingRemote(DirRemote):
    """Raises after the first file of a fetch, like a dropped connection."""

    def fetch(self, rel_paths: Sequence[str], staging: Path, revision: str | None = None) -> None:
        super().fetch(rel_paths[:1], staging, revision)
        raise ConnectionError("dropped")


def test_interrupted_pull_leaves_no_partial_object(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    push(paths, DirRemote(tmp_path / "remote"), entries)
    other = _empty_store(tmp_path, paths)

    with pytest.raises(ConnectionError):
        pull(other, _FailingRemote(tmp_path / "remote"), entries)
    assert verify(other, entries, deep=True).wrong_hash == []
    assert len(local_objects(other)) < len(entries)

    pull(other, DirRemote(tmp_path / "remote"), entries)
    assert verify(other, entries, deep=True).ok()


def test_presence_by_hash_prefix_ignores_extension(tmp_path):
    paths, _ = make_corpus(tmp_path, suffixes=(".rdf",))
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries)
    stored = tmp_path / "remote" / entries[0].path
    stored.rename(stored.with_suffix(".owl"))
    assert status(paths, remote, entries).remote_missing == []


def test_pull_limit_fetches_only_that_many(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries)
    other = _empty_store(tmp_path, paths)
    assert len(pull(other, remote, entries, limit=2).downloaded) == 2
    assert len(local_objects(other)) == 2


def test_tag_refuses_incomplete_remote(tmp_path):
    paths, _ = make_corpus(tmp_path)
    _commit_index(tmp_path, paths, "corpus-2026-01")
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries[:-1])

    with pytest.raises(StoreError):
        tag(paths, remote, "corpus-2026-01")
    push(paths, remote, entries)
    tag(paths, remote, "corpus-2026-01")
    assert "corpus-2026-01" in remote.tags()


def test_pull_at_tag_revision_lists_only_tagged_files(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries[:2])
    remote.tag("corpus-a")
    push(paths, remote, entries)
    assert len(remote.list("objects/", revision="corpus-a")) == 2
    assert len(remote.list("objects/")) == len(entries)


def test_gc_lists_objects_no_tag_references(tmp_path):
    paths, _ = make_corpus(tmp_path)
    _commit_index(tmp_path, paths, "corpus-2026-01")
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, build_index(paths))
    store = paths.objects_dir.parent
    stray = store / "objects" / "ff" / ("f" * 64 + ".owl")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")
    remote.put({"objects/ff/" + stray.name: stray}, "stray")

    report = gc(paths, remote)

    assert report.local == [stray]
    assert report.remote == ["objects/ff/" + stray.name]
    assert stray.exists()


def test_caches_round_trip_under_versioned_prefix(tmp_path):
    paths, hashes = make_corpus(tmp_path)
    sidecar = paths.sidecar_path(hashes[0])
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"parquet bytes")
    remote = DirRemote(tmp_path / "cache-remote")

    assert push_caches(paths, remote) == 1
    assert any(
        p.startswith(f"cache/structure/{paths.structure_dir.name}/") for p in remote.list("cache/")
    )
    other = _empty_store(tmp_path, paths)
    assert pull_caches(other, remote) == 1
    assert other.sidecar_path(hashes[0]).read_bytes() == b"parquet bytes"


def test_open_remote_rejects_unknown_scheme(tmp_path):
    assert isinstance(open_remote(f"file://{tmp_path}"), DirRemote)
    with pytest.raises(ValueError):
        open_remote("s3://bucket/x")


def test_cli_index_push_pull_verify_end_to_end(tmp_path):
    paths, _ = make_corpus(tmp_path)
    remote = f"file://{tmp_path / 'remote'}"
    first = ["--corpus", str(paths.corpus_dir), "--store", str(paths.objects_dir.parent)]
    second = ["--corpus", str(paths.corpus_dir), "--store", str(tmp_path / "other")]
    runner = CliRunner()

    assert runner.invoke(cli, ["index", *first]).exit_code == 0
    result = runner.invoke(cli, ["push", "--store-remote", remote, *first])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["status", "--store-remote", remote, *second])
    assert result.exit_code == 1, result.output  # the second store lacks everything
    assert runner.invoke(cli, ["pull", "--store-remote", remote, *second]).exit_code == 0
    assert runner.invoke(cli, ["verify", "--deep", *second]).exit_code == 0
    assert runner.invoke(cli, ["status", "--store-remote", remote, *second]).exit_code == 0


def test_store_remote_defaults_to_the_store_dataset(tmp_path, monkeypatch):
    monkeypatch.delenv("ONTOMETER_STORE_REMOTE", raising=False)
    opened = []

    def fake_open(url):
        opened.append(url)
        return DirRemote(tmp_path / "remote")

    monkeypatch.setattr(store_cli, "_open", fake_open)
    paths, _ = make_corpus(tmp_path)
    args = ["--corpus", str(paths.corpus_dir), "--store", str(paths.objects_dir.parent)]
    runner = CliRunner()
    assert runner.invoke(cli, ["index", *args]).exit_code == 0
    result = runner.invoke(cli, ["push", *args])
    assert result.exit_code == 0, result.output
    assert opened == ["hf://datasets/growgraph/ontometer-store"]


@pytest.mark.parametrize(
    ("command", "default"),
    [
        ("push", "hf://datasets/growgraph/ontometer-cache"),
        ("publish", "hf://datasets/growgraph/ontometer-corpus"),
    ],
)
def test_cache_and_corpus_remotes_default_to_their_datasets(command, default):
    result = CliRunner().invoke(cli, [command, "--help"])
    assert default in result.output


def test_push_refuses_a_public_remote(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    public = tmp_path / "public"
    (public / "releases" / "corpus-a" / "index").mkdir(parents=True)
    (public / "releases" / "corpus-a" / "index" / "objects.parquet").write_bytes(b"x")
    with pytest.raises(StoreError):
        push(paths, DirRemote(public), entries)
    with pytest.raises(StoreError):
        push_caches(paths, DirRemote(public))
    assert not (public / "objects").exists()


def test_tag_refuses_remote_with_damaged_object(tmp_path):
    paths, _ = make_corpus(tmp_path)
    _commit_index(tmp_path, paths, "corpus-2026-01")
    entries = build_index(paths)
    remote = DirRemote(tmp_path / "remote")
    push(paths, remote, entries)
    (tmp_path / "remote" / entries[0].path).write_bytes(b"short")
    with pytest.raises(StoreError):
        tag(paths, remote, "corpus-2026-01")
