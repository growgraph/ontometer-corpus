"""
Reuse networks of other kinds, to set the corpus's reuse inequality against.

Part of the resource paper's analysis. Each comparator is a public
dependency or link list in which a node, like an ontology, is built by one
party and reused by others:

``cran``
    CRAN packages; ``A -> B`` when A lists B under ``Depends``, ``Imports`` or
    ``LinkingTo``.
``debian``
    Debian binary packages of one release (``main``, amd64); ``A -> B`` when
    A lists B under ``Depends`` or ``Pre-Depends`` (the first alternative of a
    choice; a virtual package resolves to its first provider).
``lod``
    The Linked Open Data cloud's datasets; ``A -> B`` when A declares links
    to B.

Edges point from the user to the used, as in the corpus's reuse network, so a
node's in-degree is how many others reuse it. Only edges between two listed
nodes are kept, each once, without self-loops.

``fetch`` downloads each source once into ``<dir>/raw/`` and records its URL,
retrieval date, size and SHA-256 in ``<dir>/manifest.json``; a file already
there is kept, so the comparison stays on one snapshot. ``load`` reads the raw
files only.

Usage:
    ontometer-corpus paper comparators fetch --dir DIR
    ontometer-corpus paper comparators summary --dir DIR
"""

from __future__ import annotations

import hashlib
import json
import lzma
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click
import requests

DEBIAN_RELEASE = "trixie"

SOURCES = {
    "cran": ("https://cran.r-project.org/src/contrib/PACKAGES", "cran-PACKAGES"),
    "debian": (
        f"https://deb.debian.org/debian/dists/{DEBIAN_RELEASE}/main/binary-amd64/Packages.xz",
        f"debian-{DEBIAN_RELEASE}-Packages.xz",
    ),
    "lod": ("https://lod-cloud.net/lod-data.json", "lod-data.json"),
}

#: How each comparator is named in figures and tables.
LABELS = {
    "cran": "CRAN packages",
    "debian": "Debian packages",
    "lod": "LOD cloud datasets",
}


@dataclass(frozen=True)
class ReuseNetwork:
    """Nodes and distinct user -> used edges between them."""

    name: str
    nodes: frozenset[str]
    edges: frozenset[tuple[str, str]]

    def in_degrees(self) -> list[int]:
        """How many distinct others reuse each node, zeros included."""
        deg = dict.fromkeys(self.nodes, 0)
        for _, used in self.edges:
            deg[used] += 1
        return list(deg.values())

    def out_degrees(self) -> list[int]:
        deg = dict.fromkeys(self.nodes, 0)
        for user, _ in self.edges:
            deg[user] += 1
        return list(deg.values())


def _network(name: str, nodes: set[str], pairs) -> ReuseNetwork:
    edges = {(a, b) for a, b in pairs if a != b and a in nodes and b in nodes}
    return ReuseNetwork(name, frozenset(nodes), frozenset(edges))


def _stanzas(text: str) -> list[dict[str, str]]:
    """Debian control format: blank-line separated stanzas, folded continuation lines."""
    out = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields:
            out.append(fields)
    return out


_VERSION = re.compile(r"\(.*?\)|\[.*?\]|<.*?>")


def _names(field: str) -> list[str]:
    """Package names of a dependency field: versions, architectures and profiles dropped."""
    names = []
    for item in field.split(","):
        first = _VERSION.sub("", item.split("|")[0]).strip()
        name = first.split(":")[0].strip()
        if name:
            names.append(name)
    return names


def load_cran(path: Path) -> ReuseNetwork:
    stanzas = _stanzas(path.read_text(encoding="utf-8", errors="replace"))
    nodes = {s["Package"] for s in stanzas if "Package" in s}
    pairs = [
        (s["Package"], dep)
        for s in stanzas
        if "Package" in s
        for field in ("Depends", "Imports", "LinkingTo")
        for dep in _names(s.get(field, ""))
    ]
    return _network("cran", nodes, pairs)


def load_debian(path: Path) -> ReuseNetwork:
    with lzma.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        stanzas = _stanzas(fh.read())
    nodes = {s["Package"] for s in stanzas if "Package" in s}
    provider: dict[str, str] = {}
    for s in stanzas:
        for virtual in _names(s.get("Provides", "")):
            provider.setdefault(virtual, s["Package"])
    pairs = [
        (s["Package"], dep if dep in nodes else provider.get(dep, dep))
        for s in stanzas
        if "Package" in s
        for field in ("Depends", "Pre-Depends")
        for dep in _names(s.get(field, ""))
    ]
    return _network("debian", nodes, pairs)


def load_lod(path: Path) -> ReuseNetwork:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = set(data)
    pairs = [
        (source, link["target"])
        for source, entry in data.items()
        for link in entry.get("links") or []
        if isinstance(link, dict) and link.get("target")
    ]
    return _network("lod", nodes, pairs)


LOADERS = {"cran": load_cran, "debian": load_debian, "lod": load_lod}


def load(directory: Path) -> dict[str, ReuseNetwork]:
    """Every comparator whose raw file is in ``<directory>/raw``."""
    raw = directory / "raw"
    return {
        name: LOADERS[name](raw / filename)
        for name, (_, filename) in SOURCES.items()
        if (raw / filename).exists()
    }


@click.group()
def main() -> None:
    """Fetch and summarise the comparator reuse networks."""


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def fetch(directory: Path) -> None:
    """Download every source not yet in DIR/raw and record it in DIR/manifest.json."""
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for name, (url, filename) in SOURCES.items():
        target = raw / filename
        if not target.exists():
            resp = requests.get(url, timeout=300)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            manifest[name] = {
                "url": url,
                "file": filename,
                "retrieved": datetime.now(UTC).strftime("%Y-%m-%d"),
            }
        data = target.read_bytes()
        entry = manifest.setdefault(name, {"url": url, "file": filename, "retrieved": None})
        entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        print(f"  {name}: {filename} ({len(data):,} bytes, retrieved {entry['retrieved']})")
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def summary(directory: Path) -> None:
    """Nodes, edges and the share never reused, per comparator."""
    for name, net in load(directory).items():
        ins = net.in_degrees()
        zero = sum(v == 0 for v in ins) / len(ins)
        print(
            f"  {name}: {len(net.nodes):,} nodes, {len(net.edges):,} edges, "
            f"max in-degree {max(ins):,}, never reused {zero:.1%}"
        )


if __name__ == "__main__":
    main()
