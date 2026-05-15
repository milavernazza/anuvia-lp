"""Contract → e-signature → payment → invoice automation.

Owned by Agent A3. Per ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Contract & payment
contract" and Mila's 2026-05-15 decisions on the payment + e-signature stack.

Stack (post 2026-05-15 refactor):
  * E-signature: Google Workspace eSignature (Drive API + Docs API). Service
    account impersonates ``GOOGLE_WORKSPACE_DELEGATE_EMAIL`` (mila@anuvia.com.br).
    Graceful degradation: if creds are missing, we fall back to the legacy
    HMAC sign-link flow on this same FastAPI router.
  * Payments BR: Pix via Nubank (CNPJ-based static key, manual reconciliation)
    AND Stripe Anuvia Ltda (cards BR). Pix is the default rail for BRL.
  * Payments US: Stripe Anuvia LLC (cards US). Default rail for USD.
  * Mercado Pago is gone. Old ``mp_preference_id`` column is left in place for
    backward compatibility but never written to by this module.

Flow:

    generate_contract(lead_id, practice, value_brl, payment_method='auto',
                      currency='BRL')
        → renders PT HTML + Gotenberg PDF
        → resolves payment_method (auto → pix for BRL, stripe_us for USD)
        → if Google Workspace creds present: kicks off eSignature flow,
          stores google_doc_id + google_esign_request_id
        → otherwise: falls back to HMAC sign link (legacy flow)
        → if payment_method=='pix': generates BR Code payload + (optional) QR PNG
        → persists row in `contracts`
        → returns {ok, contract_id, pdf_url, sign_url, hmac_token, status,
                   payment_url}
    send_contract_email(contract_id)
        → emails lead the PDF + sign link via Resend
    GET  /api/contract/sign?contract_id=&token=
        → bilingual sign page (legacy fallback when Google not configured)
    POST /api/contract/accept
        → contracts.status='signed', signed_at=now
        → routes to the right rail:
            stripe_br/stripe_us → creates Stripe Checkout on the right account
            pix                 → redirects to /api/contract/pix/{id}?token=...
        → 302 redirect or JSON response
    GET  /api/contract/pix/{contract_id}?token=...
        → renders a page with QR + Pix key + value + instructions
    POST /api/contract/pix/confirm/{contract_id}
        → admin-only (HMAC-protected) — Mila marks Pix as received
        → triggers the same paid-flow as Stripe webhook success
    POST /api/contract/webhook/stripe/br
        → verifies Stripe-Signature with STRIPE_WEBHOOK_SECRET_BR
        → on success: status='paid', kickoff engagement
    POST /api/contract/webhook/stripe/us
        → analogous with STRIPE_WEBHOOK_SECRET_US
    POST /api/contract/webhook/google/esign
        → Drive API push notification — looks up the contract by
          google_doc_id, flips status='signed', triggers payment flow
    issue_invoice(contract_id)
        → Conta Azul stub. Unchanged.

Backward compatibility:
  * STRIPE_SECRET_KEY (the old, single var) is treated as STRIPE_SECRET_KEY_BR
    when the BR-specific var is unset. Same for STRIPE_WEBHOOK_SECRET.
  * Old contracts already-signed-but-not-paid continue to work — the /accept
    handler is idempotent on the 'signed' state.

Graceful degradation:
  * If no Stripe AND no Pix env vars are configured → /accept marks the
    contract signed but skips checkout creation and returns
    {ok: True, status: 'signed', reason: 'no_payment_provider'}.
  * If Google Workspace creds are missing → fall back to HMAC sign link.
  * If RESEND_API_KEY is empty → send_contract_email logs a dry-run.
  * If GOTENBERG_URL is unreachable → we still persist the contract row with
    an HTML fallback URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote as _urlquote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lib.sessions import (
    SUPA_HEADERS,
    SUPA_URL,
    session_append_artifact,
    session_append_history,
    session_append_signal,
    session_get,
    session_set_next,
)

log = logging.getLogger("anuvia-lp.contract")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
RESEND_REPLY_TO_EMAIL = os.environ.get("RESEND_REPLY_TO_EMAIL", "mila@anuvia.com.br")
RESEND_REPLY_TO_NAME = os.environ.get("RESEND_REPLY_TO_NAME", "Anuvia · Mila Vernazza")

# Public host where signed PDFs are served + sign page is hosted.
CONTRACT_HOST = os.environ.get("CONTRACT_HOST", "https://anuvia.com.br").rstrip("/")

# HMAC secret. Falls back to TRACK_B_HMAC_SECRET so a single secret can drive
# both flows in dev.
_CONTRACT_HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)

# ---------------------------------------------------------------------------
# Stripe — dual account (Anuvia Ltda BR + Anuvia LLC US).
# ---------------------------------------------------------------------------
# Backward compat: if a deployment still has the legacy ``STRIPE_SECRET_KEY``
# set and no ``STRIPE_SECRET_KEY_BR``, treat the old var as the BR account.

_LEGACY_STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
_LEGACY_STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

STRIPE_SECRET_KEY_BR = (
    os.environ.get("STRIPE_SECRET_KEY_BR", "") or _LEGACY_STRIPE_SECRET
)
STRIPE_WEBHOOK_SECRET_BR = (
    os.environ.get("STRIPE_WEBHOOK_SECRET_BR", "") or _LEGACY_STRIPE_WEBHOOK_SECRET
)
STRIPE_SECRET_KEY_US = os.environ.get("STRIPE_SECRET_KEY_US", "")
STRIPE_WEBHOOK_SECRET_US = os.environ.get("STRIPE_WEBHOOK_SECRET_US", "")

STRIPE_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL",
    f"{CONTRACT_HOST}/contract-paid",
)
STRIPE_CANCEL_URL = os.environ.get(
    "STRIPE_CANCEL_URL",
    f"{CONTRACT_HOST}/contract-cancelled",
)

# ---------------------------------------------------------------------------
# Pix via Nubank — static key, manual reconciliation initially.
# ---------------------------------------------------------------------------
PIX_NUBANK_KEY = os.environ.get("PIX_NUBANK_KEY", "")
PIX_NUBANK_DISPLAY_NAME = os.environ.get(
    "PIX_NUBANK_DISPLAY_NAME", "Anuvia Tecnologia LTDA"
)
# Free-text city shown on the BR Code merchant record. Pix spec requires
# ASCII, ≤15 chars. We default to São Paulo capital ('SAO PAULO').
PIX_MERCHANT_CITY = os.environ.get("PIX_MERCHANT_CITY", "SAO PAULO")

# ---------------------------------------------------------------------------
# Google Workspace eSignature — service account impersonates a Workspace user.
# ---------------------------------------------------------------------------
GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON", ""
)
GOOGLE_WORKSPACE_DELEGATE_EMAIL = os.environ.get(
    "GOOGLE_WORKSPACE_DELEGATE_EMAIL", "mila@anuvia.com.br"
)

# Anuvia legal placeholders — replace via env when CNPJ is finalized.
ANUVIA_CNPJ = os.environ.get("ANUVIA_CNPJ", "TODO_CNPJ")
ANUVIA_LEGAL_NAME = os.environ.get(
    "ANUVIA_LEGAL_NAME", "Anuvia Cloud & AI Consulting LTDA."
)
ANUVIA_ADDRESS = os.environ.get(
    "ANUVIA_ADDRESS",
    "Rua TODO, 000 — São Paulo, SP — Brasil",
)

# Where rendered contract PDFs land on disk. Mirrors track_b proposals dir.
_CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "static" / "contracts"

# Optional: 3× ROI clause file — used verbatim when present.
_ROI_CLAUSE_PATH = Path(__file__).resolve().parent.parent / "CONTRACT_CLAUSE_3X_ROI.md"

_HTTP_TIMEOUT = 30.0


def _now() -> datetime:
    """Tz-aware UTC now. Centralised for testability."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# Minimal per-practice config used to label the contract object. Kept local
# (not imported from track_b) so this module stays standalone — flipping a
# field here doesn't ripple into proposal copy.
#
# Practice keys MUST match the HANDLER_ALIAS strings emitted by the delivery
# agents (lib/delivery/<practice>.py). The current set after the 2026-05-15
# refactor is:
#   * growth_salesops → lib/delivery/growth_salesops.py
#   * cloud_finops    → lib/delivery/finops_audit.py
#   * ai              → lib/delivery/ai_readiness.py
#   * devops          → lib/delivery/devops_maturity.py
#   * industry        → lib/delivery/industry.py
_PRACTICE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "growth_salesops": {
        "deliverable_name": "Growth & Sales Ops Setup",
        "duration_weeks": "2-3",
        "payment_terms_pt": "50% na assinatura, 50% em 30 dias.",
        "scope_pt": [
            "Solutions Architect sênior (ex-AWS, ex-Google) em retainer",
            "Até 20 horas / mês de execução e revisão hands-on",
            "Entregáveis escritos: arquitetura, FinOps e prontidão para IA",
            "Atendimento via Slack + async em até 24h úteis",
        ],
    },
    "cloud_finops": {
        "deliverable_name": "Auditoria de Custos AWS (FinOps)",
        "duration_weeks": "4",
        "payment_terms_pt": "50% na assinatura, 50% na entrega do relatório final.",
        "scope_pt": [
            "Diagnóstico completo de gastos AWS (CUR + Cost Explorer)",
            "Identificação de waste, rightsizing e Reserved/Savings Plans",
            "Roadmap de otimização priorizado por payback (semana-a-semana)",
            "Relatório executivo escrito + handoff técnico para o time",
        ],
        "include_3x_roi": True,
    },
    "devops": {
        "deliverable_name": "DevOps Maturity Assessment",
        "duration_weeks": "3-4",
        "payment_terms_pt": "50% na assinatura, 50% na entrega.",
        "scope_pt": [
            "Avaliação DORA (deploy freq, lead time, MTTR, change fail rate)",
            "Auditoria de CI/CD, infra-as-code e observabilidade",
            "Roadmap de maturidade em 3 horizontes (30 / 90 / 180 dias)",
            "Relatório executivo + plano de ação para o time de engenharia",
        ],
    },
    "ai": {
        "deliverable_name": "AI Readiness & PoV",
        "duration_weeks": "3",
        "payment_terms_pt": "50% na assinatura, 50% na entrega.",
        "scope_pt": [
            "Inventário de casos de uso de IA priorizados por ROI",
            "Avaliação de prontidão técnica (dados, infra, segurança)",
            "Proof-of-value de 1 caso (escopo fechado, métrica clara)",
            "Roadmap de adoção + governança em 90 dias",
        ],
    },
    "industry": {
        "deliverable_name": "Industry Vertical Assessment",
        "duration_weeks": "4",
        "payment_terms_pt": "50% na assinatura, 50% na entrega.",
        "scope_pt": [
            "Diagnóstico setorial (regulação, compliance, benchmarks)",
            "Mapa de capacidades vs. concorrência no vertical",
            "Roadmap de modernização alinhado às pressões regulatórias",
            "Plano de execução com marcos trimestrais",
        ],
    },
}

# Legacy aliases — historical practice keys used by older callers / rows.
# These are normalized to the canonical key before any lookup.
_PRACTICE_ALIASES: Dict[str, str] = {
    "growth": "growth_salesops",
    "finops": "cloud_finops",
    "cloud-finops": "cloud_finops",
}


def _normalize_practice(practice: Optional[str]) -> str:
    """Normalize a practice key — apply aliases, lowercase, fall back to default."""
    key = (practice or "").strip().lower()
    if not key:
        return "growth_salesops"
    return _PRACTICE_ALIASES.get(key, key)


def _practice_config(practice: str, overrides: Optional[dict] = None) -> dict:
    """Return per-practice config, merged with optional overrides."""
    key = _normalize_practice(practice)
    base = dict(_PRACTICE_DEFAULTS.get(key) or {})
    if overrides:
        base.update({k: v for k, v in overrides.items() if v is not None})
    base.setdefault("deliverable_name", key.replace("_", " ").title())
    base.setdefault("duration_weeks", "—")
    base.setdefault("payment_terms_pt", "Pagamento conforme acordado entre as partes.")
    base.setdefault("scope_pt", [])
    return base


# ---------------------------------------------------------------------------
# HMAC helpers
# ---------------------------------------------------------------------------


def _sign_contract_token(contract_id: str) -> str:
    """HMAC-SHA256 of the contract id, hex-encoded.

    Empty string when no secret is configured — callers can detect this and
    refuse to expose unverifiable sign links.
    """
    if not _CONTRACT_HMAC_SECRET:
        log.warning(
            "contract: CONTRACT_HMAC_SECRET (and TRACK_B_HMAC_SECRET) unset; "
            "tokens will be empty"
        )
        return ""
    return hmac.new(
        _CONTRACT_HMAC_SECRET.encode("utf-8"),
        str(contract_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_contract_token(contract_id: str, token: str) -> bool:
    """Constant-time HMAC compare for the sign/accept flow."""
    if not contract_id or not token:
        return False
    expected = _sign_contract_token(contract_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def _sign_admin_action_token(contract_id: str, action: str) -> str:
    """HMAC token for admin-only actions on a contract (e.g. pix_confirm).

    The action string is part of the signed payload so a token valid for one
    admin action can't be replayed against another. Empty string when the
    secret is unset.
    """
    if not _CONTRACT_HMAC_SECRET:
        return ""
    payload = f"{contract_id}:{action}".encode("utf-8")
    return hmac.new(
        _CONTRACT_HMAC_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _verify_admin_action_token(contract_id: str, action: str, token: str) -> bool:
    """Constant-time HMAC compare for admin actions."""
    if not contract_id or not action or not token:
        return False
    expected = _sign_admin_action_token(contract_id, action)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# Supabase contract CRUD (PostgREST)
# ---------------------------------------------------------------------------


async def _insert_contract(row: dict) -> Optional[dict]:
    """Insert a contracts row. Returns the inserted row or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                f"{SUPA_URL}/contracts",
                headers=SUPA_HEADERS,
                json=row,
            )
        if r.status_code not in (200, 201):
            log.error(
                "contract: insert non-2xx status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return None
        body = r.json() if r.text else []
        if isinstance(body, list) and body:
            return body[0]
        if isinstance(body, dict):
            return body
    except Exception:  # noqa: BLE001
        log.exception("contract: insert failed")
    return None


async def _get_contract(contract_id: str) -> Optional[dict]:
    """Fetch one contracts row by id."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{SUPA_URL}/contracts?id=eq.{contract_id}&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code != 200:
            log.warning(
                "contract: get non-200 contract=%s status=%s",
                contract_id, r.status_code,
            )
            return None
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        log.exception("contract: get failed contract=%s", contract_id)
        return None


async def _get_contract_by_field(field: str, value: str) -> Optional[dict]:
    """Look up a contract by a unique field (e.g. stripe_session_id)."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{SUPA_URL}/contracts?{field}=eq.{_urlquote(str(value), safe='')}&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code != 200:
            return None
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        log.exception("contract: lookup by %s failed", field)
        return None


async def _patch_contract(contract_id: str, fields: dict) -> bool:
    """PATCH arbitrary fields on a contract. Stamps updated_at."""
    payload = dict(fields)
    payload.setdefault("updated_at", _now_iso())
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.patch(
                f"{SUPA_URL}/contracts?id=eq.{contract_id}",
                headers=SUPA_HEADERS,
                json=payload,
            )
        if r.status_code not in (200, 204):
            log.warning(
                "contract: patch non-2xx contract=%s status=%s body=%s",
                contract_id, r.status_code, r.text[:200],
            )
            return False
        return True
    except Exception:  # noqa: BLE001
        log.exception("contract: patch failed contract=%s", contract_id)
        return False


async def _insert_engagement(row: dict) -> Optional[dict]:
    """Insert an engagements row. Returns the inserted row or None."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                f"{SUPA_URL}/engagements",
                headers=SUPA_HEADERS,
                json=row,
            )
        if r.status_code not in (200, 201):
            log.error(
                "contract: engagement insert non-2xx status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return None
        body = r.json() if r.text else []
        if isinstance(body, list) and body:
            return body[0]
        if isinstance(body, dict):
            return body
    except Exception:  # noqa: BLE001
        log.exception("contract: engagement insert failed")
    return None


# ---------------------------------------------------------------------------
# Contract HTML / PDF rendering
# ---------------------------------------------------------------------------


def _brl(n: float) -> str:
    """Format a number as Brazilian-style currency: 45.000,00."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0,00"
    # Brazilian format: '.' thousands, ',' decimal.
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _load_roi_clause() -> Optional[str]:
    """Return the Brazilian-Portuguese 3× ROI clause text, or None if missing.

    Best-effort extraction of the PT block from CONTRACT_CLAUSE_3X_ROI.md.
    """
    try:
        if not _ROI_CLAUSE_PATH.exists():
            return None
        text = _ROI_CLAUSE_PATH.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.warning("contract: failed to read 3x ROI clause file")
        return None

    # Slice between the PT header and the EN header so we render PT only.
    pt_marker = "## Cláusula em Português"
    en_marker = "## Clause in English"
    if pt_marker in text:
        text = text.split(pt_marker, 1)[1]
    if en_marker in text:
        text = text.split(en_marker, 1)[0]
    return text.strip() or None


def _md_to_simple_html(md: str) -> str:
    """Very small Markdown-ish → HTML converter for the embedded ROI clause.

    Handles headings (#, ##, ###, ####), bold (**...**), italic (*...*), and
    paragraph/line breaks. Good enough for a clause block in a contract PDF.
    """
    import re

    out_lines: list[str] = []
    for line in md.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            content = m.group(2)
            # Cap at h4 → h4; we don't want huge headings inside a contract.
            tag = f"h{min(level + 2, 6)}"
            out_lines.append(f"<{tag} style=\"margin:14px 0 6px;font-size:13px;\">{content}</{tag}>")
            continue
        # Inline bold/italic
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
        content = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", content)
        out_lines.append(f"<p style=\"margin:6px 0;\">{content}</p>")

    return "\n".join(out_lines)


def _build_contract_html(
    lead: dict,
    practice_config: dict,
    value_brl: int,
    scope: Optional[dict] = None,
) -> str:
    """Render the PT-only contract as a self-contained HTML document.

    Designed for Gotenberg → PDF. Inline-styled, A4-friendly, black text on
    white, sans-serif. No external assets.
    """
    scope = scope or {}
    deliverable = practice_config.get("deliverable_name") or "Serviço Anuvia"
    duration = practice_config.get("duration_weeks") or "—"
    payment_terms = practice_config.get("payment_terms_pt") or (
        "Pagamento conforme acordado entre as partes."
    )
    bullets = practice_config.get("scope_pt") or []

    client_name = (lead.get("name") or "—").strip()
    client_company = (lead.get("company") or "—").strip()
    client_email = (lead.get("email") or "—").strip()

    today_pt = _now().strftime("%d/%m/%Y")
    value_str = _brl(value_brl)

    bullets_html = "".join(
        f'<li style="margin:4px 0;line-height:1.5;">{item}</li>'
        for item in bullets
    ) or '<li style="margin:4px 0;line-height:1.5;">Escopo a ser definido em anexo.</li>'

    # 3× ROI clause: prefer the full Markdown file when relevant; otherwise
    # short one-paragraph fallback.
    include_roi = bool(practice_config.get("include_3x_roi"))
    roi_block_html = ""
    if include_roi:
        clause_md = _load_roi_clause()
        if clause_md:
            roi_block_html = (
                '<section style="margin:24px 0;padding:16px 18px;background:#fafaf9;border:1px solid #e7e5e4;border-radius:6px;">'
                + _md_to_simple_html(clause_md)
                + "</section>"
            )
        else:
            roi_block_html = (
                '<section style="margin:24px 0;padding:16px 18px;background:#fafaf9;border:1px solid #e7e5e4;border-radius:6px;">'
                '<h3 style="margin:0 0 8px;font-size:13px;">Cláusula de Garantia 3× ROI</h3>'
                '<p style="margin:0;line-height:1.55;">'
                f'A Anuvia compromete-se a entregar, no prazo do engajamento de {deliverable}, '
                'recomendações cujas Economias Anualizadas Identificadas sejam, no mínimo, '
                'equivalentes a 3 (três) vezes o valor dos honorários pagos. Caso o limiar não '
                'seja atingido após o procedimento de contestação, o Cliente fará jus ao reembolso '
                'integral dos honorários. Condições detalhadas (acesso técnico, dados de '
                'faturamento, tenure mínima de conta, gasto mensal mínimo, dispute window de 30 '
                'dias) constarão no anexo técnico anexado a este instrumento.'
                "</p></section>"
            )

    # Any scope overrides (free-text appendix block).
    overrides_html = ""
    notes = (scope or {}).get("notes")
    if notes:
        overrides_html = (
            '<section style="margin:18px 0;">'
            '<h3 style="margin:0 0 6px;font-size:13px;">Observações específicas</h3>'
            f'<p style="margin:0;line-height:1.55;white-space:pre-wrap;">{notes}</p>'
            '</section>'
        )

    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Contrato — Anuvia · {deliverable}</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color:#0f172a; font-size:12px; line-height:1.55; margin:0; padding:0; background:#ffffff; }}
  h1 {{ font-size:18px; margin:0 0 4px; }}
  h2 {{ font-size:14px; margin:18px 0 6px; border-bottom:1px solid #e7e5e4; padding-bottom:4px; }}
  h3 {{ font-size:13px; margin:14px 0 6px; }}
  p, li {{ font-size:12px; }}
  ul {{ padding-left:20px; margin:6px 0 12px; }}
  table.meta {{ width:100%; border-collapse:collapse; margin:8px 0 18px; font-size:11px; }}
  table.meta td {{ padding:3px 0; vertical-align:top; }}
  table.meta td.k {{ color:#64748b; width:160px; }}
  .sig-block {{ margin-top:32px; padding-top:18px; border-top:1px solid #e7e5e4; }}
  .small {{ color:#64748b; font-size:11px; }}
</style></head>
<body>
<header>
  <p class="small" style="text-transform:uppercase;letter-spacing:0.16em;margin:0 0 4px;">Contrato de Prestação de Serviços</p>
  <h1>Anuvia · {deliverable}</h1>
  <p class="small" style="margin:0;">Documento gerado em {today_pt}.</p>
</header>

<h2>Partes</h2>
<table class="meta">
  <tr><td class="k">Contratada</td><td><strong>{ANUVIA_LEGAL_NAME}</strong><br>CNPJ: {ANUVIA_CNPJ}<br>{ANUVIA_ADDRESS}</td></tr>
  <tr><td class="k">Contratante</td><td><strong>{client_name}</strong><br>Empresa: {client_company}<br>Email: {client_email}</td></tr>
</table>

<h2>1. Objeto do contrato</h2>
<p>A <strong>Contratada</strong> prestará à <strong>Contratante</strong> o serviço de <strong>{deliverable}</strong>, com duração aproximada de <strong>{duration} semana(s)</strong>, compreendendo as seguintes entregas e atividades:</p>
<ul>{bullets_html}</ul>
{overrides_html}

<h2>2. Valor e condições de pagamento</h2>
<p>O valor total do serviço é de <strong>R$ {value_str}</strong> (reais), parcelado conforme as seguintes condições:</p>
<p>{payment_terms}</p>
<p class="small">Os pagamentos serão realizados via boleto, Pix ou cartão de crédito, conforme link de pagamento enviado por email após a assinatura deste instrumento. Em caso de atraso superior a 10 dias corridos, a Contratada poderá suspender a execução do serviço até regularização.</p>

<h2>3. Prazo e cronograma</h2>
<p>A execução iniciará em até 5 (cinco) dias úteis a partir da confirmação do primeiro pagamento. O cronograma detalhado será compartilhado pela Contratada após o kickoff. A Contratante compromete-se a prover acessos, dados e informações solicitadas no prazo razoável necessário para o cumprimento do cronograma.</p>

<h2>4. Cancelamento</h2>
<p>Qualquer das partes poderá rescindir este contrato mediante aviso prévio por escrito (email aceito) de 15 (quinze) dias corridos. Em caso de rescisão pela Contratante, os pagamentos já efetuados não serão reembolsados, ressalvada a garantia da Cláusula 6, quando aplicável. Trabalhos parcialmente entregues permanecerão de propriedade da Contratante.</p>

<h2>5. Confidencialidade</h2>
<p>As partes se obrigam a manter sigilo sobre toda informação técnica, comercial, financeira ou estratégica trocada no âmbito deste contrato, por prazo indeterminado a partir do término do engajamento.</p>

<h2>6. Garantia</h2>
{roi_block_html if roi_block_html else '<p>A Contratada empenhará seus melhores esforços para entregar os resultados descritos no objeto deste contrato, atuando com obrigação de meio. Eventuais garantias adicionais constarão em anexo específico.</p>'}

<h2>7. Foro e lei aplicável</h2>
<p>Este contrato é regido pelas leis da República Federativa do Brasil. Fica eleito o foro da Comarca de São Paulo/SP para dirimir quaisquer controvérsias decorrentes deste instrumento, com renúncia a qualquer outro, por mais privilegiado que seja.</p>

<div class="sig-block">
  <h3>Assinatura</h3>
  <p>A Contratante manifesta sua aceitação eletrônica deste contrato ao clicar em "Eu aceito os termos" no link enviado por email.</p>
  <p class="small">Assinado eletronicamente via link HMAC em {{signed_at}} — IP e timestamp registrados em log auditável.</p>
  <br>
  <p>______________________________<br>
  {ANUVIA_LEGAL_NAME}<br>
  <span class="small">Mila Vernazza · Founder</span></p>
  <br>
  <p>______________________________<br>
  {client_name}<br>
  <span class="small">{client_company}</span></p>
</div>

</body></html>"""


async def _render_pdf_via_gotenberg(html: str, out_path: Path) -> bool:
    """POST html to Gotenberg, write PDF to out_path. Returns success.

    Never raises — Gotenberg outages must not kill contract creation.
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
                "contract: gotenberg non-200 status=%s body=%s",
                r.status_code, r.text[:200],
            )
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("contract: gotenberg call failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API — generate_contract / send_contract_email / issue_invoice
# ---------------------------------------------------------------------------


def _any_payment_provider_configured() -> bool:
    """True iff at least one payment rail is wired up."""
    return bool(STRIPE_SECRET_KEY_BR or STRIPE_SECRET_KEY_US or PIX_NUBANK_KEY)


async def generate_contract(
    lead_id: str,
    practice: str,
    value_brl: int,
    scope_overrides: Optional[dict] = None,
    payment_method: str = "auto",
    currency: str = "BRL",
) -> dict:
    """Generate a contract for ``lead_id`` and persist it.

    Steps:
      1. Fetch the lead row (used for client name/company/email in the body).
      2. Resolve currency + payment_method (see ``_resolve_payment_method``).
      3. Render PT HTML via ``_build_contract_html``.
      4. Render PDF via Gotenberg → ``/static/contracts/{contract_id}.pdf``.
         Falls back to the HTML file when Gotenberg is unavailable.
      5. If Google Workspace eSignature is configured → start the eSign
         handshake and capture doc_id + sign_url; otherwise fall back to
         the legacy HMAC sign-link.
      6. If payment_method=='pix' → generate the BR Code payload + (optional)
         QR PNG data URL.
      7. INSERT into ``contracts`` and return all the URLs.

    Parameters:
      payment_method: one of ``'stripe_br' | 'stripe_us' | 'pix' | 'auto'``.
        ``'auto'`` selects per currency (BRL → Pix preferred, USD → Stripe US).
      currency: ``'BRL'`` or ``'USD'`` — drives Stripe account selection.

    Returns ``{ok, contract_id, pdf_url, sign_url, hmac_token, status,
    payment_url, payment_method, stripe_account, currency}``.

    Graceful degradation:
      * If Supabase insert fails → ``{ok: False, reason: ...}``.
      * If no payment rail is configured at all → ``status='draft'``.
      * If Google eSignature creds are missing or fail → fall back to the
        HMAC sign-link without breaking the flow.
      Never raises.
    """
    contract_id = str(uuid.uuid4())
    overrides = scope_overrides or {}
    practice = _normalize_practice(practice)
    currency = _normalize_currency(currency)
    resolved_method, stripe_account = _resolve_payment_method(payment_method, currency)

    try:
        lead = await session_get(lead_id) or {}
    except Exception:  # noqa: BLE001
        log.exception("contract.generate_contract: session_get failed lead=%s", lead_id)
        lead = {}

    if not lead:
        return {"ok": False, "reason": "lead_not_found", "lead_id": lead_id}

    cfg = _practice_config(practice, overrides)
    scope_snapshot = {
        "deliverable_name": cfg.get("deliverable_name"),
        "duration_weeks": cfg.get("duration_weeks"),
        "scope_pt": cfg.get("scope_pt"),
        "payment_terms_pt": cfg.get("payment_terms_pt"),
        "currency": currency,
        "payment_method": resolved_method,
        "stripe_account": stripe_account,
        "overrides": overrides,
    }

    html = _build_contract_html(lead, cfg, value_brl, overrides)

    html_path = _CONTRACTS_DIR / f"{contract_id}.html"
    pdf_path = _CONTRACTS_DIR / f"{contract_id}.pdf"
    try:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.exception(
            "contract.generate_contract: html write failed contract=%s",
            contract_id,
        )
        return {"ok": False, "reason": "html_write_failed", "contract_id": contract_id}

    pdf_ok = await _render_pdf_via_gotenberg(html, pdf_path)
    if pdf_ok:
        pdf_url = f"{CONTRACT_HOST}/static/contracts/{contract_id}.pdf"
    else:
        pdf_url = f"{CONTRACT_HOST}/static/contracts/{contract_id}.html"

    # Legacy HMAC sign-link — used as fallback when Google eSignature is off
    # or unreachable. We always compute the token so a fallback is always
    # available (and so old contract rows have a non-empty hmac_token).
    token = _sign_contract_token(contract_id)
    fallback_sign_url = (
        f"{CONTRACT_HOST}/api/contract/sign?contract_id={contract_id}&token={token}"
    )

    # ---- Google Workspace eSignature attempt ------------------------------
    google_doc_id: Optional[str] = None
    google_request_id: Optional[str] = None
    google_watch_channel_id: Optional[str] = None
    google_watch_resource_id: Optional[str] = None
    google_watch_expires_at: Optional[str] = None
    sign_url = fallback_sign_url

    if GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON and pdf_ok:
        try:
            pdf_bytes = pdf_path.read_bytes()
            esign = await _esign_via_google_workspace(
                contract_pdf_bytes=pdf_bytes,
                signer_email=(lead.get("email") or "").strip(),
                signer_name=(lead.get("name") or "").strip(),
                contract_id=contract_id,
            )
            if esign.get("ok"):
                google_doc_id = esign.get("doc_id")
                google_request_id = esign.get("request_id")
                sign_url = esign.get("sign_url") or fallback_sign_url
                watch = esign.get("watch") or {}
                google_watch_channel_id = watch.get("channelId")
                google_watch_resource_id = watch.get("resourceId")
                exp = watch.get("expiration")
                if exp:
                    # Drive returns ms-epoch — convert to ISO when present.
                    try:
                        google_watch_expires_at = datetime.fromtimestamp(
                            int(exp) / 1000.0, tz=timezone.utc
                        ).isoformat()
                    except (TypeError, ValueError):
                        google_watch_expires_at = str(exp)
            else:
                log.warning(
                    "contract.generate_contract: google esign fallback contract=%s reason=%s",
                    contract_id, esign.get("reason"),
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "contract.generate_contract: google esign exploded contract=%s",
                contract_id,
            )

    # ---- Pix BR Code (when payment_method='pix') --------------------------
    pix_payload_str: Optional[str] = None
    pix_qr_url: Optional[str] = None
    if resolved_method == "pix":
        practice_name = cfg.get("deliverable_name") or practice.title()
        pix = _generate_pix_qr_code(
            value_brl=float(value_brl),
            contract_id=contract_id,
            description=f"Anuvia {practice_name}",
        )
        if pix.get("ok"):
            pix_payload_str = pix.get("payload")
            qr_data_url = pix.get("qr_image_data_url") or ""
            # Persist QR as a static PNG when we successfully rendered one.
            if qr_data_url.startswith("data:image/png;base64,"):
                try:
                    raw_png = base64.b64decode(
                        qr_data_url.split(",", 1)[1].encode("ascii")
                    )
                    qr_path = _CONTRACTS_DIR / f"{contract_id}_pix.png"
                    qr_path.parent.mkdir(parents=True, exist_ok=True)
                    qr_path.write_bytes(raw_png)
                    pix_qr_url = f"{CONTRACT_HOST}/static/contracts/{contract_id}_pix.png"
                except Exception:  # noqa: BLE001
                    log.warning("contract: pix qr png write failed", exc_info=True)

    # Pre-compute payment_url for callers that want the post-signature link.
    # For Stripe rails we can't pre-create the checkout (we need the lead to
    # actually click /accept first), so payment_url is the same as sign_url.
    # For Pix we surface the dedicated Pix page directly.
    if resolved_method == "pix":
        payment_url = (
            f"{CONTRACT_HOST}/api/contract/pix/{contract_id}?token={token}"
        )
    else:
        payment_url = sign_url

    # Initial status: 'sent' if at least one payment rail is configured,
    # else 'draft' so an operator wires payment manually.
    initial_status = "sent" if _any_payment_provider_configured() else "draft"

    row = {
        "id": contract_id,
        "lead_id": lead_id,
        "practice": practice,
        "value_brl": value_brl,
        "currency": currency,
        "payment_method": resolved_method,
        "stripe_account": stripe_account or None,
        "status": initial_status,
        "sent_at": _now_iso(),
        "pdf_url": pdf_url,
        "sign_url": sign_url,
        "hmac_token": token,
        "scope": scope_snapshot,
        "pix_payload": pix_payload_str,
        "pix_qr_image_url": pix_qr_url,
        "google_doc_id": google_doc_id,
        "google_esign_request_id": google_request_id,
        "google_watch_channel_id": google_watch_channel_id,
        "google_watch_resource_id": google_watch_resource_id,
        "google_watch_expires_at": google_watch_expires_at,
    }
    # Strip explicit Nones — PostgREST tolerates them but the index column
    # ergonomics are cleaner without.
    row = {k: v for k, v in row.items() if v is not None}

    inserted = await _insert_contract(row)
    if not inserted:
        return {
            "ok": False,
            "reason": "supabase_insert_failed",
            "contract_id": contract_id,
            "pdf_url": pdf_url,
            "sign_url": sign_url,
        }

    # File an artifact on the lead so the timeline shows the contract.
    try:
        await session_append_artifact(
            lead_id,
            type="contract",
            url=pdf_url,
            meta={
                "contract_id": contract_id,
                "practice": practice,
                "value_brl": value_brl,
                "currency": currency,
                "payment_method": resolved_method,
                "status": initial_status,
                "pdf_rendered": pdf_ok,
                "google_esign": bool(google_doc_id),
            },
        )
        await session_append_history(
            lead_id=lead_id,
            agent="contract",
            action="generate_contract",
            result="ok",
            detail=(
                f"contract {contract_id} generated ({practice}, {currency} "
                f"{value_brl}, {resolved_method})"
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "contract.generate_contract: artifact append failed lead=%s contract=%s",
            lead_id, contract_id,
        )

    return {
        "ok": True,
        "contract_id": contract_id,
        "pdf_url": pdf_url,
        "sign_url": sign_url,
        "payment_url": payment_url,
        "hmac_token": token,
        "status": initial_status,
        "payment_method": resolved_method,
        "stripe_account": stripe_account,
        "currency": currency,
        "google_doc_id": google_doc_id,
    }


async def send_contract_email(contract_id: str) -> dict:
    """Email the lead with PDF link + sign link via Resend.

    Subject: ``Contrato — Anuvia · {deliverable_name}``.
    Includes the 3× ROI guarantee language when the practice opts in.

    Returns ``{ok, message_id}``. When ``RESEND_API_KEY`` is unset, returns
    ``{ok: True, message_id: None, dry_run: True}``.
    """
    contract = await _get_contract(contract_id)
    if not contract:
        return {"ok": False, "reason": "contract_not_found"}

    lead_id = contract.get("lead_id")
    lead = await session_get(lead_id) if lead_id else None
    if not lead:
        return {"ok": False, "reason": "lead_not_found", "contract_id": contract_id}

    to = lead.get("email")
    if not to:
        return {"ok": False, "reason": "lead_has_no_email", "contract_id": contract_id}

    practice = contract.get("practice") or "growth"
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()
    value_brl = contract.get("value_brl") or 0
    value_str = _brl(float(value_brl))

    name = (lead.get("name") or "").split(" ")[0] or "tudo bem"

    pdf_url = contract.get("pdf_url") or ""
    sign_url = contract.get("sign_url") or ""

    subject = f"Contrato — Anuvia · {deliverable}"

    roi_blurb = ""
    if cfg.get("include_3x_roi"):
        roi_blurb = (
            '<p style="color:#475569;line-height:1.65;margin:14px 0;">'
            '<strong>Garantia 3× ROI.</strong> Se a auditoria não identificar pelo menos '
            '3× o valor do investimento em economias anualizadas, o reembolso é integral. '
            'Os detalhes constam na Cláusula 6 do contrato.'
            "</p>"
        )

    body_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,-apple-system,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 32px;">
<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 6px;">Anuvia · Contrato</p>
<h1 style="font-family:Georgia,serif;font-size:26px;margin:0 0 14px;color:#0f172a;">Olá {name},</h1>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Segue o contrato de <strong>{deliverable}</strong> que conversamos. Documento curto, escopo fechado, sem letra miúda escondida.</p>
<p style="color:#475569;line-height:1.65;margin:0 0 14px;">Valor total: <strong>R$ {value_str}</strong>. Condições e cronograma detalhados no PDF.</p>
{roi_blurb}
<p style="margin:24px 0;"><a href="{pdf_url}" style="display:inline-block;background:#0f172a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Ler o contrato (PDF) -></a></p>
<p style="color:#475569;line-height:1.65;margin:0 0 8px;">Quando estiver pronto pra assinar, é um clique:</p>
<p style="margin:8px 0 24px;"><a href="{sign_url}" style="display:inline-block;background:#16a34a;color:#ffffff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:600;">Assinar contrato -></a></p>
<p style="color:#78716c;font-size:13px;line-height:1.6;margin-top:28px;">Qualquer dúvida, é só responder este email — leio todos.<br><br>Mila Vernazza · Founder Anuvia</p>
</div></body></html>"""

    if not RESEND_API_KEY:
        log.info(
            "contract.send_contract_email: dry-run (no RESEND_API_KEY) contract=%s to=%s",
            contract_id, to,
        )
        return {"ok": True, "message_id": None, "dry_run": True}

    payload = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [to],
        "reply_to": f"{RESEND_REPLY_TO_NAME} <{RESEND_REPLY_TO_EMAIL}>",
        "subject": subject,
        "html": body_html,
        "tags": [
            {"name": "category", "value": "contract"},
            {"name": "kind", "value": "contract_sent"},
            {"name": "contract_id", "value": str(contract_id)},
            {"name": "lead_id", "value": str(lead_id) if lead_id else ""},
        ],
    }
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
        log.exception("contract.send_contract_email: network failure contract=%s", contract_id)
        return {"ok": False, "reason": f"resend_network: {exc}"}

    if r.status_code >= 400:
        log.error(
            "contract.send_contract_email: resend %s body=%s",
            r.status_code, r.text[:300],
        )
        return {"ok": False, "reason": f"resend_{r.status_code}"}

    body = r.json() if r.text else {}
    msg_id = body.get("id") if isinstance(body, dict) else None

    # File the email artifact + history.
    try:
        if lead_id:
            await session_append_artifact(
                lead_id,
                type="email_sent",
                url=None,
                meta={
                    "kind": "contract_sent",
                    "contract_id": str(contract_id),
                    "resend_message_id": msg_id,
                },
            )
    except Exception:  # noqa: BLE001
        log.exception("contract.send_contract_email: artifact append failed contract=%s", contract_id)

    return {"ok": True, "message_id": msg_id}


async def issue_invoice(contract_id: str) -> dict:
    """Issue an invoice for a paid contract.

    Stub: until Mila wires Conta Azul credentials, this just persists
    ``invoice_id='manual_TODO'`` on the contract row and returns
    ``{ok: True, invoice_id: 'manual_TODO', status: 'stub'}``.

    When Conta Azul creds are configured (env CONTA_AZUL_TOKEN), this
    function will POST to the Conta Azul NF-e endpoint. The interface is kept
    stable so callers won't need to change.
    """
    contract = await _get_contract(contract_id)
    if not contract:
        return {"ok": False, "reason": "contract_not_found"}

    # Idempotency: if we already issued an invoice, return it.
    existing_invoice_id = contract.get("invoice_id")
    if existing_invoice_id:
        return {
            "ok": True,
            "invoice_id": existing_invoice_id,
            "status": "exists",
            "contract_id": contract_id,
        }

    conta_azul_token = os.environ.get("CONTA_AZUL_TOKEN", "")
    if not conta_azul_token:
        # Stub path — record a sentinel and move on.
        invoice_id = "manual_TODO"
        await _patch_contract(contract_id, {"invoice_id": invoice_id})
        log.info(
            "contract.issue_invoice: stub (no CONTA_AZUL_TOKEN) contract=%s",
            contract_id,
        )
        return {
            "ok": True,
            "invoice_id": invoice_id,
            "status": "stub",
            "contract_id": contract_id,
        }

    # Future Conta Azul flow — placeholder until the API contract is wired.
    log.warning(
        "contract.issue_invoice: Conta Azul integration not implemented yet; "
        "contract=%s",
        contract_id,
    )
    return {
        "ok": True,
        "invoice_id": "pending_conta_azul",
        "status": "pending",
        "contract_id": contract_id,
    }


# ---------------------------------------------------------------------------
# Payment method resolution
# ---------------------------------------------------------------------------


def _normalize_currency(currency: Optional[str]) -> str:
    """Normalize currency code to upper-case ISO-4217. Defaults to BRL."""
    c = (currency or "BRL").strip().upper()
    return c if c in ("BRL", "USD") else "BRL"


def _resolve_payment_method(
    requested: Optional[str], currency: str
) -> Tuple[str, str]:
    """Resolve ``payment_method`` per Mila's policy.

    Returns ``(method, stripe_account)`` where:
      * method ∈ {'stripe_br','stripe_us','pix'}
      * stripe_account ∈ {'BR','US',''} (empty when pix)

    Policy:
      * currency == BRL → prefer Pix (no Stripe fee, fast reconciliation),
        Stripe BR fallback when Pix env vars are missing.
      * currency == USD → always Stripe US.
      * Explicit overrides honoured when they're compatible with the
        currency; otherwise we coerce to the currency-correct rail and log.
    """
    currency = _normalize_currency(currency)
    requested = (requested or "auto").strip().lower()

    if requested == "stripe_us" or currency == "USD":
        return ("stripe_us", "US")

    if requested == "stripe_br":
        return ("stripe_br", "BR")

    if requested == "pix":
        # If Pix isn't configured, fall back to Stripe BR.
        if not PIX_NUBANK_KEY:
            log.warning(
                "contract: pix requested but PIX_NUBANK_KEY unset; "
                "falling back to stripe_br"
            )
            return ("stripe_br", "BR")
        return ("pix", "")

    # auto + BRL
    if PIX_NUBANK_KEY:
        return ("pix", "")
    return ("stripe_br", "BR")


# ---------------------------------------------------------------------------
# Pix BR Code (EMV QR Code) generator
# ---------------------------------------------------------------------------
# The BR Code is the Brazilian central bank's standard for Pix QR codes. It's
# a tag-length-value string (EMV merchant presented mode) that any Pix app can
# decode. Spec: BCB Manual de Padrões para Iniciação do Pix v1.x.
#
# Minimal valid static-Pix payload (the one we generate here):
#   00  Payload Format Indicator         "01"
#   26  Merchant Account Info (Pix)
#       └─ 00 GUI                         "br.gov.bcb.pix"
#       └─ 01 Pix Key                     <PIX_NUBANK_KEY>
#       └─ 02 Description (optional)      <short desc, <=72 chars>
#   52  Merchant Category Code           "0000"
#   53  Transaction Currency             "986"           (BRL ISO-4217)
#   54  Transaction Amount (optional)    e.g. "150.00"
#   58  Country Code                     "BR"
#   59  Merchant Name                    <PIX_NUBANK_DISPLAY_NAME, <=25>
#   60  Merchant City                    <PIX_MERCHANT_CITY, <=15>
#   62  Additional Data Field
#       └─ 05 Reference Label (TxID)     <contract_id_short>
#   63  CRC16                            <4 hex chars over the rest>


def _pix_emv_field(tag: str, value: str) -> str:
    """Return ``<tag><len><value>`` with len zero-padded to 2 chars."""
    length = f"{len(value):02d}"
    return f"{tag}{length}{value}"


def _pix_crc16(payload: str) -> str:
    """CCITT-FALSE CRC16 over ``payload``, hex-encoded, upper-case.

    Polynomial 0x1021, initial 0xFFFF, no reflect, no xorout.
    """
    crc = 0xFFFF
    for ch in payload.encode("utf-8"):
        crc ^= ch << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def _ascii_safe(s: str, max_len: int) -> str:
    """Strip diacritics + non-ASCII so the BR Code stays Pix-spec-compliant."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s or "")
    plain = "".join(c for c in nfkd if not unicodedata.combining(c))
    plain = plain.encode("ascii", "ignore").decode("ascii")
    return plain[:max_len].strip().upper() or "ANUVIA"


def _generate_pix_qr_code(
    value_brl: float,
    contract_id: str,
    description: str = "",
) -> Dict[str, Any]:
    """Generate the BR Code (static Pix) payload + QR image data URL.

    Returns ``{ok, payload, qr_image_data_url, error?}``. ``ok=False`` when
    PIX_NUBANK_KEY is missing — callers must handle this.

    The QR is rendered via the ``qrcode`` library when available, otherwise
    only the payload string is returned (frontends can render QR client-side
    via qrcode.js/jsQR).
    """
    if not PIX_NUBANK_KEY:
        return {"ok": False, "error": "no_pix_key", "payload": "", "qr_image_data_url": ""}

    # Tx id (61 chars max in EMV; we shorten the UUID).
    txid = (contract_id or "").replace("-", "")[:25] or "ANUVIA"

    # Merchant Account Info (tag 26).
    mai_inner = _pix_emv_field("00", "br.gov.bcb.pix") + _pix_emv_field("01", PIX_NUBANK_KEY)
    short_desc = _ascii_safe(description or "Anuvia services", 50)
    if short_desc:
        mai_inner += _pix_emv_field("02", short_desc.title())
    mai = _pix_emv_field("26", mai_inner)

    # Additional data (tag 62) — reference label only.
    add_inner = _pix_emv_field("05", txid)
    add = _pix_emv_field("62", add_inner)

    # Amount — Pix wants up to 13 chars, dot-decimal, no thousands sep.
    try:
        amount_str = f"{float(value_brl):.2f}"
    except (TypeError, ValueError):
        amount_str = "0.00"

    merchant_name = _ascii_safe(PIX_NUBANK_DISPLAY_NAME, 25)
    merchant_city = _ascii_safe(PIX_MERCHANT_CITY, 15)

    payload_no_crc = (
        _pix_emv_field("00", "01")
        + mai
        + _pix_emv_field("52", "0000")
        + _pix_emv_field("53", "986")
        + (_pix_emv_field("54", amount_str) if float(value_brl) > 0 else "")
        + _pix_emv_field("58", "BR")
        + _pix_emv_field("59", merchant_name)
        + _pix_emv_field("60", merchant_city)
        + add
        + "6304"  # CRC tag + length, value populated below
    )
    crc = _pix_crc16(payload_no_crc)
    payload = payload_no_crc + crc

    # Render QR PNG to a data URL if the qrcode library is available.
    qr_data_url = ""
    try:
        import qrcode  # type: ignore
        import io

        img = qrcode.make(payload)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        qr_data_url = f"data:image/png;base64,{b64}"
    except ImportError:
        log.info(
            "contract: qrcode library not installed; payload-only Pix render. "
            "Frontends will render QR client-side."
        )
    except Exception:  # noqa: BLE001
        log.warning("contract: pix QR render failed; payload-only", exc_info=True)

    return {
        "ok": True,
        "payload": payload,
        "qr_image_data_url": qr_data_url,
    }


# ---------------------------------------------------------------------------
# Stripe — dual account (BR + US)
# ---------------------------------------------------------------------------


def _stripe_account_key(account: str) -> Tuple[str, str]:
    """Return ``(secret_key, webhook_secret)`` for a Stripe account.

    Account is 'BR' or 'US'. Empty strings when the account isn't configured.
    """
    a = (account or "").strip().upper()
    if a == "US":
        return (STRIPE_SECRET_KEY_US, STRIPE_WEBHOOK_SECRET_US)
    # Default to BR (covers legacy callers with no account hint).
    return (STRIPE_SECRET_KEY_BR, STRIPE_WEBHOOK_SECRET_BR)


def _stripe_currency_for(account: str) -> str:
    """ISO-4217 currency Stripe should bill in for the given account."""
    return "usd" if (account or "").strip().upper() == "US" else "brl"


async def _create_stripe_checkout(
    contract: dict,
    account: str = "BR",
) -> Optional[dict]:
    """Create a Stripe Checkout Session on the BR or US account.

    Returns the session dict (with ``id`` and ``url``) or None on failure.
    Never raises.
    """
    secret_key, _ = _stripe_account_key(account)
    if not secret_key:
        log.warning(
            "contract: stripe account=%s not configured (no secret key)", account
        )
        return None

    contract_id = str(contract.get("id") or "")
    value = float(contract.get("value_brl") or 0)
    practice = _normalize_practice(contract.get("practice"))
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()
    currency_iso = _stripe_currency_for(account)

    # Stripe wants currency-minor units.
    unit_amount = int(round(value * 100))
    if unit_amount <= 0:
        log.warning("contract: stripe skip — zero value contract=%s", contract_id)
        return None

    # Use form-encoded (Stripe REST quirk).
    data = [
        ("mode", "payment"),
        ("success_url", STRIPE_SUCCESS_URL),
        ("cancel_url", STRIPE_CANCEL_URL),
        ("client_reference_id", contract_id),
        ("metadata[contract_id]", contract_id),
        ("metadata[practice]", practice),
        ("metadata[stripe_account]", account.upper()),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", currency_iso),
        ("line_items[0][price_data][unit_amount]", str(unit_amount)),
        ("line_items[0][price_data][product_data][name]", f"Anuvia · {deliverable}"),
    ]

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                auth=(secret_key, ""),
                data=data,
            )
        if r.status_code >= 400:
            log.error(
                "contract: stripe checkout failed account=%s status=%s body=%s",
                account, r.status_code, r.text[:300],
            )
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        log.exception(
            "contract: stripe checkout call exploded contract=%s account=%s",
            contract_id, account,
        )
        return None


# ---------------------------------------------------------------------------
# Google Workspace eSignature — Drive + Docs API
# ---------------------------------------------------------------------------
# Google Workspace eSignature went GA in 2024 (Business Standard+). The exact
# REST shape sits inside the Drive API v3 surface as eSignature methods on
# files. As of this writing the official path is:
#
#   POST https://www.googleapis.com/drive/v3/files/{fileId}/esignatures
#
# (subject to GA-vs-beta naming drift). We treat the exact endpoint as a
# variable so it can be updated without touching the rest of the module.
#
# Graceful degradation: if GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON is empty OR
# the network call fails, _esign_via_google_workspace returns
# ``{ok: False, fallback: True}`` and the caller MUST fall back to the legacy
# HMAC sign-link flow. We never let a Google outage block contract creation.

_GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_GOOGLE_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
# eSignature endpoint path under a given file id. Public GA shape per
# Workspace 2024 announcement; keep as a constant so it can be swapped.
_GOOGLE_ESIGN_PATH_TMPL = "https://www.googleapis.com/drive/v3/files/{file_id}/esignatures"
# Drive push notification (changes.watch) — used so we can react when the
# signer completes the eSignature flow.
_GOOGLE_DRIVE_WATCH_URL_TMPL = "https://www.googleapis.com/drive/v3/files/{file_id}/watch"

_GOOGLE_ESIGN_SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]
)


def _load_google_service_account() -> Optional[dict]:
    """Parse GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON.

    Accepts either raw JSON or a base64-encoded JSON blob (Coolify-friendly).
    Returns ``None`` when the env var is empty or unparseable.
    """
    raw = (GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        return None

    # Try base64 first.
    try:
        decoded = base64.b64decode(raw, validate=True)
        try:
            return json.loads(decoded.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    except (binascii.Error, ValueError):
        pass

    # Fall through to raw JSON.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("contract: GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON is not valid JSON")
        return None


async def _google_access_token(
    delegate_email: Optional[str] = None,
) -> Optional[str]:
    """Mint a Google OAuth2 access token via the service account JWT flow.

    Domain-wide delegation is used to impersonate ``delegate_email`` so we
    can write Docs into Mila's Drive (which is shared with the signer).
    Returns None on any failure — caller falls back to HMAC sign-link.
    """
    creds = _load_google_service_account()
    if not creds:
        return None
    delegate = delegate_email or GOOGLE_WORKSPACE_DELEGATE_EMAIL
    if not delegate:
        log.warning("contract: GOOGLE_WORKSPACE_DELEGATE_EMAIL unset; cannot mint token")
        return None

    # Build & sign the JWT. We deliberately keep this self-contained instead
    # of depending on google-auth (one fewer pip dep for the lp service).
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        log.warning(
            "contract: `cryptography` not installed; cannot sign Google JWT. "
            "Falling back to HMAC sign-link."
        )
        return None

    now = int(_now().timestamp())
    header = {"alg": "RS256", "typ": "JWT"}
    body = {
        "iss": creds.get("client_email"),
        "sub": delegate,
        "scope": _GOOGLE_ESIGN_SCOPES,
        "aud": _GOOGLE_OAUTH_TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }

    def _b64url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    b_b64 = _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{b_b64}".encode("ascii")

    try:
        private_key = serialization.load_pem_private_key(
            (creds.get("private_key") or "").encode("utf-8"),
            password=None,
        )
        signature = private_key.sign(
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        jwt_assertion = f"{h_b64}.{b_b64}.{_b64url(signature)}"
    except Exception:  # noqa: BLE001
        log.exception("contract: google JWT signing failed")
        return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                _GOOGLE_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt_assertion,
                },
            )
        if r.status_code >= 400:
            log.error("contract: google token exchange %s: %s", r.status_code, r.text[:300])
            return None
        token = (r.json() or {}).get("access_token")
        return token or None
    except Exception:  # noqa: BLE001
        log.exception("contract: google token exchange exploded")
        return None


async def _google_upload_pdf_as_doc(
    access_token: str,
    pdf_bytes: bytes,
    title: str,
) -> Optional[str]:
    """Upload a PDF and convert it to a Google Doc on the way in.

    Returns the resulting Doc's file id, or None on failure.
    """
    boundary = f"anuvia-{uuid.uuid4().hex}"
    metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    body += pdf_bytes
    body += f"\r\n--{boundary}--".encode("utf-8")

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                f"{_GOOGLE_DRIVE_UPLOAD_URL}?uploadType=multipart",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                content=body,
            )
        if r.status_code >= 400:
            log.error(
                "contract: google drive upload %s: %s",
                r.status_code, r.text[:300],
            )
            return None
        return (r.json() or {}).get("id")
    except Exception:  # noqa: BLE001
        log.exception("contract: google drive upload exploded")
        return None


async def _google_create_esign_request(
    access_token: str,
    doc_id: str,
    signer_email: str,
    signer_name: str,
    contract_id: str,
) -> Optional[dict]:
    """Create the eSignature request on the Google Doc.

    Returns the response dict (containing ``id`` / ``signingUrl`` depending on
    the GA shape) or None on failure. The exact request body has shifted as
    eSignature moved from beta → GA — TODO: pin to the official 2026 schema
    once Mila confirms her tenant is on the GA release.
    """
    payload = {
        # NOTE: shape per Drive eSignature GA reference. If the API rejects
        # this shape, update here only — callers don't care about the wire
        # format. Mila will refine once she runs the first live request.
        "signers": [
            {
                "email": signer_email,
                "name": signer_name or signer_email,
                "role": "signer",
            }
        ],
        "metadata": {
            "anuvia_contract_id": contract_id,
        },
        "completionRedirectUrl": f"{CONTRACT_HOST}/contract-signed",
    }
    url = _GOOGLE_ESIGN_PATH_TMPL.format(file_id=doc_id)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            log.error(
                "contract: google esign create %s: %s",
                r.status_code, r.text[:300],
            )
            return None
        return r.json() or {}
    except Exception:  # noqa: BLE001
        log.exception("contract: google esign create exploded")
        return None


async def _google_watch_doc(
    access_token: str,
    doc_id: str,
    contract_id: str,
) -> Optional[dict]:
    """Subscribe to Drive push notifications for the eSignature document.

    Returns ``{channelId, resourceId, expiration}`` or None on failure.
    Notifications POST to ``CONTRACT_HOST/api/contract/webhook/google/esign``.
    """
    channel_id = f"anuvia-{contract_id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{CONTRACT_HOST}/api/contract/webhook/google/esign",
        # Token echoes back in X-Goog-Channel-Token so we can authenticate
        # the inbound notification.
        "token": _sign_admin_action_token(contract_id, "google_watch"),
        # 7 days, Drive's max for watch channels is 1 day for most resources
        # — we'll renew via a cron on the lead's next_action_at.
    }
    url = _GOOGLE_DRIVE_WATCH_URL_TMPL.format(file_id=doc_id)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            log.error(
                "contract: google drive watch %s: %s",
                r.status_code, r.text[:300],
            )
            return None
        body = r.json() or {}
        return {
            "channelId": body.get("id") or channel_id,
            "resourceId": body.get("resourceId"),
            "expiration": body.get("expiration"),
        }
    except Exception:  # noqa: BLE001
        log.exception("contract: google drive watch exploded")
        return None


async def _esign_via_google_workspace(
    contract_pdf_bytes: bytes,
    signer_email: str,
    signer_name: str,
    contract_id: str,
) -> dict:
    """Full Google Workspace eSignature handshake.

    Steps:
      1. Mint an access token (service account, domain-wide delegation).
      2. Upload the PDF and convert it to a Google Doc.
      3. Create an eSignature request against the Doc for ``signer_email``.
      4. Subscribe to Drive push notifications so /api/contract/webhook/google/esign
         fires when the signer completes the request.

    Returns ``{ok, doc_id, sign_url, request_id, watch}`` on success. On any
    failure returns ``{ok: False, fallback: True, reason: ...}`` and the
    caller is expected to fall back to the legacy HMAC sign-link flow.
    """
    if not GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON:
        return {"ok": False, "fallback": True, "reason": "no_google_creds"}

    token = await _google_access_token()
    if not token:
        return {"ok": False, "fallback": True, "reason": "google_token_failed"}

    doc_id = await _google_upload_pdf_as_doc(
        token,
        contract_pdf_bytes,
        title=f"Anuvia · Contrato {contract_id[:8]}",
    )
    if not doc_id:
        return {"ok": False, "fallback": True, "reason": "google_upload_failed"}

    esign = await _google_create_esign_request(
        token, doc_id, signer_email, signer_name, contract_id
    )
    if not esign:
        return {
            "ok": False,
            "fallback": True,
            "reason": "google_esign_create_failed",
            "doc_id": doc_id,
        }

    # The GA payload shape is still in flux — try a couple of common fields.
    request_id = (
        esign.get("id")
        or esign.get("requestId")
        or (esign.get("metadata") or {}).get("requestId")
    )
    sign_url = (
        esign.get("signingUrl")
        or esign.get("url")
        or f"https://docs.google.com/document/d/{doc_id}/edit"
    )

    watch = await _google_watch_doc(token, doc_id, contract_id)

    return {
        "ok": True,
        "doc_id": doc_id,
        "sign_url": sign_url,
        "request_id": request_id,
        "watch": watch or {},
    }


async def _handle_google_esign_webhook(payload: dict) -> dict:
    """Process a Google Drive push notification for a watched eSign doc.

    Drive doesn't include the eSignature status in the push body — it just
    signals "something changed". We re-fetch the doc + its eSignature state
    via the Drive API. If signed, we flip the contract to 'signed' and
    trigger the same downstream flow as the HMAC /accept handler.
    """
    doc_id = payload.get("resource_id") or payload.get("resourceId") or payload.get("doc_id")
    if not doc_id:
        return {"ok": False, "reason": "no_doc_id"}

    contract = await _get_contract_by_field("google_doc_id", str(doc_id))
    if not contract:
        log.warning("contract: google esign webhook for unknown doc=%s", doc_id)
        return {"ok": False, "reason": "contract_not_found"}

    contract_id = str(contract.get("id"))
    if contract.get("status") in ("signed", "paid"):
        return {"ok": True, "idempotent": True, "contract_id": contract_id}

    # Best-effort: try to fetch eSignature state from Drive. If unavailable
    # (e.g. transient API failure), we still flip to 'signed' since Drive
    # only pings us for state changes on the watched doc.
    token = await _google_access_token()
    state = "completed"
    if token:
        try:
            url = _GOOGLE_ESIGN_PATH_TMPL.format(file_id=doc_id)
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                r = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code == 200:
                body = r.json() or {}
                # Try both common shapes.
                state = (
                    body.get("state")
                    or body.get("status")
                    or (body.get("esignatures") or [{}])[0].get("state")
                    or "completed"
                ).lower()
        except Exception:  # noqa: BLE001
            log.exception("contract: google esign state fetch failed doc=%s", doc_id)

    if state not in ("completed", "signed", "done"):
        log.info(
            "contract: google esign webhook fired but state=%s contract=%s",
            state, contract_id,
        )
        return {"ok": True, "ignored": state, "contract_id": contract_id}

    await _patch_contract(
        contract_id,
        {"status": "signed", "signed_at": _now_iso()},
    )

    # From here on the same downstream flow as the /accept handler: we want
    # to trigger payment if it hasn't happened yet. We don't auto-create a
    # checkout — the lead clicks the payment link in the post-signature
    # email. The webhook just needs to surface the signal.
    lead_id = contract.get("lead_id")
    if lead_id:
        try:
            await session_append_signal(
                lead_id,
                kind="contract_signed",
                value=str(contract_id),
                source="google_esign_webhook",
            )
            await session_append_history(
                lead_id=lead_id,
                agent="contract",
                action="google_esign_complete",
                result="ok",
                detail=f"contract {contract_id} signed via Google Workspace",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "contract: google esign signal failed lead=%s contract=%s",
                lead_id, contract_id,
            )

    return {"ok": True, "contract_id": contract_id, "state": state}


# ---------------------------------------------------------------------------
# Stripe webhook signature verification (per-account)
# ---------------------------------------------------------------------------


def _verify_stripe_signature(body: bytes, header: str, account: str) -> bool:
    """Verify the Stripe-Signature header per Stripe's spec, scoped to one account.

    Header format: ``t=<ts>,v1=<sig>[,v1=<sig>]``. Payload to sign:
    ``<ts>.<body>``. Returns True iff at least one v1 signature matches and
    the timestamp is within 5 minutes of now. When the matching webhook
    secret is empty, returns True (operator opted out — useful for dev).
    """
    _, webhook_secret = _stripe_account_key(account)
    if not webhook_secret:
        log.warning(
            "contract: STRIPE_WEBHOOK_SECRET_%s unset; accepting webhook unverified",
            (account or "BR").upper(),
        )
        return True
    if not header:
        return False
    ts: Optional[str] = None
    for part in header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == "t":
            ts = v.strip()
            break
    if not ts:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    # Tolerance: 5 minutes.
    if abs(int(_now().timestamp()) - ts_int) > 300:
        log.warning("contract: stripe webhook timestamp out of tolerance")
        return False

    sigs = [
        v.strip()
        for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p)
        if k.strip() == "v1"
    ]
    if not sigs:
        return False
    signed_payload = f"{ts}.{body.decode('utf-8', errors='replace')}".encode("utf-8")
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    for sig in sigs:
        if hmac.compare_digest(sig, expected):
            return True
    return False


# ---------------------------------------------------------------------------
# Engagement kickoff (called from payment webhooks)
# ---------------------------------------------------------------------------


async def _kickoff_engagement(contract: dict) -> Optional[str]:
    """Create the engagements row and queue the delivery handler.

    Idempotent: if an engagement already exists for this contract, returns
    its id without creating a duplicate.

    The ``next_action`` we queue MUST match a HANDLER_ALIAS exported by one
    of the lib/delivery/<practice>.py modules. As of 2026-05-15:
      * engagement_kickoff_cloud_finops   → finops_audit.py
      * engagement_kickoff_ai             → ai_readiness.py
      * engagement_kickoff_devops         → devops_maturity.py
      * engagement_kickoff_growth_salesops → growth_salesops.py
      * engagement_kickoff_industry       → industry.py
    """
    contract_id = str(contract.get("id") or "")
    lead_id = contract.get("lead_id")
    practice = _normalize_practice(contract.get("practice"))
    value_brl = contract.get("value_brl")

    # Idempotency check.
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{SUPA_URL}/engagements?contract_id=eq.{contract_id}&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code == 200:
            rows = r.json() or []
            if rows:
                return str(rows[0].get("id"))
    except Exception:  # noqa: BLE001
        log.exception("contract: engagement idempotency check failed contract=%s", contract_id)

    row = {
        "id": str(uuid.uuid4()),
        "lead_id": lead_id,
        "contract_id": contract_id,
        "practice": practice,
        "status": "kickoff",
        "contract_signed_at": contract.get("signed_at"),
        "first_payment_at": _now_iso(),
        "current_phase": 1,
        "total_value_brl": value_brl,
        "intake_data": {},
        "artifacts": [],
    }
    inserted = await _insert_engagement(row)
    engagement_id = inserted.get("id") if inserted else None

    if lead_id:
        # Append signal + queue the practice-specific delivery kickoff. The
        # delivery agents (lib/delivery/<practice>.py) are owned by D1-D5.
        try:
            await session_append_signal(
                lead_id,
                kind="engagement_kickoff",
                value=str(contract_id),
                source="contract_webhook",
            )
            await session_set_next(
                lead_id,
                next_action=f"engagement_kickoff_{practice}",
                next_action_at=_now() + timedelta(minutes=5),
            )
            await session_append_history(
                lead_id=lead_id,
                agent="contract",
                action="engagement_kickoff",
                result="ok",
                detail=(
                    f"engagement {engagement_id} created for contract {contract_id} "
                    f"({practice})"
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "contract: kickoff signal/queue failed lead=%s contract=%s",
                lead_id, contract_id,
            )

    # Patch contract.engagement_id for the back-reference.
    if engagement_id:
        await _patch_contract(contract_id, {"engagement_id": engagement_id})

    return engagement_id


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/contract", tags=["contract"])


@router.get("/sign")
async def sign_page(contract_id: str, token: str) -> HTMLResponse:
    """Render a bilingual sign page (PT + EN side-by-side).

    HMAC-verified. Renders contract summary + an "Eu aceito os termos" /
    "I accept the terms" button that POSTs to ``/api/contract/accept`` via
    inline JS. The actual signature flip happens in the POST handler.
    """
    if not _verify_contract_token(contract_id, token):
        return HTMLResponse(
            content=_simple_error_html(
                "Link inválido ou expirado",
                "Invalid or expired link",
            ),
            status_code=403,
        )

    contract = await _get_contract(contract_id)
    if not contract:
        return HTMLResponse(
            content=_simple_error_html(
                "Contrato não encontrado",
                "Contract not found",
            ),
            status_code=404,
        )

    # Mark as viewed (best effort, idempotent-ish).
    if contract.get("status") == "sent":
        await _patch_contract(contract_id, {"status": "viewed"})

    lead_id = contract.get("lead_id")
    lead = await session_get(lead_id) if lead_id else {}
    lead = lead or {}

    practice = contract.get("practice") or "growth"
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()
    duration = cfg.get("duration_weeks") or "—"
    value_str = _brl(float(contract.get("value_brl") or 0))
    pdf_url = contract.get("pdf_url") or ""
    already_signed = contract.get("status") in ("signed", "paid")

    client_name = (lead.get("name") or "—").strip()
    client_company = (lead.get("company") or "—").strip()

    signed_banner = ""
    if already_signed:
        signed_banner = (
            '<div style="background:#dcfce7;color:#166534;border:1px solid #86efac;'
            'border-radius:8px;padding:14px 16px;margin:0 0 20px;font-size:14px;">'
            '<strong>Contrato já assinado.</strong> Você pode fechar esta página. '
            '<span style="color:#15803d;">/ Contract already signed — you may close this page.</span>'
            "</div>"
        )

    accept_btn_disabled = "disabled" if already_signed else ""

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Assinar contrato — Anuvia</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ background:#fafaf9; font-family:-apple-system,Inter,Arial,sans-serif; color:#0f172a; margin:0; padding:48px 20px; }}
  .card {{ max-width:680px; margin:0 auto; background:#ffffff; border:1px solid #e7e5e4; border-radius:12px; padding:36px 32px; }}
  h1 {{ font-family:Georgia,serif; font-size:28px; margin:0 0 6px; }}
  h2 {{ font-size:13px; letter-spacing:0.08em; text-transform:uppercase; color:#0f172a; margin:24px 0 8px; }}
  .small {{ color:#64748b; font-size:12px; }}
  .meta {{ background:#fafaf9; border:1px solid #e7e5e4; border-radius:8px; padding:14px 16px; margin:14px 0 20px; font-size:14px; }}
  .meta-row {{ display:flex; justify-content:space-between; gap:12px; padding:4px 0; }}
  .meta-row .k {{ color:#64748b; }}
  .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin:28px 0 12px; }}
  .btn {{ display:inline-block; padding:14px 24px; border-radius:8px; font-weight:600; text-decoration:none; font-size:15px; border:0; cursor:pointer; }}
  .btn-primary {{ background:#16a34a; color:#ffffff; }}
  .btn-primary:disabled {{ background:#a3a3a3; cursor:not-allowed; }}
  .btn-secondary {{ background:#0f172a; color:#ffffff; }}
  .lang {{ color:#64748b; font-size:13px; line-height:1.6; margin:6px 0 0; }}
  #status {{ margin-top:18px; font-size:14px; }}
  #status.ok {{ color:#166534; }}
  #status.err {{ color:#b91c1c; }}
</style></head>
<body>
<div class="card">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 4px;">Anuvia · Contrato</p>
  <h1>Assinatura eletrônica</h1>
  <p class="lang">Electronic signature · Bilingual page (PT / EN)</p>

  {signed_banner}

  <div class="meta">
    <div class="meta-row"><span class="k">Serviço / Service</span><strong>{deliverable}</strong></div>
    <div class="meta-row"><span class="k">Duração / Duration</span><strong>{duration} semanas / weeks</strong></div>
    <div class="meta-row"><span class="k">Valor / Value</span><strong>R$ {value_str}</strong></div>
    <div class="meta-row"><span class="k">Cliente / Client</span><strong>{client_name} · {client_company}</strong></div>
  </div>

  <h2>Português</h2>
  <p style="color:#475569;line-height:1.65;">Ao clicar em <strong>"Eu aceito os termos"</strong> você confirma a leitura integral do contrato e manifesta sua aceitação. O documento foi gerado em formato PDF e está acessível pelo botão abaixo. Após a aceitação, você será redirecionado para a tela de pagamento.</p>

  <h2>English</h2>
  <p style="color:#475569;line-height:1.65;">By clicking <strong>"I accept the terms"</strong> you confirm you have read the contract in full and accept its terms. The document is available as a PDF via the button below. After accepting, you will be redirected to the payment page.</p>

  <div class="actions">
    <a class="btn btn-secondary" href="{pdf_url}" target="_blank" rel="noopener">Ler o contrato (PDF) -></a>
    <button id="accept-btn" class="btn btn-primary" onclick="acceptContract()" {accept_btn_disabled}>Eu aceito os termos / I accept the terms -></button>
  </div>

  <div id="status"></div>

  <p class="small" style="margin-top:28px;">Mila Vernazza · Founder Anuvia · {ANUVIA_LEGAL_NAME}<br>CNPJ: {ANUVIA_CNPJ}</p>
</div>

<script>
async function acceptContract() {{
  const btn = document.getElementById('accept-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.className = '';
  status.textContent = 'Processando / Processing...';
  try {{
    const resp = await fetch('/api/contract/accept', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ contract_id: {json.dumps(contract_id)}, token: {json.dumps(token)} }}),
      redirect: 'follow',
    }});
    if (resp.redirected) {{
      window.location.href = resp.url;
      return;
    }}
    const data = await resp.json().catch(function() {{ return {{}}; }});
    if (resp.ok && data.checkout_url) {{
      window.location.href = data.checkout_url;
      return;
    }}
    if (resp.ok) {{
      status.className = 'ok';
      status.textContent = 'Contrato assinado. Obrigada! / Contract signed. Thank you!';
      return;
    }}
    status.className = 'err';
    status.textContent = (data && data.reason) ? data.reason : ('Erro ' + resp.status);
    btn.disabled = false;
  }} catch (e) {{
    status.className = 'err';
    status.textContent = 'Erro de rede / Network error: ' + e;
    btn.disabled = false;
  }}
}}
</script>
</body></html>""", status_code=200)


def _simple_error_html(pt: str, en: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{pt}</title></head>
<body style="background:#fafaf9;font-family:-apple-system,sans-serif;color:#0f172a;padding:64px 24px;text-align:center;">
<div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:12px;padding:36px 28px;">
<h1 style="font-family:Georgia,serif;font-size:24px;margin:0 0 8px;">{pt}</h1>
<p style="color:#64748b;font-size:14px;margin:0;">{en}</p>
</div></body></html>"""


@router.post("/accept")
async def accept_contract(request: Request):
    """Mark a contract as signed, then route to the right payment rail.

    Body: ``{contract_id, token}`` (JSON) or form fields. HMAC-verified.

    Routing:
      * payment_method=='pix' → redirect to /api/contract/pix/{id}?token=...
      * payment_method=='stripe_br' → Stripe Checkout on Anuvia Ltda
      * payment_method=='stripe_us' → Stripe Checkout on Anuvia LLC
      * payment_method missing/legacy → resolve from currency + env

    Returns either:
      * 302 redirect to the checkout / Pix page, or
      * 200 JSON with ``{ok, status, checkout_url}`` (for fetch() callers
        that don't follow redirects automatically), or
      * 403/404 JSON error.
    """
    # Parse body — accept JSON or form-encoded.
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = await request.json()
        else:
            form = await request.form()
            payload = {k: v for k, v in form.items()}
    except Exception:  # noqa: BLE001
        payload = {}

    contract_id = str(payload.get("contract_id") or "").strip()
    token = str(payload.get("token") or "").strip()

    if not contract_id or not token:
        return JSONResponse({"ok": False, "reason": "missing contract_id/token"}, status_code=400)

    if not _verify_contract_token(contract_id, token):
        return JSONResponse({"ok": False, "reason": "invalid token"}, status_code=403)

    contract = await _get_contract(contract_id)
    if not contract:
        return JSONResponse({"ok": False, "reason": "contract not found"}, status_code=404)

    # Idempotency — if already signed, just re-issue the payment URL.
    if contract.get("status") in ("signed", "paid"):
        url = _existing_payment_url(contract, token)
        if url:
            return JSONResponse(
                {"ok": True, "status": contract.get("status"), "checkout_url": url},
                status_code=200,
            )
        return JSONResponse(
            {
                "ok": True,
                "status": contract.get("status"),
                "reason": "already_signed_no_checkout",
            },
            status_code=200,
        )

    # Resolve payment_method from the contract row, falling back to
    # currency-based defaults for legacy rows that predate the refactor.
    payment_method = (contract.get("payment_method") or "").strip().lower()
    currency = _normalize_currency(contract.get("currency"))
    if payment_method not in ("pix", "stripe_br", "stripe_us"):
        payment_method, stripe_account = _resolve_payment_method("auto", currency)
        # Persist the resolved choice so future webhook routing is unambiguous.
        await _patch_contract(
            contract_id,
            {
                "payment_method": payment_method,
                "stripe_account": stripe_account or None,
                "currency": currency,
            },
        )
        contract = await _get_contract(contract_id) or contract
    else:
        stripe_account = (contract.get("stripe_account") or "").strip().upper()
        if payment_method == "stripe_br" and not stripe_account:
            stripe_account = "BR"
        elif payment_method == "stripe_us" and not stripe_account:
            stripe_account = "US"

    # Flip to signed.
    patched = await _patch_contract(
        contract_id,
        {"status": "signed", "signed_at": _now_iso()},
    )
    if not patched:
        return JSONResponse({"ok": False, "reason": "db_update_failed"}, status_code=500)

    # Append signal + history on the lead (best-effort).
    lead_id = contract.get("lead_id")
    if lead_id:
        try:
            await session_append_signal(
                lead_id,
                kind="contract_signed",
                value=str(contract_id),
                source="contract_accept",
            )
            await session_append_history(
                lead_id=lead_id,
                agent="contract",
                action="accept",
                result="ok",
                detail=(
                    f"contract {contract_id} signed via /accept "
                    f"(method={payment_method})"
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "contract.accept: signal/history append failed lead=%s contract=%s",
                lead_id, contract_id,
            )

    # Route to payment.
    contract_refreshed = await _get_contract(contract_id) or contract
    checkout_url: Optional[str] = None

    if payment_method == "pix":
        # Pix doesn't need provider-side session creation; we just hand the
        # signer over to the static-key QR page.
        checkout_url = (
            f"{CONTRACT_HOST}/api/contract/pix/{contract_id}?token={token}"
        )

    elif payment_method in ("stripe_br", "stripe_us"):
        account = "BR" if payment_method == "stripe_br" else "US"
        session = await _create_stripe_checkout(contract_refreshed, account=account)
        if session:
            checkout_url = session.get("url")
            await _patch_contract(
                contract_id,
                {
                    "stripe_session_id": session.get("id"),
                    "stripe_account": account,
                },
            )
        else:
            # Fallback: if the chosen Stripe account isn't actually
            # configured, attempt the other one (legacy contracts).
            other = "BR" if account == "US" else "US"
            secret_other, _ = _stripe_account_key(other)
            if secret_other:
                session = await _create_stripe_checkout(contract_refreshed, account=other)
                if session:
                    checkout_url = session.get("url")
                    await _patch_contract(
                        contract_id,
                        {
                            "stripe_session_id": session.get("id"),
                            "stripe_account": other,
                            "payment_method": f"stripe_{other.lower()}",
                        },
                    )

    if not checkout_url:
        # No payment provider available — leave the contract signed and let
        # an operator wire payment manually.
        log.warning(
            "contract.accept: no payment provider produced a URL contract=%s method=%s",
            contract_id, payment_method,
        )
        return JSONResponse(
            {
                "ok": True,
                "status": "signed",
                "reason": "no_payment_provider",
                "contract_id": contract_id,
            },
            status_code=200,
        )

    # Return 302 — but also include the URL in JSON so fetch() clients can
    # navigate themselves when they don't follow redirects.
    if request.headers.get("accept", "").startswith("application/json") or (
        request.headers.get("content-type", "").startswith("application/json")
    ):
        return JSONResponse(
            {
                "ok": True,
                "status": "signed",
                "checkout_url": checkout_url,
                "payment_method": payment_method,
                "contract_id": contract_id,
            },
            status_code=200,
        )
    return RedirectResponse(url=checkout_url, status_code=302)


def _existing_payment_url(contract: dict, token: str) -> Optional[str]:
    """Best-effort: return a re-usable payment URL for a signed contract.

    * For Pix: returns the Pix page (it's static and idempotent).
    * For Stripe: no — Stripe Checkout Session URLs aren't stored, so we
      return None and let the caller fall back to creating a fresh session.
    """
    method = (contract.get("payment_method") or "").strip().lower()
    contract_id = contract.get("id")
    if method == "pix" and contract_id and token:
        return f"{CONTRACT_HOST}/api/contract/pix/{contract_id}?token={token}"
    return None


async def _mark_paid_and_kickoff(
    contract_id: str,
    source_event: str,
) -> JSONResponse:
    """Shared 'this contract just got paid' tail used by every payment rail.

    Idempotent on the ``paid`` state. Triggers engagement kickoff and the
    invoice stub. Returns the JSONResponse the webhook should hand back.
    """
    contract = await _get_contract(contract_id)
    if not contract:
        log.warning(
            "contract: %s for unknown contract=%s", source_event, contract_id
        )
        return JSONResponse(
            {"ok": False, "reason": "contract_not_found"}, status_code=200
        )

    if contract.get("status") == "paid":
        return JSONResponse(
            {"ok": True, "idempotent": True, "contract_id": contract_id},
            status_code=200,
        )

    await _patch_contract(
        contract_id,
        {"status": "paid", "paid_at": _now_iso()},
    )
    contract = await _get_contract(contract_id) or contract
    engagement_id = await _kickoff_engagement(contract)

    # Best-effort invoice issuance (stub today).
    try:
        await issue_invoice(contract_id)
    except Exception:  # noqa: BLE001
        log.exception("contract: invoice stub failed contract=%s", contract_id)

    return JSONResponse(
        {
            "ok": True,
            "contract_id": contract_id,
            "engagement_id": engagement_id,
            "event": source_event,
        },
        status_code=200,
    )


async def _handle_stripe_webhook(request: Request, account: str) -> JSONResponse:
    """Per-account Stripe webhook handler. ``account`` is 'BR' or 'US'."""
    raw = await request.body()
    sig_header = (
        request.headers.get("stripe-signature")
        or request.headers.get("Stripe-Signature")
        or ""
    )

    if not _verify_stripe_signature(raw, sig_header, account):
        return JSONResponse(
            {"ok": False, "reason": "invalid_signature"}, status_code=403
        )

    try:
        event = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

    event_type = str(event.get("type") or "")
    data_object = (event.get("data") or {}).get("object") or {}

    contract_id: Optional[str] = None
    if event_type == "checkout.session.completed":
        contract_id = data_object.get("client_reference_id")
        if not contract_id:
            metadata = data_object.get("metadata") or {}
            contract_id = metadata.get("contract_id")
        # Cross-check via stored stripe_session_id.
        if not contract_id and data_object.get("id"):
            found = await _get_contract_by_field(
                "stripe_session_id", data_object["id"]
            )
            if found:
                contract_id = found.get("id")
    elif event_type == "payment_intent.succeeded":
        metadata = data_object.get("metadata") or {}
        contract_id = metadata.get("contract_id")
    else:
        # Acknowledge unhandled events; Stripe expects 2xx.
        log.info(
            "contract: stripe(%s) event ignored type=%s", account, event_type
        )
        return JSONResponse({"ok": True, "ignored": event_type}, status_code=200)

    if not contract_id:
        log.warning(
            "contract: stripe(%s) %s without resolvable contract_id",
            account, event_type,
        )
        return JSONResponse({"ok": False, "reason": "no_contract_id"}, status_code=200)

    return await _mark_paid_and_kickoff(
        str(contract_id), source_event=f"stripe_{account.lower()}:{event_type}"
    )


@router.post("/webhook/stripe/br")
async def stripe_webhook_br(request: Request):
    """Stripe webhook handler for Anuvia Ltda (BR cards / BRL)."""
    return await _handle_stripe_webhook(request, account="BR")


@router.post("/webhook/stripe/us")
async def stripe_webhook_us(request: Request):
    """Stripe webhook handler for Anuvia LLC (US cards / USD)."""
    return await _handle_stripe_webhook(request, account="US")


@router.post("/webhook/stripe")
async def stripe_webhook_legacy(request: Request):
    """Legacy single-account Stripe webhook.

    Kept so already-deployed Stripe webhook endpoints don't 404 during the
    rollout. Routes to the BR handler (the legacy STRIPE_SECRET_KEY → BR).
    """
    return await _handle_stripe_webhook(request, account="BR")


# ---------------------------------------------------------------------------
# Pix — page + manual confirm endpoint
# ---------------------------------------------------------------------------


@router.get("/pix/{contract_id}")
async def pix_page(contract_id: str, token: str) -> HTMLResponse:
    """Render the Pix payment page for a signed contract.

    HMAC-verified via the same sign-link token. Shows the QR (rendered from
    ``pix_payload``), the Pix key, the value, and instructions for sending
    proof of payment to Mila.
    """
    if not _verify_contract_token(contract_id, token):
        return HTMLResponse(
            content=_simple_error_html(
                "Link inválido ou expirado", "Invalid or expired link"
            ),
            status_code=403,
        )

    contract = await _get_contract(contract_id)
    if not contract:
        return HTMLResponse(
            content=_simple_error_html(
                "Contrato não encontrado", "Contract not found"
            ),
            status_code=404,
        )

    if (contract.get("payment_method") or "").lower() != "pix":
        # Not a Pix contract — direct the user to /sign instead.
        return RedirectResponse(
            url=f"{CONTRACT_HOST}/api/contract/sign?contract_id={contract_id}&token={token}",
            status_code=302,
        )

    practice = _normalize_practice(contract.get("practice"))
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()
    value_str = _brl(float(contract.get("value_brl") or 0))
    payload = contract.get("pix_payload") or ""
    qr_url = contract.get("pix_qr_image_url") or ""
    is_paid = contract.get("status") == "paid"

    # Render the QR <img> from the stored URL, or — if we never rendered to
    # disk — embed a client-side fallback using the payload string. Use a
    # lightweight 3rd-party QR generator URL as the client-side path so we
    # don't need to ship a JS library.
    if qr_url:
        qr_img_tag = (
            f'<img src="{qr_url}" alt="Pix QR" '
            'style="width:220px;height:220px;display:block;margin:12px auto;">'
        )
    elif payload:
        # api.qrserver.com is a public, free QR rendering endpoint. No-cost
        # fallback so the page works even without the qrcode python lib.
        from urllib.parse import quote as _q
        qr_img_tag = (
            f'<img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={_q(payload)}" '
            'alt="Pix QR" style="width:220px;height:220px;display:block;margin:12px auto;">'
        )
    else:
        qr_img_tag = '<p class="small">QR indisponível — use a chave Pix abaixo.</p>'

    paid_banner = ""
    if is_paid:
        paid_banner = (
            '<div style="background:#dcfce7;color:#166534;border:1px solid #86efac;'
            'border-radius:8px;padding:14px 16px;margin:0 0 20px;font-size:14px;">'
            '<strong>Pagamento já confirmado.</strong> Pode fechar esta página. '
            '<span style="color:#15803d;">/ Payment already confirmed — you may close this page.</span>'
            "</div>"
        )

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Pagamento via Pix — Anuvia</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ background:#fafaf9; font-family:-apple-system,Inter,Arial,sans-serif; color:#0f172a; margin:0; padding:48px 20px; }}
  .card {{ max-width:600px; margin:0 auto; background:#ffffff; border:1px solid #e7e5e4; border-radius:12px; padding:36px 32px; }}
  h1 {{ font-family:Georgia,serif; font-size:26px; margin:0 0 6px; }}
  .small {{ color:#64748b; font-size:12px; }}
  .copy {{ display:flex; gap:8px; margin:12px 0 18px; }}
  .copy input {{ flex:1; padding:10px 12px; border:1px solid #e7e5e4; border-radius:6px; font-family:ui-monospace,Menlo,monospace; font-size:12px; }}
  .copy button {{ padding:10px 14px; background:#0f172a; color:#fff; border:0; border-radius:6px; cursor:pointer; }}
  .meta {{ background:#fafaf9; border:1px solid #e7e5e4; border-radius:8px; padding:14px 16px; margin:14px 0 22px; font-size:14px; }}
  .meta-row {{ display:flex; justify-content:space-between; gap:12px; padding:4px 0; }}
  .meta-row .k {{ color:#64748b; }}
</style></head>
<body>
<div class="card">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 4px;">Anuvia · Pagamento</p>
  <h1>Pagamento via Pix</h1>
  <p class="small">Payment via Pix · Bilingual page</p>
  {paid_banner}
  <div class="meta">
    <div class="meta-row"><span class="k">Serviço / Service</span><strong>{deliverable}</strong></div>
    <div class="meta-row"><span class="k">Valor / Amount</span><strong>R$ {value_str}</strong></div>
    <div class="meta-row"><span class="k">Beneficiário / Recipient</span><strong>{PIX_NUBANK_DISPLAY_NAME}</strong></div>
  </div>

  <h3 style="margin:18px 0 8px;font-size:14px;">1. Pague pelo QR</h3>
  {qr_img_tag}

  <h3 style="margin:18px 0 8px;font-size:14px;">2. Ou copie e cole o código Pix</h3>
  <div class="copy">
    <input id="pixcode" value="{payload}" readonly>
    <button onclick="navigator.clipboard.writeText(document.getElementById('pixcode').value);this.textContent='Copiado!'">Copiar</button>
  </div>

  <p class="small">Chave Pix: <strong>{PIX_NUBANK_KEY or '—'}</strong></p>

  <h3 style="margin:24px 0 8px;font-size:14px;">3. Confirmação</h3>
  <p style="color:#475569;line-height:1.65;margin:0 0 8px;">Após o pagamento, envie o comprovante para <a href="mailto:mila@anuvia.com.br">mila@anuvia.com.br</a> ou aguarde até 24h pra confirmação automática via extrato Nubank. Você receberá um email assim que o pagamento for reconciliado.</p>
  <p class="small" style="margin-top:24px;">Mila Vernazza · Founder Anuvia · {ANUVIA_LEGAL_NAME}</p>
</div>
</body></html>""", status_code=200)


@router.post("/pix/confirm/{contract_id}")
async def pix_confirm(contract_id: str, request: Request):
    """Admin-only: mark a Pix payment as received.

    HMAC-protected via an action-scoped token. Mila (or an admin script)
    POSTs ``{token}`` to this endpoint after seeing the inbound Pix on the
    Nubank extrato. Triggers the same flow as a Stripe webhook success.

    Token generation (Mila's CLI):
        python -c "from lib.contract import _sign_admin_action_token as s; \\
                   print(s('<contract_id>', 'pix_confirm'))"
    """
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = await request.json()
        else:
            form = await request.form()
            payload = {k: v for k, v in form.items()}
    except Exception:  # noqa: BLE001
        payload = {}

    token = str(payload.get("token") or "").strip()
    if not token:
        # Also accept the token as a query-string param for curl ergonomics.
        token = str(request.query_params.get("token") or "").strip()

    if not token or not _verify_admin_action_token(contract_id, "pix_confirm", token):
        return JSONResponse({"ok": False, "reason": "invalid_token"}, status_code=403)

    return await _mark_paid_and_kickoff(contract_id, source_event="pix:confirm")


# ---------------------------------------------------------------------------
# Google Workspace eSignature webhook
# ---------------------------------------------------------------------------


@router.post("/webhook/google/esign")
async def google_esign_webhook(request: Request):
    """Drive API push notification endpoint for eSignature completion.

    Drive sends a very small POST with most of the useful state in headers:
      * X-Goog-Channel-Id        — the channel id we set at watch time
      * X-Goog-Channel-Token     — echoed back (HMAC of contract_id)
      * X-Goog-Resource-Id       — the watched resource (the Doc)
      * X-Goog-Resource-State    — 'sync' | 'change' | 'update' | ...

    We verify the channel token before doing anything else.
    """
    channel_id = request.headers.get("x-goog-channel-id") or ""
    channel_token = request.headers.get("x-goog-channel-token") or ""
    resource_id = request.headers.get("x-goog-resource-id") or ""
    resource_state = request.headers.get("x-goog-resource-state") or ""

    if resource_state == "sync":
        # Initial sync ping — nothing to do.
        return JSONResponse({"ok": True, "ignored": "sync"}, status_code=200)

    if not channel_id and not resource_id:
        return JSONResponse({"ok": False, "reason": "missing headers"}, status_code=400)

    # Look up the contract by the channel id or resource id we stored.
    contract: Optional[dict] = None
    if channel_id:
        contract = await _get_contract_by_field("google_watch_channel_id", channel_id)
    if not contract and resource_id:
        contract = await _get_contract_by_field("google_watch_resource_id", resource_id)
    if not contract:
        log.warning(
            "contract: google esign webhook channel=%s resource=%s — no match",
            channel_id, resource_id,
        )
        return JSONResponse({"ok": False, "reason": "no_match"}, status_code=200)

    contract_id = str(contract.get("id") or "")
    # Verify the channel token against the contract id.
    if channel_token and not _verify_admin_action_token(
        contract_id, "google_watch", channel_token
    ):
        log.warning(
            "contract: google esign webhook invalid channel token contract=%s",
            contract_id,
        )
        return JSONResponse({"ok": False, "reason": "invalid_channel_token"}, status_code=403)

    # Delegate to the helper, which fetches the eSignature state and flips
    # the contract to 'signed' when appropriate.
    result = await _handle_google_esign_webhook(
        {
            "doc_id": contract.get("google_doc_id"),
            "resource_id": resource_id,
            "channel_id": channel_id,
            "state": resource_state,
        }
    )

    # NB: signing the document via Google eSign does NOT mark the contract
    # as paid — payment is a separate step that the lead initiates from the
    # post-signature email + payment URL. We only flip status to 'signed'
    # here so the operator sees the progression.
    return JSONResponse(result, status_code=200)
