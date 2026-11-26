"""Exchange between the local store and a remote: status, push, pull, tag, gc.

Presence is decided by content hash (the first 64 characters of a file name),
never by the exact path. Every object is hash-checked before it is uploaded
and after it is downloaded; a download lands in the staging directory and is
renamed into ``objects/`` only once its hash matches, so an interrupted pull
never leaves a partial object behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from suthing import file_hash

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.store.index import (
    HASH_LEN,
    IndexMissingAtRev,
    ObjectEntry,
    index_at_rev,
    local_objects,
)
from ontometer_corpus.store.remote import Remote, RemoteFile

#: Objects per upload or download round; each round is resumable on its own.
BATCH = 100

#: Git tags whose object index the store must keep forever.
TAG_PATTERN = "corpus-*"


class StoreError(RuntimeError):
    """Raised when an operation would leave a remote or a tag inconsistent."""


@dataclass(frozen=True, slots=True)
class Status:
    """What each side lacks for an index; ``remote_bad`` disagrees in size or hash."""

    remote_missing: list[ObjectEntry]
    local_missing: list[ObjectEntry]
    remote_bad: list[ObjectEntry]

    def ok(self) -> bool:
        return not (self.remote_missing or self.local_missing or self.remote_bad)


@dataclass(frozen=True, slots=True)
class PushReport:
    uploaded: list[ObjectEntry] = field(default_factory=list)
    corrupt: list[ObjectEntry] = field(default_factory=list)
    missing_locally: list[ObjectEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PullReport:
    downloaded: list[ObjectEntry] = field(default_factory=list)
    rejected: list[ObjectEntry] = field(default_factory=list)
    unavailable: list[ObjectEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GcReport:
    """Objects no tagged index (nor the working index) references. Listed, never deleted."""

    local: list[Path]
    remote: list[str]
    tags_without_index: list[str]


def remote_objects(remote: Remote, revision: str | None = None) -> dict[str, RemoteFile]:
    """The remote's objects, by content hash."""
    found: dict[str, RemoteFile] = {}
    for path, info in remote.list("objects/", revision).items():
        name = PurePosixPath(path).name
        if len(name) >= HASH_LEN:
            found.setdefault(name[:HASH_LEN], info)
    return found


def status(
    paths: CorpusPaths, remote: Remote, entries: list[ObjectEntry], revision: str | None = None
) -> Status:
    """Compare an index with the local store and the remote, by hash and size."""
    local = local_objects(paths)
    remote_files = remote_objects(remote, revision)
    bad = []
    for entry in entries:
        info = remote_files.get(entry.file_hash)
        if info is not None and (
            info.size != entry.size or (info.sha256 is not None and info.sha256 != entry.file_hash)
        ):
            bad.append(entry)
    return Status(
        remote_missing=[e for e in entries if e.file_hash not in remote_files],
        local_missing=[e for e in entries if e.file_hash not in local],
        remote_bad=bad,
    )


def _store_rel(paths: CorpusPaths, path: Path) -> str:
    return path.relative_to(paths.require_store_dir()).as_posix()


def _refuse_public(remote: Remote) -> None:
    """A remote holding releases is a public copy: only ``publish`` uploads to it."""
    if remote.list("releases/"):
        raise StoreError(
            "this remote holds public releases; `push` would upload withheld objects. "
            "Push to the private working remote, and use `publish` for the public one"
        )


def push(
    paths: CorpusPaths,
    remote: Remote,
    entries: list[ObjectEntry],
    *,
    limit: int | None = None,
    allow_public: bool = False,
) -> PushReport:
    """Upload the indexed objects the remote lacks, after checking each one's hash.

    Raises:
        StoreError: the remote is a public release copy (unless ``allow_public``).
    """
    if not allow_public:
        _refuse_public(remote)
    remote_files = remote_objects(remote)
    local = local_objects(paths)
    report = PushReport()
    todo = [e for e in entries if e.file_hash not in remote_files]
    if limit is not None:
        todo = todo[:limit]
    ready: list[tuple[ObjectEntry, Path]] = []
    for entry in todo:
        found = local.get(entry.file_hash)
        if found is None:
            report.missing_locally.append(entry)
        elif file_hash(found) != entry.file_hash:
            report.corrupt.append(entry)
        else:
            ready.append((entry, found))
    for start in range(0, len(ready), BATCH):
        batch = ready[start : start + BATCH]
        remote.put({_store_rel(paths, p): p for _, p in batch}, f"Add {len(batch)} objects")
        report.uploaded.extend(e for e, _ in batch)
    return report


def move_into_place(staged: Path, target: Path) -> None:
    """Rename *staged* onto *target* atomically, also across filesystems."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(staged, target)
    except OSError:
        partial = target.with_name(f".tmp-{target.name}")
        shutil.move(staged, partial)
        os.replace(partial, target)


def pull(
    paths: CorpusPaths,
    remote: Remote,
    entries: list[ObjectEntry],
    *,
    revision: str | None = None,
    limit: int | None = None,
) -> PullReport:
    """Download the indexed objects the local store lacks, keeping only hash matches."""
    local = local_objects(paths)
    remote_files = remote_objects(remote, revision)
    report = PullReport()
    todo = []
    for entry in entries:
        if entry.file_hash in local:
            continue
        if entry.file_hash not in remote_files:
            report.unavailable.append(entry)
        else:
            todo.append(entry)
    if limit is not None:
        todo = todo[:limit]
    staging = paths.staging_dir
    store = paths.require_store_dir()
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        rels = [remote_files[e.file_hash].path for e in batch]
        remote.fetch(rels, staging, revision)
        for entry, rel in zip(batch, rels, strict=True):
            staged = staging / rel
            if not staged.is_file():
                report.unavailable.append(entry)
            elif file_hash(staged) != entry.file_hash:
                staged.unlink()
                report.rejected.append(entry)
            else:
                move_into_place(staged, store / rel)
                report.downloaded.append(entry)
    return report


def _cache_prefix(kind: str, directory: Path) -> str:
    return f"cache/{kind}/{directory.name}/"


def push_caches(paths: CorpusPaths, remote: Remote) -> int:
    """Upload cache files of the current producer versions the remote lacks."""
    _refuse_public(remote)
    uploaded = 0
    for kind, directory in paths.cache_dirs().items():
        if not directory.exists():
            continue
        prefix = _cache_prefix(kind, directory)
        present = remote.list(prefix)
        files = {
            prefix + p.relative_to(directory).as_posix(): p
            for p in sorted(directory.rglob("*.parquet"))
        }
        missing = {r: p for r, p in files.items() if r not in present}
        items = sorted(missing.items())
        for start in range(0, len(items), BATCH):
            batch = dict(items[start : start + BATCH])
            remote.put(batch, f"Add {len(batch)} {kind} cache files")
            uploaded += len(batch)
    return uploaded


def pull_caches(paths: CorpusPaths, remote: Remote) -> int:
    """Download cache files of the current producer versions the local store lacks."""
    downloaded = 0
    staging = paths.staging_dir
    for kind, directory in paths.cache_dirs().items():
        prefix = _cache_prefix(kind, directory)
        listed = remote.list(prefix)
        todo = [
            info
            for rel, info in sorted(listed.items())
            if not (directory / rel[len(prefix) :]).exists()
        ]
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            remote.fetch([i.path for i in batch], staging)
            for info in batch:
                staged = staging / info.path
                if staged.is_file() and staged.stat().st_size == info.size:
                    move_into_place(staged, directory / info.path[len(prefix) :])
                    downloaded += 1
    return downloaded


def tag(paths: CorpusPaths, remote: Remote, name: str) -> None:
    """Tag the remote with *name* once it holds every object git tag *name* indexes.

    Raises:
        StoreError: the remote lacks or disagrees on some object.
    """
    entries = index_at_rev(paths.corpus_dir, name)
    result = status(paths, remote, entries)
    if result.remote_missing or result.remote_bad:
        raise StoreError(
            f"remote lacks {len(result.remote_missing)} and disagrees on "
            f"{len(result.remote_bad)} objects of {name}"
        )
    remote.tag(name)


def _git_tags(corpus_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(corpus_dir), "tag", "--list", TAG_PATTERN],
        capture_output=True,
        text=True,
    )
    return sorted(result.stdout.split()) if result.returncode == 0 else []


def gc(paths: CorpusPaths, remote: Remote | None = None) -> GcReport:
    """List local and remote objects referenced by no tagged index and not by the working one."""
    referenced: set[str] = set()
    without: list[str] = []
    for name in _git_tags(paths.corpus_dir):
        try:
            referenced |= {e.file_hash for e in index_at_rev(paths.corpus_dir, name)}
        except IndexMissingAtRev:
            without.append(name)
    try:
        referenced |= {e.file_hash for e in index_at_rev(paths.corpus_dir, None)}
    except IndexMissingAtRev:
        pass
    local = sorted(p for h, p in local_objects(paths).items() if h not in referenced)
    remote_paths = (
        sorted(info.path for h, info in remote_objects(remote).items() if h not in referenced)
        if remote is not None
        else []
    )
    return GcReport(local=local, remote=remote_paths, tags_without_index=without)
