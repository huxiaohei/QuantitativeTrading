FROM python:3.12-slim

# uv 来自官方镜像
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV TZ=Asia/Shanghai \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# 数据库备份用 pg_dump：客户端主版本必须与服务端 postgres:16 一致，否则备份里会带有
# 16 不认识的语句（如 SET transaction_timeout、\restrict），按文档恢复会失败。
# Debian 13 自带的是 17，所以从 PGDG 官方 apt 源安装 postgresql-client-16；
# 构建时检查主版本，不是 16 就让构建失败。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version \
    && pg_dump --version | grep -q ' 16\.'

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
