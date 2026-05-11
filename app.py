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
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import markdown as md_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anuvia-lp")

app = FastAPI(title="Anuvia Landing Pages", version="0.1.0")
templates = Jinja2Templates(directory="templates")

SUPA_URL = os.environ.get("SUPABASE_URL", "https://api.anuvia.com.br/rest/v1").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
SLACK_WEBHOOK = os.environ.get("SLACK_NEW_LEAD_WEBHOOK", "")  # optional, fallback to n8n

# Easyappointments (cal.anuvia.com.br) — uses public booking endpoints
# (no API token needed; same flow that the public booking page uses).
EASY_BASE = os.environ.get(
    "EASYAPPOINTMENTS_BASE_URL", "https://cal.anuvia.com.br"
).rstrip("/")
# BR_SMB defaults: provider 2 (Mila), service 2 (Discovery Growth Mesh BR, 30min)
EASY_PROVIDER_ID = int(os.environ.get("EASYAPPOINTMENTS_PROVIDER_ID", "2"))
EASY_SERVICE_ID_BR_SMB = int(os.environ.get("EASYAPPOINTMENTS_SERVICE_ID_BR_SMB", "2"))
EASY_SERVICE_DURATION_MIN = int(os.environ.get("EASYAPPOINTMENTS_SERVICE_DURATION_MIN", "30"))
TZ_SP = ZoneInfo("America/Sao_Paulo")

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
    business_type: str = Field(..., min_length=1, max_length=80)
    team_size: str = Field(..., min_length=1, max_length=40)
    leads_per_month: str = Field(..., min_length=1, max_length=40)
    main_channel: str = Field(..., min_length=1, max_length=80)
    response_time: str = Field(..., min_length=1, max_length=40)
    main_pain: str = Field(..., min_length=1, max_length=200)
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

DIAGNOSTIC_SYSTEM_PROMPT = """Você é Mila Vernazza, founder da Anuvia (consultoria de IA aplicada a vendas e ops).

Você está gerando um diagnóstico personalizado de 5 minutos para um SMB brasileiro
que preencheu um form sobre seu funil comercial. O diagnóstico precisa ser:

- Específico ao tipo de negócio e tamanho informados (não genérico)
- Honesto sobre quanto o negócio provavelmente está perdendo em receita
- Acionável: 3 ações concretas pra próximos 30 dias, priorizadas
- Mostrando como Anuvia pode ajudar SEM ser pitch comercial agressivo

Retorne APENAS um JSON válido com este shape exato:

{
  "diagnostico_resumo": "1-2 paragrafos analisando os pontos fracos do funil deles",
  "estimativa_perdida": "string com estimativa em R$/mes do que estao perdendo, com explicação curta",
  "score_maturidade": <int 0-100>,
  "pontos_fortes": ["forte 1", "forte 2"],
  "pontos_fracos": ["fraco 1", "fraco 2", "fraco 3"],
  "plano_30_dias": [
    {"semana": "1", "acao": "...", "porque": "..."},
    {"semana": "2", "acao": "...", "porque": "..."},
    {"semana": "3-4", "acao": "...", "porque": "..."}
  ],
  "proximo_passo": "1 frase com CTA específico pro próximo passo - pode ser agendar discovery, fazer X ação concreta, etc"
}

Tom: PT-BR, conversacional, direto, sem jargão de consultoria. Trate o leitor como peer.
Use números reais quando possível (não invente, mas estime baseado em benchmarks)."""


def build_diagnostic_user_message(form: DiagnosticForm) -> str:
    lines = [
        f"Tipo de negócio: {form.business_type}",
        f"Tamanho da equipe: {form.team_size}",
        f"Leads novos por mês: {form.leads_per_month}",
        f"Canal principal de captação: {form.main_channel}",
        f"Tempo médio até primeiro contato: {form.response_time}",
        f"Maior dor comercial hoje: {form.main_pain}",
        "",
        f"Nome: {form.name}",
        f"Empresa: {form.company or '(não informado)'}",
    ]
    return "\n".join(lines)


async def call_claude_diagnostic(form: DiagnosticForm) -> dict:
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2000,
        "system": DIAGNOSTIC_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": build_diagnostic_user_message(form)},
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
    # Strip code fences if Claude added them
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ----------------------------------------------------------------------------
# Supabase: insert lead
# ----------------------------------------------------------------------------


async def insert_lead(
    client: httpx.AsyncClient, form: DiagnosticForm, diagnostic: dict
) -> Optional[str]:
    """Insert lead with funnel_id BR_SMB. Returns lead.id or None on failure."""
    payload = {
        "tenant_id": "anuvia",
        "funnel_id": "BR_SMB",
        "market": "BR",
        "track": "growth_mesh",
        "language": "pt-BR",
        "source": "lp_diagnostic",
        "source_detail": {
            "lp": "diagnostico.anuvia.com.br",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        "name": form.name,
        "email": form.email,
        "phone_e164": form.whatsapp,
        "company": form.company,
        "current_stage": "new",
        "qualification_data": {
            "business_type": form.business_type,
            "team_size": form.team_size,
            "leads_per_month": form.leads_per_month,
            "main_channel": form.main_channel,
            "response_time": form.response_time,
            "main_pain": form.main_pain,
            "diagnostic_score": diagnostic.get("score_maturidade"),
            "diagnostic_estimate": diagnostic.get("estimativa_perdida"),
            "diagnostic_summary": diagnostic.get("diagnostico_resumo"),
            "diagnostic_plan": diagnostic.get("plano_30_dias"),
        },
        "consent": {
            "lp_diagnostic": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        },
        "tags": ["lp_diagnostic", "br_smb"],
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


@app.get("/api/slots")
async def api_slots(funnel: str = "BR_SMB", days: int = 5) -> JSONResponse:
    """Return available slots for the next `days` working days using
    Easyappointments' public booking endpoint (no auth)."""
    service_id = EASY_SERVICE_ID_BR_SMB  # extend later when other funnels go live
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

        all_slots = await asyncio.gather(*(fetch(d) for d in targets))
        for d, raw_slots in zip(targets, all_slots):
            slots = _coarse_slots(raw_slots, step_min=30, max_per_day=8)
            label = f"{pt_weekdays[d.weekday()]}, {d.day} {pt_months[d.month - 1]}"
            out.append({"date": d.strftime("%Y-%m-%d"), "label": label, "slots": slots})

    return JSONResponse({"days": out})


class BookingRequest(BaseModel):
    lead_id: str = Field(..., min_length=10, max_length=64)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


@app.post("/api/book")
async def api_book(payload: BookingRequest) -> JSONResponse:
    """Book a discovery using the lead's existing data (no re-asking).
    Uses Easyappointments public booking endpoint (no auth required)."""
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
        end_dt = start_dt + timedelta(minutes=EASY_SERVICE_DURATION_MIN)
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Easyappointments requires firstName + lastName non-empty.
        name_parts = (lead.get("name") or "Lead").strip().split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else "."

        diag = (lead.get("qualification_data") or {})
        notes = (
            f"Diagnóstico LP — score {diag.get('diagnostic_score', '?')}/100. "
            f"Tipo: {diag.get('business_type', '?')}. "
            f"Dor: {diag.get('main_pain', '?')}. "
            f"Estimativa perdida: {diag.get('diagnostic_estimate', '?')}"
        )

        # 3. Submit to Easyappointments public booking endpoint.
        # Uses nested form fields: post_data[appointment][...], post_data[customer][...]
        form = {
            "post_data[appointment][start_datetime]": start_str,
            "post_data[appointment][end_datetime]": end_str,
            "post_data[appointment][id_users_provider]": str(EASY_PROVIDER_ID),
            "post_data[appointment][id_services]": str(EASY_SERVICE_ID_BR_SMB),
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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "br_smb.html",
        {"request": request, "funnel": "BR_SMB"},
    )


@app.post("/api/diagnose")
async def diagnose(payload: DiagnosticForm) -> JSONResponse:
    """Receive form, call Claude, insert lead, return rendered deliverable."""
    try:
        diagnostic = await call_claude_diagnostic(payload)
    except httpx.HTTPStatusError as e:
        log.error("claude call failed: %s %s", e.response.status_code, e.response.text[:200])
        raise HTTPException(status_code=502, detail="Falha ao gerar diagnóstico (LLM)")
    except (json.JSONDecodeError, KeyError, IndexError):
        log.exception("claude returned malformed response")
        raise HTTPException(status_code=502, detail="Diagnóstico retornou em formato inválido")

    async with httpx.AsyncClient(timeout=30) as client:
        lead_id = await insert_lead(client, payload, diagnostic)
        await fire_slack_notification(client, payload, diagnostic, lead_id)

    # Render the deliverable as HTML for the SPA to inject
    deliverable_html = render_deliverable(payload, diagnostic, lead_id)

    return JSONResponse({
        "ok": True,
        "lead_id": lead_id,
        "diagnostic": diagnostic,
        "deliverable_html": deliverable_html,
    })


def render_deliverable(form: DiagnosticForm, diag: dict, lead_id: Optional[str]) -> str:
    """Build the HTML block shown to the user after submit."""
    plano = diag.get("plano_30_dias", [])
    plano_html = "".join(
        f'<li><strong>Semana {p.get("semana", "?")}:</strong> {p.get("acao", "")} '
        f'<span class="text-slate-400 text-sm block mt-1">{p.get("porque", "")}</span></li>'
        for p in plano
    )
    fortes = diag.get("pontos_fortes", [])
    fracos = diag.get("pontos_fracos", [])
    fortes_html = "".join(f"<li>{f}</li>" for f in fortes)
    fracos_html = "".join(f"<li>{f}</li>" for f in fracos)

    # Markdown the resumo in case Claude returns it formatted
    resumo_html = md_lib.markdown(diag.get("diagnostico_resumo", ""))

    score = diag.get("score_maturidade", 0)
    estimativa = diag.get("estimativa_perdida", "")
    proximo = diag.get("proximo_passo", "")

    # Embedded booking widget — replaces the old external Easyappointments redirect.
    # We pass lead_id via data attribute and let the JS in br_smb.html handle slots.
    booking_html = (
        f'<div id="booking-widget" data-lead-id="{lead_id or ""}"></div>'
        if lead_id
        else (
            '<p class="text-slate-400 text-sm text-center">'
            "Não conseguimos preparar o agendamento agora. Te chamo no WhatsApp pra alinhar.</p>"
        )
    )

    return f"""
<div class="space-y-6">
  <div class="text-center">
    <p class="text-slate-300 text-sm uppercase tracking-wider mb-2">Seu diagnóstico, {form.name.split()[0]}</p>
    <div class="inline-flex items-baseline gap-2">
      <span class="text-6xl font-bold text-indigo-400">{score}</span>
      <span class="text-slate-400">/ 100 maturidade comercial</span>
    </div>
  </div>

  <div class="card-glass p-5">
    <h3 class="text-lg font-semibold mb-3">Análise</h3>
    <div class="prose prose-invert prose-sm max-w-none text-slate-200">{resumo_html}</div>
  </div>

  <div class="card-glass p-5 border-l-4 border-amber-500">
    <h3 class="text-lg font-semibold mb-2 text-amber-300">💸 Estimativa de oportunidade perdida</h3>
    <p class="text-slate-200">{estimativa}</p>
  </div>

  <div class="grid md:grid-cols-2 gap-4">
    <div class="card-glass p-5">
      <h3 class="text-base font-semibold mb-3 text-emerald-300">✅ Pontos fortes</h3>
      <ul class="space-y-2 text-slate-200 text-sm list-disc list-inside">{fortes_html}</ul>
    </div>
    <div class="card-glass p-5">
      <h3 class="text-base font-semibold mb-3 text-rose-300">⚠️ Pontos fracos</h3>
      <ul class="space-y-2 text-slate-200 text-sm list-disc list-inside">{fracos_html}</ul>
    </div>
  </div>

  <div class="card-glass p-5">
    <h3 class="text-lg font-semibold mb-3">📋 Plano de 30 dias</h3>
    <ol class="space-y-4 text-slate-200">{plano_html}</ol>
  </div>

  <div class="card-glass p-6 bg-indigo-950/40 border border-indigo-700/50">
    <h3 class="text-base font-semibold mb-2 text-indigo-200 text-center">🚀 Próximo passo</h3>
    <p class="text-slate-200 text-center mb-5">{proximo}</p>
    {booking_html}
  </div>
</div>
"""
