"""The table release a corpus directory holds, and the table format this package reads.

A corpus directory names its release in ``release.toml``::

    release = "corpus-2026-08.1"   # the git tag that cuts it
    crawl = "2026-08"              # month of the crawl the tables derive from
    format = 1                     # layout of the tables

A release is tagged ``corpus-YYYY-MM`` after the month of its crawl, and
``corpus-YYYY-MM.N`` for a later derivation of the same crawl. ``format`` changes
whenever a table, column or meaning changes in a way an older reader would misread;
the table readers refuse any format but :data:`TABLES_FORMAT`.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

#: The table format this version of the package reads and writes.
TABLES_FORMAT = 1

#: File in the corpus directory that names its release.
RELEASE_FILE = "release.toml"

#: ``corpus-YYYY-MM`` or ``corpus-YYYY-MM.N``.
RELEASE_TAG = re.compile(r"^corpus-(?P<crawl>\d{4}-(0[1-9]|1[0-2]))(\.(?P<n>[1-9]\d*))?$")


class ReleaseError(click.ClickException):
    """Raised when a corpus directory names no release, or one this package cannot read."""


@dataclass(frozen=True, slots=True)
class Release:
    """What ``release.toml`` declares."""

    name: str
    crawl: str
    format: int


def parse_release(text: str, source: str) -> Release:
    """Parse and check the content of a ``release.toml``; *source* names it in errors.

    Raises:
        ReleaseError: a field is missing or malformed, or the name and crawl disagree.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{source}: {exc}") from exc
    name, crawl, fmt = data.get("release"), data.get("crawl"), data.get("format")
    if not isinstance(name, str) or not (m := RELEASE_TAG.match(name)):
        raise ReleaseError(f"{source}: release must be corpus-YYYY-MM or corpus-YYYY-MM.N")
    if crawl != m["crawl"]:
        raise ReleaseError(f"{source}: crawl {crawl!r} does not match release {name}")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
        raise ReleaseError(f"{source}: format must be a positive integer")
    return Release(name=name, crawl=m["crawl"], format=fmt)


def read_release(corpus_dir: Path) -> Release:
    """The release *corpus_dir* declares.

    Raises:
        ReleaseError: there is no ``release.toml``, or it is malformed.
    """
    path = corpus_dir / RELEASE_FILE
    if not path.is_file():
        raise ReleaseError(
            f"{path} is missing: the directory names no table release. Tables released before "
            f"format 1 (corpus-2026-08) are not readable by this package; a new crawl starts "
            f"by writing {RELEASE_FILE}."
        )
    return parse_release(path.read_text(), str(path))


def release_at_tag(corpus_dir: Path, tag: str) -> Release:
    """The release git tag *tag* cuts, once its ``release.toml`` names that same tag.

    Raises:
        ReleaseError: the tag has no ``release.toml``, or it names another release.
    """
    shown = subprocess.run(
        ["git", "-C", str(corpus_dir), "show", f"{tag}:./{RELEASE_FILE}"],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        raise ReleaseError(f"{tag} has no {RELEASE_FILE} in {corpus_dir}")
    release = parse_release(shown.stdout, f"{tag}:{RELEASE_FILE}")
    if release.name != tag:
        raise ReleaseError(f"tag {tag} holds {RELEASE_FILE} for {release.name}")
    return release


def require_readable(corpus_dir: Path) -> Release:
    """The release of *corpus_dir*, once its table format is the one this package reads.

    Raises:
        ReleaseError: no release is declared, or its format differs from TABLES_FORMAT.
    """
    release = read_release(corpus_dir)
    if release.format != TABLES_FORMAT:
        raise ReleaseError(
            f"{release.name} has table format {release.format}; this version of "
            f"ontometer-corpus reads format {TABLES_FORMAT}; CHANGELOG.md lists the format "
            f"each package release reads."
        )
    return release
