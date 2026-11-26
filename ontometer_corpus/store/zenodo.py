"""Zenodo drafts for the archived bundles of a corpus version.

Creates a new deposition, or a new version of an existing record (which keeps
the earlier version's files, so a later release only adds its delta bundle),
uploads the files and sets the metadata. It never publishes: a published
Zenodo record cannot be withdrawn, so that step stays manual, in the web
interface, after review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ZENODO_URL = "https://zenodo.org"
SANDBOX_URL = "https://sandbox.zenodo.org"


class ZenodoError(RuntimeError):
    """Raised when Zenodo rejects a request."""


@dataclass(frozen=True, slots=True)
class ZenodoDraft:
    id: int
    html: str
    files: list[str]


def load_metadata(path: Path, rev: str) -> dict[str, Any]:
    """Deposition metadata from ``corpus/zenodo.yaml``, versioned as *rev*."""
    metadata = yaml.safe_load(path.read_text())
    metadata["version"] = rev
    return metadata


def _check(response: Any) -> Any:
    if response.status_code >= 400:
        raise ZenodoError(f"Zenodo answered {response.status_code}: {response.text[:300]}")
    return response


def create_draft(
    files: list[Path],
    metadata: dict[str, Any],
    *,
    token: str,
    base_url: str = ZENODO_URL,
    session: Any = None,
    record: int | None = None,
) -> ZenodoDraft:
    """Create a draft (a new version of *record* when given) holding *files*; never publish."""
    if session is None:
        import requests

        session = requests.Session()
    headers = {"Authorization": f"Bearer {token}"}
    api = f"{base_url}/api/deposit/depositions"
    if record is None:
        deposition = _check(session.post(api, json={}, headers=headers)).json()
    else:
        response = _check(session.post(f"{api}/{record}/actions/newversion", headers=headers))
        latest = response.json()["links"]["latest_draft"]
        deposition = _check(session.get(latest, headers=headers)).json()
    bucket = deposition["links"]["bucket"]
    for path in files:
        with path.open("rb") as handle:
            _check(session.put(f"{bucket}/{path.name}", data=handle, headers=headers))
    _check(session.put(f"{api}/{deposition['id']}", json={"metadata": metadata}, headers=headers))
    return ZenodoDraft(
        id=deposition["id"], html=deposition["links"]["html"], files=[p.name for p in files]
    )
