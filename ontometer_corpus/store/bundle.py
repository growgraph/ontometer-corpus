"""Solid ``tar.zst`` bundles of a corpus version, for archives and offline hand-over.

A bundle holds the objects of git tag ``rev`` (or only those new since
``base``), ordered by release family and release so that near-identical
releases sit next to each other and compress together. It is deterministic:
the same objects give the same bytes. zstd runs with long-distance matching
and a 128 MiB window, which plain ``tar --zstd -xf`` can still decompress; files
larger than the window do not compress against their neighbours.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.store.index import ObjectEntry, index_at_rev, local_objects
from ontometer_corpus.store.licence import Policy, verdicts
from ontometer_corpus.store.ops import StoreError
from ontometer_corpus.store.publish import data_at_rev
from ontometer_corpus.versions import family_key, release_date, release_order

#: zstd window: 2**27 bytes, the largest a decoder accepts without a flag.
WINDOW_LOG = 27
DEFAULT_LEVEL = 10


@dataclass(frozen=True, slots=True)
class BundleResult:
    archive: Path
    hashes: Path
    entries: list[ObjectEntry]


class _HashingReader:
    """A file wrapper that hashes what tarfile reads from it."""

    def __init__(self, handle) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.digest.update(data)
        return data


def _order(paths: CorpusPaths, entries: list[ObjectEntry]) -> list[ObjectEntry]:
    """Release family, then release order within it, then hash."""
    conn = sqlite3.connect(f"file:{paths.db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT file_hash, source_id, source_registry_metadata FROM corpus_registry "
            "WHERE file_hash IS NOT NULL AND file_hash != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    keys: dict[str, tuple] = {}
    for digest, source_id, metadata in rows:
        if digest not in keys:
            source = source_id or ""
            keys[digest] = (family_key(source), release_order(source, release_date(metadata)))
    return sorted(entries, key=lambda e: (*keys.get(e.file_hash, ("￿", ("", -1, ""))), e.file_hash))


def bundle(
    paths: CorpusPaths,
    rev: str,
    out_dir: Path,
    *,
    base: str | None = None,
    policy: Policy | None = None,
    level: int = DEFAULT_LEVEL,
) -> BundleResult:
    """Write ``objects-<rev>[-since-<base>].tar.zst`` and its ``.sha256`` list into *out_dir*.

    With *policy*, only redistributable objects are included.

    Raises:
        StoreError: an object is missing locally or its bytes do not match its hash.
    """
    import zstandard

    entries = index_at_rev(paths.corpus_dir, rev)
    if base is not None:
        before = {e.file_hash for e in index_at_rev(paths.corpus_dir, base)}
        entries = [e for e in entries if e.file_hash not in before]
    with tempfile.TemporaryDirectory() as tmp:
        at_rev = data_at_rev(paths, rev, Path(tmp) / "data")
        if policy is not None:
            allowed = {v.file_hash for v in verdicts(at_rev, policy) if v.redistribute}
            entries = [e for e in entries if e.file_hash in allowed]
        entries = _order(at_rev, entries)

    local = local_objects(paths)
    missing = [e for e in entries if e.file_hash not in local]
    if missing:
        raise StoreError(f"{len(missing)} objects missing locally, e.g. {missing[0].path}")

    stem = f"objects-{rev}" + (f"-since-{base}" if base else "")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{stem}.tar.zst"
    hashes = out_dir / f"{stem}.sha256"
    partial = out_dir / f".tmp-{archive.name}"
    params = zstandard.ZstdCompressionParameters.from_level(
        level, window_log=WINDOW_LOG, enable_ldm=True
    )
    compressor = zstandard.ZstdCompressor(compression_params=params)
    try:
        with partial.open("wb") as raw, compressor.stream_writer(raw, closefd=False) as writer:
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.GNU_FORMAT) as tar:
                for entry in entries:
                    source = local[entry.file_hash]
                    info = tarfile.TarInfo(entry.path)
                    info.size = source.stat().st_size
                    info.mtime = 0
                    info.mode = 0o644
                    with source.open("rb") as handle:
                        reader = _HashingReader(handle)
                        tar.addfile(info, reader)
                    if reader.digest.hexdigest() != entry.file_hash:
                        raise StoreError(f"{entry.path} does not match its hash")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(archive)
    hashes.write_text("".join(f"{e.file_hash}  {e.path}\n" for e in entries))
    return BundleResult(archive=archive, hashes=hashes, entries=entries)
