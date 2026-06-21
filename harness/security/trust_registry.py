"""Phase 7.4 WI-05: Trust Registry — ed25519 public key management with hot-reload.

Manages a JSON file of trusted plugin publisher keys. Supports:
- Load / validate trust-registry.json
- Add / remove / get / list keys (in-memory + persist)
- Asyncio polling-based hot-reload (no watchdog dependency)
- Ed25519 signature verification via ``harness.plugins.signature``
  (Rust fast path + Python fallback). Lazy import — see
  :meth:`TrustRegistry.verify_signature`.

Trust boundary: top-level imports are ONLY stdlib + pathlib + json +
logging + asyncio. No imports from ``harness.agents``, ``harness.server``,
or any harness subpackage. The single permitted cross-package call is a
lazy ``from harness.plugins.signature import ...`` inside
:meth:`verify_signature` — ``signature.py`` is a leaf module (no harness
imports of its own), so this does not open a cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "TrustRegistry",
    "TrustRegistryValidationError",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON schema (in-code, no external dependency)
# ---------------------------------------------------------------------------

REQUIRED_TOP_KEYS = frozenset({"version", "public_keys"})
REQUIRED_KEY_KEYS = frozenset({"name", "public_key"})
VALID_KEY_KEYS = frozenset({"name", "public_key", "added_at", "notes"})
PUBLIC_KEY_PREFIX = "ed25519:"


class TrustRegistryValidationError(ValueError):
    """Raised when the trust registry JSON fails schema validation."""


# ---------------------------------------------------------------------------
# TrustRegistry
# ---------------------------------------------------------------------------


class TrustRegistry:
    """Manages trusted ed25519 public keys for plugin signature verification.

    Keys are stored in-memory (``dict[name, public_key_hex]``) and
    persisted to a JSON file on every mutation (add/remove). The file
    is polled for external changes via :meth:`check_hot_reload` or a
    background watcher started by :meth:`start_watcher`.

    Hot-reload uses ``os.stat(path).st_mtime`` — simple, cross-platform,
    zero-dependency. Watchdog is deliberately not used (extra dependency,
    polling is more reliable on Windows for network drives and Docker
    volumes).
    """

    def __init__(self, path: Path | None = None):
        """Initialise the registry.

        Args:
            path: Path to ``trust-registry.json``. If ``None``, no file
                I/O is performed (pure in-memory mode — useful for tests).
                The caller should call :meth:`load` after setting the path
                or use :meth:`add_key` to populate in-memory entries.
        """
        self._path: Path | None = path
        self._keys: dict[str, str] = {}  # name -> public_key (hex, "ed25519:...")
        self._last_mtime: float = 0.0
        self._hot_reload_enabled: bool = True
        self._poll_interval: int = 5  # seconds

        # Watcher state
        self._watcher_task: asyncio.Task[Any] | None = None
        self._watcher_stop: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Load / validate
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load and validate the trust registry from the JSON file.

        Raises:
            FileNotFoundError: If ``self._path`` is set but the file does not exist.
            TrustRegistryValidationError: If the JSON schema is invalid.
            json.JSONDecodeError: If the file is not valid JSON.

        If ``self._path`` is ``None``, this is a no-op.
        """
        if self._path is None:
            return

        if not self._path.exists():
            raise FileNotFoundError(f"Trust registry not found: {self._path}")

        raw = self._path.read_text(encoding="utf-8")
        data = json.loads(raw)

        # Validate top-level
        self._validate_schema(data)

        # Extract keys
        new_keys: dict[str, str] = {}
        for entry in data["public_keys"]:
            name: str = entry["name"]
            public_key: str = entry["public_key"]
            new_keys[name] = public_key

        self._keys = new_keys
        self._last_mtime = self._path.stat().st_mtime
        _log.info(
            "Trust registry loaded: %d key(s) from %s",
            len(self._keys),
            self._path,
        )

    def _validate_schema(self, data: Any) -> None:
        """Validate the JSON structure against the trust registry schema.

        Raises:
            TrustRegistryValidationError: On any schema violation.
        """
        if not isinstance(data, dict):
            raise TrustRegistryValidationError(
                "Trust registry must be a JSON object"
            )

        # Top-level keys
        actual_keys = frozenset(data.keys())
        missing = REQUIRED_TOP_KEYS - actual_keys
        if missing:
            raise TrustRegistryValidationError(
                f"Missing required top-level keys: {', '.join(sorted(missing))}"
            )
        unknown = actual_keys - REQUIRED_TOP_KEYS
        if unknown:
            raise TrustRegistryValidationError(
                f"Unknown top-level keys: {', '.join(sorted(unknown))}"
            )

        # version
        if data["version"] != "1":
            raise TrustRegistryValidationError(
                f"Unsupported trust registry version: {data['version']!r} "
                f"(expected '1')"
            )

        # public_keys
        public_keys = data["public_keys"]
        if not isinstance(public_keys, list):
            raise TrustRegistryValidationError(
                "'public_keys' must be a JSON array"
            )
        if not public_keys:
            raise TrustRegistryValidationError(
                "'public_keys' must be a non-empty list"
            )

        seen_names: set[str] = set()
        for i, entry in enumerate(public_keys):
            if not isinstance(entry, dict):
                raise TrustRegistryValidationError(
                    f"public_keys[{i}] must be a JSON object"
                )

            # Required keys
            ek = frozenset(entry.keys())
            missing_k = REQUIRED_KEY_KEYS - ek
            if missing_k:
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: missing required keys: "
                    f"{', '.join(sorted(missing_k))}"
                )

            # Unknown keys
            unknown_k = ek - VALID_KEY_KEYS
            if unknown_k:
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: unknown keys: "
                    f"{', '.join(sorted(unknown_k))}"
                )

            # name must be non-empty string
            name = entry["name"]
            if not isinstance(name, str) or not name.strip():
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: 'name' must be a non-empty string"
                )

            # public_key must start with "ed25519:"
            public_key = entry["public_key"]
            if not isinstance(public_key, str):
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: 'public_key' must be a string"
                )
            if not public_key.startswith(PUBLIC_KEY_PREFIX):
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: 'public_key' must start with "
                    f"'{PUBLIC_KEY_PREFIX}', got {public_key[:40]!r}..."
                )

            # Duplicate name
            if name in seen_names:
                raise TrustRegistryValidationError(
                    f"public_keys[{i}]: duplicate name {name!r}"
                )
            seen_names.add(name)

    # ------------------------------------------------------------------
    # Mutation (in-memory + persist)
    # ------------------------------------------------------------------

    def add_key(self, name: str, public_key: str) -> None:
        """Add a trusted public key (in-memory + persist to file).

        Args:
            name: Human-readable key name (e.g. ``"official-harness-team"``).
            public_key: Hex-encoded ed25519 public key, prefixed with
                ``"ed25519:"``.

        Raises:
            ValueError: If ``public_key`` does not start with ``"ed25519:"``.
            OSError: If file persistence fails.
        """
        if not isinstance(public_key, str) or not public_key.startswith(PUBLIC_KEY_PREFIX):
            raise ValueError(
                f"public_key must start with '{PUBLIC_KEY_PREFIX}'"
            )
        if not name or not name.strip():
            raise ValueError("name must be a non-empty string")

        self._keys[name] = public_key
        _log.info("Trust registry: added key %r", name)

        if self._path is not None:
            self._persist()

    def remove_key(self, name: str) -> bool:
        """Remove a trusted key by name. Returns ``True`` if found.

        Does NOT raise if the name is not found — returns ``False``.
        """
        if name in self._keys:
            del self._keys[name]
            _log.info("Trust registry: removed key %r", name)
            if self._path is not None:
                self._persist()
            return True
        return False

    def get_key(self, name: str) -> str | None:
        """Get public key by name. Returns ``None`` if not found."""
        return self._keys.get(name)

    def list_keys(self) -> list[dict[str, str | None]]:
        """List all trusted keys with metadata.

        Returns a list of dicts with keys: ``name``, ``public_key``.
        ``added_at`` and ``notes`` are ``None`` (the in-memory registry
        does not track them — they exist only in the JSON file).
        """
        return [
            {
                "name": name,
                "public_key": pk,
                "added_at": None,
                "notes": None,
            }
            for name, pk in self._keys.items()
        ]

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        public_key_hex: str,
        signature_hex: str,
        data: bytes,
    ) -> bool:
        """Verify an Ed25519 signature against a trusted public key.

        Combines two checks:

        1. **Trust check** — ``public_key_hex`` must be registered in
           this ``TrustRegistry`` (exact match against one of the
           ``"ed25519:..."`` values loaded from the JSON file).
        2. **Crypto check** — the Ed25519 signature must verify against
           ``data`` under the given public key, via
           :func:`harness.plugins.signature.verify_signature` (Rust fast
           path with a pure-Python fallback).

        Args:
            public_key_hex: Public key in registry format — a hex string
                prefixed with ``"ed25519:"`` (e.g.
                ``"ed25519:9a3f..."``). The prefix is stripped before
                decoding.
            signature_hex: Signature as a hex string (128 hex chars = 64
                bytes, RFC 8032).
            data: The signed payload (``bytes``).

        Returns:
            ``True`` if and only if both checks pass — the key is trusted
            AND the signature is valid. ``False`` on any failure:
            untrusted key, bad signature, malformed hex, wrong types.
            This method never raises.

        Notes:
            * The import of ``harness.plugins.signature`` is lazy (inside
              the method body) so that ``trust_registry.py`` keeps no
              top-level dependency on the ``harness.plugins`` package —
              ``signature`` is a leaf module that itself only touches
              ``bytes`` and ``cryptography``/``harness_perf``, so the
              trust boundary is preserved.
        """
        # Step 1: trust check — exact match against registered keys.
        if not any(pk == public_key_hex for pk in self._keys.values()):
            _log.debug(
                "Trust registry: key %s not trusted (%d keys loaded)",
                public_key_hex[:48] if isinstance(public_key_hex, str) else "?",
                len(self._keys),
            )
            return False

        # Step 2: decode hex. ``ValueError`` from ``bytes.fromhex`` covers
        # odd-length / non-hex characters; ``TypeError`` covers non-str
        # inputs (e.g. ``None``). Both are swallowed → ``False``.
        try:
            pk_hex = public_key_hex.removeprefix(PUBLIC_KEY_PREFIX)
            pk_bytes = bytes.fromhex(pk_hex)
            sig_bytes = bytes.fromhex(signature_hex)
        except (ValueError, TypeError):
            _log.debug(
                "Trust registry: malformed hex inputs (pk=%r, sig=%r)",
                public_key_hex[:48] if isinstance(public_key_hex, str) else None,
                signature_hex[:48] if isinstance(signature_hex, str) else None,
            )
            return False

        # Step 3: crypto verify via the signature module (Rust or Python).
        # Lazy import — see method docstring for the trust-boundary rationale.
        from harness.plugins.signature import verify_signature as _verify

        return _verify(pk_bytes, data, sig_bytes)

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    def check_hot_reload(self) -> bool:
        """Check if the file changed on disk and reload. Returns ``True`` if reloaded.

        Compares ``os.stat(path).st_mtime`` against the cached value from
        the last load. If the mtime has changed (or the file was just
        created), calls :meth:`load`.

        Returns ``False`` (no-op) when:
        - ``self._path`` is ``None``
        - ``self._hot_reload_enabled`` is ``False``
        - The file does not exist (no error — caller treats as "not yet created")
        - The mtime has not changed since last load
        """
        if self._path is None or not self._hot_reload_enabled:
            return False
        if not self._path.exists():
            return False

        try:
            current_mtime = self._path.stat().st_mtime
        except OSError as exc:
            _log.debug("Trust registry: stat failed for %s: %s", self._path, exc)
            return False

        if current_mtime == self._last_mtime:
            return False

        _log.info(
            "Trust registry: mtime changed for %s — reloading",
            self._path,
        )
        try:
            self.load()
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open on hot-reload
            _log.warning(
                "Trust registry: hot-reload failed for %s: %s",
                self._path,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Background watcher (asyncio polling)
    # ------------------------------------------------------------------

    async def _polling_loop(self, stop_event: asyncio.Event) -> None:
        """Background polling loop for hot-reload.

        Sleeps for ``self._poll_interval`` seconds between checks.
        Exits when ``stop_event`` is set.
        """
        _log.debug(
            "Trust registry watcher started (interval=%ds, path=%s)",
            self._poll_interval,
            self._path,
        )
        while not stop_event.is_set():
            try:
                self.check_hot_reload()
            except Exception as exc:  # noqa: BLE001 — never crash the watcher
                _log.debug(
                    "Trust registry watcher: check_hot_reload error: %s",
                    exc,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed, loop again
        _log.debug("Trust registry watcher stopped")

    def start_watcher(self) -> None:
        """Start background file watcher (polling-based, no watchdog dependency).

        Creates an ``asyncio.Task`` that runs a polling loop. The task
        checks ``os.stat(path).st_mtime`` every ``self._poll_interval``
        seconds and reloads on change.

        Safe to call multiple times — subsequent calls are no-ops if the
        watcher is already running.

        Requires a running asyncio event loop (``asyncio.get_running_loop()``
        must succeed). In a synchronous context, call :meth:`check_hot_reload`
        periodically instead.
        """
        if self._watcher_task is not None and not self._watcher_task.done():
            return  # Already running

        if self._path is None:
            _log.debug("Trust registry: no path set — skipping watcher")
            return

        self._watcher_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._watcher_task = loop.create_task(
            self._polling_loop(self._watcher_stop)
        )
        _log.info(
            "Trust registry watcher started for %s (poll=%ds)",
            self._path,
            self._poll_interval,
        )

    def stop_watcher(self) -> None:
        """Stop the background watcher.

        Cancels the asyncio task and waits for it to finish gracefully.
        Safe to call when no watcher is running (no-op).
        """
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = None
            self._watcher_stop = None
            return

        if self._watcher_stop is not None:
            self._watcher_stop.set()

        self._watcher_task.cancel()
        self._watcher_task = None
        self._watcher_stop = None
        _log.info("Trust registry watcher stopped")

    # ------------------------------------------------------------------
    # Persistence helper
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write the current in-memory keys to the JSON file.

        Preserves the ``version`` field. Overwrites the file atomically
        (write to temp + rename) where possible.
        """
        if self._path is None:
            return

        public_keys: list[dict[str, str]] = []
        for name, pk in self._keys.items():
            public_keys.append({
                "name": name,
                "public_key": pk,
            })

        data: dict[str, Any] = {
            "version": "1",
            "public_keys": public_keys,
        }

        # Atomic write: write to temp file, then rename.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self._path)
            self._last_mtime = self._path.stat().st_mtime
        except Exception:
            # Clean up temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
