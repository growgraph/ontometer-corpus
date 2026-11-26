"""Parse artifacts use complete-file identity, not the coarse entity signature."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ontometer_corpus.paths import CorpusPaths
from ontometer_corpus.pipeline import parse
from ontometer_corpus.pipeline.db import create_schema, get_connection
from ontometer_corpus.steps.entity_network import _ontologies


def test_finalize_writes_sidecars_by_file_hash(monkeypatch) -> None:
    written: list[str] = []

    def capture(file_hash: str, edges, entities, skos_concepts) -> None:
        written.append(file_hash)

    monkeypatch.setattr(parse, "_write_sidecar", capture)
    for file_hash in ("raw-a", "raw-b"):
        meta = {
            "content_hash": "same-coarse-signature",
            "subclass_edges": [],
            "entities": [],
            "skos_concepts": [],
        }
        parse._finalize_result((1, "ok", "turtle", meta, None), file_hash=file_hash)

    assert written == ["raw-a", "raw-b"]


def test_existing_sidecar_gains_skos_concepts_without_rewriting_structure(
    monkeypatch, tmp_path: Path
) -> None:
    sidecar = tmp_path / "sidecar.parquet"
    monkeypatch.setattr(parse, "_sidecar_path", lambda _: sidecar)
    parse._write_sidecar("raw", [("child", "parent")], [("term", "class")], [])
    parse._write_sidecar("raw", [], [], ["https://example.org/concepts/one"])

    records = pq.read_table(sidecar).to_pandas()
    assert records.record_type.tolist() == ["subclass_edge", "entity", "skos_concept"]
    assert records.subject.tolist() == ["child", "term", "https://example.org/concepts/one"]


def test_entity_population_keeps_latest_rows_with_same_content_hash(tmp_path: Path) -> None:
    paths = CorpusPaths(corpus_dir=tmp_path, store_dir=tmp_path / "store")
    conn = get_connection(paths.db_path)
    create_schema(conn)
    for i, file_hash in enumerate(("raw-a", "raw-b"), start=1):
        conn.execute(
            """
            INSERT INTO corpus_registry (
                source_id, source_registry, canonical_url, retrieved_file,
                file_hash, retrieval_date, parse_status, content_hash,
                class_count, property_count, individual_count,
                is_duplicate, is_latest_version, created_at, updated_at
            ) VALUES (?, 'BioPortal', ?, ?, ?, '2026-08-06', 'ok',
                      'same-coarse-signature', 2, 0, 0, 0, 1, 'x', 'x')
            """,
            (f"bp:O{i}::1", f"https://example.org/{i}", f"objects/{file_hash}", file_hash),
        )
    conn.commit()
    conn.close()
    pd.DataFrame(
        {
            "node_id": ["row:1", "row:2"],
            "file_hash": ["raw-a", "raw-b"],
            "is_external": [False, False],
        }
    ).to_parquet(paths.network_nodes_path, index=False)

    onts = _ontologies(paths)

    assert len(onts) == 2
    assert set(onts.file_hash) == {"raw-a", "raw-b"}
