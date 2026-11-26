"""
First-evidence dates: when each ontology in the entity network is first attested.

Per release family (``ontometer_corpus.versions``), the earliest date any of
these sources attests to:

* ``d_registry`` — the earliest release date recorded for the family, or for
  any corpus row, in any registry, declaring the same ontology IRI;
* ``d_archive`` — the Internet Archive's first capture of the ontology IRI
  (CDX API; ``ontometer_corpus.dating.archive_candidates``); Ontohub rows
  without an IRI use their repository page;
* ``d_wikidata`` — inception (P571) or publication date (P577) of the
  Wikidata item that Open Knowledge Graphs links to.

Every source is an upper bound on the birth date (see
``ontometer_corpus.dating``); ``birth`` is their minimum and ``birth_source``
names the source that set it. The network sources are queried once and cached
in ``<corpus>/dating_cache.jsonl``, which is tracked: the archive and
Wikidata can change their answers, so the cache is the record that makes the
table reproducible. With it, ``--offline`` rebuilds the same table without
the network.

Reads:  ``<corpus>/corpus.db`` and ``$ONTOMETER_STORE/cache/entity_ontologies.parquet``.
Writes: ``<corpus>/ontology_dates.parquet``, one row per release family,
        and appends new lookups to ``<corpus>/dating_cache.jsonl``.
Runs after: ``ontometer-corpus entity-network``.
Needs ``--store`` (or ``$ONTOMETER_STORE``).

Usage:
    ontometer-corpus dates --store /path/to/ontologies
    ontometer-corpus dates --store /path/to/ontologies --offline

An online run that cannot finish its lookups writes nothing and exits 75;
rerun to resume. ``--offline --allow-uncached`` builds from the cache alone
and marks families with absent lookups ``lookups_complete = False``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
import pandas as pd
import requests

from ontometer_corpus.dating import (
    archive_candidates,
    archive_timestamp_date,
    earliest,
    iri_match_key,
    plausible,
)
from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.versions import family_key, release_date

CDX_URL = "https://web.archive.org/cdx/search/cdx"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "ontometer-corpus (+https://github.com/growgraph/ontometer-corpus)"
_CACHE_FILE = "dating_cache.jsonl"


class _Cache:
    """Append-only JSONL cache of network lookups, keyed by ``source:key``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, str | None] = {}
        self._lock = threading.Lock()
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.data[rec["k"]] = rec["v"]

    def get(self, key: str) -> tuple[bool, str | None]:
        return (key in self.data, self.data.get(key))

    def put(self, key: str, value: str | None) -> None:
        with self._lock:
            self.data[key] = value
            with open(self.path, "a") as f:
                f.write(json.dumps({"k": key, "v": value}) + "\n")


def _require_cached(cache: _Cache, keys: list[str]) -> None:
    """Refuse an offline rebuild when any required lookup is absent.

    A cached null is a completed lookup with no result; an absent key means the
    lookup was never completed and must not be silently interpreted as null.
    """
    missing = sorted({key for key in keys if not cache.get(key)[0]})
    if not missing:
        return
    preview = ", ".join(missing[:3])
    suffix = "" if len(missing) <= 3 else f", ... ({len(missing) - 3} more)"
    raise click.ClickException(
        f"Offline dating cache is incomplete: {len(missing)} required "
        f"lookup(s) are absent: {preview}{suffix}"
    )


#: Consecutive unanswered URLs after which the archive is taken to be blocking.
_MAX_CONSECUTIVE_FAILURES = 3

#: Exit status of an online run whose archive lookups are incomplete
#: (EX_TEMPFAIL): lookups made so far are cached, and a later run resumes.
EXIT_INCOMPLETE = 75


class _Unreachable(Exception):
    """The archive refuses connections — usually a temporary block for request rate."""


def _uncached_families(
    targets: dict[str, list[str]], qids: dict[str, str], cache: _Cache
) -> set[str]:
    """Families with at least one archive or Wikidata lookup absent from the cache."""
    missing = {
        fam for fam, urls in targets.items() if any(not cache.get(f"cdx:{u}")[0] for u in urls)
    }
    missing |= {fam for fam, q in qids.items() if not cache.get(f"wd:{q}")[0]}
    return missing


def _first_capture(session: requests.Session, url: str) -> str | None:
    """Earliest capture timestamp of ``url``, retrying politely when throttled."""
    for attempt in range(4):
        try:
            r = session.get(
                CDX_URL,
                params={"url": url, "limit": "1", "fl": "timestamp", "output": "json"},
                timeout=60,
            )
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (408, 425, 429, 503):
            # Transient: timeout, too early, throttled, unavailable. Honour the
            # archive's Retry-After when it sends one.
            retry_after = r.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else 0
            time.sleep(max(wait, 30 * (attempt + 1)))
            continue
        if 400 <= r.status_code < 500:
            # A client error is the archive's final answer for this URL (a
            # blocked site, a malformed URL): cache it as "no capture".
            return None
        if r.status_code != 200:
            # 5xx gateway errors under load: retry, never cache.
            time.sleep(10 * (attempt + 1))
            continue
        try:
            rows = r.json()
        except ValueError:
            # Not the CDX answer: the archive serves its "Temporarily Offline"
            # page with status 200. Retry, and never cache it as "no capture".
            time.sleep(30 * (attempt + 1))
            continue
        # JSON output is a header row followed by data rows.
        return archive_timestamp_date(rows[1][0]) if len(rows) > 1 else None
    raise _Unreachable(url)


def _archive_dates(
    targets: dict[str, list[str]],
    cache: _Cache,
    workers: int,
    delay: float,
    today: str,
    first: set[str],
) -> tuple[bool, dict[str, str | None]]:
    """Whether every URL is now cached, and family -> earliest capture over its candidate URLs.

    Families in ``first`` are queried first, so an interrupted run still covers
    them. The archive blocks clients that query too fast; ``delay`` spaces each
    worker's requests, and when the archive stops answering the run ends early
    with what it has — lookups are cached, so a later run resumes.
    """
    todo = list(
        dict.fromkeys(
            u
            for fam in sorted(targets, key=lambda f: (f not in first, f))
            for u in targets[fam]
            if not cache.get(f"cdx:{u}")[0]
        )
    )
    print(f"archive: {len(todo)} URLs to query ({len(cache.data)} cached)")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    done = [0]
    failures = [0]  # consecutive URLs the archive did not answer
    blocked = threading.Event()

    def one(url: str) -> None:
        if blocked.is_set():
            return
        try:
            cache.put(f"cdx:{url}", _first_capture(session, url))
        except _Unreachable:
            # One unanswered URL is left uncached for the next run; several
            # in a row mean the archive is blocking us, so stop.
            failures[0] += 1
            if failures[0] >= _MAX_CONSECUTIVE_FAILURES:
                blocked.set()
            return
        failures[0] = 0
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  archive {done[0]}/{len(todo)}", flush=True)
        time.sleep(delay)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    complete = not blocked.is_set() and all(cache.get(f"cdx:{u}")[0] for u in todo)
    if blocked.is_set():
        print(f"archive stopped answering after {done[0]} lookups; rerun later to resume")
    elif not complete:
        print("archive left some URLs unanswered; rerun to complete them")
    return complete, {
        fam: earliest(plausible(cache.get(f"cdx:{u}")[1], today) for u in urls)
        for fam, urls in targets.items()
    }


def _wikidata_dates(qids: dict[str, str], cache: _Cache, today: str) -> dict[str, str | None]:
    """Family -> earliest of inception and publication date of its Wikidata item."""
    todo = sorted({q for q in qids.values() if not cache.get(f"wd:{q}")[0]})
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for i in range(0, len(todo), 200):
        chunk = todo[i : i + 200]
        query = (
            "SELECT ?i (MIN(?d) AS ?date) WHERE { VALUES ?i { "
            + " ".join(f"wd:{q}" for q in chunk)
            + " } OPTIONAL { ?i wdt:P571|wdt:P577 ?d } } GROUP BY ?i"
        )
        r = session.get(
            WIKIDATA_SPARQL,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=120,
        )
        r.raise_for_status()
        found = {}
        for b in r.json()["results"]["bindings"]:
            q = b["i"]["value"].rsplit("/", 1)[-1]
            found[q] = b.get("date", {}).get("value")
        for q in chunk:
            cache.put(f"wd:{q}", found.get(q))
    print(f"wikidata: {len(qids)} items linked")
    return {fam: plausible(cache.get(f"wd:{q}")[1], today) for fam, q in qids.items()}


def _qid(source_id: str, metadata: str | None) -> str | None:
    if source_id.startswith("okg:Q"):
        return source_id.split(":", 1)[1]
    if metadata and "wikidataId" in metadata:
        try:
            val = json.loads(metadata).get("wikidataId")
        except ValueError:
            return None
        if isinstance(val, str) and "/Q" in val:
            return val.rsplit("/", 1)[-1]
    return None


def build(
    paths: CorpusPaths,
    offline: bool,
    workers: int,
    delay: float,
    allow_uncached: bool = False,
) -> bool:
    """Write ``ontology_dates.parquet``; False when archive lookups are incomplete."""
    conn = sqlite3.connect(str(paths.db_path))
    # The latest plausible date is the corpus capture date, not the day the
    # script runs, so the same corpus and cache always give the same table.
    today = conn.execute(
        "SELECT substr(MAX(retrieval_date), 1, 10) FROM corpus_registry"
    ).fetchone()[0]
    rows = pd.read_sql(
        "SELECT source_id, source_registry, ontology_iri, source_registry_metadata, shared_iri "
        "FROM corpus_registry",
        conn,
    )
    conn.close()
    rows["family"] = rows.source_id.map(family_key)
    # An IRI shared by distinct ontologies (dedup's shared_iri) links none of
    # them to the others' releases.
    rows["iri_n"] = rows.ontology_iri.where(rows.shared_iri.fillna(0) == 0).map(iri_match_key)
    rows["d"] = [plausible(release_date(m), today) for m in rows.source_registry_metadata]

    onts = pd.read_parquet(paths.cache_dir / "entity_ontologies.parquet")
    fam_rows = rows[rows.family.isin(set(onts.family))]

    # Registry: the family's own releases, and every row sharing one of its IRIs.
    by_iri = rows.dropna(subset=["iri_n", "d"]).groupby("iri_n").d.min()
    fam_iris = fam_rows.dropna(subset=["iri_n"]).groupby("family").iri_n.agg(set)
    fam_min = fam_rows.dropna(subset=["d"]).groupby("family").d.min()
    d_registry = {
        f: earliest([fam_min.get(f), *(by_iri.get(i) for i in fam_iris.get(f, ()))])
        for f in onts.family
    }

    # Archive candidates: every IRI of the family; Ontohub repository pages.
    targets: dict[str, list[str]] = {}
    for f in onts.family:
        urls = [u for i in sorted(fam_iris.get(f, ())) for u in archive_candidates(i)]
        if not urls and f.startswith("ontohub:"):
            repo = f.split(":", 1)[1].split("/", 1)[0]
            urls = [f"ontohub.org/{repo}"]
        targets[f] = urls
    qids = {
        f: q
        for f, q in (
            (r.family, _qid(r.source_id, r.source_registry_metadata)) for r in fam_rows.itertuples()
        )
        if q
    }

    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cache = _Cache(paths.corpus_dir / _CACHE_FILE)
    complete = True
    uncached: set[str] = set()
    if offline:
        if allow_uncached:
            uncached = _uncached_families(targets, qids, cache)
            print(
                f"offline: {len(uncached)} families have lookups absent from the cache "
                "(no evidence from those sources)"
            )
        else:
            _require_cached(
                cache,
                [f"cdx:{u}" for urls in targets.values() for u in urls]
                + [f"wd:{q}" for q in qids.values()],
            )
        d_archive = {
            f: earliest(plausible(cache.get(f"cdx:{u}")[1], today) for u in urls)
            for f, urls in targets.items()
        }
        d_wiki = {f: plausible(cache.get(f"wd:{q}")[1], today) for f, q in qids.items()}
    else:
        d_wiki = _wikidata_dates(qids, cache, today)
        undated = {f for f, d in d_registry.items() if d is None}
        complete, d_archive = _archive_dates(targets, cache, workers, delay, today, first=undated)
    if not complete:
        # A partial table would silently replace the last complete one; the
        # lookups made so far are cached, so the next run resumes.
        print(f"Not writing {paths.ontology_dates_path}: archive lookups incomplete")
        return False

    out = onts[["family", "registry", "ontology_iri"]].drop_duplicates("family").copy()
    out["d_registry"] = out.family.map(d_registry)
    out["d_archive"] = out.family.map(d_archive)
    out["d_wikidata"] = out.family.map(d_wiki)
    sources = ["d_registry", "d_archive", "d_wikidata"]
    out["birth"] = [earliest(vals) for vals in out[sources].itertuples(index=False)]
    first = out[sources].fillna("9999-12-31").to_numpy().argmin(axis=1)
    out["birth_source"] = pd.Series([sources[i][2:] for i in first], index=out.index).where(
        out.birth.notna()
    )
    out["n_sources"] = out[sources].notna().sum(axis=1)
    out["as_of"] = today  # the capture date the dates are bounded by
    # False where a lookup was absent from the cache (--offline --allow-uncached).
    out["lookups_complete"] = ~out.family.isin(uncached)
    target = paths.ontology_dates_path
    out.to_parquet(target, index=False)
    print(f"Wrote {target}  ({out.birth.notna().sum()}/{len(out)} families dated)")
    print(out.groupby("registry").birth.apply(lambda s: f"{s.notna().mean():.0%}").to_string())
    print(out.birth_source.value_counts().to_string())
    return True


@click.command()
@click.option("--offline", is_flag=True, help="Use cached network lookups only.")
@click.option(
    "--workers", type=int, default=1, show_default=True, help="Concurrent archive queries."
)
@click.option(
    "--delay",
    type=float,
    default=4.0,
    show_default=True,
    help="Seconds each worker waits between queries.",
)
@click.option(
    "--allow-uncached",
    is_flag=True,
    help="With --offline: treat lookups absent from the cache as no evidence instead of failing.",
)
@corpus_paths
def main(
    offline: bool, workers: int, delay: float, allow_uncached: bool, paths: CorpusPaths
) -> None:
    """First-evidence date of each ontology family."""
    if allow_uncached and not offline:
        raise click.UsageError("--allow-uncached needs --offline")
    paths.require_store_dir()
    if not build(
        paths, offline=offline, workers=workers, delay=delay, allow_uncached=allow_uncached
    ):
        sys.exit(EXIT_INCOMPLETE)


if __name__ == "__main__":
    main()
