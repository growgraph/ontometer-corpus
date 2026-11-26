"""
Deduplication audit: how far the choice of one copy per ontology agrees with
evidence the pipeline does not use.

Part of the resource paper's analysis. Deduplication decides that two
registry copies are one ontology from their IRIs and declared terms
(``ontometer_corpus.pipeline.dedup``, Pass D). The registries also name their
copies, by acronym, prefix or title, and deduplication never reads those
names, so they serve as an independent check:

* **Merges.** Passes C and D are replayed on a copy of ``corpus.db`` to
  recover which copy each folded copy was folded into. A merge whose two
  copies carry the same registry acronym, or titles sharing at least half
  their words, is confirmed by the names; every other merge needs a verdict
  from the review file.
* **Candidate misses.** Pairs of ontologies that deduplication keeps apart
  but that share a registry acronym, a title, or an ontology IRI (a split
  IRI) are candidates for a missed merge, each needing a verdict.

Both lists carry what a reviewer needs: the IRIs, sizes, the share of the
smaller copy's terms found in the larger (containment), their Jaccard
overlap, and whether any releases of the two have identical term sets.

The review file (``dedup_review.csv``: ``kind,a,b,verdict,note``) holds the
verdicts: ``kind`` is ``merge`` or ``candidate``, ``a``/``b`` the two
``source_id``s, ``verdict`` one of ``same`` (one ontology), ``part`` (a
module, extract or variant of the other) or ``distinct``. A name-confirmed
merge needs no row; a row overrides it. Pairs without a verdict are
reported as unreviewed, never guessed.

A second, independent review checks the first. ``--blind-sheet FILE`` writes
every pair the review file judges, in a seeded random order, with the evidence
but without the verdicts; a second reviewer fills in ``verdict`` and passes the
sheet back with ``--second FILE``. Agreement and Cohen's kappa are computed over
the pairs both reviews judge, against ``--first FILE`` when given: the first
review as it stood before disagreements were resolved into the review file.
Precisions carry Wilson 95% intervals.

Reads:  ``<corpus>/corpus.db``, the structural sidecars in the object store,
        the review file (default ``<corpus>/dedup_review.csv``), and with
        ``--second`` a second reviewer's sheet.
Writes: ``<out>/dedup_merges.csv``, ``<out>/dedup_candidates.csv`` and
        ``<out>/dedup_audit.json``; with ``--stats FILE``, ``\\statdef`` lines
        in FILE; with ``--blind-sheet FILE``, the sheet for a second reviewer;
        with ``--second``, ``<out>/dedup_disagreements.csv``. ``missed`` counts ontologies counted more than once: candidate
        pairs judged ``same``, grouped into components, each less one.

Usage:
    ontometer-corpus paper dedup-audit --corpus DIR --store DIR --out DIR [--stats FILE]
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import click
import pandas as pd

from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.pipeline.db import get_connection
from ontometer_corpus.pipeline.dedup import (
    _newest_duplicate_dates,
    _sidecar_entities,
    _term_key,
    drop_stub_latest,
    mark_latest_versions,
    ontology_key,
    unify_latest_by_iri,
)
from ontometer_corpus.versions import family_key

VERDICTS = ("same", "part", "distinct")
#: Seed of the blind sheet's row order, so that a regenerated sheet matches a filled one.
BLIND_SEED = 0
#: The evidence a reviewer sees on the blind sheet.
BLIND_COLUMNS = (
    "kind",
    "a",
    "b",
    "a_names",
    "b_names",
    "a_iri",
    "b_iri",
    "a_terms",
    "b_terms",
    "containment",
    "jaccard",
    "identical_release",
    "same_registry",
)
#: A name shared by more ontologies than this is generic ("core", "ontology"), not identity.
MAX_GROUP = 6
#: Share of the shorter title's words the other must contain for titles to agree.
TITLE_AGREE = 0.5
_STOP = {"the", "of", "ontology", "and", "for", "a", "an", "vocabulary", "in", "on", "to"}
_HOMEPAGE_ACRONYM = (
    re.compile(r"/ontologies/([A-Za-z0-9_\-]+)"),
    re.compile(r"purl\.obolibrary\.org/obo/([A-Za-z0-9_]+)"),
)


# ---------------------------------------------------------------- names
def _metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        md = json.loads(raw)
    except ValueError:
        return {}
    return md if isinstance(md, dict) else {}


def _text(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = next(iter(value.values()), "")
    return str(value or "").strip()


def registry_names(source_id: str, raw_metadata: object) -> tuple[str, str]:
    """(acronym, title) a registry gives a copy; empty where it gives none.

    OntoPortal: the ontology's acronym and name. OBO: its id and title. LOV:
    its prefix (LOV's ``name`` is a version label). OKG: its title, and the
    acronym in a BioPortal or OBO homepage. Ontohub: the file name.
    """
    md = _metadata(raw_metadata)
    nested = md.get("ontology")
    ont: dict = nested if isinstance(nested, dict) else {}
    acronym = ont.get("acronym") or md.get("_acronym") or md.get("id") or md.get("_prefix")
    title = ont.get("name") or md.get("title") or ""
    if not acronym and isinstance(md.get("homepage"), str):
        for pattern in _HOMEPAGE_ACRONYM:
            if m := pattern.search(md["homepage"]):
                acronym = m.group(1)
                break
    if not acronym and source_id.startswith("ontohub:"):
        acronym = Path(md.get("file") or source_id.rsplit("/", 1)[-1]).stem
    return _text(acronym), _text(title)


def acronym_key(acronym: str) -> str:
    return re.sub(r"[^a-z0-9]", "", acronym.lower())


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(t for t in re.findall(r"[a-z0-9]+", title.lower()) if t not in _STOP)


def names_verdict(a: pd.Series, b: pd.Series) -> str:
    """``agree``, ``disagree`` or ``unknown`` on the registry names alone."""
    if a["acronym_key"] and a["acronym_key"] == b["acronym_key"]:
        return "agree"
    ta, tb = a["title_tokens"], b["title_tokens"]
    if ta and tb:
        return "agree" if len(ta & tb) / min(len(ta), len(tb)) >= TITLE_AGREE else "disagree"
    if a["acronym_key"] and b["acronym_key"]:
        return "disagree"
    return "unknown"


# ---------------------------------------------------------------- replay
def replay(paths: CorpusPaths) -> tuple[pd.DataFrame, dict[int, int], set[int]]:
    """Replay Passes C-E on a copy of corpus.db.

    Returns the parsed canonical rows, each folded row id mapped to the row
    kept for it, and the row ids that stay latest.
    """
    entities = _sidecar_entities(paths.sidecar_path)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "corpus.db"
        shutil.copy(paths.db_path, db)
        conn = get_connection(db)
        mark_latest_versions(conn)
        folded: dict[int, int] = {}
        unify_latest_by_iri(conn, entities, _newest_duplicate_dates(conn), folded)
        drop_stub_latest(conn)
        rows = pd.read_sql(
            "SELECT id, source_id, source_registry, ontology_iri, file_hash, content_hash, "
            "class_count, property_count, skos_concept_count, is_latest_version, "
            "source_registry_metadata FROM corpus_registry "
            "WHERE parse_status = 'ok' AND is_duplicate = 0",
            conn,
        )
        conn.close()
    latest = set(rows.loc[rows["is_latest_version"] == 1, "id"])
    return rows, folded, latest


# ---------------------------------------------------------------- pairs
class Evidence:
    """Term overlap and release history of a pair of rows."""

    def __init__(self, rows: pd.DataFrame, paths: CorpusPaths) -> None:
        self.rows = rows.set_index("id", drop=False)
        self._entities = _sidecar_entities(paths.sidecar_path)
        self._terms: dict[int, set[str] | None] = {}
        self.family_hashes: dict[str, set[str]] = defaultdict(set)
        for sid, h, n in zip(
            rows["source_id"],
            rows["content_hash"],
            rows["class_count"].fillna(0) + rows["property_count"].fillna(0),
            strict=True,
        ):
            if h and n:
                self.family_hashes[family_key(sid)].add(h)

    def terms(self, row_id: int) -> set[str] | None:
        if row_id not in self._terms:
            found = self._entities(self.rows.at[row_id, "file_hash"])
            self._terms[row_id] = None if found is None else {_term_key(t) for t in found}
        return self._terms[row_id]

    def pair(self, a_id: int, b_id: int) -> dict:
        a, b = self.rows.loc[a_id], self.rows.loc[b_id]
        sa, sb = self.terms(a_id), self.terms(b_id)
        out = {
            "a": a["source_id"],
            "b": b["source_id"],
            "a_iri": a["ontology_iri"],
            "b_iri": b["ontology_iri"],
            "a_names": f"{a['acronym']} | {a['title']}",
            "b_names": f"{b['acronym']} | {b['title']}",
            "a_terms": None if sa is None else len(sa),
            "b_terms": None if sb is None else len(sb),
            "same_registry": a["source_registry"] == b["source_registry"],
            "same_iri": bool(
                a["ontology_iri"]
                and b["ontology_iri"]
                and ontology_key(a["ontology_iri"]) == ontology_key(b["ontology_iri"])
            ),
            "identical_release": bool(
                self.family_hashes[family_key(a["source_id"])]
                & self.family_hashes[family_key(b["source_id"])]
            ),
            "containment": None,
            "jaccard": None,
            "names": names_verdict(a, b),
        }
        if sa and sb:
            common = len(sa & sb)
            out["containment"] = round(common / min(len(sa), len(sb)), 3)
            out["jaccard"] = round(common / len(sa | sb), 3)
        return out


def candidate_pairs(latest: pd.DataFrame) -> tuple[set[tuple[int, int]], int]:
    """Pairs of distinct ontologies sharing an acronym, a title or an IRI key;
    and the number of name groups too large to be identity."""
    keys = {
        "acronym": latest["acronym_key"].where(latest["acronym_key"].str.len() >= 2, ""),
        "title": latest["title_tokens"].map(lambda t: " ".join(sorted(t))),
        "iri": latest["ontology_iri"].map(lambda i: ontology_key(i) if isinstance(i, str) else ""),
    }
    pairs: set[tuple[int, int]] = set()
    too_large = 0
    for key in keys.values():
        groups: dict[str, list[int]] = defaultdict(list)
        for row_id, k in zip(latest["id"], key, strict=True):
            if k and len(k) >= 2:
                groups[k].append(row_id)
        for members in groups.values():
            if len(members) > MAX_GROUP:
                too_large += 1
                continue
            pairs.update(combinations(sorted(members), 2))
    return pairs, too_large


# ---------------------------------------------------------------- review
def load_review(path: Path) -> dict[tuple[str, frozenset[str]], tuple[str, str]]:
    if not path.exists():
        return {}
    review = pd.read_csv(path, dtype=str).fillna("")
    # A row left blank is unreviewed, so a partly filled sheet can be scored.
    review = review[review["verdict"] != ""]
    bad = review[~review["verdict"].isin(VERDICTS)]
    if len(bad):
        raise click.ClickException(f"{path}: unknown verdicts {sorted(set(bad['verdict']))}")
    return {
        (kind, frozenset((a, b))): (verdict, note)
        for kind, a, b, verdict, note in zip(
            review["kind"], review["a"], review["b"], review["verdict"], review["note"], strict=True
        )
    }


def attach(df: pd.DataFrame, kind: str, review: dict) -> pd.DataFrame:
    hits = [
        review.get((kind, frozenset((a, b))), ("", ""))
        for a, b in zip(df["a"], df["b"], strict=True)
    ]
    df["verdict"] = [v for v, _ in hits]
    df["note"] = [n for _, n in hits]
    return df


# ---------------------------------------------------------------- report
def _int(v: float) -> str:
    return f"{int(round(v)):,}".replace(",", "{,}")


def _pct(v: float) -> str:
    return f"{v:.1f}\\%"


def excess_nodes(pairs: pd.DataFrame) -> int:
    """Ontologies counted more than once: pairs judged ``same`` grouped into
    connected components, each counting its size less one."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    same = pairs[pairs["verdict"] == "same"]
    for a, b in zip(same["a"], same["b"], strict=True):
        parent[find(a)] = find(b)
    return len(parent) - len({find(x) for x in parent})


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval of a proportion (95% by default)."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return (centre - half, centre + half)


def cohen_kappa(first: list[str], second: list[str]) -> float:
    """Cohen's kappa of two reviews of the same pairs: agreement beyond chance."""
    n = len(first)
    observed = sum(x == y for x, y in zip(first, second, strict=True)) / n
    expected = sum((first.count(v) / n) * (second.count(v) / n) for v in VERDICTS)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def agreement(review: dict, second: dict) -> dict:
    """Agreement of two reviews over the pairs both judge."""
    shared = sorted(set(review) & set(second), key=lambda k: (k[0], sorted(k[1])))
    first = [review[k][0] for k in shared]
    other = [second[k][0] for k in shared]
    if not shared:
        return {"pairs": 0}
    return {
        "pairs": len(shared),
        "agreement": sum(x == y for x, y in zip(first, other, strict=True)) / len(shared),
        "kappa": cohen_kappa(first, other),
        # Disagreements on whether a merge joins distinct ontologies move the precision.
        "distinct_disagreements": sum(
            (x == "distinct") != (y == "distinct") for x, y in zip(first, other, strict=True)
        ),
    }


def disagreements(first: dict, second_path: Path) -> pd.DataFrame:
    """The pairs the two reviews judge differently, with the evidence and both verdicts.

    ``final`` is left empty for the resolved verdict, which belongs in the review file.
    """
    sheet = pd.read_csv(second_path, dtype=str).fillna("")
    keys = [(k, frozenset((a, b))) for k, a, b in zip(sheet["kind"], sheet["a"], sheet["b"])]
    first_verdict = [first.get(k, ("", ""))[0] for k in keys]
    first_note = [first.get(k, ("", ""))[1] for k in keys]
    sheet = sheet.rename(columns={"verdict": "second_verdict", "note": "second_note"}).assign(
        first_verdict=first_verdict, first_note=first_note, final=""
    )
    judged = (sheet["first_verdict"] != "") & (sheet["second_verdict"] != "")
    return sheet[judged & (sheet["first_verdict"] != sheet["second_verdict"])]


def blind_sheet(merges: pd.DataFrame, candidates: pd.DataFrame, review: dict) -> pd.DataFrame:
    """Every pair the review judges, with its evidence and empty verdicts, in a seeded order."""
    pairs = pd.concat([merges.assign(kind="merge"), candidates.assign(kind="candidate")])
    judged = [
        (k, frozenset((a, b))) in review
        for k, a, b in zip(pairs["kind"], pairs["a"], pairs["b"], strict=True)
    ]
    sheet = pairs.loc[judged, list(BLIND_COLUMNS)].sample(frac=1, random_state=BLIND_SEED)
    return sheet.assign(verdict="", note="").reset_index(drop=True)


def summarise(merges: pd.DataFrame, candidates: pd.DataFrame, n_ontologies: int) -> dict:
    by_names = (merges["names"] == "agree") & (merges["verdict"] == "")
    m = {v: int((merges["verdict"] == v).sum()) for v in VERDICTS}
    judged = int(by_names.sum()) + sum(m.values())
    c = {v: int((candidates["verdict"] == v).sum()) for v in VERDICTS}
    missed = excess_nodes(candidates)
    strict = int(by_names.sum()) + m["same"]
    lenient = judged - m["distinct"]
    return {
        "ontologies": n_ontologies,
        "merges": len(merges),
        "merges_confirmed_by_names": int(by_names.sum()),
        "merges_reviewed": m,
        "merges_unjudged": len(merges) - judged,
        # A part folded into its whole is not a different ontology, but not the same one either.
        "merge_precision_strict": (int(by_names.sum()) + m["same"]) / judged if judged else None,
        "merge_precision_lenient": (judged - m["distinct"]) / judged if judged else None,
        "merge_precision_strict_ci": wilson(strict, judged),
        "merge_precision_lenient_ci": wilson(lenient, judged),
        "candidates": len(candidates),
        "candidates_reviewed": c,
        "candidates_unreviewed": len(candidates) - sum(c.values()),
        "missed": missed,
        "missed_share": missed / n_ontologies if n_ontologies else None,
    }


def stats_macros(s: dict) -> dict[str, str]:
    out = {
        "dedupmerges": _int(s["merges"]),
        "dedupmergesnames": _int(s["merges_confirmed_by_names"]),
        "dedupmergesreviewed": _int(sum(s["merges_reviewed"].values())),
        "dedupmergespart": _int(s["merges_reviewed"]["part"]),
        "dedupmergeswrong": _int(s["merges_reviewed"]["distinct"]),
        "dedupmergesunjudged": _int(s["merges_unjudged"]),
        "dedupcandidates": _int(s["candidates"]),
        "dedupcandidatesdistinct": _int(s["candidates_reviewed"]["distinct"]),
        "dedupcandidatespart": _int(s["candidates_reviewed"]["part"]),
        "dedupcandidatessame": _int(s["candidates_reviewed"]["same"]),
        "dedupcandidatesunreviewed": _int(s["candidates_unreviewed"]),
        "dedupmissed": _int(s["missed"]),
    }
    if s["merge_precision_strict"] is not None:
        out["dedupprecisionstrict"] = _pct(100 * s["merge_precision_strict"])
        out["dedupprecisionlenient"] = _pct(100 * s["merge_precision_lenient"])
        for key in ("strict", "lenient"):
            lo, hi = s[f"merge_precision_{key}_ci"]
            out[f"dedupprecision{key}ci"] = f"{100 * lo:.1f}--{100 * hi:.1f}\\%"
    second = s.get("second_review")
    if second and second["pairs"]:
        out["dedupsecondn"] = _int(second["pairs"])
        out["dedupagree"] = _pct(100 * second["agreement"])
        out["dedupkappa"] = f"{second['kappa']:.2f}"
    if s["missed_share"] is not None:
        out["dedupmissedshare"] = _pct(100 * s["missed_share"])
    return out


@click.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for the two pair lists and the summary.",
)
@click.option(
    "--review",
    "review_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Review file (default: dedup_review.csv in the corpus directory).",
)
@click.option(
    "--stats",
    "stats_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the numbers as \\statdef macros to this file.",
)
@click.option(
    "--blind-sheet",
    "blind_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the judged pairs, without verdicts, for a second reviewer.",
)
@click.option(
    "--second",
    "second_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="A second reviewer's filled sheet (kind,a,b,verdict,...): agreement and kappa.",
)
@click.option(
    "--first",
    "first_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="The first review as it stood before disagreements were resolved, which --second "
    "is scored against (default: the review file).",
)
@corpus_paths
def main(
    out: Path,
    review_path: Path | None,
    stats_path: Path | None,
    blind_path: Path | None,
    second_path: Path | None,
    first_path: Path | None,
    paths: CorpusPaths,
) -> None:
    """Compare deduplication's merges and splits with the registries' names."""
    paths.require_store_dir()
    out.mkdir(parents=True, exist_ok=True)
    rows, folded, latest_ids = replay(paths)

    stored = {
        r[0]
        for r in sqlite3.connect(paths.db_path).execute(
            "SELECT id FROM corpus_registry WHERE is_latest_version = 1"
        )
    }
    if stored != latest_ids:
        print(
            f"  [warn] the replay keeps {len(latest_ids ^ stored)} rows differently from "
            "corpus.db: the corpus was built by other deduplication code"
        )

    names = [
        registry_names(s, m)
        for s, m in zip(rows["source_id"], rows["source_registry_metadata"], strict=True)
    ]
    rows["acronym"] = [a for a, _ in names]
    rows["title"] = [t for _, t in names]
    rows["acronym_key"] = rows["acronym"].map(acronym_key)
    rows["title_tokens"] = rows["title"].map(title_tokens)
    evidence = Evidence(rows, paths)
    review = load_review(review_path or paths.corpus_dir / "dedup_review.csv")

    merges = pd.DataFrame([evidence.pair(a, kept) for a, kept in sorted(folded.items())])
    merges = attach(merges, "merge", review)
    latest = rows[rows["id"].isin(latest_ids)]
    pairs, too_large = candidate_pairs(latest)
    candidates = pd.DataFrame([evidence.pair(a, b) for a, b in sorted(pairs)])
    candidates = attach(candidates, "candidate", review)

    merges.to_csv(out / "dedup_merges.csv", index=False)
    candidates.to_csv(out / "dedup_candidates.csv", index=False)
    summary = summarise(merges, candidates, len(latest_ids))
    summary["name_groups_too_large"] = too_large
    if second_path is not None:
        first = load_review(first_path) if first_path is not None else review
        summary["second_review"] = agreement(first, load_review(second_path))
        split = disagreements(first, second_path)
        split.to_csv(out / "dedup_disagreements.csv", index=False)
        print(f"  {len(split)} disagreements: {out / 'dedup_disagreements.csv'}")
    if blind_path is not None:
        blind_sheet(merges, candidates, review).to_csv(blind_path, index=False)
        print(f"  blind sheet: {blind_path}")
    (out / "dedup_audit.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if stats_path is not None:
        lines = ["% Generated by the OntoMeter deduplication audit; do not edit."]
        lines += [f"\\statdef{{{k}}}{{{v}}}" for k, v in sorted(stats_macros(summary).items())]
        stats_path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
