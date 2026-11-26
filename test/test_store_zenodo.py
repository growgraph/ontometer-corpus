"""Zenodo drafts for archived bundles; publishing stays a manual step (fake session)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ontometer_corpus.paths import CORPUS_ENV
from ontometer_corpus.store.cli import cli
from ontometer_corpus.store.zenodo import create_draft, load_metadata

BASE = "https://sandbox.zenodo.org"
CORPUS_METADATA = Path(os.environ[CORPUS_ENV]) / "zenodo.yaml" if CORPUS_ENV in os.environ else None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _response(self, payload: dict | None = None, status: int = 200):
        return SimpleNamespace(status_code=status, json=lambda: payload or {}, text="")

    def post(self, url, json=None, headers=None, **_):
        self.calls.append(("POST", url))
        if url.endswith("/actions/newversion"):
            return self._response(
                {"links": {"latest_draft": f"{BASE}/api/deposit/depositions/77"}}, 201
            )
        return self._response(
            {"id": 55, "links": {"bucket": f"{BASE}/api/files/b55", "html": f"{BASE}/deposit/55"}},
            201,
        )

    def get(self, url, headers=None, **_):
        self.calls.append(("GET", url))
        return self._response(
            {"id": 77, "links": {"bucket": f"{BASE}/api/files/b77", "html": f"{BASE}/deposit/77"}}
        )

    def put(self, url, data=None, json=None, headers=None, **_):
        self.calls.append(("PUT", url))
        if hasattr(data, "read"):
            data.read()
        return self._response({})


def _files(tmp_path: Path, names: list[str]) -> list[Path]:
    out = []
    for name in names:
        f = tmp_path / name
        f.write_bytes(b"x")
        out.append(f)
    return out


def test_new_record_uploads_bundle_hashes_and_report(tmp_path):
    session = FakeSession()
    files = _files(tmp_path, ["objects-a.tar.zst", "objects-a.sha256", "licences-a.tsv"])

    draft = create_draft(files, {"title": "t"}, token="tok", base_url=BASE, session=session)

    assert draft.id == 55
    assert draft.html.endswith("/deposit/55")
    assert ("POST", f"{BASE}/api/deposit/depositions") in session.calls
    for f in files:
        assert ("PUT", f"{BASE}/api/files/b55/{f.name}") in session.calls
    assert ("PUT", f"{BASE}/api/deposit/depositions/55") in session.calls


def test_new_version_keeps_previous_files_and_adds_delta(tmp_path):
    session = FakeSession()
    files = _files(tmp_path, ["objects-b-since-a.tar.zst"])

    draft = create_draft(
        files, {"title": "t"}, token="tok", base_url=BASE, session=session, record=55
    )

    assert draft.id == 77
    assert session.calls[0] == ("POST", f"{BASE}/api/deposit/depositions/55/actions/newversion")
    assert ("PUT", f"{BASE}/api/files/b77/objects-b-since-a.tar.zst") in session.calls
    assert not any(method == "DELETE" for method, _ in session.calls)


def test_never_calls_publish(tmp_path):
    session = FakeSession()
    create_draft(
        _files(tmp_path, ["a"]), {"title": "t"}, token="tok", base_url=BASE, session=session
    )
    create_draft(
        _files(tmp_path, ["b"]),
        {"title": "t"},
        token="tok",
        base_url=BASE,
        session=session,
        record=1,
    )
    assert not any("publish" in url for _, url in session.calls)


@pytest.mark.skipif(
    CORPUS_METADATA is None or not CORPUS_METADATA.exists(),
    reason="no corpus directory (set ONTOMETER_CORPUS)",
)
def test_shipped_metadata_gets_the_version(tmp_path):
    assert CORPUS_METADATA is not None
    metadata = load_metadata(CORPUS_METADATA, "corpus-2026-08")
    assert metadata["version"] == "corpus-2026-08"
    assert metadata["upload_type"] == "dataset"
    assert metadata["creators"]


def test_missing_token_message(tmp_path, monkeypatch):
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    result = CliRunner().invoke(cli, ["zenodo", "--rev", "corpus-x", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "ZENODO_TOKEN" in result.output
