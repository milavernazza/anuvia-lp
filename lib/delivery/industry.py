"""Industry Assessment + Vertical Pilot — autonomous delivery agent.

Owns the post-signature delivery flow for the ``industry`` practice
(R$ 35-70k depending on vertical, 4-6 weeks). Hands off from
``lib.contract`` once a contract is signed and paid, then runs three
phase groups — Discovery & Compliance Mapping, PoV Design & Build,
and Validation & Roadmap — producing client deliverables and emails
along the way.

The free diagnostic (90-second LP form) is owned by the LP intake +
qualifier pipeline and is NOT this module's responsibility. This
module only handles the paid Vertical Pilot.

Architecture::

    contract.webhook (paid)
        |
        v
    industry_kickoff                              [+0]
        |   (intake form + vertical playbook fit sent)
        v
    industry_phase_1_discovery                    [+1 day]
        |   (intake submitted → compliance posture + data inventory)
        v
    industry_phase_2_pov                          [+2 weeks]
        |   (PoV scope + implementation plan + eval framework)
        v
    industry_phase_3_validation                   [+2 weeks]
        |   (PoV results + production roadmap + final report + deck)
        v
    status='delivered', next_action=None

Quality bar mirrors ``lib/delivery/ai_readiness.py`` with these
vertical-specific additions:
  * The ``vertical`` field on ``intake_data`` MUST resolve to one of
    the five supported playbooks (manufacturing, logistics, healthcare,
    life_sciences, finserv). Anything else triggers a Slack escalation.
  * The effective ticket is computed at kickoff from the vertical's
    ``ticket_range`` midpoint and persists on ``engagement.total_value_brl``
    (unless contract.py already populated a different number).
  * Every Claude prompt injects the vertical-specific compliance
    constraints + typical cases + stakeholder set.
  * For ``life_sciences``, every deliverable carries a "GxP validation
    considerations" appendix (IQ/OQ/PQ + 21 CFR Part 11 traceability).
  * Brand voice enforced in every Claude prompt (dry, numbers-first,
    anti-hype, compliance-aware). See ``_BRAND_SYSTEM_PROMPT``.
  * All writes are append-only or idempotent.
  * Graceful degradation on missing Claude/Resend/Storage keys (same
    fallback machinery as ``ai_readiness``).
  * HMAC-tokened client links use ``CONTRACT_HMAC_SECRET``.
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

log = logging.getLogger("anuvia-lp.delivery.industry")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

#: Default ticket size for this practice. Midpoint of the entire R$ 35-70k
#: band — used as a fallback when no vertical is resolved. In practice
#: ``kickoff`` overrides this with the vertical-specific midpoint.
PRACTICE_TICKET_BRL: int = 50000

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

# 3 phase groups × 2 weeks each = 6 weeks total. Each "phase" advances
# 2 weeks. For 4-week verticals we still walk all three phases but the
# operator may compress the cadence manually.
_PHASE_INTERVAL = timedelta(days=14)
_INTAKE_REMINDER_AFTER = timedelta(days=5)
_HTTP_TIMEOUT = 30.0

# Brand voice — pinned to every Claude system prompt. Compliance-aware
# instruction is non-optional because every vertical here is regulated.
_BRAND_SYSTEM_PROMPT = (
    "Você está escrevendo em nome de Mila Vernazza, founder da Anuvia "
    "(consultoria sênior de cloud + IA, ex-AWS Solutions Architect, "
    "ex-Google, ex-MongoDB). Voz: seca, direta, anti-hype, primeiro os "
    "números, depois a narrativa. Frases curtas declarativas misturadas "
    "com cadeias causa-efeito mais longas. Use o léxico: vazamento, "
    "clareza, diagnóstico, processo, padrão, sobreviver em produção, "
    "gate de saída, evidência, PoV, success criteria, IQ/OQ/PQ. Evite: "
    "sinergia, transformação, leverage, magia, mágico, IA generativa que "
    "muda o jogo, revolucionar.\n\n"
    "REGRAS DE PROFUNDIDADE TÉCNICA (não negociáveis):\n"
    "1. Cite stack vertical-específico por nome. Manufacturing: PLC streams "
    "via OPC-UA, MQTT broker, SCADA, MES integration (SAP ME, Siemens "
    "Opcenter), historian (PI System, Wonderware). Healthcare: FHIR R4, "
    "HL7 v2.x, DICOM, RIS/PACS. Life sciences: IQ/OQ/PQ protocols, eCTD "
    "submissions, LIMS (LabWare, STARLIMS). Finserv: BACEN STR/SCR "
    "reports, SISBACEN, FEBRABAN MIG. Nunca dizer 'integrações' genéricas.\n"
    "2. Cite frameworks de compliance com nome + artigo/cláusula. "
    "Healthcare BR: LGPD art. 11 (dados sensíveis de saúde), ANS RN 305, "
    "ANVISA RDC 657. Life sciences: ANVISA RDC 430, FDA 21 CFR Part 11 "
    "(audit trail, e-signatures), GxP (GLP/GMP/GCP), EU Annex 11. "
    "Finserv: BACEN Res. 4.658 (cibersegurança), Circular 3.978 (PLD/FT), "
    "LGPD art. 7º X (proteção ao crédito). Manufacturing/QMS: ISO 9001, "
    "ISO 27001, IATF 16949. EUA: HIPAA (PHI), SOC 2 Type II. Nunca dizer "
    "'compliance regulatório' sem o nome.\n"
    "3. Para cada caso, defina requisitos de validação vertical-específicos. "
    "Life sciences: protocolo IQ/OQ/PQ + computer system validation (CSV) "
    "+ audit trail imutável. Healthcare: anonimização (k-anonymity ≥5), "
    "consentimento granular LGPD. Finserv: logs com retenção 5 anos, "
    "segregação BACEN. Manufacturing: rastreabilidade lote-a-lote.\n"
    "4. Cite tooling com posture build vs buy: Anthropic API direct, AWS "
    "Bedrock (sovereign region sa-east-1), Azure OpenAI (Brazil South), "
    "fine-tune open weights (Llama 3.1 70B self-hosted em GPU H100). "
    "Justificar: latência, sovereignty, custo/1M tokens, lock-in.\n"
    "5. Use números DO INTAKE do cliente. Se intake diz 200k transações/dia, "
    "1.2TB dados de produção/mês, 50 SKUs ativos, todos os ganhos derivam "
    "disso. Não inventar baseline.\n"
    "6. Math explícita de ROI: PoV ($25-40k, 6-8 sem) → success criteria "
    "(precision ≥0.85, recall ≥0.80, latência p95 <500ms, throughput "
    "≥X/dia) → production roadmap (Y meses, $Z capex) → savings/receita "
    "anual em R$ comparado com baseline. Mostre a conta.\n"
    "7. PoV success criteria framework obrigatório por caso: dataset "
    "(n mínimo + composição + holdout), métricas primárias e secundárias "
    "com threshold numérico, gate para go/no-go production, plano de "
    "rollback se falhar.\n"
    "8. Vendor lock-in assessment para cada decisão: data egress cost, "
    "model portability (ONNX, GGUF, vLLM), data residency, sovereignty "
    "(em BR: sa-east-1 vs us-east-1 com transferência internacional + "
    "cláusula contratual LGPD art. 33).\n"
    "9. ADRs em formato ADR-XX: ADR-01 (cloud region + sovereignty), "
    "ADR-02 (model + framework de validação), ADR-03 (audit trail "
    "architecture), ADR-04 (data classification + retention), etc.\n"
    "10. Quando estimar, use 'estimativa' uma vez só. NÃO repita 'padrão "
    "setorial' como muleta — isso é tique de junior. Nunca prometa o que "
    "não pode ser medido. Português do Brasil."
)

#: Sentinel prefix for narrative that Claude could not generate.
_CLAUDE_FALLBACK_TAG = "[CLAUDE_UNAVAILABLE_DRAFT]"


# ---------------------------------------------------------------------------
# Vertical playbooks — single source of truth.
#
# Each playbook drives:
#  * ticket midpoint at kickoff (range[0] + range[1]) / 2
#  * typical cases injected into Phase 2 PoV scoping prompt
#  * compliance frames injected into every deliverable
#  * default stakeholder set surfaced in kickoff email
#  * data inventory checklist used by Phase 1
# ---------------------------------------------------------------------------

_VERTICAL_PLAYBOOKS: Dict[str, Dict[str, Any]] = {
    "manufacturing": {
        "label": "Manufacturing",
        "ticket_range": (45000, 65000),
        "typical_cases": [
            "OEE optimization",
            "predictive maintenance from PLC/sensor streams",
            "computer-vision quality inspection",
            "MES/ERP integration",
        ],
        "compliance": ["ISO 27001", "ISO 9001"],
        "stakeholders": [
            "Plant Manager",
            "Quality Manager",
            "IT Director",
        ],
        "data_inputs": [
            "PLC sensor streams",
            "MES logs",
            "ERP transactions",
            "quality inspection records",
        ],
    },
    "logistics": {
        "label": "Logistics",
        "ticket_range": (40000, 55000),
        "typical_cases": [
            "fleet telemetry analytics",
            "ML route/ETA optimization",
            "last-mile tracking",
            "offline-first sync",
        ],
        "compliance": ["LGPD"],
        "stakeholders": [
            "Logistics Director",
            "Fleet Manager",
            "IT/Tech Lead",
        ],
        "data_inputs": [
            "telematics data",
            "GPS tracking logs",
            "delivery records",
            "fleet maintenance",
        ],
    },
    "healthcare": {
        "label": "Healthcare",
        "ticket_range": (45000, 65000),
        "typical_cases": [
            "clinical documentation assistants",
            "RAG over institutional protocols",
            "intake triage",
            "discharge summarization",
        ],
        "compliance": ["LGPD-saúde", "HIPAA (if US)", "ANS (BR)"],
        "stakeholders": [
            "Medical Director",
            "CMO",
            "DPO",
            "IT/Tech Lead",
        ],
        "data_inputs": [
            "EHR access (anonymized)",
            "clinical protocols",
            "intake forms",
            "patient interaction logs",
        ],
    },
    "life_sciences": {
        "label": "Life Sciences",
        # Premium due to GxP overhead.
        "ticket_range": (50000, 70000),
        "typical_cases": [
            "SOP automation",
            "regulatory document drafting",
            "GxP validation packages (IQ/OQ/PQ)",
            "deviation reports",
        ],
        "compliance": ["ANVISA/FDA GxP", "21 CFR Part 11"],
        "stakeholders": [
            "Quality Assurance Director",
            "Regulatory Affairs",
            "Manufacturing Director",
            "Validation Lead",
        ],
        "data_inputs": [
            "SOPs current",
            "regulatory submissions history",
            "batch records",
            "deviation logs",
        ],
    },
    "finserv": {
        "label": "Financial Services",
        "ticket_range": (50000, 70000),
        "typical_cases": [
            "real-time fraud detection",
            "AML monitoring",
            "KYC onboarding",
            "RAG over BACEN normativos",
        ],
        "compliance": ["BACEN 4.658", "LGPD", "SOC 2"],
        "stakeholders": [
            "Compliance Officer",
            "Chief Risk Officer",
            "Head of Operations",
            "CTO",
        ],
        "data_inputs": [
            "transaction logs",
            "customer onboarding records",
            "fraud incident history",
            "regulatory submissions",
        ],
    },
}

_SUPPORTED_VERTICALS: Tuple[str, ...] = tuple(_VERTICAL_PLAYBOOKS.keys())


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
    """Format a numeric as Brazilian currency: 50.000,00."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0,00"
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hmac_token(engagement_id: str, purpose: str = "intake") -> str:
    """HMAC-SHA256 token for a client-facing link."""
    if not _HMAC_SECRET:
        log.warning(
            "industry: HMAC secret unset; client links will be unverifiable"
        )
        return ""
    msg = f"{engagement_id}:{purpose}".encode("utf-8")
    return hmac.new(_HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _verify_token(engagement_id: str, purpose: str, token: str) -> bool:
    """Constant-time verify for the HMAC tokens. Never raises."""
    if not engagement_id or not token:
        return False
    expected = _hmac_token(engagement_id, purpose)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def _normalize_vertical(raw: Any) -> Optional[str]:
    """Normalize a free-form vertical string to a playbook key.

    Returns ``None`` when the input cannot be resolved — caller is
    expected to escalate to Slack.
    """
    if not raw:
        return None
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if key in _VERTICAL_PLAYBOOKS:
        return key
    # Common aliases.
    aliases = {
        "manuf": "manufacturing",
        "factory": "manufacturing",
        "industria": "manufacturing",
        "indústria": "manufacturing",
        "logistica": "logistics",
        "logística": "logistics",
        "supply_chain": "logistics",
        "supplychain": "logistics",
        "fleet": "logistics",
        "health": "healthcare",
        "healthtech": "healthcare",
        "hospital": "healthcare",
        "clinical": "healthcare",
        "saude": "healthcare",
        "saúde": "healthcare",
        "lifesciences": "life_sciences",
        "pharma": "life_sciences",
        "pharmaceutical": "life_sciences",
        "biotech": "life_sciences",
        "farma": "life_sciences",
        "finance": "finserv",
        "fintech": "finserv",
        "banking": "finserv",
        "financial": "finserv",
        "banco": "finserv",
        "financeiro": "finserv",
    }
    return aliases.get(key)


def _vertical_midpoint(vertical: str) -> int:
    """Return ticket midpoint in R$ for a vertical. Falls back to default."""
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    low, high = pb.get("ticket_range") or (PRACTICE_TICKET_BRL, PRACTICE_TICKET_BRL)
    try:
        return int((int(low) + int(high)) / 2)
    except (TypeError, ValueError):
        return PRACTICE_TICKET_BRL


def _vertical_label(vertical: str) -> str:
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    return str(pb.get("label") or vertical.title())


def _compliance_frame_block(vertical: str) -> str:
    """Render compliance frames for prompt injection."""
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    frames = pb.get("compliance") or []
    if not frames:
        return "nenhuma específica"
    return ", ".join(frames)


def _is_gxp_vertical(vertical: str) -> bool:
    """Whether to append GxP validation considerations to all deliverables."""
    return vertical == "life_sciences"


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
            "industry: engagement_get network failed id=%s", engagement_id
        )
        return None
    if r.status_code != 200:
        log.warning(
            "industry: engagement_get non-200 id=%s: %s %s",
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
            "industry: engagement_patch network failed id=%s",
            engagement_id,
        )
        return False
    if r.status_code not in (200, 204):
        log.warning(
            "industry: engagement_patch non-2xx id=%s: %s %s",
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
            "industry: merge_artifacts: engagement %s not found",
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
        log.warning("industry: storage upload failed path=%s: %s", path, exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "industry: storage upload non-2xx path=%s status=%s body=%s",
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
    """Render ``html`` to PDF bytes via Gotenberg."""
    gotenberg = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
    endpoint = f"{gotenberg}/forms/chromium/convert/html"
    try:
        files = {"files": ("index.html", html.encode("utf-8"), "text/html")}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(endpoint, files=files)
    except Exception as exc:  # noqa: BLE001
        log.warning("industry: gotenberg call failed: %s", exc)
        return None
    if r.status_code != 200:
        log.warning(
            "industry: gotenberg non-200 status=%s body=%s",
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
            async with httpx.AsyncClient(timeout=90.0) as client:
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
            last_err = f"network attempt {attempt + 1}: {exc}"
            log.warning("industry: anthropic %s", last_err)
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
                "industry: anthropic retryable %s body=%s",
                last_err, r.text[:300],
            )
        else:
            # Non-retryable error (400/401/403)
            log.warning(
                "industry: anthropic non-retryable status=%s body=%s",
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
    """Send an email via Resend. On dry-run / failure, stash the draft."""
    if not RESEND_API_KEY:
        log.info(
            "industry: RESEND_API_KEY unset; stashing draft kind=%s eng=%s",
            kind, engagement_id,
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        await _send_slack_alert(
            f":warning: Industry delivery: RESEND_API_KEY missing — "
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
            {"name": "category", "value": "delivery_industry"},
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
        log.exception("industry: resend network failed kind=%s", kind)
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend network: {exc}")

    if r.status_code >= 400:
        log.error(
            "industry: resend non-2xx kind=%s status=%s body=%s",
            kind, r.status_code, r.text[:300],
        )
        await _stash_email_draft(engagement_id, to, subject, html, kind, cc)
        raise RuntimeError(f"resend {r.status_code}: {r.text[:200]}")

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None
    log.info(
        "industry: resend ok kind=%s eng=%s msg_id=%s",
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
            "industry: stash_email_draft failed eng=%s kind=%s",
            engagement_id, kind,
        )


# ---------------------------------------------------------------------------
# Email HTML templates
# ---------------------------------------------------------------------------


def _wrap_email(title: str, body_html: str, vertical_label: str = "") -> str:
    """Wrap a body fragment in the standard Anuvia email shell."""
    suffix = f" · {vertical_label}" if vertical_label else ""
    return f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · Vertical Pilot{suffix}</p>
<h1 style="font-family:Georgia,serif;font-size:24px;margin:0 0 14px;color:#0f172a;">{title}</h1>
{body_html}
<p style="color:#78716c;font-size:13px;line-height:1.6;margin-top:28px;border-top:1px solid #f0eeec;padding-top:18px;">Qualquer dúvida, é só responder este email.<br><br>Mila Vernazza · Founder Anuvia</p>
</div></body></html>"""


def _kickoff_email_html(
    *,
    first_name: str,
    intake_url: str,
    value_str: str,
    vertical: str,
) -> str:
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    label = _vertical_label(vertical)
    compliance = ", ".join(pb.get("compliance") or []) or "—"
    stakeholders = pb.get("stakeholders") or []
    data_inputs = pb.get("data_inputs") or []
    cases = pb.get("typical_cases") or []

    stakeholder_lis = "".join(
        f'<li>{s}</li>' for s in stakeholders
    ) or "<li>—</li>"
    data_lis = "".join(
        f'<li>{d}</li>' for d in data_inputs
    ) or "<li>—</li>"
    case_lis = "".join(
        f'<li>{c}</li>' for c in cases
    ) or "<li>—</li>"

    gxp_block = ""
    if _is_gxp_vertical(vertical):
        gxp_block = (
            '<p style="color:#475569;line-height:1.65;margin:0 0 14px;">'
            '<strong>Nota GxP:</strong> todos os entregáveis carregam '
            'apêndice de "GxP validation considerations" (IQ/OQ/PQ + '
            'rastreabilidade 21 CFR Part 11). Validation Lead participa '
            'desde semana 1.</p>'
        )

    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Contrato fechado. Vertical Pilot — {label} começa agora. Investimento total: <strong>R$ {value_str}</strong>. Cronograma: 6 semanas, três fases (Discovery & Compliance Mapping → PoV Design &amp; Build → Validation &amp; Roadmap).</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Compliance frames aplicáveis a este vertical:</strong> {compliance}.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Casos típicos avaliados em PoV:</strong></p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">{case_lis}</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Stakeholders esperados no workshop semana 1:</strong></p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">{stakeholder_lis}</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Data inventory que precisamos confirmar:</strong></p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">{data_lis}</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;"><strong>Antes do workshop preciso da informação abaixo via intake:</strong></p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li>Sponsor executivo (nome + email)</li>
  <li>Lista nominal de stakeholders por área (alinhada com a lista acima)</li>
  <li>Caso(s) candidato(s) que o cliente já tem em mente</li>
  <li>Compliance officer designado (nome + email)</li>
  <li>Domain expert que vai colaborar no eval set (nome + email)</li>
  <li>Status atual de cada data input listado acima (disponível / em mapeamento / inexistente)</li>
  <li>Histórico de PoCs no vertical (status: live / killed / stalled)</li>
</ul>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário de intake &rarr;</a></p>
{gxp_block}
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Workshop de 1 dia (8h) com os stakeholders fica agendado por email separado. Em paralelo, 1:1s de 45min com cada head de área e o Compliance officer.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Padrão dos últimos vertical pilots: 3-5 casos candidatos entram no workshop, 1 caso fechado em PoV escopado pra 4 semanas, eval set construído com domain expert, success criteria pre-defined.</p>
"""
    return _wrap_email(
        f"Vertical Pilot — {label} começou", body, vertical_label=label
    )


def _phase1_email_html(
    *,
    first_name: str,
    compliance_url: str,
    inventory_url: str,
    vertical_label: str,
    n_cases: int,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Fase 1 fechada — Discovery &amp; Compliance Mapping. Workshop com lideranças rodado, 1:1s com Compliance officer e domain experts concluídos.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Saída desta fase: <strong>{n_cases} casos candidatos</strong> avaliados contra compliance posture e data inventory específicos do vertical {vertical_label}.</p>
<p style="margin:8px 0 12px;"><a href="{compliance_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Compliance posture (PDF) &rarr;</a></p>
<p style="margin:8px 0 24px;"><a href="{inventory_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Data inventory (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Fase 2 (próximas 2 semanas): seleção do caso priorizado, escopo do PoV (≤4 semanas executable), implementation plan, eval framework com success criteria pre-defined e construção do eval set com domain expert.</p>
"""
    return _wrap_email(
        "Compliance posture + Data inventory prontos — Fase 1",
        body, vertical_label=vertical_label,
    )


def _phase2_email_html(
    *,
    first_name: str,
    scope_url: str,
    plan_url: str,
    eval_url: str,
    vertical_label: str,
    case_name: str,
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Fase 2 fechada — PoV Design &amp; Build. Caso priorizado: <strong>{case_name}</strong>.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">PoV scope fechado em 4 semanas executable, implementation plan com dependências mapeadas, eval framework com success criteria pre-defined (não retrofit). Eval set construído com domain expert do cliente.</p>
<p style="margin:8px 0 12px;"><a href="{scope_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">PoV scope (PDF) &rarr;</a></p>
<p style="margin:8px 0 12px;"><a href="{plan_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Implementation plan (PDF) &rarr;</a></p>
<p style="margin:8px 0 24px;"><a href="{eval_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Eval framework (PDF) &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Fase 3 (próximas 2 semanas): PoV run em dados reais ou shadow mode, eval results vs success criteria, production rollout roadmap (6-12 meses) e apresentação executiva.</p>
"""
    return _wrap_email(
        "PoV scope + plan + eval prontos — Fase 2",
        body, vertical_label=vertical_label,
    )


def _phase3_email_html(
    *,
    first_name: str,
    results_url: str,
    roadmap_url: str,
    report_url: str,
    deck_url: str,
    nps_url: str,
    vertical_label: str,
    success_criteria_met: bool,
) -> str:
    status_line = (
        "PoV passou nos success criteria pre-definidos."
        if success_criteria_met
        else "PoV não atingiu todos os success criteria — entregável traz "
             "diagnóstico do gap + recomendações de iteração."
    )
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Vertical Pilot concluído. Seis semanas, quatro entregáveis finais. {status_line}</p>
<ul style="color:#475569;line-height:1.65;margin:0 0 18px 18px;padding:0;">
  <li><a href="{results_url}" style="color:#0f172a;">PoV results</a> — eval metrics vs success criteria + feedback qualitativo do domain expert.</li>
  <li><a href="{roadmap_url}" style="color:#0f172a;">Production roadmap</a> — sequenciamento 6-12 meses, gates técnicos + organizacionais + compliance.</li>
  <li><a href="{report_url}" style="color:#0f172a;">Relatório executivo final</a> — sumário, metodologia, eval results, ADRs, riscos, governança.</li>
  <li><a href="{deck_url}" style="color:#0f172a;">Apresentação executiva</a> — 25 slides pra C-level e board.</li>
</ul>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Sessão de handoff (2h) fica agendada por email separado. A invoice da segunda parcela já entrou na fila.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Um pedido: 2 minutos pra deixar um NPS. Direto, sem firula:</p>
<p style="margin:8px 0 24px;"><a href="{nps_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Deixar NPS &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se conhecer outro líder no mesmo vertical com PoCs estagnados ou pressão regulatória aumentando — você sabe quem precisa ouvir isso.</p>
"""
    return _wrap_email(
        f"Vertical Pilot — {vertical_label} entregue",
        body, vertical_label=vertical_label,
    )


def _intake_reminder_email_html(
    *, first_name: str, intake_url: str, vertical_label: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Lembrete curto: o formulário de intake ainda não foi preenchido. Sem ele, o workshop de discovery não roda e o cronograma desloca.</p>
<p style="margin:24px 0;"><a href="{intake_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Abrir formulário &rarr;</a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Se tiver algum bloqueio (sponsor não definido, Compliance officer ainda mapeando, domain expert não confirmado) — me avisa que a gente resolve.</p>
"""
    return _wrap_email(
        f"Intake pendente — Vertical Pilot {vertical_label}",
        body, vertical_label=vertical_label,
    )


def _progress_update_email_html(
    *, first_name: str, phase_label: str, summary: str, vertical_label: str
) -> str:
    body = f"""
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Olá {first_name},</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Update curto sobre o pilot — fase atual: <strong>{phase_label}</strong>.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">{summary}</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Próximo entregável escrito chega ao final da fase. Qualquer coisa antes, é só responder este email.</p>
"""
    return _wrap_email(
        "Pilot em andamento", body, vertical_label=vertical_label,
    )


# ---------------------------------------------------------------------------
# HTML shell for PDF deliverables
# ---------------------------------------------------------------------------


def _deliverable_html(
    title: str, subtitle: str, body_md_html: str, vertical_label: str = ""
) -> str:
    """A4-friendly inline-styled deliverable wrapper."""
    suffix = f" · {vertical_label}" if vertical_label else ""
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
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 6px;">Anuvia · Vertical Pilot{suffix}</p>
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


def _resolve_vertical(engagement: dict) -> Optional[str]:
    """Resolve normalized vertical key from engagement row.

    Checks ``intake_data.vertical`` first, then ``artifacts.vertical``,
    then ``industry`` / ``segment`` fields. Returns ``None`` when no
    supported vertical can be resolved.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    candidates = (
        intake.get("vertical"),
        intake.get("industry"),
        intake.get("segment"),
        artifacts.get("vertical"),
        artifacts.get("resolved_vertical"),
    )
    for c in candidates:
        norm = _normalize_vertical(c)
        if norm:
            return norm
    return None


# ---------------------------------------------------------------------------
# Deliverable composition — Claude prompts
# ---------------------------------------------------------------------------


async def _compose_compliance_posture(
    engagement: dict, vertical: str, intake_data: dict
) -> dict:
    """Phase 1 — vertical-specific compliance deep-dive.

    Returns::

        {
            "summary": "...",
            "frames": [
                {
                    "frame": "BACEN 4.658",
                    "applicability": "high|medium|low",
                    "current_posture": "...",
                    "gaps": ["..."],
                    "mitigations": ["..."],
                    "owner": "Compliance Officer / DPO / ...",
                },
                ...
            ],
            "critical_gates": ["..."],
        }
    """
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    label = _vertical_label(vertical)
    compliance = ", ".join(pb.get("compliance") or []) or "—"
    cases = ", ".join(pb.get("typical_cases") or [])

    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    gxp_note = ""
    if _is_gxp_vertical(vertical):
        gxp_note = (
            "ATENÇÃO: este pilot é Life Sciences. Toda análise de compliance "
            "DEVE cobrir IQ/OQ/PQ (qualificação de instalação, operação e "
            "performance), data integrity (ALCOA+), audit trail 21 CFR Part 11 "
            "e change control. Marcar gaps GxP como critical_gate."
        )

    prompt = f"""Você está compondo a Compliance Posture do Vertical Pilot Anuvia para o vertical {label}.

Frames de compliance aplicáveis a este vertical: {compliance}
Casos típicos considerados nesse vertical: {cases}

Perfil do cliente (intake submetido):
{profile_block}

{gxp_note}

Para CADA frame de compliance aplicável (incluindo frames adjacentes que possam aplicar — LGPD sempre aplica no Brasil), produza:
1. **frame**: nome exato do frame.
2. **applicability**: high | medium | low. Justifique implicitamente na current_posture.
3. **current_posture**: 2-3 frases descrevendo onde o cliente está hoje. Se faltar dado, marque "estimativa baseada em padrões setoriais do vertical".
4. **gaps**: array de gaps específicos (ex: "DPIA não documentado", "audit trail não imutável", "data residency não confirmada").
5. **mitigations**: array de mitigations concretas (ex: "implementar DPIA via template OneTrust", "habilitar S3 Object Lock + CloudTrail", "contratar revisão jurídica externa").
6. **owner**: quem do lado do cliente carrega esse frame (Compliance Officer, DPO, QA Director, etc).

Identifique até 3 critical_gates: itens que se não forem resolvidos antes do PoV go-live, BLOQUEIAM o pilot.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: principal frame, principal gap, principal risco regulatório se nada for feito>",
  "frames": [
    {{
      "frame": "<nome>",
      "applicability": "<high|medium|low>",
      "current_posture": "<2-3 frases>",
      "gaps": ["<gap>", ...],
      "mitigations": ["<mitigation>", ...],
      "owner": "<role>"
    }}
  ],
  "critical_gates": ["<gate description>", ...]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=6000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} compliance posture não gerada — "
                f"revisar manualmente. Vertical: {label}."
            ),
            "frames": [
                {
                    "frame": f,
                    "applicability": "medium",
                    "current_posture": (
                        f"{_CLAUDE_FALLBACK_TAG} estimativa pendente."
                    ),
                    "gaps": [],
                    "mitigations": [],
                    "owner": "Compliance Officer",
                }
                for f in (pb.get("compliance") or ["LGPD"])
            ],
            "critical_gates": [],
        },
        required_keys=("summary", "frames"),
    )


async def _compose_data_inventory(
    engagement: dict, vertical: str, intake_data: dict
) -> dict:
    """Phase 1 — data inventory specific to the vertical.

    Returns::

        {
            "summary": "...",
            "sources": [
                {
                    "name": "MES logs",
                    "status": "available|partial|missing",
                    "owner": "...",
                    "volume_estimate": "...",
                    "access_path": "...",
                    "quality_assessment": "...",
                    "compliance_tags": ["..."],
                    "blockers": ["..."],
                },
                ...
            ],
            "candidate_cases": [
                {
                    "name": "...",
                    "data_deps_met": "yes|partial|no",
                    "compliance_tag": "...",
                    "rationale": "..."
                },
                ...
            ]
        }
    """
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    label = _vertical_label(vertical)
    expected = ", ".join(pb.get("data_inputs") or []) or "—"
    cases = "\n".join(f"- {c}" for c in (pb.get("typical_cases") or []))

    profile_lines: List[str] = []
    for k, v in (intake_data or {}).items():
        if v in (None, "", []):
            continue
        profile_lines.append(f"- {k}: {v}")
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    prompt = f"""Você está compondo o Data Inventory do Vertical Pilot Anuvia para o vertical {label}.

Data inputs esperados nesse vertical: {expected}

Casos típicos que podem entrar em PoV:
{cases}

Perfil do cliente (intake submetido):
{profile_block}

Para CADA data input esperado (e outros que o intake mencione), produza:
1. **name**: nome exato da fonte de dado.
2. **status**: available | partial | missing.
3. **owner**: área/role do cliente responsável.
4. **volume_estimate**: número + unidade (registros/dia, GB/mês, eventos/segundo). Se faltar dado: "estimativa baseada em padrões setoriais".
5. **access_path**: como acessamos (API, S3 export, DB read-replica, file drop SFTP, etc).
6. **quality_assessment**: 1-2 frases sobre completude, freshness, schema stability.
7. **compliance_tags**: array de tags (LGPD, BACEN, GxP, HIPAA, ISO 27001, etc).
8. **blockers**: array de blockers concretos (DPIA necessária, contrato de DPA, anonimização requerida, etc) — vazio se não houver.

Em seguida, para CADA caso típico do vertical, avalie se os data_deps são met (yes | partial | no), atribua compliance_tag principal, e dê 1 frase de rationale.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: quantas fontes available, gargalo principal, qual caso fica mais fácil de PoV-ar dado o inventário>",
  "sources": [
    {{
      "name": "<nome>",
      "status": "<available|partial|missing>",
      "owner": "<role>",
      "volume_estimate": "<número + unidade>",
      "access_path": "<descrição>",
      "quality_assessment": "<1-2 frases>",
      "compliance_tags": ["<tag>", ...],
      "blockers": ["<blocker>", ...]
    }}
  ],
  "candidate_cases": [
    {{
      "name": "<exato do input>",
      "data_deps_met": "<yes|partial|no>",
      "compliance_tag": "<frame>",
      "rationale": "<1 frase>"
    }}
  ]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=6000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} data inventory não gerado — "
                f"revisar manualmente."
            ),
            "sources": [
                {
                    "name": d,
                    "status": "partial",
                    "owner": "IT/Tech Lead",
                    "volume_estimate": f"{_CLAUDE_FALLBACK_TAG} estimar",
                    "access_path": f"{_CLAUDE_FALLBACK_TAG} confirmar",
                    "quality_assessment": (
                        f"{_CLAUDE_FALLBACK_TAG} avaliar."
                    ),
                    "compliance_tags": pb.get("compliance") or [],
                    "blockers": [],
                }
                for d in (pb.get("data_inputs") or [])
            ],
            "candidate_cases": [
                {
                    "name": c,
                    "data_deps_met": "partial",
                    "compliance_tag": (pb.get("compliance") or ["nenhuma"])[0],
                    "rationale": f"{_CLAUDE_FALLBACK_TAG}",
                }
                for c in (pb.get("typical_cases") or [])
            ],
        },
        required_keys=("summary", "sources", "candidate_cases"),
    )


async def _compose_pov_scope(
    engagement: dict, vertical: str, compliance: dict, inventory: dict
) -> dict:
    """Phase 2 — define the single PoV case + scope.

    Returns::

        {
            "summary": "...",
            "selected_case": {
                "name": "...",
                "vertical": "...",
                "compliance_tag": "...",
                "rationale_for_selection": "...",
            },
            "in_scope": ["..."],
            "out_of_scope": ["..."],
            "success_criteria": [
                {
                    "metric": "...",
                    "baseline": "...",
                    "target": "...",
                    "evaluation_method": "...",
                },
                ...
            ],
            "risks": [...],
            "shadow_mode_design": "...",
        }
    """
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    label = _vertical_label(vertical)

    candidate_block_lines: List[str] = []
    for c in (inventory.get("candidate_cases") or []):
        if not isinstance(c, dict):
            continue
        candidate_block_lines.append(
            f"- {c.get('name')} | data_deps_met={c.get('data_deps_met')} | "
            f"compliance={c.get('compliance_tag')} | {c.get('rationale')}"
        )
    candidates = "\n".join(candidate_block_lines) or "(sem candidatos)"

    critical_gates = "\n".join(
        f"- {g}" for g in (compliance.get("critical_gates") or [])
    ) or "(nenhum)"

    gxp_note = ""
    if _is_gxp_vertical(vertical):
        gxp_note = (
            "ATENÇÃO Life Sciences: success_criteria DEVE incluir métricas de "
            "data integrity (ALCOA+) e o shadow_mode_design DEVE preservar "
            "audit trail compatível com 21 CFR Part 11. Out_of_scope DEVE "
            "incluir explicitamente 'submissão regulatória' a menos que "
            "validação completa IQ/OQ/PQ esteja em escopo (não está em "
            "pilot de 4 semanas)."
        )

    prompt = f"""Você está escolhendo o caso priorizado para PoV no Vertical Pilot Anuvia ({label}) e escrevendo o scope doc.

Casos candidatos surgidos do data inventory:
{candidates}

Critical gates de compliance que se não resolvidos bloqueiam PoV:
{critical_gates}

{gxp_note}

Regras:
- Escolha UM caso, não mais. PoV de 4 semanas não escala pra 2 casos.
- O caso escolhido DEVE ter data_deps_met=yes ou no mínimo partial-com-mitigation-em-4-semanas.
- O caso escolhido NÃO PODE ter critical_gate bloqueante sem mitigation no scope.
- success_criteria são pre-defined (não retrofit). Mínimo 3 métricas, cada uma com baseline + target + evaluation_method.
- shadow_mode_design SEMPRE preferido a go-live direto. Explicar.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: caso escolhido, por que, principal risco>",
  "selected_case": {{
    "name": "<exato>",
    "vertical": "{label}",
    "compliance_tag": "<frame>",
    "rationale_for_selection": "<2-3 frases>"
  }},
  "in_scope": ["<item>", ...],
  "out_of_scope": ["<item>", ...],
  "success_criteria": [
    {{
      "metric": "<nome+unidade>",
      "baseline": "<valor ou 'estimativa baseada em padrões setoriais'>",
      "target": "<valor+intervalo de confiança quando aplicável>",
      "evaluation_method": "<como vamos medir>"
    }}
  ],
  "risks": [
    {{ "risk": "<descrição>", "likelihood": "<low|med|high>", "impact": "<low|med|high>", "mitigation": "<frase>" }}
  ],
  "shadow_mode_design": "<2-4 frases descrevendo como rodar o PoV em paralelo ao processo atual sem afetar prod>"
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=6000)
    fallback_case_name = (pb.get("typical_cases") or ["Caso candidato"])[0]
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} pov scope não gerado — "
                f"revisar manualmente."
            ),
            "selected_case": {
                "name": fallback_case_name,
                "vertical": label,
                "compliance_tag": (pb.get("compliance") or ["nenhuma"])[0],
                "rationale_for_selection": f"{_CLAUDE_FALLBACK_TAG}",
            },
            "in_scope": [],
            "out_of_scope": [],
            "success_criteria": [
                {
                    "metric": f"{_CLAUDE_FALLBACK_TAG}",
                    "baseline": "—",
                    "target": "—",
                    "evaluation_method": "—",
                }
            ],
            "risks": [],
            "shadow_mode_design": f"{_CLAUDE_FALLBACK_TAG}",
        },
        required_keys=("summary", "selected_case", "success_criteria"),
    )


async def _compose_pov_implementation_plan(
    engagement: dict, vertical: str, scope: dict
) -> str:
    """Phase 2 — implementation plan markdown for the selected case."""
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"

    success_lines: List[str] = []
    for sc in scope.get("success_criteria") or []:
        if not isinstance(sc, dict):
            continue
        success_lines.append(
            f"- {sc.get('metric')} | baseline {sc.get('baseline')} | "
            f"target {sc.get('target')} | método {sc.get('evaluation_method')}"
        )
    success_block = "\n".join(success_lines) or "(sem success criteria)"

    in_scope = "\n".join(f"- {s}" for s in (scope.get("in_scope") or []))
    out_scope = "\n".join(f"- {s}" for s in (scope.get("out_of_scope") or []))

    gxp_appendix = ""
    if _is_gxp_vertical(vertical):
        gxp_appendix = (
            "\n\nIncluir seção final '## GxP validation considerations' "
            "cobrindo: traceability matrix entre user requirements → "
            "functional spec → test cases; design of IQ/OQ/PQ; "
            "data integrity (ALCOA+); audit trail e electronic records "
            "compatíveis com 21 CFR Part 11; change control para alterações "
            "pós-validação."
        )

    prompt = f"""Escreva o implementation plan do PoV {case_name} no Vertical Pilot Anuvia ({label}), em markdown.

Caso escolhido: {case_name}
Compliance tag: {compliance_tag}
Compliance frames aplicáveis ao vertical: {_compliance_frame_block(vertical)}

Success criteria pre-defined:
{success_block}

In-scope:
{in_scope or '(vazio)'}

Out-of-scope:
{out_scope or '(vazio)'}

Estrutura obrigatória:

## Resumo executivo
3-5 linhas: o que vai ser construído, em quanto tempo, o que define sucesso, qual o gate de produção.

## Arquitetura proposta
Descrição em camadas:
- Data layer (fontes, ingestão, storage, retention)
- Model layer (model selection, prompt engineering quando aplicável, fine-tuning quando aplicável, fallback strategy)
- Application layer (integração com sistemas do cliente, latência alvo, observability)
- Compliance layer (audit trail, access control, data residency, PII handling)

## Cronograma 4 semanas
Tabela markdown: Semana | Atividades | Entregável | Dono | Dependência crítica.

## Stakeholders + RACI
Tabela markdown: Stakeholder | Atividade | R/A/C/I. Incluir Compliance officer, domain expert, IT/Tech lead, Anuvia tech lead, Mila.

## Tech stack proposta
Bullets curtos com decisões (vector DB, model provider, observability, eval framework, audit trail backend) + 1 linha de rationale cada.

## Eval framework integrado
Como o eval set será construído com o domain expert do cliente. Composition rules. Tamanho mínimo. Critério de hold-out.

## Compliance gates específicos
Para cada compliance frame aplicável ao vertical, listar os gates específicos do PoV (ex: para BACEN — explainability mínima; para GxP — audit trail imutável; para LGPD — DPIA antes do go-live).

## Riscos top 5 e mitigations
Tabela markdown: Risco | Likelihood | Impact | Mitigation | Dono.

## Critérios de go/no-go pra produção
3-5 critérios objetivos que se atingidos pós-PoV destravam produção.{gxp_appendix}

Voz Anuvia: seca, direta, numbers-first. Bullets curtos. Tabelas markdown reais. Nada de fluff.
"""

    return await _claude_call_with_voice(prompt, max_tokens=6000)


async def _compose_eval_framework(
    engagement: dict, vertical: str, scope: dict
) -> str:
    """Phase 2 — eval framework markdown for the PoV."""
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"

    success_lines: List[str] = []
    for sc in scope.get("success_criteria") or []:
        if not isinstance(sc, dict):
            continue
        success_lines.append(
            f"- {sc.get('metric')} | baseline {sc.get('baseline')} | "
            f"target {sc.get('target')} | método {sc.get('evaluation_method')}"
        )
    success_block = "\n".join(success_lines) or "(sem success criteria)"

    gxp_appendix = ""
    if _is_gxp_vertical(vertical):
        gxp_appendix = (
            "\n\nApêndice GxP obrigatório: descrever como o eval framework "
            "preserva ALCOA+ (Attributable, Legible, Contemporaneous, Original, "
            "Accurate, + Complete, Consistent, Enduring, Available), como "
            "as eval traces ficam imutáveis (audit log compatível com "
            "21 CFR Part 11 §11.10), e como o controle de mudança trata "
            "atualizações posteriores do eval set."
        )

    prompt = f"""Escreva o eval framework do PoV {case_name} no Vertical Pilot Anuvia ({label}), em markdown.

Caso: {case_name}
Compliance tag: {compliance_tag}
Compliance frames do vertical: {_compliance_frame_block(vertical)}

Success criteria pre-defined:
{success_block}

Estrutura obrigatória:

## Resumo
2-3 linhas: princípio do eval, tamanho do eval set, frequência de re-eval.

## Eval set — composição
- Composition rules (% labels positivos vs negativos, % edge cases, % adversarial cases)
- Tamanho mínimo para significância estatística (cite o número + raciocínio)
- Domain expert envolvido — quem do cliente, papel, horas alocadas
- Critério de hold-out (% reservada para teste final)
- Estratégia de versionamento (cada versão do eval set carrega hash + changelog)

## Métricas por dimensão
Para CADA success_criterion acima, descreva:
- O que mede (definição operacional)
- Como mede (fórmula concreta)
- Threshold de aceitação (com banda de tolerância)
- Ação se falhar (rollback, iterate, escalate)

## Métricas de produção (além das de PoV)
- Latência p95, p99
- Custo por inferência (R$)
- Taxa de fallback humano
- Taxa de erro / hallucination (quando aplicável a casos com LLM)
- Drift detection (quando aplicável)

## Testes adversariais
Lista de 5-8 categorias de testes adversariais relevantes ao vertical {label}. Cada um com 1 exemplo concreto.

## Compliance-specific evals
Para o frame {compliance_tag}, quais validações específicas o eval precisa cobrir (ex: explainability mínima pra BACEN; PII leakage detection pra LGPD; ALCOA+ trail pra GxP).

## Cadência
- Eval inicial: pré-go-live
- Eval contínuo: cadência (diário? semanal? por release?)
- Re-baseline: quando dispara

## Critérios de aceitação final
Tabela markdown: Métrica | Threshold | Status (medido vs target) | Decisão (pass/fail/iterate).{gxp_appendix}

Voz Anuvia: seca, direta, numbers-first.
"""

    return await _claude_call_with_voice(prompt, max_tokens=6000)


async def _compose_pov_results(
    engagement: dict, vertical: str, scope: dict, eval_framework_md: str
) -> dict:
    """Phase 3 — synthesize PoV run results.

    Returns::

        {
            "summary": "...",
            "case_name": "...",
            "metrics": [
                {
                    "metric": "...",
                    "target": "...",
                    "measured": "...",
                    "status": "pass|fail|partial",
                    "notes": "..."
                }, ...
            ],
            "success_criteria_met": True|False,
            "qualitative_feedback": "...",
            "edge_cases_handled": ["..."],
            "edge_cases_failed": ["..."],
            "production_readiness": "ready|with_mitigations|not_ready",
            "next_steps": ["..."]
        }
    """
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"

    success_lines: List[str] = []
    for sc in scope.get("success_criteria") or []:
        if not isinstance(sc, dict):
            continue
        success_lines.append(
            f"- {sc.get('metric')} | baseline {sc.get('baseline')} | "
            f"target {sc.get('target')} | método {sc.get('evaluation_method')}"
        )
    success_block = "\n".join(success_lines) or "(sem success criteria)"

    gxp_note = ""
    if _is_gxp_vertical(vertical):
        gxp_note = (
            "ATENÇÃO Life Sciences: production_readiness só pode ser 'ready' "
            "se IQ/OQ/PQ estiver concluído e audit trail validado. "
            "Caso contrário, no máximo 'with_mitigations'."
        )

    prompt = f"""Você está sintetizando os resultados do PoV run do Vertical Pilot Anuvia ({label}).

Caso: {case_name}
Compliance tag: {compliance_tag}

Success criteria pre-defined:
{success_block}

Eval framework completo (extrato):
{eval_framework_md[:2000]}

{gxp_note}

Como o PoV ainda não rodou de fato no momento desta composição (este é o relatório que será preenchido com resultados reais via UI do operador), gere uma versão "esqueleto" com valores plausíveis baseados em padrões setoriais. Marque CADA medição como "estimativa baseada em padrões setoriais" e use bandas conservadoras (worst case do range esperado).

O operador vai sobrescrever esses valores via UI antes da entrega final. Não invente precisão que não existe.

Para CADA success_criterion, produza uma linha de medição.

Devolva APENAS JSON válido, sem markdown:

{{
  "summary": "<3-5 linhas: status geral, principais hits, principais misses>",
  "case_name": "{case_name}",
  "metrics": [
    {{
      "metric": "<nome+unidade>",
      "target": "<valor>",
      "measured": "<valor — estimativa baseada em padrões setoriais>",
      "status": "<pass|fail|partial>",
      "notes": "<frase>"
    }}
  ],
  "success_criteria_met": <true|false>,
  "qualitative_feedback": "<2-3 frases atribuídas ao domain expert>",
  "edge_cases_handled": ["<descrição>", ...],
  "edge_cases_failed": ["<descrição>", ...],
  "production_readiness": "<ready|with_mitigations|not_ready>",
  "next_steps": ["<step>", ...]
}}
"""

    raw = await _claude_call_with_voice(prompt, max_tokens=6000)
    return _parse_json_or_fallback(
        raw,
        fallback_factory=lambda: {
            "summary": (
                f"{_CLAUDE_FALLBACK_TAG} pov results não gerado — "
                f"revisar manualmente. Valores reais devem ser preenchidos pelo "
                f"operador antes da entrega."
            ),
            "case_name": case_name,
            "metrics": [
                {
                    "metric": sc.get("metric", "—") if isinstance(sc, dict) else "—",
                    "target": sc.get("target", "—") if isinstance(sc, dict) else "—",
                    "measured": f"{_CLAUDE_FALLBACK_TAG} aguardando run real",
                    "status": "partial",
                    "notes": "",
                }
                for sc in (scope.get("success_criteria") or [])
            ],
            "success_criteria_met": False,
            "qualitative_feedback": f"{_CLAUDE_FALLBACK_TAG}",
            "edge_cases_handled": [],
            "edge_cases_failed": [],
            "production_readiness": "with_mitigations",
            "next_steps": [],
        },
        required_keys=("summary", "metrics", "success_criteria_met"),
    )


async def _compose_production_roadmap(
    engagement: dict, vertical: str, scope: dict, results: dict
) -> str:
    """Phase 3 — production rollout roadmap markdown (6-12 months)."""
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"
    readiness = results.get("production_readiness") or "with_mitigations"

    metrics_lines: List[str] = []
    for m in (results.get("metrics") or []):
        if not isinstance(m, dict):
            continue
        metrics_lines.append(
            f"- {m.get('metric')} | target {m.get('target')} | "
            f"measured {m.get('measured')} | status {m.get('status')}"
        )
    metrics_block = "\n".join(metrics_lines) or "(sem métricas)"

    gxp_appendix = ""
    if _is_gxp_vertical(vertical):
        gxp_appendix = (
            "\n\nAdicionar seção '## GxP rollout considerations' cobrindo: "
            "fase de IQ → OQ → PQ formal; documentação de change control "
            "para cada release; assinaturas eletrônicas conforme 21 CFR "
            "Part 11; periodic review (anual ou após mudança significativa); "
            "deviation management."
        )

    prompt = f"""Escreva o production rollout roadmap (6-12 meses) do caso {case_name} no Vertical Pilot Anuvia ({label}), em markdown.

Caso: {case_name}
Compliance tag: {compliance_tag}
Compliance frames do vertical: {_compliance_frame_block(vertical)}
Production readiness pós-PoV: {readiness}

Métricas do PoV:
{metrics_block}

Estrutura obrigatória:

## Resumo executivo
3-5 linhas: rollout proposto, marcos por trimestre, principal gate de compliance, decisão pedida ao sponsor.

## Princípio de sequenciamento
Texto curto explicando ordem: production_readiness pós-PoV → mitigations pendentes → compliance gates → escala.

## Gates explícitos
Para CADA gate, escreva:
### Gate N — <nome>
- **Trigger:** o que dispara
- **Critério de saída:** condição objetiva
- **Dono:** role do cliente + role da Anuvia
- **Compliance check:** o que precisa estar validado neste gate

Mínimo 4 gates:
1. Gate Discovery → PoV concluído (já passamos no pilot)
2. Gate PoV → Shadow mode em produção
3. Gate Shadow mode → Production limited (subset de usuários/cases)
4. Gate Production limited → Production full

## Horizonte 1 — Q1 pós-pilot (0-90 dias)
Tabela markdown: Atividade | Dono | Esforço (dias-pessoa) | Dependência | Compliance gate associado.

Incluir: implementação das mitigations pendentes do PoV, automatização do eval contínuo, hardening de observability, audit trail validation.

## Horizonte 2 — Q2-Q3 (90-270 dias)
Casos adjacentes (do mesmo vertical) que podem entrar em PoV separado. Roadmap de expansão. Cross-case dependencies (vector DB compartilhado? eval framework? observability stack?).

## Horizonte 3 — Q4 (270-365 dias)
Estruturais: vendor lock-in mitigation, multi-region (se aplicável), disaster recovery, escala 10x.

## Compliance roadmap separado
Eventos regulatórios fixos no horizonte (auditorias anuais, renovação de certificações, mudanças regulatórias previstas para {label} em 2026-2027). Marcar como deadlines não-negociáveis.

## Custos estimados de operação
Para o caso em produção:
- Infra (R$/mês)
- Modelo/API calls (R$/mês estimado em runrate)
- Time alocado (horas/mês)
- Compliance overhead (R$/mês: audits, reviews, controles)

Marcar tudo como "estimativa baseada em padrões setoriais" quando não houver dado concreto.

## Governança contínua
Cadência mensal (template inline), métricas que importam, thresholds que disparam pausa, papéis em cada review.

## Decisão pedida ao sponsor
Lista numerada de 3 decisões objetivas (sim/não) que precisam ser tomadas em até 30 dias pra destravar Q1.{gxp_appendix}

Voz Anuvia: seca, direta, numbers-first. Tabelas markdown reais. Sem fluff.
"""

    return await _claude_call_with_voice(prompt, max_tokens=6500)


async def _compose_executive_deck(
    engagement: dict,
    vertical: str,
    scope: dict,
    results: dict,
    roadmap_md: str,
) -> str:
    """Phase 3 — slide-by-slide markdown skeleton (25 slides target)."""
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"

    success_met = results.get("success_criteria_met")
    readiness = results.get("production_readiness") or "with_mitigations"
    n_pass = sum(
        1 for m in (results.get("metrics") or [])
        if isinstance(m, dict) and m.get("status") == "pass"
    )
    n_total = len(results.get("metrics") or [])

    gxp_note = ""
    if _is_gxp_vertical(vertical):
        gxp_note = (
            "Incluir slide específico de GxP: status de validação "
            "(IQ/OQ/PQ), data integrity ALCOA+, audit trail 21 CFR Part 11, "
            "change control plan."
        )

    prompt = f"""Escreva o esqueleto markdown de uma apresentação executiva (25 slides) pra fechar o Vertical Pilot Anuvia — {label}.

Caso priorizado: {case_name}
Compliance tag: {compliance_tag}
Compliance frames do vertical: {_compliance_frame_block(vertical)}

Resultado do PoV:
- Success criteria met: {success_met}
- Métricas: {n_pass}/{n_total} passaram
- Production readiness: {readiness}

{gxp_note}

Para cada slide, escreva:

### Slide N — <título>
- 3-5 bullets curtos (uma frase cada, sem ponto final)
- (notas: <fala de 30s do apresentador>)

Estrutura sugerida (25 slides):
1. Slide 1 — capa: cliente, vertical, escopo, prazo.
2. Slide 2 — sumário executivo (caso escolhido, status do PoV, decisão pedida).
3. Slide 3 — contexto: o que pediram + como respondemos no pilot de 6 semanas.
4. Slide 4 — metodologia: discovery → compliance mapping → PoV design → build → validation → roadmap.
5. Slide 5 — compliance posture deep-dive (frames aplicáveis, gaps principais, mitigations propostas).
6. Slide 6 — data inventory (fontes available/partial/missing, principal gargalo).
7. Slide 7 — caso priorizado: por que este caso, rationale, alternativas descartadas.
8. Slide 8 — PoV scope (in-scope vs out-of-scope explícito).
9. Slide 9 — arquitetura proposta (data → model → application → compliance).
10. Slide 10 — success criteria pre-defined (não retrofit).
11. Slide 11 — eval framework (composition, tamanho, domain expert, hold-out).
12. Slide 12 — cronograma 4 semanas do PoV (timeline visual).
13. Slide 13 — resultados do PoV (tabela: métrica × target × measured × status).
14. Slide 14 — edge cases handled vs failed.
15. Slide 15 — production readiness assessment (ready / with_mitigations / not_ready + justificativa).
16. Slide 16 — qualitative feedback do domain expert.
17. Slide 17 — riscos top 5 (técnicos + compliance + organizacionais).
18. Slide 18 — gates de production rollout (PoV → shadow → limited → full).
19. Slide 19 — production roadmap Q1 (0-90 dias).
20. Slide 20 — production roadmap Q2-Q3-Q4 (90-365 dias).
21. Slide 21 — compliance roadmap (eventos regulatórios fixos no horizonte).
22. Slide 22 — custos estimados de operação em produção.
23. Slide 23 — governança contínua (cadência, métricas, thresholds).
24. Slide 24 — decisão pedida ao sponsor (3 itens objetivos).
25. Slide 25 — handoff + próximo passo + Anuvia retainer ongoing (CTA opcional).

Voz Anuvia: seca, direta, anti-hype. Bullets curtos sem ponto final.
"""

    return await _claude_call_with_voice(prompt, max_tokens=8000)


async def _compose_final_executive_report(
    engagement: dict,
    vertical: str,
    compliance: dict,
    inventory: dict,
    scope: dict,
    eval_framework_md: str,
    results: dict,
    roadmap_md: str,
) -> str:
    """Phase 3 — full executive report markdown (target 15-25 pages)."""
    label = _vertical_label(vertical)
    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    compliance_tag = case.get("compliance_tag") or "nenhuma"

    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    profile_lines = [
        f"- {k}: {v}" for k, v in intake.items() if v not in (None, "", [])
    ]
    profile_block = "\n".join(profile_lines) or "(intake vazio)"

    compliance_md = _compliance_to_markdown(compliance)
    inventory_md = _inventory_to_markdown(inventory)
    scope_md = _scope_to_markdown(scope)
    results_md = _results_to_markdown(results)

    gxp_appendix = ""
    if _is_gxp_vertical(vertical):
        gxp_appendix = (
            "\n\nIncluir SEÇÃO FINAL obrigatória '## Apêndice GxP — validation "
            "considerations' cobrindo: traceability matrix completa, design "
            "de IQ/OQ/PQ, ALCOA+ assessment do pipeline implementado, "
            "audit trail design (21 CFR Part 11 §11.10 e §11.30), "
            "controle de mudança pós-validação, periodic review schedule, "
            "deviation management e CAPA."
        )

    prompt = f"""Você está escrevendo o relatório executivo final do Vertical Pilot Anuvia — {label}.

Caso: {case_name}
Compliance tag dominante: {compliance_tag}
Compliance frames do vertical: {_compliance_frame_block(vertical)}

Perfil do cliente:
{profile_block}

Compliance posture (fase 1):
{compliance_md[:2500]}

Data inventory (fase 1):
{inventory_md[:2500]}

PoV scope (fase 2):
{scope_md[:2500]}

Eval framework (fase 2):
{eval_framework_md[:2000]}

Resultados do PoV (fase 3):
{results_md[:2000]}

Production roadmap (fase 3, resumo):
{roadmap_md[:2500]}

Estrutura obrigatória, nesta ordem:

1. **## Sumário executivo** — 1 página: contexto, caso escolhido, status do PoV (success_criteria_met sim/não), production_readiness, 3 decisões pedidas ao sponsor.
2. **## Contexto do cliente** — perfil, vertical, stakeholders identificados, capability interna, ambição declarada.
3. **## Metodologia** — 6 semanas, 3 fases (Discovery & Compliance Mapping → PoV Design & Build → Validation & Roadmap). Princípios de execução (pre-defined success criteria, shadow mode preferido, domain expert no eval).
4. **## Compliance posture** — frame por frame, applicability, current_posture, gaps, mitigations, critical_gates. Sempre que aplicável, tag explícito do frame ({_compliance_frame_block(vertical)}).
5. **## Data inventory** — fonte por fonte, status, owner, volume, access path, quality, compliance_tags, blockers.
6. **## Caso priorizado e PoV scope** — selected_case com rationale_for_selection, in_scope, out_of_scope, success_criteria pre-defined, shadow_mode_design.
7. **## Implementation plan executado** — arquitetura em camadas, cronograma 4 semanas, RACI, tech stack.
8. **## Eval framework** — composition do eval set, métricas por dimensão, testes adversariais, compliance-specific evals, cadência.
9. **## PoV results** — tabela completa métrica × target × measured × status. Qualitative feedback. Edge cases handled vs failed. Production readiness assessment.
10. **## Production roadmap** — gates explícitos PoV→Shadow→Limited→Full. Horizontes Q1, Q2-Q3, Q4. Compliance roadmap separado. Custos estimados.
11. **## ADRs (Architecture Decision Records)** — decisões estruturais tomadas no pilot: vector DB, model provider, observability, audit trail backend, eval framework. 1 ADR por decisão (contexto, decisão, alternativas, consequências).
12. **## Riscos top 5** — técnico, compliance, organizacional, vendor lock-in, sponsorship. Cada um com mitigação proposta.
13. **## Governança contínua** — cadência mensal, métricas (latência p95, custo realizado vs orçado, taxa de fallback humano, drift), thresholds que disparam pausa.
14. **## Handoff checklist** — os 10 itens revisados em todo Vertical Pilot Anuvia:
    1. Compliance constraints mapped + validated com Compliance officer
    2. Data inventory específico do vertical confirmado disponível
    3. PoV scope ≤4 weeks executable (não overscoped)
    4. Eval set construído com domain expert (não só métrica genérica)
    5. Success criteria do PoV pre-defined (não retrofit)
    6. Shadow mode design considered (não go-live direto)
    7. Production roadmap separated de PoV scope (não bait-and-switch)
    8. Vendor lock-in risk assessed
    9. Internal champion identificado
    10. Executive sponsor briefed and bought-in
    Para cada item: status (atendido / parcial / pendente) + nota explicativa.
15. **## Apêndices** — glossário de frames de compliance aplicáveis a {label}, referências regulatórias, scoring rubric do eval framework.{gxp_appendix}

Voz Anuvia: seca, direta, numbers-first. Cada caso/decisão carrega compliance_tag explícito. Estimativas marcadas como tal.
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
        log.warning("industry: claude returned non-JSON: %s", exc)
        out = fallback_factory()
        if isinstance(out, dict):
            out["summary"] = (
                f"{_CLAUDE_FALLBACK_TAG} resposta não-JSON da Claude.\n\n"
                f"{text[:1200]}"
            )
        return out


def _compliance_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Frames de compliance")
    out.append("")
    for f in (data.get("frames") or []):
        if not isinstance(f, dict):
            continue
        out.append(f"### {f.get('frame') or '—'}")
        out.append(f"- **Applicability:** {f.get('applicability') or '—'}")
        out.append(f"- **Owner:** {f.get('owner') or '—'}")
        out.append(f"- **Current posture:** {f.get('current_posture') or '—'}")
        gaps = f.get("gaps") or []
        if isinstance(gaps, list) and gaps:
            out.append("- **Gaps:**")
            for g in gaps:
                out.append(f"  - {g}")
        mits = f.get("mitigations") or []
        if isinstance(mits, list) and mits:
            out.append("- **Mitigations:**")
            for m in mits:
                out.append(f"  - {m}")
        out.append("")
    gates = data.get("critical_gates") or []
    if isinstance(gates, list) and gates:
        out.append("## Critical gates (bloqueiam PoV se não resolvidos)")
        for g in gates:
            out.append(f"- {g}")
    return "\n".join(out)


def _inventory_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append("## Fontes de dado")
    out.append("")
    for s in (data.get("sources") or []):
        if not isinstance(s, dict):
            continue
        out.append(f"### {s.get('name') or '—'}")
        out.append(f"- **Status:** {s.get('status') or '—'}")
        out.append(f"- **Owner:** {s.get('owner') or '—'}")
        out.append(f"- **Volume estimado:** {s.get('volume_estimate') or '—'}")
        out.append(f"- **Access path:** {s.get('access_path') or '—'}")
        out.append(
            f"- **Quality assessment:** {s.get('quality_assessment') or '—'}"
        )
        tags = s.get("compliance_tags") or []
        if isinstance(tags, list) and tags:
            out.append(
                f"- **Compliance tags:** {', '.join(str(t) for t in tags)}"
            )
        blockers = s.get("blockers") or []
        if isinstance(blockers, list) and blockers:
            out.append("- **Blockers:**")
            for b in blockers:
                out.append(f"  - {b}")
        out.append("")
    cands = data.get("candidate_cases") or []
    if cands:
        out.append("## Casos candidatos avaliados contra inventory")
        for c in cands:
            if not isinstance(c, dict):
                continue
            out.append(f"### {c.get('name') or '—'}")
            out.append(f"- **Data deps met:** {c.get('data_deps_met') or '—'}")
            out.append(
                f"- **Compliance tag:** {c.get('compliance_tag') or '—'}"
            )
            out.append(f"- {c.get('rationale') or ''}")
            out.append("")
    return "\n".join(out)


def _scope_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    sel = data.get("selected_case") or {}
    if isinstance(sel, dict):
        out.append("## Caso selecionado")
        out.append(f"- **Nome:** {sel.get('name') or '—'}")
        out.append(f"- **Vertical:** {sel.get('vertical') or '—'}")
        out.append(
            f"- **Compliance tag:** {sel.get('compliance_tag') or '—'}"
        )
        out.append(
            f"- **Rationale:** {sel.get('rationale_for_selection') or '—'}"
        )
        out.append("")
    in_scope = data.get("in_scope") or []
    if isinstance(in_scope, list) and in_scope:
        out.append("## In-scope")
        for s in in_scope:
            out.append(f"- {s}")
        out.append("")
    out_scope = data.get("out_of_scope") or []
    if isinstance(out_scope, list) and out_scope:
        out.append("## Out-of-scope")
        for s in out_scope:
            out.append(f"- {s}")
        out.append("")
    sc_list = data.get("success_criteria") or []
    if isinstance(sc_list, list) and sc_list:
        out.append("## Success criteria pre-defined")
        for sc in sc_list:
            if not isinstance(sc, dict):
                continue
            out.append(f"### {sc.get('metric') or '—'}")
            out.append(f"- **Baseline:** {sc.get('baseline') or '—'}")
            out.append(f"- **Target:** {sc.get('target') or '—'}")
            out.append(
                f"- **Evaluation method:** {sc.get('evaluation_method') or '—'}"
            )
            out.append("")
    risks = data.get("risks") or []
    if isinstance(risks, list) and risks:
        out.append("## Riscos")
        for r in risks:
            if not isinstance(r, dict):
                continue
            out.append(
                f"- **{r.get('risk') or '—'}** "
                f"(likelihood {r.get('likelihood') or '—'}, "
                f"impact {r.get('impact') or '—'}) — "
                f"{r.get('mitigation') or '—'}"
            )
        out.append("")
    sm = data.get("shadow_mode_design")
    if sm:
        out.append("## Shadow mode design")
        out.append(sm)
    return "\n".join(out)


def _results_to_markdown(data: dict) -> str:
    out: List[str] = []
    out.append("## Resumo")
    out.append(data.get("summary") or "")
    out.append("")
    out.append(f"**Caso:** {data.get('case_name') or '—'}")
    sc_met = data.get("success_criteria_met")
    out.append(
        f"**Success criteria met:** "
        f"{'sim' if sc_met else 'não'}"
    )
    out.append(
        f"**Production readiness:** "
        f"{data.get('production_readiness') or '—'}"
    )
    out.append("")
    metrics = data.get("metrics") or []
    if isinstance(metrics, list) and metrics:
        out.append("## Métricas")
        for m in metrics:
            if not isinstance(m, dict):
                continue
            out.append(f"### {m.get('metric') or '—'}")
            out.append(f"- **Target:** {m.get('target') or '—'}")
            out.append(f"- **Measured:** {m.get('measured') or '—'}")
            out.append(f"- **Status:** {m.get('status') or '—'}")
            if m.get("notes"):
                out.append(f"- **Notes:** {m.get('notes')}")
            out.append("")
    qf = data.get("qualitative_feedback")
    if qf:
        out.append("## Feedback qualitativo do domain expert")
        out.append(qf)
        out.append("")
    handled = data.get("edge_cases_handled") or []
    if handled:
        out.append("## Edge cases handled")
        for c in handled:
            out.append(f"- {c}")
        out.append("")
    failed = data.get("edge_cases_failed") or []
    if failed:
        out.append("## Edge cases failed")
        for c in failed:
            out.append(f"- {c}")
        out.append("")
    next_steps = data.get("next_steps") or []
    if next_steps:
        out.append("## Próximos passos")
        for n in next_steps:
            out.append(f"- {n}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Render + upload helper
# ---------------------------------------------------------------------------


async def _render_and_upload(
    engagement_id: str,
    *,
    title: str,
    subtitle: str,
    body_md: str,
    object_path: str,
    vertical_label: str = "",
) -> str:
    """Render markdown → HTML → PDF → upload to Supabase Storage."""
    html = _deliverable_html(
        title, subtitle, _md_to_html(body_md), vertical_label=vertical_label
    )

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
      1. Resolve vertical from intake_data. If missing/invalid → escalate.
      2. Compute vertical-specific ticket from playbook midpoint.
      3. Patch engagement: status='kickoff', total_phases=3, current_phase=1,
         vertical-aware total_value_brl.
      4. Email the lead the intake form link with vertical playbook fit.
      5. Schedule ``industry_phase_1_discovery`` on the lead 1 day out.
      6. Slack-ping Mila.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    # Merge incoming intake.
    merged_intake = {
        **(engagement.get("intake_data") or {}),
        **(intake_data or {}),
    }

    vertical = _resolve_vertical(
        {**engagement, "intake_data": merged_intake}
    )
    if not vertical:
        await _send_slack_alert(
            f":rotating_light: *Industry Vertical Pilot kickoff falhou* — "
            f"engagement `{engagement_id}`: vertical não definido ou inválido. "
            f"Esperado um de: {', '.join(_SUPPORTED_VERTICALS)}. "
            f"Recebido: `{merged_intake.get('vertical') or merged_intake.get('industry') or 'vazio'}`. "
            f"cc {SLACK_MILA_HANDLE} — definir vertical manualmente e re-disparar."
        )
        # Still persist the merged intake so the operator can fix it via UI.
        await _engagement_patch(
            engagement_id,
            {
                "intake_data": merged_intake,
                "status": "blocked_vertical_unresolved",
                "next_phase_at": None,
            },
        )
        return {
            "ok": False,
            "reason": "vertical_unresolved",
            "supported_verticals": list(_SUPPORTED_VERTICALS),
        }

    label = _vertical_label(vertical)
    pb = _VERTICAL_PLAYBOOKS.get(vertical) or {}
    midpoint = _vertical_midpoint(vertical)

    already_kicked = (
        engagement.get("status") in ("kickoff", "running", "delivered")
        and engagement.get("current_phase")
    )

    # Only override total_value_brl when contract.py left it unset or zero.
    existing_value = engagement.get("total_value_brl") or 0
    effective_value = (
        existing_value if existing_value and int(existing_value) > 0
        else midpoint
    )

    patch = {
        "total_phases": 3,
        "current_phase": engagement.get("current_phase") or 1,
        "status": engagement.get("status") or "kickoff",
        "intake_data": merged_intake,
        "total_value_brl": effective_value,
        "started_at": engagement.get("started_at") or _now_iso(),
        "next_phase_at": (
            _serialize(_now() + timedelta(days=1))
            if not already_kicked
            else engagement.get("next_phase_at")
        ),
    }
    await _engagement_patch(engagement_id, patch)
    await _engagement_merge_artifacts(
        engagement_id,
        {
            "vertical": vertical,
            "vertical_label": label,
            "vertical_playbook": {
                "ticket_range": pb.get("ticket_range"),
                "typical_cases": pb.get("typical_cases"),
                "compliance": pb.get("compliance"),
                "stakeholders": pb.get("stakeholders"),
                "data_inputs": pb.get("data_inputs"),
            },
        },
    )

    lead, email, first_name = await _lead_for_engagement(engagement)

    if email and not already_kicked:
        token = _hmac_token(engagement_id, "intake")
        intake_url = (
            f"{BASE_URL}/api/delivery/industry/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        value_str = _brl(effective_value)
        html = _kickoff_email_html(
            first_name=first_name,
            intake_url=intake_url,
            value_str=value_str,
            vertical=vertical,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject=f"Vertical Pilot {label} começou — primeiro passo (intake)",
                html=html,
                kind="industry_kickoff",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "industry.kickoff: email send failed eng=%s", engagement_id
            )

    next_at = _now() + timedelta(days=1)
    if lead and lead.get("id"):
        await session_set_next(
            str(lead["id"]),
            next_action="industry_phase_1_discovery",
            next_action_at=next_at,
        )
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.industry",
            action="industry_kickoff",
            result="ok",
            detail=(
                f"engagement {engagement_id} kickoff; vertical={vertical}; "
                f"ticket=R$ {_brl(effective_value)}; intake email sent; "
                f"phase 1 scheduled at {next_at.isoformat()}"
            ),
        )

    company = (lead or {}).get("company") or "—"
    value_str = _brl(effective_value)
    await _send_slack_alert(
        f":rocket: *Vertical Pilot kickoff — {label}* — engagement "
        f"`{engagement_id}` ({company}) · R$ {value_str} · 6 semanas. "
        f"Vertical: `{vertical}`. Intake enviado pra {email or 'n/a'}."
    )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "vertical": vertical,
        "ticket_brl": effective_value,
        "next_action_at": next_at,
    }


async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute phase N of the Vertical Pilot. Idempotent."""
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    current = int(engagement.get("current_phase") or 1)

    if phase < current:
        log.info(
            "industry.run_phase: skipping phase %s, current=%s eng=%s",
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
    operator-requested refresh from the admin UI.

    Supported types:
        compliance_posture, data_inventory,
        pov_scope_doc, pov_implementation_plan, eval_framework,
        pov_results, production_roadmap,
        final_executive_report, executive_deck

    Returns ``{ok, url, type}`` on success.
    """
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {"ok": False, "reason": "engagement_not_found"}

    vertical = _resolve_vertical(engagement)
    if not vertical:
        return {"ok": False, "reason": "vertical_unresolved"}
    label = _vertical_label(vertical)
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    compliance = artifacts.get("phase_1_compliance_posture") or {}
    inventory = artifacts.get("phase_1_data_inventory") or {}
    scope = artifacts.get("phase_2_pov_scope") or {}
    plan_md = artifacts.get("phase_2_pov_plan_md") or ""
    eval_md = artifacts.get("phase_2_eval_framework_md") or ""
    results = artifacts.get("phase_3_pov_results") or {}
    roadmap_md = artifacts.get("phase_3_production_roadmap_md") or ""

    if deliverable_type == "compliance_posture":
        if not compliance:
            compliance = await _compose_compliance_posture(
                engagement, vertical, intake
            )
        body_md = _compliance_to_markdown(compliance)
        url = await _render_and_upload(
            engagement_id,
            title=f"Compliance posture — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 1",
            body_md=body_md,
            object_path=f"{engagement_id}/compliance_posture.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_compliance_posture": compliance,
                "compliance_posture_md": body_md,
                "compliance_posture_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "data_inventory":
        if not inventory:
            inventory = await _compose_data_inventory(
                engagement, vertical, intake
            )
        body_md = _inventory_to_markdown(inventory)
        url = await _render_and_upload(
            engagement_id,
            title=f"Data inventory — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 1",
            body_md=body_md,
            object_path=f"{engagement_id}/data_inventory.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_data_inventory": inventory,
                "data_inventory_md": body_md,
                "data_inventory_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "pov_scope_doc":
        if not compliance:
            compliance = await _compose_compliance_posture(
                engagement, vertical, intake
            )
        if not inventory:
            inventory = await _compose_data_inventory(
                engagement, vertical, intake
            )
        if not scope:
            scope = await _compose_pov_scope(
                engagement, vertical, compliance, inventory
            )
        body_md = _scope_to_markdown(scope)
        url = await _render_and_upload(
            engagement_id,
            title=f"PoV scope — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 2",
            body_md=body_md,
            object_path=f"{engagement_id}/pov_scope.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_compliance_posture": compliance,
                "phase_1_data_inventory": inventory,
                "phase_2_pov_scope": scope,
                "pov_scope_md": body_md,
                "pov_scope_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "pov_implementation_plan":
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
            if not scope:
                if not compliance:
                    compliance = await _compose_compliance_posture(
                        engagement, vertical, intake
                    )
                if not inventory:
                    inventory = await _compose_data_inventory(
                        engagement, vertical, intake
                    )
                scope = await _compose_pov_scope(
                    engagement, vertical, compliance, inventory
                )
        plan_md = await _compose_pov_implementation_plan(
            engagement, vertical, scope
        )
        url = await _render_and_upload(
            engagement_id,
            title=f"PoV implementation plan — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 2",
            body_md=plan_md,
            object_path=f"{engagement_id}/pov_implementation_plan.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_pov_scope": scope,
                "phase_2_pov_plan_md": plan_md,
                "pov_plan_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "eval_framework":
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
            if not scope:
                if not compliance:
                    compliance = await _compose_compliance_posture(
                        engagement, vertical, intake
                    )
                if not inventory:
                    inventory = await _compose_data_inventory(
                        engagement, vertical, intake
                    )
                scope = await _compose_pov_scope(
                    engagement, vertical, compliance, inventory
                )
        eval_md = await _compose_eval_framework(
            engagement, vertical, scope
        )
        url = await _render_and_upload(
            engagement_id,
            title=f"Eval framework — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 2",
            body_md=eval_md,
            object_path=f"{engagement_id}/eval_framework.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_2_pov_scope": scope,
                "phase_2_eval_framework_md": eval_md,
                "eval_framework_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "pov_results":
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
        if not eval_md:
            eval_md = artifacts.get("phase_2_eval_framework_md") or ""
        results = await _compose_pov_results(
            engagement, vertical, scope, eval_md
        )
        body_md = _results_to_markdown(results)
        url = await _render_and_upload(
            engagement_id,
            title=f"PoV results — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 3",
            body_md=body_md,
            object_path=f"{engagement_id}/pov_results.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_pov_results": results,
                "pov_results_md": body_md,
                "pov_results_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "production_roadmap":
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
        if not results:
            results = artifacts.get("phase_3_pov_results") or {}
            if not results:
                results = await _compose_pov_results(
                    engagement, vertical, scope, eval_md
                )
        roadmap_md = await _compose_production_roadmap(
            engagement, vertical, scope, results
        )
        url = await _render_and_upload(
            engagement_id,
            title=f"Production roadmap — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 3",
            body_md=roadmap_md,
            object_path=f"{engagement_id}/production_roadmap.pdf",
            vertical_label=label,
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_3_pov_results": results,
                "phase_3_production_roadmap_md": roadmap_md,
                "production_roadmap_url": url,
            },
        )
        return {"ok": True, "url": url, "type": deliverable_type}

    if deliverable_type == "executive_deck":
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
        if not results:
            results = artifacts.get("phase_3_pov_results") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_3_production_roadmap_md") or ""
        deck_md = await _compose_executive_deck(
            engagement, vertical, scope, results, roadmap_md
        )
        url = await _render_and_upload(
            engagement_id,
            title=f"Apresentação Executiva — {label}",
            subtitle=f"Engagement {engagement_id} · Entrega final",
            body_md=deck_md,
            object_path=f"{engagement_id}/executive_deck.pdf",
            vertical_label=label,
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
        if not compliance:
            compliance = artifacts.get("phase_1_compliance_posture") or {}
        if not inventory:
            inventory = artifacts.get("phase_1_data_inventory") or {}
        if not scope:
            scope = artifacts.get("phase_2_pov_scope") or {}
        if not eval_md:
            eval_md = artifacts.get("phase_2_eval_framework_md") or ""
        if not results:
            results = artifacts.get("phase_3_pov_results") or {}
        if not roadmap_md:
            roadmap_md = artifacts.get("phase_3_production_roadmap_md") or ""

        report_md = await _compose_final_executive_report(
            engagement,
            vertical,
            compliance,
            inventory,
            scope,
            eval_md,
            results,
            roadmap_md,
        )
        url = await _render_and_upload(
            engagement_id,
            title=f"Relatório Executivo — Vertical Pilot {label}",
            subtitle=f"Engagement {engagement_id} · Entrega final",
            body_md=report_md,
            object_path=f"{engagement_id}/final_executive_report.pdf",
            vertical_label=label,
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
# Phase runners
# ---------------------------------------------------------------------------


def _intake_submitted(engagement: dict) -> bool:
    """Heuristic: intake counts as submitted when key vertical-pilot fields
    landed in ``intake_data``.
    """
    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        return False
    if intake.get("submitted_at"):
        return True
    required = (
        "executive_sponsor_email",
        "stakeholders",
        "candidate_cases",
        "compliance_officer_email",
        "domain_expert_email",
        "data_inputs_status",
        "past_pocs",
    )
    filled = sum(1 for k in required if intake.get(k))
    return filled >= 4


async def _run_phase_1(engagement: dict) -> dict:
    """Phase 1 — Discovery & Compliance Mapping.

    Wait for intake submission, then compose compliance posture and
    data inventory deliverables, advance to phase 2.
    """
    engagement_id = str(engagement.get("id") or "")
    vertical = _resolve_vertical(engagement)
    if not vertical:
        await _send_slack_alert(
            f":rotating_light: industry phase 1 — engagement "
            f"`{engagement_id}` sem vertical resolvido. Esperado um de: "
            f"{', '.join(_SUPPORTED_VERTICALS)}. cc {SLACK_MILA_HANDLE}"
        )
        return {"ok": False, "reason": "vertical_unresolved"}

    label = _vertical_label(vertical)
    lead, email, first_name = await _lead_for_engagement(engagement)

    if _intake_submitted(engagement):
        intake = engagement.get("intake_data") or {}
        if not isinstance(intake, dict):
            intake = {}

        compliance = await _compose_compliance_posture(
            engagement, vertical, intake
        )
        inventory = await _compose_data_inventory(
            engagement, vertical, intake
        )

        compliance_md = _compliance_to_markdown(compliance)
        inventory_md = _inventory_to_markdown(inventory)

        compliance_url = await _render_and_upload(
            engagement_id,
            title=f"Compliance posture — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 1",
            body_md=compliance_md,
            object_path=f"{engagement_id}/compliance_posture.pdf",
            vertical_label=label,
        )
        inventory_url = await _render_and_upload(
            engagement_id,
            title=f"Data inventory — {label}",
            subtitle=f"Engagement {engagement_id} · Fase 1",
            body_md=inventory_md,
            object_path=f"{engagement_id}/data_inventory.pdf",
            vertical_label=label,
        )

        await _engagement_merge_artifacts(
            engagement_id,
            {
                "phase_1_compliance_posture": compliance,
                "compliance_posture_md": compliance_md,
                "compliance_posture_url": compliance_url,
                "phase_1_data_inventory": inventory,
                "data_inventory_md": inventory_md,
                "data_inventory_url": inventory_url,
            },
        )

        n_candidates = len(inventory.get("candidate_cases") or [])

        if email:
            html = _phase1_email_html(
                first_name=first_name,
                compliance_url=compliance_url,
                inventory_url=inventory_url,
                vertical_label=label,
                n_cases=n_candidates,
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject=(
                        f"Compliance + Data inventory prontos — "
                        f"Fase 1 Vertical Pilot {label}"
                    ),
                    html=html,
                    kind="industry_phase_1_discovery",
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "industry.phase_1: email failed eng=%s", engagement_id
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
            "next_action": "industry_phase_2_pov",
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
            f"{BASE_URL}/api/delivery/industry/intake"
            f"?engagement_id={engagement_id}&token={token}"
        )
        html = _intake_reminder_email_html(
            first_name=first_name,
            intake_url=intake_url,
            vertical_label=label,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject=(
                    f"Intake pendente — Vertical Pilot {label}"
                ),
                html=html,
                kind="industry_intake_reminder",
            )
            await _engagement_merge_artifacts(
                engagement_id,
                {"intake_reminder_sent_at": _now_iso()},
            )
            await _send_slack_alert(
                f":hourglass: Industry engagement `{engagement_id}` "
                f"({label}) — intake pendente há {elapsed.days} dias. "
                f"Lembrete enviado pra {email}."
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "industry.phase_1: reminder send failed eng=%s",
                engagement_id,
            )

    next_at = _now() + timedelta(days=1)
    return {
        "ok": True,
        "waiting_for": "intake_submission",
        "next_action": "industry_phase_1_discovery",
        "next_action_at": next_at,
    }


async def _run_phase_2(engagement: dict) -> dict:
    """Phase 2 — PoV Design & Build.

    Compose PoV scope, implementation plan, and eval framework.
    Email the client. Advance to phase 3.
    """
    engagement_id = str(engagement.get("id") or "")
    vertical = _resolve_vertical(engagement)
    if not vertical:
        return {"ok": False, "reason": "vertical_unresolved"}

    label = _vertical_label(vertical)
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    compliance = artifacts.get("phase_1_compliance_posture") or {}
    inventory = artifacts.get("phase_1_data_inventory") or {}

    if not compliance:
        compliance = await _compose_compliance_posture(
            engagement, vertical, intake
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_1_compliance_posture": compliance},
        )
    if not inventory:
        inventory = await _compose_data_inventory(
            engagement, vertical, intake
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_1_data_inventory": inventory},
        )

    scope = await _compose_pov_scope(
        engagement, vertical, compliance, inventory
    )
    scope_md = _scope_to_markdown(scope)

    plan_md = await _compose_pov_implementation_plan(
        engagement, vertical, scope
    )
    eval_md = await _compose_eval_framework(
        engagement, vertical, scope
    )

    scope_url = await _render_and_upload(
        engagement_id,
        title=f"PoV scope — {label}",
        subtitle=f"Engagement {engagement_id} · Fase 2",
        body_md=scope_md,
        object_path=f"{engagement_id}/pov_scope.pdf",
        vertical_label=label,
    )
    plan_url = await _render_and_upload(
        engagement_id,
        title=f"PoV implementation plan — {label}",
        subtitle=f"Engagement {engagement_id} · Fase 2",
        body_md=plan_md,
        object_path=f"{engagement_id}/pov_implementation_plan.pdf",
        vertical_label=label,
    )
    eval_url = await _render_and_upload(
        engagement_id,
        title=f"Eval framework — {label}",
        subtitle=f"Engagement {engagement_id} · Fase 2",
        body_md=eval_md,
        object_path=f"{engagement_id}/eval_framework.pdf",
        vertical_label=label,
    )

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_2_pov_scope": scope,
            "pov_scope_md": scope_md,
            "pov_scope_url": scope_url,
            "phase_2_pov_plan_md": plan_md,
            "pov_plan_url": plan_url,
            "phase_2_eval_framework_md": eval_md,
            "eval_framework_url": eval_url,
        },
    )

    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"

    if email:
        html = _phase2_email_html(
            first_name=first_name,
            scope_url=scope_url,
            plan_url=plan_url,
            eval_url=eval_url,
            vertical_label=label,
            case_name=case_name,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject=(
                    f"PoV scope + plan + eval — Fase 2 Vertical Pilot {label}"
                ),
                html=html,
                kind="industry_phase_2_pov",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "industry.phase_2: email failed eng=%s", engagement_id
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
        "next_action": "industry_phase_3_validation",
        "next_action_at": next_at,
        "selected_case": case_name,
    }


async def _run_phase_3(engagement: dict) -> dict:
    """Phase 3 — Validation & Roadmap.

    Compose PoV results, production roadmap, executive deck, and final
    report. Close engagement, trigger invoice.
    """
    engagement_id = str(engagement.get("id") or "")
    vertical = _resolve_vertical(engagement)
    if not vertical:
        return {"ok": False, "reason": "vertical_unresolved"}

    label = _vertical_label(vertical)
    lead, email, first_name = await _lead_for_engagement(engagement)

    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    intake = engagement.get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}

    compliance = artifacts.get("phase_1_compliance_posture") or {}
    inventory = artifacts.get("phase_1_data_inventory") or {}
    scope = artifacts.get("phase_2_pov_scope") or {}
    plan_md = artifacts.get("phase_2_pov_plan_md") or ""
    eval_md = artifacts.get("phase_2_eval_framework_md") or ""

    # Backfill from earlier phases if needed.
    if not compliance:
        compliance = await _compose_compliance_posture(
            engagement, vertical, intake
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_1_compliance_posture": compliance},
        )
    if not inventory:
        inventory = await _compose_data_inventory(
            engagement, vertical, intake
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_1_data_inventory": inventory},
        )
    if not scope:
        scope = await _compose_pov_scope(
            engagement, vertical, compliance, inventory
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_2_pov_scope": scope},
        )
    if not plan_md:
        plan_md = await _compose_pov_implementation_plan(
            engagement, vertical, scope
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_2_pov_plan_md": plan_md},
        )
    if not eval_md:
        eval_md = await _compose_eval_framework(
            engagement, vertical, scope
        )
        await _engagement_merge_artifacts(
            engagement_id,
            {"phase_2_eval_framework_md": eval_md},
        )

    results = await _compose_pov_results(
        engagement, vertical, scope, eval_md
    )
    results_md = _results_to_markdown(results)

    roadmap_md = await _compose_production_roadmap(
        engagement, vertical, scope, results
    )
    deck_md = await _compose_executive_deck(
        engagement, vertical, scope, results, roadmap_md
    )
    report_md = await _compose_final_executive_report(
        engagement,
        vertical,
        compliance,
        inventory,
        scope,
        eval_md,
        results,
        roadmap_md,
    )

    results_url = await _render_and_upload(
        engagement_id,
        title=f"PoV results — {label}",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=results_md,
        object_path=f"{engagement_id}/pov_results.pdf",
        vertical_label=label,
    )
    roadmap_url = await _render_and_upload(
        engagement_id,
        title=f"Production roadmap — {label}",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=roadmap_md,
        object_path=f"{engagement_id}/production_roadmap.pdf",
        vertical_label=label,
    )
    deck_url = await _render_and_upload(
        engagement_id,
        title=f"Apresentação Executiva — {label}",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=deck_md,
        object_path=f"{engagement_id}/executive_deck.pdf",
        vertical_label=label,
    )
    report_url = await _render_and_upload(
        engagement_id,
        title=f"Relatório Executivo — Vertical Pilot {label}",
        subtitle=f"Engagement {engagement_id} · Entrega final",
        body_md=report_md,
        object_path=f"{engagement_id}/final_executive_report.pdf",
        vertical_label=label,
    )

    success_met = bool(results.get("success_criteria_met"))
    readiness = results.get("production_readiness") or "with_mitigations"

    await _engagement_merge_artifacts(
        engagement_id,
        {
            "phase_3_pov_results": results,
            "pov_results_md": results_md,
            "pov_results_url": results_url,
            "phase_3_production_roadmap_md": roadmap_md,
            "production_roadmap_url": roadmap_url,
            "deck_md": deck_md,
            "deck_url": deck_url,
            "final_report_md": report_md,
            "final_report_url": report_url,
            "success_criteria_met": success_met,
            "production_readiness": readiness,
        },
    )

    nps_url = (
        f"{BASE_URL}/api/delivery/industry/nps"
        f"?engagement_id={engagement_id}&token={_hmac_token(engagement_id, 'nps')}"
    )

    if email:
        html = _phase3_email_html(
            first_name=first_name,
            results_url=results_url,
            roadmap_url=roadmap_url,
            report_url=report_url,
            deck_url=deck_url,
            nps_url=nps_url,
            vertical_label=label,
            success_criteria_met=success_met,
        )
        try:
            await _send_email_via_resend(
                engagement_id=engagement_id,
                to=email,
                subject=(
                    f"Vertical Pilot {label} entregue — "
                    f"results + roadmap + report + deck"
                ),
                html=html,
                kind="industry_phase_3_delivery",
                cc=[RESEND_REPLY_TO_EMAIL] if RESEND_REPLY_TO_EMAIL else None,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "industry.phase_3: email failed eng=%s", engagement_id
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

    case = scope.get("selected_case") or {}
    case_name = case.get("name") or "—"
    value_str = _brl(
        engagement.get("total_value_brl") or _vertical_midpoint(vertical)
    )
    await _send_slack_alert(
        f":white_check_mark: *Vertical Pilot delivered — {label}* — "
        f"engagement `{engagement_id}`. Valor total R$ {value_str}. "
        f"Caso: {case_name}. Success criteria met: "
        f"{'sim' if success_met else 'não'}. "
        f"Production readiness: {readiness}. "
        f"Próximo: invoice ({invoice_result.get('status') or 'pending'}) + NPS. "
        f"cc {SLACK_MILA_HANDLE}"
    )

    if lead and lead.get("id"):
        await session_append_history(
            lead_id=str(lead["id"]),
            agent="delivery.industry",
            action="industry_phase_3_validation",
            result="ok",
            detail=(
                f"engagement {engagement_id} delivered; vertical={vertical}; "
                f"case={case_name}; success_met={success_met}; "
                f"readiness={readiness}; "
                f"invoice {invoice_result.get('status')}"
            ),
        )
        for kind, url in (
            ("final_report", report_url),
            ("production_roadmap", roadmap_url),
            ("executive_deck", deck_url),
            ("pov_results", results_url),
        ):
            try:
                await session_append_artifact(
                    str(lead["id"]),
                    type=kind,
                    url=url,
                    meta={
                        "engagement_id": engagement_id,
                        "phase": 3,
                        "vertical": vertical,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "industry.phase_3: artifact append failed "
                    "lead=%s kind=%s",
                    lead.get("id"), kind,
                )

    return {
        "ok": True,
        "delivered": True,
        "invoice": invoice_result,
        "next_action": None,
        "next_action_at": None,
        "vertical": vertical,
    }


async def _trigger_invoice(contract_id: str, engagement_id: str) -> dict:
    """Call ``lib.contract.issue_invoice`` if available; otherwise log stub."""
    try:
        from lib.contract import issue_invoice  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning(
            "industry: lib.contract.issue_invoice unavailable — stub "
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
            "industry: issue_invoice failed contract=%s", contract_id
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
    engagement on this lead (filtered to practice='industry' to avoid
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
        f"&practice=eq.industry"
        f"&status=in.(kickoff,running,blocked_vertical_unresolved)"
        f"&order=started_at.desc"
        f"&limit=1"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=SUPA_HEADERS)
    except Exception:  # noqa: BLE001
        log.exception("industry: resolve_engagement_id query failed")
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


@register("industry_kickoff")
async def h_industry_kickoff(lead: dict) -> dict:
    """Entry-point handler — fires once after contract.payment_webhook."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_kickoff: no active engagement found",
        }
    engagement = await _engagement_get(engagement_id)
    intake = (engagement or {}).get("intake_data") or {}
    if not isinstance(intake, dict):
        intake = {}
    result = await kickoff(engagement_id, intake)
    if not result.get("ok"):
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": (
                f"industry_kickoff failed: {result.get('reason')}; "
                f"engagement {engagement_id}"
            ),
        }
    return {
        "next_action": "industry_phase_1_discovery",
        "next_action_at": (
            result.get("next_action_at") or (_now() + timedelta(days=1))
        ),
        "status": "delivery_running",
        "detail": (
            f"industry kickoff ok; engagement {engagement_id}; "
            f"vertical={result.get('vertical')}; "
            f"ticket=R$ {_brl(result.get('ticket_brl') or 0)}"
        ),
    }


@register("industry_phase_1_discovery")
async def h_industry_phase_1(lead: dict) -> dict:
    """Phase 1 handler — Discovery & Compliance Mapping."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_phase_1: no active engagement",
        }
    result = await run_phase(engagement_id, 1)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running" if not result.get("delivered") else "won",
        "detail": (
            f"industry phase 1: "
            f"{'advanced→2' if result.get('advanced_to_phase') else 'waiting intake'}"
        ),
    }


@register("industry_phase_2_pov")
async def h_industry_phase_2(lead: dict) -> dict:
    """Phase 2 handler — PoV Design & Build."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_phase_2: no active engagement",
        }
    result = await run_phase(engagement_id, 2)
    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": (
            f"industry phase 2: PoV scope + plan + eval shipped "
            f"(case={result.get('selected_case') or '—'}) for engagement "
            f"{engagement_id}"
        ),
    }


@register("industry_phase_3_validation")
async def h_industry_phase_3(lead: dict) -> dict:
    """Phase 3 handler — Validation & Roadmap + close + invoice."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_phase_3: no active engagement",
        }
    result = await run_phase(engagement_id, 3)
    return {
        "next_action": None,
        "next_action_at": None,
        "status": "won" if result.get("delivered") else "delivery_running",
        "detail": (
            f"industry phase 3: "
            f"{'delivered' if result.get('delivered') else 'in progress'}"
            f"; invoice={result.get('invoice', {}).get('status')}"
        ),
    }


@register("industry_send_progress_update")
async def h_industry_progress_update(lead: dict) -> dict:
    """Mid-phase nudge — re-runs whichever phase the engagement is on, then
    optionally emails a progress update if the client has been silent."""
    engagement_id = await _resolve_engagement_id(lead)
    if not engagement_id:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_progress: no active engagement",
        }
    engagement = await _engagement_get(engagement_id)
    if not engagement:
        return {
            "next_action": None,
            "next_action_at": None,
            "status": "error",
            "detail": "industry_progress: engagement disappeared",
        }
    phase = int(engagement.get("current_phase") or 1)
    result = await run_phase(engagement_id, phase)

    # Best-effort: send a short progress update email when the client has
    # not seen an update in this phase yet.
    artifacts = engagement.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    seen_key = f"progress_update_phase_{phase}_at"
    vertical = _resolve_vertical(engagement)
    label = _vertical_label(vertical) if vertical else "—"

    if not artifacts.get(seen_key):
        _, email, first_name = await _lead_for_engagement(engagement)
        phase_label = {
            1: "Discovery & Compliance Mapping",
            2: "PoV Design & Build",
            3: "Validation & Roadmap",
        }.get(phase, f"Fase {phase}")
        summary = {
            1: (
                "Workshop com lideranças rodando, 1:1s com Compliance "
                "officer e domain experts em andamento. Compliance posture "
                "deep-dive em construção, data inventory sendo consolidado. "
                "Saídas saem ao final da fase."
            ),
            2: (
                "Caso priorizado em definição com base no data inventory + "
                "compliance posture. PoV scope sendo fechado em 4 semanas "
                "executable, eval set sendo construído com o domain expert, "
                "success criteria pre-defined em discussão."
            ),
            3: (
                "PoV run em dados reais (ou shadow mode) em andamento. "
                "Eval results contra success criteria pre-defined em "
                "consolidação. Production roadmap sequenciado por gates "
                "(PoV → shadow → limited → full) em composição."
            ),
        }.get(
            phase,
            "Pilot em andamento — sem update específico para esta fase.",
        )

        if email:
            html = _progress_update_email_html(
                first_name=first_name,
                phase_label=phase_label,
                summary=summary,
                vertical_label=label,
            )
            try:
                await _send_email_via_resend(
                    engagement_id=engagement_id,
                    to=email,
                    subject=(
                        f"Update — {phase_label} (Vertical Pilot {label})"
                    ),
                    html=html,
                    kind=f"industry_progress_phase_{phase}",
                )
                await _engagement_merge_artifacts(
                    engagement_id, {seen_key: _now_iso()}
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "industry.progress: email failed eng=%s phase=%s",
                    engagement_id, phase,
                )

    return {
        "next_action": result.get("next_action"),
        "next_action_at": result.get("next_action_at"),
        "status": "delivery_running",
        "detail": f"industry progress update: re-ran phase {phase}",
    }


# Alias — the contract module emits ``engagement_kickoff_industry`` for
# the ``industry`` practice (see lib/contract.py::_kickoff_engagement).
# We register the same handler under that key so the orchestrator
# dispatch lands here directly without an intermediate translation.
HANDLER_ALIAS = "engagement_kickoff_industry"


@register(HANDLER_ALIAS)
async def h_engagement_kickoff_industry(lead: dict) -> dict:
    """Alias for ``industry_kickoff`` — wired so contract.py's emitted
    action string lands on the right handler without a string remap."""
    return await h_industry_kickoff(lead)
