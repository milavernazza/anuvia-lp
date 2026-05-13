"""
Lead session helpers — shared agent memory layer.

All agents (orchestrator, track_b, follow-up, proposal) read and write the
same `leads` row through these helpers. Every function talks to Supabase via
the PostgREST endpoint using `httpx`, mirroring the pattern in `app.py`.

Design rules (from ARCHITECTURE_AUTONOMOUS_v1.md §5):
  * Append-only jsonb columns (`agent_history`, `artifacts`, `signals`) are
    mutated through read-modify-write — NEVER via the Postgres `||` operator.
  * Helpers tolerate concurrent writers via a single retry (re-fetch, re-append,
    re-PATCH) when the round trip suggests another writer raced us.
  * Idempotency: helpers only mutate fields they are given; missing fields are
    left untouched.

This module exposes both library functions and a FastAPI `router` at
`/api/session/{lead_id}` for admin inspection.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

log = logging.getLogger("anuvia-lp")

# ---------------------------------------------------------------------------
# Supabase REST config — same pattern as app.py
# ---------------------------------------------------------------------------

SUPA_URL: str = os.environ.get(
    "SUPABASE_URL", "https://api.anuvia.com.br/rest/v1"
).rstrip("/")
SUPA_KEY: str = os.environ.get("SUPABASE_KEY", "")

SUPA_HEADERS: dict[str, str] = {
    "apikey": SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# How long a single Supabase round-trip is allowed.
_HTTP_TIMEOUT: float = 15.0

# Jsonb append columns we manage with read-modify-write.
_APPEND_COLUMNS: tuple[str, ...] = ("agent_history", "artifacts", "signals")


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z'-style suffix."""
    return datetime.now(timezone.utc).isoformat()


def _serialize(value: Any) -> Any:
    """Convert datetimes to ISO strings recursively so they survive JSON."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


async def session_get(lead_id: str) -> Optional[dict]:
    """Fetch the full lead row by id. Returns None if no row exists."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(
            f"{SUPA_URL}/leads?id=eq.{lead_id}&limit=1",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        log.warning("session_get non-200: %s %s", r.status_code, r.text[:200])
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def session_create_or_get(email: str, **fields: Any) -> dict:
    """Return the existing lead row for `email`, or insert a fresh one.

    Extra `fields` (e.g. `name`, `funnel_id`, `language`, `market`) seed the
    insert when a row does not yet exist. Idempotent on email.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        # Try fetch first.
        gr = await client.get(
            f"{SUPA_URL}/leads?email=eq.{email}&limit=1",
            headers=SUPA_HEADERS,
        )
        if gr.status_code == 200:
            existing = gr.json() or []
            if existing:
                return existing[0]

        payload: dict[str, Any] = {"email": email}
        payload.update(_serialize(fields))
        # Sensible defaults for the new lifecycle columns when present.
        payload.setdefault("lifecycle_status", "new")

        ir = await client.post(
            f"{SUPA_URL}/leads",
            headers=SUPA_HEADERS,
            json=payload,
        )
        if ir.status_code in (200, 201):
            body = ir.json()
            if isinstance(body, list) and body:
                return body[0]
            if isinstance(body, dict):
                return body

        # Race: another writer inserted between GET and POST → re-fetch.
        gr2 = await client.get(
            f"{SUPA_URL}/leads?email=eq.{email}&limit=1",
            headers=SUPA_HEADERS,
        )
        if gr2.status_code == 200:
            rows = gr2.json() or []
            if rows:
                return rows[0]

    raise RuntimeError(
        f"session_create_or_get failed for {email}: insert {ir.status_code} "
        f"{ir.text[:200]}"
    )


async def session_update(lead_id: str, **fields: Any) -> dict:
    """PATCH arbitrary scalar columns on a lead. Returns the updated row.

    Refuses to clobber the append-only jsonb columns — those must go through
    the dedicated `session_append_*` helpers. Always stamps `last_touch_at`.
    """
    for col in _APPEND_COLUMNS:
        if col in fields:
            raise ValueError(
                f"Use session_append_{col[:-1]} to mutate `{col}` "
                "(append-only column)."
            )

    payload: dict[str, Any] = _serialize(fields)
    payload.setdefault("last_touch_at", _now_iso())

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code not in (200, 204):
        log.warning(
            "session_update non-200 lead=%s: %s %s",
            lead_id, r.status_code, r.text[:200],
        )
        raise RuntimeError(
            f"session_update failed: {r.status_code} {r.text[:200]}"
        )
    body = r.json() if r.status_code == 200 and r.text else []
    if isinstance(body, list) and body:
        return body[0]
    # Some PATCH responses come back empty — re-fetch to be safe.
    fresh = await session_get(lead_id)
    return fresh or {}


# ---------------------------------------------------------------------------
# Append-only jsonb helpers (read-modify-write with one retry on race)
# ---------------------------------------------------------------------------


async def _append_to_jsonb_array(
    lead_id: str,
    column: str,
    entry: dict,
) -> None:
    """Append `entry` to `leads.<column>` (a jsonb array) via read-modify-write.

    Retries once if the row appears to have been mutated between read and
    write. Never raises on concurrent races we successfully resolved.
    """
    if column not in _APPEND_COLUMNS:
        raise ValueError(f"_append_to_jsonb_array: unknown column {column!r}")

    last_err: Optional[str] = None
    for attempt in (1, 2):
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            gr = await client.get(
                f"{SUPA_URL}/leads?id=eq.{lead_id}&select={column}",
                headers=SUPA_HEADERS,
            )
            if gr.status_code != 200:
                last_err = f"GET {gr.status_code} {gr.text[:200]}"
                await asyncio.sleep(0.15)
                continue
            rows = gr.json() or []
            if not rows:
                log.warning(
                    "_append_to_jsonb_array: lead %s not found (column=%s)",
                    lead_id, column,
                )
                return
            current = rows[0].get(column) or []
            if not isinstance(current, list):
                current = []
            new_value = list(current) + [entry]

            # Read-modify-write: we then re-read the column and verify the
            # length grew by exactly 1. If not, another writer raced us; retry
            # once and on the second attempt recover by re-appending onto the
            # then-current value.
            pr = await client.patch(
                f"{SUPA_URL}/leads?id=eq.{lead_id}",
                headers=SUPA_HEADERS,
                json={column: new_value, "last_touch_at": _now_iso()},
            )
            if pr.status_code not in (200, 204):
                last_err = f"PATCH {pr.status_code} {pr.text[:200]}"
                await asyncio.sleep(0.15)
                continue

            # Verify nobody else snuck in a write that we just overwrote.
            # If the array length didn't grow by exactly 1, someone else wrote
            # between our GET and PATCH — re-run.
            vr = await client.get(
                f"{SUPA_URL}/leads?id=eq.{lead_id}&select={column}",
                headers=SUPA_HEADERS,
            )
            if vr.status_code == 200:
                vrows = vr.json() or []
                if vrows:
                    final = vrows[0].get(column) or []
                    if isinstance(final, list) and len(final) == len(current) + 1:
                        return  # success
                    # Length mismatch — concurrent writer clobbered us; retry.
                    last_err = (
                        f"race detected: expected len {len(current) + 1}, "
                        f"got {len(final) if isinstance(final, list) else 'n/a'}"
                    )
                    if attempt == 2:
                        # On second attempt, do a final re-append to recover
                        # the entry we may have just lost.
                        recover = list(final) + [entry] if isinstance(final, list) else [entry]
                        rr = await client.patch(
                            f"{SUPA_URL}/leads?id=eq.{lead_id}",
                            headers=SUPA_HEADERS,
                            json={column: recover, "last_touch_at": _now_iso()},
                        )
                        if rr.status_code in (200, 204):
                            return
                        last_err = f"recover PATCH {rr.status_code} {rr.text[:200]}"
                    continue
            return  # PATCH succeeded; verification skipped (best effort)

    log.warning(
        "_append_to_jsonb_array failed lead=%s col=%s: %s",
        lead_id, column, last_err,
    )


async def session_append_history(
    lead_id: str,
    agent: str,
    action: str,
    result: str,
    detail: str = "",
    error: Optional[str] = None,
    latency_ms: int = 0,
) -> None:
    """Append one entry to `agent_history`. Append-only, race-tolerant."""
    entry = {
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "result": result,
        "detail": detail,
        "error": error,
        "latency_ms": int(latency_ms),
    }
    await _append_to_jsonb_array(lead_id, "agent_history", entry)


async def session_append_artifact(
    lead_id: str,
    type: str,
    url: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Append one entry to `artifacts` (e.g. brief PDF, proposal PDF, email)."""
    entry = {
        "ts": _now_iso(),
        "type": type,
        "url": url,
        "meta": _serialize(meta or {}),
    }
    await _append_to_jsonb_array(lead_id, "artifacts", entry)


async def session_append_signal(
    lead_id: str,
    kind: str,
    value: str = "",
    source: str = "",
) -> None:
    """Append one entry to `signals` (email_open, click, reply, page_view…)."""
    entry = {
        "ts": _now_iso(),
        "kind": kind,
        "value": value,
        "source": source,
    }
    await _append_to_jsonb_array(lead_id, "signals", entry)


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------


async def session_set_next(
    lead_id: str,
    next_action: Optional[str],
    next_action_at: Optional[datetime],
) -> None:
    """Set the next-action key and its due time. Either may be None to clear."""
    payload: dict[str, Any] = {
        "next_action": next_action,
        "next_action_at": _serialize(next_action_at) if next_action_at else None,
        "last_touch_at": _now_iso(),
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code not in (200, 204):
        log.warning(
            "session_set_next non-200 lead=%s: %s %s",
            lead_id, r.status_code, r.text[:200],
        )


async def session_set_status(lead_id: str, status: str) -> None:
    """Set `lifecycle_status` (e.g. 'proposal_sent', 'won', 'error')."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            headers=SUPA_HEADERS,
            json={"lifecycle_status": status, "last_touch_at": _now_iso()},
        )
    if r.status_code not in (200, 204):
        log.warning(
            "session_set_status non-200 lead=%s: %s %s",
            lead_id, r.status_code, r.text[:200],
        )


async def session_due(limit: int = 50) -> list[dict]:
    """Return up to `limit` leads whose `next_action_at` is in the past.

    Ordered by `next_action_at` ascending so the oldest due jobs run first.
    The orchestrator consumes this list each tick.
    """
    now_iso = _now_iso()
    url = (
        f"{SUPA_URL}/leads"
        f"?next_action=not.is.null"
        f"&next_action_at=lte.{now_iso}"
        f"&order=next_action_at.asc"
        f"&limit={int(limit)}"
    )
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning(
            "session_due non-200: %s %s", r.status_code, r.text[:200]
        )
        return []
    return r.json() or []


# ---------------------------------------------------------------------------
# Admin router — read-only inspection of a session
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/session", tags=["sessions"])


@router.get("/{lead_id}")
async def get_session(lead_id: str) -> dict:
    """Return the full lead row. Behind CF Access (admin-only) in production."""
    row = await session_get(lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="lead not found")
    return row
