"""
OBO Foundry.

About:
    Fetches every active ontology listed in the OBO Foundry registry and
    normalises each result to a FetchRecord. Obsolete entries are skipped.
    The OBO JSON object, projected to an allowlist of fields, is stored in
    source_registry_metadata.

    Registry API: https://obofoundry.org/registry/ontologies.jsonld
"""

import time
from pathlib import Path

import requests

from ._base import (
    FetchRecord,
    append_record,
    download_ontology,
    now_utc_iso,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_NAME = "OBO"
API_URL = "https://obofoundry.org/registry/ontologies.jsonld"
REQUEST_DELAY = 0.2  # seconds between downloads


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------


def fetch_obo(
    session: requests.Session,
    fail_log: Path,
    objects_dir: Path,
    manifest_path: Path,
    resume: set[str] | None = None,
) -> list[FetchRecord]:
    """
    Download all active OBO Foundry ontologies and return a FetchRecord for
    every attempt (successes and failures alike).

    Parameters
    ----------
    session
        Shared requests.Session (with retry adapter).
    fail_log
        Path to the failure log (``fetch_failures.log`` in the corpus directory).
    manifest_path
        Path to manifest.jsonl; each record is appended as soon as it is produced.
    objects_dir
        Root of the content-addressed object store for raw ontology files.
    resume
        Set of source_ids already captured on a previous run; these are skipped.

    Returns
    -------
    list[FetchRecord]
        One record per attempted ontology (status ok/fail). Obsolete entries
        and entries with no download URL are omitted entirely.
    """
    resume = resume or set()

    print(f"\n=== {REGISTRY_NAME} Foundry ===")

    # ---- Fetch registry listing ----------------------------------------
    try:
        r = session.get(API_URL, timeout=30)
        r.raise_for_status()
        data: dict = r.json()
    except Exception as exc:
        print(f"  [err] {REGISTRY_NAME} registry fetch failed: {exc}")
        return []

    ontologies: list[dict] = data.get("ontologies", [])
    print(f"Found {len(ontologies)} ontologies")

    records: list[FetchRecord] = []
    counts = {"ok": 0, "fail": 0, "skip": 0, "no_dl": 0}

    for onto in ontologies:
        # Skip obsolete entries
        if onto.get("is_obsolete"):
            continue

        oid: str = onto.get("id", "")
        if not oid:
            continue

        source_id = f"obo:{oid}"

        if source_id in resume:
            counts["skip"] += 1
            continue

        # Resolve the primary download URL from products list
        products: list[dict] = onto.get("products", [])
        canonical_url: str = ""
        for p in products:
            candidate = p.get("ontology_purl", "")
            if candidate:
                canonical_url = candidate
                break

        if not canonical_url:
            counts["no_dl"] += 1
            continue

        retrieval_date = now_utc_iso()
        success, saved_path, file_hash, error, content_kind = download_ontology(
            session=session,
            url=canonical_url,
            objects_dir=objects_dir,
            source=REGISTRY_NAME,
            fail_log=fail_log,
        )

        if success and saved_path:
            rec = FetchRecord.ok(
                source_id=source_id,
                canonical_url=canonical_url,
                retrieved_file=saved_path,
                retrieval_date=retrieval_date,
                source_registry=REGISTRY_NAME,
                source_registry_metadata=onto,
                file_hash=file_hash,
                content_kind=content_kind,
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["ok"] += 1
        else:
            rec = FetchRecord.failure(
                source_id=source_id,
                canonical_url=canonical_url,
                retrieval_date=retrieval_date,
                source_registry=REGISTRY_NAME,
                source_registry_metadata=onto,
                error=error,
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1

        time.sleep(REQUEST_DELAY)

    print(f"\n{REGISTRY_NAME} summary:")
    print(f"  ok={counts['ok']}")
    print(f"  fail={counts['fail']}")
    print(f"  skip={counts['skip']}")
    print(f"  no_download_url={counts['no_dl']}")

    return records
