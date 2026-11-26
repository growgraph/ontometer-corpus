"""Remotes that mirror the object store: a directory, or a Hugging Face dataset.

A remote holds files at store-relative paths (``objects/<hh>/<hash><ext>``,
``cache/<kind>/v<N>/<hh>/<hash>.parquet``). It is add-only: :meth:`Remote.put`
skips a path that already exists and nothing here deletes. Object paths are
content hashes, so skipping an existing path never loses data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

T = TypeVar("T")

#: The datasets the remote options default to: every object (private), the
#: versioned caches (private), and the public, licence-filtered releases.
STORE_REMOTE = "hf://datasets/growgraph/ontometer-store"
CACHE_REMOTE = "hf://datasets/growgraph/ontometer-cache"
CORPUS_REMOTE = "hf://datasets/growgraph/ontometer-corpus"


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A file on a remote; ``sha256`` is set when the remote records one."""

    path: str
    size: int
    sha256: str | None = None


class Remote(Protocol):
    """What the store operations need from a remote."""

    def list(self, prefix: str, revision: str | None = None) -> dict[str, RemoteFile]:
        """Files under *prefix*, by path, at *revision* (latest when None)."""
        ...

    def fetch(self, rel_paths: Sequence[str], staging: Path, revision: str | None = None) -> None:
        """Download each path to ``staging / path``."""
        ...

    def put(self, files: Mapping[str, Path], message: str) -> None:
        """Upload local files to the given remote paths, skipping paths that exist."""
        ...

    def write(self, rel_path: str, data: bytes, message: str) -> None:
        """Create or replace one small metadata file (the dataset card); never an object."""
        ...

    def tag(self, name: str) -> None:
        """Mark the current state with *name*."""
        ...

    def tags(self) -> set[str]:
        """Names given to :meth:`tag`."""
        ...


def _refuse_object_path(rel_path: str) -> None:
    if rel_path.startswith(("objects/", "cache/", "releases/")):
        raise ValueError(f"{rel_path} is add-only; use put()")


class DirRemote:
    """A remote that is a directory with the store's layout (``file://…``).

    Serves as the offline hand-over, an rsync target and the test double. A tag
    is a listing snapshot under ``tags/<name>.tsv``.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _tag_file(self, name: str) -> Path:
        return self.root / "tags" / f"{name}.tsv"

    def list(self, prefix: str, revision: str | None = None) -> dict[str, RemoteFile]:
        base = self.root / prefix
        found: dict[str, RemoteFile] = {}
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.name.startswith(".tmp-"):
                    rel = path.relative_to(self.root).as_posix()
                    found[rel] = RemoteFile(rel, path.stat().st_size)
        if revision is not None:
            tag_file = self._tag_file(revision)
            if not tag_file.exists():
                raise ValueError(f"no tag {revision!r} on {self.root}")
            tagged = set(tag_file.read_text().splitlines())
            found = {p: f for p, f in found.items() if p in tagged}
        return found

    def fetch(self, rel_paths: Sequence[str], staging: Path, revision: str | None = None) -> None:
        for rel in rel_paths:
            source = self.root / rel
            if not source.is_file():
                continue
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def put(self, files: Mapping[str, Path], message: str) -> None:
        for rel, local in files.items():
            target = self.root / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f".tmp-{target.name}")
            shutil.copyfile(local, partial)
            os.replace(partial, target)

    def write(self, rel_path: str, data: bytes, message: str) -> None:
        _refuse_object_path(rel_path)
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".tmp-{target.name}")
        partial.write_bytes(data)
        os.replace(partial, target)

    def tag(self, name: str) -> None:
        tag_file = self._tag_file(name)
        tag_file.parent.mkdir(parents=True, exist_ok=True)
        listing = sorted(p for p in self.list("") if not p.startswith("tags/"))
        tag_file.write_text("\n".join(listing) + "\n")

    def tags(self) -> set[str]:
        tag_dir = self.root / "tags"
        return {p.stem for p in tag_dir.glob("*.tsv")} if tag_dir.exists() else set()


#: Commit operations per Hugging Face commit; the Hub checks each one in a single request.
HF_COMMIT_OPS = 100
#: Parallel downloads from the Hub.
HF_DOWNLOAD_WORKERS = 8
#: HTTP statuses worth retrying, with exponential backoff.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRIES = 6
#: Keeps every object an LFS/Xet file, so tree listings carry its sha256.
_GITATTRIBUTES_RULE = "objects/** filter=lfs diff=lfs merge=lfs -text"


def _with_retries(call: Callable[[], T]) -> T:
    """Run *call*, retrying throttling and server errors with exponential backoff."""
    for attempt in range(_RETRIES):
        try:
            return call()
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            if code not in _RETRY_STATUSES or attempt == _RETRIES - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


class HfRemote:
    """A Hugging Face dataset repository (``hf://datasets/<org>/<name>``).

    Credentials come from the ``huggingface_hub`` login. Downloads go to the
    caller's staging directory, never to the hub cache, so the store is the only
    full copy on disk.
    """

    def __init__(self, repo_id: str, *, api: Any = None) -> None:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi()
        self.repo_id = repo_id
        self.api = api
        self._known: set[str] | None = None

    def list(self, prefix: str, revision: str | None = None) -> dict[str, RemoteFile]:
        from huggingface_hub.errors import EntryNotFoundError, RemoteEntryNotFoundError
        from huggingface_hub.hf_api import RepoFile

        try:
            tree = list(
                _with_retries(
                    lambda: self.api.list_repo_tree(
                        self.repo_id,
                        path_in_repo=prefix.rstrip("/") or None,
                        recursive=True,
                        revision=revision,
                        repo_type="dataset",
                    )
                )
            )
        except (EntryNotFoundError, RemoteEntryNotFoundError):
            return {}
        found: dict[str, RemoteFile] = {}
        for item in tree:
            if isinstance(item, RepoFile):
                sha256 = item.lfs.sha256 if item.lfs is not None else None
                found[item.path] = RemoteFile(item.path, item.size, sha256)
        return found

    def fetch(self, rel_paths: Sequence[str], staging: Path, revision: str | None = None) -> None:
        def _one(rel: str) -> None:
            _with_retries(
                lambda: self.api.hf_hub_download(
                    self.repo_id,
                    rel,
                    repo_type="dataset",
                    revision=revision,
                    local_dir=staging,
                )
            )

        staging.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(HF_DOWNLOAD_WORKERS) as pool:
            list(pool.map(_one, rel_paths))

    def _known_paths(self) -> set[str]:
        if self._known is None:
            self._known = set(self.list(""))
        return self._known

    def _ensure_gitattributes(self) -> None:
        """Commit the LFS rule for ``objects/`` once, before the first object."""
        from huggingface_hub import CommitOperationAdd

        current = ""
        if ".gitattributes" in self._known_paths():
            with tempfile.TemporaryDirectory() as tmp:
                self.fetch([".gitattributes"], Path(tmp))
                current = (Path(tmp) / ".gitattributes").read_text()
        if _GITATTRIBUTES_RULE in current.splitlines():
            return
        content = (current.rstrip("\n") + "\n" if current else "") + _GITATTRIBUTES_RULE + "\n"
        op = CommitOperationAdd(path_in_repo=".gitattributes", path_or_fileobj=content.encode())
        _with_retries(
            lambda: self.api.create_commit(
                self.repo_id,
                [op],
                commit_message="Track objects/ through LFS",
                repo_type="dataset",
            )
        )
        self._known_paths().add(".gitattributes")

    def put(self, files: Mapping[str, Path], message: str) -> None:
        from huggingface_hub import CommitOperationAdd

        known = self._known_paths()
        todo = sorted((rel, local) for rel, local in files.items() if rel not in known)
        if not todo:
            return
        if any(rel.startswith("objects/") for rel, _ in todo):
            self._ensure_gitattributes()
        for start in range(0, len(todo), HF_COMMIT_OPS):
            batch = todo[start : start + HF_COMMIT_OPS]
            ops = [
                CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(local))
                for rel, local in batch
            ]
            _with_retries(
                lambda ops=ops: self.api.create_commit(
                    self.repo_id, ops, commit_message=message, repo_type="dataset"
                )
            )
            known.update(rel for rel, _ in batch)

    def write(self, rel_path: str, data: bytes, message: str) -> None:
        from huggingface_hub import CommitOperationAdd

        _refuse_object_path(rel_path)
        op = CommitOperationAdd(path_in_repo=rel_path, path_or_fileobj=data)
        _with_retries(
            lambda: self.api.create_commit(
                self.repo_id, [op], commit_message=message, repo_type="dataset"
            )
        )
        self._known_paths().add(rel_path)

    def tag(self, name: str) -> None:
        if name in self.tags():
            return
        _with_retries(lambda: self.api.create_tag(self.repo_id, tag=name, repo_type="dataset"))

    def tags(self) -> set[str]:
        refs = _with_retries(lambda: self.api.list_repo_refs(self.repo_id, repo_type="dataset"))
        return {ref.name for ref in refs.tags}


def open_remote(url: str, *, api: Any = None) -> Remote:
    """A remote from its URL: ``file:///path`` or ``hf://datasets/<org>/<name>``.

    Raises:
        ValueError: unknown scheme.
    """
    if url.startswith("file://"):
        return DirRemote(Path(url.removeprefix("file://")))
    if url.startswith("hf://datasets/"):
        repo_id = url.removeprefix("hf://datasets/").strip("/")
        if repo_id.count("/") == 1:
            return HfRemote(repo_id, api=api)
    raise ValueError(f"unsupported remote {url!r}; use file:///path or hf://datasets/<org>/<name>")
