"""release.toml: the table release a corpus directory holds, and the format the readers accept."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ontometer_corpus.release import (
    RELEASE_FILE,
    TABLES_FORMAT,
    ReleaseError,
    parse_release,
    read_release,
    release_at_tag,
    require_readable,
)


def write_release(corpus_dir: Path, name: str, fmt: int = TABLES_FORMAT) -> None:
    """Declare *corpus_dir* as release *name* (``corpus-YYYY-MM[.N]``) in table format *fmt*."""
    crawl = name.removeprefix("corpus-").split(".")[0]
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / RELEASE_FILE).write_text(
        f'release = "{name}"\ncrawl = "{crawl}"\nformat = {fmt}\n'
    )


def test_a_rederivation_of_a_crawl_keeps_its_month(tmp_path):
    write_release(tmp_path, "corpus-2026-08.1")

    release = require_readable(tmp_path)

    assert (release.name, release.crawl, release.format) == ("corpus-2026-08.1", "2026-08", 1)


def test_a_directory_without_release_is_refused(tmp_path):
    with pytest.raises(ReleaseError, match="names no table release"):
        read_release(tmp_path)


def test_another_table_format_is_refused(tmp_path):
    write_release(tmp_path, "corpus-2026-08", fmt=TABLES_FORMAT + 1)

    with pytest.raises(ReleaseError, match=f"reads format {TABLES_FORMAT}"):
        require_readable(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('release = "v0.1.0"\ncrawl = "2026-08"\nformat = 1\n', "corpus-YYYY-MM"),
        ('release = "corpus-2026-13"\ncrawl = "2026-13"\nformat = 1\n', "corpus-YYYY-MM"),
        ('release = "corpus-2026-08.0"\ncrawl = "2026-08"\nformat = 1\n', "corpus-YYYY-MM"),
        ('release = "corpus-2026-08"\ncrawl = "2026-09"\nformat = 1\n', "does not match"),
        ('release = "corpus-2026-08"\ncrawl = "2026-08"\nformat = true\n', "positive integer"),
        ('release = "corpus-2026-08"\ncrawl = "2026-08"\n', "positive integer"),
        ("release = ", "release.toml"),
    ],
)
def test_malformed_release_is_refused(text, message):
    with pytest.raises(ReleaseError, match=message):
        parse_release(text, "release.toml")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        capture_output=True,
    )


def test_a_tag_must_cut_the_release_it_names(tmp_path):
    data = tmp_path / "data"
    write_release(data, "corpus-2026-08")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "data")
    _git(tmp_path, "commit", "-qm", "tables")
    _git(tmp_path, "tag", "corpus-2026-08")
    _git(tmp_path, "tag", "corpus-2026-08.1")

    assert release_at_tag(data, "corpus-2026-08").name == "corpus-2026-08"
    with pytest.raises(ReleaseError, match="holds release.toml for corpus-2026-08"):
        release_at_tag(data, "corpus-2026-08.1")


def test_a_tag_without_release_is_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "objects.tsv").write_text("")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "data")
    _git(tmp_path, "commit", "-qm", "tables")
    _git(tmp_path, "tag", "corpus-2026-08")

    with pytest.raises(ReleaseError, match="has no release.toml"):
        release_at_tag(data, "corpus-2026-08")


TRACKED = Path(__file__).resolve().parents[1] / "data"


@pytest.mark.skipif(not TRACKED.is_dir(), reason="the sdist ships no tables")
def test_the_tracked_tables_are_readable():
    assert require_readable(TRACKED).format == TABLES_FORMAT
