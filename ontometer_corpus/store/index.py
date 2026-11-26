"""The tracked object index (``corpus/objects.tsv``) and local store verification.

The index lists every object the corpus references: its SHA-256, its size and
its path relative to the store root. A store *satisfies* an index when every
listed hash is present with the listed size (and, with ``deep``, the listed
hash). Presence is decided by hash, not by path, because the fetcher keeps the
extension of the first download of a given content.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from suthing import atomic_write, file_hash

from ontometer_corpus.paths import CorpusPaths

INDEX_HEADER = "file_hash\tsize\tpath"
HASH_LEN = 64
#: An object file name starts with its full hex digest; anything else (a partial
#: ``.tmp-`` file) is not an object.
_OBJECT_NAME = re.compile(r"[0-9a-f]{64}")


class IndexMissingAtRev(RuntimeError):
    """Raised when a revision has no object index to read."""


class MissingObjects(RuntimeError):
    """Raised when the index cannot be built because referenced objects are absent."""


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    """One referenced object: content hash, size in bytes, store-relative POSIX path."""

    file_hash: str
    size: int
    path: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Differences between a local store and an index."""

    missing: list[ObjectEntry] = field(default_factory=list)
    wrong_size: list[ObjectEntry] = field(default_factory=list)
    wrong_hash: list[ObjectEntry] = field(default_factory=list)
    orphans: list[Path] = field(default_factory=list)

    def ok(self) -> bool:
        """True when every indexed object is present and intact (orphans are allowed)."""
        return not (self.missing or self.wrong_size or self.wrong_hash)


def _relative_object_path(retrieved_file: str) -> str:
    """``retrieved_file`` relative to the store root, re-anchoring legacy absolute paths."""
    index = retrieved_file.find("objects/")
    return retrieved_file[index:] if index > 0 else retrieved_file


def build_index(paths: CorpusPaths) -> list[ObjectEntry]:
    """Index every object ``corpus.db`` references, sized from the local store.

    Raises:
        MissingObjects: some referenced object is not in the store.
    """
    conn = sqlite3.connect(f"file:{paths.db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT file_hash, MIN(retrieved_file) FROM corpus_registry "
            "WHERE file_hash IS NOT NULL AND file_hash != '' GROUP BY file_hash"
        ).fetchall()
    finally:
        conn.close()
    local = local_objects(paths)
    entries, missing = [], []
    for digest, retrieved_file in rows:
        found = local.get(digest)
        if found is None:
            missing.append(digest)
            continue
        entries.append(
            ObjectEntry(digest, found.stat().st_size, _relative_object_path(retrieved_file))
        )
    if missing:
        raise MissingObjects(
            f"{len(missing)} referenced objects are not in {paths.objects_dir}, e.g. {missing[0]}"
        )
    return sorted(entries, key=lambda e: e.file_hash)


def write_index(entries: list[ObjectEntry], dest: Path) -> None:
    """Write the index canonically: header, rows sorted by hash, LF endings."""
    lines = [INDEX_HEADER] + [
        f"{e.file_hash}\t{e.size}\t{e.path}" for e in sorted(entries, key=lambda e: e.file_hash)
    ]
    atomic_write(dest, "\n".join(lines) + "\n", mkdir=True)


def parse_index(text: str) -> list[ObjectEntry]:
    """Read an index written by :func:`write_index`.

    Raises:
        ValueError: the text is not an object index.
    """
    lines = text.splitlines()
    if not lines or lines[0] != INDEX_HEADER:
        raise ValueError(f"not an object index: expected header {INDEX_HEADER!r}")
    entries = []
    for number, line in enumerate(lines[1:], start=2):
        parts = line.split("\t")
        if len(parts) != 3 or len(parts[0]) != HASH_LEN:
            raise ValueError(f"malformed object index line {number}: {line!r}")
        entries.append(ObjectEntry(parts[0], int(parts[1]), parts[2]))
    return entries


def index_at_rev(corpus_dir: Path, rev: str | None) -> list[ObjectEntry]:
    """The index in the working tree (``rev=None``) or at a git revision.

    Raises:
        IndexMissingAtRev: there is no index there.
    """
    index_file = corpus_dir / "objects.tsv"
    if rev is None:
        if not index_file.exists():
            raise IndexMissingAtRev(f"{index_file} does not exist; run `store index`")
        return parse_index(index_file.read_text())
    root = subprocess.run(
        ["git", "-C", str(corpus_dir), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if root.returncode != 0:
        raise IndexMissingAtRev(f"{corpus_dir} is not inside a git repository")
    rel = index_file.resolve().relative_to(Path(root.stdout.strip()).resolve()).as_posix()
    shown = subprocess.run(
        ["git", "-C", root.stdout.strip(), "show", f"{rev}:{rel}"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        raise IndexMissingAtRev(f"revision {rev} has no {rel}")
    return parse_index(shown.stdout)


def local_objects(paths: CorpusPaths) -> dict[str, Path]:
    """Every object file in the local store, by the hash its name starts with."""
    found: dict[str, Path] = {}
    if not paths.objects_dir.exists():
        return found
    for shard in sorted(paths.objects_dir.iterdir()):
        if not shard.is_dir():
            continue
        for item in sorted(shard.iterdir()):
            if item.is_file() and _OBJECT_NAME.match(item.name):
                found.setdefault(item.name[:HASH_LEN], item)
    return found


def verify(paths: CorpusPaths, entries: list[ObjectEntry], *, deep: bool = False) -> VerifyReport:
    """Compare the local store with an index; ``deep`` re-hashes every present object."""
    local = local_objects(paths)
    report = VerifyReport()
    for entry in entries:
        found = local.get(entry.file_hash)
        if found is None:
            report.missing.append(entry)
        elif found.stat().st_size != entry.size:
            report.wrong_size.append(entry)
        elif deep and file_hash(found) != entry.file_hash:
            report.wrong_hash.append(entry)
    indexed = {e.file_hash for e in entries}
    report.orphans.extend(sorted(p for h, p in local.items() if h not in indexed))
    return report


def move_orphans(report: VerifyReport, dest: Path) -> int:
    """Move orphaned objects under *dest*, keeping their store-relative paths."""
    moved = 0
    for orphan in report.orphans:
        target = dest / "objects" / orphan.parent.name / orphan.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(orphan, target)
        moved += 1
    return moved
