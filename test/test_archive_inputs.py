"""Archive containers must expose ontology content, not package metadata."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

from ontometer_corpus.pipeline.parse import (
    _pick_zip_member,
    estimate_real_size,
    parse_worker,
)
from ontometer_corpus.steps.term_coupling import _process_one

TURTLE = b"""
@prefix ex: <https://example.org/onto#> .
@prefix ext: <https://example.net/vocab#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<https://example.org/onto> a owl:Ontology .
ex:Local a owl:Class ; rdfs:subClassOf ext:External .
"""


def test_zip_member_rejects_office_package_metadata() -> None:
    names = [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/worksheets/sheet1.xml",
        "customXml/item1.xml",
    ]

    assert _pick_zip_member(names) is None


def test_zip_member_accepts_supported_nested_ontology_and_nquads() -> None:
    assert _pick_zip_member(["README.txt", "export/model.owl"]) == "export/model.owl"
    assert _pick_zip_member(["dump.nq"]) == "dump.nq"


def test_parse_worker_marks_office_package_unsupported(tmp_path: Path) -> None:
    workbook = tmp_path / "not-an-ontology.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")

    _, status, fmt, meta, error = parse_worker(
        (1, str(workbook), None, "okg:Q1", "raw-file-hash", str(tmp_path))
    )

    assert status == "unsupported"
    assert fmt is None and meta is None
    assert error == "Zip file contains no supported ontology member"


def test_term_coupling_reads_zip_payload(tmp_path: Path) -> None:
    archive_path = tmp_path / "ontology.rdf"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested/ontology.ttl", TURTLE)

    _, kind, member = estimate_real_size(archive_path)
    record = _process_one(archive_path, {"https://example.org/onto#"}, "turtle", kind, member)

    assert record["n_terms_logical"] == 2
    assert record["n_ext_logical"] == 1


def test_term_coupling_reads_gzip_payload(tmp_path: Path) -> None:
    archive_path = tmp_path / "ontology.ttl"
    with gzip.open(archive_path, "wb") as archive:
        archive.write(TURTLE)

    _, kind, member = estimate_real_size(archive_path)
    record = _process_one(archive_path, {"https://example.org/onto#"}, "turtle", kind, member)

    assert record["n_terms_logical"] == 2
    assert record["n_ext_logical"] == 1


def test_parse_worker_takes_paths_from_the_task_not_the_environment(
    tmp_path: Path, monkeypatch
) -> None:
    """A spawned worker inherits nothing; --corpus given as a flag must still reach it."""
    monkeypatch.delenv("ONTOMETER_CORPUS", raising=False)
    store = tmp_path / "store"
    source = store / "objects" / "ab" / "onto.ttl"
    source.parent.mkdir(parents=True)
    source.write_bytes(TURTLE)

    _, status, *_ = parse_worker(
        (1, str(source), str(store), None, "ab" + "0" * 62, str(tmp_path / "data"))
    )

    assert status == "ok"


def test_parse_worker_fails_an_empty_file(tmp_path: Path) -> None:
    """Zero bytes hold no ontology; an `ok` would let the row into dedup and the network."""
    empty = tmp_path / "e3.api"
    empty.write_bytes(b"")

    _, status, fmt, meta, error = parse_worker((1, str(empty), None, None, "e3", str(tmp_path)))

    assert status == "fail"
    assert fmt is None and meta is None
    assert error == "Empty file"
