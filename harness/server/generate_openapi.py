"""Generate static OpenAPI spec for docs-site.

Usage:
    cd C:/MyAI/06_Harness/harness/server
    python generate_openapi.py

Output: openapi.json in the same directory.
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure the harness package parent is importable
_HERE = Path(__file__).resolve().parent
_HARNESS_ROOT = _HERE.parent.parent  # C:\MyAI\06_Harness
sys.path.insert(0, str(_HARNESS_ROOT))


@asynccontextmanager
async def _noop_lifespan(_app):
    """No-op lifespan — skip DB init, JobStore, LLMRouter, etc."""
    yield


# Monkey-patch the module-level lifespan BEFORE create_app() is called.
# create_app() references ``lifespan`` as a module-global, so replacing it
# here means create_app() will pick up the no-op version.
import harness.server.app as _app_module  # noqa: E402

_app_module.lifespan = _noop_lifespan

# Now it is safe to call create_app — no heavy init will run.
from harness.server.app import create_app  # noqa: E402

app = create_app()

# Extract OpenAPI spec (FastAPI generates it on-demand from the registered routes)
spec = app.openapi()

# Write to file
out = _HERE / "openapi.json"
out.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")

path_count = len(spec.get("paths", {}))
print(f"[generate_openapi] Written {out} ({path_count} paths)")
