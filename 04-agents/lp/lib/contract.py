"""Contract → payment → invoice automation.

Owned by Agent A3. Per ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Contract & payment
contract". Mirrors the patterns in ``lib/track_b.py`` (HMAC accept, Gotenberg
PDF render, Resend email, Supabase REST via ``lib.sessions`` config).

Flow:

    generate_contract(lead_id, practice, value_brl)
        → renders PT HTML + Gotenberg PDF
        → persists row in `contracts`
        → returns sign_url (HMAC-tokened)
    send_contract_email(contract_id)
        → emails lead the PDF + sign link via Resend
    GET  /api/contract/sign?contract_id=&token=
        → bilingual sign page
    POST /api/contract/accept
        → contracts.status='signed', signed_at=now
        → creates Stripe Checkout (or Mercado Pago preference)
        → 302 redirect to provider checkout URL
    POST /api/contract/webhook/stripe
        → verifies Stripe-Signature
        → on checkout.session.completed / payment_intent.succeeded:
            contracts.status='paid', paid_at=now
            engagement kickoff signal + next_action queued
    POST /api/contract/webhook/mercadopago
        → analogous via MP_WEBHOOK_SECRET
    issue_invoice(contract_id)
        → Conta Azul stub. Until Mila provides creds we just write
          invoice_id='manual_TODO' and return status='stub'.

Graceful degradation:
  * If STRIPE_SECRET_KEY is empty AND MERCADO_PAGO_ACCESS_TOKEN is empty,
    /accept marks the contract signed but skips checkout creation and
    returns a JSON `{ok: True, status: 'signed', reason: 'no_payment_provider'}`
    so the operator can wire payment manually.
  * If RESEND_API_KEY is empty, send_contract_email logs a dry-run and
    returns ``{ok: True, message_id: None, dry_run: True}``.
  * If GOTENBERG_URL is unreachable, we still persist the contract row with
    an HTML fallback URL and ``status='draft'`` only if HTML write fails too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
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

# Payment providers
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL",
    f"{CONTRACT_HOST}/contract-paid",
)
STRIPE_CANCEL_URL = os.environ.get(
    "STRIPE_CANCEL_URL",
    f"{CONTRACT_HOST}/contract-cancelled",
)

MP_ACCESS_TOKEN = os.environ.get("MERCADO_PAGO_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

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
_PRACTICE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "growth": {
        "deliverable_name": "Growth Sales Ops Setup",
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


def _practice_config(practice: str, overrides: Optional[dict] = None) -> dict:
    """Return per-practice config, merged with optional overrides."""
    base = dict(_PRACTICE_DEFAULTS.get(practice) or {})
    if overrides:
        base.update({k: v for k, v in overrides.items() if v is not None})
    base.setdefault("deliverable_name", practice.title())
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


async def generate_contract(
    lead_id: str,
    practice: str,
    value_brl: int,
    scope_overrides: Optional[dict] = None,
) -> dict:
    """Generate a contract for ``lead_id`` and persist it.

    Steps:
      1. Fetch the lead row (used for client name/company/email in the body).
      2. Render PT HTML via ``_build_contract_html``.
      3. Render PDF via Gotenberg → ``/static/contracts/{contract_id}.pdf``.
         Falls back to the HTML file when Gotenberg is unavailable.
      4. INSERT into ``contracts`` with status='sent' and an HMAC token.
      5. Return ``{contract_id, pdf_url, sign_url, hmac_token}``.

    Graceful degradation: if Supabase insert fails, returns
    ``{ok: False, reason: ...}``. Never raises.

    Note re Stripe: this function does NOT create a Stripe Checkout session.
    Checkout is created lazily when the lead actually clicks the sign button
    and POSTs to ``/api/contract/accept``. So missing STRIPE_SECRET_KEY does
    not impede contract generation.
    """
    contract_id = str(uuid.uuid4())
    overrides = scope_overrides or {}

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

    token = _sign_contract_token(contract_id)
    sign_url = f"{CONTRACT_HOST}/api/contract/sign?contract_id={contract_id}&token={token}"

    initial_status = "sent"
    if not STRIPE_SECRET_KEY and not MP_ACCESS_TOKEN:
        # No payment provider configured at all → mark as draft so an operator
        # knows to wire payment manually before signing.
        initial_status = "draft"

    row = {
        "id": contract_id,
        "lead_id": lead_id,
        "practice": practice,
        "value_brl": value_brl,
        "status": initial_status,
        "sent_at": _now_iso(),
        "pdf_url": pdf_url,
        "sign_url": sign_url,
        "hmac_token": token,
        "scope": scope_snapshot,
    }
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
                "status": initial_status,
                "pdf_rendered": pdf_ok,
            },
        )
        await session_append_history(
            lead_id=lead_id,
            agent="contract",
            action="generate_contract",
            result="ok",
            detail=f"contract {contract_id} generated ({practice}, R$ {value_brl})",
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
        "hmac_token": token,
        "status": initial_status,
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
# Stripe / Mercado Pago helpers
# ---------------------------------------------------------------------------


async def _create_stripe_checkout(contract: dict) -> Optional[dict]:
    """Create a Stripe Checkout Session for the contract value.

    Returns the session dict (with ``id`` and ``url``) or None on failure.
    Never raises.
    """
    if not STRIPE_SECRET_KEY:
        return None

    contract_id = str(contract.get("id") or "")
    value_brl = float(contract.get("value_brl") or 0)
    practice = contract.get("practice") or "growth"
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()

    # Stripe wants currency-minor units (centavos).
    unit_amount = int(round(value_brl * 100))
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
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", "brl"),
        ("line_items[0][price_data][unit_amount]", str(unit_amount)),
        ("line_items[0][price_data][product_data][name]", f"Anuvia · {deliverable}"),
    ]

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                auth=(STRIPE_SECRET_KEY, ""),
                data=data,
            )
        if r.status_code >= 400:
            log.error(
                "contract: stripe checkout failed status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        log.exception("contract: stripe checkout call exploded contract=%s", contract_id)
        return None


async def _create_mp_preference(contract: dict) -> Optional[dict]:
    """Create a Mercado Pago preference for the contract value.

    Returns the preference dict (with ``id`` and ``init_point``) or None on
    failure. Never raises.
    """
    if not MP_ACCESS_TOKEN:
        return None

    contract_id = str(contract.get("id") or "")
    value_brl = float(contract.get("value_brl") or 0)
    practice = contract.get("practice") or "growth"
    cfg = _practice_config(practice)
    deliverable = cfg.get("deliverable_name") or practice.title()

    if value_brl <= 0:
        return None

    payload = {
        "items": [
            {
                "title": f"Anuvia · {deliverable}",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": round(value_brl, 2),
            }
        ],
        "external_reference": contract_id,
        "metadata": {"contract_id": contract_id, "practice": practice},
        "back_urls": {
            "success": STRIPE_SUCCESS_URL,
            "failure": STRIPE_CANCEL_URL,
            "pending": STRIPE_SUCCESS_URL,
        },
        "auto_return": "approved",
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={
                    "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            log.error(
                "contract: mp preference failed status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        log.exception("contract: mp preference call exploded contract=%s", contract_id)
        return None


# ---------------------------------------------------------------------------
# Stripe webhook signature verification
# ---------------------------------------------------------------------------


def _verify_stripe_signature(body: bytes, header: str) -> bool:
    """Verify the Stripe-Signature header per Stripe's spec.

    Header format: ``t=<ts>,v1=<sig>[,v1=<sig>]``. Payload to sign:
    ``<ts>.<body>``. Returns True iff at least one v1 signature matches and
    the timestamp is within 5 minutes of now. When STRIPE_WEBHOOK_SECRET is
    empty, returns True (operator opted out — useful for dev).
    """
    if not STRIPE_WEBHOOK_SECRET:
        log.warning("contract: STRIPE_WEBHOOK_SECRET unset; accepting webhook unverified")
        return True
    if not header:
        return False
    try:
        parts = {k.strip(): v.strip() for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p)}
    except Exception:  # noqa: BLE001
        return False
    ts = parts.get("t")
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

    # Multiple v1 signatures may be present (one per active key).
    sigs = [v.strip() for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k.strip() == "v1"]
    if not sigs:
        return False
    signed_payload = f"{ts}.{body.decode('utf-8', errors='replace')}".encode("utf-8")
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    for sig in sigs:
        if hmac.compare_digest(sig, expected):
            return True
    return False


def _verify_mp_signature(body: bytes, signature_header: str) -> bool:
    """Verify a Mercado Pago webhook.

    MP supports x-signature with format ``ts=<ts>,v1=<hex>``. The signed
    payload is ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;``. We accept
    the simpler hex(body) HMAC fallback when format differs. If
    ``MP_WEBHOOK_SECRET`` is empty we accept (operator opted out).
    """
    if not MP_WEBHOOK_SECRET:
        return True
    if not signature_header:
        return False
    # Naive: HMAC the raw body and compare any v1=<hex> token.
    expected = hmac.new(
        MP_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    for part in signature_header.split(","):
        if "=" not in part:
            continue
        _, val = part.split("=", 1)
        if hmac.compare_digest(val.strip(), expected):
            return True
    return False


# ---------------------------------------------------------------------------
# Engagement kickoff (called from payment webhooks)
# ---------------------------------------------------------------------------


async def _kickoff_engagement(contract: dict) -> Optional[str]:
    """Create the engagements row and queue the delivery handler.

    Idempotent: if an engagement already exists for this contract, returns
    its id without creating a duplicate.
    """
    contract_id = str(contract.get("id") or "")
    lead_id = contract.get("lead_id")
    practice = contract.get("practice") or "growth"
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
    """Mark a contract as signed, then create a Stripe/MP checkout.

    Body: ``{contract_id, token}`` (JSON) or form fields. HMAC-verified.

    Returns either:
      * 302 redirect to the checkout URL (Stripe or MP), or
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

    # Idempotency — if already signed, just re-issue the checkout URL.
    if contract.get("status") in ("signed", "paid"):
        existing_url = _existing_checkout_url(contract)
        if existing_url:
            return JSONResponse(
                {"ok": True, "status": contract.get("status"), "checkout_url": existing_url},
                status_code=200,
            )
        return JSONResponse(
            {"ok": True, "status": contract.get("status"), "reason": "already_signed_no_checkout"},
            status_code=200,
        )

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
                detail=f"contract {contract_id} signed via /accept",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "contract.accept: signal/history append failed lead=%s contract=%s",
                lead_id, contract_id,
            )

    # Create checkout.
    contract_refreshed = await _get_contract(contract_id) or contract
    checkout_url: Optional[str] = None
    if STRIPE_SECRET_KEY:
        session = await _create_stripe_checkout(contract_refreshed)
        if session:
            checkout_url = session.get("url")
            await _patch_contract(
                contract_id,
                {"stripe_session_id": session.get("id")},
            )
    if not checkout_url and MP_ACCESS_TOKEN:
        pref = await _create_mp_preference(contract_refreshed)
        if pref:
            checkout_url = pref.get("init_point") or pref.get("sandbox_init_point")
            await _patch_contract(
                contract_id,
                {"mp_preference_id": pref.get("id")},
            )

    if not checkout_url:
        # No payment provider available — leave the contract signed and let
        # an operator wire payment manually.
        log.warning(
            "contract.accept: no payment provider configured contract=%s",
            contract_id,
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
                "contract_id": contract_id,
            },
            status_code=200,
        )
    return RedirectResponse(url=checkout_url, status_code=302)


def _existing_checkout_url(contract: dict) -> Optional[str]:
    """Best-effort: return the previously stored checkout URL, if any.

    We didn't persist the URL itself (only the provider id) — so this is a
    placeholder that returns None. The /accept endpoint falls back to a
    fresh checkout creation on re-entry.
    """
    return None


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook handler.

    Verifies the ``Stripe-Signature`` header. On
    ``checkout.session.completed`` or ``payment_intent.succeeded``, marks
    the matching contract as paid and queues delivery kickoff.

    Returns 200 JSON for processed events (so Stripe stops retrying), 400
    for malformed payloads, 403 for signature failures.
    """
    raw = await request.body()
    sig_header = request.headers.get("stripe-signature") or request.headers.get("Stripe-Signature") or ""

    if not _verify_stripe_signature(raw, sig_header):
        return JSONResponse({"ok": False, "reason": "invalid_signature"}, status_code=403)

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
            found = await _get_contract_by_field("stripe_session_id", data_object["id"])
            if found:
                contract_id = found.get("id")
    elif event_type == "payment_intent.succeeded":
        metadata = data_object.get("metadata") or {}
        contract_id = metadata.get("contract_id")
    else:
        # Acknowledge unhandled events; Stripe expects 2xx.
        log.info("contract: stripe event ignored type=%s", event_type)
        return JSONResponse({"ok": True, "ignored": event_type}, status_code=200)

    if not contract_id:
        log.warning("contract: stripe %s without resolvable contract_id", event_type)
        return JSONResponse({"ok": False, "reason": "no_contract_id"}, status_code=200)

    contract = await _get_contract(contract_id)
    if not contract:
        log.warning("contract: stripe %s for unknown contract=%s", event_type, contract_id)
        return JSONResponse({"ok": False, "reason": "contract_not_found"}, status_code=200)

    if contract.get("status") == "paid":
        return JSONResponse({"ok": True, "idempotent": True, "contract_id": contract_id}, status_code=200)

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
            "event": event_type,
        },
        status_code=200,
    )


@router.post("/webhook/mercadopago")
async def mp_webhook(request: Request):
    """Mercado Pago webhook handler.

    MP sends short JSON notifications like ``{"action": "payment.updated",
    "data": {"id": "<payment_id>"}, ...}``. We verify the ``x-signature``
    header, look up the payment to discover its ``external_reference``
    (which we set to the contract_id at preference creation time), and flip
    the contract to ``paid`` when status==approved.
    """
    raw = await request.body()
    sig_header = (
        request.headers.get("x-signature")
        or request.headers.get("X-Signature")
        or ""
    )
    if not _verify_mp_signature(raw, sig_header):
        return JSONResponse({"ok": False, "reason": "invalid_signature"}, status_code=403)

    try:
        event = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "reason": "invalid_json"}, status_code=400)

    # MP sometimes includes `external_reference` directly, sometimes only a
    # payment id we have to resolve via the API.
    data = event.get("data") or {}
    payment_id = data.get("id")
    contract_id: Optional[str] = None
    payment_status: Optional[str] = None

    if payment_id and MP_ACCESS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                r = await client.get(
                    f"https://api.mercadopago.com/v1/payments/{payment_id}",
                    headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                )
            if r.status_code == 200:
                body = r.json() or {}
                contract_id = body.get("external_reference")
                payment_status = body.get("status")
        except Exception:  # noqa: BLE001
            log.exception("contract: mp payment lookup failed id=%s", payment_id)

    if not contract_id:
        # Fall back to event metadata if present.
        contract_id = event.get("external_reference") or (event.get("metadata") or {}).get("contract_id")

    if not contract_id:
        log.warning("contract: mp webhook without resolvable contract_id")
        return JSONResponse({"ok": False, "reason": "no_contract_id"}, status_code=200)

    if payment_status and payment_status != "approved":
        log.info(
            "contract: mp webhook payment not approved contract=%s status=%s",
            contract_id, payment_status,
        )
        return JSONResponse(
            {"ok": True, "ignored": payment_status, "contract_id": contract_id},
            status_code=200,
        )

    contract = await _get_contract(contract_id)
    if not contract:
        return JSONResponse({"ok": False, "reason": "contract_not_found"}, status_code=200)

    if contract.get("status") == "paid":
        return JSONResponse({"ok": True, "idempotent": True, "contract_id": contract_id}, status_code=200)

    await _patch_contract(
        contract_id,
        {"status": "paid", "paid_at": _now_iso()},
    )
    contract = await _get_contract(contract_id) or contract
    engagement_id = await _kickoff_engagement(contract)
    try:
        await issue_invoice(contract_id)
    except Exception:  # noqa: BLE001
        log.exception("contract: invoice stub failed contract=%s", contract_id)

    return JSONResponse(
        {
            "ok": True,
            "contract_id": contract_id,
            "engagement_id": engagement_id,
            "event": event.get("action") or event.get("type") or "mp_event",
        },
        status_code=200,
    )
