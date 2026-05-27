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
from typing import Any, Callable, Dict, Optional

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
# Practice config — single source of truth for ticket bands & autonomy flag
# ---------------------------------------------------------------------------

#: Maps internal practice keys to a small bundle of facts the handlers need:
#: pricing band, whether the autonomous-close autopilot is enabled (vs.
#: discovery-led), the human-readable deliverable name, and rough duration.
#:
#: This is the **only** place to flip a practice from discovery-led to
#: autonomous-close — every handler reads ``autonomous_enabled`` here.
PRACTICE_CONFIG: Dict[str, Dict[str, Any]] = {
    "growth": {
        "ticket_min_brl": 4000,
        "ticket_max_brl": 8000,
        "autonomous_enabled": True,
        "deliverable_name": "Growth Sales Ops Setup",
        "duration_weeks": "2-3",
        # Copy seeds — short enough to live inline, used by the generic
        # proposal renderer when the practice is not 'growth' (growth keeps
        # its own bespoke renderer for backwards compatibility).
        "scope_pt": [
            "Solutions Architect sênior (ex-AWS, ex-Google) em retainer",
            "Até 20 horas / mês de execução e revisão hands-on",
            "Entregáveis escritos: arquitetura, FinOps e prontidão para IA",
            "Atendimento via Slack + async em até 24h úteis",
        ],
        "scope_en": [
            "Senior Solutions Architect (ex-AWS, ex-Google) on retainer",
            "Up to 20 hours / month of hands-on engineering and review",
            "Architecture, FinOps and AI-readiness deliverables in writing",
            "Slack + async response within 24h business hours",
        ],
    },
    "cloud_finops": {
        "ticket_min_brl": 45000,
        "ticket_max_brl": 60000,
        "autonomous_enabled": False,  # discovery-led for now (bigger tickets)
        "deliverable_name": "FinOps Audit",
        "duration_weeks": "4",
        "scope_pt": [
            "Diagnóstico completo de gastos AWS (CUR + Cost Explorer)",
            "Identificação de waste, rightsizing e Reserved/Savings Plans",
            "Roadmap de otimização priorizado por payback (semana-a-semana)",
            "Relatório executivo escrito + handoff técnico para o time",
        ],
        "scope_en": [
            "Full AWS spend diagnostic (CUR + Cost Explorer)",
            "Waste, rightsizing and Reserved/Savings Plans opportunities",
            "Optimization roadmap prioritized by payback (week-by-week)",
            "Written executive report + technical handoff to your team",
        ],
    },
    "devops": {
        "ticket_min_brl": 30000,
        "ticket_max_brl": 50000,
        "autonomous_enabled": False,
        "deliverable_name": "DevOps Maturity Assessment",
        "duration_weeks": "3-4",
        "scope_pt": [
            "Avaliação DORA (deploy freq, lead time, MTTR, change fail rate)",
            "Auditoria de CI/CD, infra-as-code e observabilidade",
            "Roadmap de maturidade em 3 horizontes (30 / 90 / 180 dias)",
            "Relatório executivo + plano de ação para o time de engenharia",
        ],
        "scope_en": [
            "DORA assessment (deploy freq, lead time, MTTR, change fail rate)",
            "CI/CD, infra-as-code and observability audit",
            "Maturity roadmap on 3 horizons (30 / 90 / 180 days)",
            "Executive report + action plan for the engineering team",
        ],
    },
    "ai": {
        "ticket_min_brl": 25000,
        "ticket_max_brl": 40000,
        "autonomous_enabled": False,
        "deliverable_name": "AI Readiness & PoV",
        "duration_weeks": "3",
        "scope_pt": [
            "Inventário de casos de uso de IA priorizados por ROI",
            "Avaliação de prontidão técnica (dados, infra, segurança)",
            "Proof-of-value de 1 caso (escopo fechado, métrica clara)",
            "Roadmap de adoção + governança em 90 dias",
        ],
        "scope_en": [
            "Inventory of AI use-cases prioritized by ROI",
            "Technical readiness assessment (data, infra, security)",
            "Proof-of-value on 1 use-case (closed scope, clear metric)",
            "Adoption roadmap + governance over 90 days",
        ],
    },
    "industry": {
        "ticket_min_brl": 35000,
        "ticket_max_brl": 55000,
        "autonomous_enabled": False,
        "deliverable_name": "Industry Vertical Assessment",
        "duration_weeks": "4",
        "scope_pt": [
            "Diagnóstico setorial (regulação, compliance, benchmarks)",
            "Mapa de capacidades vs. concorrência no vertical",
            "Roadmap de modernização alinhado às pressões regulatórias",
            "Plano de execução com marcos trimestrais",
        ],
        "scope_en": [
            "Vertical diagnostic (regulation, compliance, benchmarks)",
            "Capability map vs. competition in the vertical",
            "Modernization roadmap aligned to regulatory pressures",
            "Execution plan with quarterly milestones",
        ],
    },
}


#: Maps funnel_id (uppercase) prefix → practice key. Anything not listed here
#: falls through to ``None`` and the lead is parked in 'discovery'.
_FUNNEL_TO_PRACTICE: Dict[str, str] = {
    "BR_GROWTH": "growth",
    "US_GROWTH": "growth",
    "BR_FINOPS": "cloud_finops",
    "US_FINOPS": "cloud_finops",
    "BR_AWS_WA": "cloud_finops",
    "BR_AWS_MIG": "cloud_finops",
    "BR_AWS_LZ": "cloud_finops",
    "BR_AWS_SP": "cloud_finops",
    "BR_GCP_MIG": "cloud_finops",
    "BR_DEVOPS": "devops",
    "US_DEVOPS": "devops",
    "BR_AI": "ai",
    "US_AI": "ai",
    "BR_INDUSTRY": "industry",
    "US_INDUSTRY": "industry",
}


def _funnel_to_practice(funnel_id: Optional[str]) -> Optional[str]:
    """Map a raw funnel_id string to our internal practice key.

    Tolerates ``None``, mixed case and trailing variants (e.g. ``BR_GROWTH_LP``).
    Returns ``None`` when the funnel doesn't match any known practice — the
    caller should treat that as 'discovery' (human in the loop).
    """
    if not funnel_id:
        return None
    fid = str(funnel_id).upper().strip()
    # Exact match first, then prefix scan (handles BR_GROWTH_LP, etc).
    if fid in _FUNNEL_TO_PRACTICE:
        return _FUNNEL_TO_PRACTICE[fid]
    for prefix, practice in _FUNNEL_TO_PRACTICE.items():
        if fid.startswith(prefix):
            return practice
    return None


def _qd(lead: dict) -> dict:
    """Pull `qualification_data` out as a plain dict, tolerating jsonb-as-str."""
    qd = lead.get("qualification_data") or {}
    if isinstance(qd, dict):
        return qd
    try:
        loaded = json.loads(qd) if isinstance(qd, str) else {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# classify_track — pure function, no I/O
# ---------------------------------------------------------------------------


def _signals_growth(qd: dict) -> int:
    """Existing Growth qualification heuristic. >=2 of 3 signals = autonomous."""
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

    return signals


def _signals_cloud_finops(qd: dict) -> int:
    """FinOps autonomous signals.

    Need AWS spend band >= 25k/mo AND a real decision maker (CTO/VP/Head)
    that is NOT a solo 'owner'. Ticket size is R$ 45-60k so the bar is high
    even if autopilot is later enabled.
    """
    signals = 0

    # Spend signal ------------------------------------------------------------
    spend_hit = False
    aws_spend = qd.get("aws_spend")
    try:
        if aws_spend is not None and float(aws_spend) >= 25000:
            spend_hit = True
    except (TypeError, ValueError):
        # Spend may come as a band string like '25k-50k' / '>50k'.
        s = str(aws_spend or "").lower().replace("$", "").replace(" ", "")
        if any(tag in s for tag in ("25k-50k", "50k-100k", ">25k", ">50k", "100k+")):
            spend_hit = True
    if spend_hit:
        signals += 1

    # Decision maker ----------------------------------------------------------
    dm = str(qd.get("decision_maker") or qd.get("role") or qd.get("title") or "").lower()
    if dm and "owner" not in dm:
        if any(tag in dm for tag in ("cto", "vp", "head", "director", "diretor")):
            signals += 1

    return signals


def _signals_devops(qd: dict) -> int:
    """DevOps maturity autonomous signals."""
    signals = 0

    team_size = qd.get("team_size") or qd.get("eng_team_size")
    try:
        if team_size is not None and float(team_size) >= 5:
            signals += 1
    except (TypeError, ValueError):
        pass

    cicd = str(qd.get("ci_cd") or qd.get("cicd_status") or "").lower()
    if cicd in ("yes", "true", "present", "wants_setup", "setup") or "yes" in cicd:
        signals += 1

    obs = str(qd.get("observability_pain") or qd.get("observability") or "").lower()
    if obs in ("true", "yes", "high", "pain") or "pain" in obs:
        signals += 1

    return signals


def _signals_ai(qd: dict) -> int:
    """AI readiness autonomous signals."""
    signals = 0

    stage = str(qd.get("ai_stage") or qd.get("stage") or qd.get("notes") or "").lower()
    if "pov" in stage or "proof" in stage or "exploring" in stage or "explorando" in stage:
        signals += 1

    readiness = str(qd.get("technical_readiness") or qd.get("data_ready") or "").lower()
    if readiness in ("true", "yes", "high", "ready", "pronto"):
        signals += 1

    return signals


def _signals_industry(qd: dict) -> int:
    """Industry vertical autonomous signals."""
    signals = 0

    vertical = str(qd.get("vertical") or "").lower().strip()
    if vertical and vertical not in ("other", "n/a", "none"):
        signals += 1

    compliance = str(qd.get("compliance_pressure") or qd.get("regulation") or "").lower()
    if compliance in ("true", "yes", "high", "urgent") or "lgpd" in compliance or "soc" in compliance or "iso" in compliance:
        signals += 1

    return signals


#: Per-practice signal counter. Each returns an int — the dispatcher decides
#: how many signals are needed to flip the lead to autonomous. Default
#: threshold is 2; growth uses the historical 2-of-3 rule.
_PRACTICE_SIGNALS: Dict[str, Callable[[dict], int]] = {
    "growth": _signals_growth,
    "cloud_finops": _signals_cloud_finops,
    "devops": _signals_devops,
    "ai": _signals_ai,
    "industry": _signals_industry,
}


def classify_track(lead: dict) -> str:
    """Decide whether this lead goes through the autonomous-close path.

    Practice-aware. Returns ``'autonomous'`` only when:
      * ``funnel_id`` maps to a known practice,
      * that practice has ``autonomous_enabled=True`` in ``PRACTICE_CONFIG``, and
      * the practice-specific signal counter returns >= 2.

    Otherwise returns ``'discovery'``. Never raises — type errors / missing
    keys silently fall through to ``'discovery'`` so a malformed row can't
    crash the dispatcher.
    """
    try:
        practice = _funnel_to_practice(lead.get("funnel_id"))
    except Exception:  # noqa: BLE001
        return "discovery"

    if not practice:
        return "discovery"

    cfg = PRACTICE_CONFIG.get(practice) or {}
    if not cfg.get("autonomous_enabled"):
        return "discovery"

    counter = _PRACTICE_SIGNALS.get(practice)
    if counter is None:
        return "discovery"

    try:
        signals = counter(_qd(lead))
    except Exception:  # noqa: BLE001
        return "discovery"

    return "autonomous" if signals >= 2 else "discovery"


def classify_practice_and_track(lead: dict) -> tuple:
    """Companion to ``classify_track`` — also returns the resolved practice.

    Used by handlers that need to know which practice-specific next-action
    string to schedule. Returns ``(practice_or_None, 'autonomous'|'discovery')``.
    """
    practice = _funnel_to_practice(lead.get("funnel_id"))
    track = classify_track(lead)
    return practice, track


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
        sig = "Anuvia"
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
        sig = "Anuvia"
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
    """Decide the lead's practice + track and schedule the next step.

    Autonomous leads get a 15-minute breather before the proposal lands —
    enough for the LP confirmation page to settle and any human review to
    bail out. Discovery leads simply wait for the booking widget.

    Dispatch logic:
      * Map ``funnel_id`` → practice via ``_funnel_to_practice``.
      * Look up ``PRACTICE_CONFIG[practice]['autonomous_enabled']``.
      * If False (or practice unknown) → track='discovery', no autopilot.
      * If True → run the practice's signal counter; >=2 signals flips
        the lead to 'autonomous' and schedules the practice-specific
        ``{practice}_generate_proposal_v1`` (or ``generate_proposal_v1``
        for growth, for back-compat with the existing handler name).
    """
    lead_id = str(lead.get("id") or "")
    practice = _funnel_to_practice(lead.get("funnel_id"))
    track = classify_track(lead)

    try:
        await session_update(lead_id, track=track)
    except Exception:
        log.exception("track_b.classify_track: session_update failed lead=%s", lead_id)
        raise

    if track == "autonomous" and practice:
        # Growth retains the historical un-prefixed handler name so existing
        # in-flight leads keep dispatching cleanly.
        next_action = (
            "generate_proposal_v1"
            if practice == "growth"
            else f"{practice}_generate_proposal_v1"
        )
        return {
            "next_action": next_action,
            "next_action_at": _now() + timedelta(minutes=15),
            "status": "qualified",
            "detail": f"autonomous track ({practice})",
        }

    detail = (
        f"discovery track ({practice}) — human in the loop"
        if practice
        else "discovery track — unmapped funnel_id"
    )
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "in_discovery",
        "detail": detail,
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
<p style="color:#78716c;font-size:13px;margin-top:32px;">Anuvia<br>Reply to this email if anything is unclear.</p>
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
<p style="color:#78716c;font-size:13px;margin-top:32px;">Anuvia<br>Responde esse email se algo estiver confuso.</p>
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
# Practice-aware handlers (cloud_finops, devops, ai, industry)
# ---------------------------------------------------------------------------
#
# These mirror the Growth handlers above 1:1 but pull their copy and
# scheduling targets from PRACTICE_CONFIG. They are registered under
# practice-prefixed names so the orchestrator can dispatch by funnel:
#
#     {practice}_classify_track
#     {practice}_generate_proposal_v1
#     {practice}_followup_proposal_d2
#     {practice}_followup_proposal_d5
#     {practice}_close_ghosted_d10
#
# `autonomous_enabled` is currently False for all four practices — Mila
# wants a human in the loop at these ticket sizes — so in practice the
# `_classify_track` handlers will route to discovery and the proposal/
# followup handlers will not be invoked until that flag is flipped. The
# code exists so flipping the flag is a one-line change.


def _practice_pricing(lead: dict, lang: str, practice: str) -> dict:
    """Per-practice pricing line.

    Uses PRACTICE_CONFIG ticket bands as the default; honours
    `qualification_data.budget_amount` when the LP captured an explicit value.
    Returns the same shape as ``_pricing`` for the Growth path.
    """
    cfg = PRACTICE_CONFIG.get(practice) or {}
    qd = _qd(lead)

    if lang == "en":
        # Rough BRL→USD divisor (5x). Defensive: we'd rather show a clean
        # number than nothing.
        lo = int((cfg.get("ticket_min_brl") or 0) / 5)
        hi = int((cfg.get("ticket_max_brl") or 0) / 5)
        pricing = {
            "currency_symbol": "US$",
            "retainer": f"{lo:,}–{hi:,}",
            "setup": f"{int(lo/2):,}",
            "period_label": "/project",
        }
    else:
        lo = cfg.get("ticket_min_brl") or 0
        hi = cfg.get("ticket_max_brl") or 0
        pricing = {
            "currency_symbol": "R$",
            "retainer": f"{lo:,}–{hi:,}".replace(",", "."),
            "setup": f"{int(lo/2):,}".replace(",", "."),
            "period_label": "/projeto",
        }

    # Explicit budget override (still respect lead self-declaration).
    amt = qd.get("budget_amount")
    try:
        if amt is not None and float(amt) > 0:
            n = float(amt)
            if lang == "en":
                pricing["retainer"] = f"{int(n):,}"
                pricing["setup"] = f"{int(n / 2):,}"
            else:
                pricing["retainer"] = f"{int(n):,}".replace(",", ".")
                pricing["setup"] = f"{int(n / 2):,}".replace(",", ".")
    except (TypeError, ValueError):
        pass

    return pricing


def _practice_proposal_html(lead: dict, practice: str) -> str:
    """Render a bilingual practice-specific proposal one-pager."""
    cfg = PRACTICE_CONFIG.get(practice) or {}
    lang = _lang(lead)
    pricing = _practice_pricing(lead, lang, practice)
    name = (lead.get("name") or "").split(" ")[0] or ("there" if lang == "en" else "olá")
    company = lead.get("company") or ""

    deliverable = cfg.get("deliverable_name") or practice.title()
    duration = cfg.get("duration_weeks") or "—"

    if lang == "en":
        title = f"Anuvia · {deliverable} proposal"
        intro = (
            f"Hi {name}, here's the {deliverable} proposal we discussed. "
            f"Closed scope, ~{duration} weeks end-to-end, senior team."
        )
        scope_hdr = "What you get"
        scope = cfg.get("scope_en") or []
        price_hdr = "Investment"
        price_line = (
            f"{pricing['currency_symbol']} {pricing['retainer']}{pricing['period_label']} · "
            f"{duration} weeks delivery"
        )
        terms = "50% upfront, 50% on delivery. Includes one revision round."
        cta = "Accept proposal"
        sig = "Anuvia"
        footer = "Ex-AWS Solutions Architect · Ex-Google · 15+ AWS certifications"
        headline = deliverable
    else:
        title = f"Anuvia · Proposta {deliverable}"
        intro = (
            f"Olá {name}, segue a proposta de {deliverable} que conversamos. "
            f"Escopo fechado, ~{duration} semanas ponta a ponta, time sênior."
        )
        scope_hdr = "O que está incluso"
        scope = cfg.get("scope_pt") or []
        price_hdr = "Investimento"
        price_line = (
            f"{pricing['currency_symbol']} {pricing['retainer']}{pricing['period_label']} · "
            f"entrega em {duration} semanas"
        )
        terms = "50% na assinatura, 50% na entrega. Inclui uma rodada de revisão."
        cta = "Aceitar proposta"
        sig = "Anuvia"
        footer = "Ex-Solutions Architect AWS · Ex-Google · 15+ certificações AWS"
        headline = deliverable

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
<h1 style="font-family:Georgia,serif;font-size:30px;margin:0 0 18px 0;color:#0f172a;">{headline}</h1>
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


def _practice_has_proposal_v1(lead: dict, practice: str) -> bool:
    """Same as ``_has_proposal_v1`` but scoped to artifacts tagged with this practice."""
    artifacts = lead.get("artifacts") or []
    if not isinstance(artifacts, list):
        return False
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "proposal_pdf":
            continue
        meta = a.get("meta") or {}
        if isinstance(meta, dict) and meta.get("version") == 1 and meta.get("practice") == practice:
            return True
    return False


def _practice_proposal_sent_ts(lead: dict, practice: str) -> Optional[datetime]:
    """Return the ts of this practice's first proposal_pdf artifact, or None."""
    for a in lead.get("artifacts") or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "proposal_pdf":
            continue
        meta = a.get("meta") or {}
        if not (isinstance(meta, dict) and meta.get("practice") == practice):
            continue
        ts = a.get("ts")
        if not ts:
            continue
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


async def _practice_generate_proposal(lead: Dict[str, Any], practice: str) -> Dict[str, Any]:
    """Render HTML/PDF, send email, file artifact — generic per-practice variant.

    Returns the standard handler contract so callers can return it directly.
    Idempotent: re-checks artifacts before sending.
    """
    lead_id = str(lead.get("id") or "")
    lang = _lang(lead)
    cfg = PRACTICE_CONFIG.get(practice) or {}
    deliverable = cfg.get("deliverable_name") or practice.title()

    fresh = await session_get(lead_id) or lead
    if _practice_has_proposal_v1(fresh, practice):
        log.info(
            "track_b.%s_generate_proposal_v1: already sent lead=%s; skipping",
            practice, lead_id,
        )
        return {
            "next_action": f"{practice}_followup_proposal_d2",
            "next_action_at": _now() + timedelta(days=2),
            "status": "proposal_sent",
            "detail": f"{practice} proposal v1 already on file (idempotent skip)",
        }

    html = _practice_proposal_html(fresh, practice)

    html_path = _PROPOSALS_DIR / f"{lead_id}_{practice}_v1.html"
    pdf_path = _PROPOSALS_DIR / f"{lead_id}_{practice}_v1.pdf"
    try:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        log.exception(
            "track_b.%s_generate_proposal_v1: html write failed lead=%s",
            practice, lead_id,
        )
        raise

    pdf_ok = await _render_pdf_via_gotenberg(html, pdf_path)

    host = _proposal_host(lang)
    if pdf_ok:
        hosted_url = f"{host}/static/proposals/{lead_id}_{practice}_v1.pdf"
    else:
        hosted_url = f"{host}/static/proposals/{lead_id}_{practice}_v1.html"

    # Email copy ---------------------------------------------------------------
    token = _accept_token(lead_id)
    accept_url = f"{PROPOSAL_HOST_PT}/api/track-b/accept?lead_id={lead_id}&token={token}"
    name = (fresh.get("name") or "").split(" ")[0]

    if lang == "en":
        subject = f"Your Anuvia {deliverable} proposal"
        body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Proposal</p>
<p style="font-family:Georgia,serif;font-size:28px;margin:0 0 16px 0;">Hi {name or 'there'},</p>
<p style="color:#475569;line-height:1.65;">Your {deliverable} proposal is ready. Closed scope, written, no slide deck.</p>
<p style="margin:24px 0;"><a href="{hosted_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Read the proposal -></a></p>
<p style="color:#475569;line-height:1.65;">Ready to start? One click below and we kick off this week.</p>
<p style="margin:24px 0;"><a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Accept proposal -></a></p>
<p style="color:#78716c;font-size:13px;margin-top:32px;">Anuvia<br>Reply to this email if anything is unclear.</p>
</div></body></html>"""
    else:
        subject = f"Sua proposta Anuvia {deliverable}"
        body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Proposta</p>
<p style="font-family:Georgia,serif;font-size:28px;margin:0 0 16px 0;">Olá {name or 'tudo bem'},</p>
<p style="color:#475569;line-height:1.65;">Sua proposta de {deliverable} está pronta. Escopo fechado, escrita, sem slide.</p>
<p style="margin:24px 0;"><a href="{hosted_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Ler a proposta -></a></p>
<p style="color:#475569;line-height:1.65;">Topa começar? Um clique abaixo e a gente arranca essa semana.</p>
<p style="margin:24px 0;"><a href="{accept_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Aceitar proposta -></a></p>
<p style="color:#78716c;font-size:13px;margin-top:32px;">Anuvia<br>Responde esse email se algo estiver confuso.</p>
</div></body></html>"""

    to = fresh.get("email")
    if not to:
        raise RuntimeError(
            f"track_b.{practice}_generate_proposal_v1: lead {lead_id} has no email"
        )

    msg_id = await _send_email(
        to=to,
        subject=subject,
        html=body_html,
        lead_id=lead_id,
        kind=f"{practice}_proposal_v1",
    )

    try:
        await session_append_artifact(
            lead_id,
            type="proposal_pdf",
            url=hosted_url,
            meta={
                "version": 1,
                "practice": practice,
                "pdf_rendered": pdf_ok,
                "lang": lang,
                "resend_message_id": msg_id,
            },
        )
        await session_append_artifact(
            lead_id,
            type="email_sent",
            url=None,
            meta={
                "kind": f"{practice}_proposal_v1",
                "practice": practice,
                "resend_message_id": msg_id,
                "lang": lang,
            },
        )
    except Exception:
        log.exception(
            "track_b.%s_generate_proposal_v1: artifact append failed lead=%s",
            practice, lead_id,
        )
        raise

    return {
        "next_action": f"{practice}_followup_proposal_d2",
        "next_action_at": _now() + timedelta(days=2),
        "status": "proposal_sent",
        "detail": f"{practice} proposal v1 sent",
    }


async def _practice_send_nudge(
    lead: Dict[str, Any],
    *,
    practice: str,
    kind: str,            # 'nudge_d2' | 'nudge_d5'
    next_action: Optional[str],
    next_delay: timedelta,
    next_status: Optional[str],
    detail: str,
) -> Dict[str, Any]:
    """Shared body for practice-prefixed d2/d5 nudges. Mirrors `_send_nudge_and_schedule`."""
    lead_id = str(lead.get("id") or "")
    lang = _lang(lead)
    cfg = PRACTICE_CONFIG.get(practice) or {}
    deliverable = cfg.get("deliverable_name") or practice.title()

    fresh = await session_get(lead_id) or lead
    sent_ts = _practice_proposal_sent_ts(fresh, practice)
    if _has_engagement_since(fresh, sent_ts):
        log.info(
            "track_b.%s_%s: engagement detected lead=%s; pausing autopilot",
            practice, kind, lead_id,
        )
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "proposal_opened",
            "detail": "engagement detected, pausing autopilot",
        }

    name = (fresh.get("name") or "").split(" ")[0]
    host = _proposal_host(lang)
    pdf_path = _PROPOSALS_DIR / f"{lead_id}_{practice}_v1.pdf"
    if pdf_path.exists():
        hosted_url = f"{host}/static/proposals/{lead_id}_{practice}_v1.pdf"
    else:
        hosted_url = f"{host}/static/proposals/{lead_id}_{practice}_v1.html"

    if kind == "nudge_d2":
        if lang == "en":
            subject = f"Quick nudge on the {deliverable} proposal"
            body = (
                f"Hi {name or 'there'} — just bubbling the {deliverable} proposal back up. "
                "If now isn't the right week, tell me when is and I'll hold the slot."
            )
        else:
            subject = f"Voltando aqui sobre a proposta {deliverable}"
            body = (
                f"Oi {name or 'tudo bem'} — só subindo a proposta de {deliverable}. "
                "Se essa semana não é a hora, me diz quando é e eu seguro a vaga."
            )
    else:  # nudge_d5
        if lang == "en":
            subject = f"Last check-in on the {deliverable} proposal"
            body = (
                f"Hi {name or 'there'} — last check-in on the {deliverable} proposal. "
                "If the timing is off, no worries; I'll close the file and you can ping me when ready."
            )
        else:
            subject = f"Último toque na proposta {deliverable}"
            body = (
                f"Oi {name or 'tudo bem'} — último contato sobre a proposta de {deliverable}. "
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
        raise RuntimeError(f"track_b.{practice}_{kind}: lead {lead_id} has no email")

    msg_id = await _send_email(
        to=to,
        subject=subject,
        html=body_html,
        lead_id=lead_id,
        kind=f"{practice}_{kind}",
    )

    try:
        await session_append_artifact(
            lead_id,
            type="email_sent",
            url=None,
            meta={
                "kind": f"{practice}_{kind}",
                "practice": practice,
                "resend_message_id": msg_id,
                "lang": lang,
            },
        )
    except Exception:
        log.exception(
            "track_b.%s_%s: artifact append failed lead=%s",
            practice, kind, lead_id,
        )
        raise

    return {
        "next_action": next_action,
        "next_action_at": (_now() + next_delay) if next_action else None,
        "status": next_status,
        "detail": detail,
    }


def _make_practice_handlers(practice: str) -> None:
    """Register the 5 standard handlers for a non-growth practice.

    Called once per practice at module import time. Each handler is a thin
    closure over ``_practice_*`` helpers above so the lifecycle stays
    identical to Growth's flow.
    """

    @register(f"{practice}_classify_track")
    async def _classify(lead: Dict[str, Any]) -> Dict[str, Any]:
        """Practice-specific classify entry point.

        Useful when a future LP wants to schedule the practice classifier
        directly (skipping the generic ``classify_track``). Behaviour is
        identical: read PRACTICE_CONFIG, route to autonomous or discovery.
        """
        lead_id = str(lead.get("id") or "")
        cfg = PRACTICE_CONFIG.get(practice) or {}
        if not cfg.get("autonomous_enabled"):
            try:
                await session_update(lead_id, track="discovery")
            except Exception:
                log.exception(
                    "track_b.%s_classify_track: session_update failed lead=%s",
                    practice, lead_id,
                )
                raise
            return {
                "next_action": None,
                "next_action_at": None,
                "status": "in_discovery",
                "detail": f"{practice} is discovery-led (autonomous_enabled=False)",
            }

        counter = _PRACTICE_SIGNALS.get(practice)
        signals = counter(_qd(lead)) if counter else 0
        track = "autonomous" if signals >= 2 else "discovery"
        try:
            await session_update(lead_id, track=track)
        except Exception:
            log.exception(
                "track_b.%s_classify_track: session_update failed lead=%s",
                practice, lead_id,
            )
            raise

        if track == "autonomous":
            return {
                "next_action": f"{practice}_generate_proposal_v1",
                "next_action_at": _now() + timedelta(minutes=15),
                "status": "qualified",
                "detail": f"autonomous track ({practice})",
            }
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "in_discovery",
            "detail": f"discovery track ({practice}, signals<2)",
        }

    @register(f"{practice}_generate_proposal_v1")
    async def _gen_proposal(lead: Dict[str, Any]) -> Dict[str, Any]:
        return await _practice_generate_proposal(lead, practice)

    @register(f"{practice}_followup_proposal_d2")
    async def _fwup_d2(lead: Dict[str, Any]) -> Dict[str, Any]:
        return await _practice_send_nudge(
            lead,
            practice=practice,
            kind="nudge_d2",
            next_action=f"{practice}_followup_proposal_d5",
            next_delay=timedelta(days=3),
            next_status=None,
            detail=f"{practice} d2 nudge sent",
        )

    @register(f"{practice}_followup_proposal_d5")
    async def _fwup_d5(lead: Dict[str, Any]) -> Dict[str, Any]:
        return await _practice_send_nudge(
            lead,
            practice=practice,
            kind="nudge_d5",
            next_action=f"{practice}_close_ghosted_d10",
            next_delay=timedelta(days=5),
            next_status=None,
            detail=f"{practice} d5 final nudge sent",
        )

    @register(f"{practice}_close_ghosted_d10")
    async def _close_ghosted(lead: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "ghosted",
            "detail": f"{practice} closed as ghosted after 10 days no response",
        }


# Register handlers for every non-growth practice. Growth keeps its own
# bespoke set above (back-compat with leads already in flight).
for _practice in ("cloud_finops", "devops", "ai", "industry"):
    _make_practice_handlers(_practice)


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
<p style="color:#78716c;font-size:13px;margin-top:32px;">Anuvia</p>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=200)
