#!/usr/bin/env python3
"""Fail on process labels in artifacts and on bloated changelog entries.

Source, tests, and user-facing documentation outlive the work that produced
them. A ticket ID or pull-request number embedded in them stops being accurate
the moment commits are squashed or tickets renumbered, while ``git log`` and
``git blame`` stay accurate for free.

Only high-confidence shapes are flagged. Bare version numbers and the word
"phase" produce too many false positives to be worth checking. A line may opt
out with a trailing ``stele-lint: allow-process-label`` comment.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOW = "stele-lint: allow-process-label"

# Files whose whole purpose is to carry process context.
EXEMPT_PATHS = {
    "CHANGELOG.md",
    "CLAUDE.md",
    "agent-approach-brief.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "scripts/lint_prose.py",
    "tests/test_lint_prose.py",
}
EXEMPT_PREFIXES = (".beads/",)
SCANNED_SUFFIXES = (".py", ".md", ".jinja", ".sh")

TICKET = re.compile(r"\bbd-[0-9a-z]{3,}(?:\.[0-9]+)?\b", re.IGNORECASE)
PR_REF = re.compile(r"\bPRs?\s*#\d+", re.IGNORECASE)
BARE_ISSUE = re.compile(r"(?<![A-Za-z0-9_#])#\d{1,5}(?![0-9A-Za-z_])")
RELEASE_CUE = re.compile(
    r"\b(?:since|as of|flipped in|default since|new in|changed in)\s+"
    r"v?\d+\.\d+",
    re.IGNORECASE,
)

RULES = (
    (TICKET, "ticket ID"),
    (PR_REF, "pull-request reference"),
    (BARE_ISSUE, "bare issue reference"),
    (RELEASE_CUE, "version-tagged claim"),
)

# Roughly 25-40 words is the target for a changelog entry. Fail well above
# that, so the check catches an entry that has turned into a summary rather
# than nagging about a sentence or two of overrun.
MAX_CHANGELOG_WORDS = 60


def candidate_files() -> list[str]:
    """Every file git would consider, committed or not.

    Scanning only committed files would let a brand new file through the
    check on the run that matters most: the one before it is committed.
    """
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def scanned(path: str) -> bool:
    if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return False
    return path.endswith(SCANNED_SUFFIXES)


def check_process_labels(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for pattern, label in RULES:
                match = pattern.search(line)
                if match:
                    problems.append(
                        f"{path}:{lineno}: {label} {match.group(0)!r} "
                        f"in a persistent artifact"
                    )
    return problems


def check_changelog(path: Path) -> list[str]:
    """Flag changelog entries that have grown into summaries."""
    if not path.exists():
        return []

    problems: list[str] = []
    entry: list[str] = []
    start = 0

    def flush() -> None:
        if not entry:
            return
        words = len(" ".join(entry).split())
        if words > MAX_CHANGELOG_WORDS:
            problems.append(
                f"{path}:{start}: changelog entry is {words} words; keep it "
                f"under {MAX_CHANGELOG_WORDS} and move the detail to the "
                f"commit message or PR description"
            )

    lines = path.read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            flush()
            entry = [stripped[2:]]
            start = lineno
        elif not stripped or stripped.startswith("#"):
            flush()
            entry = []
        elif entry:
            entry.append(stripped)
    flush()
    return problems


def main() -> int:
    paths = [p for p in candidate_files() if scanned(p)]
    problems = check_process_labels(paths)
    problems += check_changelog(Path("CHANGELOG.md"))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
