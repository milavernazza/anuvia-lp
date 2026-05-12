"""
Anuvia Landing Pages — Layer 1 capture engine.

Single-page LPs with diagnostic agents per funnel. v1 ships only BR_SMB at /
(future routes: /eng for BR_ENG, /us-smb for US_SMB, /us-eng for US_ENG).

Flow:
  GET /            -> serves the BR_SMB landing page (hero + wizard form)
  POST /api/diagnose -> validates form, calls Claude for personalized
                        diagnostic, inserts lead in Supabase (which fires the
                        Workflow 2 enrichment webhook), returns rendered HTML.

The lead row triggers the Supabase Database Webhook -> n8n /webhook/lead-enrichment
-> Workflow 2 (enrichment + scoring) -> Workflow 3 (hot lead Slack alert if
score >= 80). No additional wiring needed here.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import frontmatter
import markdown as md_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anuvia-lp")

app = FastAPI(title="Anuvia Landing Pages", version="0.1.0")
templates = Jinja2Templates(directory="templates")

SUPA_URL = os.environ.get("SUPABASE_URL", "https://api.anuvia.com.br/rest/v1").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
SLACK_WEBHOOK = os.environ.get("SLACK_NEW_LEAD_WEBHOOK", "")  # optional, fallback to n8n

# Resend — transactional email pro deliverable do diagnostic
# Quando o domínio anuvia.com.br for verificado em resend.com/domains,
# trocar RESEND_FROM_EMAIL pra contato@anuvia.com.br.
# Por enquanto fallback pro onboarding@resend.dev (sandbox da Resend, sem verificação).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")

# Easyappointments (cal.anuvia.com.br) — uses public booking endpoints
# (no API token needed; same flow that the public booking page uses).
EASY_BASE = os.environ.get(
    "EASYAPPOINTMENTS_BASE_URL", "https://cal.anuvia.com.br"
).rstrip("/")
EASY_PROVIDER_ID = int(os.environ.get("EASYAPPOINTMENTS_PROVIDER_ID", "2"))
TZ_SP = ZoneInfo("America/Sao_Paulo")

# Public holidays per market — block all slots on those days.
# Source: official national holidays (statutory). Add new years as needed.
BR_PUBLIC_HOLIDAYS: set[str] = {
    # 2026
    "2026-01-01",  # Confraternização Universal
    "2026-02-16", "2026-02-17",  # Carnaval
    "2026-04-03",  # Sexta-feira Santa
    "2026-04-21",  # Tiradentes
    "2026-05-01",  # Dia do Trabalho
    "2026-06-04",  # Corpus Christi
    "2026-09-07",  # Independência
    "2026-10-12",  # N. Sra. Aparecida
    "2026-11-02",  # Finados
    "2026-11-15",  # Proclamação República
    "2026-11-20",  # Consciência Negra
    "2026-12-25",  # Natal
    # 2027
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-26", "2027-04-21",
    "2027-05-01", "2027-05-27", "2027-09-07", "2027-10-12", "2027-11-02",
    "2027-11-15", "2027-11-20", "2027-12-25",
}
US_PUBLIC_HOLIDAYS: set[str] = {
    # 2026 — federal holidays observed
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day observed
    "2026-09-07",  # Labor Day
    "2026-10-12",  # Columbus Day
    "2026-11-11",  # Veterans Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-10-11", "2027-11-11", "2027-11-25",
    "2027-12-24",
}


def is_public_holiday(date_str: str, market: str = "BR") -> bool:
    """Return True if a YYYY-MM-DD date is a public holiday for the given market."""
    if market == "US":
        return date_str in US_PUBLIC_HOLIDAYS
    return date_str in BR_PUBLIC_HOLIDAYS


# Google Calendar — server-side multi-account freebusy
# Reuses Easyappointments OAuth client (same project) by adding callback URI in GCP.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GCAL_REDIRECT_URI = os.environ.get(
    "GCAL_REDIRECT_URI", "https://anuvia.com.br/api/admin/gcal/callback"
)
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")  # required for /api/admin/* routes

# In-memory access token cache: {email: (access_token, expires_at_epoch)}
_GCAL_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Per-funnel routing config. Each funnel has its own Easyappointments service
# (different durations) and prompt persona / questions.
FUNNEL_CONFIG: dict[str, dict] = {
    "BR_SMB": {
        "market": "BR",
        "track": "growth_mesh",
        "language": "pt-BR",
        "template": "br_smb.html",
        "easy_service_id": int(os.environ.get("EASYAPPOINTMENTS_SERVICE_ID_BR_SMB", "2")),
        "easy_duration_min": 30,
        "tags": ["lp_diagnostic", "br_smb"],
        "lp_host_alias": "diagnostico",
    },
    "BR_ENG": {
        "market": "BR",
        "track": "engineering",
        "language": "pt-BR",
        "template": "br_eng.html",
        "easy_service_id": int(os.environ.get("EASYAPPOINTMENTS_SERVICE_ID_BR_ENG", "3")),
        "easy_duration_min": 45,
        "tags": ["lp_ai_readiness", "br_eng"],
        "lp_host_alias": "roadmap",
    },
}


def detect_funnel(request: Request) -> str:
    """Pick the funnel based on the Host header.

    diagnostico.anuvia.com.br -> BR_SMB
    roadmap.anuvia.com.br     -> BR_ENG
    everything else            -> BR_SMB (default)
    """
    host = (request.headers.get("host") or "").lower().split(":")[0]
    for fid, cfg in FUNNEL_CONFIG.items():
        alias = cfg.get("lp_host_alias")
        if alias and host.startswith(alias + "."):
            return fid
    return "BR_SMB"

if not SUPA_KEY:
    raise RuntimeError("SUPABASE_KEY env var is required")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY env var is required")

SUPA_HEADERS = {
    "apikey": SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

ANTHROPIC_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


EASY_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


# ----------------------------------------------------------------------------
# Form validation
# ----------------------------------------------------------------------------

PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(raw: str) -> Optional[str]:
    """Normalize a Brazilian phone to E.164. Returns None if it doesn't look valid."""
    digits = PHONE_RE.sub("", raw or "")
    if not digits:
        return None
    if digits.startswith("+"):
        digits = digits[1:]
    # If starts with 55 and has 12-13 digits total, looks already E.164-ish
    if digits.startswith("55") and 12 <= len(digits) <= 13:
        return f"+{digits}"
    # If 10-11 digits (BR local: DDD + number), assume BR
    if 10 <= len(digits) <= 11:
        return f"+55{digits}"
    # Otherwise pass through with + prefix
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


class DiagnosticForm(BaseModel):
    """Flexible form — accepts BR_SMB or BR_ENG fields as extras.

    Required: name / email / whatsapp / company. Funnel-specific question
    fields (business_type, team_size, ... for SMB; setor, tamanho_empresa,
    ... for ENG) are stored as model extras and read with getattr in
    build_diagnostic_user_message and insert_lead.
    """
    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: Optional[str] = Field(default=None, max_length=200)

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido. Use formato (XX) XXXXX-XXXX ou +5511XXXXXXXX.")
        return normalized


# ----------------------------------------------------------------------------
# Claude diagnostic
# ----------------------------------------------------------------------------

SYSTEM_PROMPT_BR_SMB = """Você é Mila Vernazza, founder da Anuvia (consultoria de IA aplicada a vendas e ops).

Você está gerando um diagnóstico personalizado de 5 minutos para um SMB brasileiro
que preencheu um form sobre seu funil comercial. O diagnóstico precisa ser:

- Específico ao tipo de negócio e tamanho informados (não genérico)
- Honesto sobre quanto o negócio provavelmente está perdendo em receita
- Acionável: 3 ações concretas pra próximos 30 dias, priorizadas
- Mostrando como Anuvia pode ajudar SEM ser pitch comercial agressivo

Retorne APENAS um JSON válido com este shape exato:

{
  "diagnostico_resumo": "1-2 parágrafos analisando os pontos fracos do funil",
  "estimativa_perdida": "string com estimativa em R$/mês do que está perdendo, com explicação curta",
  "score_maturidade": <int 0-100>,
  "pontos_fortes": ["forte 1", "forte 2"],
  "pontos_fracos": ["fraco 1", "fraco 2", "fraco 3"],
  "plano": [
    {"etapa": "Semana 1", "acao": "...", "porque": "..."},
    {"etapa": "Semana 2", "acao": "...", "porque": "..."},
    {"etapa": "Semana 3-4", "acao": "...", "porque": "..."}
  ],
  "proximo_passo": "1 frase com CTA específico pro próximo passo"
}

Tom: PT-BR, conversacional, direto, sem jargão de consultoria. Trate o leitor como peer.
Use números reais quando possível (não invente, mas estime baseado em benchmarks)."""


SYSTEM_PROMPT_BR_ENG = """Você é Mila Vernazza, founder da Anuvia (consultoria de IA / engineering aplicada).

Você está gerando um AI Readiness Assessment personalizado para um líder técnico (CTO,
Head of Engineering, Head of Data) de uma empresa brasileira de médio/grande porte que
preencheu um form sobre o estado de IA na operação. O assessment precisa ser:

- Específico ao setor, tamanho e maturidade de IA informados (não genérico)
- Honesto sobre os gaps técnicos (talento, dados, infra, governança, integração)
- Estratégico: roadmap de 90 dias com 3-4 etapas (Mês 1 / Mês 2 / Mês 3)
- Mostrando como Anuvia pode ajudar SEM ser pitch comercial agressivo
- Falando o idioma do técnico (MLOps, embeddings, vector DB, RAG, agentes, IoT, RPA)
  mas sem encher de jargão — clareza acima de demonstração de vocabulário

Retorne APENAS um JSON válido com este shape exato:

{
  "diagnostico_resumo": "1-2 parágrafos analisando o estado atual de IA na empresa e o gap pra o caso de uso priorizado",
  "estimativa_perdida": "string descrevendo o VALOR potencial da implementação ou o CUSTO de não implementar (em R$/ano de eficiência, redução de OPEX, ou aceleração de receita)",
  "score_maturidade": <int 0-100, calibrado: 0-25 nada, 26-50 experimentando, 51-75 pilotos isolados, 76-100 IA em produção sólida>,
  "pontos_fortes": ["forte 1", "forte 2"],
  "pontos_fracos": ["gap técnico 1", "gap técnico 2", "gap técnico 3"],
  "plano": [
    {"etapa": "Mês 1", "acao": "...", "porque": "..."},
    {"etapa": "Mês 2", "acao": "...", "porque": "..."},
    {"etapa": "Mês 3", "acao": "...", "porque": "..."}
  ],
  "proximo_passo": "1 frase com CTA — discovery técnica de 45 min com a Mila pra desenhar arquitetura específica"
}

Tom: PT-BR, técnico mas claro. Trate o leitor como peer técnico sênior.
Se a empresa for <50 pessoas e maturidade baixa, sugira começar pequeno (POC) antes de roadmap completo."""


SYSTEM_PROMPTS = {
    "BR_SMB": SYSTEM_PROMPT_BR_SMB,
    "BR_ENG": SYSTEM_PROMPT_BR_ENG,
}


def build_diagnostic_user_message(form: "DiagnosticForm", funnel: str) -> str:
    if funnel == "BR_ENG":
        lines = [
            f"Setor da empresa: {getattr(form, 'setor', '?')}",
            f"Tamanho (colaboradores): {getattr(form, 'tamanho_empresa', '?')}",
            f"Maturidade de IA hoje: {getattr(form, 'maturidade_ia', '?')}",
            f"Caso de uso prioritário (12 meses): {getattr(form, 'caso_uso', '?')}",
            f"Maior bloqueio hoje: {getattr(form, 'bloqueio', '?')}",
            f"Orçamento típico (12 meses): {getattr(form, 'orcamento', '?')}",
            "",
            f"Responsável: {form.name}",
            f"Empresa: {form.company or '(não informado)'}",
        ]
    else:  # BR_SMB
        lines = [
            f"Tipo de negócio: {getattr(form, 'business_type', '?')}",
            f"Tamanho da equipe: {getattr(form, 'team_size', '?')}",
            f"Leads novos por mês: {getattr(form, 'leads_per_month', '?')}",
            f"Canal principal de captação: {getattr(form, 'main_channel', '?')}",
            f"Tempo médio até primeiro contato: {getattr(form, 'response_time', '?')}",
            f"Maior dor comercial hoje: {getattr(form, 'main_pain', '?')}",
            "",
            f"Nome: {form.name}",
            f"Empresa: {form.company or '(não informado)'}",
        ]
    return "\n".join(lines)


async def call_claude_diagnostic(form: "DiagnosticForm", funnel: str) -> dict:
    system_prompt = SYSTEM_PROMPTS.get(funnel, SYSTEM_PROMPT_BR_SMB)
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2000,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": build_diagnostic_user_message(form, funnel)},
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    text = data["content"][0]["text"].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    result = json.loads(text)
    # Backwards-compat: rename plano_30_dias -> plano if present
    if "plano_30_dias" in result and "plano" not in result:
        result["plano"] = result.pop("plano_30_dias")
    return result


# ----------------------------------------------------------------------------
# Supabase: insert lead
# ----------------------------------------------------------------------------


async def insert_lead(
    client: httpx.AsyncClient, form: DiagnosticForm, diagnostic: dict, funnel: str
) -> Optional[str]:
    """Insert lead tagged with the right funnel_id. Returns lead.id or None."""
    cfg = FUNNEL_CONFIG.get(funnel, FUNNEL_CONFIG["BR_SMB"])

    # Build the answers payload per funnel
    answers: dict = {}
    if funnel == "BR_ENG":
        for key in ("setor", "tamanho_empresa", "maturidade_ia",
                    "caso_uso", "bloqueio", "orcamento"):
            answers[key] = getattr(form, key, None)
    else:  # BR_SMB
        for key in ("business_type", "team_size", "leads_per_month",
                    "main_channel", "response_time", "main_pain"):
            answers[key] = getattr(form, key, None)

    qualification = {
        **answers,
        "diagnostic_score": diagnostic.get("score_maturidade"),
        "diagnostic_estimate": diagnostic.get("estimativa_perdida"),
        "diagnostic_summary": diagnostic.get("diagnostico_resumo"),
        "diagnostic_plan": diagnostic.get("plano") or diagnostic.get("plano_30_dias"),
        "diagnostic_pontos_fortes": diagnostic.get("pontos_fortes") or [],
        "diagnostic_pontos_fracos": diagnostic.get("pontos_fracos") or [],
        "diagnostic_proximo_passo": diagnostic.get("proximo_passo") or "",
    }

    payload = {
        "tenant_id": "anuvia",
        "funnel_id": funnel,
        "market": cfg["market"],
        "track": cfg["track"],
        "language": cfg["language"],
        "source": "lp_diagnostic",
        "source_detail": {
            "lp": cfg.get("lp_host_alias", "diagnostico") + ".anuvia.com.br",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        "name": form.name,
        "email": form.email,
        "phone_e164": form.whatsapp,
        "company": form.company,
        "current_stage": "new",
        "qualification_data": qualification,
        "consent": {
            "lp_diagnostic": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        },
        "tags": cfg["tags"],
    }
    try:
        r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=payload)
        if r.status_code >= 400:
            log.error("supabase insert failed: %s %s", r.status_code, r.text[:300])
            return None
        rows = r.json()
        if rows and isinstance(rows, list) and rows[0].get("id"):
            return rows[0]["id"]
    except Exception:
        log.exception("supabase insert exception")
    return None


async def fire_slack_notification(
    client: httpx.AsyncClient, form: DiagnosticForm, diagnostic: dict, lead_id: Optional[str]
) -> None:
    """Best-effort Slack notification to Mila about a new diagnostic lead."""
    if not SLACK_WEBHOOK:
        return
    score = diagnostic.get("score_maturidade", "?")
    estimate = diagnostic.get("estimativa_perdida", "?")
    text = (
        f"🆕 Novo diagnóstico LP BR_SMB\n"
        f"*{form.name}* — {form.company or '(sem empresa)'}\n"
        f"📞 {form.whatsapp}  ✉️ {form.email}\n"
        f"Tipo: {form.business_type} · Equipe: {form.team_size} · "
        f"Leads/mês: {form.leads_per_month}\n"
        f"Score maturidade: *{score}/100*\n"
        f"Estimativa perdida: {estimate}\n"
        f"Lead id: {lead_id or 'falhou ao inserir'}"
    )
    try:
        await client.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception:
        log.exception("slack notify failed (non-fatal)")


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------


def build_pre_call_brief(lead: dict) -> str:
    """Build a rich pre-call brief for the discovery appointment notes.

    Pulled from lead.qualification_data which the LP populates on submit.
    Format: plain text with section dividers — readable in Easyappointments
    admin calendar, the synced Google Calendar event description, and any
    email reminder template.
    """
    diag = lead.get("qualification_data") or {}
    name = lead.get("name") or "—"
    company = lead.get("company") or "—"
    phone = lead.get("phone_e164") or "—"
    email = lead.get("email") or "—"

    score = diag.get("diagnostic_score")
    estimativa = diag.get("diagnostic_estimate") or "—"
    summary = diag.get("diagnostic_summary") or ""

    pontos_fracos = diag.get("diagnostic_pontos_fracos") or []
    plano = diag.get("diagnostic_plan") or []

    parts: list[str] = []
    parts.append("PRE-CALL BRIEF — Diagnóstico LP")
    parts.append("=" * 36)
    parts.append("")
    parts.append("👤 CONTATO")
    parts.append(f"  Nome:    {name}")
    parts.append(f"  Empresa: {company}")
    parts.append(f"  WhatsApp: {phone}")
    parts.append(f"  Email:   {email}")
    parts.append("")

    parts.append("📋 RESPOSTAS DO FUNIL")
    parts.append(f"  Tipo de negócio:   {diag.get('business_type') or '—'}")
    parts.append(f"  Tamanho equipe:    {diag.get('team_size') or '—'}")
    parts.append(f"  Leads/mês:         {diag.get('leads_per_month') or '—'}")
    parts.append(f"  Canal principal:   {diag.get('main_channel') or '—'}")
    parts.append(f"  Tempo de resposta: {diag.get('response_time') or '—'}")
    parts.append(f"  Maior dor:         {diag.get('main_pain') or '—'}")
    parts.append("")

    if score is not None:
        parts.append(f"🎯 SCORE: {score}/100 maturidade comercial")
        parts.append("")

    if summary:
        parts.append("🔍 ANÁLISE")
        # Strip excessive newlines; wrap at ~78 chars for readability
        summary_clean = " ".join(summary.split())
        parts.append(_wrap(summary_clean, 78, indent="  "))
        parts.append("")

    parts.append("💸 OPORTUNIDADE PERDIDA (estimativa)")
    parts.append(_wrap(estimativa, 78, indent="  "))
    parts.append("")

    if pontos_fracos:
        parts.append("⚠️  PONTOS FRACOS IDENTIFICADOS")
        for p in pontos_fracos[:5]:
            parts.append(f"  • {p}")
        parts.append("")

    if plano:
        parts.append("📅 PLANO DE 30 DIAS (sugerido pela IA)")
        for item in plano:
            sem = item.get("semana", "?")
            acao = item.get("acao", "")
            porque = item.get("porque", "")
            parts.append(f"  Semana {sem}: {acao}")
            if porque:
                parts.append(_wrap(porque, 74, indent="    ↳ "))
        parts.append("")

    parts.append("—")
    parts.append("Gerado automaticamente pela LP diagnostico.anuvia.com.br")

    return "\n".join(parts)


def _wrap(text: str, width: int, indent: str = "") -> str:
    """Simple word-wrap with optional per-line indent. Returns joined string."""
    words = text.split()
    lines: list[str] = []
    cur = indent
    for w in words:
        if len(cur) + len(w) + 1 > width and len(cur) > len(indent):
            lines.append(cur.rstrip())
            cur = indent + w
        else:
            cur += (" " if cur != indent else "") + w
    if cur.strip():
        lines.append(cur.rstrip())
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Blog (markdown-static at blog.anuvia.com.br)
# ----------------------------------------------------------------------------

POSTS_DIR = os.environ.get("BLOG_POSTS_DIR", "posts")
BLOG_BASE_URL = os.environ.get("BLOG_BASE_URL", "https://blog.anuvia.com.br")


def _make_excerpt(content: str, max_chars: int = 220) -> str:
    """Strip markdown markers and return a short excerpt."""
    text = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    text = re.sub(r"[#*`>_\[\]()~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _post_meta(slug: str, post) -> dict:
    """Normalize frontmatter into a dict the templates can rely on."""
    meta = dict(post.metadata or {})
    meta["slug"] = meta.get("slug", slug)
    raw_date = meta.get("date")
    if hasattr(raw_date, "isoformat"):
        meta["date_iso"] = raw_date.isoformat()
        meta["date_display"] = raw_date.strftime("%d %b %Y") if hasattr(raw_date, "strftime") else str(raw_date)
        meta["date_sort"] = raw_date.isoformat()
    else:
        meta["date_iso"] = str(raw_date or "")
        meta["date_display"] = str(raw_date or "")
        meta["date_sort"] = str(raw_date or "")
    meta["excerpt"] = meta.get("excerpt") or _make_excerpt(post.content)
    meta["title"] = meta.get("title") or slug.replace("-", " ").title()
    meta["author"] = meta.get("author") or "Mila Vernazza"
    meta["tags"] = meta.get("tags") or []
    meta["cover_image"] = meta.get("cover_image") or meta.get("cover") or ""
    return meta


def _list_posts() -> list[dict]:
    """List all posts with metadata, sorted by date desc."""
    posts: list[dict] = []
    if not os.path.isdir(POSTS_DIR):
        return posts
    for filename in os.listdir(POSTS_DIR):
        if not filename.endswith(".md"):
            continue
        slug = filename[:-3]
        path = os.path.join(POSTS_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                post = frontmatter.load(f)
            if (post.metadata or {}).get("draft") is True:
                continue
            posts.append(_post_meta(slug, post))
        except Exception:
            log.exception("failed to load post %s", path)
    posts.sort(key=lambda p: p.get("date_sort") or "", reverse=True)
    return posts


def _load_post(slug: str) -> Optional[dict]:
    """Load a single post by slug with rendered HTML body."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,200}", slug or ""):
        return None
    path = os.path.join(POSTS_DIR, f"{slug}.md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)
    except Exception:
        log.exception("failed to load post %s", path)
        return None
    if (post.metadata or {}).get("draft") is True:
        return None
    meta = _post_meta(slug, post)
    meta["body_html"] = md_lib.markdown(
        post.content,
        extensions=["fenced_code", "tables", "smarty"],
    )
    return meta


def _is_blog_host(request: Request) -> bool:
    host = (request.headers.get("host") or "").lower().split(":")[0]
    return host.startswith("blog.")


@app.get("/blog", response_class=HTMLResponse)
@app.get("/blog/", response_class=HTMLResponse)
async def blog_index(request: Request) -> HTMLResponse:
    posts = _list_posts()
    return templates.TemplateResponse(
        "blog_index.html",
        {
            "request": request,
            "posts": posts,
            "blog_base_url": BLOG_BASE_URL,
        },
    )


@app.get("/blog/feed.xml")
async def blog_feed(request: Request) -> Response:
    posts = _list_posts()[:20]
    items_xml = []
    for p in posts:
        url = f"{BLOG_BASE_URL}/blog/{p['slug']}"
        items_xml.append(
            f"""    <item>
      <title><![CDATA[{p.get('title','')}]]></title>
      <link>{url}</link>
      <guid isPermaLink="true">{url}</guid>
      <pubDate>{p.get('date_iso','')}</pubDate>
      <description><![CDATA[{p.get('excerpt','')}]]></description>
    </item>"""
        )
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Anuvia — Blog</title>
    <link>{BLOG_BASE_URL}/blog</link>
    <description>IA aplicada a vendas e ops. Insights e estudos de caso da Anuvia.</description>
    <language>pt-BR</language>
{chr(10).join(items_xml)}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post(request: Request, slug: str) -> HTMLResponse:
    post = _load_post(slug)
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")
    return templates.TemplateResponse(
        "blog_post.html",
        {
            "request": request,
            "post": post,
            "blog_base_url": BLOG_BASE_URL,
        },
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ----------------------------------------------------------------------------
# Booking — Easyappointments embedded picker
# ----------------------------------------------------------------------------


def working_days(start_date: datetime, count: int) -> list[datetime]:
    """Return next `count` working days (Mon-Fri) in São Paulo TZ, starting at start_date."""
    days = []
    cur = start_date
    while len(days) < count:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            days.append(cur)
        cur = cur + timedelta(days=1)
    return days


def _coarse_slots(slots: list[str], step_min: int = 30, max_per_day: int = 8) -> list[str]:
    """Easyappointments returns 15-min granularity by default. Snap to half-hours
    and cap to `max_per_day` (spread evenly across morning/afternoon)."""
    if not slots:
        return []
    half = [s for s in slots if s.endswith(":00") or s.endswith(":30")]
    if not half:
        half = slots[: max_per_day]
    if len(half) <= max_per_day:
        return half
    # Evenly sample across the day
    step = len(half) / max_per_day
    return [half[int(i * step)] for i in range(max_per_day)]


# ============================================================================
# Multi-account Google Calendar freebusy
# Lets Mila add N Google accounts; their busy ranges block booking slots.
# ============================================================================

import time as _time
import secrets as _secrets
from urllib.parse import urlencode as _urlencode


def _admin_auth(request: Request) -> None:
    """Raise 401 if admin key is missing or wrong. Accepts ?key= or Bearer header."""
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    key = request.query_params.get("key") or ""
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _fetch_active_gcal_accounts(client: httpx.AsyncClient) -> list[dict]:
    """Get all active Google accounts from Supabase."""
    try:
        r = await client.get(
            f"{SUPA_URL}/admin_gcal_accounts?is_active=eq.true&select=id,email,refresh_token,calendar_id",
            headers=SUPA_HEADERS,
        )
        if r.status_code == 200:
            return r.json() or []
        log.warning("fetch_gcal_accounts non-200: %s", r.status_code)
    except Exception:
        log.exception("fetch_gcal_accounts failed")
    return []


async def _exchange_refresh_token(client: httpx.AsyncClient, refresh_token: str) -> Optional[str]:
    """Exchange a refresh_token for a fresh access_token via Google OAuth."""
    try:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
        log.warning("refresh_token exchange non-200: %s %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("refresh_token exchange failed")
    return None


async def _get_cached_access_token(
    client: httpx.AsyncClient, email: str, refresh_token: str
) -> Optional[str]:
    """Return cached access token if still valid (5 min buffer), else refresh."""
    now = _time.time()
    cached = _GCAL_TOKEN_CACHE.get(email)
    if cached and cached[1] - now > 300:
        return cached[0]
    token = await _exchange_refresh_token(client, refresh_token)
    if token:
        # Access tokens are valid 1h; cache for 55 min
        _GCAL_TOKEN_CACHE[email] = (token, now + 55 * 60)
    return token


def _is_holiday_calendar(cal_id: str, summary: str = "") -> bool:
    """Exclude public holiday calendars from busy aggregation (they shouldn't block slots)."""
    if "#holiday@group.v.calendar.google.com" in cal_id:
        return True
    s = (summary or "").lower()
    return any(kw in s for kw in ("feriado", "festivo", "holiday", "feiertag", "festa"))


async def _list_user_calendars(
    client: httpx.AsyncClient, access_token: str
) -> list[dict]:
    """List all calendars the user has access to with their accessRole.
    Returns list of dicts: {id, summary, accessRole, primary}.
    Filtered: must be selected (visible in UI), not deleted, not a holiday calendar."""
    try:
        r = await client.get(
            "https://www.googleapis.com/calendar/v3/users/me/calendarList",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"maxResults": "100", "minAccessRole": "freeBusyReader"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("calendarList non-200: %s %s", r.status_code, r.text[:200])
            return [{"id": "primary", "accessRole": "owner", "summary": "primary"}]
        items = r.json().get("items", []) or []
        out = []
        for c in items:
            cid = c.get("id")
            if not cid or c.get("deleted"):
                continue
            if c.get("selected") is False:
                continue
            if _is_holiday_calendar(cid, c.get("summary", "")):
                continue
            out.append({
                "id": cid,
                "summary": c.get("summary") or c.get("summaryOverride") or "",
                "accessRole": c.get("accessRole") or "reader",
                "primary": c.get("primary", False),
            })
        return out or [{"id": "primary", "accessRole": "owner", "summary": "primary"}]
    except Exception:
        log.exception("calendarList query failed")
        return [{"id": "primary", "accessRole": "owner", "summary": "primary"}]


async def _list_events_for_calendar(
    client: httpx.AsyncClient, access_token: str, cal_id: str,
    time_min_iso: str, time_max_iso: str,
) -> list[tuple[datetime, datetime]]:
    """Call events.list — captures ALL events regardless of transparency.
    Skips cancelled, all-day, and out-of-office reflective events that shouldn't block."""
    try:
        import urllib.parse as _up
        r = await client.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{_up.quote(cal_id)}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "singleEvents": "true",  # expand recurring
                "orderBy": "startTime",
                "timeZone": "America/Sao_Paulo",
                "maxResults": "250",
                "showDeleted": "false",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        items = (r.json() or {}).get("items", []) or []
        out = []
        for ev in items:
            if ev.get("status") == "cancelled":
                continue
            # Skip events the user has declined
            attendees = ev.get("attendees") or []
            self_resp = next((a.get("responseStatus") for a in attendees if a.get("self")), None)
            if self_resp == "declined":
                continue
            start_obj = ev.get("start") or {}
            end_obj = ev.get("end") or {}
            start_dt = start_obj.get("dateTime")
            end_dt = end_obj.get("dateTime")
            if not start_dt or not end_dt:
                continue  # all-day events — don't block slots
            try:
                s = datetime.fromisoformat(start_dt.replace("Z", "+00:00")).astimezone(TZ_SP)
                e = datetime.fromisoformat(end_dt.replace("Z", "+00:00")).astimezone(TZ_SP)
                out.append((s, e))
            except ValueError:
                continue
        return out
    except Exception:
        log.exception("events.list failed for %s", cal_id)
        return []


async def _query_freebusy_via_events(
    client: httpx.AsyncClient,
    access_token: str,
    cal_id: str,
    time_min_iso: str,
    time_max_iso: str,
) -> list[tuple[datetime, datetime]]:
    """freeBusy fallback for freeBusyReader-only calendars (events.list requires reader+)."""
    try:
        r = await client.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "items": [{"id": cal_id}],
                "timeZone": "America/Sao_Paulo",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        cal_data = (data.get("calendars") or {}).get(cal_id, {})
        out = []
        for b in (cal_data.get("busy") or []):
            try:
                s = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ_SP)
                e = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ_SP)
                out.append((s, e))
            except (KeyError, ValueError):
                continue
        return out
    except Exception:
        log.exception("freeBusy fallback failed for %s", cal_id)
        return []


async def _query_freebusy(
    client: httpx.AsyncClient,
    access_token: str,
    _calendar_id_unused: str,
    time_min_iso: str,
    time_max_iso: str,
) -> list[tuple[datetime, datetime]]:
    """Hybrid busy-range fetcher across ALL user's calendars:
    - reader+ access → events.list (captures transparency=transparent events too)
    - freeBusyReader only → freeBusy (only busy events visible to us)
    Excludes holiday calendars + declined events + all-day events."""
    cals = await _list_user_calendars(client, access_token)
    intervals: list[tuple[datetime, datetime]] = []

    async def _one(cal: dict) -> list[tuple[datetime, datetime]]:
        role = cal.get("accessRole") or "freeBusyReader"
        if role in ("owner", "writer", "reader"):
            return await _list_events_for_calendar(
                client, access_token, cal["id"], time_min_iso, time_max_iso
            )
        return await _query_freebusy_via_events(
            client, access_token, cal["id"], time_min_iso, time_max_iso
        )

    results = await asyncio.gather(*(_one(c) for c in cals), return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            intervals.extend(r)
    return intervals


async def fetch_all_busy_ranges(
    client: httpx.AsyncClient, time_min: datetime, time_max: datetime
) -> list[tuple[datetime, datetime]]:
    """Aggregate busy ranges from all active Google accounts."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return []
    accounts = await _fetch_active_gcal_accounts(client)
    if not accounts:
        return []
    time_min_iso = time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    time_max_iso = time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    async def _one(acc):
        token = await _get_cached_access_token(client, acc["email"], acc["refresh_token"])
        if not token:
            return []
        return await _query_freebusy(
            client, token, acc.get("calendar_id") or "primary", time_min_iso, time_max_iso
        )

    all_results = await asyncio.gather(*(_one(a) for a in accounts), return_exceptions=True)
    merged: list[tuple[datetime, datetime]] = []
    for res in all_results:
        if isinstance(res, list):
            merged.extend(res)
    return merged


def _slot_conflicts_with_busy(
    slot_start: datetime, slot_duration_min: int, busy_ranges: list[tuple[datetime, datetime]]
) -> bool:
    """True if a slot [start, start+duration) overlaps any busy interval."""
    slot_end = slot_start + timedelta(minutes=slot_duration_min)
    for b_start, b_end in busy_ranges:
        # Overlap iff slot_start < b_end and b_start < slot_end
        if slot_start < b_end and b_start < slot_end:
            return True
    return False


# ---------------------- Admin OAuth flow endpoints ----------------------

@app.get("/api/admin/gcal/connect")
async def admin_gcal_connect(request: Request, email: str):
    """Start OAuth flow. Mila visits this URL once per Google account she wants tracked.
    Example: /api/admin/gcal/connect?key=XXX&email=milavernazza@gmail.com"""
    _admin_auth(request)
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GOOGLE_CLIENT_ID not set in env")
    # Generate a state token that encodes email + nonce (signed by ADMIN_API_KEY)
    nonce = _secrets.token_urlsafe(16)
    state_raw = f"{email}|{nonce}"
    state_sig = _secrets.token_urlsafe(8)  # Simple — relies on ADMIN_API_KEY auth on callback
    state = f"{state_raw}|{state_sig}"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GCAL_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar.readonly",
        "access_type": "offline",
        "prompt": "consent",  # Force refresh_token return even if previously consented
        "login_hint": email,
        "state": state,
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + _urlencode(params)
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/admin/gcal/callback")
async def admin_gcal_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """OAuth callback. Exchanges code for refresh_token, persists in Supabase."""
    if error:
        return HTMLResponse(
            f"<h1>OAuth error</h1><pre>{error}</pre><p>Tente novamente.</p>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    # Parse state: email|nonce|sig
    parts = state.split("|")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid state")
    email = parts[0]

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            tr = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": GCAL_REDIRECT_URI,
                },
            )
            if tr.status_code != 200:
                log.error("code exchange failed: %s %s", tr.status_code, tr.text[:300])
                return HTMLResponse(
                    f"<h1>Token exchange failed</h1><pre>{tr.text[:300]}</pre>",
                    status_code=500,
                )
            tok = tr.json()
            refresh_token = tok.get("refresh_token")
            if not refresh_token:
                return HTMLResponse(
                    "<h1>Sem refresh_token</h1><p>Google não retornou refresh_token. "
                    "Vá em <a href='https://myaccount.google.com/permissions'>myaccount.google.com/permissions</a>, "
                    "revogue o acesso da app Anuvia e tente de novo.</p>",
                    status_code=400,
                )

            # Upsert: if email exists, update refresh_token; else insert
            payload = {
                "email": email,
                "refresh_token": refresh_token,
                "calendar_id": "primary",
                "is_active": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            ur = await client.post(
                f"{SUPA_URL}/admin_gcal_accounts?on_conflict=email",
                headers={**SUPA_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
                json=payload,
            )
            if ur.status_code not in (200, 201):
                log.error("gcal_account upsert failed: %s %s", ur.status_code, ur.text[:300])
                return HTMLResponse(
                    f"<h1>Falha ao salvar</h1><pre>{ur.text[:300]}</pre>",
                    status_code=500,
                )
            # Invalidate token cache so first /api/slots call refreshes
            _GCAL_TOKEN_CACHE.pop(email, None)
        except HTTPException:
            raise
        except Exception:
            log.exception("oauth callback failed")
            return HTMLResponse("<h1>Erro interno</h1>", status_code=500)

    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Anuvia · Calendar connected</title>
<style>body{{font-family:Inter,sans-serif;background:#fafaf9;padding:48px 24px;color:#1a1a1a;}}
.card{{max-width:520px;margin:0 auto;background:#fff;border:1px solid #e7e5e4;padding:32px;}}
.h-serif{{font-family:Georgia,serif;font-size:28px;margin:0 0 16px 0;}}
.eyebrow{{font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#0c4a6e;margin:0 0 8px 0;}}
.btn{{display:inline-block;background:#1a1a1a;color:#fafaf9;padding:10px 18px;text-decoration:none;margin-top:16px;}}</style></head>
<body><div class="card">
<p class="eyebrow">Anuvia Admin</p>
<p class="h-serif">Calendário conectado.</p>
<p><strong>{email}</strong> foi adicionado à lista de calendários que bloqueiam slots no widget de booking.</p>
<p style="color:#78716c;font-size:14px;">Eventos nessa conta agora aparecem como busy automaticamente em <code>/api/slots</code>.</p>
<a class="btn" href="/api/admin/gcal/accounts?key={request.query_params.get('key', '')}">Ver lista de contas</a>
</div></body></html>"""
    )


@app.get("/api/admin/gcal/debug-busy")
async def admin_gcal_debug_busy(request: Request, date: str):
    """Debug: dump raw busy ranges from all accounts for a given date (YYYY-MM-DD).
    Usage: /api/admin/gcal/debug-busy?key=XXX&date=2026-05-13"""
    _admin_auth(request)
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    time_min = datetime.combine(day, datetime.min.time(), tzinfo=TZ_SP)
    time_max = datetime.combine(day, datetime.max.time(), tzinfo=TZ_SP)
    time_min_iso = time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    time_max_iso = time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    out = {"date": date, "time_min_iso": time_min_iso, "time_max_iso": time_max_iso, "accounts": []}
    async with httpx.AsyncClient(timeout=15) as client:
        accounts = await _fetch_active_gcal_accounts(client)
        for acc in accounts:
            email = acc["email"]
            cal_id = acc.get("calendar_id") or "primary"
            token = await _get_cached_access_token(client, email, acc["refresh_token"])
            entry = {"email": email, "got_access_token": bool(token)}
            if not token:
                out["accounts"].append(entry)
                continue

            # First: list ALL calendars the user has access to
            try:
                cl = await client.get(
                    "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"maxResults": "100", "minAccessRole": "freeBusyReader"},
                    timeout=10,
                )
                entry["calendarList_status"] = cl.status_code
                if cl.status_code == 200:
                    items = cl.json().get("items", [])
                    entry["calendarList"] = [
                        {
                            "id": c.get("id"),
                            "summary": c.get("summary") or c.get("summaryOverride"),
                            "primary": c.get("primary", False),
                            "selected": c.get("selected"),
                            "accessRole": c.get("accessRole"),
                        } for c in items
                    ]
            except Exception as e:
                entry["calendarList_exception"] = str(e)

            cal_ids = [c["id"] for c in entry.get("calendarList", []) if c.get("id") and c.get("selected") is not False] or ["primary"]
            entry["queried_calendar_ids"] = cal_ids
            # Multi-cal freebusy
            try:
                r = await client.post(
                    "https://www.googleapis.com/calendar/v3/freeBusy",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"timeMin": time_min_iso, "timeMax": time_max_iso, "items": [{"id": cid} for cid in cal_ids], "timeZone": "America/Sao_Paulo"},
                    timeout=10,
                )
                entry["freebusy_status"] = r.status_code
                if r.status_code == 200:
                    entry["freebusy_response"] = r.json()
                else:
                    entry["freebusy_error"] = r.text[:300]
            except Exception as e:
                entry["freebusy_exception"] = str(e)
            # events.list across ALL calendars (production path)
            entry["events_per_calendar"] = {}
            import urllib.parse as _up2
            for cid in cal_ids:
                try:
                    er = await client.get(
                        f"https://www.googleapis.com/calendar/v3/calendars/{_up2.quote(cid)}/events",
                        headers={"Authorization": f"Bearer {token}"},
                        params={
                            "timeMin": time_min_iso, "timeMax": time_max_iso,
                            "singleEvents": "true", "orderBy": "startTime",
                            "timeZone": "America/Sao_Paulo", "maxResults": "100",
                            "showDeleted": "false",
                        }, timeout=10,
                    )
                    if er.status_code == 200:
                        evs = (er.json() or {}).get("items", [])
                        entry["events_per_calendar"][cid] = [
                            {
                                "summary": e.get("summary", "(no title)"),
                                "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                                "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
                                "transparency": e.get("transparency", "opaque"),
                            }
                            for e in evs
                        ]
                    else:
                        entry["events_per_calendar"][cid] = {"error": er.status_code, "msg": er.text[:200]}
                except Exception as e:
                    entry["events_per_calendar"][cid] = {"exception": str(e)}
            # Also list events directly (with transparency info) to debug transparency issue
            try:
                er = await client.get(
                    f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "timeMin": time_min_iso,
                        "timeMax": time_max_iso,
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "timeZone": "America/Sao_Paulo",
                        "maxResults": "50",
                    },
                    timeout=10,
                )
                entry["events_status"] = er.status_code
                if er.status_code == 200:
                    evs = er.json().get("items", [])
                    entry["events"] = [
                        {
                            "summary": e.get("summary", "(no title)"),
                            "start": e.get("start"),
                            "end": e.get("end"),
                            "transparency": e.get("transparency", "opaque"),
                            "status": e.get("status"),
                            "visibility": e.get("visibility"),
                            "event_type": e.get("eventType"),
                        }
                        for e in evs
                    ]
                else:
                    entry["events_error"] = er.text[:300]
            except Exception as e:
                entry["events_exception"] = str(e)
            out["accounts"].append(entry)
    return JSONResponse(out)


@app.get("/api/admin/gcal/accounts")
async def admin_gcal_list(request: Request):
    """List connected Google accounts (admin only)."""
    _admin_auth(request)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SUPA_URL}/admin_gcal_accounts?select=id,email,calendar_id,is_active,created_at,updated_at,notes&order=created_at.desc",
            headers=SUPA_HEADERS,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to list accounts")
        return JSONResponse({"accounts": r.json()})


@app.delete("/api/admin/gcal/accounts/{account_id}")
async def admin_gcal_delete(account_id: str, request: Request):
    """Soft-delete (deactivate) a Google account from freebusy aggregation."""
    _admin_auth(request)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.patch(
            f"{SUPA_URL}/admin_gcal_accounts?id=eq.{account_id}",
            headers=SUPA_HEADERS,
            json={"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail="Failed to deactivate")
    return JSONResponse({"ok": True})


@app.get("/api/slots")
async def api_slots(request: Request, days: int = 5) -> JSONResponse:
    """Return available slots for the next `days` working days using
    Easyappointments' public booking endpoint (no auth). Funnel detected
    from Host header to pick the right service_id (BR_SMB=2, BR_ENG=3)."""
    funnel = detect_funnel(request)
    cfg = FUNNEL_CONFIG[funnel]
    service_id = cfg["easy_service_id"]
    days = max(1, min(days, 10))

    today_sp = datetime.now(TZ_SP).date()
    tomorrow_sp = today_sp + timedelta(days=1)
    start_dt = datetime.combine(tomorrow_sp, datetime.min.time(), tzinfo=TZ_SP)
    targets = working_days(start_dt, days)

    pt_weekdays = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    pt_months = [
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    ]

    out = []
    duration_min = cfg.get("easy_duration_min", 30)
    async with httpx.AsyncClient(timeout=20) as client:
        async def fetch(d: datetime) -> list[str]:
            ymd = d.strftime("%Y-%m-%d")
            try:
                r = await client.post(
                    f"{EASY_BASE}/index.php/booking/get_available_hours",
                    headers=EASY_FORM_HEADERS,
                    data={
                        "service_id": service_id,
                        "provider_id": EASY_PROVIDER_ID,
                        "selected_date": ymd,
                        "manage_mode": "false",
                        "csrfToken": "",
                    },
                )
                r.raise_for_status()
                slots = r.json()
                return slots if isinstance(slots, list) else []
            except Exception:
                log.exception("get_available_hours failed for %s", ymd)
                return []

        # Query Easyappointments AND Google freeBusy in parallel
        easy_task = asyncio.gather(*(fetch(d) for d in targets))
        # freebusy spans the full window (first target start → last target end-of-day)
        time_min = datetime.combine(targets[0].date(), datetime.min.time(), tzinfo=TZ_SP)
        time_max = datetime.combine(targets[-1].date(), datetime.max.time(), tzinfo=TZ_SP)
        busy_task = fetch_all_busy_ranges(client, time_min, time_max)
        all_slots, busy_ranges = await asyncio.gather(easy_task, busy_task)

        # Determine market for holiday filter — from locale
        market = get_locale(request).get("market", "BR")
        for d, raw_slots in zip(targets, all_slots):
            date_str = d.strftime("%Y-%m-%d")
            # Public holiday: skip the whole day
            if is_public_holiday(date_str, market):
                continue
            slots = _coarse_slots(raw_slots, step_min=30, max_per_day=8)
            # Filter slots that conflict with Google Calendar busy ranges
            if busy_ranges:
                filtered = []
                for t in slots:
                    try:
                        hh, mm = t.split(":")
                        slot_start = d.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                        if not _slot_conflicts_with_busy(slot_start, duration_min, busy_ranges):
                            filtered.append(t)
                    except (ValueError, AttributeError):
                        filtered.append(t)
                slots = filtered
            if not slots:
                continue  # don't show days with zero availability after filtering
            label = f"{pt_weekdays[d.weekday()]}, {d.day} {pt_months[d.month - 1]}"
            out.append({"date": date_str, "label": label, "slots": slots})

    return JSONResponse({"days": out})


class BookingRequest(BaseModel):
    lead_id: str = Field(..., min_length=10, max_length=64)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


@app.post("/api/book")
async def api_book(payload: BookingRequest, request: Request) -> JSONResponse:
    """Book a discovery using the lead's existing data (no re-asking).
    Uses Easyappointments public booking endpoint (no auth required).
    Funnel detected from Host header to pick the right service_id."""
    funnel = detect_funnel(request)
    cfg = FUNNEL_CONFIG[funnel]
    service_id = cfg["easy_service_id"]
    duration_min = cfg["easy_duration_min"]
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Fetch lead
        r = await client.get(
            f"{SUPA_URL}/leads?id=eq.{payload.lead_id}&limit=1",
            headers=SUPA_HEADERS,
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail="Falha ao buscar lead")
        rows = r.json()
        if not rows:
            raise HTTPException(status_code=404, detail="Lead não encontrado")
        lead = rows[0]

        # 2. Build appointment times in SP local (Easyappointments wants local)
        start_dt = datetime.strptime(
            f"{payload.date} {payload.time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TZ_SP)
        end_dt = start_dt + timedelta(minutes=duration_min)
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Easyappointments requires firstName + lastName non-empty.
        name_parts = (lead.get("name") or "Lead").strip().split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else "."

        notes = build_pre_call_brief(lead)

        # 3. Submit to Easyappointments public booking endpoint.
        # Uses nested form fields: post_data[appointment][...], post_data[customer][...]
        form = {
            "post_data[appointment][start_datetime]": start_str,
            "post_data[appointment][end_datetime]": end_str,
            "post_data[appointment][id_users_provider]": str(EASY_PROVIDER_ID),
            "post_data[appointment][id_services]": str(service_id),
            "post_data[appointment][notes]": notes,
            "post_data[appointment][is_unavailability]": "false",
            "post_data[customer][first_name]": first_name,
            "post_data[customer][last_name]": last_name,
            "post_data[customer][email]": lead.get("email") or "",
            "post_data[customer][phone_number]": lead.get("phone_e164") or "",
            "post_data[customer][timezone]": "America/Sao_Paulo",
            "post_data[manage_mode]": "false",
            "csrfToken": "",
        }
        try:
            br = await client.post(
                f"{EASY_BASE}/index.php/booking/register",
                headers=EASY_FORM_HEADERS,
                data=form,
            )
            body_text = br.text[:400]
            if br.status_code >= 400:
                log.error("easy register failed: %s %s", br.status_code, body_text)
                raise HTTPException(
                    status_code=502, detail="Falha ao agendar no calendário."
                )
            booking = br.json()
            if not isinstance(booking, dict) or "appointment_id" not in booking:
                log.error("easy register unexpected response: %s", body_text)
                raise HTTPException(
                    status_code=502, detail="Resposta do calendário inválida."
                )
        except HTTPException:
            raise
        except Exception:
            log.exception("easy register exception")
            raise HTTPException(status_code=502, detail="Erro ao agendar.")

        # 4. Update lead stage to meeting_booked
        try:
            await client.patch(
                f"{SUPA_URL}/leads?id=eq.{payload.lead_id}",
                headers=SUPA_HEADERS,
                json={"current_stage": "meeting_booked"},
            )
        except Exception:
            log.exception("lead stage update failed (non-fatal)")

        # 5. Slack notification (best-effort)
        if SLACK_WEBHOOK:
            try:
                await client.post(
                    SLACK_WEBHOOK,
                    json={
                        "text": (
                            f":calendar: Discovery agendada via LP\n"
                            f"*{lead.get('name')}* — {lead.get('company') or '(sem empresa)'}\n"
                            f"📅 {payload.date} {payload.time} (SP)\n"
                            f"📞 {lead.get('phone_e164')}  ✉️ {lead.get('email')}\n"
                            f"Easyappointments id: {booking.get('appointment_id')}"
                        )
                    },
                    timeout=10,
                )
            except Exception:
                log.exception("slack notify (book) failed (non-fatal)")

    # Format friendly confirmation
    pt_weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    pt_months = [
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    ]
    weekday_pt = pt_weekdays[start_dt.weekday()]
    month_pt = pt_months[start_dt.month - 1]
    pretty = f"{weekday_pt}, {start_dt.day} de {month_pt} às {payload.time}"

    return JSONResponse({
        "ok": True,
        "appointment_id": booking.get("appointment_id"),
        "appointment_hash": booking.get("appointment_hash"),
        "pretty": pretty,
        "iso": start_str,
    })


class ContactBookForm(BaseModel):
    """Form do widget de booking embedded na /contact (e outras práticas).
    Captura lead + agenda discovery em um único POST."""
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: Optional[str] = Field(default="", max_length=200)
    context: Optional[str] = Field(default="", max_length=2000)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    source: Optional[str] = "lp_brand"
    offering: Optional[str] = Field(default=None, max_length=80)
    practice: Optional[str] = Field(default=None, max_length=40)

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


@app.post("/api/contact-book")
async def api_contact_book(form: ContactBookForm, request: Request) -> JSONResponse:
    """Cria lead em Supabase + agenda discovery em Easyappointments.
    Usado pelo widget de booking embedded no site brand (sem funnel-specific LP)."""
    funnel = "BR_BRAND"
    service_id = int(os.environ.get("EASYAPPOINTMENTS_SERVICE_ID_BR_SMB", "2"))
    duration_min = 30

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Insert lead
        qd = {
            "context": form.context or "",
            "booking_via": "contact_widget",
        }
        if form.offering:
            qd["offering"] = form.offering
        if form.practice:
            qd["practice"] = form.practice
        tags = ["lp_brand", "contact_widget"]
        if form.practice:
            tags.append(f"practice:{form.practice}")
        if form.offering:
            tags.append(f"offering:{form.offering}")
        lead_payload = {
            "tenant_id": "anuvia",
            "funnel_id": funnel,
            "market": "BR",
            "track": "brand_contact",
            "language": "pt-BR",
            "name": form.name,
            "email": form.email,
            "phone_e164": form.whatsapp,
            "company": form.company or None,
            "source": form.source or "lp_brand",
            "source_detail": {
                "lp": "anuvia.com.br/contact",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
            "qualification_data": qd,
            "consent": {
                "lp_contact_widget": True,
                "granted_at": datetime.now(timezone.utc).isoformat(),
            },
            "tags": tags,
            "current_stage": "qualified",
        }
        lead_id = None
        try:
            r = await client.post(
                f"{SUPA_URL}/leads",
                headers=SUPA_HEADERS,
                json=lead_payload,
            )
            if r.status_code in (200, 201):
                rows = r.json()
                if rows and isinstance(rows, list):
                    lead_id = rows[0].get("id")
            else:
                log.warning("contact_book lead_insert non-200: %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("contact_book lead_insert failed")

        # 2. Build appointment in SP local time
        try:
            start_dt = datetime.strptime(
                f"{form.date} {form.time}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=TZ_SP)
        except ValueError:
            raise HTTPException(status_code=400, detail="Data ou hora inválidas.")
        end_dt = start_dt + timedelta(minutes=duration_min)
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Easyappointments requires firstName + lastName non-empty
        name_parts = form.name.strip().split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else "."

        # Build brief from form context for the appointment notes
        notes_lines = [
            f"Lead via /contact (brand site)",
            f"Empresa: {form.company or '(não informada)'}",
            f"Email: {form.email}",
            f"WhatsApp: {form.whatsapp}",
        ]
        if form.offering or form.practice:
            notes_lines.append("")
            notes_lines.append(f"Oferta de interesse: {form.offering or '(n/a)'} (prática: {form.practice or '(n/a)'})")
        if form.context:
            notes_lines.append("")
            notes_lines.append("Contexto compartilhado:")
            notes_lines.append(form.context)
        if lead_id:
            notes_lines.append("")
            notes_lines.append(f"Supabase lead_id: {lead_id}")
        notes = "\n".join(notes_lines)

        # 3. Book in Easyappointments
        booking_form = {
            "post_data[appointment][start_datetime]": start_str,
            "post_data[appointment][end_datetime]": end_str,
            "post_data[appointment][id_users_provider]": str(EASY_PROVIDER_ID),
            "post_data[appointment][id_services]": str(service_id),
            "post_data[appointment][notes]": notes,
            "post_data[appointment][is_unavailability]": "false",
            "post_data[customer][first_name]": first_name,
            "post_data[customer][last_name]": last_name,
            "post_data[customer][email]": form.email,
            "post_data[customer][phone_number]": form.whatsapp,
            "post_data[customer][timezone]": "America/Sao_Paulo",
            "post_data[manage_mode]": "false",
            "csrfToken": "",
        }
        try:
            br = await client.post(
                f"{EASY_BASE}/index.php/booking/register",
                headers=EASY_FORM_HEADERS,
                data=booking_form,
            )
            body_text = br.text[:400]
            if br.status_code >= 400:
                log.error("contact_book easy register failed: %s %s", br.status_code, body_text)
                raise HTTPException(status_code=502, detail="Não foi possível confirmar o agendamento. Tente outro horário.")
            booking = br.json()
            if not isinstance(booking, dict) or "appointment_id" not in booking:
                log.error("contact_book easy register unexpected: %s", body_text)
                raise HTTPException(status_code=502, detail="Resposta do calendário inválida.")
        except HTTPException:
            raise
        except Exception:
            log.exception("contact_book easy register exception")
            raise HTTPException(status_code=502, detail="Erro ao agendar.")

        # 4. Update lead stage to discovery_scheduled (best-effort)
        if lead_id:
            try:
                await client.patch(
                    f"{SUPA_URL}/leads?id=eq.{lead_id}",
                    headers=SUPA_HEADERS,
                    json={"current_stage": "discovery_scheduled"},
                )
            except Exception:
                log.exception("contact_book lead stage update failed")

        # 5. Slack notification (best-effort)
        if SLACK_WEBHOOK:
            try:
                await client.post(
                    SLACK_WEBHOOK,
                    json={
                        "text": (
                            f":calendar: Discovery agendada via brand site\n"
                            f"*{form.name}* — {form.company or '(sem empresa)'}\n"
                            f"📅 {form.date} {form.time} (SP)\n"
                            f"📞 {form.whatsapp}  ✉️ {form.email}\n"
                            f"Oferta: {form.offering or '(n/a)'} · Prática: {form.practice or '(n/a)'}\n"
                            f"Source: {form.source}\n"
                            f"Easyappointments id: {booking.get('appointment_id')}"
                        )
                    },
                    timeout=10,
                )
            except Exception:
                log.exception("contact_book slack notify failed (non-fatal)")

        # 6. Send confirmation email (best-effort)
        if RESEND_API_KEY:
            try:
                pt_weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
                pt_months = [
                    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
                    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
                ]
                pretty = f"{pt_weekdays[start_dt.weekday()]}, {start_dt.day} de {pt_months[start_dt.month - 1]} às {form.time}"
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Agendamento confirmado</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {first_name}</p><p style="color:#475569;line-height:1.65;">Sua conversa com um Solutions Architect da Anuvia está confirmada para <strong>{pretty}</strong> (horário de São Paulo).</p><p style="color:#475569;line-height:1.65;">Vamos te encontrar no horário com brief prévio do contexto que você compartilhou. A sessão dura 30 minutos.</p><p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS Solutions Architect · Ex-Google · 15+ AWS Certifications</p></div></body></html>"""
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": f"Conversa Anuvia confirmada — {pretty}",
                        "html": email_html,
                        "tags": [{"name": "category", "value": "contact_booking"}],
                    },
                    timeout=20,
                )
            except Exception:
                log.exception("contact_book confirmation email failed")

    # Format friendly response
    pt_weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    pt_months = [
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    ]
    pretty = f"{pt_weekdays[start_dt.weekday()]}, {start_dt.day} de {pt_months[start_dt.month - 1]} às {form.time}"
    return JSONResponse({
        "ok": True,
        "appointment_id": booking.get("appointment_id"),
        "pretty": pretty,
        "lead_id": lead_id,
    })


def _is_brand_host(request: Request) -> bool:
    """True if host is the main brand site (anuvia.com.br or anuvia.net + www variants)."""
    host = (request.headers.get("host") or "").lower().split(":")[0]
    return host in (
        "anuvia.com.br", "www.anuvia.com.br",
        "anuvia.net", "www.anuvia.net",
        "localhost", "127.0.0.1",
    )


# ============================================================================
# i18n — Locale detection for anuvia.com.br (PT/BRL) vs anuvia.net (EN/USD)
# Priority: ?lang= override > cookie > host > Accept-Language > default PT
# ============================================================================

LOCALE_COOKIE = "anuvia_lang"
SUPPORTED_LANGS = ("pt", "en")
DEFAULT_LANG = "pt"


def get_locale(request: Request) -> dict:
    """Resolve language + currency for this request.
    STRICT host binding: anuvia.com.br = PT only, anuvia.net = EN only.
    No ?lang= override and no cookie override — user crosses domains to change language.
    Localhost falls back to ?lang= for dev convenience."""
    host = (request.headers.get("host") or "").lower().split(":")[0]
    is_en_host = host.endswith("anuvia.net")
    is_localhost = host in ("localhost", "127.0.0.1") or host.startswith("127.")

    if is_localhost:
        # Dev mode: respect ?lang= override
        qlang = request.query_params.get("lang", "").lower()
        chosen = qlang if qlang in SUPPORTED_LANGS else DEFAULT_LANG
    else:
        chosen = "en" if is_en_host else "pt"

    lang_full = "pt-BR" if chosen == "pt" else "en-US"
    market = "BR" if chosen == "pt" else "US"
    currency = "BRL" if chosen == "pt" else "USD"
    currency_symbol = "R$" if chosen == "pt" else "US$"

    return {
        "lang": chosen,
        "lang_full": lang_full,
        "market": market,
        "currency": currency,
        "currency_symbol": currency_symbol,
        "host_default_lang": "en" if is_en_host else "pt",
        "host": host,
    }


# Currency conversion (rough — premium pricing pra US market)
# BRL → USD divisor (effectively R$5,50 = US$1 + small premium)
USD_FX_DIVISOR = 5.0


def format_price(brl_text: str, currency: str = "BRL") -> str:
    """Convert 'R$ 45-60k' → 'US$ 9-12k' for English markets.
    Handles formats: 'R$ 45-60k', 'R$ 15-30k/mês', 'R$ 200k+', 'R$ 8-15k/mês', etc."""
    if currency != "USD":
        return brl_text
    import re as _re
    def conv_num(m):
        n = float(m.group(0).replace(',', '.'))
        # Round to nearest reasonable USD
        usd = n / USD_FX_DIVISOR
        if usd >= 100:
            usd = round(usd / 10) * 10
        else:
            usd = round(usd)
        return str(int(usd))
    # Drop the R$, swap k pattern, then prepend US$
    text = brl_text.replace("R$", "US$").replace("R $", "US$")
    text = _re.sub(r"\d+(?:[.,]\d+)?", conv_num, text)
    return text


# Translation strings — keep small, only critical UI chrome and nav.
# Page-specific copy stays in templates with {% if lang == 'en' %} blocks.
TRANSLATIONS = {
    "pt": {
        "nav_cloud": "Cloud",
        "nav_engineering": "Engineering",
        "nav_ai": "AI",
        "nav_growth": "Growth",
        "nav_industry": "Industry",
        "nav_cases": "Cases",
        "nav_about": "Sobre",
        "cta_book": "Agendar conversa",
        "cta_sa": "Falar com um Solutions Architect",
        "cta_view_all": "Ver todas as ofertas",
        "footer_tagline": "Engenharia sênior em Cloud, IA, Plataforma e RevOps. Production-grade desde dia 1.",
        "footer_practices": "Práticas",
        "footer_diagnostics": "Diagnósticos",
        "footer_company": "Anuvia",
        "footer_about": "Sobre",
        "footer_cases": "Cases",
        "footer_blog": "Blog",
        "footer_contact": "Contato",
        "lang_toggle_to": "EN",
        "lang_toggle_to_url": "https://anuvia.net",
    },
    "en": {
        "nav_cloud": "Cloud",
        "nav_engineering": "Engineering",
        "nav_ai": "AI",
        "nav_growth": "Growth",
        "nav_industry": "Industry",
        "nav_cases": "Cases",
        "nav_about": "About",
        "cta_book": "Book a call",
        "cta_sa": "Talk to a Solutions Architect",
        "cta_view_all": "See full catalog",
        "footer_tagline": "Senior engineering in Cloud, AI, Platform and RevOps. Production-grade from day one.",
        "footer_practices": "Practices",
        "footer_diagnostics": "Diagnostics",
        "footer_company": "Anuvia",
        "footer_about": "About",
        "footer_cases": "Cases",
        "footer_blog": "Blog",
        "footer_contact": "Contact",
        "lang_toggle_to": "PT",
        "lang_toggle_to_url": "https://anuvia.com.br",
    },
}


def tpl_ctx(request: Request, **extra) -> dict:
    """Build the standard template context with locale, t (translations), currency."""
    loc = get_locale(request)
    ctx = {
        "request": request,
        "lang": loc["lang"],
        "lang_full": loc["lang_full"],
        "market": loc["market"],
        "currency": loc["currency"],
        "currency_symbol": loc["currency_symbol"],
        "host": loc["host"],
        "t": TRANSLATIONS[loc["lang"]],
        "fmt_price": lambda s: format_price(s, loc["currency"]),
    }
    ctx.update(extra)
    return ctx


# Removed lang-cookie middleware — strict host binding now (anuvia.com.br=PT, anuvia.net=EN).


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # blog.anuvia.com.br/ -> /blog (subdomain root = blog home)
    if _is_blog_host(request):
        return RedirectResponse(url="/blog", status_code=302)
    # anuvia.com.br -> new multi-practice brand home
    if _is_brand_host(request):
        return templates.TemplateResponse("home.html", tpl_ctx(request))
    # diagnostico.anuvia.com.br / roadmap.anuvia.com.br -> funnel LPs
    funnel = detect_funnel(request)
    cfg = FUNNEL_CONFIG[funnel]
    return templates.TemplateResponse(
        cfg["template"],
        {"request": request, "funnel": funnel},
    )


# ----------------------------------------------------------------------------
# Practice pages (anuvia.com.br/<practice>)
# ----------------------------------------------------------------------------

@app.get("/cloud", response_class=HTMLResponse)
@app.get("/cloud/", response_class=HTMLResponse)
async def practice_cloud(request: Request):
    return templates.TemplateResponse("practice_cloud.html", tpl_ctx(request))


@app.get("/engineering", response_class=HTMLResponse)
@app.get("/engineering/", response_class=HTMLResponse)
async def practice_engineering(request: Request):
    return templates.TemplateResponse("practice_engineering.html", tpl_ctx(request))


@app.get("/ai", response_class=HTMLResponse)
@app.get("/ai/", response_class=HTMLResponse)
async def practice_ai(request: Request):
    return templates.TemplateResponse("practice_ai.html", tpl_ctx(request))


@app.get("/growth", response_class=HTMLResponse)
@app.get("/growth/", response_class=HTMLResponse)
async def practice_growth(request: Request):
    return templates.TemplateResponse("practice_growth.html", tpl_ctx(request))


@app.get("/industry", response_class=HTMLResponse)
@app.get("/industry/", response_class=HTMLResponse)
async def practice_industry(request: Request):
    return templates.TemplateResponse("practice_industry.html", tpl_ctx(request))


# ----------------------------------------------------------------------------
# Diagnostic LPs (anchor offerings per practice)
# ----------------------------------------------------------------------------

@app.get("/cloud/finops/audit", response_class=HTMLResponse)
async def lp_finops_audit(request: Request):
    return templates.TemplateResponse("finops_audit.html", tpl_ctx(request))


@app.get("/cloud/aws/well-architected", response_class=HTMLResponse)
async def lp_aws_well_architected(request: Request):
    return templates.TemplateResponse("aws_well_architected.html", tpl_ctx(request))


@app.get("/engineering/devops/maturity", response_class=HTMLResponse)
async def lp_devops_maturity(request: Request):
    return templates.TemplateResponse("devops_maturity.html", tpl_ctx(request))


@app.get("/ai/readiness", response_class=HTMLResponse)
async def lp_ai_readiness(request: Request):
    return templates.TemplateResponse("ai_readiness.html", tpl_ctx(request))


@app.get("/growth/sales-ops", response_class=HTMLResponse)
async def lp_growth_sales_ops(request: Request):
    return templates.TemplateResponse("growth_sales_ops.html", tpl_ctx(request))


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse("about.html", tpl_ctx(request))


@app.get("/cases", response_class=HTMLResponse)
async def cases(request: Request):
    return templates.TemplateResponse("cases.html", tpl_ctx(request))


@app.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return templates.TemplateResponse("contact.html", tpl_ctx(request))


# ============================================================================
# Diagnostic-first helpers — shared across all 5 diagnostic LPs
# Flow:
#   POST /api/<diag>/analyze   → anonymous lead in Supabase + return analysis HTML + lead_id
#   POST /api/<diag>/contact   → PATCH lead with PII + optional booking + send email report
# ============================================================================

async def _create_anonymous_diag_lead(
    client: httpx.AsyncClient,
    funnel_id: str,
    source: str,
    diag_type: str,
    business_meta: dict,
    deliverable_html: str,
    tags: Optional[list] = None,
    market: str = "BR",
    track: str = "diagnostic",
    language: str = "pt-BR",
) -> Optional[str]:
    """Insert anonymous lead (no PII) for tracking diagnostic completion.
    Returns lead_id or None on failure. Uses placeholder email/phone for tracking;
    gets overwritten when contact is upgraded.
    Uses qualification_data jsonb (correct schema column — NOT 'meta')."""
    session_token = uuid.uuid4().hex[:12]
    placeholder_email = f"anon-{session_token}@diagnostic.anuvia.local"
    placeholder_phone = "+0000000000"
    placeholder_name = f"(anônimo · {diag_type} · {session_token})"
    qualification_data = {
        "diagnostic_type": diag_type,
        "anonymous_diagnostic": True,
        "session_token": session_token,
        "deliverable_html": deliverable_html,
        **business_meta,
    }
    payload = {
        "tenant_id": "anuvia",
        "funnel_id": funnel_id,
        "market": market,
        "track": track,
        "language": language,
        "source": source,
        "source_detail": {
            "lp": "anuvia.com.br",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "session_token": session_token,
        },
        "name": placeholder_name,
        "email": placeholder_email,
        "phone_e164": placeholder_phone,
        "company": None,
        "current_stage": "new",
        "qualification_data": qualification_data,
        "consent": {
            "lp_diagnostic": True,
            "anonymous": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        },
        "tags": list(tags or []) + ["anonymous_diagnostic", f"diag:{diag_type}"],
    }
    try:
        r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=payload)
        if r.status_code in (200, 201):
            rows = r.json()
            if rows and isinstance(rows, list):
                return rows[0].get("id")
            log.warning("anonymous_diag_lead empty response body: %s", r.text[:200])
        else:
            log.error("anonymous_diag_lead non-200: status=%s body=%s", r.status_code, r.text[:400])
    except Exception:
        log.exception("anonymous_diag_lead failed")
    return None


async def _upgrade_lead_with_contact(
    client: httpx.AsyncClient,
    lead_id: str,
    name: str,
    email: str,
    whatsapp: str,
    company: Optional[str],
) -> bool:
    """PATCH anonymous lead with real contact info. Moves stage to 'qualified'.
    Uses qualification_data jsonb column (correct schema)."""
    try:
        gr = await client.get(
            f"{SUPA_URL}/leads?id=eq.{lead_id}&select=qualification_data,tags",
            headers=SUPA_HEADERS,
        )
        existing_qd = {}
        existing_tags = []
        if gr.status_code == 200:
            rows = gr.json()
            if rows:
                existing_qd = rows[0].get("qualification_data", {}) or {}
                existing_tags = rows[0].get("tags", []) or []

        existing_qd["upgraded_at"] = datetime.now(timezone.utc).isoformat()
        existing_qd["anonymous_diagnostic"] = False
        new_tags = [t for t in existing_tags if t != "anonymous_diagnostic"]
        new_tags.append("contact_provided")

        patch_payload = {
            "name": name,
            "email": email,
            "phone_e164": whatsapp,
            "company": company or None,
            "current_stage": "qualified",
            "qualification_data": existing_qd,
            "tags": new_tags,
        }
        pr = await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            headers=SUPA_HEADERS,
            json=patch_payload,
        )
        if pr.status_code not in (200, 204):
            log.warning("upgrade_lead non-200: %s %s", pr.status_code, pr.text[:200])
            return False
        return True
    except Exception:
        log.exception("upgrade_lead_failed")
        return False


async def _send_diag_report_email(
    client: httpx.AsyncClient,
    name: str,
    email: str,
    practice_label: str,
    subject: str,
    deliverable_html: str,
    cta_url: str = "https://anuvia.com.br/contact",
) -> bool:
    """Send the diagnostic report email after the user provides contact info."""
    if not RESEND_API_KEY:
        return False
    first = name.split()[0] if name else "Olá"
    inner = deliverable_html.replace('class="card p-8 md:p-10"', '')
    email_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#fafaf9;font-family:-apple-system,Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:640px;margin:0 auto;">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 8px 0;">Anuvia · {practice_label}</p>
  <p style="font-family:Playfair Display,Georgia,serif;font-size:32px;margin:0 0 24px 0;line-height:1.15;">Olá, {first}</p>
  <p style="color:#475569;line-height:1.65;">Obrigada por completar o diagnóstico. Aqui está o resultado completo gerado a partir das suas respostas:</p>
  <div style="background:#ffffff;border:1px solid #e7e5e4;padding:24px;margin:24px 0;">
    {inner}
  </div>
  <p style="color:#475569;line-height:1.65;">Próximo passo natural é uma conversa de 30 minutos com um Solutions Architect pra revisar o diagnóstico junto e priorizar próximos passos.</p>
  <p style="margin:24px 0;"><a href="{cta_url}" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;font-weight:500;">Agendar Discovery Call</a></p>
  <p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS Solutions Architect · Ex-Google · 15+ AWS Certifications</p>
</div>
</body></html>"""
    try:
        await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                "to": [email],
                "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                "subject": subject,
                "html": email_html,
                "tags": [{"name": "category", "value": "diagnostic_report"}],
            },
            timeout=20,
        )
        return True
    except Exception:
        log.exception("diag_report_email_failed")
        return False


async def _notify_slack_diag(
    client: httpx.AsyncClient,
    diag_label: str,
    name: str,
    email: str,
    whatsapp: str,
    company: Optional[str],
    extra_lines: Optional[list] = None,
) -> None:
    """Best-effort Slack notification when a diagnostic captures contact."""
    if not SLACK_WEBHOOK:
        return
    lines = [
        f":mag: Diagnóstico {diag_label} — contato capturado",
        f"*{name}* — {company or '(sem empresa)'}",
        f"📞 {whatsapp}  ✉️ {email}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    try:
        await client.post(SLACK_WEBHOOK, json={"text": "\n".join(lines)}, timeout=10)
    except Exception:
        log.exception("slack_diag_notify_failed")


class DiagContactForm(BaseModel):
    """Step 2 form — captures PII after user has seen the analysis."""
    model_config = ConfigDict(extra="allow")
    lead_id: str = Field(..., min_length=8, max_length=80)
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: Optional[str] = Field(default="", max_length=200)

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


# ----------------------------------------------------------------------------
# FinOps Audit — diagnostic-first flow
# ----------------------------------------------------------------------------

class FinOpsAnalyzeForm(BaseModel):
    """Step 1 — anonymous business questions, no PII."""
    model_config = ConfigDict(extra="allow")
    role: str
    aws_spend: str
    main_pain: str
    aws_tenure: str
    context: Optional[str] = ""


def _build_finops_deliverable(form_data: dict, with_name: Optional[str] = None) -> tuple[dict, str]:
    """Build the FinOps analysis HTML and metadata. Returns (analysis_meta, html)."""
    aws_spend = form_data.get("aws_spend", "")
    main_pain = form_data.get("main_pain", "")
    spend_label, spend_low, spend_annual_low = FINOPS_SPEND_TIERS.get(
        aws_spend, (aws_spend, 0, 0)
    )
    is_fit = aws_spend not in ("under_10k",)
    savings_low = int(spend_annual_low * 0.20) if is_fit else 0
    savings_high = int(spend_annual_low * 0.40) if is_fit else 0
    pain_insight = FINOPS_PAIN_INSIGHT.get(main_pain, "")
    greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."

    if is_fit:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Estimativa preliminar de economia anualizada</p>
    <p class="h-serif text-5xl mb-2">R$ {savings_low:,}</p>
    <p class="text-sm text-subtle">a R$ {savings_high:,}/ano</p>
    <p class="text-xs text-subtle mt-3">Baseado em fatura {spend_label} e padrões observados em audits anteriores.<br>Faixa conservadora 20% → ambiciosa 40% da economia identificada.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Insight sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Esta pré-análise é orientativa baseada em padrões agregados. Audit completo individualiza pra sua realidade específica (workloads, configuração, tags, contratos AWS).</p>
</div>
""".strip()
    else:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">Pelo perfil de fatura ({spend_label}), FinOps Audit completo de R$ 45-60k provavelmente não cobre o ROI necessário pra valer a pena pra você agora.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Sugestões mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500. Diagnóstico ao vivo + quick wins acionáveis no mesmo dia.</li>
    <li>• <strong>Workshop FinOps Express</strong> — R$ 8-12k, 1 semana. Audit mais leve com economia identificada documentada.</li>
    <li>• <strong>Anuvia AI Ops</strong> — se a dor não é só AWS mas operação como um todo, considera nosso produto SaaS de automação de operações.</li>
  </ul>
</div>
""".strip()

    meta = {
        "is_fit": is_fit,
        "savings_estimate_low": savings_low,
        "savings_estimate_high": savings_high,
        "spend_label": spend_label,
    }
    return meta, html


@app.post("/api/finops-audit/analyze")
async def api_finops_audit_analyze(form: FinOpsAnalyzeForm, request: Request):
    """Step 1 — receive business answers, return analysis HTML + lead_id (no PII)."""
    form_data = form.model_dump()
    analysis_meta, html = _build_finops_deliverable(form_data)
    business_meta = {
        "role": form.role,
        "aws_spend": form.aws_spend,
        "main_pain": form.main_pain,
        "aws_tenure": form.aws_tenure,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_FINOPS",
            source="lp_finops_audit",
            diag_type="finops_audit",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_finops_audit", "br_finops"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html, "is_fit": analysis_meta["is_fit"]})


@app.post("/api/finops-audit/contact")
async def api_finops_audit_contact(form: DiagContactForm):
    """Step 2 — capture PII, upgrade lead, email report."""
    async with httpx.AsyncClient(timeout=20) as client:
        # Re-fetch business meta to rebuild HTML with name
        try:
            r = await client.get(
                f"{SUPA_URL}/leads?id=eq.{form.lead_id}&select=qualification_data",
                headers=SUPA_HEADERS,
            )
            rows = r.json() if r.status_code == 200 else []
            meta = rows[0].get("qualification_data", {}) if rows else {}
        except Exception:
            meta = {}
        _, deliverable_html = _build_finops_deliverable(meta, with_name=form.name)

        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        subject = (
            f"FinOps Audit — pré-análise pra {first}"
            if is_fit else
            f"Obrigada, {first} — alternativas pro seu caso"
        )
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "FinOps", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "FinOps Audit", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Fatura: {meta.get('spend_label', '?')}  ·  Dor: {meta.get('main_pain', '?')}",
                f"Fit: {'yes' if is_fit else 'no'}",
            ],
        )
    return JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})


# ----------------------------------------------------------------------------
# AWS Well-Architected — diagnostic-first flow
# ----------------------------------------------------------------------------

class WAAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    focus: str
    workload: str
    context: Optional[str] = ""


def _build_wa_deliverable(form_data: dict, with_name: Optional[str] = None) -> tuple[dict, str]:
    focus = form_data.get("focus", "")
    workload = form_data.get("workload", "")
    focus_insight = WA_FOCUS_INSIGHT.get(focus, "")
    greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
    html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o foco que você indicou</p>
    <p class="text-ink/80 leading-relaxed">{focus_insight}</p>
  </div>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">O que o WA Review entrega</p>
    <p class="text-ink/80 leading-relaxed mb-3">Avaliação técnica nos 6 pilares AWS (Security, Reliability, Performance, Cost, Operational Excellence, Sustainability) com gap analysis específico para seu workload ({workload or 'não informado'}). Saída: relatório executivo + remediação priorizada por effort/impact.</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AWS Well-Architected Review da Anuvia é executado por ex-AWS Solutions Architect (15+ certs). 3-5 semanas, R$ 30-50k.</p>
</div>
""".strip()
    return {"focus": focus, "workload": workload}, html


@app.post("/api/aws-well-architected/analyze")
async def api_aws_wa_analyze(form: WAAnalyzeForm, request: Request):
    form_data = form.model_dump()
    analysis_meta, html = _build_wa_deliverable(form_data)
    business_meta = {
        "role": form.role,
        "focus": form.focus,
        "workload": form.workload,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_AWS_WA",
            source="lp_aws_well_architected",
            diag_type="aws_well_architected",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_aws_wa", "br_aws_wa"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html})


@app.post("/api/aws-well-architected/contact")
async def api_aws_wa_contact(form: DiagContactForm):
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{SUPA_URL}/leads?id=eq.{form.lead_id}&select=qualification_data",
                headers=SUPA_HEADERS,
            )
            rows = r.json() if r.status_code == 200 else []
            meta = rows[0].get("qualification_data", {}) if rows else {}
        except Exception:
            meta = {}
        _, deliverable_html = _build_wa_deliverable(meta, with_name=form.name)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        subject = f"AWS Well-Architected Review — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AWS", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AWS Well-Architected", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[f"Foco: {meta.get('focus', '?')}  ·  Workload: {meta.get('workload', '?')}"],
        )
    return JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})


# ----------------------------------------------------------------------------
# DevOps Maturity — diagnostic-first flow
# ----------------------------------------------------------------------------

class DevOpsAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    team_size: str
    deploy_freq: str
    main_pain: str
    stack: Optional[str] = ""
    context: Optional[str] = ""


def _build_devops_deliverable(form_data: dict, with_name: Optional[str] = None) -> tuple[dict, str]:
    deploy_freq = form_data.get("deploy_freq", "")
    main_pain = form_data.get("main_pain", "")
    level, level_insight = DORA_LEVEL.get(deploy_freq, ("?", ""))
    pain_insight = DEVOPS_PAIN_INSIGHT.get(main_pain, "")
    greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
    html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise DORA · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Nível DORA preliminar</p>
    <p class="h-serif text-5xl mb-2">{level}</p>
    <p class="text-sm text-ink/70 leading-relaxed">{level_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre sua dor principal</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">DevOps Maturity Assessment: 4 semanas, R$ 35-50k. Entrega DORA baseline + gap analysis + 6-month roadmap.</p>
</div>
""".strip()
    return {"dora_level": level, "deploy_freq": deploy_freq, "main_pain": main_pain}, html


@app.post("/api/devops-maturity/analyze")
async def api_devops_analyze(form: DevOpsAnalyzeForm, request: Request):
    form_data = form.model_dump()
    analysis_meta, html = _build_devops_deliverable(form_data)
    business_meta = {
        "role": form.role,
        "team_size": form.team_size,
        "deploy_freq": form.deploy_freq,
        "main_pain": form.main_pain,
        "stack": form.stack or "",
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_DEVOPS",
            source="lp_devops_maturity",
            diag_type="devops_maturity",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_devops_maturity", "br_devops"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html, "dora_level": analysis_meta["dora_level"]})


@app.post("/api/devops-maturity/contact")
async def api_devops_contact(form: DiagContactForm):
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{SUPA_URL}/leads?id=eq.{form.lead_id}&select=qualification_data",
                headers=SUPA_HEADERS,
            )
            rows = r.json() if r.status_code == 200 else []
            meta = rows[0].get("qualification_data", {}) if rows else {}
        except Exception:
            meta = {}
        _, deliverable_html = _build_devops_deliverable(meta, with_name=form.name)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        subject = f"DevOps Maturity — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "DevOps", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "DevOps Maturity", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[f"DORA: {meta.get('dora_level', '?')}  ·  Deploy: {meta.get('deploy_freq', '?')}"],
        )
    return JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})


# ----------------------------------------------------------------------------
# AI Readiness — diagnostic-first flow
# ----------------------------------------------------------------------------

class AIReadinessAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    ai_stage: str
    main_pain: str
    revenue_tier: str
    context: Optional[str] = ""


def _build_ai_readiness_deliverable(form_data: dict, with_name: Optional[str] = None) -> tuple[dict, str]:
    ai_stage = form_data.get("ai_stage", "")
    main_pain = form_data.get("main_pain", "")
    revenue_tier = form_data.get("revenue_tier", "")
    stage_insight = AI_STAGE_INSIGHT.get(ai_stage, "")
    pain_insight = AI_PAIN_INSIGHT.get(main_pain, "")
    is_fit = revenue_tier in ("5m_30m", "30m_100m", "100m_plus")
    greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
    if is_fit:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre seu estágio atual</p>
    <p class="text-ink/80 leading-relaxed">{stage_insight}</p>
  </div>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre sua dor principal</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AI Readiness Sprint: 2-3 semanas, R$ 25-40k. Entrega inventário de use cases + ROI estimado + roadmap 12 meses + decisão build vs buy.</p>
</div>
""".strip()
    else:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">Pelo perfil de faturamento, Sprint completo de R$ 25-40k provavelmente não cobre ROI necessário agora.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Alternativas mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500.</li>
    <li>• <strong>AI Quick Win</strong> — R$ 8-15k, 2-3 semanas.</li>
    <li>• <strong>Anuvia AI Ops</strong> — squad de agentes recorrente, R$ 3-8k/mês.</li>
  </ul>
</div>
""".strip()
    return {"ai_stage": ai_stage, "main_pain": main_pain, "revenue_tier": revenue_tier, "is_fit": is_fit}, html


@app.post("/api/ai-readiness/analyze")
async def api_ai_readiness_analyze(form: AIReadinessAnalyzeForm, request: Request):
    form_data = form.model_dump()
    analysis_meta, html = _build_ai_readiness_deliverable(form_data)
    business_meta = {
        "role": form.role,
        "ai_stage": form.ai_stage,
        "main_pain": form.main_pain,
        "revenue_tier": form.revenue_tier,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_AI",
            source="lp_ai_readiness",
            diag_type="ai_readiness",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_ai_readiness", "br_ai"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html, "is_fit": analysis_meta["is_fit"]})


@app.post("/api/ai-readiness/contact")
async def api_ai_readiness_contact(form: DiagContactForm):
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{SUPA_URL}/leads?id=eq.{form.lead_id}&select=qualification_data",
                headers=SUPA_HEADERS,
            )
            rows = r.json() if r.status_code == 200 else []
            meta = rows[0].get("qualification_data", {}) if rows else {}
        except Exception:
            meta = {}
        _, deliverable_html = _build_ai_readiness_deliverable(meta, with_name=form.name)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        subject = f"AI Readiness Sprint — pré-análise pra {first}" if is_fit else f"Obrigada, {first} — alternativas pro seu caso"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AI", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AI Readiness", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Estágio: {meta.get('ai_stage', '?')}  ·  Dor: {meta.get('main_pain', '?')}",
                f"Fit: {'yes' if is_fit else 'no'}",
            ],
        )
    return JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})


# ----------------------------------------------------------------------------
# Growth Sales Ops — diagnostic-first flow
# ----------------------------------------------------------------------------

class GrowthAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    team_size: str
    ticket_size: str
    main_pain: str
    context: Optional[str] = ""


def _build_growth_deliverable(form_data: dict, with_name: Optional[str] = None) -> tuple[dict, str]:
    ticket_size = form_data.get("ticket_size", "")
    main_pain = form_data.get("main_pain", "")
    pain_insight = SALESOPS_PAIN_INSIGHT.get(main_pain, "")
    is_fit = ticket_size in ("5k_25k", "25k_100k", "100k_plus")
    greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
    if is_fit:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Sales Ops Diagnostic: 2 semanas, R$ 15-25k. Entrega funnel map + automation playbook + roadmap 90 dias.</p>
</div>
""".strip()
    else:
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">Com ticket abaixo de R$ 5k, diagnóstico completo de R$ 15-25k provavelmente não cobre o ROI necessário no curto prazo.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Alternativas mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>AI Quick Win</strong> — R$ 8-15k, 2-3 semanas.</li>
    <li>• <strong>Anuvia AI Ops Subscription</strong> — R$ 3-8k/mês.</li>
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500.</li>
  </ul>
</div>
""".strip()
    return {"ticket_size": ticket_size, "main_pain": main_pain, "is_fit": is_fit}, html


@app.post("/api/growth-sales-ops/analyze")
async def api_growth_analyze(form: GrowthAnalyzeForm, request: Request):
    form_data = form.model_dump()
    analysis_meta, html = _build_growth_deliverable(form_data)
    business_meta = {
        "role": form.role,
        "team_size": form.team_size,
        "ticket_size": form.ticket_size,
        "main_pain": form.main_pain,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_GROWTH",
            source="lp_growth_sales_ops",
            diag_type="growth_sales_ops",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_growth_sales_ops", "br_growth"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html, "is_fit": analysis_meta["is_fit"]})


@app.post("/api/growth-sales-ops/contact")
async def api_growth_contact(form: DiagContactForm):
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"{SUPA_URL}/leads?id=eq.{form.lead_id}&select=qualification_data",
                headers=SUPA_HEADERS,
            )
            rows = r.json() if r.status_code == 200 else []
            meta = rows[0].get("qualification_data", {}) if rows else {}
        except Exception:
            meta = {}
        _, deliverable_html = _build_growth_deliverable(meta, with_name=form.name)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        subject = f"Sales Ops Diagnostic — pré-análise pra {first}" if is_fit else f"Obrigada, {first} — alternativas pro seu caso"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "Growth", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "Sales Ops Diagnostic", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Ticket: {meta.get('ticket_size', '?')}  ·  Dor: {meta.get('main_pain', '?')}",
                f"Fit: {'yes' if is_fit else 'no'}",
            ],
        )
    return JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})


class FinOpsAuditForm(BaseModel):
    """Pré-qualificação FinOps Audit — captura lead + envia análise preliminar."""
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: str = Field(..., min_length=2, max_length=200)
    role: str
    aws_spend: str
    main_pain: str
    aws_tenure: str
    context: Optional[str] = ""

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


FINOPS_SPEND_TIERS = {
    "under_10k": ("< $10k/mês", 8_000, 80_000),
    "10k_30k": ("$10-30k/mês", 20_000, 240_000),
    "30k_100k": ("$30-100k/mês", 65_000, 780_000),
    "100k_300k": ("$100-300k/mês", 200_000, 2_400_000),
    "300k_plus": ("> $300k/mês", 400_000, 4_800_000),
}

FINOPS_PAIN_INSIGHT = {
    "bill_growth": "Quando fatura cresce sem visibilidade, tipicamente 30-45% é overprovisioning + Reserved Instances/Savings Plans subutilizados. Padrão recorrente.",
    "no_visibility": "Sem tagging strategy e showback/chargeback, atribuir custo por produto/time é impossível. Audit começa exatamente por isso.",
    "ri_savings": "RI/SP mal otimizados são a fonte mais comum de 15-25% economia imediata. Audit traz portfolio analysis em 1 semana.",
    "architecture": "Arquitetura herdada que não escala economicamente costuma ter pelo menos 3-4 padrões corrigíveis sem refactor profundo. Quick wins primeiro, depois roadmap.",
    "finops_practice": "FinOps practice interno requer 3 pilares: visibilidade, allocation, governance. Audit estabelece o baseline pra cada um.",
    "cfo_pressure": "Quando CFO pressiona, foco em quick wins primeiros 30 dias (10-15% economia) pra mostrar resultado, depois roadmap completo.",
}


@app.post("/api/finops-audit")
async def api_finops_audit(form: FinOpsAuditForm):
    """Recebe form de pré-qualificação FinOps Audit, gera análise preliminar."""
    # Determine fit
    spend_label, spend_low, spend_annual_low = FINOPS_SPEND_TIERS.get(
        form.aws_spend, (form.aws_spend, 0, 0)
    )
    is_fit = form.aws_spend not in ("under_10k",)

    # Conservative estimates
    savings_low = int(spend_annual_low * 0.20) if is_fit else 0  # 20% conservative
    savings_high = int(spend_annual_low * 0.40) if is_fit else 0  # 40% optimistic
    savings_mid = (savings_low + savings_high) // 2

    pain_insight = FINOPS_PAIN_INSIGHT.get(form.main_pain, "")

    # Build deliverable HTML
    if is_fit:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Vocês são bom fit pro FinOps Audit, {form.name.split()[0]}.</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Estimativa preliminar de economia anualizada</p>
    <p class="h-serif text-5xl mb-2">R$ {savings_low:,}</p>
    <p class="text-sm text-subtle">a R$ {savings_high:,}/ano</p>
    <p class="text-xs text-subtle mt-3">Baseado em fatura {spend_label} e padrões observados em audits anteriores.<br>Faixa conservadora 20% → ambiciosa 40% da economia identificada.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Insight sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Próximo passo</p>
    <p class="text-ink/80 leading-relaxed mb-5">Em até 24h, Mila vai te mandar email com (a) data sugerida de discovery call de 30 min, (b) detalhes do contrato + garantia 3× ROI, (c) lista exata de IAM permissions read-only que o audit precisa.</p>
    <a href="https://cal.anuvia.com.br" class="btn-primary text-sm inline-block">Ou agende você mesmo aqui</a>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Esta pré-análise é orientativa baseada em padrões agregados. Audit completo individualiza pra sua realidade específica (workloads, configuração atual, tags, contratos AWS).</p>
</div>
""".strip()
    else:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Obrigada, {form.name.split()[0]}.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Pelo perfil de fatura ({spend_label}), FinOps Audit completo de R$ 45-60k provavelmente não cobre o ROI necessário pra valer a pena pra você.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Sugestões mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500. Diagnóstico ao vivo + quick wins acionáveis no mesmo dia.</li>
    <li>• <strong>Workshop FinOps Express</strong> — R$ 8-12k, 1 semana. Audit mais leve com economia identificada documentada.</li>
    <li>• <strong>Anuvia AI Ops</strong> — se a dor não é só AWS mas operação como um todo, considera nosso produto SaaS de automação de operações.</li>
  </ul>
  <p class="text-sm text-ink/70">Mila te manda essas opções por email em até 24h.</p>
</div>
""".strip()

    # Insert lead in Supabase (funnel BR_FINOPS as new funnel id)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lead_payload = {
                "funnel_id": "BR_FINOPS",
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "source": "lp_finops_audit",
                "meta": {
                    "role": form.role,
                    "aws_spend": form.aws_spend,
                    "main_pain": form.main_pain,
                    "aws_tenure": form.aws_tenure,
                    "context": form.context,
                    "is_fit": is_fit,
                    "savings_estimate_low": savings_low,
                    "savings_estimate_high": savings_high,
                },
                "tags": ["lp_finops_audit", "br_finops"],
            }
            r = await client.post(
                f"{SUPA_URL}/leads",
                headers=SUPA_HEADERS,
                json=lead_payload,
            )
            if r.status_code not in (200, 201):
                log.warning("supabase_lead_insert non-200: %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("supabase_lead_insert_failed")

        # Send email with deliverable
        try:
            subject = (
                f"FinOps Audit — pré-análise pra {form.name.split()[0]}"
                if is_fit else
                f"Obrigada, {form.name.split()[0]} — alternativas pro seu caso"
            )
            email_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body style="background:#fafaf9;font-family:-apple-system,Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:640px;margin:0 auto;">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 8px 0;">Anuvia · FinOps</p>
  <p style="font-family:Playfair Display,Georgia,serif;font-size:32px;margin:0 0 24px 0;line-height:1.15;">Olá, {form.name.split()[0]}</p>
  <p style="color:#475569;line-height:1.65;">Obrigada pelo interesse no FinOps Audit. Aqui está a pré-análise gerada a partir das suas respostas:</p>
  <div style="background:#ffffff;border:1px solid #e7e5e4;padding:24px;margin:24px 0;">
    {deliverable_html.replace('class="card p-8 md:p-10"', '')}
  </div>
  <p style="color:#475569;line-height:1.65;">Em até 24h te respondo com próximos passos concretos. Se quiser adiantar, agenda direto:</p>
  <p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;font-weight:500;">Agendar Discovery Call</a></p>
  <p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS Solutions Architect · Ex-Google · 15+ AWS Certifications</p>
</div>
</body></html>"""
            if RESEND_API_KEY:
                await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": subject,
                        "html": email_html,
                        "tags": [
                            {"name": "category", "value": "finops_audit_signup"},
                            {"name": "is_fit", "value": "yes" if is_fit else "no"},
                        ],
                    },
                    timeout=20,
                )
        except Exception:
            log.exception("finops_audit_email_failed")

    return JSONResponse({
        "ok": True,
        "is_fit": is_fit,
        "savings_estimate_low": savings_low,
        "savings_estimate_high": savings_high,
        "deliverable_html": deliverable_html,
    })


# ----------------------------------------------------------------------------
# AWS Well-Architected Review - signup API
# ----------------------------------------------------------------------------

class WellArchitectedForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: str = Field(..., min_length=2, max_length=200)
    role: str
    focus: str
    workload: str
    context: Optional[str] = ""

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


WA_FOCUS_INSIGHT = {
    "security": "Security é onde mais vejo gaps em audit AWS — IAM permissions excessivas, network public exposure, encryption gaps em S3/RDS, missing CloudTrail/Config baseline. Audit prioriza essas categorias.",
    "reliability": "Reliability começa com BCP/DR realista — RPO/RTO documentados, backups testados, multi-AZ vs multi-region trade-offs claros. Audit valida prontidão pra falhas.",
    "performance": "Performance issues quase sempre são scaling policy mal-configurada, RDS sub-dimensionado, ou network bottlenecks. Audit identifica via real metrics, não suposição.",
    "cost": "Cost focus se sobrepõe ao FinOps Audit dedicado — sugiro avaliar se faz mais sentido começar pelo FinOps Risk-Free, que é específico em economia.",
    "operational": "Operational Excellence é IaC + automação + runbooks + post-mortems. Audit identifica onde está manual vs automatizado e prioriza investimento.",
    "all": "Cobertura completa nos 6 pilares dá visão executiva pra board/founders. Útil pré-investment round ou pré-acquisition due diligence.",
}


@app.post("/api/aws-well-architected")
async def api_aws_well_architected(form: WellArchitectedForm):
    """Recebe form pré-qualificação AWS Well-Architected Review."""
    focus_insight = WA_FOCUS_INSIGHT.get(form.focus, "")

    deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Obrigada, {form.name.split()[0]}.</p>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o foco que você indicou</p>
    <p class="text-ink/80 leading-relaxed">{focus_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Próximo passo</p>
    <p class="text-ink/80 leading-relaxed mb-5">Em até 24h, Mila te manda email com (a) escopo detalhado do review pra seu workload, (b) sugestão de data pra discovery call, (c) lista de IAM permissions read-only necessárias pro audit.</p>
    <a href="https://cal.anuvia.com.br" class="btn-primary text-sm inline-block">Ou agende você mesmo</a>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AWS Well-Architected Review da Anuvia é executado por ex-AWS Solutions Architect (15+ certs). Você recebe relatório executivo com gap analysis nos 6 pilares + remediation roadmap priorizado.</p>
</div>
""".strip()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lead_payload = {
                "funnel_id": "BR_AWS_WA",
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "source": "lp_aws_well_architected",
                "meta": {
                    "role": form.role,
                    "focus": form.focus,
                    "workload": form.workload,
                    "context": form.context,
                },
                "tags": ["lp_aws_wa", "br_aws_wa"],
            }
            r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=lead_payload)
            if r.status_code not in (200, 201):
                log.warning("supabase_lead_insert non-200: %s", r.status_code)
        except Exception:
            log.exception("supabase_lead_insert_failed")

        try:
            if RESEND_API_KEY:
                subject = f"AWS Well-Architected Review — pré-análise pra {form.name.split()[0]}"
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · AWS</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {form.name.split()[0]}</p><p style="color:#475569;line-height:1.65;">Obrigada pelo interesse no AWS Well-Architected Review. Em até 24h te respondemos com escopo detalhado.</p><p style="color:#475569;line-height:1.65;">{focus_insight}</p><p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p><p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS Solutions Architect · Ex-Google · 15+ AWS Certifications</p></div></body></html>"""
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": subject,
                        "html": email_html,
                        "tags": [{"name": "category", "value": "aws_wa_signup"}],
                    },
                    timeout=20,
                )
        except Exception:
            log.exception("aws_wa_email_failed")

    return JSONResponse({"ok": True, "deliverable_html": deliverable_html})


# ----------------------------------------------------------------------------
# DevOps Maturity Assessment - signup API
# ----------------------------------------------------------------------------

class DevOpsMaturityForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: str = Field(..., min_length=2, max_length=200)
    role: str
    team_size: str
    deploy_freq: str
    main_pain: str
    stack: str
    context: Optional[str] = ""

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


DORA_LEVEL = {
    "multiple_day": ("Elite", "Sua deploy frequency já está em nível Elite (top 11% global). Audit foca em refinar reliability, observability, e fortalecer práticas avançadas (chaos engineering, progressive delivery)."),
    "daily": ("High", "Sua deploy frequency está em nível High (top 25%). Audit identifica caminhos pra chegar em Elite — tipicamente reduzir change failure rate ou acelerar lead time."),
    "weekly": ("Medium", "Deploy semanal é Medium. Audit identifica os gargalos principais: test automation, deployment automation, ou approval bottlenecks."),
    "biweekly": ("Medium", "Quinzenal está entre Medium-Low. Costuma haver mistura de manual processes + falta de IaC + test gaps. Audit prioriza intervenções de maior ROI."),
    "monthly": ("Low", "Deploy mensal é Low performer. Maior alavanca tipicamente é eliminar gates manuais + introduzir automation incrementalmente."),
    "quarterly": ("Low", "Deploy trimestral indica processo crítico de release. Audit foca em desconstruir os gates que tornam release evento."),
}

DEVOPS_PAIN_INSIGHT = {
    "release_pain": "Releases como evento são quase sempre sintoma de: (a) testes manuais demais, (b) feature flags ausentes/mal usados, (c) rollback procedure não testado, (d) coordenação cross-team excessiva. Audit identifica qual destes domina.",
    "incidents": "MTTR alto vem de combinação: falta de observability detalhada + runbooks fracos + on-call exhaustion. Atacar primeiro o pilar mais fraco dá maior return.",
    "observability": "Debugging cego é caro. Stack moderno (DataDog, NewRelic, Grafana Cloud) com tracing + structured logging muda o jogo em semanas, não meses.",
    "oncall": "On-call burnout precede churn. Audit identifica top 10 noisy alerts (geram 80% das interrupções) e cria plano pra silenciar/refinar.",
    "quality": "Change failure rate alto tipicamente: test coverage baixo, integration tests ausentes, ou deploy pipeline pula gates. Audit mede e prioriza.",
    "speed": "Lead time longo é normalmente: PR review demorado, CI lento, ou approvals manuais. Cada um tem fix específico.",
}


@app.post("/api/devops-maturity")
async def api_devops_maturity(form: DevOpsMaturityForm):
    """Recebe form pré-qualificação DevOps Maturity Assessment."""
    level, level_insight = DORA_LEVEL.get(form.deploy_freq, ("?", ""))
    pain_insight = DEVOPS_PAIN_INSIGHT.get(form.main_pain, "")

    deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise DORA · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Olá, {form.name.split()[0]}.</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Nível DORA preliminar</p>
    <p class="h-serif text-5xl mb-2">{level}</p>
    <p class="text-sm text-ink/70 leading-relaxed">{level_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre sua dor principal</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Próximo passo</p>
    <p class="text-ink/80 leading-relaxed mb-5">Em até 24h, Mila te manda email com escopo do audit customizado pro seu contexto + sugestão de data pra discovery call.</p>
    <a href="https://cal.anuvia.com.br" class="btn-primary text-sm inline-block">Ou agende você mesmo</a>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">DevOps Maturity Assessment usa DORA framework + práticas adicionais (observability, security, IaC). Audit completo: 4 semanas, R$ 35-50k.</p>
</div>
""".strip()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lead_payload = {
                "funnel_id": "BR_DEVOPS",
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "source": "lp_devops_maturity",
                "meta": {
                    "role": form.role,
                    "team_size": form.team_size,
                    "deploy_freq": form.deploy_freq,
                    "dora_level": level,
                    "main_pain": form.main_pain,
                    "stack": form.stack,
                    "context": form.context,
                },
                "tags": ["lp_devops_maturity", "br_devops"],
            }
            r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=lead_payload)
            if r.status_code not in (200, 201):
                log.warning("supabase_lead_insert non-200: %s", r.status_code)
        except Exception:
            log.exception("supabase_lead_insert_failed")

        try:
            if RESEND_API_KEY:
                subject = f"DevOps Maturity Assessment — pré-análise pra {form.name.split()[0]}"
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · DevOps</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {form.name.split()[0]}</p><p style="color:#475569;line-height:1.65;">Pré-análise DORA preliminar: nível <strong>{level}</strong>.</p><p style="color:#475569;line-height:1.65;">{level_insight}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p><p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS · Ex-Google · 15+ AWS Certifications</p></div></body></html>"""
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": subject,
                        "html": email_html,
                        "tags": [{"name": "category", "value": "devops_maturity_signup"}],
                    },
                    timeout=20,
                )
        except Exception:
            log.exception("devops_maturity_email_failed")

    return JSONResponse({"ok": True, "dora_level": level, "deliverable_html": deliverable_html})


# ----------------------------------------------------------------------------
# AI Readiness Sprint — pré-qualificação form
# ----------------------------------------------------------------------------

class AIReadinessForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: str = Field(..., min_length=2, max_length=200)
    role: str
    ai_stage: str
    main_pain: str
    revenue_tier: str
    context: Optional[str] = ""

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


AI_STAGE_INSIGHT = {
    "exploring": "Na fase exploratória, Readiness Sprint costuma ser o primeiro investimento defensável — antes de comprometer R$ 100-300k em uma plataforma. Saídas típicas: 2-3 casos prioritários com business case calibrado.",
    "experimenting": "Quando há PoCs em paralelo sem ordem, o Sprint normalmente identifica que metade dos experimentos não justifica investimento de produção. Foco na metade que justifica.",
    "early_prod": "Com 1-2 casos rodando, próxima etapa costuma ser MLOps Practice Build (governança, observabilidade, custo) antes de adicionar mais casos. Sprint formaliza esse caminho.",
    "scaling": "Em escala, o ROI maior costuma vir de FinOps de IA (otimizar custo por inferência) e governance, não de novos casos. Sprint redireciona o roadmap.",
    "mature": "Operação madura tipicamente busca o Sprint para validar próximas fronteiras: agentes autônomos, multi-modelo, especialização vertical. Discussão técnica direta.",
}

AI_PAIN_INSIGHT = {
    "poc_to_prod": "PoC que não vira produção é quase sempre falha no desenho inicial — sem gates de evolução claros e sem critério técnico de aprovação. Sprint formaliza esses gates desde o discovery.",
    "cost_runaway": "Custo fora de controle costuma vir de combinação: prompt caching ausente, modelo errado para a tarefa, e ausência de cap por tenant. Sprint identifica e quantifica cada vetor.",
    "no_eval": "Sem eval harness, qualquer melhoria de prompt é cega. Estabelecer baseline de avaliação é a primeira entrega sempre — sem isso o resto é teatro.",
    "usecase_blur": "Casos pouco claros são o cenário em que o Sprint mais agrega. Saída típica: 8-15 candidatos mapeados, 3-5 priorizados, business case explícito para cada.",
    "vendor_lock": "Estratégia multi-modelo é viável com a abstração correta. Sprint avalia trade-offs Bedrock vs OpenAI vs Vertex vs self-hosted considerando custo, latência e governança.",
    "governance": "Governança bloqueando avanço normalmente vem de risco mal mapeado. Sprint produz o registro de risco que o time jurídico/compliance precisa para destravar.",
}


@app.post("/api/ai-readiness")
async def api_ai_readiness(form: AIReadinessForm):
    """Recebe form pré-qualificação AI Readiness Sprint."""
    stage_insight = AI_STAGE_INSIGHT.get(form.ai_stage, "")
    pain_insight = AI_PAIN_INSIGHT.get(form.main_pain, "")
    is_fit = form.revenue_tier in ("5m_30m", "30m_100m", "100m_plus")

    if is_fit:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Olá, {form.name.split()[0]}.</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre seu estágio atual</p>
    <p class="text-ink/80 leading-relaxed">{stage_insight}</p>
  </div>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre sua dor principal</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Próximo passo</p>
    <p class="text-ink/80 leading-relaxed mb-5">Em até 24h, Mila te manda email com escopo do Sprint adaptado pro seu contexto + sugestão de horários para discovery call de 30 minutos.</p>
    <a href="/contact?offering=ai-readiness&practice=ai" class="btn-primary text-sm inline-block">Ou agende você mesmo agora</a>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AI Readiness Sprint: 2-3 semanas, R$ 25-40k. Entrega inventário de use cases + ROI estimado + roadmap 12 meses + decisão build vs buy.</p>
</div>
""".strip()
    else:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Obrigada, {form.name.split()[0]}.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Pelo perfil de faturamento, Sprint completo de R$ 25-40k provavelmente não cobre ROI necessário pra valer a pena pra você agora.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Alternativas mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500. Discussão técnica direcionada com saídas acionáveis no mesmo dia.</li>
    <li>• <strong>AI Quick Win</strong> (Anuvia Growth) — R$ 8-15k. Implementação de 1 automação alto-impacto pronta em 2-3 semanas.</li>
    <li>• <strong>Anuvia AI Ops</strong> — squad de agentes recorrente, R$ 3-8k/mês, sem investimento inicial pesado.</li>
  </ul>
  <p class="text-sm text-ink/70">Mila te manda essas opções por email em até 24h.</p>
</div>
""".strip()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lead_payload = {
                "funnel_id": "BR_AI",
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "source": "lp_ai_readiness",
                "meta": {
                    "role": form.role,
                    "ai_stage": form.ai_stage,
                    "main_pain": form.main_pain,
                    "revenue_tier": form.revenue_tier,
                    "is_fit": is_fit,
                    "context": form.context,
                },
                "tags": ["lp_ai_readiness", "br_ai"],
            }
            r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=lead_payload)
            if r.status_code not in (200, 201):
                log.warning("supabase_lead_insert non-200: %s", r.status_code)
        except Exception:
            log.exception("ai_readiness lead_insert failed")

        try:
            if RESEND_API_KEY:
                first = form.name.split()[0]
                subject = f"AI Readiness Sprint — pré-análise pra {first}"
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · AI</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {first}</p><p style="color:#475569;line-height:1.65;">{stage_insight}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://anuvia.com.br/contact?offering=ai-readiness&practice=ai" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p><p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS · Ex-Google · 15+ AWS Certifications</p></div></body></html>"""
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": subject,
                        "html": email_html,
                        "tags": [{"name": "category", "value": "ai_readiness_signup"}],
                    },
                    timeout=20,
                )
        except Exception:
            log.exception("ai_readiness_email_failed")

    return JSONResponse({"ok": True, "html": deliverable_html})


# ----------------------------------------------------------------------------
# Growth Sales Ops Diagnostic — pré-qualificação form
# ----------------------------------------------------------------------------

class GrowthSalesOpsForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    whatsapp: str = Field(..., min_length=8, max_length=30)
    company: str = Field(..., min_length=2, max_length=200)
    role: str
    team_size: str
    ticket_size: str
    main_pain: str
    context: Optional[str] = ""

    @field_validator("whatsapp")
    @classmethod
    def validate_whatsapp(cls, v: str) -> str:
        normalized = normalize_phone(v)
        if not normalized:
            raise ValueError("WhatsApp inválido.")
        return normalized


SALESOPS_PAIN_INSIGHT = {
    "lead_leakage": "Lead esfriando antes da resposta é métrica direta de SLA quebrado. Diagnóstico mede tempo real de resposta por canal e identifica o canal que merece automação imediata.",
    "manual_ops": "Processo manual demais costuma se concentrar em 3-5 atividades repetitivas (qualificação, scheduling, follow-up). Diagnóstico quantifica horas/semana gastas em cada uma.",
    "no_visibility": "Sem visibilidade no pipeline normalmente é sintoma de CRM mal configurado ou stages mal definidos. Diagnóstico produz funnel map e estados consistentes em 2 semanas.",
    "low_conversion": "Queda inesperada de conversão entre estágios costuma ter causa identificável em uma transição específica. Diagnóstico isola onde está e propõe intervenção.",
    "team_burnout": "Time sobrecarregado é sintoma — não causa. Diagnóstico identifica se é volume excessivo de leads de baixa qualidade, processo manual, ou problema de alocação.",
    "founder_blocked": "Founder como gargalo em toda venda costuma vir de ausência de playbook documentado + falta de qualificação automática. Diagnóstico produz os dois.",
}


@app.post("/api/growth-sales-ops")
async def api_growth_sales_ops(form: GrowthSalesOpsForm):
    """Recebe form pré-qualificação Sales Ops Diagnostic."""
    pain_insight = SALESOPS_PAIN_INSIGHT.get(form.main_pain, "")
    # Fit calc: ticket size 5k+ AND team has at least a founder. Solo with under_5k tickets = sugerir AI Quick Win
    is_fit = form.ticket_size in ("5k_25k", "25k_100k", "100k_plus")

    if is_fit:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Olá, {form.name.split()[0]}.</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Próximo passo</p>
    <p class="text-ink/80 leading-relaxed mb-5">Em até 24h, Mila te manda email com escopo do diagnóstico adaptado pro seu funil + sugestão de horários para discovery call de 30 minutos.</p>
    <a href="/contact?offering=sales-ops-audit&practice=growth" class="btn-primary text-sm inline-block">Ou agende você mesmo agora</a>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Sales Ops Diagnostic: 2 semanas, R$ 15-25k. Entrega funnel map + automation playbook + roadmap 90 dias.</p>
</div>
""".strip()
    else:
        deliverable_html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise</p>
  <p class="h-serif text-4xl mb-6 leading-tight">Obrigada, {form.name.split()[0]}.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Com ticket abaixo de R$ 5k, diagnóstico completo de R$ 15-25k provavelmente não cobre o ROI necessário no curto prazo.</p>
  <p class="text-ink/80 leading-relaxed mb-5">Alternativas mais adequadas:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>AI Quick Win</strong> — R$ 8-15k, 2-3 semanas. Implementação de 1 automação alto-impacto (ex: WhatsApp auto-qualifier, proposal generator).</li>
    <li>• <strong>Anuvia AI Ops Subscription</strong> — R$ 3-8k/mês. Squad de agentes recorrente sem investimento inicial pesado.</li>
    <li>• <strong>Office hours de 90 min com Mila</strong> — R$ 1.500. Discussão direcionada com saídas acionáveis no mesmo dia.</li>
  </ul>
  <p class="text-sm text-ink/70">Mila te manda essas opções por email em até 24h.</p>
</div>
""".strip()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            lead_payload = {
                "funnel_id": "BR_GROWTH",
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "source": "lp_growth_sales_ops",
                "meta": {
                    "role": form.role,
                    "team_size": form.team_size,
                    "ticket_size": form.ticket_size,
                    "main_pain": form.main_pain,
                    "is_fit": is_fit,
                    "context": form.context,
                },
                "tags": ["lp_growth_sales_ops", "br_growth"],
            }
            r = await client.post(f"{SUPA_URL}/leads", headers=SUPA_HEADERS, json=lead_payload)
            if r.status_code not in (200, 201):
                log.warning("supabase_lead_insert non-200: %s", r.status_code)
        except Exception:
            log.exception("growth_sales_ops lead_insert failed")

        try:
            if RESEND_API_KEY:
                first = form.name.split()[0]
                subject = f"Sales Ops Diagnostic — pré-análise pra {first}"
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Growth</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {first}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://anuvia.com.br/contact?offering=sales-ops-audit&practice=growth" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p><p style="color:#78716c;font-size:13px;margin-top:32px;">Mila Vernazza · Founder Anuvia<br>Ex-AWS · Ex-Google · 15+ AWS Certifications</p></div></body></html>"""
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": subject,
                        "html": email_html,
                        "tags": [{"name": "category", "value": "growth_sales_ops_signup"}],
                    },
                    timeout=20,
                )
        except Exception:
            log.exception("growth_sales_ops_email_failed")

    return JSONResponse({"ok": True, "html": deliverable_html})


async def send_diagnostic_email(
    client: httpx.AsyncClient,
    form: "DiagnosticForm",
    diagnostic: dict,
    deliverable_html: str,
    funnel: str,
) -> Optional[str]:
    """Envia o deliverable do diagnóstico por email via Resend. Não-fatal."""
    if not RESEND_API_KEY:
        log.info("send_diagnostic_email: RESEND_API_KEY missing, skipping")
        return None

    is_eng = funnel == "BR_ENG"
    subject = (
        f"Seu AI Readiness Assessment — {form.name.split()[0] if form.name else 'Anuvia'}"
        if is_eng else
        f"Seu diagnóstico Anuvia — {form.name.split()[0] if form.name else 'pronto'}"
    )
    # CTA back to booking + LP
    cta_url = "https://roadmap.anuvia.com.br" if is_eng else "https://diagnostico.anuvia.com.br"
    # Build full email HTML with inline-friendly styling wrapping the deliverable
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject}</title>
<style>
  body {{ margin: 0; padding: 0; background: #fafaf9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; color: #0f172a; }}
  .wrap {{ max-width: 640px; margin: 0 auto; padding: 32px 24px; }}
  .h-serif {{ font-family: Georgia, "Times New Roman", serif; font-weight: 600; letter-spacing: -0.02em; line-height: 1.15; }}
  .eyebrow {{ font-size: 11px; font-weight: 500; letter-spacing: 0.18em; text-transform: uppercase; color: #64748b; }}
  .card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 4px; padding: 24px; }}
  .rule {{ border-top: 1px solid #e2e8f0; margin: 24px 0; }}
  a {{ color: #0f172a; }}
  .button {{ display: inline-block; background: #0f172a; color: #ffffff !important; padding: 12px 22px; border-radius: 3px; text-decoration: none; font-weight: 500; font-size: 14px; }}
  p {{ line-height: 1.65; color: #1e293b; }}
</style>
</head><body>
<div class="wrap">
  <p class="eyebrow" style="margin: 0 0 8px 0;">Anuvia</p>
  <p class="h-serif" style="font-size: 32px; margin: 0 0 8px 0;">Olá, {form.name.split()[0] if form.name else ''}</p>
  <p style="color: #475569; margin: 0 0 32px 0;">Aqui está o diagnóstico que geramos pro seu negócio. Foi montado em tempo real a partir das suas respostas.</p>

  <div class="card">
    {deliverable_html}
  </div>

  <div class="rule"></div>

  <p style="margin: 0 0 16px 0;">Próximo passo natural: <strong>discovery call de 30 min</strong> pra revisar o diagnóstico junto e priorizar os próximos passos pra sua operação.</p>
  <p style="margin: 0 0 24px 0;"><a class="button" href="{cta_url}">Agendar discovery call</a></p>

  <p style="color: #64748b; font-size: 13px; margin: 32px 0 0 0;">
    Anuvia — IA aplicada a vendas e operações.<br>
    Se este email caiu por engano, basta ignorar.<br>
    <a href="https://anuvia.com.br" style="color: #64748b;">anuvia.com.br</a>
  </p>
</div>
</body></html>"""

    body = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [form.email],
        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "subject": subject,
        "html": full_html,
        "tags": [
            {"name": "category", "value": "diagnostic_deliverable"},
            {"name": "funnel", "value": funnel.lower()},
        ],
    }
    try:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=20,
        )
        if r.status_code in (200, 202):
            data = r.json() if r.text else {}
            msg_id = data.get("id", "")
            log.info("resend_sent to=%s funnel=%s msg_id=%s", form.email, funnel, msg_id)
            return msg_id or "sent"
        log.error("resend_failed status=%s body=%s", r.status_code, r.text[:300])
        return None
    except Exception:
        log.exception("resend_exception")
        return None


@app.post("/api/diagnose")
async def diagnose(payload: DiagnosticForm, request: Request) -> JSONResponse:
    """Receive form, call Claude, insert lead, return rendered deliverable."""
    funnel = detect_funnel(request)
    try:
        diagnostic = await call_claude_diagnostic(payload, funnel)
    except httpx.HTTPStatusError as e:
        log.error("claude call failed: %s %s", e.response.status_code, e.response.text[:200])
        raise HTTPException(status_code=502, detail="Falha ao gerar diagnóstico (LLM)")
    except (json.JSONDecodeError, KeyError, IndexError):
        log.exception("claude returned malformed response")
        raise HTTPException(status_code=502, detail="Diagnóstico retornou em formato inválido")

    # Render the deliverable as HTML (used both on-screen and in email)
    deliverable_html = render_deliverable(payload, diagnostic, None, funnel)

    async with httpx.AsyncClient(timeout=30) as client:
        lead_id = await insert_lead(client, payload, diagnostic, funnel)
        await fire_slack_notification(client, payload, diagnostic, lead_id)
        email_msg_id = await send_diagnostic_email(client, payload, diagnostic, deliverable_html, funnel)

    # Re-render with the lead_id for accurate on-screen display
    deliverable_html = render_deliverable(payload, diagnostic, lead_id, funnel)

    return JSONResponse({
        "ok": True,
        "lead_id": lead_id,
        "diagnostic": diagnostic,
        "deliverable_html": deliverable_html,
        "email_sent": bool(email_msg_id),
    })


def render_deliverable(
    form: DiagnosticForm, diag: dict, lead_id: Optional[str], funnel: str = "BR_SMB"
) -> str:
    """Build the HTML block shown to the user after submit.

    Editorial light theme — no emojis, serif headlines, generous whitespace.
    """
    plano = diag.get("plano") or diag.get("plano_30_dias") or []
    plano_html = "".join(
        f'''
        <div class="mb-7">
          <p class="eyebrow mb-1.5">{p.get("etapa") or ("Semana " + str(p.get("semana", "?")))}</p>
          <p class="text-slate-900 leading-relaxed mb-1">{p.get("acao", "")}</p>
          <p class="text-sm text-slate-500 leading-relaxed">{p.get("porque", "")}</p>
        </div>
        '''
        for p in plano
    )
    fortes = diag.get("pontos_fortes", [])
    fracos = diag.get("pontos_fracos", [])
    fortes_html = "".join(f"<li>{f}</li>" for f in fortes)
    fracos_html = "".join(f"<li>{f}</li>" for f in fracos)

    resumo_html = md_lib.markdown(diag.get("diagnostico_resumo", ""))

    score = diag.get("score_maturidade", 0)
    estimativa = diag.get("estimativa_perdida", "")
    proximo = diag.get("proximo_passo", "")
    first_name = form.name.split()[0]

    # Funnel-aware section labels
    if funnel == "BR_ENG":
        score_label = "Maturidade de IA"
        estimativa_label = "Valor potencial não capturado"
        plano_label = "Roadmap sugerido — próximos 90 dias"
        fracos_label = "Gaps técnicos"
    else:  # BR_SMB
        score_label = "Maturidade do funil comercial"
        estimativa_label = "Estimativa de oportunidade não capturada"
        plano_label = "Plano sugerido — próximos 30 dias"
        fracos_label = "Pontos de atenção"

    booking_html = (
        f'<div id="booking-widget" data-lead-id="{lead_id or ""}"></div>'
        if lead_id
        else (
            '<p class="text-sm text-slate-500 text-center">'
            "Não conseguimos preparar o agendamento agora. Vou te chamar pelo WhatsApp pra alinhar.</p>"
        )
    )

    return f"""
<article class="prose-anuvia">
  <header class="text-center mb-12">
    <p class="eyebrow mb-4">Análise personalizada · {first_name}</p>
    <p class="h-serif text-7xl md:text-8xl mb-2 leading-none">{score}<span class="text-3xl md:text-4xl text-slate-400 align-top">/100</span></p>
    <p class="text-sm text-slate-500 tracking-wide">{score_label}</p>
  </header>

  <div class="rule"></div>

  <section class="mb-10">
    <p class="eyebrow mb-4">Análise</p>
    <div class="text-slate-800 leading-[1.75] text-lg">{resumo_html}</div>
  </section>

  <div class="rule"></div>

  <section class="mb-10">
    <p class="eyebrow mb-4">{estimativa_label}</p>
    <p class="h-serif text-2xl md:text-3xl text-slate-900 leading-snug">{estimativa}</p>
  </section>

  <div class="rule"></div>

  <section class="grid md:grid-cols-2 gap-10 mb-10">
    <div>
      <p class="eyebrow mb-4">Pontos fortes</p>
      <ul class="text-slate-700">{fortes_html}</ul>
    </div>
    <div>
      <p class="eyebrow mb-4">{fracos_label}</p>
      <ul class="text-slate-700">{fracos_html}</ul>
    </div>
  </section>

  <div class="rule"></div>

  <section class="mb-10">
    <p class="eyebrow mb-5">{plano_label}</p>
    {plano_html}
  </section>

  <div class="rule"></div>

  <section class="card p-8 md:p-10 mt-10">
    <p class="eyebrow text-center mb-5">Próximo passo</p>
    <p class="h-serif text-xl md:text-2xl text-slate-900 leading-snug text-center mb-8 max-w-2xl mx-auto">{proximo}</p>
    {booking_html}
  </section>
</article>
"""
