"""The latest-version flag never outlives a row's canonical status."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontometer_corpus.pipeline.db import create_schema, get_connection
from ontometer_corpus.pipeline.dedup import mark_latest_versions, run_dedup


def _insert(conn, source_id: str, date: str, ontology_iri: str = "http://ex.org/o") -> None:
    conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            parse_status, ontology_iri, source_version_date, is_duplicate,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'LOV', 'ok', ?, ?, 0, ?, ?)
        """,
        (
            source_id,
            f"http://ex.org/{source_id}",
            f"objects/{source_id}",
            date,
            ontology_iri,
            date,
            date,
            date,
        ),
    )


def test_a_row_that_becomes_a_duplicate_loses_its_latest_flag(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _insert(conn, "lov:o::1", "2020-01-01")
    _insert(conn, "lov:o::2", "2021-01-01")
    assert mark_latest_versions(conn) == 1

    def latest() -> list[str]:
        rows = conn.execute("SELECT source_id FROM corpus_registry WHERE is_latest_version = 1")
        return [r[0] for r in rows]

    assert latest() == ["lov:o::2"]
    conn.execute("UPDATE corpus_registry SET is_duplicate = 1 WHERE source_id = 'lov:o::2'")
    mark_latest_versions(conn)
    assert latest() == ["lov:o::1"]


def test_versioned_submissions_with_changing_iris_keep_one_latest_family_row(
    tmp_path: Path,
) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _insert(conn, "bp:PTO::276", "2024-01-01", "http://ex.org/generated-276")
    _insert(conn, "bp:PTO::277", "2025-01-01", "http://ex.org/generated-277")

    assert mark_latest_versions(conn) == 1
    rows = conn.execute(
        "SELECT source_id FROM corpus_registry WHERE is_latest_version = 1"
    ).fetchall()
    assert [row[0] for row in rows] == ["bp:PTO::277"]


def _copy(conn, source_id, registry, iri, version_iri, content_hash, n_classes, n_concepts=0):
    conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            parse_status, ontology_iri, ontology_version_iri, file_hash, content_hash, class_count,
            skos_concept_count, is_duplicate, is_latest_version, created_at, updated_at
        ) VALUES (?, ?, ?, '2026-07-01', ?, 'ok', ?, ?, ?, ?, ?, ?, 0, 1, 'x', 'x')
        """,
        (
            source_id,
            source_id,
            source_id,
            registry,
            iri,
            version_iri,
            content_hash,
            content_hash,
            n_classes,
            n_concepts,
        ),
    )


def _latest(conn) -> list[str]:
    rows = conn.execute(
        "SELECT source_id FROM corpus_registry WHERE is_latest_version = 1 ORDER BY id"
    )
    return [r[0] for r in rows]


def _unversioned_release(
    conn, source_id: str, ontology_iri: str, file_hash: str, content_hash: str, triples: int
) -> None:
    conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, file_hash, retrieval_date,
            source_registry, parse_status, ontology_iri, ontology_version_iri,
            content_hash, triple_count, class_count, property_count,
            is_duplicate, created_at, updated_at
        ) VALUES (?, ?, ?, ?, '2026-07-01', 'BioPortal', 'ok', ?, NULL,
                  ?, ?, 2, 0, 0, 'x', 'x')
        """,
        (source_id, source_id, source_id, file_hash, ontology_iri, content_hash, triples),
    )
    conn.commit()


def test_unversioned_releases_with_same_coarse_hash_are_not_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    conn = get_connection(db)
    create_schema(conn)
    _unversioned_release(conn, "bp:EX::1", "http://ex.org/o", "file-old", "entities", 20)
    _unversioned_release(conn, "bp:EX::2", "http://ex.org/o", "file-new", "entities", 30)
    # Simulate the old A2 rule so the rerun must actively restore this row.
    conn.execute(
        "UPDATE corpus_registry SET is_duplicate = 1, canonical_entry_id = 2 WHERE source_id = 'bp:EX::1'"
    )
    conn.commit()
    conn.close()

    run_dedup(db, sidecar_path=None)

    conn = get_connection(db)
    rows = conn.execute(
        "SELECT source_id, is_duplicate, is_latest_version FROM corpus_registry ORDER BY source_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("bp:EX::1", 0, 0), ("bp:EX::2", 0, 1)]


def test_coarse_hash_alone_does_not_merge_different_ontologies(tmp_path: Path) -> None:
    db = tmp_path / "corpus.db"
    conn = get_connection(db)
    create_schema(conn)
    _unversioned_release(conn, "bp:A::1", "http://ex.org/a", "file-a", "entities", 20)
    _unversioned_release(conn, "bp:B::1", "http://ex.org/b", "file-b", "entities", 30)
    conn.close()

    run_dedup(db, sidecar_path=None)

    conn = get_connection(db)
    counts = conn.execute(
        "SELECT SUM(is_duplicate), SUM(is_latest_version) FROM corpus_registry"
    ).fetchone()
    assert tuple(counts) == (0, 2)


def test_copies_of_one_ontology_across_registries_keep_the_newest(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    ro = "http://purl.obolibrary.org/obo/ro.owl"
    _copy(
        conn,
        "agroportal:RO::7",
        "AgroPortal",
        ro,
        ro[:-6] + "releases/2017-09-22/ro.owl",
        "h17",
        25,
    )
    _copy(conn, "okg:Q28729320", "OKG", ro, ro[:-6] + "releases/2025-12-17/ro.owl", "h25", 58)
    terms = {"h17": {f"t{i}" for i in range(20)}, "h25": {f"t{i}" for i in range(40)}}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h, set())) == (1, 0, 0)
    assert _latest(conn) == ["okg:Q28729320"]


def test_spellings_of_one_iri_are_one_ontology(tmp_path: Path) -> None:
    """http/https and trailing-separator spellings of an IRI are one ontology,
    recognised even though the copies spell their terms with different schemes."""
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "lov:schema::2020", "LOV", "http://schema.org/", None, "old", 20)
    _copy(conn, "biodiv:SCHEMA::3", "BiodivPortal", "https://schema.org", None, "new", 40)
    conn.execute("UPDATE corpus_registry SET source_version_date = '2020-03-10' WHERE id = 1")
    conn.execute("UPDATE corpus_registry SET source_version_date = '2022-10-25' WHERE id = 2")
    terms = {
        "old": {f"http://schema.org/T{i}" for i in range(20)},
        "new": {f"https://schema.org/T{i}" for i in range(40)},
    }

    assert unify_latest_by_iri(conn, lambda h: terms.get(h, set())) == (1, 0, 0)
    assert _latest(conn) == ["biodiv:SCHEMA::3"]
    assert conn.execute("SELECT SUM(shared_iri) FROM corpus_registry").fetchone()[0] == 0


def test_distinct_ontologies_sharing_an_iri_stay_latest_and_lose_the_iri(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "https://cropontology.org/rdf/"
    _copy(conn, "agroportal:CO_321::1", "AgroPortal", iri, None, "wheat", 10)
    _copy(conn, "agroportal:CO_322::1", "AgroPortal", iri, None, "maize", 10)
    terms = {"wheat": {"CO_321:1", "CO_321:2"}, "maize": {"CO_322:1", "CO_322:2"}}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h, set())) == (0, 1, 0)
    assert _latest(conn) == ["agroportal:CO_321::1", "agroportal:CO_322::1"]
    shared = conn.execute("SELECT DISTINCT shared_iri FROM corpus_registry").fetchall()
    assert [r[0] for r in shared] == [1]


def test_dated_copy_beats_undated_and_size_breaks_ties(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://ex.org/o"
    _copy(conn, "okg:Q1", "OKG", iri, None, "big", 99)
    _copy(conn, "lov:http://ex.org/o::v1", "LOV", iri, "http://ex.org/o/2024-01-01", "small", 9)
    terms = {"big": {"a", "b", "c"}, "small": {"a", "b"}}

    unify_latest_by_iri(conn, lambda h: terms.get(h, set()))
    assert _latest(conn) == ["lov:http://ex.org/o::v1"]


def test_schema_migration_is_idempotent_with_the_generated_kind_column(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    create_schema(conn)
    _copy(conn, "x:1", "LOV", "http://ex.org/o", None, "h", 0)
    conn.execute("UPDATE corpus_registry SET skos_concept_count = 5")
    assert conn.execute("SELECT kind FROM corpus_registry").fetchone()[0] == "thesaurus"


def test_obo_releases_and_empty_copies_stay_one_ontology(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    ogms = "http://purl.obolibrary.org/obo/ogms.owl"
    base = "http://purl.obolibrary.org/obo/ogms/"
    _copy(conn, "obo:ogms", "OBO", ogms, base + "2021-08-19/ogms.owl", "old", 186)
    _copy(conn, "okg:Q55118512", "OKG", ogms, base + "releases/2025-07-02/ogms.owl", "new", 17)
    _copy(conn, "ontohub:OGMS.owl", "Ontohub", ogms, None, "empty", 0)
    terms = {"old": {f"o{i}" for i in range(10)}, "new": {"o1", "n1", "n2", "n3", "n4", "n5"}}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h, set())) == (2, 0, 0)
    assert _latest(conn) == ["okg:Q55118512"]


def test_thesauri_sharing_a_schema_are_not_merged(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://aims.fao.org/aos/agrontology"
    _copy(conn, "agroportal:AGROVOC::61", "AgroPortal", iri, None, "agrovoc", 0, 41841)
    _copy(conn, "agroportal:ASFA::1", "AgroPortal", iri, None, "asfa", 0, 5131)
    schema = {f"p{i}" for i in range(170)}
    assert unify_latest_by_iri(conn, lambda h: schema) == (0, 1, 0)
    assert _latest(conn) == ["agroportal:AGROVOC::61", "agroportal:ASFA::1"]


@pytest.mark.parametrize(
    ("iri", "version", "expected"),
    [
        (
            "http://purl.obolibrary.org/obo/bfo.owl",
            "http://purl.obolibrary.org/obo/bfo/2020/bfo-core.ttl",
            "2020",
        ),
        (
            "http://purl.obolibrary.org/obo/ro.owl",
            "http://purl.obolibrary.org/obo/ro/releases/2025-06-12/ro.owl",
            "2025-06-12",
        ),
        (
            "http://www.semanticweb.org/u/ontologies/2020/7/OMSSA",
            "http://www.semanticweb.org/u/ontologies/2020/7/V1.0.0/OMSSA",
            None,
        ),
        ("http://www.w3.org/2006/time", "http://www.w3.org/2006/time#2016", None),
        ("http://ex.org/ontologies/2015/aeo", "http://ex.org/ontologies/2015/aeo", None),
        (None, "http://ex.org/o/2019-03-01/o.owl", "2019-03-01"),
        ("http://a.org/o", "http://b.org/releases/2021/o.owl", "2021"),
        ("http://ex.org/o", None, None),
        ("http://example.org/onto/2019", "http://example.org/onto/2019/1.1/onto.owl", None),
    ],
)
def test_version_iri_date_ignores_years_in_the_shared_namespace(iri, version, expected) -> None:
    from ontometer_corpus.pipeline.dedup import _version_iri_date

    assert _version_iri_date(iri, version) == expected


def test_release_day_falls_back_to_the_registry_when_the_version_iri_adds_no_date() -> None:
    from ontometer_corpus.pipeline.dedup import release_day

    row = {
        "ontology_iri": "http://www.semanticweb.org/u/ontologies/2020/7/OMSSA",
        "ontology_version_iri": "http://www.semanticweb.org/u/ontologies/2020/7/V1.0.0/OMSSA",
        "source_registry_metadata": None,
        "source_version_date": "2025-03-31T00:00:00",
    }
    assert release_day(row) == "2025-03-31"


def _release(
    conn,
    source_id: str,
    date: str,
    *,
    classes: int = 0,
    props: int = 0,
    concepts: int = 0,
    individuals: int = 0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            parse_status, ontology_iri, source_version_date, class_count, property_count,
            individual_count, declared_individual_count, skos_concept_count, is_duplicate,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'LOV', 'ok', 'http://ex.org/o', ?, ?, ?, ?, 0, ?, 0, 'x', 'x')
        """,
        (source_id, source_id, source_id, date, date, classes, props, individuals, concepts),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mark_duplicate(conn, row_id: int, canonical_id: int) -> None:
    conn.execute(
        "UPDATE corpus_registry SET is_duplicate = 1, canonical_entry_id = ? WHERE id = ?",
        (canonical_id, row_id),
    )
    conn.commit()


def test_newest_stub_release_does_not_displace_a_populated_one(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _release(conn, "lov:o::v2013", "2013-06-03", classes=14, props=5)
    _release(conn, "lov:o::v2015", "2015-01-11")
    mark_latest_versions(conn)
    assert _latest(conn) == ["lov:o::v2013"]


def test_all_stub_family_still_flags_the_newest(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _release(conn, "lov:o::v1", "2013-01-01")
    _release(conn, "lov:o::v2", "2014-01-01")
    assert mark_latest_versions(conn) == 1
    assert _latest(conn) == ["lov:o::v2"]


def test_canonical_row_competes_with_its_newest_duplicates_date(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    canonical = _release(conn, "lov:o::v2014-02", "2014-02-16", props=268)
    _release(conn, "lov:o::v2014-05", "2014-05-31", props=10)
    newest = _release(conn, "lov:o::v2014-06", "2014-06-03", props=268)
    _mark_duplicate(conn, newest, canonical)
    mark_latest_versions(conn)
    assert _latest(conn) == ["lov:o::v2014-02"]


def test_duplicate_chains_point_at_the_final_canonical(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import _flatten_canonical_chains

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    a = _release(conn, "lov:o::1", "2020-01-01", classes=1)
    b = _release(conn, "lov:o::2", "2021-01-01", classes=1)
    c = _release(conn, "lov:o::3", "2022-01-01", classes=1)
    _mark_duplicate(conn, b, a)
    _mark_duplicate(conn, c, b)
    assert _flatten_canonical_chains(conn) == 1
    row = conn.execute(
        "SELECT canonical_entry_id FROM corpus_registry WHERE id = ?", (c,)
    ).fetchone()
    assert row[0] == a


def test_flatten_terminates_on_a_cycle(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import _flatten_canonical_chains

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    a = _release(conn, "lov:o::1", "2020-01-01", classes=1)
    b = _release(conn, "lov:o::2", "2021-01-01", classes=1)
    _mark_duplicate(conn, a, b)
    _mark_duplicate(conn, b, a)
    _flatten_canonical_chains(conn)  # must return, whatever it repoints


def test_stub_copy_with_a_newer_date_does_not_win_pass_d(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://purl.obolibrary.org/obo/x.owl"
    base = "http://purl.obolibrary.org/obo/x/releases/"
    _copy(conn, "obo:x", "OBO", iri, base + "2024-01-01/x.owl", "full", 50)
    _copy(conn, "agroportal:X::9", "AgroPortal", iri, base + "2025-01-01/x.owl", "empty", 0)
    terms = {"full": {f"t{i}" for i in range(50)}, "empty": set()}

    unify_latest_by_iri(conn, lambda h: terms.get(h, set()))
    assert _latest(conn) == ["obo:x"]


def test_missing_sidecar_leaves_the_iri_group_undecided(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://ex.org/o"
    _copy(conn, "lov:o::1", "LOV", iri, None, "known", 10)
    _copy(conn, "okg:Q1", "OKG", iri, None, "missing", 10)
    terms: dict[str, set[str] | None] = {"known": {"a", "b"}, "missing": None}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (0, 0, 1)
    assert _latest(conn) == ["lov:o::1", "okg:Q1"]
    shared = conn.execute("SELECT SUM(shared_iri) FROM corpus_registry").fetchone()[0]
    assert shared == 0


def test_unreadable_or_absent_sidecar_reads_as_no_evidence(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import _sidecar_entities

    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    paths = {"bad": bad, "absent": tmp_path / "absent.parquet"}
    entities = _sidecar_entities(lambda h: paths[h])
    assert entities("bad") is None
    assert entities("absent") is None
    assert entities(None) is None


def test_a_release_of_typed_individuals_only_is_not_a_stub(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _release(conn, "lov:o::v1", "2013-01-01", classes=1)
    _release(conn, "lov:o::v2", "2014-01-01", individuals=5000)
    mark_latest_versions(conn)
    assert _latest(conn) == ["lov:o::v2"]


def test_pass_d_ranks_a_canonical_copy_by_its_newest_duplicates_date(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import _newest_duplicate_dates, unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://ex.org/o"
    _copy(conn, "lov:http://ex.org/o::v2014-02", "LOV", iri, None, "lov", 10)
    _copy(conn, "bp:O::1", "BioPortal", iri, None, "bp", 10)
    _copy(conn, "lov:http://ex.org/o::v2014-06", "LOV", iri, None, "lovdup", 10)
    for source_id, date in (
        ("lov:http://ex.org/o::v2014-02", "2014-02-16"),
        ("bp:O::1", "2014-04-01"),
        ("lov:http://ex.org/o::v2014-06", "2014-06-03"),
    ):
        conn.execute(
            "UPDATE corpus_registry SET source_version_date = ? WHERE source_id = ?",
            (date, source_id),
        )
    conn.execute(
        "UPDATE corpus_registry SET is_duplicate = 1, is_latest_version = 0, canonical_entry_id = "
        "(SELECT id FROM corpus_registry WHERE source_id = 'lov:http://ex.org/o::v2014-02') "
        "WHERE source_id = 'lov:http://ex.org/o::v2014-06'"
    )
    conn.commit()
    terms = {"lov": {"a", "b"}, "bp": {"a", "b"}}

    unify_latest_by_iri(conn, lambda h: terms.get(h), _newest_duplicate_dates(conn))
    assert _latest(conn) == ["lov:http://ex.org/o::v2014-02"]


@pytest.mark.parametrize(
    ("iri", "key"),
    [
        ("http://purl.obolibrary.org/obo/pco/pco-full.owl", "purl.obolibrary.org/obo/pco.owl"),
        ("http://purl.obolibrary.org/obo/oba/oba-base.owl", "purl.obolibrary.org/obo/oba.owl"),
        ("http://purl.obolibrary.org/obo/clo-merged.owl", "purl.obolibrary.org/obo/clo.owl"),
        (
            "http://purl.obolibrary.org/obo/go/extensions/go-plus.owl",
            "purl.obolibrary.org/obo/go.owl",
        ),
        (
            "http://purl.obolibrary.org/obo/subsets/envo-basic.obo.owl",
            "purl.obolibrary.org/obo/envo.owl",
        ),
        ("http://purl.obolibrary.org/obo/dideo/dev/dideo.owl", "purl.obolibrary.org/obo/dideo.owl"),
        ("http://purl.obolibrary.org/obo/MF.owl", "purl.obolibrary.org/obo/mf.owl"),
        ("http:///purl.obolibrary.org/obo/ogsf.owl", "purl.obolibrary.org/obo/ogsf.owl"),
        (
            "http://purl.obolibrary.org/obo/uberon/chebi_import.owl",
            "purl.obolibrary.org/obo/uberon/chebi_import.owl",
        ),
        ("http://purl.obolibrary.org/obo/bco/sample.owl", "purl.obolibrary.org/obo/bco/sample.owl"),
        ("https://schema.org/", "schema.org"),
    ],
)
def test_obo_build_artifacts_name_their_ontology(iri: str, key: str) -> None:
    from ontometer_corpus.pipeline.dedup import ontology_key

    assert ontology_key(iri) == key


def test_a_full_artifact_and_the_purl_are_one_ontology(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(
        conn,
        "bp:OHPI::11",
        "BioPortal",
        "http://purl.obolibrary.org/obo/ohpi/ohpi-full.owl",
        None,
        "full",
        50,
    )
    _copy(conn, "okg:Q1", "OKG", "http://purl.obolibrary.org/obo/ohpi.owl", None, "purl", 40)
    terms = {"full": {f"t{i}" for i in range(50)}, "purl": {f"t{i}" for i in range(40)}}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (1, 0, 0)
    assert _latest(conn) == ["bp:OHPI::11"]


def test_hash_and_slash_term_spellings_are_one_ontology(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "biodivportal:APTO::1", "BiodivPortal", "https://w3id.org/APTO#", None, "hash", 20)
    _copy(conn, "agroportal:APTO::2", "AgroPortal", "https://w3id.org/APTO/", None, "slash", 21)
    terms = {
        "hash": {f"https://w3id.org/APTO#T{i}" for i in range(20)},
        "slash": {f"https://w3id.org/APTO/T{i}" for i in range(21)},
    }

    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (1, 0, 0)
    assert conn.execute("SELECT SUM(shared_iri) FROM corpus_registry").fetchone()[0] == 0


def test_a_module_in_the_same_registry_stays_its_own_ontology(tmp_path: Path) -> None:
    """A registry's separate entries under one IRI need comparable term sets;
    a smaller copy in another registry still joins (an older release)."""
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    iri = "http://ex.org/o"
    _copy(conn, "bp:O::1", "BioPortal", iri, None, "whole", 100)
    _copy(conn, "bp:OMODULE::1", "BioPortal", iri, None, "module", 10)
    _copy(conn, "ontohub:o.owl", "Ontohub", "http://ex.org/o2", None, "old", 10)
    whole = {f"t{i}" for i in range(100)}
    terms = {"whole": whole, "module": {f"t{i}" for i in range(10)}, "old": set()}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (0, 1, 0)
    assert _latest(conn) == ["bp:O::1", "bp:OMODULE::1", "ontohub:o.owl"]

    conn.execute(
        "UPDATE corpus_registry SET source_registry = 'OKG' WHERE source_id = 'bp:OMODULE::1'"
    )
    conn.execute("UPDATE corpus_registry SET is_latest_version = 1")
    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (1, 0, 0)
    assert _latest(conn) == ["bp:O::1", "ontohub:o.owl"]


def test_identical_term_sets_are_one_ontology_without_an_iri(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "bp:STY::23", "BioPortal", None, None, "sty", 127)
    _copy(conn, "ontohub:STY.ttl", "Ontohub", None, None, "sty", 127)
    _copy(conn, "lov:geo::v1", "LOV", "http://ex.org/geo", None, "geo", 7)
    _copy(conn, "okg:Q2", "OKG", None, None, "geo", 7)

    assert unify_latest_by_iri(conn, lambda h: None) == (2, 0, 0)
    assert _latest(conn) == ["bp:STY::23", "lov:geo::v1"]


def test_small_identical_sets_under_different_iris_stay_apart(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import MIN_IDENTICAL_TERMS, unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "okg:Q1", "OKG", "http://ex.org/a/About", None, "about", MIN_IDENTICAL_TERMS - 1)
    _copy(conn, "okg:Q2", "OKG", "http://ex.org/b/About", None, "about", MIN_IDENTICAL_TERMS - 1)
    _copy(conn, "lov:moved::1", "LOV", "http://old.org/v", None, "big", MIN_IDENTICAL_TERMS)
    _copy(conn, "okg:Q3", "OKG", "https://new.org/v", None, "big", MIN_IDENTICAL_TERMS)

    assert unify_latest_by_iri(conn, lambda h: None) == (1, 0, 0)
    assert len(_latest(conn)) == 3


def test_identical_schema_sets_of_different_thesauri_stay_apart(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "astro:A::1", "OntoPortal-Astro", None, None, "schema", 30, 400)
    _copy(conn, "astro:B::1", "OntoPortal-Astro", None, None, "schema", 30, 12)

    assert unify_latest_by_iri(conn, lambda h: None) == (0, 0, 0)


def test_latest_rows_declaring_nothing_are_not_ontologies(tmp_path: Path) -> None:
    from ontometer_corpus.pipeline.dedup import drop_stub_latest

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _release(conn, "lov:o::v1", "2013-01-01", classes=3)
    _release(conn, "lov:p::v1", "2013-01-01")
    _release(conn, "lov:q::v1", "2013-01-01", concepts=4)
    conn.execute("UPDATE corpus_registry SET is_latest_version = 1")
    log = tmp_path / "excl.log"

    assert drop_stub_latest(conn, log) == 1
    assert _latest(conn) == ["lov:o::v1", "lov:q::v1"]
    assert "lov:p::v1" in log.read_text()


def test_a_copy_of_an_earlier_release_joins_its_family(tmp_path: Path) -> None:
    """The newest release renamed every term; another registry's copy equals
    an earlier release of that family."""
    from ontometer_corpus.pipeline.dedup import unify_latest_by_iri

    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    _copy(conn, "agroportal:APTO::12", "AgroPortal", "https://w3id.org/APTO/", None, "renamed", 581)
    _copy(conn, "agroportal:APTO::4", "AgroPortal", "https://w3id.org/APTO#", None, "old", 580)
    _copy(conn, "biodivportal:APTO::1", "BiodivPortal", "https://w3id.org/APTO#", None, "old", 580)
    conn.execute(
        "UPDATE corpus_registry SET is_latest_version = 0 WHERE source_id = 'agroportal:APTO::4'"
    )
    conn.execute("UPDATE corpus_registry SET source_version_date = '2025-01-01' WHERE id = 1")
    terms = {"renamed": {f"PT{i}" for i in range(581)}, "old": {f"Name{i}" for i in range(580)}}

    assert unify_latest_by_iri(conn, lambda h: terms.get(h)) == (1, 0, 0)
    assert _latest(conn) == ["agroportal:APTO::12"]
    assert conn.execute("SELECT SUM(shared_iri) FROM corpus_registry").fetchone()[0] == 0
