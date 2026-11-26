"""The home registry of an ontology held by several registries."""

from __future__ import annotations

from pathlib import Path

from ontometer_corpus.pipeline.db import create_schema, get_connection
from ontometer_corpus.registries import (
    assign_home_registries,
    home_registry,
    hosted_on,
    iri_key,
)


def _row(conn, source_id, registry, iri, file_hash, latest=1, shared=0) -> None:
    conn.execute(
        """
        INSERT INTO corpus_registry (
            source_id, canonical_url, retrieved_file, retrieval_date, source_registry,
            parse_status, ontology_iri, file_hash, is_duplicate, is_latest_version, shared_iri,
            created_at, updated_at
        ) VALUES (?, ?, ?, '2026-07-01', ?, 'ok', ?, ?, 0, ?, ?, 'x', 'x')
        """,
        (source_id, source_id, source_id, registry, iri, file_hash, latest, shared),
    )


def _homes(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT source_id, home_registry FROM corpus_registry"))


def test_iri_key_ignores_scheme_and_trailing_separator() -> None:
    assert iri_key("https://schema.org/") == iri_key("http://schema.org")
    assert iri_key("http://www.w3.org/ns/ssn/") == iri_key("https://www.w3.org/ns/ssn#")
    assert iri_key("http://ex.org/a") != iri_key("http://ex.org/b")


def test_home_is_the_most_specific_registry_holding_a_copy() -> None:
    foaf = "http://xmlns.com/foaf/0.1/"
    assert home_registry(foaf, {"BioPortal", "LOV", "OKG", "Ontohub"}) == "LOV"
    assert home_registry(foaf, {"OKG", "Ontohub"}) == "Ontohub"
    assert home_registry(None, {"OKG"}) == "OKG"
    # A registry outside the priority list ranks after every listed one.
    assert home_registry(foaf, {"SomewhereNew", "OKG"}) == "OKG"


def test_an_obo_purl_is_an_obo_ontology_wherever_it_was_fetched() -> None:
    assert home_registry("http://purl.obolibrary.org/obo/go-plus.owl", {"Ontohub"}) == "OBO"
    assert home_registry("http://purl.obolibrary.org/obo/bfo.owl", {"OKG", "AgroPortal"}) == "OBO"


def test_home_registries_are_written_for_every_row(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "corpus.db")
    create_schema(conn)
    # FOAF: LOV holds older versions, BioPortal the copy dedup kept as latest.
    _row(conn, "lov:foaf::v0.99", "LOV", "http://xmlns.com/foaf/0.1/", "foaf99", latest=0)
    _row(conn, "bp:FOAF::1", "BioPortal", "http://xmlns.com/foaf/0.1/", "foaf99")
    # schema.org under two spellings.
    _row(conn, "lov:schema", "LOV", "http://schema.org/", "s1", latest=0)
    _row(conn, "biodiv:SCHEMA::3", "BiodivPortal", "https://schema.org", "s2")
    # An OBO PURL the OBO registry does not hold.
    _row(conn, "ontohub:goplus", "Ontohub", "http://purl.obolibrary.org/obo/go-plus.owl", "gp")
    # The same file on two registries, with no ontology IRI.
    _row(conn, "okg:Q1", "OKG", None, "same-file")
    _row(conn, "bp:X::1", "BioPortal", None, "same-file", latest=0)
    # A shared IRI names several ontologies, so it is no evidence of a copy.
    _row(conn, "agro:CO_321::1", "AgroPortal", "https://cropontology.org/rdf/", "wheat", shared=1)
    _row(conn, "lov:crop", "LOV", "https://cropontology.org/rdf/", "other", shared=1)

    moved = assign_home_registries(conn)

    homes = _homes(conn)
    assert homes["bp:FOAF::1"] == homes["lov:foaf::v0.99"] == "LOV"
    assert homes["biodiv:SCHEMA::3"] == "LOV"
    assert homes["ontohub:goplus"] == "OBO"
    assert homes["okg:Q1"] == "BioPortal"
    assert homes["agro:CO_321::1"] == "AgroPortal"
    # Latest rows whose home differs from the registry they were fetched from.
    assert moved == 4
    hosts = hosted_on(conn)
    assert hosts[2] == {"LOV", "BioPortal"}
    assert hosts[8] == {"AgroPortal"}
