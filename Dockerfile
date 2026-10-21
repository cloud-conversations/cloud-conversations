FROM python:3.12-slim

ARG UV_VERSION=0.12.5
ARG EXTRAS="--extra integration --extra knowledge --extra service"

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1

COPY pyproject.toml uv.lock ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "uv==${UV_VERSION}" \
 && uv sync --locked --no-install-project $EXTRAS \
 && apt-get purge -y --auto-remove git \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN uv sync --locked --no-editable $EXTRAS

ENTRYPOINT ["ccx"]
