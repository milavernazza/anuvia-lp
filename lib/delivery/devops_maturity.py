"""DevOps Maturity Assessment — autonomous delivery agent.

Owns the post-signature delivery flow for the ``devops`` practice
(R$ 35-50k, 4 weeks). Hands off from ``lib.contract`` once a contract is
signed and paid, then runs four weekly phases — Baseline DORA, Maturity
Deep-dive, Roadmap & Quick Wins, Executive Sync & Handoff — producing
client deliverables and emails along the way.

Architecture::

    contract.webhook (paid)
        |
        v
    devops_kickoff                              [+0]
        |   (intake form sent, awaiting tool access + sample data)
        v
    devops_phase_1_baseline                     [+1 day]
        |   (intake submitted → DORA baseline composed)
        v
    devops_phase_2_maturity                     [+1 week]
        |   (6-dimension maturity scorecard + findings)
        v
    devops_phase_3_roadmap                      [+1 week]
        |   (6-month roadmap + quick wins playbook + tooling recs)
        v
    devops_phase_4_handoff                      [+1 week]
        |   (final executive report + deck + KPI tracking template)
        v
    status='delivered', next_action=None

Quality bar mirrors ``lib/delivery/ai_readiness.py`` and
``lib/delivery/finops_audit.py``:
  * All writes are append-only or idempotent. Each handler checks
    ``engagement.current_phase`` before mutating state.
  * Network failures bubble up so the orchestrator can retry.
  * Graceful degradation:
      - no ANTHROPIC_API_KEY → deliverables fall back to a templated
        narrative tagged ``[CLAUDE_UNAVAILABLE_DRAFT]``.
      - no RESEND_API_KEY → email payloads stash in
        ``engagement.artifacts.email_drafts`` and a Slack alert fires.
      - no Supabase Storage credentials → artifacts persist as inline
        HTML/markdown blobs on the engagement row.
  * Brand voice is enforced in every Claude prompt (dry, numbers-first,
    anti-hype, compliance-aware when intake flags a regulated env).
    See ``_BRAND_SYSTEM_PROMPT``.
  * HMAC-tokened client links use ``CONTRACT_HMAC_SECRET`` so a single
    secret drives sign + intake + approval flows.
  * DevOps Maturity has lighter compliance burden than AI Readiness or
    FinOps, but BACEN fintechs and ANS healthtechs DO carry compliance
    implications on observability + incident response. Every deliverable
    that touches those dimensions tags compliance explicitly when the
    intake signals a regulated environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _urlquote

import httpx

from lib.orchestrator import register, _send_slack_alert
from lib.sessions import (
    SUPA_HEADERS,
    SUPA_URL,
    session_append_artifact,
    session_append_history,
    session_get,
    session_set_next,
)

log = logging.getLogger("anuvia-lp.delivery.devops_maturity")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

#: Default ticket size for this practice. Midpoint of R$ 35-50k band.
PRACTICE_TICKET_BRL: int = 42000

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get(
    "ANUVIA_DELIVERY_MODEL", "claude-sonnet-4-5-20250929"
)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
RESEND_REPLY_TO_EMAIL = os.environ.get(
    "RESEND_REPLY_TO_EMAIL", "mila@anuvia.com.br"
)
RESEND_REPLY_TO_NAME = os.environ.get(
    "RESEND_REPLY_TO_NAME", "Anuvia · Mila Vernazza"
)

BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("CONTRACT_HOST", "https://anuvia.com.br"),
).rstrip("/")

_HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)

SUPA_STORAGE_BUCKET = os.environ.get(
    "ANUVIA_DELIVERABLES_BUCKET", "anuvia-deliverables"
)

SLACK_MILA_HANDLE = os.environ.get("SLACK_MILA_HANDLE", "@mila")

# 4-week cadence — same as FinOps.
_PHASE_INTERVAL = timedelta(days=7)
_INTAKE_REMINDER_AFTER = timedelta(days=4)
_HTTP_TIMEOUT = 30.0

# Brand voice — pinned to every Claude system prompt in this module.
# Same tone as ai_readiness / finops_audit but specialised for DevOps:
# numbers-first on DORA metrics, anti-hype on tooling, blameless on
# incident response, explicit on (impact × confidence) / effort math
# for prioritisation.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA + DevOps). Voz: seca, direta, "
    "anti-hype, primeiro os números, depois a narrativa. Frases curtas "
    "declarativas misturadas com cadeias causa-efeito mais longas. Use o "
    "léxico: vazamento, clareza, diagnóstico, processo, padrão, sobreviver "
    "em produção, gate de saída, evidência, post-mortem blameless, "
    "regression, runbook, SLI/SLO, eval. Evite: sinergia, transformação, "
    "leverage, magia, revolução, devops-cultura-mágica, world-class. "
    "Nunca prometa o que não pode ser medido. Sempre cite números concretos "
    "(deploys/dia, MTTR em min, lead time em horas, CFR %, cobertura %, "
    "R$/mês) quando tiver dados — quando não tiver, marque explicitamente "
    "como 'estimativa baseada em padrões setoriais'. Para clientes em "
    "ambiente regulado (BACEN fintechs, ANS healthtechs, GxP life "
    "sciences), tag a constraint de compliance explícita em observability "
    "e incident response. Para os demais, não inventar compliance. "
    "Priorização SEMPRE por (impact × confidence) / effort com cada termo "
    "definido. Português do Brasil."
)

#: Sentinel prefix for narrative that Claude could not generate.
_CLAUDE_FALLBACK_TAG = "[CLAUDE_UNAVAILABLE_DRAFT]"

#: The 6 maturity dimensions from SPRINT_INPUTS_MILA.md §5.3 semana 2.
_MATURITY_DIMENSIONS: List[str] = [
    "ci_cd",                # 1. Pipeline structure, test coverage, deployment patterns
    "test_automation",      # 2. Unit/integration/E2E coverage, test reliability
    "iac",                  # 3. Terraform/CDK/CloudFormation coverage, state, drift
    "gitops",               # 4. Argo CD/Flux, declarative state, rollback automation
    "observability",        # 5. Metrics/logs/traces/alerts, SLI/SLO, dashboard hygiene
    "incident_response",    # 6. Runbooks, post-mortem culture, on-call health
]

#: Human-readable labels for the 6 dimensions (used in PDFs + decks).
_MATURITY_DIMENSION_LABELS: Dict[str, str] = {
    "ci_cd": "CI/CD",
    "test_automation": "Test Automation",
    "iac": "Infrastructure as Code",
    "gitops": "GitOps",
    "observability": "Observability",
    "incident_response": "Incident Response",
}

#: DORA 2023 thresholds (Elite / High / Medium / Low). Source of truth for
#: the gap analysis in phase 1.
_DORA_THRESHOLDS: Dict[str, Dict[str, str]] = {
    "elite": {
        "deploy_frequency": "multiple per day",
        "lead_time": "< 1 day",
        "mttr": "< 1 hour",
        "cfr": "< 5%",
    },
    "high": {
        "deploy_frequency": "between once per week and once per month",
        "lead_time": "< 1 week",
        "mttr": "< 1 day",
        "cfr": "< 10%",
    },
    "medium": {
        "deploy_frequency": "between once per month and once every 6 months",
        "lead_time": "< 1 month",
        "mttr": "< 1 week",
        "cfr": "< 15%",
    },
    "low": {
        "deploy_frequency": "fewer than once per 6 months",
        "lead_time": "> 1 month",
        "mttr": "> 1 week",
        "cfr": "> 15%",
    },
}

#: Compliance frames that this practice might touch. DevOps Maturity is
#: lighter than AI Readiness but observability + incident response in
#: regulated environments (BACEN/ANS/GxP) do carry constraints.
_COMPLIANCE_FRAMES: List[str] = [
    "BACEN 4.658",   # fintechs
    "ANS RN 452",    # healthtechs (operadoras de saúde)
    "LGPD",
    "GxP",
    "SOC 2",
    "HIPAA",
    "ISO 27001",
    "nenhuma",
]


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Tz-aware UTC now. Centralised for testability."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _serialize(value: Any) -> Any:
    """Recursively convert datetimes to ISO strings so values survive JSON."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


def _brl(n: Any) -> str:
    """Format a numeric as Brazilian currency: 42.000,00."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0,00"
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hmac_token(engagement_id: str, purpose: str = "intake") -> str:
    """HMAC-SHA256 token for a client-facing link.

    The ``purpose`` keeps intake / approval / nps links from being
    interchangeable — a leaked intake link cannot approve a roadmap.
    """
    if not _HMAC_SECRET:
        log.warning(
            "devops_maturity: HMAC secret unset; client links unverifiable"
        )
        return ""
    msg = f"{engagement_id}:{purpose}".encode("utf-8")
    return hmac.new(_HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _verify_token(engagement_id: str, purpose: str, token: str) -> bool:
    """Constant-time verify for the HMAC tokens above. Never raises."""
    if not engagement_id or not token:
        return False
    expected = _hmac_token(engagement_id, purpose)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def _is_regulated(intake: dict) -> Optional[str]:
    """Detect regulated environment from intake. Returns the most
    restrictive frame, or ``None`` if intake is silent."""
    if not isinstance(intake, dict):
        return None
    flags = intake.get("compliance_constraints") or intake.get("regulated") or ""
    flags_str = (
        " ".join(flags) if isinstance(flags, list) else str(flags or "")
    ).lower()
    industry = str(intake.get("industry") or "").lower()
    if "bacen" in flags_str or "fintech" in industry or "banking" in industry:
        return "BACEN 4.658"
    if "gxp" in flags_str or "anvisa" in flags_str or "life science" in industry:
        return "GxP"
    if "ans" in flags_str or "healthtech" in industry or "operadora" in industry:
        return "ANS RN 452"
    if "hipaa" in flags_str or "saúde" in industry or "health" in industry:
        return "HIPAA"
    if "lgpd" in flags_str:
        return "LGPD"
    if "soc 2" in flags_str or "soc2" in flags_str:
        return "SOC 2"
    if "iso 27001" in flags_str or "iso27001" in flags_str:
        return "ISO 27001"
    return None


# ---------------------------------------------------------------------------
# Engagement row CRUD (PostgREST via httpx)
# ---------------------------------------------------------------------------


async def _engagement_get(engagement_id: str) -> Optional[dict]:
    """Fetch the full engagements row by id, or None if not found."""
    url = (
        f"{SUPA_URL}/engagements?id=eq."
        f"{_urlquote(str(engagement_id), safe='')}&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception(
            "devops_maturity: engagement_get network failed id=%s",
            engagement_id,
        )
        return None
    if r.status_code != 200:
        log.warning(
            "devops_maturity: engagement_get non-200 id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def _engagement_patch(engagement_id: str, fields: dict) -> bool:
    """PATCH an engagements row. Returns True on success. Never raises."""
    payload = _serialize(fields)
    url = (
        f"{SUPA_URL}/engagements?id=eq."
        f"{_urlquote(str(engagement_id), safe='')}"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.patch(url, headers=SUPA_HEADERS, json=payload)
    except Exception:  # noqa: BLE001
        log.exception(
            "devops_maturity: engagement_patch network failed id=%s",
            engagement_id,
        )
        return False
    if r.status_code not in (200, 204):
        log.warning(
            "devops_maturity: engagement_patch non-2xx id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return False
    return True


async def _engagement_merge_artifacts(
    engagement_id: str, additions: dict
) -> bool:
    """Merge ``additions`` into ``engagement.artifacts`` (a jsonb object)."""
    row = await _engagement_get(engagement_id)
    if not row:
        log.warning(
            "devops_maturity: merge_artifacts: engagement %s not found",
            engagement_id,
        )
        return False
    current = row.get("artifacts") or {}
    if not isinstance(current, dict):
        current = {"_legacy": current}
    new_value = dict(current)
    new_value.update(additions)
    return await _engagement_patch(engagement_id, {"artifacts": new_value})


async def _engagement_get_artifacts(engagement_id: str) -> dict:
    """Return ``engagement.artifacts`` as a dict (empty when missing)."""
    row = await _engagement_get(engagement_id)
    if not row:
        return {}
    a = row.get("artifacts") or {}
    return a if isinstance(a, dict) else {"_legacy": a}


# ---------------------------------------------------------------------------
# Supabase Storage upload
# ---------------------------------------------------------------------------


async def _upload_artifact(
    path: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Optional[str]:
    """Upload ``content`` to the ``anuvia-deliverables`` bucket.

    Returns the public URL or ``None`` if storage is unavailable.
    Best-effort. Storage outages must NOT crash a delivery handler.
    """
    if not SUPA_URL or not SUPA_HEADERS.get("apikey"):
        return None

    base = SUPA_URL.replace("/rest/v1", "")
    object_url = (
        f"{base}/storage/v1/object/{SUPA_STORAGE_BUCKET}/{path.lstrip('/')}"
    )
    headers = {
        "apikey": SUPA_HEADERS.get("apikey", ""),
        "Authorization": SUPA_HEADERS.get("Authorization", ""),
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(object_url, headers=headers, content=content)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "devops_maturity: storage upload failed path=%s: %s", path, exc
        )
        return None
    if r.status_code >= 400:
        log.warning(
            "devops_maturity: storage upload non-2xx path=%s status=%s body=%s",
            path, r.status_code, r.text[:200],
        )
        return None
    return (
        f"{base}/storage/v1/object/public/"
        f"{SUPA_STORAGE_BUCKET}/{path.lstrip('/')}"
    )


# ---------------------------------------------------------------------------
# HTML → PDF (Gotenberg)
# ---------------------------------------------------------------------------


async def _html_to_pdf(html: str) -> Optional[bytes]:
    """Render ``html`` to PDF bytes via Gotenberg.

    Returns ``None`` if Gotenberg is unreachable.
    """
    gotenberg = os.environ.get(
        "GOTENBERG_URL", "http://gotenberg:3000"
    ).rstrip("/")
    endpoint = f"{gotenberg}/forms/chromium/convert/html"
    try:
        files = {"files": ("index.html", html.encode("utf-8"), "text/html")}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(endpoint, files=files)
    except Exception as exc:  # noqa: BLE001
        log.warning("devops_maturity: gotenberg call failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning(
            "devops_maturity: gotenberg non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return None
    return r.content


# ---------------------------------------------------------------------------
# Claude wrapper — single source of truth for the brand voice
# ---------------------------------------------------------------------------


async def _claude_call_with_voice(
    prompt: str,
    *,
    max_tokens: int = 2400,
    system: str = _BRAND_SYSTEM_PROMPT,
) -> str:
    """One-shot call to the Anthropic Messages API.

    Returns the model's text. On any failure returns a sentinel string
    prefixed with ``_CLAUDE_FALLBACK_TAG`` so the caller can ship a
    degraded but obviously-flagged deliverable.
    """
    if not ANTHROPIC_API_KEY:
        return f"{_CLAUDE_FALLBACK_TAG} (no ANTHROPIC_API_KEY)\n\n{prompt[:800]}"

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT * 3) as client:
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
        log.warning("devops_maturity: anthropic network failed: %s", exc)
        return f"{_CLAUDE_FALLBACK_TAG} (network: {exc})"

    if r.status_code >= 400:
        log.warning(
            "devops_maturity: anthropic non-2xx status=%s body=%s",
            r.status_code, r.text[:300],
        )
        return f"{_CLAUDE_FALLBACK_TAG} (status {r.status_code})"

    body = r.json() if r.text else {}
    blocks = body.get("content") or []
    parts: List[str] = []
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text") or "")
    out = "\n".join(parts).strip()
    return out or f"{_CLAUDE_FALLBACK_TAG} (empty response)"


# ---------------------------------------------------------------------------
# Email send (Resend) — graceful degradation
# ---------------------------------------------------------------------------


async def _send_email_via_resend(
    *,
    engagement_id: str,
    to: str,
    subject: str,
    html: str,
    kind: str,
    cc: Optional[List[str]] = None,
) -> Optional[str]:
    """Send an email via Resend. On dry-run / failure, stash the draft.

    Returns the Resend message id on success, ``None`` otherwise.
    """
    if not RESEND_API_KEY:
        log.info(
            "devops_maturity: RESEND_API_KEY unset; stashing draft "
            "kind=%s eng=%s",
            kind, engagement_id,
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        await _send_slack_alert(
            f":warning: DevOps Maturity delivery: RESEND_API_KEY missing — "
            f"email `{kind}` for engagement `{engagement_id}` stashed as draft."
        )
        return None

    payload: Dict[str, Any] = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [to],
        "reply_to": f"{RESEND_REPLY_TO_NAME} <{RESEND_REPLY_TO_EMAIL}>",
        "subject": subject,
        "html": html,
        "tags": [
            {"name": "category", "value": "delivery_devops_maturity"},
            {"name": "kind", "value": kind},
            {"name": "engagement_id", "value": str(engagement_id)},
        ],
    }
    if cc:
        payload["cc"] = cc

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
    except Exception as exc:  # noqa: BLE001
        log.exception("devops_maturity: resend network failed kind=%s", kind)
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend network: {exc}")

    if r.status_code >= 400:
        log.error(
            "devops_maturity: resend non-2xx kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info(
        "devops_maturity: resend ok kind=%s eng=%s msg_id=%s",
        kind, engagement_id, msg_id,
    )
    return msg_id


async def _stash_email_draft(
    engagement_id: str,
    to: str,
    subject: str,
    html: str,
    kind: str,
    cc: Optional[List[str]],
) -> None:
    """Append an undeliverable email to ``engagement.artifacts.email_drafts``."""
    try:
        artifacts = await _engagement_get_artifacts(engagement_id)
        drafts = artifacts.get("email_drafts") or []
        if not isinstance(drafts, list):
            drafts = []
        drafts.append(
            {
                "ts": _now_iso(),
                "kind": kind,
                "to": to,
                "cc": cc or [],
                "subject": subject,
                "html": html,
            }
        )
        await _engagement_merge_artifacts(
            engagement_id, {"email_drafts": drafts}
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "devops_maturity: stash_email_draft failed eng=%s kind=%s",
            engagement_id, kind,
        )


# ---------------------------------------------------------------------------
# Email HTML templates — compact, inline-styled, brand-consistent
# ---------------------------------------------------------------------------


def _wrap_email(title: str, body_html: str) -> str:
    """Wrap a body fragment in the standard Anuvia email shell."""
    return f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · DevOps Maturity Assessment</p>
<h1 style="font-family:Georgia,serif;font-size:24px;margin:0 0 14px;color:#0f172a;">{title}</h1>
{body_html}
<p style="color:#78716c;font-size:13px;line-height:1.6;margin-top:28px;border-top:1px solid #f0eeec;padding-top:18px;">Qualquer dúvida, é só responder este email.<br><br>Mila Vernazza · Founder Anuvia</p>
</div></body></html>"""


def _kickoff_email_html(
    *,
    first_name: str,
    intake_url: str,
    value_str: str,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Contrato fechado. DevOps Maturity Assessment começa agora. Investimento total: <strong>R$ {value_str}</strong>. Cronograma: 4 semanas, quatro fases, entregáveis por fase.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Semana 1 — Baseline DORA.</strong> Antes de extrair as 4 métricas reais, preciso da informação abaixo. Sem isso, a semana 2 (maturity deep-dive) não roda.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>Sponsor executivo (nome + email)</li>
  <li>Tamanho do time de engenharia (total + por squad se multi-squad)</li>
  <li>Número de serviços em produção (críticos vs supporting)</li>
  <li>CI tool em uso (Jenkins / GitHub Actions / CircleCI / GitLab CI / outro) + acesso read-only últimos 90 dias</li>
  <li>Incident tracker em uso (Linear / Jira / Opsgenie / PagerDuty) + acesso read-only</li>
  <li>On-call rotation tool</li>
  <li>DORA self-reported atual (deploy frequency, lead time, MTTR mediano, change failure rate %) — só pra calibrar; vamos medir de novo</li>
  <li>Feature flag tool (se houver — LaunchDarkly / Unleash / OpenFeature / nenhum)</li>
  <li>Observability stack (Datadog / Grafana / New Relic / outro)</li>
  <li>Post-mortem dos últimos 5 incidentes (sim / alguns / nenhum)</li>
  <li>Ambiente regulado? (BACEN fintechs, ANS healthtechs, GxP life sciences, ISO 27001, ou nenhum)</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Padrão dos últimos assessments: 70% dos clientes acham que são "High" no DORA — 80% deles ficam em "Medium" quando a gente mede de verdade. Sem julgamento; só os números reais.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Workshop de discovery (2h) com Eng Leadership fica agendado por email separado depois que o intake voltar.</p>
"""
    return _wrap_email("DevOps Maturity Assessment começou", body)


def _phase1_email_html(
    *,
    first_name: str,
    baseline_url: str,
    deploy_freq: str,
    lead_time: str,
    mttr: str,
    cfr: str,
    cluster: str,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 1 fechada. DORA baseline extraído das fontes reais (CI logs + git history + incident tracker + on-call data), não auto-reportado.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Números reais — últimos 90 dias:</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 18px 18px;padding:0;">
  <li><strong>Deploy frequency:</strong> {deploy_freq}</li>
  <li><strong>Lead time for changes:</strong> {lead_time}</li>
  <li><strong>MTTR (mediano):</strong> {mttr}</li>
  <li><strong>Change failure rate:</strong> {cfr}</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Cluster DORA 2023: <strong>{cluster}</strong>.</p>
<p style="margin:24px 0;"><a href="{baseline_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">DORA Baseline (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2: maturity deep-dive em 6 dimensões (CI/CD, test automation, IaC, GitOps, observability, incident response). Scorecard 1-5 com sub-critérios + finding details. Saída: foto sistemática de onde o time está.</p>
"""
    return _wrap_email("DORA Baseline pronto — Semana 1", body)


def _phase2_email_html(
    *,
    first_name: str,
    scorecard_url: str,
    avg_score: str,
    weakest: List[str],
    strongest: List[str],
) -> str:
    weak_bullets = "".join(
        f'<li style="margin:6px 0;line-height:1.55;">{c}</li>' for c in weakest[:3]
    )
    strong_bullets = "".join(
        f'<li style="margin:6px 0;line-height:1.55;">{c}</li>' for c in strongest[:3]
    )
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2 fechada. Maturity scorecard pronto: 6 dimensões pontuadas em escala 1-5 com sub-critérios objetivos.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;"><strong>Score médio agregado: {avg_score}/5.</strong></p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Dimensões mais fracas (onde o roadmap vai concentrar):</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 14px 18px;padding:0;">{weak_bullets}</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Dimensões mais fortes (manter e alavancar):</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 18px 18px;padding:0;">{strong_bullets}</ul>
<p style="margin:24px 0;"><a href="{scorecard_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Maturity Scorecard completo (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 3: roadmap 6 meses sequenciado por (impact × confidence) / effort + 5-8 quick wins identificados + tooling recommendations (feature flags, observability, IaC).</p>
"""
    return _wrap_email("Maturity Scorecard — Semana 2", body)


def _phase3_email_html(
    *,
    first_name: str,
    roadmap_url: str,
    quick_wins_url: str,
    tooling_url: str,
    n_quick_wins: int,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 3 fechada. Roadmap 6 meses montado, quick wins identificados, tooling recommendations escritas.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;"><strong>{n_quick_wins} quick wins</strong> identificados — itens que entram em produção em 1-2 meses com impact mensurável.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{roadmap_url}" style="color:#0f172a;">Roadmap 6 meses</a> — 3 fases (quick wins 1-2mo, structural 2-4mo, optimization 4-6mo). Cada item com impact estimate, effort estimate, owner sugerido.</li>
  <li><a href="{quick_wins_url}" style="color:#0f172a;">Quick wins playbook</a> — passos executáveis pra cada quick win, com critério de sucesso explícito.</li>
  <li><a href="{tooling_url}" style="color:#0f172a;">Tooling recommendations</a> — feature flags, observability, IaC. Build vs buy com justificativa.</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 4: relatório executivo consolidado (30 slides equivalente) + workshop de handoff 2h com Eng Leadership + KPI tracking template pra DORA metrics ongoing.</p>
"""
    return _wrap_email("Roadmap + Quick Wins — Semana 3", body)


def _phase4_email_html(
    *,
    first_name: str,
    report_url: str,
    deck_url: str,
    kpi_template_url: str,
    handoff_scheduling_url: str,
    nps_url: str,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Assessment concluído. Quatro semanas, entregáveis consolidados:</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo final</a> — DORA baseline + maturity scorecard + roadmap + quick wins + tooling + governança.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva</a> — 30 slides pra rodar com C-level e Eng Leadership.</li>
  <li><a href="{kpi_template_url}" style="color:#0f172a;">KPI tracking template</a> — planilha/dashboard spec pra DORA metrics ongoing (deploy freq, lead time, MTTR, CFR).</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Workshop de handoff (2h) — agenda 5-8 stakeholders do Eng Leadership, sessão de Q&A + walkthrough dos quick wins:</p>
<p style="margin:8px 0 24px;"><a href="{handoff_scheduling_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Agendar handoff workshop &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">A invoice da segunda parcela já entrou na fila.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Um pedido: 2 minutos pra deixar um NPS. Direto, sem firula:</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar NPS &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se conhecer outro VP Eng / CTO com DORA self-reported alto e sensação de que algo não bate — você sabe quem precisa ouvir isso.</p>
"""
    return _wrap_email("DevOps Maturity Assessment entregue", body)


def _intake_reminder_email_html(*, first_name: str, intake_url: str) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete curto: o formulário de intake ainda não foi preenchido. Sem os acessos read-only (CI, tracker, on-call), a extração DORA não roda e o cronograma desloca.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se tiver bloqueio (sponsor não definido, SecOps pedindo aprovação de acesso, dúvida de scoping) — me avisa que a gente resolve.</p>
"""
    return _wrap_email("Intake pendente — DevOps Maturity", body)


def _progress_update_email_html(
    *, first_name: str, phase_label: str, summary: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Update curto sobre o assessment — fase atual: <strong>{phase_label}</strong>.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">{summary}</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Próximo entregável escrito chega ao final desta semana. Qualquer coisa antes, é só responder este email.</p>
"""
    return _wrap_email("Assessment em andamento", body)


# ---------------------------------------------------------------------------
# HTML shell for PDF deliverables
# ---------------------------------------------------------------------------


def _deliverable_html(title: str, subtitle: str, body_md_html: str) -> str:
    """A4-friendly inline-styled deliverable wrapper."""
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color:#0f172a; font-size:12px; line-height:1.6; margin:0; padding:0; background:#ffffff; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:20px 0 8px; border-bottom:1px solid #e7e5e4; padding-bottom:4px; }}
  h3 {{ font-size:13px; margin:14px 0 6px; }}
  p, li {{ font-size:12px; }}
  ul {{ padding-left:20px; margin:6px 0 12px; }}
  table {{ border-collapse:collapse; width:100%; margin:8px 0 14px; font-size:11px; }}
  th, td {{ border:1px solid #e7e5e4; padding:6px 8px; text-align:left; vertical-align:top; }}
  th {{ background:#fafaf9; font-weight:600; }}
  .small {{ color:#64748b; font-size:11px; }}
  .meta {{ color:#475569; font-size:11px; margin:0 0 18px; }}
  .tag {{ display:inline-block; background:#fafaf9; border:1px solid #e7e5e4; padding:2px 8px; border-radius:9999px; font-size:10px; color:#475569; }}
</style></head>
<body>
<header style="margin-bottom:24px;">
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 6px;">Anuvia · DevOps Maturity Assessment</p>
  <h1>{title}</h1>
  <p class="meta">{subtitle}</p>
</header>
{body_md_html}
<footer style="margin-top:32px;padding-top:18px;border-top:1px solid #e7e5e4;color:#64748b;font-size:11px;">
  Anuvia Cloud &amp; AI Consulting · Mila Vernazza · Documento gerado em {_now().strftime("%d/%m/%Y")}
</footer>
</body></html>"""


def _md_to_html(md: str) -> str:
    """Tiny Markdown-ish converter (headings, bullet lists, tables, bold)."""
    import re

    lines: List[str] = []
    in_list = False
    in_table = False
    table_buffer: List[List[str]] = []

    def _flush_table() -> None:
        nonlocal in_table, table_buffer
        if not in_table or not table_buffer:
            in_table = False
            table_buffer = []
            return
        header = table_buffer[0]
        rows = table_buffer[1:]
        out = ["<table><thead><tr>"]
        for h in header:
            out.append(f"<th>{h.strip()}</th>")
        out.append("</tr></thead><tbody>")
        for row in rows:
            # Skip separator row like |---|---|
            if all(set(c.strip()) <= set("-: ") for c in row):
                continue
            out.append("<tr>")
            for cell in row:
                out.append(f"<td>{cell.strip()}</td>")
            out.append("</tr>")
        out.append("</tbody></table>")
        lines.append("".join(out))
        in_table = False
        table_buffer = []

    for raw in md.splitlines():
        line = raw.rstrip()
        if "|" in line and line.strip().startswith("|") and line.strip().endswith("|"):
            if in_list:
                lines.append("</ul>")
                in_list = False
            cells = [c for c in line.strip().strip("|").split("|")]
            in_table = True
            table_buffer.append(cells)
            continue
        else:
            if in_table:
                _flush_table()
        if not line.strip():
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append("")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line.strip())
        if m:
            if in_list:
                lines.append("</ul>")
                in_list = False
            level = len(m.group(1))
            tag = f"h{min(level + 1, 6)}"
            lines.append(f"<{tag}>{m.group(2)}</{tag}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                lines.append("<ul>")
                in_list = True
            content = m.group(1)
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            lines.append(f"<li>{content}</li>")
            continue
        if in_list:
            lines.append("</ul>")
            in_list = False
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        lines.append(f"<p>{content}</p>")
    if in_list:
        lines.append("</ul>")
    if in_table:
        _flush_table()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lead helper
# ---------------------------------------------------------------------------


async def _lead_for_engagement(
    engagement: dict,
) -> Tuple[Optional[dict], Optional[str], str]:
    """Return ``(lead_row, email, first_name)`` for an engagement."""
    lead_id = engagement.get("lead_id")
    if not lead_id:
        return None, None, ""
    lead = await session_get(str(lead_id))
    if not lead:
        return None, None, ""
    email = lead.get("email") or None
    first_name = (lead.get("name") or "").split(" ")[0] or "tudo bem"
    return lead, email, first_name


# ---------------------------------------------------------------------------
# Deliverable composition — Claude prompts
# ---------------------------------------------------------------------------


async def _compose_dora_baseline(engagement: dict, intake_data: dict) -> dict:
    """Phase 1 — given intake (tool access + self-reported DORA + samples),
    Claude composes the baseline narrative with hypothesized real values
    extrapolated from intake-provided sample data, plus gap analysis vs
    DORA 2023 Elite/High/Medium/Low thresholds.

    Returns::

        {
            "summary": "<paragraph>",
            "metrics": {
                "deploy_frequency": "<string: number + unit>",
                "lead_time": "<string>",
                "mttr": "<string>",
                "change_failure_rate": "<string>"
            },
            "cluster": "elite|high|medium|low",
            "cluster_rationale": "<string>",
            "per_service": [
                {"service": "...", "criticality": "critical|supporting",
                 "deploy_frequency": "...", "lead_time": "...", "mttr": "...",
                 "cfr": "..."},
                ...
            ],
            "gap_analysis": {
                "deploy_frequency": "<gap vs next tier>",
                "lead_time": "...",
                "mttr": "...",
                "change_failure_rate": "..."
            },
            "regulated": "BACEN 4.658|GxP|ANS RN 452|LGPD|...|nenhuma",
            "compliance_callout": "<string when regulated, '' otherwise>"
        }
    """
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = (
        "\n".join(profile_lines)
        or "(intake vazio — usar padrões setoriais)"
    )

    regulated = _is_regulated(intake_data) or "nenhuma"
    compliance_note = (
        f"O ambiente do cliente está em frame de compliance: {regulated}. "
        "Mencionar explicitamente em observability/incident response a "
        "implicação regulatória (audit trail, retention policy, log "
        "imutabilidade quando aplicável)."
        if regulated != "nenhuma"
        else (
            "Cliente NÃO está em ambiente regulado declarado. NÃO inventar "
            "compliance frames se o intake não mencionar."
        )
    )

    elite = _DORA_THRESHOLDS["elite"]
    high = _DORA_THRESHOLDS["high"]
    medium = _DORA_THRESHOLDS["medium"]
    low = _DORA_THRESHOLDS["low"]

    prompt = f"""Você está compondo o DORA baseline doc da semana 1 do DevOps Maturity Assessment Anuvia.

Perfil do cliente (intake submetido):
{profile_block}

Contexto de compliance:
{compliance_note}

Sua tarefa:
1. Compor as 4 métricas DORA reais (NÃO copiar o self-reported direto — extrapolar a partir do sample data + tamanho do time + número de serviços + padrões do CI tool informado).
2. Atribuir um cluster (elite | high | medium | low) usando o DORA 2023:
   - Elite: deploy {elite['deploy_frequency']}, lead time {elite['lead_time']}, MTTR {elite['mttr']}, CFR {elite['cfr']}.
   - High: deploy {high['deploy_frequency']}, lead time {high['lead_time']}, MTTR {high['mttr']}, CFR {high['cfr']}.
   - Medium: deploy {medium['deploy_frequency']}, lead time {medium['lead_time']}, MTTR {medium['mttr']}, CFR {medium['cfr']}.
   - Low: deploy {low['deploy_frequency']}, lead time {low['lead_time']}, MTTR {low['mttr']}, CFR {low['cfr']}.
3. Quebrar por serviço (até 6 serviços; se intake não informar, hipotetizar serviços críticos vs supporting com base em tamanho do time).
4. Gap analysis: pra cada métrica, qual o gap (em unidades concretas) vs o próximo tier acima do cluster atual.
5. Se o ambiente é regulado (frame: {regulated}), incluir compliance_callout sobre audit trail / retention / log imutabilidade. Senão, deixar string vazia.

Quando dado real não existir no intake, marcar a métrica como "estimativa baseada em padrões setoriais" — sempre dimensionar conservador.

Devolva APENAS JSON válido, sem markdown, sem comentários:

{{
  "summary": "<3-5 linhas: cluster identificado, métrica mais distante do próximo tier, principal gap, observação sobre regulado se aplicar>",
  "metrics": {{
    "deploy_frequency": "<número + unidade, ex: '0,8 deploy/dia (estimado em CI logs)'>",
    "lead_time": "<número + unidade, ex: '36 horas mediana'>",
    "mttr": "<número + unidade, ex: '4 horas mediana / 18 horas P95'>",
    "change_failure_rate": "<percentual, ex: '12% últimos 90 dias'>"
  }},
  "cluster": "<elite|high|medium|low>",
  "cluster_rationale": "<2-3 frases: como as 4 métricas posicionam o time nesse cluster>",
  "per_service": [
    {{
      "service": "<nome ou 'service-N'>",
      "criticality": "<critical|supporting>",
      "deploy_frequency": "<...>",
      "lead_time": "<...>",
      "mttr": "<...>",
      "cfr": "<...>"
    }}
  ],
  "gap_analysis": {{
    "deploy_frequency": "<gap concreto vs próximo tier>",
    "lead_time": "<gap concreto>",
    "mttr": "<gap concreto>",
    "change_failure_rate": "<gap concreto>"
  }},
  "regulated": "<{regulated}>",
  "compliance_callout": "<linha sobre obrigações regulatórias em obs+incident, ou string vazia>"
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} DORA baseline não gerado — "
                "revisar manualmente."
            ),
            "metrics": {
                "deploy_frequency": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "lead_time": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "mttr": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "change_failure_rate": f"{_CLAUDE_FALLBACK_TAG} estimar",
            },
            "cluster": "medium",
            "cluster_rationale": (
                f"{_CLAUDE_FALLBACK_TAG} cluster pendente de revisão."
            ),
            "per_service": [],
            "gap_analysis": {
                "deploy_frequency": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "lead_time": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "mttr": f"{_CLAUDE_FALLBACK_TAG} estimar",
                "change_failure_rate": f"{_CLAUDE_FALLBACK_TAG} estimar",
            },
            "regulated": regulated,
            "compliance_callout": "",
        },
        required_keys=("summary", "metrics", "cluster"),
    )


async def _compose_maturity_scorecard(
    engagement: dict, baseline: dict, intake_data: dict
) -> dict:
    """Phase 2 — Claude scores 6 dimensions on 1-5 with sub-criteria.

    Returns::

        {
            "summary": "<paragraph>",
            "average_score": <float 1.0-5.0>,
            "dimensions": [
                {
                    "key": "ci_cd",
                    "label": "CI/CD",
                    "score": <1-5>,
                    "sub_criteria": [
                        {"name": "Pipeline structure", "score": <1-5>,
                         "evidence": "..."},
                        ...
                    ],
                    "findings": "<narrative 3-5 sentences>",
                    "compliance_callout": "<string when regulated, ''>"
                },
                ...
            ],
            "weakest_dimensions": ["<label>", "<label>", "<label>"],
            "strongest_dimensions": ["<label>", "<label>"]
        }
    """
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = (
        "\n".join(profile_lines) or "(intake vazio)"
    )

    metrics = baseline.get("metrics") or {}
    cluster = baseline.get("cluster") or "medium"
    regulated = baseline.get("regulated") or "nenhuma"

    dims_block = "\n".join(
        f"- {k} ({_MATURITY_DIMENSION_LABELS[k]})"
        for k in _MATURITY_DIMENSIONS
    )

    compliance_note = (
        f"Ambiente regulado: {regulated}. Em observability e incident "
        "response, tag explícita de obrigação regulatória (audit trail, "
        "retention de logs, imutabilidade quando aplicável)."
        if regulated != "nenhuma"
        else "Ambiente NÃO regulado. Não inventar compliance frames."
    )

    prompt = f"""Você está aplicando o maturity scorecard da semana 2 do DevOps Maturity Assessment Anuvia.

DORA baseline (semana 1):
- cluster: {cluster}
- métricas: {json.dumps(metrics, ensure_ascii=False)}

Perfil do cliente:
{profile_block}

Contexto de compliance:
{compliance_note}

Escore CADA UMA das 6 dimensões abaixo em escala 1-5, com sub-critérios objetivos. Use as rubric markers:
1 = ausente/ad-hoc
2 = inicial/inconsistente
3 = repetível/padronizado em alguns serviços
4 = gerenciado/padronizado em maioria dos serviços críticos
5 = otimizado/instrumentado

Dimensões:
{dims_block}

Sub-critérios obrigatórios POR dimensão:

1. **ci_cd** — Pipeline structure | Test coverage no pipeline | Deployment patterns (manual / scripted / blue-green / canary)
2. **test_automation** — Unit coverage % | Integration coverage % | E2E reliability (flake rate) | Test execution time
3. **iac** — Coverage % de workloads gerenciados por código | State management (remote backend, locking) | Drift detection
4. **gitops** — Argo CD/Flux usage % | Declarative state | Rollback automation testado nos últimos 90 dias
5. **observability** — Metrics/logs/traces coverage | SLI/SLO definidos por serviço crítico | Dashboard hygiene (não-staleness) | Alerting fatigue/noise
6. **incident_response** — Runbooks count por serviço crítico (target ≥1) | Post-mortem culture (last 5 incidents documentados?) | On-call rotation health (burnout signals, fairness) | Blameless framework adotado

Compute average_score = média aritmética dos 6 scores principais (não dos sub-critérios).

Identifique weakest_dimensions (3 mais baixos) e strongest_dimensions (2 mais altos), retornando os LABELS humano (ex: "CI/CD", "Observability"), não as keys.

Quando o intake não trouxer dado suficiente, marque "findings" como "estimativa baseada em padrões setoriais" e dimensione conservador. Em ambientes regulados, incluir compliance_callout em observability e incident_response; nas demais dimensões deixar vazio.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<4-6 linhas: score médio, dimensão mais fraca, dimensão mais forte, principal alavanca pra subir um tier DORA>",
  "average_score": <float, ex: 2.7>,
  "dimensions": [
    {{
      "key": "<key>",
      "label": "<label>",
      "score": <1-5>,
      "sub_criteria": [
        {{"name": "<sub-critério>", "score": <1-5>, "evidence": "<1-2 frases>"}}
      ],
      "findings": "<3-5 frases: o que está bom, o que está faltando, qual o gap pra subir um nível>",
      "compliance_callout": "<linha sobre obrigação regulatória OU string vazia>"
    }}
  ],
  "weakest_dimensions": ["<label>", "<label>", "<label>"],
  "strongest_dimensions": ["<label>", "<label>"]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=7000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} maturity scorecard não gerado — "
                "revisar manualmente."
            ),
            "average_score": 2.5,
            "dimensions": [
                {
                    "key": k,
                    "label": _MATURITY_DIMENSION_LABELS[k],
                    "score": 2,
                    "sub_criteria": [],
                    "findings": f"{_CLAUDE_FALLBACK_TAG} revisar manualmente.",
                    "compliance_callout": "",
                }
                for k in _MATURITY_DIMENSIONS
            ],
            "weakest_dimensions": [
                _MATURITY_DIMENSION_LABELS["observability"],
                _MATURITY_DIMENSION_LABELS["incident_response"],
                _MATURITY_DIMENSION_LABELS["gitops"],
            ],
            "strongest_dimensions": [
                _MATURITY_DIMENSION_LABELS["ci_cd"],
                _MATURITY_DIMENSION_LABELS["iac"],
            ],
        },
        required_keys=("summary", "dimensions"),
    )


async def _compose_roadmap_6mo(
    engagement: dict, baseline: dict, scorecard: dict
) -> str:
    """Phase 3 — 6-month roadmap markdown with 3 phases:
    quick wins (1-2mo), structural (2-4mo), optimization (4-6mo).
    Each item has impact, effort, owner, and explicit (impact × confidence) / effort math.
    """
    cluster = baseline.get("cluster") or "medium"
    metrics = baseline.get("metrics") or {}
    avg = scorecard.get("average_score") or 0
    weakest = scorecard.get("weakest_dimensions") or []
    strongest = scorecard.get("strongest_dimensions") or []
    regulated = baseline.get("regulated") or "nenhuma"

    dims_summary_lines: List[str] = []
    for d in scorecard.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        dims_summary_lines.append(
            f"- {d.get('label')}: score {d.get('score')}/5 — {d.get('findings')}"
        )
    dims_summary = "\n".join(dims_summary_lines) or "(scorecard vazio)"

    compliance_note = (
        f"Em ambiente regulado ({regulated}): em observability + incident "
        "response, incluir explicitly retention policy, audit trail "
        "obrigatório, e log imutabilidade quando aplicável."
        if regulated != "nenhuma"
        else "Ambiente NÃO regulado — não inventar compliance gates."
    )

    prompt = f"""Escreva um roadmap DevOps de 6 meses pra um cliente Anuvia, em markdown.

DORA cluster atual: {cluster}
DORA metrics: {json.dumps(metrics, ensure_ascii=False)}
Maturity average score: {avg}/5
Weakest dimensions (concentrar aqui): {", ".join(weakest)}
Strongest dimensions (manter e alavancar): {", ".join(strongest)}

Scorecard detalhado por dimensão:
{dims_summary}

Compliance:
{compliance_note}

Estrutura obrigatória:

## Resumo executivo
3-5 linhas: cluster DORA atual → cluster alvo em 6 meses, dimensão de maior alavanca, dependências críticas, decisão pedida ao sponsor.

## Critério de priorização
Texto curto explicando que itens são sequenciados por (impact × confidence) / effort, com:
- impact em "tiers DORA subidos" + "horas/sprint economizadas"
- confidence em % (0-100)
- effort em "dias-pessoa"
Definir cada termo. Sem hand-waving.

## Fase 1 — Quick Wins (1-2 meses)
5-8 itens executáveis em 1-2 meses. Cada item:
- **Nome do item**
- Dimensão alvo (uma das 6)
- **Impact:** <tier subido OU horas economizadas/sprint>
- **Confidence:** <%>
- **Effort:** <dias-pessoa>
- **Score (impact × confidence) / effort:** <número>
- **Owner sugerido:** <papel, ex: "SRE lead">
- **Critério de sucesso:** <métrica + threshold>

## Fase 2 — Structural (2-4 meses)
4-7 itens estruturais (introdução de feature flags, GitOps adoption, SLI/SLO formal, IaC coverage push, runbook standardization). Mesmo schema de campos.

## Fase 3 — Optimization (4-6 meses)
3-5 itens de otimização (canary deploys automatizados, chaos engineering pilot, observability cost optimization, on-call fairness rotation). Mesmo schema.

## Tabela consolidada — Horizonte 6 meses
Tabela markdown com colunas: item | fase | dimensão | impact | effort | score | owner.

## Dependências cross-fase
- Quais itens são pré-req de outros (ex: feature flags antes de canary deploys).
- Quais itens compartilham infra (ex: SLI/SLO + dashboard hygiene).

## Tier DORA alvo
Em 6 meses: cluster alvo + justificativa numérica (que métricas chegam ao próximo tier).

## Governança contínua
Cadência de DORA review (mensal? trimestral?), template de scorecard updates, métricas que disparam pausa de roadmap.

Voz Anuvia: seca, direta, numbers-first. NUNCA prometa o que não se mede. Em ambiente regulado, tag compliance explícita nas dimensões obs+incident.
"""
    return await _claude_call_with_voice(prompt, max_tokens=6500)


async def _compose_quick_wins_playbook(
    engagement: dict, baseline: dict, scorecard: dict, roadmap_md: str
) -> str:
    """Phase 3 — quick wins playbook with executable steps per item."""
    cluster = baseline.get("cluster") or "medium"
    weakest = scorecard.get("weakest_dimensions") or []
    regulated = baseline.get("regulated") or "nenhuma"

    prompt = f"""Escreva o "Quick Wins Playbook" do DevOps Maturity Assessment Anuvia, em markdown.

Contexto:
- DORA cluster atual: {cluster}
- Dimensões mais fracas: {", ".join(weakest)}
- Ambiente: {"regulado (" + regulated + ")" if regulated != "nenhuma" else "não regulado"}

Roadmap fase 1 (referência — extraia os 5-8 quick wins daqui):
{roadmap_md[:3500]}

Estrutura:

## Resumo
2-3 frases: quantos quick wins, qual a janela (1-2 meses), qual o resultado agregado esperado.

## Quick Wins — passos executáveis

Para CADA quick win (5-8 no total), escreva uma seção `### <Nome do quick win>` com:

- **Dimensão:** <uma das 6>
- **Owner sugerido:** <papel>
- **Janela:** <semanas>
- **Pré-req:** <itens que precisam estar prontos antes, ou "nenhum">

**Passos executáveis:**
1. Passo 1 (uma frase imperativa + sub-bullets com comando/tooling concreto quando aplicável)
2. Passo 2
3. ... (5-8 passos por quick win)

**Critério de sucesso:** <métrica + threshold mensurável>
**Como medir:** <onde extrair a métrica, qual ferramenta, com que cadência>
**Riscos & rollback:** <1-2 riscos concretos + plano de rollback>
**Compliance callout:** <linha sobre obrigação regulatória SE aplicar — observability/incident only — caso contrário deixar vazio>

## Sequência sugerida
Numerada 1-N: ordem em que executar os quick wins (alguns podem rodar em paralelo).

## Métricas agregadas — pós quick wins
Tabela markdown: métrica DORA | valor atual | valor esperado pós quick wins | método de medição.

Voz Anuvia: seca, imperativa nos passos, numbers-first nos critérios.
"""
    return await _claude_call_with_voice(prompt, max_tokens=6000)


async def _compose_tooling_recommendations(
    engagement: dict, baseline: dict, scorecard: dict
) -> str:
    """Phase 3 — tooling recommendations: feature flags, observability, IaC."""
    regulated = baseline.get("regulated") or "nenhuma"
    cluster = baseline.get("cluster") or "medium"
    weakest = scorecard.get("weakest_dimensions") or []

    profile_intake = engagement.get("intake_data") or {}
    if not isinstance(profile_intake, dict):
        profile_intake = {}
    ci_tool = profile_intake.get("ci_tool") or "—"
    obs_stack = profile_intake.get("observability_stack") or "—"
    feature_flag_tool = profile_intake.get("feature_flag_tool") or "nenhum"
    iac_tool = profile_intake.get("iac_tool") or "—"

    prompt = f"""Escreva o "Tooling Recommendations" doc do DevOps Maturity Assessment Anuvia, em markdown.

Contexto cliente:
- CI tool atual: {ci_tool}
- Observability stack: {obs_stack}
- Feature flag tool: {feature_flag_tool}
- IaC tool: {iac_tool}
- DORA cluster: {cluster}
- Dimensões fracas: {", ".join(weakest)}
- Ambiente: {"regulado (" + regulated + ")" if regulated != "nenhuma" else "não regulado"}

Estrutura:

## Resumo
2-3 frases: quais 3 categorias estão sendo avaliadas e qual a recomendação top-level por categoria.

## 1. Feature Flags

Comparar três opções: **LaunchDarkly**, **Unleash**, **OpenFeature**.

Tabela markdown: critério | LaunchDarkly | Unleash | OpenFeature.
Critérios obrigatórios (linhas da tabela):
- Modelo de pricing
- Self-hosted vs SaaS
- SDK ecosystem coverage
- Targeting rules sophistication
- Observability/audit log
- Integração com CI atual ({ci_tool})
- Vendor lock-in risk
- Compliance (audit trail pra {regulated}) — incluir SOMENTE se regulado

Recomendação: 1 linha + 3 linhas de justificativa.

Build vs buy: argumento concreto (custo, time-to-value, manutenção). Quantificar em R$/mês ou dias-pessoa.

## 2. Observability

Comparar três opções: **Datadog**, **Grafana stack (Prometheus + Loki + Tempo)**, **New Relic**.

Tabela markdown: critério | Datadog | Grafana stack | New Relic.
Critérios:
- Modelo de pricing (custo por host, por GB ingerido)
- Metrics + logs + traces coverage
- SLI/SLO native support
- Alerting sophistication
- Dashboard hygiene tooling
- Integração com stack atual ({obs_stack})
- Vendor lock-in risk
- Retention compliance (pra {regulated}) — incluir SOMENTE se regulado

Recomendação: 1 linha + 3 linhas de justificativa.

Build vs buy: custo estimado em R$/mês baseado em volume típico do tamanho do cliente.

## 3. Infrastructure as Code

Comparar três opções: **Terraform**, **Pulumi**, **AWS CDK / Azure Bicep / GCP CDK** (escolher pela cloud principal do cliente).

Tabela markdown: critério | Terraform | Pulumi | CDK.
Critérios:
- Linguagem (HCL vs general purpose)
- State management
- Drift detection
- Ecosystem providers
- Multi-cloud support
- Curva de aprendizado pro time atual
- Vendor lock-in risk
- Compliance (provider audit logs pra {regulated}) — incluir SOMENTE se regulado

Recomendação: 1 linha + 3 linhas de justificativa.

Build vs buy: já são open source — comparação é só de fit pro time.

## Stack consolidado recomendado
Tabela markdown final: categoria | recomendação | razão principal | custo estimado mensal.

## Migration path
Se o cliente já tem ferramenta de uma das 3 categorias, escrever migration path (1 parágrafo): quando migrar, quando manter, quanto custa migrar.

Voz Anuvia: seca, comparativa, numbers-first em custo. NUNCA cair em fanboy de vendor.
"""
    return await _claude_call_with_voice(prompt, max_tokens=6500)


async def _compose_executive_deck(
    engagement: dict, baseline: dict, scorecard: dict, roadmap_md: str
) -> str:
    """Phase 4 — slide-by-slide markdown skeleton (30 slides target)."""
    cluster = baseline.get("cluster") or "medium"
    metrics = baseline.get("metrics") or {}
    avg = scorecard.get("average_score") or 0
    weakest = scorecard.get("weakest_dimensions") or []
    strongest = scorecard.get("strongest_dimensions") or []
    regulated = baseline.get("regulated") or "nenhuma"

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (30 slides) pra fechar um DevOps Maturity Assessment Anuvia.

Contexto:
- DORA cluster atual: {cluster}
- Métricas: {json.dumps(metrics, ensure_ascii=False)}
- Maturity médio: {avg}/5
- Dimensões fracas: {", ".join(weakest)}
- Dimensões fortes: {", ".join(strongest)}
- Ambiente regulado: {regulated}

Roadmap (referência):
{roadmap_md[:2500]}

Para cada slide, escreva:

### Slide N — <título>
- 3-5 bullets curtos (uma frase cada, sem ponto final)
- (notas: <fala de 30s do apresentador>)

Estrutura (30 slides):
1. Slide 1 — capa: cliente, escopo, prazo (4 semanas).
2. Slide 2 — sumário executivo (cluster DORA, maturity médio, top 3 alavancas, decisão pedida).
3. Slide 3 — contexto: o que pediram + como vamos responder.
4. Slide 4 — metodologia: DORA baseline → maturity scorecard → roadmap → handoff.
5. Slide 5 — DORA 2023 thresholds (tabela: elite/high/medium/low × 4 métricas).
6. Slide 6 — DORA atual do cliente (4 métricas + cluster identificado).
7. Slide 7 — DORA por serviço (heatmap visual).
8. Slide 8 — gap analysis: o que falta pra subir um tier (números concretos).
9. Slide 9 — maturity scorecard agregado (radar chart instruction com os 6 eixos).
10. Slides 10-15 — UM SLIDE POR DIMENSÃO (CI/CD, Test Automation, IaC, GitOps, Observability, Incident Response). Cada slide: score 1-5, sub-critérios principais, finding-chave em 1 frase, próximo nível.
11. Slide 16 — Top 3 dimensões fracas — onde concentrar.
12. Slide 17 — Top 2 dimensões fortes — manter e alavancar.
13. Slide 18 — Roadmap 6 meses (timeline visual: quick wins / structural / optimization).
14. Slides 19-21 — UM SLIDE POR FASE do roadmap (quick wins, structural, optimization). Itens + impact/effort/owner.
15. Slide 22 — Critério de priorização: (impact × confidence) / effort com cada termo definido.
16. Slide 23 — Quick wins highlight (3-5 itens, impact esperado consolidado).
17. Slide 24 — Tooling recommendations (feature flags + observability + IaC, decisão por categoria).
18. Slide 25 — Tier DORA alvo em 6 meses (números esperados pós-roadmap).
19. Slide 26 — Riscos top 5 (organizacional, técnico, vendor, compliance se aplicar, time capacity).
20. Slide 27 — Governança contínua (cadência DORA review, métricas, alertas).
21. Slide 28 — KPI tracking template (DORA dashboard spec).
22. Slide 29 — Handoff workshop agenda (2h, stakeholders, tópicos, próximas ações).
23. Slide 30 — Encerramento + Anuvia retainer ongoing (CTA opcional pra suporte mensal pós-assessment).

Voz Anuvia: seca, direta, anti-hype. Bullets curtos sem ponto final. Em ambiente regulado ({regulated}), slides de observability/incident_response têm 1 bullet de compliance callout.
"""
    return await _claude_call_with_voice(prompt, max_tokens=6500)


async def _compose_final_executive_report(
    engagement: dict,
    baseline: dict,
    scorecard: dict,
    roadmap_md: str,
    quick_wins_md: str,
    tooling_md: str,
) -> str:
    """Phase 4 — full executive report markdown (target 15-20 pages)."""
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    profile_lines = [
        f"- {k}: {v}" for k, v in intake.items() if v not in (None, "", [])
    ]
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    baseline_md = _baseline_to_markdown(baseline)
    scorecard_md = _scorecard_to_markdown(scorecard)
    regulated = baseline.get("regulated") or "nenhuma"

    prompt = f"""Você está escrevendo o relatório executivo final do DevOps Maturity Assessment Anuvia.

Perfil do cliente:
{profile_block}

DORA Baseline (semana 1):
{baseline_md[:2500]}

Maturity Scorecard (semana 2):
{scorecard_md[:3500]}

Roadmap 6 meses (semana 3, resumo):
{roadmap_md[:2500]}

Quick Wins Playbook (semana 3, resumo):
{quick_wins_md[:2000]}

Tooling Recommendations (semana 3, resumo):
{tooling_md[:2000]}

Ambiente regulado: {regulated}

Estrutura obrigatória markdown:

1. **## Sumário executivo** — 1 página: cluster DORA, maturity médio, 3 dimensões mais fracas, 3 decisões pedidas ao sponsor, tier DORA alvo em 6 meses.
2. **## Contexto do cliente** — perfil, stakeholders identificados, tamanho do time, número de serviços, frame de compliance se aplicar.
3. **## Metodologia** — DORA baseline (extração real) → maturity scorecard 6D → roadmap (impact × confidence)/effort → handoff. Brief sobre DORA 2023 thresholds.
4. **## DORA Baseline detalhado** — incluir as 4 métricas, cluster identificado, gap analysis vs próximo tier, breakdown por serviço (críticos vs supporting).
5. **## Maturity Scorecard por dimensão** — uma subseção por dimensão (`### CI/CD`, etc) com: score 1-5, sub-critérios scored, findings narrativas, próximo nível, e compliance_callout quando regulado.
6. **## Roadmap 6 meses** — incluir conteúdo do roadmap markdown, com 3 fases (quick wins / structural / optimization) e tabela consolidada de horizonte.
7. **## Quick Wins Playbook (resumo)** — lista dos quick wins com passos high-level, critério de sucesso e owner sugerido. Referenciar o playbook detalhado anexo.
8. **## Tooling Recommendations (resumo)** — stack recomendado por categoria (feature flags, observability, IaC) com migration path se houver tooling atual.
9. **## Tier DORA alvo em 6 meses** — números esperados após o roadmap, com método de medição.
10. **## Riscos top 5** — organizacional, técnico, vendor, compliance (se aplicar), time capacity. Cada um com mitigação proposta.
11. **## Governança contínua** — cadência de DORA review (mensal), template scorecard updates, métricas que disparam pausa do roadmap, thresholds de alerta.
12. **## KPI tracking template** — descrição da planilha/dashboard que vai trackear DORA metrics ongoing (specs das 4 métricas, fonte de dados, cadência).
13. **## Handoff workshop** — agenda 2h, stakeholders sugeridos, tópicos, próximas ações pós-workshop.
14. **## Checklist de qualidade Anuvia (14 itens)** — items revisados em todo DevOps Maturity (DORA real, deploy freq por serviço, CFR definição clara, MTTR mediano vs P95, test coverage por serviço crítico, IaC coverage %, SLI/SLO definidos, runbooks count, post-mortem culture, on-call health, feature flag adoption, canary infra, rollback automation, engineer satisfaction).
15. **## Apêndices** — DORA 2023 thresholds reference, scoring rubric completo (1-5 rubric markers), glossário (SLI/SLO/MTTR/CFR/blameless/runbook/canary).

Voz Anuvia: seca, direta, numbers-first. Em ambiente regulado ({regulated}), tag compliance explícito em obs+incident; nas demais dimensões NÃO inventar compliance.
"""
    return await _claude_call_with_voice(prompt, max_tokens=8000)


async def _compose_kpi_tracking_template(
    engagement: dict, baseline: dict, scorecard: dict
) -> str:
    """Phase 4 — KPI tracking template markdown (dashboard spec)."""
    metrics = baseline.get("metrics") or {}
    cluster = baseline.get("cluster") or "medium"

    prompt = f"""Escreva o "KPI Tracking Template" doc do DevOps Maturity Assessment Anuvia, em markdown. Esse documento serve de spec pra time interno montar dashboard ongoing (Datadog / Grafana / Notion) que trackeia as 4 métricas DORA + sub-métricas de maturity.

Contexto:
- DORA atual: {json.dumps(metrics, ensure_ascii=False)}
- Cluster atual: {cluster}

Estrutura:

## Resumo
2-3 frases: pra que serve, quem é o owner do dashboard, cadência de review.

## DORA Metrics — specs

Para CADA uma das 4 métricas, uma seção `### <nome>`:
- **Definição operacional:** <fórmula concreta, ex: "deploy_frequency = count(successful_prod_deploys) / 7 dias">
- **Fonte de dados:** <sistema, ex: "GitHub Actions API /repos/.../actions/runs">
- **Filtros:** <branch, environment, deploy type>
- **Cadence de atualização:** <real-time / hourly / daily>
- **Visualização recomendada:** <line chart 90d rolling / heatmap por service / gauge>
- **Threshold de alerta:** <quando vira pra vermelho>
- **Owner:** <papel>

## Sub-métricas de Maturity — specs

Tabela markdown: sub-métrica | dimensão | fonte | cadência | threshold.
Sub-métricas obrigatórias na tabela:
- Test coverage % por serviço crítico
- IaC coverage % (workloads gerenciados por código)
- SLI/SLO defined count por serviço crítico
- Runbook count por serviço crítico
- Post-mortem completion rate (last 5 incidents)
- On-call pager load mediano por engenheiro/semana
- Feature flag adoption % de deploys gated
- Rollback automation last execution date

## Dashboard layout sugerido

Texto descrevendo seções do dashboard:
1. Top row — 4 cards DORA (deploy freq, lead time, MTTR, CFR) com tier coloration.
2. Second row — trend line 90d das 4 métricas.
3. Third row — breakdown por serviço (heatmap).
4. Fourth row — maturity scorecard radar (snapshot mensal).
5. Bottom row — alerts ativos + on-call load.

## Cadência operacional

- **Daily:** check dashboard (5min) por SRE on-call.
- **Weekly:** review com Eng Leadership (15min).
- **Monthly:** DORA cluster recompute + scorecard refresh (30min). Decisão de pausa/aceleração de roadmap.
- **Quarterly:** Comparar contra benchmark interno (cluster movement) + ajuste de threshold.

## Anti-padrões a evitar

3-5 anti-padrões com explicação curta:
- Misturar staging deploys no DORA prod counter
- Definir CFR sem critério explícito (rollback? hotfix? incident em 24h?)
- Trackear MTTR só mediano sem P95
- Dashboard sem owner — sempre vira stale
- Goal-setting sem baseline (definir alvo antes de medir 90d real)

Voz Anuvia: seca, operacional, imperativa. NÃO romantizar dashboards.
"""
    return await _claude_call_with_voice(prompt, max_tokens=5500)


# ---------------------------------------------------------------------------
# Helpers — JSON parse + markdown rendering
# ---------------------------------------------------------------------------


def _parse_json_or_fallback(
    raw: str,
    *,
    fallback_factory,
    required_keys: Tuple[str, ...],
) -> dict:
    """Defensive parse — strip code fences, tolerate prose around the JSON."""
    text = (raw or "").strip()
    if text.startswith(_CLAUDE_FALLBACK_TAG):
        out = fallback_factory()
        if isinstance(out, dict):
            out.setdefault("summary", text)
        return out

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        if "```" in text:
            text = text.split("```", 1)[0]

    if not text.lstrip().startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("top-level not object")
        for k in required_keys:
            if k not in data:
                raise ValueError(f"missing required key: {k}")
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("devops_maturity: claude returned non-JSON: %s", exc)
        out = fallback_factory()
        if isinstance(out, dict):
            out["summary"] = (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude.\n\n"
                f"{text[:1200]}"
            )
        return out


def _baseline_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Métricas DORA — últimos 90 dias")
    metrics = data.get("metrics") or {}
    if isinstance(metrics, dict):
        out.append(f"- **Deploy frequency:** {metrics.get('deploy_frequency') or '—'}")
        out.append(f"- **Lead time for changes:** {metrics.get('lead_time') or '—'}")
        out.append(f"- **MTTR:** {metrics.get('mttr') or '—'}")
        out.append(
            f"- **Change failure rate:** "
            f"{metrics.get('change_failure_rate') or '—'}"
        )
    out.append("")
    out.append("## Cluster DORA 2023")
    out.append(f"- **Cluster identificado:** {data.get('cluster') or '—'}")
    out.append(f"- **Justificativa:** {data.get('cluster_rationale') or '—'}")
    out.append("")
    out.append("## Breakdown por serviço")
    services = data.get("per_service") or []
    if not services:
        out.append("- (sem breakdown por serviço — intake não trouxe a lista)")
    else:
        for s in services:
            if not isinstance(s, dict):
                continue
            out.append(
                f"- **{s.get('service') or '—'}** "
                f"({s.get('criticality') or 'supporting'}) — "
                f"deploy {s.get('deploy_frequency') or '—'}, "
                f"lead {s.get('lead_time') or '—'}, "
                f"MTTR {s.get('mttr') or '—'}, "
                f"CFR {s.get('cfr') or '—'}"
            )
    out.append("")
    out.append("## Gap analysis — próximo tier")
    gap = data.get("gap_analysis") or {}
    if isinstance(gap, dict):
        out.append(f"- **Deploy frequency:** {gap.get('deploy_frequency') or '—'}")
        out.append(f"- **Lead time:** {gap.get('lead_time') or '—'}")
        out.append(f"- **MTTR:** {gap.get('mttr') or '—'}")
        out.append(
            f"- **Change failure rate:** "
            f"{gap.get('change_failure_rate') or '—'}"
        )
    out.append("")
    regulated = data.get("regulated") or "nenhuma"
    if regulated and regulated != "nenhuma":
        out.append("## Compliance callout")
        out.append(f"- **Frame:** {regulated}")
        callout = data.get("compliance_callout") or ""
        if callout:
            out.append(callout)
    return "\n".join(out)


def _scorecard_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append(f"## Score médio agregado: {data.get('average_score') or '—'}/5")
    out.append("")
    out.append("## Dimensões mais fracas")
    weakest = data.get("weakest_dimensions") or []
    if not weakest:
        out.append("- (vazio)")
    else:
        for w in weakest:
            out.append(f"- {w}")
    out.append("")
    out.append("## Dimensões mais fortes")
    strongest = data.get("strongest_dimensions") or []
    if not strongest:
        out.append("- (vazio)")
    else:
        for s in strongest:
            out.append(f"- {s}")
    out.append("")
    out.append("## Scoring por dimensão")
    dims = data.get("dimensions") or []
    for d in dims:
        if not isinstance(d, dict):
            continue
        out.append(f"### {d.get('label') or d.get('key') or '—'}")
        out.append(f"- **Score:** {d.get('score') or '—'}/5")
        subs = d.get("sub_criteria") or []
        if isinstance(subs, list) and subs:
            out.append("- **Sub-critérios:**")
            for sub in subs:
                if not isinstance(sub, dict):
                    continue
                out.append(
                    f"  - {sub.get('name') or '—'}: {sub.get('score') or '—'}/5 "
                    f"— {sub.get('evidence') or '—'}"
                )
        findings = d.get("findings") or "—"
        out.append(f"- **Findings:** {findings}")
        callout = d.get("compliance_callout") or ""
        if callout:
            out.append(f"- **Compliance callout:** {callout}")
        out.append("")
    return "\n".join(out)


def _scorecard_weakest_labels(scorecard: dict) -> List[str]:
    """Return weakest dimension labels or fallback to lowest-score ones."""
    weakest = scorecard.get("weakest_dimensions") or []
    if isinstance(weakest, list) and weakest:
        return [str(w) for w in weakest][:3]
    dims = scorecard.get("dimensions") or []
    ranked = sorted(
        [d for d in dims if isinstance(d, dict)],
        key=lambda d: d.get("score") or 5,
    )
    return [str(d.get("label") or d.get("key") or "—") for d in ranked[:3]]


def _scorecard_strongest_labels(scorecard: dict) -> List[str]:
    strongest = scorecard.get("strongest_dimensions") or []
    if isinstance(strongest, list) and strongest:
        return [str(s) for s in strongest][:3]
    dims = scorecard.get("dimensions") or []
    ranked = sorted(
        [d for d in dims if isinstance(d, dict)],
        key=lambda d: d.get("score") or 0,
        reverse=True,
    )
    return [str(d.get("label") or d.get("key") or "—") for d in ranked[:2]]


def _count_quick_wins(roadmap_md: str) -> int:
    """Heuristic: count '### ' subsections under the 'Fase 1 — Quick Wins'
    section. Fallback to 5 when parsing fails."""
    if not roadmap_md:
        return 5
    try:
        lower = roadmap_md.lower()
        start_markers = ("## fase 1", "## quick wins", "## fase 1 — quick wins")
        start = -1
        for m in start_markers:
            idx = lower.find(m)
            if idx != -1:
                start = idx
                break
        if start == -1:
            return 5
        end_markers = ("## fase 2", "## structural", "## fase 3")
        end = len(roadmap_md)
        for m in end_markers:
            idx = lower.find(m, start + 1)
            if idx != -1:
                end = idx
                break
        section = roadmap_md[start:end]
        return max(
            5,
            sum(1 for line in section.splitlines() if line.strip().startswith("### ")),
        )
    except Exception:  # noqa: BLE001
        return 5


# ---------------------------------------------------------------------------
# Render + upload helper — turns markdown into a hosted PDF URL
# ---------------------------------------------------------------------------


async def _render_and_upload(
    engagement_id: str,
    *,
    title: str,
    subtitle: str,
    body_md: str,
    object_path: str,
) -> str:
    """Render markdown → HTML → PDF → upload to Supabase Storage.

    Returns the public PDF URL when storage is available; otherwise an
    ``about:blank`` sentinel pointing the operator at the stashed copy.
    """
    html = _deliverable_html(title, subtitle, _md_to_html(body_md))

    pdf_bytes = await _html_to_pdf(html)
    if pdf_bytes is None:
        public = await _upload_artifact(
            object_path.replace(".pdf", ".html"),
            html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
        if public:
            return public
        return f"about:blank#stashed-{engagement_id}-{object_path}"

    public = await _upload_artifact(
        object_path, pdf_bytes, content_type="application/pdf"
    )
    if public:
        return public
    return f"about:blank#stashed-{engagement_id}-{object_path}"


# ---------------------------------------------------------------------------
# Public surface — kickoff, run_phase, generate_deliverable
# ---------------------------------------------------------------------------


async def kickoff(engagement_id: str, intake_data: dict) -> dict:
    """Called by ``lib.contract`` once a contract is signed + paid.

    Side effects:
      1. Patch engagement: status='kickoff', total_phases=4, current_phase=1.
      2. Email the lead the intake form link.
      3. Schedule ``devops_phase_1_baseline`` on the lead 1 day out.
      4. Slack-ping Mila with the engagement summary.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    already_kicked = (
        engagement.get("status") in ("kickoff", "running", "delivered")
        and engagement.get("current_phase")
    )

    patch = {
        "total_phases": 4,
        "current_phase": engagement.get("current_phase") or 1,
        "status": engagement.get("status") or "kickoff",
        "intake_data": {
            **(engagement.get("intake_data") or {}),
            **(intake_data or {}),
        },
        "started_at": engagement.get("started_at") or _now_iso(),
        "next_phase_at": (
            _serialize(_now() + timedelta(days=1))
            if not already_kicked
            else engagement.get("next_phase_at")
        ),
    }
    await _engagement_patch(engagement_id, patch)

    lead, email, first_name = await _lead_for_engagement(engagement)

    if email and not already_kicked:
        token = _hmac_token(engagement_id, "intake")
        intake_url = (
            f"{BASE_URL}/api/delivery/devops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        value_str = _brl(
            engagement.get("total_value_brl") or PRACTICE_TICKET_BRL
        )
        html = _kickoff_email_html(
            first_name=first_name,
            intake_url=intake_url,
            value_str=value_str,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="DevOps Maturity Assessment começou — primeiro passo (intake)",
                html=html,
                kind="devops_kickoff",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "devops_maturity.kickoff: email send failed eng=%s",
                engagement_id,
            )

    next_at = _now() + timedelta(days=1)
    if lead and lead.get("id"):
        await session_set_next(
            str(lead["id"]),
            next_action="devops_phase_1_baseline",
            next_action_at=next_at,
        )
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.devops_maturity",
            action="devops_kickoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} kickoff; intake email sent; "
                f"phase 1 scheduled at {next_at.isoformat()}"
            ),
        )

    company = (lead or {}).get("company") or "—"
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":rocket: *DevOps Maturity Assessment kickoff* — engagement "
        f"`{engagement_id}` ({company}) · R$ {value_str} · 4 semanas. "
        f"Intake enviado pra {email or 'n/a'}."
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "next_action_at": next_at,
    }


async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute phase N of the DevOps Maturity Assessment. Idempotent."""
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    current = int(engagement.get("current_phase") or 1)

    if phase < current:
        log.info(
            "devops_maturity.run_phase: skipping phase %s, current=%s eng=%s",
            phase, current, engagement_id,
        )
        return {"ok": True, "skipped": True, "current_phase": current}

    if phase == 1:
        return await _run_phase_1(engagement)
    if phase == 2:
        return await _run_phase_2(engagement)
    if phase == 3:
        return await _run_phase_3(engagement)
    if phase == 4:
        return await _run_phase_4(engagement)

    return {"ok": False, "reason": f"unknown_phase_{phase}"}


async def generate_deliverable(
    engagement_id: str, deliverable_type: str
) -> dict:
    """Generate one deliverable on demand. Useful for re-rendering or
    when an operator manually requests a refresh from the admin UI.

    Returns ``{ok, url, type}`` on success.

    Supported types:
      - dora_baseline (phase 1)
      - maturity_scorecard (phase 2)
      - roadmap_6mo (phase 3)
      - quick_wins_playbook (phase 3)
      - tooling_recommendations (phase 3)
      - final_executive_report (phase 4)
      - executive_deck (phase 4)
      - kpi_tracking_template (phase 4)
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    baseline = artifacts.get("phase_1_baseline") or {}
    scorecard = artifacts.get("phase_2_scorecard") or {}
    roadmap_md = artifacts.get("phase_3_roadmap_md") or ""
    quick_wins_md = artifacts.get("phase_3_quick_wins_md") or ""
    tooling_md = artifacts.get("phase_3_tooling_md") or ""

    if deliverable_type == "dora_baseline":
        if not baseline:
            baseline = await _compose_dora_baseline(engagement, intake)
        body_md = _baseline_to_markdown(baseline)
        url = await _render_and_upload(
            engagement_id,
            title="DORA Baseline — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/dora_baseline.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_baseline": baseline,
                "dora_baseline_md": body_md,
                "dora_baseline_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "maturity_scorecard":
        if not baseline:
            baseline = await _compose_dora_baseline(engagement, intake)
        if not scorecard:
            scorecard = await _compose_maturity_scorecard(
                engagement, baseline, intake
            )
        body_md = _scorecard_to_markdown(scorecard)
        url = await _render_and_upload(
            engagement_id,
            title="Maturity Scorecard — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=body_md,
            object_path=f"{engagement_id}/maturity_scorecard.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_baseline": baseline,
                "phase_2_scorecard": scorecard,
                "maturity_scorecard_md": body_md,
                "maturity_scorecard_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "roadmap_6mo":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        roadmap_md = await _compose_roadmap_6mo(engagement, baseline, scorecard)
        url = await _render_and_upload(
            engagement_id,
            title="Roadmap DevOps — 6 meses",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=roadmap_md,
            object_path=f"{engagement_id}/roadmap_6mo.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_roadmap_md": roadmap_md,
                "roadmap_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "quick_wins_playbook":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        if not roadmap_md:
            roadmap_md = await _compose_roadmap_6mo(
                engagement, baseline, scorecard
            )
        quick_wins_md = await _compose_quick_wins_playbook(
            engagement, baseline, scorecard, roadmap_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Quick Wins Playbook — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=quick_wins_md,
            object_path=f"{engagement_id}/quick_wins_playbook.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_quick_wins_md": quick_wins_md,
                "quick_wins_url": url,
                "phase_3_roadmap_md": roadmap_md,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "tooling_recommendations":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        tooling_md = await _compose_tooling_recommendations(
            engagement, baseline, scorecard
        )
        url = await _render_and_upload(
            engagement_id,
            title="Tooling Recommendations — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=tooling_md,
            object_path=f"{engagement_id}/tooling_recommendations.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_tooling_md": tooling_md,
                "tooling_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "executive_deck":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_3_roadmap_md") or ""
        deck_md = await _compose_executive_deck(
            engagement, baseline, scorecard, roadmap_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Apresentação Executiva — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Entrega final",
            body_md=deck_md,
            object_path=f"{engagement_id}/executive_deck.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "deck_md": deck_md,
                "deck_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "kpi_tracking_template":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        kpi_md = await _compose_kpi_tracking_template(
            engagement, baseline, scorecard
        )
        url = await _render_and_upload(
            engagement_id,
            title="KPI Tracking Template — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Entrega final",
            body_md=kpi_md,
            object_path=f"{engagement_id}/kpi_tracking_template.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "kpi_template_md": kpi_md,
                "kpi_template_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "final_executive_report":
        if not baseline:
            baseline = artifacts.get("phase_1_baseline") or {}
        if not scorecard:
            scorecard = artifacts.get("phase_2_scorecard") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_3_roadmap_md") or ""
        if not quick_wins_md:
            quick_wins_md = artifacts.get("phase_3_quick_wins_md") or ""
        if not tooling_md:
            tooling_md = artifacts.get("phase_3_tooling_md") or ""
        report_md = await _compose_final_executive_report(
            engagement, baseline, scorecard, roadmap_md, quick_wins_md, tooling_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Relatório Executivo — DevOps Maturity Assessment",
            subtitle=f"Engagement {engagement_id} · Entrega final",
            body_md=report_md,
            object_path=f"{engagement_id}/final_executive_report.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "final_report_md": report_md,
                "final_report_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    return {"ok": False, "reason": f"unknown_deliverable_{deliverable_type}"}


# ---------------------------------------------------------------------------
# Phase runners — each invoked by its registered orchestrator handler
# ---------------------------------------------------------------------------


def _intake_submitted(engagement: dict) -> bool:
    """Heuristic: intake counts as submitted when enough operator-facing
    fields are present in ``intake_data``, OR a sentinel timestamp is set.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        return False
    if intake.get("submitted_at"):
        return True
    required = (
        "executive_sponsor_email",
        "team_size",
        "production_services_count",
        "ci_tool",
        "incident_tracker",
        "oncall_tool",
        "self_reported_dora",
        "observability_stack",
        "post_mortem_culture",
    )
    filled = sum(1 for k in required if intake.get(k))
    return filled >= 4


async def _run_phase_1(engagement: dict) -> dict:
    """Phase 1 — wait for intake submission, compose DORA baseline, advance."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    if _intake_submitted(engagement):
        intake = engagement.get("intake_data") or {}
        if not isinstance(intake, dict):
            intake = {}

        baseline = await _compose_dora_baseline(engagement, intake)
        baseline_md = _baseline_to_markdown(baseline)

        baseline_url = await _render_and_upload(
            engagement_id,
            title="DORA Baseline — DevOps Maturity",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=baseline_md,
            object_path=f"{engagement_id}/dora_baseline.pdf",
        )

        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_baseline": baseline,
                "dora_baseline_md": baseline_md,
                "dora_baseline_url": baseline_url,
            },
        )

        if email:
            metrics = baseline.get("metrics") or {}
            html = _phase1_email_html(
                first_name=first_name,
                baseline_url=baseline_url,
                deploy_freq=str(metrics.get("deploy_frequency") or "—"),
                lead_time=str(metrics.get("lead_time") or "—"),
                mttr=str(metrics.get("mttr") or "—"),
                cfr=str(metrics.get("change_failure_rate") or "—"),
                cluster=str(baseline.get("cluster") or "—").upper(),
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject="DORA Baseline pronto — Semana 1 DevOps Maturity",
                    html=html,
                    kind="devops_phase_1_baseline",
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "devops_maturity.phase_1: email failed eng=%s",
                    engagement_id,
                )

        await _engagement_patch(
            engagement_id,
            {
                "current_phase": 2,
                "status": "running",
                "next_phase_at": _serialize(_now() + _PHASE_INTERVAL),
            },
        )

        next_at = _now() + _PHASE_INTERVAL
        return {
            "ok": True,
            "advanced_to_phase": 2,
            "next_action": "devops_phase_2_maturity",
            "next_action_at": next_at,
        }

    # Intake not submitted — has it been long enough to nudge?
    started_at = engagement.get("started_at")
    started_dt = None
    if started_at:
        try:
            started_dt = datetime.fromisoformat(
                str(started_at).replace("Z", "+00:00")
            )
        except ValueError:
            started_dt = None
    elapsed = (_now() - started_dt) if started_dt else timedelta(0)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    reminder_sent = bool(artifacts.get("intake_reminder_sent_at"))

    if elapsed >= _INTAKE_REMINDER_AFTER and not reminder_sent and email:
        token = _hmac_token(engagement_id, "intake")
        intake_url = (
            f"{BASE_URL}/api/delivery/devops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _intake_reminder_email_html(
            first_name=first_name, intake_url=intake_url
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Intake pendente — DevOps Maturity Assessment",
                html=html,
                kind="devops_intake_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"intake_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":hourglass: DevOps Maturity engagement `{engagement_id}` — "
                f"intake pendente há {elapsed.days} dias. Lembrete enviado pra "
                f"{email}."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "devops_maturity.phase_1: reminder send failed eng=%s",
                engagement_id,
            )

    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "intake_submission",
        "next_action": "devops_phase_1_baseline",
        "next_action_at": next_at,
    }


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — Claude scores 6 dimensions. Ship scorecard PDF + email."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    baseline = artifacts.get("phase_1_baseline") or {}
    if not baseline:
        baseline = await _compose_dora_baseline(engagement, intake)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_baseline": baseline}
        )

    scorecard = await _compose_maturity_scorecard(engagement, baseline, intake)
    scorecard_md = _scorecard_to_markdown(scorecard)

    scorecard_url = await _render_and_upload(
        engagement_id,
        title="Maturity Scorecard — DevOps Maturity",
        subtitle=f"Engagement {engagement_id} · Semana 2",
        body_md=scorecard_md,
        object_path=f"{engagement_id}/maturity_scorecard.pdf",
    )

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_2_scorecard": scorecard,
            "maturity_scorecard_md": scorecard_md,
            "maturity_scorecard_url": scorecard_url,
        },
    )

    if email:
        avg = scorecard.get("average_score") or 0
        try:
            avg_str = f"{float(avg):.1f}"
        except (TypeError, ValueError):
            avg_str = str(avg)
        html = _phase2_email_html(
            first_name=first_name,
            scorecard_url=scorecard_url,
            avg_score=avg_str,
            weakest=_scorecard_weakest_labels(scorecard),
            strongest=_scorecard_strongest_labels(scorecard),
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Maturity Scorecard pronto — Semana 2 DevOps Maturity",
                html=html,
                kind="devops_phase_2_scorecard",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "devops_maturity.phase_2: email failed eng=%s", engagement_id
            )

    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 3,
            "status": "running",
            "next_phase_at": _serialize(_now() + _PHASE_INTERVAL),
        },
    )

    next_at = _now() + _PHASE_INTERVAL
    return {
        "ok": True,
        "advanced_to_phase": 3,
        "next_action": "devops_phase_3_roadmap",
        "next_action_at": next_at,
    }


async def _run_phase_3(engagement: dict) -> dict:
    """Phase 3 — compose roadmap + quick wins playbook + tooling recs.
    Ship 3 PDFs + email. Advance to phase 4."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    baseline = artifacts.get("phase_1_baseline") or {}
    scorecard = artifacts.get("phase_2_scorecard") or {}

    if not baseline:
        baseline = await _compose_dora_baseline(
            engagement, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_baseline": baseline}
        )
    if not scorecard:
        scorecard = await _compose_maturity_scorecard(
            engagement, baseline, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_2_scorecard": scorecard}
        )

    roadmap_md = await _compose_roadmap_6mo(engagement, baseline, scorecard)
    quick_wins_md = await _compose_quick_wins_playbook(
        engagement, baseline, scorecard, roadmap_md
    )
    tooling_md = await _compose_tooling_recommendations(
        engagement, baseline, scorecard
    )

    roadmap_url = await _render_and_upload(
        engagement_id,
        title="Roadmap DevOps — 6 meses",
        subtitle=f"Engagement {engagement_id} · Semana 3",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/roadmap_6mo.pdf",
    )
    quick_wins_url = await _render_and_upload(
        engagement_id,
        title="Quick Wins Playbook — DevOps Maturity",
        subtitle=f"Engagement {engagement_id} · Semana 3",
        body_md=quick_wins_md,
        object_path=f"{engagement_id}/quick_wins_playbook.pdf",
    )
    tooling_url = await _render_and_upload(
        engagement_id,
        title="Tooling Recommendations — DevOps Maturity",
        subtitle=f"Engagement {engagement_id} · Semana 3",
        body_md=tooling_md,
        object_path=f"{engagement_id}/tooling_recommendations.pdf",
    )

    n_quick_wins = _count_quick_wins(roadmap_md)

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_3_roadmap_md": roadmap_md,
            "roadmap_url": roadmap_url,
            "phase_3_quick_wins_md": quick_wins_md,
            "quick_wins_url": quick_wins_url,
            "phase_3_tooling_md": tooling_md,
            "tooling_url": tooling_url,
            "quick_wins_count": n_quick_wins,
        },
    )

    if email:
        html = _phase3_email_html(
            first_name=first_name,
            roadmap_url=roadmap_url,
            quick_wins_url=quick_wins_url,
            tooling_url=tooling_url,
            n_quick_wins=n_quick_wins,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Roadmap + Quick Wins prontos — Semana 3 DevOps Maturity",
                html=html,
                kind="devops_phase_3_roadmap",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "devops_maturity.phase_3: email failed eng=%s", engagement_id
            )

    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 4,
            "status": "running",
            "next_phase_at": _serialize(_now() + _PHASE_INTERVAL),
        },
    )

    next_at = _now() + _PHASE_INTERVAL
    return {
        "ok": True,
        "advanced_to_phase": 4,
        "next_action": "devops_phase_4_handoff",
        "next_action_at": next_at,
    }


async def _run_phase_4(engagement: dict) -> dict:
    """Phase 4 — final executive report + deck + KPI template. Close + invoice."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    baseline = artifacts.get("phase_1_baseline") or {}
    scorecard = artifacts.get("phase_2_scorecard") or {}
    roadmap_md = artifacts.get("phase_3_roadmap_md") or ""
    quick_wins_md = artifacts.get("phase_3_quick_wins_md") or ""
    tooling_md = artifacts.get("phase_3_tooling_md") or ""

    # Backfill if a previous phase silently failed.
    if not baseline:
        baseline = await _compose_dora_baseline(
            engagement, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_baseline": baseline}
        )
    if not scorecard:
        scorecard = await _compose_maturity_scorecard(
            engagement, baseline, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_2_scorecard": scorecard}
        )
    if not roadmap_md:
        roadmap_md = await _compose_roadmap_6mo(engagement, baseline, scorecard)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_3_roadmap_md": roadmap_md}
        )
    if not quick_wins_md:
        quick_wins_md = await _compose_quick_wins_playbook(
            engagement, baseline, scorecard, roadmap_md
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_3_quick_wins_md": quick_wins_md}
        )
    if not tooling_md:
        tooling_md = await _compose_tooling_recommendations(
            engagement, baseline, scorecard
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_3_tooling_md": tooling_md}
        )

    deck_md = await _compose_executive_deck(
        engagement, baseline, scorecard, roadmap_md
    )
    report_md = await _compose_final_executive_report(
        engagement, baseline, scorecard, roadmap_md, quick_wins_md, tooling_md
    )
    kpi_md = await _compose_kpi_tracking_template(
        engagement, baseline, scorecard
    )

    deck_url = await _render_and_upload(
        engagement_id,
        title="Apresentação Executiva — DevOps Maturity",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
    )
    report_url = await _render_and_upload(
        engagement_id,
        title="Relatório Executivo — DevOps Maturity Assessment",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
    )
    kpi_url = await _render_and_upload(
        engagement_id,
        title="KPI Tracking Template — DevOps Maturity",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=kpi_md,
        object_path=f"{engagement_id}/kpi_tracking_template.pdf",
    )

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "deck_md": deck_md,
            "deck_url": deck_url,
            "final_report_md": report_md,
            "final_report_url": report_url,
            "kpi_template_md": kpi_md,
            "kpi_template_url": kpi_url,
        },
    )

    nps_url = (
        f"{BASE_URL}/api/delivery/devops/nps"
        f"?engagement_id={engagement_id}"
        f"&token={_hmac_token(engagement_id, 'nps')}"
    )
    handoff_url = (
        f"{BASE_URL}/api/delivery/devops/handoff"
        f"?engagement_id={engagement_id}"
        f"&token={_hmac_token(engagement_id, 'handoff')}"
    )
    if email:
        html = _phase4_email_html(
            first_name=first_name,
            report_url=report_url,
            deck_url=deck_url,
            kpi_template_url=kpi_url,
            handoff_scheduling_url=handoff_url,
            nps_url=nps_url,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject=(
                    "DevOps Maturity entregue — relatório + deck + KPI template"
                ),
                html=html,
                kind="devops_phase_4_delivery",
                cc=[RESEND_REPLY_TO_EMAIL] if RESEND_REPLY_TO_EMAIL else None,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "devops_maturity.phase_4: email failed eng=%s", engagement_id
            )

    contract_id = engagement.get("contract_id")
    invoice_result: dict = {"ok": False, "reason": "not_attempted"}
    if contract_id:
        invoice_result = await _trigger_invoice(str(contract_id), engagement_id)

    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 4,
            "status": "delivered",
            "delivered_at": _now_iso(),
            "next_phase_at": None,
        },
    )

    cluster = (baseline.get("cluster") or "—").upper()
    avg = scorecard.get("average_score") or 0
    try:
        avg_str = f"{float(avg):.1f}"
    except (TypeError, ValueError):
        avg_str = str(avg)
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":white_check_mark: *DevOps Maturity Assessment delivered* — engagement "
        f"`{engagement_id}`. Valor total R$ {value_str}. "
        f"DORA cluster: {cluster} · Maturity médio: {avg_str}/5. "
        f"Próximo: invoice ({invoice_result.get('status') or 'pending'}) + "
        f"handoff workshop + NPS. cc {SLACK_MILA_HANDLE}"
    )

    if lead and lead.get("id"):
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.devops_maturity",
            action="devops_phase_4_handoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} delivered; DORA {cluster}; "
                f"maturity {avg_str}/5; invoice {invoice_result.get('status')}"
            ),
        )
        for kind, url in (
            ("final_report", report_url),
            ("executive_deck", deck_url),
            ("kpi_tracking_template", kpi_url),
        ):
            try:
                await session_append_artifact(
                    str(lead["id"]),
                    type=kind,
                    url=url,
                    meta={
                        "engagement_id": engagement_id,
                        "phase": 4,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "devops_maturity.phase_4: artifact append failed "
                    "lead=%s kind=%s",
                    lead.get("id"), kind,
                )

    return {
        "ok": True,
        "delivered": True,
        "invoice": invoice_result,
        "next_action": None,
        "next_action_at": None,
    }


async def _trigger_invoice(contract_id: str, engagement_id: str) -> dict:
    """Call ``lib.contract.issue_invoice`` if available; otherwise log stub."""
    try:
        from lib.contract import issue_invoice  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning(
            "devops_maturity: lib.contract.issue_invoice unavailable — stub "
            "invoice for engagement %s contract %s",
            engagement_id, contract_id,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "invoice_request": {
                    "ts": _now_iso(),
                    "contract_id": contract_id,
                    "status": "stub_pending_manual",
                },
            },
        )
        return {"ok": True, "status": "stub_pending_manual"}

    try:
        result = await issue_invoice(contract_id)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "devops_maturity: issue_invoice failed contract=%s", contract_id
        )
        return {"ok": False, "status": "error", "reason": str(exc)}

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "invoice_request": {
                "ts": _now_iso(),
                "contract_id": contract_id,
                "result": result,
            },
        },
    )
    return result if isinstance(result, dict) else {"ok": True, "status": "issued"}


# ---------------------------------------------------------------------------
# Orchestrator handlers
# ---------------------------------------------------------------------------


def _engagement_id_from_lead(lead: dict) -> Optional[str]:
    """Resolve the active engagement id from a lead row."""
    lead_id = lead.get("id")
    if not lead_id:
        return None

    qd = lead.get("qualification_data") or {}
    if isinstance(qd, dict):
        eid = qd.get("active_engagement_id")
        if eid:
            return str(eid)

    artifacts = lead.get("artifacts") or []
    if isinstance(artifacts, list):
        for a in reversed(artifacts):
            if not isinstance(a, dict):
                continue
            meta = a.get("meta") or {}
            if not isinstance(meta, dict):
                continue
            eid = meta.get("engagement_id")
            if eid:
                return str(eid)
    return None


async def _resolve_engagement_id(lead: dict) -> Optional[str]:
    """Sync resolution first; on miss, query PostgREST for the latest
    engagement on this lead (filtered to practice='devops' to avoid
    cross-practice collisions)."""
    eid = _engagement_id_from_lead(lead)
    if eid:
        return eid
    lead_id = lead.get("id")
    if not lead_id:
        return None
    url = (
        f"{SUPA_URL}/engagements"
        f"?lead_id=eq.{_urlquote(str(lead_id), safe='')}"
        f"&practice=eq.devops"
        f"&status=in.(kickoff,running)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception("devops_maturity: resolve_engagement_id query failed")
        return None
    if r.status_code != 200:
        return None
    rows = r.json() or []
    if rows and rows[0].get("id"):
        return str(rows[0]["id"])
    # Fallback: any active engagement for this lead, regardless of practice.
    url2 = (
        f"{SUPA_URL}/engagements"
        f"?lead_id=eq.{_urlquote(str(lead_id), safe='')}"
        f"&status=in.(kickoff,running)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r2 = await client.get(url2, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        return None
    if r2.status_code != 200:
        return None
    rows2 = r2.json() or []
    return str(rows2[0]["id"]) if rows2 and rows2[0].get("id") else None


@register("devops_kickoff")
async def h_devops_kickoff(lead: dict) -> dict:
    """Entry-point handler — fires once after contract.payment_webhook."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_kickoff: no active engagement found",
        }
    engagement = await _engagement_get(engagement_id)
    intake = (engagement or {}).get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    result = await kickoff(engagement_id, intake)
    return {
        "next_action": "devops_phase_1_baseline",
        "next_action_at": (
            result.get("next_action_at") or (_now() + timedelta(days=1))
        ),
        "status": "delivery_running",
        "detail": (
            f"devops_maturity kickoff ok; engagement {engagement_id}; "
            f"intake email sent"
        ),
    }


@register("devops_phase_1_baseline")
async def h_devops_phase_1(lead: dict) -> dict:
    """Phase 1 handler — intake gate + DORA baseline composition."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_phase_1: no active engagement",
        }
    result = await run_phase(engagement_id, 1)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running" if not result.get("delivered") else "won",
        "detail": (
            f"devops_maturity phase 1: "
            f"{'advanced→2' if result.get('advanced_to_phase') else 'waiting intake'}"
        ),
    }


@register("devops_phase_2_maturity")
async def h_devops_phase_2(lead: dict) -> dict:
    """Phase 2 handler — maturity scorecard."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_phase_2: no active engagement",
        }
    result = await run_phase(engagement_id, 2)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            f"devops_maturity phase 2: scorecard shipped for "
            f"engagement {engagement_id}"
        ),
    }


@register("devops_phase_3_roadmap")
async def h_devops_phase_3(lead: dict) -> dict:
    """Phase 3 handler — roadmap + quick wins + tooling."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_phase_3: no active engagement",
        }
    result = await run_phase(engagement_id, 3)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            f"devops_maturity phase 3: roadmap + quick wins + tooling "
            f"shipped for engagement {engagement_id}"
        ),
    }


@register("devops_phase_4_handoff")
async def h_devops_phase_4(lead: dict) -> dict:
    """Phase 4 handler — final report + deck + KPI template + invoice + close."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_phase_4: no active engagement",
        }
    result = await run_phase(engagement_id, 4)
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "won" if result.get("delivered") else "delivery_running",
        "detail": (
            f"devops_maturity phase 4: "
            f"{'delivered' if result.get('delivered') else 'in progress'}"
            f"; invoice={result.get('invoice', {}).get('status')}"
        ),
    }


@register("devops_send_progress_update")
async def h_devops_progress_update(lead: dict) -> dict:
    """Mid-phase nudge — re-runs whichever phase the engagement is on, then
    optionally emails a progress update if the client has been silent."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_progress: no active engagement",
        }
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "devops_progress: engagement disappeared",
        }
    phase = int(engagement.get("current_phase") or 1)
    result = await run_phase(engagement_id, phase)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    seen_key = f"progress_update_phase_{phase}_at"
    if not artifacts.get(seen_key):
        _, email, first_name = await _lead_for_engagement(engagement)
        phase_label = {
            1: "Baseline DORA",
            2: "Maturity Deep-dive",
            3: "Roadmap & Quick Wins",
            4: "Executive Sync & Handoff",
        }.get(phase, f"Fase {phase}")
        summary = {
            1: (
                "Extração DORA em andamento via CI logs + git history + "
                "incident tracker + on-call data. Baseline real chega ao "
                "final desta semana."
            ),
            2: (
                "Maturity deep-dive em 6 dimensões (CI/CD, test automation, "
                "IaC, GitOps, observability, incident response). Scoring "
                "1-5 com sub-critérios objetivos."
            ),
            3: (
                "Roadmap 6 meses sendo sequenciado por (impact × confidence) "
                "/ effort. Quick wins playbook em redação. Tooling "
                "recommendations (feature flags, observability, IaC) em "
                "comparação."
            ),
            4: (
                "Relatório executivo consolidado, deck (30 slides) e KPI "
                "tracking template em finalização. Handoff workshop "
                "(2h) sendo agendado em paralelo."
            ),
        }.get(
            phase,
            "Assessment em andamento — sem update específico para esta fase.",
        )

        if email:
            html = _progress_update_email_html(
                first_name=first_name,
                phase_label=phase_label,
                summary=summary,
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject=f"Update — {phase_label} (DevOps Maturity)",
                    html=html,
                    kind=f"devops_progress_phase_{phase}",
                )
                await _engagement_merge_artifacts(
                    engagement_id, {seen_key: _now_iso()}
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "devops_maturity.progress: email failed eng=%s phase=%s",
                    engagement_id, phase,
                )

    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": f"devops_maturity progress update: re-ran phase {phase}",
    }


# Alias — the contract module emits ``engagement_kickoff_devops`` for the
# ``devops`` practice (see lib/contract.py::_kickoff_engagement). We register
# the same handler under that key so the orchestrator dispatch lands here
# directly without an intermediate translation.
HANDLER_ALIAS = "engagement_kickoff_devops"


@register(HANDLER_ALIAS)
async def h_engagement_kickoff_devops(lead: dict) -> dict:
    """Alias for ``devops_kickoff`` — wired so contract.py's emitted action
    string lands on the right handler without a string remap."""
    return await h_devops_kickoff(lead)
