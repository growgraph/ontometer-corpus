"""Projection of raw registry metadata down to the fields the corpus work uses.

Registries return a large JSON document per submission, and it would be stored
twice: in `corpus.db` and in `manifest.jsonl`. Verbatim registry responses are
mostly not research data:

* ``links`` and ``@context`` are JSON-LD boilerplate, templated URLs repeated
  once per submission;
* ``contact``, ``administeredBy``, ``reviews`` and ``projects`` carry the
  personal details of ontology maintainers — names and email addresses
  harvested from registry APIs — which a public corpus should not
  redistribute.

The allowlist keeps the descriptive fields and drops the contact data. What is
left is what the corpus analysis wants: what the ontology is called, what
domain it claims, what language it is written in, how it is licensed, and where
it came from. That is the input for the domain classification and coverage
work.

The projection runs at fetch time, so a new crawl never materialises the
dropped fields, and the same function backfills older artifacts.
"""

from __future__ import annotations

import re
from typing import Any

#: Top-level submission fields worth keeping.
SUBMISSION_FIELDS: frozenset[str] = frozenset(
    {
        "creationDate",
        "deprecated",
        "description",
        "documentation",
        "hasLicense",
        "hasOntologyLanguage",
        "homepage",
        "license",
        "publication",
        "released",
        "status",
        "submissionId",
        "version",
    }
)

#: Fields worth keeping from the nested ``ontology`` object.
ONTOLOGY_FIELDS: frozenset[str] = frozenset(
    {
        "acronym",
        "group",
        "hasDomain",
        "name",
        "notes",
        "ontologyType",
        "viewingRestriction",
    }
)

#: Dropped wherever they appear. ``links``/``@context`` are boilerplate; the
#: rest are contact channels and personal data.
DROPPED_FIELDS: frozenset[str] = frozenset(
    {
        "@context",
        "administeredBy",
        "contact",
        "contacts",
        "email",
        "links",
        "mailing_list",
        "mailingList",
        "projects",
        "reviews",
        "tracker",
    }
)

#: Replaces any address found in retained free text.
REDACTED = "[email redacted]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scrub_text(value: str) -> str:
    """Redact email addresses from retained free text.

    ``description`` is kept because it is the input the domain-classification
    work reads, and some of them mention a maintainer's address in passing.
    Redacting the address costs that work nothing — it reads the words,
    not the contact details — and keeping it would let personal data into the
    corpus through the side door that dropping the contact fields closes.
    """
    return _EMAIL.sub(REDACTED, value)


#: Replaces the value of a credential query parameter.
SECRET_REDACTED = "REDACTED"

# Query parameters that carry credentials: OntoPortal passes its API key as
# ``?apikey=``, and a failed request echoes the full URL into its error.
_SECRET_PARAM = re.compile(r"(?i)([?&](?:apikey|api_key|access_token|token)=)[^&\s'\"<>]+")


def redact_secrets(value: str | None) -> str | None:
    """Replace credential query-parameter values in a URL or message.

    Args:
        value: A URL, or free text such as an HTTP error that quotes one.

    Returns:
        The text with every ``apikey``, ``api_key``, ``access_token`` or
        ``token`` value replaced by ``REDACTED``; None stays None.
    """
    if not value:
        return value
    return _SECRET_PARAM.sub(lambda m: m.group(1) + SECRET_REDACTED, value)


def project_registry_metadata(raw: Any) -> dict[str, Any]:
    """Reduce one registry metadata document to the fields worth keeping.

    Returns an empty dict for anything that is not a mapping, so a registry that
    changes its response shape degrades to "no metadata" rather than raising in
    the middle of a crawl.
    """
    if not isinstance(raw, dict):
        return {}

    def keep(value: Any) -> Any:
        return scrub_text(value) if isinstance(value, str) else value

    out: dict[str, Any] = {k: keep(raw[k]) for k in SUBMISSION_FIELDS if k in raw}

    ontology = raw.get("ontology")
    if isinstance(ontology, dict):
        kept = {k: keep(ontology[k]) for k in ONTOLOGY_FIELDS if k in ontology}
        if kept:
            out["ontology"] = kept

    # Registries that are not OntoPortal-shaped put useful fields at the top
    # level under different names; keep anything that is a short scalar and not
    # explicitly dropped, so a non-OntoPortal source is not silently emptied.
    for key, value in raw.items():
        if key in out or key in DROPPED_FIELDS or key == "ontology":
            continue
        if isinstance(value, str) and len(value) <= 2000:
            out[key] = scrub_text(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value

    return out
