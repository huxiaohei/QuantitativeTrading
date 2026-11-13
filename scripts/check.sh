#!/usr/bin/env bash
# 质量门禁：任一步失败即退出非零
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pyright
# 数据库测试需要 PostgreSQL；启动失败或连不上时直接失败，不跳过
docker compose -f compose.test.yaml up -d --wait
uv run pytest -q
