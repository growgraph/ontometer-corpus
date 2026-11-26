"""Run one task per OS process, killing any that overrun a wall-clock budget.

Corpus passes that re-read raw ontology files meet pathological inputs: a
malformed RDF/XML file can hold lxml inside C code indefinitely. ``signal.alarm``
fires only between Python bytecodes, so it cannot interrupt such a hang; a
supervisor that terminates the worker from outside can. Each task therefore
gets its own forked process and a one-way pipe for its single result, and the
parent enforces the budget with SIGTERM, then SIGKILL.

The design choice is one process per task rather than a pool: a pool recycles
workers, so a task that hangs or leaks memory takes innocent tasks down with
it. Forking per task costs milliseconds; the tasks here cost seconds.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

T = TypeVar("T")

Record = dict[str, Any]


def _child(
    target: Callable[[Any], Record], task: Any, on_error: Callable[[Any, str], Record], conn
) -> None:
    try:
        rec = target(task)
    except BaseException as exc:  # incl. MemoryError — the record says what happened
        rec = on_error(task, f"{type(exc).__name__}: {str(exc)[:160]}")
    try:
        conn.send(rec)
    except (BrokenPipeError, OSError):
        pass  # the parent already gave up on this task
    finally:
        conn.close()


def run_supervised(
    tasks: Iterable[T],
    target: Callable[[T], Record],
    on_error: Callable[[T, str], Record],
    *,
    timeout_s: float,
    workers: int = 8,
    progress_every: int = 200,
    label: str = "done",
) -> list[Record]:
    """Apply *target* to every task in a separate process; return the records.

    Args:
        tasks: Inputs, each passed to *target* in its own forked process.
        target: Produces one picklable record per task.
        on_error: Builds the record for a task that raised, timed out, or died.
        timeout_s: Wall-clock budget per task, enforced from outside.
        workers: Maximum concurrent processes.
        progress_every: Print a progress line every N completed tasks.
        label: Verb for the progress line.

    Returns:
        One record per task, in completion order.
    """
    ctx = mp.get_context("fork")
    pending = deque(tasks)
    total = len(pending)
    running: dict[int, tuple[Any, Any, T, float]] = {}
    records: list[Record] = []

    def spawn(task: T) -> None:
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(target, task, on_error, child_conn))
        proc.start()
        child_conn.close()  # only the child writes
        pid = proc.pid
        assert pid is not None  # set by start()
        running[pid] = (proc, parent_conn, task, time.monotonic())

    while pending or running:
        while len(running) < workers and pending:
            spawn(pending.popleft())

        for pid, (proc, conn, task, start) in list(running.items()):
            if conn.poll(0.05):
                try:
                    rec = conn.recv()
                except EOFError:
                    rec = on_error(task, "EOFError: worker died without a result")
                proc.join(2)
            elif time.monotonic() - start > timeout_s:
                proc.terminate()
                proc.join(3)
                if proc.is_alive():
                    proc.kill()
                    proc.join(3)
                rec = on_error(task, f"HardTimeout: exceeded {timeout_s:.0f}s wall clock")
            else:
                continue
            conn.close()
            records.append(rec)
            del running[pid]
            if len(records) % progress_every == 0 or len(records) == total:
                n_ok = sum(1 for r in records if r.get("ok"))
                print(
                    f"  {len(records)}/{total} {label}  (ok={n_ok} fail={len(records) - n_ok})",
                    flush=True,
                )

    return records
