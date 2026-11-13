FROM python:3.12-slim

# uv 来自官方镜像
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV TZ=Asia/Shanghai \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# 先装依赖（利用构建缓存），再拷贝源码并安装项目本身
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
RUN uv sync --frozen

# 默认运行 qt；不设 ENTRYPOINT，便于 `docker compose run worker uv run ...` 执行其他命令
CMD ["qt", "--help"]
