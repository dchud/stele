#!/usr/bin/env bash
# Pre-push check. CI runs the same steps in the same order.
#
#   ./check.sh            everything
#   ./check.sh --quick    formatting, linting, prose, and workflows only
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

quick=0
if [[ ${1:-} == "--quick" ]]; then
    quick=1
elif [[ -n ${1:-} ]]; then
    echo "usage: $0 [--quick]" >&2
    exit 2
fi

step() { printf '\n=== %s ===\n' "$1"; }

step "sync"
uv sync --all-extras --quiet

step "format"
uv run ruff format --check .

step "lint"
uv run ruff check .

step "prose"
uv run python scripts/lint_prose.py

step "workflows"
uv run zizmor --offline .github/workflows

if (( quick )); then
    printf '\nquick: skipped mypy and pytest\n'
    exit 0
fi

step "types"
uv run mypy

step "tests"
uv run pytest

printf '\nall checks passed\n'
