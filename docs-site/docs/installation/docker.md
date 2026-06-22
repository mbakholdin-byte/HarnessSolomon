---
id: install-docker
title: Installation with Docker
sidebar_position: 4
slug: /installation/docker
description: Run Solomon Harness in a container with Docker Engine 24+ and Docker Compose v2 — quick start, persistent volumes, Compose stack with Qdrant, environment variables, and production hardening.
---

# Installation with Docker

Solomon Harness ships a multi-arch container image at
`ghcr.io/solomon-labs/harness`. This is the fastest way to get a working
agent shell without installing Python 3.12, Rust toolchain, or system
dependencies locally.

## Prerequisites

| Component | Minimum version | Verify |
|-----------|-----------------|--------|
| Docker Engine | 24.0+ | `docker --version` |
| Docker Compose | v2 (plugin) | `docker compose version` |
| Disk | 2 GB (image + SQLite data) | — |
| RAM | 1 GB free (4 GB if running local LLM via Ollama sidecar) | — |

No Python, pip, or Rust installation is required on the host. The image
bundles the `harness` CLI, the FastAPI server, and all optional native
dependencies (compiled PyO3 extensions included).

## Pull the image

```bash
docker pull ghcr.io/solomon-labs/harness:latest
```

Pin a specific release for reproducible deployments:

```bash
docker pull ghcr.io/solomon-labs/harness:1.3.2
```

Available tags: `latest` (tracking `main`), semver (`1.3.2`, `1.3`, `1`),
and short-SHA tags for each CI build.

## Quick start

The container exposes the Harness server on port **8765** (the default
in `settings.port`; chosen because Windows 11 + Docker Desktop reserves
8000/8001 via the HNS service).

```bash
docker run -d \
  --name harness \
  -p 8765:8765 \
  ghcr.io/solomon-labs/harness:latest
```

Verify the server is up:

```bash
curl http://localhost:8765/health/live
# {"status":"alive"}
```

The CLI is also available inside the container:

```bash
docker exec -it harness harness agents list
docker exec -it harness harness plugins install <name>
```

:::note
The `harness init` wizard and `harness run` one-shot commands are
**interactive** and require a TTY. For non-interactive container use,
prefer the FastAPI server (`harness serve`, which is the default entry
point) or pass `--prompt` to `harness agents run`.
:::

## Persistent configuration

By default, container data is ephemeral: sessions, the SQLite index, and
the `~/.harness` operator directory are lost when the container is
removed. Mount a named volume to persist them across restarts.

Harness uses two storage locations inside the container:

| Path | Contents |
|------|----------|
| `/root/.harness` | Operator-level config (follow-state, auth tokens) |
| `/app/data` | SQLite database (`harness.db`), session JSONL mirrors |

```bash
docker run -d \
  --name harness \
  -p 8765:8765 \
  -v harness-home:/root/.harness \
  -v harness-data:/app/data \
  ghcr.io/solomon-labs/harness:latest
```

Create the volumes once beforehand (optional, Docker auto-creates on
first use):

```bash
docker volume create harness-home
docker volume create harness-data
```

To mount a host directory instead of a named volume (useful for
inspecting files or sharing config across containers):

```bash
docker run -d \
  --name harness \
  -p 8765:8765 \
  -v "$HOME/.harness:/root/.harness" \
  -v "$PWD/harness-data:/app/data" \
  ghcr.io/solomon-labs/harness:latest
```

:::warning
On Linux hosts, bind-mounted directories must be writable by UID 0
(container runs as root). If you encounter permission errors, run
`chmod -R 777 ./harness-data` on the host directory, or build a custom
image that runs as a non-root user.
:::

## Full stack with Docker Compose

For a complete setup with **Qdrant** (enables L2 vector memory and
hybrid dense+BM25 retrieval), use the Compose file below. Qdrant is
optional — without it, Harness falls back to in-SQLite vector storage
(`SqliteL2Store`) and works fully offline.

```yaml
# docker-compose.yml
services:
  harness:
    image: ghcr.io/solomon-labs/harness:1.3.2
    ports:
      - "8765:8765"
    environment:
      - SCRATCHPAD_L2_QDRANT_URL=http://qdrant:6333
      - SCRATCHPAD_L2_QDRANT_COLLECTION=scratchpad_l2
      - LOG_LEVEL=INFO
      # LLM provider keys (set at least one):
      # - MINIMAX_API_KEY=...
      # - ZHIPUAI_API_KEY=...
      # - MOONSHOT_API_KEY=...
    volumes:
      - harness-home:/root/.harness
      - harness-data:/app/data
    depends_on:
      qdrant:
        condition: service_healthy
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:v1.13.2
    ports:
      - "6333:6333"   # REST + gRPC
      - "6334:6334"   # gRPC (optional)
    volumes:
      - qdrant-storage:/qdrant/storage
    healthcheck:
      test: ["CMD-SHELL", "bash -c ':> /dev/tcp/127.0.0.1/6333 || exit 1'"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 5s
    restart: unless-stopped

volumes:
  harness-home:
  harness-data:
  qdrant-storage:
```

Launch the stack:

```bash
docker compose up -d
docker compose logs -f harness   # follow server startup
```

Both services now run with persistent volumes and automatic restart on
host reboot. The Qdrant health-check ensures Harness waits until the
vector store is ready before binding the agent loop.

## Environment variables

Harness reads configuration from environment variables (field names are
case-insensitive; no prefix required). The most common ones for Docker
deployments:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address. Keep `0.0.0.0` in containers. |
| `PORT` | `8765` | Server port. |
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `PROJECT_ROOT` | `/app` | Root directory for file tools (resolve paths under this). |
| `MAX_ITERATIONS` | `5` | Safety cap on agent loop iterations per task (1-20). |
| `SCRATCHPAD_L2_QDRANT_URL` | _(unset)_ | Qdrant URL for L2 vector memory. Falls back to SQLite when unset. |
| `SCRATCHPAD_L2_QDRANT_COLLECTION` | `scratchpad_l2` | Qdrant collection name. |
| `MINIMAX_API_KEY` | _(empty)_ | MiniMax (Cloud) API key. |
| `ZHIPUAI_API_KEY` | _(empty)_ | ZhipuAI / GLM (Cloud) API key. |
| `MOONSHOT_API_KEY` | _(empty)_ | Moonshot / Kimi (Cloud) API key. |

Pass variables via `-e` flags or the `environment:` block in Compose:

```bash
docker run -d \
  -p 8765:8765 \
  -e LOG_LEVEL=DEBUG \
  -e ZHIPUAI_API_KEY=sk-... \
  -e SCRATCHPAD_L2_QDRANT_URL=http://host.docker.internal:6333 \
  ghcr.io/solomon-labs/harness:latest
```

For the full list of 100+ settings (CORS, agent tier models, privacy
zones, hook paths), see the `Settings` class in `harness/config.py` or
run `harness config show` inside the container.

## Production notes

### Health check

Harness exposes three health endpoints. Add a Docker health-check so the
orchestrator knows when the server is ready:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://localhost:8765/health/live || exit 1
```

For deeper checks (database, Qdrant, Neo4j probes), use `/health/deep`
instead — it returns per-subsystem status and is useful for readiness
gates in Kubernetes.

### Restart policy

Use `restart: unless-stopped` (Compose) or `--restart unless-stopped`
(standalone `docker run`) so the container survives host reboots without
a separate process supervisor.

### Resource limits

A single Harness instance with a cloud LLM provider typically needs
under 512 MB RAM. With a local Ollama sidecar, budget 4-8 GB for the
LLM. Example Compose limits:

```yaml
services:
  harness:
    # ... (image, ports, etc.)
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1G
        reservations:
          memory: 256M
```

### Secrets

Never bake API keys into the image. Use Docker secrets, a `.env` file
referenced via `env_file:` in Compose, or an external secret manager
(Vault, AWS Secrets Manager). The image reads keys only from environment
variables at runtime.

## Next steps

- [Linux installation](./linux.md) — bare-metal pip install without Docker
- [Configuration reference](/configuration/config-overview) — all settings
- [Getting Started](/tutorials/quickstart) — first agent walkthrough
- [API Reference](/api/) — REST endpoints and authentication
