"""``ontometer-corpus store``: index, verify and exchange the object store."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import click
from suthing import atomic_write

from ontometer_corpus.paths import CorpusPaths, corpus_paths
from ontometer_corpus.store.bundle import DEFAULT_LEVEL, bundle
from ontometer_corpus.store.index import (
    IndexMissingAtRev,
    VerifyReport,
    build_index,
    index_at_rev,
    move_orphans,
    verify,
    write_index,
)
from ontometer_corpus.store.licence import email_counts, load_policy, verdicts
from ontometer_corpus.store.ops import (
    StoreError,
    gc,
    pull,
    pull_caches,
    push,
    push_caches,
    status,
    tag,
)
from ontometer_corpus.store.publish import data_at_rev, fetch_withheld, publish
from ontometer_corpus.store.remote import (
    CACHE_REMOTE,
    CORPUS_REMOTE,
    STORE_REMOTE,
    Remote,
    open_remote,
)
from ontometer_corpus.store.zenodo import SANDBOX_URL, ZENODO_URL, create_draft, load_metadata


@click.group()
def cli() -> None:
    """Index, verify and exchange the object store (--store)."""


@cli.command("index")
@corpus_paths
def index_cmd(paths: CorpusPaths) -> None:
    """Write corpus/objects.tsv from corpus.db and the local store."""
    entries = build_index(paths)
    write_index(entries, paths.objects_index_path)
    click.echo(f"{len(entries)} objects -> {paths.objects_index_path}")


def _summarise(report: VerifyReport) -> None:
    click.echo(
        f"missing {len(report.missing)}, wrong size {len(report.wrong_size)}, "
        f"wrong hash {len(report.wrong_hash)}, orphans {len(report.orphans)}"
    )
    for label, items in (
        ("missing", report.missing),
        ("wrong size", report.wrong_size),
        ("wrong hash", report.wrong_hash),
    ):
        for entry in items[:20]:
            click.echo(f"  {label}: {entry.path}")


@cli.command("verify")
@click.option("--deep", is_flag=True, help="Re-hash every object instead of comparing sizes.")
@click.option("--rev", default=None, help="Git revision whose corpus/objects.tsv to check against.")
@click.option(
    "--if-indexed",
    is_flag=True,
    help="Succeed with a notice when there is no object index yet (a first build).",
)
@click.option(
    "--orphans-to",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Move objects the index does not list into this directory.",
)
@corpus_paths
def verify_cmd(
    deep: bool,
    rev: str | None,
    if_indexed: bool,
    orphans_to: Path | None,
    paths: CorpusPaths,
) -> None:
    """Check the local store against the object index; exit 1 if anything is missing."""
    if orphans_to is not None and rev is not None:
        raise click.UsageError(
            "--orphans-to moves objects the working index does not list; drop --rev, "
            "or objects added since that revision would be moved out"
        )
    if if_indexed and rev is None and not paths.objects_index_path.exists():
        click.echo(f"no object index at {paths.objects_index_path}; nothing to verify")
        return
    try:
        entries = index_at_rev(paths.corpus_dir, rev)
    except IndexMissingAtRev as exc:
        raise click.ClickException(str(exc)) from exc
    report = verify(paths, entries, deep=deep)
    _summarise(report)
    if orphans_to is not None and report.orphans:
        click.echo(f"moved {move_orphans(report, orphans_to)} orphans to {orphans_to}")
    if not report.ok():
        sys.exit(1)


def _remote_option(command):
    return click.option(
        "--store-remote",
        "remote_url",
        envvar="ONTOMETER_STORE_REMOTE",
        show_envvar=True,
        default=STORE_REMOTE,
        show_default=True,
        help="Store remote: hf://datasets/<org>/<name> or file:///path.",
    )(command)


def _cache_remote_option(command):
    return click.option(
        "--cache-remote",
        "cache_remote_url",
        envvar="ONTOMETER_CACHE_REMOTE",
        show_envvar=True,
        default=CACHE_REMOTE,
        show_default=True,
        help="Cache remote, used with --caches: hf://datasets/<org>/<name> or file:///path.",
    )(command)


def _open(url: str) -> Remote:
    try:
        return open_remote(url)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _entries(paths: CorpusPaths, rev: str | None):
    try:
        return index_at_rev(paths.corpus_dir, rev)
    except IndexMissingAtRev as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("status")
@click.option("--rev", default=None, help="Git revision whose index to compare.")
@_remote_option
@corpus_paths
def status_cmd(rev: str | None, remote_url: str, paths: CorpusPaths) -> None:
    """What the remote and the local store lack for an index; exit 1 if anything."""
    result = status(paths, _open(remote_url), _entries(paths, rev))
    click.echo(
        f"remote missing {len(result.remote_missing)}, remote bad {len(result.remote_bad)}, "
        f"local missing {len(result.local_missing)}"
    )
    for entry in result.remote_bad[:20]:
        click.echo(f"  remote bad: {entry.path}")
    if not result.ok():
        sys.exit(1)


@cli.command("push")
@click.option("--caches", is_flag=True, help="Also upload the current cache versions.")
@click.option("--limit", type=int, default=None, help="Upload at most N objects.")
@_remote_option
@_cache_remote_option
@corpus_paths
def push_cmd(
    caches: bool,
    limit: int | None,
    remote_url: str,
    cache_remote_url: str,
    paths: CorpusPaths,
) -> None:
    """Upload indexed objects the remote lacks; exit 1 if any was corrupt or missing."""
    cache_remote = _open(cache_remote_url) if caches else None
    report = push(paths, _open(remote_url), _entries(paths, None), limit=limit)
    click.echo(
        f"uploaded {len(report.uploaded)}, corrupt {len(report.corrupt)}, "
        f"missing locally {len(report.missing_locally)}"
    )
    for entry in report.corrupt[:20]:
        click.echo(f"  corrupt: {entry.path}")
    if cache_remote is not None:
        click.echo(f"uploaded {push_caches(paths, cache_remote)} cache files")
    if report.corrupt or report.missing_locally:
        sys.exit(1)


@cli.command("pull")
@click.option("--rev", default=None, help="Git revision whose index to fetch.")
@click.option("--caches", is_flag=True, help="Also download the current cache versions.")
@click.option("--limit", type=int, default=None, help="Download at most N objects.")
@_remote_option
@_cache_remote_option
@corpus_paths
def pull_cmd(
    rev: str | None,
    caches: bool,
    limit: int | None,
    remote_url: str,
    cache_remote_url: str,
    paths: CorpusPaths,
) -> None:
    """Download indexed objects the local store lacks; exit 1 if any could not be had."""
    cache_remote = _open(cache_remote_url) if caches else None
    report = pull(paths, _open(remote_url), _entries(paths, rev), limit=limit)
    click.echo(
        f"downloaded {len(report.downloaded)}, rejected {len(report.rejected)}, "
        f"unavailable {len(report.unavailable)}"
    )
    if cache_remote is not None:
        click.echo(f"downloaded {pull_caches(paths, cache_remote)} cache files")
    if report.rejected or report.unavailable:
        sys.exit(1)


@cli.command("tag")
@click.argument("name")
@_remote_option
@corpus_paths
def tag_cmd(name: str, remote_url: str, paths: CorpusPaths) -> None:
    """Tag the remote with git tag NAME, once it holds every object that tag indexes."""
    try:
        tag(paths, _open(remote_url), name)
    except (StoreError, IndexMissingAtRev) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"tagged {name}")


@cli.command("gc")
@click.option("--store-remote", "remote_url", envvar="ONTOMETER_STORE_REMOTE", default=None)
@corpus_paths
def gc_cmd(remote_url: str | None, paths: CorpusPaths) -> None:
    """List objects no corpus-* tag references. Deletes nothing."""
    report = gc(paths, _open(remote_url) if remote_url else None)
    for path in report.local:
        click.echo(f"local: {path}")
    for rel in report.remote:
        click.echo(f"remote: {rel}")
    for name in report.tags_without_index:
        click.echo(f"tag without index: {name}")
    click.echo(f"{len(report.local)} local, {len(report.remote)} remote unreferenced")


def _write_licence_report(results, counts: dict[str, int], out: Path) -> None:
    lines = ["file_hash\tpath\tlicence_ids\tevidence\tredistribute\treason\temails"]
    lines += [
        f"{v.file_hash}\t{v.path}\t{'|'.join(v.licence_ids)}\t{v.evidence}\t"
        f"{int(v.redistribute)}\t{v.reason}\t{counts.get(v.file_hash, '')}"
        for v in results
    ]
    atomic_write(out, "\n".join(lines) + "\n", mkdir=True)


@cli.command("licenses")
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Redistribution policy; corpus/redistribution.yaml by default.",
)
@click.option("--emails", is_flag=True, help="Also count email addresses in every object.")
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write one row per object to this TSV.",
)
@corpus_paths
def licenses_cmd(
    policy_path: Path | None, emails: bool, out: Path | None, paths: CorpusPaths
) -> None:
    """Report the redistribution verdict of every object under the policy."""
    results = verdicts(paths, _policy(paths, policy_path))
    counts = email_counts(paths, _entries(paths, None)) if emails else {}
    tally = Counter(
        ("publish" if v.redistribute else "withhold", v.evidence, v.reason) for v in results
    )
    for (decision, evidence, reason), n in sorted(tally.items(), key=lambda kv: -kv[1]):
        click.echo(f"{n:6d}  {decision:8s}  {evidence:8s}  {reason}")
    published = sum(v.redistribute for v in results)
    click.echo(f"{published} of {len(results)} objects redistributable")
    if counts:
        click.echo(
            f"{sum(1 for c in counts.values() if c)} objects contain email addresses "
            f"({sum(counts.values())} distinct per-object addresses)"
        )
    if out is not None:
        _write_licence_report(results, counts, out)
        click.echo(f"-> {out}")


def _policy(paths: CorpusPaths, policy_path: Path | None):
    return load_policy(policy_path or paths.corpus_dir / "redistribution.yaml")


_policy_option = click.option(
    "--policy",
    "policy_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Redistribution policy; corpus/redistribution.yaml by default.",
)


@cli.command("publish")
@click.option("--rev", required=True, help="Git tag of the corpus version to publish.")
@click.option(
    "--corpus-remote",
    "public_url",
    envvar="ONTOMETER_CORPUS_REMOTE",
    show_envvar=True,
    default=CORPUS_REMOTE,
    show_default=True,
    help="Public dataset: hf://datasets/<org>/<name> or file:///path.",
)
@_policy_option
@corpus_paths
def publish_cmd(rev: str, public_url: str, policy_path: Path | None, paths: CorpusPaths) -> None:
    """Publish a tagged version: redistributable objects, full index, tables, card."""
    try:
        policy = _policy(paths, policy_path) if policy_path else None
        report = publish(paths, _open(public_url), rev, policy)
    except (StoreError, IndexMissingAtRev) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{rev}: {len(report.uploaded)} objects published, {len(report.withheld)} withheld")


@cli.command("fetch-withheld")
@click.option("--rev", default=None, help="Git revision whose index to complete.")
@_policy_option
@corpus_paths
def fetch_withheld_cmd(rev: str | None, policy_path: Path | None, paths: CorpusPaths) -> None:
    """Download withheld objects from their sources, keeping only identical bytes."""
    policy = _policy(paths, policy_path) if policy_path else None
    report = fetch_withheld(paths, _entries(paths, rev), policy, rev=rev)
    click.echo(
        f"fetched {len(report.fetched)}, changed at source {len(report.changed)}, "
        f"unavailable {len(report.failed)}"
    )


@cli.command("bundle")
@click.option("--rev", required=True, help="Git tag of the corpus version.")
@click.option("--base", default=None, help="Earlier tag: bundle only objects new since it.")
@click.option("--public", is_flag=True, help="Only objects the redistribution policy allows.")
@click.option("--level", type=int, default=DEFAULT_LEVEL, show_default=True, help="zstd level.")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for the .tar.zst and its .sha256 list.",
)
@_policy_option
@corpus_paths
def bundle_cmd(
    rev: str,
    base: str | None,
    public: bool,
    level: int,
    out_dir: Path,
    policy_path: Path | None,
    paths: CorpusPaths,
) -> None:
    """Write a solid tar.zst of a tagged version (or of what is new since --base)."""
    policy = _policy(paths, policy_path) if public else None
    try:
        result = bundle(paths, rev, out_dir, base=base, policy=policy, level=level)
    except (StoreError, IndexMissingAtRev) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{len(result.entries)} objects -> {result.archive}")


@cli.command("zenodo")
@click.option("--rev", required=True, help="Git tag of the corpus version.")
@click.option("--base", default=None, help="Tag of the version the record already holds.")
@click.option("--record", type=int, default=None, help="Existing record: make a new version of it.")
@click.option("--sandbox", is_flag=True, help="Use sandbox.zenodo.org.")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for the bundle and report files before upload.",
)
@click.option(
    "--token",
    envvar="ZENODO_TOKEN",
    show_envvar=True,
    required=True,
    help="Zenodo personal access token (deposit scopes).",
)
@_policy_option
@corpus_paths
def zenodo_cmd(
    rev: str,
    base: str | None,
    record: int | None,
    sandbox: bool,
    out_dir: Path,
    token: str,
    policy_path: Path | None,
    paths: CorpusPaths,
) -> None:
    """Create a Zenodo draft with the public bundle of a tagged version. Never publishes."""
    if (base is None) != (record is None):
        raise click.UsageError("--base and --record go together: a new version adds a delta")
    policy = _policy(paths, policy_path)
    try:
        result = bundle(paths, rev, out_dir, base=base, policy=policy)
        with tempfile.TemporaryDirectory() as tmp:
            at_rev = data_at_rev(paths, rev, Path(tmp) / "data")
            report = out_dir / f"licences-{rev}.tsv"
            _write_licence_report(verdicts(at_rev, policy), {}, report)
            index = out_dir / f"objects-{rev}.tsv"
            shutil.copyfile(at_rev.objects_index_path, index)
    except (StoreError, IndexMissingAtRev) as exc:
        raise click.ClickException(str(exc)) from exc
    draft = create_draft(
        [result.archive, result.hashes, index, report],
        load_metadata(paths.corpus_dir / "zenodo.yaml", rev),
        token=token,
        base_url=SANDBOX_URL if sandbox else ZENODO_URL,
        record=record,
    )
    click.echo(f"draft {draft.id}: {draft.html}")
    click.echo("Review it there and publish from the web interface; this tool never publishes.")
