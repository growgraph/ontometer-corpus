"""Licence normalisation and the redistribution verdict for each object."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from ontometer_corpus.paths import CORPUS_ENV, CorpusPaths
from ontometer_corpus.pipeline.db import create_schema, get_connection
from ontometer_corpus.store.index import build_index
from ontometer_corpus.store.licence import (
    Policy,
    email_counts,
    load_policy,
    normalise_licence,
    verdicts,
)

CORPUS_POLICY = (
    Path(os.environ[CORPUS_ENV]) / "redistribution.yaml" if CORPUS_ENV in os.environ else None
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("https://creativecommons.org/licenses/by-sa/3.0/legalcode", "CC-BY-SA-3.0"),
        ("http://creativecommons.org/licenses/by-nc/4.0/", "CC-BY-NC-4.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("CC BY 4.0", "CC-BY-4.0"),
        ("CC-BY-SA 4.0", "CC-BY-SA-4.0"),
        ("Creative Commons Attribution-Share Alike 3.0 United States License", "CC-BY-SA-3.0"),
        ("Creative Commons Attribution 4.0 International", "CC-BY-4.0"),
        ("http://spdx.org/licenses/CC-BY-4.0", "CC-BY-4.0"),
        ("https://spdx.org/licenses/Apache-2.0.html", "Apache-2.0"),
        ("http://www.apache.org/licenses/LICENSE-2.0", "Apache-2.0"),
        ("Apache 2.0 License", "Apache-2.0"),
        ("http://opensource.org/licenses/MIT", "MIT"),
        ("http://opensource.org/licenses/Artistic-2.0", "Artistic-2.0"),
        ("https://www.gnu.org/licenses/gpl-3.0.en.html", "GPL-3.0"),
        ("GPL-3.0", "GPL-3.0"),
        ("Copyright © 2011 Mondeca", "other:Copyright © 2011 Mondeca"),
    ],
)
def test_normalise_cc_iri_and_label_variants(raw, expected):
    assert normalise_licence(raw) == expected


POLICY = Policy(
    default="withhold",
    unrecognised="withhold",
    redistribute=("CC0-*", "CC-BY*", "MIT", "Apache-2.0"),
    withhold=("CC-BY-NC*", "CC-BY-ND*"),
)


def _corpus(tmp_path: Path, rows: list[dict]) -> CorpusPaths:
    """One object per row: ``suffix``, ``licence`` (list or None), ``metadata``, ``status``."""
    paths = CorpusPaths(corpus_dir=tmp_path / "data", store_dir=tmp_path / "store")
    paths.corpus_dir.mkdir()
    conn = get_connection(paths.db_path)
    create_schema(conn)
    for i, row in enumerate(rows):
        content = row.get("content", f"object {i}".encode())
        digest = hashlib.sha256(content).hexdigest()
        rel = f"objects/{digest[:2]}/{digest}{row.get('suffix', '.owl')}"
        target = tmp_path / "store" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        licence = row.get("licence")
        conn.execute(
            "INSERT INTO corpus_registry (source_id, canonical_url, retrieved_file, file_hash, "
            "retrieval_date, source_registry, source_registry_metadata, parse_status, licence, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, '2026-01-01', ?, ?, ?, ?, 'x', 'x')",
            (
                f"r:o{i}::1",
                f"https://example.org/o{i}",
                rel,
                digest,
                row.get("registry", "LOV"),
                json.dumps(row.get("metadata", {})),
                row.get("status", "ok"),
                json.dumps(licence) if licence is not None else None,
            ),
        )
    conn.commit()
    conn.close()
    return paths


def _only(paths: CorpusPaths, policy: Policy = POLICY):
    (verdict,) = verdicts(paths, policy)
    return verdict


def test_file_licence_beats_registry(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            {
                "licence": ["http://creativecommons.org/licenses/by-nc/4.0/"],
                "metadata": {"license": {"label": "CC BY 4.0"}},
                "registry": "OBO",
            }
        ],
    )
    verdict = _only(paths)
    assert (verdict.evidence, verdict.licence_ids, verdict.redistribute) == (
        "file",
        ("CC-BY-NC-4.0",),
        False,
    )


def test_registry_licence_used_when_file_declares_none(tmp_path):
    paths = _corpus(tmp_path, [{"licence": [], "metadata": {"license": {"label": "CC BY 4.0"}}}])
    verdict = _only(paths)
    assert (verdict.evidence, verdict.redistribute) == ("registry", True)


def test_nonpublic_viewing_restriction_withholds(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            {
                "licence": ["http://creativecommons.org/licenses/by/4.0/"],
                "metadata": {"ontology": {"viewingRestriction": "private"}},
            }
        ],
    )
    verdict = _only(paths)
    assert not verdict.redistribute
    assert "viewingRestriction" in verdict.reason


def test_undeclared_defaults_to_withhold(tmp_path):
    verdict = _only(_corpus(tmp_path, [{"licence": []}]))
    assert (verdict.evidence, verdict.redistribute) == ("none", False)


def test_copyright_statement_alone_is_unrecognised(tmp_path):
    verdict = _only(_corpus(tmp_path, [{"licence": ["Copyright 2015 Someone"]}]))
    assert not verdict.redistribute
    assert verdict.reason.startswith("unrecognised")


def test_copyright_beside_open_licence_redistributes(tmp_path):
    paths = _corpus(
        tmp_path,
        [{"licence": ["Copyright 2015 Someone", "https://creativecommons.org/licenses/by/4.0/"]}],
    )
    assert _only(paths).redistribute


def test_non_rdf_extension_withheld(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            {
                "suffix": ".pdf",
                "status": "fail",
                "licence": None,
                "metadata": {"license": {"label": "CC BY 4.0"}},
            }
        ],
    )
    verdict = _only(paths)
    assert not verdict.redistribute
    assert "not an ontology file" in verdict.reason


def test_unknown_suffix_that_parsed_counts_as_rdf(tmp_path):
    paths = _corpus(
        tmp_path, [{"suffix": ".3355", "licence": ["http://creativecommons.org/licenses/by/4.0/"]}]
    )
    assert _only(paths).redistribute


def test_policy_glob_matching(tmp_path):
    assert POLICY.decide(("CC-BY-SA-4.0",)) == (True, "redistribute: CC-BY-SA-4.0")
    assert POLICY.decide(("CC-BY-NC-SA-4.0",))[0] is False
    assert POLICY.decide(("MIT", "CC-BY-ND-4.0"))[0] is False


@pytest.mark.skipif(
    CORPUS_POLICY is None or not CORPUS_POLICY.exists(),
    reason="no corpus directory (set ONTOMETER_CORPUS)",
)
def test_shipped_policy_loads_and_withholds_by_default():
    assert CORPUS_POLICY is not None
    policy = load_policy(CORPUS_POLICY)
    assert policy.default == "withhold"
    assert policy.decide(("CC-BY-4.0",))[0] is True
    assert policy.decide(("CC-BY-NC-4.0",))[0] is False


def test_email_count_scan(tmp_path):
    plain = b"<owl:Ontology><dc:creator>a@example.org, b@example.org, a@example.org</dc:creator>"
    paths = _corpus(
        tmp_path,
        [
            {"content": plain},
            {"content": gzip.compress(b"contact: c@example.org"), "suffix": ".owl.gz"},
        ],
    )
    counts = email_counts(paths, build_index(paths))
    assert sorted(counts.values()) == [1, 2]


def test_database_parsed_before_capture_reads_as_no_file_evidence(tmp_path):
    paths = _corpus(tmp_path, [{"metadata": {"license": {"label": "CC0 1.0"}}}])
    conn = get_connection(paths.db_path)
    conn.execute("ALTER TABLE corpus_registry DROP COLUMN licence")
    conn.commit()
    conn.close()
    verdict = _only(paths)
    assert (verdict.evidence, verdict.redistribute) == ("registry", True)


def test_html_error_page_withheld_whatever_suffix(tmp_path):
    paths = _corpus(
        tmp_path,
        [
            {
                "content": b"  <!DOCTYPE html><html><body>404</body></html>",
                "suffix": ".owl",
                "status": "fail",
                "metadata": {"license": {"label": "CC BY 4.0"}},
            }
        ],
    )
    verdict = _only(paths)
    assert not verdict.redistribute
    assert "not an ontology file" in verdict.reason


def test_negated_public_domain_is_not_public_domain():
    assert normalise_licence("This work is not in the public domain").startswith("other:")
    assert normalise_licence("Released into the public domain") == "public-domain"


def test_unrecognised_licence_text_is_one_line():
    """A multi-line copyright notice must not break the one-row-per-object report."""
    raw = "Copyright 2019, International Council on Archives\n            (ICA)"
    assert normalise_licence(raw) == "other:Copyright 2019, International Council on Archives (ICA)"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
            "OGL-UK-3.0",
        ),
        (
            "https://joinup.ec.europa.eu/category/licence/isa-open-metadata-licence-v11",
            "ISA-OML-1.1",
        ),
        ("https://www.etalab.gouv.fr/licence-ouverte-open-licence", "etalab-2.0"),
        ("http://data.europa.eu/eli/dec_impl/2017/863/oj", "EUPL-1.2"),
        ("http://www.omg.org/techprocess/ab/SpecificationMetadata/MITLicense", "MIT"),
        ("Creative Commons - By Attribution", "CC-BY"),
    ],
)
def test_normalise_open_government_and_spec_licences(raw, expected):
    assert normalise_licence(raw) == expected


@pytest.mark.skipif(
    CORPUS_POLICY is None or not CORPUS_POLICY.exists(),
    reason="no corpus data directory (set ONTOMETER_CORPUS)",
)
def test_shipped_policy_publishes_open_government_licences():
    assert CORPUS_POLICY is not None
    policy = load_policy(CORPUS_POLICY)
    for licence_id in ("OGL-UK-3.0", "ISA-OML-1.1", "etalab-2.0", "EUPL-1.2"):
        assert policy.decide((licence_id,))[0] is True
