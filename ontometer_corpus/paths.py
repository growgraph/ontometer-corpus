"""Every filesystem location the corpus pipeline uses, in one place.

All locations derive from two inputs.

``corpus_dir``
    Tracked corpus artifacts — the registry database, the fetch manifest, the
    per-ontology tables and the dependency network: a clone of the corpus
    repository.

``store_dir``
    The object store: the raw ontology files themselves, content-addressed
    under ``objects/``, and everything rebuildable from them under ``cache/``
    (per-file structural sidecars and role tables, entity-level tables). Far
    too large for a repository, and the one thing a fresh checkout has to
    obtain separately.

Both are settable by flag (``--corpus``, ``--store``) or by
environment (``ONTOMETER_CORPUS``, ``ONTOMETER_STORE``), flag winning. Neither
has a default: a missing corpus directory is an error rather than an empty
corpus started in the working directory.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

#: Environment variable naming the tracked-artifact directory.
CORPUS_ENV = "ONTOMETER_CORPUS"

#: Environment variable naming the object-store root.
STORE_ENV = "ONTOMETER_STORE"

#: Producer versions of the two per-file caches, part of their paths. Bump one
#: whenever its writer's output changes (``parse._write_sidecar`` for the
#: structure sidecars, ``ontometer-corpus entity-roles`` for the role tables): both
#: writers skip files that exist, so only a new version directory gets fresh
#: output.
STRUCTURE_VERSION = 1
ROLES_VERSION = 1


class MissingStore(RuntimeError):
    """Raised when an operation needs the raw files and none is configured."""


class MissingCorpus(click.UsageError):
    """Raised when no corpus directory is given; a click command exits 2 with the message."""

    def __init__(self) -> None:
        super().__init__(f"no corpus directory: pass --corpus or set {CORPUS_ENV}")


@dataclass(frozen=True, slots=True)
class CorpusPaths:
    """Resolved locations for one pipeline run."""

    corpus_dir: Path
    store_dir: Path | None = None

    # --- tracked artifacts -------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.corpus_dir / "corpus.db"

    @property
    def manifest_path(self) -> Path:
        return self.corpus_dir / "manifest.jsonl"

    @property
    def network_nodes_path(self) -> Path:
        return self.corpus_dir / "network_nodes.parquet"

    @property
    def network_edges_path(self) -> Path:
        return self.corpus_dir / "network_edges.parquet"

    @property
    def structural_metrics_path(self) -> Path:
        return self.corpus_dir / "structural_metrics.parquet"

    @property
    def term_coupling_path(self) -> Path:
        return self.corpus_dir / "term_coupling.parquet"

    @property
    def namespace_usage_path(self) -> Path:
        """Distinct terms per (file, namespace) by usage mode (``ontometer_corpus.usage``)."""
        return self.corpus_dir / "namespace_usage.parquet"

    @property
    def ontology_metrics_path(self) -> Path:
        """Per-ontology metric table (``ontometer_corpus.ontology_metrics``)."""
        return self.corpus_dir / "ontology_metrics.parquet"

    @property
    def ontology_abstractness_path(self) -> Path:
        return self.corpus_dir / "ontology_abstractness.parquet"

    @property
    def concept_redeclarations_path(self) -> Path:
        """Class labels declared in several homes (``ontometer_corpus.network.redeclarations``)."""
        return self.corpus_dir / "concept_redeclarations.parquet"

    @property
    def ontology_dates_path(self) -> Path:
        return self.corpus_dir / "ontology_dates.parquet"

    @property
    def ontology_specialisations_path(self) -> Path:
        """Ontology-level specialisation pairs; with the two tables above, enough for G(t)."""
        return self.corpus_dir / "ontology_specialisations.parquet"

    @property
    def objects_index_path(self) -> Path:
        """One line per referenced object: hash, size, store-relative path."""
        return self.corpus_dir / "objects.tsv"

    @property
    def fail_log(self) -> Path:
        return self.corpus_dir / "fetch_failures.log"

    @property
    def dedup_log(self) -> Path:
        return self.corpus_dir / "dedup_exclusions.log"

    # --- object store ------------------------------------------------------

    @property
    def objects_dir(self) -> Path:
        """The raw ontology files, content-addressed: ``objects/<hh>/<sha256><ext>``."""
        return self.require_store_dir() / "objects"

    @property
    def structure_dir(self) -> Path:
        """Per-file structural sidecars, keyed by raw-byte ``file_hash``: ``cache/structure/v<N>``."""
        return self.cache_dir / "structure" / f"v{STRUCTURE_VERSION}"

    @property
    def cache_dir(self) -> Path:
        """Everything rebuildable from the raw files: ``<store>/cache``.

        Per-file sidecars and role tables, and the entity-level tables, which
        run to millions of rows — one per term, per role, per edge — and so stay
        out of the corpus, which only holds per-ontology tables small enough to
        commit.
        """
        return self.require_store_dir() / "cache"

    @property
    def roles_dir(self) -> Path:
        """Per-file term-role tables: ``cache/roles/v<N>``."""
        return self.cache_dir / "roles" / f"v{ROLES_VERSION}"

    def roles_path(self, file_hash: str) -> Path:
        """Sharded per-ontology term-role table: ``cache/roles/v<N>/<hh>/<hash>.parquet``."""
        return self.roles_dir / file_hash[:2] / f"{file_hash}.parquet"

    def cache_dirs(self) -> dict[str, Path]:
        """The versioned per-file caches, by kind."""
        return {"structure": self.structure_dir, "roles": self.roles_dir}

    @property
    def scratch_dir(self) -> Path:
        """Working space for the streaming parser.

        A sibling of the object store rather than a subdirectory of it, so a
        half-written temporary file can never be mistaken for a stored object.
        It is deliberately not the system temp directory: that is frequently a
        tmpfs, and streaming a multi-hundred-megabyte ontology through RAM is
        the failure this path exists to avoid.
        """
        return self.require_store_dir().parent / "_parse_scratch"

    @property
    def staging_dir(self) -> Path:
        """Where store downloads land until their hash is checked.

        Inside the store root (not under ``objects/``), so moving a checked file
        into place is an atomic rename on the same filesystem.
        """
        return self.require_store_dir() / ".staging"

    def require_store_dir(self) -> Path:
        """Return the object-store root, or explain why the run cannot proceed."""
        if self.store_dir is None:
            raise MissingStore(
                "This step needs the raw ontology files. Set --store or "
                f"the {STORE_ENV} environment variable to the object-store "
                "root (the directory containing `objects/`)."
            )
        return self.store_dir

    def sidecar_path(self, file_hash: str) -> Path:
        """Sharded sidecar location: ``cache/structure/v<N>/<hh>/<hash>.parquet``."""
        return self.structure_dir / file_hash[:2] / f"{file_hash}.parquet"

    def resolve_object(self, retrieved_file: str) -> Path:
        """Turn a stored ``retrieved_file`` into a path on this machine.

        Paths are recorded relative to the object-store root. An absolute path
        is used as-is if it exists; otherwise it is re-anchored at ``objects/``
        under the configured store.
        """
        candidate = Path(retrieved_file).expanduser()
        if candidate.is_absolute():
            if candidate.exists() or self.store_dir is None:
                return candidate
            marker = "objects/"
            text = str(candidate)
            index = text.find(marker)
            if index != -1:
                return self.require_store_dir() / text[index:]
            return candidate
        return self.require_store_dir() / candidate

    # --- construction ------------------------------------------------------

    def ensure_corpus_dir(self) -> Path:
        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        return self.corpus_dir

    @classmethod
    def resolve(
        cls,
        corpus_dir: str | Path | None = None,
        store_dir: str | Path | None = None,
    ) -> CorpusPaths:
        """Build from explicit values, falling back to the environment.

        Raises:
            MissingCorpus: Neither *corpus_dir* nor ``ONTOMETER_CORPUS`` is set.
        """
        data = corpus_dir or os.environ.get(CORPUS_ENV)
        if not data:
            raise MissingCorpus
        onto = store_dir or os.environ.get(STORE_ENV) or None
        return cls(
            corpus_dir=Path(data).expanduser(),
            store_dir=Path(onto).expanduser() if onto else None,
        )

    def describe(self) -> str:
        """One block naming every location, for a run's opening banner."""
        store = self.store_dir if self.store_dir else "(not configured)"
        return (
            f"  corpus       : {self.corpus_dir}\n"
            f"  corpus db    : {self.db_path}\n"
            f"  manifest     : {self.manifest_path}\n"
            f"  store        : {store}"
        )


def corpus_paths(command: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``--corpus`` and ``--store`` to a click command.

    The command receives a resolved :class:`CorpusPaths` as its ``paths``
    keyword argument instead of the two raw options. A flag wins over its
    environment variable (``ONTOMETER_CORPUS``, ``ONTOMETER_STORE``). Without
    a corpus directory the command exits with a usage error; without an object
    store it is unset.

    Args:
        command: The command callback, taking ``paths`` as a keyword argument.

    Returns:
        The callback with both options attached; apply ``@click.command()`` above it.
    """

    @click.option(
        "--corpus",
        "corpus_dir",
        type=click.Path(file_okay=False, path_type=Path),
        envvar=CORPUS_ENV,
        show_envvar=True,
        default=None,
        help=(
            "Corpus directory (a clone of the corpus repository) holding corpus.db, "
            "manifest.jsonl and the corpus tables. Required."
        ),
    )
    @click.option(
        "--store",
        "store_dir",
        type=click.Path(file_okay=False, path_type=Path),
        envvar=STORE_ENV,
        show_envvar=True,
        default=None,
        help="Object store containing objects/ and cache/.",
    )
    @functools.wraps(command)
    def wrapper(*args: Any, corpus_dir: Path | None, store_dir: Path | None, **kwargs: Any) -> Any:
        paths = CorpusPaths.resolve(corpus_dir=corpus_dir, store_dir=store_dir)
        return command(*args, paths=paths, **kwargs)

    return wrapper
