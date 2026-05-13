"""Orchestrator — the heartbeat of Anuvia's autonomous funnel.

This module is intentionally tiny and single-purpose:

  * A handler **registry** (`HANDLERS`) populated via the `@register("name")`
    decorator. Each handler is an async function that takes the current lead
    row (dict) and returns a small mutation payload describing what should
    happen next:
        {
            "next_action":    str | None,
            "next_action_at": datetime | None,
            "status":         str | None,
            "detail":         str,
        }
    A returned `None` for any of those three scheduling/status fields means
    "do not change that field". Returning an empty `detail` is fine.

  * A `tick(limit)` coroutine that the external cron hits every ~10 minutes.
    It pulls due leads from `lib.sessions.session_due`, dispatches by the
    `next_action` string, retries each handler up to 3 times with
    exponential backoff (2, 4 seconds), and records the outcome in
    `agent_history`. On full failure the lead is parked in
    `lifecycle_status='error'` and a Slack alert is fired.

  * A FastAPI `router` exposing:
        POST /api/orchestrator/tick   (secret-protected)
        GET  /api/orchestrator/handlers (debug)

Per the architecture spec, this module **does not import any handler
modules** — handlers register themselves on import from `app.py`. The
orchestrator only ever sees them as string keys in `HANDLERS`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException

from lib.sessions import (
    session_append_history,
    session_due,
    session_set_next,
    session_set_status,
)

log = logging.getLogger("anuvia-lp.orchestrator")


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

#: Type alias for a registered handler coroutine.
Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]

#: Module-level registry. Keys are `next_action` strings (e.g.
#: ``"classify_track"``); values are async handler functions.
HANDLERS: Dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    """Decorator that registers an async handler under ``name``.

    The decorated function must be ``async`` and accept a single positional
    arg ``lead: dict``. It must return a dict shaped as::

        {
            "next_action":    str | None,
            "next_action_at": datetime | None,
            "status":         str | None,
            "detail":         str,
        }

    Any field set to ``None`` means "leave the existing value alone".
    Missing keys are treated the same as ``None`` (defensive).

    Re-registering the same name overwrites the previous entry and emits a
    warning — useful in dev, never expected in production.
    """

    def deco(fn: Handler) -> Handler:
        if name in HANDLERS:
            log.warning("orchestrator: re-registering handler %r", name)
        HANDLERS[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# Slack alerting
# ---------------------------------------------------------------------------


async def _send_slack_alert(message: str) -> None:
    """Best-effort Slack POST. NEVER raises.

    Tries ``SLACK_ALERTS_WEBHOOK`` first, falls back to
    ``SLACK_NEW_LEAD_WEBHOOK``. If neither is configured, just logs a
    warning. Any HTTP / network error is swallowed and logged so a Slack
    outage cannot crash the tick loop.
    """
    webhook = os.environ.get("SLACK_ALERTS_WEBHOOK") or os.environ.get(
        "SLACK_NEW_LEAD_WEBHOOK"
    )
    if not webhook:
        log.warning(
            "orchestrator: no Slack webhook configured; alert dropped: %s",
            message,
        )
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(webhook, json={"text": message})
        if r.status_code >= 400:
            log.warning(
                "orchestrator: slack webhook returned %s: %s",
                r.status_code,
                r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001 — alert path must not raise
        log.warning("orchestrator: slack alert failed: %s", exc)


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------


# Retry policy constants (kept as module-level so they're easy to tweak/test).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2  # seconds; sleep = _BACKOFF_BASE ** attempt_index


async def _run_handler_with_retry(
    handler: Handler,
    lead: Dict[str, Any],
) -> tuple[Optional[Dict[str, Any]], Optional[Exception], int]:
    """Run ``handler(lead)`` with up to 3 attempts and exponential backoff.

    Returns ``(result, last_exception, latency_ms)``. On success
    ``last_exception`` is ``None`` and ``result`` is the handler's return
    dict. On failure ``result`` is ``None`` and ``last_exception`` holds the
    final exception raised. ``latency_ms`` measures the total wall-clock
    cost (including the failed attempts and their backoff sleeps for
    failure case; only the successful attempt for success case).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        start = time.monotonic()
        try:
            result = await handler(lead)
            latency_ms = int((time.monotonic() - start) * 1000)
            return result, None, latency_ms
        except Exception as exc:  # noqa: BLE001 — we genuinely want all of them
            last_exc = exc
            log.warning(
                "orchestrator: handler attempt %d/%d failed lead=%s action=%s: %s",
                attempt + 1,
                _MAX_ATTEMPTS,
                lead.get("id"),
                lead.get("next_action"),
                exc,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE ** (attempt + 1))
    # All attempts exhausted.
    return None, last_exc, 0


async def _process_lead(lead: Dict[str, Any]) -> str:
    """Process a single lead end-to-end. Returns one of:
    ``"ok"``, ``"failed"``, ``"unknown"``. Never raises — failures are
    swallowed, logged, and recorded in ``agent_history`` so one bad lead
    cannot poison the rest of the batch.
    """
    lead_id = lead.get("id")
    action = lead.get("next_action")

    if not action:
        # Defensive: session_due shouldn't return these, but be safe.
        log.warning("orchestrator: lead %s has no next_action; skipping", lead_id)
        return "unknown"

    handler = HANDLERS.get(action)
    if handler is None:
        log.warning(
            "orchestrator: no handler registered for action=%r lead=%s",
            action,
            lead_id,
        )
        try:
            await session_append_history(
                lead_id=lead_id,
                agent="orchestrator",
                action=action,
                result="unknown_handler",
                detail=f"no handler registered for {action!r}",
            )
            # Clear next_action so this lead doesn't get picked up forever.
            await session_set_next(lead_id, None, None)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "orchestrator: failed to record unknown_handler for lead=%s: %s",
                lead_id,
                exc,
            )
        return "unknown"

    log.info(
        "orchestrator: processing lead=%s action=%s", lead_id, action,
    )

    result, last_exc, latency_ms = await _run_handler_with_retry(handler, lead)

    if result is not None:
        # Success path — persist the handler's intent.
        detail = str(result.get("detail") or "")
        try:
            await session_append_history(
                lead_id=lead_id,
                agent="orchestrator",
                action=action,
                result="ok",
                detail=detail,
                latency_ms=latency_ms,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "orchestrator: history append failed lead=%s: %s", lead_id, exc,
            )

        next_action = result.get("next_action")
        next_action_at = result.get("next_action_at")
        status = result.get("status")

        # Only touch scheduling fields if the handler explicitly returned
        # something for them. `None` means "leave alone" per the contract;
        # to *clear* a field a handler must omit the key... but the spec
        # says None means "do not change". So we treat missing-key and
        # None identically: do not update.
        if "next_action" in result or "next_action_at" in result:
            if next_action is not None or next_action_at is not None:
                try:
                    await session_set_next(lead_id, next_action, next_action_at)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "orchestrator: session_set_next failed lead=%s: %s",
                        lead_id,
                        exc,
                    )

        if status is not None:
            try:
                await session_set_status(lead_id, status)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "orchestrator: session_set_status failed lead=%s: %s",
                    lead_id,
                    exc,
                )

        return "ok"

    # Failure path — 3 attempts exhausted.
    err_str = str(last_exc) if last_exc else "unknown error"
    log.error(
        "orchestrator: lead=%s action=%s failed after %d attempts: %s",
        lead_id,
        action,
        _MAX_ATTEMPTS,
        err_str,
    )

    try:
        await session_append_history(
            lead_id=lead_id,
            agent="orchestrator",
            action=action,
            result="failed",
            detail=f"failed after {_MAX_ATTEMPTS} attempts",
            error=err_str,
            latency_ms=0,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "orchestrator: history append (failed) errored lead=%s: %s",
            lead_id,
            exc,
        )

    try:
        await session_set_status(lead_id, "error")
    except Exception as exc:  # noqa: BLE001
        log.error("orchestrator: set_status('error') failed lead=%s: %s", lead_id, exc)

    try:
        await session_set_next(lead_id, None, None)
    except Exception as exc:  # noqa: BLE001
        log.error("orchestrator: clear next_action failed lead=%s: %s", lead_id, exc)

    await _send_slack_alert(
        f":rotating_light: Anuvia orchestrator: handler `{action}` failed "
        f"for lead `{lead_id}` after {_MAX_ATTEMPTS} attempts.\n"
        f"Error: ```{err_str}```"
    )
    return "failed"


async def tick(limit: int = 50) -> Dict[str, Any]:
    """Run one orchestrator tick.

    1. Pulls up to ``limit`` due leads via :func:`session_due`.
    2. Dispatches each lead's ``next_action`` to its registered handler.
    3. Retries each handler up to 3 times with 2,4-second backoff.
    4. Records every outcome in ``agent_history``; on terminal failure,
       parks the lead with ``lifecycle_status='error'`` and Slack-alerts.

    Each lead is processed independently inside its own try/except — one
    bad lead cannot abort the batch.

    Returns a summary dict::

        {
            "processed": int,
            "ok":        int,
            "failed":    int,
            "unknown":   int,
            "errors":    list[str],  # short, lead_id-tagged error strings
        }
    """
    summary: Dict[str, Any] = {
        "processed": 0,
        "ok": 0,
        "failed": 0,
        "unknown": 0,
        "errors": [],
    }

    try:
        leads = await session_due(limit)
    except Exception as exc:  # noqa: BLE001
        log.error("orchestrator: session_due failed: %s", exc)
        summary["errors"].append(f"session_due: {exc}")
        return summary

    log.info("orchestrator: tick start — %d due lead(s)", len(leads))

    for lead in leads:
        summary["processed"] += 1
        lead_id = lead.get("id")
        try:
            outcome = await _process_lead(lead)
        except Exception as exc:  # noqa: BLE001 — defense in depth
            log.exception(
                "orchestrator: unexpected error processing lead=%s", lead_id,
            )
            summary["failed"] += 1
            summary["errors"].append(f"{lead_id}: {exc}")
            continue

        if outcome == "ok":
            summary["ok"] += 1
        elif outcome == "failed":
            summary["failed"] += 1
            summary["errors"].append(
                f"{lead_id}: {lead.get('next_action')} failed"
            )
        elif outcome == "unknown":
            summary["unknown"] += 1
            summary["errors"].append(
                f"{lead_id}: unknown handler {lead.get('next_action')!r}"
            )

    log.info(
        "orchestrator: tick done — processed=%d ok=%d failed=%d unknown=%d",
        summary["processed"],
        summary["ok"],
        summary["failed"],
        summary["unknown"],
    )
    return summary


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


@router.post("/tick")
async def http_tick(secret: str, limit: int = 50) -> Dict[str, Any]:
    """Trigger one orchestrator tick.

    Called by an external cron (n8n or Coolify) every ~10 minutes. The
    ``secret`` query param must match ``ORCHESTRATOR_SECRET`` in the
    environment; otherwise we 401. ``limit`` caps how many due leads are
    processed in this tick (default 50).
    """
    expected = os.environ.get("ORCHESTRATOR_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    return await tick(limit)


@router.get("/handlers")
async def http_handlers() -> Dict[str, Any]:
    """List currently registered handler names. No auth — debug aid only."""
    return {"registered": list(HANDLERS.keys())}
