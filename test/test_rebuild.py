"""``ontometer-corpus rebuild``: step selection and argument handling, via --dry-run."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from ontometer_corpus import rebuild

PREFIX = f"+ {sys.executable} -m ontometer_corpus "


def _run(*args: str, env: dict[str, str] | None = None) -> Result:
    return CliRunner().invoke(rebuild.main, ["--dry-run", *args], env=env)


def _commands(res: Result) -> list[str]:
    return [line for line in res.output.splitlines() if line.startswith("+ ")]


def test_dry_run_lists_the_selected_steps_in_order() -> None:
    res = _run("--out", "out", "--from", "dedup", "--to", "network")
    assert res.exit_code == 0, res.output
    cmds = _commands(res)
    assert len(cmds) == 3
    assert cmds[0] == PREFIX + "process --phases dedup"
    assert cmds[1] == PREFIX + "term-coupling"
    assert cmds[2] == PREFIX + "network"


def test_metric_table_is_rebuilt_after_its_inputs() -> None:
    res = _run("--out", "out", "--from", "structural_metrics", "--to", "roles")
    assert res.exit_code == 0, res.output
    assert _commands(res) == [
        PREFIX + "structural-metrics",
        PREFIX + "ontology-metrics",
        PREFIX + "entity-roles",
    ]


def test_redeclarations_run_after_abstractness() -> None:
    res = _run("--out", "out", "--from", "abstractness", "--to", "redeclarations")
    assert _commands(res) == [PREFIX + "abstractness", PREFIX + "redeclarations"]


def test_relative_paths_are_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = _run("--out", "out", "--corpus", "d", "--from", "growth_viz", "--to", "growth_viz")
    assert res.exit_code == 0, res.output
    cmd = _commands(res)[0]
    base = tmp_path.resolve()
    assert f"--corpus {base}/d" in cmd
    assert f"--out {base}/out" in cmd


def test_validation_is_skipped_without_norms() -> None:
    res = _run("--out", "o", "--from", "validation", "--to", "validation")
    assert res.exit_code == 0
    assert "validation: skipped" in res.output


def test_reparse_requires_the_parse_step() -> None:
    res = _run("--out", "o", "--from", "dedup", "--reparse")
    assert res.exit_code == 2
    assert "--reparse" in res.output


def test_out_is_required_and_steps_are_checked() -> None:
    assert _run().exit_code == 2
    assert _run("--out", "o", "--from", "nope").exit_code == 2


def test_local_mode_sandboxes_every_step_and_dates_from_the_cache() -> None:
    res = _run("--out", "o", "--local", "--from", "term_coupling", "--to", "dates")
    assert res.exit_code == 0, res.output
    cmds = _commands(res)
    assert cmds and all(c.startswith("+ bwrap --dev-bind / / --unshare-net ") for c in cmds)
    assert cmds[-1].endswith("dates --offline --allow-uncached")


def test_local_mode_refuses_to_run_unguarded() -> None:
    res = CliRunner().invoke(
        rebuild.main,
        ["--out", "o", "--local", "--from", "dates", "--to", "dates"],
        env={"BWRAP": "/nonexistent/bwrap"},
    )
    assert res.exit_code == 2
    assert "bubblewrap" in res.output


def test_offline_and_local_are_exclusive() -> None:
    assert _run("--out", "o", "--offline", "--local").exit_code == 2


def test_incomplete_dating_exits_75_and_points_at_a_command_that_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rebuild.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, rebuild.INCOMPLETE),
    )
    res = CliRunner().invoke(rebuild.main, ["--out", "o", "--from", "dates", "--to", "dates"])
    assert res.exit_code == 75
    assert "--from dates" in res.output
    assert "--local" in res.output


def test_rebuild_verifies_store_first_and_indexes_last() -> None:
    cmds = _commands(_run("--out", "o"))
    assert cmds[0] == PREFIX + "store verify --if-indexed"
    assert cmds[1] == PREFIX + "process --phases seed,parse"
    assert cmds[-1] == PREFIX + "store index"
    assert len(cmds) == len(rebuild.STEPS) - 1  # validation is skipped


def test_from_dedup_skips_verify() -> None:
    cmds = _commands(_run("--out", "o", "--from", "dedup"))
    assert not any("store verify" in c for c in cmds)
    assert cmds[-1] == PREFIX + "store index"


def test_reparse_accepted_from_the_verify_step() -> None:
    res = _run("--out", "o", "--reparse", "--to", "parse")
    assert res.exit_code == 0
    assert _commands(res)[-1] == PREFIX + "process --phases seed,parse --reparse"


def test_every_step_invokes_a_registered_command() -> None:
    from ontometer_corpus.cli import COMMANDS

    for line in _commands(_run("--out", "o", "--validation", "n.tsv")):
        assert line.removeprefix(PREFIX).split()[0] in COMMANDS
