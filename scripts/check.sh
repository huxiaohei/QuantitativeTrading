#!/usr/bin/env bash
# 质量门禁：任一步失败即退出非零
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
