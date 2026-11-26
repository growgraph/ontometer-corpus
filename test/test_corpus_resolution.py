"""Corpus identity and resolution: registry IRI fallback, property counting,
namespace matching, ownership of popular vocabularies, OBO import modules, and
usage evidence on dependency edges."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import rdflib
from rdflib import OWL, RDF, URIRef

from ontometer_corpus.namespaces import reference_kind
from ontometer_corpus.network.construction import (
    _external_key,
    _ns_key,
    build_network,
    load_network,
    save_network,
)
from ontometer_corpus.network.entities import NamespaceOwnerRecord, resolve_namespace_owners
from ontometer_corpus.pipeline.parse import (
    _registry_declared_iri,
    compute_content_hash,
    extract_metadata,
)

FOAF = "http://xmlns.com/foaf/0.1/"
EX = "https://example.org/vocab#"


def test_ns_key_matches_hash_namespace_to_bare_iri():
    assert _ns_key("https://example.org/linked-data/cube#") == _ns_key(
        "https://example.org/linked-data/cube"
    )
    # A match key, not an IRI: https reads as http, so both spellings agree.
    assert _ns_key("https://example.org/voc/") == "http://example.org/voc"
    assert _ns_key("https://schema.org/") == _ns_key("http://schema.org")


def test_registry_iri_recovered_only_from_lov_ids():
    assert _registry_declared_iri("lov:http://purl.org/dc/terms/::2020-01-20") == (
        "http://purl.org/dc/terms/"
    )
    assert _registry_declared_iri("obo:go") is None
    assert _registry_declared_iri(None) is None


def _rdf_property_vocab() -> rdflib.Graph:
    """A pre-OWL vocabulary: no owl:Ontology, terms typed rdf:Property only."""
    g = rdflib.Graph()
    g.add((URIRef(EX + "creator"), RDF.type, RDF.Property))
    g.add((URIRef(EX + "title"), RDF.type, RDF.Property))
    return g


def test_rdf_property_counted_in_counter_and_hash():
    g = _rdf_property_vocab()
    meta = extract_metadata(g)
    assert meta["property_count"] == 2
    # The dedup proxy (class_count + property_count > 0) must agree with the
    # hash: a counted row may not fingerprint as the empty ontology.
    assert compute_content_hash(g) != hashlib.sha256(b"").hexdigest()


def test_undeclared_iri_falls_back_to_registry():
    meta = extract_metadata(_rdf_property_vocab(), "lov:https://example.org/vocab#::1.0")
    assert meta["ontology_iri"] == EX
    assert meta["iri_source"] == "registry"


def test_declared_iri_wins_over_registry():
    g = _rdf_property_vocab()
    g.add((URIRef("https://example.org/declared"), RDF.type, OWL.Ontology))
    meta = extract_metadata(g, "lov:https://example.org/vocab#::1.0")
    assert meta["ontology_iri"] == "https://example.org/declared"
    assert meta["iri_source"] == "declared"


_COLUMNS = (
    "id",
    "ontology_iri",
    "ontology_version_iri",
    "source_registry",
    "file_hash",
    "content_hash",
    "triple_count",
    "class_count",
    "property_count",
    "individual_count",
    "skos_concept_count",
    "imports",
    "referenced_namespaces",
    "own_namespaces",
    "shared_iri",
)


def _corpus(tmp_path: Path, rows: list[dict]) -> Path:
    """A corpus_registry holding only the columns build_network reads."""
    db = tmp_path / "corpus.db"
    conn = sqlite3.connect(db)
    conn.execute(
        f"CREATE TABLE corpus_registry ({', '.join(_COLUMNS)}, is_latest_version, parse_status)"
    )
    for i, row in enumerate(rows, start=1):
        values = (
            {c: None for c in _COLUMNS}
            | {
                "id": i,
                "file_hash": f"file-{i}",
                "triple_count": 10,
                "shared_iri": 0,
            }
            | row
        )
        for key in ("imports", "referenced_namespaces", "own_namespaces"):
            values[key] = json.dumps(values[key] or [])
        conn.execute(
            f"INSERT INTO corpus_registry VALUES ({', '.join('?' * len(_COLUMNS))}, 1, 'ok')",
            [values[c] for c in _COLUMNS],
        )
    conn.commit()
    conn.close()
    return db


def _usage_targets(G, source: str) -> list[str]:
    return [t for _, t, d in G.out_edges(source, data=True) if d["edge_type"] == "usage"]


def test_popular_vocabulary_is_owned_through_its_iri_from_any_registry(tmp_path):
    # FOAF's latest row can come from any registry after dedup; its IRI names
    # its namespace, so usage resolves to it rather than to an external twin.
    _corpus(
        tmp_path,
        [
            {"ontology_iri": "http://xmlns.com/foaf/0.1/", "source_registry": "BioPortal"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "referenced_namespaces": [FOAF],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/user") == ["http://xmlns.com/foaf/0.1/"]


def test_language_namespace_stays_external_even_with_a_corpus_row(tmp_path):
    owl = "http://www.w3.org/2002/07/owl#"
    _corpus(
        tmp_path,
        [
            {"ontology_iri": "http://www.w3.org/2002/07/owl", "source_registry": "LOV"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "referenced_namespaces": [owl],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/user") == [f"external:{_external_key(owl)}"]
    edge = next(iter(G.out_edges("https://example.org/user", data=True)))[2]
    assert edge["reference_kind"] == "language"


def test_named_claimant_declaring_more_owns_the_namespace():
    # Two rows whose IRIs both name FOAF: the one declaring FOAF's terms wins
    # over one that merely carries the IRI.
    def record(owner, declared):
        return NamespaceOwnerRecord(
            owner=owner,
            ontology_iri="http://xmlns.com/foaf/0.1/",
            source_registry="X",
            own_namespaces=(FOAF,),
            declarations={FOAF: declared},
            skos_concepts={},
        )

    owners = resolve_namespace_owners([record(1, 2), record(2, 80)])
    assert owners[FOAF] == 2


def test_obo_import_module_resolves_to_its_source_ontology(tmp_path):
    _corpus(
        tmp_path,
        [
            {"ontology_iri": "http://purl.obolibrary.org/obo/chebi.owl", "source_registry": "OBO"},
            {"ontology_iri": "http://purl.obolibrary.org/obo/iao.owl", "source_registry": "OBO"},
            {
                "ontology_iri": "http://purl.obolibrary.org/obo/foodon.owl",
                "source_registry": "OBO",
                "imports": [
                    "http://purl.obolibrary.org/obo/foodon/imports/chebi_import.owl",
                    "http://purl.obolibrary.org/obo/iao/2017-03-24/iao.owl",
                    "http://purl.obolibrary.org/obo/foodon/imports/foodon_import.owl",
                ],
            },
        ],
    )
    G = build_network(tmp_path)
    edges = {
        (t, d["resolution"])
        for _, t, d in G.out_edges("http://purl.obolibrary.org/obo/foodon.owl", data=True)
    }
    assert edges == {
        ("http://purl.obolibrary.org/obo/chebi.owl", "obo_module"),
        ("http://purl.obolibrary.org/obo/iao.owl", "obo_module"),
    }


def test_external_spellings_differing_by_separator_are_one_node(tmp_path):
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": "https://example.org/a",
                "source_registry": "OBO",
                "referenced_namespaces": ["https://ext.example.org/voc/"],
            },
            {
                "ontology_iri": "https://example.org/b",
                "source_registry": "OBO",
                "referenced_namespaces": ["https://ext.example.org/voc#"],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/a") == _usage_targets(G, "https://example.org/b")


def test_usage_edges_carry_term_evidence_when_measured(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    _corpus(
        tmp_path,
        [
            {"ontology_iri": "http://xmlns.com/foaf/0.1/", "source_registry": "LOV"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "referenced_namespaces": [FOAF, "http://purl.org/dc/terms/"],
            },
        ],
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "file_hash": "file-2",
                    "namespace": FOAF,
                    "n_logical_terms": 3,
                    "n_annotation_terms": 1,
                    "n_declared_terms": 0,
                },
                {
                    "file_hash": "file-2",
                    "namespace": "http://purl.org/dc/terms/",
                    "n_logical_terms": 0,
                    "n_annotation_terms": 4,
                    "n_declared_terms": 0,
                },
            ]
        ),
        tmp_path / "namespace_usage.parquet",
    )
    G = build_network(tmp_path)
    by_target = {t: d for _, t, d in G.out_edges("https://example.org/user", data=True)}
    assert by_target[FOAF]["n_logical_terms"] == 3
    dc = by_target["external:http://purl.org/dc/terms"]
    assert (dc["n_logical_terms"], dc["n_annotation_terms"]) == (0, 4)

    save_network(G, tmp_path)
    loaded = load_network(tmp_path)
    assert loaded["https://example.org/user"][FOAF]["usage"]["n_logical_terms"] == 3


def test_owner_using_its_own_generic_namespace_does_not_self_loop(tmp_path):
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": "http://xmlns.com/foaf/0.1/",
                "source_registry": "LOV",
                "referenced_namespaces": [FOAF],
            }
        ],
    )
    G = build_network(tmp_path)
    assert G.number_of_edges() == 0


def test_hash_suffixed_usage_namespace_finds_its_ontology(tmp_path):
    _corpus(
        tmp_path,
        [
            {"ontology_iri": "https://example.org/cube", "source_registry": "OBO"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "referenced_namespaces": ["https://example.org/cube#"],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/user") == ["https://example.org/cube"]
    assert not any(d.get("is_external") for _, d in G.nodes(data=True))


def test_copied_namespace_resolves_to_its_owner_not_the_first_claimant(tmp_path):
    bfo_ns = "http://purl.obolibrary.org/obo/BFO_"
    _corpus(
        tmp_path,
        [
            # Lower row id: a domain ontology that copied BFO terms into itself.
            {
                "ontology_iri": "https://example.org/energy",
                "source_registry": "BioPortal",
                "own_namespaces": ["https://example.org/energy/", bfo_ns],
            },
            {
                "ontology_iri": "http://purl.obolibrary.org/obo/bfo.owl",
                "source_registry": "OBO",
                "own_namespaces": [bfo_ns],
            },
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "BioPortal",
                "referenced_namespaces": [bfo_ns],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/user") == [
        "http://purl.obolibrary.org/obo/bfo.owl"
    ]


def test_undeclared_iri_is_not_invented_from_the_file_name():
    from ontometer_corpus.pipeline.parse import _parse_owl_xml, _settle_ontology_iri

    owx = b"""<?xml version="1.0"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#">
  <Declaration><Class IRI="http://example.org/o#Dog"/></Declaration>
</Ontology>"""
    _, _, error, meta = _parse_owl_xml(owx, "3e8ac4d5707a9e8e")
    assert error is None
    assert meta is not None
    assert meta["ontology_iri"] is None

    _settle_ontology_iri(meta, "bp:DOGS::3")
    assert meta["ontology_iri"] is None and meta["iri_source"] is None

    meta: dict = {"ontology_iri": None}
    _settle_ontology_iri(meta, "lov:http://purl.org/dogs#::v1.0")
    assert meta["iri_source"] == "registry"
    assert meta["ontology_iri"] is not None

    meta = {"ontology_iri": "http://example.org/o"}
    _settle_ontology_iri(meta, "bp:DOGS::3")
    assert meta["iri_source"] == "declared"


def test_copied_namespace_still_counts_as_usage_of_its_owner(tmp_path):
    iao_ns = "http://purl.obolibrary.org/obo/IAO_"
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": "http://purl.obolibrary.org/obo/iao.owl",
                "source_registry": "OBO",
                "own_namespaces": [iao_ns],
            },
            # Declares copied IAO terms (MIREOT) and references them.
            {
                "ontology_iri": "http://purl.obolibrary.org/obo/uberon.owl",
                "source_registry": "OBO",
                "own_namespaces": ["http://purl.obolibrary.org/obo/UBERON_", iao_ns],
                "referenced_namespaces": ["http://purl.obolibrary.org/obo/UBERON_", iao_ns],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "http://purl.obolibrary.org/obo/uberon.owl") == [
        "http://purl.obolibrary.org/obo/iao.owl"
    ]


def test_import_without_an_exact_iri_match_stays_external(tmp_path):
    """A final filename is not enough evidence to name an import target."""
    imported = "https://imports.example.org/modules/core.owl"
    _corpus(
        tmp_path,
        [
            {"ontology_iri": "https://alpha.example.org/vocab/core.owl", "source_registry": "OBO"},
            {"ontology_iri": "https://beta.example.org/model/core.owl", "source_registry": "OBO"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "imports": [imported],
            },
        ],
    )

    graph = build_network(tmp_path)
    edges = list(graph.out_edges("https://example.org/user", data=True))
    assert [(source, target) for source, target, _ in edges] == [
        ("https://example.org/user", f"external:{imported}")
    ]
    assert edges[0][2]["resolution"] == "external"
    assert edges[0][2]["reference_iris"] == [imported]
    assert edges[0][2]["evidence_count"] == 1


def test_import_with_unique_stem_stays_external(tmp_path):
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": "https://alpha.example.org/vocab/unique.owl",
                "source_registry": "OBO",
            },
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "imports": ["https://imports.example.org/modules/unique.owl"],
            },
        ],
    )

    graph = build_network(tmp_path)
    edges = list(graph.out_edges("https://example.org/user", data=True))
    assert [(source, target) for source, target, _ in edges] == [
        (
            "https://example.org/user",
            "external:https://imports.example.org/modules/unique.owl",
        )
    ]
    assert edges[0][2]["reference_iris"] == ["https://imports.example.org/modules/unique.owl"]

    save_network(graph, tmp_path)
    loaded = load_network(tmp_path)
    assert list(loaded.out_edges("https://example.org/user", data=True)) == edges


def test_multiple_import_spellings_are_one_relation_with_all_evidence(tmp_path):
    owner = "https://example.org/vocab/onto"
    aliases = [
        "https://example.org/vocab/onto/imports/part_import.owl",
        "https://example.org/vocab/onto/imports/another_import.owl",
    ]
    _corpus(
        tmp_path,
        [
            {"ontology_iri": owner, "source_registry": "OBO"},
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "OBO",
                "imports": aliases,
            },
        ],
    )
    graph = build_network(tmp_path)
    edges = list(graph.out_edges("https://example.org/user", data=True))
    assert len(edges) == 1
    assert edges[0][1] == owner
    assert edges[0][2]["reference_iris"] == sorted(aliases)
    assert edges[0][2]["evidence_count"] == 2


def test_import_module_resolving_to_its_source_is_dropped(tmp_path):
    source = "https://example.org/vocab/onto"
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": source,
                "source_registry": "OBO",
                "imports": ["https://example.org/vocab/onto/imports/part_import.owl"],
            }
        ],
    )
    assert list(build_network(tmp_path).edges(data=True)) == []


def test_reference_kind_distinguishes_domain_language_metadata_and_malformed():
    assert reference_kind("https://example.org/vocabulary/") == "domain_vocabulary"
    assert reference_kind("http://www.w3.org/2002/07/owl#") == "language"
    assert reference_kind("urn:swrl#") == "language"
    # Popular vocabularies are ontologies like any other; how they are used is
    # measured per use, not decided by namespace.
    assert reference_kind("http://xmlns.com/foaf/0.1/") == "domain_vocabulary"
    assert reference_kind("http://purl.org/dc/terms/") == "domain_vocabulary"
    assert reference_kind("http://purl.obolibrary.org/obo/") == "malformed_or_local"
    assert reference_kind("https://orcid.org/") == "metadata_or_identifier"
    assert reference_kind("http://identifiers.org") == "metadata_or_identifier"
    assert reference_kind("https://upload.wikimedia.org/wikipedia/commons/") == (
        "metadata_or_identifier"
    )
    assert reference_kind("http://") == "malformed_or_local"


def test_file_path_is_not_an_ontology_iri():
    import rdflib

    from ontometer_corpus.pipeline.parse import extract_metadata

    g = rdflib.Graph().parse(
        data="<file:///tmp/scratch/tmpab12.owl> a <http://www.w3.org/2002/07/owl#Ontology> .",
        format="turtle",
    )
    assert extract_metadata(g)["ontology_iri"] is None


def test_shared_iri_rows_become_separate_row_nodes(tmp_path):
    crop = "https://cropontology.org/rdf/"
    _corpus(
        tmp_path,
        [
            {"ontology_iri": crop, "source_registry": "AgroPortal", "shared_iri": 1},
            {"ontology_iri": crop, "source_registry": "AgroPortal", "shared_iri": 1},
        ],
    )
    G = build_network(tmp_path)
    assert sorted(n for n, d in G.nodes(data=True) if not d.get("is_external")) == [
        "row:1",
        "row:2",
    ]


def test_contested_namespace_without_owner_stays_external(tmp_path):
    orcid = "https://orcid.org/"
    _corpus(
        tmp_path,
        [
            {
                "ontology_iri": "https://example.org/a",
                "source_registry": "OBO",
                "own_namespaces": [orcid],
            },
            {
                "ontology_iri": "https://example.org/b",
                "source_registry": "OBO",
                "own_namespaces": [orcid],
            },
            {
                "ontology_iri": "https://example.org/user",
                "source_registry": "LOV",
                "referenced_namespaces": [orcid],
            },
        ],
    )
    G = build_network(tmp_path)
    assert _usage_targets(G, "https://example.org/user") == [f"external:{_external_key(orcid)}"]


def test_namespace_counts_read_roles_only_for_rows_that_may_have_concepts(tmp_path):
    import pandas as pd

    from ontometer_corpus.network.construction import _namespace_counts
    from ontometer_corpus.paths import CorpusPaths
    from ontometer_corpus.role_names import SKOS_CONCEPT_ROLE

    paths = CorpusPaths(corpus_dir=tmp_path / "data", store_dir=tmp_path / "store")
    h = "ab" + "0" * 62
    side = paths.sidecar_path(h)
    side.parent.mkdir(parents=True)
    pd.DataFrame(
        {"record_type": ["entity", "entity"], "subject": ["http://x.org/a#A", "http://x.org/a#B"]}
    ).to_parquet(side)
    roles = paths.roles_path(h)
    roles.parent.mkdir(parents=True)
    pd.DataFrame(
        {"record_type": ["role"], "subject": ["http://x.org/c/1"], "object": [SKOS_CONCEPT_ROLE]}
    ).to_parquet(roles)

    declared, concepts = _namespace_counts(paths, h, skos_concept_count=0)
    assert declared == {"http://x.org/a#": 2}
    assert concepts == {}
    _, concepts = _namespace_counts(paths, h, skos_concept_count=None)
    assert concepts == {"http://x.org/c/": 1}
    assert _namespace_counts(paths, None, skos_concept_count=None) == ({}, {})
