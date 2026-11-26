"""
Ontologies published on GitHub, as a second list beside the registries.

Part of the resource paper's analysis. The registries hold only the
ontologies someone registered; GitHub holds many that were never registered.
Matching a random sample of GitHub ontologies against the corpus by ontology
IRI gives, per period of first appearance, the share of GitHub ontologies the
registries hold (their coverage) and, with the corpus, a two-list
(Lincoln-Petersen) estimate of the population both draw from.

Sampling. GitHub's code search covers default branches and files under 384 KB
and returns at most 1,000 results per query, so the files are sampled rather
than listed. Each file type (``FILE_TYPES``) is split into size bins
(``SIZE_EDGES``); each bin's total comes from the search, and within each bin a
few randomly placed narrow size windows are listed in full. A file's weight is
its bin's total over the files sampled from that bin.

``plan``      counts the files per (type, size bin).
``sample``    lists the files in the random windows.
``enrich``    fetches each sampled file, reads its ontology IRI, and dates it
              by the oldest commit touching its path.
``recall``    checks the search index against repositories the registries name in
              their own metadata: for each, whether code search finds a file declaring
              an ontology there, with the repository's creation and last-push dates, so
              that the index's coverage can be compared across old and recent
              repositories. Writes ``<dir>/recall.csv``.
``signals``   for the authored ontologies first committed since ``SIGNALS_FROM``,
              looks for signs of LLM assistance: an AI co-author or tool named in
              the first commit's message, AI terms in the repository's description
              or topics, and a note in the file saying a model generated it.
              Writes ``<dir>/signals.csv``.
``baseline``  counts the repositories created on GitHub each year (forks excluded), the
              growth of GitHub itself against which the ontologies' growth is read.
              Writes ``<dir>/baseline.csv``.
``analyse``   matches the IRIs to the corpus and writes
              ``<dir>/github_ontologies.parquet``: one row per distinct
              ontology IRI with its earliest first-commit date, its weight,
              whether a registry holds it, the registry's earliest release date,
              its kind (``FILE_KINDS``: an authored ontology, or a generated file,
              a test or example file, or a shapes file that also declares
              ``owl:Ontology``) and the sampling window it was drawn in.

Every API answer is cached under ``<dir>/cache/``, so a rerun reads the cache
and ``analyse`` runs offline. Requests go through an authenticated GitHub CLI
(``gh api``), read-only. The CLI uses ``GH_TOKEN`` when it is set, so a
fine-grained token limited to read-only access to public repositories keeps
the harvest off a personal login.

Usage:
    ontometer-corpus paper github plan --dir DIR
    ontometer-corpus paper github sample --dir DIR
    ontometer-corpus paper github enrich --dir DIR
    ontometer-corpus paper github recall --dir DIR --corpus CORPUS
    ontometer-corpus paper github analyse --dir DIR --corpus CORPUS
    ontometer-corpus paper github signals --dir DIR
    ontometer-corpus paper github baseline --dir DIR
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import click
import pandas as pd
import requests
from rdflib import OWL, RDF, Graph, URIRef

from ontometer_corpus.pipeline.dedup import ontology_key

#: File type -> code-search query; each finds files declaring an ontology.
FILE_TYPES = {
    "ttl": '"owl:Ontology" extension:ttl',
    "owl": '"owl:Ontology" extension:owl',
    "rdf": '"owl:Ontology" extension:rdf',
    "owx": '"Ontology" extension:owx',
    "obo": "format-version extension:obo",
}
#: Size bins in bytes; code search indexes files under 384 KB.
SIZE_EDGES = (0, 500, 1000, 2000, 4000, 8000, 16000, 32000, 64000, 128000, 256000, 384000)
#: Random windows per bin, and the files each should hold on average.
WINDOWS_PER_BIN = 4
FILES_PER_WINDOW = 20
SEED = 0
#: Seconds between code-search calls (the limit is 10 a minute).
SEARCH_PAUSE = 6.5
#: Language vocabularies and placeholder hosts are not ontologies someone published.
EXCLUDED_KEY = re.compile(
    r"^(www\.w3\.org/(2002/07/owl|1999/02/22-rdf-syntax-ns|2000/01/rdf-schema|2001/XMLSchema)"
    r"|(www\.)?example\.(org|com|net)|localhost|127\.0\.0\.1)"
)


# ---------------------------------------------------------------- GitHub access
def gh(
    endpoint: str, params: dict[str, str | int], include_headers: bool = False
) -> tuple[dict, str]:
    """One read-only GET through the GitHub CLI; returns the JSON body and the raw headers."""
    cmd = ["gh", "api", "-X", "GET", endpoint]
    for key, value in params.items():
        cmd += ["-f", f"{key}={value}"]
    if include_headers:
        cmd.append("--include")
    for attempt in range(6):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode == 0:
            break
        error = (out.stderr + out.stdout).lower()
        if "rate limit" in error:
            time.sleep(_until_reset(endpoint))
            continue
        # GitHub answers an occasional request with a spurious 401 or a 5xx; retry those.
        if any(s in error for s in ("bad credentials", "http 50", "timeout", "connection")):
            time.sleep(30 * (attempt + 1))
            continue
        raise click.ClickException(f"gh api {endpoint}: {out.stderr.strip()[:300]}")
    else:
        raise click.ClickException(f"gh api {endpoint}: rate limited")
    headers, body = "", out.stdout
    if include_headers:
        headers, _, body = out.stdout.partition("\r\n\r\n")
        if not body:
            headers, _, body = out.stdout.partition("\n\n")
    return json.loads(body) if body.strip() else {}, headers


def _until_reset(endpoint: str) -> float:
    """Seconds until the exhausted rate limit for this endpoint resets, plus a margin."""
    resource = "code_search" if endpoint.startswith("search/code") else "core"
    out = subprocess.run(
        ["gh", "api", "rate_limit", "--jq", f".resources.{resource}.reset"],
        capture_output=True,
        text=True,
    )
    try:
        return max(60.0, float(out.stdout.strip()) - time.time() + 10)
    except ValueError:
        return 300.0


class Cache:
    """JSON answers on disk, keyed by a hash of the request."""

    def __init__(self, directory: Path, kind: str) -> None:
        self.dir = directory / "cache" / kind
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        return self.dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"

    def get(self, key: str):
        p = self.path(key)
        return json.loads(p.read_text()) if p.exists() else None

    def put(self, key: str, value) -> None:
        self.path(key).write_text(json.dumps(value))


def search_count(cache: Cache, query: str) -> int:
    hit = cache.get(query)
    if hit is None:
        body, _ = gh("search/code", {"q": query, "per_page": 1})
        time.sleep(SEARCH_PAUSE)
        hit = {"total_count": body.get("total_count", 0)}
        cache.put(query, hit)
    return int(hit["total_count"])


def search_items(cache: Cache, query: str) -> list[dict]:
    """Every result of a query (at most 1,000), as repository, path and commit."""
    items: list[dict] = []
    for page in range(1, 11):
        key = f"{query}#page={page}"
        hit = cache.get(key)
        if hit is None:
            body, _ = gh("search/code", {"q": query, "per_page": 100, "page": page})
            time.sleep(SEARCH_PAUSE)
            hit = [
                {
                    "repo": it["repository"]["full_name"],
                    "path": it["path"],
                    "ref": it["html_url"].split("/blob/", 1)[1].split("/", 1)[0],
                    "fork": it["repository"].get("fork", False),
                }
                for it in body.get("items", [])
            ]
            cache.put(key, hit)
        items += hit
        if len(hit) < 100:
            break
    return items


# ---------------------------------------------------------------- ontology IRI
_OBO_ONTOLOGY = re.compile(r"^ontology:\s*(\S+)", re.M)
_OWX_ONTOLOGY = re.compile(r"<Ontology[^>]*\bontologyIRI=\"([^\"]+)\"")


def ontology_iri(text: str, file_type: str) -> str | None:
    """The IRI the file declares as its ontology, or None."""
    if file_type == "obo":
        m = _OBO_ONTOLOGY.search(text)
        return f"http://purl.obolibrary.org/obo/{m.group(1)}.owl" if m else None
    if file_type == "owx":
        m = _OWX_ONTOLOGY.search(text)
        return m.group(1) if m else None
    g = Graph()
    try:
        g.parse(data=text, format="turtle" if file_type == "ttl" else "xml")
    except Exception:
        return None
    subjects = sorted(str(s) for s in g.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef))
    return subjects[0] if subjects else None


# ---------------------------------------------------------------- commands
@click.group()
def main() -> None:
    """Sample, date and match the ontologies published on GitHub."""


def _bins() -> list[tuple[int, int]]:
    return list(zip(SIZE_EDGES[:-1], SIZE_EDGES[1:], strict=True))


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def plan(directory: Path) -> None:
    """Count the files per file type and size bin."""
    cache = Cache(directory, "search")
    rows = []
    for file_type, query in FILE_TYPES.items():
        for lo, hi in _bins():
            n = search_count(cache, f"{query} size:{lo}..{hi - 1}")
            rows.append({"type": file_type, "lo": lo, "hi": hi, "files": n})
            print(f"  {file_type} {lo}..{hi - 1}: {n:,}")
    pd.DataFrame(rows).to_csv(directory / "plan.csv", index=False)
    print(f"  total {sum(r['files'] for r in rows):,} files")


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def sample(directory: Path) -> None:
    """List every file in a few random size windows of each bin."""
    cache = Cache(directory, "search")
    plan_df = pd.read_csv(directory / "plan.csv")
    rng = random.Random(SEED)
    rows = []
    for r in plan_df.itertuples():
        if r.files == 0:
            continue
        span = r.hi - r.lo
        # Window width so that a window holds FILES_PER_WINDOW files on average.
        width = max(1, min(span, round(span * FILES_PER_WINDOW / r.files)))
        starts = sorted(
            rng.sample(range(r.lo, r.hi - width + 1), min(WINDOWS_PER_BIN, span - width + 1))
        )
        for start in starts:
            query = f"{FILE_TYPES[r.type]} size:{start}..{start + width - 1}"
            items = search_items(cache, query)
            for it in items:
                rows.append({"type": r.type, "lo": r.lo, "window": f"{r.type}:{start}", **it})
        print(f"  {r.type} {r.lo}..{r.hi - 1}: windows of {width} bytes")
    df = pd.DataFrame(rows).drop_duplicates(["repo", "path"])
    sampled = df.groupby(["type", "lo"]).size().rename("sampled").reset_index()
    weights = plan_df.merge(sampled, on=["type", "lo"])
    weights["weight"] = weights["files"] / weights["sampled"]
    df = df.merge(weights[["type", "lo", "weight"]], on=["type", "lo"])
    df.to_csv(directory / "sample.csv", index=False)
    print(f"  {len(df):,} files sampled, standing for {weights['files'].sum():,}")


def _first_commit(cache: Cache, repo: str, path: str) -> str | None:
    """Date of the oldest commit touching the path (renames before it are not followed)."""
    key = f"{repo}:{path}"
    hit = cache.get(key)
    if hit is not None:
        return hit["date"]
    endpoint = f"repos/{repo}/commits"
    body, headers = gh(endpoint, {"path": path, "per_page": 1}, include_headers=True)
    last = re.search(r'[?&]page=(\d+)>; rel="last"', headers)
    if last:
        body, _ = gh(endpoint, {"path": path, "per_page": 1, "page": last.group(1)})
    date = None
    if isinstance(body, list) and body:
        commit = body[0]["commit"]
        date = min(commit["author"]["date"], commit["committer"]["date"])
    cache.put(key, {"date": date})
    return date


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def enrich(directory: Path) -> None:
    """Read each sampled file's ontology IRI and date its first commit."""
    files = Cache(directory, "files")
    commits = Cache(directory, "commits")
    df = pd.read_csv(directory / "sample.csv")
    iris, dates = [], []
    for i, r in enumerate(df.itertuples(), 1):
        key = f"{r.repo}/{r.ref}/{r.path}"
        hit = files.get(key)
        if hit is None:
            url = f"https://raw.githubusercontent.com/{r.repo}/{r.ref}/{quote(r.path)}"
            resp = requests.get(url, timeout=60)
            text = resp.text if resp.ok else ""
            hit = {"iri": ontology_iri(text, r.type) if text else None, "status": resp.status_code}
            files.put(key, hit)
        iris.append(hit["iri"])
        dates.append(_first_commit(commits, r.repo, r.path) if hit["iri"] else None)
        if i % 200 == 0:
            print(f"  {i:,}/{len(df):,}")
    df["iri"] = iris
    df["first_commit"] = dates
    df.to_csv(directory / "sample_enriched.csv", index=False)
    print(f"  {df['iri'].notna().sum():,} of {len(df):,} files declare an ontology IRI")


_GITHUB_REPO = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


def _registry_repos(corpus: Path) -> pd.DataFrame:
    """GitHub repositories named in the registries' metadata of their latest records."""
    con = sqlite3.connect(corpus / "corpus.db")
    rows = pd.read_sql(
        "SELECT source_registry, source_registry_metadata AS m FROM corpus_registry "
        "WHERE is_latest_version = 1 AND source_registry_metadata IS NOT NULL",
        con,
    )
    found = rows["m"].map(_GITHUB_REPO.search)
    rows["repo"] = [m.group(1).removesuffix(".git") if m else None for m in found]
    return rows.dropna(subset=["repo"]).drop_duplicates("repo")[["source_registry", "repo"]]


def _found_in_repo(cache: Cache, repo: str) -> bool | None:
    """Whether code search finds a file declaring an ontology in the repository; None if the
    repository cannot be searched (renamed, removed or private)."""
    for query in ('"owl:Ontology"', '"Ontology" extension:owx', "format-version extension:obo"):
        q = f"repo:{repo} {query}"
        hit = cache.get(q)
        if hit is None:
            try:
                body, _ = gh("search/code", {"q": q, "per_page": 1})
                hit = {"total_count": body.get("total_count", 0)}
            except click.ClickException:
                hit = {"total_count": None}
            time.sleep(SEARCH_PAUSE)
            cache.put(q, hit)
        if hit["total_count"] is None:
            return None
        if hit["total_count"] > 0:
            return True
    return False


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
@click.option("--corpus", "corpus", type=click.Path(file_okay=False, path_type=Path), required=True)
def recall(directory: Path, corpus: Path) -> None:
    """Does code search find the ontologies of repositories the registries name?"""
    search, meta = Cache(directory, "recall"), Cache(directory, "repo_meta")
    rows = []
    for r in _registry_repos(corpus).itertuples():
        info = _repo_meta(meta, r.repo)
        if info.get("created") is None:
            continue
        rows.append(
            {
                "registry": r.source_registry,
                "repo": r.repo,
                "created": info["created"],
                "pushed": info.get("pushed"),
                "archived": info.get("archived", False),
                "fork": info.get("fork", False),
                "found": _found_in_repo(search, r.repo),
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(directory / "recall.csv", index=False)
    ok = df.dropna(subset=["found"])
    print(f"  {len(ok):,} searchable repositories, found in {ok['found'].astype(bool).mean():.1%}")
    pushed = pd.to_datetime(ok["pushed"], utc=True).dt.year
    print(
        ok.groupby(pd.cut(pushed, [0, 2018, 2021, 2024, 2100], right=False))["found"]
        .agg(["size", "mean"])
        .to_string()
    )


def _repo_meta(cache: Cache, repo: str) -> dict:
    """A repository's description, topics, creation and last-push dates; ``created`` is None
    when the repository cannot be read (renamed, removed or private)."""
    info = cache.get(repo)
    if info is None or "pushed" not in info:
        try:
            body, _ = gh(f"repos/{repo}", {})
            info = {
                "description": body.get("description") or "",
                "topics": body.get("topics") or [],
                "created": body.get("created_at"),
                "pushed": body.get("pushed_at"),
                "archived": body.get("archived", False),
                "fork": body.get("fork", False),
                "stars": body.get("stargazers_count", 0),
            }
        except click.ClickException:
            info = {"created": None, "pushed": None}
        cache.put(repo, info)
    return info


#: Kinds of file that declare ``owl:Ontology`` without being an authored ontology, by path
#: and IRI; anything else is an authored ontology.
FILE_KINDS = {
    "test or example": (
        re.compile(
            r"(^|/)(tests?|testing|fixtures?|examples?|samples?|demos?|testdata|test-data)(/|$)"
            r"|_test\.|test_|\.test\."
        ),
        re.compile(r"example|/test"),
    ),
    "generated": (
        re.compile(
            r"(^|/)(output|build|dist|target|gen|generated|site|public)/"
            r"|(valueset|codesystem|structuredefinition|searchparameter|conceptmap"
            r"|capabilitystatement|implementationguide|operationdefinition)-"
        ),
        re.compile(r"model\.geneontology\.org|/fhir/"),
    ),
    "shapes": (re.compile(r"shacl|shapes?(/|\.|_)"), None),
}


def file_kind(path: str, iri: str) -> str:
    """``ontology`` for an authored ontology, else the first matching kind of ``FILE_KINDS``."""
    path, iri = path.lower(), iri.lower()
    for kind, (by_path, by_iri) in FILE_KINDS.items():
        if by_path.search(path) or (by_iri is not None and by_iri.search(iri)):
            return kind
    return "ontology"


#: First year of first commit whose authored ontologies ``signals`` inspects.
SIGNALS_FROM = 2018
_AI_COMMIT = re.compile(
    r"co-authored-by:.*(claude|copilot|cursor|gemini|codex|devin|openai|anthropic)"
    r"|generated with \[?claude|🤖|chatgpt|gpt-?[345o]|\bllm\b|ai[- ]generated|copilot",
    re.I,
)
_AI_REPO = re.compile(
    r"\b(llm|llms|gpt|chatgpt|genai|generative ai|ai[- ]generated|langchain|rag|agentic|agents?"
    r"|mcp|ontogpt|claude|gemini)\b",
    re.I,
)
_AI_FILE = re.compile(
    r"(generated|created|produced|drafted|written) (by|with|using) (an? )?"
    r"(llm|large language model|chatgpt|gpt-?[345o]?|claude|gemini|copilot|ai)\b"
    r"|ontogpt|llm-generated|ai-generated",
    re.I,
)


def _first_message(cache: Cache, repo: str, path: str) -> str:
    """Message of the oldest commit touching the path."""
    key = f"{repo}:{path}"
    hit = cache.get(key)
    if hit is None:
        endpoint = f"repos/{repo}/commits"
        body, headers = gh(endpoint, {"path": path, "per_page": 1}, include_headers=True)
        last = re.search(r'[?&]page=(\d+)>; rel="last"', headers)
        if last:
            body, _ = gh(endpoint, {"path": path, "per_page": 1, "page": last.group(1)})
        hit = {"message": body[0]["commit"]["message"] if isinstance(body, list) and body else ""}
        cache.put(key, hit)
    return hit["message"]


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
def signals(directory: Path) -> None:
    """Signs of LLM assistance in recently committed authored ontologies."""
    onts = pd.read_parquet(directory / "github_ontologies.parquet")
    onts = onts[(onts["kind"] == "ontology") & (onts["first_commit"].dt.year >= SIGNALS_FROM)]
    messages, meta, texts = (
        Cache(directory, "first_message"),
        Cache(directory, "repo_meta"),
        Cache(directory, "raw_text"),
    )
    rows = []
    for r in onts.itertuples():
        info = _repo_meta(meta, r.repo)
        about = " ".join([r.repo, info.get("description") or "", *(info.get("topics") or [])])
        key = f"{r.repo}/{r.ref}/{r.path}"
        text = texts.get(key)
        if text is None:
            resp = requests.get(
                f"https://raw.githubusercontent.com/{r.repo}/{r.ref}/{quote(r.path)}", timeout=60
            )
            found = _AI_FILE.search(resp.text) if resp.ok else None
            text = {"hit": bool(found), "snippet": found.group(0) if found else ""}
            texts.put(key, text)
        rows.append(
            {
                "key": r.key,
                "year": r.first_commit.year,
                "commit_ai": bool(_AI_COMMIT.search(_first_message(messages, r.repo, r.path))),
                "repo_ai": bool(_AI_REPO.search(about)),
                "file_ai": text["hit"],
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(directory / "signals.csv", index=False)
    print(df.groupby("year")[["commit_ai", "repo_ai", "file_ai"]].mean().round(3).to_string())


#: First year of GitHub's yearly repository counts.
BASELINE_FROM = 2008


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
@click.option("--until", type=int, required=True, help="Last full year to count.")
def baseline(directory: Path, until: int) -> None:
    """Repositories created on GitHub per year, forks excluded."""
    cache = Cache(directory, "baseline")
    rows = []
    for year in range(BASELINE_FROM, until + 1):
        query = f"created:{year}-01-01..{year}-12-31"
        hit = cache.get(query)
        if hit is None:
            body, _ = gh("search/repositories", {"q": query, "per_page": 1})
            hit = {"total_count": body.get("total_count", 0)}
            cache.put(query, hit)
            time.sleep(2.5)
        rows.append({"year": year, "repositories": hit["total_count"]})
        print(f"  {year}: {hit['total_count']:,}")
    pd.DataFrame(rows).to_csv(directory / "baseline.csv", index=False)


def _corpus_keys(corpus: Path) -> pd.DataFrame:
    """Each ontology key the registries hold, with its earliest registry release date.

    Reads ``corpus.db`` and ``ontology_dates.parquet``.
    """
    con = sqlite3.connect(corpus / "corpus.db")
    rows = pd.read_sql(
        "SELECT ontology_iri, source_version_date FROM corpus_registry "
        "WHERE ontology_iri IS NOT NULL",
        con,
    )
    dates = pd.read_parquet(
        corpus / "ontology_dates.parquet", columns=["ontology_iri", "d_registry"]
    )
    dates = dates.rename(columns={"d_registry": "source_version_date"}).dropna()
    rows = pd.concat([rows, dates])
    rows["key"] = rows["ontology_iri"].map(ontology_key)
    rows["date"] = pd.to_datetime(
        rows["source_version_date"], errors="coerce", utc=True, format="ISO8601"
    )
    # Every key a registry holds, with its earliest registry release date where one is known
    # (the file's own release date, or the dating table's registry date).
    return rows.groupby("key")["date"].min().rename("registry_date").reset_index()


@main.command()
@click.option("--dir", "directory", type=click.Path(path_type=Path), required=True)
@click.option("--corpus", "corpus", type=click.Path(file_okay=False, path_type=Path), required=True)
def analyse(directory: Path, corpus: Path) -> None:
    """One row per distinct GitHub ontology, matched to the registries."""
    df = pd.read_csv(directory / "sample_enriched.csv").dropna(subset=["iri", "first_commit"])
    df["key"] = df["iri"].map(ontology_key)
    df = df[~df["key"].str.match(EXCLUDED_KEY)]
    df["first_commit"] = pd.to_datetime(df["first_commit"], utc=True)
    df["kind"] = [file_kind(path, iri) for path, iri in zip(df["path"], df["iri"], strict=True)]
    df = df.sort_values("first_commit")
    # Copies of one ontology in several repositories count once, at the earliest commit, in
    # the window and file type of that copy; its weight is the mean weight of its sampled
    # copies, and it is authored if any copy is.
    onts = df.groupby("key").agg(
        iri=("iri", "first"),
        first_commit=("first_commit", "min"),
        copies=("repo", "nunique"),
        weight=("weight", "mean"),
        window=("window", "first"),
        type=("type", "first"),
        repo=("repo", "first"),
        path=("path", "first"),
        ref=("ref", "first"),
        kind=("kind", lambda k: "ontology" if (k == "ontology").any() else k.iloc[0]),
    )
    onts = onts.reset_index().merge(_corpus_keys(corpus), on="key", how="left", indicator=True)
    onts["registered"] = onts.pop("_merge") == "both"
    onts.to_parquet(directory / "github_ontologies.parquet", index=False)
    year = onts["first_commit"].dt.year
    print(f"  {len(onts):,} distinct ontologies, {onts['registered'].mean():.1%} registered")
    print(onts.groupby([year, "kind"])["registered"].agg(["size", "mean"]).unstack().to_string())


if __name__ == "__main__":
    main()
