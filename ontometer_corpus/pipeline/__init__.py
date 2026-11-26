"""
Processing that runs after fetching (``ontometer-corpus fetch``); ``ontometer-corpus
process`` drives it in three phases:

    Phase 0 (db.py)          — Seed corpus_registry and fetch_attempts from
                               manifest.jsonl.

    Phase 1 (parse.py)       — Parse each raw file with rdflib / custom /
                               streaming / owlready2 parsers; extract identity,
                               metrics, content hash, namespace footprint, and
                               per-file structural sidecars. Write results
                               to corpus_registry (SQLite) + the sidecar store.

    Phase 2 (dedup.py)       — Identify duplicate ontologies across registries
                               by raw-file hash (Pass 0), declared ontology IRI
                               + version IRI (Pass A), and corroborated content
                               signatures (Pass B). Unversioned signature
                               matches remain distinct releases. Then select one
                               latest canonical representative per registry
                               release family (Pass C).

    db.py                    — SQLite schema, connection factory, and manifest
                               seeding helpers shared by all phases.

The ontology dependency network is built downstream of this package from
corpus.db + the sidecars — see :mod:`ontometer_corpus.network`.
"""

from __future__ import annotations

from ontometer_corpus._extras import require

require("build", "rdflib", "pyoxigraph", "pyarrow", "suthing")
