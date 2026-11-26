"""
Ontology bulk fetcher (orchestrator).

About:
    Thin orchestrator that drives the per-source fetcher modules in
    ``ontometer_corpus.fetchers`` and writes results to a consolidated
    manifest.jsonl in the corpus directory.

    Raw files go into the content-addressed object store under
    ``<store>/objects/``. Every download attempt, successful or not,
    produces one FetchRecord (see ``ontometer_corpus.fetchers._base``) that is
    appended to manifest.jsonl (one JSON object per line) with status
    ``ok`` or ``fail``. Failures are also written to ``fetch_failures.log``
    in the corpus directory. Source_ids already captured with status ``ok`` are
    skipped on a re-run; failed ones are retried.

    --sources selects the non-OntoPortal fetchers (LOV, OBO, OKG, Ontohub);
    --ontoportal selects OntoPortal Alliance instance(s). Independent
    instances can be run concurrently as separate processes, since each has
    its own host and rate limit.

    API keys are read from the environment only, one variable per OntoPortal
    instance (``BIOPORTAL``, ``AGROPORTAL``, …); an instance without its key
    is skipped.

Usage:
    ontometer-corpus fetch --store /path/to/store --sources all
    ontometer-corpus fetch --store /path/to/store --ontoportal BioPortal
    ONTOMETER_STORE=/path/to/store ontometer-corpus fetch --sources lov,okg
"""

import os
from collections.abc import Callable
from pathlib import Path

import click
import requests
from suthing import iter_jsonl

from ontometer_corpus.fetchers._base import FetchRecord, make_session
from ontometer_corpus.fetchers.lov import fetch_lov
from ontometer_corpus.fetchers.obo import fetch_obo
from ontometer_corpus.fetchers.okg import fetch_okg
from ontometer_corpus.fetchers.ontohub import fetch_ontohub
from ontometer_corpus.fetchers.ontoportal import OntoPortalInstance, fetch_ontoportal
from ontometer_corpus.paths import CorpusPaths, corpus_paths

# =============================================================================
# CONFIG
# =============================================================================

# API keys are read from the environment only.

# Paths are NOT resolved here. They are resolved in main() from
# --corpus / --store so a run can be pointed anywhere, and so that
# nothing is derived from __file__ — which is the repository root from a
# checkout and site-packages once the package is installed.

# OntoPortal Alliance instances. Each runs the same OntoPortal REST API, needs
# its own API key read from the named environment variable, and is skipped
# without one.
ONTOPORTAL_INSTANCES: list[OntoPortalInstance] = [
    OntoPortalInstance(
        "BioPortal", "bp", "https://data.bioontology.org", os.environ.get("BIOPORTAL", "")
    ),
    OntoPortalInstance(
        "AgroPortal", "agroportal", "https://data.agroportal.eu", os.environ.get("AGROPORTAL", "")
    ),
    OntoPortalInstance(
        "BiodivPortal",
        "biodivportal",
        "https://data.biodivportal.gfbio.org",
        os.environ.get("BIODIVPORTAL", ""),
    ),
    OntoPortalInstance(
        "MatPortal", "matportal", "https://rest.matportal.org", os.environ.get("MATPORTAL", "")
    ),
    OntoPortalInstance(
        "OntoPortal-Astro",
        "ontoportalastro",
        "https://data.ontoportal-astro.eu",
        os.environ.get("ONTOPORTAL_ASTRO", ""),
    ),
    OntoPortalInstance(
        "TechnoPortal",
        "technoportal",
        "https://data.technoportal.hevs.ch",
        os.environ.get("TECHNOPORTAL", ""),
    ),
]


# =============================================================================
# MANIFEST WRITER + RESUME
# =============================================================================


def load_captured_source_ids(manifest_path: Path) -> set[str]:
    """
    Return the set of source_ids already captured (status 'ok') in a previous
    run, so they can be skipped. Failed attempts are NOT included — they are
    retried on the next run.
    """
    done: set[str] = set()
    if not manifest_path.exists():
        return done
    for rec in iter_jsonl(manifest_path, strict=False):
        if rec.get("status", "ok") == "ok" and rec.get("source_id"):
            done.add(rec["source_id"])
    return done


# Non-OntoPortal sources, selectable by the --sources CLI value.
SOURCE_NAMES = ("lov", "obo", "okg", "ontohub")


def build_source_fetchers(
    fail_log: Path, objects_dir: Path, manifest: Path
) -> dict[str, Callable[[requests.Session, set[str]], list[FetchRecord]]]:
    """Bind every non-OntoPortal fetcher to this run's resolved paths.

    Returns a mapping from source name to a callable taking
    ``(session, resume)`` and returning ``list[FetchRecord]``, so ``main()``
    can dispatch every selected source the same way.
    """
    return {
        "lov": lambda s, r: fetch_lov(s, fail_log, objects_dir, manifest, r),
        "obo": lambda s, r: fetch_obo(s, fail_log, objects_dir, manifest, r),
        "okg": lambda s, r: fetch_okg(s, fail_log, objects_dir, manifest, r),
        "ontohub": lambda s, r: fetch_ontohub(s, fail_log, objects_dir, manifest, r),
    }


def _resolve_sources(values: tuple[str, ...]) -> list[str]:
    """Resolve one or more --sources values (comma-separated/repeated) to fetcher names."""
    if not values:
        return []
    requested = {v.strip().lower() for chunk in values for v in chunk.split(",") if v.strip()}
    if "all" in requested:
        return sorted(SOURCE_NAMES)
    unknown = requested - set(SOURCE_NAMES)
    if unknown:
        names = ", ".join(sorted(SOURCE_NAMES))
        print(f"  [err] unknown source(s): {', '.join(sorted(unknown))}. Available: {names}")
    return sorted(requested & set(SOURCE_NAMES))


def _resolve_ontoportal_instances(selector: str | None) -> list[OntoPortalInstance]:
    """Resolve the --ontoportal CLI value to a list of instances to run."""
    if not selector:
        return []
    if selector.lower() == "all":
        return ONTOPORTAL_INSTANCES
    key = selector.lower()
    matches = [i for i in ONTOPORTAL_INSTANCES if key in (i.name.lower(), i.slug.lower())]
    if not matches:
        names = ", ".join(sorted(i.name for i in ONTOPORTAL_INSTANCES))
        print(f"  [err] no configured OntoPortal instance matches '{selector}'. Available: {names}")
    return matches


# =============================================================================
# MAIN
# =============================================================================


@click.command()
@click.option(
    "--sources",
    metavar="SOURCE[,SOURCE...]",
    multiple=True,
    help=(
        f"Non-OntoPortal sources to fetch: {', '.join(sorted(SOURCE_NAMES))}, or 'all'. "
        "Comma-separated and/or repeatable. If omitted, none of them runs."
    ),
)
@click.option(
    "--ontoportal",
    metavar="INSTANCE",
    default=None,
    help=(
        "OntoPortal Alliance instance to fetch, matched case-insensitively against name or "
        f"slug: {', '.join(sorted(i.name for i in ONTOPORTAL_INSTANCES))}, or 'all' to run "
        "every configured instance in this process."
    ),
)
@corpus_paths
def main(sources: tuple[str, ...], ontoportal: str | None, paths: CorpusPaths) -> None:
    """Fetch ontologies from the registries into the object store and the manifest.

    Each OntoPortal instance is an independent host with its own rate limit and
    API key, so instances can run as parallel processes. All of them append to
    the same manifest.jsonl, which is safe: each appends only its own records.

    \b
    ontometer-corpus fetch --sources lov,okg --ontoportal BioPortal
    ontometer-corpus fetch --sources all
    ontometer-corpus fetch --ontoportal AgroPortal &
    """
    paths.ensure_corpus_dir()

    objects_dir = paths.objects_dir
    objects_dir.mkdir(parents=True, exist_ok=True)
    manifest = paths.manifest_path
    fail_log = paths.fail_log

    print("OntoMeter — ontology bulk fetcher")
    print(paths.describe())
    print(f"  object store : {objects_dir}")
    print(f"  failure log  : {fail_log}")

    session = make_session()
    total_records: list[FetchRecord] = []
    fetchers = build_source_fetchers(fail_log, objects_dir, manifest)

    # Skip source_ids already captured (status 'ok') on a previous run.
    resume = load_captured_source_ids(manifest)
    if resume:
        print(f"  resume       : {len(resume)} source_ids already captured — skipping")

    for name in _resolve_sources(sources):
        try:
            total_records.extend(fetchers[name](session, resume))
        except Exception as exc:
            print(f"  [err] {name} fetch aborted: {exc}")

    # OntoPortal Alliance instances each have their own host and rate limit, so
    # they can be run in parallel as separate processes. Each instance is
    # isolated: one erroring out does not stop the others.
    for instance in _resolve_ontoportal_instances(ontoportal):
        if not instance.api_key:
            print(f"  [skip] {instance.name}: no API key configured")
            continue
        try:
            total_records.extend(
                fetch_ontoportal(session, fail_log, objects_dir, manifest, instance, resume)
            )
        except Exception as exc:
            print(f"  [err] {instance.name} fetch aborted: {exc}")

    n_ok = sum(1 for r in total_records if r.status == "ok")
    n_fail = sum(1 for r in total_records if r.status == "fail")

    print(f"\n{'=' * 60}")
    print(f"Total attempts written to manifest: {len(total_records)}")
    print(f"  captured (ok): {n_ok}")
    print(f"  failed:        {n_fail}")
    print(f"  manifest : {manifest}")
    print(f"  failures : {fail_log}")
    print("\nNext: seed, parse and deduplicate —")
    print("  ontometer-corpus process")


if __name__ == "__main__":
    main()
