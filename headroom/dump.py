"""Per-request dump of original vs. compressed payloads.

Saves, for every request that flows through the compression pipeline, the
exact messages sent to the model *before* and *after* Headroom compression so
they can be compared offline (e.g. to audit what compression dropped).

Layout (one set of files per request)::

    <dump_dir>/
        20260625-093012-123456-<rid>.original.json    # pre-compression body
        20260625-093012-123456-<rid>.compressed.json  # post-compression body
        20260625-093012-123456-<rid>.meta.json        # token counts + ratio

Activation
----------
On by default, writing to ``~/.headroom/dumps``. Override the directory with
``HEADROOM_DUMP_DIR``; disable entirely with a truthy ``HEADROOM_DUMP_DISABLE``
(``1``/``true``/``yes``/``on``).

This module is best-effort: it must never raise into the compression path. Any
failure (disk full, permission, serialization) is swallowed and logged at debug
level so a dump problem can never break or slow a real request beyond the write.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# Resolved once on first use: (enabled, dump_dir). Cached because env vars are
# fixed for the process lifetime and this runs on the hot compression path.
_lock = threading.Lock()
_resolved: tuple[bool, Path | None] | None = None


def _resolve() -> tuple[bool, Path | None]:
    """Resolve (enabled, dump_dir) from the environment, once per process."""
    global _resolved
    if _resolved is not None:
        return _resolved
    with _lock:
        if _resolved is not None:  # double-checked under lock
            return _resolved
        disabled = os.environ.get("HEADROOM_DUMP_DISABLE", "").strip().lower() in _TRUTHY
        if disabled:
            _resolved = (False, None)
            return _resolved
        raw_dir = os.environ.get("HEADROOM_DUMP_DIR", "").strip()
        dump_dir = Path(raw_dir).expanduser() if raw_dir else Path.home() / ".headroom" / "dumps"
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("dump: cannot create dump dir %s; disabling", dump_dir, exc_info=True)
            _resolved = (False, None)
            return _resolved
        logger.info("dump: saving original/compressed request pairs to %s", dump_dir)
        _resolved = (True, dump_dir)
        return _resolved


def is_enabled() -> bool:
    """True if request dumping is active for this process."""
    return _resolve()[0]


def _byte_len(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return -1


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )


def dump_request(
    *,
    original_messages: list[dict[str, Any]],
    compressed_messages: list[dict[str, Any]],
    model: str,
    tokens_before: int,
    tokens_after: int,
    transforms_applied: list[str] | None = None,
    request_id: str = "",
    provider: str | None = None,
) -> Path | None:
    """Write the original + compressed payloads and a metadata sidecar.

    Returns the shared path stem the three files were written under, or ``None``
    if dumping is disabled or failed. Never raises.
    """
    enabled, dump_dir = _resolve()
    if not enabled or dump_dir is None:
        return None

    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        rid = (request_id or uuid.uuid4().hex[:8]).replace("/", "_").replace(os.sep, "_")
        stem = dump_dir / f"{ts}-{rid}"

        orig_bytes = _byte_len(original_messages)
        comp_bytes = _byte_len(compressed_messages)
        saved = tokens_before - tokens_after
        ratio = (saved / tokens_before) if tokens_before > 0 else 0.0

        _write_json(
            stem.with_suffix(".original.json"),
            {"model": model, "provider": provider, "messages": original_messages},
        )
        _write_json(
            stem.with_suffix(".compressed.json"),
            {"model": model, "provider": provider, "messages": compressed_messages},
        )
        _write_json(
            stem.with_suffix(".meta.json"),
            {
                "timestamp": ts,
                "request_id": request_id,
                "model": model,
                "provider": provider,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "tokens_saved": saved,
                "compression_ratio": round(ratio, 4),
                "original_bytes": orig_bytes,
                "compressed_bytes": comp_bytes,
                "original_message_count": len(original_messages),
                "compressed_message_count": len(compressed_messages),
                "transforms_applied": transforms_applied or [],
            },
        )
        return stem
    except Exception:
        logger.debug("dump: failed to write request dump", exc_info=True)
        return None
