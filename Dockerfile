# syntax=docker/dockerfile:1

FROM rust:1.89-slim-bookworm AS cx-builder

ARG CX_VERSION=0.6.0
ARG CX_SHA256=f7304df170d5297dd74038c9245b8726f0bb7c57d69ffc8d16c2258a92f9a7f8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        pkg-config \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/cx-build

RUN curl -fsSL "https://github.com/ind-igo/cx/archive/refs/tags/v${CX_VERSION}.tar.gz" -o cx.tar.gz \
    && echo "${CX_SHA256}  cx.tar.gz" | sha256sum -c - \
    && tar -xzf cx.tar.gz --strip-components=1 \
    && cargo install --locked --path . --root /tmp/cx-install

FROM python:3.12-slim

ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=2.1.89
ARG CODEX_VERSION=0.118.0

ENV DEBIAN_FRONTEND=noninteractive
ENV DISABLE_AUTOUPDATER=1

COPY --from=cx-builder /tmp/cx-install/bin/cx /usr/local/bin/cx
COPY scripts/container-launch.sh /usr/local/bin/dclaude-container-launch

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        coreutils \
        curl \
        fd-find \
        git \
        gnupg \
        jq \
        openssh-client \
        procps \
        ripgrep \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && pip install --no-cache-dir uv \
    && chmod +x /usr/local/bin/cx /usr/local/bin/dclaude-container-launch \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && mkdir -p /var/run/dclaude \
    && chmod 1777 /var/run/dclaude \
    && ln -s /var/run/dclaude/workspace /workspace \
    && rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*
