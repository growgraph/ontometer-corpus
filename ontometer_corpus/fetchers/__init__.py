"""
Lightweight per-source ontology fetchers. Each module exposes a single
    fetch_<source>() function that records one FetchRecord per download
    attempt (appended to manifest.jsonl as it is produced) and returns the
    list of records. Every record shares the normalised schema defined in
    ``_base.FetchRecord``:
        (source_id, canonical_url, retrieved_file, retrieval_date,
         source_registry, source_registry_metadata, status, file_hash, error,
         source_version, source_version_date, content_kind)
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("build", "requests", "bs4", "suthing")
