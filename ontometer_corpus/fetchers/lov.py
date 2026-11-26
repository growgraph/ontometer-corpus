"""
LOV (Linked Open Vocabularies).

About:
    Fetches EVERY version of every vocabulary listed in the LOV API — not just
    the current snapshot — and normalises each to a FetchRecord.

    LOV exposes version history: the vocabulary/list endpoint enumerates vocabs
    but only carries version names, while vocabulary/info?vocab=<prefix> returns
    a full versions[] array where each entry has a dated `fileURL`. This fetcher
    walks list -> info -> each version's fileURL, and version-qualifies the
    source_id (``lov:<uri>::<version_label>``) so every version is preserved as
    its own object / corpus_registry row rather than being collapsed.

    List API: https://lov.linkeddata.es/dataset/api/v2/vocabulary/list
    Info API: https://lov.linkeddata.es/dataset/api/v2/vocabulary/info?vocab=<prefix>
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

REGISTRY_NAME = "LOV"
API_URL = "https://lov.linkeddata.es/dataset/api/v2/vocabulary/list"
INFO_URL = "https://lov.linkeddata.es/dataset/api/v2/vocabulary/info"
REQUEST_DELAY = 0.2  # seconds between downloads


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------


def _version_label(ver: dict) -> str:
    """
    Derive a stable, human-meaningful version label for a LOV version entry.
    Prefers LOV's own name (e.g. "v0.99"), falling back to the issued date.
    """
    name = (ver.get("name") or "").strip()
    if name:
        return name
    issued = (ver.get("issued") or "").strip()
    return issued[:10]  # YYYY-MM-DD


def fetch_lov(
    session: requests.Session,
    fail_log: Path,
    objects_dir: Path,
    manifest_path: Path,
    resume: set[str] | None = None,
    only_prefixes: set[str] | None = None,
) -> list[FetchRecord]:
    """
    Download EVERY version of every LOV vocabulary and return a FetchRecord for
    each attempt (successes and failures alike).

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
        Note: the per-vocab info call still runs for captured vocabs — that is
        how newly-published versions are discovered; only already-captured
        versions are skipped.
    only_prefixes
        If given, restrict to these vocab prefixes (for scoped / testing runs).

    Returns
    -------
    list[FetchRecord]
        One record per attempted version (status ok/fail).
    """
    resume = resume or set()

    print(f"\n=== {REGISTRY_NAME} ===")

    # ---- Fetch registry listing ----------------------------------------
    try:
        r = session.get(API_URL, timeout=30)
        r.raise_for_status()
        vocabs: list[dict] = r.json()
    except Exception as exc:
        print(f"  [err] {REGISTRY_NAME} registry fetch failed: {exc}")
        return []

    if only_prefixes:
        vocabs = [v for v in vocabs if v.get("prefix") in only_prefixes]
    print(f"Found {len(vocabs)} vocabularies")

    records: list[FetchRecord] = []
    counts = {"ok": 0, "fail": 0, "skip": 0}

    for v in vocabs:
        uri: str = v.get("uri", "")
        prefix: str = v.get("prefix", "")
        if not uri or not prefix:
            continue

        # ---- Enumerate versions via the info endpoint ------------------
        try:
            info_r = session.get(INFO_URL, params={"vocab": prefix}, timeout=30)
            info_r.raise_for_status()
            info: dict = info_r.json()
            versions: list[dict] = info.get("versions", []) or []
        except Exception as exc:
            msg = f"info lookup failed: {exc}"
            print(f"  [err] {REGISTRY_NAME} {prefix}: {msg}")
            rec = FetchRecord.failure(
                source_id=f"lov:{uri}",
                canonical_url=f"{INFO_URL}?vocab={prefix}",
                retrieval_date=now_utc_iso(),
                source_registry=REGISTRY_NAME,
                source_registry_metadata=v,
                error=msg,
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1
            time.sleep(REQUEST_DELAY)
            continue

        if not versions:
            print(f"  [no versions] {prefix}")
            rec = FetchRecord.failure(
                source_id=f"lov:{uri}",
                canonical_url=f"{INFO_URL}?vocab={prefix}",
                retrieval_date=now_utc_iso(),
                source_registry=REGISTRY_NAME,
                source_registry_metadata=v,
                error="no versions listed by LOV info endpoint",
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1
            time.sleep(REQUEST_DELAY)
            continue

        # ---- Download each version -------------------------------------
        for ver in versions:
            version_label = _version_label(ver)
            version_date: str = (ver.get("issued") or "").strip()
            file_url: str = (ver.get("fileURL") or "").strip()
            source_id = f"lov:{uri}::{version_label}"
            # Provenance: the version entry plus its vocab context.
            metadata = {**ver, "_vocab_uri": uri, "_prefix": prefix}

            if source_id in resume:
                counts["skip"] += 1
                continue

            if not file_url:
                rec = FetchRecord.failure(
                    source_id=source_id,
                    canonical_url="",
                    retrieval_date=now_utc_iso(),
                    source_registry=REGISTRY_NAME,
                    source_registry_metadata=metadata,
                    error="version entry has no fileURL",
                    source_version=version_label,
                    source_version_date=version_date,
                )
                records.append(rec)
                append_record(rec, manifest_path)
                counts["fail"] += 1
                continue

            retrieval_date = now_utc_iso()
            success, saved_path, file_hash, error, content_kind = download_ontology(
                session=session,
                url=file_url,
                objects_dir=objects_dir,
                source=REGISTRY_NAME,
                fail_log=fail_log,
            )

            if success and saved_path:
                rec = FetchRecord.ok(
                    source_id=source_id,
                    canonical_url=file_url,
                    retrieved_file=saved_path,
                    retrieval_date=retrieval_date,
                    source_registry=REGISTRY_NAME,
                    source_registry_metadata=metadata,
                    file_hash=file_hash,
                    source_version=version_label,
                    source_version_date=version_date,
                    content_kind=content_kind,
                )
                records.append(rec)
                append_record(rec, manifest_path)
                counts["ok"] += 1
            else:
                rec = FetchRecord.failure(
                    source_id=source_id,
                    canonical_url=file_url,
                    retrieval_date=retrieval_date,
                    source_registry=REGISTRY_NAME,
                    source_registry_metadata=metadata,
                    error=error,
                    source_version=version_label,
                    source_version_date=version_date,
                )
                records.append(rec)
                append_record(rec, manifest_path)
                counts["fail"] += 1

            time.sleep(REQUEST_DELAY)

        time.sleep(REQUEST_DELAY)

    print(f"\n{REGISTRY_NAME} summary:")
    print(f"  ok={counts['ok']}")
    print(f"  fail={counts['fail']}")
    print(f"  skip={counts['skip']}")

    return records
