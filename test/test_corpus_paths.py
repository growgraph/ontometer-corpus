"""Path resolution — the one place the corpus pipeline decides where things live."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from ontometer_corpus import paths as corpus_paths_module
from ontometer_corpus.paths import (
    CORPUS_ENV,
    ROLES_VERSION,
    STORE_ENV,
    STRUCTURE_VERSION,
    CorpusPaths,
    MissingCorpus,
    MissingStore,
    corpus_paths,
)


def test_corpus_is_required(monkeypatch):
    """No ./data default: a forgotten flag must not start an empty corpus in the cwd."""
    monkeypatch.delenv("ONTOMETER_CORPUS", raising=False)
    with pytest.raises(MissingCorpus, match="--corpus"):
        CorpusPaths.resolve()


def test_command_without_corpus_is_a_usage_error(monkeypatch):
    monkeypatch.delenv("ONTOMETER_CORPUS", raising=False)

    @click.command()
    @corpus_paths
    def show(paths):
        click.echo(str(paths.corpus_dir))

    result = CliRunner().invoke(show, [])
    assert result.exit_code == 2
    assert "ONTOMETER_CORPUS" in result.output


def test_environment_is_read(monkeypatch):
    monkeypatch.setenv("ONTOMETER_CORPUS", "/tmp/d")
    monkeypatch.setenv("ONTOMETER_STORE", "/tmp/o")
    paths = CorpusPaths.resolve()
    assert paths.corpus_dir == Path("/tmp/d")
    assert paths.store_dir == Path("/tmp/o")


def test_explicit_values_beat_the_environment(monkeypatch):
    monkeypatch.setenv("ONTOMETER_CORPUS", "/tmp/from-env")
    assert CorpusPaths.resolve(corpus_dir="/tmp/explicit").corpus_dir == Path("/tmp/explicit")


def test_derived_artifact_paths():
    paths = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o"))
    assert paths.db_path == Path("/d/corpus.db")
    assert paths.manifest_path == Path("/d/manifest.jsonl")
    assert paths.network_nodes_path == Path("/d/network_nodes.parquet")
    assert paths.objects_dir == Path("/o/objects")
    assert paths.cache_dir == Path("/o/cache")
    assert paths.structure_dir == Path(f"/o/cache/structure/v{STRUCTURE_VERSION}")
    # Scratch is a sibling of the store, never inside it: a half-written
    # temporary file must not be mistakable for a stored object.
    assert paths.scratch_dir == Path("/_parse_scratch")
    # Staging shares the store's filesystem, so moving a checked download in is atomic.
    assert paths.staging_dir == Path("/o/.staging")


def test_sidecar_path_includes_structure_version():
    paths = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o"))
    assert paths.sidecar_path("abcdef") == Path(
        f"/o/cache/structure/v{STRUCTURE_VERSION}/ab/abcdef.parquet"
    )


def test_roles_path_includes_roles_version():
    paths = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o"))
    assert paths.roles_path("abcdef") == Path(f"/o/cache/roles/v{ROLES_VERSION}/ab/abcdef.parquet")


def test_cache_dirs_name_both_versioned_caches():
    paths = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o"))
    assert paths.cache_dirs() == {"structure": paths.structure_dir, "roles": paths.roles_dir}


def test_bumped_version_hides_old_caches(tmp_path, monkeypatch):
    paths = CorpusPaths(corpus_dir=tmp_path, store_dir=tmp_path / "store")
    old = paths.sidecar_path("abcdef")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    monkeypatch.setattr(corpus_paths_module, "STRUCTURE_VERSION", STRUCTURE_VERSION + 1)
    assert not paths.sidecar_path("abcdef").exists()


def test_parse_sidecar_path_matches_corpus_paths(tmp_path):
    from ontometer_corpus.pipeline import parse

    paths = CorpusPaths(corpus_dir=tmp_path, store_dir=tmp_path / "store")
    parse.configure_paths(paths)
    try:
        assert parse._sidecar_path("abcdef") == paths.sidecar_path("abcdef")
    finally:
        parse.configure_paths(None)


def test_object_store_is_required_with_a_useful_message():
    paths = CorpusPaths(corpus_dir=Path("/d"))
    with pytest.raises(MissingStore) as excinfo:
        _ = paths.objects_dir
    assert "--store" in str(excinfo.value)


def test_relative_object_paths_resolve_against_the_store():
    paths = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o"))
    assert paths.resolve_object("objects/ab/abc.ttl") == Path("/o/objects/ab/abc.ttl")


def test_legacy_absolute_paths_are_re_anchored(tmp_path):
    """Databases written before the change hold another machine's absolute paths."""
    store = tmp_path / "store"
    target = store / "objects" / "ab" / "abc.ttl"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    paths = CorpusPaths(corpus_dir=tmp_path, store_dir=store)
    stale = "/home/someone-else/data/Ontometer/ontologies/objects/ab/abc.ttl"
    assert paths.resolve_object(stale) == target


def test_an_absolute_path_that_exists_is_left_alone(tmp_path):
    real = tmp_path / "somewhere.ttl"
    real.write_text("x")
    paths = CorpusPaths(corpus_dir=tmp_path, store_dir=tmp_path / "store")
    assert paths.resolve_object(str(real)) == real


def _paths_from_cli(args: list[str], env: dict[str, str] | None = None) -> CorpusPaths:
    seen: list[CorpusPaths] = []

    @click.command()
    @corpus_paths
    def command(paths: CorpusPaths) -> None:
        seen.append(paths)

    result = CliRunner(env=env).invoke(command, args)
    assert result.exit_code == 0, result.output
    return seen[0]


def test_click_options_resolve_to_corpus_paths():
    paths = _paths_from_cli(["--corpus", "/a", "--store", "/b"])
    assert paths.corpus_dir == Path("/a")
    assert paths.store_dir == Path("/b")


def test_click_options_fall_back_to_the_environment_and_flags_win():
    env = {CORPUS_ENV: "/env-data", STORE_ENV: "/env-store"}
    assert _paths_from_cli([], env).store_dir == Path("/env-store")
    assert _paths_from_cli(["--corpus", "/flag"], env).corpus_dir == Path("/flag")


def test_describe_names_every_location():
    text = CorpusPaths(corpus_dir=Path("/d"), store_dir=Path("/o")).describe()
    assert "/d/corpus.db" in text
    assert "/o" in text


def test_describe_is_honest_when_no_store_is_configured():
    assert "not configured" in CorpusPaths(corpus_dir=Path("/d")).describe()
