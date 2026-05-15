"""FinOps Audit — autonomous delivery agent.

Owns the post-signature delivery flow for the ``cloud_finops`` practice
(R$ 45-60k, 4 weeks). Hands off from ``lib.contract`` once a contract is
signed and paid, then runs four weekly phases — Discovery, Analysis,
Quick Wins and Roadmap — producing client deliverables and emails along
the way.

Architecture:

    contract.webhook (paid)
        |
        v
    finops_kickoff                           [+0]
        |   (intake form sent, awaiting client data)
        v
    finops_phase_1_data_collection           [+1 day]
        |   (intake submitted → analysis composed)
        v
    finops_phase_2_analysis                  [+1 week]
        |   (findings PDF delivered)
        v
    finops_phase_3_quickwins                 [+1 week]
        |   (change-log plan + sign-off request)
        v
    finops_phase_4_roadmap                   [+1 week]
        |   (final report + deck + invoice trigger)
        v
    status='delivered', next_action=None

Quality bar (mirrors lib/track_b.py and lib/contract.py):
  * All writes are append-only or idempotent. Each handler checks
    ``engagement.current_phase`` before mutating state, so re-runs and
    out-of-order ticks cannot corrupt the timeline.
  * Network failures bubble up so the orchestrator can retry with
    exponential backoff (2 attempts in addition to the first).
  * Graceful degradation:
      - no ANTHROPIC_API_KEY → deliverables fall back to a templated
        narrative tagged ``[CLAUDE_UNAVAILABLE_DRAFT]``.
      - no RESEND_API_KEY → email payloads are stashed in
        ``engagement.artifacts.email_drafts`` and a Slack alert fires.
      - no Supabase Storage credentials → artifacts are persisted as
        inline HTML/markdown blobs on the engagement row instead of
        public URLs.
  * Brand voice is enforced in every Claude prompt (dry, numbers-first,
    anti-hype). See ``_BRAND_SYSTEM_PROMPT``.
  * HMAC-tokened client links use ``CONTRACT_HMAC_SECRET`` so a single
    secret drives sign + intake + approval flows.
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

log = logging.getLogger("anuvia-lp.delivery.finops")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

#: Default ticket size for this practice. Used in Slack pings + the final
#: hand-off message when an explicit value is not stored on the engagement.
PRACTICE_TICKET_BRL: int = 52500  # midpoint of R$ 45-60k band

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

# Public host for client-facing links (intake form, approval buttons). The
# contract module already exposes the same env var so the brand domain is
# consistent across the funnel.
BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("CONTRACT_HOST", "https://anuvia.com.br"),
).rstrip("/")

# HMAC secret — shared with contract.py so a single token drives every
# client-facing link in the funnel.
_HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)

# Supabase Storage bucket where rendered PDFs land. Bucket itself is
# provisioned out-of-band; we just write into it.
SUPA_STORAGE_BUCKET = os.environ.get(
    "ANUVIA_DELIVERABLES_BUCKET", "anuvia-deliverables"
)

# Mila's Slack ID for DM-style alerts. Falls back to the channel webhook
# resolved inside ``_send_slack_alert`` when unset.
SLACK_MILA_HANDLE = os.environ.get("SLACK_MILA_HANDLE", "@mila")

# How long each phase nominally runs, and how long we wait before nudging
# a silent client during phase 1 / 3.
_PHASE_INTERVAL = timedelta(days=7)
_INTAKE_REMINDER_AFTER = timedelta(days=5)
_APPROVAL_REMINDER_AFTER = timedelta(days=5)

# Short timeout for HTTP calls; the orchestrator wraps us in retries.
_HTTP_TIMEOUT = 30.0

# Brand voice — pinned to every Claude system prompt in this module. Sourced
# from SPRINT_INPUTS_MILA.md section 1.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA, ex-AWS Solutions Architect, ex-Google, "
    "ex-MongoDB). Voz: seca, direta, anti-hype, primeiro os números, depois a "
    "narrativa. Frases curtas declarativas misturadas com cadeias causa-efeito "
    "mais longas. Léxico que usa: vazamento, clareza, diagnóstico, processo, "
    "padrão, sobreviver em produção. Léxico que evita: sinergia, transformação, "
    "leverage, magia, mágico, IA generativa que muda o jogo.\n\n"
    "REGRAS DE PROFUNDIDADE TÉCNICA (não negociáveis):\n"
    "1. Cite instance types específicos (db.m5.2xlarge, m7g.xlarge, t4g.large). "
    "Nunca diga genericamente 'instâncias'.\n"
    "2. Cite serviços AWS exatos com seu nome de produto (AWS Compute Optimizer, "
    "Cost Explorer, Trusted Advisor, S3 Intelligent-Tiering, Aurora I/O-Optimized).\n"
    "3. Cite métricas CloudWatch concretas (CPUUtilization, VolumeReadOps, "
    "DBIOPS) e thresholds reais (CPU <15% por 14d).\n"
    "4. Cite comandos AWS CLI/API quando relevante (modify-db-instance, "
    "AbortIncompleteMultipartUpload, AbortIncompleteMultipartUpload lifecycle rule).\n"
    "5. Use números DO INTAKE do cliente sempre que possível. Se intake diz "
    "R$ 95k/mês AWS spend, todos os números derivam disso, não de fantasia.\n"
    "6. Math explícita: 'gp2 ~US$ 0,10/GB/mês vs gp3 ~US$ 0,08/GB/mês × 8TB × "
    "12 = US$ 1.920/ano = R$ 9.600/ano (USD/BRL 5,0)'. Mostre a conta.\n"
    "7. Quando estimar, use 'estimativa' uma vez só. NÃO repita 'padrão setorial' "
    "como muleta — isso é tique de junior. Diga o número, justifique com a math.\n"
    "8. Para CADA finding, inclua: validation criteria (como confirmar), rollback "
    "plan (como reverter), janela de execução (quando).\n"
    "9. ADRs em formato ADR-XX: ADR-01 (RI 1-year vs 3-year), ADR-02 (Graviton "
    "blue/green migration), etc.\n"
    "10. Nunca prometa o que não pode ser medido. Português do Brasil."
)

#: Sentinel prefix for narrative that Claude could not generate (env var
#: missing or upstream error). Lets the human reviewer find drafts quickly.
_CLAUDE_FALLBACK_TAG = "[CLAUDE_UNAVAILABLE_DRAFT]"


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
    """Format a numeric as Brazilian currency: 45.000,00."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0,00"
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hmac_token(engagement_id: str, purpose: str = "intake") -> str:
    """HMAC-SHA256 token for a client-facing link.

    The `purpose` keeps intake / approval / NPS links from being
    interchangeable — a leaked intake link cannot approve a change log.
    """
    if not _HMAC_SECRET:
        log.warning(
            "finops: HMAC secret unset; client links will be unverifiable"
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
        log.exception("finops: engagement_get network failed id=%s", engagement_id)
        return None
    if r.status_code != 200:
        log.warning(
            "finops: engagement_get non-200 id=%s: %s %s",
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
            "finops: engagement_patch network failed id=%s", engagement_id
        )
        return False
    if r.status_code not in (200, 204):
        log.warning(
            "finops: engagement_patch non-2xx id=%s: %s %s",
            engagement_id, r.status_code, r.text[:200],
        )
        return False
    return True


async def _engagement_merge_artifacts(
    engagement_id: str, additions: dict
) -> bool:
    """Merge ``additions`` into ``engagement.artifacts`` (a jsonb object).

    Read-modify-write. Top-level keys in ``additions`` overwrite existing
    keys with the same name — phase-keyed payloads ("phase_2_findings",
    "change_log", etc.) are expected to be replaced on a re-run.
    """
    row = await _engagement_get(engagement_id)
    if not row:
        log.warning(
            "finops: merge_artifacts: engagement %s not found",
            engagement_id,
        )
        return False
    current = row.get("artifacts") or {}
    if not isinstance(current, dict):
        # Some rows store artifacts as a jsonb array (matches the leads
        # schema). Coerce to a dict-with-history under "_legacy" so we
        # don't lose data.
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
# Supabase Storage upload (PDF / markdown deliverables)
# ---------------------------------------------------------------------------


async def _upload_artifact(
    path: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Optional[str]:
    """Upload ``content`` to the ``anuvia-deliverables`` bucket. Returns the
    public URL or ``None`` if storage is unavailable.

    Best-effort. Storage outages must NOT crash a delivery handler — when
    upload fails we still attach the artifact inline as base64 / text on
    the engagement so the deliverable is recoverable manually.
    """
    if not SUPA_URL or not SUPA_HEADERS.get("apikey"):
        return None

    # Storage endpoint sits at /storage/v1, not /rest/v1. Most Supabase
    # deployments expose both behind the same host.
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
        log.warning("finops: storage upload failed path=%s: %s", path, exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "finops: storage upload non-2xx path=%s status=%s body=%s",
            path, r.status_code, r.text[:200],
        )
        return None
    return (
        f"{base}/storage/v1/object/public/"
        f"{SUPA_STORAGE_BUCKET}/{path.lstrip('/')}"
    )


# ---------------------------------------------------------------------------
# HTML → PDF (delegates to lib.contract.gotenberg helper if available)
# ---------------------------------------------------------------------------


async def _html_to_pdf(html: str) -> Optional[bytes]:
    """Render ``html`` to PDF bytes via Gotenberg.

    We re-use the Gotenberg URL from the environment but write our own
    in-memory call so we never block on the contract module's file-system
    side effects. Returns ``None`` if Gotenberg is unreachable.
    """
    gotenberg = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
    endpoint = f"{gotenberg}/forms/chromium/convert/html"
    try:
        files = {"files": ("index.html", html.encode("utf-8"), "text/html")}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(endpoint, files=files)
    except Exception as exc:  # noqa: BLE001
        log.warning("finops: gotenberg call failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning(
            "finops: gotenberg non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return None
    return r.content


# ---------------------------------------------------------------------------
# Claude wrapper — single source of truth for the brand voice
# ---------------------------------------------------------------------------


async def _call_claude(
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
            log.warning("finops: anthropic %s", last_err)
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
                "finops: anthropic retryable %s body=%s",
                last_err, r.text[:300],
            )
        else:
            # Non-retryable error (400/401/403)
            log.warning(
                "finops: anthropic non-retryable status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return f"{_CLAUDE_FALLBACK_TAG} (status {r.status_code})"

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    return f"{_CLAUDE_FALLBACK_TAG} ({last_err})"


# ---------------------------------------------------------------------------
# Email send (Resend) — graceful degradation to engagement.artifacts.email_drafts
# ---------------------------------------------------------------------------


async def _send_email(
    *,
    engagement_id: str,
    to: str,
    subject: str,
    html: str,
    kind: str,
    cc: Optional[List[str]] = None,
) -> Optional[str]:
    """Send an email via Resend. On dry-run / failure, stash the draft.

    Returns the Resend message id on success, ``None`` otherwise. Failures
    are stored as ``email_drafts`` under ``engagement.artifacts`` so the
    operator can flush them manually. A Slack alert fires when Resend is
    misconfigured so the human knows.
    """
    if not RESEND_API_KEY:
        log.info(
            "finops: RESEND_API_KEY unset; stashing draft kind=%s eng=%s",
            kind, engagement_id,
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        await _send_slack_alert(
            f":warning: FinOps delivery: RESEND_API_KEY missing — "
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
            {"name": "category", "value": "delivery_finops"},
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
        log.exception("finops: resend network failed kind=%s", kind)
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend network: {exc}")

    if r.status_code >= 400:
        log.error(
            "finops: resend non-2xx kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info(
        "finops: resend ok kind=%s eng=%s msg_id=%s",
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
    """Append an undeliverable email to ``engagement.artifacts.email_drafts``.

    Best-effort: failures here are logged but never raised — we don't want
    the email-send failure to cascade into a Supabase write failure.
    """
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
            "finops: stash_email_draft failed eng=%s kind=%s",
            engagement_id, kind,
        )


# ---------------------------------------------------------------------------
# Email HTML templates — kept compact, inline-styled, brand-consistent
# ---------------------------------------------------------------------------


def _wrap_email(title: str, body_html: str) -> str:
    """Wrap a body fragment in the standard Anuvia email shell."""
    return f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · FinOps Audit</p>
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
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Contrato fechado, FinOps Audit começa agora. Investimento total: <strong>R$ {value_str}</strong>. Cronograma: 4 semanas, com entregáveis escritos no fim de cada uma.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Semana 1 — Discovery &amp; Data Collection.</strong> Primeira coisa que preciso: as informações listadas no formulário abaixo. Sem isso, a análise da semana 2 não roda.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>AWS spend dos últimos 6 meses (CSV do CUR ou self-report)</li>
  <li>Quantidade de accounts e estrutura de Organizations</li>
  <li>Serviços primários em uso</li>
  <li>Estratégia atual de tagging</li>
  <li>Maiores preocupações de custo que vocês já mapearam</li>
  <li>Nome e email do sponsor executivo</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Em paralelo, vou pedir um IAM role read-only pra rodar queries direto no CUR. O detalhamento técnico vai junto no formulário.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Padrão das 14 últimas auditorias: 27% do bill mensal sai por 4 mesmos canos. Vamos achar os de vocês.</p>
"""
    return _wrap_email("FinOps Audit começou", body)


def _phase2_email_html(
    *, first_name: str, pdf_url: str, top_findings: List[str]
) -> str:
    bullets = "".join(
        f'<li style="margin:6px 0;line-height:1.55;">{f}</li>'
        for f in top_findings[:5]
    )
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 2 fechada. Rodei a análise nos 8 vetores (compute, storage, network, data transfer, RDS, S3, SaaS de terceiros, support tier). Findings priorizados por impacto × esforço × risco.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Top 5 do que encontramos:</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 18px 18px;padding:0;">{bullets}</ul>
<p style="margin:24px 0;"><a href="{pdf_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Findings completos (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Próximo passo: na semana 3 implementamos os quick wins (high impact, low risk) com aprovação em cada step. Já te mando o plano de mudanças pra sign-off.</p>
"""
    return _wrap_email("Findings da semana 2", body)


def _phase3_email_html(
    *, first_name: str, changelog_url: str, approval_url: str, savings_brl: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 3 — plano de quick wins prontos. Economia anualizada estimada: <strong>R$ {savings_brl}</strong>. Cada mudança vem com rollback documentado.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Antes de executar, preciso do seu sign-off explícito por mudança. O change log completo (com timestamp, ticket, aprovador, rollback procedure) está no link abaixo.</p>
<p style="margin:24px 0;"><a href="{changelog_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Change log (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;">Quando estiver pronto pra eu executar, é um clique:</p>
<p style="margin:8px 0 24px;"><a href="{approval_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Aprovar quick wins &rarr;</a></p>
<p style="color:#78716c;line-height:1.55;font-size:13px;margin:0 0 14px;">Sem aprovação não toco em nada em produção. Se quiser ajustar escopo (tirar item, adicionar contexto), só responder este email.</p>
"""
    return _wrap_email("Quick wins prontos pra aprovação", body)


def _phase4_email_html(
    *,
    first_name: str,
    report_url: str,
    deck_url: str,
    roadmap_url: str,
    savings_brl: str,
    nps_url: str,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Auditoria concluída. Quatro semanas, três entregáveis principais:</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo</a> — 12 páginas, baseline + findings + savings realizadas + ADRs.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva</a> — pra rodar com C-level e board.</li>
  <li><a href="{roadmap_url}" style="color:#0f172a;">Roadmap 12 meses</a> — médio prazo (RI/SP, Graviton, S3 tiering) e alto risco (re-arch).</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Economia anualizada identificada: <strong>R$ {savings_brl}</strong>. Sessão de handoff 2h fica agendada pelo email com a Mila.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">A invoice da segunda parcela já entrou na fila — ela cai no seu inbox separada.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Um pedido: 2 minutos pra deixar um NPS. Direto, sem firula:</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar NPS &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">E se conhecer outro CTO/Head Cloud com bill AWS &gt; R$ 80k/mês — você sabe quem precisa ouvir isso. Indicação direta vale mais que qualquer outbound nosso.</p>
"""
    return _wrap_email("FinOps Audit entregue", body)


def _intake_reminder_email_html(*, first_name: str, intake_url: str) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete curto: o formulário de intake ainda não foi preenchido. Sem ele, a análise da semana 2 não pode rodar e o cronograma desloca.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se tiver algum bloqueio (acesso AWS, sponsor não definido), me avisa. A gente resolve.</p>
"""
    return _wrap_email("Intake pendente — FinOps Audit", body)


def _approval_reminder_email_html(
    *, first_name: str, approval_url: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Os quick wins continuam aguardando aprovação. Cada dia sem executar é economia anualizada que fica na mesa.</p>
<p style="margin:8px 0 24px;"><a href="{approval_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Aprovar quick wins &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se quiser revisar item por item antes, dá pra agendar 30 min comigo. Só responder.</p>
"""
    return _wrap_email("Quick wins ainda aguardando sign-off", body)


# ---------------------------------------------------------------------------
# HTML shells for PDF deliverables
# ---------------------------------------------------------------------------


def _deliverable_html(title: str, subtitle: str, body_md_html: str) -> str:
    """A4-friendly inline-styled deliverable wrapper. Same look as contracts."""
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
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 6px;">Anuvia · FinOps Audit</p>
  <h1>{title}</h1>
  <p class="meta">{subtitle}</p>
</header>
{body_md_html}
<footer style="margin-top:32px;padding-top:18px;border-top:1px solid #e7e5e4;color:#64748b;font-size:11px;">
  Anuvia Cloud &amp; AI Consulting · Mila Vernazza · Documento gerado em {_now().strftime("%d/%m/%Y")}
</footer>
</body></html>"""


def _md_to_html(md: str) -> str:
    """Tiny Markdown-ish converter. Same shape as contract._md_to_simple_html."""
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
# Lead helper — pulled out of `leads` via session_get
# ---------------------------------------------------------------------------


async def _lead_for_engagement(
    engagement: dict,
) -> Tuple[Optional[dict], Optional[str], str]:
    """Return ``(lead_row, email, first_name)`` for an engagement.

    Tolerant of missing leads / partial rows — returns ``(None, None, '')``
    so callers can decide how to degrade.
    """
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
# Deliverable composition (Claude prompts + HTML/PDF render + storage upload)
# ---------------------------------------------------------------------------


_FINOPS_VECTORS: List[str] = [
    "Compute (EC2 right-sizing, RI/SP coverage, Spot, Graviton)",
    "Storage (EBS órfão, snapshots, S3 lifecycle, intelligent tiering)",
    "Network (NAT Gateway, VPC peering, cross-AZ, egress)",
    "Data transfer (S3→EC2, RDS replicas, ELB)",
    "RDS/Aurora (instance class fit, RI, gp3, I/O optimized)",
    "S3 (lifecycle gaps, Glacier, multipart uploads)",
    "Third-party SaaS (Marketplace, dev tools, security stack)",
    "Support tier (Business vs Enterprise ROI)",
]


async def _compose_findings_narrative(
    engagement: dict, intake_data: dict
) -> dict:
    """Ask Claude for the phase-2 findings, one per vector.

    Returns a dict shaped::

        {
            "summary": "<paragraph>",
            "findings": [
                {
                    "vector": "Compute",
                    "hypothesis": "...",
                    "savings_brl_low": int,
                    "savings_brl_high": int,
                    "effort": "low" | "med" | "high",
                    "risk": "low" | "med" | "high",
                    "priority": "quick_win" | "medium_term" | "structural",
                },
                ...
            ],
        }

    If Claude fails the function still returns a well-formed dict — the
    findings just get a ``[CLAUDE_UNAVAILABLE_DRAFT]`` placeholder so the
    operator can see what would have shipped.
    """
    profile_lines = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    vectors_block = "\n".join(f"{i+1}. {v}" for i, v in enumerate(_FINOPS_VECTORS))

    prompt = f"""Você está compondo a seção de findings da auditoria FinOps de um cliente.

Perfil do cliente (intake submetido):
{profile_block}

Para cada um dos 8 vetores abaixo, gere uma hipótese específica baseada no perfil. Quando não tiver dado suficiente, marque a hipótese como "estimativa baseada em padrões setoriais" e dimensione conservador. Sempre forneça intervalo de economia anualizada em R$, esforço (low/med/high), risco (low/med/high) e bucket de prioridade (quick_win | medium_term | structural).

Vetores:
{vectors_block}

Devolva APENAS um JSON válido com esta estrutura, sem markdown, sem comentários:

{{
  "summary": "<parágrafo de 3-5 linhas, voz Anuvia: seca, direta, numbers-first>",
  "findings": [
    {{
      "vector": "<nome curto, ex: Compute>",
      "hypothesis": "<2-3 frases>",
      "savings_brl_low": <int>,
      "savings_brl_high": <int>,
      "effort": "<low|med|high>",
      "risk": "<low|med|high>",
      "priority": "<quick_win|medium_term|structural>"
    }}
  ]
}}
"""

    raw = await _call_claude(prompt, max_tokens=3000)

    # Defensive parse — strip code fences if Claude added them despite the
    # explicit instruction, and tolerate trailing prose.
    text = raw.strip()
    if text.startswith(_CLAUDE_FALLBACK_TAG):
        return {
            "summary": text,
            "findings": [
                {
                    "vector": v.split(" ")[0],
                    "hypothesis": f"{_CLAUDE_FALLBACK_TAG} estimativa pendente",
                    "savings_brl_low": 0,
                    "savings_brl_high": 0,
                    "effort": "med",
                    "risk": "med",
                    "priority": "medium_term",
                }
                for v in _FINOPS_VECTORS
            ],
        }

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        # Drop everything after the closing fence if any leftover.
        if "```" in text:
            text = text.split("```", 1)[0]

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("top-level not object")
        if "findings" not in data or not isinstance(data["findings"], list):
            raise ValueError("missing findings array")
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("finops: claude returned non-JSON: %s", exc)
        return {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude.\n\n"
                f"{text[:1200]}"
            ),
            "findings": [
                {
                    "vector": v.split(" ")[0],
                    "hypothesis": (
                        f"{_CLAUDE_FALLBACK_TAG} revisar manualmente. "
                        f"Vetor: {v}"
                    ),
                    "savings_brl_low": 0,
                    "savings_brl_high": 0,
                    "effort": "med",
                    "risk": "med",
                    "priority": "medium_term",
                }
                for v in _FINOPS_VECTORS
            ],
        }


def _findings_to_markdown(data: dict) -> str:
    """Render the structured findings dict as a markdown document."""
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Findings por vetor")
    out.append("")
    findings = data.get("findings") or []
    total_low = 0
    total_high = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        vector = f.get("vector") or "—"
        out.append(f"### {vector}")
        out.append(f.get("hypothesis") or "—")
        low = int(f.get("savings_brl_low") or 0)
        high = int(f.get("savings_brl_high") or 0)
        total_low += low
        total_high += high
        out.append(
            f"- **Economia estimada (anualizada):** R$ {_brl(low)} – R$ {_brl(high)}"
        )
        out.append(f"- **Esforço:** {f.get('effort') or '—'}")
        out.append(f"- **Risco:** {f.get('risk') or '—'}")
        out.append(f"- **Prioridade:** {f.get('priority') or '—'}")
        out.append("")
    out.append("## Total estimado")
    out.append(
        f"- **Economia anualizada (faixa):** "
        f"R$ {_brl(total_low)} – R$ {_brl(total_high)}"
    )
    return "\n".join(out)


def _top_findings_for_email(data: dict, n: int = 5) -> List[str]:
    """Return up to ``n`` finding strings ordered by savings_brl_high desc."""
    findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
    findings.sort(
        key=lambda f: int(f.get("savings_brl_high") or 0), reverse=True
    )
    out: List[str] = []
    for f in findings[:n]:
        vec = f.get("vector") or "—"
        low = int(f.get("savings_brl_low") or 0)
        high = int(f.get("savings_brl_high") or 0)
        out.append(
            f"<strong>{vec}.</strong> R$ {_brl(low)}–{_brl(high)}/ano "
            f"(prioridade: {f.get('priority') or 'medium_term'})"
        )
    return out


def _findings_total_savings(data: dict) -> Tuple[int, int]:
    low = 0
    high = 0
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        low += int(f.get("savings_brl_low") or 0)
        high += int(f.get("savings_brl_high") or 0)
    return low, high


async def _compose_change_log_narrative(
    engagement: dict, findings: dict
) -> str:
    """Ask Claude for the phase-3 change log markdown."""
    quick_wins = [
        f
        for f in (findings.get("findings") or [])
        if isinstance(f, dict) and f.get("priority") == "quick_win"
    ]
    if not quick_wins:
        quick_wins = (findings.get("findings") or [])[:4]

    qw_block = "\n".join(
        f"- {f.get('vector')}: {f.get('hypothesis')}" for f in quick_wins
    )

    prompt = f"""Você está escrevendo o plano de mudanças (change log) da semana 3 de uma auditoria FinOps Anuvia. Esse documento vai para o cliente aprovar mudança por mudança ANTES de qualquer execução em produção.

Quick wins identificados na semana 2:
{qw_block}

Para cada quick win, escreva uma seção markdown com:
1. **Descrição** — o que será feito, em 2-3 frases.
2. **Critérios de validação** — bullets do que precisa ser verdade antes de aprovar.
3. **Plano de rollback** — o passo-a-passo se algo quebrar.
4. **Janela proposta** — quando executar (ex: "fora do horário comercial BRT", "próxima janela de manutenção").
5. **Economia anualizada esperada** — em R$.

Comece com uma seção "## Visão geral" curta (3-5 linhas) explicando o que é o documento e qual é a regra de aprovação. Termine com uma seção "## Próximos passos" explicando o que acontece após o sign-off."""

    return await _call_claude(prompt, max_tokens=3000)


async def _compose_final_report_narrative(
    engagement: dict, findings: dict, change_log_md: str
) -> str:
    """Ask Claude for the phase-4 12-page executive report markdown."""
    low, high = _findings_total_savings(findings)
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    profile_lines = [
        f"- {k}: {v}" for k, v in intake.items() if v not in (None, "", [])
    ]
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    findings_block = _findings_to_markdown(findings)

    prompt = f"""Você está escrevendo o relatório executivo final (12 páginas) de uma auditoria FinOps Anuvia.

Perfil do cliente:
{profile_block}

Findings da semana 2:
{findings_block}

Change log da semana 3 (resumo):
{change_log_md[:2500]}

Economia anualizada total identificada: R$ {_brl(low)} – R$ {_brl(high)}.

Estruture o documento markdown com estas seções, nesta ordem:

1. **## Sumário executivo** — 1 página: contexto, principais números (baseline mensal estimado, economia identificada, payback), decisão pedida.
2. **## Baseline de spend** — descrição do gasto atual por categoria. Quando faltar dado concreto, marque como "estimativa baseada em padrões setoriais".
3. **## Metodologia** — como rodamos a auditoria (8 vetores, CUR via Athena, Cost Explorer, Trusted Advisor, Compute Optimizer).
4. **## Findings detalhados** — uma subseção por vetor com hipótese, evidências esperadas, economia, esforço, risco, prioridade.
5. **## Savings realizadas (semana 3)** — o que foi executado no quick wins phase, números antes/depois.
6. **## Roadmap 12 meses** — 3 horizontes: 30 dias (quick wins residuais), 90 dias (RI/SP strategy, Graviton, S3 intelligent tiering), 180-365 dias (re-arch cross-AZ, multi-region, database migrations).
7. **## ADRs** — Architecture Decision Records para cada decisão estrutural (RI strategy, Graviton, observability cost).
8. **## Handoff checklist** — 16 itens revisados em toda auditoria Anuvia.
9. **## Apêndices** — queries SQL usadas (Athena), referências.

Voz Anuvia: seca, direta, numbers-first. Cada afirmação com número quando possível. Quando estimar, dizer "estimativa".
"""

    return await _call_claude(prompt, max_tokens=4000)


async def _compose_roadmap_narrative(engagement: dict, findings: dict) -> str:
    """Standalone 12-month roadmap markdown (separate from the report)."""
    findings_block = _findings_to_markdown(findings)
    prompt = f"""Escreva um roadmap de FinOps de 12 meses pra um cliente Anuvia, em markdown.

Findings da auditoria:
{findings_block}

Estrutura:

## Horizonte 1 — 30 dias
Os quick wins residuais e setup de governança (alertas de billing, tagging policy enforced, dashboards default). Tabela com item, dono sugerido, esforço (dias-pessoa), economia esperada.

## Horizonte 2 — 90 dias
Iniciativas de médio prazo: RI/SP strategy (target coverage 70-85%), Graviton migration por workload, S3 intelligent tiering rollout, observability cost optimization.

## Horizonte 3 — 180-365 dias
Iniciativas estruturais: re-arch cross-AZ, multi-region rationalization, database migration considerations, FinOps culture (FinOps Foundation framework adoption, monthly review cadence).

## Governança contínua
Cadência mensal de revisão (template incluso), métricas que importam (cost per workload, cost per request, RI/SP utilization, unit economics), thresholds que disparam alerta.

Voz Anuvia: seca, direta, numbers-first.
"""
    return await _call_claude(prompt, max_tokens=4000)


async def _compose_deck_narrative(engagement: dict, findings: dict) -> str:
    """Slide-by-slide markdown skeleton — converted later to PDF or pptx."""
    low, high = _findings_total_savings(findings)
    top = sorted(
        [f for f in findings.get("findings") or [] if isinstance(f, dict)],
        key=lambda f: int(f.get("savings_brl_high") or 0),
        reverse=True,
    )[:6]
    top_block = "\n".join(
        f"- {f.get('vector')}: R$ {_brl(f.get('savings_brl_high') or 0)}/ano "
        f"(prioridade: {f.get('priority')})"
        for f in top
    )

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (15-20 slides) pra um cliente Anuvia, fechando uma auditoria FinOps de 4 semanas.

Top 6 findings:
{top_block}

Economia anualizada total identificada: R$ {_brl(low)} – R$ {_brl(high)}.

Para cada slide, escreva:

### Slide N — <título>
- 3-5 bullets curtos (uma frase cada, sem ponto final)
- (notas: <fala de 30s do apresentador, opcional>)

Estrutura:
1. Slide 1 — capa: cliente, escopo, prazo, garantia 3× ROI.
2. Slide 2 — sumário (números headline: baseline, economia identificada, payback).
3. Slide 3-4 — metodologia (8 vetores, ferramentas).
4. Slide 5-12 — um slide por vetor com economia + evidência.
5. Slide 13 — savings realizadas (quick wins phase).
6. Slide 14-16 — roadmap 30/90/365 dias.
7. Slide 17 — ADRs principais.
8. Slide 18 — governança contínua (cadência mensal, métricas).
9. Slide 19 — handoff (próximos passos, ownership).
10. Slide 20 — encerramento + Anuvia retainer ongoing (CTA).

Voz Anuvia: seca, direta. Sem hype. Bullets curtos."""

    return await _call_claude(prompt, max_tokens=4500)


# ---------------------------------------------------------------------------
# Render + upload helper — turns a markdown blob into a hosted PDF URL
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

    Returns the public PDF URL when storage is available, otherwise an
    embedded ``data:`` placeholder URL pointing the operator at the
    stashed inline copy. Always succeeds — never raises.
    """
    html = _deliverable_html(title, subtitle, _md_to_html(body_md))

    pdf_bytes = await _html_to_pdf(html)
    if pdf_bytes is None:
        # Fall back to uploading the HTML so at least *something* hosts.
        public = await _upload_artifact(
            object_path.replace(".pdf", ".html"),
            html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
        if public:
            return public
        # No storage either — return a sentinel; the markdown is stashed
        # inline on the engagement via the caller.
        return f"about:blank#stashed-{engagement_id}-{object_path}"

    public = await _upload_artifact(
        object_path, pdf_bytes, content_type="application/pdf"
    )
    if public:
        return public
    # Upload failed → write the HTML next to it so the operator can grab
    # the deliverable manually from the engagement row.
    return f"about:blank#stashed-{engagement_id}-{object_path}"


# ---------------------------------------------------------------------------
# Public surface — kickoff, run_phase, generate_deliverable
# ---------------------------------------------------------------------------


async def kickoff(engagement_id: str, intake_data: dict) -> dict:
    """Called by ``lib.contract`` once a contract is signed + paid.

    Side effects:
      1. Patch engagement: status='kickoff', total_phases=4, current_phase=1.
      2. Email the lead the intake form link.
      3. Schedule ``finops_phase_1_data_collection`` on the lead 1 day out.
      4. Slack-ping Mila with the engagement summary.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    # Idempotency — re-runs don't reset the phase counter.
    already_kicked = (
        engagement.get("status") in ("kickoff", "running", "delivered")
        and engagement.get("current_phase")
    )

    patch = {
        "total_phases": 4,
        "current_phase": engagement.get("current_phase") or 1,
        "status": engagement.get("status") or "kickoff",
        "intake_data": {**(engagement.get("intake_data") or {}), **(intake_data or {})},
        "started_at": engagement.get("started_at") or _now_iso(),
        "next_phase_at": (
            _serialize(_now() + timedelta(days=1))
            if not already_kicked
            else engagement.get("next_phase_at")
        ),
    }
    await _engagement_patch(engagement_id, patch)

    lead, email, first_name = await _lead_for_engagement(engagement)

    # Email the intake form link.
    if email and not already_kicked:
        token = _hmac_token(engagement_id, "intake")
        intake_url = (
            f"{BASE_URL}/api/delivery/finops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
        html = _kickoff_email_html(
            first_name=first_name,
            intake_url=intake_url,
            value_str=value_str,
        )
        try:
            await _send_email(
                engagement_id=engagement_id,
                to=email,
                subject="FinOps Audit começou — primeiro passo (intake)",
                html=html,
                kind="finops_kickoff",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.kickoff: email send failed eng=%s", engagement_id
            )

    # Schedule the phase 1 handler on the lead.
    next_at = _now() + timedelta(days=1)
    if lead and lead.get("id"):
        await session_set_next(
            str(lead["id"]),
            next_action="finops_phase_1_data_collection",
            next_action_at=next_at,
        )
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.finops",
            action="finops_kickoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} kickoff; intake email sent; "
                f"phase 1 scheduled at {next_at.isoformat()}"
            ),
        )

    # Slack ping with the engagement summary.
    company = (lead or {}).get("company") or "—"
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":rocket: *FinOps Audit kickoff* — engagement `{engagement_id}` "
        f"({company}) · R$ {value_str} · 4 semanas. "
        f"Intake enviado pra {email or 'n/a'}."
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "next_action_at": next_at,
    }


async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute phase N of the FinOps Audit. Idempotent.

    Each phase advances ``current_phase`` and schedules the next phase on
    the lead, OR ends the engagement (phase 4). Returns a small status
    dict — the orchestrator-level scheduling is wired by the registered
    handlers below.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    current = int(engagement.get("current_phase") or 1)

    # Out-of-order tick — refuse to re-run a phase we already passed.
    if phase < current:
        log.info(
            "finops.run_phase: skipping phase %s, current=%s eng=%s",
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
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    findings = artifacts.get("phase_2_findings") or {}
    change_log_md = artifacts.get("phase_3_change_log_md") or ""

    if deliverable_type == "baseline_report":
        body_md = _baseline_report_md(engagement)
        url = await _render_and_upload(
            engagement_id,
            title="Baseline de Spend — FinOps Audit",
            subtitle=f"Engagement {engagement_id} · Semana 1",
            body_md=body_md,
            object_path=f"{engagement_id}/baseline_report.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "baseline_report_url": url,
                "baseline_report_md": body_md,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "findings_list":
        if not findings:
            findings = await _compose_findings_narrative(
                engagement, engagement.get("intake_data") or {}
            )
        body_md = _findings_to_markdown(findings)
        url = await _render_and_upload(
            engagement_id,
            title="Findings FinOps — análise por vetor",
            subtitle=f"Engagement {engagement_id} · Semana 2",
            body_md=body_md,
            object_path=f"{engagement_id}/findings_list.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_findings": findings,
                "findings_list_url": url,
                "findings_list_md": body_md,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "change_log":
        if not change_log_md:
            change_log_md = await _compose_change_log_narrative(
                engagement, findings or {}
            )
        url = await _render_and_upload(
            engagement_id,
            title="Change Log — Quick Wins",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=change_log_md,
            object_path=f"{engagement_id}/change_log.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_change_log_md": change_log_md,
                "change_log_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "final_executive_report":
        report_md = await _compose_final_report_narrative(
            engagement, findings or {}, change_log_md
        )
        url = await _render_and_upload(
            engagement_id,
            title="Relatório Executivo — FinOps Audit",
            subtitle=f"Engagement {engagement_id} · Semana 4",
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

    if deliverable_type == "roadmap_12mo":
        roadmap_md = await _compose_roadmap_narrative(
            engagement, findings or {}
        )
        url = await _render_and_upload(
            engagement_id,
            title="Roadmap FinOps — 12 meses",
            subtitle=f"Engagement {engagement_id} · Semana 4",
            body_md=roadmap_md,
            object_path=f"{engagement_id}/roadmap_12mo.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "roadmap_md": roadmap_md,
                "roadmap_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "executive_deck":
        deck_md = await _compose_deck_narrative(engagement, findings or {})
        url = await _render_and_upload(
            engagement_id,
            title="Apresentação Executiva — FinOps Audit",
            subtitle=f"Engagement {engagement_id} · Semana 4",
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

    return {"ok": False, "reason": f"unknown_deliverable_{deliverable_type}"}


def _baseline_report_md(engagement: dict) -> str:
    """Render the phase-1 baseline report from intake_data alone."""
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    lines: List[str] = []
    lines.append("## Contexto")
    lines.append(
        "Documento de baseline da semana 1. Captura o que o cliente reportou "
        "no intake e o que vamos validar na análise da semana 2."
    )
    lines.append("")
    lines.append("## Dados reportados no intake")
    if not intake:
        lines.append("- (intake vazio — aguardando submissão)")
    else:
        for k, v in intake.items():
            if v in (None, "", []):
                continue
            lines.append(f"- **{k}:** {v}")
    lines.append("")
    lines.append("## Próximos passos")
    lines.append("- Acesso IAM read-only validado.")
    lines.append("- CUR via Athena habilitado.")
    lines.append("- Análise nos 8 vetores roda na semana 2.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase runners — each is invoked by its registered orchestrator handler
# ---------------------------------------------------------------------------


def _intake_submitted(engagement: dict) -> bool:
    """Heuristic: intake counts as submitted when the operator-facing
    columns landed in ``intake_data`` AND a sentinel timestamp is set.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        return False
    if intake.get("submitted_at"):
        return True
    # Older / partial submissions: count enough required fields.
    required = (
        "aws_spend_last_6_months",
        "aws_account_count",
        "primary_services",
        "tagging_strategy",
        "biggest_cost_concerns",
        "executive_sponsor_email",
    )
    filled = sum(1 for k in required if intake.get(k))
    return filled >= 4


async def _run_phase_1(engagement: dict) -> dict:
    """Phase 1 — wait for intake submission, nudge if silent."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    if _intake_submitted(engagement):
        # Advance to phase 2.
        await _engagement_patch(
            engagement_id,
            {
                "current_phase": 2,
                "status": "running",
                "next_phase_at": _serialize(_now() + _PHASE_INTERVAL),
            },
        )
        # Render the baseline report and stash it.
        try:
            await generate_deliverable(engagement_id, "baseline_report")
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_1: baseline report failed eng=%s", engagement_id
            )

        next_at = _now() + timedelta(minutes=5)
        return {
            "ok": True,
            "advanced_to_phase": 2,
            "next_action": "finops_phase_2_analysis",
            "next_action_at": next_at,
        }

    # Not submitted yet — has it been long enough to nudge?
    started_at = engagement.get("started_at")
    started_dt = None
    if started_at:
        try:
            started_dt = datetime.fromisoformat(
                str(started_at).replace("Z", "+00:00")
            )
        except ValueError:
            started_dt = None
    elapsed = (
        (_now() - started_dt) if started_dt else timedelta(0)
    )

    # Reminder once at the 5-day mark.
    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    reminder_sent = bool(artifacts.get("intake_reminder_sent_at"))

    if elapsed >= _INTAKE_REMINDER_AFTER and not reminder_sent and email:
        token = _hmac_token(engagement_id, "intake")
        intake_url = (
            f"{BASE_URL}/api/delivery/finops/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _intake_reminder_email_html(
            first_name=first_name, intake_url=intake_url
        )
        try:
            await _send_email(
                engagement_id=engagement_id,
                to=email,
                subject="Intake pendente — FinOps Audit",
                html=html,
                kind="finops_intake_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"intake_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":hourglass: FinOps engagement `{engagement_id}` — intake "
                f"pendente há {elapsed.days} dias. Lembrete enviado pra "
                f"{email}."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_1: reminder send failed eng=%s", engagement_id
            )

    # Re-check tomorrow.
    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "intake_submission",
        "next_action": "finops_phase_1_data_collection",
        "next_action_at": next_at,
    }


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — Claude composes findings narrative, ship PDF + email."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    findings = await _compose_findings_narrative(engagement, intake)
    findings_md = _findings_to_markdown(findings)

    pdf_url = await _render_and_upload(
        engagement_id,
        title="Findings FinOps — análise por vetor",
        subtitle=f"Engagement {engagement_id} · Semana 2",
        body_md=findings_md,
        object_path=f"{engagement_id}/findings_list.pdf",
    )

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_2_findings": findings,
            "findings_list_md": findings_md,
            "findings_list_url": pdf_url,
        },
    )

    if email:
        top = _top_findings_for_email(findings)
        html = _phase2_email_html(
            first_name=first_name,
            pdf_url=pdf_url,
            top_findings=top,
        )
        try:
            await _send_email(
                engagement_id=engagement_id,
                to=email,
                subject="Findings da semana 2 — FinOps Audit",
                html=html,
                kind="finops_phase_2_findings",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_2: email failed eng=%s", engagement_id
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
        "next_action": "finops_phase_3_quickwins",
        "next_action_at": next_at,
    }


async def _run_phase_3(engagement: dict) -> dict:
    """Phase 3 — compose change log, request client sign-off."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    findings = artifacts.get("phase_2_findings") or {}

    # If we hit phase 3 with no findings cached (e.g. someone fast-forwarded
    # the engagement manually), compose them on the fly.
    if not findings:
        findings = await _compose_findings_narrative(
            engagement, engagement.get("intake_data") or {}
        )
        await _engagement_merge_artifacts(
            engagement_id, {"phase_2_findings": findings}
        )

    # Idempotency — if we already sent the change log and are just waiting,
    # check for approval or hit the reminder window.
    approved_at = artifacts.get("phase_3_approved_at")
    change_log_url = artifacts.get("change_log_url")
    change_log_md = artifacts.get("phase_3_change_log_md")

    if approved_at:
        # Already approved — schedule phase 4.
        await _engagement_patch(
            engagement_id,
            {
                "current_phase": 4,
                "status": "running",
                "next_phase_at": _serialize(_now() + _PHASE_INTERVAL),
            },
        )
        next_at = _now() + timedelta(minutes=5)
        return {
            "ok": True,
            "advanced_to_phase": 4,
            "next_action": "finops_phase_4_roadmap",
            "next_action_at": next_at,
        }

    if not change_log_md or not change_log_url:
        change_log_md = await _compose_change_log_narrative(engagement, findings)
        change_log_url = await _render_and_upload(
            engagement_id,
            title="Change Log — Quick Wins",
            subtitle=f"Engagement {engagement_id} · Semana 3",
            body_md=change_log_md,
            object_path=f"{engagement_id}/change_log.pdf",
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_change_log_md": change_log_md,
                "change_log_url": change_log_url,
                "phase_3_sent_at": _now_iso(),
            },
        )

        low, high = _findings_total_savings(findings)
        if email:
            token = _hmac_token(engagement_id, "approval")
            approval_url = (
                f"{BASE_URL}/api/delivery/finops/approve"
                f"?engagement_id={engagement_id}&token={token}"
            )
            html = _phase3_email_html(
                first_name=first_name,
                changelog_url=change_log_url,
                approval_url=approval_url,
                savings_brl=f"{_brl(low)} – {_brl(high)}",
            )
            try:
                await _send_email(
                    engagement_id=engagement_id,
                    to=email,
                    subject="Quick wins prontos — aprovação requerida",
                    html=html,
                    kind="finops_phase_3_approval",
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "finops.phase_3: email failed eng=%s", engagement_id
                )

        # Re-check in 1 day.
        next_at = _now() + timedelta(days=1)
        return {
            "ok": True,
            "waiting_for": "client_approval",
            "next_action": "finops_phase_3_quickwins",
            "next_action_at": next_at,
        }

    # Already sent — check whether the reminder window elapsed.
    sent_at = artifacts.get("phase_3_sent_at")
    sent_dt = None
    if sent_at:
        try:
            sent_dt = datetime.fromisoformat(
                str(sent_at).replace("Z", "+00:00")
            )
        except ValueError:
            sent_dt = None
    elapsed = (_now() - sent_dt) if sent_dt else timedelta(0)
    reminder_sent = bool(artifacts.get("phase_3_reminder_sent_at"))

    if elapsed >= _APPROVAL_REMINDER_AFTER and not reminder_sent and email:
        token = _hmac_token(engagement_id, "approval")
        approval_url = (
            f"{BASE_URL}/api/delivery/finops/approve"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _approval_reminder_email_html(
            first_name=first_name, approval_url=approval_url
        )
        try:
            await _send_email(
                engagement_id=engagement_id,
                to=email,
                subject="Quick wins ainda aguardando sign-off",
                html=html,
                kind="finops_phase_3_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"phase_3_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":warning: FinOps engagement `{engagement_id}` — sign-off "
                f"de quick wins pendente há {elapsed.days} dias. Lembrete "
                f"enviado, escalando pra Mila."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_3: reminder failed eng=%s", engagement_id
            )

    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "client_approval",
        "next_action": "finops_phase_3_quickwins",
        "next_action_at": next_at,
    }


async def _run_phase_4(engagement: dict) -> dict:
    """Phase 4 — generate final deliverables, fire invoice, close engagement."""
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    findings = artifacts.get("phase_2_findings") or {}
    change_log_md = artifacts.get("phase_3_change_log_md") or ""

    # Compose all three deliverables.
    report_md = await _compose_final_report_narrative(
        engagement, findings, change_log_md
    )
    roadmap_md = await _compose_roadmap_narrative(engagement, findings)
    deck_md = await _compose_deck_narrative(engagement, findings)

    report_url = await _render_and_upload(
        engagement_id,
        title="Relatório Executivo — FinOps Audit",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
    )
    roadmap_url = await _render_and_upload(
        engagement_id,
        title="Roadmap FinOps — 12 meses",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/roadmap_12mo.pdf",
    )
    deck_url = await _render_and_upload(
        engagement_id,
        title="Apresentação Executiva — FinOps Audit",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
    )

    low, high = _findings_total_savings(findings)
    savings_str = f"{_brl(low)} – {_brl(high)}"

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "final_report_md": report_md,
            "final_report_url": report_url,
            "roadmap_md": roadmap_md,
            "roadmap_url": roadmap_url,
            "deck_md": deck_md,
            "deck_url": deck_url,
            "savings_low_brl": low,
            "savings_high_brl": high,
        },
    )

    # Send the closing email with all artifacts + NPS link.
    nps_url = (
        f"{BASE_URL}/api/delivery/finops/nps"
        f"?engagement_id={engagement_id}&token={_hmac_token(engagement_id, 'nps')}"
    )
    if email:
        html = _phase4_email_html(
            first_name=first_name,
            report_url=report_url,
            deck_url=deck_url,
            roadmap_url=roadmap_url,
            savings_brl=savings_str,
            nps_url=nps_url,
        )
        try:
            await _send_email(
                engagement_id=engagement_id,
                to=email,
                subject="FinOps Audit entregue — relatório + roadmap + deck",
                html=html,
                kind="finops_phase_4_delivery",
                cc=[RESEND_REPLY_TO_EMAIL] if RESEND_REPLY_TO_EMAIL else None,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_4: email failed eng=%s", engagement_id
            )

    # Trigger invoice generation. ``lib.contract.issue_invoice`` works on
    # contract_id, so we look that up from the engagement row.
    contract_id = engagement.get("contract_id")
    invoice_result: dict = {"ok": False, "reason": "not_attempted"}
    if contract_id:
        invoice_result = await _trigger_invoice(str(contract_id), engagement_id)

    # Close the engagement.
    await _engagement_patch(
        engagement_id,
        {
            "current_phase": 4,
            "status": "delivered",
            "delivered_at": _now_iso(),
            "next_phase_at": None,
        },
    )

    # Slack DM Mila with the wrap.
    value_str = _brl(engagement.get("total_value_brl") or PRACTICE_TICKET_BRL)
    await _send_slack_alert(
        f":white_check_mark: *FinOps Audit delivered* — engagement "
        f"`{engagement_id}`. Valor total R$ {value_str}. "
        f"Economia identificada R$ {savings_str}/ano. "
        f"Próximo: invoice ({invoice_result.get('status') or 'pending'}) + "
        f"NPS. cc {SLACK_MILA_HANDLE}"
    )

    if lead and lead.get("id"):
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.finops",
            action="finops_phase_4_roadmap",
            result="ok",
            detail=(
                f"engagement {engagement_id} delivered; savings "
                f"R$ {savings_str}/ano; invoice {invoice_result.get('status')}"
            ),
        )
        # Also append the deliverables as lead-level artifacts so the
        # operator timeline reflects the close.
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
                        "phase": 4,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "finops.phase_4: artifact append failed lead=%s kind=%s",
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
    """Call ``lib.contract.issue_invoice`` if available; otherwise log a stub.

    Lazy import so this module stays importable without ``lib.contract``.
    """
    try:
        from lib.contract import issue_invoice  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning(
            "finops: lib.contract.issue_invoice unavailable — stub invoice "
            "for engagement %s contract %s",
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
            "finops: issue_invoice failed contract=%s", contract_id
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
# Orchestrator handlers — registered by name. Each one is the thin shim
# the orchestrator dispatches to; the real work lives in the phase
# runners above.
# ---------------------------------------------------------------------------


def _engagement_id_from_lead(lead: dict) -> Optional[str]:
    """Resolve the active engagement id from a lead row.

    Strategy:
      1. ``lead.qualification_data.active_engagement_id`` if set.
      2. ``lead.artifacts`` — newest entry with ``type='engagement_kickoff'``
         or ``type='contract'`` with a stamped ``engagement_id`` in meta.
      3. PostgREST query: ``engagements?lead_id=eq.<id>&status=in.(...)&order=started_at.desc&limit=1``.

    Returns ``None`` when no engagement can be located (defensive).
    """
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
    engagement on this lead."""
    eid = _engagement_id_from_lead(lead)
    if eid:
        return eid
    lead_id = lead.get("id")
    if not lead_id:
        return None
    url = (
        f"{SUPA_URL}/engagements"
        f"?lead_id=eq.{_urlquote(str(lead_id), safe='')}"
        f"&status=in.(kickoff,running)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception("finops: resolve_engagement_id query failed")
        return None
    if r.status_code != 200:
        return None
    rows = r.json() or []
    return str(rows[0]["id"]) if rows and rows[0].get("id") else None


@register("finops_kickoff")
async def h_finops_kickoff(lead: dict) -> dict:
    """Entry-point handler — fires once after contract.payment_webhook."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_kickoff: no active engagement found",
        }
    engagement = await _engagement_get(engagement_id)
    intake = (engagement or {}).get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    result = await kickoff(engagement_id, intake)
    return {
        "next_action": "finops_phase_1_data_collection",
        "next_action_at": result.get("next_action_at") or (_now() + timedelta(days=1)),
        "status": "delivery_running",
        "detail": (
            f"finops kickoff ok; engagement {engagement_id}; intake email sent"
        ),
    }


@register("finops_phase_1_data_collection")
async def h_finops_phase_1(lead: dict) -> dict:
    """Phase 1 handler — waits for intake, sends reminders, advances."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_phase_1: no active engagement",
        }
    result = await run_phase(engagement_id, 1)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running" if not result.get("delivered") else "won",
        "detail": (
            f"finops phase 1: "
            f"{'advanced→2' if result.get('advanced_to_phase') else 'waiting intake'}"
        ),
    }


@register("finops_phase_2_analysis")
async def h_finops_phase_2(lead: dict) -> dict:
    """Phase 2 handler — analysis + findings PDF."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_phase_2: no active engagement",
        }
    result = await run_phase(engagement_id, 2)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            f"finops phase 2: findings shipped for engagement {engagement_id}"
        ),
    }


@register("finops_phase_3_quickwins")
async def h_finops_phase_3(lead: dict) -> dict:
    """Phase 3 handler — change log + approval request."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_phase_3: no active engagement",
        }
    result = await run_phase(engagement_id, 3)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            "finops phase 3: "
            + (
                "approved → advancing to phase 4"
                if result.get("advanced_to_phase")
                else "change log sent; awaiting approval"
            )
        ),
    }


@register("finops_phase_4_roadmap")
async def h_finops_phase_4(lead: dict) -> dict:
    """Phase 4 handler — final deliverables + invoice trigger + close."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_phase_4: no active engagement",
        }
    result = await run_phase(engagement_id, 4)
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "won" if result.get("delivered") else "delivery_running",
        "detail": (
            f"finops phase 4: "
            f"{'delivered' if result.get('delivered') else 'in progress'}"
            f"; invoice={result.get('invoice', {}).get('status')}"
        ),
    }


@register("finops_send_progress_update")
async def h_finops_progress_update(lead: dict) -> dict:
    """Mid-phase nudge — re-runs whichever phase the engagement is on.

    Useful when the cron tick wants to give the engagement a "poke" without
    actually advancing it — the underlying phase runners are all idempotent
    and will send reminders / Slack pings as needed.
    """
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_progress: no active engagement",
        }
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "finops_progress: engagement disappeared",
        }
    phase = int(engagement.get("current_phase") or 1)
    result = await run_phase(engagement_id, phase)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": f"finops progress update: re-ran phase {phase}",
    }


# Alias — the contract module emits ``engagement_kickoff_cloud_finops`` as
# its initial next_action string (see lib/contract.py::_kickoff_engagement).
# We register the same handler under that key so the orchestrator dispatch
# lands here directly without an intermediate translation.
HANDLER_ALIAS = "engagement_kickoff_cloud_finops"


@register(HANDLER_ALIAS)
async def h_engagement_kickoff_cloud_finops(lead: dict) -> dict:
    """Alias for ``finops_kickoff`` — wired so contract.py's emitted action
    string lands on the right handler without a string remap."""
    return await h_finops_kickoff(lead)
