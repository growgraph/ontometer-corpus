"""Archive lookups: which answers are final, which are retried."""

from __future__ import annotations

from typing import cast

import pytest
import requests

import ontometer_corpus.steps.ontology_dates as od


class _Resp:
    def __init__(self, status: int, payload: object = None) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        self._payload = payload

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *responses: _Resp) -> None:
        self._responses = list(responses)
        self.calls = 0

    def get(self, *args: object, **kwargs: object) -> _Resp:
        self.calls += 1
        return self._responses.pop(0)


def _capture(session: _Session, url: str) -> str | None:
    return od._first_capture(cast(requests.Session, session), url)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(od.time, "sleep", lambda seconds: None)


def test_client_error_is_a_final_no_capture_answer() -> None:
    session = _Session(_Resp(403))
    assert _capture(session, "blocked.example.org") is None
    assert session.calls == 1


def test_gateway_errors_are_retried_then_unreachable() -> None:
    session = _Session(*(_Resp(502) for _ in range(4)))
    with pytest.raises(od._Unreachable):
        _capture(session, "ex.org")
    assert session.calls == 4


def test_a_retried_gateway_error_can_still_succeed() -> None:
    session = _Session(_Resp(504), _Resp(200, [["timestamp"], ["20150102030405"]]))
    assert _capture(session, "ex.org") == "2015-01-02"


def test_rate_limit_is_retried_not_cached() -> None:
    session = _Session(_Resp(429), _Resp(200, [["timestamp"]]))
    assert _capture(session, "ex.org") is None
    assert session.calls == 2


def test_uncached_families_are_those_with_any_absent_lookup(tmp_path) -> None:
    cache = od._Cache(tmp_path / "cache.jsonl")
    cache.put("cdx:a.org", None)  # completed lookup, no capture: cached
    cache.put("cdx:b.org", "2010-01-01")
    cache.put("wd:Q1", None)
    targets = {"fa": ["a.org"], "fb": ["b.org", "c.org"], "fc": []}
    qids = {"fa": "Q1", "fc": "Q2"}
    assert od._uncached_families(targets, qids, cache) == {"fb", "fc"}


def test_allow_uncached_requires_offline() -> None:
    from click.testing import CliRunner

    res = CliRunner().invoke(
        od.main,
        ["--allow-uncached", "--corpus", "/nonexistent", "--store", "/nonexistent"],
    )
    assert res.exit_code == 2
    assert "--offline" in res.output


def test_request_timeout_is_retried_not_cached() -> None:
    session = _Session(_Resp(408), _Resp(200, [["timestamp"], ["20150102030405"]]))
    assert _capture(session, "ex.org") == "2015-01-02"
    assert session.calls == 2
