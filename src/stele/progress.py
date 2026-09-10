"""A counted, timed line for commands that run for a long time.

`profile` and `infer --validate` each send one statement per table or per
candidate, and on a large catalog that is thousands of statements over many
minutes. What an operator needs while it happens is which one is in hand,
how long the last one took, and how much is left.

Output goes to stderr, so a command's own result stays on stdout and stays
pipeable. On a terminal the line rewrites itself and shows the unit being
worked on; where stderr is redirected it prints one line per completed
unit instead, because a log full of carriage returns is worse than a log
that is long.

Deliberately not a dependency. `rich` and `tqdm` are each larger than a
counter, a clock and an estimate.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

#: Room for a schema-qualified table name before the timings.
NAME_WIDTH = 32


def format_duration(seconds: float) -> str:
    """A duration at the precision a reader can act on.

    Tenths below ten seconds, because that is where per-table differences
    show; whole minutes above an hour, because nobody schedules around the
    seconds of a two-hour run.
    """
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Progress:
    """Counts units of work and reports each one as it completes.

    `total` is the work this run will do, not the size of the catalog. A
    resumed run that skips two thirds of its tables should estimate
    against the third it is going to touch, or the first line it prints is
    a lie about how far along it is.
    """

    def __init__(
        self,
        total: int,
        *,
        stream: TextIO | None = None,
        name_width: int = NAME_WIDTH,
    ) -> None:
        self.total = total
        self.completed = 0
        self._stream = sys.stderr if stream is None else stream
        self._name_width = name_width
        self._started = time.monotonic()
        self._durations: list[float] = []
        self._live = bool(getattr(self._stream, "isatty", lambda: False)())
        self._painted = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time one unit, reporting it even when it raises.

        A unit that failed still took time and still advances the count -
        the run has that much less left to do either way, and hiding the
        failure from the counter makes the estimate drift.
        """
        if self._live:
            self._paint(f"{self._label(self.completed + 1)} {name}")
        started = time.monotonic()
        try:
            yield
        finally:
            taken = time.monotonic() - started
            self._durations.append(taken)
            self.completed += 1
            self._report(name, taken)

    def finish(self) -> None:
        """Release the line so whatever prints next starts clean."""
        if self._live and self._painted:
            self._stream.write("\n")
            self._stream.flush()
            self._painted = 0

    # -- rendering ---------------------------------------------------------

    def _label(self, position: int) -> str:
        width = len(str(self.total))
        return f"[{position:>{width}}/{self.total}]"

    def _report(self, name: str, taken: float) -> None:
        line = (
            f"{self._label(self.completed)} "
            f"{name[: self._name_width]:<{self._name_width}} "
            f"{format_duration(taken):>7} "
            f"elapsed {format_duration(self.elapsed)}"
        )
        eta = self._eta()
        if eta is not None:
            line += f"  eta {format_duration(eta)}"
        if self._live:
            self._paint(line)
        else:
            self._stream.write(line + "\n")
            self._stream.flush()

    def _eta(self) -> float | None:
        """Remaining work at the pace set so far, or nothing to say yet."""
        remaining = self.total - self.completed
        if remaining <= 0 or not self._durations:
            return None
        mean = sum(self._durations) / len(self._durations)
        return mean * remaining

    def _paint(self, line: str) -> None:
        """Overwrite the current line, clearing what the last one left."""
        pad = max(0, self._painted - len(line))
        self._stream.write("\r" + line + " " * pad)
        self._stream.flush()
        self._painted = len(line)
