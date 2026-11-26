"""--reparse clears exactly what parse and dedup gate on."""

from __future__ import annotations

from pathlib import Path

from ontometer_corpus.pipeline.db import create_schema, get_connection, reset_parse_state


def test_reset_parse_state_clears_parse_and_dedup_columns(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            parse_status, ontology_iri, class_count, is_duplicate, is_latest_version,
            shared_iri, created_at, updated_at
        ) VALUES ('lov:o::1', 'u', 'objects/x', '2026-01-01', 'LOV', 'ok',
                  'http://ex.org/o', 3, 1, 1, 1, 'x', 'x')
        """
    )
    conn.commit()
    assert reset_parse_state(conn) == 1
    row = conn.execute(
        "SELECT source_id, parse_status, ontology_iri, class_count, is_duplicate, "
        "is_latest_version, shared_iri FROM corpus_registry"
    ).fetchone()
    assert tuple(row) == ("lov:o::1", None, None, None, 0, 0, 0)
