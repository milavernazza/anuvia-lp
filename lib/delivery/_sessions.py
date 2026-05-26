"""White-glove delivery sessions — auto-booked presentation meetings.

When an engagement runs in ``delivery_mode='whiteglove'`` (the default for
new engagements per task #56), every phase boundary that would normally
trigger a client email instead:

  1. Auto-books a presentation meeting on Mila's primary calendar
     (mila@anuvia.com.br) with a Google Meet link. The client is invited
     as an attendee.

  2. Generates an operator-facing pre-call brief (talking points, top
     findings, anticipated questions). The brief lives in the *private*
     event B description so Mila can prep without leaking it to the client.

  3. Slack-DMs Mila a rich block with the materials, the Meet link, and a
     button: "Apresentei → enviar materiais ao cliente". The button hits
     :mod:`lib.whiteglove_routes` which calls :func:`send_client_materials`
     to fire the real artifacts email.

  4. The client email is held back until Mila clicks the button. This makes
     the human moment (the presentation) the actual point of delivery —
     everything else is autonomous.

Public API:
    * ``book_phase_session(engagement_id, phase, *, ...)`` — does steps
      1-3 above. Idempotent: re-running with an existing session returns
      the previously-booked event.
    * ``generate_pre_call_brief(engagement_id, phase)`` — Claude-generated
      operator brief. Used as the description of the private Gcal event.
    * ``send_client_materials(engagement_id, phase)`` — invoked by the
      Slack button handler. Renders the phase-N artifacts email via the
      existing ``_phaseN_email_html`` templates and stamps
      ``phase_N_email_sent_at``. Idempotent: won't re-send.

Design rules (mirrors :mod:`lib.delivery.finops_audit`):
    * Network failures bubble up so the caller can retry.
    * Graceful degradation: missing GOOGLE_CLIENT_ID, missing Slack
      webhook, missing Resend key — each falls back to a logged warning
      and persists best-effort state on the engagement so Mila can recover
      manually.
    * Phase durations:
        phase 2 = 60min (findings walkthrough)
        phase 3 = 45min (quick wins review)
        phase 4 = 90min (final handoff + roadmap walkthrough)
    * Working hours: Mon-Fri 09:00-17:30 BRT, 30-min coarse slots.
      Public holidays + Gcal busy ranges are filtered out (re-uses the
      same helpers backing ``/api/slots``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _urlquote
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("anuvia-lp.delivery.sessions")


# ---------------------------------------------------------------------------
# Config + env
# ---------------------------------------------------------------------------

TZ_SP = ZoneInfo("America/Sao_Paulo")

# Phase → presentation duration. Sourced from sprint inputs:
#   phase 2 — findings walkthrough (60 min)
#   phase 3 — quick wins review + sign-off (45 min)
#   phase 4 — final handoff + roadmap walkthrough (90 min)
PHASE_DURATIONS_MIN: Dict[int, int] = {2: 60, 3: 45, 4: 90}

PHASE_LABELS_PT: Dict[int, str] = {
    2: "Findings — semana 2",
    3: "Quick wins — semana 3",
    4: "Entrega final — semana 4",
}

# Default lookahead window when searching for a slot.
_DEFAULT_DAYS_LOOKAHEAD: int = 5

# How far ahead the booked slot must be from now. Gives the client a
# breathing window to put it on their calendar before the meeting.
_MIN_LEAD_TIME = timedelta(hours=24)

# Working hours (BRT).
_WORKING_HOUR_START = 9
_WORKING_HOUR_END = 17  # last slot starts at 17:30, ends at 18:00

# Short timeout for HTTP calls; outer caller does the retrying.
_HTTP_TIMEOUT: float = 30.0

BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("CONTRACT_HOST", "https://anuvia.com.br"),
).rstrip("/")

# HMAC secret — shared with contract.py + finops_audit.py.
_HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)

# Mila's calendar email — used for fallback descriptions when the gcal
# account lookup fails.
MILA_EMAIL = os.environ.get("ANUVIA_MILA_EMAIL", "mila@anuvia.com.br")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get(
    "ANUVIA_DELIVERY_MODEL", "claude-sonnet-4-5-20250929"
)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _hmac_release_token(engagement_id: str, phase: int) -> str:
    """HMAC token for the Slack ``release materials`` button URL.

    Purpose string ``release:{engagement_id}:{phase}`` so a leaked phase-2
    button cannot trigger the phase-4 email.
    """
    if not _HMAC_SECRET:
        log.warning(
            "sessions: HMAC secret unset; release links will be unverifiable"
        )
        return ""
    msg = f"release:{engagement_id}:{int(phase)}".encode("utf-8")
    return hmac.new(
        _HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


def verify_release_token(engagement_id: str, phase: int, token: str) -> bool:
    """Constant-time verify for the release-materials HMAC token."""
    if not engagement_id or not token:
        return False
    expected = _hmac_release_token(engagement_id, int(phase))
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def _format_br_datetime(dt: datetime) -> str:
    """Return a human-readable BRT timestamp: 'Qua, 21 mai · 14:30'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TZ_SP)
    wd = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"][local.weekday()]
    months = [
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    ]
    return f"{wd}, {local.day} {months[local.month - 1]} · {local.strftime('%H:%M')}"


# ---------------------------------------------------------------------------
# Engagement + lead row helpers (thin wrappers around Supabase REST)
# ---------------------------------------------------------------------------


def _supa_endpoint() -> Tuple[str, Dict[str, str]]:
    """Return ``(SUPA_URL, SUPA_HEADERS)`` — lazily so env loads at call time."""
    from lib.sessions import SUPA_URL, SUPA_HEADERS  # local import = test friendly
    return SUPA_URL, SUPA_HEADERS


async def _engagement_get(engagement_id: str) -> Optional[dict]:
    supa_url, headers = _supa_endpoint()
    url = (
        f"{supa_url}/engagements?"
        f"id=eq.{_urlquote(str(engagement_id), safe='')}&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=headers)
    except Exception:  # noqa: BLE001
        log.exception("sessions: engagement_get failed id=%s", engagement_id)
        return None
    if r.status_code != 200:
        log.warning(
            "sessions: engagement_get non-200 id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def _engagement_merge_artifacts(
    engagement_id: str, additions: dict
) -> bool:
    """Merge ``additions`` into ``engagement.artifacts``. Read-modify-write."""
    eng = await _engagement_get(engagement_id)
    if not eng:
        return False
    current = eng.get("artifacts") or {}
    if not isinstance(current, dict):
        current = {"_legacy": current}
    merged = dict(current)
    merged.update(additions)

    supa_url, headers = _supa_endpoint()
    url = f"{supa_url}/engagements?id=eq.{_urlquote(str(engagement_id), safe='')}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.patch(url, headers=headers, json={"artifacts": merged})
    except Exception:  # noqa: BLE001
        log.exception(
            "sessions: merge_artifacts patch failed id=%s", engagement_id
        )
        return False
    return r.status_code in (200, 204)


async def _lead_get(lead_id: str) -> Optional[dict]:
    supa_url, headers = _supa_endpoint()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{supa_url}/leads?id=eq.{_urlquote(str(lead_id), safe='')}&limit=1",
                headers=headers,
            )
    except Exception:  # noqa: BLE001
        log.exception("sessions: lead_get failed id=%s", lead_id)
        return None
    if r.status_code != 200:
        return None
    rows = r.json() or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Slot-finding helpers (re-implemented locally so this module never imports
# app.py — both directions of import would cycle).
# ---------------------------------------------------------------------------


def _generate_working_day_slots(d: datetime) -> List[str]:
    """30-min slot strings 09:00..17:30 for a Mon-Fri day. Public-holiday-aware
    via best-effort lazy import of app.is_public_holiday."""
    if d.weekday() >= 5:
        return []
    try:
        from app import is_public_holiday  # type: ignore
        if is_public_holiday(d.strftime("%Y-%m-%d"), market="BR"):
            return []
    except Exception:  # noqa: BLE001
        # Worst case: we don't filter holidays. Mila still gets the slot
        # proposal and can re-pick if it lands on a feriado.
        pass

    slots: List[str] = []
    hour = _WORKING_HOUR_START
    minute = 0
    while hour < _WORKING_HOUR_END or (hour == _WORKING_HOUR_END and minute <= 30):
        slots.append(f"{hour:02d}:{minute:02d}")
        minute += 30
        if minute == 60:
            minute = 0
            hour += 1
    return slots


async def _fetch_busy_ranges(
    client: httpx.AsyncClient, time_min: datetime, time_max: datetime
) -> List[Tuple[datetime, datetime]]:
    """Delegate to app.fetch_all_busy_ranges if importable, else []."""
    try:
        from app import fetch_all_busy_ranges  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning(
            "sessions: app.fetch_all_busy_ranges not importable — skipping gcal busy filter"
        )
        return []
    try:
        return await fetch_all_busy_ranges(client, time_min, time_max)
    except Exception:  # noqa: BLE001
        log.exception("sessions: fetch_all_busy_ranges raised — skipping busy filter")
        return []


def _slot_conflicts_with_busy(
    slot_start: datetime,
    slot_duration_min: int,
    busy_ranges: List[Tuple[datetime, datetime]],
) -> bool:
    slot_end = slot_start + timedelta(minutes=slot_duration_min)
    for b_start, b_end in busy_ranges:
        if slot_start < b_end and b_start < slot_end:
            return True
    return False


async def _find_next_available_slot(
    *,
    duration_min: int,
    days_lookahead: int,
    min_lead_time: timedelta = _MIN_LEAD_TIME,
) -> Optional[datetime]:
    """Return the first available slot >= now + min_lead_time, BRT-aware.

    Returns a tz-aware datetime in TZ_SP (America/Sao_Paulo), or None when
    the entire lookahead window is booked / no working days remain.
    """
    days_lookahead = max(1, min(days_lookahead, 10))
    now_sp = datetime.now(TZ_SP)
    earliest = now_sp + min_lead_time
    # Start scanning from tomorrow in SP (matches /api/slots behaviour).
    today_sp = now_sp.date()
    start_day = datetime.combine(
        today_sp + timedelta(days=1), datetime.min.time(), tzinfo=TZ_SP
    )

    # Build a list of working-day datetimes (Mon-Fri) up to days_lookahead.
    working: List[datetime] = []
    cur = start_day
    while len(working) < days_lookahead and (cur - start_day).days < 21:
        if cur.weekday() < 5:
            working.append(cur)
        cur = cur + timedelta(days=1)

    if not working:
        return None

    # Fetch busy ranges once across the full window.
    time_min = datetime.combine(
        working[0].date(), datetime.min.time(), tzinfo=TZ_SP
    )
    time_max = datetime.combine(
        working[-1].date(), datetime.max.time(), tzinfo=TZ_SP
    )
    busy_ranges: List[Tuple[datetime, datetime]] = []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            busy_ranges = await _fetch_busy_ranges(client, time_min, time_max)
    except Exception:  # noqa: BLE001
        log.exception("sessions: busy range fetch failed — proceeding unfiltered")
        busy_ranges = []

    for d in working:
        raw = _generate_working_day_slots(d)
        if not raw:
            continue
        for hhmm in raw:
            try:
                hh, mm = hhmm.split(":")
                slot_start = d.replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
            except ValueError:
                continue
            if slot_start < earliest:
                continue
            if busy_ranges and _slot_conflicts_with_busy(
                slot_start, duration_min, busy_ranges
            ):
                continue
            return slot_start
    return None


# ---------------------------------------------------------------------------
# Gcal event creation (re-uses Mila's primary calendar)
# ---------------------------------------------------------------------------


async def _create_session_events(
    *,
    client: httpx.AsyncClient,
    client_email: Optional[str],
    client_name: str,
    start_dt: datetime,
    end_dt: datetime,
    title: str,
    client_description: str,
    operator_brief: str,
) -> dict:
    """Create A) public event with attendee + Meet, B) private event with brief.

    Mirrors :func:`app._create_gcal_event_pair_for_booking` but is scoped
    for delivery sessions (not discovery bookings) — different title prefix
    and the brief content comes from :func:`generate_pre_call_brief`.

    Returns ``{public_event_id, private_event_id, meet_url, html_link,
    calendar_id, error}`` — best-effort, never raises.
    """
    result: dict = {
        "public_event_id": None,
        "private_event_id": None,
        "meet_url": None,
        "html_link": None,
        "calendar_id": None,
        "error": None,
    }
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        result["error"] = "google_oauth_not_configured"
        return result

    # Lazy-import the gcal helpers from app.py. If app isn't importable
    # (e.g. unit test), we can't create events — flag and bail.
    try:
        from app import (  # type: ignore
            _pick_primary_gcal_account,
            _get_cached_access_token,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "sessions: app.* gcal helpers not importable — cannot create event"
        )
        result["error"] = "gcal_helpers_unavailable"
        return result

    account = await _pick_primary_gcal_account(client)
    if not account:
        result["error"] = "no_active_gcal_account"
        return result

    email = account.get("email")
    refresh_token = account.get("refresh_token")
    cal_id = account.get("calendar_id") or "primary"
    if not (email and refresh_token):
        result["error"] = "gcal_account_missing_credentials"
        return result

    token = await _get_cached_access_token(client, email, refresh_token)
    if not token:
        result["error"] = "gcal_token_exchange_failed"
        return result

    import urllib.parse as _up
    cal_url = (
        f"https://www.googleapis.com/calendar/v3/calendars/"
        f"{_up.quote(cal_id)}/events"
    )

    start_iso = start_dt.astimezone(TZ_SP).isoformat()
    end_iso = end_dt.astimezone(TZ_SP).isoformat()

    # ----- Event A: public, with client attendee + Meet -----
    public_body: dict = {
        "summary": title,
        "description": client_description,
        "start": {"dateTime": start_iso, "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end_iso, "timeZone": "America/Sao_Paulo"},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {"useDefault": True},
        "guestsCanModify": False,
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
    }
    if client_email:
        public_body["attendees"] = [{
            "email": client_email,
            "displayName": client_name or "Cliente",
            "responseStatus": "needsAction",
        }]

    try:
        ra = await client.post(
            cal_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={
                "conferenceDataVersion": "1",
                "sendUpdates": "all" if client_email else "none",
            },
            json=public_body,
            timeout=15,
        )
        if ra.status_code in (200, 201):
            ev_a = ra.json()
            result["public_event_id"] = ev_a.get("id")
            result["html_link"] = ev_a.get("htmlLink")
            result["calendar_id"] = cal_id
            meet_url = ev_a.get("hangoutLink")
            if not meet_url:
                for ep in (ev_a.get("conferenceData") or {}).get("entryPoints") or []:
                    if ep.get("entryPointType") == "video" and ep.get("uri"):
                        meet_url = ep["uri"]
                        break
            result["meet_url"] = meet_url
        else:
            result["error"] = f"event_a_insert_{ra.status_code}"
            log.warning(
                "sessions: gcal event A non-2xx %s %s",
                ra.status_code, ra.text[:300],
            )
            return result
    except Exception:  # noqa: BLE001
        log.exception("sessions: gcal event A insert exception")
        result["error"] = "event_a_insert_exception"
        return result

    # ----- Event B: private operator brief -----
    private_summary = f"Brief · {title}"
    private_description = operator_brief or "(brief não disponível)"
    if result.get("meet_url"):
        private_description += (
            f"\n\n—\nMeet link (Event A): {result['meet_url']}"
            + (f"\nEvent A: {result.get('html_link')}" if result.get('html_link') else "")
        )

    private_body = {
        "summary": private_summary,
        "description": private_description,
        "start": {"dateTime": start_iso, "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end_iso, "timeZone": "America/Sao_Paulo"},
        "visibility": "private",
        "transparency": "opaque",
        "reminders": {"useDefault": False},
    }
    try:
        rb = await client.post(
            cal_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"sendUpdates": "none"},
            json=private_body,
            timeout=15,
        )
        if rb.status_code in (200, 201):
            ev_b = rb.json()
            result["private_event_id"] = ev_b.get("id")
        else:
            log.warning(
                "sessions: gcal event B non-2xx %s %s",
                rb.status_code, rb.text[:300],
            )
            result["error"] = (result.get("error") or "") + f" event_b_insert_{rb.status_code}".strip()
    except Exception:  # noqa: BLE001
        log.exception("sessions: gcal event B insert exception")
        result["error"] = (result.get("error") or "") + " event_b_insert_exception"

    return result


# ---------------------------------------------------------------------------
# Claude wrapper — re-uses the brand prompt from finops_audit when available
# ---------------------------------------------------------------------------


_FALLBACK_BRIEF_PREFIX = "[BRIEF_UNAVAILABLE_DRAFT]"


async def _call_claude(prompt: str, *, max_tokens: int = 2500) -> str:
    """Thin Anthropic Messages API call. Mirrors the pattern in
    :mod:`lib.delivery.finops_audit` but lives here so the brief can be
    generated without circular imports.

    Returns either the model's text or a ``_FALLBACK_BRIEF_PREFIX``-tagged
    snippet so the operator can see what was attempted.
    """
    # Re-use the finops brand system prompt when available so the brief
    # speaks Mila's voice.
    try:
        from lib.delivery.finops_audit import _BRAND_SYSTEM_PROMPT  # type: ignore
        system = _BRAND_SYSTEM_PROMPT
    except Exception:  # noqa: BLE001
        system = (
            "Você está escrevendo um pre-call brief operacional para Mila "
            "Vernazza (founder Anuvia). Voz seca, numbers-first, anti-hype. "
            "Português do Brasil."
        )

    if not ANTHROPIC_API_KEY:
        return f"{_FALLBACK_BRIEF_PREFIX} (no ANTHROPIC_API_KEY)\n\n{prompt[:600]}"

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("sessions: anthropic network failed: %s", exc)
        return f"{_FALLBACK_BRIEF_PREFIX} (network: {exc})"

    if r.status_code != 200:
        log.warning(
            "sessions: anthropic non-200 status=%s body=%s",
            r.status_code, r.text[:300],
        )
        return f"{_FALLBACK_BRIEF_PREFIX} (status {r.status_code})"

    body = r.json() if r.text else {}
    parts: List[str] = []
    for blk in (body.get("content") or []):
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text") or "")
    out = "\n".join(parts).strip()
    return out or f"{_FALLBACK_BRIEF_PREFIX} (empty)"


# ---------------------------------------------------------------------------
# Slack DM helpers (rich block kit)
# ---------------------------------------------------------------------------


async def _slack_post(payload: dict) -> bool:
    """Post a Slack message payload (text or blocks) to the configured webhook.

    Best-effort. Tries ``SLACK_ALERTS_WEBHOOK`` first, then
    ``SLACK_NEW_LEAD_WEBHOOK``. Returns True iff the webhook responded 2xx.
    """
    webhook = os.environ.get("SLACK_ALERTS_WEBHOOK") or os.environ.get(
        "SLACK_NEW_LEAD_WEBHOOK"
    )
    if not webhook:
        log.warning("sessions: no slack webhook configured; payload dropped")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(webhook, json=payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("sessions: slack post failed: %s", exc)
        return False
    if r.status_code >= 400:
        log.warning(
            "sessions: slack webhook %s: %s", r.status_code, r.text[:200]
        )
        return False
    return True


def _build_release_block(
    *,
    engagement_id: str,
    phase: int,
    client_name: str,
    findings_summary: str,
    scheduled_at_br: str,
    duration_min: int,
    meet_url: Optional[str],
    materials: List[Tuple[str, str]],
    brief_snippet: str,
) -> dict:
    """Build the Slack Block Kit payload for the 'apresentei → liberar' DM."""
    phase_label = PHASE_LABELS_PT.get(phase, f"Phase {phase}")

    materials_lines = []
    for label, url in materials:
        if url and url.startswith("http"):
            materials_lines.append(f"• <{url}|{label}>")
        else:
            materials_lines.append(f"• {label} (sem URL pública)")
    materials_block = "\n".join(materials_lines) or "• (sem materiais)"

    token = _hmac_release_token(engagement_id, phase)
    release_url = (
        f"{BASE_URL}/api/_admin/whiteglove/release/"
        f"{_urlquote(str(engagement_id), safe='')}/{int(phase)}"
        f"?token={token}"
    )

    meet_line = (
        f":calendar: *Reunião agendada:* {scheduled_at_br} ({duration_min}min)\n"
        f"Meet: {meet_url}"
        if meet_url
        else f":calendar: *Reunião agendada:* {scheduled_at_br} ({duration_min}min)\n"
             f"(meet link pendente — gcal indisponível)"
    )

    header_text = (
        f":warning: *FinOps Phase {phase} — Materiais prontos pra apresentação*\n\n"
        f"Engagement: `{engagement_id}`\n"
        f"Cliente: *{client_name}*\n"
        f"{findings_summary}"
    )

    brief_excerpt = (brief_snippet or "").strip()
    if len(brief_excerpt) > 600:
        brief_excerpt = brief_excerpt[:600].rsplit("\n", 1)[0] + "\n…"

    blocks: List[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": meet_line},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":open_file_folder: *Materiais*\n{materials_block}",
            },
        },
    ]
    if brief_excerpt:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":notebook: *Pre-call brief (resumo)*\n```{brief_excerpt}```",
            },
        })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {
                    "type": "plain_text",
                    "text": "Apresentei → enviar materiais ao cliente",
                    "emoji": True,
                },
                "url": release_url,
            }
        ],
    })

    # Fallback text for clients that don't render block kit (mobile push).
    fallback_text = (
        f"FinOps Phase {phase_label} — materiais prontos pra apresentação. "
        f"Cliente: {client_name}. Reunião: {scheduled_at_br}. "
        f"Liberar materiais: {release_url}"
    )
    return {"text": fallback_text, "blocks": blocks}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def book_phase_session(
    engagement_id: str,
    phase: int,
    *,
    practice: str = "cloud_finops",
    duration_min: Optional[int] = None,
    days_lookahead: int = _DEFAULT_DAYS_LOOKAHEAD,
) -> dict:
    """Auto-book a presentation meeting for ``phase`` of ``engagement_id``.

    Returns ``{ok, gcal_event_id, meet_url, scheduled_at, duration_min,
    session_id, reason?}``. Idempotent: if a session is already booked for
    this phase, returns the cached values.

    Steps:
        1. Resolve the client email + name from ``engagement.lead_id``.
        2. Find the next available 30-min-aligned slot (Mon-Fri 09:00-17:30
           BRT, ≥24h ahead, gcal-busy filtered).
        3. Generate the pre-call brief (Claude).
        4. Create a Gcal event pair on Mila's primary calendar:
           - Event A: public, client invited, Google Meet conference.
           - Event B: private, operator-only, brief in description.
        5. Persist ``session_id`` + identifiers under
           ``engagement.artifacts.sessions["phase_{phase}"]``.
        6. Return the booked-slot dict.
    """
    phase = int(phase)
    if phase not in PHASE_DURATIONS_MIN:
        return {"ok": False, "reason": f"invalid_phase_{phase}"}
    if duration_min is None:
        duration_min = PHASE_DURATIONS_MIN[phase]

    eng = await _engagement_get(engagement_id)
    if not eng:
        return {"ok": False, "reason": "engagement_not_found"}

    # --- Idempotency: do we already have a session for this phase? ---
    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    sessions_map = artifacts.get("sessions") or {}
    if not isinstance(sessions_map, dict):
        sessions_map = {}
    phase_key = f"phase_{phase}"
    existing = sessions_map.get(phase_key)
    if isinstance(existing, dict) and existing.get("gcal_event_id"):
        log.info(
            "sessions: phase %s already booked for eng=%s — returning cached",
            phase, engagement_id,
        )
        return {
            "ok": True,
            "cached": True,
            "session_id": existing.get("session_id"),
            "gcal_event_id": existing.get("gcal_event_id"),
            "meet_url": existing.get("meet_url"),
            "scheduled_at": existing.get("scheduled_at"),
            "duration_min": existing.get("duration_min") or duration_min,
        }

    # --- 1) Resolve client identity ---
    lead_id = eng.get("lead_id")
    lead = await _lead_get(str(lead_id)) if lead_id else None
    client_email = (lead or {}).get("email")
    client_name = (
        (lead or {}).get("company")
        or (lead or {}).get("name")
        or "Cliente"
    )
    first_name = ((lead or {}).get("name") or "").split(" ")[0] or "Time"

    # --- 2) Find a slot ---
    slot_dt = await _find_next_available_slot(
        duration_min=duration_min,
        days_lookahead=days_lookahead,
    )
    if not slot_dt:
        log.warning(
            "sessions: no slot available eng=%s phase=%s lookahead=%s",
            engagement_id, phase, days_lookahead,
        )
        return {"ok": False, "reason": "no_slot_available"}

    start_dt = slot_dt
    end_dt = slot_dt + timedelta(minutes=duration_min)

    # --- 3) Generate the operator brief ---
    try:
        operator_brief = await generate_pre_call_brief(engagement_id, phase)
    except Exception:  # noqa: BLE001
        log.exception(
            "sessions: brief generation crashed eng=%s phase=%s — proceeding "
            "with placeholder", engagement_id, phase,
        )
        operator_brief = (
            f"{_FALLBACK_BRIEF_PREFIX} (exception during composition)"
        )

    phase_label = PHASE_LABELS_PT.get(phase, f"Phase {phase}")
    title = f"Anuvia FinOps Audit — {phase_label}"
    client_desc = (
        f"Olá {first_name},\n\n"
        f"Sessão de apresentação da fase {phase} da FinOps Audit Anuvia "
        f"({phase_label}). Nossa equipe apresenta os achados + "
        f"recomendações; ao final, vocês recebem os materiais por email.\n\n"
        f"Duração: {duration_min} min. O link do Google Meet está neste convite."
    )

    # --- 4) Create Gcal events ---
    session_id = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        gcal_result = await _create_session_events(
            client=client,
            client_email=client_email,
            client_name=client_name,
            start_dt=start_dt,
            end_dt=end_dt,
            title=title,
            client_description=client_desc,
            operator_brief=operator_brief,
        )

    # --- 5) Persist + return ---
    session_record = {
        "session_id": session_id,
        "phase": phase,
        "phase_label": phase_label,
        "duration_min": duration_min,
        "scheduled_at": start_dt.astimezone(timezone.utc).isoformat(),
        "scheduled_at_br": _format_br_datetime(start_dt),
        "client_email": client_email,
        "client_name": client_name,
        "gcal_event_id": gcal_result.get("public_event_id"),
        "gcal_private_event_id": gcal_result.get("private_event_id"),
        "gcal_html_link": gcal_result.get("html_link"),
        "gcal_calendar_id": gcal_result.get("calendar_id"),
        "meet_url": gcal_result.get("meet_url"),
        "gcal_error": gcal_result.get("error"),
        "operator_brief_snippet": (operator_brief or "")[:1500],
        "booked_at": _now_iso(),
    }

    sessions_map[phase_key] = session_record
    await _engagement_merge_artifacts(
        engagement_id,
        {"sessions": sessions_map},
    )

    return {
        "ok": bool(gcal_result.get("public_event_id")) or bool(gcal_result.get("error") in (None, "")),
        "session_id": session_id,
        "gcal_event_id": gcal_result.get("public_event_id"),
        "gcal_private_event_id": gcal_result.get("private_event_id"),
        "meet_url": gcal_result.get("meet_url"),
        "scheduled_at": session_record["scheduled_at"],
        "scheduled_at_br": session_record["scheduled_at_br"],
        "duration_min": duration_min,
        "client_email": client_email,
        "client_name": client_name,
        "error": gcal_result.get("error"),
    }


async def generate_pre_call_brief(engagement_id: str, phase: int) -> str:
    """Operator-facing pre-call brief — 2-3 page talking points.

    Structure (all sections obligatory):
        1. Phase context — what's being delivered, what's at stake.
        2. Top 5 findings/changes with talking points (numbers first).
        3. Client-specific intake data summary.
        4. Anticipated questions + suggested answers.
        5. Recommended next steps from this presentation.

    The brief lives in the *private* Gcal event (Event B), never in the
    client-facing Event A. Voice: dry, numbers-first, Anuvia.
    """
    phase = int(phase)
    eng = await _engagement_get(engagement_id)
    if not eng:
        return f"{_FALLBACK_BRIEF_PREFIX} (engagement not found: {engagement_id})"

    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    intake = eng.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    intake_lines: List[str] = []
    for k, v in intake.items():
        if v in (None, "", []):
            continue
        intake_lines.append(f"- {k}: {v}")
    intake_block = "\n".join(intake_lines) or "(intake vazio)"

    # Pull phase-specific context.
    findings = artifacts.get("phase_2_findings") or {}
    findings_summary = findings.get("summary") or ""
    findings_items = findings.get("findings") or []
    if not isinstance(findings_items, list):
        findings_items = []
    top = sorted(
        [f for f in findings_items if isinstance(f, dict)],
        key=lambda f: int(f.get("savings_brl_high") or 0),
        reverse=True,
    )[:5]
    top_lines = []
    for f in top:
        top_lines.append(
            f"- {f.get('vector') or '—'} | "
            f"R$ {f.get('savings_brl_low') or 0:,} – {f.get('savings_brl_high') or 0:,}/ano | "
            f"priority={f.get('priority') or '—'} | "
            f"effort={f.get('effort') or '—'} | "
            f"risk={f.get('risk') or '—'}\n  Hipótese: {f.get('hypothesis') or '—'}"
        )
    top_block = "\n".join(top_lines) or "(sem findings cacheados)"

    change_log_md = artifacts.get("phase_3_change_log_md") or ""
    report_md = artifacts.get("final_report_md") or ""
    roadmap_md = artifacts.get("roadmap_md") or ""

    phase_label = PHASE_LABELS_PT.get(phase, f"Phase {phase}")
    duration = PHASE_DURATIONS_MIN.get(phase, 60)

    if phase == 2:
        phase_specific_context = (
            "Fase 2 — apresentação dos findings da auditoria. O cliente recebe "
            "PDF com 8 vetores analisados (compute/storage/network/data "
            "transfer/RDS/S3/SaaS/support) e top opportunities priorizadas. "
            "Mila apresenta numbers-first, framework references (FinOps Capability + "
            "AWS WA BP code), e abre conversa sobre o caminho de remediação "
            "(time interno do cliente vs Anuvia via success-fee).\n\n"
            f"Findings summary:\n{findings_summary or '(não cacheado)'}\n\n"
            f"Top 5 findings:\n{top_block}"
        )
    elif phase == 3:
        phase_specific_context = (
            "Fase 3 — apresentação do plano de execução (change log) das "
            "quick wins. Cada mudança vem com critério de validação prévio, "
            "comandos AWS CLI, plano de rollback e janela proposta. Mila "
            "apresenta item por item, recolhe sign-off, decide a ordem de "
            "execução.\n\n"
            f"Top findings da fase 2 (contexto):\n{top_block}\n\n"
            f"Change log snippet (primeiros 1500 chars):\n"
            f"{change_log_md[:1500] or '(não cacheado)'}"
        )
    elif phase == 4:
        phase_specific_context = (
            "Fase 4 — handoff final. Apresentação do relatório executivo "
            "(15-20 páginas), deck PPTX (18-22 slides) e roadmap 12 meses "
            "(Crawl/Walk/Run). Mila apresenta o pacote completo, alinha o "
            "modelo de governança contínua (cadência mensal + métricas + "
            "thresholds), e abre conversa sobre retainer ongoing FinOps + "
            "execução de iniciativas estruturais via success-fee.\n\n"
            f"Top findings (contexto):\n{top_block}\n\n"
            f"Final report snippet (primeiros 1500 chars):\n"
            f"{report_md[:1500] or '(não cacheado)'}\n\n"
            f"Roadmap snippet (primeiros 1000 chars):\n"
            f"{roadmap_md[:1000] or '(não cacheado)'}"
        )
    else:
        phase_specific_context = f"Fase {phase} — (sem template específico)."

    prompt = f"""Você está escrevendo um pre-call brief OPERACIONAL — só pra Mila Vernazza ver antes de apresentar a fase {phase} ({phase_label}) de uma auditoria FinOps Anuvia. NÃO é pro cliente. Duração da reunião: {duration} min.

Esse documento vai pra dentro do evento Gcal privado (visibility=private) — operator-only. Voz da Mila: seca, numbers-first, anti-hype, referências de framework explícitas.

Contexto da fase:
{phase_specific_context}

Dados do intake do cliente:
{intake_block}

Estruture o brief em 5 seções, markdown:

## 1. Contexto da apresentação ({duration} min)
- O que tá sendo entregue nesta sessão (3-5 bullets).
- O que tá em jogo (decisão do cliente, próximo passo do funil).

## 2. Top 5 findings/mudanças com talking points
Para cada um:
- Headline (1 linha — vetor + economia anualizada + confiança).
- Math explícita (mostra a conta).
- Talking point principal (1-2 frases — o que Mila diz alto).
- Objeção provável + resposta (uma linha cada).

## 3. Dados específicos do cliente (do intake)
- Resumo de 4-6 bullets com os números do cliente que vão ancorar a conversa (baseline mensal, account count, primary services, etc.).

## 4. Perguntas antecipadas (5-7) + respostas sugeridas
Formato: **Pergunta** → resposta de 1-2 frases. Cobrir: math, prazo de execução, blast radius, RI/SP strategy, GCP equivalente (se multi-cloud), success-fee model, IAM access pendente.

## 5. Próximos passos recomendados
- 2-3 bullets do que Mila pede ao cliente sair da reunião com decidido (sign-off, escolha de remediação, IAM role provision, etc.).
- 1 bullet do que a Anuvia entrega em sequência (timing).

Português do Brasil. Total: ~2-3 páginas. Mantém compacto — Mila lê isso 10 min antes da call."""

    return await _call_claude(prompt, max_tokens=2500)


# ---------------------------------------------------------------------------
# Client materials release (called by the Slack button)
# ---------------------------------------------------------------------------


async def send_client_materials(engagement_id: str, phase: int) -> dict:
    """Send the artifacts email to the client. Called by the Slack button
    handler in :mod:`lib.whiteglove_routes` AFTER Mila presents.

    Reuses the existing ``_phaseN_email_html`` templates from
    :mod:`lib.delivery.finops_audit` so the email is byte-identical to the
    legacy autonomous flow. Idempotent: returns early if
    ``phase_N_email_sent_at`` is already stamped.

    Returns ``{ok, reason?, phase, sent_at, message_id?}``.
    """
    phase = int(phase)
    eng = await _engagement_get(engagement_id)
    if not eng:
        return {"ok": False, "reason": "engagement_not_found"}

    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    sent_key = f"phase_{phase}_email_sent_at"
    if artifacts.get(sent_key):
        return {
            "ok": True,
            "reason": "already_sent",
            "phase": phase,
            "sent_at": artifacts[sent_key],
        }

    # Lazy import — avoids any cycle with finops_audit (which imports
    # the orchestrator registry and could in theory close back to here).
    from lib.delivery import finops_audit as fa  # type: ignore

    lead_id = eng.get("lead_id")
    lead = await _lead_get(str(lead_id)) if lead_id else None
    client_email = (lead or {}).get("email")
    first_name = ((lead or {}).get("name") or "").split(" ")[0] or "tudo bem"

    if not client_email:
        return {
            "ok": False,
            "reason": "client_email_missing",
            "phase": phase,
        }

    msg_id: Optional[str] = None
    subject: str
    html: str
    kind: str
    cc: Optional[List[str]] = None

    if phase == 2:
        pdf_url = artifacts.get("findings_list_url") or ""
        findings = artifacts.get("phase_2_findings") or {}
        top = fa._top_findings_for_email(findings)
        html = fa._phase2_email_html(
            first_name=first_name, pdf_url=pdf_url, top_findings=top
        )
        subject = "Findings da semana 2 — FinOps Audit"
        kind = "finops_phase_2_findings"
    elif phase == 3:
        changelog_url = artifacts.get("change_log_url") or ""
        token = fa._hmac_token(str(engagement_id), "approval")
        approval_url = (
            f"{BASE_URL}/api/delivery/finops/approve"
            f"?engagement_id={engagement_id}&token={token}"
        )
        findings = artifacts.get("phase_2_findings") or {}
        low, high = fa._findings_total_savings(findings)
        intake = eng.get("intake_data") or {}
        remediation_choice = str(
            (intake or {}).get("remediation_choice")
            or (intake or {}).get("execution_choice")
            or "cliente_interno"
        )
        html = fa._phase3_email_html(
            first_name=first_name,
            changelog_url=changelog_url,
            approval_url=approval_url,
            savings_brl=f"{fa._brl(low)} – {fa._brl(high)}",
            remediation_choice=remediation_choice,
        )
        subject = (
            "Plano de execução pronto — autorização requerida"
            if remediation_choice == "anuvia_success_fee"
            else "Runbook de quick wins pronto — execução pelo time interno"
        )
        kind = "finops_phase_3_approval"
    elif phase == 4:
        findings = artifacts.get("phase_2_findings") or {}
        low, high = fa._findings_total_savings(findings)
        savings_str = f"{fa._brl(low)} – {fa._brl(high)}"
        nps_url = (
            f"{BASE_URL}/api/delivery/finops/nps"
            f"?engagement_id={engagement_id}"
            f"&token={fa._hmac_token(str(engagement_id), 'nps')}"
        )
        html = fa._phase4_email_html(
            first_name=first_name,
            report_url=artifacts.get("final_report_url") or "",
            deck_url=artifacts.get("deck_url") or "",
            roadmap_url=artifacts.get("roadmap_url") or "",
            savings_brl=savings_str,
            nps_url=nps_url,
        )
        subject = "FinOps Audit entregue — relatório + roadmap + deck"
        kind = "finops_phase_4_delivery"
        if fa.RESEND_REPLY_TO_EMAIL:
            cc = [fa.RESEND_REPLY_TO_EMAIL]
    else:
        return {"ok": False, "reason": f"unsupported_phase_{phase}"}

    try:
        msg_id = await fa._send_email(
            engagement_id=str(engagement_id),
            to=client_email,
            subject=subject,
            html=html,
            kind=kind,
            cc=cc,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "sessions: send_client_materials email failed eng=%s phase=%s",
            engagement_id, phase,
        )
        return {
            "ok": False,
            "reason": f"resend_error: {exc}",
            "phase": phase,
        }

    sent_at = _now_iso()
    await _engagement_merge_artifacts(
        engagement_id,
        {
            sent_key: sent_at,
            f"phase_{phase}_email_released_via": "whiteglove_button",
            f"phase_{phase}_email_message_id": msg_id or "",
        },
    )

    return {
        "ok": True,
        "phase": phase,
        "sent_at": sent_at,
        "message_id": msg_id,
        "to": client_email,
    }


# ---------------------------------------------------------------------------
# Slack DM — public surface for the phase handlers
# ---------------------------------------------------------------------------


async def slack_dm_materials_ready(
    *,
    engagement_id: str,
    phase: int,
    client_name: str,
    findings_summary: str,
    scheduled_at_br: str,
    duration_min: int,
    meet_url: Optional[str],
    materials: List[Tuple[str, str]],
    brief_snippet: str = "",
) -> bool:
    """Send the Slack DM with materials + 'Apresentei' button.

    Returns True on successful Slack POST. Best-effort: a failed Slack
    post does NOT raise — the engagement state already records that the
    button is pending, and Mila can retrieve it from the engagement row.
    """
    payload = _build_release_block(
        engagement_id=engagement_id,
        phase=phase,
        client_name=client_name,
        findings_summary=findings_summary,
        scheduled_at_br=scheduled_at_br,
        duration_min=duration_min,
        meet_url=meet_url,
        materials=materials,
        brief_snippet=brief_snippet,
    )
    return await _slack_post(payload)


async def slack_dm_text(message: str) -> bool:
    """Plain-text Slack DM helper (for the 'email enviado' confirmation
    after Mila clicks the button)."""
    return await _slack_post({"text": message})
