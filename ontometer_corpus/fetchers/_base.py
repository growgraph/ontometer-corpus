"""
Shared base infrastructure.

About:
    Defines the canonical FetchRecord dataclass and all shared HTTP/IO helpers
    used by every per-source fetcher module. Nothing in here is source-specific.

    Raw files are stored in a content-addressed (SHA-256), write-once object
    store. Every fetch attempt is recorded (status ok/fail, with file_hash and
    error), so fetch-level failure rates are queryable.

    Normalised output schema (core fields; see FetchRecord for the rest):
        source_id               – globally unique identifier (e.g. "lov:http://...")
        canonical_url           – URL that was actually fetched
        retrieved_file          – object path relative to the store root ("" on failure)
        retrieval_date          – ISO-8601 UTC timestamp of the fetch attempt
        source_registry         – human-readable registry name ("LOV", "OBO", ...)
        source_registry_metadata – registry metadata for this entry, projected
                                   to an allowlist of fields
"""

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from suthing import atomic_write, bytes_hash, utc_now_iso
from urllib3.util.retry import Retry

from ontometer_corpus.metadata import project_registry_metadata, redact_secrets

# ---------------------------------------------------------------------------
# Global HTTP noise suppression
# ---------------------------------------------------------------------------

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Reconfigure stdout to UTF-8 on Windows
if sys.stdout.encoding != "utf-8":
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEOUT = 30

HEADERS = {
    "Accept": ",".join(
        [
            "text/turtle",
            "application/rdf+xml",
            "application/ld+json",
            "text/n3",
            "application/n-triples",
            "application/xml;q=0.9",
            "*/*;q=0.1",
        ]
    ),
    "User-Agent": "ontometer-corpus (+https://github.com/growgraph/ontometer-corpus)",
}

CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "text/turtle": ".ttl",
    "application/x-turtle": ".ttl",
    "application/rdf+xml": ".rdf",
    "application/xml": ".xml",
    "application/ld+json": ".jsonld",
    "application/n-triples": ".nt",
    "text/n3": ".n3",
    "application/trig": ".trig",
}

# ---------------------------------------------------------------------------
# FetchRecord — the normalised output schema
# ---------------------------------------------------------------------------


@dataclass
class FetchRecord:
    """
    Canonical record produced by every per-source fetcher for EVERY fetch
    attempt — both successes and failures.

    Recording failures (status="fail") as well as successes is what lets the
    pipeline compute the fraction of each registry's ontologies actually
    captured. A failed attempt (broken/abandoned ontology, dead URL, HTML page
    instead of RDF) is part of the population, not noise to be discarded.

    Fields
    ------
    source_id
        Globally unique identifier for this entry, namespaced by registry.
        Examples: "lov:http://xmlns.com/foaf/0.1/", "obo:go", "okg:Q123"
    canonical_url
        The URL that was fetched (or attempted).
    retrieved_file
        Path of the saved object relative to the store root
        (``objects/<hh>/<sha256><ext>``). Empty string on failure.
    retrieval_date
        ISO-8601 UTC timestamp of the fetch attempt.
    source_registry
        Human-readable registry name: "LOV", "OBO", "BioPortal", "OKG", "Ontohub".
    source_registry_metadata
        The registry API's dict for this entry (for provenance), projected to
        an allowlist of fields by ``project_registry_metadata``.
    status
        "ok" if a file was written/reused, "fail" if the download failed.
    file_hash
        SHA-256 (hex) of the raw file bytes — the object-store key. This is the
        RAW-BYTES hash, distinct from the semantic class/property content hash
        computed later during parsing. Empty string on failure.
    error
        Failure reason. Empty string on success.
    source_version
        Registry-declared version label for this entry (e.g. LOV "v0.99"), when
        the source exposes version history. Empty string if unknown/unversioned.
        This is what makes source_id version-unique and lets the pipeline keep
        multiple versions of the same ontology as distinct rows.
    source_version_date
        Publication date of this version as an ISO-8601 string (e.g. LOV's
        `issued`), when the source provides one. Empty string otherwise. Used to
        order versions by recency (version *labels* are not reliably sortable),
        so the pipeline can flag the most-recent version per ontology.
    content_kind
        Cheap heuristic classification of the downloaded content, computed by
        `classify_content_kind()` at fetch time: "ontology_schema" (has
        class/property definitions), "instance_data" (RDF with no schema
        markers — a data/ABox dump), "catalog_metadata" (DCAT/void dataset
        catalogue description), "service_description" (SPARQL endpoint
        description — never a real fetch, download_ontology rejects these
        outright), "empty", "unknown", or "" when not computed (e.g. on
        failure). It matters most for crawl-based sources such as OKG
        homepages, where linked files are unvetted; curated registries like
        OBO/LOV/OntoPortal already list real ontologies.
    """

    source_id: str
    canonical_url: str
    retrieved_file: str
    retrieval_date: str
    source_registry: str
    source_registry_metadata: dict = field(default_factory=dict)
    status: str = "ok"
    file_hash: str = ""
    error: str = ""
    source_version: str = ""
    source_version_date: str = ""
    content_kind: str = ""

    def __post_init__(self) -> None:
        """Project registry metadata down to the fields the corpus work reads.

        Applied here rather than at serialisation time so the dropped fields
        never reach either artifact: the same record is written to
        `manifest.jsonl` and, via `seed_from_manifest`, into `corpus_registry`.
        Verbatim registry responses are mostly boilerplate plus maintainers'
        contact details; see `ontometer_corpus.metadata`.
        """
        self.source_registry_metadata = project_registry_metadata(self.source_registry_metadata)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def ok(
        cls,
        *,
        source_id: str,
        canonical_url: str,
        retrieved_file: str,
        retrieval_date: str,
        source_registry: str,
        source_registry_metadata: dict,
        file_hash: str,
        source_version: str = "",
        source_version_date: str = "",
        content_kind: str = "",
    ) -> "FetchRecord":
        """Build a successful-fetch record."""
        return cls(
            source_id=source_id,
            canonical_url=canonical_url,
            retrieved_file=retrieved_file,
            retrieval_date=retrieval_date,
            source_registry=source_registry,
            source_registry_metadata=source_registry_metadata,
            status="ok",
            file_hash=file_hash,
            error="",
            source_version=source_version,
            source_version_date=source_version_date,
            content_kind=content_kind,
        )

    @classmethod
    def failure(
        cls,
        *,
        source_id: str,
        canonical_url: str,
        retrieval_date: str,
        source_registry: str,
        source_registry_metadata: dict,
        error: str,
        source_version: str = "",
        source_version_date: str = "",
    ) -> "FetchRecord":
        """Build a failed-fetch record (no file on disk)."""
        return cls(
            source_id=source_id,
            canonical_url=canonical_url,
            retrieved_file="",
            retrieval_date=retrieval_date,
            source_registry=source_registry,
            source_registry_metadata=source_registry_metadata,
            status="fail",
            file_hash="",
            error=error,
            source_version=source_version,
            source_version_date=source_version_date,
        )


def now_utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    # "auto" keeps the microseconds (dropped only when they are zero), the
    # format manifest retrieval dates use.
    return utc_now_iso(timespec="auto")


# ---------------------------------------------------------------------------
# HTTP session factory
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    """Build a requests.Session with retry logic pre-configured."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def safe_filename(uri: str) -> str:
    """
    Produce a filesystem-safe name from a URI or arbitrary string.
    Strips the scheme, joins netloc + path, then replaces unsafe chars.
    Capped at 180 characters.
    """
    parsed = urlparse(uri)
    if parsed.netloc:
        name = (parsed.netloc + parsed.path).replace("/", "_")
    else:
        name = uri.replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return name[:180].strip("_")


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def extension_from_response(response: requests.Response, url: str) -> str:
    """
    Infer a file extension from the HTTP response.
    Priority: Content-Type header → URL path suffix → ".rdf" fallback.
    """
    ct = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[ct]
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 10:
        return suffix
    return ".rdf"


def looks_like_html(content: bytes) -> bool:
    """Return True if the first 1 000 bytes look like an HTML document."""
    head = content[:1000].lower()
    return b"<html" in head or b"<!doctype html" in head or b"<head" in head


def looks_like_feed(content: bytes) -> bool:
    """
    Return True if the content looks like an RSS or Atom feed rather than RDF.

    RSS/Atom feeds share the .xml / .rdf MIME type with actual RDF, but are
    fundamentally not ontology data. Some ontology homepages (e.g. WordPress
    sites) answer with an RSS feed, which would otherwise be saved with a .rdf
    extension.

    Detection is based on the first 500 bytes only (fast, before full parse).
    """
    head = content[:500].lower()
    return (
        b"<rss " in head
        or b"<feed " in head  # Atom feeds
        or b"<channel>" in head  # RSS body marker
        or (b"<rss" in head and b"version=" in head)
    )


def looks_like_sparql_service_description(content: bytes) -> bool:
    """
    Return True if *content* is a SPARQL Service Description document
    (describes a query endpoint's capabilities) rather than ontology data.

    Some dataset pages link an auto-generated `sd:Service` description
    (endpoint URL, supported result formats) instead of the actual
    vocabulary/data dump. These are valid RDF but contain zero domain
    classes/properties, so they must not be counted as a fetched ontology.
    """
    sample = content[:5000].decode("utf-8", errors="ignore")
    if "sparql-service-description" not in sample and "sd:Service" not in sample:
        return False
    # A real ontology that merely references the SD vocabulary (rare) would
    # still declare its own classes/properties; a pure endpoint description
    # will not.
    return not _SCHEMA_MARKER_RE.search(sample)


def zip_looks_like_source_repo(content: bytes) -> bool:
    """
    Return True if a ZIP archive's member listing looks like a GitHub
    source-code repository dump (README/LICENSE/.git*) rather than a
    compressed ontology/data file.

    Some "download" links point at a GitHub repo archive (e.g. an ontology's
    development repo including CI config, issue templates, etc.). Without this
    check such an archive would be saved with a `.rdf`/`.owl` extension
    inferred from the URL/Content-Type, although the archive itself is not
    ontology content.
    """
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = [n.lower() for n in zf.namelist()]
    except Exception:
        return False

    if not names:
        return False

    repo_markers = (
        "readme",
        "license",
        ".git/",
        ".github/",
        "contributing",
        "package.json",
        ".gitignore",
        "setup.py",
        "makefile",
    )
    has_repo_marker = any(any(m in n for m in repo_markers) for n in names)
    rdf_like = sum(
        1 for n in names if n.endswith((".owl", ".rdf", ".ttl", ".nt", ".n3", ".jsonld", ".xml"))
    )
    return has_repo_marker and rdf_like < max(1, len(names) // 10)


_SCHEMA_MARKER_RE = re.compile(
    r"\b(?:owl|rdfs):(?:Class|ObjectProperty|DatatypeProperty|AnnotationProperty)\b"
    r"|<owl:Class\b|<rdfs:Class\b"
)
_CATALOG_MARKER_RE = re.compile(r"\bdcat:Dataset\b|dcatap:|dcterms:accrualPeriodicity")


def classify_content_kind(content: bytes) -> str:
    """
    Cheap heuristic classification of downloaded RDF content, based on a
    sample of the first 200KB (fast even for multi-hundred-MB dumps).

    Returns one of: "empty", "service_description", "ontology_schema",
    "catalog_metadata", "instance_data", "unknown". This does not require a
    full RDF parse — it is a fast first-pass filter so that non-ontology
    content (bare data dumps, DCAT catalogues) fetched incidentally by
    crawl-based sources (e.g. links scraped from OKG homepages) can be
    distinguished from real ontologies without waiting for the parse stage.
    """
    if not content or not content.strip():
        return "empty"
    sample = content[:200_000]
    try:
        text = sample.decode("utf-8", errors="ignore")
    except Exception:
        return "unknown"
    if not text.strip():
        return "empty"
    if _SCHEMA_MARKER_RE.search(text):
        return "ontology_schema"
    if "sparql-service-description" in text or "sd:Service" in text:
        return "service_description"
    if _CATALOG_MARKER_RE.search(text):
        return "catalog_metadata"
    return "instance_data"


def extract_ontology_links_from_html(html_text: str, base_url: str) -> list[str]:
    """
    Parse HTML and return candidate direct links to ontology files,
    ordered by heuristic priority (highest first).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    candidates: list[tuple[str, int]] = []

    # 1. <link> tags -- require the "type" attribute to actually look like an
    # RDF/ontology media type. A bare rel="alternate"/"meta" is NOT enough on
    # its own: e.g. GitHub repo pages carry
    # <link rel="alternate icon" type="image/png" href=".../favicon.png">
    # for the site favicon, which satisfies "alternate" in rel regardless of
    # content, so matching on rel alone would capture the favicon as "the
    # ontology".
    for link in soup.find_all("link"):
        raw_type = link.get("type")
        type_attr = raw_type.lower() if isinstance(raw_type, str) else ""
        href = link.get("href")
        if isinstance(href, str) and any(
            marker in type_attr
            for marker in ("rdf", "turtle", "ontology", "owl", "n-triples", "ld+json")
        ):
            candidates.append((urljoin(base_url, href), 3))

    # 2. <a> tags scored by extension and link text
    for a in soup.find_all("a"):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        full_url = urljoin(base_url, href)
        text = a.get_text().strip().lower()
        path_lower = urlparse(full_url).path.lower()

        priority = 0
        if path_lower.endswith(".ttl"):
            priority = 5
        elif path_lower.endswith((".owl", ".rdf")):
            priority = 4
        elif path_lower.endswith((".n3", ".nt", ".jsonld")):
            priority = 4
        elif path_lower.endswith(".xml") and not path_lower.endswith(".html"):
            priority = 2

        if any(kw in text for kw in ("download", "rdf", "turtle", "owl", "ttl")):
            priority = max(priority, 3)

        if priority > 0:
            candidates.append((full_url, priority))

    candidates.sort(key=lambda x: x[1], reverse=True)
    seen: set[str] = set()
    result: list[str] = []
    for url, _ in candidates:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


# ---------------------------------------------------------------------------
# Content-addressed object store
# ---------------------------------------------------------------------------


def content_hash(content: bytes) -> str:
    """Return the SHA-256 hex digest of raw file bytes (the object-store key)."""
    return bytes_hash(content)


def store_object(objects_dir: Path, content: bytes, ext: str) -> tuple[Path, str]:
    """
    Store *content* immutably in a content-addressed object store.

    Files are keyed by the SHA-256 of their bytes and sharded by the first two
    hex chars to keep directories small:

        <objects_dir>/<hh>/<full-hash><ext>

    The store is write-once / immutable: if an object with the same content
    hash already exists it is reused as-is (this is how byte-identical files
    fetched from different registries or URLs are de-duplicated at the storage
    layer). The extension of the first writer wins.

    Returns:
        (path, hash): absolute path of the stored object and its content hash.
    """
    h = content_hash(content)
    shard = objects_dir / h[:2]
    shard.mkdir(parents=True, exist_ok=True)

    # Reuse an existing object with this hash regardless of extension.
    for existing in shard.glob(f"{h}.*"):
        if existing.suffix != ".tmp":
            return existing, h
    exact = shard / h  # extension-less variant
    if exact.exists():
        return exact, h

    dest = shard / f"{h}{ext}"
    atomic_write(dest, content)
    return dest, h


# ---------------------------------------------------------------------------
# Failure log
# ---------------------------------------------------------------------------


def log_failure(fail_log: Path, source: str, url: str, error: str) -> None:
    """Append a tab-delimited failure line to the failure log, credentials redacted."""
    with fail_log.open("a", encoding="utf-8") as f:
        f.write(f"{source}\t{redact_secrets(url)}\t{redact_secrets(error)}\n")


# ---------------------------------------------------------------------------
# Manifest writer (incremental)
# ---------------------------------------------------------------------------


def append_record(record: FetchRecord, manifest_path: Path) -> None:
    """
    Append a single FetchRecord to manifest.jsonl immediately.

    Fetchers call this right after producing each record (not once at the end
    of the whole run) so that a crash or kill mid-run loses at most the one
    attempt in flight, not every record collected so far.
    """
    row = record.to_dict()
    for key in ("canonical_url", "error"):
        if isinstance(row.get(key), str):
            row[key] = redact_secrets(row[key])
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Core fetch with SSL fallback
# ---------------------------------------------------------------------------


def fetch_url(
    session: requests.Session,
    url: str,
    extra_headers: dict | None = None,
) -> requests.Response:
    """
    GET *url* with the standard ontology headers plus any extra_headers.
    Retries without SSL verification only if the first attempt raises an
    SSLError (typically a host serving an incomplete certificate chain).
    Raises on HTTP errors (response.raise_for_status()).

    Connection/read timeouts, DNS failures, and other non-SSL errors are NOT
    retried here: they fail identically the second time, so retrying would
    only double the wait on every dead host.
    """
    hdrs = {**HEADERS, **(extra_headers or {})}

    # (connect_timeout, read_timeout): fail fast on unreachable hosts instead
    # of waiting the full read timeout just to find out the TCP handshake
    # never completed.
    timeout = (10, TIMEOUT)

    def _get(verify: bool) -> requests.Response:
        return session.get(
            url,
            headers=hdrs,
            timeout=timeout,
            allow_redirects=True,
            verify=verify,
        )

    try:
        r = _get(verify=True)
    except requests.exceptions.SSLError:
        print("  [ssl retry]")
        r = _get(verify=False)

    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# High-level download helper
# ---------------------------------------------------------------------------


def download_ontology(
    session: requests.Session,
    url: str,
    objects_dir: Path,
    source: str,
    fail_log: Path,
    parse_html_for_links: bool = False,
    reject_non_schema: bool = False,
) -> tuple[bool, str, str, str, str]:
    """
    Fetch *url* and store the result in the content-addressed object store
    rooted at *objects_dir* (keyed by the SHA-256 of the raw bytes, immutable).

    Responses that look like HTML are rejected (some hosts serve a landing
    page or similar instead of the file); with *parse_html_for_links* the
    page is first scraped for candidate ontology links, which are tried in
    priority order. RSS/Atom feeds, empty bodies, SPARQL service
    descriptions and zipped source-code repositories are rejected too.
    Every failure is appended to *fail_log*.

    Args:
        parse_html_for_links: If True and the response is HTML, try the
            ontology links found in the page before failing.
        reject_non_schema: If True, also reject content classified as
            "instance_data" or "catalog_metadata" by `classify_content_kind()`
            — i.e. only accept content that actually defines
            classes/properties. Off by default: curated registries (OBO, LOV,
            OntoPortal) already only list real ontologies, so this heuristic
            would be redundant noise there. Turn it on for crawl-based sources
            where linked files are unvetted and may turn out to be bare data
            dumps.

    Returns:
        (success, saved_path, file_hash, error_message, content_kind):
            success       – True if a file was written or already existed
            saved_path    – path relative to the store root
                            (``objects/<hh>/<file>``); empty on failure
            file_hash     – SHA-256 hex of the stored bytes; empty on failure
            error_message – non-empty string on failure, empty on success
            content_kind  – classify_content_kind() result; "" on failure
    """
    try:
        r = fetch_url(session, url)
        ext = extension_from_response(r, str(r.url))

        if looks_like_html(r.content):
            if parse_html_for_links:
                print(f"  [html page] Parsing links from: {url}")
                candidates = extract_ontology_links_from_html(r.text, str(r.url))
                for cand in candidates:
                    print(f"    [try candidate] {cand}")
                    ok, path, fhash, err, kind = download_ontology(
                        session,
                        cand,
                        objects_dir,
                        source,
                        fail_log,
                        parse_html_for_links=False,
                        reject_non_schema=reject_non_schema,
                    )
                    if ok:
                        return ok, path, fhash, err, kind
            raise RuntimeError("received HTML instead of RDF")

        if looks_like_feed(r.content):
            raise RuntimeError(
                "received RSS/Atom feed instead of RDF "
                "(URL returned a syndication feed, not an ontology)"
            )

        if not r.content or not r.content.strip():
            raise RuntimeError("received empty content")

        if looks_like_sparql_service_description(r.content):
            raise RuntimeError("received a SPARQL service/endpoint description, not ontology data")

        if r.content.startswith(b"PK\x03\x04") and zip_looks_like_source_repo(r.content):
            raise RuntimeError(
                "received a zip archive that looks like a source-code repository "
                "(README/LICENSE/.git*), not an ontology/data dump"
            )

        content_kind = classify_content_kind(r.content)
        if reject_non_schema and content_kind in ("instance_data", "catalog_metadata"):
            raise RuntimeError(f"content classified as '{content_kind}', not an ontology schema")

        dest, file_hash = store_object(objects_dir, r.content, ext)
        size_kb = len(r.content) // 1024
        print(f"  [ok] {source}: {dest.name} ({size_kb} KB) [{content_kind}]")
        # Recorded relative to the store root (``objects/<hh>/<file>``), so the
        # manifest resolves wherever the store is mounted.
        return True, dest.relative_to(objects_dir.parent).as_posix(), file_hash, "", content_kind

    except Exception as exc:
        # The URL may carry an API key, and HTTP errors quote the URL.
        msg = redact_secrets(str(exc)) or ""
        print(f"  [err] {source}: {redact_secrets(url)} -> {msg}")
        log_failure(fail_log, source, url, msg)
        return False, "", "", msg, ""
