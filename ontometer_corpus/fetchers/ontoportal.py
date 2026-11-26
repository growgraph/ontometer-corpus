"""
OntoPortal Alliance (BioPortal, AgroPortal, BiodivPortal, etc.).

About:
    Fetches EVERY submission (version) of every ontology from an OntoPortal
    Alliance instance — not just the current snapshot.

    OntoPortal Alliance members (e.g. BioPortal, AgroPortal, BiodivPortal,
    MatPortal, OntoPortal-Astro, TechnoPortal) run the same open-source
    OntoPortal codebase and REST API, so ONE parameterised fetcher covers
    every instance — each configured with its own name/API base/API key,
    since each instance is independently administered and issues its own
    keys.

    Per ontology, `links.submissions` (NOT `links.latest_submission`) returns
    the full submission history, each with `version`, `released`,
    `creationDate`, and a `submissionId`. source_id is keyed on `submissionId`
    (not the free-text `version` label) since that field's presence/quality
    can't be assumed to hold across every ontology on every instance.

    Each submission is processed in its own try/except and its version/date/
    @id fields are str()-coerced, so one malformed submission is logged as a
    failure without affecting the rest of the run. Registry API calls retry
    without SSL verification on an SSLError (see _api_get), matching the
    fallback used for file downloads.

    Registry API pattern: {api_base}/ontologies
                           {api_base}/ontologies/{acronym}/submissions
                           {submission[@id]}/download?apikey=<key>
"""

import time
from dataclasses import dataclass
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
# Instance configuration
# ---------------------------------------------------------------------------


@dataclass
class OntoPortalInstance:
    """
    Connection details for one OntoPortal Alliance instance.

    name
        Human-readable registry label (becomes source_registry), e.g. "AgroPortal".
    slug
        Short identifier used in source_id, e.g. "agroportal".
    api_base
        Base URL of the instance's REST API, e.g. "https://data.agroportal.eu".
    api_key
        API key for this instance. Instances with no key configured should be
        skipped by the caller before invoking fetch_ontoportal.
    """

    name: str
    slug: str
    api_base: str
    api_key: str


REQUEST_DELAY = 0.5  # seconds between downloads — conservative for community-run instances


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_get(
    session: requests.Session, url: str, api_key: str, timeout: int = 30
) -> requests.Response:
    """
    GET a JSON API endpoint with the apikey query param, retrying without SSL
    verification only on an SSLError. An instance that serves an incomplete
    certificate chain (missing intermediate CA) fails with
    SSLCertVerificationError on a verified connection but succeeds with
    verify=False. Mirrors the SSL-fallback pattern in fetch_url (_base.py).
    Non-SSL errors (timeouts, 5xx, DNS failures) are not retried here — they
    fail identically the second time, so retrying just doubles the wait.
    """
    req_timeout = (10, timeout)
    try:
        r = session.get(url, params={"apikey": api_key}, timeout=req_timeout)
    except requests.exceptions.SSLError:
        print("  [ssl retry]")
        r = session.get(url, params={"apikey": api_key}, timeout=req_timeout, verify=False)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------


def fetch_ontoportal(
    session: requests.Session,
    fail_log: Path,
    objects_dir: Path,
    manifest_path: Path,
    instance: OntoPortalInstance,
    resume: set[str] | None = None,
    only_acronyms: set[str] | None = None,
) -> list[FetchRecord]:
    """
    Download EVERY submission (version) of every ontology from one OntoPortal
    instance and return a FetchRecord for each attempt (successes and failures
    alike).

    Parameters
    ----------
    session
        Shared requests.Session (with retry adapter).
    fail_log
        Path to the failure log (``fetch_failures.log`` in the corpus directory).
    objects_dir
        Root of the content-addressed object store for raw ontology files.
    manifest_path
        Path to manifest.jsonl. Each record is appended immediately after it
        is produced (not batched to the end of the run) so a crash mid-run
        only loses the one attempt in flight.
    instance
        Which OntoPortal Alliance instance to fetch from.
    resume
        Set of source_ids already captured on a previous run; these are skipped.
        Note: the per-ontology submissions call still runs for captured
        ontologies — that is how newly-published submissions are discovered;
        only already-captured submissions are skipped.
    only_acronyms
        If given, restrict to these ontology acronyms (for scoped / testing runs).

    Returns
    -------
    list[FetchRecord]
        One record per attempted submission (status ok/fail).
    """
    resume = resume or set()
    registry_name = instance.name

    print(f"\n=== {registry_name} ===")

    if not instance.api_key:
        print(f"  [skip] missing API key for {registry_name} — skipping")
        return []

    # ---- Fetch registry listing ----------------------------------------
    try:
        r = _api_get(session, f"{instance.api_base}/ontologies", instance.api_key)
        ontologies: list[dict] = r.json()
    except Exception as exc:
        print(f"  [err] {registry_name} registry fetch failed: {exc}")
        return []

    if only_acronyms:
        ontologies = [o for o in ontologies if o.get("acronym") in only_acronyms]
    print(f"Found {len(ontologies)} ontologies")

    records: list[FetchRecord] = []
    counts = {"ok": 0, "fail": 0, "skip": 0}

    for onto in ontologies:
        acronym: str = onto.get("acronym", "")
        if not acronym:
            continue

        subs_url: str = onto.get("links", {}).get("submissions", "")
        if not subs_url:
            msg = "no submissions link"
            log_failure(fail_log, registry_name, acronym, msg)
            rec = FetchRecord.failure(
                source_id=f"{instance.slug}:{acronym}",
                canonical_url="",
                retrieval_date=now_utc_iso(),
                source_registry=registry_name,
                source_registry_metadata=onto,
                error=msg,
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1
            continue

        # ---- Enumerate full submission history --------------------------
        try:
            subs_r = _api_get(session, subs_url, instance.api_key)
            submissions: list[dict] = subs_r.json() or []
        except Exception as exc:
            msg = f"submissions lookup failed: {exc}"
            print(f"  [err] {registry_name} {acronym}: {msg}")
            log_failure(fail_log, registry_name, subs_url, msg)
            rec = FetchRecord.failure(
                source_id=f"{instance.slug}:{acronym}",
                canonical_url=subs_url,
                retrieval_date=now_utc_iso(),
                source_registry=registry_name,
                source_registry_metadata=onto,
                error=msg,
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1
            time.sleep(REQUEST_DELAY)
            continue

        if not submissions:
            print(f"  [no submissions] {acronym}")
            rec = FetchRecord.failure(
                source_id=f"{instance.slug}:{acronym}",
                canonical_url=subs_url,
                retrieval_date=now_utc_iso(),
                source_registry=registry_name,
                source_registry_metadata=onto,
                error="no submissions listed",
            )
            records.append(rec)
            append_record(rec, manifest_path)
            counts["fail"] += 1
            time.sleep(REQUEST_DELAY)
            continue

        # ---- Download each submission ------------------------------------
        for sub in submissions:
            try:
                submission_id = sub.get("submissionId")
                if submission_id is None:
                    counts["fail"] += 1
                    continue

                # Defensive str() coercion: instances may return non-string
                # values (e.g. a float) for these fields, which a bare .strip()
                # would crash on. Each submission is also isolated by this
                # try/except so one malformed record cannot take down the
                # rest of the run.
                version_label: str = str(sub.get("version") or "").strip()
                version_date: str = str(
                    sub.get("released") or sub.get("creationDate") or ""
                ).strip()
                submission_iri: str = str(sub.get("@id") or "")
                source_id = f"{instance.slug}:{acronym}::{submission_id}"
                metadata = {**sub, "_acronym": acronym, "_ontology": onto}

                if source_id in resume:
                    counts["skip"] += 1
                    continue

                if not submission_iri:
                    rec = FetchRecord.failure(
                        source_id=source_id,
                        canonical_url="",
                        retrieval_date=now_utc_iso(),
                        source_registry=registry_name,
                        source_registry_metadata=metadata,
                        error="submission has no @id",
                        source_version=version_label,
                        source_version_date=version_date,
                    )
                    records.append(rec)
                    append_record(rec, manifest_path)
                    counts["fail"] += 1
                    continue

                download_url = f"{submission_iri}/download?apikey={instance.api_key}"
                key_free_url = f"{submission_iri}/download"  # key-free URL for records

                retrieval_date = now_utc_iso()
                success, saved_path, file_hash, error, content_kind = download_ontology(
                    session=session,
                    url=download_url,
                    objects_dir=objects_dir,
                    source=registry_name,
                    fail_log=fail_log,
                )

                if success and saved_path:
                    rec = FetchRecord.ok(
                        source_id=source_id,
                        canonical_url=key_free_url,
                        retrieved_file=saved_path,
                        retrieval_date=retrieval_date,
                        source_registry=registry_name,
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
                        canonical_url=key_free_url,
                        retrieval_date=retrieval_date,
                        source_registry=registry_name,
                        source_registry_metadata=metadata,
                        error=error,
                        source_version=version_label,
                        source_version_date=version_date,
                    )
                    records.append(rec)
                    append_record(rec, manifest_path)
                    counts["fail"] += 1
            except Exception as exc:
                # Isolate one malformed submission from the rest of the batch.
                msg = f"unexpected error processing submission: {exc}"
                print(f"  [err] {registry_name} {acronym}: {msg}")
                log_failure(fail_log, registry_name, acronym, msg)
                counts["fail"] += 1

            time.sleep(REQUEST_DELAY)

        time.sleep(REQUEST_DELAY)

    print(f"\n{registry_name} summary:")
    print(f"  ok={counts['ok']}")
    print(f"  fail={counts['fail']}")
    print(f"  skip={counts['skip']}")

    return records
