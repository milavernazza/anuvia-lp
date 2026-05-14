"""Track B — autonomous-close path for small Growth leads.

This module owns the funnel branch where Anuvia closes a deal without Mila
ever joining a discovery call. The orchestrator dispatches by string key, so
all of the work is registered through `@register(...)` decorators from
`lib.orchestrator`.

Flow (all handlers async, all retry-safe, all idempotent):

    classify_track  -> generate_proposal_v1  (after 15 min)
                       |
                       v
                    followup_proposal_d2  (after 2 days)
                       |
                       v
                    followup_proposal_d5  (after 3 days)
                       |
                       v
                    close_ghosted_d10     (after 5 days)

Engagement signals (email_open, email_click, reply, proposal_view) collected
via Resend webhooks and the `/accept` endpoint pause the autopilot — once a
human shows up, Mila takes over.

Quality bar (per ARCHITECTURE_AUTONOMOUS_v1.md §5):
  * Append-only writes via lib.sessions helpers (never overwrite jsonb).
  * Idempotent on retry: artifacts are checked before re-sending.
  * Network failures bubble up so the orchestrator can retry with backoff.
  * Bilingual: PT and EN copy hand-written, not machine-translated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from lib.orchestrator import register
from lib.sessions import (
    session_append_artifact,
    session_append_history,
    session_append_signal,
    session_get,
    session_set_next,
    session_set_status,
    session_update,
)

log = logging.getLogger("anuvia-lp.track_b")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")

# Public host where the rendered proposal will be served (anchored on the
# brand domain so the lead clicks a familiar URL, not the LP host).
PROPOSAL_HOST_PT = os.environ.get("PROPOSAL_HOST_PT", "https://anuvia.com.br")
PROPOSAL_HOST_EN = os.environ.get("PROPOSAL_HOST_EN", "https://anuvia.net")

# Where rendered proposals land on disk. We resolve relative to the app
# package so behaviour matches the existing /static mount.
_PROPOSALS_DIR = Path(__file__).resolve().parent.parent / "static" / "proposals"

# HTTP timeouts kept short — orchestrator wraps us in retry/backoff.
_HTTP_TIMEOUT = 30.0


def _now() -> datetime:
    """Return tz-aware UTC now. Centralised for testability."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# classify_track — pure function, no I/O
# ---------------------------------------------------------------------------


def classify_track(lead: dict) -> str:
    """Decide whether this lead goes through the autonomous-close path.

    Returns ``'autonomous'`` if and only if:
      * ``funnel_id`` starts with ``BR_GROWTH`` or ``US_GROWTH`` (case-insens.), AND
      * at least 2 of these qualification signals are true:
          - budget signal: ``budget_declared == True`` OR a numeric ``budget_amount``
          - urgency signal: ``urgency in {'high', 'urgent', 'this_quarter'}``
          - size signal:   ``team_size`` is a number <= 10, OR
                           ``company_size`` is one of {'SMB','small','1-10','11-50'}

    Otherwise returns ``'discovery'``. Never raises — type errors / missing
    keys silently fall through to ``'discovery'`` so a malformed row can't
    crash the dispatcher.
    """
    try:
        funnel_id = (lead.get("funnel_id") or "").upper().strip()
    except Exception:  # noqa: BLE001
        return "discovery"

    if not (funnel_id.startswith("BR_GROWTH") or funnel_id.startswith("US_GROWTH")):
        return "discovery"

    qd = lead.get("qualification_data") or {}
    if not isinstance(qd, dict):
        # Some Supabase clients hand jsonb back as a string.
        try:
            qd = json.loads(qd) if isinstance(qd, str) else {}
            if not isinstance(qd, dict):
                qd = {}
        except Exception:  # noqa: BLE001
            qd = {}

    signals = 0

    # Budget signal -----------------------------------------------------------
    budget_declared = qd.get("budget_declared")
    if budget_declared is True or str(budget_declared).lower() == "true":
        signals += 1
    else:
        amt = qd.get("budget_amount")
        try:
            if amt is not None and float(amt) > 0:
                signals += 1
        except (TypeError, ValueError):
            pass

    # Urgency signal ----------------------------------------------------------
    urgency = str(qd.get("urgency") or "").lower().strip()
    if urgency in ("high", "urgent", "this_quarter"):
        signals += 1

    # Size signal -------------------------------------------------------------
    size_hit = False
    team_size = qd.get("team_size")
    try:
        if team_size is not None and float(team_size) <= 10:
            size_hit = True
    except (TypeError, ValueError):
        pass
    if not size_hit:
        company_size = str(qd.get("company_size") or "").lower().strip()
        if company_size in ("smb", "small", "1-10", "11-50"):
            size_hit = True
    if size_hit:
        signals += 1

    return "autonomous" if signals >= 2 else "discovery"


# ---------------------------------------------------------------------------
# Helpers — language, pricing, HMAC, artifact lookup, HTTP wrappers
# ---------------------------------------------------------------------------


def _lang(lead: dict) -> str:
    """Return 'pt' or 'en'. Defaults to 'pt' (Anuvia is BR-first)."""
    lang = str(lead.get("language") or "").lower().strip()
    return "en" if lang.startswith("en") else "pt"


def _proposal_host(lang: str) -> str:
    return PROPOSAL_HOST_EN if lang == "en" else PROPOSAL_HOST_PT


def _pricing(lead: dict, lang: str) -> dict:
    """Derive proposal pricing.

    Pulls from ``qualification_data`` when the LP captured an explicit number;
    otherwise falls back to Growth defaults (R$ 8.000 / US$ 1.500 retainer).
    Returned dict shape::

        {
            "currency_symbol": "R$" | "US$",
            "retainer":        "8.000" | "1,500",
            "setup":           "4.000" | "750",
            "period_label":    "/mês"  | "/month",
        }
    """
    qd = lead.get("qualification_data") or {}
    if not isinstance(qd, dict):
        qd = {}

    if lang == "en":
        defaults = {
            "currency_symbol": "US$",
            "retainer": "1,500",
            "setup": "750",
            "period_label": "/month",
        }
    else:
        defaults = {
            "currency_symbol": "R$",
            "retainer": "8.000",
            "setup": "4.000",
            "period_label": "/mês",
        }

    # If the LP captured an explicit budget_amount, honour it as the retainer.
    amt = qd.get("budget_amount")
    try:
        if amt is not None and float(amt) > 0:
            n = float(amt)
            if lang == "en":
                defaults["retainer"] = f"{int(n):,}"
                defaults["setup"] = f"{int(n / 2):,}"
            else:
                # Brazilian number format: thousand-separator '.' (period).
                defaults["retainer"] = f"{int(n):,}".replace(",", ".")
                defaults["setup"] = f"{int(n / 2):,}".replace(",", ".")
    except (TypeError, ValueError):
        pass

    return defaults


def _accept_token(lead_id: str) -> str:
    """HMAC-SHA256 of the lead id. Returned as hex; verified on /accept."""
    secret = os.environ.get("TRACK_B_HMAC_SECRET", "")
    if not secret:
        # We intentionally do NOT fall back to a hard-coded default — if the
        # secret is missing the email link is unverifiable and that's correct.
        log.warning("track_b: TRACK_B_HMAC_SECRET is unset; tokens will be empty")
        return ""
    return hmac.new(
        secret.encode("utf-8"),
        lead_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_accept_token(lead_id: str, token: str) -> bool:
    """Constant-time compare for the /accept HMAC token."""
    if not lead_id or not token:
        return False
    expected = _accept_token(lead_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def _has_proposal_v1(lead: dict) -> bool:
    """True iff `artifacts` already contains a proposal_pdf with version==1."""
    artifacts = lead.get("artifacts") or []
    if not isinstance(artifacts, list):
        return False
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "proposal_pdf":
            continue
        meta = a.get("meta") or {}
        if isinstance(meta, dict) and meta.get("version") == 1:
            return True
    return False


def _proposal_sent_ts(lead: dict) -> Optional[datetime]:
    """Return the ts of the first proposal_pdf artifact, or None."""
    for a in lead.get("artifacts") or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "proposal_pdf":
            continue
        ts = a.get("ts")
        if not ts:
            continue
        try:
            # Tolerate both 'Z' suffix and explicit +00:00.
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _has_engagement_since(lead: dict, since: Optional[datetime]) -> bool:
    """True iff signals contains an engagement kind after `since`.

    Engagement = email_open | email_click | reply | proposal_view. If `since`
    is None we consider any engagement signal at all.
    """
    relevant = {"email_open", "email_click", "reply", "proposal_view", "proposal_accepted"}
    for s in lead.get("signals") or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") not in relevant:
            continue
        if since is None:
            return True
        ts = s.get("ts")
        if not ts:
            continue
        try:
            ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts_dt >= since:
            return True
    return False


async def _render_pdf_via_gotenberg(html: str, out_path: Path) -> bool:
    """POST `html` to Gotenberg, write the PDF to `out_path`. Returns success.

    Never raises — Gotenberg outages must not kill the proposal send (we fall
    back to the HTML link). The caller decides whether the PDF was produced
    based on the boolean return.
    """
    endpoint = f"{GOTENBERG_URL}/forms/chromium/convert/html"
    try:
        files = {
            "files": ("index.html", html.encode("utf-8"), "text/html"),
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(endpoint, files=files)
        if r.status_code != 200:
            log.warning(
                "track_b: gotenberg non-200 status=%s body=%s",
                r.status_code, r.text[:200],
            )
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return True
    except Exception as exc:  # noqa: BLE001 — Gotenberg is optional
        log.warning("track_b: gotenberg call failed: %s", exc)
        return False


async def _send_email(
    to: str,
    subject: str,
    html: str,
    *,
    lead_id: str,
    kind: str,
) -> Optional[str]:
    """Send an email via Resend. Returns the resend message id on success.

    Raises on network/HTTP failure so the orchestrator retry kicks in. Skips
    silently (returning None) when RESEND_API_KEY is unset — useful for tests
    and local dev where we don't want to spam real inboxes.
    """
    if not RESEND_API_KEY:
        log.info(
            "track_b: RESEND_API_KEY unset; dry-run send kind=%s to=%s subject=%s",
            kind, to, subject,
        )
        return None

    payload: Dict[str, Any] = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [to],
        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "subject": subject,
        "html": html,
        "tags": [
            {"name": "category", "value": "track_b"},
            {"name": "kind", "value": kind},
            {"name": "lead_id", "value": lead_id},
        ],
    }
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
        log.error(
            "track_b: resend failed kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")
    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info("track_b: resend ok kind=%s lead=%s msg_id=%s", kind, lead_id, msg_id)
    return msg_id


# ---------------------------------------------------------------------------
# Proposal HTML — kept compact, inline styles, email-friendly
# ---------------------------------------------------------------------------


def _proposal_html(lead: dict) -> str:
    """Render a bilingual one-pager. <80 lines of inline-styled HTML."""
    lang = _lang(lead)
    pricing = _pricing(lead, lang)
    name = (lead.get("name") or "").split(" ")[0] or ("there" if lang == "en" else "olá")
    company = lead.get("company") or ""

    if lang == "en":
        title = "Anuvia · Growth proposal"
        intro = (
            f"Hi {name}, here's the proposal we discussed. "
            "Built for fast-moving teams that want senior cloud + AI execution "
            "without hiring a full platform squad."
        )
        scope_hdr = "What you get"
        scope = [
            "Senior Solutions Architect (ex-AWS, ex-Google) on retainer",
            "Up to 20 hours / month of hands-on engineering and review",
            "Architecture, FinOps and AI-readiness deliverables in writing",
            "Slack + async response within 24h business hours",
        ]
        price_hdr = "Investment"
        price_line = (
            f"{pricing['currency_symbol']} {pricing['retainer']}{pricing['period_label']} "
            f"retainer · one-time setup {pricing['currency_symbol']} {pricing['setup']}"
        )
        terms = "3-month minimum, then month-to-month. Cancel anytime with 30 days notice."
        cta = "Accept proposal"
        sig = "Mila Vernazza · Founder Anuvia"
        footer = "Ex-AWS Solutions Architect · Ex-Google · 15+ AWS certifications"
    else:
        title = "Anuvia · Proposta Growth"
        intro = (
            f"Olá {name}, segue a proposta que conversamos. "
            "Feita para times que precisam de execução sênior em cloud + IA "
            "sem montar uma squad de plataforma do zero."
        )
        scope_hdr = "O que está incluso"
        scope = [
            "Solutions Architect sênior (ex-AWS, ex-Google) em retainer",
            "Até 20 horas / mês de execução e revisão hands-on",
            "Entregáveis escritos: arquitetura, FinOps e prontidão para IA",
            "Atendimento via Slack + async em até 24h úteis",
        ]
        price_hdr = "Investimento"
        price_line = (
            f"{pricing['currency_symbol']} {pricing['retainer']}{pricing['period_label']} "
            f"de retainer · setup único {pricing['currency_symbol']} {pricing['setup']}"
        )
        terms = "Mínimo 3 meses, depois mensal. Cancelamento com 30 dias de aviso."
        cta = "Aceitar proposta"
        sig = "Mila Vernazza · Founder Anuvia"
        footer = "Ex-Solutions Architect AWS · Ex-Google · 15+ certificações AWS"

    company_line = (
        f'<p style="color:#78716c;font-size:13px;margin:0 0 8px 0;">'
        f'{("Prepared for" if lang == "en" else "Preparada para")}: {company}</p>'
        if company
        else ""
    )

    lead_id = str(lead.get("id") or "")
    token = _accept_token(lead_id)
    accept_url = f"{PROPOSAL_HOST_PT}/api/track-b/accept?lead_id={lead_id}&token={token}"

    bullets = "".join(
        f'<li style="margin:6px 0;color:#1a1a1a;line-height:1.5;">{item}</li>'
        for item in scope
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:40px 36px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 4px 0;">{title}</p>
{company_line}
<h1 style="font-family:Georgia,serif;font-size:30px;margin:0 0 18px 0;color:#0f172a;">{("Growth retainer" if lang == "en" else "Retainer Growth")}</h1>
<p style="color:#475569;line-height:1.65;margin:0 0 24px 0;">{intro}</p>
<h2 style="font-size:14px;letter-spacing:0.08em;text-transform:uppercase;color:#0f172a;margin:24px 0 8px 0;">{scope_hdr}</h2>
<ul style="padding-left:20px;margin:0 0 24px 0;">{bullets}</ul>
<h2 style="font-size:14px;letter-spacing:0.08em;text-transform:uppercase;color:#0f172a;margin:24px 0 8px 0;">{price_hdr}</h2>
<p style="font-family:Georgia,serif;font-size:20px;color:#0f172a;margin:0 0 6px 0;">{price_line}</p>
<p style="color:#78716c;font-size:13px;margin:0 0 28px 0;">{terms}</p>
<p style="margin:32px 0;"><a href="{accept_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;">{cta} -></a></p>
<hr style="border:none;border-top:1px solid #e7e5e4;margin:32px 0 16px 0;">
<p style="color:#78716c;font-size:13px;margin:0;">{sig}<br><span style="color:#a8a29e;">{footer}</span></p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@register("classify_track")
async def h_classify_track(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Decide the lead's track and schedule the next step.

    Autonomous leads get a 15-minute breather before the proposal lands —
    enough for the LP confirmation page to settle and any human review to
    bail out. Discovery leads simply wait for the booking widget.
    """
    lead_id = str(lead.get("id") or "")
    track = classify_track(lead)

    try:
        await session_update(lead_id, track=track)
    except Exception:
        log.exception("track_b.classify_track: session_update failed lead=%s", lead_id)
        raise

    if track == "autonomous":
        return {
            "next_action": "generate_proposal_v1",
            "next_action_at": _now() + timedelta(minutes=15),
            "status": "qualified",
            "detail": "autonomous track",
        }

    return {
        "next_action": None,
        "next_action_at": None,
        "status": "in_discovery",
        "detail": "discovery track — waiting on booking",
    }


@register("generate_proposal_v1")
async def h_generate_proposal_v1(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Render proposal HTML/PDF, email it, file the artifact.

    Idempotency: if `artifacts` already shows a proposal_pdf with version==1
    we assume the orchestrator is retrying after a partial failure and skip
    the send to avoid double-emailing. The next-action contract is still
    returned so the caller sees consistent scheduling output.
    """
    lead_id = str(lead.get("id") or "")
    lang = _lang(lead)

    # Re-fetch the lead so idempotency check sees fresh artifacts. The
    # orchestrator passes us the row from `session_due`, which is at most a
    # tick old — but a partial retry might have already filed the artifact.
    fresh = await session_get(lead_id) or lead
    if _has_proposal_v1(fresh):
        log.info("track_b.generate_proposal_v1: proposal_v1 already sent lead=%s; skipping", lead_id)
        return {
            "next_action": "followup_proposal_d2",
            "next_action_at": _now() + timedelta(days=2),
            "status": "proposal_sent",
            "detail": "proposal v1 already on file (idempotent skip)",
        }

    html = _proposal_html(fresh)

    # Persist the HTML version to /static/proposals so the email link works
    # even if Gotenberg is down.
    html_path = _PROPOSALS_DIR / f"{lead_id}_v1.html"
    pdf_path = _PROPOSALS_DIR / f"{lead_id}_v1.pdf"
    try:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        log.exception("track_b.generate_proposal_v1: html write failed lead=%s", lead_id)
        raise

    pdf_ok = await _render_pdf_via_gotenberg(html, pdf_path)

    # Hosted URL — prefer PDF, fall back to HTML.
    host = _proposal_host(lang)
    if pdf_ok:
        hosted_url = f"{host}/static/proposals/{lead_id}_v1.pdf"
    else:
        hosted_url = f"{host}/static/proposals/{lead_id}_v1.html"

    # Email copy ---------------------------------------------------------------
    token = _accept_token(lead_id)
    accept_url = f"{PROPOSAL_HOST_PT}/api/track-b/accept?lead_id={lead_id}&token={token}"
    name = (fresh.get("name") or "").split(" ")[0]

    if lang == "en":
        subject = "Your Anuvia Growth proposal"
        body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Proposal</p>
<p style="font-family:Georgia,serif;font-size:28px;margin:0 0 16px 0;">Hi {name or 'there'},</p>
<p style="color:#475569;line-height:1.65;">Your Growth retainer proposal is ready. Quick read — three minutes, written, no slide deck.</p>
<p style="margin:24px 0;"><a href="{hosted_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Read the proposal -></a></p>
<p style="color:#475569;line-height:1.65;">Ready to start? One click below and we kick off this week.</p>
<p style="margin:24px 0;"><a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Accept proposal -></a></p>
<p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Reply to this email if anything is unclear — I read every one.</p>
</div></body></html>"""
    else:
        subject = "Sua proposta Anuvia Growth"
        body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Proposta</p>
<p style="font-family:Georgia,serif;font-size:28px;margin:0 0 16px 0;">Olá {name or 'tudo bem'},</p>
<p style="color:#475569;line-height:1.65;">Sua proposta de retainer Growth está pronta. Leitura curta — três minutos, escrita, sem slide.</p>
<p style="margin:24px 0;"><a href="{hosted_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Ler a proposta -></a></p>
<p style="color:#475569;line-height:1.65;">Topa começar? Um clique abaixo e a gente arranca essa semana.</p>
<p style="margin:24px 0;"><a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Aceitar proposta -></a></p>
<p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Responde esse email se algo estiver confuso — leio todos.</p>
</div></body></html>"""

    to = fresh.get("email")
    if not to:
        raise RuntimeError(f"track_b.generate_proposal_v1: lead {lead_id} has no email")

    msg_id = await _send_email(
        to=to,
        subject=subject,
        html=body_html,
        lead_id=lead_id,
        kind="proposal_v1",
    )

    # File the artifact AFTER the send so a network blow-up doesn't poison
    # the idempotency check on retry.
    try:
        await session_append_artifact(
            lead_id,
            type="proposal_pdf",
            url=hosted_url,
            meta={
                "version": 1,
                "pdf_rendered": pdf_ok,
                "lang": lang,
                "resend_message_id": msg_id,
            },
        )
        await session_append_artifact(
            lead_id,
            type="email_sent",
            url=None,
            meta={"kind": "proposal_v1", "resend_message_id": msg_id, "lang": lang},
        )
    except Exception:
        log.exception("track_b.generate_proposal_v1: artifact append failed lead=%s", lead_id)
        raise

    return {
        "next_action": "followup_proposal_d2",
        "next_action_at": _now() + timedelta(days=2),
        "status": "proposal_sent",
        "detail": "proposal v1 sent",
    }


async def _send_nudge_and_schedule(
    lead: Dict[str, Any],
    *,
    kind: str,
    next_action: Optional[str],
    next_delay: timedelta,
    next_status: Optional[str] = None,
    detail: str,
) -> Dict[str, Any]:
    """Shared body for the d2 and d5 nudges.

    Engagement bail-out: if any engagement signal landed since the proposal
    was sent, we stop the autopilot and let Mila handle the warm lead.
    """
    lead_id = str(lead.get("id") or "")
    lang = _lang(lead)

    fresh = await session_get(lead_id) or lead
    sent_ts = _proposal_sent_ts(fresh)
    if _has_engagement_since(fresh, sent_ts):
        log.info(
            "track_b.%s: engagement detected lead=%s; pausing autopilot",
            kind, lead_id,
        )
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "proposal_opened",
            "detail": "engagement detected, pausing autopilot",
        }

    name = (fresh.get("name") or "").split(" ")[0]
    host = _proposal_host(lang)
    # Re-derive hosted URL — match whichever file we wrote at send time.
    pdf_path = _PROPOSALS_DIR / f"{lead_id}_v1.pdf"
    if pdf_path.exists():
        hosted_url = f"{host}/static/proposals/{lead_id}_v1.pdf"
    else:
        hosted_url = f"{host}/static/proposals/{lead_id}_v1.html"

    if kind == "nudge_d2":
        if lang == "en":
            subject = "Quick nudge on the Anuvia proposal"
            body = (
                f"Hi {name or 'there'} — just bubbling the proposal back up. "
                "If now isn't the right week, tell me when is and I'll hold the slot."
            )
        else:
            subject = "Voltando aqui sobre a proposta Anuvia"
            body = (
                f"Oi {name or 'tudo bem'} — só subindo a proposta. "
                "Se essa semana não é a hora, me diz quando é e eu seguro a vaga."
            )
    else:  # nudge_d5
        if lang == "en":
            subject = "Last check-in on the Anuvia proposal"
            body = (
                f"Hi {name or 'there'} — last check-in from me. "
                "If the timing is off, no worries; I'll close the file and you can ping me when ready."
            )
        else:
            subject = "Último toque na proposta Anuvia"
            body = (
                f"Oi {name or 'tudo bem'} — último contato meu. "
                "Se o timing não bate, sem problema — fecho o arquivo e tu me chama quando fizer sentido."
            )

    body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:560px;margin:0 auto;">
<p style="color:#475569;line-height:1.65;font-size:16px;">{body}</p>
<p style="margin:24px 0;"><a href="{hosted_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">
{("Re-open the proposal" if lang == "en" else "Reabrir a proposta")} -></a></p>
<p style="color:#78716c;font-size:13px;margin-top:24px;">Mila Vernazza · Anuvia</p>
</div></body></html>"""

    to = fresh.get("email")
    if not to:
        raise RuntimeError(f"track_b.{kind}: lead {lead_id} has no email")

    msg_id = await _send_email(
        to=to,
        subject=subject,
        html=body_html,
        lead_id=lead_id,
        kind=kind,
    )

    try:
        await session_append_artifact(
            lead_id,
            type="email_sent",
            url=None,
            meta={"kind": kind, "resend_message_id": msg_id, "lang": lang},
        )
    except Exception:
        log.exception("track_b.%s: artifact append failed lead=%s", kind, lead_id)
        raise

    return {
        "next_action": next_action,
        "next_action_at": (_now() + next_delay) if next_action else None,
        "status": next_status,
        "detail": detail,
    }


@register("followup_proposal_d2")
async def h_followup_proposal_d2(lead: Dict[str, Any]) -> Dict[str, Any]:
    """2-day nudge. Bails out if the lead has shown any engagement."""
    return await _send_nudge_and_schedule(
        lead,
        kind="nudge_d2",
        next_action="followup_proposal_d5",
        next_delay=timedelta(days=3),
        next_status=None,
        detail="d2 nudge sent",
    )


@register("followup_proposal_d5")
async def h_followup_proposal_d5(lead: Dict[str, Any]) -> Dict[str, Any]:
    """5-day final nudge. Same engagement bail-out as d2."""
    return await _send_nudge_and_schedule(
        lead,
        kind="nudge_d5",
        next_action="close_ghosted_d10",
        next_delay=timedelta(days=5),
        next_status=None,
        detail="d5 final nudge sent",
    )


@register("close_ghosted_d10")
async def h_close_ghosted_d10(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Terminal state for unresponsive autonomous leads."""
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "ghosted",
        "detail": "closed as ghosted after 10 days no response",
    }


# ---------------------------------------------------------------------------
# HTTP router — Resend webhook + /accept
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/track-b", tags=["track-b"])


# Map Resend event names to our internal signal kinds.
_RESEND_EVENT_MAP = {
    "email.opened": "email_open",
    "email.clicked": "email_click",
    "email.delivered": "email_delivered",
    "email.bounced": "email_bounced",
    "email.complained": "email_complained",
    "email.replied": "reply",
}


def _verify_resend_signature(body: bytes, headers: dict) -> bool:
    """Verify Resend's HMAC signature when RESEND_WEBHOOK_SECRET is set.

    Resend uses the `svix-id`, `svix-timestamp`, `svix-signature` headers
    (Resend is built on Svix). Signature format: `v1,<base64>`; payload to
    sign is `<id>.<timestamp>.<body>`. If the secret isn't configured we
    return True (the operator opted out of verification).
    """
    if not RESEND_WEBHOOK_SECRET:
        return True
    svix_id = headers.get("svix-id") or headers.get("Svix-Id")
    svix_ts = headers.get("svix-timestamp") or headers.get("Svix-Timestamp")
    svix_sig = headers.get("svix-signature") or headers.get("Svix-Signature")
    if not (svix_id and svix_ts and svix_sig):
        return False
    try:
        secret = RESEND_WEBHOOK_SECRET
        # Svix secrets are prefixed with "whsec_"; the raw key is base64.
        if secret.startswith("whsec_"):
            import base64
            key = base64.b64decode(secret[len("whsec_"):])
        else:
            key = secret.encode("utf-8")
        signed_payload = f"{svix_id}.{svix_ts}.{body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(key, signed_payload, hashlib.sha256).digest()
        import base64 as _b64
        expected_b64 = _b64.b64encode(expected).decode("utf-8")
        # Header can carry multiple signatures separated by spaces.
        for token in svix_sig.split():
            if "," not in token:
                continue
            _, sig = token.split(",", 1)
            if hmac.compare_digest(sig, expected_b64):
                return True
        return False
    except Exception as exc:  # noqa: BLE001 — invalid sig is just `False`
        log.warning("track_b: resend signature verify error: %s", exc)
        return False


async def _find_lead_id_for_email_event(payload: dict) -> Optional[str]:
    """Best-effort: pull lead_id from Resend tags, then fall back to email lookup."""
    data = payload.get("data") or {}

    # 1. Tags carry our lead_id (we attach it in `_send_email`).
    tags = data.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict) and t.get("name") == "lead_id":
                val = t.get("value")
                if val:
                    return str(val)

    # 2. Fall back to Supabase email lookup.
    to = data.get("to")
    if isinstance(to, list) and to:
        target = to[0]
    elif isinstance(to, str):
        target = to
    else:
        target = None
    if not target:
        return None

    try:
        from lib.sessions import SUPA_URL, SUPA_HEADERS  # local import to avoid cycle in dev
        from urllib.parse import quote as _q
        # PostgREST: `+` in email (gmail aliasing) becomes space when not URL-encoded.
        target_enc = _q(target, safe="@.")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPA_URL}/leads?email=eq.{target_enc}&select=id&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code == 200:
            rows = r.json() or []
            if rows:
                return str(rows[0].get("id"))
    except Exception:  # noqa: BLE001
        log.exception("track_b: lead lookup by email failed for %s", target)
    return None


@router.post("/email-event")
async def email_event(request: Request) -> dict:
    """Receive Resend webhook events and translate them into `signals`.

    Optionally verifies the Svix signature when `RESEND_WEBHOOK_SECRET` is
    set in the environment. Unknown event types are recorded as a generic
    signal so we have a forensic trail.
    """
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_resend_signature(raw, headers):
        log.warning("track_b: rejected resend webhook — bad signature")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    event_type = str(payload.get("type") or "").lower()
    kind = _RESEND_EVENT_MAP.get(event_type, "email_event")

    lead_id = await _find_lead_id_for_email_event(payload)
    if not lead_id:
        log.warning("track_b: webhook event %s with no resolvable lead_id", event_type)
        return {"ok": False, "reason": "lead not found"}

    data = payload.get("data") or {}
    value = str(data.get("email_id") or data.get("id") or "")
    try:
        await session_append_signal(
            lead_id,
            kind=kind,
            value=value,
            source="resend",
        )
    except Exception:
        log.exception("track_b: failed to append signal lead=%s", lead_id)
        raise HTTPException(status_code=500, detail="signal write failed")

    return {"ok": True, "lead_id": lead_id, "kind": kind}


@router.get("/accept")
async def accept(lead_id: str, token: str) -> HTMLResponse:
    """One-click proposal acceptance.

    Verifies the HMAC token, files a `proposal_accepted` signal, flips
    lifecycle to `proposal_signed`, clears `next_action` so the autopilot
    stops nagging, and returns a small thank-you page. Mila gets the Slack
    ping via the orchestrator's history append.
    """
    if not _verify_accept_token(lead_id, token):
        log.warning("track_b: /accept rejected — bad token for lead=%s", lead_id)
        raise HTTPException(status_code=403, detail="invalid token")

    try:
        await session_append_signal(
            lead_id,
            kind="proposal_accepted",
            value=token[:12],  # short fingerprint for the audit log
            source="accept_link",
        )
        await session_set_status(lead_id, "proposal_signed")
        await session_set_next(lead_id, None, None)
        await session_append_history(
            lead_id=lead_id,
            agent="track_b",
            action="accept",
            result="ok",
            detail="proposal accepted via one-click link",
        )
    except Exception:
        log.exception("track_b: /accept post-verify writes failed lead=%s", lead_id)
        # Still render the thank-you so the user isn't punished for our DB hiccup.
        # The orchestrator/admin will reconcile.

    lead = await session_get(lead_id) or {}
    lang = _lang(lead)
    if lang == "en":
        title = "Proposal accepted — thank you"
        line1 = "We got it. You'll hear from Mila within one business day with the kickoff plan."
        line2 = "In the meantime: nothing else to do. The contract and invoice are coming your way."
    else:
        title = "Proposta aceita — obrigada!"
        line1 = "Recebido. A Mila te chama em até um dia útil com o plano de kickoff."
        line2 = "Por enquanto: nada a fazer. O contrato e a nota chegam em sequência."

    html = f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:64px 24px;">
<div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:40px 36px;text-align:center;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#16a34a;margin:0 0 12px 0;">Anuvia</p>
<h1 style="font-family:Georgia,serif;font-size:32px;margin:0 0 20px 0;color:#0f172a;">{title}</h1>
<p style="color:#475569;line-height:1.65;font-size:16px;">{line1}</p>
<p style="color:#475569;line-height:1.65;font-size:16px;">{line2}</p>
<p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia</p>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=200)
