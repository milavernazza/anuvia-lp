"""Outbound — cold-email engine.

Owned by Agent A1 in the v2 sprint. Per
ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Outbound contract".

Responsibilities:
  * Render personalised cold emails from per-practice / per-language templates
    via Claude, using prospect enrichment data + Anuvia value prop.
  * Dispatch the first touch via Resend immediately.
  * Queue touch 2 (d+3) and touch 3 (d+6) through the orchestrator.
  * Stop the sequence the moment a prospect engages (reply or click).
  * Mark `prospect.status` accordingly across the lifecycle.

Module boundaries (do not import from sibling sprint modules):
  * Reply classification + Resend inbound  → `lib/reply_classify.py` (A4)
  * Apollo / Clay enrichment + ICP scoring → `lib/prospecting.py` (A5)
  * Contract + payments                    → `lib/contract.py` (A3)
  * Per-practice Track B handlers          → `lib/track_b.py` (A2)

Quality bar (mirrors `lib/track_b.py`):
  * All public functions are async; httpx-based.
  * Idempotent: every send checks Resend deduplication via `prospects.current_touch`.
  * Retry policy: 2 Resend attempts with 3 s linear backoff. On final failure
    we record an `agent_history`-style entry in `prospects.enriched_data.agent_history`.
  * Anti-spam: hard daily cap (env `OUTBOUND_DAILY_CAP`, default 50) and a
    30-second inter-send rate limit.
  * Bounce / complaint statuses are part of the prospect status enum
    (`bounced`, `complained`) — Agent A4 owns the webhook receiver itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _urlquote

import httpx

from lib.orchestrator import register
from lib.sessions import SUPA_HEADERS, SUPA_URL

log = logging.getLogger("anuvia-lp.outbound")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "mila@anuvia.com.br")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
RESEND_REPLY_TO = os.environ.get("RESEND_REPLY_TO", RESEND_FROM_EMAIL)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
ANTHROPIC_API_URL = os.environ.get(
    "ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages"
)

OUTBOUND_DAILY_CAP = int(os.environ.get("OUTBOUND_DAILY_CAP", "50"))
OUTBOUND_RATE_LIMIT_SECONDS = int(os.environ.get("OUTBOUND_RATE_LIMIT_SECONDS", "30"))

CALENDAR_URL = os.environ.get("CALENDAR_URL", "https://anuvia.com.br/agenda")
UNSUB_URL = os.environ.get("UNSUB_URL", "https://anuvia.com.br/unsubscribe")

# Resolved relative to this file so layout matches `outbound/templates/...`
# at the project root (see ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Module layout").
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "outbound" / "templates"

_HTTP_TIMEOUT = 30.0
_RESEND_RETRIES = 2
_RESEND_BACKOFF_S = 3.0

# Module-level in-process rate limiter. The 30 s gap is enforced across all
# `send_email_via_resend` calls in one process. The daily cap is checked
# against the prospects table so it survives restarts.
_RATE_LOCK: Optional[asyncio.Lock] = None
_LAST_SEND_AT: Dict[str, float] = {"ts": 0.0}


def _rate_lock() -> asyncio.Lock:
    """Lazily build the asyncio.Lock so import is cheap and event-loop-safe."""
    global _RATE_LOCK
    if _RATE_LOCK is None:
        _RATE_LOCK = asyncio.Lock()
    return _RATE_LOCK


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Prospect-side persistence helpers (mirrors lib.sessions for `prospects`).
# Kept inline so this module does not depend on A5's prospecting helpers.
# ---------------------------------------------------------------------------


async def _prospect_get(prospect_id: str) -> Optional[dict]:
    """Fetch a prospect row by id. Returns None if not found."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(
            f"{SUPA_URL}/prospects?id=eq.{prospect_id}&limit=1",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        log.warning(
            "outbound._prospect_get non-200: %s %s", r.status_code, r.text[:200]
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def _prospect_update(prospect_id: str, **fields: Any) -> None:
    """PATCH arbitrary scalar columns on a prospect row. Stamps updated_at."""
    payload = dict(fields)
    payload.setdefault("updated_at", _now_iso())
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/prospects?id=eq.{prospect_id}",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code not in (200, 204):
        log.warning(
            "outbound._prospect_update non-200 prospect=%s: %s %s",
            prospect_id, r.status_code, r.text[:200],
        )
        raise RuntimeError(
            f"prospect_update failed: {r.status_code} {r.text[:200]}"
        )


async def _prospect_append_history(
    prospect_id: str,
    *,
    agent: str,
    action: str,
    result: str,
    detail: str = "",
    error: Optional[str] = None,
    latency_ms: int = 0,
) -> None:
    """Append a history entry to `prospects.enriched_data.agent_history`.

    `agent_history` lives inside the prospect's `enriched_data` jsonb so we
    don't need a separate jsonb column. Read-modify-write — best effort,
    never raises (history loss must not block the next touch).
    """
    entry = {
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "result": result,
        "detail": detail,
        "error": error,
        "latency_ms": int(latency_ms),
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            gr = await client.get(
                f"{SUPA_URL}/prospects?id=eq.{prospect_id}&select=enriched_data",
                headers=SUPA_HEADERS,
            )
            if gr.status_code != 200:
                log.warning(
                    "outbound._prospect_append_history GET failed: %s %s",
                    gr.status_code, gr.text[:200],
                )
                return
            rows = gr.json() or []
            if not rows:
                return
            enriched = rows[0].get("enriched_data") or {}
            if not isinstance(enriched, dict):
                try:
                    enriched = json.loads(enriched) if isinstance(enriched, str) else {}
                except Exception:  # noqa: BLE001
                    enriched = {}
            history = enriched.get("agent_history") or []
            if not isinstance(history, list):
                history = []
            history.append(entry)
            enriched["agent_history"] = history
            pr = await client.patch(
                f"{SUPA_URL}/prospects?id=eq.{prospect_id}",
                headers=SUPA_HEADERS,
                json={"enriched_data": enriched, "updated_at": _now_iso()},
            )
            if pr.status_code not in (200, 204):
                log.warning(
                    "outbound._prospect_append_history PATCH failed: %s %s",
                    pr.status_code, pr.text[:200],
                )
    except Exception:  # noqa: BLE001
        log.exception(
            "outbound._prospect_append_history: unexpected error prospect=%s",
            prospect_id,
        )


async def _prospect_set_next(
    prospect_id: str,
    next_action: Optional[str],
    next_action_at: Optional[datetime],
) -> None:
    """Set `next_touch_at` (and store the action key in enriched_data).

    The spec wording calls for `session_set_next` semantics with the
    prospect's id. Since prospects live in their own table we mirror the
    primitive on the prospects row. The orchestrator picks these up via a
    sibling tick (or wakes them by polling prospects.next_touch_at; the
    scheduler integration is owned by O5).
    """
    payload: Dict[str, Any] = {
        "next_touch_at": next_action_at.astimezone(timezone.utc).isoformat()
            if next_action_at
            else None,
        "updated_at": _now_iso(),
    }
    # We piggy-back the action key onto enriched_data so the orchestrator
    # (or A5's prospects-due polling) knows which handler to dispatch.
    if next_action is not None or next_action_at is None:
        # Need a read-modify-write to preserve the rest of enriched_data.
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            gr = await client.get(
                f"{SUPA_URL}/prospects?id=eq.{prospect_id}&select=enriched_data",
                headers=SUPA_HEADERS,
            )
            enriched: Dict[str, Any] = {}
            if gr.status_code == 200:
                rows = gr.json() or []
                if rows:
                    e = rows[0].get("enriched_data") or {}
                    if isinstance(e, dict):
                        enriched = e
            enriched["next_action"] = next_action
            payload["enriched_data"] = enriched
            r = await client.patch(
                f"{SUPA_URL}/prospects?id=eq.{prospect_id}",
                headers=SUPA_HEADERS,
                json=payload,
            )
            if r.status_code not in (200, 204):
                log.warning(
                    "outbound._prospect_set_next non-200 prospect=%s: %s %s",
                    prospect_id, r.status_code, r.text[:200],
                )
        return

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/prospects?id=eq.{prospect_id}",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code not in (200, 204):
        log.warning(
            "outbound._prospect_set_next non-200 prospect=%s: %s %s",
            prospect_id, r.status_code, r.text[:200],
        )


# ---------------------------------------------------------------------------
# Anti-spam guards: daily cap + 30 s inter-send rate limit
# ---------------------------------------------------------------------------


async def _sent_today_count() -> int:
    """Count prospects whose `last_engaged_at` shows a send in the last 24h.

    We use `current_touch >= 1` and an updated_at within the window as a
    proxy for "sent today" without a separate sends ledger table.
    """
    since = (_now() - timedelta(hours=24)).isoformat()
    url = (
        f"{SUPA_URL}/prospects"
        f"?status=in.(sequence_running,replied,bounced,complained,stopped,converted)"
        f"&updated_at=gte.{_urlquote(since, safe='')}"
        f"&select=id"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                url,
                headers={**SUPA_HEADERS, "Prefer": "count=exact"},
            )
        if r.status_code not in (200, 206):
            return 0
        # PostgREST returns total in Content-Range when Prefer: count=exact.
        cr = r.headers.get("content-range") or r.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except ValueError:
                pass
        rows = r.json() or []
        return len(rows)
    except Exception:  # noqa: BLE001
        log.exception("outbound._sent_today_count failed")
        return 0


async def _check_daily_cap() -> bool:
    """Return True if we're allowed to send another email today."""
    sent = await _sent_today_count()
    if sent >= OUTBOUND_DAILY_CAP:
        log.warning(
            "outbound: daily cap reached (%d / %d) — skipping send",
            sent, OUTBOUND_DAILY_CAP,
        )
        return False
    return True


async def _rate_limit_gate() -> None:
    """Block until at least OUTBOUND_RATE_LIMIT_SECONDS has elapsed since last send."""
    async with _rate_lock():
        elapsed = time.monotonic() - _LAST_SEND_AT["ts"]
        wait_for = OUTBOUND_RATE_LIMIT_SECONDS - elapsed
        if wait_for > 0:
            log.info("outbound: rate-limit sleeping %.1fs", wait_for)
            await asyncio.sleep(wait_for)
        _LAST_SEND_AT["ts"] = time.monotonic()


# ---------------------------------------------------------------------------
# Template loading + Claude personalisation
# ---------------------------------------------------------------------------


def _lang(prospect: dict) -> str:
    """Return 'pt' or 'en'. Anuvia is BR-first so default is 'pt'."""
    country = str(prospect.get("country") or "").lower().strip()
    if country in ("br", "brazil", "brasil"):
        return "pt"
    lang = str(prospect.get("language") or "").lower().strip()
    if lang.startswith("en"):
        return "en"
    if lang.startswith("pt"):
        return "pt"
    return "pt"


def _template_path(practice: str, touch_num: int, lang: str) -> Path:
    """Build `outbound/templates/{practice}_touch_{n}.{lang}.md` path."""
    fname = f"{practice}_touch_{touch_num}.{lang}.md"
    return _TEMPLATES_DIR / fname


def _read_template(path: Path) -> str:
    """Read a template file. Raises FileNotFoundError with a clear message."""
    if not path.exists():
        raise FileNotFoundError(
            f"outbound template not found: {path}. "
            "Did you provision outbound/templates/ for this practice + lang?"
        )
    return path.read_text(encoding="utf-8")


def _split_subject_body(rendered: str) -> tuple[str, str]:
    """Split rendered output into (subject, body).

    Templates and Claude responses use a leading `Subject: ...\\n\\n<body>`
    convention. If the marker is missing we fall back to a generic subject.
    """
    text = rendered.strip()
    m = re.match(r"^\s*subject\s*:\s*(.+?)\s*\n+(.*)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        subject = m.group(1).strip()
        body = m.group(2).strip()
        return subject, body
    return "", text


def _plain_to_html(plain: str) -> str:
    """Wrap plain-text email body in minimal email-friendly HTML.

    Email body style: very short, no marketing fluff. We use inline styles
    that match `lib/track_b.py` so the visual tone is consistent across the
    whole funnel.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", plain.strip()) if p.strip()]
    body_inner = "\n".join(
        f'<p style="color:#1a1a1a;line-height:1.65;font-size:15px;margin:0 0 16px 0;">'
        f'{p.replace(chr(10), "<br>")}</p>'
        for p in paragraphs
    )
    return (
        '<!DOCTYPE html><html><body style="background:#ffffff;'
        'font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif;'
        'color:#1a1a1a;margin:0;padding:24px;">'
        '<div style="max-width:560px;margin:0 auto;">'
        f'{body_inner}'
        '</div></body></html>'
    )


def _strip_html(html: str) -> str:
    """Crude HTML→text fallback used when only an HTML body is available."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _value_prop(practice: str, lang: str) -> str:
    """One-paragraph Anuvia value prop, keyed by practice + language.

    Used as a system-prompt anchor so Claude has a sharp positioning to riff
    on. Kept terse on purpose — the email body should be 3–5 sentences.
    """
    if lang == "pt":
        base = {
            "finops": (
                "Anuvia faz auditoria FinOps de AWS para empresas com gasto "
                "mensal acima de R$ 30k. Foco em EBS órfão, cobertura de "
                "Reserved Instances baixa e instâncias super-dimensionadas. "
                "Garantia: 3× ROI sobre o preço da auditoria em 90 dias."
            ),
            "ai": (
                "Anuvia ajuda times de produto a passar PoVs de IA da prova "
                "para produção. Foco em eval gates, observabilidade de LLM "
                "(latência, custo, drift) e arquitetura mínima viável para "
                "rodar agentes em produção sem incidentes."
            ),
        }
    else:
        base = {
            "finops": (
                "Anuvia runs FinOps audits on AWS for teams spending over "
                "USD 8k / month. Focus: orphaned EBS, under-utilised "
                "Reserved Instances, oversized EC2 / RDS. Guarantee: 3× ROI "
                "over the audit fee within 90 days."
            ),
            "ai": (
                "Anuvia helps product teams move AI PoVs from demo to "
                "production. Focus: eval gates, LLM observability (latency, "
                "cost, drift), and the minimum viable architecture to run "
                "agents in production without incidents."
            ),
        }
    return base.get(practice, "")


async def _call_claude(prompt: str, system: str) -> str:
    """One-shot call to Anthropic Messages API. Returns the model text."""
    if not ANTHROPIC_API_KEY:
        log.warning("outbound: ANTHROPIC_API_KEY unset — using raw template as body")
        # Fallback: return the user prompt as-is so callers can still send.
        return prompt

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 800,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if r.status_code >= 400:
        log.error(
            "outbound: anthropic non-200 status=%s body=%s",
            r.status_code, r.text[:300],
        )
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:200]}")
    body = r.json()
    blocks = body.get("content") or []
    parts: List[str] = []
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text") or "")
    return "\n".join(parts).strip()


async def render_personalized_email(
    prospect: dict,
    template_path: str,
    practice: str,
    touch_num: int,
) -> dict:
    """Render a personalised cold email for one prospect.

    Reads the template at `template_path` (or, if empty, derives the path
    from `practice` + `touch_num` + prospect language), feeds it to Claude
    with the prospect enrichment data + Anuvia value prop, and returns::

        {"subject": str, "html_body": str, "plain_body": str}

    The model is instructed to keep Mila's voice: direct, technical, no
    marketing fluff, no "hope this email finds you well", closing line is a
    single question or a single soft CTA, signature is `Anuvia · Mila
    Vernazza` (not personal bio).
    """
    lang = _lang(prospect)
    if template_path:
        tpath = Path(template_path)
        if not tpath.is_absolute():
            tpath = _TEMPLATES_DIR / tpath.name
    else:
        tpath = _template_path(practice, touch_num, lang)
    raw_template = _read_template(tpath)

    first_name = (prospect.get("first_name") or "").strip()
    company = (prospect.get("company") or "").strip()
    title = (prospect.get("title") or "").strip()
    vertical = (prospect.get("vertical") or "").strip()
    enriched = prospect.get("enriched_data") or {}
    if not isinstance(enriched, dict):
        try:
            enriched = json.loads(enriched) if isinstance(enriched, str) else {}
        except Exception:  # noqa: BLE001
            enriched = {}

    value_prop = _value_prop(practice, lang)

    if lang == "pt":
        system = (
            "Você é a Mila Vernazza, fundadora da Anuvia. Voz: direta, "
            "técnica, sem clichês de vendas. Nunca use exclamações. Nunca "
            "diga 'espero que esteja bem'. Cada email tem 3-5 frases no "
            "máximo. Cite UMA dor específica observada. A última linha é "
            "uma única pergunta OU um único CTA suave. Assinatura: "
            "'Anuvia · Mila Vernazza' (não escreva mini-bio). "
            "Formato de saída obrigatório:\n"
            "Subject: <linha de assunto curta, sem emojis, sem CAPS>\n\n"
            "<corpo do email em texto puro>"
        )
    else:
        system = (
            "You are Mila Vernazza, founder of Anuvia. Voice: direct, "
            "technical, no sales clichés. Never use exclamation marks. "
            "Never say 'hope this email finds you well'. Each email is 3-5 "
            "sentences max. Cite ONE specific observed pain. Closing line "
            "is a single question OR a single soft CTA. Signature: "
            "'Anuvia · Mila Vernazza' (no mini-bio). "
            "Required output format:\n"
            "Subject: <short subject line, no emojis, no CAPS>\n\n"
            "<plain-text email body>"
        )

    user_prompt = (
        f"Anuvia value prop ({practice}):\n{value_prop}\n\n"
        f"Prospect data:\n"
        f"  first_name: {first_name or '(unknown)'}\n"
        f"  title: {title or '(unknown)'}\n"
        f"  company: {company or '(unknown)'}\n"
        f"  vertical: {vertical or '(unknown)'}\n"
        f"  country: {prospect.get('country') or '(unknown)'}\n"
        f"  icp_score: {prospect.get('icp_score')}\n"
        f"  enriched_data: {json.dumps(enriched, ensure_ascii=False)[:2000]}\n\n"
        f"Template to personalise (touch #{touch_num}):\n"
        f"---\n{raw_template}\n---\n\n"
        f"Calendar URL: {CALENDAR_URL}\n"
        f"Personalise the template above. Fill placeholders like "
        f"{{first_name}}, {{company}}, {{specific_pain_observation}}, "
        f"{{calendar_url}}. Keep the template's structure and voice."
    )

    rendered = await _call_claude(user_prompt, system)
    subject, body = _split_subject_body(rendered)

    if not subject:
        # Last-resort fallback so we never send a blank subject.
        subject = (
            "Auditoria rápida" if lang == "pt" else "Quick audit thought"
        )
    if not body:
        body = rendered

    plain_body = body
    html_body = _plain_to_html(plain_body)
    return {
        "subject": subject,
        "html_body": html_body,
        "plain_body": plain_body,
    }


# ---------------------------------------------------------------------------
# Resend wrapper with retry
# ---------------------------------------------------------------------------


async def send_email_via_resend(
    to: str,
    subject: str,
    html: str,
    plain: str,
    tags: dict,
) -> dict:
    """POST one email to Resend. Returns `{message_id, status, attempts}`.

    `tags` is a dict like ``{"prospect_id": ..., "practice": ..., "touch_num":
    ..., "sequence_id": ...}``. We flatten it into Resend's tag-list shape
    plus add `category=outbound` so the inbound webhook in A4 can route.

    Retry policy: 2 attempts with 3 s backoff. On final failure raise
    `RuntimeError`. Caller is responsible for logging the failure to the
    prospect's agent_history.
    """
    if not RESEND_API_KEY:
        log.info(
            "outbound: RESEND_API_KEY unset — dry-run send to=%s subject=%s",
            to, subject,
        )
        return {"message_id": None, "status": "dry_run", "attempts": 0}

    tag_list = [{"name": "category", "value": "outbound"}]
    for k, v in (tags or {}).items():
        if v is None:
            continue
        # Resend tag values must be strings; keys must be [a-zA-Z0-9_-].
        safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(k))
        tag_list.append({"name": safe_key, "value": str(v)})

    payload: Dict[str, Any] = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [to],
        "reply_to": RESEND_REPLY_TO,
        "subject": subject,
        "html": html,
        "text": plain,
        "tags": tag_list,
    }

    last_err: Optional[str] = None
    for attempt in range(1, _RESEND_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if r.status_code >= 400:
                last_err = f"resend {r.status_code}: {r.text[:200]}"
                log.warning(
                    "outbound: resend attempt %d/%d failed status=%s body=%s",
                    attempt, _RESEND_RETRIES, r.status_code, r.text[:300],
                )
                if attempt < _RESEND_RETRIES:
                    await asyncio.sleep(_RESEND_BACKOFF_S)
                    continue
                raise RuntimeError(last_err)
            body = r.json() if r.text else {}
            msg_id = body.get("id") if isinstance(body, dict) else None
            log.info(
                "outbound: resend ok to=%s subject=%r msg_id=%s attempt=%d",
                to, subject, msg_id, attempt,
            )
            return {
                "message_id": msg_id,
                "status": "sent",
                "attempts": attempt,
            }
        except httpx.HTTPError as exc:
            last_err = f"httpx: {exc}"
            log.warning(
                "outbound: resend attempt %d/%d network error: %s",
                attempt, _RESEND_RETRIES, exc,
            )
            if attempt < _RESEND_RETRIES:
                await asyncio.sleep(_RESEND_BACKOFF_S)
                continue
            raise RuntimeError(last_err) from exc

    raise RuntimeError(last_err or "resend failed for unknown reason")


# ---------------------------------------------------------------------------
# Sequence kickoff
# ---------------------------------------------------------------------------


async def _send_touch(
    prospect: dict,
    practice: str,
    sequence_id: str,
    touch_num: int,
) -> dict:
    """Render + send one touch. Updates prospect row on success/failure.

    Returns `{message_id, sent_at, status}`. Honours the daily cap and the
    inter-send rate limit. On retryable failure we still update the prospect
    with an agent_history entry so the next tick can decide what to do.
    """
    prospect_id = str(prospect.get("id") or "")
    email = prospect.get("email") or ""
    if not email:
        raise RuntimeError(f"prospect {prospect_id} has no email")

    if not await _check_daily_cap():
        # Treat as soft failure: leave prospect where it is, schedule retry.
        await _prospect_append_history(
            prospect_id,
            agent="outbound",
            action=f"touch_{touch_num}",
            result="skipped",
            detail="daily_cap_reached",
        )
        raise RuntimeError("outbound daily cap reached")

    rendered = await render_personalized_email(
        prospect, template_path="", practice=practice, touch_num=touch_num
    )

    await _rate_limit_gate()

    tags = {
        "prospect_id": prospect_id,
        "practice": practice,
        "touch_num": touch_num,
        "sequence_id": sequence_id,
    }
    started = time.monotonic()
    try:
        send_result = await send_email_via_resend(
            to=email,
            subject=rendered["subject"],
            html=rendered["html_body"],
            plain=rendered["plain_body"],
            tags=tags,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        await _prospect_append_history(
            prospect_id,
            agent="outbound",
            action=f"touch_{touch_num}",
            result="failed",
            detail=f"send error: {exc}",
            error=str(exc),
            latency_ms=latency_ms,
        )
        raise

    latency_ms = int((time.monotonic() - started) * 1000)
    sent_at = _now()
    await _prospect_append_history(
        prospect_id,
        agent="outbound",
        action=f"touch_{touch_num}",
        result="ok",
        detail=f"sent via resend (msg_id={send_result.get('message_id')})",
        latency_ms=latency_ms,
    )
    return {
        "message_id": send_result.get("message_id"),
        "sent_at": sent_at,
        "status": send_result.get("status"),
        "subject": rendered["subject"],
    }


async def send_outbound_sequence(
    prospect: dict,
    practice: str,
    sequence_id: str = "v1",
) -> dict:
    """Kick off a 3-touch sequence for `prospect`.

    Touch 1 sent immediately via Resend. Touches 2 and 3 queued via
    `_prospect_set_next` (mirror of `session_set_next` on the prospects
    table). Sets `prospects.status='sequence_running'`.

    Returns::

        {"sequence_id", "prospect_id", "first_touch_at", "message_id"}

    Idempotency: if the prospect already has `current_touch >= 1` we skip
    the first send to avoid double-emailing on retry.
    """
    prospect_id = str(prospect.get("id") or "")
    if not prospect_id:
        raise ValueError("send_outbound_sequence: prospect missing id")

    # Re-fetch to read fresh `current_touch` for idempotency.
    fresh = await _prospect_get(prospect_id) or prospect

    if int(fresh.get("current_touch") or 0) >= 1:
        log.info(
            "outbound.send_outbound_sequence: prospect %s already on touch %s — skipping kickoff",
            prospect_id, fresh.get("current_touch"),
        )
        return {
            "sequence_id": sequence_id,
            "prospect_id": prospect_id,
            "first_touch_at": fresh.get("updated_at"),
            "message_id": None,
        }

    # Hard-stop guards: don't re-engage prospects we shouldn't.
    status = (fresh.get("status") or "").lower()
    if status in ("unsubscribed", "bounced", "complained", "converted"):
        raise RuntimeError(
            f"prospect {prospect_id} is {status} — refusing to send"
        )

    result = await _send_touch(fresh, practice, sequence_id, touch_num=1)

    # Schedule touch 2 for d+3 from now.
    touch_2_at = _now() + timedelta(days=3)
    await _prospect_set_next(prospect_id, "outbound_touch_2", touch_2_at)
    await _prospect_update(
        prospect_id,
        status="sequence_running",
        current_touch=1,
        sequence_id=sequence_id,
        practice_fit=practice,
    )

    return {
        "sequence_id": sequence_id,
        "prospect_id": prospect_id,
        "first_touch_at": result["sent_at"].isoformat() if result.get("sent_at") else None,
        "message_id": result.get("message_id"),
    }


# ---------------------------------------------------------------------------
# Handlers — invoked by the orchestrator (or A5's prospect-due loop)
# ---------------------------------------------------------------------------


def _engagement_detected(prospect: dict, since_iso: Optional[str] = None) -> bool:
    """True iff `prospects.last_engaged_at` is more recent than `since_iso`.

    Per spec: "fires 3 days after touch 1 IF no reply detected (check
    prospects.last_engaged_at)". Engagement is set by A4's reply
    classifier when an open/click/reply arrives.
    """
    last = prospect.get("last_engaged_at")
    if not last:
        return False
    if since_iso is None:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        since_dt = datetime.fromisoformat(str(since_iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    return last_dt >= since_dt


def _coerce_prospect(lead: dict) -> dict:
    """Resolve the prospect row from a handler argument.

    The orchestrator's contract is "handler takes a lead dict". For outbound
    we may be called either:
      * with a real prospect row (from A5's prospects-due polling), OR
      * with a leads-shaped dict carrying `prospect_id` in `enriched_data`.

    We look in the obvious places and return whichever we can resolve.
    """
    if lead.get("id") and "current_touch" in lead:
        return lead  # looks like a prospects row
    if lead.get("prospect_id"):
        return {**lead, "id": lead["prospect_id"]}
    # leads-shaped row with prospect_id stashed in qualification_data
    qd = lead.get("qualification_data") or {}
    if isinstance(qd, dict) and qd.get("prospect_id"):
        return {**lead, "id": qd["prospect_id"]}
    return lead


@register("outbound_touch_2")
async def h_outbound_touch_2(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Send touch 2 (d+3 after touch 1) unless the prospect engaged."""
    shell = _coerce_prospect(lead)
    prospect_id = str(shell.get("id") or "")
    fresh = await _prospect_get(prospect_id)
    if not fresh:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": None,
            "detail": f"prospect {prospect_id} not found",
        }

    if _engagement_detected(fresh):
        await _prospect_set_next(prospect_id, None, None)
        return {
            "next_action": None,
            "next_action_at": None,
            "detail": "engaged — sequence paused",
        }

    practice = fresh.get("practice_fit") or "finops"
    sequence_id = fresh.get("sequence_id") or "v1"

    try:
        await _send_touch(fresh, practice, sequence_id, touch_num=2)
    except Exception as exc:  # noqa: BLE001
        # Re-raise so the orchestrator retry kicks in.
        log.warning("outbound.touch_2 send failed for %s: %s", prospect_id, exc)
        raise

    touch_3_at = _now() + timedelta(days=3)
    await _prospect_set_next(prospect_id, "outbound_touch_3", touch_3_at)
    await _prospect_update(prospect_id, current_touch=2)

    return {
        "next_action": "outbound_touch_3",
        "next_action_at": touch_3_at,
        "status": "sequence_running",
        "detail": "touch 2 sent",
    }


@register("outbound_touch_3")
async def h_outbound_touch_3(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Send touch 3 (d+3 after touch 2). Final touch. Queues outbound_stop @ d+7."""
    shell = _coerce_prospect(lead)
    prospect_id = str(shell.get("id") or "")
    fresh = await _prospect_get(prospect_id)
    if not fresh:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": None,
            "detail": f"prospect {prospect_id} not found",
        }

    if _engagement_detected(fresh):
        await _prospect_set_next(prospect_id, None, None)
        return {
            "next_action": None,
            "next_action_at": None,
            "detail": "engaged — sequence paused",
        }

    practice = fresh.get("practice_fit") or "finops"
    sequence_id = fresh.get("sequence_id") or "v1"

    try:
        await _send_touch(fresh, practice, sequence_id, touch_num=3)
    except Exception as exc:  # noqa: BLE001
        log.warning("outbound.touch_3 send failed for %s: %s", prospect_id, exc)
        raise

    stop_at = _now() + timedelta(days=7)
    await _prospect_set_next(prospect_id, "outbound_stop", stop_at)
    await _prospect_update(prospect_id, current_touch=3)

    return {
        "next_action": "outbound_stop",
        "next_action_at": stop_at,
        "status": "sequence_running",
        "detail": "touch 3 (final) sent",
    }


@register("outbound_stop")
async def h_outbound_stop(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Terminal state — no reply after 3 touches in ~10 days. Mark stopped."""
    shell = _coerce_prospect(lead)
    prospect_id = str(shell.get("id") or "")
    fresh = await _prospect_get(prospect_id)
    if not fresh:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": None,
            "detail": f"prospect {prospect_id} not found",
        }

    # If engagement appeared during the 7-day cool-off, don't stamp 'stopped'.
    if _engagement_detected(fresh):
        await _prospect_set_next(prospect_id, None, None)
        return {
            "next_action": None,
            "next_action_at": None,
            "detail": "engaged during cooldown — leaving sequence to A4",
        }

    await _prospect_update(prospect_id, status="stopped")
    await _prospect_set_next(prospect_id, None, None)
    await _prospect_append_history(
        prospect_id,
        agent="outbound",
        action="stop",
        result="ok",
        detail="3-touch sequence exhausted with no reply",
    )

    return {
        "next_action": None,
        "next_action_at": None,
        "status": "stopped",
        "detail": "sequence exhausted — no reply",
    }


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "send_outbound_sequence",
    "render_personalized_email",
    "send_email_via_resend",
    "h_outbound_touch_2",
    "h_outbound_touch_3",
    "h_outbound_stop",
]
