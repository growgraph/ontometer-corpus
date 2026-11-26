"""The tracked object index and local store verification."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ontometer_corpus.paths import CORPUS_ENV, CorpusPaths
from ontometer_corpus.pipeline.db import create_schema, get_connection
from ontometer_corpus.store.cli import cli
from ontometer_corpus.store.index import (
    IndexMissingAtRev,
    build_index,
    index_at_rev,
    move_orphans,
    parse_index,
    verify,
    write_index,
)

#: The corpus directory, when ``ONTOMETER_CORPUS`` names one.
CORPUS_DATA = Path(os.environ[CORPUS_ENV]) if CORPUS_ENV in os.environ else None

#: Suffixes the real store holds, including ones no RDF tool would guess.
ODD_SUFFIXES = (".rdf", ".3355", ".1", ".api", ".ttl.gz", "")


def _add_object(store: Path, content: bytes, suffix: str) -> tuple[str, str]:
    file_hash = hashlib.sha256(content).hexdigest()
    rel = f"objects/{file_hash[:2]}/{file_hash}{suffix}"
    target = store / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return file_hash, rel


def make_corpus(tmp_path: Path, suffixes=ODD_SUFFIXES) -> tuple[CorpusPaths, list[str]]:
    """A store with one object per suffix, each referenced by a corpus.db row."""
    paths = CorpusPaths(corpus_dir=tmp_path / "data", store_dir=tmp_path / "store")
    paths.corpus_dir.mkdir()
    conn = get_connection(paths.db_path)
    create_schema(conn)
    hashes = []
    for i, suffix in enumerate(suffixes):
        file_hash, rel = _add_object(paths.objects_dir.parent, f"object {i}".encode(), suffix)
        hashes.append(file_hash)
        conn.execute(
            "INSERT INTO corpus_registry (source_id, canonical_url, retrieved_file, file_hash, "
            "retrieval_date, source_registry, created_at, updated_at) "
            "VALUES (?, 'u', ?, ?, '2026-01-01', 'LOV', 'x', 'x')",
            (f"lov:o{i}::1", rel, file_hash),
        )
    conn.commit()
    conn.close()
    return paths, hashes


def test_index_sorted_and_byte_idempotent(tmp_path):
    paths, _ = make_corpus(tmp_path)
    write_index(build_index(paths), paths.objects_index_path)
    first = paths.objects_index_path.read_bytes()
    write_index(build_index(paths), paths.objects_index_path)
    assert paths.objects_index_path.read_bytes() == first
    entries = parse_index(first.decode())
    assert [e.file_hash for e in entries] == sorted(e.file_hash for e in entries)
    assert b"\r" not in first


def test_odd_extensions_round_trip(tmp_path):
    paths, hashes = make_corpus(tmp_path)
    entries = build_index(paths)
    by_hash = {e.file_hash: e for e in entries}
    assert set(by_hash) == set(hashes)
    for suffix, file_hash in zip(ODD_SUFFIXES, hashes, strict=True):
        assert by_hash[file_hash].path == f"objects/{file_hash[:2]}/{file_hash}{suffix}"
    write_index(entries, paths.objects_index_path)
    assert parse_index(paths.objects_index_path.read_text()) == entries


def test_verify_reports_missing_wrong_size_orphan(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    store = paths.objects_dir.parent
    (store / entries[0].path).unlink()
    (store / entries[1].path).write_bytes(b"truncated")
    _, orphan = _add_object(store, b"nobody references me", ".owl")

    report = verify(paths, entries)

    assert [e.file_hash for e in report.missing] == [entries[0].file_hash]
    assert [e.file_hash for e in report.wrong_size] == [entries[1].file_hash]
    assert report.orphans == [store / orphan]
    assert not report.ok()


def test_deep_verify_catches_same_size_corruption(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    target = paths.objects_dir.parent / entries[0].path
    target.write_bytes(bytes(b ^ 1 for b in target.read_bytes()))

    assert verify(paths, entries).ok()
    report = verify(paths, entries, deep=True)
    assert [e.file_hash for e in report.wrong_hash] == [entries[0].file_hash]


def test_presence_is_by_hash_not_extension(tmp_path):
    paths, _ = make_corpus(tmp_path, suffixes=(".rdf",))
    entry = build_index(paths)[0]
    store = paths.objects_dir.parent
    (store / entry.path).rename(store / entry.path.replace(".rdf", ".owl"))
    report = verify(paths, [entry], deep=True)
    assert report.ok()
    assert report.orphans == []


def test_move_orphans_moves_only_orphans(tmp_path):
    paths, _ = make_corpus(tmp_path)
    entries = build_index(paths)
    store = paths.objects_dir.parent
    _, orphan = _add_object(store, b"orphan", ".owl")
    dest = tmp_path / "orphans"

    assert move_orphans(verify(paths, entries), dest) == 1
    assert (dest / orphan).exists()
    assert not (store / orphan).exists()
    assert verify(paths, entries).ok()


def test_index_at_rev_without_index_raises(tmp_path):
    paths, _ = make_corpus(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@e"]
    (tmp_path / "README").write_text("x")
    subprocess.run([*git, "add", "README"], check=True)
    subprocess.run([*git, "commit", "-qm", "no index yet"], check=True)
    with pytest.raises(IndexMissingAtRev):
        index_at_rev(paths.corpus_dir, "HEAD")

    write_index(build_index(paths), paths.objects_index_path)
    subprocess.run([*git, "add", "data/objects.tsv"], check=True)
    subprocess.run([*git, "commit", "-qm", "index"], check=True)
    assert index_at_rev(paths.corpus_dir, "HEAD") == build_index(paths)


def test_parse_index_rejects_a_foreign_file():
    with pytest.raises(ValueError):
        parse_index("hash\tpath\nabc\tobjects/x\n")


def test_cli_verify_exits_nonzero_on_missing(tmp_path):
    paths, _ = make_corpus(tmp_path)
    args = ["--corpus", str(paths.corpus_dir), "--store", str(paths.objects_dir.parent)]
    runner = CliRunner()
    assert runner.invoke(cli, ["index", *args]).exit_code == 0
    assert runner.invoke(cli, ["verify", *args]).exit_code == 0
    entry = parse_index(paths.objects_index_path.read_text())[0]
    (paths.objects_dir.parent / entry.path).unlink()
    result = runner.invoke(cli, ["verify", *args])
    assert result.exit_code == 1
    assert "missing" in result.output


@pytest.mark.skipif(
    CORPUS_DATA is None or not (CORPUS_DATA / "objects.tsv").exists(),
    reason="no corpus directory (set ONTOMETER_CORPUS)",
)
def test_tracked_index_hashes_and_paths_match_corpus_db():
    """Sizes need the store; hashes and paths can be checked from the repository alone."""
    assert CORPUS_DATA is not None
    tracked = {
        (e.file_hash, e.path) for e in parse_index((CORPUS_DATA / "objects.tsv").read_text())
    }
    conn = sqlite3.connect(f"file:{CORPUS_DATA / 'corpus.db'}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT file_hash, MIN(retrieved_file) FROM corpus_registry "
        "WHERE file_hash IS NOT NULL AND file_hash != '' GROUP BY file_hash"
    ).fetchall()
    assert tracked == {(h, p) for h, p in rows}


def test_local_objects_ignore_partial_files(tmp_path):
    paths, _ = make_corpus(tmp_path, suffixes=(".rdf",))
    entry = build_index(paths)[0]
    shard = (paths.objects_dir.parent / entry.path).parent
    (shard / f".tmp-{entry.file_hash}.rdf").write_bytes(b"half")
    assert verify(paths, [entry]).orphans == []


def test_cli_verify_rejects_orphans_to_with_rev(tmp_path):
    paths, _ = make_corpus(tmp_path)
    args = ["--corpus", str(paths.corpus_dir), "--store", str(paths.objects_dir.parent)]
    result = CliRunner().invoke(
        cli, ["verify", "--rev", "HEAD", "--orphans-to", str(tmp_path / "o"), *args]
    )
    assert result.exit_code == 2


def test_cli_verify_if_indexed_skips_without_an_index(tmp_path):
    paths, _ = make_corpus(tmp_path)
    args = ["--corpus", str(paths.corpus_dir), "--store", str(paths.objects_dir.parent)]
    result = CliRunner().invoke(cli, ["verify", "--if-indexed", *args])
    assert result.exit_code == 0
    assert "no object index" in result.output
    assert CliRunner().invoke(cli, ["verify", *args]).exit_code == 1
