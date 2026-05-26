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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#fafaf9; --card:#ffffff; --ink:#1c1917; --ink-soft:#44403c;
    --muted:#78716c; --line:#e7e5e4; --line-strong:#d6d3d1;
    --accent:#0c4a6e; --accent-soft:#e0f2fe; --ok:#15803d; --ok-bg:#f0fdf4;
    --err:#b91c1c; --err-bg:#fef2f2; --warn:#b45309; --warn-bg:#fffbeb;
    --chip-bg:#f5f5f4; --chip-active:#0c4a6e;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:var(--bg); color:var(--ink); max-width:760px; margin:40px auto;
         padding:32px; line-height:1.55; }}
  h1 {{ font-family:'Playfair Display', Georgia, serif; font-weight:600;
        font-size:32px; margin:8px 0 18px; letter-spacing:-0.01em; }}
  h2 {{ font-family:'Playfair Display', Georgia, serif; font-weight:500;
        font-size:20px; margin:32px 0 8px; color:var(--ink); }}
  .card {{ background:var(--card); border:1px solid var(--line);
           border-radius:14px; padding:36px; box-shadow:0 1px 3px rgba(0,0,0,0.04); }}
  .eyebrow {{ font-size:11px; letter-spacing:0.18em; text-transform:uppercase;
              color:var(--accent); margin:0 0 6px; font-weight:600; }}
  .section {{ margin-top:28px; padding-top:20px; border-top:1px solid var(--line); }}
  .section:first-of-type {{ border-top:none; padding-top:0; margin-top:0; }}
  .section-title {{ font-family:'Playfair Display', Georgia, serif; font-weight:500;
                    font-size:15px; color:var(--ink-soft); text-transform:none;
                    letter-spacing:0; margin:0 0 4px; }}
  label {{ display:block; margin-top:18px; font-weight:500; font-size:14px; color:var(--ink); }}
  .help {{ font-size:12px; color:var(--muted); margin-top:3px; font-weight:400; }}
  input, textarea, select {{ width:100%; padding:10px 12px; border:1px solid var(--line-strong);
                              border-radius:6px; font-size:14px; font-family:inherit;
                              background:#fff; color:var(--ink); margin-top:6px;
                              transition:border-color 0.15s; }}
  input:focus, textarea:focus, select:focus {{ outline:none; border-color:var(--accent); }}
  textarea {{ min-height:110px; resize:vertical; }}
  button {{ display:inline-block; background:var(--ink); color:#fff;
           padding:12px 24px; border:none; border-radius:8px; font-weight:600;
           font-size:15px; cursor:pointer; margin-top:24px; font-family:inherit; }}
  button:hover {{ background:#0c0a09; }}
  button.success {{ background:var(--ok); }}
  button.ghost {{ background:transparent; color:var(--accent);
                  border:1px solid var(--line-strong); padding:6px 12px;
                  font-size:13px; margin-top:8px; }}
  .muted {{ color:var(--muted); font-size:13px; }}
  .err {{ background:var(--err-bg); color:var(--err); padding:14px; border-radius:6px;
          margin-bottom:20px; }}
  .ok {{ background:var(--ok-bg); color:var(--ok); padding:14px 16px;
         border-radius:8px; margin-bottom:20px; }}
  .multi-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 14px;
                 margin-top:8px; }}
  .multi-grid label.opt {{ display:flex; align-items:flex-start; gap:8px;
                            font-weight:400; font-size:13px; margin-top:0;
                            padding:6px 8px; border-radius:5px; cursor:pointer;
                            transition:background 0.1s; line-height:1.35; }}
  .multi-grid label.opt:hover {{ background:var(--chip-bg); }}
  .multi-grid input[type=checkbox] {{ width:auto; margin:3px 0 0; flex-shrink:0; }}
  .chips-input {{ position:relative; }}
  .chips-box {{ display:flex; flex-wrap:wrap; gap:6px; padding:8px;
                border:1px solid var(--line-strong); border-radius:6px;
                background:#fff; min-height:44px; margin-top:6px; }}
  .chip {{ display:inline-flex; align-items:center; gap:6px; padding:4px 10px;
           background:var(--chip-bg); color:var(--ink); border-radius:20px;
           font-size:13px; font-weight:500; }}
  .chip .x {{ cursor:pointer; color:var(--muted); font-weight:700; padding:0 0 0 2px; }}
  .chip .x:hover {{ color:var(--err); }}
  .chips-box input.chip-input {{ flex:1; min-width:160px; border:none;
                                  padding:4px 6px; margin:0; font-size:13px;
                                  background:transparent; }}
  .chips-box input.chip-input:focus {{ outline:none; border:none; }}
  .stakeholder-row {{ display:grid; grid-template-columns:1fr 1fr 1fr auto;
                       gap:8px; align-items:end; margin-top:10px;
                       padding:10px; background:#fafaf9; border-radius:8px;
                       border:1px solid var(--line); }}
  .stakeholder-row input {{ margin-top:2px; padding:8px 10px; font-size:13px; }}
  .stakeholder-row .field-label {{ font-size:11px; color:var(--muted);
                                    text-transform:uppercase; letter-spacing:0.05em;
                                    font-weight:600; margin-bottom:2px; display:block; }}
  .stakeholder-row .remove-btn {{ background:transparent; color:var(--err);
                                   border:1px solid var(--line-strong);
                                   border-radius:6px; padding:8px 10px;
                                   font-size:12px; margin:0; cursor:pointer; }}
  .stakeholder-row .remove-btn:hover {{ background:var(--err-bg); }}
  .stakeholder-add {{ background:transparent; color:var(--accent);
                       border:1px dashed var(--line-strong); border-radius:8px;
                       padding:10px 14px; font-size:13px; cursor:pointer;
                       margin-top:10px; width:100%; font-weight:500; }}
  .stakeholder-add:hover {{ background:var(--accent-soft); border-color:var(--accent); }}
  .currency-prefix {{ position:relative; }}
  .currency-prefix:before {{ content:"R$"; position:absolute; left:12px;
                              top:50%; transform:translateY(-30%);
                              color:var(--muted); font-size:14px;
                              pointer-events:none; }}
  .currency-prefix input {{ padding-left:34px; }}
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


# AWS services catalog for the chips autocomplete (FinOps intake)
_AWS_SERVICES_CATALOG = [
    "EC2", "ECS", "EKS", "Fargate", "Lambda", "Batch", "Lightsail", "Outposts",
    "RDS", "Aurora", "DynamoDB", "DocumentDB", "ElastiCache", "MemoryDB",
    "Neptune", "Timestream", "Keyspaces",
    "S3", "EFS", "FSx", "EBS", "Storage Gateway", "Backup",
    "CloudFront", "Route 53", "ALB", "NLB", "API Gateway", "Direct Connect",
    "VPN", "Transit Gateway", "Global Accelerator",
    "SageMaker", "Bedrock", "Comprehend", "Rekognition", "Textract", "Polly",
    "Transcribe", "Translate", "Forecast", "Personalize", "Q",
    "Glue", "Athena", "Redshift", "EMR", "Kinesis", "MSK", "OpenSearch",
    "QuickSight", "Lake Formation", "DataZone",
    "CodeCommit", "CodeBuild", "CodeDeploy", "CodePipeline", "CodeArtifact",
    "CodeGuru", "Cloud9",
    "CloudWatch", "X-Ray", "CloudTrail", "Config", "Systems Manager",
    "Trusted Advisor", "Compute Optimizer", "Cost Explorer",
    "IAM", "Cognito", "KMS", "Secrets Manager", "WAF", "Shield", "GuardDuty",
    "Inspector", "Macie", "Security Hub", "Detective",
    "EventBridge", "SQS", "SNS", "Step Functions", "MQ", "AppFlow", "AppSync",
    "WorkSpaces", "AppStream", "WorkDocs", "Chime", "Connect", "SES", "Pinpoint",
    "IoT Core", "Greengrass", "FreeRTOS", "RoboMaker",
    "Organizations", "Control Tower", "SSO", "Resource Access Manager",
    "Service Catalog",
]


# Field spec format: (name, label, type, required, opts_dict)
# Types: text, email, number, textarea, select, currency_brl, multiselect,
#        aws_services_chips, stakeholders_list
# `opts` keys: placeholder, help_text, options
#   options for select: list[str] OR list[(value, label) tuples]
#   options for multiselect: list[str]

_INTAKE_FIELDS = {
    "finops": [
        # Section: Identification
        ("executive_sponsor_name", "Nome do executive sponsor", "text", True, {}),
        ("executive_sponsor_email", "Email do sponsor (CC nos deliverables)", "email", True, {}),
        ("company_name", "Nome da empresa", "text", True, {}),

        # Section: Business context
        ("industry_vertical", "Vertical / indústria", "select", True, {
            "options": ["SaaS B2B", "E-commerce", "Fintech", "Healthtech",
                        "EdTech", "Marketplace", "Media/AdTech", "Logistics",
                        "Manufacturing", "Other"]}),
        ("arr_range_brl", "ARR aproximado (R$)", "select", True, {
            "options": ["< 5M", "5-15M", "15-50M", "50-200M", "> 200M",
                        "Pré-receita"]}),
        ("growth_stage", "Estágio de crescimento", "select", True, {
            "options": ["Pré-Seed", "Seed", "Series A", "Series B", "Series C+",
                        "Profitable Mature", "Outro"]}),
        ("urgency", "Timeline esperado pra primeiros resultados", "select", True, {
            "options": ["Urgente (board pressure / quarter end)",
                        "4-6 semanas (padrão)",
                        "Flexível (3+ meses)"]}),

        # Section: AWS environment
        ("aws_spend_brl_monthly", "AWS spend mensal aproximado (R$)", "currency_brl", True, {
            "help_text": "Valor médio dos últimos 3 meses, antes de descontos. Ex: R$ 95.000"}),
        ("aws_account_count", "Quantos AWS accounts (production+staging+dev)?", "number", True, {}),
        ("aws_organizations_structure", "Estrutura AWS Organizations", "select", True, {
            "options": ["Single account", "Multi-account sem OU",
                        "OUs por ambiente (prod/staging/dev)",
                        "OUs por unidade de negócio", "Não tenho certeza"]}),
        ("aws_regions", "Regions AWS principais (multi-select)", "multiselect", True, {
            "options": ["us-east-1 (N. Virginia)", "us-east-2 (Ohio)",
                        "us-west-2 (Oregon)", "sa-east-1 (São Paulo)",
                        "eu-west-1 (Ireland)", "eu-central-1 (Frankfurt)",
                        "ap-southeast-1 (Singapore)", "Outras"]}),
        ("ri_sp_coverage", "Cobertura atual de Reserved Instances / Savings Plans", "select", True, {
            "options": ["Não temos", "< 30% do compute",
                        "30-70% do compute", "> 70% do compute", "Não sei"]}),

        # Section: Services + observability
        ("primary_services", "Serviços AWS principais em uso (chips)", "aws_services_chips", True, {
            "help_text": "Comece a digitar pra buscar. Ex: EC2, RDS, S3..."}),
        ("observability_stack", "Stack de observability atual (multi-select)", "multiselect", False, {
            "options": ["CloudWatch (default)", "Datadog",
                        "Grafana + Loki + Tempo", "New Relic", "Dynatrace",
                        "Honeycomb", "Splunk", "Elastic Stack",
                        "OpenTelemetry", "Outro/Nenhum"]}),
        ("tagging_strategy", "Estratégia atual de tagging (1-2 parágrafos)", "textarea", True, {
            "help_text": "Como vocês taggeiam recursos hoje? Ex: 'temos owner+environment em ~70% dos recursos' ou 'sem padrão'"}),

        # Section: Pain + compliance
        ("biggest_concerns", "Maiores preocupações de custo que já mapeou (uma por linha)", "textarea", True, {
            "placeholder": "Ex: RDS sobre-provisionado\nNAT Gateway custo crescendo\nSpend imprevisível mês a mês"}),
        ("why_now", "Por que rodar essa auditoria AGORA?", "textarea", True, {
            "placeholder": "Ex: 'CFO pediu plano de redução 25% antes do Q4', 'bill cresceu 60% YoY sem revenue match', 'preparação pra rodada de investimento'"}),
        ("compliance_frames", "Compliance frameworks aplicáveis (multi-select)", "multiselect", False, {
            "options": ["LGPD", "SOC 2 Type II", "BACEN 4.658 (fintech BR)",
                        "GxP / ANVISA / FDA", "HIPAA (US healthcare)",
                        "ISO 27001", "PCI DSS", "Nenhum aplicável"]}),

        # Section: Implementation choice
        ("remediation_choice", "Após findings, quem implementa quick wins?", "select", True, {
            "options": [
                ("cliente_interno", "Time interno de vocês executa (autônomo)"),
                ("anuvia_success_fee",
                 "Anuvia executa via success-fee 15-20% da economia validada"),
            ]}),

        # Section: Stakeholders
        ("stakeholders", "Stakeholders pras 4 sessões executivas (Mila apresenta ao vivo)",
         "stakeholders_list", True, {
             "help_text": "Adicione todos que vão participar das apresentações: CTO, Head Cloud, CFO, etc."}),
    ],
    "ai": [
        ("executive_sponsor_name", "Nome do executive sponsor", "text", True, {}),
        ("executive_sponsor_email", "Email do sponsor", "email", True, {}),
        ("company_name", "Nome da empresa", "text", True, {}),
        ("stakeholders", "Stakeholders pro workshop", "stakeholders_list", True, {
            "help_text": "Quem participa do workshop AI (CTO, Head Data, PMs)"}),
        ("past_pocs", "PoCs IA já tentados (nome + status: live / killed / stalled)",
         "textarea", True, {
             "placeholder": "Ex:\nchatbot atendimento — stalled\nrecomendação produto — live"}),
        ("data_assets", "Datasets disponíveis (CRM, transaction logs, etc.)", "textarea", True, {}),
        ("compliance_frames", "Compliance frameworks aplicáveis", "multiselect", False, {
            "options": ["LGPD", "SOC 2 Type II", "BACEN 4.658 (fintech BR)",
                        "GxP / ANVISA / FDA", "HIPAA (US healthcare)",
                        "ISO 27001", "PCI DSS", "Nenhum aplicável"]}),
        ("annual_ai_budget_brl", "Budget anual IA (R$)", "currency_brl", True, {}),
        ("internal_ai_capability", "Capacidade interna IA/ML", "select", True, {
            "options": [
                ("none", "Sem time IA dedicado"),
                ("1-2 engineers", "1-2 engenheiros com tempo parcial"),
                ("dedicated team", "Time dedicado IA/ML"),
            ]}),
    ],
    "devops": [
        ("executive_sponsor_name", "Nome do executive sponsor", "text", True, {}),
        ("executive_sponsor_email", "Email do sponsor", "email", True, {}),
        ("company_name", "Nome da empresa", "text", True, {}),
        ("engineering_team_size", "Tamanho time engenharia", "number", True, {}),
        ("squads_count", "Número de squads", "number", True, {}),
        ("production_services_count", "Número de serviços em produção", "number", True, {}),
        ("ci_tool", "CI tool (GitHub Actions, Jenkins, CircleCI, GitLab CI)", "text", True, {}),
        ("incident_tracker", "Incident tracker (Linear, Jira, Opsgenie)", "text", True, {}),
        ("self_reported_deploy_frequency", "Deploy frequency atual", "select", True, {
            "options": [
                ("daily", "Diário (ou mais)"),
                ("weekly", "Semanal"),
                ("monthly", "Mensal"),
                ("quarterly", "Trimestral"),
                ("ad-hoc", "Ad-hoc / sem cadência"),
            ]}),
        ("self_reported_mttr_hours", "MTTR médio (horas)", "number", True, {}),
        ("self_reported_cfr_pct", "Change failure rate atual (%)", "number", True, {}),
        ("observability_stack", "Stack de observability atual", "multiselect", True, {
            "options": ["CloudWatch (default)", "Datadog",
                        "Grafana + Loki + Tempo", "New Relic", "Dynatrace",
                        "Honeycomb", "Splunk", "Elastic Stack",
                        "OpenTelemetry", "Outro/Nenhum"]}),
        ("post_mortem_culture", "Cultura post-mortem", "select", True, {
            "options": [
                ("none", "Não fazemos post-mortems"),
                ("some", "Fazemos quando o incidente é grande"),
                ("always", "Sempre, com runbook documentado"),
            ]}),
        ("stakeholders", "Stakeholders pras sessões de delivery", "stakeholders_list", True, {
            "help_text": "Eng leads, SRE leads, CTO — quem participa das presentations"}),
    ],
    "growth": [
        ("executive_sponsor_name", "Nome do executive sponsor", "text", True, {}),
        ("executive_sponsor_email", "Email do sponsor", "email", True, {}),
        ("company_name", "Nome da empresa", "text", True, {}),
        ("crm_in_use", "CRM atual (HubSpot, Salesforce, Pipedrive, RD, Notion)", "text", True, {}),
        ("sales_team_composition", "Composição time comercial", "text", True, {}),
        ("sales_cycle_median_days", "Sales cycle mediano (dias)", "number", True, {}),
        ("avg_ticket_brl", "Ticket médio (R$)", "currency_brl", True, {}),
        ("lead_sources", "Canais de entrada (vírgula-separado)", "text", True, {}),
        ("monthly_volume", "Volume mensal (leads / qualified / closed, separado por barra)", "text", True, {}),
        ("response_time_sla", "SLA de resposta (goal vs atual)", "text", True, {}),
        ("top_pain_points", "Top 3 dores (uma por linha)", "textarea", True, {}),
        ("stakeholders", "Stakeholders pras sessões executivas", "stakeholders_list", True, {
            "help_text": "Head of Sales, RevOps, SDR Lead, etc."}),
    ],
    "industry": [
        ("executive_sponsor_name", "Nome do executive sponsor", "text", True, {}),
        ("executive_sponsor_email", "Email do sponsor", "email", True, {}),
        ("company_name", "Nome da empresa", "text", True, {}),
        ("vertical", "Vertical", "select", True, {
            "options": [
                ("manufacturing", "Manufacturing / industrial"),
                ("logistics", "Logistics / supply chain"),
                ("healthcare", "Healthcare provider"),
                ("life_sciences", "Life sciences / pharma"),
                ("finserv", "Financial services"),
            ]}),
        ("company_revenue_brl", "Revenue anual (R$)", "currency_brl", True, {}),
        ("main_pain", "Dor principal específica do vertical", "textarea", True, {}),
        ("compliance_frames", "Compliance frameworks aplicáveis", "multiselect", False, {
            "options": ["LGPD", "SOC 2 Type II", "BACEN 4.658 (fintech BR)",
                        "GxP / ANVISA / FDA", "HIPAA (US healthcare)",
                        "ISO 27001", "PCI DSS", "ANVISA", "Nenhum aplicável"]}),
        ("ai_maturity", "AI maturity", "select", True, {
            "options": [
                ("none", "Não temos IA hoje"),
                ("exploring", "Explorando casos isolados"),
                ("scaling", "IA em produção, escalando"),
            ]}),
        ("stakeholders", "Stakeholders pras sessões executivas", "stakeholders_list", True, {
            "help_text": "Quem participa das apresentações ao vivo"}),
    ],
}


# Fields that submit as JSON arrays (multi-value form keys)
_MULTI_VALUE_FIELDS = {
    name
    for fields in _INTAKE_FIELDS.values()
    for (name, _label, ftype, _req, _opts) in fields
    if ftype in ("multiselect", "aws_services_chips")
}

# Currency fields (submitted as raw integers after JS strips the formatting)
_CURRENCY_FIELDS = {
    name
    for fields in _INTAKE_FIELDS.values()
    for (name, _label, ftype, _req, _opts) in fields
    if ftype == "currency_brl"
}


def _escape(s: str) -> str:
    """Minimal HTML attribute escaping."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_field(name: str, label: str, ftype: str, required: bool,
                  opts: dict) -> str:
    """Render a single intake field. Returns HTML fragment."""
    req = "required" if required else ""
    placeholder = opts.get("placeholder", "")
    help_text = opts.get("help_text", "")
    help_html = f'<div class="help">{_escape(help_text)}</div>' if help_text else ""
    safe_label = _escape(label)
    ph_attr = f'placeholder="{_escape(placeholder)}"' if placeholder else ""

    if ftype == "textarea":
        return (
            f'<label for="{name}">{safe_label}</label>'
            f'{help_html}'
            f'<textarea id="{name}" name="{name}" {req} {ph_attr}></textarea>'
        )

    if ftype == "select":
        options = opts.get("options", [])
        opt_html = ['<option value="">Selecione...</option>']
        for o in options:
            if isinstance(o, (tuple, list)) and len(o) == 2:
                value, lbl = o
                opt_html.append(
                    f'<option value="{_escape(value)}">{_escape(lbl)}</option>'
                )
            else:
                opt_html.append(
                    f'<option value="{_escape(o)}">{_escape(o)}</option>'
                )
        return (
            f'<label for="{name}">{safe_label}</label>'
            f'{help_html}'
            f'<select id="{name}" name="{name}" {req}>'
            f'{"".join(opt_html)}</select>'
        )

    if ftype == "multiselect":
        options = opts.get("options", [])
        items = []
        for o in options:
            items.append(
                f'<label class="opt"><input type="checkbox" name="{name}" '
                f'value="{_escape(o)}"/><span>{_escape(o)}</span></label>'
            )
        return (
            f'<label>{safe_label}</label>'
            f'{help_html}'
            f'<div class="multi-grid">{"".join(items)}</div>'
        )

    if ftype == "currency_brl":
        return (
            f'<label for="{name}">{safe_label}</label>'
            f'{help_html}'
            f'<div class="currency-prefix">'
            f'<input type="text" inputmode="numeric" id="{name}" '
            f'name="{name}" data-currency-brl="1" {req} '
            f'autocomplete="off" placeholder="0"/>'
            f'</div>'
        )

    if ftype == "aws_services_chips":
        datalist_opts = "".join(
            f'<option value="{_escape(s)}"></option>'
            for s in _AWS_SERVICES_CATALOG
        )
        return (
            f'<label>{safe_label}</label>'
            f'{help_html}'
            f'<div class="chips-input" data-chips-field="{name}">'
            f'<div class="chips-box" id="chips-box-{name}">'
            f'<input type="text" class="chip-input" '
            f'list="aws-services-list" '
            f'placeholder="Digite e tecle Enter (ou vírgula)…" '
            f'autocomplete="off"/>'
            f'</div>'
            f'<datalist id="aws-services-list">{datalist_opts}</datalist>'
            f'</div>'
        )

    if ftype == "stakeholders_list":
        return (
            f'<label>{safe_label}</label>'
            f'{help_html}'
            f'<div id="stakeholders-rows" data-min="1" data-max="8">'
            f'</div>'
            f'<button type="button" class="stakeholder-add" '
            f'onclick="addStakeholderRow()">+ Adicionar stakeholder</button>'
        )

    # Default: text/email/number/etc.
    return (
        f'<label for="{name}">{safe_label}</label>'
        f'{help_html}'
        f'<input type="{ftype}" id="{name}" name="{name}" {req} {ph_attr}/>'
    )


# JS for currency formatting, chips, stakeholders. Lives in the form page.
_INTAKE_JS = r"""
<script>
(function(){
  // -- Currency BR formatter (e.g. 120000 -> "120.000") --
  function fmtBR(digits){
    if(!digits) return "";
    // Group every 3 digits from the right with dots.
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }
  document.querySelectorAll("input[data-currency-brl]").forEach(function(inp){
    inp.addEventListener("input", function(){
      var digits = inp.value.replace(/\D/g, "");
      inp.value = fmtBR(digits);
    });
  });

  // -- AWS services chips --
  // Each .chips-input has a hidden serialized JSON value injected on submit.
  var chipFields = document.querySelectorAll(".chips-input");
  chipFields.forEach(function(wrapper){
    var fieldName = wrapper.getAttribute("data-chips-field");
    var box = wrapper.querySelector(".chips-box");
    var input = wrapper.querySelector(".chip-input");
    var chips = [];

    function render(){
      // Clear all but the input
      Array.from(box.querySelectorAll(".chip")).forEach(function(c){ c.remove(); });
      chips.forEach(function(label, idx){
        var span = document.createElement("span");
        span.className = "chip";
        span.innerHTML = '<span class="chip-label"></span><span class="x">×</span>';
        span.querySelector(".chip-label").textContent = label;
        span.querySelector(".x").addEventListener("click", function(){
          chips.splice(idx, 1);
          render();
        });
        box.insertBefore(span, input);
      });
    }
    function addChip(val){
      val = (val || "").trim();
      if(!val) return;
      if(chips.indexOf(val) === -1){ chips.push(val); render(); }
      input.value = "";
    }
    input.addEventListener("keydown", function(e){
      if(e.key === "Enter" || e.key === ","){
        e.preventDefault();
        addChip(input.value.replace(/,$/, ""));
      } else if(e.key === "Backspace" && input.value === "" && chips.length){
        chips.pop(); render();
      }
    });
    input.addEventListener("change", function(){
      // Triggered when user picks from datalist (and on blur sometimes).
      if(input.value){ addChip(input.value); }
    });
    // Expose serializer for the form submit handler
    wrapper.__getChipValues = function(){ return chips.slice(); };
  });

  // -- Stakeholders dynamic rows --
  var stkContainer = document.getElementById("stakeholders-rows");
  var stkIdx = 0;
  function rowHtml(idx){
    return ''
      + '<div class="stakeholder-row" data-stk-row="' + idx + '">'
      +   '<div><span class="field-label">Nome</span>'
      +     '<input type="text" name="stakeholder_name_' + idx + '" placeholder="Ex: Ana Silva"/></div>'
      +   '<div><span class="field-label">Email</span>'
      +     '<input type="email" name="stakeholder_email_' + idx + '" placeholder="ana@empresa.com"/></div>'
      +   '<div><span class="field-label">Papel</span>'
      +     '<input type="text" name="stakeholder_role_' + idx + '" placeholder="CTO / Head Cloud / CFO"/></div>'
      +   '<button type="button" class="remove-btn" onclick="removeStakeholderRow(' + idx + ')">Remover</button>'
      + '</div>';
  }
  window.addStakeholderRow = function(){
    if(!stkContainer) return;
    var max = parseInt(stkContainer.getAttribute("data-max") || "8", 10);
    var current = stkContainer.querySelectorAll(".stakeholder-row").length;
    if(current >= max){ return; }
    stkContainer.insertAdjacentHTML("beforeend", rowHtml(stkIdx++));
  };
  window.removeStakeholderRow = function(idx){
    var row = stkContainer.querySelector('[data-stk-row="' + idx + '"]');
    var min = parseInt(stkContainer.getAttribute("data-min") || "1", 10);
    var current = stkContainer.querySelectorAll(".stakeholder-row").length;
    if(current <= min){ return; }
    if(row){ row.remove(); }
  };
  if(stkContainer && stkContainer.children.length === 0){ addStakeholderRow(); }

  // -- Submit interceptor: clean currencies + chips + stakeholders --
  var form = document.getElementById("intake-form");
  if(form){
    form.addEventListener("submit", function(){
      // 1) Strip dots in currency fields (server gets "120000")
      form.querySelectorAll("input[data-currency-brl]").forEach(function(inp){
        inp.value = inp.value.replace(/\./g, "");
      });
      // 2) Serialize chips into hidden inputs
      chipFields.forEach(function(wrapper){
        var fieldName = wrapper.getAttribute("data-chips-field");
        // Remove any previously injected hiddens
        wrapper.querySelectorAll('input[type=hidden][data-chip-hidden]').forEach(function(h){h.remove();});
        var values = wrapper.__getChipValues();
        values.forEach(function(v){
          var h = document.createElement("input");
          h.type = "hidden"; h.name = fieldName; h.value = v;
          h.setAttribute("data-chip-hidden", "1");
          wrapper.appendChild(h);
        });
      });
      // 3) Compact stakeholders: collected server-side by walking
      //    stakeholder_name_*, stakeholder_email_*, stakeholder_role_*.
    });
  }
})();
</script>
"""


def _section_titles_for(practice: str) -> dict:
    """Map (field_name -> section_title) for visual grouping.

    Only finops uses richer grouping. Other practices render flat.
    """
    if practice != "finops":
        return {}
    return {
        "executive_sponsor_name": "Identificação",
        "industry_vertical": "Contexto de negócio",
        "aws_spend_brl_monthly": "Ambiente AWS",
        "primary_services": "Serviços + observability",
        "biggest_concerns": "Dores + compliance",
        "remediation_choice": "Após findings",
        "stakeholders": "Apresentações executivas",
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

    section_starters = _section_titles_for(practice)

    chunks: list[str] = []
    open_section = False
    for spec in fields:
        # Support both old 4-tuple and new 5-tuple for safety
        if len(spec) == 5:
            name, label, ftype, required, opts = spec
        else:
            name, label, ftype, required = spec
            opts = {}

        if name in section_starters:
            if open_section:
                chunks.append("</div>")
            chunks.append(
                f'<div class="section"><div class="section-title">'
                f'{_escape(section_starters[name])}</div>'
            )
            open_section = True

        chunks.append(_render_field(name, label, ftype, required, opts or {}))

    if open_section:
        chunks.append("</div>")

    body = f"""
    <p class="eyebrow">Anuvia · {_escape(practice_label)}</p>
    <h1>Formulário de intake</h1>
    <p class="muted" style="margin-bottom:8px;">Esses dados destravam a análise da próxima fase. Leva ~10 minutos. Salve só quando estiver completo.</p>
    <form id="intake-form" method="POST" action="/api/delivery/{practice}/intake?engagement_id={engagement_id}&amp;token={token}">
      {''.join(chunks)}
      <button type="submit">Enviar e iniciar análise →</button>
    </form>
    {_INTAKE_JS}
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


def _parse_intake_form(form, practice: str) -> dict:
    """Parse multi-value form data into a normalized intake dict.

    - Multi-value fields (multiselect, chips) → list[str]
    - currency_brl fields → int (digits only)
    - stakeholder_name_*/email_*/role_* → list[{name,email,role}]
    - Everything else → str
    """
    intake: dict = {}
    # Determine field types from spec for this practice
    field_types = {
        name: ftype
        for (name, _label, ftype, _req, _opts) in _INTAKE_FIELDS.get(practice, [])
    }

    # multi_items() gives every (key, value) pair in submission order; use it
    # to collect lists for multiselect / chips.
    try:
        pairs = list(form.multi_items())
    except AttributeError:  # fallback (very old Starlette)
        pairs = [(k, form[k]) for k in form.keys()]

    # Group stakeholders by index suffix
    stakeholders_by_idx: dict[str, dict] = {}

    grouped: dict[str, list[str]] = {}
    for key, value in pairs:
        # Stakeholder repeatables
        if key.startswith("stakeholder_name_"):
            idx = key.split("_", 2)[-1]
            stakeholders_by_idx.setdefault(idx, {})["name"] = value
            continue
        if key.startswith("stakeholder_email_"):
            idx = key.split("_", 2)[-1]
            stakeholders_by_idx.setdefault(idx, {})["email"] = value
            continue
        if key.startswith("stakeholder_role_"):
            idx = key.split("_", 2)[-1]
            stakeholders_by_idx.setdefault(idx, {})["role"] = value
            continue
        grouped.setdefault(key, []).append(value)

    for key, values in grouped.items():
        ftype = field_types.get(key)
        # Multi-value fields always submit as list
        if ftype in ("multiselect", "aws_services_chips"):
            cleaned = [v for v in values if v]
            if cleaned:
                intake[key] = cleaned
            continue
        # Currency: strip non-digits, store int
        if ftype == "currency_brl" or key in _CURRENCY_FIELDS:
            raw = (values[-1] or "").replace(".", "").replace(",", "").strip()
            if raw.isdigit():
                intake[key] = int(raw)
            elif raw:
                intake[key] = raw  # bad input, keep raw for debugging
            continue
        # Number: cast to int if possible
        if ftype == "number":
            raw = (values[-1] or "").strip()
            if raw.replace("-", "").isdigit():
                try:
                    intake[key] = int(raw)
                except ValueError:
                    intake[key] = raw
            elif raw:
                intake[key] = raw
            continue
        # Default: single-value scalar (last wins)
        v = values[-1]
        if v not in ("", None):
            intake[key] = v

    # Collect stakeholders into a list (only keep rows with at least a name)
    if stakeholders_by_idx:
        stk_list = []
        for idx in sorted(stakeholders_by_idx.keys(),
                          key=lambda s: int(s) if s.isdigit() else 9999):
            row = stakeholders_by_idx[idx]
            if (row.get("name") or "").strip():
                stk_list.append({
                    "name": (row.get("name") or "").strip(),
                    "email": (row.get("email") or "").strip(),
                    "role": (row.get("role") or "").strip(),
                })
        if stk_list:
            intake["stakeholders"] = stk_list

    return intake


@router.post("/{practice}/intake")
async def intake_post(practice: str, request: Request):
    engagement_id = request.query_params.get("engagement_id", "")
    token = request.query_params.get("token", "")
    if practice not in _PRACTICE_ROUTE:
        return _err_page(f"prática desconhecida: {practice}", 404)
    if not _verify_token(engagement_id, "intake", token):
        return _err_page("token inválido", 401)

    form = await request.form()
    intake = _parse_intake_form(form, practice)

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

    delivery_mode = (eng.get("delivery_mode") or "whiteglove").strip().lower()
    if delivery_mode == "autonomous":
        body = (
            '<p class="eyebrow">Anuvia · Intake recebido</p>'
            '<h1>Intake recebido ✓</h1>'
            '<p class="ok">Tudo certo. A análise da próxima fase já entrou na fila — '
            'orchestrator processa nas próximas horas. Você recebe o relatório '
            'por email.</p>'
            '<p class="muted">Sem ação adicional do teu lado por enquanto.</p>'
        )
    else:
        # whiteglove default
        body = (
            '<p class="eyebrow">Anuvia · Intake recebido</p>'
            '<h1>Intake recebido ✓</h1>'
            '<p class="ok">Os dados chegaram em nossa equipe. Nas próximas 24h, '
            'Mila Vernazza analisa o material e agenda uma sessão executiva para '
            'apresentar os findings da semana 2 ao vivo (60 min). O convite '
            'chega no email do sponsor — todos os stakeholders listados serão CC\'d.</p>'
            '<p class="muted">Próximos passos:</p>'
            '<ol class="muted">'
            '<li>Anuvia gera análise + materiais (~3-5 dias)</li>'
            '<li>Apresentação executiva da semana 2 (Mila ao vivo) — convite por email</li>'
            '<li>Após apresentação: materiais (PDF + PPTX) enviados oficialmente</li>'
            '</ol>'
        )
    return _render("Intake recebido", body)


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


@router.get("/debug/infra")
async def debug_infra(token: str):
    """Probe Gotenberg + Supabase Storage to diagnose PDF generation pipeline.

    Returns:
      gotenberg: {url, reachable, status, error?, html_to_pdf_ok}
      storage: {bucket, upload_ok, public_url?, error?}
      slack: {webhook_set, ping_ok}
    """
    if not HMAC_SECRET or token != _admin_token():
        raise HTTPException(401, "bad admin token")

    result = {"gotenberg": {}, "storage": {}, "slack": {}, "env": {}}

    # --- Gotenberg ---
    gotenberg_url = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000").rstrip("/")
    result["env"]["GOTENBERG_URL"] = gotenberg_url
    result["gotenberg"]["url"] = gotenberg_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            health = await client.get(f"{gotenberg_url}/health")
            result["gotenberg"]["reachable"] = True
            result["gotenberg"]["health_status"] = health.status_code
    except Exception as e:
        result["gotenberg"]["reachable"] = False
        result["gotenberg"]["error"] = f"{type(e).__name__}: {e}"

    # Try HTML→PDF
    if result["gotenberg"].get("reachable"):
        try:
            test_html = "<html><body><h1>Anuvia infra test</h1><p>If you can read this in a PDF, Gotenberg works.</p></body></html>"
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"{gotenberg_url}/forms/chromium/convert/html",
                    files={"files": ("index.html", test_html.encode(), "text/html")},
                )
            result["gotenberg"]["html_to_pdf_status"] = r.status_code
            result["gotenberg"]["html_to_pdf_ok"] = r.status_code == 200
            result["gotenberg"]["pdf_bytes"] = len(r.content) if r.status_code == 200 else 0
            if r.status_code != 200:
                result["gotenberg"]["error"] = r.text[:300]
        except Exception as e:
            result["gotenberg"]["html_to_pdf_ok"] = False
            result["gotenberg"]["error"] = f"{type(e).__name__}: {e}"

    # --- Storage ---
    bucket = os.environ.get("ANUVIA_DELIVERABLES_BUCKET", "anuvia-deliverables")
    result["env"]["ANUVIA_DELIVERABLES_BUCKET"] = bucket
    result["storage"]["bucket"] = bucket
    if not SUPA_URL or not SUPA_KEY:
        result["storage"]["error"] = "SUPA_URL or SUPA_KEY missing"
    else:
        base = SUPA_URL.replace("/rest/v1", "")
        test_path = f"_diag/infra_test_{int(datetime.now().timestamp())}.txt"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                up = await client.post(
                    f"{base}/storage/v1/object/{bucket}/{test_path}",
                    headers={
                        "apikey": SUPA_KEY,
                        "Authorization": f"Bearer {SUPA_KEY}",
                        "Content-Type": "text/plain",
                        "x-upsert": "true",
                    },
                    content=b"Anuvia diag test",
                )
            result["storage"]["upload_status"] = up.status_code
            result["storage"]["upload_ok"] = 200 <= up.status_code < 300
            if up.status_code >= 400:
                result["storage"]["error"] = up.text[:300]
            else:
                result["storage"]["public_url"] = (
                    f"{base}/storage/v1/object/public/{bucket}/{test_path}"
                )
        except Exception as e:
            result["storage"]["upload_ok"] = False
            result["storage"]["error"] = f"{type(e).__name__}: {e}"

    # --- Slack ---
    slack_url = (
        os.environ.get("SLACK_NEW_LEAD_WEBHOOK", "")
        or os.environ.get("SLACK_ALERTS_WEBHOOK", "")
    )
    result["env"]["SLACK_NEW_LEAD_WEBHOOK"] = "SET" if os.environ.get("SLACK_NEW_LEAD_WEBHOOK") else "UNSET"
    result["env"]["SLACK_ALERTS_WEBHOOK"] = "SET" if os.environ.get("SLACK_ALERTS_WEBHOOK") else "UNSET"
    result["slack"]["webhook_set"] = bool(slack_url)
    if slack_url:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                sp = await client.post(
                    slack_url,
                    json={"text": "Anuvia infra diag ping (ignore)"},
                )
            result["slack"]["ping_status"] = sp.status_code
            result["slack"]["ping_ok"] = sp.status_code == 200
        except Exception as e:
            result["slack"]["ping_ok"] = False
            result["slack"]["error"] = f"{type(e).__name__}: {e}"

    return JSONResponse(result)
