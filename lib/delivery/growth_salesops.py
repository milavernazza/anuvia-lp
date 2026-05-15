"""Sales Ops Diagnostic — autonomous delivery agent.

Owns the post-signature delivery flow for the ``growth_salesops`` practice
(R$ 15-25k, 2 weeks). Hands off from ``lib.contract`` once the Sales Ops
Diagnostic contract is signed and paid, then runs two weekly phases —
Funnel Mapping & Stack Audit and Automation Playbook & Roadmap — producing
client deliverables and emails along the way.

This module is distinct from ``lib/track_b.py`` (autonomous SMB growth
practice keyed simply ``growth``). The Sales Ops Diagnostic is a paid
engagement type. Practice key here is ``growth_salesops``.

Architecture::

    contract.webhook (paid, practice=growth_salesops)
        |
        v
    growth_kickoff                              [+0]
        |   (intake form sent, awaiting funnel/stack data)
        v
    growth_phase_1_funnel                       [+1 day]
        |   (intake submitted → funnel map + leakage + stack audit composed)
        v
    growth_phase_2_automation                   [+1 week]
        |   (automation playbook + 90-day roadmap + tooling + deck + report)
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
    anti-hype, vazamento/clareza/diagnóstico lexicon). See
    ``_BRAND_SYSTEM_PROMPT``.
  * HMAC-tokened client links use ``CONTRACT_HMAC_SECRET`` so a single
    secret drives sign + intake + approval flows.
  * Every automation candidate names the build-vs-buy posture explicitly
    (HubSpot workflows / n8n custom / Make.com / Zapier / custom code).
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

log = logging.getLogger("anuvia-lp.delivery.growth_salesops")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

#: Default ticket size for this practice. Midpoint of R$ 15-25k band.
PRACTICE_TICKET_BRL: int = 20000

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

# 2-week cadence — shorter than AI Readiness (3w) and FinOps (4w).
_PHASE_INTERVAL = timedelta(days=7)
_INTAKE_REMINDER_AFTER = timedelta(days=3)
_HTTP_TIMEOUT = 30.0

# Brand voice — pinned to every Claude system prompt in this module. Same
# tone as ai_readiness but specialised for Sales Ops (funil, vazamento,
# conversão, ciclo de venda, response time, automation map). No compliance
# instruction since Sales Ops is not compliance-heavy.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA + sales ops, ex-AWS Solutions "
    "Architect, ex-Google, ex-MongoDB). Voz: seca, direta, anti-hype, "
    "primeiro os números, depois a narrativa. Frases curtas declarativas "
    "misturadas com cadeias causa-efeito mais longas. Use o léxico: "
    "vazamento, clareza, diagnóstico, processo, padrão, gate de saída, "
    "evidência, funil, conversão stage-a-stage, response time, ciclo de "
    "venda, dwell time, lead leakage. Evite: sinergia, transformação, "
    "leverage, magia, mágico, IA generativa que muda o jogo, revolucionar, "
    "growth hack.\n\n"
    "REGRAS DE PROFUNDIDADE TÉCNICA (não negociáveis):\n"
    "1. Cite CRMs por nome e edition (HubSpot Sales Hub Professional, "
    "Salesforce Sales Cloud Enterprise, Pipedrive Power, RD Station CRM "
    "Pro, Close, Outreach). Nunca dizer 'o CRM' genericamente.\n"
    "2. Cite stages do funil com a definição operacional exata (Lead → MQL "
    "= score ≥ X + fit ICP; MQL → SQL = SDR qualified com BANT/MEDDIC; "
    "SQL → Opportunity = discovery call done + budget confirmed; "
    "Opportunity → Closed Won = contrato assinado). Não confundir lifecycle "
    "stage com deal stage.\n"
    "3. Cite métricas com a math explícita: 'response time 5min → 24h "
    "derruba conversão MQL→SQL de ~25% para ~5% (Lead Response Management "
    "Study, Harvard Business Review). Cliente em 8h = perda estimada de "
    "12pp = 30 SQLs/mês não convertidos'. Mostre a conta.\n"
    "4. Cite fontes de dados CRM exatas (HubSpot Reports → Sales Analytics "
    "→ Deal Funnel; HubSpot Workflows → Performance tab; Salesforce Reports "
    "tipo Opportunity History; Pipeline Analytics). Quando faltar dado, "
    "marcar como 'a coletar via Reports' e listar o caminho.\n"
    "5. Use números DO INTAKE do cliente. Se intake diz 800 leads/mês, "
    "120 MQLs, 30 SQLs, ticket médio R$ 25k, ciclo 60d, todos os ganhos "
    "derivam disso: 'response time fix levanta MQL→SQL de 25% para 33% = "
    "+10 SQLs/mês × 20% close rate × R$ 25k = +R$ 50k/mês recorrente'.\n"
    "6. Math explícita de ICP scoring: critérios firmográficos (porte 50-500 "
    "FTE = 10pts, setor SaaS = 8pts, ARR R$ 5-50M = 7pts) + comportamentais "
    "(demo request = 15pts, pricing page 3+ visits = 10pts, abrir email "
    "<24h = 3pts). Cutoff de MQL = 50pts. Mostre a regra.\n"
    "7. Para CADA automação, posture build vs buy explícita (HubSpot "
    "workflows nativos, n8n self-hosted, Make.com cloud, Zapier, custom "
    "Node.js/Python) com justificativa de 1 linha: custo mensal, "
    "manutenção, lock-in, latência.\n"
    "8. Para CADA finding, inclua: validation criteria (qual métrica + "
    "baseline + target + janela), rollback plan (workflow off + lifecycle "
    "stage revert + lista de leads afetados), janela proposta.\n"
    "9. ADRs em formato ADR-XX: ADR-01 (CRM principal: HubSpot vs "
    "Salesforce), ADR-02 (lifecycle stage taxonomy), ADR-03 (lead scoring "
    "model), ADR-04 (orquestrador de automação), etc.\n"
    "10. Quando estimar, use 'estimativa' uma vez só. NÃO repita 'padrão "
    "setorial' como muleta — isso é tique de junior. Nunca prometa o que "
    "não pode ser medido. Português do Brasil."
)

#: Sentinel prefix for narrative that Claude could not generate.
_CLAUDE_FALLBACK_TAG = "[CLAUDE_UNAVAILABLE_DRAFT]"

#: Build-vs-buy postures Claude should pick from per automation.
_BUILD_VS_BUY_OPTIONS: List[str] = [
    "HubSpot workflows",
    "n8n custom",
    "Make.com",
    "Zapier",
    "custom code",
    "CRM native",
]

#: The 12-item Sales Ops checklist from SPRINT_INPUTS_MILA.md §5.4. Echoed
#: verbatim in the final executive report and in the Claude prompt for the
#: report so every engagement carries the same audit trail.
_SALESOPS_CHECKLIST_12: List[str] = [
    "Funnel stages claramente definidos (não vagas como 'MQL')",
    "Conversion rate stage-by-stage (não só top-to-bottom)",
    "Avg dwell time por stage",
    "Response time SLA medido por canal (form, WhatsApp, email, referral)",
    "Top 3 leakage points com nome + volume + impacto $",
    "Lead source attribution",
    "Funnel velocity (deals por semana per stage)",
    "Stack inventory (todas as ferramentas + cost mensal + uso real)",
    "Tool integration map (qual fala com qual? gaps?)",
    "Sales cycle distribution (mediano vs P75 vs P95)",
    "SDR/AE capacity utilization",
    "Quota attainment % (se aplicável)",
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
    """Format a numeric as Brazilian currency: 20.000,00."""
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
            "growth_salesops: HMAC secret unset; client links will be unverifiable"
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


# ---------------------------------------------------------------------------
# Engagement row CRUD (PostgREST via httpx)
# ---------------------------------------------------------------------------


async def _engagement_get(engagement_id: str) -> Optional[dict]:
    """Fetch the full engagements row by id, or None if not found."""
    url = f"{SUPA_URL}/engagements?id=eq.{_urlquote(str(engagement_id), safe='')}&limit=1"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception(
            "growth_salesops: engagement_get network failed id=%s", engagement_id
        )
        return None
    if r.status_code != 200:
        log.warning(
            "growth_salesops: engagement_get non-200 id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def _engagement_patch(engagement_id: str, fields: dict) -> bool:
    """PATCH an engagements row. Returns True on success. Never raises."""
    payload = _serialize(fields)
    url = f"{SUPA_URL}/engagements?id=eq.{_urlquote(str(engagement_id), safe='')}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.patch(url, headers=SUPA_HEADERS, json=payload)
    except Exception:  # noqa: BLE001
        log.exception(
            "growth_salesops: engagement_patch network failed id=%s",
            engagement_id,
        )
        return False
    if r.status_code not in (200, 204):
        log.warning(
            "growth_salesops: engagement_patch non-2xx id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return False
    return True


async def _engagement_merge_artifacts(
    engagement_id: str, additions: dict
) -> bool:
    """Merge ``additions`` into ``engagement.artifacts`` (a jsonb object).

    Read-modify-write. Top-level keys in ``additions`` overwrite existing
    keys with the same name — phase-keyed payloads are expected to be
    replaced on a re-run.
    """
    row = await _engagement_get(engagement_id)
    if not row:
        log.warning(
            "growth_salesops: merge_artifacts: engagement %s not found",
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
    """Upload ``content`` to the ``anuvia-deliverables`` bucket. Returns the
    public URL or ``None`` if storage is unavailable.

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
        log.warning("growth_salesops: storage upload failed path=%s: %s", path, exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "growth_salesops: storage upload non-2xx path=%s status=%s body=%s",
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
    gotenberg = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
    endpoint = f"{gotenberg}/forms/chromium/convert/html"
    try:
        files = {"files": ("index.html", html.encode("utf-8"), "text/html")}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(endpoint, files=files)
    except Exception as exc:  # noqa: BLE001
        log.warning("growth_salesops: gotenberg call failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning(
            "growth_salesops: gotenberg non-200 status=%s body=%s",
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
    max_tokens: int = 4000,
    system: str = _BRAND_SYSTEM_PROMPT,
    max_retries: int = 3,
) -> str:
    """Call the Anthropic Messages API with retry + exponential backoff.

    Returns the model's text. Only falls back to ``_CLAUDE_FALLBACK_TAG`` after
    ``max_retries`` consecutive failures. Each retry waits 2^attempt seconds.
    Timeout per attempt is 90 seconds (Claude can take 30-60s on big prompts).
    """
    if not ANTHROPIC_API_KEY:
        return f"{_CLAUDE_FALLBACK_TAG} (no ANTHROPIC_API_KEY)\n\n{prompt[:800]}"

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    last_err: str = ""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
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
            last_err = f"network attempt {attempt + 1}: {type(exc).__name__} {exc!r}"
            log.warning("growth_salesops: anthropic %s", last_err)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return f"{_CLAUDE_FALLBACK_TAG} ({last_err})"

        if r.status_code == 200:
            body = r.json() if r.text else {}
            blocks = body.get("content") or []
            parts: List[str] = []
            for blk in blocks:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text") or "")
            out = "\n".join(parts).strip()
            if out:
                return out
            last_err = "empty response"
        elif r.status_code in (429, 500, 502, 503, 504, 529):
            # Retryable: rate limit or transient server error
            last_err = f"status {r.status_code} (attempt {attempt + 1})"
            log.warning(
                "growth_salesops: anthropic retryable %s body=%s",
                last_err, r.text[:300],
            )
        else:
            # Non-retryable error (400/401/403)
            log.warning(
                "growth_salesops: anthropic non-retryable status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return f"{_CLAUDE_FALLBACK_TAG} (status {r.status_code})"

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    return f"{_CLAUDE_FALLBACK_TAG} ({last_err})"


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
            "growth_salesops: RESEND_API_KEY unset; stashing draft kind=%s eng=%s",
            kind, engagement_id,
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        await _send_slack_alert(
            f":warning: Sales Ops delivery: RESEND_API_KEY missing — "
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
            {"name": "category", "value": "delivery_growth_salesops"},
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
        log.exception("growth_salesops: resend network failed kind=%s", kind)
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend network: {exc}")

    if r.status_code >= 400:
        log.error(
            "growth_salesops: resend non-2xx kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info(
        "growth_salesops: resend ok kind=%s eng=%s msg_id=%s",
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
            "growth_salesops: stash_email_draft failed eng=%s kind=%s",
            engagement_id, kind,
        )


# ---------------------------------------------------------------------------
# Email HTML templates — compact, inline-styled, brand-consistent
# ---------------------------------------------------------------------------


def _wrap_email(title: str, body_html: str) -> str:
    """Wrap a body fragment in the standard Anuvia email shell."""
    return f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · Sales Ops Diagnostic</p>
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
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Contrato fechado. Sales Ops Diagnostic começa agora. Investimento total: <strong>R$ {value_str}</strong>. Cronograma: 2 semanas, duas fases, dois pacotes de entrega.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Semana 1 — Funnel Mapping &amp; Stack Audit.</strong> Antes de cravar o funil preciso da informação abaixo. Sem isso, a semana 2 (automation playbook) não roda.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>Sponsor executivo (nome + email)</li>
  <li>CRM em uso (HubSpot / Salesforce / Pipedrive / RD Station / Notion / planilha) — com acesso read-only</li>
  <li>Composição do time comercial (founder vendendo? n SDRs, n AEs, n closers)</li>
  <li>Ciclo de venda atual (mediano, P75, P95 — estimativa serve)</li>
  <li>Ticket médio em R$</li>
  <li>Lead sources (form, WhatsApp inbound, outbound, referral, paid)</li>
  <li>Volume últimos 3 meses: leads/mês, qualificados/mês, fechados/mês</li>
  <li>Response time SLA — meta vs realidade estimada por canal</li>
  <li>Automações já existentes (sequences, auto-replies, workflows)</li>
  <li>Top 3 dores auto-reportadas pelo time</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Acesso read-only ao CRM + export de últimos 90 dias acelera tudo. Se não rolar export, a gente roda em cima da telemetria do funil declarado.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Padrão dos últimos diagnósticos: 5-8 leakage points concretos identificados (com nome, volume e impacto em R$), 5-8 automações priorizadas, roadmap de 13 semanas com gates por fase.</p>
"""
    return _wrap_email("Sales Ops Diagnostic começou", body)


def _phase1_email_html(
    *,
    first_name: str,
    funnel_url: str,
    leakage_url: str,
    stack_url: str,
    n_stages: int,
    n_leakage: int,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 1 fechada. Funil mapeado end-to-end, response time SLA medido por canal, stack assessment consolidado.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Saída: <strong>{n_stages} stages</strong> mapeados com conversion rate stage-a-stage + dwell time, <strong>{n_leakage} leakage points</strong> nomeados (com volume e impacto $).</p>
<p style="margin:18px 0 8px;"><a href="{funnel_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;">Funnel map (PDF) &rarr;</a></p>
<p style="margin:8px 0;"><a href="{leakage_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;">Leakage points (PDF) &rarr;</a></p>
<p style="margin:8px 0 24px;"><a href="{stack_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;">Stack assessment (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2: automation map por leakage point (qual automação aplica, impact, effort, ROI), top 5-8 automações priorizadas, recomendação build vs buy por automação (HubSpot workflows, n8n custom, Make.com, Zapier ou custom), roadmap 90 dias (13 semanas) com gates.</p>
"""
    return _wrap_email("Funil + leakage + stack — Semana 1", body)


def _phase2_email_html(
    *,
    first_name: str,
    report_url: str,
    deck_url: str,
    playbook_url: str,
    roadmap_url: str,
    tooling_url: str,
    nps_url: str,
    n_automations: int,
    savings_band_low: int,
    savings_band_high: int,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Diagnóstico concluído. Duas semanas, cinco entregáveis principais:</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo</a> — funil, leakage, stack, playbook, roadmap, tooling, checklist 12 itens.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva</a> — 20 slides pra rodar com sponsor e time comercial.</li>
  <li><a href="{playbook_url}" style="color:#0f172a;">Automation playbook</a> — {n_automations} automações priorizadas com impact/effort/ROI.</li>
  <li><a href="{roadmap_url}" style="color:#0f172a;">Roadmap 90 dias</a> — 13 semanas em 3 fases (quick wins, structural, optimization) com gates.</li>
  <li><a href="{tooling_url}" style="color:#0f172a;">Tooling recommendations</a> — build vs buy por automação.</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Banda agregada de impacto ano 1 (cenário conservador a otimista): <strong>R$ {_brl(savings_band_low)} – R$ {_brl(savings_band_high)}</strong>. Assumptions explícitas no relatório — quando faltou dado concreto, foi marcado como estimativa setorial.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Sessão de handoff (90min) fica agendada por email separado. A invoice da segunda parcela já entrou na fila.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Um pedido: 2 minutos pra deixar um NPS. Direto, sem firula:</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar NPS &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se conhecer outro CEO/Head Comercial com funil esfriando e stack que ninguém usa direito — você sabe quem precisa ouvir isso.</p>
"""
    return _wrap_email("Sales Ops Diagnostic entregue", body)


def _intake_reminder_email_html(*, first_name: str, intake_url: str) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete curto: o formulário de intake ainda não foi preenchido. Sem ele, o funnel mapping não roda e o cronograma de 2 semanas desloca.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se tiver bloqueio (acesso ao CRM travado, time comercial sem sponsor, dado dos últimos 90 dias confuso) — me avisa que a gente desbloqueia.</p>
"""
    return _wrap_email("Intake pendente — Sales Ops Diagnostic", body)


def _progress_update_email_html(
    *, first_name: str, phase_label: str, summary: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Update curto sobre o diagnóstico — fase atual: <strong>{phase_label}</strong>.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">{summary}</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Próximo entregável escrito chega ao final desta semana. Qualquer coisa antes, é só responder este email.</p>
"""
    return _wrap_email("Diagnóstico em andamento", body)


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
  table {{ border-collapse:collapse; margin:8px 0 16px; font-size:11px; }}
  th, td {{ border:1px solid #e7e5e4; padding:6px 10px; text-align:left; }}
  th {{ background:#fafaf9; }}
  .small {{ color:#64748b; font-size:11px; }}
  .meta {{ color:#475569; font-size:11px; margin:0 0 18px; }}
  .tag {{ display:inline-block; background:#fafaf9; border:1px solid #e7e5e4; padding:2px 8px; border-radius:9999px; font-size:10px; color:#475569; }}
</style></head>
<body>
<header style="margin-bottom:24px;">
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 6px;">Anuvia · Sales Ops Diagnostic</p>
  <h1>{title}</h1>
  <p class="meta">{subtitle}</p>
</header>
{body_md_html}
<footer style="margin-top:32px;padding-top:18px;border-top:1px solid #e7e5e4;color:#64748b;font-size:11px;">
  Anuvia Cloud &amp; AI Consulting · Mila Vernazza · Documento gerado em {_now().strftime("%d/%m/%Y")}
</footer>
</body></html>"""


def _md_to_html(md: str) -> str:
    """Tiny Markdown-ish converter."""
    import re

    lines: List[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
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


async def _compose_funnel_map(engagement: dict, intake_data: dict) -> dict:
    """Phase 1 — given intake (CRM summary + channel SLAs + cycle distribution),
    ask Claude to compose:
      (a) funnel stages with conversion rate per stage
      (b) avg dwell time per stage
      (c) channel response-time SLA measured vs target
      (d) funnel velocity per stage

    Returns::

        {
            "summary": "<paragraph>",
            "stages": [
                {
                    "name": "...",
                    "order": 1,
                    "conversion_rate_pct": <0-100>,
                    "avg_dwell_days": <number>,
                    "volume_per_month": <int>,
                    "drop_off_reasons": ["..."],
                },
                ...
            ],
            "channel_slas": [
                {
                    "channel": "form|whatsapp|email|referral|paid|outbound",
                    "target_response": "<string>",
                    "actual_response": "<string>",
                    "gap_severity": "low|med|high",
                },
                ...
            ],
            "funnel_velocity_notes": "<paragraph>",
            "sales_cycle": {
                "median_days": <number>,
                "p75_days": <number>,
                "p95_days": <number>,
                "notes": "<string>"
            },
        }
    """
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio — usar padrões setoriais)"

    prompt = f"""Você está compondo o funnel map do Sales Ops Diagnostic Anuvia.

Perfil do cliente (intake submetido):
{profile_block}

Mapeie o funil end-to-end. Estágios típicos B2B: Lead → MQL → SQL → Discovery → Demo → Proposta → Fechamento. Adapte ao perfil do cliente (founder-led pode pular MQL, e-commerce inverte ordem, etc).

Para CADA stage:
1. Nome curto (não vago — "Discovery" sim, "MQL" só se o cliente já usa).
2. order (1-based).
3. conversion_rate_pct: estágio anterior → este estágio (0-100). Para o primeiro estágio, % do total de leads que entram.
4. avg_dwell_days: tempo médio que um deal fica nesse stage antes de avançar.
5. volume_per_month: estimativa de quantos passam por esse stage por mês (com base nos números do intake).
6. drop_off_reasons: 1-3 motivos concretos de drop-off nesse stage.

Para CADA canal de entrada (form, WhatsApp, email, referral, paid, outbound — apenas os relevantes ao cliente):
- target_response (SLA declarado, ex: "10 min")
- actual_response (estimativa baseada no intake, ex: "2h em horário comercial")
- gap_severity (low | med | high)

Para sales_cycle: median_days, p75_days, p95_days a partir do intake. Se não vier no intake, use padrão B2B brasileiro (ticket × complexidade) e marque em notes como "estimativa baseada em padrões setoriais".

Quando o intake não trouxer dado suficiente, marque embutido nos valores como "estimativa baseada em padrões setoriais".

Devolva APENAS JSON válido, sem markdown, sem comentários:

{{
  "summary": "<parágrafo 3-5 linhas: forma do funil, principal gargalo identificado, response time gap geral>",
  "stages": [
    {{
      "name": "<stage>",
      "order": <int>,
      "conversion_rate_pct": <0-100>,
      "avg_dwell_days": <number>,
      "volume_per_month": <int>,
      "drop_off_reasons": ["<motivo>", ...]
    }}
  ],
  "channel_slas": [
    {{
      "channel": "<canal>",
      "target_response": "<string>",
      "actual_response": "<string>",
      "gap_severity": "<low|med|high>"
    }}
  ],
  "funnel_velocity_notes": "<parágrafo curto sobre deals/semana por stage e velocity bottleneck>",
  "sales_cycle": {{
    "median_days": <number>,
    "p75_days": <number>,
    "p95_days": <number>,
    "notes": "<string>"
  }}
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} funnel map não gerado — revisar manualmente."
            ),
            "stages": [
                {
                    "name": stage,
                    "order": i + 1,
                    "conversion_rate_pct": 50,
                    "avg_dwell_days": 7,
                    "volume_per_month": 0,
                    "drop_off_reasons": [f"{_CLAUDE_FALLBACK_TAG} estimar"],
                }
                for i, stage in enumerate(
                    ["Lead", "MQL", "SQL", "Discovery", "Proposta", "Fechamento"]
                )
            ],
            "channel_slas": [
                {
                    "channel": ch,
                    "target_response": "—",
                    "actual_response": f"{_CLAUDE_FALLBACK_TAG} estimar",
                    "gap_severity": "med",
                }
                for ch in ["form", "whatsapp", "email", "referral"]
            ],
            "funnel_velocity_notes": (
                f"{_CLAUDE_FALLBACK_TAG} velocity não mapeado."
            ),
            "sales_cycle": {
                "median_days": 0,
                "p75_days": 0,
                "p95_days": 0,
                "notes": f"{_CLAUDE_FALLBACK_TAG} estimar",
            },
        },
        required_keys=("summary", "stages"),
    )


async def _compose_leakage_points(
    engagement: dict, funnel_map: dict, intake_data: dict
) -> dict:
    """Phase 1 — identify 5-8 named leakage points from the funnel map.

    Each leakage point has: name (concrete, not "leads frios"), stage where
    it bleeds, channel(s), volume per week, $ impact estimate, root cause.

    Returns::

        {
            "summary": "<paragraph>",
            "leakage_points": [
                {
                    "name": "...",
                    "stage": "...",
                    "channel": "...",
                    "volume_per_week": <int>,
                    "monthly_impact_brl": <int>,
                    "root_cause": "<2-3 phrases>",
                    "severity": "low|med|high",
                },
                ...
            ],
        }
    """
    stages = funnel_map.get("stages") or []
    channels = funnel_map.get("channel_slas") or []
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    stages_block_lines: List[str] = []
    for s in stages:
        if not isinstance(s, dict):
            continue
        stages_block_lines.append(
            f"- {s.get('name')} (order {s.get('order')}): "
            f"conv {s.get('conversion_rate_pct')}% | "
            f"dwell {s.get('avg_dwell_days')}d | "
            f"vol/mês {s.get('volume_per_month')} | "
            f"drop-off: {', '.join(s.get('drop_off_reasons') or [])}"
        )
    stages_block = "\n".join(stages_block_lines) or "(funil vazio)"

    channels_block_lines: List[str] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        channels_block_lines.append(
            f"- {ch.get('channel')}: target {ch.get('target_response')} | "
            f"actual {ch.get('actual_response')} | gap {ch.get('gap_severity')}"
        )
    channels_block = "\n".join(channels_block_lines) or "(sem canais)"

    prompt = f"""Você está identificando leakage points concretos no funil do Sales Ops Diagnostic Anuvia.

Perfil:
{profile_block}

Funil mapeado:
{stages_block}

Channel SLAs:
{channels_block}

Identifique 5-8 leakage points NOMEADOS. NUNCA aceitar "leads frios" ou "drop-off MQL" como nome — sempre dizer qual stage, qual canal, quantos por semana, quanto custa por mês.

Exemplos de nomes VÁLIDOS:
- "Leads via WhatsApp respondidos só após 4h ficam frios — perde 60% antes de discovery"
- "Demo agendada sem qualificação de budget — 35% no-show"
- "Outbound sem cadência estruturada — SDR perde 20% das replies"

Para CADA leakage point:
1. name: concreto (qual stage, qual canal, comportamento) — máx 18 palavras
2. stage: nome do stage exato do funil acima
3. channel: nome do canal afetado (ou "todos" se transversal)
4. volume_per_week: quantos leads/deals vazam por semana
5. monthly_impact_brl: impacto em R$/mês (volume × ticket médio × taxa de recuperação plausível com a automação certa)
6. root_cause: 2-3 frases — por que vaza (processo, ferramenta, falta de SLA, time sobrecarregado, etc)
7. severity: low | med | high (high = >20% do volume do stage)

Quando faltar dado para volume ou impacto, estime com base no intake e marque em root_cause como "estimativa baseada em padrões setoriais".

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: total mensal vazando, principais 3 stages afetados, principal canal problemático>",
  "leakage_points": [
    {{
      "name": "<concreto, max 18 palavras>",
      "stage": "<stage>",
      "channel": "<canal>",
      "volume_per_week": <int>,
      "monthly_impact_brl": <int>,
      "root_cause": "<2-3 frases>",
      "severity": "<low|med|high>"
    }}
  ]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} leakage points não gerados — revisar manualmente."
            ),
            "leakage_points": [
                {
                    "name": f"Leakage point {i+1}",
                    "stage": "—",
                    "channel": "—",
                    "volume_per_week": 0,
                    "monthly_impact_brl": 0,
                    "root_cause": f"{_CLAUDE_FALLBACK_TAG} estimar",
                    "severity": "med",
                }
                for i in range(5)
            ],
        },
        required_keys=("summary", "leakage_points"),
    )


async def _compose_stack_assessment(
    engagement: dict, intake_data: dict
) -> dict:
    """Phase 1 — inventory stack + recommend keep/integrate/replace per tool.

    Returns::

        {
            "summary": "<paragraph>",
            "tools": [
                {
                    "name": "...",
                    "category": "crm|email|automation|analytics|other",
                    "monthly_cost_brl": <int>,
                    "actual_usage_pct": <0-100>,
                    "verdict": "keep|integrate|replace",
                    "rationale": "<1-2 phrases>",
                    "integration_gaps": ["..."],
                },
                ...
            ],
            "integration_map_notes": "<paragraph>",
            "total_monthly_cost_brl": <int>,
        }
    """
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    prompt = f"""Você está fazendo o stack assessment do Sales Ops Diagnostic Anuvia.

Perfil do cliente (com stack declarada):
{profile_block}

Inventario CADA tool comercial em uso e classifique. Categorias: crm | email | automation | analytics | other.

Para CADA tool:
1. name: nome do produto (ex: HubSpot, Pipedrive, RD Station, n8n, Notion, planilha)
2. category: uma das categorias acima
3. monthly_cost_brl: custo mensal estimado em R$ (use tabela pública ou estimativa setorial)
4. actual_usage_pct: % do que a tool oferece que está realmente em uso (0-100)
5. verdict: keep | integrate | replace
   - keep: usado >60%, integrado, sem redundância
   - integrate: bom mas mal conectado (sem webhook, sem sync com CRM)
   - replace: <30% uso, overlap com outra, ou licença cara demais pra o que entrega
6. rationale: 1-2 frases secas — POR QUE esse verdict
7. integration_gaps: lista de integrações que faltam (ex: ["CRM não sincroniza com email tool", "form fill não atualiza lead status"])

Quando o intake não nomear a tool exata, deduza pelo perfil ("CRM-only" → HubSpot free) e marque em rationale.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: total mensal em stack, principais redundâncias, integration gap mais crítico>",
  "tools": [
    {{
      "name": "<tool>",
      "category": "<categoria>",
      "monthly_cost_brl": <int>,
      "actual_usage_pct": <0-100>,
      "verdict": "<keep|integrate|replace>",
      "rationale": "<1-2 frases>",
      "integration_gaps": ["<gap>", ...]
    }}
  ],
  "integration_map_notes": "<parágrafo curto sobre quem fala com quem e onde quebra>",
  "total_monthly_cost_brl": <int>
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} stack assessment não gerado — revisar manualmente."
            ),
            "tools": [
                {
                    "name": "CRM (a definir)",
                    "category": "crm",
                    "monthly_cost_brl": 0,
                    "actual_usage_pct": 50,
                    "verdict": "keep",
                    "rationale": f"{_CLAUDE_FALLBACK_TAG} estimar",
                    "integration_gaps": [],
                }
            ],
            "integration_map_notes": (
                f"{_CLAUDE_FALLBACK_TAG} integration map não mapeado."
            ),
            "total_monthly_cost_brl": 0,
        },
        required_keys=("summary", "tools"),
    )


async def _compose_automation_playbook(
    engagement: dict, funnel_map: dict, leakage: dict, stack: dict
) -> dict:
    """Phase 2 — for each leakage point, recommend a specific automation
    with build-vs-buy posture and ROI estimate.

    Returns::

        {
            "summary": "<paragraph>",
            "automations": [
                {
                    "name": "...",
                    "addresses_leakage": "<leakage point name>",
                    "stage": "...",
                    "description": "<2-3 phrases>",
                    "build_vs_buy": "HubSpot workflows|n8n custom|Make.com|Zapier|custom code|CRM native",
                    "build_vs_buy_rationale": "<1 line>",
                    "impact": "low|med|high",
                    "effort_days": <int>,
                    "monthly_savings_brl_low": <int>,
                    "monthly_savings_brl_high": <int>,
                    "monthly_cost_brl": <int>,
                    "priority_rank": <int>,
                },
                ...
            ],
            "top_priorities": ["<name>", ...],  # top 5-8 ranked
        }
    """
    leakage_points = leakage.get("leakage_points") or []
    leakage_block_lines: List[str] = []
    for lp in leakage_points:
        if not isinstance(lp, dict):
            continue
        leakage_block_lines.append(
            f"- {lp.get('name')} | stage: {lp.get('stage')} | "
            f"canal: {lp.get('channel')} | vol/sem: {lp.get('volume_per_week')} | "
            f"impacto/mês: R$ {lp.get('monthly_impact_brl')} | "
            f"severity: {lp.get('severity')} | "
            f"root cause: {lp.get('root_cause')}"
        )
    leakage_block = "\n".join(leakage_block_lines) or "(sem leakage points)"

    tools_block_lines: List[str] = []
    for t in stack.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tools_block_lines.append(
            f"- {t.get('name')} ({t.get('category')}): "
            f"{t.get('verdict')} — {t.get('rationale')}"
        )
    tools_block = "\n".join(tools_block_lines) or "(stack vazio)"

    options_block = " | ".join(_BUILD_VS_BUY_OPTIONS)

    prompt = f"""Você está construindo o automation playbook do Sales Ops Diagnostic Anuvia.

Leakage points identificados:
{leakage_block}

Stack disponível:
{tools_block}

Para CADA leakage point, recomende UMA automação concreta. Você pode propor automações adicionais (até 8 no total) que atacam padrões transversais (ex: lead routing, follow-up cadence, no-show recovery).

Para CADA automação:
1. name: nome curto (max 8 palavras)
2. addresses_leakage: nome exato do leakage point principal que ataca (ou "transversal" se não for 1:1)
3. stage: stage do funil onde opera
4. description: 2-3 frases concretas — o que dispara, o que faz, qual o resultado esperado
5. build_vs_buy: ESCOLHA UMA: {options_block}
   - HubSpot workflows: cliente usa HubSpot, lógica cabe em workflow nativo
   - CRM native: cliente usa outro CRM e a feature já existe lá
   - n8n custom: lógica complexa, integração multi-sistema, controle total
   - Make.com / Zapier: lógica simples, baixo volume, sem dev
   - custom code: edge case, alta volume, lock-in pra evitar
6. build_vs_buy_rationale: 1 linha justificando a escolha
7. impact: low | med | high
8. effort_days: dias-pessoa de implementação (1-30)
9. monthly_savings_brl_low e high: banda de impacto recuperado por mês em R$ (sempre banda, nunca número único)
10. monthly_cost_brl: custo mensal recorrente da automação (licença + manutenção rateada)
11. priority_rank: ranqueamento global 1-N (1 = mais prioritário)

Regra de priorização: (impact × monthly_savings_high) / effort_days × ROI confidence. Top 5-8 entram em top_priorities.

Quando faltar dado, marque em description como "estimativa baseada em padrões setoriais" e use o low band conservador.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: total recuperável por mês banda baixa-alta, principal posture (HubSpot vs n8n vs Zapier), risco de execução>",
  "automations": [
    {{
      "name": "<max 8 palavras>",
      "addresses_leakage": "<leakage name ou 'transversal'>",
      "stage": "<stage>",
      "description": "<2-3 frases>",
      "build_vs_buy": "<opção>",
      "build_vs_buy_rationale": "<1 linha>",
      "impact": "<low|med|high>",
      "effort_days": <int>,
      "monthly_savings_brl_low": <int>,
      "monthly_savings_brl_high": <int>,
      "monthly_cost_brl": <int>,
      "priority_rank": <int>
    }}
  ],
  "top_priorities": ["<name>", ...]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} automation playbook não gerado — revisar manualmente."
            ),
            "automations": [
                {
                    "name": f"Automação {i+1}",
                    "addresses_leakage": (
                        (lp.get("name") if isinstance(lp, dict) else "transversal")
                        or "transversal"
                    ),
                    "stage": (
                        (lp.get("stage") if isinstance(lp, dict) else "—") or "—"
                    ),
                    "description": f"{_CLAUDE_FALLBACK_TAG} estimar.",
                    "build_vs_buy": "HubSpot workflows",
                    "build_vs_buy_rationale": (
                        f"{_CLAUDE_FALLBACK_TAG} default."
                    ),
                    "impact": "med",
                    "effort_days": 5,
                    "monthly_savings_brl_low": 0,
                    "monthly_savings_brl_high": 0,
                    "monthly_cost_brl": 0,
                    "priority_rank": i + 1,
                }
                for i, lp in enumerate(leakage_points[:5])
            ],
            "top_priorities": [
                (lp.get("name") if isinstance(lp, dict) else f"Automação {i+1}")
                or f"Automação {i+1}"
                for i, lp in enumerate(leakage_points[:5])
            ],
        },
        required_keys=("summary", "automations", "top_priorities"),
    )


async def _compose_roadmap_90day(
    engagement: dict, playbook: dict
) -> str:
    """Phase 2 — 13-week roadmap markdown with 3 phases:
      - Phase 1 quick wins (weeks 1-4)
      - Phase 2 structural (weeks 5-9)
      - Phase 3 optimization (weeks 10-13)

    Each phase carries KPIs + evolution gates.
    """
    automations = playbook.get("automations") or []
    top = playbook.get("top_priorities") or []

    blocks: List[str] = []
    for name in top:
        match = next(
            (
                a for a in automations
                if isinstance(a, dict) and a.get("name") == name
            ),
            None,
        )
        if not match:
            continue
        blocks.append(
            f"- {match.get('name')} | leakage: {match.get('addresses_leakage')} | "
            f"build: {match.get('build_vs_buy')} | "
            f"effort: {match.get('effort_days')}d | "
            f"impact: {match.get('impact')} | "
            f"savings/mês: R$ {match.get('monthly_savings_brl_low')}–"
            f"R$ {match.get('monthly_savings_brl_high')}"
        )
    top_block = "\n".join(blocks) or "(top priorities vazio)"

    prompt = f"""Escreva o roadmap de 90 dias (13 semanas) do Sales Ops Diagnostic Anuvia em markdown.

Top automações priorizadas:
{top_block}

Estrutura obrigatória:

## Resumo executivo
3-5 linhas com (a) quantas automações entram em cada fase, (b) banda de impacto agregado em 90 dias, (c) maior risco de execução, (d) decisão pedida ao sponsor.

## Sequenciamento — critério de priorização
Texto curto explicando: (impact × monthly_savings) / effort + dependência de stack. Quick wins primeiro pra ganhar momentum e credibilidade interna.

## Fase 1 — Quick wins (semanas 1-4)
Automações com effort <= 5 dias e impact ≥ med. Foco: visibilidade de funil + response time fixes + lead routing básico.
- Tabela markdown: automação | leakage atacado | build vs buy | effort (dias) | dono sugerido | semana de entrega.
- **KPIs Fase 1:** response time mediano, % leads contactados em <SLA, conversion rate stage 1→2.
- **Gate de saída Fase 1:** "passa pra Fase 2 se KPIs melhoraram X% E sponsor aprovou."

## Fase 2 — Structural (semanas 5-9)
Automações que mexem em processo (cadência outbound, scoring, no-show recovery, lost-deal sequences).
- Mesma estrutura tabular.
- **KPIs Fase 2:** velocidade de funil (deals/semana per stage), SDR/AE capacity utilization, no-show rate.
- **Gate de saída Fase 2:** "passa pra Fase 3 se velocity subiu E capacity utilization > Y%."

## Fase 3 — Optimization (semanas 10-13)
Automações de ajuste fino: scoring com data, attribution refinement, dashboard executivo, alertas de pipeline at-risk.
- Mesma estrutura tabular.
- **KPIs Fase 3:** quota attainment, pipeline coverage ratio, conversion stage-a-stage refinado.
- **Gate de saída Fase 3:** "diagnóstico concluído — operação rodando com clareza de número."

## Dependências cross-fase
Quais automações da Fase 2 dependem de algo da Fase 1. Quais da Fase 3 dependem da Fase 2. Riscos de pular fase.

## Governança contínua (pós-90d)
Cadência semanal de revisão (template inline): KPIs principais, threshold que dispara intervenção, dono da revisão. Mensal: review com sponsor — vai/não-vai pra próxima onda de automação.

Voz Anuvia: seca, direta, numbers-first. Cada automação carrega build vs buy nomeado. NUNCA prometa o que não se mede.
"""
    return await _claude_call_with_voice(prompt, max_tokens=4000)


async def _compose_tooling_recommendations(
    engagement: dict, playbook: dict, stack: dict
) -> str:
    """Phase 2 — tooling recommendations markdown summarising build-vs-buy
    choices, license footprint, integration plumbing."""
    automations = playbook.get("automations") or []

    by_build: Dict[str, List[dict]] = {}
    for a in automations:
        if not isinstance(a, dict):
            continue
        key = a.get("build_vs_buy") or "—"
        by_build.setdefault(key, []).append(a)

    build_block_lines: List[str] = []
    for opt, items in by_build.items():
        names = ", ".join(str(it.get("name") or "—") for it in items)
        build_block_lines.append(f"- **{opt}** ({len(items)}): {names}")
    build_block = "\n".join(build_block_lines) or "(sem automações)"

    tools_block_lines: List[str] = []
    for t in stack.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tools_block_lines.append(
            f"- {t.get('name')} ({t.get('category')}): "
            f"{t.get('verdict')} — R$ {t.get('monthly_cost_brl')}/mês — "
            f"{t.get('rationale')}"
        )
    tools_block = "\n".join(tools_block_lines) or "(stack vazio)"

    prompt = f"""Escreva o documento de tooling recommendations do Sales Ops Diagnostic Anuvia em markdown.

Distribuição de build vs buy por automação:
{build_block}

Stack atual:
{tools_block}

Estrutura:

## Resumo
3-4 linhas com (a) qual posture domina (HubSpot vs n8n vs Zapier vs Make), (b) custo recorrente agregado das automações, (c) gap de integração mais crítico, (d) decisão crítica de tooling pedida.

## Recomendações por automação
Para CADA automação, escreva uma subseção `### <nome>` com:
- **Build vs buy:** opção escolhida
- **Justificativa:** 2 linhas — por que essa opção e não as outras
- **Setup esperado:** dias-pessoa + dependências (precisa de webhook? API access? trigger from CRM?)
- **Custo recorrente:** R$/mês
- **Manutenção:** quem mantém (cliente / Anuvia ongoing / vendor)

## Stack — keep / integrate / replace
Tabela markdown com: tool | categoria | custo/mês | uso real | verdict | ação proposta.

## Integration plumbing
Quais conectores precisam ser feitos (CRM ↔ email, form ↔ CRM, WhatsApp ↔ CRM, etc). Para cada um: opção sugerida (n8n? native? Zapier?) + custo + complexidade.

## Decisões pedidas ao sponsor
- Aprovação de novas licenças (qual tool, qual tier, custo/mês)
- Descontinuação de licenças existentes (qual tool, economia/mês)
- Alocação de tempo de dev/ops interno (quantas horas/semana)
- Decisão: usar n8n self-hosted ou ir de Make.com pra começar

Voz Anuvia: seca, direta, numbers-first. Cada recomendação com posture nomeada e justificativa.
"""
    return await _claude_call_with_voice(prompt, max_tokens=4000)


async def _compose_executive_deck(
    engagement: dict,
    funnel_map: dict,
    leakage: dict,
    stack: dict,
    playbook: dict,
) -> str:
    """Phase 2 — slide-by-slide markdown skeleton (20 slides target)."""
    top = playbook.get("top_priorities") or []
    automations = playbook.get("automations") or []

    monthly_low = sum(
        int(a.get("monthly_savings_brl_low") or 0)
        for a in automations if isinstance(a, dict)
    )
    monthly_high = sum(
        int(a.get("monthly_savings_brl_high") or 0)
        for a in automations if isinstance(a, dict)
    )

    n_leakage = len(leakage.get("leakage_points") or [])
    n_automations = len(automations)
    n_stages = len(funnel_map.get("stages") or [])

    top_block = "\n".join(f"- {n}" for n in top[:8]) or "(top vazio)"

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (20 slides) pra fechar um Sales Ops Diagnostic Anuvia.

Números headline:
- {n_stages} stages mapeados
- {n_leakage} leakage points nomeados
- {n_automations} automações priorizadas
- Banda de impacto mensal recuperável: R$ {_brl(monthly_low)} – R$ {_brl(monthly_high)}

Top priorities:
{top_block}

Para cada slide:

### Slide N — <título>
- 3-5 bullets curtos (uma frase cada, sem ponto final)
- (notas: <fala de 30s do apresentador>)

Estrutura sugerida (20 slides):
1. Slide 1 — capa: cliente, escopo, prazo.
2. Slide 2 — sumário executivo (n leakage, n automações, banda de savings, payback médio).
3. Slide 3 — contexto: o que o cliente pediu + como respondemos em 2 semanas.
4. Slide 4 — metodologia: funnel mapping → leakage points → stack audit → automation map → roadmap 90d.
5. Slide 5 — funnel atual (visualização texto-instrução: stage → conv rate → dwell → volume).
6. Slide 6 — channel response time SLAs (target vs actual por canal, com gap_severity).
7. Slide 7 — sales cycle distribution (mediano / P75 / P95) com benchmark setorial.
8. Slide 8 — top 3-5 leakage points NOMEADOS (cada um com nome, volume/semana, impacto/mês).
9. Slide 9 — stack assessment overview (gráfico-instrução: keep / integrate / replace por tool).
10. Slide 10 — integration gaps críticos (qual tool não fala com qual + impacto operacional).
11. Slide 11 — automation playbook overview (n automações, distribuição por build vs buy).
12. Slides 12-15 — UM SLIDE POR AUTOMAÇÃO TOP-4. Cada slide: nome, leakage atacado, build vs buy + justificativa, effort em dias, impacto monthly_savings band, monthly_cost.
13. Slide 16 — roadmap 90 dias (timeline visual: Fase 1 quick wins / Fase 2 structural / Fase 3 optimization).
14. Slide 17 — KPIs por fase + gates de evolução.
15. Slide 18 — tooling recommendations (tabela: tool atual → ação + tool novo se aplicável + custo delta/mês).
16. Slide 19 — decisões pedidas ao sponsor (3-5 decisões concretas: licenças, descontinuações, tempo de dev interno).
17. Slide 20 — encerramento + retainer Anuvia ongoing (CTA opcional).

Voz Anuvia: seca, direta, anti-hype. Bullets curtos sem ponto final. Cada automação com build vs buy nomeado.
"""
    return await _claude_call_with_voice(prompt, max_tokens=4500)


async def _compose_final_executive_report(
    engagement: dict,
    funnel_map: dict,
    leakage: dict,
    stack: dict,
    playbook: dict,
    roadmap_md: str,
    tooling_md: str,
) -> str:
    """Phase 2 — full executive report markdown (target 12-18 pages)."""
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    profile_lines = [
        f"- {k}: {v}" for k, v in intake.items() if v not in (None, "", [])
    ]
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    funnel_md = _funnel_to_markdown(funnel_map)
    leakage_md = _leakage_to_markdown(leakage)
    stack_md = _stack_to_markdown(stack)
    playbook_md = _playbook_to_markdown(playbook)

    checklist_block = "\n".join(
        f"{i+1}. ☐ {item}" for i, item in enumerate(_SALESOPS_CHECKLIST_12)
    )

    prompt = f"""Você está escrevendo o relatório executivo final do Sales Ops Diagnostic Anuvia.

Perfil do cliente:
{profile_block}

Funnel map (semana 1):
{funnel_md[:2500]}

Leakage points (semana 1):
{leakage_md[:2500]}

Stack assessment (semana 1):
{stack_md[:2000]}

Automation playbook (semana 2):
{playbook_md[:3000]}

Roadmap 90 dias (semana 2, resumo):
{roadmap_md[:2000]}

Tooling recommendations (semana 2, resumo):
{tooling_md[:1500]}

Estruture o documento markdown com estas seções, nesta ordem:

1. **## Sumário executivo** — 1 página: contexto, principais números (n leakage points, n automações, banda de savings mensal, payback médio), 3-5 decisões pedidas ao sponsor.
2. **## Contexto do cliente** — perfil comercial, time, ciclo de venda, ticket médio, stack declarada, dores auto-reportadas.
3. **## Metodologia** — funnel mapping → leakage points → stack audit → automation map → roadmap 90d.
4. **## Funnel map** — uma subseção por stage com conversion rate, dwell, volume/mês, drop-off reasons. Inclua tabela markdown com todos os stages.
5. **## Channel SLAs e response time** — tabela target vs actual por canal, com gap_severity. Comentário sobre canal mais problemático.
6. **## Sales cycle distribution** — mediano / P75 / P95 com comentário sobre cauda longa e implicação no forecasting.
7. **## Leakage points nomeados** — uma subseção por leakage point (`### <nome>`) com stage, canal, volume/semana, impacto/mês em R$, root cause, severity.
8. **## Stack assessment** — tabela com tool, categoria, custo/mês, uso real %, verdict (keep/integrate/replace), rationale. Subseção sobre integration map gaps.
9. **## Automation playbook** — uma subseção por automação (`### <nome>`) com leakage atacado, stage, descrição, build vs buy + justificativa, impact, effort em dias, savings band mensal, custo mensal, priority rank.
10. **## Roadmap 90 dias (13 semanas)** — incluir conteúdo do roadmap composto, com Fase 1 quick wins / Fase 2 structural / Fase 3 optimization, KPIs + gates.
11. **## Tooling recommendations** — incluir conteúdo do tooling doc, com keep/integrate/replace + integration plumbing + decisões de licença.
12. **## Riscos top 5** — execução, capacity do time, vendor dependence, data hygiene, sponsor disponibilidade. Cada um com mitigação proposta.
13. **## Governança contínua** — cadência semanal e mensal, métricas obrigatórias (response time mediano, conversion stage-a-stage, velocity, capacity utilization, no-show rate), thresholds de intervenção.
14. **## Handoff checklist — 12 itens revisados em todo Sales Ops Diagnostic Anuvia**

{checklist_block}

15. **## Apêndices** — glossário (funnel, conversion rate stage-a-stage, dwell time, velocity, response time SLA, build vs buy), referências de playbooks setoriais consultados, links para templates Notion (se aplicável).

Voz Anuvia: seca, direta, numbers-first. Cada automação carrega build vs buy nomeado. Estimativas marcadas como tal. NUNCA prometa o que não se mede.
"""
    return await _claude_call_with_voice(prompt, max_tokens=4500)


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

    # Try to locate first '{' and last '}' if there's leading/trailing prose.
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
        log.warning("growth_salesops: claude returned non-JSON: %s", exc)
        out = fallback_factory()
        if isinstance(out, dict):
            out["summary"] = (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude.\n\n"
                f"{text[:1200]}"
            )
        return out


def _funnel_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Stages")
    out.append("")
    stages = data.get("stages") or []
    for s in stages:
        if not isinstance(s, dict):
            continue
        out.append(f"### {s.get('order')}. {s.get('name') or '—'}")
        out.append(f"- **Conversion rate stage-a-stage:** {s.get('conversion_rate_pct')}%")
        out.append(f"- **Avg dwell time:** {s.get('avg_dwell_days')} dias")
        out.append(f"- **Volume mensal estimado:** {s.get('volume_per_month')}")
        reasons = s.get("drop_off_reasons") or []
        if isinstance(reasons, list) and reasons:
            out.append(
                f"- **Drop-off reasons:** {', '.join(str(r) for r in reasons)}"
            )
        out.append("")

    channels = data.get("channel_slas") or []
    if channels:
        out.append("## Channel SLAs")
        out.append("")
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            out.append(f"### {ch.get('channel') or '—'}")
            out.append(f"- **Target:** {ch.get('target_response') or '—'}")
            out.append(f"- **Actual:** {ch.get('actual_response') or '—'}")
            out.append(f"- **Gap severity:** {ch.get('gap_severity') or '—'}")
            out.append("")

    velocity_notes = data.get("funnel_velocity_notes")
    if velocity_notes:
        out.append("## Funnel velocity")
        out.append(str(velocity_notes))
        out.append("")

    cycle = data.get("sales_cycle") or {}
    if isinstance(cycle, dict) and cycle:
        out.append("## Sales cycle distribution")
        out.append(f"- **Mediano:** {cycle.get('median_days')} dias")
        out.append(f"- **P75:** {cycle.get('p75_days')} dias")
        out.append(f"- **P95:** {cycle.get('p95_days')} dias")
        if cycle.get("notes"):
            out.append(f"- **Notas:** {cycle.get('notes')}")
    return "\n".join(out)


def _leakage_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Leakage points nomeados")
    out.append("")
    points = data.get("leakage_points") or []
    total_monthly = 0
    for lp in points:
        if not isinstance(lp, dict):
            continue
        out.append(f"### {lp.get('name') or '—'}")
        out.append(f"- **Stage:** {lp.get('stage') or '—'}")
        out.append(f"- **Canal:** {lp.get('channel') or '—'}")
        out.append(f"- **Volume/semana:** {lp.get('volume_per_week')}")
        impact = int(lp.get("monthly_impact_brl") or 0)
        total_monthly += impact
        out.append(f"- **Impacto mensal:** R$ {_brl(impact)}")
        out.append(f"- **Severity:** {lp.get('severity') or '—'}")
        out.append(f"- **Root cause:** {lp.get('root_cause') or '—'}")
        out.append("")
    out.append("## Total mensal vazando")
    out.append(f"- **Soma agregada:** R$ {_brl(total_monthly)}/mês")
    return "\n".join(out)


def _stack_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Inventário")
    tools = data.get("tools") or []
    total = 0
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append(f"### {t.get('name') or '—'}")
        out.append(f"- **Categoria:** {t.get('category') or '—'}")
        cost = int(t.get("monthly_cost_brl") or 0)
        total += cost
        out.append(f"- **Custo mensal:** R$ {_brl(cost)}")
        out.append(f"- **Uso real:** {t.get('actual_usage_pct')}%")
        out.append(f"- **Verdict:** {t.get('verdict') or '—'}")
        out.append(f"- **Justificativa:** {t.get('rationale') or '—'}")
        gaps = t.get("integration_gaps") or []
        if isinstance(gaps, list) and gaps:
            out.append("- **Integration gaps:**")
            for g in gaps:
                out.append(f"  - {g}")
        out.append("")
    notes = data.get("integration_map_notes")
    if notes:
        out.append("## Integration map")
        out.append(str(notes))
        out.append("")
    out.append("## Total mensal de stack")
    total_field = int(data.get("total_monthly_cost_brl") or total or 0)
    out.append(f"- **Custo agregado:** R$ {_brl(total_field)}/mês")
    return "\n".join(out)


def _playbook_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Automações priorizadas")
    out.append("")
    automations = sorted(
        (a for a in (data.get("automations") or []) if isinstance(a, dict)),
        key=lambda a: int(a.get("priority_rank") or 999),
    )
    total_low = 0
    total_high = 0
    total_cost = 0
    for a in automations:
        out.append(f"### #{a.get('priority_rank') or '—'} — {a.get('name') or '—'}")
        out.append(f"- **Ataca leakage:** {a.get('addresses_leakage') or '—'}")
        out.append(f"- **Stage:** {a.get('stage') or '—'}")
        out.append(f"- **Build vs buy:** {a.get('build_vs_buy') or '—'}")
        out.append(
            f"- **Justificativa:** {a.get('build_vs_buy_rationale') or '—'}"
        )
        out.append(f"- **Impact:** {a.get('impact') or '—'}")
        out.append(f"- **Effort:** {a.get('effort_days')} dias-pessoa")
        low = int(a.get("monthly_savings_brl_low") or 0)
        high = int(a.get("monthly_savings_brl_high") or 0)
        cost = int(a.get("monthly_cost_brl") or 0)
        total_low += low
        total_high += high
        total_cost += cost
        out.append(
            f"- **Savings mensal (banda):** R$ {_brl(low)} – R$ {_brl(high)}"
        )
        out.append(f"- **Custo recorrente:** R$ {_brl(cost)}/mês")
        out.append(f"- **Descrição:** {a.get('description') or '—'}")
        out.append("")

    out.append("## Top priorities")
    top = data.get("top_priorities") or []
    if not top:
        out.append("- (vazio)")
    else:
        for n in top:
            out.append(f"- {n}")
    out.append("")
    out.append("## Total agregado")
    out.append(
        f"- **Savings mensal (banda):** R$ {_brl(total_low)} – R$ {_brl(total_high)}"
    )
    out.append(f"- **Custo recorrente:** R$ {_brl(total_cost)}/mês")
    out.append(
        f"- **Banda anual:** R$ {_brl(total_low * 12)} – R$ {_brl(total_high * 12)}"
    )
    return "\n".join(out)


def _aggregate_savings_band(playbook: dict) -> Tuple[int, int]:
    """Return (annual_low, annual_high) in BRL across all automations."""
    automations = playbook.get("automations") or []
    monthly_low = sum(
        int(a.get("monthly_savings_brl_low") or 0)
        for a in automations if isinstance(a, dict)
    )
    monthly_high = sum(
        int(a.get("monthly_savings_brl_high") or 0)
        for a in automations if isinstance(a, dict)
    )
    return monthly_low * 12, monthly_high * 12


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
      1. Patch engagement: status='kickoff', total_phases=2, current_phase=1.
      2. Email the lead the intake form link.
      3. Schedule ``growth_phase_1_funnel`` on the lead 1 day out.
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
        "total_phases": 2,
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
            f"{BASE_URL}/api/delivery/growth_salesops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
        html = _kickoff_email_html(
            first_name=first_name,
            intake_url=intake_url,
            value_str=value_str,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Sales Ops Diagnostic começou — primeiro passo (intake)",
                html=html,
                kind="growth_salesops_kickoff",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "growth_salesops.kickoff: email send failed eng=%s", engagement_id
            )

    next_at = _now() + timedelta(days=1)
    if lead and lead.get("id"):
        await session_set_next(
            str(lead["id"]),
            next_action="growth_phase_1_funnel",
            next_action_at=next_at,
        )
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.growth_salesops",
            action="growth_kickoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} kickoff; intake email sent; "
                f"phase 1 scheduled at {next_at.isoformat()}"
            ),
        )

    company = (lead or {}).get("company") or "—"
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":rocket: *Sales Ops Diagnostic kickoff* — engagement `{engagement_id}` "
        f"({company}) · R$ {value_str} · 2 semanas. "
        f"Intake enviado pra {email or 'n/a'}."
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "next_action_at": next_at,
    }


async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute phase N of the Sales Ops Diagnostic. Idempotent."""
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    current = int(engagement.get("current_phase") or 1)

    if phase < current and current >= 3:
        # Already delivered, don't rewind.
        log.info(
            "growth_salesops.run_phase: skipping phase %s, current=%s eng=%s",
            phase, current, engagement_id,
        )
        return {"ok": True, "skipped": True, "current_phase": current}

    if phase == 1:
        return await _run_phase_1(engagement)
    if phase == 2:
        return await _run_phase_2(engagement)

    return {"ok": False, "reason": f"unknown_phase_{phase}"}


async def generate_deliverable(
    engagement_id: str, deliverable_type: str
) -> dict:
    """Generate one deliverable on demand. Useful for re-rendering or
    when an operator manually requests a refresh from the admin UI.

    Returns ``{ok, url, type}`` on success.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    funnel_map = artifacts.get("phase_1_funnel_map") or {}
    leakage = artifacts.get("phase_1_leakage_points") or {}
    stack = artifacts.get("phase_1_stack_assessment") or {}
    playbook = artifacts.get("phase_2_automation_playbook") or {}
    roadmap_md = artifacts.get("phase_2_roadmap_md") or ""
    tooling_md = artifacts.get("phase_2_tooling_md") or ""

    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    if deliverable_type == "funnel_map":
        if not funnel_map:
            funnel_map = await _compose_funnel_map(engagement, intake)
        body_md = _funnel_to_markdown(funnel_map)
        url = await _render_and_upload(
            engagement_id,
            title="Funnel map — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/funnel_map.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_funnel_map": funnel_map,
                "funnel_map_md": body_md,
                "funnel_map_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "leakage_points":
        if not funnel_map:
            funnel_map = await _compose_funnel_map(engagement, intake)
        if not leakage:
            leakage = await _compose_leakage_points(engagement, funnel_map, intake)
        body_md = _leakage_to_markdown(leakage)
        url = await _render_and_upload(
            engagement_id,
            title="Leakage points — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/leakage_points.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_leakage_points": leakage,
                "leakage_points_md": body_md,
                "leakage_points_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "stack_assessment":
        if not stack:
            stack = await _compose_stack_assessment(engagement, intake)
        body_md = _stack_to_markdown(stack)
        url = await _render_and_upload(
            engagement_id,
            title="Stack assessment — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/stack_assessment.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_stack_assessment": stack,
                "stack_assessment_md": body_md,
                "stack_assessment_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "automation_playbook":
        if not funnel_map:
            funnel_map = await _compose_funnel_map(engagement, intake)
        if not leakage:
            leakage = await _compose_leakage_points(engagement, funnel_map, intake)
        if not stack:
            stack = await _compose_stack_assessment(engagement, intake)
        if not playbook:
            playbook = await _compose_automation_playbook(
                engagement, funnel_map, leakage, stack
            )
        body_md = _playbook_to_markdown(playbook)
        url = await _render_and_upload(
            engagement_id,
            title="Automation playbook — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=body_md,
            object_path=f"{engagement_id}/automation_playbook.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_automation_playbook": playbook,
                "automation_playbook_md": body_md,
                "automation_playbook_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "roadmap_90day":
        if not playbook:
            return {"ok": False, "reason": "automation_playbook_missing"}
        roadmap_md = await _compose_roadmap_90day(engagement, playbook)
        url = await _render_and_upload(
            engagement_id,
            title="Roadmap 90 dias — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=roadmap_md,
            object_path=f"{engagement_id}/roadmap_90day.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_roadmap_md": roadmap_md,
                "roadmap_90day_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "tooling_recommendations":
        if not playbook:
            return {"ok": False, "reason": "automation_playbook_missing"}
        if not stack:
            stack = await _compose_stack_assessment(engagement, intake)
        tooling_md = await _compose_tooling_recommendations(
            engagement, playbook, stack
        )
        url = await _render_and_upload(
            engagement_id,
            title="Tooling recommendations — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=tooling_md,
            object_path=f"{engagement_id}/tooling_recommendations.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_tooling_md": tooling_md,
                "tooling_recommendations_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "executive_deck":
        if not playbook:
            return {"ok": False, "reason": "automation_playbook_missing"}
        deck_md = await _compose_executive_deck(
            engagement, funnel_map, leakage, stack, playbook
        )
        url = await _render_and_upload(
            engagement_id,
            title="Apresentação Executiva — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 2",
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

    if deliverable_type == "final_executive_report":
        if not funnel_map:
            funnel_map = artifacts.get("phase_1_funnel_map") or {}
        if not leakage:
            leakage = artifacts.get("phase_1_leakage_points") or {}
        if not stack:
            stack = artifacts.get("phase_1_stack_assessment") or {}
        if not playbook:
            playbook = artifacts.get("phase_2_automation_playbook") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_2_roadmap_md") or ""
        if not tooling_md:
            tooling_md = artifacts.get("phase_2_tooling_md") or ""
        report_md = await _compose_final_executive_report(
            engagement, funnel_map, leakage, stack, playbook, roadmap_md, tooling_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Relatório Executivo — Sales Ops Diagnostic",
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
    """Heuristic: intake counts as submitted when the operator-facing
    fields landed in ``intake_data`` AND a sentinel timestamp is set,
    or enough of the key fields are filled.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        return False
    if intake.get("submitted_at"):
        return True
    required = (
        "executive_sponsor_email",
        "crm_in_use",
        "sales_team_composition",
        "sales_cycle_median_days",
        "avg_ticket_brl",
        "lead_sources",
        "monthly_volume",
        "response_time_sla",
        "existing_automation",
        "top_pain_points",
    )
    filled = sum(1 for k in required if intake.get(k))
    return filled >= 4


async def _run_phase_1(engagement: dict) -> dict:
    """Phase 1 — wait for intake submission, compose funnel map + leakage
    + stack assessment, advance to phase 2."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    if _intake_submitted(engagement):
        intake = engagement.get("intake_data") or {}
        if not isinstance(intake, dict):
            intake = {}

        funnel_map = await _compose_funnel_map(engagement, intake)
        leakage = await _compose_leakage_points(engagement, funnel_map, intake)
        stack = await _compose_stack_assessment(engagement, intake)

        funnel_md = _funnel_to_markdown(funnel_map)
        leakage_md = _leakage_to_markdown(leakage)
        stack_md = _stack_to_markdown(stack)

        funnel_url = await _render_and_upload(
            engagement_id,
            title="Funnel map — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=funnel_md,
            object_path=f"{engagement_id}/funnel_map.pdf",
        )
        leakage_url = await _render_and_upload(
            engagement_id,
            title="Leakage points — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=leakage_md,
            object_path=f"{engagement_id}/leakage_points.pdf",
        )
        stack_url = await _render_and_upload(
            engagement_id,
            title="Stack assessment — Sales Ops Diagnostic",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=stack_md,
            object_path=f"{engagement_id}/stack_assessment.pdf",
        )

        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_funnel_map": funnel_map,
                "funnel_map_md": funnel_md,
                "funnel_map_url": funnel_url,
                "phase_1_leakage_points": leakage,
                "leakage_points_md": leakage_md,
                "leakage_points_url": leakage_url,
                "phase_1_stack_assessment": stack,
                "stack_assessment_md": stack_md,
                "stack_assessment_url": stack_url,
            },
        )

        if email:
            html = _phase1_email_html(
                first_name=first_name,
                funnel_url=funnel_url,
                leakage_url=leakage_url,
                stack_url=stack_url,
                n_stages=len(funnel_map.get("stages") or []),
                n_leakage=len(leakage.get("leakage_points") or []),
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject="Funil + leakage + stack — Semana 1 Sales Ops",
                    html=html,
                    kind="growth_salesops_phase_1_funnel",
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "growth_salesops.phase_1: email failed eng=%s", engagement_id
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
            "next_action": "growth_phase_2_automation",
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
            f"{BASE_URL}/api/delivery/growth_salesops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _intake_reminder_email_html(
            first_name=first_name, intake_url=intake_url
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Intake pendente — Sales Ops Diagnostic",
                html=html,
                kind="growth_salesops_intake_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"intake_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":hourglass: Sales Ops engagement `{engagement_id}` — "
                f"intake pendente há {elapsed.days} dias. Lembrete enviado pra "
                f"{email}."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "growth_salesops.phase_1: reminder send failed eng=%s",
                engagement_id,
            )

    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "intake_submission",
        "next_action": "growth_phase_1_funnel",
        "next_action_at": next_at,
    }


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — compose automation playbook + roadmap + tooling + deck +
    final report. Close engagement."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    funnel_map = artifacts.get("phase_1_funnel_map") or {}
    leakage = artifacts.get("phase_1_leakage_points") or {}
    stack = artifacts.get("phase_1_stack_assessment") or {}

    # Backfill if a previous phase silently failed.
    if not funnel_map:
        funnel_map = await _compose_funnel_map(engagement, intake)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_funnel_map": funnel_map}
        )
    if not leakage:
        leakage = await _compose_leakage_points(engagement, funnel_map, intake)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_leakage_points": leakage}
        )
    if not stack:
        stack = await _compose_stack_assessment(engagement, intake)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_stack_assessment": stack}
        )

    playbook = await _compose_automation_playbook(
        engagement, funnel_map, leakage, stack
    )
    playbook_md = _playbook_to_markdown(playbook)

    roadmap_md = await _compose_roadmap_90day(engagement, playbook)
    tooling_md = await _compose_tooling_recommendations(
        engagement, playbook, stack
    )
    deck_md = await _compose_executive_deck(
        engagement, funnel_map, leakage, stack, playbook
    )
    report_md = await _compose_final_executive_report(
        engagement, funnel_map, leakage, stack, playbook, roadmap_md, tooling_md
    )

    playbook_url = await _render_and_upload(
        engagement_id,
        title="Automation playbook — Sales Ops Diagnostic",
        subtitle=f"Engagement {engagement_id} · Semana 2",
        body_md=playbook_md,
        object_path=f"{engagement_id}/automation_playbook.pdf",
    )
    roadmap_url = await _render_and_upload(
        engagement_id,
        title="Roadmap 90 dias — Sales Ops Diagnostic",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/roadmap_90day.pdf",
    )
    tooling_url = await _render_and_upload(
        engagement_id,
        title="Tooling recommendations — Sales Ops Diagnostic",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=tooling_md,
        object_path=f"{engagement_id}/tooling_recommendations.pdf",
    )
    deck_url = await _render_and_upload(
        engagement_id,
        title="Apresentação Executiva — Sales Ops Diagnostic",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
    )
    report_url = await _render_and_upload(
        engagement_id,
        title="Relatório Executivo — Sales Ops Diagnostic",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
    )

    annual_low, annual_high = _aggregate_savings_band(playbook)
    n_automations = len(playbook.get("automations") or [])

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_2_automation_playbook": playbook,
            "automation_playbook_md": playbook_md,
            "automation_playbook_url": playbook_url,
            "phase_2_roadmap_md": roadmap_md,
            "roadmap_90day_url": roadmap_url,
            "phase_2_tooling_md": tooling_md,
            "tooling_recommendations_url": tooling_url,
            "deck_md": deck_md,
            "deck_url": deck_url,
            "final_report_md": report_md,
            "final_report_url": report_url,
            "savings_band_annual_low": annual_low,
            "savings_band_annual_high": annual_high,
        },
    )

    nps_url = (
        f"{BASE_URL}/api/delivery/growth_salesops/nps"
        f"?engagement_id={engagement_id}&token={_hmac_token(engagement_id, 'nps')}"
    )
    if email:
        html = _phase2_email_html(
            first_name=first_name,
            report_url=report_url,
            deck_url=deck_url,
            playbook_url=playbook_url,
            roadmap_url=roadmap_url,
            tooling_url=tooling_url,
            nps_url=nps_url,
            n_automations=n_automations,
            savings_band_low=annual_low,
            savings_band_high=annual_high,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Sales Ops Diagnostic entregue — relatório + roadmap + playbook",
                html=html,
                kind="growth_salesops_phase_2_delivery",
                cc=[RESEND_REPLY_TO_EMAIL] if RESEND_REPLY_TO_EMAIL else None,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "growth_salesops.phase_2: email failed eng=%s", engagement_id
            )

    contract_id = engagement.get("contract_id")
    invoice_result: dict = {"ok": False, "reason": "not_attempted"}
    if contract_id:
        invoice_result = await _trigger_invoice(str(contract_id), engagement_id)

    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 2,
            "status": "delivered",
            "delivered_at": _now_iso(),
            "next_phase_at": None,
        },
    )

    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":white_check_mark: *Sales Ops Diagnostic delivered* — engagement "
        f"`{engagement_id}`. Valor total R$ {value_str}. "
        f"{n_automations} automações · banda anual R$ {_brl(annual_low)}–"
        f"R$ {_brl(annual_high)}. "
        f"Próximo: invoice ({invoice_result.get('status') or 'pending'}) + "
        f"NPS. cc {SLACK_MILA_HANDLE}"
    )

    if lead and lead.get("id"):
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.growth_salesops",
            action="growth_phase_2_automation",
            result="ok",
            detail=(
                f"engagement {engagement_id} delivered; "
                f"automations={n_automations}; "
                f"annual_band=R${annual_low}-{annual_high}; "
                f"invoice {invoice_result.get('status')}"
            ),
        )
        for kind, url in (
            ("final_report", report_url),
            ("roadmap_90day", roadmap_url),
            ("automation_playbook", playbook_url),
            ("tooling_recommendations", tooling_url),
            ("executive_deck", deck_url),
        ):
            try:
                await session_append_artifact(
                    str(lead["id"]),
                    type=kind,
                    url=url,
                    meta={
                        "engagement_id": engagement_id,
                        "phase": 2,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "growth_salesops.phase_2: artifact append failed lead=%s kind=%s",
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
            "growth_salesops: lib.contract.issue_invoice unavailable — stub "
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
            "growth_salesops: issue_invoice failed contract=%s", contract_id
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
    engagement on this lead (filtered to practice='growth_salesops' to
    avoid cross-practice collisions with the autonomous 'growth' track)."""
    eid = _engagement_id_from_lead(lead)
    if eid:
        return eid
    lead_id = lead.get("id")
    if not lead_id:
        return None
    url = (
        f"{SUPA_URL}/engagements"
        f"?lead_id=eq.{_urlquote(str(lead_id), safe='')}"
        f"&practice=eq.growth_salesops"
        f"&status=in.(kickoff,running)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception("growth_salesops: resolve_engagement_id query failed")
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


@register("growth_kickoff")
async def h_growth_kickoff(lead: dict) -> dict:
    """Entry-point handler — fires once after contract.payment_webhook."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "growth_kickoff: no active engagement found",
        }
    engagement = await _engagement_get(engagement_id)
    intake = (engagement or {}).get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    result = await kickoff(engagement_id, intake)
    return {
        "next_action": "growth_phase_1_funnel",
        "next_action_at": result.get("next_action_at") or (_now() + timedelta(days=1)),
        "status": "delivery_running",
        "detail": (
            f"growth_salesops kickoff ok; engagement {engagement_id}; "
            f"intake email sent"
        ),
    }


@register("growth_phase_1_funnel")
async def h_growth_phase_1(lead: dict) -> dict:
    """Phase 1 handler — funnel mapping + leakage + stack audit."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "growth_phase_1: no active engagement",
        }
    result = await run_phase(engagement_id, 1)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running" if not result.get("delivered") else "won",
        "detail": (
            f"growth_salesops phase 1: "
            f"{'advanced→2' if result.get('advanced_to_phase') else 'waiting intake'}"
        ),
    }


@register("growth_phase_2_automation")
async def h_growth_phase_2(lead: dict) -> dict:
    """Phase 2 handler — automation playbook + roadmap + tooling + deck +
    final report + invoice + close."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "growth_phase_2: no active engagement",
        }
    result = await run_phase(engagement_id, 2)
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "won" if result.get("delivered") else "delivery_running",
        "detail": (
            f"growth_salesops phase 2: "
            f"{'delivered' if result.get('delivered') else 'in progress'}"
            f"; invoice={result.get('invoice', {}).get('status')}"
        ),
    }


@register("growth_send_progress_update")
async def h_growth_progress_update(lead: dict) -> dict:
    """Mid-phase nudge — re-runs whichever phase the engagement is on, then
    optionally emails a progress update if the client has been silent."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "growth_progress: no active engagement",
        }
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "growth_progress: engagement disappeared",
        }
    phase = int(engagement.get("current_phase") or 1)
    result = await run_phase(engagement_id, phase)

    # Best-effort: send a short progress update email when the client has
    # not seen an update in this phase yet.
    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    seen_key = f"progress_update_phase_{phase}_at"
    if not artifacts.get(seen_key):
        _, email, first_name = await _lead_for_engagement(engagement)
        phase_label = {
            1: "Funnel Mapping & Stack Audit",
            2: "Automation Playbook & Roadmap",
        }.get(phase, f"Fase {phase}")
        summary = {
            1: (
                "Funnel mapping em curso — conversion rate stage-a-stage, "
                "dwell time, channel SLAs. Leakage points sendo nomeados. "
                "Stack assessment com verdict keep/integrate/replace por tool "
                "saindo ao final desta semana."
            ),
            2: (
                "Automation map sendo construído por leakage point. Top 5-8 "
                "automações priorizadas por (impact × savings) / effort. "
                "Roadmap 13 semanas em 3 fases (quick wins, structural, "
                "optimization) com KPIs + gates. Build vs buy nomeado por "
                "automação (HubSpot workflows / n8n custom / Make.com / Zapier)."
            ),
        }.get(phase, "Diagnóstico em andamento — sem update específico para esta fase.")

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
                    subject=f"Update — {phase_label} (Sales Ops Diagnostic)",
                    html=html,
                    kind=f"growth_salesops_progress_phase_{phase}",
                )
                await _engagement_merge_artifacts(
                    engagement_id, {seen_key: _now_iso()}
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "growth_salesops.progress: email failed eng=%s phase=%s",
                    engagement_id, phase,
                )

    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": f"growth_salesops progress update: re-ran phase {phase}",
    }


# Alias — the contract module emits ``engagement_kickoff_growth_salesops``
# for the ``growth_salesops`` practice (see lib/contract.py::_kickoff_engagement).
# We register the same handler under that key so the orchestrator dispatch
# lands here directly without an intermediate translation.
HANDLER_ALIAS = "engagement_kickoff_growth_salesops"


@register(HANDLER_ALIAS)
async def h_engagement_kickoff_growth_salesops(lead: dict) -> dict:
    """Alias for ``growth_kickoff`` — wired so contract.py's emitted action
    string lands on the right handler without a string remap."""
    return await h_growth_kickoff(lead)
