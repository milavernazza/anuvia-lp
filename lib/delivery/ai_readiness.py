"""AI Readiness Sprint — autonomous delivery agent.

Owns the post-signature delivery flow for the ``ai`` practice
(R$ 25-40k, 2-3 weeks). Hands off from ``lib.contract`` once a contract
is signed and paid, then runs three weekly phases — Discovery, Scoring
& Filtering and Roadmap & Decisions — producing client deliverables and
emails along the way.

Architecture::

    contract.webhook (paid)
        |
        v
    ai_kickoff                                 [+0]
        |   (intake form sent, awaiting workshop data)
        v
    ai_phase_1_discovery                       [+1 day]
        |   (intake submitted → long list composed)
        v
    ai_phase_2_scoring                         [+1 week]
        |   (scored inventory + short list + ROI model)
        v
    ai_phase_3_roadmap                         [+1 week]
        |   (12-month roadmap + deck + final report)
        v
    status='delivered', next_action=None

Quality bar mirrors ``lib/delivery/finops_audit.py``:
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
    anti-hype, compliance-aware). See ``_BRAND_SYSTEM_PROMPT``.
  * HMAC-tokened client links use ``CONTRACT_HMAC_SECRET`` so a single
    secret drives sign + intake + approval flows.
  * Every case the agent produces carries an explicit compliance tag
    (LGPD, GxP, BACEN, SOC 2, HIPAA, ANVISA, none).
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

log = logging.getLogger("anuvia-lp.delivery.ai_readiness")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

#: Default ticket size for this practice. Midpoint of R$ 25-40k band.
PRACTICE_TICKET_BRL: int = 32000

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

# 3-week cadence — slightly shorter than FinOps' 4-week.
_PHASE_INTERVAL = timedelta(days=7)
_INTAKE_REMINDER_AFTER = timedelta(days=4)
_HTTP_TIMEOUT = 30.0

# Brand voice — pinned to every Claude system prompt in this module. Same
# tone as finops_audit but with explicit compliance-tag instruction since
# AI Readiness is compliance-heavy.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA, ex-AWS Solutions Architect, ex-Google, "
    "ex-MongoDB). Voz: seca, direta, anti-hype, primeiro os números, depois a "
    "narrativa. Frases curtas declarativas misturadas com cadeias causa-efeito "
    "mais longas. Use o léxico: vazamento, clareza, diagnóstico, processo, "
    "padrão, sobreviver em produção, gate de saída, evidência, eval. Evite: "
    "sinergia, transformação, leverage, magia, mágico, IA generativa que muda "
    "o jogo, revolucionar.\n\n"
    "REGRAS DE PROFUNDIDADE TÉCNICA (não negociáveis):\n"
    "1. Cite modelos por nome exato e versão (Claude Sonnet 4.5, Claude Haiku "
    "3.5, GPT-4o, GPT-4.1, Llama 3.1 70B, Mistral Large 2, Gemini 1.5 Pro). "
    "Nunca diga genericamente 'LLM' ou 'um modelo de linguagem'.\n"
    "2. Cite preço por 1M tokens com input/output separado quando relevante "
    "(Claude Sonnet 4.5 ~US$ 3 input / US$ 15 output por 1M tokens). Mostre "
    "a fonte do número se citar.\n"
    "3. Cite latency budgets concretos por caso (real-time chat <2s p95, "
    "batch overnight <8h, async <30s) e qual modelo cabe em cada budget.\n"
    "4. Cite compliance frames por nome (LGPD art. 7º/11/46, GxP, ANVISA RDC "
    "430, BACEN 4.658, SOC 2 Type II, HIPAA, ISO 27001, EU AI Act). Nunca "
    "'compliance regulatório' genérico — sempre o nome da norma e o artigo "
    "quando aplicável.\n"
    "5. Use números DO INTAKE do cliente. Se intake diz 50k chamadas/mês com "
    "média 800 tokens input + 400 output, todos os custos derivam disso: "
    "(50.000 × 800 × $3 + 50.000 × 400 × $15) / 1.000.000 = $420/mês. "
    "Mostre a conta.\n"
    "6. Math explícita de cost-per-inference em R$: '50k chamadas × R$ 0,02/"
    "chamada = R$ 1.000/mês × 12 = R$ 12.000/ano (USD/BRL 5,0)'. Compare "
    "build vs buy com payback em meses.\n"
    "7. Para CADA caso de uso, posture build vs buy explícita (Anthropic API "
    "direct, OpenAI direct, AWS Bedrock, Azure OpenAI, fine-tune open weights "
    "self-hosted) com justificativa de 1 linha.\n"
    "8. Para CADA caso, eval framework concreto: dataset size mínimo (n=100 "
    "para regressão simples, n=500 para classificação multi-label), métrica "
    "primária (exact match, BLEU, LLM-as-judge com critério escrito), gate "
    "para promoção a produção.\n"
    "9. ADRs em formato ADR-XX: ADR-01 (modelo escolhido + alternativas "
    "rejeitadas), ADR-02 (vector DB: pgvector vs Pinecone vs Weaviate vs "
    "Qdrant), ADR-03 (RAG architecture: naive vs hybrid vs agentic), etc.\n"
    "10. Quando estimar, use 'estimativa' uma vez só. NÃO repita 'padrão "
    "setorial' como muleta — isso é tique de junior. Nunca prometa o que "
    "não pode ser medido. Português do Brasil."
)

#: Sentinel prefix for narrative that Claude could not generate.
_CLAUDE_FALLBACK_TAG = "[CLAUDE_UNAVAILABLE_DRAFT]"

#: The 15-dimension scoring framework from SPRINT_INPUTS_MILA.md §5.2.
_SCORING_DIMENSIONS: List[str] = [
    "data_availability",        # 1
    "data_quality",             # 2
    "inference_cost",           # 3
    "latency_tolerance",        # 4
    "compliance_burden",        # 5
    "build_vs_buy",             # 6
    "tech_integration",         # 7
    "change_management",        # 8
    "roi_y1_brl",               # 9
    "time_to_value",            # 10
    "regulatory_risk",          # 11
    "reputational_risk",        # 12
    "vendor_lock_in",           # 13
    "internal_champion",        # 14
    "executive_sponsorship",    # 15
]

#: Compliance frames Claude should tag explicitly per case.
_COMPLIANCE_FRAMES: List[str] = [
    "LGPD", "GxP", "BACEN", "SOC 2", "HIPAA", "ANVISA", "nenhuma",
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
    """Format a numeric as Brazilian currency: 32.000,00."""
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
            "ai_readiness: HMAC secret unset; client links will be unverifiable"
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
            "ai_readiness: engagement_get network failed id=%s", engagement_id
        )
        return None
    if r.status_code != 200:
        log.warning(
            "ai_readiness: engagement_get non-200 id=%s: %s %s",
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
            "ai_readiness: engagement_patch network failed id=%s",
            engagement_id,
        )
        return False
    if r.status_code not in (200, 204):
        log.warning(
            "ai_readiness: engagement_patch non-2xx id=%s: %s %s",
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
            "ai_readiness: merge_artifacts: engagement %s not found",
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
        log.warning("ai_readiness: storage upload failed path=%s: %s", path, exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "ai_readiness: storage upload non-2xx path=%s status=%s body=%s",
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
        log.warning("ai_readiness: gotenberg call failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning(
            "ai_readiness: gotenberg non-200 status=%s body=%s",
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
    max_tokens: int = 6000,
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
            log.warning("ai_readiness: anthropic %s", last_err)
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
                "ai_readiness: anthropic retryable %s body=%s",
                last_err, r.text[:300],
            )
        else:
            # Non-retryable error (400/401/403)
            log.warning(
                "ai_readiness: anthropic non-retryable status=%s body=%s",
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
            "ai_readiness: RESEND_API_KEY unset; stashing draft kind=%s eng=%s",
            kind, engagement_id,
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        await _send_slack_alert(
            f":warning: AI Readiness delivery: RESEND_API_KEY missing — "
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
            {"name": "category", "value": "delivery_ai_readiness"},
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
        log.exception("ai_readiness: resend network failed kind=%s", kind)
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend network: {exc}")

    if r.status_code >= 400:
        log.error(
            "ai_readiness: resend non-2xx kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info(
        "ai_readiness: resend ok kind=%s eng=%s msg_id=%s",
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
            "ai_readiness: stash_email_draft failed eng=%s kind=%s",
            engagement_id, kind,
        )


# ---------------------------------------------------------------------------
# Email HTML templates — compact, inline-styled, brand-consistent
# ---------------------------------------------------------------------------


def _wrap_email(title: str, body_html: str) -> str:
    """Wrap a body fragment in the standard Anuvia email shell."""
    return f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · AI Readiness Sprint</p>
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
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Contrato fechado. AI Readiness Sprint começa agora. Investimento total: <strong>R$ {value_str}</strong>. Cronograma: 2-3 semanas, três fases, três entregáveis principais.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Semana 1 — Discovery.</strong> Antes do workshop preciso da informação abaixo. Sem isso, a semana 2 (scoring) não roda.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>Sponsor executivo (nome + email)</li>
  <li>Lista de stakeholders por área (marketing, ops, atendimento, antifraude, tech, compliance)</li>
  <li>Histórico de PoCs de IA já tentados (status: live / killed / stalled / em avaliação)</li>
  <li>Inventário de dados (data lakes, warehouses, histórico de CRM, transaction logs)</li>
  <li>Compliance constraints já nomeados (LGPD, GxP, BACEN, SOC 2, HIPAA, ANVISA)</li>
  <li>Budget anual de tooling de IA (R$) e capability interna (none / 1-2 engs / time dedicado)</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Workshop de 1 dia (8h) com 5-8 stakeholders fica agendado por email separado. Em paralelo, 1:1s de 45min com cada head de área.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Padrão dos últimos sprints: 12-20 casos candidatos entram, 5-8 passam pelo filtro de scoring. Os outros viram lista de "não-agora" justificada.</p>
"""
    return _wrap_email("AI Readiness Sprint começou", body)


def _phase1_email_html(
    *, first_name: str, longlist_url: str, n_cases: int
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 1 fechada. Workshop + 1:1s rodados, data inventory consolidado, compliance posture mapeada por caso.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Long list preliminar: <strong>{n_cases} casos candidatos</strong> de IA, cada um com descrição em 1 parágrafo, heat indicator de impacto e feasibility, data dependencies, e compliance flag explícito.</p>
<p style="margin:24px 0;"><a href="{longlist_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Long list (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2: scoring em 15 dimensões por caso (dado, custo de inferência, latência, compliance burden, ROI, time-to-value, risco regulatório, risco reputacional, vendor lock-in, etc). Saída: short list de 5-8 cases pra entrar no roadmap.</p>
"""
    return _wrap_email("Long list pronta — Semana 1", body)


def _phase2_email_html(
    *,
    first_name: str,
    scored_url: str,
    roi_url: str,
    short_list: List[str],
    short_list_count: int,
) -> str:
    bullets = "".join(
        f'<li style="margin:6px 0;line-height:1.55;">{c}</li>'
        for c in short_list[:8]
    )
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2 fechada. Cada caso da long list passou pelo scoring de 15 dimensões. Filtro aplicado: score combinado &gt; 70/100 + ROI defensável + compliance posture viável.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Short list — {short_list_count} casos priorizados:</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 18px 18px;padding:0;">{bullets}</ul>
<p style="margin:24px 0;"><a href="{scored_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Scored inventory completo (PDF) &rarr;</a></p>
<p style="margin:8px 0 24px;"><a href="{roi_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">ROI model — assumptions por caso (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 3: roadmap 12 meses sequenciado, gates por caso (discovery → PoV → produção) com critério de saída, build vs buy por caso, e recomendações de descontinuação dos PoCs atuais que não passaram no filtro.</p>
"""
    return _wrap_email("Scored inventory + ROI model — Semana 2", body)


def _phase3_email_html(
    *,
    first_name: str,
    report_url: str,
    deck_url: str,
    roadmap_url: str,
    nps_url: str,
    n_kill: int,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Sprint concluído. Três semanas, três entregáveis principais:</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo</a> — long list pontuada + short list + ROI model + roadmap + ADRs + descontinuações.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva</a> — 30 slides pra rodar com C-level e board.</li>
  <li><a href="{roadmap_url}" style="color:#0f172a;">Roadmap 12 meses</a> — sequenciado por (ROI/effort × dependências × compliance criticality), com gates explícitos.</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Recomendação de descontinuação: <strong>{n_kill} PoCs</strong> atuais (justificativa por caso no relatório). Continuar com eles é vazamento de budget e atenção do time.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Sessão de handoff (2h) fica agendada por email separado. A invoice da segunda parcela já entrou na fila.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Um pedido: 2 minutos pra deixar um NPS. Direto, sem firula:</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar NPS &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se conhecer outro CEO/Head de Inovação com 8-15 casos de IA no radar e zero clareza de priorização — você sabe quem precisa ouvir isso.</p>
"""
    return _wrap_email("AI Readiness Sprint entregue", body)


def _intake_reminder_email_html(*, first_name: str, intake_url: str) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete curto: o formulário de intake ainda não foi preenchido. Sem ele, o workshop de discovery não roda e o cronograma desloca.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se tiver algum bloqueio (sponsor não definido, stakeholders não confirmados, compliance team ainda mapeando) — me avisa que a gente resolve.</p>
"""
    return _wrap_email("Intake pendente — AI Readiness Sprint", body)


def _progress_update_email_html(
    *, first_name: str, phase_label: str, summary: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Update curto sobre o sprint — fase atual: <strong>{phase_label}</strong>.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">{summary}</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Próximo entregável escrito chega ao final desta semana. Qualquer coisa antes, é só responder este email.</p>
"""
    return _wrap_email("Sprint em andamento", body)


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
  .small {{ color:#64748b; font-size:11px; }}
  .meta {{ color:#475569; font-size:11px; margin:0 0 18px; }}
  .tag {{ display:inline-block; background:#fafaf9; border:1px solid #e7e5e4; padding:2px 8px; border-radius:9999px; font-size:10px; color:#475569; }}
</style></head>
<body>
<header style="margin-bottom:24px;">
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 6px;">Anuvia · AI Readiness Sprint</p>
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


async def _compose_long_list(engagement: dict, intake_data: dict) -> dict:
    """Phase 1 — given workshop intake, ask Claude to draft 12-20 candidate
    use cases with heat indicators and compliance flags.

    Returns::

        {
            "summary": "<paragraph>",
            "cases": [
                {
                    "name": "...",
                    "description": "...",
                    "area": "marketing|ops|atendimento|antifraude|tech|...",
                    "impact": "low|med|high",
                    "feasibility": "low|med|high",
                    "data_dependencies": ["..."],
                    "compliance_flags": ["LGPD", ...],
                },
                ...
            ],
        }
    """
    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio — usar padrões setoriais)"

    prompt = f"""Você está compondo a long list inicial de casos de IA do AI Readiness Sprint Anuvia.

Perfil do cliente (intake submetido após workshop):
{profile_block}

Gere entre 12 e 20 casos de uso candidatos. Cada caso DEVE ter:
1. Nome curto (max 6 palavras, ex: "Atendimento — triagem automatizada chamados").
2. Descrição (2-3 frases, voz Anuvia: seca, numbers-first).
3. Área principal (marketing | ops | atendimento | antifraude | tech | compliance | financeiro | outros).
4. Impact preliminar (low|med|high) com 1 linha de justificativa embutida na descrição.
5. Feasibility preliminar (low|med|high) com base nos dados disponíveis no intake.
6. Data dependencies (array de strings: quais datasets/sistemas o caso precisa).
7. Compliance flags (array de tags entre: LGPD, GxP, BACEN, SOC 2, HIPAA, ANVISA, nenhuma).

Quando o intake não trouxer dado suficiente, marque a descrição como "estimativa baseada em padrões setoriais".

Devolva APENAS JSON válido, sem markdown, sem comentários:

{{
  "summary": "<parágrafo 3-5 linhas: contexto, quantos casos, distribuição por área, observação sobre compliance burden agregado>",
  "cases": [
    {{
      "name": "<6 palavras max>",
      "description": "<2-3 frases>",
      "area": "<area>",
      "impact": "<low|med|high>",
      "feasibility": "<low|med|high>",
      "data_dependencies": ["<dataset/sistema>", ...],
      "compliance_flags": ["<frame>", ...]
    }}
  ]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=4000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} long list não gerada — revisar manualmente."
            ),
            "cases": [
                {
                    "name": f"Caso candidato {i+1}",
                    "description": (
                        f"{_CLAUDE_FALLBACK_TAG} estimativa pendente."
                    ),
                    "area": "outros",
                    "impact": "med",
                    "feasibility": "med",
                    "data_dependencies": [],
                    "compliance_flags": ["nenhuma"],
                }
                for i in range(12)
            ],
        },
        required_keys=("summary", "cases"),
    )


async def _compose_scored_inventory(
    engagement: dict, long_list: dict
) -> dict:
    """Phase 2 — score each candidate case across the 15 dimensions.

    Each case gets a 0-100 score per dimension PLUS a combined score
    (0-100) and a priority bucket (must_do | consider | kill).

    Returns::

        {
            "summary": "<paragraph>",
            "scored_cases": [
                {
                    "name": "...",
                    "scores": { "data_availability": 0-100, ... },
                    "combined_score": 0-100,
                    "priority_bucket": "must_do" | "consider" | "kill",
                    "compliance_tag": "LGPD" | ...,
                    "rationale": "<2-3 frases>",
                },
                ...
            ],
            "short_list": [<names of must_do + top consider, 5-8 items>],
        }
    """
    cases = long_list.get("cases") or []
    if not cases:
        return {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} long list vazia — scoring abortado."
            ),
            "scored_cases": [],
            "short_list": [],
        }

    cases_block_lines: List[str] = []
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            continue
        cases_block_lines.append(
            f"{i+1}. {c.get('name') or '—'} ({c.get('area') or '—'}) — "
            f"{c.get('description') or '—'} | "
            f"impact={c.get('impact')} feasibility={c.get('feasibility')} | "
            f"data={','.join(c.get('data_dependencies') or [])} | "
            f"compliance={','.join(c.get('compliance_flags') or [])}"
        )
    cases_block = "\n".join(cases_block_lines)

    dimensions_block = "\n".join(
        f"{i+1}. {d}" for i, d in enumerate(_SCORING_DIMENSIONS)
    )

    prompt = f"""Você está aplicando o scoring framework do AI Readiness Sprint Anuvia.

Long list de casos candidatos:
{cases_block}

Para CADA caso, atribua nota 0-100 nas 15 dimensões abaixo e calcule um score combinado 0-100 (média ponderada, com peso DOBRADO em compliance_burden, regulatory_risk e reputational_risk porque esse sprint é compliance-heavy).

Dimensões:
{dimensions_block}

Regra do priority_bucket:
- combined_score >= 75 → "must_do"
- 55 <= combined_score < 75 → "consider"
- combined_score < 55 → "kill"
- QUALQUER caso com regulatory_risk > 70 sem revisão humana obrigatória mapeada → forçar para "kill" e explicar.

Para cada caso atribua também um compliance_tag PRINCIPAL (LGPD | GxP | BACEN | SOC 2 | HIPAA | ANVISA | nenhuma). Use o frame mais restritivo aplicável.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: distribuição must_do/consider/kill, principal frame de compliance, principal gargalo de dado>",
  "scored_cases": [
    {{
      "name": "<exato do input>",
      "scores": {{
        "data_availability": <0-100>,
        "data_quality": <0-100>,
        "inference_cost": <0-100>,
        "latency_tolerance": <0-100>,
        "compliance_burden": <0-100>,
        "build_vs_buy": <0-100>,
        "tech_integration": <0-100>,
        "change_management": <0-100>,
        "roi_y1_brl": <0-100>,
        "time_to_value": <0-100>,
        "regulatory_risk": <0-100>,
        "reputational_risk": <0-100>,
        "vendor_lock_in": <0-100>,
        "internal_champion": <0-100>,
        "executive_sponsorship": <0-100>
      }},
      "combined_score": <0-100>,
      "priority_bucket": "<must_do|consider|kill>",
      "compliance_tag": "<frame>",
      "rationale": "<2-3 frases>"
    }}
  ],
  "short_list": ["<nome>", ...]
}}

Regra para short_list: todos os must_do + completar até 8 com os melhores consider. Mínimo 5, máximo 8.
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=8000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} scoring não gerado — revisar manualmente."
            ),
            "scored_cases": [
                {
                    "name": c.get("name", f"Caso {i+1}"),
                    "scores": {d: 50 for d in _SCORING_DIMENSIONS},
                    "combined_score": 50,
                    "priority_bucket": "consider",
                    "compliance_tag": (
                        (c.get("compliance_flags") or ["nenhuma"])[0]
                        if isinstance(c.get("compliance_flags"), list)
                        else "nenhuma"
                    ),
                    "rationale": f"{_CLAUDE_FALLBACK_TAG} revisar.",
                }
                for i, c in enumerate(cases)
                if isinstance(c, dict)
            ],
            "short_list": [
                (c.get("name") or f"Caso {i+1}")
                for i, c in enumerate(cases[:5])
                if isinstance(c, dict)
            ],
        },
        required_keys=("summary", "scored_cases", "short_list"),
    )


async def _compose_roi_model(engagement: dict, scored: dict) -> dict:
    """Phase 2 — generate the explicit assumptions ROI model for the short list.

    Returns::

        {
            "summary": "<paragraph>",
            "models": [
                {
                    "name": "...",
                    "compliance_tag": "...",
                    "assumptions": {
                        "volume_monthly": "...",
                        "cost_per_inference_brl": "...",
                        "ganho_marginal_pct": "...",
                        "team_cost_brl_y1": "...",
                        ...
                    },
                    "year_1_cost_brl": int,
                    "year_1_savings_brl_low": int,
                    "year_1_savings_brl_high": int,
                    "payback_months": int | str,
                },
                ...
            ],
        }
    """
    short_list = scored.get("short_list") or []
    scored_cases = scored.get("scored_cases") or []
    short_blocks: List[str] = []
    for name in short_list:
        match = next(
            (
                c for c in scored_cases
                if isinstance(c, dict) and c.get("name") == name
            ),
            None,
        )
        if not match:
            continue
        short_blocks.append(
            f"- {match.get('name')} | compliance: {match.get('compliance_tag')} | "
            f"score: {match.get('combined_score')} | "
            f"bucket: {match.get('priority_bucket')} | "
            f"rationale: {match.get('rationale')}"
        )
    cases_block = "\n".join(short_blocks) or "(short list vazia)"

    prompt = f"""Você está construindo o ROI model do AI Readiness Sprint para a short list de casos abaixo.

Short list:
{cases_block}

Para CADA caso construa uma tabela de assumptions explícita e calcule:
1. **Volume mensal estimado** (transações/inferências/decisões — escolha a métrica certa por caso).
2. **Custo por inferência em R$** (com fonte: API rate × tokens estimados, ou licença SaaS prorateada).
3. **Ganho marginal em %** (redução de tempo de atendimento, % de fraude prevenida, lift de conversão, etc — sempre com unidade clara).
4. **Custo do time em R$ ano 1** (engenharia + ops + change management).
5. **Year 1 cost em R$** (inferência + licenças + time).
6. **Year 1 savings band em R$ — baixo e alto** (com banda explícita, nunca número único).
7. **Payback em meses** (ou "> 24m" se não houver payback no horizonte).

CADA caso deve ter o compliance_tag repetido (LGPD/GxP/BACEN/SOC 2/HIPAA/ANVISA/nenhuma) porque assumptions de compliance afetam custo de implementação.

Quando faltar dado concreto, marque a assumption como "estimativa baseada em padrões setoriais" e dimensione conservador (use o low band).

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: ROI agregado, principais riscos de assumption, qual caso tem payback mais curto>",
  "models": [
    {{
      "name": "<exato>",
      "compliance_tag": "<frame>",
      "assumptions": {{
        "volume_monthly": "<string com número + unidade>",
        "cost_per_inference_brl": "<string>",
        "ganho_marginal_pct": "<string>",
        "team_cost_brl_y1": "<string>",
        "notes": "<string opcional>"
      }},
      "year_1_cost_brl": <int>,
      "year_1_savings_brl_low": <int>,
      "year_1_savings_brl_high": <int>,
      "payback_months": <int ou string>
    }}
  ]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=6000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} ROI model não gerado — revisar manualmente."
            ),
            "models": [
                {
                    "name": name,
                    "compliance_tag": "nenhuma",
                    "assumptions": {
                        "volume_monthly": f"{_CLAUDE_FALLBACK_TAG} estimar",
                        "cost_per_inference_brl": f"{_CLAUDE_FALLBACK_TAG} estimar",
                        "ganho_marginal_pct": f"{_CLAUDE_FALLBACK_TAG} estimar",
                        "team_cost_brl_y1": f"{_CLAUDE_FALLBACK_TAG} estimar",
                    },
                    "year_1_cost_brl": 0,
                    "year_1_savings_brl_low": 0,
                    "year_1_savings_brl_high": 0,
                    "payback_months": "—",
                }
                for name in short_list
            ],
        },
        required_keys=("summary", "models"),
    )


async def _compose_roadmap_12mo(
    engagement: dict, scored: dict, roi: dict
) -> str:
    """Phase 3 — 12-month roadmap markdown with explicit gates per case."""
    short_list = scored.get("short_list") or []
    scored_cases = scored.get("scored_cases") or []
    models = roi.get("models") or []

    blocks: List[str] = []
    for name in short_list:
        match = next(
            (
                c for c in scored_cases
                if isinstance(c, dict) and c.get("name") == name
            ),
            None,
        )
        roi_match = next(
            (m for m in models if isinstance(m, dict) and m.get("name") == name),
            None,
        )
        if not match:
            continue
        roi_summary = ""
        if roi_match:
            roi_summary = (
                f" | savings y1 R$ "
                f"{_brl(roi_match.get('year_1_savings_brl_low') or 0)}-"
                f"{_brl(roi_match.get('year_1_savings_brl_high') or 0)} | "
                f"payback {roi_match.get('payback_months')}m"
            )
        blocks.append(
            f"- {match.get('name')} | compliance: {match.get('compliance_tag')} | "
            f"score: {match.get('combined_score')} | "
            f"bucket: {match.get('priority_bucket')}{roi_summary}"
        )
    short_block = "\n".join(blocks) or "(short list vazia)"

    prompt = f"""Escreva um roadmap de IA de 12 meses pra um cliente Anuvia, em markdown.

Short list priorizada:
{short_block}

Estrutura:

## Resumo executivo
3-5 linhas com (a) quantos casos entram em PoV no Q1, (b) qual o frame de compliance dominante, (c) maior risco de execução, (d) decisão pedida.

## Sequenciamento — critério de priorização
Texto curto explicando o critério: (ROI × confidence) / effort, com peso dobrado em compliance criticality e dependência cross-case.

## Gates por caso
Para CADA caso da short list, escreva uma subseção `### <nome do caso>` com:
- **Compliance tag:** <frame> (linha única)
- **Gate 1 — Discovery (2-4 semanas).** Atividades, critério de saída ("vai pra PoV se ...").
- **Gate 2 — PoV (4-8 semanas).** Atividades, critério de saída ("vai pra produção se ...").
- **Gate 3 — Produção (8-12 semanas).** Atividades, critério de saída ("hardening completo se ...").
- **Build vs buy:** recomendação (1 linha + justificativa em 1 linha).
- **Dependências cross-case:** vector DB compartilhado? eval framework? observability stack?

## Horizonte 1 — Q1 (0-90 dias)
Tabela markdown: caso | gate atual | dono sugerido | esforço (dias-pessoa) | dependência crítica.

## Horizonte 2 — Q2-Q3 (90-270 dias)
Casos que entram em PoV nesse período. Mesma estrutura tabular.

## Horizonte 3 — Q4 (270-365 dias)
Casos estruturais (vendor lock-in baixo, compliance-heavy, transformação de processo).

## Governança contínua
Cadência mensal de revisão (template inline), métricas que importam (taxa de hallucination, latência p95, custo por inferência realizado vs orçado, taxa de fallback para humano), thresholds que disparam pausa.

## Descontinuações recomendadas
Lista dos PoCs/iniciativas existentes (vindas do bucket "kill" do scoring) com justificativa de 2 linhas cada. Quanto economiza por mês descontinuar.

Voz Anuvia: seca, direta, numbers-first. NUNCA prometa o que não se mede.
"""
    return await _claude_call_with_voice(prompt, max_tokens=6000)


async def _compose_executive_deck(
    engagement: dict, scored: dict, roi: dict
) -> str:
    """Phase 3 — slide-by-slide markdown skeleton (30 slides target)."""
    short_list = scored.get("short_list") or []
    n_must = sum(
        1 for c in scored.get("scored_cases") or []
        if isinstance(c, dict) and c.get("priority_bucket") == "must_do"
    )
    n_kill = sum(
        1 for c in scored.get("scored_cases") or []
        if isinstance(c, dict) and c.get("priority_bucket") == "kill"
    )

    models = roi.get("models") or []
    total_low = sum(
        int(m.get("year_1_savings_brl_low") or 0)
        for m in models if isinstance(m, dict)
    )
    total_high = sum(
        int(m.get("year_1_savings_brl_high") or 0)
        for m in models if isinstance(m, dict)
    )

    top_block = "\n".join(
        f"- {n}" for n in short_list[:8]
    ) or "(short list vazia)"

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (30 slides) pra fechar um AI Readiness Sprint Anuvia.

Top casos da short list:
{top_block}

Números headline: {n_must} cases must-do, {len(short_list)} no short list total, {n_kill} descontinuações recomendadas. Savings ano 1 (banda agregada): R$ {_brl(total_low)} – R$ {_brl(total_high)}.

Para cada slide, escreva:

### Slide N — <título>
- 3-5 bullets curtos (uma frase cada, sem ponto final)
- (notas: <fala de 30s do apresentador>)

Estrutura sugerida (30 slides):
1. Slide 1 — capa: cliente, escopo, prazo.
2. Slide 2 — sumário executivo (must_do, kill, savings band, payback médio).
3. Slide 3 — contexto: o que pediram + como vamos responder.
4. Slide 4 — metodologia: long list → 15-D scoring → short list → roadmap.
5. Slide 5 — long list (heatmap em tabela: caso × impact × feasibility × compliance).
6. Slide 6 — scoring framework (as 15 dimensões + pesos especiais em compliance/risco).
7. Slide 7 — distribuição must_do/consider/kill por área (marketing/ops/atendimento/etc).
8. Slide 8 — distribuição por compliance tag (LGPD vs BACEN vs nenhuma vs ...).
9. Slides 9-16 — UM SLIDE POR CASO da short list (top 8). Cada slide: nome, área, compliance_tag, scores principais (3 mais relevantes), ROI band y1, payback, build_vs_buy. Voz seca, numbers-first.
10. Slide 17 — ROI model agregado (gráfico-instrução: barra empilhada savings vs cost por trimestre).
11. Slide 18 — assumptions críticas que se quebradas, quebram o ROI.
12. Slide 19 — roadmap 12 meses (timeline visual: quem entra em Q1, Q2, Q3, Q4).
13. Slide 20 — dependências cross-case (vector DB? eval framework? observability?).
14. Slide 21 — gates por caso (discovery → PoV → produção, com critério de saída exemplificado).
15. Slide 22 — build vs buy summary (tabela).
16. Slide 23 — riscos top 5 (regulatório, hallucination, vendor, internal champion, sponsorship).
17. Slide 24 — compliance posture summary (LGPD posture, GxP validation gaps, BACEN approvals needed, etc).
18. Slide 25 — descontinuações recomendadas (lista + economia mensal).
19. Slide 26 — governança contínua (cadência mensal, métricas, alertas).
20. Slide 27 — ADRs principais (architectural decisions tomadas no sprint).
21. Slide 28 — handoff (próximos passos, ownership, primeira PoV a iniciar).
22. Slide 29 — pedido (sponsor sign-off, time alocado, decisão de tooling).
23. Slide 30 — encerramento + Anuvia retainer ongoing (CTA opcional).

Voz Anuvia: seca, direta, anti-hype. Bullets curtos sem ponto final.
"""
    return await _claude_call_with_voice(prompt, max_tokens=8000)


async def _compose_final_executive_report(
    engagement: dict, long_list: dict, scored: dict, roi: dict, roadmap_md: str
) -> str:
    """Phase 3 — full executive report markdown (target 15-20 pages)."""
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    profile_lines = [
        f"- {k}: {v}" for k, v in intake.items() if v not in (None, "", [])
    ]
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    scored_md = _scored_to_markdown(scored)
    roi_md = _roi_to_markdown(roi)

    n_kill = sum(
        1 for c in scored.get("scored_cases") or []
        if isinstance(c, dict) and c.get("priority_bucket") == "kill"
    )

    prompt = f"""Você está escrevendo o relatório executivo final do AI Readiness Sprint Anuvia.

Perfil do cliente:
{profile_block}

Long list (semana 1) — resumo: {long_list.get('summary', '—')}

Scored inventory (semana 2):
{scored_md[:3500]}

ROI model (semana 2):
{roi_md[:2500]}

Roadmap 12 meses (semana 3, resumo):
{roadmap_md[:2500]}

Descontinuações recomendadas: {n_kill} PoCs/iniciativas.

Estruture o documento markdown com estas seções, nesta ordem:

1. **## Sumário executivo** — 1 página: contexto, principais números (casos avaliados, must_do, kill, ROI band y1, payback médio), 3 decisões pedidas ao sponsor.
2. **## Contexto do cliente** — perfil, stakeholders identificados, compliance posture inicial, capability interna.
3. **## Metodologia** — long list → 15-D scoring → ROI model → roadmap. Pesos especiais em compliance_burden/regulatory_risk/reputational_risk.
4. **## Long list — casos candidatos** — uma subseção por caso (`### <nome>`) com descrição, área, impact/feasibility preliminar, data dependencies, compliance flag.
5. **## Scoring detalhado** — uma subseção por caso com nota em cada uma das 15 dimensões, combined score, priority bucket, rationale, compliance_tag dominante.
6. **## Short list — casos priorizados** — recap focado em por que cada caso entrou.
7. **## ROI model** — tabela markdown por caso com assumptions explícitas (volume, custo unitário inferência, ganho marginal, team cost y1, payback). Marcar estimativas como "estimativa baseada em padrões setoriais".
8. **## Roadmap 12 meses** — incluir o conteúdo do roadmap composto, com gates explícitos.
9. **## Descontinuações recomendadas** — bucket "kill" detalhado, com justificativa e custo mensal economizado.
10. **## ADRs (Architecture Decision Records)** — decisões estruturais: vector DB compartilhado, eval framework, observability stack, modelo proprietário vs API. 1 ADR por decisão (contexto, decisão, alternativas, consequências).
11. **## Compliance posture summary** — por frame (LGPD/GxP/BACEN/SOC 2/HIPAA/ANVISA): quais casos tocam o frame, quais gaps existem, qual mitigation.
12. **## Riscos top 5** — regulatório, reputacional, vendor lock-in, internal champion, sponsorship. Cada um com mitigação proposta.
13. **## Governança contínua** — cadência mensal, métricas (taxa de hallucination, latência p95, custo realizado vs orçado, taxa de fallback humano), thresholds.
14. **## Handoff checklist** — os 12 itens revisados em todo AI Readiness Anuvia.
15. **## Apêndices** — referências de modelos avaliados, scoring rubric completo, glossário (LGPD/GxP/BACEN/SOC 2/HIPAA/ANVISA).

Voz Anuvia: seca, direta, numbers-first. Cada caso carrega compliance_tag explícito. Estimativas marcadas como tal.
"""
    return await _claude_call_with_voice(prompt, max_tokens=8000)


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
        log.warning("ai_readiness: claude returned non-JSON: %s", exc)
        out = fallback_factory()
        if isinstance(out, dict):
            out["summary"] = (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude.\n\n"
                f"{text[:1200]}"
            )
        return out


def _long_list_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Casos candidatos")
    out.append("")
    cases = data.get("cases") or []
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            continue
        out.append(f"### {i+1}. {c.get('name') or '—'}")
        out.append(c.get("description") or "—")
        out.append(f"- **Área:** {c.get('area') or '—'}")
        out.append(f"- **Impact preliminar:** {c.get('impact') or '—'}")
        out.append(f"- **Feasibility preliminar:** {c.get('feasibility') or '—'}")
        deps = c.get("data_dependencies") or []
        if isinstance(deps, list) and deps:
            out.append(f"- **Data dependencies:** {', '.join(str(d) for d in deps)}")
        flags = c.get("compliance_flags") or []
        if isinstance(flags, list) and flags:
            out.append(f"- **Compliance flags:** {', '.join(str(f) for f in flags)}")
        out.append("")
    out.append(f"## Total")
    out.append(f"- **Casos candidatos:** {len(cases)}")
    return "\n".join(out)


def _scored_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Scoring por caso")
    out.append("")
    cases = data.get("scored_cases") or []
    for c in cases:
        if not isinstance(c, dict):
            continue
        out.append(f"### {c.get('name') or '—'}")
        out.append(c.get("rationale") or "—")
        out.append(f"- **Compliance tag:** {c.get('compliance_tag') or 'nenhuma'}")
        out.append(f"- **Combined score:** {c.get('combined_score') or '—'}/100")
        out.append(f"- **Priority bucket:** {c.get('priority_bucket') or '—'}")
        scores = c.get("scores") or {}
        if isinstance(scores, dict):
            out.append("- **Scores por dimensão:**")
            for dim in _SCORING_DIMENSIONS:
                v = scores.get(dim)
                if v is not None:
                    out.append(f"  - {dim}: {v}/100")
        out.append("")
    sl = data.get("short_list") or []
    out.append("## Short list")
    if not sl:
        out.append("- (vazia)")
    else:
        for n in sl:
            out.append(f"- {n}")
    return "\n".join(out)


def _roi_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## ROI model por caso")
    models = data.get("models") or []
    total_low = 0
    total_high = 0
    total_cost = 0
    for m in models:
        if not isinstance(m, dict):
            continue
        out.append(f"### {m.get('name') or '—'}")
        out.append(f"- **Compliance tag:** {m.get('compliance_tag') or 'nenhuma'}")
        a = m.get("assumptions") or {}
        if isinstance(a, dict):
            out.append("- **Assumptions:**")
            for k, v in a.items():
                out.append(f"  - {k}: {v}")
        cost = int(m.get("year_1_cost_brl") or 0)
        low = int(m.get("year_1_savings_brl_low") or 0)
        high = int(m.get("year_1_savings_brl_high") or 0)
        total_low += low
        total_high += high
        total_cost += cost
        out.append(f"- **Year-1 cost:** R$ {_brl(cost)}")
        out.append(
            f"- **Year-1 savings (banda):** R$ {_brl(low)} – R$ {_brl(high)}"
        )
        out.append(f"- **Payback:** {m.get('payback_months') or '—'} meses")
        out.append("")
    out.append("## Total agregado")
    out.append(f"- **Cost y1:** R$ {_brl(total_cost)}")
    out.append(
        f"- **Savings y1 (banda):** R$ {_brl(total_low)} – R$ {_brl(total_high)}"
    )
    return "\n".join(out)


def _top_short_list_for_email(scored: dict, n: int = 8) -> List[str]:
    """Return up to ``n`` short-list strings with compliance tag + score."""
    short = scored.get("short_list") or []
    cases_idx: Dict[str, dict] = {}
    for c in scored.get("scored_cases") or []:
        if isinstance(c, dict) and c.get("name"):
            cases_idx[c["name"]] = c
    out: List[str] = []
    for name in short[:n]:
        c = cases_idx.get(name)
        if not c:
            out.append(f"<strong>{name}</strong>")
            continue
        tag = c.get("compliance_tag") or "—"
        score = c.get("combined_score") or "—"
        bucket = c.get("priority_bucket") or "—"
        out.append(
            f"<strong>{name}</strong> "
            f"<span style=\"color:#475569;\">— score {score}/100 · "
            f"{bucket} · compliance: {tag}</span>"
        )
    return out


def _count_kill_bucket(scored: dict) -> int:
    return sum(
        1 for c in scored.get("scored_cases") or []
        if isinstance(c, dict) and c.get("priority_bucket") == "kill"
    )


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
      1. Patch engagement: status='kickoff', total_phases=3, current_phase=1.
      2. Email the lead the intake form link.
      3. Schedule ``ai_phase_1_discovery`` on the lead 1 day out.
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
        "total_phases": 3,
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
            f"{BASE_URL}/api/delivery/ai_readiness/intake"
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
                subject="AI Readiness Sprint começou — primeiro passo (intake)",
                html=html,
                kind="ai_readiness_kickoff",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "ai_readiness.kickoff: email send failed eng=%s", engagement_id
            )

    next_at = _now() + timedelta(days=1)
    if lead and lead.get("id"):
        await session_set_next(
            str(lead["id"]),
            next_action="ai_phase_1_discovery",
            next_action_at=next_at,
        )
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.ai_readiness",
            action="ai_kickoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} kickoff; intake email sent; "
                f"phase 1 scheduled at {next_at.isoformat()}"
            ),
        )

    company = (lead or {}).get("company") or "—"
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":rocket: *AI Readiness Sprint kickoff* — engagement `{engagement_id}` "
        f"({company}) · R$ {value_str} · 3 semanas. "
        f"Intake enviado pra {email or 'n/a'}."
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "next_action_at": next_at,
    }


async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute phase N of the AI Readiness Sprint. Idempotent."""
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    current = int(engagement.get("current_phase") or 1)

    if phase < current:
        log.info(
            "ai_readiness.run_phase: skipping phase %s, current=%s eng=%s",
            phase, current, engagement_id,
        )
        return {"ok": True, "skipped": True, "current_phase": current}

    if phase == 1:
        return await _run_phase_1(engagement)
    if phase == 2:
        return await _run_phase_2(engagement)
    if phase == 3:
        return await _run_phase_3(engagement)

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
    long_list = artifacts.get("phase_1_long_list") or {}
    scored = artifacts.get("phase_2_scored_inventory") or {}
    roi = artifacts.get("phase_2_roi_model") or {}
    roadmap_md = artifacts.get("phase_3_roadmap_md") or ""

    if deliverable_type == "long_list":
        if not long_list:
            long_list = await _compose_long_list(
                engagement, engagement.get("intake_data") or {}
            )
        body_md = _long_list_to_markdown(long_list)
        url = await _render_and_upload(
            engagement_id,
            title="Long list — casos candidatos de IA",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/long_list.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_long_list": long_list,
                "long_list_md": body_md,
                "long_list_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "scored_inventory":
        if not long_list:
            long_list = await _compose_long_list(
                engagement, engagement.get("intake_data") or {}
            )
        if not scored:
            scored = await _compose_scored_inventory(engagement, long_list)
        body_md = _scored_to_markdown(scored)
        url = await _render_and_upload(
            engagement_id,
            title="Scored inventory — IA Readiness",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=body_md,
            object_path=f"{engagement_id}/scored_inventory.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_long_list": long_list,
                "phase_2_scored_inventory": scored,
                "scored_inventory_md": body_md,
                "scored_inventory_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "roi_model":
        if not long_list:
            long_list = await _compose_long_list(
                engagement, engagement.get("intake_data") or {}
            )
        if not scored:
            scored = await _compose_scored_inventory(engagement, long_list)
        if not roi:
            roi = await _compose_roi_model(engagement, scored)
        body_md = _roi_to_markdown(roi)
        url = await _render_and_upload(
            engagement_id,
            title="ROI model — short list",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=body_md,
            object_path=f"{engagement_id}/roi_model.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_roi_model": roi,
                "roi_model_md": body_md,
                "roi_model_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "roadmap_12mo":
        if not scored:
            scored = artifacts.get("phase_2_scored_inventory") or {}
        if not roi:
            roi = artifacts.get("phase_2_roi_model") or {}
        roadmap_md = await _compose_roadmap_12mo(engagement, scored, roi)
        url = await _render_and_upload(
            engagement_id,
            title="Roadmap IA — 12 meses",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=roadmap_md,
            object_path=f"{engagement_id}/roadmap_12mo.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_roadmap_md": roadmap_md,
                "roadmap_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "executive_deck":
        if not scored:
            scored = artifacts.get("phase_2_scored_inventory") or {}
        if not roi:
            roi = artifacts.get("phase_2_roi_model") or {}
        deck_md = await _compose_executive_deck(engagement, scored, roi)
        url = await _render_and_upload(
            engagement_id,
            title="Apresentação Executiva — AI Readiness",
            subtitle=f"Engagement {engagement_id} · Semana 3",
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
        if not long_list:
            long_list = artifacts.get("phase_1_long_list") or {}
        if not scored:
            scored = artifacts.get("phase_2_scored_inventory") or {}
        if not roi:
            roi = artifacts.get("phase_2_roi_model") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_3_roadmap_md") or ""
        report_md = await _compose_final_executive_report(
            engagement, long_list, scored, roi, roadmap_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Relatório Executivo — AI Readiness Sprint",
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
    fields landed in ``intake_data`` AND a sentinel timestamp is set.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        return False
    if intake.get("submitted_at"):
        return True
    required = (
        "executive_sponsor_email",
        "stakeholders",
        "past_pocs",
        "data_assets",
        "compliance_constraints",
        "annual_ai_budget_brl",
        "internal_ai_capability",
    )
    filled = sum(1 for k in required if intake.get(k))
    return filled >= 4


async def _run_phase_1(engagement: dict) -> dict:
    """Phase 1 — wait for intake submission, compose long list, advance."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    if _intake_submitted(engagement):
        # Compose long list and ship deliverable + email.
        intake = engagement.get("intake_data") or {}
        if not isinstance(intake, dict):
            intake = {}
        long_list = await _compose_long_list(engagement, intake)
        long_list_md = _long_list_to_markdown(long_list)

        long_list_url = await _render_and_upload(
            engagement_id,
            title="Long list — casos candidatos de IA",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=long_list_md,
            object_path=f"{engagement_id}/long_list.pdf",
        )

        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_long_list": long_list,
                "long_list_md": long_list_md,
                "long_list_url": long_list_url,
            },
        )

        if email:
            html = _phase1_email_html(
                first_name=first_name,
                longlist_url=long_list_url,
                n_cases=len(long_list.get("cases") or []),
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject="Long list pronta — Semana 1 AI Readiness",
                    html=html,
                    kind="ai_readiness_phase_1_long_list",
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "ai_readiness.phase_1: email failed eng=%s", engagement_id
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
            "next_action": "ai_phase_2_scoring",
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
            f"{BASE_URL}/api/delivery/ai_readiness/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _intake_reminder_email_html(
            first_name=first_name, intake_url=intake_url
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Intake pendente — AI Readiness Sprint",
                html=html,
                kind="ai_readiness_intake_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"intake_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":hourglass: AI Readiness engagement `{engagement_id}` — "
                f"intake pendente há {elapsed.days} dias. Lembrete enviado pra "
                f"{email}."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "ai_readiness.phase_1: reminder send failed eng=%s",
                engagement_id,
            )

    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "intake_submission",
        "next_action": "ai_phase_1_discovery",
        "next_action_at": next_at,
    }


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — Claude scores long list + builds ROI model. Ship PDFs + email."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    long_list = artifacts.get("phase_1_long_list") or {}
    if not long_list:
        long_list = await _compose_long_list(
            engagement, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_1_long_list": long_list}
        )

    scored = await _compose_scored_inventory(engagement, long_list)
    scored_md = _scored_to_markdown(scored)

    roi = await _compose_roi_model(engagement, scored)
    roi_md = _roi_to_markdown(roi)

    scored_url = await _render_and_upload(
        engagement_id,
        title="Scored inventory — IA Readiness",
        subtitle=f"Engagement {engagement_id} · Semana 2",
        body_md=scored_md,
        object_path=f"{engagement_id}/scored_inventory.pdf",
    )
    roi_url = await _render_and_upload(
        engagement_id,
        title="ROI model — short list",
        subtitle=f"Engagement {engagement_id} · Semana 2",
        body_md=roi_md,
        object_path=f"{engagement_id}/roi_model.pdf",
    )

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_2_scored_inventory": scored,
            "scored_inventory_md": scored_md,
            "scored_inventory_url": scored_url,
            "phase_2_roi_model": roi,
            "roi_model_md": roi_md,
            "roi_model_url": roi_url,
        },
    )

    if email:
        short = _top_short_list_for_email(scored)
        html = _phase2_email_html(
            first_name=first_name,
            scored_url=scored_url,
            roi_url=roi_url,
            short_list=short,
            short_list_count=len(scored.get("short_list") or []),
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="Scoring + ROI prontos — Semana 2 AI Readiness",
                html=html,
                kind="ai_readiness_phase_2_scoring",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "ai_readiness.phase_2: email failed eng=%s", engagement_id
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
        "next_action": "ai_phase_3_roadmap",
        "next_action_at": next_at,
    }


async def _run_phase_3(engagement: dict) -> dict:
    """Phase 3 — compose roadmap + deck + final report. Close engagement."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    long_list = artifacts.get("phase_1_long_list") or {}
    scored = artifacts.get("phase_2_scored_inventory") or {}
    roi = artifacts.get("phase_2_roi_model") or {}

    # Backfill if a previous phase silently failed.
    if not scored:
        scored = await _compose_scored_inventory(engagement, long_list)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_2_scored_inventory": scored}
        )
    if not roi:
        roi = await _compose_roi_model(engagement, scored)
        await _engagement_merge_artifacts(
            engagement_id, {"phase_2_roi_model": roi}
        )

    roadmap_md = await _compose_roadmap_12mo(engagement, scored, roi)
    deck_md = await _compose_executive_deck(engagement, scored, roi)
    report_md = await _compose_final_executive_report(
        engagement, long_list, scored, roi, roadmap_md
    )

    roadmap_url = await _render_and_upload(
        engagement_id,
        title="Roadmap IA — 12 meses",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/roadmap_12mo.pdf",
    )
    deck_url = await _render_and_upload(
        engagement_id,
        title="Apresentação Executiva — AI Readiness",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
    )
    report_url = await _render_and_upload(
        engagement_id,
        title="Relatório Executivo — AI Readiness Sprint",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
    )

    n_kill = _count_kill_bucket(scored)

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_3_roadmap_md": roadmap_md,
            "roadmap_url": roadmap_url,
            "deck_md": deck_md,
            "deck_url": deck_url,
            "final_report_md": report_md,
            "final_report_url": report_url,
            "discontinuation_count": n_kill,
        },
    )

    nps_url = (
        f"{BASE_URL}/api/delivery/ai_readiness/nps"
        f"?engagement_id={engagement_id}&token={_hmac_token(engagement_id, 'nps')}"
    )
    if email:
        html = _phase3_email_html(
            first_name=first_name,
            report_url=report_url,
            deck_url=deck_url,
            roadmap_url=roadmap_url,
            nps_url=nps_url,
            n_kill=n_kill,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject="AI Readiness Sprint entregue — relatório + roadmap + deck",
                html=html,
                kind="ai_readiness_phase_3_delivery",
                cc=[RESEND_REPLY_TO_EMAIL] if RESEND_REPLY_TO_EMAIL else None,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "ai_readiness.phase_3: email failed eng=%s", engagement_id
            )

    contract_id = engagement.get("contract_id")
    invoice_result: dict = {"ok": False, "reason": "not_attempted"}
    if contract_id:
        invoice_result = await _trigger_invoice(str(contract_id), engagement_id)

    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 3,
            "status": "delivered",
            "delivered_at": _now_iso(),
            "next_phase_at": None,
        },
    )

    short_list_count = len(scored.get("short_list") or [])
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":white_check_mark: *AI Readiness Sprint delivered* — engagement "
        f"`{engagement_id}`. Valor total R$ {value_str}. "
        f"Short list: {short_list_count} casos · Descontinuações: {n_kill}. "
        f"Próximo: invoice ({invoice_result.get('status') or 'pending'}) + "
        f"NPS. cc {SLACK_MILA_HANDLE}"
    )

    if lead and lead.get("id"):
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.ai_readiness",
            action="ai_phase_3_roadmap",
            result="ok",
            detail=(
                f"engagement {engagement_id} delivered; "
                f"short_list={short_list_count}; kill={n_kill}; "
                f"invoice {invoice_result.get('status')}"
            ),
        )
        for kind, url in (
            ("final_report", report_url),
            ("roadmap_12mo", roadmap_url),
            ("executive_deck", deck_url),
        ):
            try:
                await session_append_artifact(
                    str(lead["id"]),
                    type=kind,
                    url=url,
                    meta={
                        "engagement_id": engagement_id,
                        "phase": 3,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "ai_readiness.phase_3: artifact append failed lead=%s kind=%s",
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
            "ai_readiness: lib.contract.issue_invoice unavailable — stub "
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
            "ai_readiness: issue_invoice failed contract=%s", contract_id
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
    engagement on this lead (filtered to practice='ai' to avoid
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
        f"&practice=eq.ai"
        f"&status=in.(kickoff,running)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception("ai_readiness: resolve_engagement_id query failed")
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


@register("ai_kickoff")
async def h_ai_kickoff(lead: dict) -> dict:
    """Entry-point handler — fires once after contract.payment_webhook."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_kickoff: no active engagement found",
        }
    engagement = await _engagement_get(engagement_id)
    intake = (engagement or {}).get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    result = await kickoff(engagement_id, intake)
    return {
        "next_action": "ai_phase_1_discovery",
        "next_action_at": result.get("next_action_at") or (_now() + timedelta(days=1)),
        "status": "delivery_running",
        "detail": (
            f"ai_readiness kickoff ok; engagement {engagement_id}; "
            f"intake email sent"
        ),
    }


@register("ai_phase_1_discovery")
async def h_ai_phase_1(lead: dict) -> dict:
    """Phase 1 handler — workshop intake + long list composition."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_phase_1: no active engagement",
        }
    result = await run_phase(engagement_id, 1)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running" if not result.get("delivered") else "won",
        "detail": (
            f"ai_readiness phase 1: "
            f"{'advanced→2' if result.get('advanced_to_phase') else 'waiting intake'}"
        ),
    }


@register("ai_phase_2_scoring")
async def h_ai_phase_2(lead: dict) -> dict:
    """Phase 2 handler — scoring + ROI model."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_phase_2: no active engagement",
        }
    result = await run_phase(engagement_id, 2)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            f"ai_readiness phase 2: scoring + ROI shipped for "
            f"engagement {engagement_id}"
        ),
    }


@register("ai_phase_3_roadmap")
async def h_ai_phase_3(lead: dict) -> dict:
    """Phase 3 handler — roadmap + deck + final report + invoice + close."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_phase_3: no active engagement",
        }
    result = await run_phase(engagement_id, 3)
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "won" if result.get("delivered") else "delivery_running",
        "detail": (
            f"ai_readiness phase 3: "
            f"{'delivered' if result.get('delivered') else 'in progress'}"
            f"; invoice={result.get('invoice', {}).get('status')}"
        ),
    }


@register("ai_send_progress_update")
async def h_ai_progress_update(lead: dict) -> dict:
    """Mid-phase nudge — re-runs whichever phase the engagement is on, then
    optionally emails a progress update if the client has been silent."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_progress: no active engagement",
        }
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "ai_progress: engagement disappeared",
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
            1: "Discovery",
            2: "Scoring & Filtering",
            3: "Roadmap & Decisions",
        }.get(phase, f"Fase {phase}")
        summary = {
            1: (
                "Workshop e 1:1s rodando, data inventory sendo consolidado. "
                "Long list sai ao final desta semana."
            ),
            2: (
                "Scoring nas 15 dimensões em andamento. Pesos dobrados em "
                "compliance burden e risco regulatório. ROI model com "
                "assumptions explícitas em construção."
            ),
            3: (
                "Roadmap 12 meses sendo sequenciado por (ROI × confidence) / "
                "effort. Gates por caso (discovery → PoV → produção) com "
                "critério de saída em definição. Deck e relatório final em "
                "composição."
            ),
        }.get(phase, "Sprint em andamento — sem update específico para esta fase.")

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
                    subject=f"Update — {phase_label} (AI Readiness Sprint)",
                    html=html,
                    kind=f"ai_readiness_progress_phase_{phase}",
                )
                await _engagement_merge_artifacts(
                    engagement_id, {seen_key: _now_iso()}
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "ai_readiness.progress: email failed eng=%s phase=%s",
                    engagement_id, phase,
                )

    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": f"ai_readiness progress update: re-ran phase {phase}",
    }


# Alias — the contract module emits ``engagement_kickoff_ai`` for the
# ``ai`` practice (see lib/contract.py::_kickoff_engagement). We register
# the same handler under that key so the orchestrator dispatch lands here
# directly without an intermediate translation.
HANDLER_ALIAS = "engagement_kickoff_ai"


@register(HANDLER_ALIAS)
async def h_engagement_kickoff_ai(lead: dict) -> dict:
    """Alias for ``ai_kickoff`` — wired so contract.py's emitted action
    string lands on the right handler without a string remap."""
    return await h_ai_kickoff(lead)
