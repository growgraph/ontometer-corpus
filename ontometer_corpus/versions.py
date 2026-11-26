"""Version families: which corpus rows are releases of the same ontology.

Deduplication by ontology IRI cannot link releases that never declare an
``owl:Ontology`` IRI, and content hashing separates every release that changed
at all. OntoPortal-family registries (BioPortal, AgroPortal, …) number their
submissions, and LOV labels vocabulary versions, in the fetch identifier
itself — ``bp:PTO::355``, ``lov:http://schema.org/::v0.95`` — so the identifier
before the last ``::`` names the family and what follows orders it.

Registries without versioned identifiers (Ontohub, OKG, OBO) give each row its
own family.
"""

from __future__ import annotations

import json
import re

_SEP = "::"
_NUM = re.compile(r"^\d+$")


def family_key(source_id: str) -> str:
    """The family a fetch identifier belongs to."""
    head, sep, _ = source_id.rpartition(_SEP)
    return head if sep else source_id


def release_label(source_id: str) -> str | None:
    """The part after the last ``::``, or None when the identifier has none."""
    head, sep, tail = source_id.rpartition(_SEP)
    return tail if sep else None


def release_date(metadata: str | None) -> str | None:
    """ISO date (``YYYY-MM-DD``) the registry records for this release, if any.

    OntoPortal registries record ``released`` per submission; LOV records
    ``issued`` per version. ``creationDate`` is the fallback.
    """
    if not metadata:
        return None
    try:
        meta = json.loads(metadata)
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    for key in ("released", "issued", "creationDate"):
        val = meta.get(key)
        if isinstance(val, str) and len(val) >= 10 and val[:4].isdigit() and val[4] == "-":
            return val[:10]
    return None


def release_order(source_id: str, date: str | None) -> tuple[str, int, str]:
    """Sort key placing releases of one family in publication order.

    The registry date leads; a numeric submission number breaks ties and
    orders undated releases; a textual label is the last resort.
    """
    label = release_label(source_id) or ""
    number = int(label) if _NUM.match(label) else -1
    return (date or "", number, label)
