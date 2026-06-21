---
id: install-docker
title: Installation with Docker
sidebar_position: 4
slug: /installation/docker
---

# Installation with Docker

:::info Work in progress
This page is being written. Check back soon.
:::

Prerequisites:

- Docker Engine 24.0+
- Docker Compose v2

Quick start:

```bash
docker pull ghcr.io/solomon-labs/harness:latest
docker run -d -p 4096:4096 ghcr.io/solomon-labs/harness:latest
```

Or use Docker Compose for the full stack (Harness + PostgreSQL + Qdrant + OpenSearch).
