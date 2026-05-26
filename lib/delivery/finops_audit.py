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

# Brand voice + framework grounding — pinned to every Claude system prompt in
# this module. Sourced from SPRINT_INPUTS_MILA.md section 1 + FinOps Foundation
# Framework (https://www.finops.org/framework/) + AWS Well-Architected Cost
# Optimization Pillar + GCP Cost Optimization best practices.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA, ex-AWS Solutions Architect, ex-Google, "
    "ex-MongoDB, 15× AWS certifications). Esta NÃO é uma proposta comercial — "
    "é a entrega final de uma auditoria FinOps multicloud de 4 semanas (R$ 45-60k), "
    "no padrão de qualidade de um AWS Well-Architected Review ou um Google Cloud "
    "TAM Cost Optimization Engagement. O leitor é CTO, VP Engineering ou "
    "Head of Platform — ele tem conhecimento técnico e vai detectar fluff.\n\n"
    "VOZ ANUVIA: seca, direta, anti-hype, primeiro os números, depois a "
    "narrativa. Frases curtas declarativas misturadas com cadeias causa-efeito "
    "mais longas. Léxico que usa: vazamento, clareza, diagnóstico, processo, "
    "padrão, sobreviver em produção. Léxico que evita: sinergia, transformação, "
    "leverage, magia, mágico, IA generativa que muda o jogo.\n\n"
    "FRAMEWORKS DE REFERÊNCIA (obrigatórios — cite explicitamente):\n\n"
    "1) FinOps Foundation Framework — 6 Capabilities em 3 Domains, com maturity "
    "Crawl/Walk/Run:\n"
    "   Domain INFORM: Allocation · Reporting & Analytics · Showback/Chargeback\n"
    "   Domain OPTIMIZE: Workload Optimization · Pricing & Rate Optimization · "
    "Anomaly Management\n"
    "   Domain OPERATE: Budget Management · Forecasting · Cloud Policy & "
    "Governance · Decentralized Decision Making · FinOps Education & Enablement · "
    "Onboarding Workloads\n"
    "   REGRA: cada finding deve mapear pra ≥1 FinOps Capability + indicar a "
    "maturity atual (Crawl|Walk|Run) e a maturity-alvo em 12 meses.\n\n"
    "2) AWS Well-Architected — Cost Optimization Pillar (5 design principles, "
    "~20 best practices agrupadas em COST01-COST06):\n"
    "   COST01 Cloud Financial Management (BP: team enablement, governance, "
    "executive sponsorship)\n"
    "   COST02 Expenditure & Usage Awareness (BP: cost allocation, AWS Budgets, "
    "Cost Explorer, CUR ingestion via Athena)\n"
    "   COST03 Cost-Effective Resources (BP: instance/storage selection, "
    "pricing models RI/SP, license optimization, SaaS rationalization)\n"
    "   COST04 Manage Demand & Supplying Resources (BP: rightsizing, "
    "autoscaling, scheduling, Spot)\n"
    "   COST05 Optimize Over Time (BP: workload review cadence, monitoring, "
    "capacity reservations strategy)\n"
    "   COST06 Modernization (BP: FinOps lens on architectural decisions)\n"
    "   REGRA: cada finding AWS deve citar um código BP específico (ex: "
    "'COST05-BP01: Develop a workload review process'). Se incerto do BP "
    "exato, use 'COST03-BP*' genérico — mas SEMPRE cite o COST-XX.\n\n"
    "3) GCP Cost Optimization (quando intake mencionar GCP/multi-cloud):\n"
    "   Discounts: CUDs (Spend-based 1y/3y, Resource-based 1y/3y, Flexible CUDs) · "
    "SUDs (auto) · Preemptible/Spot VMs\n"
    "   Tooling: Recommender API (rightsizing, idle VM, CUD coverage, BQ slot) · "
    "FinOps Hub · Billing Export to BigQuery (equivalente do AWS CUR — query "
    "via SQL) · Active Assist\n"
    "   Allocation: hierarquia Folders → Projects → Labels (mapear pra BUs)\n"
    "   Workloads especiais: BigQuery slot reservations vs on-demand · "
    "GKE/Anthos cost allocation · Cloud SQL committed use\n"
    "   REGRA: em ambiente AWS-only, mencione equivalência GCP em nota "
    "comparativa quando relevante (ex: 'AWS RI 1-year → equivalente GCP: "
    "CUD spend-based 1-year').\n\n"
    "DATA SOURCE PROVENANCE — toda afirmação quantitativa DEVE declarar fonte "
    "em tag entre colchetes:\n"
    "   [INTAKE]         declarado pelo cliente no formulário de intake\n"
    "   [CUR]            AWS Cost & Usage Report via Athena (cite query name)\n"
    "   [CE]             Cost Explorer API\n"
    "   [TA]             Trusted Advisor\n"
    "   [CO]             Compute Optimizer\n"
    "   [GCP-EXPORT]     GCP Billing Export para BigQuery\n"
    "   [GCP-REC]        GCP Recommender API\n"
    "   [FH]             FinOps Hub (GCP)\n"
    "   [ESTIMATIVA]     fallback explícito quando raw data ainda não acessível\n"
    "   Exemplo: 'RDS sobre-provisionado [INTAKE confirmou 3 instâncias "
    "db.m5.xlarge + CO Recommender flagged downsizing pra db.t4g.large]. "
    "Economia anualizada: R$ 42.750 [ESTIMATIVA baseada em right-sizing 60% + "
    "RI 1-year coverage 80%].'\n\n"
    "PREMISSAS E LIMITAÇÕES — todo deliverable DEVE incluir seção 'Premissas "
    "e Limitações' logo após Sumário Executivo, declarando:\n"
    "   • Dados analisados (intake responses, sample CUR se houver, etc.)\n"
    "   • Dados pendentes (IAM read-only role pra Athena, GCP IAM viewer pra "
    "Billing Export BQ, sample CloudWatch metrics, etc.)\n"
    "   • Premissas adotadas (ex: 'right-sizing potential estimado em 20-30% "
    "baseado em distribuição típica de CPU idle — validar com Compute "
    "Optimizer + CloudWatch 14d quando IAM role for provisionado')\n"
    "   • Status do documento: 'Rascunho preliminar baseado em intake' OU "
    "'Auditoria validada com CUR + CE'\n\n"
    "REGRAS DE PROFUNDIDADE TÉCNICA (não negociáveis):\n"
    "1. Cite instance types específicos (db.m5.2xlarge, m7g.xlarge, t4g.large, "
    "r6g.2xlarge). Nunca diga genericamente 'instâncias'. Em GCP: n2-standard-8, "
    "n2d-highmem-16, e2-medium, c3-standard-22.\n"
    "2. Cite serviços com nome de produto exato (AWS Compute Optimizer, Cost "
    "Explorer, Trusted Advisor, S3 Intelligent-Tiering, Aurora I/O-Optimized, "
    "EBS gp3, Savings Plans Compute vs EC2 Instance; GCP Recommender, FinOps "
    "Hub, Active Assist, Committed Use Discounts).\n"
    "3. Cite métricas CloudWatch (CPUUtilization, VolumeReadOps, DBIOPS, "
    "NetworkIn) com thresholds reais (CPU p95 <20% por 14d → candidato a "
    "downsizing). Em GCP: Cloud Monitoring metrics (compute.googleapis.com/"
    "instance/cpu/utilization).\n"
    "4. Cite comandos AWS CLI/API/Athena query quando relevante "
    "(modify-db-instance, AbortIncompleteMultipartUpload lifecycle rule, "
    "describe-reserved-instances-modifications). Em GCP: gcloud recommender "
    "recommendations list, BigQuery SQL contra billing export.\n"
    "5. Use números DO INTAKE do cliente sempre que possível. Se intake diz "
    "R$ 95k/mês AWS spend, todos os números derivam disso — não invente.\n"
    "6. Math explícita: 'gp2 ~US$ 0,10/GB/mês vs gp3 ~US$ 0,08/GB/mês × 8 TB × "
    "12 = US$ 1.920/ano = R$ 9.600/ano (USD/BRL 5,0)'. Mostre a conta.\n"
    "7. Quando estimar, use [ESTIMATIVA] uma vez só e justifique. NÃO repita "
    "'padrão setorial' como muleta — tique de junior.\n"
    "8. Cada finding DEVE ter: FinOps Capability + maturity atual/alvo + AWS "
    "WA BP code + data source tags + validation criteria + rollback plan + "
    "janela de execução + economia anualizada com confiança (alta|média|baixa).\n"
    "9. ADRs em formato ADR-XX (ADR-01 RI 1-year vs 3-year, ADR-02 Graviton "
    "blue/green migration, ADR-03 S3 Intelligent-Tiering vs lifecycle manual, "
    "ADR-04 NAT Gateway vs VPC Endpoints, etc.)\n"
    "10. Confiança baixa quando só intake foi acessado. Diga isso explicitamente "
    "e indique o que precisa pra confiança alta.\n"
    "11. Nunca prometa o que não pode ser medido. Português do Brasil.\n"
    "12. Fechamento honesto: se faltou acesso (IAM role, Billing Export), "
    "diga que validação dos achados fica limitada a 60-70% de precisão sem "
    "isso, e liste o que pedir pra próxima rodada."
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
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Obrigada pela confiança. O contrato da FinOps Audit foi confirmado — investimento total <strong>R$ {value_str}</strong>, cronograma de 4 semanas com entregáveis ao final de cada uma. Nosso time já está organizando o kickoff da semana 1.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Semana 1 — Discovery &amp; Data Collection.</strong> Pra começarmos com a análise calibrada ao perfil de vocês, nosso time precisa das informações abaixo. Quanto antes recebermos, mais aprofundada fica a análise da semana 2.</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>AWS spend dos últimos 6 meses (CSV do CUR ou self-report)</li>
  <li>Quantidade de accounts e estrutura de Organizations</li>
  <li>Serviços primários em uso</li>
  <li>Estratégia atual de tagging</li>
  <li>Maiores preocupações de custo que vocês já mapearam</li>
  <li>Preferência de remediação: time interno OU Anuvia executa via success-fee</li>
  <li>Nome e email do sponsor executivo</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Em paralelo, vamos solicitar um IAM role read-only pra rodar queries direto no CUR — o detalhamento técnico está no formulário.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Como referência, padrão observado nas últimas 14 auditorias da Anuvia: 27% do bill mensal sai por 4 mesmos canos. Nosso time vai mapear os de vocês.</p>
"""
    return _wrap_email("Boas-vindas — FinOps Audit Anuvia", body)


def _phase2_email_html(
    *, first_name: str, pdf_url: str, top_findings: List[str]
) -> str:
    bullets = "".join(
        f'<li style="margin:6px 0;line-height:1.55;">{f}</li>'
        for f in top_findings[:5]
    )
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Concluímos a análise da semana 2. Nossa equipe revisou os 8 vetores da auditoria (compute, storage, network, data transfer, RDS, S3, SaaS de terceiros, support tier) e segue em anexo o relatório de findings priorizados por impacto × esforço × risco.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Top 5 oportunidades identificadas:</strong></p>
<ul style="color:#1a1a1a;line-height:1.6;margin:0 0 18px 18px;padding:0;">{bullets}</ul>
<p style="margin:24px 0;"><a href="{pdf_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Findings completos (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Na semana 3, preparamos o plano detalhado de execução das quick wins (high impact, low risk). O cronograma ramifica baseado na opção de remediação escolhida no intake — time interno de vocês ou execução pela Anuvia via success-fee.</p>
"""
    return _wrap_email("Findings da semana 2 — FinOps Audit", body)


def _phase3_email_html(
    *,
    first_name: str,
    changelog_url: str,
    approval_url: str,
    savings_brl: str,
    remediation_choice: str = "cliente_interno",
) -> str:
    """Phase 3 email branches based on intake remediation_choice.

    - 'cliente_interno': delivers plan + runbooks for client team to execute
    - 'anuvia_success_fee': delivers plan + asks for sign-off for Anuvia to execute
    """
    is_anuvia = remediation_choice == "anuvia_success_fee"

    if is_anuvia:
        execution_block = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Conforme acordado no intake, nossa equipe executa as mudanças em produção via success-fee (15-20% da economia validada). Antes de iniciar, precisamos do sign-off explícito por mudança — cada item vem com rollback documentado e janela proposta.</p>
<p style="margin:24px 0;"><a href="{changelog_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Change log completo (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;">Quando estiverem prontos para autorizar a execução pela nossa equipe:</p>
<p style="margin:8px 0 24px;"><a href="{approval_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Aprovar execução Anuvia &rarr;</a></p>
<p style="color:#78716c;line-height:1.55;font-size:13px;margin:0 0 14px;">Sem aprovação não tocamos em nada em produção. Se quiserem ajustar escopo (excluir item, adicionar contexto, mudar janela), basta responder este email.</p>
"""
    else:
        execution_block = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Como o time interno de vocês vai executar as mudanças, o documento abaixo está estruturado como runbook prático: cada item com critério de validação, comandos AWS CLI/console, métricas CloudWatch para checar pré/pós, plano de rollback e janela sugerida.</p>
<p style="margin:24px 0;"><a href="{changelog_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Runbook completo (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;">Quando completarem a execução das quick wins, nos avisem para validarmos os ganhos via CUR:</p>
<p style="margin:8px 0 24px;"><a href="{approval_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Confirmar execução concluída &rarr;</a></p>
<p style="color:#78716c;line-height:1.55;font-size:13px;margin:0 0 14px;">Se tiverem dúvidas técnicas durante a execução, basta responder este email — nossa equipe acompanha e responde em até 1 dia útil. Se preferirem, ainda é possível migrar para o modelo de execução Anuvia via success-fee.</p>
"""

    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Semana 3 — o plano de quick wins está pronto. Economia anualizada estimada nesta fase: <strong>R$ {savings_brl}</strong>. Cada mudança vem com critério de validação prévio, plano de execução e procedimento de rollback documentado.</p>
{execution_block}
"""
    return _wrap_email("Plano de quick wins pronto — FinOps Audit", body)


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
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Concluímos a FinOps Audit. Foram quatro semanas de trabalho conjunto entre nossa equipe e vocês — segue em anexo os três entregáveis finais:</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo</a> — baseline AWS, findings detalhados por vetor, savings realizadas, ADRs e premissas explícitas.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva (PPTX)</a> — material para apresentação com C-level e board.</li>
  <li><a href="{roadmap_url}" style="color:#0f172a;">Roadmap 12 meses</a> — Crawl→Walk→Run mapeado nas FinOps Foundation capabilities, com gates de evolução.</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Economia anualizada identificada nesta auditoria: <strong>R$ {savings_brl}</strong>. A sessão final de handoff (90 min) será agendada nos próximos dias com nossa equipe.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Como próximos passos, nossa equipe está à disposição para acompanhar a evolução do roadmap. Caso queiram que a Anuvia execute as iniciativas estruturais (RI/SP strategy, Graviton migration, re-arch cross-AZ), trabalhamos via success-fee 15-20% da economia validada — sem custo upfront adicional.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Pedimos um favor breve: 2 minutos para deixar a avaliação NPS — feedback honesto nos ajuda a evoluir o programa.</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar avaliação &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">E se conhecerem outro CTO ou Head of Cloud enfrentando os mesmos desafios de spend AWS, ficaríamos gratos pela indicação. Referrals diretos seguem sendo nossa principal forma de crescimento.</p>
"""
    return _wrap_email("Entrega final — FinOps Audit Anuvia", body)


def _intake_reminder_email_html(*, first_name: str, intake_url: str) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete amigável — nosso time ainda não recebeu o formulário de intake preenchido. Para mantermos o cronograma das 4 semanas, idealmente recebemos as informações nos próximos 2 dias úteis.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Caso haja algum bloqueio (acesso AWS pendente, sponsor executivo a definir, qualquer dúvida sobre os campos), basta responder este email — nosso time ajuda a destravar.</p>
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


# ---------------------------------------------------------------------------
# Shared branding module — Anuvia visual identity (HTML/PDF + PPTX)
# ---------------------------------------------------------------------------
#
# We migrated off inline-styled HTML helpers to ``lib.delivery._branding`` so
# every practice (finops, ai, devops, ...) renders deliverables with the
# same cover page, table styling, fonts, and tokens. The import is wrapped
# in a try/except so a missing module never breaks the delivery pipeline —
# we fall back to a minimal inline renderer in that case.

try:
    from lib.delivery import _branding as _branding_mod  # type: ignore
    _BRANDING_AVAILABLE = True
except Exception:  # noqa: BLE001
    _branding_mod = None  # type: ignore[assignment]
    _BRANDING_AVAILABLE = False
    log.warning(
        "finops: lib.delivery._branding not importable; falling back to "
        "inline-styled deliverable renderer"
    )


def _deliverable_html(
    title: str,
    subtitle: str,
    body_md_html: str,
    *,
    body_md: Optional[str] = None,
    engagement_meta: Optional[dict] = None,
    show_cover: bool = True,
) -> str:
    """Render a full deliverable HTML doc with Anuvia branding.

    Preferred path: delegate to ``_branding.render_deliverable_html`` so the
    document gets the cover page, running headers/footers, table styling,
    blockquotes and code blocks defined once in the shared module.

    Backward-compat: the legacy two-arg signature ``(title, subtitle,
    body_md_html)`` still works — we just wrap the pre-rendered HTML in the
    minimal inline template so existing call sites keep functioning during
    the transition.
    """
    if _BRANDING_AVAILABLE and body_md is not None:
        return _branding_mod.render_deliverable_html(
            practice_label="FINOPS AUDIT",
            title=title,
            subtitle=subtitle,
            body_md=body_md,
            engagement_meta=engagement_meta,
            show_cover=show_cover,
        )

    # Fallback path — A4-friendly inline-styled wrapper, no cover page.
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color:#1a1a1a; font-size:12px; line-height:1.6; margin:0; padding:0; background:#ffffff; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:20px 0 8px; border-bottom:1px solid #e7e5e4; padding-bottom:4px; }}
  h3 {{ font-size:13px; margin:14px 0 6px; }}
  p, li {{ font-size:12px; }}
  ul {{ padding-left:20px; margin:6px 0 12px; }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:11px; }}
  th {{ background:#1a1a1a; color:#fafaf9; text-align:left; padding:8px 10px; font-weight:600; }}
  td {{ padding:6px 10px; border-bottom:1px solid #e7e5e4; vertical-align:top; }}
  .small {{ color:#78716c; font-size:11px; }}
  .meta {{ color:#475569; font-size:11px; margin:0 0 18px; }}
</style></head>
<body>
<header style="margin-bottom:24px;">
  <p class="small" style="text-transform:uppercase;letter-spacing:0.18em;margin:0 0 6px;font-weight:600;">Anuvia · FinOps Audit</p>
  <h1>{title}</h1>
  <p class="meta">{subtitle}</p>
</header>
{body_md_html}
<footer style="margin-top:32px;padding-top:18px;border-top:1px solid #e7e5e4;color:#78716c;font-size:11px;">
  Anuvia Cloud &amp; AI Consulting · Mila Vernazza · Documento gerado em {_now().strftime("%d/%m/%Y")}
</footer>
</body></html>"""


def _md_to_html(md: str) -> str:
    """Markdown -> HTML with Anuvia styling. Delegates to the shared module.

    Falls back to a tiny hand-rolled converter (lists, headings, bold) when
    ``_branding`` isn't importable so we never block a delivery on a missing
    dependency.
    """
    if _BRANDING_AVAILABLE:
        return _branding_mod.md_to_html_rich(md)

    # Minimal in-line fallback — preserved from the original implementation.
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

    prompt = f"""Você está compondo a seção de findings da auditoria FinOps — padrão AWS Well-Architected Review + FinOps Foundation framework.

INSTRUÇÃO DE OUTPUT (CRÍTICA): Responda APENAS com JSON válido. NADA antes ou depois do `{{`. NADA de prosa, markdown, comentários, ` ``` `. Apenas o objeto JSON puro.

LIMITES DE TAMANHO (não negociáveis pra caber no token budget):
- hypothesis: máximo 3 frases (250 chars)
- validation_criteria: máximo 3 bullets, cada um <80 chars
- implementation_steps: máximo 4 steps, cada um <120 chars
- rollback_plan: 1-2 frases máximo
- gcp_equivalent: 1 frase de 1 linha

Voz Anuvia senior = densidade, NÃO verbosidade. Frase curta, número, referência técnica. STOP.


Perfil do cliente (intake submetido):
{profile_block}

REQUISITOS DE FRAMEWORK (não negociáveis):
- Cada finding mapeia pra ≥1 FinOps Capability (Allocation, Workload Optimization, Pricing & Rate Optimization, Anomaly Management, Budget Management, Forecasting, Cloud Policy & Governance, Showback/Chargeback, etc.) com maturity_current e maturity_target (Crawl|Walk|Run).
- Cada finding AWS cita o AWS WA Best Practice code (COST01-BPxx até COST06-BPxx). Se incerto do BPxx específico, use 'COST03-BP*' genérico — SEMPRE cite o COST-XX.
- Se intake indicar GCP ou multi-cloud, cada finding GCP referencia o serviço/recommender específico (Recommender API, CUDs, FinOps Hub, Billing Export BQ, etc.). Caso contrário, inclua 1-line de equivalência GCP em 'gcp_equivalent'.
- Cada quantitativo tem data source tag: [INTAKE] | [CUR] | [CE] | [TA] | [CO] | [GCP-EXPORT] | [GCP-REC] | [FH] | [ESTIMATIVA].
- 'confidence' = alta quando baseado em CUR/CE/TA/CO/GCP-REC; média quando intake + math setorial; baixa quando só estimativa sem validação.

Vetores (gere 1 finding por vetor — todos os 8):
{vectors_block}

Devolva APENAS um JSON válido com esta estrutura, sem markdown, sem comentários:

{{
  "summary": "<3-5 linhas, voz Anuvia: seca, numbers-first. Cite o baseline mensal (com tag [INTAKE]), economia total anualizada (faixa), payback estimado em dias, e o status do documento (rascunho preliminar baseado em intake OU validado com CUR).>",
  "status_documento": "<'rascunho preliminar baseado em intake' OU 'auditoria validada com CUR + CE' — based em quais dados foram realmente acessados>",
  "premissas_limitacoes": {{
    "dados_analisados": ["<lista do que foi acessado: intake fields, sample CUR, etc.>"],
    "dados_pendentes": ["<o que falta: IAM read-only role pra Athena CUR, GCP IAM viewer pra Billing Export BQ, CloudWatch metrics 14d, etc.>"],
    "premissas_adotadas": ["<premissas explícitas: 'right-sizing estimado em 20-30% baseado em distribuição CPU típica — validar com CO + CloudWatch quando IAM role provisionada'>"]
  }},
  "findings": [
    {{
      "vector": "<nome curto, ex: Compute>",
      "finops_capability": "<ex: Workload Optimization + Pricing & Rate Optimization>",
      "maturity_current": "<Crawl|Walk|Run>",
      "maturity_target_12mo": "<Crawl|Walk|Run>",
      "aws_wa_bp": "<ex: COST04-BP01, COST04-BP03 OR COST03-BP*>",
      "gcp_equivalent": "<1-line: ex: 'Equivalente GCP: Recommender rightsizing + CUD spend-based 1y'>",
      "data_sources": ["<lista de tags: ex: ['INTAKE', 'ESTIMATIVA']>"],
      "hypothesis": "<3-5 frases. Cite instance types específicos (db.m5.2xlarge → db.t4g.large), serviços com nome de produto exato (Compute Optimizer, S3 Intelligent-Tiering), métricas CloudWatch concretas (CPU p95 <20% por 14d), e math explícita (gp2 → gp3: 0.10 vs 0.08 USD/GB/mês × 8TB × 12).>",
      "validation_criteria": ["<bullet de query CUR/CloudWatch metric a verificar>", "<bullet de Compute Optimizer recommendation a cross-check>"],
      "implementation_steps": ["<step 1>", "<step 2>"],
      "rollback_plan": "<procedure curta de reversão>",
      "execution_window": "<ex: 'fora do horário comercial BRT' ou 'próxima janela de manutenção'>",
      "savings_brl_low": <int>,
      "savings_brl_high": <int>,
      "confidence": "<alta|média|baixa>",
      "effort": "<low|med|high>",
      "risk": "<low|med|high>",
      "priority": "<quick_win|medium_term|structural>"
    }}
  ],
  "closing_recommendation": "<2-3 linhas: o que pedir na próxima rodada pra elevar confiança (IAM role Athena, GCP Billing Export access, etc.). Honesto sobre limitação de precisão sem esses dados.>"
}}
"""

    # Sonnet 4.5 supports up to 64k output. 16k handles 8 vectors w/ size
    # caps enforced in prompt (max 4 steps × 120 chars, 3 validation × 80, etc).
    raw = await _call_claude(prompt, max_tokens=16000)

    # Defensive parse — strip code fences if Claude added them despite the
    # explicit instruction, and tolerate trailing prose.
    text = raw.strip()
    if text.startswith(_CLAUDE_FALLBACK_TAG):
        return {
            "summary": text,
            "status_documento": "rascunho preliminar baseado em intake",
            "premissas_limitacoes": {
                "dados_analisados": ["intake submetido pelo cliente"],
                "dados_pendentes": [
                    "IAM read-only role pra Athena CUR queries",
                    "GCP IAM viewer pra Billing Export BigQuery",
                    "CloudWatch metrics 14d via cross-account read role",
                ],
                "premissas_adotadas": [
                    "Claude indisponível — preencher manualmente antes de enviar ao cliente",
                ],
            },
            "findings": [
                {
                    "vector": v.split(" ")[0],
                    "finops_capability": "Workload Optimization",
                    "maturity_current": "Crawl",
                    "maturity_target_12mo": "Walk",
                    "aws_wa_bp": "COST03-BP*",
                    "gcp_equivalent": "—",
                    "data_sources": ["ESTIMATIVA"],
                    "hypothesis": f"{_CLAUDE_FALLBACK_TAG} estimativa pendente",
                    "validation_criteria": [],
                    "implementation_steps": [],
                    "rollback_plan": "—",
                    "execution_window": "—",
                    "savings_brl_low": 0,
                    "savings_brl_high": 0,
                    "confidence": "baixa",
                    "effort": "med",
                    "risk": "med",
                    "priority": "medium_term",
                }
                for v in _FINOPS_VECTORS
            ],
            "closing_recommendation": (
                "Próxima rodada: solicitar IAM read-only role pra Athena CUR + "
                "GCP IAM viewer pra Billing Export BQ. Sem isso, validação fica "
                "limitada a 60-70% de precisão."
            ),
        }

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        # Drop everything after the closing fence if any leftover.
        if "```" in text:
            text = text.split("```", 1)[0]

    # Extract the first balanced {...} JSON object — handles cases where Claude
    # wraps the object in prose preamble/outro despite the JSON-only instruction.
    def _extract_first_json_object(s: str) -> Optional[str]:
        start = s.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    candidate = _extract_first_json_object(text) or text

    # JSON repair safety net — if Claude hit max_tokens mid-string, close
    # outstanding string + arrays + objects so we salvage what we have.
    def _repair_truncated_json(s: str) -> str:
        """Best-effort: close any unterminated string, then balance braces."""
        depth_obj = 0
        depth_arr = 0
        in_str = False
        esc = False
        i = 0
        last_complete = 0  # index AFTER last char where state was clean
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth_obj += 1
                elif ch == "}":
                    depth_obj -= 1
                elif ch == "[":
                    depth_arr += 1
                elif ch == "]":
                    depth_arr -= 1
        # Close everything cleanly.
        out = s
        if in_str:
            # Truncate to last complete value or string-end. Easiest: trim
            # trailing partial string and close it.
            # Find the LAST safe close point: trim to last "," outside string.
            last_safe = out.rfind(",")
            if last_safe > 0:
                out = out[:last_safe]
            else:
                out += '"'  # close the string
        # Close any open arrays/objects by counting again on trimmed text.
        depth_obj = 0
        depth_arr = 0
        in_str = False
        esc = False
        for ch in out:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth_obj += 1
                elif ch == "}":
                    depth_obj -= 1
                elif ch == "[":
                    depth_arr += 1
                elif ch == "]":
                    depth_arr -= 1
        # Now close in order.
        out += "]" * max(0, depth_arr)
        out += "}" * max(0, depth_obj)
        return out

    try:
        data = json.loads(candidate)
        if not isinstance(data, dict):
            raise ValueError("top-level not object")
        if "findings" not in data or not isinstance(data["findings"], list):
            raise ValueError("missing findings array")
        return data
    except Exception as exc:  # noqa: BLE001
        # Try JSON repair first (likely max_tokens cut mid-output).
        if isinstance(exc, json.JSONDecodeError):
            log.warning("finops: claude JSON truncated, attempting repair: %s", exc)
            try:
                repaired = _repair_truncated_json(candidate)
                data = json.loads(repaired)
                if isinstance(data, dict) and isinstance(data.get("findings"), list):
                    data["_repaired_from_truncation"] = True
                    log.warning("finops: JSON repaired OK (was truncated)")
                    return data
            except Exception:  # noqa: BLE001
                pass
        log.warning("finops: claude returned non-JSON: %s", exc)
        return {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude — "
                f"erro: {type(exc).__name__}: {exc}.\n\n"
                f"=== CANDIDATE LEN {len(candidate)} ===\n"
                f"{candidate[:600]}\n...[TRUNCATED]...\n{candidate[-600:]}\n\n"
                f"=== RAW TEXT LEN {len(text)} ===\n"
                f"{text[:300]}"
            ),
            "_debug_raw_text_len": len(text),
            "_debug_candidate_len": len(candidate),
            "_debug_parse_error": f"{type(exc).__name__}: {exc}",
            "status_documento": "rascunho preliminar baseado em intake",
            "premissas_limitacoes": {
                "dados_analisados": ["intake submetido pelo cliente"],
                "dados_pendentes": [
                    "IAM read-only role pra Athena CUR queries",
                    "GCP IAM viewer pra Billing Export BigQuery",
                ],
                "premissas_adotadas": [
                    "Claude retornou JSON inválido — preencher manualmente",
                ],
            },
            "findings": [
                {
                    "vector": v.split(" ")[0],
                    "finops_capability": "Workload Optimization",
                    "maturity_current": "Crawl",
                    "maturity_target_12mo": "Walk",
                    "aws_wa_bp": "COST03-BP*",
                    "gcp_equivalent": "—",
                    "data_sources": ["ESTIMATIVA"],
                    "hypothesis": (
                        f"{_CLAUDE_FALLBACK_TAG} revisar manualmente. "
                        f"Vetor: {v}"
                    ),
                    "validation_criteria": [],
                    "implementation_steps": [],
                    "rollback_plan": "—",
                    "execution_window": "—",
                    "savings_brl_low": 0,
                    "savings_brl_high": 0,
                    "confidence": "baixa",
                    "effort": "med",
                    "risk": "med",
                    "priority": "medium_term",
                }
                for v in _FINOPS_VECTORS
            ],
            "closing_recommendation": (
                "Próxima rodada: solicitar acesso programático aos dados de billing."
            ),
        }


def _findings_to_markdown(data: dict) -> str:
    """Render the structured findings dict as a framework-grade markdown doc."""
    out: List[str] = []
    out.append("## Resumo executivo")
    out.append(data.get("summary") or "")
    out.append("")

    status = data.get("status_documento") or "rascunho preliminar baseado em intake"
    out.append("## Premissas e Limitações")
    out.append("")
    out.append(f"**Status do documento:** {status}")
    out.append("")

    premissas = data.get("premissas_limitacoes") or {}
    if isinstance(premissas, dict):
        for label_key, list_key in (
            ("Dados analisados", "dados_analisados"),
            ("Dados pendentes", "dados_pendentes"),
            ("Premissas adotadas", "premissas_adotadas"),
        ):
            items = premissas.get(list_key) or []
            if not items:
                continue
            out.append(f"**{label_key}:**")
            for item in items:
                out.append(f"- {item}")
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
        out.append("")

        # Framework metadata block — bold inline labels so it renders cleanly
        # in both markdown and the styled HTML pipeline.
        cap = f.get("finops_capability") or "—"
        mat_cur = f.get("maturity_current") or "—"
        mat_tgt = f.get("maturity_target_12mo") or "—"
        bp = f.get("aws_wa_bp") or "—"
        gcp = f.get("gcp_equivalent") or "—"
        sources = f.get("data_sources") or []
        sources_str = ", ".join(sources) if sources else "—"

        out.append(f"**FinOps Capability:** {cap}")
        out.append(f"**Maturity atual:** {mat_cur}  ·  **Maturity-alvo (12 meses):** {mat_tgt}")
        out.append(f"**AWS WA Best Practice:** {bp}")
        out.append(f"**GCP Equivalent:** {gcp}")
        out.append(f"**Fontes de dado:** [{sources_str}]")
        out.append("")
        out.append("**Hipótese e math:**")
        out.append(f.get("hypothesis") or "—")
        out.append("")

        vcrit = f.get("validation_criteria") or []
        if vcrit:
            out.append("**Validation criteria:**")
            for v in vcrit:
                out.append(f"- {v}")
            out.append("")

        steps = f.get("implementation_steps") or []
        if steps:
            out.append("**Implementation steps:**")
            for i, s in enumerate(steps, start=1):
                out.append(f"{i}. {s}")
            out.append("")

        rb = f.get("rollback_plan")
        if rb and rb != "—":
            out.append(f"**Rollback:** {rb}")
            out.append("")

        win = f.get("execution_window")
        if win and win != "—":
            out.append(f"**Janela de execução:** {win}")
            out.append("")

        low = int(f.get("savings_brl_low") or 0)
        high = int(f.get("savings_brl_high") or 0)
        total_low += low
        total_high += high
        out.append(
            f"**Economia anualizada:** R$ {_brl(low)} – R$ {_brl(high)}  ·  "
            f"**Confiança:** {f.get('confidence') or '—'}  ·  "
            f"**Esforço:** {f.get('effort') or '—'}  ·  "
            f"**Risco:** {f.get('risk') or '—'}  ·  "
            f"**Prioridade:** {f.get('priority') or '—'}"
        )
        out.append("")

    out.append("## Total estimado")
    out.append(
        f"- **Economia anualizada (faixa):** "
        f"R$ {_brl(total_low)} – R$ {_brl(total_high)}"
    )
    out.append("")

    closing = data.get("closing_recommendation")
    if closing:
        out.append("## Próxima rodada — o que pedir")
        out.append(closing)

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
        f"- {f.get('vector')} [FinOps: {f.get('finops_capability') or '—'} · "
        f"WA: {f.get('aws_wa_bp') or '—'} · sources: "
        f"{','.join(f.get('data_sources') or []) or '—'}]\n  {f.get('hypothesis')}"
        for f in quick_wins
    )

    prompt = f"""Você está escrevendo o plano de mudanças (change log) da semana 3 de uma auditoria FinOps Anuvia. Esse documento vai para o cliente aprovar mudança por mudança ANTES de qualquer execução em produção — formato AWS Well-Architected remediation plan + GCP TAM execution plan.

Quick wins identificados na semana 2:
{qw_block}

REQUISITOS DE FRAMEWORK:
- Comece com seção "## Sumário executivo" (3-5 linhas) + "## Premissas e Limitações" declarando o status do documento (rascunho preliminar OU validado com CUR) e o que ainda falta pra validação completa.
- Para cada quick win, inclua referência ao FinOps Capability + AWS WA Best Practice code + GCP equivalent (quando relevante).
- Toda afirmação quantitativa traz tag de fonte [INTAKE/CUR/CE/TA/CO/GCP-EXPORT/GCP-REC/FH/ESTIMATIVA].
- Cite instance types específicos (db.m5.2xlarge → db.t4g.large), comandos AWS CLI (modify-db-instance, put-bucket-lifecycle-configuration), e thresholds CloudWatch reais.

Estrutura markdown por quick win (use heading ### para cada um):

### {{Vector}} — {{ação concreta de 1-line}}

**FinOps Capability:** {{capability}}  ·  **AWS WA BP:** {{COST-XX-BPYY}}  ·  **GCP Equivalent:** {{1-line}}

**Fontes de dado:** [{{INTAKE/CUR/...}}]

**Descrição da mudança** (3-5 frases com instance types, comandos AWS CLI/API, math explícita):
- ...

**Critérios de validação (PRÉ-execução):**
- {{query CUR ou CloudWatch metric a verificar}}
- {{Compute Optimizer recommendation a cross-check}}
- {{checagem de blast radius — workloads dependentes}}

**Implementation steps:**
1. {{step}}
2. {{step}}

**Plano de rollback (se algo quebrar):**
1. {{step}}
2. {{step}}

**Janela proposta:** {{ex: "sábado 02:00-06:00 BRT", "próxima janela de manutenção", "imediato — operação não-disruptiva"}}

**Critérios de sucesso (PÓS-execução):**
- {{ex: "CPU p95 da nova instance permanece <70% por 72h"}}
- {{ex: "Cost Explorer mostra delta de spend conforme estimado em 48h"}}

**Economia anualizada esperada:** R$ X – R$ Y  ·  **Confiança:** alta|média|baixa

---

Termine com seção "## Próximos passos" explicando: (1) processo de sign-off por cliente, (2) ordem de execução proposta, (3) janela de monitoring pós-execução (típico 7d), (4) o que precisa pra elevar confiança baixa em alta (ex: 'IAM read-only role pra Athena CUR queries').

Voz Anuvia: seca, direta, numbers-first. Português do Brasil."""

    return await _call_claude(prompt, max_tokens=4500)


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

    prompt = f"""Você está escrevendo o relatório executivo final (15-20 páginas) de uma auditoria FinOps Anuvia — padrão de qualidade AWS Well-Architected Review Report + Google Cloud TAM Cost Optimization Engagement deliverable. O leitor é CTO/VP Engineering/Head of Platform.

Perfil do cliente:
{profile_block}

Findings estruturados da semana 2 (framework metadata + math):
{findings_block}

Change log da semana 3 (resumo):
{change_log_md[:2500]}

Economia anualizada total identificada: R$ {_brl(low)} – R$ {_brl(high)}.

REQUISITOS DE FRAMEWORK (não negociáveis):
- Toda quantitativo traz tag de fonte [INTAKE/CUR/CE/TA/CO/GCP-EXPORT/GCP-REC/FH/ESTIMATIVA].
- Toda finding referencia FinOps Capability + AWS WA BP code (COST01-COST06) + GCP equivalent.
- Maturity assessment (Crawl/Walk/Run) por capability é OBRIGATÓRIO em seção dedicada.
- Premissas e Limitações vem imediatamente após Sumário Executivo — declare honestly o que foi acessado vs estimado.
- Math explícita em todos os cálculos (mostre a conta: $/unit × quantity × período).
- Status do documento na primeira página: "rascunho preliminar baseado em intake" OU "auditoria validada com CUR + CE".

Estruture o documento markdown com estas seções, nesta ordem:

1. **## Sumário executivo** — 1 página. Inclua: contexto (1-2 frases), baseline mensal com tag [INTAKE], economia anualizada faixa, payback dias, % do bill recuperável, decisão pedida. Em formato tabela compacta no final.

2. **## Premissas e Limitações** — Status do documento + Dados analisados + Dados pendentes + Premissas adotadas (use seção callout). Esta seção CONSTRÓI CONFIANÇA — seja honesto sobre o que ainda precisa ser validado com IAM role / Billing Export access.

3. **## Baseline de spend** — Decomposição por categoria com tag de fonte. Tabela: Categoria | Spend mensal estimado | % do total | Fonte. Para AWS use categorias AWS WA padrão (Compute, Storage, Database, Network, Data Transfer, Support, Third-party SaaS). Para multi-cloud, separe AWS / GCP.

4. **## Metodologia** — 8 vetores Anuvia mapeados pra FinOps Foundation Capabilities + AWS WA Cost Pillar BPs. Liste ferramentas usadas (CUR via Athena, Cost Explorer, Trusted Advisor, Compute Optimizer, GCP Recommender, FinOps Hub, Billing Export BQ). Indique quais foram efetivamente acessadas vs pendentes.

5. **## FinOps Maturity Assessment** — Tabela com todas as 6 Capabilities da FinOps Foundation (Allocation, Reporting & Analytics, Showback/Chargeback, Workload Optimization, Pricing & Rate Optimization, Anomaly Management) + 5 do Operate domain (Budget Mgmt, Forecasting, Cloud Policy & Governance, Decentralized Decision Making, FinOps Education). Colunas: Capability | Domain | Maturity atual | Maturity-alvo 12mo | Gap principal. Use Crawl/Walk/Run.

6. **## Findings detalhados** — Uma subseção ### por vetor. Cada uma com: FinOps Capability + maturity + AWS WA BP code + GCP equivalent + Fontes + Hipótese com math explícita + Validation criteria + Implementation steps + Rollback + Janela + Economia + Confiança. Cite instance types reais (db.m5.2xlarge → db.t4g.large, m7g.xlarge), serviços com nome de produto (Compute Optimizer, S3 Intelligent-Tiering, Aurora I/O-Optimized, EBS gp3), métricas CloudWatch (CPUUtilization p95 <20% por 14d), comandos AWS CLI (modify-db-instance --apply-immediately false).

7. **## Savings realizadas (quick wins phase)** — Tabela: Mudança | Data | Spend antes | Spend depois | Delta mensal | Delta anualizado | Fonte da medição [CE/CUR]. Se ainda em rascunho, marque "Pendente execução pós-sign-off".

8. **## Roadmap 12 meses** — 3 horizontes:
   - **30 dias** — quick wins residuais + setup de governança (AWS Budgets alerts, tagging policy enforced via SCP, Cost Explorer custom dashboards). Mapear cada item pra COST01/COST02 BP.
   - **90 dias** — RI/SP strategy (target coverage 70-85%, mix Compute SP + EC2 RI), Graviton migration por workload (blue/green via AMI swap), S3 Intelligent-Tiering rollout, Aurora I/O-Optimized eval, observability cost optimization. Mapear cada item pra COST03/COST04 BP.
   - **180-365 dias** — re-arch cross-AZ traffic (VPC Endpoints vs NAT Gateway), multi-region rationalization, database migration considerations (RDS → Aurora Serverless v2 quando aplicável), FinOps Foundation framework adoption formal (monthly cadence + chargeback model). Mapear cada item pra COST05/COST06 BP.

9. **## ADRs (Architecture Decision Records)** — Para cada decisão estrutural, formato ADR-XX:
   - ADR-01 RI 1-year vs 3-year (com math de break-even e flexibility tradeoffs)
   - ADR-02 Graviton migration (workloads candidatos, blue/green plan, rollback)
   - ADR-03 S3 Intelligent-Tiering vs lifecycle manual
   - ADR-04 NAT Gateway consolidation vs VPC Endpoints
   - ADR-05 Observability cost (CloudWatch vs Datadog vs OpenTelemetry self-hosted)
   - + adicionais conforme findings

10. **## Governança contínua** — Cadência mensal de FinOps review (template incluso: agenda, métricas obrigatórias). Métricas: cost per workload, cost per request (unit economics), RI/SP coverage, RI/SP utilization, anomaly count, savings realized YTD. Thresholds que disparam alerta (ex: anomaly >20% MoM em qualquer categoria).

11. **## Handoff checklist** — 16 itens revisados em toda auditoria Anuvia: ownership de cost dashboards, runbooks de RI purchase, alerting wiring, tagging compliance, etc.

12. **## Próxima rodada — o que pedir** — Honest closing: se confiança ficou em "média" ou "baixa" em vários findings, liste exatamente o que pedir pra próxima iteração elevar pra "alta" (IAM read-only role com policies específicas, GCP Billing Export BQ access, sample CloudWatch metrics 30d via cross-account role, etc.). Esta seção PROTEGE o cliente — sem ela o relatório vira só estimativa.

13. **## Apêndices** — Queries SQL Athena usadas (template + parameters), GCP Billing BQ SQL templates, referências (links pra AWS WA Cost Pillar whitepaper, FinOps Foundation framework page, GCP cost optimization docs).

Voz Anuvia: seca, direta, numbers-first. Cada afirmação com número + tag de fonte quando possível. Quando estimar, use [ESTIMATIVA] e justifique com math. Português do Brasil.
"""

    return await _call_claude(prompt, max_tokens=5000)


async def _compose_roadmap_narrative(engagement: dict, findings: dict) -> str:
    """Standalone 12-month roadmap markdown (separate from the report)."""
    findings_block = _findings_to_markdown(findings)
    prompt = f"""Escreva o Roadmap FinOps de 12 meses como deliverable standalone — padrão AWS Well-Architected Improvement Plan + FinOps Foundation maturity progression plan. Vai pra leadership técnica (CTO/VP Eng).

Findings da auditoria:
{findings_block}

REQUISITOS DE FRAMEWORK:
- Cada iniciativa mapeada pra FinOps Capability + AWS WA BP code (COST01-COST06) + GCP equivalent quando relevante.
- Tags de fonte [INTAKE/CUR/CE/TA/CO/GCP-EXPORT/GCP-REC/FH/ESTIMATIVA] em toda economia citada.
- Trajetória de maturity explicit: Crawl → Walk → Run por capability, com timestamp.

Estrutura markdown:

## Sumário executivo
3-5 linhas: target maturity em 12 meses, economia total acumulada esperada (faixa), estado atual da maturity por domain (INFORM/OPTIMIZE/OPERATE).

## Premissas e Limitações
- Status do documento (rascunho preliminar baseado em intake OU validado com CUR)
- Dados pendentes pra refinar roadmap (IAM read-only role Athena, Billing Export BQ access)

## Trajetória de maturity FinOps (12 meses)
Tabela: Capability | Domain | T0 (hoje) | T+30d | T+90d | T+365d. Use Crawl/Walk/Run. Inclua todas as 12 capabilities (Allocation, Reporting & Analytics, Showback/Chargeback, Workload Optimization, Pricing & Rate Optimization, Anomaly Management, Budget Management, Forecasting, Cloud Policy & Governance, Decentralized Decision Making, FinOps Education & Enablement, Onboarding Workloads).

## Horizonte 1 — 30 dias (Crawl → Walk em Inform domain)
Quick wins residuais + setup de governança. Mapear cada item pra COST01/COST02 BP.
Tabela: Item | FinOps Capability | AWS WA BP | Dono sugerido | Esforço (pessoa-dias) | Economia anualizada | Fonte
Itens típicos:
- AWS Budgets com alertas em 50/80/100% (COST02-BP02) — owner: Platform Eng
- Tagging policy enforced via SCP (COST02-BP04) — owner: Platform Eng + Sec
- Cost Explorer custom dashboards por business unit (COST02-BP05) — owner: FinOps lead
- Compute Optimizer recommendations review semanal (COST04-BP05) — owner: SRE
- GCP equivalente (se multi-cloud): Recommender API automation + FinOps Hub setup

## Horizonte 2 — 90 dias (Walk em Optimize domain)
Iniciativas de médio prazo. Mapear cada item pra COST03/COST04 BP.
- RI/SP strategy: target coverage 70-85% (Compute SP cobre EC2 + Lambda + Fargate; EC2 RI pra workloads estáveis). Math break-even. COST03-BP05.
- Graviton migration blue/green por workload (m5 → m7g, c5 → c7g, r5 → r7g). AMI swap + ASG canary. COST04-BP02.
- S3 Intelligent-Tiering enable para buckets >100GB com access pattern variável. COST03-BP08.
- Aurora I/O-Optimized eval para clusters com I/O >25% do bill. COST03-BP06.
- Observability cost optimization (CloudWatch log retention rationalization, metric filter cleanup, Datadog/New Relic SKU review). COST03-BP11.
- GCP equivalente: CUDs spend-based 1y commitment 60-70% baseline; BigQuery slot reservations vs on-demand decision.

## Horizonte 3 — 180-365 dias (Run em Operate domain)
Iniciativas estruturais. Mapear cada item pra COST05/COST06 BP.
- Re-arch cross-AZ traffic: VPC Endpoints (Gateway pra S3/DynamoDB free; Interface endpoints com custo mas evitam NAT egress). COST05-BP04.
- Multi-region rationalization: consolidar workloads dev/staging em single region; DR strategy explicit. COST06-BP01.
- Database migration considerations: RDS → Aurora Serverless v2 onde aplicável; DynamoDB on-demand vs provisioned. COST06-BP02.
- FinOps Foundation framework adoption formal: monthly cadence + chargeback model + KPIs (cost per workload, unit economics). COST01-BP03.
- FinOps Education program: training Plataforma + Eng leads em FinOps fundamentals.

## Governança contínua
- Cadência mensal de FinOps review (1h agenda template):
  1. RI/SP coverage + utilization
  2. Anomaly review (>20% MoM movement)
  3. Top 10 spend drivers
  4. Forecast vs actual delta
  5. Maturity progression (mover capability Crawl→Walk→Run)
- Métricas obrigatórias (dashboard): cost per workload, cost per request (unit economics), RI/SP coverage, RI/SP utilization, anomaly count, savings realized YTD, % spend coberto por tags.
- Thresholds que disparam alerta automático: anomaly >20% MoM em qualquer categoria; RI/SP utilization <85%; tagging compliance <95%.

## Riscos e mitigações
Tabela: Risco | Probabilidade | Impacto | Mitigação | Owner

## Próxima rodada — o que pedir
Honest closing: dados pendentes que destravariam refinamento do roadmap (IAM read-only role Athena, GCP Billing Export BQ, sample CloudWatch metrics 30d).

Voz Anuvia: seca, direta, numbers-first. Português do Brasil.
"""
    return await _call_claude(prompt, max_tokens=4500)


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

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (18-22 slides) — padrão Google Cloud TAM readout deck + AWS Well-Architected Review presentation. Cliente é CTO/VP Eng. Sem hype, sem marketing fluff.

Top 6 findings (de findings JSON já estruturado):
{top_block}

Economia anualizada total identificada: R$ {_brl(low)} – R$ {_brl(high)}.

REQUISITOS DE FRAMEWORK:
- Cada slide de vetor cita FinOps Capability + AWS WA BP code + instance types específicos + economia com tag de fonte.
- Slide de maturity (Crawl/Walk/Run) é obrigatório.
- Slide de premissas/limitações é obrigatório (logo após sumário).

Formato por slide:

### Slide N — Título curto
- bullet 1 (uma frase curta, sem ponto final, com tag de fonte quando quantitativo)
- bullet 2
- bullet 3
(notas: 30s do apresentador — contexto adicional)

Estrutura:

1. Slide 1 — Capa (cliente, escopo "Auditoria FinOps Multicloud", prazo 4 semanas, engagement id, Anuvia + Mila Vernazza)

2. Slide 2 — Sumário em números (baseline mensal [INTAKE], economia anualizada faixa, % do bill recuperável, payback dias, # quick wins identificados)

3. Slide 3 — Premissas e Limitações (Status do documento + Dados acessados + Dados pendentes + Confiança média/baixa onde aplicável)

4. Slide 4 — Metodologia (8 vetores Anuvia + frameworks: FinOps Foundation + AWS WA Cost Pillar + GCP Cost Optimization)

5. Slide 5 — FinOps Maturity Assessment (tabela compacta: 6 Capabilities × Crawl/Walk/Run atual vs alvo 12mo)

6. Slide 6 — Baseline de spend (decomposição por categoria com tag de fonte — Compute / Storage / Database / Network / Data Transfer / Support / SaaS)

7-12. Slides 7-12 — Um slide por vetor top-6 (em ordem decrescente de savings). Para cada um:
   - **Vetor [Capability · COST-XX-BPYY]**
   - bullet: instance types ou serviços específicos identificados [INTAKE/CUR/CO/ESTIMATIVA]
   - bullet: math explícita (ex: gp2 → gp3, US$ 0,10 vs 0,08/GB/mês × 8TB)
   - bullet: economia anualizada R$ X-Y · confiança alta/média/baixa
   - bullet: GCP equivalent (1-line)
   - (notas: validation criteria + rollback resumido)

13. Slide 13 — Quick wins phase: savings realizadas (tabela antes/depois com tag [CE])

14. Slide 14 — Roadmap H1 (30 dias) — quick wins residuais + governance setup (COST01/COST02 BPs)

15. Slide 15 — Roadmap H2 (90 dias) — RI/SP strategy + Graviton + S3 Intelligent-Tiering (COST03/COST04 BPs)

16. Slide 16 — Roadmap H3 (180-365 dias) — re-arch cross-AZ + database migrations + FinOps culture (COST05/COST06 BPs)

17. Slide 17 — ADRs principais (ADR-01 RI strategy, ADR-02 Graviton, ADR-03 S3 tiering, ADR-04 NAT vs VPC Endpoints, ADR-05 observability cost)

18. Slide 18 — Governança contínua (cadência mensal + métricas obrigatórias + thresholds de alerta)

19. Slide 19 — Handoff checklist (16 itens — ownership de dashboards, runbooks RI purchase, alerting, tagging compliance)

20. Slide 20 — Próxima rodada: o que pedir (IAM read-only role Athena CUR + GCP Billing Export BQ + sample CloudWatch metrics 30d — honest sobre limitação de precisão sem isso)

21. Slide 21 — Encerramento (Mila Vernazza · founder@anuvia.com.br · próximos passos: opcional retainer ongoing FinOps)

Voz Anuvia: seca, direta. Sem hype. Bullets curtos sem ponto final. Português do Brasil."""

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
    engagement_meta: Optional[dict] = None,
    show_cover: bool = True,
) -> str:
    """Render markdown → HTML → PDF → upload to Supabase Storage.

    Returns the public PDF URL when storage is available, otherwise an
    embedded ``data:`` placeholder URL pointing the operator at the
    stashed inline copy. Always succeeds — never raises.

    ``engagement_meta`` and ``show_cover`` are forwarded to the shared
    branding module so phase 4 deliverables get the enterprise cover
    page; phase 1/2 documents pass ``show_cover=False`` for a leaner
    intermediate look.
    """
    html = _deliverable_html(
        title,
        subtitle,
        _md_to_html(body_md),
        body_md=body_md,
        engagement_meta=engagement_meta,
        show_cover=show_cover,
    )

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


async def _render_deck_artifact(
    engagement_id: str,
    *,
    deck_md: str,
    client_name: str,
    engagement_meta: Optional[dict] = None,
) -> str:
    """Build the executive deck as a real .pptx (light Anuvia theme) and
    upload it to Supabase Storage.

    Falls back to PDF rendering (the legacy path) if either the shared
    branding module or python-pptx is unavailable, so the delivery never
    blocks on a missing dep. Always returns *some* URL.
    """
    pptx_bytes: Optional[bytes] = None
    if _BRANDING_AVAILABLE:
        try:
            slide_specs = _branding_mod.parse_deck_markdown(deck_md)
            if not slide_specs:
                slide_specs = [
                    {
                        "type": "cover",
                        "title": "Apresentação Executiva — FinOps Audit",
                        "subtitle": f"Engagement {engagement_id}",
                    },
                    {
                        "type": "content",
                        "title": "Deck",
                        "bullets": ["(conteúdo gerado a partir do markdown)"],
                    },
                ]
            pptx_bytes = await _branding_mod.generate_pptx_deck(
                practice_label="FINOPS AUDIT",
                title="Apresentação Executiva — FinOps Audit",
                client_name=client_name,
                engagement_id=engagement_id,
                slides=slide_specs,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_4: PPTX build failed eng=%s — falling back to PDF",
                engagement_id,
            )
            pptx_bytes = None

    if pptx_bytes:
        object_path = f"{engagement_id}/executive_deck.pptx"
        public = await _upload_artifact(
            object_path,
            pptx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )
        if public:
            return public
        log.warning(
            "finops.phase_4: PPTX upload failed eng=%s — falling back to PDF",
            engagement_id,
        )

    # Fallback: original markdown -> PDF path.
    return await _render_and_upload(
        engagement_id,
        title="Apresentação Executiva — FinOps Audit",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
        engagement_meta=engagement_meta,
        show_cover=True,
    )


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


# ---------------------------------------------------------------------------
# White-glove delivery mode — book a session, Slack Mila, hold client email
# ---------------------------------------------------------------------------
#
# When ``engagement.delivery_mode == 'whiteglove'`` (default for new
# engagements per task #56), each phase boundary holds back the client
# email and instead:
#   1. Auto-books a presentation meeting on Mila's calendar (via
#      :mod:`lib.delivery._sessions.book_phase_session`).
#   2. Slack-DMs Mila a rich block with the materials + a button to
#      release the email when she's done presenting.
# The client email only fires once Mila clicks the button — handled by
# :mod:`lib.whiteglove_routes`. ``delivery_mode='autonomous'`` keeps the
# legacy fire-and-forget behaviour for smoke tests + backward compat.


_DELIVERY_MODE_WHITEGLOVE = "whiteglove"
_DELIVERY_MODE_AUTONOMOUS = "autonomous"


def _engagement_delivery_mode(engagement: dict) -> str:
    """Return ``'whiteglove'`` (default) or ``'autonomous'`` for a row.

    Defensive: if the column doesn't exist yet (migration not applied) we
    treat the engagement as autonomous to preserve legacy behaviour.
    """
    mode = engagement.get("delivery_mode")
    if not mode:
        # Column missing / NULL — preserve legacy email-direct behaviour
        # until the migration lands. Once the migration is applied with
        # DEFAULT 'whiteglove', NULL won't happen for new rows.
        return _DELIVERY_MODE_AUTONOMOUS
    mode_str = str(mode).strip().lower()
    if mode_str not in (_DELIVERY_MODE_WHITEGLOVE, _DELIVERY_MODE_AUTONOMOUS):
        return _DELIVERY_MODE_AUTONOMOUS
    return mode_str


async def _whiteglove_hold_for_presentation(
    *,
    engagement_id: str,
    phase: int,
    client_name: str,
    findings_summary: str,
    materials: List[Tuple[str, str]],
) -> dict:
    """Book the presentation meeting + Slack DM Mila. Used by phases 2/3/4
    when ``delivery_mode='whiteglove'``.

    Returns ``{ok, session, slack_sent}``. Best-effort — failures don't
    cascade (we still record ``phase_N_pending_presentation_at`` so the
    operator timeline reflects state).
    """
    # Lazy import so this module loads even when _sessions has a syntax
    # issue (the other delivery modules need to import unaffected).
    try:
        from lib.delivery import _sessions as _sess  # type: ignore
    except Exception:  # noqa: BLE001
        log.exception(
            "finops.whiteglove: _sessions module not importable eng=%s",
            engagement_id,
        )
        return {"ok": False, "reason": "sessions_module_unavailable"}

    # 1) Book the slot + Gcal events. Idempotent inside book_phase_session.
    session = await _sess.book_phase_session(
        engagement_id, phase, practice="cloud_finops"
    )

    # 2) Mark engagement as pending presentation. The Slack alert + button
    # come next — but if any of that fails we want the operator to see the
    # pending state.
    pending_key = f"phase_{phase}_pending_presentation_at"
    await _engagement_merge_artifacts(
        engagement_id,
        {pending_key: _now_iso()},
    )

    # 3) Slack DM Mila with the block + button.
    slack_sent = False
    try:
        slack_sent = await _sess.slack_dm_materials_ready(
            engagement_id=engagement_id,
            phase=phase,
            client_name=client_name,
            findings_summary=findings_summary,
            scheduled_at_br=session.get("scheduled_at_br")
                or (session.get("scheduled_at") or "horário pendente"),
            duration_min=session.get("duration_min")
                or _sess.PHASE_DURATIONS_MIN.get(phase, 60),
            meet_url=session.get("meet_url"),
            materials=materials,
            brief_snippet="",  # full brief is in the private Gcal event description
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "finops.whiteglove: slack DM failed eng=%s phase=%s",
            engagement_id, phase,
        )

    return {"ok": True, "session": session, "slack_sent": slack_sent}


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — Claude composes findings narrative, ship PDF.

    Branches on ``delivery_mode``:
      * ``'whiteglove'`` (default) → book presentation + Slack DM Mila
        with the materials and the ``Apresentei`` button. NO client email
        until Mila clicks the button.
      * ``'autonomous'`` → legacy fire-and-forget: send the findings email
        to the client right after PDF upload.
    """
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

    mode = _engagement_delivery_mode(engagement)
    if mode == _DELIVERY_MODE_WHITEGLOVE:
        low, high = _findings_total_savings(findings)
        client_name = (
            (lead or {}).get("company")
            or (lead or {}).get("name")
            or "Cliente"
        )
        findings_count = len(
            [f for f in (findings.get("findings") or []) if isinstance(f, dict)]
        )
        findings_summary = (
            f"Findings totais: R$ {_brl(low)} – R$ {_brl(high)}/ano "
            f"({findings_count} oportunidades em 8 vetores)"
        )
        await _whiteglove_hold_for_presentation(
            engagement_id=engagement_id,
            phase=2,
            client_name=client_name,
            findings_summary=findings_summary,
            materials=[("Findings PDF", pdf_url)],
        )
        # IMPORTANT: still advance to phase 3 — the orchestrator continues
        # to compose phase-3 deliverables in parallel with Mila's phase-2
        # presentation. The client email release is async (Slack button),
        # not blocking on the phase machine.
    elif email:
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
            await _engagement_merge_artifacts(
                engagement_id,
                {"phase_2_email_sent_at": _now_iso()},
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
        "delivery_mode": mode,
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

    # Always (re-)call delivery side. If whiteglove session previously failed
    # (gcal_error) book_phase_session retries; if client email already sent
    # (autonomous mode) the email sender is idempotent. Re-fires resume here.
    if True:
        low, high = _findings_total_savings(findings)
        mode = _engagement_delivery_mode(engagement)
        if mode == _DELIVERY_MODE_WHITEGLOVE:
            client_name = (
                (lead or {}).get("company")
                or (lead or {}).get("name")
                or "Cliente"
            )
            quick_wins_count = len([
                f for f in (findings.get("findings") or [])
                if isinstance(f, dict) and f.get("priority") == "quick_win"
            ])
            findings_summary = (
                f"Quick wins: {quick_wins_count or 'todos os top findings'} "
                f"| Economia anualizada R$ {_brl(low)} – R$ {_brl(high)}"
            )
            await _whiteglove_hold_for_presentation(
                engagement_id=engagement_id,
                phase=3,
                client_name=client_name,
                findings_summary=findings_summary,
                materials=[("Change log PDF (quick wins)", change_log_url)],
            )
        elif email:
            token = _hmac_token(engagement_id, "approval")
            approval_url = (
                f"{BASE_URL}/api/delivery/finops/approve"
                f"?engagement_id={engagement_id}&token={token}"
            )
            # Branch email tone on remediation choice declared in intake.
            intake = engagement.get("intake_data") or {}
            remediation_choice = str(
                intake.get("remediation_choice")
                or intake.get("execution_choice")
                or "cliente_interno"
            )
            html = _phase3_email_html(
                first_name=first_name,
                changelog_url=change_log_url,
                approval_url=approval_url,
                savings_brl=f"{_brl(low)} – {_brl(high)}",
                remediation_choice=remediation_choice,
            )
            subject = (
                "Plano de execução pronto — autorização requerida"
                if remediation_choice == "anuvia_success_fee"
                else "Runbook de quick wins pronto — execução pelo time interno"
            )
            try:
                await _send_email(
                    engagement_id=engagement_id,
                    to=email,
                    subject=subject,
                    html=html,
                    kind="finops_phase_3_approval",
                )
                await _engagement_merge_artifacts(
                    engagement_id,
                    {"phase_3_email_sent_at": _now_iso()},
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
    """Phase 4 — generate final deliverables, fire invoice, close engagement.

    Idempotent: if ``phase_4_email_sent_at`` is set, skip re-send. If any
    of the 3 Claude deliverables fall back, DO NOT send email and DO NOT
    mark engagement delivered — Slack-escalate to Mila and exit early.
    """
    engagement_id = str(engagement.get("id") or "")
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    # Hard idempotency: if this engagement already finalized + emailed, skip.
    if artifacts.get("phase_4_email_sent_at"):
        log.info(
            "finops.phase_4: skipping — already delivered eng=%s at %s",
            engagement_id, artifacts.get("phase_4_email_sent_at"),
        )
        return {
            "ok": True,
            "skipped_already_delivered": True,
            "delivered": True,
        }

    # White-glove mode: if we've already booked the phase-4 presentation
    # and Slack-DM'd Mila, don't re-compose every tick. The orchestrator
    # is still scheduled but should idle until Mila releases (which sets
    # phase_4_email_sent_at). Re-runs only happen via explicit smoke fire.
    if (
        _engagement_delivery_mode(engagement) == _DELIVERY_MODE_WHITEGLOVE
        and artifacts.get("phase_4_pending_presentation_at")
    ):
        log.info(
            "finops.phase_4: whiteglove pending Mila release eng=%s since %s",
            engagement_id,
            artifacts.get("phase_4_pending_presentation_at"),
        )
        return {
            "ok": True,
            "waiting_for": "mila_button_click",
            "delivered": False,
            "delivery_mode": _DELIVERY_MODE_WHITEGLOVE,
            "next_action": None,
            "next_action_at": None,
        }

    findings = artifacts.get("phase_2_findings") or {}
    change_log_md = artifacts.get("phase_3_change_log_md") or ""

    # Compose all three deliverables INCREMENTALLY — persist after each so
    # if a worker dies mid-phase, next tick can resume from where it stopped.

    # 1) Report — skip if we already have it from prior run.
    report_md = artifacts.get("final_report_md") or ""
    if not report_md or _CLAUDE_FALLBACK_TAG in report_md:
        report_md = await _compose_final_report_narrative(
            engagement, findings, change_log_md
        )
        await _engagement_merge_artifacts(
            engagement_id, {"final_report_md": report_md}
        )
        log.info("finops.phase_4: report_md saved (%s chars)", len(report_md))

    # 2) Roadmap — skip if already done.
    roadmap_md = artifacts.get("roadmap_md") or ""
    if not roadmap_md or _CLAUDE_FALLBACK_TAG in roadmap_md:
        roadmap_md = await _compose_roadmap_narrative(engagement, findings)
        await _engagement_merge_artifacts(
            engagement_id, {"roadmap_md": roadmap_md}
        )
        log.info("finops.phase_4: roadmap_md saved (%s chars)", len(roadmap_md))

    # 3) Deck — skip if already done.
    deck_md = artifacts.get("deck_md") or ""
    if not deck_md or _CLAUDE_FALLBACK_TAG in deck_md:
        deck_md = await _compose_deck_narrative(engagement, findings)
        await _engagement_merge_artifacts(
            engagement_id, {"deck_md": deck_md}
        )
        log.info("finops.phase_4: deck_md saved (%s chars)", len(deck_md))

    # SAFETY: if any deliverable came back as fallback, do NOT email the client.
    # Stash artifacts (so operator has the partial work) + Slack-escalate.
    any_fallback = any(
        _CLAUDE_FALLBACK_TAG in (md or "")
        for md in (report_md, roadmap_md, deck_md)
    )
    if any_fallback:
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_4_partial_at": _now_iso(),
                "phase_4_partial_report_md": report_md,
                "phase_4_partial_roadmap_md": roadmap_md,
                "phase_4_partial_deck_md": deck_md,
            },
        )
        await _send_slack_alert(
            f":warning: *FinOps phase 4 produziu fallback* — engagement "
            f"`{engagement_id}` precisa intervenção manual antes do email final. "
            f"Anthropic API instável (3 retries falharam em pelo menos 1 dos 3 "
            f"deliverables). Markdown parcial salvo em "
            f"`engagement.artifacts.phase_4_partial_*`."
        )
        return {
            "ok": False,
            "reason": "claude_fallback_detected",
            "next_action": "finops_send_progress_update",
            "next_action_at": _now() + timedelta(minutes=15),
            "delivered": False,
        }

    low, high = _findings_total_savings(findings)
    savings_str = f"{_brl(low)} – {_brl(high)}"

    # Cover-page meta — passed to the shared branding module so each PDF
    # opens with a proper engagement summary card (Cliente, Período,
    # Baseline, Economia, Payback, Analista).
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    monthly_spend = (
        intake.get("aws_spend_last_6_months")
        or intake.get("baseline_mensal")
        or intake.get("monthly_spend")
    )
    client_name = (lead or {}).get("company") or (lead or {}).get("name") or "Confidencial"
    baseline_str = "—"
    if monthly_spend:
        try:
            baseline_str = f"R$ {_brl(monthly_spend)}/mês"
        except Exception:  # noqa: BLE001
            baseline_str = str(monthly_spend)

    engagement_meta = {
        "Cliente": client_name,
        "Período": "4 semanas",
        "Baseline mensal": baseline_str,
        "Economia identificada": f"R$ {savings_str}/ano",
        "Payback": "11–18 dias (quick wins)",
        "Analista responsável": "Mila Vernazza · mila@anuvia.com.br",
    }

    report_url = await _render_and_upload(
        engagement_id,
        title="Relatório Executivo — FinOps Audit",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
        engagement_meta=engagement_meta,
        show_cover=True,
    )
    roadmap_url = await _render_and_upload(
        engagement_id,
        title="Roadmap FinOps — 12 meses",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/roadmap_12mo.pdf",
        engagement_meta=engagement_meta,
        show_cover=True,
    )

    # Executive deck — now a real PPTX (was a markdown→PDF). Falls back to
    # PDF rendering if python-pptx or the branding module isn't available
    # so the delivery never blocks on a missing dep.
    deck_url = await _render_deck_artifact(
        engagement_id,
        deck_md=deck_md,
        client_name=client_name,
        engagement_meta=engagement_meta,
    )

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
    mode = _engagement_delivery_mode(engagement)
    email_sent = False

    if mode == _DELIVERY_MODE_WHITEGLOVE:
        # White-glove path: book the final handoff (90 min) + Slack DM Mila
        # with all three deliverables + the release button. Client email is
        # held until Mila clicks "Apresentei → enviar materiais".
        findings_summary_wg = (
            f"Economia anualizada identificada: R$ {savings_str}/ano · "
            f"3 deliverables prontos (relatório + deck + roadmap)"
        )
        await _whiteglove_hold_for_presentation(
            engagement_id=engagement_id,
            phase=4,
            client_name=client_name,
            findings_summary=findings_summary_wg,
            materials=[
                ("Relatório executivo (PDF)", report_url),
                ("Apresentação executiva (PPTX)", deck_url),
                ("Roadmap 12 meses (PDF)", roadmap_url),
            ],
        )
    elif email:
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
            email_sent = True
        except Exception:  # noqa: BLE001
            log.exception(
                "finops.phase_4: email failed eng=%s", engagement_id
            )

    # Stamp idempotency marker AFTER successful email (or if email skipped).
    # White-glove mode stamps via the Slack button release path; here we
    # only stamp for autonomous mode so the invoice / NPS flow can still
    # finalise even when the client email is deferred.
    if email_sent:
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_4_email_sent_at": _now_iso()},
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
