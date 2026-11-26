"""
Ontohub.

About:
    Fetches all ontology files from every repository listed at
    https://ontohub.org/repositories.txt.

    Strategy:
      1. Fetch repositories.txt (one git:// URL per line).
      2. For each repo GET https://ontohub.org/{repo_name} which returns a
         Ruby object dump listing all files as @path="filename".
      3. Filter for known ontology file extensions.
      4. Download each file directly from https://ontohub.org/{repo}/{file}.

    source_registry_metadata stores {"repo": repo_name, "file": fname,
    "repo_url": repo_url} for each record — enough to reconstruct the original source path.
"""

import re
import time
from pathlib import Path

import requests

from ._base import (
    FetchRecord,
    append_record,
    download_ontology,
    log_failure,
    now_utc_iso,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTRY_NAME = "Ontohub"
REPO_LIST_URL = "https://ontohub.org/repositories.txt"
ONTOLOGY_EXTS = {".owl", ".ttl", ".rdf", ".n3", ".nt", ".jsonld"}
TIMEOUT = 30
REQUEST_DELAY = 0.2  # seconds between downloads


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------


def fetch_ontohub(
    session: requests.Session,
    fail_log: Path,
    objects_dir: Path,
    manifest_path: Path,
    resume: set[str] | None = None,
) -> list[FetchRecord]:
    """
    Download all ontology files from Ontohub and return a FetchRecord for
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
        One record per attempted ontology file (status ok/fail).
    """
    resume = resume or set()

    print(f"\n=== {REGISTRY_NAME} ===")

    # ---- 1. Fetch repository list --------------------------------------
    try:
        r = session.get(REPO_LIST_URL, timeout=TIMEOUT, verify=False)
        r.raise_for_status()
    except Exception as exc:
        print(f"  [err] Failed to fetch {REGISTRY_NAME} repository list: {exc}")
        log_failure(fail_log, REGISTRY_NAME, REPO_LIST_URL, str(exc))
        return []

    repo_lines = [line.strip() for line in r.text.splitlines() if line.strip()]
    print(f"Found {len(repo_lines)} repositories")

    records: list[FetchRecord] = []
    counts = {"ok": 0, "fail": 0, "skip": 0}

    for repo_url in repo_lines:
        # Derive the plain repo name from the git:// URL
        repo_name = repo_url.rstrip("/")
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        repo_name = repo_name.split("/")[-1]

        # ---- 2. Get file listing for this repo -------------------------
        listing_url = f"https://ontohub.org/{repo_name}"
        try:
            listing_r = session.get(listing_url, timeout=TIMEOUT, verify=False)
            listing_r.raise_for_status()
        except Exception as exc:
            print(f"  [err] {REGISTRY_NAME} {repo_name}: listing failed -> {exc}")
            log_failure(fail_log, REGISTRY_NAME, listing_url, str(exc))
            counts["fail"] += 1
            continue

        # ---- 3. Parse @path="filename" from the Ruby object dump ------
        file_paths = re.findall(r'@path="([^"]+)"', listing_r.text)
        ont_files = [p for p in file_paths if Path(p).suffix.lower() in ONTOLOGY_EXTS]

        if not ont_files:
            print(f"  [no ontologies] {repo_name}")
            continue

        # ---- 4. Download each ontology file ----------------------------
        for fname in ont_files:
            file_url = f"https://ontohub.org/{repo_name}/{fname}"
            source_id = f"ontohub:{repo_name}/{fname}"
            metadata = {"repo": repo_name, "file": fname, "repo_url": repo_url}

            if source_id in resume:
                counts["skip"] += 1
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
                )
                records.append(rec)
                append_record(rec, manifest_path)
                counts["fail"] += 1

        time.sleep(REQUEST_DELAY)

    print(f"\n{REGISTRY_NAME} summary:")
    print(f"  ok={counts['ok']}")
    print(f"  fail={counts['fail']}")
    print(f"  skip={counts['skip']}")

    return records
