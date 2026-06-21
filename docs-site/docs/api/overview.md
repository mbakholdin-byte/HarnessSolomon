---
sidebar_position: 1
title: API Reference
description: Harness REST API documentation
custom_edit_url: null
---

# API Reference

Auto-generated from the Harness FastAPI application (OpenAPI 3.1).

## Base URL

```
http://localhost:8080/api/v1
```

The default port is `8080` (see [Configuration](/configuration/reference#port) for details).

## Versioning

- **`/api/v1/*`** — canonical endpoints (recommended for all new clients)
- **`/api/*`** — legacy paths, returning `Deprecation: true` and `Sunset: Wed, 31 Dec 2026 23:59:59 GMT` headers

After the sunset date, legacy paths will return `410 Gone`. See the [Migration Guide](/migration/v1.32-to-v1.40) for details.

## Authentication

Harness supports token-based API authentication via the `Authorization: Bearer <token>` header. See [Configuration Reference](/configuration/reference#auth_required) for setup instructions.

Unauthenticated access is supported when `auth_required=False` (development mode).

## Scopes

API endpoints are scope-gated. Common scopes:

| Scope | Description |
|-------|-------------|
| `agents.read` | Read agent jobs and merge queue |
| `agents.write` | Create/cancel agent jobs |
| `memory.read` | Read memory entries |
| `memory.write` | Write memory entries |
| `sessions.read` | Read session data |
| `observability.read` | Access metrics, health, audit logs |
| `hooks.admin` | Manage hooks configuration |
| `plugins.admin` | Manage plugins |
| `privacy.read` | Read privacy zones |
| `privacy.write` | Manage privacy zones |

## OpenAPI Specification

The full specification is available as:

- **[openapi.json](pathname:///../harness/server/openapi.json)** — raw OpenAPI 3.1 schema (43 endpoints)

## Endpoints

The following pages are auto-generated from the OpenAPI specification:
