"""The repository's prose lint: what it catches and what it leaves alone."""

from __future__ import annotations

from pathlib import Path

import pytest
from lint_prose import ALLOW, MAX_CHANGELOG_WORDS, RULES, check_changelog


def first_match(line: str) -> str | None:
    if ALLOW in line:
        return None
    for pattern, label in RULES:
        if pattern.search(line):
            return label
    return None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("resolved by bd-o8p last week", "ticket ID"),
        ("parent epic bd-0x73.12 covers this", "ticket ID"),
        ("see PR #117 for the reasoning", "pull-request reference"),
        ("closes #42", "bare issue reference"),
        ("strict since 0.8.1", "version-tagged claim"),
        ("as of v2.0 this is the default", "version-tagged claim"),
    ],
)
def test_flags_process_labels(line: str, expected: str) -> None:
    assert first_match(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "phase 2 of the rollout",
        "version 0.8.1 of the library",
        "the accent colour is #123456",
        "900/1700-byte index key limits",
        "a broadband connection",
        f"tracked in bd-o8p  # {ALLOW}",
    ],
)
def test_leaves_ordinary_prose_alone(line: str) -> None:
    assert first_match(line) is None


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_terse_changelog_entry_passes(tmp_path: Path) -> None:
    path = write(tmp_path, "## [Unreleased]\n\n- Ships a py.typed marker.\n")
    assert check_changelog(path) == []


def test_bloated_changelog_entry_is_flagged(tmp_path: Path) -> None:
    entry = " ".join(["word"] * (MAX_CHANGELOG_WORDS + 5))
    path = write(tmp_path, f"## [Unreleased]\n\n- {entry}\n")
    problems = check_changelog(path)
    assert len(problems) == 1
    assert "changelog entry is" in problems[0]


def test_wrapped_entry_counts_all_its_lines(tmp_path: Path) -> None:
    half = " ".join(["word"] * (MAX_CHANGELOG_WORDS // 2 + 3))
    path = write(tmp_path, f"## [Unreleased]\n\n- {half}\n  {half}\n")
    assert len(check_changelog(path)) == 1


def test_missing_changelog_is_not_an_error(tmp_path: Path) -> None:
    assert check_changelog(tmp_path / "nope.md") == []
