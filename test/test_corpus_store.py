"""Stored corpus keys and formats: object-store hashes, content hashes,
timestamps and manifest reading must not drift, since corpus.db and
manifest.jsonl already hold values produced by them."""

from __future__ import annotations

import re
from pathlib import Path

import rdflib
from rdflib import OWL, RDF, URIRef

from ontometer_corpus.fetchers._base import content_hash, now_utc_iso, store_object
from ontometer_corpus.pipeline.db import _now, create_schema, get_connection, seed_from_manifest
from ontometer_corpus.pipeline.parse import compute_content_hash

_ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00$")


def test_object_store_key_is_sha256_of_bytes():
    assert (
        content_hash(b"ontology")
        == "c205f711aa5d877300e2cdbcb5d060be2e0de276e525c020356210df8836e8e9"
    )


def test_store_object_is_content_addressed(tmp_path: Path):
    path, h = store_object(tmp_path, b"ontology", ".ttl")
    assert h == content_hash(b"ontology")
    assert path == tmp_path / h[:2] / f"{h}.ttl"
    assert path.read_bytes() == b"ontology"
    assert list(path.parent.iterdir()) == [path]


def test_content_hash_is_sha256_of_sorted_iris():
    g = rdflib.Graph()
    g.add((URIRef("http://example.org/p"), RDF.type, OWL.ObjectProperty))
    g.add((URIRef("http://example.org/A"), RDF.type, OWL.Class))
    assert (
        compute_content_hash(g)
        == "71f8df3cdb1a14a9b75c5632c0c0be68c94ea9ed46b2c3a10d500e4d23080f0a"
    )


def test_timestamps_keep_offset_and_microseconds():
    for stamp in (now_utc_iso(), _now()):
        assert _ISO_UTC.match(stamp), stamp


def test_seed_skips_bad_manifest_lines(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"source_id": "a", "source_registry": "x", "status": "ok", '
        '"retrieved_file": "aa/a.ttl"}\n'
        "\n"
        "{not json\n"
        '{"source_id": "b", "source_registry": "x", "status": "fail"}\n',
        encoding="utf-8",
    )
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    counts = seed_from_manifest(conn, manifest)
    assert counts == {"corpus_inserted": 1, "corpus_skipped": 0, "attempts_inserted": 2}
