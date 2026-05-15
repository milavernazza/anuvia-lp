"""
Customer-facing delivery endpoints (intake form, approve quick wins, NPS).

These complement the lib/delivery/<practice>.py handler modules. Each delivery
handler generates email buttons with HMAC-protected URLs that route here.

Routes (all under /api/delivery):
  GET  /{practice}/intake   — render intake form for engagement
  POST /{practice}/intake   — submit intake data, advance to next phase
  GET  /{practice}/approve  — render approval confirmation page
  POST /{practice}/approve  — record approval, advance phase
  GET  /{practice}/nps      — render NPS form
  POST /{practice}/nps      — save NPS score

  GET  /debug/engagement/{engagement_id}?token=<admin>  — dump artifacts JSON

practice ∈ {finops, ai, devops, growth, industry}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lib.sessions import SUPA_HEADERS, SUPA_URL, SUPA_KEY

log = logging.getLogger("anuvia-delivery-routes")
router = APIRouter(prefix="/api/delivery", tags=["delivery"])

HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)
BASE_URL = os.environ.get("BASE_URL", "https://anuvia.com.br").rstrip("/")

# practice slug → (module path, internal practice key)
_PRACTICE_ROUTE = {
    "finops": ("lib.delivery.finops_audit", "cloud_finops"),
    "ai": ("lib.delivery.ai_readiness", "ai"),
    "devops": ("lib.delivery.devops_maturity", "devops"),
    "growth": ("lib.delivery.growth_salesops", "growth_salesops"),
    "industry": ("lib.delivery.industry", "industry"),
}

_PHASE_AFTER_INTAKE = {
    "finops": "finops_phase_2_analysis",
    "ai": "ai_phase_2_scoring",
    "devops": "devops_phase_2_maturity",
    "growth": "growth_phase_2_automation",
    "industry": "industry_phase_2_pov",
}

_PHASE_AFTER_APPROVE = {
    "finops": "finops_phase_4_roadmap",
    "ai": None,  # AI doesn't have approval flow
    "devops": "devops_phase_4_handoff",
    "growth": None,
    "industry": "industry_phase_3_validation",
}


# -----------------------------------------------------------------------------
# Token helpers — match the HMAC pattern used in each delivery module
# (engagement_id:purpose, signed with CONTRACT_HMAC_SECRET).
# -----------------------------------------------------------------------------


def _verify_token(engagement_id: str, purpose: str, token: str) -> bool:
    if not HMAC_SECRET or not token:
        return False
    expected = hmac.new(
        HMAC_SECRET.encode("utf-8"),
        f"{engagement_id}:{purpose}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


def _admin_token() -> str:
    return hmac.new(HMAC_SECRET.encode("utf-8"), b"admin_smoke", hashlib.sha256).hexdigest()


# -----------------------------------------------------------------------------
# Supabase helpers
# -----------------------------------------------------------------------------


async def _get_engagement(engagement_id: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{SUPA_URL}/engagements?id=eq.{engagement_id}&select=*",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


async def _patch_engagement(engagement_id: str, patch: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.patch(
            f"{SUPA_URL}/engagements?id=eq.{engagement_id}",
            json=patch,
            headers=SUPA_HEADERS,
        )


async def _patch_lead(lead_id: str, patch: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            json=patch,
            headers=SUPA_HEADERS,
        )


# -----------------------------------------------------------------------------
# Page rendering
# -----------------------------------------------------------------------------

_PAGE_BASE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#fafaf9; color:#0f172a; max-width:680px; margin:40px auto; padding:32px; line-height:1.55; }}
  h1 {{ font-family: 'Playfair Display', Georgia, serif; font-weight:600; }}
  .card {{ background:white; border:1px solid #e7e5e4; border-radius:12px; padding:32px; box-shadow:0 1px 3px rgba(0,0,0,0.04); }}
  label {{ display:block; margin-top:18px; font-weight:600; font-size:14px; }}
  input, textarea, select {{ width:100%; padding:10px 12px; border:1px solid #d6d3d1; border-radius:6px; font-size:14px; font-family:inherit; box-sizing:border-box; }}
  textarea {{ min-height:120px; resize:vertical; }}
  button {{ display:inline-block; background:#0f172a; color:white; padding:12px 24px; border:none; border-radius:8px; font-weight:600; font-size:15px; cursor:pointer; margin-top:24px; }}
  button.success {{ background:#16a34a; }}
  .muted {{ color:#78716c; font-size:13px; }}
  .err {{ background:#fef2f2; color:#991b1b; padding:14px; border-radius:6px; margin-bottom:20px; }}
  .ok {{ background:#f0fdf4; color:#166534; padding:14px; border-radius:6px; margin-bottom:20px; }}
</style>
</head>
<body>
  <div class="card">
  {body}
  </div>
</body>
</html>"""


def _render(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE_BASE.format(title=title, body=body))


def _err_page(msg: str, code: int = 400) -> HTMLResponse:
    body = f'<div class="err"><strong>Erro:</strong> {msg}</div><p class="muted">Se o link expirou, peça pra Mila reenviar.</p>'
    return HTMLResponse(_PAGE_BASE.format(title="Erro", body=body), status_code=code)


# -----------------------------------------------------------------------------
# INTAKE — GET (render form) + POST (submit)
# -----------------------------------------------------------------------------


_INTAKE_FIELDS = {
    "finops": [
        ("executive_sponsor_name", "Nome do executivo sponsor", "text", True),
        ("executive_sponsor_email", "Email do sponsor (CC nos deliverables)", "email", True),
        ("aws_spend_brl_monthly", "AWS spend mensal aproximado (R$)", "number", True),
        ("aws_account_count", "Quantos AWS accounts vocês têm?", "number", True),
        ("primary_services", "Serviços AWS principais (vírgula-separado: EC2, RDS, S3, CloudFront, ...)", "text", True),
        ("tagging_strategy", "Estratégia atual de tagging (1 parágrafo)", "textarea", True),
        ("biggest_concerns", "Maiores preocupações de custo que já mapeou (uma por linha)", "textarea", True),
    ],
    "ai": [
        ("executive_sponsor_name", "Nome do executivo sponsor", "text", True),
        ("executive_sponsor_email", "Email do sponsor", "email", True),
        ("stakeholders", "Stakeholders pro workshop (nome + área, um por linha)", "textarea", True),
        ("past_pocs", "PoCs IA já tentados (nome + status: live / killed / stalled)", "textarea", True),
        ("data_assets", "Datasets disponíveis (CRM, transaction logs, etc.)", "textarea", True),
        ("compliance_constraints", "Compliance aplicável (LGPD, GxP, BACEN, SOC 2, HIPAA — separe por vírgula)", "text", True),
        ("annual_ai_budget_brl", "Budget anual IA (R$)", "number", True),
        ("internal_ai_capability", "Capacidade interna IA/ML (none / 1-2 engineers / dedicated team)", "select", True),
    ],
    "devops": [
        ("executive_sponsor_name", "Nome do executivo sponsor", "text", True),
        ("executive_sponsor_email", "Email do sponsor", "email", True),
        ("engineering_team_size", "Tamanho time engenharia", "number", True),
        ("squads_count", "Número de squads", "number", True),
        ("production_services_count", "Número de serviços em produção", "number", True),
        ("ci_tool", "CI tool (GitHub Actions, Jenkins, CircleCI, GitLab CI)", "text", True),
        ("incident_tracker", "Incident tracker (Linear, Jira, Opsgenie)", "text", True),
        ("self_reported_deploy_frequency", "Deploy frequency atual (daily / weekly / monthly)", "select", True),
        ("self_reported_mttr_hours", "MTTR médio (horas)", "number", True),
        ("self_reported_cfr_pct", "Change failure rate atual (%)", "number", True),
        ("observability_stack", "Stack de observability (Datadog, CloudWatch, Grafana, ...)", "textarea", True),
        ("post_mortem_culture", "Cultura post-mortem (none / some / always)", "select", True),
    ],
    "growth": [
        ("executive_sponsor_name", "Nome do executivo sponsor", "text", True),
        ("executive_sponsor_email", "Email do sponsor", "email", True),
        ("crm_in_use", "CRM atual (HubSpot, Salesforce, Pipedrive, RD, Notion)", "text", True),
        ("sales_team_composition", "Composição time comercial", "text", True),
        ("sales_cycle_median_days", "Sales cycle mediano (dias)", "number", True),
        ("avg_ticket_brl", "Ticket médio (R$)", "number", True),
        ("lead_sources", "Canais de entrada (vírgula-separado)", "text", True),
        ("monthly_volume", "Volume mensal (leads / qualified / closed, separado por barra)", "text", True),
        ("response_time_sla", "SLA de resposta (goal vs atual)", "text", True),
        ("top_pain_points", "Top 3 dores (uma por linha)", "textarea", True),
    ],
    "industry": [
        ("executive_sponsor_name", "Nome do executivo sponsor", "text", True),
        ("executive_sponsor_email", "Email do sponsor", "email", True),
        ("vertical", "Vertical (manufacturing / logistics / healthcare / life_sciences / finserv)", "select", True),
        ("company_revenue_brl", "Revenue anual (R$)", "number", True),
        ("main_pain", "Dor principal específica do vertical", "textarea", True),
        ("compliance_named", "Compliance frame nomeado (ISO, ANVISA, BACEN, HIPAA, LGPD-saúde, ...)", "text", True),
        ("ai_maturity", "AI maturity (none / exploring / scaling)", "select", True),
    ],
}

_SELECT_OPTIONS = {
    "internal_ai_capability": ["none", "1-2 engineers", "dedicated team"],
    "self_reported_deploy_frequency": ["daily", "weekly", "monthly", "quarterly", "ad-hoc"],
    "post_mortem_culture": ["none", "some", "always"],
    "vertical": ["manufacturing", "logistics", "healthcare", "life_sciences", "finserv"],
    "ai_maturity": ["none", "exploring", "scaling"],
}


def _render_intake_form(practice: str, engagement_id: str, token: str) -> HTMLResponse:
    fields = _INTAKE_FIELDS.get(practice, _INTAKE_FIELDS["finops"])
    practice_label = {
        "finops": "FinOps Audit",
        "ai": "AI Readiness Sprint",
        "devops": "DevOps Maturity Assessment",
        "growth": "Sales Ops Diagnostic",
        "industry": "Industry Assessment",
    }.get(practice, practice)

    field_html = []
    for name, label, ftype, required in fields:
        req = "required" if required else ""
        if ftype == "textarea":
            field_html.append(
                f'<label for="{name}">{label}</label><textarea id="{name}" name="{name}" {req}></textarea>'
            )
        elif ftype == "select":
            opts = "".join(f'<option value="{o}">{o}</option>' for o in _SELECT_OPTIONS.get(name, []))
            field_html.append(
                f'<label for="{name}">{label}</label><select id="{name}" name="{name}" {req}><option value="">Selecione...</option>{opts}</select>'
            )
        else:
            field_html.append(
                f'<label for="{name}">{label}</label><input type="{ftype}" id="{name}" name="{name}" {req}/>'
            )

    body = f"""
    <p class="muted">ANUVIA · {practice_label.upper()}</p>
    <h1>Formulário de intake</h1>
    <p>Esses dados destravam a análise da próxima fase. Leva ~10 minutos.</p>
    <form method="POST" action="/api/delivery/{practice}/intake?engagement_id={engagement_id}&token={token}">
      {''.join(field_html)}
      <button type="submit">Enviar e iniciar análise →</button>
    </form>
    """
    return _render(f"Intake — {practice_label}", body)


@router.get("/{practice}/intake")
async def intake_get(practice: str, engagement_id: str, token: str):
    if practice not in _PRACTICE_ROUTE:
        return _err_page(f"prática desconhecida: {practice}", 404)
    if not _verify_token(engagement_id, "intake", token):
        return _err_page("token inválido ou expirado", 401)
    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)
    # Already submitted?
    artifacts = eng.get("artifacts") or {}
    if isinstance(artifacts, dict) and artifacts.get("intake_submitted_at"):
        return _render(
            "Intake já recebido",
            '<h1>Intake já recebido</h1><p class="ok">Recebemos teus dados em '
            f'{artifacts["intake_submitted_at"]}. A análise está em andamento — '
            'fica de olho no email.</p>',
        )
    return _render_intake_form(practice, engagement_id, token)


@router.post("/{practice}/intake")
async def intake_post(practice: str, request: Request):
    engagement_id = request.query_params.get("engagement_id", "")
    token = request.query_params.get("token", "")
    if practice not in _PRACTICE_ROUTE:
        return _err_page(f"prática desconhecida: {practice}", 404)
    if not _verify_token(engagement_id, "intake", token):
        return _err_page("token inválido", 401)

    # Parse form data
    form = await request.form()
    intake = {k: v for k, v in form.items() if v}

    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)

    now_iso = datetime.now(timezone.utc).isoformat()
    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {"_legacy": artifacts}
    artifacts["intake"] = intake
    artifacts["intake_submitted_at"] = now_iso

    await _patch_engagement(
        engagement_id,
        {"intake_data": intake, "artifacts": artifacts},
    )

    # Schedule the next phase now
    next_action = _PHASE_AFTER_INTAKE.get(practice)
    if next_action and eng.get("lead_id"):
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await _patch_lead(
            eng["lead_id"],
            {"next_action": next_action, "next_action_at": past_iso},
        )

    return _render(
        "Intake recebido",
        '<h1>Intake recebido ✓</h1>'
        '<p class="ok">Tudo certo. A análise da próxima fase já entrou na fila — '
        'orchestrator processa nas próximas horas. Você recebe o relatório '
        'por email.</p>'
        '<p class="muted">Sem ação adicional do teu lado por enquanto.</p>',
    )


# -----------------------------------------------------------------------------
# APPROVE quick wins — POST (record approval, advance phase)
# -----------------------------------------------------------------------------


@router.get("/{practice}/approve")
async def approve_get(practice: str, engagement_id: str, token: str):
    if practice not in _PRACTICE_ROUTE:
        return _err_page("prática desconhecida", 404)
    if not _verify_token(engagement_id, "approval", token):
        return _err_page("token inválido", 401)
    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)
    artifacts = eng.get("artifacts") or {}
    if isinstance(artifacts, dict) and artifacts.get("approved_at"):
        return _render(
            "Aprovação já registrada",
            f'<h1>Já aprovado</h1><p class="ok">Aprovação registrada em '
            f'{artifacts["approved_at"]}. Execução em andamento.</p>',
        )
    body = f"""
    <p class="muted">ANUVIA · {practice.upper()}</p>
    <h1>Aprovar quick wins</h1>
    <p>Confirmando, vamos executar as mudanças listadas no change log. Cada uma com rollback documentado. Você pode pausar a qualquer momento respondendo ao email.</p>
    <form method="POST" action="/api/delivery/{practice}/approve?engagement_id={engagement_id}&token={token}">
      <button class="success" type="submit">Sim, aprovar e executar →</button>
    </form>
    <p class="muted" style="margin-top:24px;">Se quiser ajustar escopo antes, é só responder o email com instruções.</p>
    """
    return _render("Aprovar quick wins", body)


@router.post("/{practice}/approve")
async def approve_post(practice: str, request: Request):
    engagement_id = request.query_params.get("engagement_id", "")
    token = request.query_params.get("token", "")
    if not _verify_token(engagement_id, "approval", token):
        return _err_page("token inválido", 401)

    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)

    now_iso = datetime.now(timezone.utc).isoformat()
    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {"_legacy": artifacts}
    artifacts["approved_at"] = now_iso

    await _patch_engagement(engagement_id, {"artifacts": artifacts})

    next_action = _PHASE_AFTER_APPROVE.get(practice)
    if next_action and eng.get("lead_id"):
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await _patch_lead(
            eng["lead_id"],
            {"next_action": next_action, "next_action_at": past_iso},
        )

    return _render(
        "Aprovado",
        '<h1>Aprovado ✓</h1>'
        '<p class="ok">Sign-off registrado. Execução agendada — você recebe o '
        'change log com timestamps por mudança quando terminar.</p>',
    )


# -----------------------------------------------------------------------------
# NPS — GET (form) + POST (save)
# -----------------------------------------------------------------------------


@router.get("/{practice}/nps")
async def nps_get(practice: str, engagement_id: str, token: str):
    if not _verify_token(engagement_id, "nps", token):
        return _err_page("token inválido", 401)
    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)
    body = f"""
    <p class="muted">ANUVIA · {practice.upper()}</p>
    <h1>Como foi a experiência?</h1>
    <p>Em uma escala de 0 a 10, qual a probabilidade de você indicar a Anuvia pra um colega CTO/Head Cloud?</p>
    <form method="POST" action="/api/delivery/{practice}/nps?engagement_id={engagement_id}&token={token}">
      <label for="score">Nota (0-10)</label>
      <input type="number" id="score" name="score" min="0" max="10" required/>
      <label for="reason">Por quê? (opcional, ajuda muito)</label>
      <textarea id="reason" name="reason"></textarea>
      <button type="submit">Enviar →</button>
    </form>
    """
    return _render("NPS Anuvia", body)


@router.post("/{practice}/nps")
async def nps_post(practice: str, request: Request):
    engagement_id = request.query_params.get("engagement_id", "")
    token = request.query_params.get("token", "")
    if not _verify_token(engagement_id, "nps", token):
        return _err_page("token inválido", 401)

    form = await request.form()
    try:
        score = int(form.get("score", "-1"))
    except (TypeError, ValueError):
        score = -1
    if not (0 <= score <= 10):
        return _err_page("nota deve estar entre 0 e 10")
    reason = (form.get("reason") or "").strip()

    eng = await _get_engagement(engagement_id)
    if not eng:
        return _err_page("engagement não encontrado", 404)

    now_iso = datetime.now(timezone.utc).isoformat()
    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {"_legacy": artifacts}
    artifacts["nps"] = {"score": score, "reason": reason, "submitted_at": now_iso}
    await _patch_engagement(engagement_id, {"artifacts": artifacts})

    msg = "Obrigada pela nota."
    if score >= 9:
        msg = "Obrigada! Promotores assim destravam nosso crescimento. Se conhece outro CTO/Head Cloud que sofreria com o mesmo problema, manda contato — indicação direta é o canal que mais converte."
    elif score <= 6:
        msg = "Obrigada pelo sinal honesto. Vou te chamar pessoalmente pra entender o que faltou — quero corrigir o gap."

    return _render(
        "Obrigada",
        f'<h1>Nota {score}/10 registrada ✓</h1><p class="ok">{msg}</p>',
    )


# -----------------------------------------------------------------------------
# DEBUG — dump engagement artifacts (admin-only, for smoke testing)
# -----------------------------------------------------------------------------


@router.get("/debug/engagement/{engagement_id}")
async def debug_engagement(engagement_id: str, token: str):
    """Dump full engagement row including artifacts. Admin-only.

    Token = HMAC(CONTRACT_HMAC_SECRET, 'admin_smoke') — same as /api/_admin/smoke/token.
    """
    if not HMAC_SECRET or token != _admin_token():
        raise HTTPException(401, "bad admin token")
    eng = await _get_engagement(engagement_id)
    if not eng:
        raise HTTPException(404, "engagement not found")
    return JSONResponse(eng)


@router.get("/debug/lead/{lead_id}")
async def debug_lead(lead_id: str, token: str):
    """Dump engagement and lead artifacts. Admin-only."""
    if not HMAC_SECRET or token != _admin_token():
        raise HTTPException(401, "bad admin token")
    async with httpx.AsyncClient(timeout=15) as client:
        r1 = await client.get(
            f"{SUPA_URL}/leads?id=eq.{lead_id}&select=*",
            headers=SUPA_HEADERS,
        )
        r2 = await client.get(
            f"{SUPA_URL}/engagements?lead_id=eq.{lead_id}&select=*",
            headers=SUPA_HEADERS,
        )
    return JSONResponse(
        {
            "lead": (r1.json() if r1.status_code == 200 else []),
            "engagements": (r2.json() if r2.status_code == 200 else []),
        }
    )
