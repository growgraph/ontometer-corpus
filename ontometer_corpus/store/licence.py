"""Which objects may be redistributed, from their declared licences and a policy file.

Evidence, strongest first: the licences a file declares on its own header
(``corpus_registry.licence``, raw), then the registry's licence field. A
registry ``viewingRestriction`` other than ``public`` withholds, and so does a
capture that is not an ontology file. The policy (``corpus/redistribution.yaml``)
maps normalised licence ids to a decision with glob patterns; ``withhold``
patterns win over ``redistribute`` ones.
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.store.index import HASH_LEN, ObjectEntry, local_objects

#: Extensions of files that hold an ontology, after any compression suffix.
RDF_SUFFIXES = frozenset(
    {
        ".owl",
        ".rdf",
        ".ttl",
        ".n3",
        ".nt",
        ".nq",
        ".trig",
        ".jsonld",
        ".json",
        ".obo",
        ".xml",
        ".owx",
        ".ofn",
        ".omn",
    }
)
_WRAPPERS = (".gz", ".zip", ".bz2")

_CC_IRI = re.compile(r"creativecommons\.org/licenses/([a-z-]+)/(\d\.\d)")
_CC_ZERO = re.compile(r"creativecommons\.org/publicdomain/zero/(\d\.\d)|\bcc0\b")
_SPDX_IRI = re.compile(r"spdx\.org/licenses/([A-Za-z0-9.+-]+?)(?:\.html|\.json)?/?$")
_CC_LABEL = re.compile(r"\bcc[\s-]?(by(?:[\s-]nc)?(?:[\s-](?:sa|nd))?)[\s-]*(\d\.\d)?\b")
_CC_WORDS = re.compile(
    r"creative commons[\s-]+(?:by[\s-]+)?attribution"
    r"(?P<nc>[\s-]+non-?commercial)?"
    r"(?:[\s-]+(?P<sa>share[\s-]?alike)|[\s-]+(?P<nd>no-?deriv\w*))?"
    r"[\s-]*(?P<ver>\d\.\d)?"
)
#: (pattern on the lowercased text, licence id), tried in order after the CC rules.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"apache\.org/licenses/license-2\.0|apache[\s-]*(?:license)?[\s,-]*(?:version\s*)?2\.0"
        ),
        "Apache-2.0",
    ),
    (re.compile(r"opensource\.org/licenses/mit\b|\bmit[\s-]?license\b|^mit$"), "MIT"),
    (re.compile(r"nationalarchives\.gov\.uk/doc/open-government-licence/version/3"), "OGL-UK-3.0"),
    (re.compile(r"isa-open-metadata-licence-v1\.?1"), "ISA-OML-1.1"),
    (re.compile(r"etalab\.gouv\.fr/licence-ouverte"), "etalab-2.0"),
    # Commission Implementing Decision (EU) 2017/863 publishes EUPL v1.2.
    (re.compile(r"data\.europa\.eu/eli/dec_impl/2017/863|\beupl[\s-]*v?1\.2\b"), "EUPL-1.2"),
    (re.compile(r"artistic[\s-]*(?:license)?[\s-]*2\.0"), "Artistic-2.0"),
    (re.compile(r"\blgpl[\s-]*v?(?:2\.1|3)|gnu\.org/licenses/lgpl"), "LGPL"),
    (re.compile(r"gnu\.org/licenses/gpl-3\.0|\bgpl[\s-]*v?3(?:\.0)?\b"), "GPL-3.0"),
    (re.compile(r"gnu\.org/licenses/gpl-2\.0|\bgpl[\s-]*v?2(?:\.0)?\b"), "GPL-2.0"),
    (re.compile(r"bsd[\s-]*3[\s-]*clause"), "BSD-3-Clause"),
    (re.compile(r"bsd[\s-]*2[\s-]*clause"), "BSD-2-Clause"),
    (re.compile(r"\bodbl\b|opendatacommons\.org/licenses/odbl"), "ODbL-1.0"),
    (re.compile(r"\bodc[\s-]by\b|opendatacommons\.org/licenses/by"), "ODC-By-1.0"),
    (re.compile(r"\bpddl\b|opendatacommons\.org/licenses/pddl"), "PDDL-1.0"),
    (re.compile(r"w3\.org/consortium/legal/(?:\d{4}/)?(?:copyright-software|w3c-software)"), "W3C"),
    (re.compile(r"(?<!not in the )(?<!not )\bpublic domain\b"), "public-domain"),
)

#: Email addresses, for the personal-data line of the report.
_EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _cc_id(kind: str, version: str | None) -> str:
    parts = [p for p in re.split(r"[\s-]+", kind.upper()) if p]
    return "CC-" + "-".join(parts) + (f"-{version}" if version else "")


def normalise_licence(raw: str) -> str:
    """An SPDX-like id for a raw licence IRI or label, or ``other:<text>`` on one line."""
    text = " ".join(raw.split())
    low = text.lower()
    m = _CC_ZERO.search(low)
    if m:
        return f"CC0-{m.group(1) or '1.0'}"
    m = _CC_IRI.search(low)
    if m:
        return _cc_id(m.group(1), m.group(2))
    m = _SPDX_IRI.search(text)
    if m:
        return m.group(1)
    m = _CC_WORDS.search(low)
    if m:
        kind = "by" + ("-nc" if m.group("nc") else "")
        kind += "-sa" if m.group("sa") else "-nd" if m.group("nd") else ""
        return _cc_id(kind, m.group("ver"))
    m = _CC_LABEL.search(low)
    if m:
        return _cc_id(m.group(1), m.group(2))
    for pattern, licence_id in _RULES:
        if pattern.search(low):
            return licence_id
    if re.fullmatch(r"[A-Za-z0-9.+-]+-\d(\.\d)*", text):  # already an SPDX id
        return text
    return f"other:{text[:120]}"


@dataclass(frozen=True, slots=True)
class Policy:
    """Redistribution decisions over normalised licence ids (glob patterns)."""

    default: str
    unrecognised: str
    redistribute: tuple[str, ...]
    withhold: tuple[str, ...]

    def decide(self, licence_ids: tuple[str, ...]) -> tuple[bool, str]:
        """(redistribute?, reason) for an object declaring *licence_ids*."""
        if not licence_ids:
            return self.default == "redistribute", f"no licence declared: {self.default}"
        recognised = [i for i in licence_ids if not i.startswith("other:")]
        for licence_id in recognised:
            if any(fnmatch.fnmatchcase(licence_id, p) for p in self.withhold):
                return False, f"withhold: {licence_id}"
        for licence_id in recognised:
            if any(fnmatch.fnmatchcase(licence_id, p) for p in self.redistribute):
                return True, f"redistribute: {licence_id}"
        return self.unrecognised == "redistribute", f"unrecognised licence: {self.unrecognised}"


def load_policy(path: Path) -> Policy:
    """Read ``redistribution.yaml``."""
    data = yaml.safe_load(path.read_text())
    for key in ("default", "unrecognised"):
        if data.get(key) not in ("redistribute", "withhold"):
            raise ValueError(f"{path}: {key} must be 'redistribute' or 'withhold'")
    return Policy(
        default=data["default"],
        unrecognised=data["unrecognised"],
        redistribute=tuple(data.get("redistribute") or ()),
        withhold=tuple(data.get("withhold") or ()),
    )


@dataclass(frozen=True, slots=True)
class Verdict:
    """The redistribution decision for one object."""

    file_hash: str
    path: str
    licence_ids: tuple[str, ...]
    evidence: str  # "file" | "registry" | "none"
    redistribute: bool
    reason: str


def registry_licences(metadata: dict) -> list[str]:
    """Raw licence values from projected registry metadata (OBO, LOV, OntoPortal)."""
    out: list[str] = []
    for key in ("license", "hasLicense"):
        value = metadata.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                out.extend(str(item[k]) for k in ("url", "label") if item.get(k))
            elif isinstance(item, str) and item.strip():
                out.append(item)
    return out


def _looks_like_html(path: Path) -> bool:
    """An HTML page saved under an ontology suffix (a registry error page)."""
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rb") as handle:
            head = handle.read(2048)
    except OSError:
        return False
    head = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1][HASH_LEN:].lower()
    for wrapper in _WRAPPERS:
        if name.endswith(wrapper):
            name = name[: -len(wrapper)]
    return name


def verdicts(paths: CorpusPaths, policy: Policy) -> list[Verdict]:
    """One verdict per object, over every corpus row that references it."""
    conn = sqlite3.connect(f"file:{paths.db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_xinfo(corpus_registry)")}
        # A database parsed before licence capture existed has no column: no file evidence.
        licence = "licence" if "licence" in columns else "NULL"
        rows = conn.execute(
            f"SELECT file_hash, retrieved_file, {licence}, source_registry_metadata, parse_status "
            "FROM corpus_registry WHERE file_hash IS NOT NULL AND file_hash != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    local = local_objects(paths) if paths.store_dir is not None else {}
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)
    out = []
    for digest in sorted(grouped):
        group = grouped[digest]
        path = min(r[1] for r in group)
        path = path[path.find("objects/") :] if "objects/" in path else path
        file_values: list[str] = []
        registry_values: list[str] = []
        restricted = False
        parsed = False
        for _, _, licence, metadata_json, parse_status in group:
            file_values += json.loads(licence) if licence else []
            metadata = json.loads(metadata_json) if metadata_json else {}
            registry_values += registry_licences(metadata)
            restriction = (metadata.get("ontology") or {}).get("viewingRestriction")
            restricted |= restriction not in (None, "public")
            parsed |= parse_status == "ok"
        values, evidence = (
            (file_values, "file")
            if file_values
            else (registry_values, "registry")
            if registry_values
            else ([], "none")
        )
        ids = tuple(dict.fromkeys(normalise_licence(v) for v in values))
        if restricted:
            allowed, reason = False, "registry viewingRestriction is not public"
        elif (_suffix(path) not in RDF_SUFFIXES and not parsed) or (
            digest in local and _looks_like_html(local[digest])
        ):
            allowed, reason = False, "not an ontology file"
        else:
            allowed, reason = policy.decide(ids)
        out.append(Verdict(digest, path, ids, evidence, allowed, reason))
    return out


def email_counts(paths: CorpusPaths, entries: list[ObjectEntry]) -> dict[str, int]:
    """Distinct email addresses in each object's bytes (gzip objects decompressed)."""
    local = local_objects(paths)
    counts: dict[str, int] = {}
    for entry in entries:
        found = local.get(entry.file_hash)
        if found is None:
            continue
        opener = gzip.open if found.name.endswith(".gz") else open
        seen: set[bytes] = set()
        tail = b""
        with opener(found, "rb") as handle:
            while chunk := handle.read(1 << 20):
                block = tail + chunk
                seen.update(_EMAIL.findall(block))
                tail = block[-256:]
        counts[entry.file_hash] = len(seen)
    return counts
