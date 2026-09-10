"""The counted, timed line long commands print while they work."""

from __future__ import annotations

import io
from typing import Any

import pytest

from stele.progress import Progress, format_duration


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, so the line rewrites."""

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [
        (0.04, "0.0s"),
        (3.25, "3.2s"),
        (9.99, "10.0s"),
        (12.4, "12s"),
        (59.6, "60s"),
        (252.0, "4m12s"),
        (3600.0, "1h00m"),
        (7845.0, "2h10m"),
    ],
)
def test_a_duration_reads_at_the_precision_it_is_useful(
    seconds: float, rendered: str
) -> None:
    assert format_duration(seconds) == rendered


def test_a_redirected_stream_gets_one_line_per_unit() -> None:
    """A log full of carriage returns is worse than a log that is long."""
    out = io.StringIO()
    p = Progress(3, stream=out)
    for name in ("dbo.Order", "dbo.Customer", "ref.Owner"):
        with p.step(name):
            pass
    p.finish()

    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    assert "\r" not in out.getvalue()
    assert lines[0].startswith("[1/3] dbo.Order")
    assert lines[2].startswith("[3/3] ref.Owner")


def test_a_terminal_gets_one_line_that_rewrites_itself() -> None:
    out = _Tty()
    p = Progress(2, stream=out)
    with p.step("dbo.Order"):
        pass
    p.finish()

    written = out.getvalue()
    assert "\r" in written
    assert written.endswith("\n")
    # The name shows while the unit is in hand, and again once it is done.
    assert written.count("dbo.Order") == 2


def test_the_counter_is_the_work_this_run_will_do() -> None:
    """A resumed run estimates against what it is going to touch."""
    out = io.StringIO()
    p = Progress(94, stream=out)
    with p.step("dbo.Order"):
        pass
    assert out.getvalue().startswith("[ 1/94]")


def test_an_estimate_appears_once_there_is_a_pace_to_go_on() -> None:
    out = io.StringIO()
    p = Progress(2, stream=out)
    with p.step("dbo.Order"):
        pass
    with p.step("dbo.Customer"):
        pass
    first, second = out.getvalue().splitlines()
    assert "eta" in first
    # Nothing is left after the last unit, so there is nothing to estimate.
    assert "eta" not in second


def test_a_unit_that_raises_still_counts_and_still_reports() -> None:
    """Hiding a failure from the counter makes the estimate drift."""
    out = io.StringIO()
    p = Progress(2, stream=out)
    with pytest.raises(RuntimeError), p.step("dbo.Order"):
        raise RuntimeError("no such table")

    assert p.completed == 1
    assert out.getvalue().startswith("[1/2] dbo.Order")


def test_a_long_name_is_cut_rather_than_wrapped() -> None:
    out = io.StringIO()
    p = Progress(1, stream=out, name_width=8)
    with p.step("dbo.AVeryLongTableNameIndeed"):
        pass
    assert "dbo.AVer " in out.getvalue()


def test_finish_is_safe_when_nothing_was_painted() -> None:
    out: Any = _Tty()
    Progress(0, stream=out).finish()
    assert out.getvalue() == ""
