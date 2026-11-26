"""Fetched objects are recorded relative to the store root."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import requests

from ontometer_corpus.fetchers import _base
from ontometer_corpus.paths import CorpusPaths

_TTL = b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<http://ex.org/o> a owl:Ontology .\n"


def test_download_records_a_store_relative_path(tmp_path: Path, monkeypatch) -> None:
    response = SimpleNamespace(
        content=_TTL,
        text=_TTL.decode(),
        url="http://ex.org/o.ttl",
        headers={"Content-Type": "text/turtle"},
    )
    monkeypatch.setattr(_base, "fetch_url", lambda session, url, *a, **k: response)
    store = tmp_path / "store"
    objects = store / "objects"

    ok, saved, file_hash, error, _ = _base.download_ontology(
        requests.Session(), "http://ex.org/o.ttl", objects, "test", tmp_path / "fail.log"
    )

    assert ok and not error
    assert saved.startswith("objects/") and not Path(saved).is_absolute()
    resolved = CorpusPaths(corpus_dir=tmp_path, store_dir=store).resolve_object(saved)
    assert resolved.read_bytes() == _TTL
    assert file_hash in saved


def test_failures_never_record_an_api_key(tmp_path: Path, monkeypatch) -> None:
    def refuse(session, url, *a, **k):
        raise RuntimeError(f"404 Client Error: Not Found for url: {url}")

    monkeypatch.setattr(_base, "fetch_url", refuse)
    fail_log = tmp_path / "fetch_failures.log"
    url = "https://data.example.org/ontologies/X/submissions/3/download?apikey=s3cr3t&x=1"

    ok, _, _, error, _ = _base.download_ontology(
        requests.Session(), url, tmp_path / "objects", "t", fail_log
    )
    record = _base.FetchRecord.failure(
        source_id="t:X::3",
        canonical_url=url,
        retrieval_date="2026-01-01T00:00:00Z",
        source_registry="T",
        source_registry_metadata={},
        error=error,
    )
    manifest = tmp_path / "manifest.jsonl"
    _base.append_record(record, manifest)

    assert not ok
    for text in (error, fail_log.read_text(), manifest.read_text()):
        assert "s3cr3t" not in text and "apikey=REDACTED" in text
