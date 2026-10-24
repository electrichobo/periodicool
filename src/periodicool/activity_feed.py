"""What PeriodiCool is doing right now, for the readout under the nav (like Sonarr's
task list in the corner). Kept in memory only: running tasks with their latest line,
and the last few finished ones for a short while.

A task is tied to the thread that runs it, so code deep inside a search can call
`say()` without knowing which task it belongs to; outside a task, `say()` does nothing.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

KEEP_FINISHED_S = 12.0
KEEP_FINISHED = 5


@dataclass
class Task:
    id: int
    title: str
    line: str = ""
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    ok: bool = True

    @property
    def running(self) -> bool:
        return self.ended is None


_lock = threading.Lock()
_ids = itertools.count(1)
_running: dict[int, Task] = {}
_finished: deque[Task] = deque(maxlen=KEEP_FINISHED)
_local = threading.local()


@contextmanager
def task(title: str, done: str | None = None):
    """Show `title` while the block runs. The block's last `say()` line (or `done`) stays
    visible for a few seconds after it ends; an exception marks it failed."""
    item = Task(next(_ids), title)
    with _lock:
        _running[item.id] = item
    outer = getattr(_local, "task", None)
    _local.task = item
    try:
        yield item
    except Exception as exc:
        item.ok, item.line = False, f"Failed: {str(exc)[:200]}"
        raise
    else:
        if done:
            item.line = done
    finally:
        _local.task = outer
        item.ended = time.monotonic()
        with _lock:
            _running.pop(item.id, None)
            _finished.append(item)


def say(line: str) -> None:
    """Update the current thread's task line, if a task is running here."""
    item = getattr(_local, "task", None)
    if item is not None:
        item.line = line[:300]


def snapshot() -> tuple[list[Task], list[Task]]:
    """(running, recently finished) tasks, oldest first."""
    now = time.monotonic()
    with _lock:
        running = sorted(_running.values(), key=lambda t: t.id)
        recent = [t for t in _finished if t.ended and now - t.ended < KEEP_FINISHED_S]
    return running, recent


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _running.clear()
        _finished.clear()
