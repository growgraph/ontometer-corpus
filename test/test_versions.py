"""Release families recovered from registry fetch identifiers."""

from __future__ import annotations

import json

from ontometer_corpus.versions import family_key, release_date, release_label, release_order


def test_ontoportal_and_lov_identifiers_share_a_family_across_releases():
    assert family_key("bp:PTO::355") == family_key("bp:PTO::12") == "bp:PTO"
    assert family_key("lov:http://schema.org/::v0.95") == "lov:http://schema.org/"


def test_unversioned_identifier_is_its_own_family():
    assert (
        family_key("ontohub:boc2019/BookOntologyNew.owl") == "ontohub:boc2019/BookOntologyNew.owl"
    )
    assert release_label("ontohub:boc2019/BookOntologyNew.owl") is None


def test_release_date_prefers_released_then_issued():
    assert (
        release_date(
            json.dumps({"released": "2025-11-07T18:17:46.000-08:00", "creationDate": "2020-01-01"})
        )
        == "2025-11-07"
    )
    assert release_date(json.dumps({"issued": "2014-05-02"})) == "2014-05-02"
    assert release_date(json.dumps({"released": "May 2025"})) is None
    assert release_date(None) is None


def test_release_order_uses_date_then_submission_number():
    a = release_order("bp:X::9", "2020-01-01")
    b = release_order("bp:X::10", "2020-01-01")
    c = release_order("bp:X::2", "2021-06-01")
    assert sorted([c, b, a]) == [a, b, c]
    # Undated releases still order numerically, not lexically.
    assert release_order("bp:X::9", None) < release_order("bp:X::10", None)
