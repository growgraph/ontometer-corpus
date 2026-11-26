"""Solid zstd tar bundles of a corpus version, full or since a base version."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import zstandard

from ontometer_corpus.store.bundle import bundle
from ontometer_corpus.store.index import build_index, write_index
from ontometer_corpus.store.licence import Policy
from ontometer_corpus.store.ops import StoreError

from .test_store_licence import _corpus

CC_BY = "http://creativecommons.org/licenses/by/4.0/"
POLICY = Policy(default="withhold", unrecognised="withhold", redistribute=("CC-BY*",), withhold=())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        capture_output=True,
    )


def _tagged(tmp_path: Path, rows: list[dict], name: str):
    paths = _corpus(tmp_path, rows)
    write_index(build_index(paths), paths.objects_index_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "data")
    _git(tmp_path, "commit", "-qm", "data")
    _git(tmp_path, "tag", name)
    return paths


def _members(archive: Path) -> dict[str, bytes]:
    raw = zstandard.ZstdDecompressor().stream_reader(archive.open("rb"))
    with tarfile.open(fileobj=io.BufferedReader(raw), mode="r|") as tar:
        out = {}
        for member in tar:
            handle = tar.extractfile(member)
            assert handle is not None
            out[member.name] = handle.read()
        return out


ROWS = [
    {"content": b"alpha " * 1000, "licence": [CC_BY]},
    {"content": b"beta " * 1000, "licence": []},
    {"content": b"gamma", "licence": [CC_BY], "suffix": ".ttl.gz"},
]


def test_bundle_round_trip_extracts_identical_bytes(tmp_path):
    paths = _tagged(tmp_path, ROWS, "corpus-a")
    result = bundle(paths, "corpus-a", tmp_path / "out")

    members = _members(result.archive)
    assert len(members) == 3
    for name, data in members.items():
        assert name.rsplit("/", 1)[-1].startswith(hashlib.sha256(data).hexdigest())
    listed = result.hashes.read_text().splitlines()
    assert sorted(line.split("  ")[1] for line in listed) == sorted(members)


def test_bundle_is_deterministic(tmp_path):
    paths = _tagged(tmp_path, ROWS, "corpus-a")
    first = bundle(paths, "corpus-a", tmp_path / "one").archive.read_bytes()
    second = bundle(paths, "corpus-a", tmp_path / "two").archive.read_bytes()
    assert first == second


def test_delta_contains_only_new_objects(tmp_path):
    paths = _tagged(tmp_path, ROWS[:2], "corpus-a")
    (tmp_path / "next").mkdir()
    paths = _corpus(tmp_path / "next", ROWS)
    # Same repository: replace data/ with the larger corpus and tag it.
    shutil.rmtree(tmp_path / "data")
    shutil.copytree(tmp_path / "next" / "data", tmp_path / "data")
    shutil.copytree(tmp_path / "next" / "store", tmp_path / "store", dirs_exist_ok=True)
    paths = paths.__class__(corpus_dir=tmp_path / "data", store_dir=tmp_path / "store")
    write_index(build_index(paths), paths.objects_index_path)
    _git(tmp_path, "add", "-A", "data")
    _git(tmp_path, "commit", "-qm", "more")
    _git(tmp_path, "tag", "corpus-b")

    result = bundle(paths, "corpus-b", tmp_path / "out", base="corpus-a")

    assert result.archive.name == "objects-corpus-b-since-corpus-a.tar.zst"
    (only,) = _members(result.archive).values()
    assert only == b"gamma"


def test_public_bundle_excludes_withheld(tmp_path):
    paths = _tagged(tmp_path, ROWS, "corpus-a")
    result = bundle(paths, "corpus-a", tmp_path / "out", policy=POLICY)
    assert sorted(_members(result.archive).values()) == sorted([b"alpha " * 1000, b"gamma"])


def test_corrupt_object_fails_the_bundle(tmp_path):
    paths = _tagged(tmp_path, ROWS, "corpus-a")
    entry = build_index(paths)[0]
    target = paths.objects_dir.parent / entry.path
    target.write_bytes(b"x" * entry.size)
    with pytest.raises(StoreError):
        bundle(paths, "corpus-a", tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.tar.zst"))


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs the zstd binary for tar --zstd")
def test_tar_cli_reads_bundle(tmp_path):
    paths = _tagged(tmp_path, ROWS, "corpus-a")
    result = bundle(paths, "corpus-a", tmp_path / "out")
    dest = tmp_path / "extracted"
    dest.mkdir()
    subprocess.run(["tar", "--zstd", "-xf", str(result.archive), "-C", str(dest)], check=True)
    subprocess.run(["sha256sum", "-c", "--quiet", str(result.hashes)], cwd=dest, check=True)
