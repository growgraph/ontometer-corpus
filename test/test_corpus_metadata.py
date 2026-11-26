"""The registry-metadata projection — what it keeps, and what it must never keep."""

from __future__ import annotations

import json

from ontometer_corpus.metadata import (
    DROPPED_FIELDS,
    REDACTED,
    project_registry_metadata,
    scrub_text,
)

RAW = {
    "description": "An ontology. Questions to maintainer@example.org please.",
    "homepage": "https://example.org",
    "hasOntologyLanguage": "OWL",
    "version": "1.2.3",
    "status": "production",
    "contact": [{"name": "A Maintainer", "email": "maintainer@example.org"}],
    "administeredBy": ["https://example.org/users/someone"],
    "@context": {"a": "https://example.org/ctx/a"},
    "ontology": {
        "acronym": "EX",
        "name": "Example Ontology",
        "hasDomain": ["https://example.org/domain/biology"],
        "links": {"submissions": "https://example.org/x", "classes": "https://example.org/y"},
        "@context": {"b": "https://example.org/ctx/b"},
    },
}


def test_research_fields_survive():
    out = project_registry_metadata(RAW)
    assert out["homepage"] == "https://example.org"
    assert out["hasOntologyLanguage"] == "OWL"
    assert out["version"] == "1.2.3"
    assert out["ontology"]["acronym"] == "EX"
    assert out["ontology"]["hasDomain"] == ["https://example.org/domain/biology"]


def test_contact_records_are_dropped():
    out = project_registry_metadata(RAW)
    assert "contact" not in out
    assert "administeredBy" not in out


def test_jsonld_boilerplate_is_dropped():
    out = project_registry_metadata(RAW)
    assert "@context" not in out
    assert "links" not in out["ontology"]
    assert "@context" not in out["ontology"]


def test_no_address_survives_anywhere():
    serialized = json.dumps(project_registry_metadata(RAW))
    assert "@example.org" not in serialized
    assert REDACTED in serialized


def test_description_keeps_its_words():
    out = project_registry_metadata(RAW)
    assert out["description"].startswith("An ontology.")
    assert "maintainer@example.org" not in out["description"]


def test_scrub_leaves_text_without_addresses_alone():
    assert scrub_text("nothing to see") == "nothing to see"


def test_unknown_scalar_fields_are_kept_for_non_ontoportal_registries():
    # LOV, OBO and OKG do not use the OntoPortal shape; emptying them would
    # silently lose the only metadata those registries provide.
    out = project_registry_metadata({"title": "A Vocabulary", "nsp": "https://example.org/ns#"})
    assert out["title"] == "A Vocabulary"
    assert out["nsp"] == "https://example.org/ns#"


def test_oversized_unknown_fields_are_dropped():
    out = project_registry_metadata({"blob": "x" * 5000})
    assert "blob" not in out


def test_non_mapping_input_degrades_to_empty():
    assert project_registry_metadata(None) == {}
    assert project_registry_metadata("not a dict") == {}
    assert project_registry_metadata([1, 2]) == {}


def test_projection_is_idempotent():
    once = project_registry_metadata(RAW)
    assert project_registry_metadata(once) == once


def test_every_contact_channel_is_in_the_drop_list():
    for field in ("contact", "contacts", "email", "mailing_list", "mailingList"):
        assert field in DROPPED_FIELDS
