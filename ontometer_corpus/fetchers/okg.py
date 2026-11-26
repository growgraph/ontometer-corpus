"""
Open Knowledge Graphs (OKG).

About:
    Fetches all ontologies catalogued at openknowledgegraphs.com and normalises
    each result to a FetchRecord. The canonical URL is chosen in the order
    namespaceURI > sourceRepo > homepage.

    Many catalogued URLs are github.com repo pages rather than raw files, and
    a repo homepage exposes no reliable direct link to the ontology file. Such
    URLs are resolved to a specific raw ontology file via the GitHub API (repo
    tree, recursive; see _resolve_github_repo_file). Any other URL that
    returns HTML is scraped for direct ontology file links.

    Unauthenticated GitHub API access is rate-limited, so set GITHUB_TOKEN in
    the environment for a full run.

    Registry API: https://openknowledgegraphs.com/data/ontologies.json
"""

import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

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

REGISTRY_NAME = "OKG"
METADATA_URL = "https://openknowledgegraphs.com/data/ontologies.json"
REQUEST_DELAY = 0.1  # seconds between downloads

# Same extension set the Ontohub fetcher uses to identify ontology files in a
# raw file listing.
ONTOLOGY_EXTS = {".owl", ".ttl", ".rdf", ".n3", ".nt", ".jsonld"}

# Highest priority wins when a repo contains multiple candidate files
# (mirrors extract_ontology_links_from_html's extension scoring in _base.py).
_EXT_PRIORITY = {".ttl": 5, ".owl": 4, ".rdf": 4, ".n3": 4, ".nt": 4, ".jsonld": 3}

_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?(?:$|[/?#])"
)

GITHUB_API_ROOT = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_qid(wikidata_url: str) -> str:
    """Extract the Q-identifier from a Wikidata entity URL."""
    if not isinstance(wikidata_url, str):
        return ""
    parts = urlparse(wikidata_url).path.strip("/").split("/")
    return parts[-1] if parts else ""


def _github_headers() -> dict:
    """
    Authorization header for the GitHub API, if GITHUB_TOKEN is set in the
    environment. Unauthenticated access is rate-limited, so a run over many
    github.com repo URLs needs a token.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_github_repo_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a github.com repo URL, or None if url isn't one."""
    m = _GITHUB_REPO_RE.match(url or "")
    if not m:
        return None
    return m.group("owner"), m.group("repo")


def _resolve_github_repo_file(session: requests.Session, owner: str, repo: str) -> str | None:
    """
    Resolve a GitHub repo to a raw download URL for its best ontology file
    candidate, via the GitHub API (repo default branch + recursive tree
    listing) -- not the generic HTML-link scraper, which has no way to see
    into a repo's file tree from the repo homepage alone.

    Returns None (not an exception) on any failure -- rate limit, private/
    deleted repo, no ontology file found, etc. -- so the caller can fall back
    to the generic HTML-scraping path instead of hard-failing the whole item.
    """
    headers = _github_headers()
    try:
        repo_resp = session.get(
            f"{GITHUB_API_ROOT}/repos/{owner}/{repo}", headers=headers, timeout=15
        )
        if repo_resp.status_code != 200:
            return None
        branch = repo_resp.json().get("default_branch", "main")

        tree_resp = session.get(
            f"{GITHUB_API_ROOT}/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
            headers=headers,
            timeout=15,
        )
        if tree_resp.status_code != 200:
            return None

        candidates = []
        for entry in tree_resp.json().get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            ext = Path(path).suffix.lower()
            if ext in ONTOLOGY_EXTS:
                # Prefer root-level files (fewer path separators) as a
                # tiebreaker over deeply nested ones (e.g. test fixtures).
                candidates.append((_EXT_PRIORITY.get(ext, 1), -path.count("/"), path))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        best_path = candidates[0][2]
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{best_path}"

    except requests.exceptions.RequestException:
        return None


# ---------------------------------------------------------------------------
# Public fetcher
# ---------------------------------------------------------------------------


def fetch_okg(
    session: requests.Session,
    fail_log: Path,
    objects_dir: Path,
    manifest_path: Path,
    resume: set[str] | None = None,
) -> list[FetchRecord]:
    """
    Download all OKG ontologies and return a FetchRecord for every attempt
    (successes and failures alike).

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
        One record per attempted ontology (status ok/fail).
    """
    resume = resume or set()

    print(f"\n=== {REGISTRY_NAME} (Open Knowledge Graphs) ===")

    # ---- Fetch registry metadata dump ----------------------------------
    try:
        print(f"Fetching OKG metadata dump from {METADATA_URL} ...")
        r = session.get(METADATA_URL, timeout=30)
        r.raise_for_status()
        data: dict = r.json()
    except Exception as exc:
        print(f"  [err] {REGISTRY_NAME} metadata fetch failed: {exc}")
        log_failure(fail_log, REGISTRY_NAME, METADATA_URL, f"Metadata fetch failed: {exc}")
        return []

    # The full registry response is deliberately not written to disk: the
    # per-ontology slice that matters is carried on each FetchRecord and
    # seeded into corpus_registry.

    items: list[dict] = data.get("items", [])
    print(f"Found {len(items)} ontologies in OKG catalogue")

    records: list[FetchRecord] = []
    counts = {"ok": 0, "fail": 0, "skip": 0, "no_source": 0}

    for item in items:
        wikidata_url: str = item.get("wikidataId", "")

        if not wikidata_url:
            counts["skip"] += 1
            continue

        qid = _extract_qid(wikidata_url)
        if not qid:
            counts["skip"] += 1
            continue

        source_id = f"okg:{qid}"

        if source_id in resume:
            counts["skip"] += 1
            continue

        # Resolve best available source URL
        canonical_url: str = (
            item.get("namespaceURI") or item.get("sourceRepo") or item.get("homepage") or ""
        )

        if not canonical_url:
            counts["no_source"] += 1
            continue

        # GitHub repo pages carry no direct download link the generic
        # HTML-link scraper can reliably find -- resolve via the GitHub API
        # instead (repo tree listing -> raw.githubusercontent.com URL). Falls
        # through to the generic path on any resolution failure (rate limit,
        # private/deleted repo, no ontology file in the tree, etc.).
        download_url = canonical_url
        original_url = canonical_url
        github_repo = _parse_github_repo_url(canonical_url)
        if github_repo:
            resolved = _resolve_github_repo_file(session, *github_repo)
            if resolved:
                download_url = resolved

        retrieval_date = now_utc_iso()
        success, saved_path, file_hash, error, content_kind = download_ontology(
            session=session,
            url=download_url,
            objects_dir=objects_dir,
            source=REGISTRY_NAME,
            fail_log=fail_log,
            parse_html_for_links=True,
        )
        if success:
            canonical_url = download_url  # record what was actually retrieved

        metadata = item
        if download_url != original_url:
            metadata = {**item, "_resolved_from_github_repo": original_url}

        if success and saved_path:
            rec = FetchRecord.ok(
                source_id=source_id,
                canonical_url=canonical_url,
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
                canonical_url=canonical_url,
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
    print(f"  no_source={counts['no_source']}")

    return records
