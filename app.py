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
import base64
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anuvia-lp")

app = FastAPI(title="Anuvia Landing Pages", version="0.1.0")
templates = Jinja2Templates(directory="templates")

# Serve /static/* (compiled Tailwind CSS, sample PDFs, etc.) from the local ./static folder.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ---------------------------------------------------------------------------
# Autonomous funnel v1 — unified lead memory + orchestrator + Track B handlers
# (see ARCHITECTURE_AUTONOMOUS_v1.md). Each module exposes an APIRouter.
# Importing lib.track_b triggers handler registration via @register decorators.
# ---------------------------------------------------------------------------
try:
    from lib.sessions import router as _sessions_router, session_update, session_set_next  # noqa: E402
    from lib.orchestrator import router as _orchestrator_router, tick as _orchestrator_tick  # noqa: E402
    from lib.track_b import router as _track_b_router  # noqa: E402,F401
    import lib.track_b  # noqa: E402,F401 — registers handlers as side-effect
    app.include_router(_sessions_router)
    app.include_router(_orchestrator_router)
    app.include_router(_track_b_router)
    _AUTONOMOUS_FUNNEL_ENABLED = True
    log.info("Autonomous funnel v1 mounted: /api/session, /api/orchestrator, /api/track-b")

    # In-process scheduler — runs orchestrator.tick() every 10 minutes.
    # Disable by setting ORCHESTRATOR_SCHEDULER_ENABLED=0 (e.g., when running
    # behind multiple uvicorn workers and you'd rather use n8n as the cron).
    _SCHEDULER_INTERVAL_S = int(os.environ.get("ORCHESTRATOR_SCHEDULER_INTERVAL_S", "600"))
    _SCHEDULER_ENABLED = os.environ.get("ORCHESTRATOR_SCHEDULER_ENABLED", "1") == "1"

    @app.on_event("startup")
    async def _start_orchestrator_loop():
        if not _SCHEDULER_ENABLED:
            log.info("orchestrator scheduler disabled by env")
            return
        import asyncio as _asyncio
        async def _loop():
            # Initial delay so worker boots before first run.
            await _asyncio.sleep(30)
            while True:
                try:
                    summary = await _orchestrator_tick(limit=100)
                    if summary.get("processed"):
                        log.info("orchestrator scheduled tick: %s", summary)
                except Exception:
                    log.exception("orchestrator scheduled tick crashed")
                await _asyncio.sleep(_SCHEDULER_INTERVAL_S)
        _asyncio.create_task(_loop())
        log.info("orchestrator scheduler started: every %ss", _SCHEDULER_INTERVAL_S)

    # Daily E2E smoke test — runs every day at 07:00 America/Sao_Paulo.
    # Validates the full funnel (analyze → contact → booking) and alerts Slack on fail.
    _SMOKE_ENABLED = os.environ.get("SMOKE_DAILY_ENABLED", "1") == "1"
    _SMOKE_HOUR_BRT = int(os.environ.get("SMOKE_DAILY_HOUR_BRT", "7"))

    @app.on_event("startup")
    async def _start_daily_smoke_loop():
        if not _SMOKE_ENABLED:
            log.info("daily smoke disabled by env")
            return
        import asyncio as _asyncio
        import subprocess as _sp
        async def _smoke_loop():
            last_run_date = None
            await _asyncio.sleep(60)  # let app fully boot
            while True:
                try:
                    now_brt = datetime.now(ZoneInfo("America/Sao_Paulo"))
                    today = now_brt.date()
                    if now_brt.hour == _SMOKE_HOUR_BRT and last_run_date != today:
                        log.info("daily smoke firing at %s BRT", now_brt.isoformat())
                        script = os.path.join(os.path.dirname(__file__), "scripts", "smoke_e2e.py")
                        if os.path.exists(script):
                            proc = await _asyncio.create_subprocess_exec(
                                "python", script,
                                "--base-url", "https://anuvia.com.br",
                                "--track", "discovery",
                                stdout=_sp.PIPE, stderr=_sp.PIPE,
                                env={**os.environ, "SUPABASE_URL": SUPA_URL, "SUPABASE_KEY": SUPA_KEY,
                                     "ORCHESTRATOR_SECRET": os.environ.get("ORCHESTRATOR_SECRET", "")},
                            )
                            out, err = await proc.communicate()
                            log.info("daily smoke exit=%s out=%s err=%s",
                                     proc.returncode, out.decode()[-400:], err.decode()[-400:])
                        last_run_date = today
                except Exception:
                    log.exception("daily smoke crashed")
                await _asyncio.sleep(60)  # check every minute
        _asyncio.create_task(_smoke_loop())
        log.info("daily smoke scheduler started: %02d:00 BRT", _SMOKE_HOUR_BRT)
except Exception as _e:  # pragma: no cover — defensive boot
    _AUTONOMOUS_FUNNEL_ENABLED = False
    log.warning("Autonomous funnel v1 NOT mounted (%s). Site still serves OK.", _e)

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
    meta["author"] = meta.get("author") or "Anuvia"
    meta["tags"] = meta.get("tags") or []
    meta["cover_image"] = meta.get("cover_image") or meta.get("cover") or ""
    # Language frontmatter — defaults to PT for backward compatibility with
    # existing posts. New posts should set `lang: pt` or `lang: en` explicitly.
    raw_lang = str(meta.get("lang", "pt")).lower().strip()
    meta["lang"] = "en" if raw_lang in ("en", "en-us", "en_us", "english") else "pt"
    return meta


def _list_posts(lang_filter: Optional[str] = None) -> list[dict]:
    """List all posts with metadata, sorted by date desc.
    If lang_filter is set ('pt' or 'en'), only return posts of that language."""
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
            meta = _post_meta(slug, post)
            if lang_filter and meta.get("lang") != lang_filter:
                continue
            posts.append(meta)
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
    loc = get_locale(request)
    lang = loc["lang"]
    posts = _list_posts(lang_filter=lang)
    ctx = tpl_ctx(request, posts=posts, blog_base_url=BLOG_BASE_URL)
    return templates.TemplateResponse("blog_index.html", ctx)


@app.get("/blog/feed.xml")
async def blog_feed(request: Request) -> Response:
    loc = get_locale(request)
    posts = _list_posts(lang_filter=loc["lang"])[:20]
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
    rss_lang = "en-US" if loc["lang"] == "en" else "pt-BR"
    rss_desc = (
        "Anuvia blog — applied engineering for cloud, AI and platform teams."
        if loc["lang"] == "en"
        else "Anuvia — IA aplicada a vendas e ops. Insights e estudos de caso."
    )
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Anuvia — Blog</title>
    <link>{BLOG_BASE_URL}/blog</link>
    <description>{rss_desc}</description>
    <language>{rss_lang}</language>
{chr(10).join(items_xml)}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post(request: Request, slug: str) -> HTMLResponse:
    post = _load_post(slug)
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")
    # Host-language strict: a PT post on anuvia.net (EN) is hidden — redirect to /blog index.
    loc = get_locale(request)
    if post.get("lang") and post["lang"] != loc["lang"]:
        return RedirectResponse(url="/blog", status_code=302)
    ctx = tpl_ctx(request, post=post, blog_base_url=BLOG_BASE_URL)
    return templates.TemplateResponse("blog_post.html", ctx)


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


# ----------------------------------------------------------------------------
# Gcal event creation for discovery bookings
#
# Two events are created on Mila's primary calendar for every booking:
#   A) PUBLIC: client is invited as attendee, auto-Meet conference, short
#      client-facing description. This is what the lead sees.
#   B) PRIVATE: same start/end, no attendees, no conference, FULL pre-call brief
#      (lead_id, qualification_data, role, spend, pains, contexto). visibility
#      'private' so it never leaks even if the calendar is shared.
#
# Both events live on the SAME calendar (mila@anuvia.com.br primary) — Mila
# sees A on her calendar with the client attached, and B as a quiet private
# entry with all the operational metadata she needs to prep.
#
# Persisted onto leads.qualification_data.gcal:
#   { public_event_id, private_event_id, meet_url, html_link }
# The booking confirmation email reads qualification_data.meet_url and
# artifacts.gcal.meet_url — we write to qualification_data.meet_url as the
# canonical path (mirrored inside qualification_data.gcal for traceability).
# ----------------------------------------------------------------------------


# Map funnel_id → human-readable diagnostic label (single source of truth).
# Used in event titles ("Anuvia · Discovery · {label}" / "Brief · {label} · ...")
# and in client-facing copy. Bilingual variants kept minimal — labels stay in
# English by design (they're product names: "FinOps Audit", "DevOps Maturity").
_DIAG_LABELS: dict[str, dict[str, str]] = {
    "BR_FINOPS":     {"pt": "FinOps Audit",         "en": "FinOps Audit"},
    "US_FINOPS":     {"pt": "FinOps Audit",         "en": "FinOps Audit"},
    "BR_DEVOPS":     {"pt": "DevOps Maturity",      "en": "DevOps Maturity"},
    "BR_AI":         {"pt": "AI Readiness",         "en": "AI Readiness"},
    "BR_GROWTH":     {"pt": "Growth Sales Ops",     "en": "Growth Sales Ops"},
    "BR_INDUSTRY":   {"pt": "Industry Assessment",  "en": "Industry Assessment"},
    "BR_AWS_WA":     {"pt": "AWS Well-Architected", "en": "AWS Well-Architected"},
    "BR_AWS_MIG":    {"pt": "AWS Migration Plan",   "en": "AWS Migration Plan"},
    "BR_AWS_COST":   {"pt": "AWS Cost Review",      "en": "AWS Cost Review"},
    "BR_GCP_WA":     {"pt": "GCP Well-Architected", "en": "GCP Well-Architected"},
    "BR_GCP_MIG":    {"pt": "GCP Migration Plan",   "en": "GCP Migration Plan"},
    "BR_GCP_COST":   {"pt": "GCP Cost Review",      "en": "GCP Cost Review"},
    "BR_SMB":        {"pt": "Growth Sales Ops",     "en": "Growth Sales Ops"},
    "BR_ENG":        {"pt": "AI Readiness",         "en": "AI Readiness"},
    "BR_BRAND":      {"pt": "Discovery",            "en": "Discovery"},
}


def _diag_label_for_funnel(funnel_id: Optional[str], lang: str = "pt") -> str:
    """Map funnel_id → human label for event titles.

    Falls back to title-casing the suffix after the BR_/US_ prefix when the
    funnel_id isn't in the explicit map (e.g. a new BR_AWS_* sub-LP added
    without updating this file still gets a reasonable label).
    """
    fid = (funnel_id or "").strip().upper()
    lang_key = "en" if (lang or "pt").lower().startswith("en") else "pt"
    if fid in _DIAG_LABELS:
        return _DIAG_LABELS[fid].get(lang_key) or _DIAG_LABELS[fid]["pt"]
    # Fallback: strip BR_/US_ prefix and title-case the remainder.
    suffix = re.sub(r"^(?:BR|US)_", "", fid)
    if not suffix:
        return "Discovery"
    # Convert underscores to spaces, title-case each word
    return suffix.replace("_", " ").title()


def _build_client_facing_description(lead_name: str, diag_label: str, lang: str = "pt") -> str:
    """Short, friendly description for Event A (public). NO operational metadata."""
    first = (lead_name or "").strip().split()[0] if lead_name else ""
    if (lang or "pt").lower().startswith("en"):
        return (
            f"30-minute conversation with a Solutions Architect from Anuvia"
            f"{' — ' + diag_label if diag_label else ''}.\n\n"
            f"We'll review your diagnostic and map a practical next step.\n\n"
            f"The Google Meet link is on this invite."
        )
    return (
        f"Conversa de 30 min com um Solutions Architect da Anuvia"
        f"{' — ' + diag_label if diag_label else ''}.\n\n"
        f"Vamos revisar seu diagnóstico e mapear o próximo passo prático.\n\n"
        f"O link do Google Meet está neste convite."
    )


def _build_brief_description(lead: dict, form_extras: Optional[dict] = None) -> str:
    """Rich pre-call brief for Event B (private). Includes everything Mila needs
    to prep: lead_id, qualification_data, contact info, contexto compartilhado.

    Reuses build_pre_call_brief for the structured section and appends any
    extras passed in (e.g. /contact widget context/offering/practice that may
    not live in qualification_data yet)."""
    brief = build_pre_call_brief(lead or {})
    extras = form_extras or {}
    extra_lines: list[str] = []
    if extras.get("offering") or extras.get("practice"):
        extra_lines.append("")
        extra_lines.append(
            f"Oferta de interesse: {extras.get('offering') or '(n/a)'} "
            f"(prática: {extras.get('practice') or '(n/a)'})"
        )
    ctx = (extras.get("context") or "").strip()
    if ctx:
        extra_lines.append("")
        extra_lines.append("Contexto compartilhado (form):")
        extra_lines.append(ctx)
    lid = (lead or {}).get("id")
    if lid:
        extra_lines.append("")
        extra_lines.append(f"Supabase lead_id: {lid}")
    src = extras.get("source")
    if src:
        extra_lines.append(f"Source: {src}")
    fid = (lead or {}).get("funnel_id") or extras.get("funnel_id")
    if fid:
        extra_lines.append(f"Funnel: {fid}")
    if extra_lines:
        return brief + "\n" + "\n".join(extra_lines)
    return brief


async def _pick_primary_gcal_account(client: httpx.AsyncClient) -> Optional[dict]:
    """Return the first active Google account from admin_gcal_accounts.

    For now we use the first active account (which should be Mila's primary —
    mila@anuvia.com.br). If multiple accounts are active, we'd need explicit
    routing; that's not in scope here."""
    accounts = await _fetch_active_gcal_accounts(client)
    if not accounts:
        return None
    return accounts[0]


async def _create_gcal_event_pair_for_booking(
    client: httpx.AsyncClient,
    lead: dict,
    start_dt: datetime,
    end_dt: datetime,
    funnel_id: str,
    lang: str = "pt",
    form_extras: Optional[dict] = None,
) -> dict:
    """Create Event A (public, with attendee + Meet) and Event B (private brief).

    Returns: {
        "public_event_id": str|None,
        "private_event_id": str|None,
        "meet_url": str|None,
        "html_link": str|None,
        "calendar_id": str|None,
        "error": str|None,
    }

    Best-effort: never raises. On full failure returns a dict with "error" set
    and ids as None — the caller continues so booking confirmation still goes
    out (the .ics attachment carries enough info on its own)."""
    result: dict = {
        "public_event_id": None,
        "private_event_id": None,
        "meet_url": None,
        "html_link": None,
        "calendar_id": None,
        "error": None,
    }
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        result["error"] = "google_oauth_not_configured"
        return result

    account = await _pick_primary_gcal_account(client)
    if not account:
        result["error"] = "no_active_gcal_account"
        return result

    email = account.get("email")
    refresh_token = account.get("refresh_token")
    cal_id = account.get("calendar_id") or "primary"
    if not (email and refresh_token):
        result["error"] = "gcal_account_missing_credentials"
        return result

    token = await _get_cached_access_token(client, email, refresh_token)
    if not token:
        result["error"] = "gcal_token_exchange_failed"
        return result

    import urllib.parse as _up
    cal_url = f"https://www.googleapis.com/calendar/v3/calendars/{_up.quote(cal_id)}/events"

    diag_label = _diag_label_for_funnel(funnel_id, lang)
    lead_name = (lead or {}).get("name") or "Lead"
    lead_email = (lead or {}).get("email") or (form_extras or {}).get("email") or ""

    # --- Event A: public, client-facing, Meet link, with attendee ---
    public_summary = f"Anuvia · Discovery · {diag_label}"
    public_description = _build_client_facing_description(lead_name, diag_label, lang)
    start_iso = start_dt.astimezone(TZ_SP).isoformat()
    end_iso = end_dt.astimezone(TZ_SP).isoformat()

    public_body: dict = {
        "summary": public_summary,
        "description": public_description,
        "start": {"dateTime": start_iso, "timeZone": "America/Sao_Paulo"},
        "end":   {"dateTime": end_iso,   "timeZone": "America/Sao_Paulo"},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {"useDefault": True},
        "guestsCanModify": False,
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
    }
    if lead_email:
        public_body["attendees"] = [{
            "email": lead_email,
            "displayName": lead_name,
            "responseStatus": "needsAction",
        }]

    try:
        ra = await client.post(
            cal_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={
                "conferenceDataVersion": "1",
                # Send invite email so the lead gets an Accept/Decline.
                "sendUpdates": "all" if lead_email else "none",
            },
            json=public_body,
            timeout=15,
        )
        if ra.status_code in (200, 201):
            ev_a = ra.json()
            result["public_event_id"] = ev_a.get("id")
            result["html_link"] = ev_a.get("htmlLink")
            result["calendar_id"] = cal_id
            # hangoutLink is the canonical Meet URL; entryPoints carries fuller info.
            meet_url = ev_a.get("hangoutLink")
            if not meet_url:
                # Fall back to entryPoints[*].uri where type == "video"
                for ep in (ev_a.get("conferenceData") or {}).get("entryPoints") or []:
                    if ep.get("entryPointType") == "video" and ep.get("uri"):
                        meet_url = ep["uri"]
                        break
            result["meet_url"] = meet_url
        else:
            result["error"] = f"event_a_insert_{ra.status_code}"
            log.warning("gcal event A insert non-2xx: %s %s", ra.status_code, ra.text[:300])
            return result
    except Exception:
        log.exception("gcal event A insert exception")
        result["error"] = "event_a_insert_exception"
        return result

    # --- Event B: private brief, no attendees, no conference ---
    private_summary = f"Brief · {diag_label} · {lead_name}"
    private_description = _build_brief_description(lead, form_extras=form_extras)
    if result.get("meet_url"):
        # Cross-link so Mila can jump from the brief to the Meet.
        private_description = (
            private_description
            + f"\n\n—\nMeet link (Event A): {result['meet_url']}"
            + (f"\nEvent A: {result.get('html_link')}" if result.get("html_link") else "")
        )

    private_body = {
        "summary": private_summary,
        "description": private_description,
        "start": {"dateTime": start_iso, "timeZone": "America/Sao_Paulo"},
        "end":   {"dateTime": end_iso,   "timeZone": "America/Sao_Paulo"},
        "visibility": "private",
        "transparency": "opaque",
        "reminders": {"useDefault": False},
    }
    try:
        rb = await client.post(
            cal_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"sendUpdates": "none"},
            json=private_body,
            timeout=15,
        )
        if rb.status_code in (200, 201):
            ev_b = rb.json()
            result["private_event_id"] = ev_b.get("id")
        else:
            log.warning("gcal event B insert non-2xx: %s %s", rb.status_code, rb.text[:300])
            # Don't blow away the successful A — just note partial failure.
            result["error"] = (result.get("error") or "") + f" event_b_insert_{rb.status_code}".strip()
    except Exception:
        log.exception("gcal event B insert exception")
        result["error"] = (result.get("error") or "") + " event_b_insert_exception"

    return result


async def _persist_gcal_on_lead(
    client: httpx.AsyncClient, lead_id: str, gcal_result: dict
) -> None:
    """Merge the gcal result into qualification_data.gcal (and mirror meet_url
    at qualification_data.meet_url, which is what the email helper reads first)."""
    if not lead_id or not gcal_result:
        return
    try:
        lr = await client.get(
            f"{SUPA_URL}/leads?id=eq.{lead_id}&select=qualification_data",
            headers=SUPA_HEADERS,
        )
        if lr.status_code != 200:
            return
        rows = lr.json() or []
        if not rows:
            return
        qd = rows[0].get("qualification_data") or {}
        gcal_payload = {
            "public_event_id": gcal_result.get("public_event_id"),
            "private_event_id": gcal_result.get("private_event_id"),
            "meet_url": gcal_result.get("meet_url"),
            "html_link": gcal_result.get("html_link"),
            "calendar_id": gcal_result.get("calendar_id"),
            "error": gcal_result.get("error"),  # diagnostic — exposes which step failed
        }
        qd["gcal"] = gcal_payload
        if gcal_result.get("meet_url"):
            qd["meet_url"] = gcal_result["meet_url"]
        pr = await client.patch(
            f"{SUPA_URL}/leads?id=eq.{lead_id}",
            headers=SUPA_HEADERS,
            json={"qualification_data": qd},
        )
        if pr.status_code not in (200, 204):
            log.warning("persist_gcal patch non-2xx: %s %s", pr.status_code, pr.text[:200])
    except Exception:
        log.exception("persist_gcal failed (non-fatal)")


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

        # 4b. Create Gcal event pair on Mila's calendar (best-effort).
        # Event A: public, with attendee + Meet. Event B: private brief.
        try:
            lead_funnel = lead.get("funnel_id") or funnel
            gcal_result = await _create_gcal_event_pair_for_booking(
                client,
                lead=lead,
                start_dt=start_dt,
                end_dt=end_dt,
                funnel_id=lead_funnel,
                lang=("en" if (lead.get("language") or "").lower().startswith("en") else "pt"),
                form_extras={
                    "source": lead.get("source"),
                    "funnel_id": lead_funnel,
                },
            )
            await _persist_gcal_on_lead(client, payload.lead_id, gcal_result)
        except Exception:
            log.exception("api_book gcal event creation failed (non-fatal)")

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
    Usado pelo widget de booking embedded no site brand (sem funnel-specific LP).

    Cookie-aware: if `anuvia_lead_id` cookie is set and resolves to an existing
    lead row, we MERGE the booking into that row (PATCH) instead of creating a
    new one. This prevents asking returning leads for their PII a second time
    after they've already submitted /api/<diag>/contact. Direct visitors to
    /contact with no cookie keep the original insert path."""
    funnel = "BR_BRAND"
    service_id = int(os.environ.get("EASYAPPOINTMENTS_SERVICE_ID_BR_SMB", "2"))
    duration_min = 30

    # Cookie-driven: only trust the cookie if the lead row actually exists.
    cookie_lead_id = request.cookies.get(LEAD_ID_COOKIE) or ""

    async with httpx.AsyncClient(timeout=30) as client:
        existing_lead_id: Optional[str] = None
        if cookie_lead_id:
            try:
                lr = await client.get(
                    f"{SUPA_URL}/leads?id=eq.{cookie_lead_id}&select=id,name,email,phone_e164,company,qualification_data,tags",
                    headers=SUPA_HEADERS,
                )
                if lr.status_code == 200:
                    rows = lr.json() or []
                    if rows:
                        existing_lead_id = rows[0].get("id")
                        existing_row = rows[0]
            except Exception:
                log.exception("contact_book cookie-lead lookup failed (non-fatal)")

        lead_id: Optional[str] = None

        if existing_lead_id:
            # MERGE path — PATCH existing lead, do not insert.
            lead_id = existing_lead_id
            existing_qd = existing_row.get("qualification_data") or {}
            existing_tags = existing_row.get("tags") or []
            existing_qd["booking_via"] = "contact_widget"
            if form.context:
                existing_qd["context"] = form.context
            if form.offering:
                existing_qd["offering"] = form.offering
            if form.practice:
                existing_qd["practice"] = form.practice
            new_tags = list(dict.fromkeys(existing_tags + ["contact_widget"]))
            if form.practice and f"practice:{form.practice}" not in new_tags:
                new_tags.append(f"practice:{form.practice}")
            if form.offering and f"offering:{form.offering}" not in new_tags:
                new_tags.append(f"offering:{form.offering}")
            # Only overwrite PII fields if the existing row is missing them;
            # we never want to clobber a known good name/email/phone with a
            # weaker value submitted later.
            patch_payload: dict = {
                "current_stage": "qualified",
                "qualification_data": existing_qd,
                "tags": new_tags,
            }
            if not (existing_row.get("name") or "").strip():
                patch_payload["name"] = form.name
            if not (existing_row.get("email") or "").strip():
                patch_payload["email"] = form.email
            if not (existing_row.get("phone_e164") or "").strip():
                patch_payload["phone_e164"] = form.whatsapp
            if form.company and not (existing_row.get("company") or "").strip():
                patch_payload["company"] = form.company
            try:
                pr = await client.patch(
                    f"{SUPA_URL}/leads?id=eq.{lead_id}",
                    headers=SUPA_HEADERS,
                    json=patch_payload,
                )
                if pr.status_code not in (200, 204):
                    log.warning("contact_book lead_patch non-2xx: %s %s", pr.status_code, pr.text[:200])
            except Exception:
                log.exception("contact_book lead_patch failed")
        else:
            # INSERT path — direct /contact visitor with no prior lead row.
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

        # 4b. Create Gcal event pair on Mila's calendar (best-effort).
        #     Event A: public + Meet + client as attendee.
        #     Event B: private brief (no attendees, no Meet).
        # Diagnostic label is derived from form.practice (brand widget hint)
        # falling back to the lead's funnel_id. /contact lands on BR_BRAND
        # by default; the practice attr is how the widget tags FinOps/DevOps/AI.
        practice_to_funnel = {
            "finops":   "BR_FINOPS",
            "devops":   "BR_DEVOPS",
            "ai":       "BR_AI",
            "growth":   "BR_GROWTH",
            "industry": "BR_INDUSTRY",
            "aws":      "BR_AWS_WA",
            "gcp":      "BR_GCP_WA",
        }
        practice_key = (form.practice or "").strip().lower()
        diag_funnel = practice_to_funnel.get(practice_key, funnel)
        try:
            # Build a minimal lead-shaped dict so the brief builder works even
            # on the INSERT path (where we haven't refetched the inserted row).
            # qd/existing_qd live in mutually-exclusive branches above; pull
            # from whichever exists in the local namespace.
            _qd_for_brief = (
                locals().get("existing_qd")
                if existing_lead_id
                else locals().get("qd")
            ) or {}
            lead_for_brief = {
                "id": lead_id,
                "name": form.name,
                "email": form.email,
                "phone_e164": form.whatsapp,
                "company": form.company,
                "funnel_id": diag_funnel,
                "qualification_data": _qd_for_brief,
            }
            gcal_result = await _create_gcal_event_pair_for_booking(
                client,
                lead=lead_for_brief,
                start_dt=start_dt,
                end_dt=end_dt,
                funnel_id=diag_funnel,
                lang="pt",
                form_extras={
                    "context": form.context,
                    "offering": form.offering,
                    "practice": form.practice,
                    "source": form.source,
                    "funnel_id": diag_funnel,
                },
            )
            if lead_id:
                await _persist_gcal_on_lead(client, lead_id, gcal_result)
        except Exception:
            log.exception("contact_book gcal event creation failed (non-fatal)")

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
        # Includes an .ics attachment so the lead can accept the meeting into
        # their own calendar (Apple/Outlook/Gmail render Accept/Decline buttons)
        # plus an explicit "Como entrar na conversa" section pointing at the
        # Meet link. The Meet URL is created by the Gcal event creation step
        # (handled by another agent) — if it's not available here yet, we tell
        # the lead the link is in the calendar invite and let the .ics carry it.
        if RESEND_API_KEY:
            try:
                pt_weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
                pt_months = [
                    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
                    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
                ]
                pretty = f"{pt_weekdays[start_dt.weekday()]}, {start_dt.day} de {pt_months[start_dt.month - 1]} às {form.time}"

                # Try to pull a Meet URL from the lead's artifacts/qualification_data
                # if one has already been set by the Gcal event creation step.
                meet_url = ""
                try:
                    if lead_id:
                        lr2 = await client.get(
                            f"{SUPA_URL}/leads?id=eq.{lead_id}&select=qualification_data,artifacts",
                            headers=SUPA_HEADERS,
                        )
                        if lr2.status_code == 200:
                            rows2 = lr2.json() or []
                            if rows2:
                                qd2 = rows2[0].get("qualification_data") or {}
                                arts = rows2[0].get("artifacts") or {}
                                meet_url = (
                                    qd2.get("meet_url")
                                    or qd2.get("meeting_url")
                                    or (arts.get("gcal") or {}).get("meet_url")
                                    or (arts.get("gcal") or {}).get("hangoutLink")
                                    or ""
                                )
                except Exception:
                    log.exception("contact_book meet_url lookup failed (non-fatal)")

                if meet_url:
                    join_block = (
                        '<div style="background:#fafaf9;border:1px solid #e7e5e4;padding:20px;margin:24px 0;">'
                        '<p style="font-size:11px;letter-spacing:0.15em;text-transform:uppercase;color:#78716c;margin:0 0 10px 0;font-weight:500;">Como entrar na conversa</p>'
                        f'<p style="margin:0 0 12px 0;color:#475569;line-height:1.55;font-size:14px;">No horário marcado, entre pelo link abaixo:</p>'
                        f'<p style="margin:0;"><a href="{meet_url}" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:10px 18px;text-decoration:none;font-weight:500;font-size:14px;">Entrar na reunião</a></p>'
                        f'<p style="margin:12px 0 0 0;color:#78716c;font-size:12px;line-height:1.55;">{meet_url}</p>'
                        '</div>'
                    )
                    ics_location = meet_url
                    ics_description = (
                        f"Discovery Anuvia com {form.name}.\\n\\n"
                        f"Link da reunião: {meet_url}\\n\\n"
                        f"Duração: 30 minutos."
                    )
                else:
                    join_block = (
                        '<div style="background:#fafaf9;border:1px solid #e7e5e4;padding:20px;margin:24px 0;">'
                        '<p style="font-size:11px;letter-spacing:0.15em;text-transform:uppercase;color:#78716c;margin:0 0 10px 0;font-weight:500;">Como entrar na conversa</p>'
                        '<p style="margin:0;color:#475569;line-height:1.55;font-size:14px;">Link enviado no convite do calendário em anexo. Aceite o convite para receber a notificação automática no horário.</p>'
                        '</div>'
                    )
                    ics_location = "Online · link no convite do calendário"
                    ics_description = (
                        f"Discovery Anuvia com {form.name}.\n\n"
                        f"Duração: 30 minutos.\n"
                        f"O link da reunião será compartilhado pelo organizador."
                    )

                # Pick a practice-aware summary if available.
                diag_label = (form.practice or form.offering or "Discovery").strip() or "Discovery"
                ics_summary = f"Anuvia · Discovery · {diag_label}"

                ics_text = _build_booking_ics(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    summary=ics_summary,
                    description=ics_description,
                    location=ics_location,
                    attendee_email=form.email,
                    attendee_name=form.name,
                )
                ics_b64 = base64.b64encode(ics_text.encode("utf-8")).decode("ascii")

                footer = _anuvia_email_footer("pt")
                email_html = (
                    '<!DOCTYPE html><html><body style="background:#fafaf9;font-family:-apple-system,Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">'
                    '<div style="max-width:640px;margin:0 auto;">'
                    '<p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 8px 0;">Anuvia · Agendamento confirmado</p>'
                    f'<p style="font-family:Georgia,\'Times New Roman\',serif;font-size:32px;margin:0 0 16px 0;line-height:1.15;">Olá, {first_name}</p>'
                    f'<p style="color:#475569;line-height:1.65;">Sua conversa com um Solutions Architect da Anuvia está confirmada para <strong>{pretty}</strong> (horário de São Paulo).</p>'
                    '<p style="color:#475569;line-height:1.65;">A sessão dura 30 minutos e usamos o brief prévio do contexto que você compartilhou.</p>'
                    f'{join_block}'
                    '<p style="color:#78716c;font-size:13px;line-height:1.55;margin:24px 0 0 0;">O arquivo <strong>discovery.ics</strong> em anexo pode ser aceito direto no seu calendário (Apple, Outlook, Google).</p>'
                    f'{footer}'
                    '</div></body></html>'
                )
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "to": [form.email],
                        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
                        "subject": f"Conversa Anuvia confirmada — {pretty}",
                        "html": email_html,
                        "attachments": [
                            {
                                "filename": "discovery.ics",
                                "content": ics_b64,
                                "content_type": "text/calendar; charset=utf-8; method=REQUEST",
                            }
                        ],
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
    response = JSONResponse({
        "ok": True,
        "appointment_id": booking.get("appointment_id"),
        "pretty": pretty,
        "lead_id": lead_id,
    })
    # Per-lead booked cookie + persist lead_id for future cross-page visits.
    if lead_id:
        set_lead_id_cookie(response, lead_id)
    return set_booked_cookie(response, lead_id)


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
        "cta_post_booking": "Ler nossos cases",
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
        "cta_post_booking": "Read our cases",
        "lang_toggle_to": "PT",
        "lang_toggle_to_url": "https://anuvia.com.br",
    },
}


# Cookie set by booking endpoints (server-side) and by the booking widget JS
# (client-side) to track whether the visitor has already booked a discovery
# call. Used by templates via the `has_booked` template context flag.
#
# IMPORTANT: the booked cookie is SCOPED PER LEAD — its name is
# `anuvia_booked_<lead_id_short>` (last 8 chars of the lead UUID). The lead id
# itself lives in `anuvia_lead_id` (HttpOnly). This way a fresh visitor with a
# stale per-lead cookie still sees the booking form, and the same browser used
# by a new lead (e.g. cleared form, different campaign) doesn't get told they
# already booked.
BOOKED_COOKIE_PREFIX = "anuvia_booked_"
LEAD_ID_COOKIE = "anuvia_lead_id"
BOOKED_COOKIE_MAX_AGE = 60 * 24 * 60 * 60  # 60 days, in seconds
LEAD_ID_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days, in seconds

# Legacy global cookie name — kept ONLY as a constant for callers/tests that
# may still reference it. New code MUST NOT use this — use the per-lead helpers.
BOOKED_COOKIE = "anuvia_booked"


def _short_lead_id(lead_id: str) -> str:
    """Last 8 chars of a lead uuid — used to scope the booked cookie per lead."""
    s = (lead_id or "").replace("-", "")
    return s[-8:] if len(s) >= 8 else s


def set_booked_cookie(response: JSONResponse, lead_id: Optional[str] = None) -> JSONResponse:
    """Attach the per-lead `anuvia_booked_<short>=1` cookie to a JSONResponse.
    httponly=False so JS on the client can also detect the booking state
    (used for the diag-flow booking widget reveal).

    If no lead_id is provided, this is a no-op — we never want to set a
    global booked cookie because it leaks across leads."""
    if not lead_id:
        return response
    response.set_cookie(
        key=BOOKED_COOKIE_PREFIX + _short_lead_id(lead_id),
        value="1",
        max_age=BOOKED_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=False,
    )
    return response


def set_lead_id_cookie(response: JSONResponse, lead_id: str) -> JSONResponse:
    """Attach the `anuvia_lead_id` cookie (HttpOnly, 7d, SameSite=Lax) so
    subsequent booking submissions can be merged into the existing lead row
    instead of creating a fresh one."""
    if not lead_id:
        return response
    response.set_cookie(
        key=LEAD_ID_COOKIE,
        value=lead_id,
        max_age=LEAD_ID_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=True,
    )
    return response


def tpl_ctx(request: Request, **extra) -> dict:
    """Build the standard template context with locale, t (translations), currency.

    Also resolves the per-lead `anuvia_booked_<short>` cookie and exposes:
      - has_booked: bool — only True when a lead_id cookie is present AND the
        matching per-lead booked cookie is set. A stale global cookie no
        longer hijacks fresh visitors.
      - lead_known: bool — True when the lead_id cookie is present AND
        resolves to a real row in Supabase.
      - lead_known_name / lead_known_email: pre-fill values for the booking
        widget when lead_known is True. Empty strings otherwise."""
    loc = get_locale(request)
    lead_id = request.cookies.get(LEAD_ID_COOKIE) or ""
    has_booked = False
    lead_known = False
    lead_known_name = ""
    lead_known_email = ""
    if lead_id:
        short = _short_lead_id(lead_id)
        if request.cookies.get(BOOKED_COOKIE_PREFIX + short) == "1":
            has_booked = True
        # Synchronous best-effort lookup — tpl_ctx is called inside both async
        # and sync template-render paths, and Jinja itself is sync. Tolerate
        # any network/Supabase failure quietly: a missing/unreachable row just
        # means we render the full form (lead_known stays False).
        try:
            with httpx.Client(timeout=5) as client:
                rr = client.get(
                    f"{SUPA_URL}/leads?id=eq.{lead_id}&select=id,name,email",
                    headers=SUPA_HEADERS,
                )
                if rr.status_code == 200:
                    rows = rr.json() or []
                    if rows:
                        lead_known = True
                        lead_known_name = (rows[0].get("name") or "") or ""
                        lead_known_email = (rows[0].get("email") or "") or ""
        except Exception:
            pass
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
        "has_booked": has_booked,
        "lead_known": lead_known,
        "lead_known_name": lead_known_name,
        "lead_known_email": lead_known_email,
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


@app.get("/cloud/aws/migration", response_class=HTMLResponse)
async def lp_aws_migration(request: Request):
    return templates.TemplateResponse("aws_migration.html", tpl_ctx(request))


@app.get("/cloud/aws/landing-zone", response_class=HTMLResponse)
async def lp_aws_landing_zone(request: Request):
    return templates.TemplateResponse("aws_landing_zone.html", tpl_ctx(request))


@app.get("/cloud/aws/security-posture", response_class=HTMLResponse)
async def lp_aws_security_posture(request: Request):
    return templates.TemplateResponse("aws_security_posture.html", tpl_ctx(request))


@app.get("/cloud/gcp/migration", response_class=HTMLResponse)
async def lp_gcp_migration(request: Request):
    return templates.TemplateResponse("gcp_migration.html", tpl_ctx(request))


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
# Sample PDF deliverables — watermarked "SAMPLE · CONFIDENTIAL" previews
# Served via short keys so the LPs can link with friendly URLs.
# ============================================================================
_SAMPLE_PDFS: dict[str, str] = {
    "finops_audit": "sample_finops_audit_report.pdf",
    "aws_well_architected": "sample_aws_well_architected.pdf",
    "devops_maturity": "sample_devops_maturity_assessment.pdf",
    "ai_readiness": "sample_ai_readiness_sprint.pdf",
    "sales_ops": "sample_sales_ops_diagnostic.pdf",
    "industry": "sample_industry_assessment.pdf",
}


@app.get("/samples/{filename}")
async def serve_sample_pdf(filename: str):
    """Serve a watermarked sample deliverable PDF.

    `filename` is a whitelisted short key (not the actual filename) to avoid
    path traversal and to keep the LP URLs stable independent of file naming.
    """
    actual = _SAMPLE_PDFS.get(filename)
    if not actual:
        raise HTTPException(status_code=404, detail="Sample not found")
    path = os.path.join(os.path.dirname(__file__), "static", "samples", actual)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Sample file missing")
    return FileResponse(path, media_type="application/pdf", filename=actual)


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

        # Autonomous funnel v1: enqueue the lead for classify_track.
        # The orchestrator's next tick picks it up and routes to Track A (discovery)
        # or Track B (autonomous close). Failures here NEVER block the upgrade —
        # the lead is fully saved and the discovery flow still works.
        if _AUTONOMOUS_FUNNEL_ENABLED:
            try:
                await session_set_next(
                    lead_id,
                    next_action="classify_track",
                    next_action_at=datetime.now(timezone.utc),
                )
            except Exception:
                log.exception("enqueue_classify_track_failed lead_id=%s", lead_id)

        return True
    except Exception:
        log.exception("upgrade_lead_failed")
        return False


# ----------------------------------------------------------------------------
# Email helpers — Anuvia branded footer, inline-style adapter for deliverable
# HTML, and ICS (iCalendar) attachment builder for booking confirmations.
# ----------------------------------------------------------------------------

def _anuvia_email_footer(lang: str = "pt") -> str:
    """Return the canonical Anuvia organizational footer (no personal name).

    Used at the bottom of every transactional email. Inline styles only — email
    clients strip <style> blocks. Keep this in ONE place so the footer stays
    consistent across diagnostic, booking, and follow-up emails."""
    if lang == "en":
        tagline = "Anuvia · Senior engineering for Cloud, AI, Platform and RevOps"
    else:
        tagline = "Anuvia · Engenharia sênior em Cloud, IA, Plataforma e RevOps"
    creds = "Ex-AWS · Ex-Google · Ex-MongoDB · 15× AWS-certified · MongoDB-certified · GCP-certified"
    return (
        '<p style="color:#78716c;font-size:13px;line-height:1.55;margin-top:32px;">'
        f'<strong style="color:#1a1a1a;font-weight:500;">{tagline}</strong><br>'
        f'{creds}'
        '</p>'
    )


def _inline_deliverable_for_email(html: str) -> str:
    """Adapt a deliverable HTML fragment (Tailwind classes) into inline-styled
    HTML for email rendering. Email clients (Gmail, Outlook, Apple Mail) strip
    <style> blocks and ignore external Tailwind. The fragments use a small
    set of project-specific classes — we map the ones that carry visual
    hierarchy into inline-style attributes.

    Specifically, this restores the visual hierarchy of the "Estimativa
    preliminar de economia anualizada" block (eyebrow + big serif amount +
    sub-explanation) which otherwise renders as flat text in clients."""

    # The headline savings card. Replace the wrapper div + its children with an
    # inline-styled equivalent that mirrors the site visual.
    out = html

    # Card wrapper (the savings block).
    out = out.replace(
        '<div class="my-8 p-6 bg-paper border border-rule">',
        '<div style="background:#fafaf9;border:1px solid #e7e5e4;padding:24px;margin:32px 0;">',
    )

    # Eyebrows inside the savings block (and elsewhere in the deliverable).
    out = re.sub(
        r'<p class="eyebrow mb-3">',
        '<p style="font-size:11px;letter-spacing:0.15em;text-transform:uppercase;color:#78716c;margin:0 0 12px 0;font-weight:500;">',
        out,
    )
    out = re.sub(
        r'<p class="eyebrow mb-4">',
        '<p style="font-size:11px;letter-spacing:0.15em;text-transform:uppercase;color:#78716c;margin:0 0 16px 0;font-weight:500;">',
        out,
    )

    # Headline greeting (h-serif text-4xl).
    out = re.sub(
        r'<p class="h-serif text-4xl mb-6 leading-tight">',
        '<p style="font-family:Georgia,\'Times New Roman\',serif;font-size:28px;font-weight:normal;color:#1a1a1a;line-height:1.15;margin:0 0 24px 0;">',
        out,
    )

    # The big savings amount (h-serif text-5xl) — this is the key visual.
    out = re.sub(
        r'<p class="h-serif text-5xl leading-none mb-1">',
        '<p style="font-family:Georgia,\'Times New Roman\',serif;font-size:36px;font-weight:normal;color:#1a1a1a;line-height:1.1;margin:0 0 4px 0;">',
        out,
    )

    # The "/ano" or "/year" suffix inside the amount.
    out = out.replace(
        '<span class="text-2xl text-subtle font-normal align-baseline">',
        '<span style="font-size:18px;color:#78716c;font-weight:normal;">',
    )

    # The em-dash separator between low and high in the amount.
    out = out.replace(
        '<span class="text-subtle font-normal">',
        '<span style="color:#78716c;font-weight:normal;">',
    )

    # Sub-explanation paragraph under the amount.
    out = re.sub(
        r'<p class="text-xs text-subtle mt-3 leading-relaxed">',
        '<p style="font-size:12px;color:#78716c;margin:12px 0 0 0;line-height:1.55;">',
        out,
    )

    # Other text-subtle paragraphs (close-out lines in the deliverable).
    out = re.sub(
        r'<p class="text-xs text-subtle leading-relaxed">',
        '<p style="font-size:12px;color:#78716c;margin:0;line-height:1.55;">',
        out,
    )

    # Body paragraphs.
    out = re.sub(
        r'<p class="text-ink/80 leading-relaxed mb-5">',
        '<p style="color:#475569;line-height:1.65;margin:0 0 20px 0;">',
        out,
    )
    out = re.sub(
        r'<p class="text-ink/80 leading-relaxed">',
        '<p style="color:#475569;line-height:1.65;margin:0;">',
        out,
    )

    # Section wrappers (my-8 div).
    out = out.replace(
        '<div class="my-8">',
        '<div style="margin:32px 0;">',
    )

    # The horizontal rule.
    out = out.replace(
        '<div class="rule"></div>',
        '<div style="border-top:1px solid #e7e5e4;margin:24px 0;"></div>',
    )

    # List items.
    out = re.sub(
        r'<li class="text-ink/80 leading-relaxed">',
        '<li style="color:#475569;line-height:1.65;margin-bottom:8px;">',
        out,
    )
    out = re.sub(
        r'<ul class="space-y-3 text-sm list-disc list-inside">',
        '<ul style="font-size:14px;color:#475569;padding-left:18px;margin:0;">',
        out,
    )
    out = re.sub(
        r'<ul class="space-y-2 text-sm text-ink/80 mb-5">',
        '<ul style="font-size:14px;color:#475569;padding-left:0;list-style:none;margin:0 0 20px 0;">',
        out,
    )

    # "Or schedule yourself" CTA inside the deliverable.
    out = re.sub(
        r'<a href="([^"]+)" class="btn-primary text-sm inline-block">',
        r'<a href="\1" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:10px 18px;text-decoration:none;font-size:14px;font-weight:500;">',
        out,
    )

    # The outer card class is already stripped by callers; strip mb-* utilities
    # that survived to avoid stray-class attributes in some clients.
    out = re.sub(r'\s*class="text-sm text-ink/70"', '', out)

    return out


def _ics_escape(text: str) -> str:
    """Escape special chars per RFC 5545: backslash, comma, semicolon, newline."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
            .replace("\r", "")
    )


def _build_booking_ics(
    *,
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str,
    location: str,
    attendee_email: str,
    attendee_name: str,
    organizer_email: str = "mila@anuvia.com.br",
    organizer_name: str = "Anuvia",
    uid: Optional[str] = None,
) -> str:
    """Construct a minimal but valid RFC 5545 iCalendar string for a single
    appointment. Uses UTC stamps to avoid VTIMEZONE block complexity. The
    receiving client renders it in the local TZ and offers accept/decline.

    No external dependency — keeps the deployment simple. If `icalendar` lib
    is later added, this can be swapped for a richer builder."""
    if uid is None:
        uid = f"anuvia-discovery-{uuid.uuid4()}@anuvia.com.br"

    # Convert to UTC for DTSTART/DTEND/DTSTAMP.
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)

    def _fmt(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Anuvia//Discovery Call//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_fmt(now_utc)}",
        f"DTSTART:{_fmt(start_utc)}",
        f"DTEND:{_fmt(end_utc)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer_name)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={_ics_escape(attendee_name)};RSVP=TRUE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:{attendee_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "TRANSP:OPAQUE",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Discovery Anuvia em 30 minutos",
        "TRIGGER:-PT30M",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # RFC 5545 mandates CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


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
    # Adapt Tailwind classes inside the deliverable to inline styles so the
    # "Estimativa preliminar" block keeps its visual hierarchy in email clients.
    inner = _inline_deliverable_for_email(inner)
    footer = _anuvia_email_footer("pt")
    email_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#fafaf9;font-family:-apple-system,Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:640px;margin:0 auto;">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 8px 0;">Anuvia · {practice_label}</p>
  <p style="font-family:Georgia,'Times New Roman',serif;font-size:32px;margin:0 0 24px 0;line-height:1.15;">Olá, {first}</p>
  <p style="color:#475569;line-height:1.65;">Obrigada por completar o diagnóstico. Aqui está o resultado completo gerado a partir das suas respostas:</p>
  <div style="background:#ffffff;border:1px solid #e7e5e4;padding:24px;margin:24px 0;">
    {inner}
  </div>
  <p style="color:#475569;line-height:1.65;">Próximo passo natural é uma conversa de 30 minutos com um Solutions Architect pra revisar o diagnóstico junto e priorizar próximos passos.</p>
  <p style="margin:24px 0;"><a href="{cta_url}" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;font-weight:500;">Agendar Discovery Call</a></p>
  {footer}
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
    ri_coverage: Optional[str] = ""
    cost_dominant: Optional[str] = ""
    context: Optional[str] = ""


def _build_finops_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    """Build the FinOps analysis HTML and metadata. Returns (analysis_meta, html)."""
    aws_spend = form_data.get("aws_spend", "")
    main_pain = form_data.get("main_pain", "")
    ri_coverage = form_data.get("ri_coverage", "") or ""
    cost_dominant = form_data.get("cost_dominant", "") or ""
    spend_label, spend_low, spend_annual_low = FINOPS_SPEND_TIERS.get(
        aws_spend, (aws_spend, 0, 0)
    )
    if lang == "en":
        spend_label = spend_label.replace("/mês", "/month")
    is_fit = aws_spend not in ("under_10k",)
    savings_low = int(spend_annual_low * 0.20) if is_fit else 0
    savings_high = int(spend_annual_low * 0.40) if is_fit else 0
    if lang == "en":
        pain_insight = FINOPS_PAIN_INSIGHT_EN.get(main_pain, "")
    else:
        pain_insight = FINOPS_PAIN_INSIGHT.get(main_pain, "")

    extra_blocks = []
    if lang == "en":
        if ri_coverage == "none":
            extra_blocks.append("RI/SP coverage at zero — bill is 100% on-demand. Observed pattern: 15-25% immediate savings just by rebalancing into a 1-year Compute Savings Plan (no workload changes, no AZ changes).")
        elif ri_coverage == "under_30":
            extra_blocks.append("RI/SP coverage below 30% — typically 8-15% savings available just by rebalancing the existing portfolio, before any architectural change.")
        elif ri_coverage == "above_60":
            extra_blocks.append("RI/SP coverage above 60% — the easy Reserved Instance win is already captured. The audit focuses on right-sizing, S3/EBS lifecycle, and data transfer optimization.")
        if cost_dominant == "compute_dominated":
            extra_blocks.append("Compute-dominated: audit focuses on right-sizing (Compute Optimizer), Spot mix for interruption-tolerant workloads, and Graviton where applicable (~20% savings on arm64).")
        elif cost_dominant == "storage_dominated":
            extra_blocks.append("Storage-dominated: priority on S3 lifecycle, Intelligent-Tiering, EBS gp3 vs gp2, orphan snapshots, and RDS storage autoscaling. Pattern: 10-20% recoverable without refactor.")
        elif cost_dominant == "data_transfer_dominated":
            extra_blocks.append("Data-transfer-heavy: focus on VPC Endpoints (eliminate NAT egress), CloudFront for public traffic, cross-AZ traffic mapping, and Direct Connect for on-prem hybrid. Usually the line item with the biggest surprise for the CFO.")
    else:
        if ri_coverage == "none":
            extra_blocks.append("Cobertura RI/SP zero — fatura está 100% on-demand. Padrão observado: 15-25% de economia imediata só rebalanceando para Compute Savings Plan de 1 ano (sem fechar workload, sem fechar AZ).")
        elif ri_coverage == "under_30":
            extra_blocks.append("Cobertura RI/SP abaixo de 30% — tipicamente 8-15% de economia disponível só rebalanceando o portfolio existente, antes de qualquer mudança de arquitetura.")
        elif ri_coverage == "above_60":
            extra_blocks.append("Cobertura RI/SP acima de 60% — o ganho fácil de Reserved Instances já foi capturado. Audit foca em right-sizing, lifecycle de S3/EBS e otimização de data transfer.")
        if cost_dominant == "compute_dominated":
            extra_blocks.append("Compute-dominated: foco do audit em right-sizing (Compute Optimizer), Spot mix para workloads tolerantes a interrupção e Graviton onde aplicável (~20% de saving em arm64).")
        elif cost_dominant == "storage_dominated":
            extra_blocks.append("Storage-dominated: prioridade em S3 lifecycle, Intelligent-Tiering, EBS gp3 vs gp2, snapshots órfãos e RDS storage autoscaling. Padrão: 10-20% recuperáveis sem refactor.")
        elif cost_dominant == "data_transfer_dominated":
            extra_blocks.append("Data-transfer-heavy: foco em VPC Endpoints (eliminam NAT egress), CloudFront para tráfego público, cross-AZ traffic mapping e Direct Connect se on-prem hybrid. Costuma ser a categoria com maior surpresa pro CFO.")

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."

    # Currency strings — savings figures are computed in BRL; convert for EN.
    if lang == "en":
        usd_low = int(savings_low / USD_FX_DIVISOR)
        usd_high = int(savings_high / USD_FX_DIVISOR)
        savings_amount_html = f'US$ {usd_low:,} <span class="text-subtle font-normal">–</span> US$ {usd_high:,}<span class="text-2xl text-subtle font-normal align-baseline">/year</span>'
    else:
        savings_amount_html = f'R$ {savings_low:,} <span class="text-subtle font-normal">–</span> R$ {savings_high:,}<span class="text-2xl text-subtle font-normal align-baseline">/ano</span>'

    if is_fit:
        if lang == "en":
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Preliminary estimate of annualized savings</p>
    <p class="h-serif text-5xl leading-none mb-1">{savings_amount_html}</p>
    <p class="text-xs text-subtle mt-3 leading-relaxed">Based on a {spend_label} bill cross-referenced with patterns from prior audits. The range reflects real scenarios — the floor assumes partial execution of recommendations; the ceiling, full execution within the first 12 weeks. Audits above $30k/month have closed within or above the floor in every engagement to date.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Signal from your stated concern</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">This preliminary analysis is orientative, based on aggregated patterns. The full audit individualizes for your specific reality — workloads, configuration, tags, AWS contracts, negotiated discounts.</p>
</div>
""".strip()
        else:
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Estimativa preliminar de economia anualizada</p>
    <p class="h-serif text-5xl leading-none mb-1">{savings_amount_html}</p>
    <p class="text-xs text-subtle mt-3 leading-relaxed">Baseado em fatura {spend_label} cruzado com padrões observados em audits anteriores. A faixa reflete cenários reais — o piso assume execução parcial das recomendações; o topo, execução completa nas primeiras 12 semanas. Auditorias acima de $30k/mês fecharam dentro ou acima do piso em todos os casos até hoje.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Insight sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Pré-análise orientativa baseada em padrões agregados. O audit completo individualiza pra sua realidade — workloads, configuração, tags, contratos AWS, descontos negociados.</p>
</div>
""".strip()
    else:
        if lang == "en":
            full_audit_price = format_price("R$ 45-60k", "USD")
            office_hours_price = format_price("R$ 1.500", "USD")
            quick_win_price = format_price("R$ 8-12k", "USD")
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">Given your bill profile ({spend_label}), a full FinOps Audit at {full_audit_price} likely will not clear the ROI threshold required to be worth your time right now.</p>
  <p class="text-ink/80 leading-relaxed mb-5">More appropriate options:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>90-min office hours with Mila</strong> — {office_hours_price}. Live diagnostic + actionable quick wins the same day.</li>
    <li>• <strong>FinOps Express workshop</strong> — {quick_win_price}, 1 week. Lighter audit with identified savings documented.</li>
    <li>• <strong>Anuvia AI Ops</strong> — if the concern is broader than AWS and covers operations as a whole, consider our SaaS product for operations automation.</li>
  </ul>
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
        "ri_coverage": ri_coverage,
        "cost_dominant": cost_dominant,
    }
    return meta, html


@app.post("/api/finops-audit/analyze")
async def api_finops_audit_analyze(form: FinOpsAnalyzeForm, request: Request):
    """Step 1 — receive business answers, return analysis HTML + lead_id (no PII)."""
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_finops_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "aws_spend": form.aws_spend,
        "main_pain": form.main_pain,
        "aws_tenure": form.aws_tenure,
        "ri_coverage": form.ri_coverage or "",
        "cost_dominant": form.cost_dominant or "",
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
async def api_finops_audit_contact(form: DiagContactForm, request: Request):
    """Step 2 — capture PII, upgrade lead, email report."""
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_finops_deliverable(meta, with_name=form.name, lang=lang)

        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        if lang == "en":
            subject = (
                f"FinOps Audit — preliminary analysis for {first}"
                if is_fit else
                f"Thanks, {first} — alternatives for your case"
            )
        else:
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
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# AWS Well-Architected — diagnostic-first flow
# ----------------------------------------------------------------------------

class WAAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    focus: str
    workload: str
    accounts_count: Optional[str] = ""
    last_audit: Optional[str] = ""
    context: Optional[str] = ""


def _build_wa_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    focus = form_data.get("focus", "")
    workload = form_data.get("workload", "")
    accounts_count = form_data.get("accounts_count", "") or ""
    last_audit = form_data.get("last_audit", "") or ""
    if lang == "en":
        focus_insight = WA_FOCUS_INSIGHT_EN.get(focus, "")
    else:
        focus_insight = WA_FOCUS_INSIGHT.get(focus, "")

    extra_blocks = []
    if lang == "en":
        if accounts_count == "1":
            extra_blocks.append("Single production account — Security pillar gap likely significant: no blast-radius isolation between dev and prod, IAM permissions with broad scope. The WA Review typically recommends Control Tower / Organizations as HRI remediation.")
        elif accounts_count in ("6_15", "16_plus"):
            extra_blocks.append(f"{accounts_count.replace('_', '-')} accounts — review scope includes Control Tower posture, IAM SCPs, cross-account roles, and log aggregation in the audit account. Reliability gains from account separation are already captured; focus shifts to standardization.")
        if last_audit == "never":
            extra_blocks.append("No external audit on record — the report will also serve as an auditable baseline for later conversations with SOC 2, ISO 27001, or sector regulators.")
        elif last_audit == "over_2y":
            extra_blocks.append("Last audit over 2 years ago — configuration drift is significant. A typical WA Review surfaces 8-15 HRI findings in this scenario, with 60% in Security and 25% in Reliability.")
    else:
        if accounts_count == "1":
            extra_blocks.append("Single production account — Security pillar gap likely significant: no blast-radius isolation entre dev/prod, IAM permissions com escopo amplo. WA Review tipicamente recomenda Control Tower / Organizations como remediação HRI.")
        elif accounts_count in ("6_15", "16_plus"):
            extra_blocks.append(f"{accounts_count.replace('_', '-')} accounts — escopo do review inclui Control Tower posture, SCPs IAM, cross-account roles e log aggregation no audit account. Reliability gains via account separation já estão capturados; foco maior em standardization.")
        if last_audit == "never":
            extra_blocks.append("Nenhum audit externo registrado — o relatório vai funcionar também como baseline auditável para discussões posteriores com SOC 2, ISO 27001 ou regulador setorial.")
        elif last_audit == "over_2y":
            extra_blocks.append("Último audit há mais de 2 anos — drift de configuração é significativo. WA Review típico encontra 8-15 findings HRI nesse cenário, sendo 60% em Security e 25% em Reliability.")

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        wa_price = format_price("R$ 30-50k", "USD")
        workload_label = workload or "not provided"
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8">
    <p class="eyebrow mb-3">About the focus you indicated</p>
    <p class="text-ink/80 leading-relaxed">{focus_insight}</p>
  </div>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">What the WA Review delivers</p>
    <p class="text-ink/80 leading-relaxed mb-3">Technical assessment across the 6 AWS pillars (Security, Reliability, Performance, Cost, Operational Excellence, Sustainability) with workload-specific gap analysis for {workload_label}. Output: executive report with HRI/MRI/LRI findings and a remediation roadmap sequenced by blast radius.</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Anuvia's AWS Well-Architected Review is led by Mila Vernazza, ex-AWS Solutions Architect with 15+ active certifications. 3-5 weeks, {wa_price}.</p>
</div>
""".strip()
    else:
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
    <p class="text-ink/80 leading-relaxed mb-3">Avaliação técnica nos 6 pilares AWS (Security, Reliability, Performance, Cost, Operational Excellence, Sustainability) com gap analysis específico para seu workload ({workload or 'não informado'}). Saída: relatório executivo com findings HRI/MRI/LRI e roadmap de remediação sequenciado por blast radius.</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AWS Well-Architected Review da Anuvia é conduzido por Mila Vernazza, ex-AWS Solutions Architect com 15× certificações ativas. 3-5 semanas, R$ 30-50k.</p>
</div>
""".strip()
    return {"focus": focus, "workload": workload, "accounts_count": accounts_count, "last_audit": last_audit}, html


@app.post("/api/aws-well-architected/analyze")
async def api_aws_wa_analyze(form: WAAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_wa_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "focus": form.focus,
        "workload": form.workload,
        "accounts_count": form.accounts_count or "",
        "last_audit": form.last_audit or "",
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
async def api_aws_wa_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_wa_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"AWS Well-Architected Review — preliminary analysis for {first}"
        else:
            subject = f"AWS Well-Architected Review — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AWS", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AWS Well-Architected", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[f"Foco: {meta.get('focus', '?')}  ·  Workload: {meta.get('workload', '?')}"],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# AWS Migration Planning — diagnostic-first flow
# ----------------------------------------------------------------------------

AWS_MIG_ENV_LABEL = {
    "on_prem": "on-premise / data center próprio",
    "other_cloud": "outro provedor cloud (Azure/GCP/Oracle)",
    "hybrid": "ambiente híbrido (on-prem + cloud)",
    "multi_account_aws": "AWS já em uso, consolidação multi-account",
}

AWS_MIG_ENV_LABEL_EN = {
    "on_prem": "on-premise / owned data center",
    "other_cloud": "another cloud provider (Azure/GCP/Oracle)",
    "hybrid": "hybrid environment (on-prem + cloud)",
    "multi_account_aws": "AWS already in use, multi-account consolidation",
}

AWS_MIG_DRIVER_INSIGHT = {
    "cost_pressure": "Cost-driven migrations typically realize 18-32% TCO reduction year one, peaking at year 3. Conditional on RI/Savings Plan commit being modeled during planning (not after migration) — the most common cause of post-migration sticker shock is on-demand spend that never got rebalanced.",
    "m_and_a": "M&A migrations require account-aware landing zone before workload migration. Without account isolation upfront, workload migrations from the acquired entity contaminate the IAM/network boundary of the acquirer — typically takes 9-12 months to untangle if skipped.",
    "end_of_lifecycle": "End-of-lifecycle migrations (hardware EOL, OS EOL, vendor sunset) are the highest-urgency category. Plan accommodates parallel cutover with rollback windows tied to the lifecycle deadline — typically a 4-6 week buffer pre-deadline is non-negotiable.",
    "regulatory": "Regulatory-driven migrations (LGPD, data residency, BACEN 4.658, healthcare) require region/account selection as the first decision, not the last. Wave plan sequences regulated workloads first to validate the compliance posture before scaling.",
    "scaling": "Scaling-driven migrations are the easiest economically (clear ROI) but most architecture-sensitive — the wave plan needs to refactor at least the data layer (RDS-managed, S3 tiering, Aurora Serverless where applicable) to capture the elasticity benefit. Pure lift-and-shift wastes 60-70% of the migration upside.",
}

AWS_MIG_TIMELINE_INSIGHT = {
    "under_3m": "Sub-3-month timeline is aggressive — only viable with Strangler pattern (one workload at a time, parallel run, gradual traffic cutover). Big-bang migration in this window almost always overruns and triggers rollback. Plan will prioritize cost-bleeders and end-of-lifecycle workloads first.",
    "3_6m": "3-6 month timeline is the standard mid-market migration window. Plan typically delivers 60-80% of workloads migrated within window, with stragglers (legacy DB, niche dependencies) deferred to follow-on wave.",
    "6_12m": "6-12 month timeline supports a full multi-wave migration with proper landing zone build, account vending, and parallel data sync periods. This is the lowest-risk window for >20 workloads.",
    "above_12m": "12+ month timeline gives room for full re-platforming (not just lift-and-shift) — capturing the architectural upside (managed databases, serverless, autoscaling) that lift-and-shift leaves on the table. ROI is materially higher but requires sustained executive commitment.",
}

# EN variants — same content (English already), provided for symmetry.
AWS_MIG_DRIVER_INSIGHT_EN = dict(AWS_MIG_DRIVER_INSIGHT)
AWS_MIG_TIMELINE_INSIGHT_EN = dict(AWS_MIG_TIMELINE_INSIGHT)


class AWSMigrationAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    current_environment: str
    workload_count: str
    migration_drivers: str
    timeline: str
    context: Optional[str] = ""


def _build_aws_migration_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    current_environment = form_data.get("current_environment", "")
    workload_count = form_data.get("workload_count", "")
    migration_drivers = form_data.get("migration_drivers", "")
    timeline = form_data.get("timeline", "")
    if lang == "en":
        env_label = AWS_MIG_ENV_LABEL_EN.get(current_environment, current_environment or "current environment")
        driver_insight = AWS_MIG_DRIVER_INSIGHT_EN.get(migration_drivers, "")
        timeline_insight = AWS_MIG_TIMELINE_INSIGHT_EN.get(timeline, "")
    else:
        env_label = AWS_MIG_ENV_LABEL.get(current_environment, current_environment or "ambiente atual")
        driver_insight = AWS_MIG_DRIVER_INSIGHT.get(migration_drivers, "")
        timeline_insight = AWS_MIG_TIMELINE_INSIGHT.get(timeline, "")

    extra_blocks = []
    if lang == "en":
        if timeline == "under_3m" and workload_count in ("20_50", "above_50"):
            extra_blocks.append(
                "Tight timeline vs. workload count: we recommend Strangler pattern with cost-bleeder prioritization first. Big-bang migration above 20 workloads in 3 months has a historical rollback rate above 60%."
            )
        if current_environment == "m_and_a" or migration_drivers == "m_and_a":
            extra_blocks.append(
                "M&A migration: an account-aware landing zone is a non-negotiable prerequisite before the first workload migrates. Skipping this step adds 9-12 months of IAM/network rework later."
            )
        if workload_count == "above_50":
            extra_blocks.append(
                "50+ workloads demands a factory model: well-defined wave pipeline, runbooks reusable by architectural pattern (web tier, batch, data, ML), dedicated squad. Without a factory, throughput collapses after wave 3."
            )
        if current_environment == "on_prem":
            extra_blocks.append(
                "On-prem → AWS migration: Application Discovery Service + Migration Hub collect server/dependency inventory in 2-4 weeks. TCO is modeled against real data center cost — not just hardware, but also energy, cooling, space, and ops staff."
            )
        elif current_environment == "other_cloud":
            extra_blocks.append(
                "Cloud-to-cloud migration: discovery focuses on mapping managed-service equivalences (e.g., Azure SQL → Aurora, GCS → S3, Pub/Sub → SQS/SNS/Kinesis). Egress costs from the current provider are usually the most underestimated line item in TCO."
            )
    else:
        if timeline == "under_3m" and workload_count in ("20_50", "above_50"):
            extra_blocks.append(
                "Timeline apertado vs. número de workloads: recomendamos Strangler pattern com priorização de cost-bleeders primeiro. Migration big-bang acima de 20 workloads em 3 meses tem taxa de rollback histórica acima de 60%."
            )
        if current_environment == "m_and_a" or migration_drivers == "m_and_a":
            extra_blocks.append(
                "M&A migration: landing zone account-aware é pré-requisito não-negociável antes do primeiro workload migrar. Pular essa etapa adiciona 9-12 meses de retrabalho IAM/network depois."
            )
        if workload_count == "above_50":
            extra_blocks.append(
                "50+ workloads exige factory model: pipeline de wave bem definido, runbooks reutilizáveis por padrão arquitetural (web tier, batch, data, ML), squad dedicada. Sem factory, throughput cai a partir do wave 3."
            )
        if current_environment == "on_prem":
            extra_blocks.append(
                "Migração on-prem→AWS: Application Discovery Service + Migration Hub coletam inventário de servidor/dependência em 2-4 semanas. TCO modelado contra custo real de data center (não apenas hardware — também energia, cooling, espaço, ops staff)."
            )
        elif current_environment == "other_cloud":
            extra_blocks.append(
                "Migração cloud-to-cloud: discovery foca em mapear equivalências de serviço gerenciado (ex.: Azure SQL → Aurora, GCS → S3, Pub/Sub → SQS/SNS/Kinesis). Egress costs do provider atual costumam ser o item mais subestimado do TCO."
            )

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        mig_price = format_price("R$ 50-120k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Discovery & TCO baseline</p>
    <p class="text-ink/80 leading-relaxed">Discovery starts with AWS Migration Hub + Application Discovery Service to inventory workloads. TCO is modeled against your existing data center costs or current cloud provider invoices. Starting from {env_label}, the plan covers wave map, dependency graph, and per-workload cutover schedule.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About the migration driver</p>
    <p class="text-ink/80 leading-relaxed">{driver_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About the timeline you indicated</p>
    <p class="text-ink/80 leading-relaxed">{timeline_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Anuvia AWS Migration Planning: 6-10 weeks, {mig_price}. Delivers TCO model, wave plan, dependency map, and risk register to support executive migration decisions.</p>
</div>
""".strip()
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Discovery & TCO baseline</p>
    <p class="text-ink/80 leading-relaxed">Discovery starts with AWS Migration Hub + Application Discovery Service to inventory workloads. TCO modeled against your existing data center costs or current cloud provider invoices. A partir de {env_label}, o plano cobre wave map, dependency graph, e cutover schedule por workload.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o driver de migração</p>
    <p class="text-ink/80 leading-relaxed">{driver_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o timeline informado</p>
    <p class="text-ink/80 leading-relaxed">{timeline_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AWS Migration Planning Anuvia: 6-10 semanas, R$ 50-120k. Entrega TCO model, wave plan, dependency map e risk register pra suportar decisão executiva de migração.</p>
</div>
""".strip()
    return {
        "current_environment": current_environment,
        "workload_count": workload_count,
        "migration_drivers": migration_drivers,
        "timeline": timeline,
    }, html


@app.post("/api/aws-migration/analyze")
async def api_aws_migration_analyze(form: AWSMigrationAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_aws_migration_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "current_environment": form.current_environment,
        "workload_count": form.workload_count,
        "migration_drivers": form.migration_drivers,
        "timeline": form.timeline,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_AWS_MIG",
            source="lp_aws_migration",
            diag_type="aws_migration",
            market=get_locale(request)["market"],
            track="cloud_aws_migration",
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_aws_migration", "cloud_aws_migration"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html})


@app.post("/api/aws-migration/contact")
async def api_aws_migration_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_aws_migration_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"AWS Migration Planning — preliminary analysis for {first}"
        else:
            subject = f"AWS Migration Planning — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AWS Migration", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AWS Migration Planning", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Env: {meta.get('current_environment', '?')}  ·  Workloads: {meta.get('workload_count', '?')}",
                f"Driver: {meta.get('migration_drivers', '?')}  ·  Timeline: {meta.get('timeline', '?')}",
            ],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# AWS Landing Zone / Multi-Account — diagnostic-first flow
# ----------------------------------------------------------------------------

AWS_LZ_CONCERN_INSIGHT = {
    "security_baseline": "Security baseline at LZ build typically includes Security Hub, GuardDuty, Config rules per pillar, centralized CloudTrail in audit account, SCPs preventing root-key creation. A maturidade de cada item se diferencia mais por SCP coverage do que por ferramenta — Anuvia entrega catálogo SCP por OU como artefato.",
    "cost_allocation": "Multi-team cost allocation depends on tagging strategy first, then per-OU billing via Cost Categories. Landing Zone formalizes both. Sem tagging baseline antes do LZ, allocation post-hoc gera disputa entre times e não para de pé. Cost Categories per OU é o controle mais reutilizável.",
    "multi_team_isolation": "Multi-team isolation matures via OU design + SCPs limiting cross-team blast radius, plus per-team VPC patterns and shared services accounts (logging, security, network). O erro comum é tratar isolation como problema de IAM puro — VPC/network isolation é igualmente importante.",
    "regulatory": "Common Brazilian patterns: ISO 27001-aligned LZ + LGPD data-residency SCPs blocking us-east regions for sensitive workloads. Para BACEN 4.658, adicionamos SCP de retenção mínima de logs em audit account + WAF baseline per ingress. ANVISA/healthcare requer encryption-at-rest SCPs por classe de dado.",
    "scaling_team": "Scaling-team motivation prioritiza account vending automation (Service Catalog + Account Factory) com baseline turn-key — o gargalo deixa de ser provisionamento manual e vira governance review. SLA típico: nova account em <2h vs. semanas em ambiente sem LZ.",
}

AWS_LZ_GOVERNANCE_INSIGHT = {
    "none": "Sem governance hoje: caso clássico de LZ greenfield. Engagement completo cobre AWS Organizations setup, OU hierarchy, Control Tower deployment, baseline SCPs, account vending. 8-12 semanas.",
    "ad_hoc": "Governance ad-hoc é o pior dos mundos — accounts existem mas sem padrão. LZ build requer migration plan de accounts existentes pra OUs corretas + retrofit de baseline. Pré-trabalho de auditoria de 1-2 semanas antes do build começar.",
    "partial_control_tower": "Control Tower parcial: tipicamente Account Factory deployed mas sem SCP catalog maduro ou baseline observability. Engagement reduzido (6-8 semanas) focado em fechar gaps específicos — guardrails, audit/log account, network shared services.",
    "full_control_tower": "Control Tower maduro: engagement vira optimization review (4-6 semanas). Foco em SCP coverage gaps, cost allocation refinement, e potencial migração pra OUs mais granulares conforme org cresce.",
}

AWS_LZ_ORG_INSIGHT = {
    "none": "Sem AWS Organizations: precondição obrigatória — Organizations é fundação do LZ moderno. Setup integrado ao engagement (semana 1).",
    "basic": "Organizations básica (root + 1-2 OUs): cobertura suficiente pra começar. Engagement vai refinar OU design pra refletir org boundaries (workload, environment, sensitivity).",
    "mature": "Organizations madura com OUs estruturados: engagement foca em policy depth — SCP coverage, delegated admin patterns, shared services accounts.",
}

AWS_LZ_CONCERN_INSIGHT_EN = {
    "security_baseline": "Security baseline at LZ build typically includes Security Hub, GuardDuty, Config rules per pillar, centralized CloudTrail in audit account, and SCPs preventing root-key creation. Maturity differs more by SCP coverage than by tooling — Anuvia delivers a per-OU SCP catalog as an artifact.",
    "cost_allocation": "Multi-team cost allocation depends on tagging strategy first, then per-OU billing via Cost Categories. Landing Zone formalizes both. Without a tagging baseline before LZ, post-hoc allocation triggers disputes between teams and does not hold. Cost Categories per OU is the most reusable control.",
    "multi_team_isolation": "Multi-team isolation matures via OU design + SCPs limiting cross-team blast radius, plus per-team VPC patterns and shared services accounts (logging, security, network). A common mistake is treating isolation as a pure IAM problem — VPC/network isolation is equally important.",
    "regulatory": "Common Brazilian patterns: ISO 27001-aligned LZ + LGPD data-residency SCPs blocking us-east regions for sensitive workloads. For BACEN 4.658, we add a minimum log retention SCP in the audit account plus per-ingress WAF baseline. ANVISA/healthcare requires encryption-at-rest SCPs by data class.",
    "scaling_team": "Scaling-team motivation prioritizes account vending automation (Service Catalog + Account Factory) with a turn-key baseline — the bottleneck moves from manual provisioning to governance review. Typical SLA: a new account in <2h vs. weeks without LZ.",
}

AWS_LZ_GOVERNANCE_INSIGHT_EN = {
    "none": "No governance today: classic greenfield LZ case. Full engagement covers AWS Organizations setup, OU hierarchy, Control Tower deployment, baseline SCPs, and account vending. 8-12 weeks.",
    "ad_hoc": "Ad-hoc governance is the worst case — accounts exist but without a standard. LZ build requires a migration plan for existing accounts into the correct OUs plus baseline retrofit. 1-2 weeks of audit pre-work before the build begins.",
    "partial_control_tower": "Partial Control Tower: Account Factory typically deployed but no mature SCP catalog or baseline observability. Reduced engagement (6-8 weeks) focused on closing specific gaps — guardrails, audit/log account, network shared services.",
    "full_control_tower": "Mature Control Tower: engagement becomes an optimization review (4-6 weeks). Focus on SCP coverage gaps, cost allocation refinement, and potential migration to more granular OUs as the org grows.",
}

AWS_LZ_ORG_INSIGHT_EN = {
    "none": "No AWS Organizations: mandatory precondition — Organizations is the foundation of a modern LZ. Setup is included in the engagement (week 1).",
    "basic": "Basic Organizations (root + 1-2 OUs): enough coverage to start. The engagement will refine OU design to reflect org boundaries (workload, environment, sensitivity).",
    "mature": "Mature Organizations with structured OUs: the engagement focuses on policy depth — SCP coverage, delegated admin patterns, shared services accounts.",
}


class AWSLandingZoneAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    accounts_today: str
    governance_state: str
    top_concern: str
    aws_org_setup: Optional[str] = ""
    context: Optional[str] = ""


def _build_aws_landing_zone_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    accounts_today = form_data.get("accounts_today", "")
    governance_state = form_data.get("governance_state", "")
    top_concern = form_data.get("top_concern", "")
    aws_org_setup = form_data.get("aws_org_setup", "") or ""
    if lang == "en":
        concern_insight = AWS_LZ_CONCERN_INSIGHT_EN.get(top_concern, "")
        gov_insight = AWS_LZ_GOVERNANCE_INSIGHT_EN.get(governance_state, "")
        org_insight = AWS_LZ_ORG_INSIGHT_EN.get(aws_org_setup, "")
    else:
        concern_insight = AWS_LZ_CONCERN_INSIGHT.get(top_concern, "")
        gov_insight = AWS_LZ_GOVERNANCE_INSIGHT.get(governance_state, "")
        org_insight = AWS_LZ_ORG_INSIGHT.get(aws_org_setup, "")

    extra_blocks = []
    if lang == "en":
        if accounts_today == "1" and governance_state in ("none", "ad_hoc"):
            extra_blocks.append(
                "Single account today + low governance: greenfield LZ with migration of existing workloads into dedicated accounts per environment (dev/stg/prod) + workload boundary. Most common pattern for mid-market entering compliance."
            )
        if accounts_today == "16_plus" and governance_state in ("none", "ad_hoc"):
            extra_blocks.append(
                "16+ accounts without structured governance is the highest-risk scenario — baseline drift accumulates quickly. We recommend an inventory audit (week 0) before the build starts, to map what migrates, what stays, what gets retired."
            )
        if top_concern == "regulatory" and accounts_today in ("6_15", "16_plus"):
            extra_blocks.append(
                "Regulatory + 6+ accounts: prioritize a dedicated audit/log account with retention SCPs + per-OU region-restriction SCPs. ISO 27001 and LGPD are treated as foundation controls, not retrofit."
            )
    else:
        if accounts_today == "1" and governance_state in ("none", "ad_hoc"):
            extra_blocks.append(
                "Single account hoje + governance baixa: LZ greenfield com migração de workloads existentes pra accounts dedicadas por ambiente (dev/stg/prod) + workload boundary. Padrão mais comum em mid-market entrando em compliance."
            )
        if accounts_today == "16_plus" and governance_state in ("none", "ad_hoc"):
            extra_blocks.append(
                "16+ accounts sem governance estruturada é o cenário de maior risco — drift de baseline acumula rapidamente. Recomendamos audit de inventário (semana 0) antes do build começar, pra mapear o que migra, o que fica, o que se aposenta."
            )
        if top_concern == "regulatory" and accounts_today in ("6_15", "16_plus"):
            extra_blocks.append(
                "Regulatory + 6+ accounts: priorizar dedicated audit/log account com retention SCPs + region-restriction SCPs por OU. ISO 27001 e LGPD são tratados como controles de fundação, não retrofit."
            )

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        lz_price = format_price("R$ 60-150k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">What the Landing Zone build delivers</p>
    <p class="text-ink/80 leading-relaxed">AWS Control Tower + AWS Organizations implementation with OU hierarchy, per-OU Service Control Policies, automated account vending, and baseline IAM/network/logging in dedicated audit & log accounts. 6-12 weeks, {lz_price}.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About your primary concern</p>
    <p class="text-ink/80 leading-relaxed">{concern_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About the current governance state</p>
    <p class="text-ink/80 leading-relaxed">{gov_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About AWS Organizations</p>
    <p class="text-ink/80 leading-relaxed">{org_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Anuvia Landing Zone Implementation: Control Tower, SCPs, OU design, account vending, baseline IAM/network/logging.</p>
</div>
""".strip()
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">O que o Landing Zone build entrega</p>
    <p class="text-ink/80 leading-relaxed">Implementação AWS Control Tower + AWS Organizations com OU hierarchy, Service Control Policies por OU, account vending automatizado, baseline IAM/network/logging em audit & log accounts dedicadas. 6-12 semanas, R$ 60-150k.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre sua preocupação principal</p>
    <p class="text-ink/80 leading-relaxed">{concern_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o estado de governance atual</p>
    <p class="text-ink/80 leading-relaxed">{gov_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre AWS Organizations</p>
    <p class="text-ink/80 leading-relaxed">{org_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Landing Zone Implementation Anuvia: Control Tower, SCPs, OU design, account vending, baseline IAM/network/logging.</p>
</div>
""".strip()
    return {
        "accounts_today": accounts_today,
        "governance_state": governance_state,
        "top_concern": top_concern,
        "aws_org_setup": aws_org_setup,
    }, html


@app.post("/api/aws-landing-zone/analyze")
async def api_aws_landing_zone_analyze(form: AWSLandingZoneAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_aws_landing_zone_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "accounts_today": form.accounts_today,
        "governance_state": form.governance_state,
        "top_concern": form.top_concern,
        "aws_org_setup": form.aws_org_setup or "",
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_AWS_LZ",
            source="lp_aws_landing_zone",
            diag_type="aws_landing_zone",
            market=get_locale(request)["market"],
            track="cloud_aws_landing_zone",
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_aws_landing_zone", "cloud_aws_landing_zone"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html})


@app.post("/api/aws-landing-zone/contact")
async def api_aws_landing_zone_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_aws_landing_zone_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"AWS Landing Zone — preliminary analysis for {first}"
        else:
            subject = f"AWS Landing Zone — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AWS Landing Zone", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AWS Landing Zone", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Accounts: {meta.get('accounts_today', '?')}  ·  Governance: {meta.get('governance_state', '?')}",
                f"Top concern: {meta.get('top_concern', '?')}",
            ],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# AWS Security Posture Assessment — diagnostic-first flow
# ----------------------------------------------------------------------------

AWS_SEC_IAM_INSIGHT = {
    "federated_sso": "IAM federado via SSO + SCIM provisioning é o estado-meta. Audit foca em coverage gaps — break-glass accounts, service accounts ainda locais, role assumption boundaries. Tipicamente <2 HRI findings em IAM nesse cenário.",
    "mixed": "IAM misto (alguns federados, outros locais) é o cenário mais comum. Audit prioriza inventário de IAM users locais ativos, migração path pra SSO + SCIM, e SCP de prevenção de novas keys de longa duração. Padrão: 4-8 findings em IAM, 1-2 HRI.",
    "mostly_local_users": "Mostly local users is the highest-leverage finding category — typical first-month remediation moves the org to SSO + SCIM provisioning. Toda credencial estática rotacionável é exposure window aberto. Audit costuma encontrar 3-5 HRI só em IAM nesse cenário, incluindo access keys com idade >365 dias.",
    "dont_know": "Não saber o estado de IAM é, na prática, equivalente a 'mostly local users' — inventário de IAM é o ponto-zero do audit. Costuma virar a primeira semana inteira do engagement.",
}

AWS_SEC_COMPLIANCE_INSIGHT = {
    "none": "Sem target de compliance: audit usa CIS AWS Foundations Benchmark + Anuvia security baseline como referência. Cobertura de IAM, network, encryption, logging.",
    "soc2": "SOC 2: audit alinha findings aos controls SOC 2 Type II (CC6 logical access, CC7 system operations, CC8 change management). Saída inclui control mapping pra auditor externo.",
    "iso27001": "ISO 27001: audit alinha aos controles Anexo A (A.5-A.18). Findings classificados por cláusula. Compatível com SoA já em uso.",
    "lgpd": "LGPD compliance posture review covers data residency, encryption at rest with KMS keys per data class, access logging via CloudTrail Lake, deletion/portability automation. Mapeamento aos Art. 46-49 (medidas de segurança) é entregue.",
    "hipaa": "HIPAA: audit cobre PHI handling — encryption at rest/transit, BAA-applicable services, access logging por tipo de PHI, breach notification automation.",
    "pci_dss": "PCI-DSS: audit foca em CDE (cardholder data environment) — segmentation, network controls, encryption keys, log retention. Saída inclui scope reduction recommendations quando aplicável.",
    "bacen_4658": "BACEN Res. 4.658: audit cobre controles obrigatórios — política de segurança cibernética, plano de ação/resposta, autenticação multifator, criptografia, logs de eventos relevantes, gestão de terceiros. Compatível com framework BACEN.",
    "multiple": "Múltiplos frameworks: audit prioriza overlap analysis — controles únicos costumam ter overlap de 60-80% entre SOC 2, ISO 27001 e LGPD. Saída inclui matrix unificada de findings por framework.",
}

AWS_SEC_INCIDENT_INSIGHT = {
    "never": "Sem incidente reportado: audit usa baseline preventivo. Recomendação típica inclui tabletop exercise pra validar runbooks contra cenário real.",
    "minor": "Incidente menor histórico: audit usa o post-mortem (se documentado) como input. Costuma indicar gap em detecção mais do que em prevention — GuardDuty + Security Hub maturação.",
    "major_one_plus": "Incidente major histórico: audit é tratado como post-incident review estendido. Prioridade em containment patterns, blast-radius reduction (SCPs, network segmentation), e incident response automation (Lambda response runbooks, SOAR-light com Security Hub).",
}

AWS_SEC_IAM_INSIGHT_EN = {
    "federated_sso": "Federated IAM via SSO + SCIM provisioning is the target state. Audit focuses on coverage gaps — break-glass accounts, service accounts still local, role assumption boundaries. Typically <2 HRI findings in IAM in this scenario.",
    "mixed": "Mixed IAM (some federated, some local) is the most common scenario. Audit prioritizes inventory of active local IAM users, migration path to SSO + SCIM, and an SCP preventing new long-lived keys. Pattern: 4-8 findings in IAM, 1-2 HRI.",
    "mostly_local_users": "Mostly local users is the highest-leverage finding category — typical first-month remediation moves the org to SSO + SCIM provisioning. Every static rotatable credential is an open exposure window. The audit typically finds 3-5 HRI in IAM alone in this scenario, including access keys older than 365 days.",
    "dont_know": "Not knowing the IAM state is, in practice, equivalent to 'mostly local users' — IAM inventory is the zero point of the audit. It usually consumes the first full week of the engagement.",
}

AWS_SEC_COMPLIANCE_INSIGHT_EN = {
    "none": "No compliance target: audit uses CIS AWS Foundations Benchmark + Anuvia security baseline as reference. Coverage of IAM, network, encryption, logging.",
    "soc2": "SOC 2: audit aligns findings to SOC 2 Type II controls (CC6 logical access, CC7 system operations, CC8 change management). Output includes control mapping for the external auditor.",
    "iso27001": "ISO 27001: audit aligns to Annex A controls (A.5-A.18). Findings classified by clause. Compatible with the SoA already in use.",
    "lgpd": "LGPD compliance posture review covers data residency, encryption at rest with KMS keys per data class, access logging via CloudTrail Lake, and deletion/portability automation. Mapping to Articles 46-49 (security measures) is delivered.",
    "hipaa": "HIPAA: audit covers PHI handling — encryption at rest/in transit, BAA-applicable services, access logging by PHI type, breach notification automation.",
    "pci_dss": "PCI-DSS: audit focuses on the CDE (cardholder data environment) — segmentation, network controls, encryption keys, log retention. Output includes scope reduction recommendations where applicable.",
    "bacen_4658": "BACEN Res. 4.658: audit covers mandatory controls — cybersecurity policy, action/response plan, multi-factor authentication, encryption, relevant event logs, third-party management. Compatible with the BACEN framework.",
    "multiple": "Multiple frameworks: audit prioritizes overlap analysis — unique controls typically overlap 60-80% across SOC 2, ISO 27001, and LGPD. Output includes a unified findings matrix by framework.",
}

AWS_SEC_INCIDENT_INSIGHT_EN = {
    "never": "No reported incident: audit uses a preventive baseline. Typical recommendation includes a tabletop exercise to validate runbooks against a real scenario.",
    "minor": "Minor historical incident: audit uses the post-mortem (if documented) as input. Usually indicates a detection gap more than a prevention gap — GuardDuty + Security Hub maturation.",
    "major_one_plus": "Major historical incident: audit is treated as an extended post-incident review. Priority on containment patterns, blast-radius reduction (SCPs, network segmentation), and incident response automation (Lambda response runbooks, SOAR-light with Security Hub).",
}


class AWSSecurityAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    compliance_target: str
    latest_audit: str
    iam_state: str
    incident_history: Optional[str] = ""
    context: Optional[str] = ""


def _build_aws_security_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    compliance_target = form_data.get("compliance_target", "")
    latest_audit = form_data.get("latest_audit", "")
    iam_state = form_data.get("iam_state", "")
    incident_history = form_data.get("incident_history", "") or ""
    if lang == "en":
        iam_insight = AWS_SEC_IAM_INSIGHT_EN.get(iam_state, "")
        compliance_insight = AWS_SEC_COMPLIANCE_INSIGHT_EN.get(compliance_target, "")
        incident_insight = AWS_SEC_INCIDENT_INSIGHT_EN.get(incident_history, "") if incident_history else ""
    else:
        iam_insight = AWS_SEC_IAM_INSIGHT.get(iam_state, "")
        compliance_insight = AWS_SEC_COMPLIANCE_INSIGHT.get(compliance_target, "")
        incident_insight = AWS_SEC_INCIDENT_INSIGHT.get(incident_history, "") if incident_history else ""

    extra_blocks = []
    if lang == "en":
        if latest_audit == "never":
            extra_blocks.append(
                "No prior audit: the report also serves as an auditable baseline for later conversations with external auditors. Everything is documented with reproducible evidence."
            )
        elif latest_audit == "over_2y":
            extra_blocks.append(
                "Last audit over 2 years ago: configuration drift is significant. A typical audit identifies 8-15 HRI in this scenario — 50%+ in IAM, 20-30% in network exposure, the rest in logging/encryption."
            )
        elif latest_audit == "in_progress":
            extra_blocks.append(
                "External audit in progress: our engagement is coordinated to produce a complementary evidence package — technical controls the auditor will not exercise in depth (network policy, IAM permissioning, key management) are documented to accelerate closing."
            )
        if iam_state == "mostly_local_users" and incident_history == "major_one_plus":
            extra_blocks.append(
                "Local users + major historical incident is the highest recurring-risk combination. Typical recommendation: SSO + SCIM provisioning + access key rotation enforcement as week-1 remediation, before anything else."
            )
    else:
        if latest_audit == "never":
            extra_blocks.append(
                "Nenhum audit prévio: relatório serve também como baseline auditável pra conversas posteriores com auditores externos. Vamos documentar tudo com evidência reproducível."
            )
        elif latest_audit == "over_2y":
            extra_blocks.append(
                "Último audit há mais de 2 anos: drift de configuração é significativo. Audit típico identifica 8-15 HRI nesse cenário — 50%+ em IAM, 20-30% em network exposure, restante em logging/encryption."
            )
        elif latest_audit == "in_progress":
            extra_blocks.append(
                "Audit externo em curso: nosso engagement é coordenado pra produzir evidence pacote complementar — controles técnicos que auditor não vai exercitar em depth (network policy, IAM permissioning, key management) ficam documentados pra acelerar fechamento."
            )
        if iam_state == "mostly_local_users" and incident_history == "major_one_plus":
            extra_blocks.append(
                "Combinação local users + incident major histórico é o cenário de maior risco recorrente. Recomendação típica: SSO + SCIM provisioning + access key rotation enforcement como remediação semana 1, antes de qualquer outra coisa."
            )

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    incident_label = "About the incident history" if lang == "en" else "Sobre o histórico de incidente"
    incident_block = ""
    if incident_insight:
        incident_block = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{incident_label}</p>
    <p class="text-ink/80 leading-relaxed">{incident_insight}</p>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        sec_price = format_price("R$ 30-70k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">About the IAM state (highest-leverage signal)</p>
    <p class="text-ink/80 leading-relaxed">{iam_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About the compliance target</p>
    <p class="text-ink/80 leading-relaxed">{compliance_insight}</p>
  </div>
{incident_block}{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Anuvia Security Posture Assessment: 4-6 weeks, {sec_price}. Coverage across IAM, network exposure, encryption, logging baseline, and compliance posture. Findings prioritized by blast-radius (HRI/MRI/LRI).</p>
</div>
""".strip()
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre o estado IAM (sinal de maior alavancagem)</p>
    <p class="text-ink/80 leading-relaxed">{iam_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o target de compliance</p>
    <p class="text-ink/80 leading-relaxed">{compliance_insight}</p>
  </div>
{incident_block}{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Security Posture Assessment Anuvia: 4-6 semanas, R$ 30-70k. Cobertura IAM, network exposure, encryption, logging baseline, compliance posture. Findings priorizados por blast-radius (HRI/MRI/LRI).</p>
</div>
""".strip()
    return {
        "compliance_target": compliance_target,
        "latest_audit": latest_audit,
        "iam_state": iam_state,
        "incident_history": incident_history,
    }, html


@app.post("/api/aws-security-posture/analyze")
async def api_aws_security_analyze(form: AWSSecurityAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_aws_security_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "compliance_target": form.compliance_target,
        "latest_audit": form.latest_audit,
        "iam_state": form.iam_state,
        "incident_history": form.incident_history or "",
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_AWS_SEC",
            source="lp_aws_security_posture",
            diag_type="aws_security_posture",
            market=get_locale(request)["market"],
            track="cloud_aws_security_posture",
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_aws_security_posture", "cloud_aws_security_posture"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html})


@app.post("/api/aws-security-posture/contact")
async def api_aws_security_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_aws_security_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"AWS Security Posture — preliminary analysis for {first}"
        else:
            subject = f"AWS Security Posture — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "AWS Security", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "AWS Security Posture", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"IAM: {meta.get('iam_state', '?')}  ·  Compliance: {meta.get('compliance_target', '?')}",
                f"Audit history: {meta.get('latest_audit', '?')}  ·  Incident: {meta.get('incident_history', '?')}",
            ],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# GCP Migration Strategy — diagnostic-first flow
# ----------------------------------------------------------------------------

GCP_INTEREST_INSIGHT = {
    "bigquery_analytics": "BigQuery vs. Redshift comparison hinges on query patterns (BigQuery wins on ad-hoc analytical, Redshift wins on predictable BI). Migration cost dominated by data egress from AWS — modelamos egress real do CUR antes de qualquer decisão. Em ~30% dos casos, a melhor jogada é federated query (Redshift Spectrum + BigQuery Omni), não migração completa.",
    "vertex_ai_ml": "Vertex AI's competitive edge today is in MLOps tooling for traditional ML; for LLM-centric work, AWS Bedrock + SageMaker is typically stronger. We'll model the workload-specific case — quando os pipelines são tabular/clássicos, Vertex costuma ganhar; quando são generative/agentic, Bedrock costuma ganhar.",
    "kubernetes_gke": "GKE Autopilot é diferenciado em ops overhead (literalmente nodeless), mas custo por workload em escala média costuma ser maior do que EKS bem-tunado. Análise foca em ops cost (não só infra cost) — tempo de SRE liberado conta.",
    "full_workload_migration": "Full workload migration AWS→GCP é tipicamente a chamada errada para 60-70% dos casos — egress costs + retreinamento da operação superam o ganho de feature. Análise vai mapear quais workloads de fato beneficiam, e quais ficam melhor com integração cross-cloud.",
    "multi_cloud_strategy": "Multi-cloud is rarely the right answer for cost, often the right answer for resilience and vendor leverage. We'll model both with explicit overhead estimates — typical multi-cloud overhead is 15-30% em ops + 5-15% em infra. Vale a pena quando o leverage de negociação compensa, ou quando regulatory exige.",
}

GCP_DECISION_INSIGHT = {
    "exploring": "Exploring stage: análise mais ampla, com TCO modeling cross-cloud e identificação de 2-3 use cases candidatos. Saída suporta decisão go/no-go.",
    "evaluating": "Evaluating stage: análise mais profunda, com benchmarks de workload específico e POC scope. Saída suporta decisão de qual workload migrar primeiro.",
    "committed": "Committed stage: análise direta vira plano executável — wave plan, account/project setup, migration patterns por workload. Reduz scope analítico, aumenta scope de execução.",
}

GCP_CURRENT_CLOUD_INSIGHT = {
    "aws_primary": "AWS primário: análise foca em egress modeling, equivalência de serviço gerenciado (RDS↔Cloud SQL, Aurora↔Spanner, Redshift↔BigQuery, etc.), e workload affinity (alguns workloads naturalmente preferem GCP, outros AWS).",
    "azure_primary": "Azure primário: cross-cloud Azure↔GCP é menos comum mas tem casos válidos (analytics consolidation em BigQuery, ML em Vertex). Análise mapeia equivalências e gaps.",
    "multi_cloud": "Multi-cloud já em uso: análise foca em consolidation ou expansion — reduzir overhead operacional vs. adicionar capability específica.",
    "on_prem_only": "On-prem only: análise inclui modeling on-prem→GCP direto (raramente faz sentido pular AWS, mas em casos data-gravity com BigQuery o caso fecha).",
}

GCP_INTEREST_INSIGHT_EN = {
    "bigquery_analytics": "BigQuery vs. Redshift comparison hinges on query patterns (BigQuery wins on ad-hoc analytical, Redshift wins on predictable BI). Migration cost is dominated by data egress from AWS — we model real CUR egress before any decision. In ~30% of cases, the best play is federated query (Redshift Spectrum + BigQuery Omni), not full migration.",
    "vertex_ai_ml": "Vertex AI's competitive edge today is in MLOps tooling for traditional ML; for LLM-centric work, AWS Bedrock + SageMaker is typically stronger. We model the workload-specific case — when pipelines are tabular/classical, Vertex usually wins; when they are generative/agentic, Bedrock usually wins.",
    "kubernetes_gke": "GKE Autopilot is differentiated on ops overhead (literally nodeless), but cost per workload at medium scale is usually higher than a well-tuned EKS. Analysis focuses on ops cost (not just infra cost) — SRE time released counts.",
    "full_workload_migration": "Full workload migration AWS→GCP is typically the wrong call for 60-70% of cases — egress costs + retraining the operation outweigh the feature gain. Analysis maps which workloads actually benefit and which are better served by cross-cloud integration.",
    "multi_cloud_strategy": "Multi-cloud is rarely the right answer for cost, often the right answer for resilience and vendor leverage. We model both with explicit overhead estimates — typical multi-cloud overhead is 15-30% in ops + 5-15% in infra. Worth it when negotiation leverage compensates, or when regulators require it.",
}

GCP_DECISION_INSIGHT_EN = {
    "exploring": "Exploring stage: broader analysis, with cross-cloud TCO modeling and identification of 2-3 candidate use cases. Output supports a go/no-go decision.",
    "evaluating": "Evaluating stage: deeper analysis, with workload-specific benchmarks and POC scope. Output supports the decision of which workload to migrate first.",
    "committed": "Committed stage: analysis turns directly into an executable plan — wave plan, account/project setup, migration patterns per workload. Reduces analytical scope, increases execution scope.",
}

GCP_CURRENT_CLOUD_INSIGHT_EN = {
    "aws_primary": "AWS primary: analysis focuses on egress modeling, managed-service equivalence (RDS↔Cloud SQL, Aurora↔Spanner, Redshift↔BigQuery, etc.), and workload affinity (some workloads naturally prefer GCP, others AWS).",
    "azure_primary": "Azure primary: cross-cloud Azure↔GCP is less common but has valid cases (analytics consolidation in BigQuery, ML in Vertex). Analysis maps equivalences and gaps.",
    "multi_cloud": "Multi-cloud already in use: analysis focuses on consolidation or expansion — reducing operational overhead vs. adding specific capability.",
    "on_prem_only": "On-prem only: analysis includes direct on-prem→GCP modeling (rarely makes sense to skip AWS, but in data-gravity cases with BigQuery the case closes).",
}


class GCPMigrationAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    gcp_interest: str
    current_cloud: str
    decision_stage: str
    timeline: str
    context: Optional[str] = ""


def _build_gcp_migration_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    gcp_interest = form_data.get("gcp_interest", "")
    current_cloud = form_data.get("current_cloud", "")
    decision_stage = form_data.get("decision_stage", "")
    timeline = form_data.get("timeline", "")
    if lang == "en":
        interest_insight = GCP_INTEREST_INSIGHT_EN.get(gcp_interest, "")
        decision_insight = GCP_DECISION_INSIGHT_EN.get(decision_stage, "")
        current_insight = GCP_CURRENT_CLOUD_INSIGHT_EN.get(current_cloud, "")
    else:
        interest_insight = GCP_INTEREST_INSIGHT.get(gcp_interest, "")
        decision_insight = GCP_DECISION_INSIGHT.get(decision_stage, "")
        current_insight = GCP_CURRENT_CLOUD_INSIGHT.get(current_cloud, "")

    extra_blocks = []
    if lang == "en":
        if gcp_interest == "multi_cloud_strategy" and current_cloud == "aws_primary":
            extra_blocks.append(
                "Multi-cloud strategy starting from AWS primary: analysis will explicitly model 3 scenarios — (a) AWS-only optimized, (b) selective AWS+GCP (BigQuery / Vertex specific), (c) full multi-cloud. In 60-70% of cases, (b) beats (c) on TCO."
            )
        if gcp_interest == "bigquery_analytics" and current_cloud == "aws_primary":
            extra_blocks.append(
                "BigQuery analytics starting from AWS: data gravity is the decisive factor. If 80%+ of data originates on AWS, federated query (Redshift Spectrum + BigQuery Omni) usually beats migration. If data is born distributed or in SaaS, BigQuery centralizes better."
            )
        if decision_stage == "committed" and timeline == "exploratory":
            extra_blocks.append(
                "'Committed' + 'exploratory timeline' is an uncommon combination — analysis will validate the commitment before spending time on an open-ended timeline. In ~30% of cases, the decision reverses after real TCO."
            )
        if timeline == "under_3m":
            extra_blocks.append(
                "Sub-3-month timeline: analysis compresses and focuses on an executable decision. Technical detail moves to a follow-on sprint. Only recommended when the case is clear (regulatory, EOL, M&A)."
            )
    else:
        if gcp_interest == "multi_cloud_strategy" and current_cloud == "aws_primary":
            extra_blocks.append(
                "Multi-cloud strategy partindo de AWS primário: análise modelará explicitamente 3 cenários — (a) AWS-only otimizado, (b) AWS+GCP seletivo (BigQuery / Vertex specific), (c) full multi-cloud. Em 60-70% dos casos, (b) bate (c) em TCO."
            )
        if gcp_interest == "bigquery_analytics" and current_cloud == "aws_primary":
            extra_blocks.append(
                "BigQuery analytics partindo de AWS: data gravity é o fator decisivo. Se 80%+ do dado nasce na AWS, federated query (Redshift Spectrum + BigQuery Omni) costuma bater migration. Se nasce distribuído ou em SaaS, BigQuery centraliza melhor."
            )
        if decision_stage == "committed" and timeline == "exploratory":
            extra_blocks.append(
                "Combinação 'committed' + 'exploratory timeline' é incomum — análise vai validar o committed antes de gastar tempo em timeline open-ended. Em ~30% dos casos, a decisão volta atrás depois do TCO real."
            )
        if timeline == "under_3m":
            extra_blocks.append(
                "Timeline sub-3 meses: análise comprime e foca em decisão executável. Detalhamento técnico fica em sprint follow-on. Recomendável só quando o caso é claro (regulatory, EOL, M&A)."
            )

    extras_label = "Specific signals from your answers" if lang == "en" else "Sinais específicos das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        gcp_price = format_price("R$ 40-90k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">About your primary GCP interest</p>
    <p class="text-ink/80 leading-relaxed">{interest_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About your current environment</p>
    <p class="text-ink/80 leading-relaxed">{current_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About the decision stage</p>
    <p class="text-ink/80 leading-relaxed">{decision_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Anuvia GCP Migration Strategy: 4-8 weeks, {gcp_price}. Cross-cloud comparison, TCO modeling, data gravity analysis, vendor lock-in assessment. Led by ex-Google + ex-AWS — technical advice not committed to a single vendor.</p>
</div>
""".strip()
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre o interesse principal em GCP</p>
    <p class="text-ink/80 leading-relaxed">{interest_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o ambiente atual</p>
    <p class="text-ink/80 leading-relaxed">{current_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre o estágio de decisão</p>
    <p class="text-ink/80 leading-relaxed">{decision_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">GCP Migration Strategy Anuvia: 4-8 semanas, R$ 40-90k. Cross-cloud comparison, TCO modeling, data gravity analysis, vendor lock-in assessment. Conduzido por ex-Google + ex-AWS — conselho técnico não comprometido com vendor único.</p>
</div>
""".strip()
    return {
        "gcp_interest": gcp_interest,
        "current_cloud": current_cloud,
        "decision_stage": decision_stage,
        "timeline": timeline,
    }, html


@app.post("/api/gcp-migration/analyze")
async def api_gcp_migration_analyze(form: GCPMigrationAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_gcp_migration_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "gcp_interest": form.gcp_interest,
        "current_cloud": form.current_cloud,
        "decision_stage": form.decision_stage,
        "timeline": form.timeline,
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_GCP_MIG",
            source="lp_gcp_migration",
            diag_type="gcp_migration",
            market=get_locale(request)["market"],
            track="cloud_gcp_migration",
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_gcp_migration", "cloud_gcp_migration"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html})


@app.post("/api/gcp-migration/contact")
async def api_gcp_migration_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_gcp_migration_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"GCP Migration Strategy — preliminary analysis for {first}"
        else:
            subject = f"GCP Migration Strategy — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "GCP Migration", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "GCP Migration Strategy", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Interest: {meta.get('gcp_interest', '?')}  ·  Current: {meta.get('current_cloud', '?')}",
                f"Stage: {meta.get('decision_stage', '?')}  ·  Timeline: {meta.get('timeline', '?')}",
            ],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


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
    mttr: Optional[str] = ""
    context: Optional[str] = ""


def _build_devops_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    deploy_freq = form_data.get("deploy_freq", "")
    main_pain = form_data.get("main_pain", "")
    mttr = form_data.get("mttr", "") or ""
    if lang == "en":
        level, level_insight = DORA_LEVEL_EN.get(deploy_freq, ("?", ""))
        pain_insight = DEVOPS_PAIN_INSIGHT_EN.get(main_pain, "")
    else:
        level, level_insight = DORA_LEVEL.get(deploy_freq, ("?", ""))
        pain_insight = DEVOPS_PAIN_INSIGHT.get(main_pain, "")

    extra_blocks = []
    if lang == "en":
        if mttr == "above_8h":
            extra_blocks.append("MTTR above 8h puts the team below DORA Medium. Typical root cause: no documented runbook, observability limited to CloudWatch dashboards (no traces), diffuse ownership. The assessment details these three fronts.")
        elif mttr == "2h_8h":
            extra_blocks.append("MTTR between 2-8h is consistent with DORA Medium. Usually there is observability but no distributed tracing — discovery time dominates mitigation time. Typical gain of 40-60% in MTTR after introducing OpenTelemetry with proper sampling.")
        elif mttr == "dont_track":
            extra_blocks.append("Unmeasured MTTR is the most common gap (DORA 2023 reports ~35% of teams in this state). The assessment instruments collection from the paging system + incident tracker — in 2-3 weeks you have a real baseline.")
        elif mttr == "under_30min":
            extra_blocks.append("Sub-30min MTTR indicates consolidated investment in observability and runbooks. The assessment focuses on the other 3 DORA vectors (lead time, change failure rate, deploy frequency) and on-call sustainability.")
    else:
        if mttr == "above_8h":
            extra_blocks.append("MTTR acima de 8h coloca o time abaixo do Medium DORA. Causa raiz típica: ausência de runbook documentado, observability limitada a CloudWatch dashboards (sem traces), ownership difuso. Assessment vai detalhar essas três frentes.")
        elif mttr == "2h_8h":
            extra_blocks.append("MTTR entre 2-8h é compatível com DORA Medium. Geralmente há observability mas sem distributed tracing — o tempo de descoberta domina o tempo de mitigação. Ganho típico de 40-60% em MTTR ao introduzir OpenTelemetry com sampling adequado.")
        elif mttr == "dont_track":
            extra_blocks.append("MTTR não medido é o gap mais comum (DORA 2023 reporta ~35% dos times nessa situação). O assessment vai instrumentar a coleta a partir do paging system + incident tracker — em 2-3 semanas você tem baseline real.")
        elif mttr == "under_30min":
            extra_blocks.append("MTTR sub-30min indica investimento em observability e runbooks já consolidado. Assessment vai focar nos outros 3 vetores DORA (lead time, change failure rate, deploy frequency) e na sustentabilidade do on-call.")

    extras_label = "Specific signal from the MTTR you indicated" if lang == "en" else "Sinal específico do MTTR informado"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        devops_price = format_price("R$ 35-50k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">DORA preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Preliminary DORA level</p>
    <p class="h-serif text-5xl mb-2">{level}</p>
    <p class="text-sm text-ink/70 leading-relaxed">{level_insight}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About your stated concern</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">DevOps Maturity Assessment: 4 weeks, {devops_price}. Delivers a DORA baseline measured from your own artifacts (CI logs, git, paging, incident tracker) + gap analysis + 6-month roadmap.</p>
</div>
""".strip()
    else:
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
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">DevOps Maturity Assessment: 4 semanas, R$ 35-50k. Entrega DORA baseline medido a partir dos seus próprios artefatos (CI logs, git, paging, incident tracker) + gap analysis + 6-month roadmap.</p>
</div>
""".strip()
    return {"dora_level": level, "deploy_freq": deploy_freq, "main_pain": main_pain, "mttr": mttr}, html


@app.post("/api/devops-maturity/analyze")
async def api_devops_analyze(form: DevOpsAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_devops_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "team_size": form.team_size,
        "deploy_freq": form.deploy_freq,
        "main_pain": form.main_pain,
        "stack": form.stack or "",
        "mttr": form.mttr or "",
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
async def api_devops_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_devops_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"DevOps Maturity — preliminary analysis for {first}"
        else:
            subject = f"DevOps Maturity — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "DevOps", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "DevOps Maturity", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[f"DORA: {meta.get('dora_level', '?')}  ·  Deploy: {meta.get('deploy_freq', '?')}"],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# AI Readiness — diagnostic-first flow
# ----------------------------------------------------------------------------

class AIReadinessAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    ai_stage: str
    main_pain: str
    revenue_tier: str
    data_readiness: Optional[str] = ""
    build_vs_buy: Optional[str] = ""
    context: Optional[str] = ""


def _build_ai_readiness_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    ai_stage = form_data.get("ai_stage", "")
    main_pain = form_data.get("main_pain", "")
    revenue_tier = form_data.get("revenue_tier", "")
    data_readiness = form_data.get("data_readiness", "") or ""
    build_vs_buy = form_data.get("build_vs_buy", "") or ""
    if lang == "en":
        stage_insight = AI_STAGE_INSIGHT_EN.get(ai_stage, "")
        pain_insight = AI_PAIN_INSIGHT_EN.get(main_pain, "")
    else:
        stage_insight = AI_STAGE_INSIGHT.get(ai_stage, "")
        pain_insight = AI_PAIN_INSIGHT.get(main_pain, "")
    is_fit = revenue_tier in ("5m_30m", "30m_100m", "100m_plus")

    extra_blocks = []
    if lang == "en":
        if data_readiness == "no_data_yet":
            extra_blocks.append("No production data yet — in practice, the first AI case has to be a capture/instrumentation case. The Sprint flags this as a precondition and prioritizes use cases that tolerate cold-start (RAG over existing documents, few-shot classification).")
        elif data_readiness == "data_distributed_partial_quality":
            extra_blocks.append("Distributed data with partial quality is the most common gap — 60-70% of effort on a typical case is data preparation, not modeling. The Sprint scopes this explicitly in the per-case ROI.")
        elif data_readiness == "data_clean_centralized":
            extra_blocks.append("Clean, centralized data: precondition met. The Sprint focuses on selecting use cases that exploit this advantage (RAG, supervised classification, tool-using agents).")
        if build_vs_buy == "prefer_build":
            extra_blocks.append("Preference for internal build: the Sprint weighs 12-month TCO against SaaS-equivalent options. In ~40% of cases, build is justified by proprietary data or compliance; in the remaining 60%, specialized SaaS wins on ROI.")
        elif build_vs_buy == "prefer_buy":
            extra_blocks.append("Preference for buy: the Sprint lists applicable SaaS per case, weighted for vendor lock-in, data exportability, and compliance. Buy is still the right call in ~60% of generic cases, but vertical-specific use cases often demand build.")
    else:
        if data_readiness == "no_data_yet":
            extra_blocks.append("Sem dado de produção ainda — o primeiro caso de IA precisa ser, na prática, um caso de captura/instrumentação. Sprint vai marcar isso como pré-condição e priorizar use cases que toleram cold-start (RAG sobre documentos já existentes, classificação por few-shot).")
        elif data_readiness == "data_distributed_partial_quality":
            extra_blocks.append("Dado distribuído com qualidade parcial é o gap mais comum — 60-70% do esforço de um caso típico vira preparação de dado, não modelagem. Sprint vai escopar isso explicitamente no ROI por caso.")
        elif data_readiness == "data_clean_centralized":
            extra_blocks.append("Dado limpo e centralizado: pré-condição satisfeita. Sprint foca em selecionar use cases que aproveitam essa vantagem (RAG, classificação supervisionada, agentes com tool-use).")
        if build_vs_buy == "prefer_build":
            extra_blocks.append("Preferência por build interno: Sprint vai pesar TCO em 12 meses contra opções SaaS-equivalentes. Em ~40% dos casos, build acaba justificado por dado proprietário ou compliance; nos demais 60%, buy de SaaS especializado bate em ROI.")
        elif build_vs_buy == "prefer_buy":
            extra_blocks.append("Preferência por buy: Sprint vai listar SaaS aplicáveis por caso, com pesos para vendor lock-in, exportabilidade de dado e compliance. Buy ainda é a chamada certa em ~60% dos casos genéricos, mas use cases vertical-específicos costumam exigir build.")

    extras_label = "Signals from your answers" if lang == "en" else "Sinais das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."

    if is_fit:
        if lang == "en":
            sprint_price = format_price("R$ 25-40k", "USD")
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">About your current stage</p>
    <p class="text-ink/80 leading-relaxed">{stage_insight}</p>
  </div>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">About your stated concern</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AI Readiness Sprint: 2-3 weeks, {sprint_price}. Delivers an inventory of 8-15 scored use cases (data readiness, inference cost, latency, compliance) + estimated ROI + 12-month roadmap + per-case build vs buy decision.</p>
</div>
""".strip()
        else:
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
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">AI Readiness Sprint: 2-3 semanas, R$ 25-40k. Entrega inventário de 8-15 use cases pontuados (data readiness, custo de inferência, latência, compliance) + ROI estimado + roadmap 12 meses + decisão build vs buy por caso.</p>
</div>
""".strip()
    else:
        if lang == "en":
            sprint_price = format_price("R$ 25-40k", "USD")
            oh_price = format_price("R$ 1.500", "USD")
            qw_price = format_price("R$ 8-15k", "USD")
            ops_price = format_price("R$ 3-8k", "USD")
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">Given your revenue profile, a full Sprint at {sprint_price} likely will not clear the ROI required right now.</p>
  <p class="text-ink/80 leading-relaxed mb-5">More appropriate alternatives:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>90-min office hours with Mila</strong> — {oh_price}.</li>
    <li>• <strong>AI Quick Win</strong> — {qw_price}, 2-3 weeks.</li>
    <li>• <strong>Anuvia AI Ops</strong> — recurring agent squad, {ops_price}/month.</li>
  </ul>
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
    return {
        "ai_stage": ai_stage,
        "main_pain": main_pain,
        "revenue_tier": revenue_tier,
        "is_fit": is_fit,
        "data_readiness": data_readiness,
        "build_vs_buy": build_vs_buy,
    }, html


@app.post("/api/ai-readiness/analyze")
async def api_ai_readiness_analyze(form: AIReadinessAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_ai_readiness_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "ai_stage": form.ai_stage,
        "main_pain": form.main_pain,
        "revenue_tier": form.revenue_tier,
        "data_readiness": form.data_readiness or "",
        "build_vs_buy": form.build_vs_buy or "",
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
async def api_ai_readiness_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_ai_readiness_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        if lang == "en":
            subject = f"AI Readiness Sprint — preliminary analysis for {first}" if is_fit else f"Thanks, {first} — alternatives for your case"
        else:
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
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# Growth Sales Ops — diagnostic-first flow
# ----------------------------------------------------------------------------

class GrowthAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    team_size: str
    ticket_size: str
    main_pain: str
    crm: Optional[str] = ""
    sales_cycle: Optional[str] = ""
    context: Optional[str] = ""


def _build_growth_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    ticket_size = form_data.get("ticket_size", "")
    main_pain = form_data.get("main_pain", "")
    crm = form_data.get("crm", "") or ""
    sales_cycle = form_data.get("sales_cycle", "") or ""
    if lang == "en":
        pain_insight = SALESOPS_PAIN_INSIGHT_EN.get(main_pain, "")
    else:
        pain_insight = SALESOPS_PAIN_INSIGHT.get(main_pain, "")
    is_fit = ticket_size in ("5k_25k", "25k_100k", "100k_plus")

    extra_blocks = []
    if lang == "en":
        if crm == "spreadsheet" or crm == "none":
            extra_blocks.append("Operation without a real CRM: the first Diagnostic deliverable will include a CRM recommendation compatible with your ticket and cycle. Skipping this step makes most downstream automation fragile.")
        elif crm in ("hubspot", "salesforce", "pipedrive", "rd_station"):
            extra_blocks.append(f"{crm.replace('_', ' ').title()} CRM in use: the Diagnostic will audit pipeline stages, unused native automation, and qualified-stage data quality. Typical pattern: ~40% of qualification fields are not consistently populated.")
        if sales_cycle == "under_30d":
            extra_blocks.append("Short cycle (sub-30 days): response time dominates conversion. SLA measured per channel (form, WhatsApp, referral) is the most leverageable metric — early qualification automation has higher ROI than late follow-up automation.")
        elif sales_cycle == "above_180d":
            extra_blocks.append("Long cycle (above 180 days): nurture and attribution dominate. Lead leakage between touchpoints is the main cause of drop. The Diagnostic will map handoff gaps and SLA per stage, not just per inbound channel.")
    else:
        if crm == "spreadsheet" or crm == "none":
            extra_blocks.append("Operação sem CRM real: o primeiro entregável do Diagnostic vai incluir uma recomendação de CRM compatível com seu ticket e ciclo. Saltar essa etapa torna a maioria das automações posteriores frágil.")
        elif crm in ("hubspot", "salesforce", "pipedrive", "rd_station"):
            extra_blocks.append(f"CRM {crm.replace('_', ' ').title()} em uso: Diagnostic auditará pipeline stages, automação nativa não-aproveitada e qualidade dos dados de qualified-stage. Padrão típico: ~40% dos campos de qualificação não preenchidos consistentemente.")
        if sales_cycle == "under_30d":
            extra_blocks.append("Ciclo curto (sub-30 dias): tempo de resposta domina conversão. SLA mensurado por canal (form, WhatsApp, indicação) é a métrica mais alavancável — automação de qualificação inicial tem ROI mais alto que automação de follow-up tardio.")
        elif sales_cycle == "above_180d":
            extra_blocks.append("Ciclo longo (acima de 180 dias): nurture e attribution dominam. Lead leakage entre touchpoints é a principal causa de drop. Diagnostic vai mapear gaps de handoff e SLA por estágio, não só por canal de entrada.")

    extras_label = "Signals from your answers" if lang == "en" else "Sinais das suas respostas"
    extras_html = ""
    if extra_blocks:
        items = "".join(f'<li class="text-ink/80 leading-relaxed">{b}</li>' for b in extra_blocks)
        extras_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{extras_label}</p>
    <ul class="space-y-3 text-sm list-disc list-inside">{items}</ul>
  </div>
"""
    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."

    if is_fit:
        if lang == "en":
            diag_price = format_price("R$ 15-25k", "USD")
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">About your stated concern</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Sales Ops Diagnostic: 2 weeks, {diag_price}. Delivers funnel map + automation playbook + 90-day roadmap.</p>
</div>
""".strip()
        else:
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Sobre sua dor declarada</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{extras_html}
  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Sales Ops Diagnostic: 2 semanas, R$ 15-25k. Entrega funnel map + automation playbook + roadmap 90 dias.</p>
</div>
""".strip()
    else:
        if lang == "en":
            ticket_floor = format_price("R$ 5k", "USD")
            diag_price = format_price("R$ 15-25k", "USD")
            qw_price = format_price("R$ 8-15k", "USD")
            ops_price = format_price("R$ 3-8k", "USD")
            oh_price = format_price("R$ 1.500", "USD")
            html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Preliminary analysis</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>
  <p class="text-ink/80 leading-relaxed mb-5">With a ticket below {ticket_floor}, the full {diag_price} diagnostic likely will not clear the ROI required in the short term.</p>
  <p class="text-ink/80 leading-relaxed mb-5">More appropriate alternatives:</p>
  <ul class="space-y-2 text-sm text-ink/80 mb-5">
    <li>• <strong>AI Quick Win</strong> — {qw_price}, 2-3 weeks.</li>
    <li>• <strong>Anuvia AI Ops Subscription</strong> — {ops_price}/month.</li>
    <li>• <strong>90-min office hours with Mila</strong> — {oh_price}.</li>
  </ul>
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
    return {
        "ticket_size": ticket_size,
        "main_pain": main_pain,
        "is_fit": is_fit,
        "crm": crm,
        "sales_cycle": sales_cycle,
    }, html


@app.post("/api/growth-sales-ops/analyze")
async def api_growth_analyze(form: GrowthAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_growth_deliverable(form_data, lang=lang)
    # Track B autonomous classification reads signals from qualification_data —
    # persist any extras the client sent (budget_declared, budget_amount, urgency,
    # company_size, etc.) so classify_track can act on them.
    extras = getattr(form, "model_extra", None) or {}
    business_meta = {
        "role": form.role,
        "team_size": form.team_size,
        "ticket_size": form.ticket_size,
        "main_pain": form.main_pain,
        "crm": form.crm or "",
        "sales_cycle": form.sales_cycle or "",
        "context": form.context or "",
        **extras,
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
async def api_growth_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_growth_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        is_fit = meta.get("is_fit", True)
        if lang == "en":
            subject = f"Sales Ops Diagnostic — preliminary analysis for {first}" if is_fit else f"Thanks, {first} — alternatives for your case"
        else:
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
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


# ----------------------------------------------------------------------------
# Industry Assessment — vertical playbook fit diagnostic
# ----------------------------------------------------------------------------

INDUSTRY_VERTICAL_INSIGHT = {
    "manufacturing": {
        "label": "Manufacturing",
        "playbook": "Manufatura tem playbook consolidado: predictive maintenance a partir de streams existentes de PLC/sensor, integração MES/ERP, dashboards de OEE, inspeção de qualidade por visão computacional.",
        "preconditions": "ROI alto exige (a) histórico de falha por ativo de pelo menos 12 meses, (b) CMMS já operando e (c) capacidade de gerar OT/work-order automaticamente. Sem essas três pré-condições, ROI cai rapidamente.",
        "compliance": "Compliance setorial baixo — quando aplicável, costuma ser ISO 9001/14001 e LGPD para dado de operador. Não bloqueia engagement.",
        "depth": "Profundo. Anuvia tem reference architecture, code skeletons para integração OPC-UA / Modbus, e dashboards OEE pré-montados.",
    },
    "logistics": {
        "label": "Logistics &amp; supply chain",
        "playbook": "Logística tem playbook em telemetria offline-first com fallback satelital, ML para otimização de rota/ETA, e last-mile tracking. Stack típica: edge devices com SQLite local + sync incremental, ML hosted em SageMaker.",
        "preconditions": "ROI alto exige diversidade real de rota (mix rural/urbano), volume de >50 veículos OU >5k pacotes/dia, e dado histórico de entregas com timestamp e geolocalização confiável.",
        "compliance": "LGPD para dado de motorista e cliente. Baixo bloqueio regulatório operacional.",
        "depth": "Médio-alto. Telemetria edge é o componente mais reutilizável; otimização de rota costuma exigir adaptação significativa ao contexto.",
    },
    "healthcare": {
        "label": "Healthcare provider",
        "playbook": "Healthcare provider tem playbook em assistentes de documentação clínica (reduzem tempo de documentação por consulta), RAG sobre protocolos institucionais, e triagem em intake. Tudo com auditabilidade por consulta.",
        "preconditions": "ROI alto exige acesso a EHR/prontuário eletrônico, fluxo de intake bem definido, e patrocínio médico forte (não só TI). Sem médico campeão, projeto perde tração rápido.",
        "compliance": "LGPD-saúde, CFM 2.314/2022, regulamentação ANS aplicável. Anuvia entrega controles desde a arquitetura — não retrofitados sob pressão de auditoria.",
        "depth": "Médio-alto. Documentação clínica é altamente reutilizável; integração com EHR varia muito por fornecedor (TASY, MV, Philips).",
    },
    "life_sciences": {
        "label": "Life Sciences (pharma / biotech)",
        "playbook": "Life Sciences tem playbook em automação de SOPs, drafting de documentação regulatória (CTD modules, IND/IMPD sections), e recuperação estruturada sobre literatura técnica. GxP é tratado como pré-requisito, não opcional.",
        "preconditions": "ROI alto exige governança documental já estruturada, departamento regulatório com bandwidth para revisar drafts, e patrocínio executivo. Sem isso, ferramenta vira shelfware.",
        "compliance": "GxP (GMP, GLP, GCP), ANVISA RDC aplicáveis, FDA 21 CFR Part 11 quando exportador. Anuvia entrega validation package — artefatos IQ/OQ/PQ — bundled com a solução.",
        "depth": "Profundo. Reuso significativo de SOPs e templates regulatórios entre clientes; vertical mais codificada da Anuvia.",
    },
    "finserv": {
        "label": "Financial services",
        "playbook": "FinServ tem playbook em detecção de fraude em tempo real, monitoramento AML, onboarding KYC, e RAG sobre normativos (BACEN, CVM, ANBIMA). Latência sub-segundo é tratada como requisito de arquitetura.",
        "preconditions": "ROI alto exige stream de transações com campos de identidade confiáveis, histórico de fraude para treino, e área de compliance disposta a co-validar modelos. Sem dado rotulado de fraude, fica-se em rule-based.",
        "compliance": "BACEN Res. 4.658 (cibersegurança), LGPD, SOC 2 quando aplicável, regulamentações específicas para fintechs. Controles alinhados desde a arquitetura.",
        "depth": "Médio-alto. Fraude/AML reutiliza muito; KYC depende fortemente de integração com bureaus específicos do mercado.",
    },
    "other": {
        "label": "Other vertical",
        "playbook": "Vertical fora dos cinco playbooks consolidados (manufatura, logística, healthcare, Life Sciences, FinServ). Engagement seria construído como projeto bespoke de Anuvia AI Engineering — sem reuso de kit pré-pronto.",
        "preconditions": "Pré-condições gerais: dado proprietário relevante, caso de uso medível, patrocínio executivo e budget para production-grade.",
        "compliance": "Avaliado caso a caso.",
        "depth": "Sem profundidade vertical pré-acumulada. Ticket tende a ser maior pelo trabalho extra de discovery e desenho.",
    },
}


INDUSTRY_PAIN_INSIGHT = {
    "unplanned_downtime": "Downtime não planejado é o caso mais maduro de predictive maintenance — ganhos típicos de 10-25% em availability quando pré-condições estão no lugar. Quando não estão, projeto fica em 0% e vira shelfware.",
    "quality_inspection": "Inspeção por visão computacional tem ROI rápido em linhas de produção com defeito visível e cadência alta. Para defeitos sub-visuais (térmicos, ultrassom), o ROI exige sensoreamento adicional antes do modelo.",
    "supply_visibility": "Visibilidade de supply chain costuma ter retorno alto em controle de ruptura e SLA com cliente. Pré-requisito: integração de dado dos parceiros logísticos — geralmente o gargalo, não o modelo.",
    "route_optimization": "Otimização de rota com ML costuma trazer 8-15% de redução em distância/tempo sobre baseline manual. ROI fica abaixo disso em frota pequena (<30 veículos) ou rotas muito uniformes.",
    "document_processing": "Processamento documental é o caso de uso mais transversal e com tempo-de-valor mais curto. RAG + extração estruturada típica reduz tempo de processamento em 60-80%.",
    "clinical_documentation": "Documentação clínica é o caso de IA mais alavancável em healthcare provider — reduz tempo médico por consulta em 30-50% quando integrado nativamente ao EHR. Sem integração, vira ferramenta paralela e adoção colapsa.",
    "regulatory_reporting": "Reporte regulatório (GxP, BACEN, ANVISA) tem ROI muito alto onde há volume e linguagem padronizada. Pré-requisito é governança documental já estruturada, senão entrega documento que ninguém audita.",
    "fraud_aml": "Detecção de fraude/AML é o caso mais maduro de ML em FinServ. ROI exige dado de fraude rotulado historicamente (volume e qualidade) — sem isso, fica em rule-based augmentado, não em ML supervisionado real.",
    "kyc_onboarding": "KYC automation reduz tempo de onboarding em 60-80% quando integrado a bureaus já contratados. Sem essa integração, ganho colapsa para 20-30%.",
    "other": "Pain descrita como 'outra' — Sprint vai escopar o caso individualmente antes de qualquer estimativa de ROI. Próximo passo é a conversa de 30 minutos.",
}

INDUSTRY_PAIN_INSIGHT_EN = {
    "unplanned_downtime": "Unplanned downtime is the most mature predictive-maintenance case — typical gains of 10-25% in availability when preconditions are in place. When they aren't, the project lands at 0% and becomes shelfware.",
    "quality_inspection": "Computer vision inspection has fast ROI on production lines with visible defects and high cadence. For sub-visual defects (thermal, ultrasound), ROI requires additional sensing before the model.",
    "supply_visibility": "Supply chain visibility usually has high return on stockout control and customer SLA. Prerequisite: data integration with logistics partners — usually the bottleneck, not the model.",
    "route_optimization": "ML-based route optimization usually delivers 8-15% reduction in distance/time over manual baseline. ROI is below that on small fleets (<30 vehicles) or very uniform routes.",
    "document_processing": "Document processing is the most cross-cutting use case with the shortest time-to-value. Typical RAG + structured extraction reduces processing time by 60-80%.",
    "clinical_documentation": "Clinical documentation is the most leverageable AI case in healthcare provider — reduces physician time per visit by 30-50% when natively integrated with the EHR. Without integration, it becomes a parallel tool and adoption collapses.",
    "regulatory_reporting": "Regulatory reporting (GxP, BACEN, ANVISA) has very high ROI where volume and standardized language exist. Prerequisite is structured document governance, otherwise it produces documents no one audits.",
    "fraud_aml": "Fraud/AML detection is the most mature ML case in FinServ. ROI requires historically labeled fraud data (volume and quality) — without it, you end up in augmented rule-based, not real supervised ML.",
    "kyc_onboarding": "KYC automation cuts onboarding time by 60-80% when integrated with already-contracted bureaus. Without that integration, the gain collapses to 20-30%.",
    "other": "Pain described as 'other' — the Sprint will scope the case individually before any ROI estimate. Next step is the 30-minute conversation.",
}

INDUSTRY_VERTICAL_INSIGHT_EN = {
    "manufacturing": {
        "label": "Manufacturing",
        "playbook": "Manufacturing has a consolidated playbook: predictive maintenance from existing PLC/sensor streams, MES/ERP integration, OEE dashboards, computer-vision quality inspection.",
        "preconditions": "High ROI requires (a) per-asset failure history of at least 12 months, (b) CMMS already operating, and (c) ability to generate WO/work-order automatically. Without those three preconditions, ROI drops quickly.",
        "compliance": "Low sector compliance — when applicable, usually ISO 9001/14001 and LGPD for operator data. Does not block the engagement.",
        "depth": "Deep. Anuvia has reference architecture, code skeletons for OPC-UA / Modbus integration, and pre-built OEE dashboards.",
    },
    "logistics": {
        "label": "Logistics &amp; supply chain",
        "playbook": "Logistics has a playbook in offline-first telemetry with satellite fallback, ML for route/ETA optimization, and last-mile tracking. Typical stack: edge devices with local SQLite + incremental sync, ML hosted on SageMaker.",
        "preconditions": "High ROI requires real route diversity (rural/urban mix), volume of >50 vehicles OR >5k packages/day, and historical delivery data with reliable timestamp and geolocation.",
        "compliance": "LGPD for driver and customer data. Low operational regulatory blocking.",
        "depth": "Medium-high. Edge telemetry is the most reusable component; route optimization usually requires significant context adaptation.",
    },
    "healthcare": {
        "label": "Healthcare provider",
        "playbook": "Healthcare provider has a playbook in clinical documentation assistants (reduce documentation time per visit), RAG over institutional protocols, and intake triage. All with per-visit auditability.",
        "preconditions": "High ROI requires access to the EHR, a well-defined intake flow, and strong physician sponsorship (not just IT). Without a physician champion, the project loses traction quickly.",
        "compliance": "Healthcare LGPD, CFM 2.314/2022, applicable ANS regulation. Anuvia delivers controls from the architecture — not retrofitted under audit pressure.",
        "depth": "Medium-high. Clinical documentation is highly reusable; EHR integration varies a lot by vendor (TASY, MV, Philips).",
    },
    "life_sciences": {
        "label": "Life Sciences (pharma / biotech)",
        "playbook": "Life Sciences has a playbook in SOP automation, regulatory documentation drafting (CTD modules, IND/IMPD sections), and structured retrieval over technical literature. GxP is treated as a prerequisite, not optional.",
        "preconditions": "High ROI requires already-structured documentation governance, regulatory department with bandwidth to review drafts, and executive sponsorship. Without that, the tool becomes shelfware.",
        "compliance": "GxP (GMP, GLP, GCP), applicable ANVISA RDC, FDA 21 CFR Part 11 when exporting. Anuvia delivers the validation package — IQ/OQ/PQ artifacts — bundled with the solution.",
        "depth": "Deep. Significant reuse of SOPs and regulatory templates across clients; most codified vertical at Anuvia.",
    },
    "finserv": {
        "label": "Financial services",
        "playbook": "FinServ has a playbook in real-time fraud detection, AML monitoring, KYC onboarding, and RAG over regulations (BACEN, CVM, ANBIMA). Sub-second latency is treated as an architecture requirement.",
        "preconditions": "High ROI requires a transaction stream with reliable identity fields, fraud history for training, and a compliance team willing to co-validate models. Without labeled fraud data, you stay rule-based.",
        "compliance": "BACEN Res. 4.658 (cybersecurity), LGPD, SOC 2 when applicable, fintech-specific regulation. Controls aligned from the architecture.",
        "depth": "Medium-high. Fraud/AML reuses extensively; KYC depends heavily on integration with market-specific bureaus.",
    },
    "other": {
        "label": "Other vertical",
        "playbook": "Vertical outside the five consolidated playbooks (manufacturing, logistics, healthcare, Life Sciences, FinServ). The engagement would be built as a bespoke Anuvia AI Engineering project — no pre-built kit reuse.",
        "preconditions": "General preconditions: relevant proprietary data, a measurable use case, executive sponsorship, and budget for production-grade.",
        "compliance": "Evaluated case by case.",
        "depth": "No pre-accumulated vertical depth. Ticket tends to be higher due to the extra discovery and design work.",
    },
}


INDUSTRY_SIZE_TICKET = {
    "under_100m": ("Below R$100M / Below $20M", "R$ 60-120k pilot, R$ 5-12k/month ongoing", "$12-24k pilot, $1-2.4k/month"),
    "100m_500m": ("R$100M-500M / $20M-100M", "R$ 100-250k pilot, R$ 8-15k/month ongoing", "$20-50k pilot, $1.5-3k/month"),
    "500m_2b": ("R$500M-2B / $100M-400M", "R$ 200-400k pilot, R$ 12-25k/month ongoing", "$40-80k pilot, $2.5-5k/month"),
    "above_2b": ("Above R$2B / Above $400M", "R$ 300-600k pilot, R$ 20-40k/month ongoing", "$60-120k pilot, $4-8k/month"),
}


class IndustryAnalyzeForm(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    vertical: str
    company_size: str
    ai_maturity: str
    main_pain: str
    compliance: Optional[str] = ""
    context: Optional[str] = ""


def _build_industry_deliverable(form_data: dict, with_name: Optional[str] = None, lang: str = "pt") -> tuple[dict, str]:
    vertical = form_data.get("vertical", "")
    company_size = form_data.get("company_size", "")
    ai_maturity = form_data.get("ai_maturity", "")
    main_pain = form_data.get("main_pain", "")
    compliance = (form_data.get("compliance", "") or "").strip()

    if lang == "en":
        vinfo = INDUSTRY_VERTICAL_INSIGHT_EN.get(vertical, INDUSTRY_VERTICAL_INSIGHT_EN["other"])
        pain_insight = INDUSTRY_PAIN_INSIGHT_EN.get(main_pain, "")
    else:
        vinfo = INDUSTRY_VERTICAL_INSIGHT.get(vertical, INDUSTRY_VERTICAL_INSIGHT["other"])
        pain_insight = INDUSTRY_PAIN_INSIGHT.get(main_pain, "")
    size_label, ticket_brl, ticket_usd = INDUSTRY_SIZE_TICKET.get(
        company_size, (company_size, "Custom-quoted", "Custom-quoted")
    )

    maturity_note = ""
    if lang == "en":
        readiness_price = format_price("R$ 25-40k", "USD")
        if ai_maturity == "none":
            maturity_note = f"Initial AI maturity — typical recommendation is AI Readiness Sprint ({readiness_price}, 2-3 weeks) before the vertical pilot, to select the best-ROI case based on data from your environment."
        elif ai_maturity == "exploring":
            maturity_note = "PoCs running but nothing in production — typical gap is in eval harness, rollback procedure, and cost cap. Anuvia's vertical pilot ships with those three elements in the architecture from day one."
        elif ai_maturity == "early_prod":
            maturity_note = "1-2 cases in production — ready to scale with a new vertical, as long as existing governance and monitoring can absorb it. The Diagnostic validates this before proposing a pilot."
        elif ai_maturity == "scaling":
            maturity_note = "Multiple cases in production — engagement with Anuvia makes more sense in retainer or platform-engineering mode, not as an isolated pilot. A 30-min conversation calibrates that."
        elif ai_maturity == "mature":
            maturity_note = "Mature operation with internal ML/AI team — Anuvia enters as advisory or on specific cases that demand vertical depth the internal team does not cover."
    else:
        if ai_maturity == "none":
            maturity_note = "Maturidade IA inicial — recomendação típica é AI Readiness Sprint (R$ 25-40k, 2-3 semanas) antes do pilot vertical, para escolher o caso de melhor ROI baseado em dados do seu ambiente."
        elif ai_maturity == "exploring":
            maturity_note = "PoCs rodando mas nada em produção — gap típico está em eval harness, rollback procedure e cost cap. O pilot vertical da Anuvia já entra com esses três elementos na arquitetura."
        elif ai_maturity == "early_prod":
            maturity_note = "1-2 casos em produção — pronto para escalar com novo vertical, desde que governança e monitoring existentes possam absorver. Diagnostic vai validar antes de propor pilot."
        elif ai_maturity == "scaling":
            maturity_note = "Múltiplos casos em produção — engagement com Anuvia faz mais sentido em modo retainer ou platform-engineering, não em pilot isolado. Conversa de 30 min vai calibrar isso."
        elif ai_maturity == "mature":
            maturity_note = "Operação madura com time ML/IA interno — Anuvia entra como advisory ou em casos específicos que demandem profundidade vertical que time interno não cobre."

    is_fit_vertical = vertical != "other"
    if lang == "en":
        fit_signal = "Vertical with accumulated playbook" if is_fit_vertical else "Vertical outside the five consolidated playbooks — the engagement would be bespoke"
    else:
        fit_signal = "Vertical com playbook acumulado" if is_fit_vertical else "Vertical fora dos cinco playbooks consolidados — engagement seria bespoke"

    compliance_html = ""
    if compliance:
        if lang == "en":
            compliance_label = "Compliance environment you indicated"
        else:
            compliance_label = "Ambiente de compliance que você indicou"
        compliance_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{compliance_label}</p>
    <p class="text-ink/80 leading-relaxed">{compliance[:200]}</p>
    <p class="text-sm text-ink/70 leading-relaxed mt-3">{vinfo['compliance']}</p>
  </div>
"""
    else:
        if lang == "en":
            compliance_label = "Typical compliance for this vertical"
        else:
            compliance_label = "Compliance típica desta vertical"
        compliance_html = f"""
  <div class="my-8">
    <p class="eyebrow mb-3">{compliance_label}</p>
    <p class="text-ink/80 leading-relaxed">{vinfo['compliance']}</p>
  </div>
"""

    if lang == "en":
        greeting = f"Analysis ready, {with_name.split()[0]}." if with_name else "Analysis ready."
        # For EN, ticket display uses the USD figure
        ticket_display = ticket_usd
        readiness_price = format_price("R$ 25-40k", "USD")
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Vertical preliminary analysis · just generated</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Fit with vertical playbook</p>
    <p class="h-serif text-2xl mb-3">{vinfo['label']}</p>
    <p class="text-sm text-ink/70 leading-relaxed mb-3">{fit_signal}. Depth: {vinfo['depth']}</p>
    <p class="text-ink/80 leading-relaxed">{vinfo['playbook']}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">ROI preconditions for your vertical</p>
    <p class="text-ink/80 leading-relaxed">{vinfo['preconditions']}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About your primary concern</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{compliance_html}
  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Estimated ticket for your range ({size_label})</p>
    <p class="text-ink/80 leading-relaxed">{ticket_display}</p>
    <p class="text-xs text-subtle mt-3">Range observed across prior engagements; the real quote depends on scope defined in the Sprint.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">About your current maturity</p>
    <p class="text-ink/80 leading-relaxed">{maturity_note}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Industry Assessment is free and orientative. Typical next step is an AI Readiness Sprint ({readiness_price}) or a direct vertical pilot (4-6 weeks), depending on the signal above.</p>
</div>
""".strip()
    else:
        greeting = f"Análise pronta, {with_name.split()[0]}." if with_name else "Análise pronta."
        html = f"""
<div class="card p-8 md:p-10">
  <p class="eyebrow mb-4">Pré-análise vertical · gerada agora</p>
  <p class="h-serif text-4xl mb-6 leading-tight">{greeting}</p>

  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Fit com playbook vertical</p>
    <p class="h-serif text-2xl mb-3">{vinfo['label']}</p>
    <p class="text-sm text-ink/70 leading-relaxed mb-3">{fit_signal}. Profundidade: {vinfo['depth']}</p>
    <p class="text-ink/80 leading-relaxed">{vinfo['playbook']}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Pré-condições de ROI para sua vertical</p>
    <p class="text-ink/80 leading-relaxed">{vinfo['preconditions']}</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre sua dor primária</p>
    <p class="text-ink/80 leading-relaxed">{pain_insight}</p>
  </div>
{compliance_html}
  <div class="my-8 p-6 bg-paper border border-rule">
    <p class="eyebrow mb-3">Ticket estimado para sua faixa ({size_label})</p>
    <p class="text-ink/80 leading-relaxed">{ticket_brl}</p>
    <p class="text-xs text-subtle mt-3">Faixa observada em engagements anteriores; cotação real depende de escopo definido no Sprint.</p>
  </div>

  <div class="my-8">
    <p class="eyebrow mb-3">Sobre sua maturidade atual</p>
    <p class="text-ink/80 leading-relaxed">{maturity_note}</p>
  </div>

  <div class="rule"></div>

  <p class="text-xs text-subtle leading-relaxed">Industry Assessment é gratuito e orientativo. Próximo passo típico é AI Readiness Sprint (R$ 25-40k) ou pilot vertical direto (4-6 semanas), conforme o sinal acima.</p>
</div>
""".strip()

    meta = {
        "vertical": vertical,
        "vertical_label": vinfo["label"],
        "company_size": company_size,
        "ai_maturity": ai_maturity,
        "main_pain": main_pain,
        "compliance": compliance,
        "is_fit": is_fit_vertical,
        "ticket_estimate_brl": ticket_brl,
        "ticket_estimate_usd": ticket_usd,
    }
    return meta, html


@app.get("/industry/diagnostic", response_class=HTMLResponse)
async def lp_industry_assessment(request: Request):
    return templates.TemplateResponse("industry_assessment.html", tpl_ctx(request))


@app.post("/api/industry-assessment/analyze")
async def api_industry_analyze(form: IndustryAnalyzeForm, request: Request):
    form_data = form.model_dump()
    lang = get_locale(request)["lang"]
    analysis_meta, html = _build_industry_deliverable(form_data, lang=lang)
    business_meta = {
        "role": form.role,
        "vertical": form.vertical,
        "company_size": form.company_size,
        "ai_maturity": form.ai_maturity,
        "main_pain": form.main_pain,
        "compliance": form.compliance or "",
        "context": form.context or "",
        **analysis_meta,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        lead_id = await _create_anonymous_diag_lead(
            client,
            funnel_id=f"{get_locale(request)['market']}_INDUSTRY",
            source="lp_industry_assessment",
            diag_type="industry_assessment",
            market=get_locale(request)["market"],
            language=get_locale(request)["lang_full"],
            business_meta=business_meta,
            deliverable_html=html,
            tags=["lp_industry_assessment", "br_industry"],
        )
    return JSONResponse({"ok": True, "lead_id": lead_id, "html": html, "is_fit": analysis_meta["is_fit"]})


@app.post("/api/industry-assessment/contact")
async def api_industry_contact(form: DiagContactForm, request: Request):
    lang = get_locale(request)["lang"]
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
        _, deliverable_html = _build_industry_deliverable(meta, with_name=form.name, lang=lang)
        ok_upgrade = await _upgrade_lead_with_contact(
            client, form.lead_id, form.name, form.email, form.whatsapp, form.company
        )
        first = form.name.split()[0]
        if lang == "en":
            subject = f"Industry Assessment — preliminary analysis for {first}"
        else:
            subject = f"Industry Assessment — pré-análise pra {first}"
        email_sent = await _send_diag_report_email(
            client, form.name, form.email, "Industry", subject, deliverable_html
        )
        await _notify_slack_diag(
            client, "Industry Assessment", form.name, form.email, form.whatsapp, form.company,
            extra_lines=[
                f"Vertical: {meta.get('vertical_label', meta.get('vertical', '?'))}  ·  Size: {meta.get('company_size', '?')}",
                f"AI maturity: {meta.get('ai_maturity', '?')}  ·  Main pain: {meta.get('main_pain', '?')}",
            ],
        )
    response = JSONResponse({"ok": True, "lead_upgraded": ok_upgrade, "email_sent": email_sent})
    # Persist lead_id so the booking widget on /contact (or returned inline)
    # can merge the appointment into THIS lead row instead of asking PII again.
    if ok_upgrade and form.lead_id:
        set_lead_id_cookie(response, form.lead_id)
    return response


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

FINOPS_PAIN_INSIGHT_EN = {
    "bill_growth": "When the bill grows without visibility, 30-45% is typically overprovisioning plus underused Reserved Instances and Savings Plans. Recurring pattern.",
    "no_visibility": "Without a tagging strategy and showback/chargeback, attributing cost per product or team is impossible. The audit starts exactly there.",
    "ri_savings": "Poorly optimized RIs/Savings Plans are the most common source of 15-25% immediate savings. The audit delivers portfolio analysis within a week.",
    "architecture": "Legacy architecture that does not scale economically usually has at least 3-4 patterns correctable without deep refactor. Quick wins first, then roadmap.",
    "finops_practice": "An internal FinOps practice needs three pillars: visibility, allocation, governance. The audit establishes the baseline for each.",
    "cfo_pressure": "When the CFO is pushing, focus on quick wins in the first 30 days (10-15% savings) to show results, then a full roadmap.",
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
    <p class="h-serif text-5xl leading-none mb-1">R$ {savings_low:,} <span class="text-subtle font-normal">–</span> R$ {savings_high:,}<span class="text-2xl text-subtle font-normal align-baseline">/ano</span></p>
    <p class="text-xs text-subtle mt-3 leading-relaxed">Baseado em fatura {spend_label} cruzado com padrões observados em audits anteriores. A faixa reflete cenários reais — o piso assume execução parcial das recomendações; o topo, execução completa nas primeiras 12 semanas.</p>
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
            _inner_html = _inline_deliverable_for_email(
                deliverable_html.replace('class="card p-8 md:p-10"', '')
            )
            _footer_html = _anuvia_email_footer("pt")
            email_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body style="background:#fafaf9;font-family:-apple-system,Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;">
<div style="max-width:640px;margin:0 auto;">
  <p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;margin:0 0 8px 0;">Anuvia · FinOps</p>
  <p style="font-family:Georgia,'Times New Roman',serif;font-size:32px;margin:0 0 24px 0;line-height:1.15;">Olá, {form.name.split()[0]}</p>
  <p style="color:#475569;line-height:1.65;">Obrigada pelo interesse no FinOps Audit. Aqui está a pré-análise gerada a partir das suas respostas:</p>
  <div style="background:#ffffff;border:1px solid #e7e5e4;padding:24px;margin:24px 0;">
    {_inner_html}
  </div>
  <p style="color:#475569;line-height:1.65;">Em até 24h te respondo com próximos passos concretos. Se quiser adiantar, agenda direto:</p>
  <p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="display:inline-block;background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;font-weight:500;">Agendar Discovery Call</a></p>
  {_footer_html}
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

WA_FOCUS_INSIGHT_EN = {
    "security": "Security is where I see the most gaps in AWS audits — excessive IAM permissions, public network exposure, encryption gaps in S3/RDS, missing CloudTrail/Config baseline. The audit prioritizes these categories.",
    "reliability": "Reliability starts with realistic BCP/DR — documented RPO/RTO, tested backups, clear multi-AZ vs multi-region trade-offs. The audit validates readiness against failure.",
    "performance": "Performance issues are almost always misconfigured scaling policy, undersized RDS, or network bottlenecks. The audit identifies them via real metrics, not assumption.",
    "cost": "Cost focus overlaps with the dedicated FinOps Audit — consider whether starting with FinOps Risk-Free, which is savings-specific, fits better.",
    "operational": "Operational Excellence is IaC + automation + runbooks + post-mortems. The audit identifies what is manual vs automated and prioritizes investment.",
    "all": "Full coverage across all 6 pillars gives an executive view for board/founders. Useful pre-investment round or pre-acquisition due diligence.",
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
                _footer_html = _anuvia_email_footer("pt")
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · AWS</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {form.name.split()[0]}</p><p style="color:#475569;line-height:1.65;">Obrigada pelo interesse no AWS Well-Architected Review. Em até 24h te respondemos com escopo detalhado.</p><p style="color:#475569;line-height:1.65;">{focus_insight}</p><p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p>{_footer_html}</div></body></html>"""
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

DORA_LEVEL_EN = {
    "multiple_day": ("Elite", "Your deploy frequency is already Elite (top 11% globally). The audit focuses on refining reliability, observability, and reinforcing advanced practices (chaos engineering, progressive delivery)."),
    "daily": ("High", "Your deploy frequency is High (top 25%). The audit identifies paths to reach Elite — typically reducing change failure rate or accelerating lead time."),
    "weekly": ("Medium", "Weekly deploy is Medium. The audit identifies the primary bottlenecks: test automation, deployment automation, or approval bottlenecks."),
    "biweekly": ("Medium", "Biweekly sits between Medium and Low. Usually a mix of manual processes + missing IaC + test gaps. The audit prioritizes the highest-ROI interventions."),
    "monthly": ("Low", "Monthly deploy is Low performer. The biggest lever is typically eliminating manual gates + introducing automation incrementally."),
    "quarterly": ("Low", "Quarterly deploy indicates a critical release process. The audit focuses on deconstructing the gates that turn release into an event."),
}

DEVOPS_PAIN_INSIGHT = {
    "release_pain": "Releases como evento são quase sempre sintoma de: (a) testes manuais demais, (b) feature flags ausentes/mal usados, (c) rollback procedure não testado, (d) coordenação cross-team excessiva. Audit identifica qual destes domina.",
    "incidents": "MTTR alto vem de combinação: falta de observability detalhada + runbooks fracos + on-call exhaustion. Atacar primeiro o pilar mais fraco dá maior return.",
    "observability": "Debugging cego é caro. Stack moderno (DataDog, NewRelic, Grafana Cloud) com tracing + structured logging muda o jogo em semanas, não meses.",
    "oncall": "On-call burnout precede churn. Audit identifica top 10 noisy alerts (geram 80% das interrupções) e cria plano pra silenciar/refinar.",
    "quality": "Change failure rate alto tipicamente: test coverage baixo, integration tests ausentes, ou deploy pipeline pula gates. Audit mede e prioriza.",
    "speed": "Lead time longo é normalmente: PR review demorado, CI lento, ou approvals manuais. Cada um tem fix específico.",
}

DEVOPS_PAIN_INSIGHT_EN = {
    "release_pain": "Releases as events are almost always a symptom of: (a) too many manual tests, (b) missing or misused feature flags, (c) untested rollback procedure, (d) excessive cross-team coordination. The audit identifies which of these dominates.",
    "incidents": "High MTTR comes from a combination: lack of detailed observability + weak runbooks + on-call exhaustion. Attacking the weakest pillar first yields the highest return.",
    "observability": "Blind debugging is expensive. A modern stack (DataDog, NewRelic, Grafana Cloud) with tracing + structured logging changes the game in weeks, not months.",
    "oncall": "On-call burnout precedes churn. The audit identifies the top 10 noisy alerts (responsible for 80% of interruptions) and builds a plan to silence/refine them.",
    "quality": "High change failure rate usually means: low test coverage, missing integration tests, or a deploy pipeline that skips gates. The audit measures and prioritizes.",
    "speed": "Long lead time is usually: slow PR review, slow CI, or manual approvals. Each has a specific fix.",
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
                _footer_html = _anuvia_email_footer("pt")
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · DevOps</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {form.name.split()[0]}</p><p style="color:#475569;line-height:1.65;">Pré-análise DORA preliminar: nível <strong>{level}</strong>.</p><p style="color:#475569;line-height:1.65;">{level_insight}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://cal.anuvia.com.br" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p>{_footer_html}</div></body></html>"""
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

AI_STAGE_INSIGHT_EN = {
    "exploring": "In the exploratory phase, the Readiness Sprint is usually the first defensible investment — before committing R$ 100-300k to a platform. Typical outputs: 2-3 priority cases with a calibrated business case.",
    "experimenting": "When PoCs run in parallel without order, the Sprint typically finds that half of the experiments do not justify production investment. Focus shifts to the half that does.",
    "early_prod": "With 1-2 cases running, the next step is usually MLOps Practice Build (governance, observability, cost) before adding more cases. The Sprint formalizes that path.",
    "scaling": "At scale, the bigger ROI usually comes from AI FinOps (optimize cost per inference) and governance, not new cases. The Sprint redirects the roadmap.",
    "mature": "A mature operation typically uses the Sprint to validate next frontiers: autonomous agents, multi-model, vertical specialization. Direct technical discussion.",
}

AI_PAIN_INSIGHT_EN = {
    "poc_to_prod": "A PoC that does not graduate to production is almost always a design failure — no clear evolution gates and no technical approval criteria. The Sprint formalizes these gates from discovery.",
    "cost_runaway": "Cost out of control usually comes from a combination: missing prompt caching, wrong model for the task, no per-tenant cap. The Sprint identifies and quantifies each vector.",
    "no_eval": "Without an eval harness, every prompt improvement is blind. Establishing an evaluation baseline is always the first deliverable — everything else is theater without it.",
    "usecase_blur": "Unclear cases are where the Sprint adds the most. Typical output: 8-15 candidates mapped, 3-5 prioritized, explicit business case for each.",
    "vendor_lock": "A multi-model strategy is viable with the right abstraction. The Sprint evaluates trade-offs across Bedrock vs OpenAI vs Vertex vs self-hosted on cost, latency, and governance.",
    "governance": "Governance blocking progress usually comes from poorly mapped risk. The Sprint produces the risk register that legal/compliance needs to unblock.",
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
                _footer_html = _anuvia_email_footer("pt")
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · AI</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {first}</p><p style="color:#475569;line-height:1.65;">{stage_insight}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://anuvia.com.br/contact?offering=ai-readiness&practice=ai" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p>{_footer_html}</div></body></html>"""
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

SALESOPS_PAIN_INSIGHT_EN = {
    "lead_leakage": "Leads cooling before the response is a direct symptom of broken SLAs. The diagnostic measures real response time per channel and identifies the channel that deserves immediate automation.",
    "manual_ops": "Excessively manual process usually concentrates in 3-5 repetitive activities (qualification, scheduling, follow-up). The diagnostic quantifies hours/week spent on each.",
    "no_visibility": "No pipeline visibility is usually a symptom of misconfigured CRM or poorly defined stages. The diagnostic produces a funnel map and consistent states in 2 weeks.",
    "low_conversion": "Unexpected conversion drops between stages usually have an identifiable cause at a specific transition. The diagnostic isolates where it is and proposes intervention.",
    "team_burnout": "An overloaded team is a symptom, not a cause. The diagnostic identifies whether it is excess low-quality lead volume, manual process, or allocation issue.",
    "founder_blocked": "Founder as bottleneck on every deal usually comes from missing documented playbook + lack of automated qualification. The diagnostic produces both.",
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
                _footer_html = _anuvia_email_footer("pt")
                email_html = f"""<!DOCTYPE html><html><body style="background:#fafaf9;font-family:Inter,sans-serif;color:#1a1a1a;margin:0;padding:32px 24px;"><div style="max-width:640px;margin:0 auto;"><p style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#78716c;">Anuvia · Growth</p><p style="font-family:Georgia,serif;font-size:32px;margin:0 0 16px 0;">Olá, {first}</p><p style="color:#475569;line-height:1.65;">{pain_insight}</p><p style="margin:24px 0;"><a href="https://anuvia.com.br/contact?offering=sales-ops-audit&practice=growth" style="background:#1a1a1a;color:#fafaf9;padding:12px 22px;text-decoration:none;">Agendar Discovery Call</a></p>{_footer_html}</div></body></html>"""
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
