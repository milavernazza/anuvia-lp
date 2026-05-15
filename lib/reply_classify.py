"""Reply classification — inbound email webhook + Claude intent classifier.

Owned by Agent A4 in the v2 sprint. Per
ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Reply contract" and the bullet under
"reply_classify.py".

Responsibilities:
  * Receive an inbound email webhook (Resend, Cloudflare Email Routing, or
    Mailgun shape — normalised here).
  * Match the sender to a prospect (table `prospects`) or lead (table
    `leads`) — same URL-encoded-email bug we already burned on once.
  * Call Claude to classify the reply: ``interested | question | objection
    | unsubscribe | out_of_office | no``.
  * Persist the classification in ``leads.signals`` (or prospect equivalent)
    plus an ``agent_history`` entry.
  * Take action: auto-reply (FAQ-grounded), book discovery, escalate to
    Slack, stop sequence, or ignore.

Module boundaries (do not import from sibling sprint modules):
  * Outbound cold engine                  → `lib/outbound.py`     (A1)
  * Per-practice Track B handlers         → `lib/track_b.py`      (A2)
  * Contract + payments                   → `lib/contract.py`     (A3)
  * Apollo / Clay enrichment + ICP scoring → `lib/prospecting.py` (A5)

Quality bar (mirrors `lib/track_b.py`):
  * All public functions are async; httpx-based.
  * Idempotent: same Resend `message_id` received twice (Resend retries)
    is detected via the `inbound_msg_id` artifact-style marker and skipped.
  * Confidence < 0.6 → always escalate to Slack, regardless of intent
    (safety net so we never auto-reply on shaky ground).
  * Resend signature verification is optional (env var
    `INBOUND_WEBHOOK_SECRET`); when set the request must carry the matching
    HMAC-SHA256 hex in `X-Webhook-Signature` (or `?key=` query param).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _urlquote

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from lib.sessions import (
    SUPA_HEADERS,
    SUPA_URL,
    session_append_history,
    session_append_signal,
    session_get,
    session_set_next,
    session_set_status,
)

log = logging.getLogger("anuvia-lp.reply_classify")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "mila@anuvia.com.br")
RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
RESEND_REPLY_TO = os.environ.get("RESEND_REPLY_TO", RESEND_FROM_EMAIL)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_URL = os.environ.get(
    "ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages"
)

INBOUND_WEBHOOK_SECRET = os.environ.get("INBOUND_WEBHOOK_SECRET", "")

SLACK_WEBHOOK = (
    os.environ.get("SLACK_ALERTS_WEBHOOK")
    or os.environ.get("SLACK_NEW_LEAD_WEBHOOK")
    or ""
)

# Hosts used in user-facing links (calendar / context). Reusing track_b's
# defaults so the brand is consistent.
PUBLIC_HOST_PT = os.environ.get("PROPOSAL_HOST_PT", "https://anuvia.com.br")
PUBLIC_HOST_EN = os.environ.get("PROPOSAL_HOST_EN", "https://anuvia.net")
CALENDAR_URL = os.environ.get("CALENDAR_URL", "https://anuvia.com.br/agenda")

# When confidence is below this floor we ALWAYS escalate to Slack regardless
# of the classified intent. Tightens our auto-action surface.
CONFIDENCE_FLOOR = 0.60

# How long to pause the sequence after an auto-reply / discovery offer.
AUTO_REPLY_PAUSE_DAYS = 7

_HTTP_TIMEOUT = 30.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _qenc(email: str) -> str:
    """URL-encode an email for PostgREST equality filters.

    Without this `+` in addresses (gmail aliasing) becomes a space and the
    lookup silently misses. Same bug we already fixed in `track_b.py` —
    NEVER drop this helper.
    """
    return _urlquote(email or "", safe="@.")


# ---------------------------------------------------------------------------
# Webhook signature verification (optional)
# ---------------------------------------------------------------------------


def _verify_inbound_signature(body: bytes, headers: Dict[str, str], query_key: Optional[str]) -> bool:
    """Verify the inbound webhook when `INBOUND_WEBHOOK_SECRET` is set.

    Three ways to authenticate, any one is enough:
      1. ``X-Webhook-Signature: sha256=<hex>`` — HMAC-SHA256 of the raw body.
      2. ``X-Webhook-Signature: <hex>`` — same, without the prefix.
      3. ``?key=<secret>`` query string — for forwarders that can't sign.

    When the env var is empty we return True (operator opted out).
    """
    if not INBOUND_WEBHOOK_SECRET:
        return True

    # Query-string shared secret -------------------------------------------------
    if query_key and hmac.compare_digest(str(query_key), INBOUND_WEBHOOK_SECRET):
        return True

    sig_header = (
        headers.get("x-webhook-signature")
        or headers.get("X-Webhook-Signature")
        or ""
    )
    if not sig_header:
        return False

    candidate = sig_header.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256="):].strip()

    try:
        expected = hmac.new(
            INBOUND_WEBHOOK_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, candidate)
    except Exception as exc:  # noqa: BLE001 — invalid sig is just False
        log.warning("reply_classify: signature verify error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Payload normalisation (Resend / Cloudflare Routing / Mailgun)
# ---------------------------------------------------------------------------


def _first_email_from_field(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(email, display_name)`` from a heterogeneous From field.

    Handles all of these shapes:
      * ``{"email": "...", "name": "..."}``  (Resend inbound)
      * ``"Jane Doe <jane@x.com>"``           (RFC-5322 string, Cloudflare/Mailgun)
      * ``"jane@x.com"``                      (bare address)
      * ``[{"email": "..."}, ...]``           (list, take first)
    """
    if value is None:
        return None, None
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        return (value.get("email") or "").strip().lower() or None, (
            value.get("name") or None
        )
    if isinstance(value, str):
        s = value.strip()
        m = re.search(r"<([^>]+)>", s)
        if m:
            email = m.group(1).strip().lower()
            name = s[: m.start()].strip().strip('"').strip() or None
            return email, name
        # Fallback: hunt for any email-looking token.
        m2 = re.search(r"[\w\.\+\-]+@[\w\.\-]+", s)
        if m2:
            return m2.group(0).strip().lower(), None
    return None, None


def _normalise_payload(raw: dict) -> dict:
    """Coalesce Resend / Cloudflare Routing / Mailgun inbound shapes.

    Returned dict::

        {
            "from_email": str | None,
            "from_name":  str | None,
            "to":         list[str],
            "subject":    str,
            "text":       str,        # plain-text body, best-effort
            "html":       str,        # html body, best-effort
            "message_id": str | None, # used for dedup
            "in_reply_to": str | None,
            "references": list[str],
        }
    """
    if not isinstance(raw, dict):
        return {
            "from_email": None,
            "from_name": None,
            "to": [],
            "subject": "",
            "text": "",
            "html": "",
            "message_id": None,
            "in_reply_to": None,
            "references": [],
        }

    # Some providers wrap the payload in {"data": {...}} (Resend, Mailgun-events).
    body = raw
    if "data" in raw and isinstance(raw["data"], dict) and (
        "from" in raw["data"] or "sender" in raw["data"]
    ):
        body = raw["data"]

    # ---- From -----------------------------------------------------------------
    from_email, from_name = _first_email_from_field(
        body.get("from")
        or body.get("From")
        or body.get("sender")
        or body.get("envelope-from")
    )

    # ---- To -------------------------------------------------------------------
    to_list: List[str] = []
    raw_to = body.get("to") or body.get("To") or body.get("recipient") or []
    if isinstance(raw_to, str):
        for piece in re.split(r"[,;]", raw_to):
            e, _ = _first_email_from_field(piece)
            if e:
                to_list.append(e)
    elif isinstance(raw_to, list):
        for item in raw_to:
            e, _ = _first_email_from_field(item)
            if e:
                to_list.append(e)
    elif isinstance(raw_to, dict):
        e, _ = _first_email_from_field(raw_to)
        if e:
            to_list.append(e)

    # ---- Subject / bodies ------------------------------------------------------
    subject = str(
        body.get("subject") or body.get("Subject") or ""
    ).strip()

    text = (
        body.get("text")
        or body.get("body-plain")
        or body.get("plain")
        or body.get("stripped-text")
        or ""
    )
    if not isinstance(text, str):
        text = str(text)

    html = (
        body.get("html")
        or body.get("body-html")
        or ""
    )
    if not isinstance(html, str):
        html = str(html)

    # ---- Threading -------------------------------------------------------------
    message_id = (
        body.get("message_id")
        or body.get("messageId")
        or body.get("Message-Id")
        or body.get("Message-ID")
        or None
    )
    if message_id is not None:
        message_id = str(message_id).strip().strip("<>").lower() or None

    in_reply_to = (
        body.get("in_reply_to")
        or body.get("inReplyTo")
        or body.get("In-Reply-To")
        or None
    )
    if in_reply_to is not None:
        in_reply_to = str(in_reply_to).strip().strip("<>").lower() or None

    refs_raw = body.get("references") or body.get("References") or []
    references: List[str] = []
    if isinstance(refs_raw, str):
        for tok in refs_raw.split():
            references.append(tok.strip("<>").lower())
    elif isinstance(refs_raw, list):
        for r in refs_raw:
            references.append(str(r).strip("<>").lower())

    return {
        "from_email": from_email,
        "from_name": from_name,
        "to": to_list,
        "subject": subject,
        "text": text,
        "html": html,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
    }


def _strip_quoted_reply(text: str) -> str:
    """Drop quoted history from a plain-text reply so Claude only sees the new part.

    Heuristic — looks for the most common reply delimiters:
      * ``"On <date>, <person> wrote:"`` line
      * Gmail's ``"Em <date>, <pessoa> escreveu:"`` line
      * Leading ``"> "`` quoted blocks
      * ``"-- "`` signature delimiter (cut on first occurrence)
    """
    if not text:
        return ""
    lines = text.splitlines()
    cut_at = len(lines)

    delim_patterns = [
        re.compile(r"^\s*On\s.+\s+wrote:\s*$", re.IGNORECASE),
        re.compile(r"^\s*Em\s.+\s+escreveu:\s*$", re.IGNORECASE),
        re.compile(r"^\s*Em\s.+\s+às\s.+\s+escreveu:\s*$", re.IGNORECASE),
        re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
        re.compile(r"^\s*From:\s.+@.+", re.IGNORECASE),
        re.compile(r"^\s*De:\s.+@.+", re.IGNORECASE),
    ]
    for i, ln in enumerate(lines):
        if any(p.match(ln) for p in delim_patterns):
            cut_at = i
            break
        if ln.strip() == "-- ":  # signature delimiter
            cut_at = i
            break

    cleaned = [
        ln for ln in lines[:cut_at]
        if not ln.lstrip().startswith(">")
    ]
    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# Prospect / lead lookup
# ---------------------------------------------------------------------------


async def _find_prospect_by_email(email: str) -> Optional[dict]:
    """Return the prospect row matching `email` (case-insensitive) or None."""
    if not email:
        return None
    enc = _qenc(email.lower())
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{SUPA_URL}/prospects?email=eq.{enc}&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code == 200:
            rows = r.json() or []
            return rows[0] if rows else None
        log.warning(
            "reply_classify: prospect lookup non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
    except Exception:  # noqa: BLE001 — table may not exist yet in dev
        log.exception("reply_classify: prospect lookup failed for %s", email)
    return None


async def _find_lead_by_email(email: str) -> Optional[dict]:
    """Return the most recent lead row matching `email` or None."""
    if not email:
        return None
    enc = _qenc(email.lower())
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                f"{SUPA_URL}/leads?email=eq.{enc}&order=created_at.desc&limit=1",
                headers=SUPA_HEADERS,
            )
        if r.status_code == 200:
            rows = r.json() or []
            return rows[0] if rows else None
        log.warning(
            "reply_classify: lead lookup non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
    except Exception:  # noqa: BLE001
        log.exception("reply_classify: lead lookup failed for %s", email)
    return None


async def _prospect_update(prospect_id: str, **fields: Any) -> None:
    """PATCH a prospect row. Best-effort; never raises."""
    if not prospect_id:
        return
    fields = dict(fields)
    fields.setdefault("updated_at", _now_iso())
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.patch(
                f"{SUPA_URL}/prospects?id=eq.{prospect_id}",
                headers=SUPA_HEADERS,
                json=fields,
            )
        if r.status_code not in (200, 204):
            log.warning(
                "reply_classify: prospect patch non-200 status=%s body=%s",
                r.status_code, r.text[:200],
            )
    except Exception:  # noqa: BLE001
        log.exception("reply_classify: prospect patch failed id=%s", prospect_id)


async def _prospect_append_signal(prospect: dict, entry: dict) -> None:
    """Append `entry` to `prospects.enriched_data.inbound_signals` (jsonb).

    The prospects table doesn't have a dedicated `signals` column, so we
    co-locate inbound markers inside `enriched_data` to keep the audit trail
    in one place. Read-modify-write; one retry on failure.
    """
    prospect_id = prospect.get("id")
    if not prospect_id:
        return
    enriched = prospect.get("enriched_data") or {}
    if isinstance(enriched, str):
        try:
            enriched = json.loads(enriched)
        except Exception:  # noqa: BLE001
            enriched = {}
    if not isinstance(enriched, dict):
        enriched = {}
    signals = list(enriched.get("inbound_signals") or [])
    signals.append(entry)
    enriched["inbound_signals"] = signals
    await _prospect_update(prospect_id, enriched_data=enriched)


# ---------------------------------------------------------------------------
# Idempotency — have we processed this Message-ID before?
# ---------------------------------------------------------------------------


def _has_inbound_message_id(target: dict, message_id: str) -> bool:
    """Return True iff `target` (lead OR prospect) already saw `message_id`."""
    if not message_id:
        return False
    msg_id = message_id.lower()

    # Leads use the top-level `signals` jsonb column.
    for s in target.get("signals") or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") != "inbound_reply":
            continue
        payload = s.get("payload") or {}
        if isinstance(payload, dict) and str(payload.get("message_id", "")).lower() == msg_id:
            return True

    # Prospects nest signals under enriched_data.inbound_signals.
    enriched = target.get("enriched_data") or {}
    if isinstance(enriched, str):
        try:
            enriched = json.loads(enriched)
        except Exception:  # noqa: BLE001
            enriched = {}
    if isinstance(enriched, dict):
        for s in enriched.get("inbound_signals") or []:
            if not isinstance(s, dict):
                continue
            payload = s.get("payload") or {}
            if isinstance(payload, dict) and str(payload.get("message_id", "")).lower() == msg_id:
                return True
    return False


# ---------------------------------------------------------------------------
# Anuvia FAQ — minimal knowledge base for auto-replies
# ---------------------------------------------------------------------------

#: Hardcoded FAQ used by `_send_auto_reply`. Future iteration: swap for RAG
#: over Mila's posts + delivery SOPs. Keys are normalised slugs; values are
#: short paragraphs we feed Claude as context — Claude paraphrases in
#: Mila's voice (never copy-paste). Bilingual: PT + EN markers in the body.
ANUVIA_FAQ: Dict[str, str] = {
    "what_is_anuvia": (
        "Anuvia é uma consultoria sênior de Cloud, IA, Engineering, Growth e "
        "Industry. Fundada pela Mila Vernazza (ex-AWS, ex-Google), atua como "
        "Solutions Architect terceirizado para times que querem execução "
        "técnica sem montar squad interno completo. / Anuvia is a senior "
        "consultancy across Cloud, AI, Engineering, Growth and Industry — "
        "founded by Mila Vernazza (ex-AWS, ex-Google). We act as an "
        "outsourced senior Solutions Architect for teams that want serious "
        "execution without hiring a full platform squad."
    ),
    "pricing_finops": (
        "Auditoria FinOps começa em R$ 45-60k, 4 semanas, com garantia de "
        "3× ROI nos primeiros 90 dias (devolução parcial se não atingir). "
        "Inclui diagnóstico CUR + Cost Explorer, identificação de waste / "
        "rightsizing / RIs, roadmap priorizado por payback e handoff técnico. "
        "/ FinOps audit: R$ 45-60k, 4 weeks, 3× ROI guarantee in 90 days."
    ),
    "pricing_ai": (
        "AI Readiness + PoV: R$ 25-40k, 3 semanas. Inventário de casos "
        "priorizados por ROI, avaliação de prontidão técnica (dados, infra, "
        "segurança), proof-of-value em 1 caso fechado, roadmap de adoção + "
        "governança em 90 dias. / AI Readiness + PoV: R$ 25-40k, 3 weeks. "
        "ROI-prioritised use-case inventory, readiness assessment, 1 PoV, "
        "90-day adoption + governance roadmap."
    ),
    "pricing_devops": (
        "DevOps Maturity Assessment: R$ 30-50k, 3-4 semanas. Avaliação DORA, "
        "auditoria de CI/CD e observabilidade, roadmap em 3 horizontes "
        "(30/90/180 dias). / DevOps Maturity Assessment: R$ 30-50k, 3-4 "
        "weeks. DORA assessment, CI/CD + observability audit, 30/90/180-day "
        "maturity roadmap."
    ),
    "pricing_growth": (
        "Growth Sales Ops Setup: retainer mensal R$ 4-8k, 2-3 semanas para "
        "stand-up inicial. Senior Solutions Architect em retainer, até 20h/mês "
        "hands-on, entregáveis escritos, Slack + async com SLA 24h úteis. / "
        "Growth Sales Ops Setup: R$ 4-8k/month retainer, 2-3 weeks to stand "
        "up, 20h/month senior SA, written deliverables, 24h Slack SLA."
    ),
    "pricing_industry": (
        "Industry Vertical Assessment: R$ 35-55k, 4 semanas. Diagnóstico "
        "setorial (regulação, compliance, benchmarks), mapa de capacidades "
        "vs. concorrência, roadmap de modernização alinhado a pressões "
        "regulatórias. / Industry Assessment: R$ 35-55k, 4 weeks. Sector "
        "diagnostic, capability map, regulatory-aligned modernisation "
        "roadmap."
    ),
    "timeline": (
        "Engajamentos típicos vão de 2 semanas (Growth setup) a 4 semanas "
        "(FinOps / Industry). Começamos com kickoff em até 5 dias úteis após "
        "assinatura. / Engagements run 2 weeks (Growth setup) to 4 weeks "
        "(FinOps / Industry). Kickoff inside 5 business days after signing."
    ),
    "team": (
        "Mila Vernazza (ex-AWS, ex-Google) é a Solutions Architect principal. "
        "Para projetos maiores, traz especialistas sob demanda (DevOps senior, "
        "Data Engineer, FinOps Analyst). Sem juniores escondidos no projeto. "
        "/ Mila Vernazza (ex-AWS, ex-Google) is the lead SA. For larger "
        "engagements she brings on-demand specialists (senior DevOps, Data "
        "Engineer, FinOps Analyst). No hidden juniors on the project."
    ),
    "guarantee": (
        "Para auditorias FinOps oferecemos garantia 3× ROI: se o saving "
        "identificado não chega a 3× o valor pago em 90 dias, devolvemos "
        "parcialmente. Outras práticas têm garantia de satisfação (revisão "
        "gratuita do entregável). / FinOps audits carry a 3× ROI guarantee "
        "— if identified savings don't reach 3× the fee inside 90 days we "
        "partial-refund. Other practices have a satisfaction guarantee "
        "(free deliverable revision)."
    ),
    "delivery_format": (
        "Entregáveis sempre escritos: relatório executivo (PDF), matriz "
        "de recomendações priorizadas, plano de execução com marcos. Todo "
        "trabalho documentado, sem 'we'll get back to you'. / Everything "
        "written: executive report (PDF), prioritised recommendation matrix, "
        "execution plan with milestones. All documented, no 'we'll get back "
        "to you'."
    ),
    "languages": (
        "Atendemos PT-BR e EN. Mila escreve em ambos os idiomas; reuniões e "
        "deliverables podem ser em qualquer dos dois. / We work in PT-BR and "
        "EN. Mila writes both; meetings and deliverables can be in either."
    ),
    "discovery_call": (
        "Reunião de discovery de 30 min, sem custo, sem compromisso. Você "
        "pode marcar direto em {calendar_url}. / 30-min discovery call, "
        "free, no commitment. Book at {calendar_url}."
    ),
    "contract_payment": (
        "Contrato assinado digitalmente (HMAC + e-sign). Pagamento via "
        "transferência (BR) ou Stripe/MP (cartão / boleto). NF-e emitida "
        "via Conta Azul. / Digitally-signed contract (HMAC + e-sign). "
        "Payment via wire (BR) or Stripe/MP (card / boleto). NF-e issued "
        "via Conta Azul."
    ),
    "stack_used": (
        "Trabalhamos principalmente com AWS, GCP, Snowflake, Databricks, "
        "dbt, Terraform, Python e a stack moderna de LLMs (Claude, OpenAI, "
        "vector DBs). Open-source primeiro quando faz sentido. / Primarily "
        "AWS, GCP, Snowflake, Databricks, dbt, Terraform, Python and modern "
        "LLM stack (Claude, OpenAI, vector DBs). Open-source first when it "
        "makes sense."
    ),
    "references": (
        "Cases sob NDA estão disponíveis em ligação. Recentes: redução de "
        "42% no spend AWS de uma fintech (R$ 180k/ano), AI gateway com "
        "evals para uma SaaS de RH, modernização DevOps de uma seguradora "
        "(LGPD-aligned). / NDA-covered cases available on call. Recent: "
        "42% AWS spend reduction at a fintech (R$ 180k/yr saved), AI "
        "gateway with evals for an HR SaaS, DevOps modernisation for an "
        "insurer (LGPD-aligned)."
    ),
}


def _faq_context_text() -> str:
    """Serialise ANUVIA_FAQ as a model-readable knowledge block."""
    lines = ["=== Anuvia FAQ (use only this knowledge; do not invent facts) ==="]
    for k, v in ANUVIA_FAQ.items():
        lines.append(f"\n[{k}]\n{v}")
    lines.append("\n=== End FAQ ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude calls — classification + auto-reply
# ---------------------------------------------------------------------------


_ALLOWED_INTENTS = (
    "interested",
    "question",
    "objection",
    "unsubscribe",
    "out_of_office",
    "no",
)

_ALLOWED_ACTIONS = (
    "auto_reply",
    "book_discovery",
    "escalate_slack",
    "stop_sequence",
    "ignore",
)

_ALLOWED_OBJECTIONS = ("price", "timing", "fit", "other")
_ALLOWED_CHANNELS = ("email", "call", "linkedin")


def _empty_classification(error: str = "") -> dict:
    """Return a safe default classification (forces Slack escalation)."""
    return {
        "intent": "objection",
        "confidence": 0.0,
        "summary": f"Classification failed: {error}" if error else "Classification unavailable.",
        "extracted_signals": {
            "wants_call": False,
            "wants_proposal": False,
            "specific_question": None,
            "objection_type": "other",
            "preferred_channel": None,
        },
        "suggested_reply": None,
        "suggested_action": "escalate_slack",
    }


def _coerce_classification(raw: Any) -> dict:
    """Normalise whatever Claude returned into the strict schema.

    Defensive — Claude occasionally returns extra keys, missing keys, or
    JSON wrapped in markdown fences. We sanitise to keep callers simple.
    """
    if isinstance(raw, str):
        # Strip ``` fences if present.
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        try:
            raw = json.loads(s)
        except Exception:  # noqa: BLE001
            # Try to find the first JSON object in the string.
            m = re.search(r"\{.*\}", s, re.DOTALL)
            if m:
                try:
                    raw = json.loads(m.group(0))
                except Exception:  # noqa: BLE001
                    return _empty_classification("invalid json from model")
            else:
                return _empty_classification("no json from model")

    if not isinstance(raw, dict):
        return _empty_classification("non-dict result")

    intent = str(raw.get("intent") or "").lower().strip()
    if intent not in _ALLOWED_INTENTS:
        intent = "no"

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    summary = str(raw.get("summary") or "")[:500]

    signals = raw.get("extracted_signals") or {}
    if not isinstance(signals, dict):
        signals = {}

    objection_type = signals.get("objection_type")
    if isinstance(objection_type, str):
        objection_type = objection_type.lower().strip()
        if objection_type not in _ALLOWED_OBJECTIONS:
            objection_type = "other"
    else:
        objection_type = None

    preferred_channel = signals.get("preferred_channel")
    if isinstance(preferred_channel, str):
        preferred_channel = preferred_channel.lower().strip()
        if preferred_channel not in _ALLOWED_CHANNELS:
            preferred_channel = None
    else:
        preferred_channel = None

    specific_question = signals.get("specific_question")
    if specific_question is not None and not isinstance(specific_question, str):
        specific_question = str(specific_question)
    if isinstance(specific_question, str):
        specific_question = specific_question.strip()[:500] or None

    extracted = {
        "wants_call": bool(signals.get("wants_call")),
        "wants_proposal": bool(signals.get("wants_proposal")),
        "specific_question": specific_question,
        "objection_type": objection_type,
        "preferred_channel": preferred_channel,
    }

    suggested_reply = raw.get("suggested_reply")
    if suggested_reply is not None and not isinstance(suggested_reply, str):
        suggested_reply = str(suggested_reply)
    if isinstance(suggested_reply, str):
        suggested_reply = suggested_reply.strip() or None

    suggested_action = str(raw.get("suggested_action") or "").lower().strip()
    if suggested_action not in _ALLOWED_ACTIONS:
        suggested_action = _action_for_intent(intent, confidence)

    return {
        "intent": intent,
        "confidence": confidence,
        "summary": summary,
        "extracted_signals": extracted,
        "suggested_reply": suggested_reply,
        "suggested_action": suggested_action,
    }


def _action_for_intent(intent: str, confidence: float) -> str:
    """Map intent → action when Claude didn't supply a valid suggestion.

    Confidence floor is enforced in the routing layer too, but we mirror it
    here so the suggested_action field is consistent.
    """
    if confidence < CONFIDENCE_FLOOR:
        return "escalate_slack"
    return {
        "interested": "book_discovery",
        "question": "auto_reply",
        "objection": "escalate_slack",
        "unsubscribe": "stop_sequence",
        "out_of_office": "ignore",
        "no": "stop_sequence",
    }.get(intent, "escalate_slack")


async def _call_claude_json(prompt: str, system: str, max_tokens: int = 700) -> str:
    """One-shot Claude call. Returns the assistant text or raises."""
    if not ANTHROPIC_API_KEY:
        log.warning("reply_classify: ANTHROPIC_API_KEY unset — returning empty result")
        return ""

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if r.status_code >= 400:
        log.error(
            "reply_classify: anthropic non-200 status=%s body=%s",
            r.status_code, r.text[:300],
        )
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:200]}")
    body = r.json()
    blocks = body.get("content") or []
    parts: List[str] = []
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text") or "")
    return "\n".join(parts).strip()


async def classify_reply_intent(
    body_text: str,
    subject: str,
    sender: str,
    original_context: Optional[dict] = None,
) -> dict:
    """Classify the inbound reply via Claude. Returns the strict schema dict.

    Never raises — on any failure returns a safe default that routes to
    Slack escalation so a human reviews. The original outbound touch
    (subject, last sent body, days since last touch, practice) may be
    passed via `original_context` to disambiguate ambiguous one-liners
    like "ok" or "lgtm".
    """
    cleaned = _strip_quoted_reply(body_text or "")[:5000]
    subject = (subject or "")[:400]
    sender = (sender or "").lower()[:200]
    ctx = original_context or {}

    system = (
        "You classify inbound email replies to a B2B sales sequence run by "
        "Anuvia (senior cloud/AI consultancy, founder Mila Vernazza, BR-first, "
        "also EN). Output STRICT JSON only — no prose, no markdown fences. "
        "Schema:\n"
        "{\n"
        '  "intent": "interested" | "question" | "objection" | "unsubscribe" '
        '| "out_of_office" | "no",\n'
        '  "confidence": 0.0 to 1.0,\n'
        '  "summary": "1-2 sentence summary",\n'
        '  "extracted_signals": {\n'
        '    "wants_call": boolean,\n'
        '    "wants_proposal": boolean,\n'
        '    "specific_question": string or null,\n'
        '    "objection_type": "price" | "timing" | "fit" | "other" | null,\n'
        '    "preferred_channel": "email" | "call" | "linkedin" | null\n'
        "  },\n"
        '  "suggested_reply": string or null (only for "question" intent, 3-5 '
        "sentences, Mila's voice: direct, technical, no sales clichés),\n"
        '  "suggested_action": "auto_reply" | "book_discovery" | '
        '"escalate_slack" | "stop_sequence" | "ignore"\n'
        "}\n"
        "Rules:\n"
        "- 'interested' = wants to talk / book a call / move forward.\n"
        "- 'question' = specific clarifying question we can answer.\n"
        "- 'objection' = pushback we should handle (price, timing, fit).\n"
        "- 'unsubscribe' = opt-out, remove me, stop, don't email again.\n"
        "- 'out_of_office' = auto-reply, vacation, on leave.\n"
        "- 'no' = polite decline without follow-up potential.\n"
        "- When the reply is ambiguous, set confidence < 0.6 — we will "
        "escalate to a human.\n"
        "- Never invent facts about Anuvia in suggested_reply; use only "
        "what's in the FAQ context provided."
    )

    ctx_lines = []
    if ctx.get("last_subject"):
        ctx_lines.append(f"Last outbound subject: {ctx['last_subject']}")
    if ctx.get("touch_num") is not None:
        ctx_lines.append(f"This is a reply to touch #{ctx['touch_num']}.")
    if ctx.get("practice"):
        ctx_lines.append(f"Practice: {ctx['practice']}")
    if ctx.get("language"):
        ctx_lines.append(f"Language: {ctx['language']}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(no prior context)"

    user_prompt = (
        f"{_faq_context_text()}\n\n"
        f"--- Outbound context ---\n{ctx_block}\n\n"
        f"--- Inbound reply ---\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Body:\n{cleaned}\n\n"
        "Return JSON only."
    )

    try:
        raw = await _call_claude_json(user_prompt, system, max_tokens=900)
    except Exception as exc:  # noqa: BLE001
        log.exception("reply_classify: claude classify failed")
        return _empty_classification(str(exc)[:120])

    if not raw:
        return _empty_classification("empty model response")

    return _coerce_classification(raw)


# ---------------------------------------------------------------------------
# Outbound email helper (Resend) — used by auto-reply + book-discovery
# ---------------------------------------------------------------------------


async def _send_email_via_resend(
    to: str,
    subject: str,
    html: str,
    *,
    tags: Optional[List[Dict[str, str]]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> Optional[str]:
    """Send an outbound email via Resend. Returns message id or None."""
    if not RESEND_API_KEY:
        log.info(
            "reply_classify: RESEND_API_KEY unset; dry-run send to=%s subject=%s",
            to, subject,
        )
        return None

    headers_block: Dict[str, str] = {}
    if in_reply_to:
        headers_block["In-Reply-To"] = f"<{in_reply_to}>" if not in_reply_to.startswith("<") else in_reply_to
    if references:
        refs = " ".join(
            (f"<{r}>" if not r.startswith("<") else r) for r in references
        )
        if refs:
            headers_block["References"] = refs

    payload: Dict[str, Any] = {
        "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
        "to": [to],
        "reply_to": f"{RESEND_FROM_NAME} <{RESEND_REPLY_TO}>",
        "subject": subject,
        "html": html,
        "tags": tags or [{"name": "category", "value": "reply_auto"}],
    }
    if headers_block:
        payload["headers"] = headers_block

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
        if r.status_code >= 400:
            log.error(
                "reply_classify: resend non-200 status=%s body=%s",
                r.status_code, r.text[:300],
            )
            return None
        body = r.json() if r.text else {}
        return body.get("id") if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        log.exception("reply_classify: resend send failed to=%s", to)
        return None


# ---------------------------------------------------------------------------
# Auto-reply (intent='question')
# ---------------------------------------------------------------------------


async def _generate_auto_reply_body(
    sender_name: Optional[str],
    inbound_text: str,
    inbound_subject: str,
    classification: dict,
    language: str,
) -> str:
    """Produce a 3-5 sentence response in Mila's voice, grounded in the FAQ."""
    if classification.get("suggested_reply"):
        # The classifier already wrote one — trust it but strip leading fences.
        body = str(classification["suggested_reply"]).strip()
        if body:
            return body

    if language == "en":
        system = (
            "You are Mila Vernazza, founder of Anuvia (senior cloud / AI / "
            "engineering / growth / industry consultancy). Voice: direct, "
            "technical, no marketing fluff, no exclamation marks, no 'hope "
            "this email finds you well'. Reply in 3-5 sentences max. "
            "Answer the prospect's question using ONLY facts in the FAQ "
            "below — do NOT invent numbers, names, or commitments. End "
            "with a single soft CTA (a question, OR an offer to jump on a "
            "30-min call: " + CALENDAR_URL + "). Sign: 'Anuvia · Mila "
            "Vernazza' (no mini-bio). Output PLAIN TEXT only, no subject "
            "line, no greetings beyond a single 'Hi <first_name>,' if known."
        )
    else:
        system = (
            "Você é a Mila Vernazza, fundadora da Anuvia (consultoria sênior "
            "de cloud / IA / engineering / growth / industry). Voz: direta, "
            "técnica, sem clichês de venda, sem exclamações, sem 'espero que "
            "esteja bem'. Responda em 3-5 frases no máximo. Use APENAS fatos "
            "presentes no FAQ abaixo — não invente números, nomes ou "
            "compromissos. Termine com UM CTA suave (uma pergunta OU uma "
            "oferta de call de 30 min: " + CALENDAR_URL + "). Assine: "
            "'Anuvia · Mila Vernazza' (sem mini-bio). Devolva TEXTO PURO, "
            "sem subject, sem saudação além de um único 'Olá <primeiro_nome>,' "
            "quando o nome for conhecido."
        )

    first_name = (sender_name or "").split(" ")[0] if sender_name else ""
    extracted_q = (
        (classification.get("extracted_signals") or {}).get("specific_question")
        or classification.get("summary")
        or ""
    )
    cleaned_inbound = _strip_quoted_reply(inbound_text or "")[:2500]

    user_prompt = (
        f"{_faq_context_text()}\n\n"
        f"--- Inbound ---\n"
        f"From first name: {first_name or '(unknown)'}\n"
        f"Subject: {inbound_subject}\n"
        f"Question (extracted): {extracted_q}\n"
        f"Full text:\n{cleaned_inbound}\n\n"
        "Write the reply now (plain text, no preamble, no markdown)."
    )

    try:
        text = await _call_claude_json(user_prompt, system, max_tokens=500)
    except Exception:  # noqa: BLE001
        log.exception("reply_classify: auto-reply generation failed")
        return ""
    return (text or "").strip()


def _html_wrap_reply(body_text: str) -> str:
    """Wrap a plain-text reply in a tiny HTML scaffold for Resend."""
    safe = body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = "".join(
        f"<p style=\"margin:0 0 12px 0;line-height:1.55;\">{p.strip()}</p>"
        for p in safe.split("\n\n") if p.strip()
    )
    return (
        "<div style=\"font-family:Inter,-apple-system,sans-serif;"
        "font-size:15px;color:#0f172a;max-width:560px;\">"
        f"{paragraphs}"
        "</div>"
    )


async def _send_auto_reply(
    *,
    target_email: str,
    sender_name: Optional[str],
    inbound_subject: str,
    inbound_text: str,
    inbound_message_id: Optional[str],
    references: List[str],
    classification: dict,
    language: str,
    lead_id: Optional[str],
    prospect_id: Optional[str],
) -> Optional[str]:
    """Generate + send an auto-reply email. Returns Resend message id or None."""
    body_text = await _generate_auto_reply_body(
        sender_name=sender_name,
        inbound_text=inbound_text,
        inbound_subject=inbound_subject,
        classification=classification,
        language=language,
    )
    if not body_text:
        log.warning("reply_classify: auto-reply body empty; skipping send")
        return None

    if inbound_subject.lower().startswith(("re:", "res:")):
        reply_subject = inbound_subject
    else:
        reply_subject = f"Re: {inbound_subject}" if inbound_subject else "Re:"

    tags = [
        {"name": "category", "value": "reply_auto"},
        {"name": "intent", "value": str(classification.get("intent") or "question")},
    ]
    if lead_id:
        tags.append({"name": "lead_id", "value": lead_id})
    if prospect_id:
        tags.append({"name": "prospect_id", "value": prospect_id})

    refs = list(references or [])
    if inbound_message_id and inbound_message_id not in refs:
        refs.append(inbound_message_id)

    return await _send_email_via_resend(
        target_email,
        reply_subject,
        _html_wrap_reply(body_text),
        tags=tags,
        in_reply_to=inbound_message_id,
        references=refs,
    )


# ---------------------------------------------------------------------------
# Book-discovery email (intent='interested')
# ---------------------------------------------------------------------------


async def _send_book_discovery(
    *,
    target_email: str,
    sender_name: Optional[str],
    inbound_subject: str,
    inbound_message_id: Optional[str],
    references: List[str],
    language: str,
    lead_id: Optional[str],
    prospect_id: Optional[str],
) -> Optional[str]:
    """Send a short email with a calendar link. Returns Resend message id."""
    first_name = (sender_name or "").split(" ")[0] if sender_name else ""

    context_url = (
        f"{PUBLIC_HOST_PT}/contact?lead_id={lead_id}"
        if lead_id else f"{PUBLIC_HOST_PT}/contact"
    )

    if language == "en":
        subject = inbound_subject if inbound_subject.lower().startswith("re:") else f"Re: {inbound_subject or 'Quick chat'}"
        hello = f"Hi {first_name}," if first_name else "Hi,"
        body = (
            f"{hello}\n\n"
            "Glad this lands. A 30-min discovery call is the fastest way "
            "to scope the right engagement. You can pick a slot directly "
            f"here: {CALENDAR_URL}\n\n"
            f"If you'd rather share context first, send a quick brief: "
            f"{context_url}\n\n"
            "Anuvia · Mila Vernazza"
        )
    else:
        subject = inbound_subject if inbound_subject.lower().startswith(("re:", "res:")) else f"Re: {inbound_subject or 'Vamos conversar'}"
        hello = f"Olá {first_name}," if first_name else "Olá,"
        body = (
            f"{hello}\n\n"
            "Que ótimo. Uma call de discovery de 30 min é o jeito mais "
            "rápido de escopar o engajamento certo. Você pode marcar "
            f"direto aqui: {CALENDAR_URL}\n\n"
            f"Se preferir mandar contexto antes, fica mais fácil por aqui: "
            f"{context_url}\n\n"
            "Anuvia · Mila Vernazza"
        )

    tags = [
        {"name": "category", "value": "reply_book_discovery"},
        {"name": "intent", "value": "interested"},
    ]
    if lead_id:
        tags.append({"name": "lead_id", "value": lead_id})
    if prospect_id:
        tags.append({"name": "prospect_id", "value": prospect_id})

    refs = list(references or [])
    if inbound_message_id and inbound_message_id not in refs:
        refs.append(inbound_message_id)

    return await _send_email_via_resend(
        target_email,
        subject,
        _html_wrap_reply(body),
        tags=tags,
        in_reply_to=inbound_message_id,
        references=refs,
    )


# ---------------------------------------------------------------------------
# Slack escalation
# ---------------------------------------------------------------------------


def _last_outbound_touch(target: dict) -> Optional[dict]:
    """Return the most recent outbound touch artifact, if any.

    Reads `artifacts` (leads) or `enriched_data.touches` (prospects) and
    returns a small dict with subject + touch_num + ts.
    """
    artifacts = target.get("artifacts") or []
    if isinstance(artifacts, list):
        outbound = [
            a for a in artifacts
            if isinstance(a, dict)
            and a.get("type") in ("outbound_email", "proposal_pdf", "email_sent")
        ]
        if outbound:
            last = outbound[-1]
            meta = last.get("meta") or {}
            return {
                "subject": meta.get("subject") or last.get("type"),
                "touch_num": meta.get("touch_num"),
                "ts": last.get("ts"),
            }

    enriched = target.get("enriched_data") or {}
    if isinstance(enriched, str):
        try:
            enriched = json.loads(enriched)
        except Exception:  # noqa: BLE001
            enriched = {}
    if isinstance(enriched, dict):
        touches = enriched.get("touches") or []
        if isinstance(touches, list) and touches:
            last = touches[-1]
            if isinstance(last, dict):
                return {
                    "subject": last.get("subject"),
                    "touch_num": last.get("touch_num") or target.get("current_touch"),
                    "ts": last.get("ts"),
                }
    return None


async def _escalate_to_slack(
    *,
    target: dict,
    target_kind: str,  # 'lead' or 'prospect'
    sender_email: str,
    sender_name: Optional[str],
    inbound_subject: str,
    inbound_text: str,
    classification: dict,
) -> None:
    """Post a rich Slack message so Mila can reply manually. Never raises."""
    if not SLACK_WEBHOOK:
        log.warning(
            "reply_classify: no slack webhook configured; classification=%s",
            classification.get("intent"),
        )
        return

    name = (
        target.get("name")
        or " ".join(
            x for x in (target.get("first_name"), target.get("last_name")) if x
        ).strip()
        or sender_name
        or sender_email
    )
    company = target.get("company") or "(unknown company)"
    last_touch = _last_outbound_touch(target) or {}

    target_id = target.get("id") or ""
    if target_kind == "lead":
        context_link = f"{PUBLIC_HOST_PT}/api/session/{target_id}"
    else:
        context_link = f"{PUBLIC_HOST_PT}/api/prospect/{target_id}"

    extracted = classification.get("extracted_signals") or {}
    inbound_clean = _strip_quoted_reply(inbound_text or "")[:1500]

    signals_pretty = json.dumps(extracted, ensure_ascii=False, indent=2)

    intent = classification.get("intent", "?")
    confidence = classification.get("confidence", 0.0)
    suggested = (
        classification.get("suggested_reply")
        or "(no suggested reply — write fresh)"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Inbound reply — {intent} ({confidence:.2f})",
                "emoji": False,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Who*\n{name}\n_{company}_"},
                {"type": "mrkdwn", "text": f"*Email*\n{sender_email}"},
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Last touch*\n#{last_touch.get('touch_num') or '?'}: "
                        f"{last_touch.get('subject') or '(unknown)'}\n"
                        f"_{last_touch.get('ts') or 'n/a'}_"
                    ),
                },
                {"type": "mrkdwn", "text": f"*Subject*\n{inbound_subject or '(no subject)'}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Summary*\n{classification.get('summary', '')}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Signals*\n```{signals_pretty}```",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reply text*\n```{inbound_clean}```",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested response (Claude)*\n{suggested}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<{context_link}|Full context>"},
            ],
        },
    ]

    payload = {
        "text": (
            f"Inbound reply from {name} ({sender_email}) — intent={intent} "
            f"confidence={confidence:.2f}"
        ),
        "blocks": blocks,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(SLACK_WEBHOOK, json=payload)
        if r.status_code >= 400:
            log.warning(
                "reply_classify: slack webhook returned %s: %s",
                r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001 — Slack outages must not raise
        log.warning("reply_classify: slack escalation failed: %s", exc)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def _resolve_action(intent: str, confidence: float, suggested: str) -> str:
    """Pick the final action. Confidence floor always wins.

    Out-of-office stays at 'ignore' regardless of confidence — false-positive
    OOO replies are very low-risk to ignore (worst case Mila replies later).
    """
    if intent == "out_of_office":
        return "ignore"
    if intent == "unsubscribe":
        return "stop_sequence"
    if confidence < CONFIDENCE_FLOOR:
        return "escalate_slack"
    if suggested in _ALLOWED_ACTIONS:
        return suggested
    return _action_for_intent(intent, confidence)


async def _record_inbound_signal(
    *,
    lead_id: Optional[str],
    prospect: Optional[dict],
    classification: dict,
    payload_meta: dict,
) -> None:
    """Persist the classification on the lead (preferred) or prospect."""
    entry_payload = {
        "intent": classification.get("intent"),
        "confidence": classification.get("confidence"),
        "summary": classification.get("summary"),
        "extracted_signals": classification.get("extracted_signals"),
        "suggested_action": classification.get("suggested_action"),
        "message_id": payload_meta.get("message_id"),
        "in_reply_to": payload_meta.get("in_reply_to"),
        "subject": payload_meta.get("subject"),
    }

    if lead_id:
        try:
            await session_append_signal(
                lead_id,
                kind="inbound_reply",
                value=str(classification.get("intent") or ""),
                source="reply_classify",
            )
            # session_append_signal records the kind/value/source only; we also
            # want the full payload for later replay → append a second signal
            # entry that nests it under value (kept as JSON string).
            # Simpler: do a single read-modify-write on signals via the helper.
        except Exception:  # noqa: BLE001
            log.exception("reply_classify: append signal failed lead=%s", lead_id)
        # Also stash the rich payload via the agent_history audit log.
        try:
            await session_append_history(
                lead_id=lead_id,
                agent="reply_classify",
                action="classify_reply",
                result=str(classification.get("intent") or ""),
                detail=json.dumps(entry_payload, ensure_ascii=False)[:1500],
            )
        except Exception:  # noqa: BLE001
            log.exception("reply_classify: append history failed lead=%s", lead_id)

    if prospect:
        entry = {
            "ts": _now_iso(),
            "kind": "inbound_reply",
            "value": classification.get("intent"),
            "source": "reply_classify",
            "payload": entry_payload,
        }
        await _prospect_append_signal(prospect, entry)


async def _stop_sequence(
    *,
    lead_id: Optional[str],
    prospect: Optional[dict],
    intent: str,
) -> None:
    """Mark the prospect/lead as never-contact-again."""
    new_prospect_status = "unsubscribed" if intent == "unsubscribe" else "declined"
    if prospect and prospect.get("id"):
        await _prospect_update(
            prospect["id"],
            status=new_prospect_status,
            next_touch_at=None,
        )
    if lead_id:
        try:
            await session_set_status(lead_id, "lost")
            await session_set_next(lead_id, None, None)
        except Exception:  # noqa: BLE001
            log.exception("reply_classify: stop_sequence lead writes failed lead=%s", lead_id)


async def _pause_sequence(
    *,
    lead_id: Optional[str],
    prospect: Optional[dict],
    days: int = AUTO_REPLY_PAUSE_DAYS,
) -> None:
    """Pause the autopilot for `days` days. Keeps the relationship warm but quiet."""
    until = _now() + asyncio_timedelta(days=days)
    if prospect and prospect.get("id"):
        await _prospect_update(
            prospect["id"],
            status="replied",
            next_touch_at=until.isoformat(),
        )
    if lead_id:
        try:
            # We don't have a concrete handler to schedule; just clear the
            # next-action so the orchestrator stops nagging. Mila/Track-B
            # can re-queue when she replies manually.
            await session_set_next(lead_id, None, None)
        except Exception:  # noqa: BLE001
            log.exception("reply_classify: pause writes failed lead=%s", lead_id)


def asyncio_timedelta(*, days: int = 0, hours: int = 0):
    """Tiny helper to avoid an `import` line for `timedelta` deep in the file."""
    from datetime import timedelta as _td
    return _td(days=days, hours=hours)


# ---------------------------------------------------------------------------
# Background processing — the core orchestration
# ---------------------------------------------------------------------------


async def _process_inbound(raw_payload: dict) -> dict:
    """Full pipeline: normalise → match → classify → act. Returns summary dict.

    Returned summary is logged + used by the test harness; the HTTP handler
    already returned 200 to the webhook caller before this kicks off.
    """
    norm = _normalise_payload(raw_payload)
    sender = norm.get("from_email") or ""
    if not sender:
        log.warning("reply_classify: no sender email in payload; dropping")
        return {"ok": False, "reason": "no sender"}

    # 1. Find prospect or lead ------------------------------------------------
    prospect = await _find_prospect_by_email(sender)
    lead = None
    if prospect and prospect.get("converted_lead_id"):
        # Prefer the converted lead row when it exists.
        try:
            lead = await session_get(str(prospect["converted_lead_id"]))
        except Exception:  # noqa: BLE001
            lead = None
    if lead is None:
        lead = await _find_lead_by_email(sender)

    target = lead or prospect
    if target is None:
        log.warning(
            "reply_classify: no lead/prospect for sender=%s; recording as orphan",
            sender,
        )
        # We still try to escalate to Slack so Mila can see the inbound.
        classification = await classify_reply_intent(
            body_text=norm.get("text", ""),
            subject=norm.get("subject", ""),
            sender=sender,
        )
        await _escalate_to_slack(
            target={"name": norm.get("from_name") or "(orphan)", "id": ""},
            target_kind="orphan",
            sender_email=sender,
            sender_name=norm.get("from_name"),
            inbound_subject=norm.get("subject", ""),
            inbound_text=norm.get("text", ""),
            classification=classification,
        )
        return {"ok": True, "matched": False, "intent": classification.get("intent")}

    target_kind = "lead" if lead is not None else "prospect"
    lead_id = str(lead["id"]) if lead else None
    prospect_id = str(prospect["id"]) if prospect else None

    # 2. Idempotency check ----------------------------------------------------
    message_id = norm.get("message_id")
    if message_id and _has_inbound_message_id(target, message_id):
        log.info(
            "reply_classify: duplicate inbound message_id=%s for %s; skipping",
            message_id, sender,
        )
        return {
            "ok": True,
            "deduplicated": True,
            "lead_id": lead_id,
            "prospect_id": prospect_id,
        }

    # 3. Build classification context from the target row ---------------------
    language = (
        (target.get("language") if isinstance(target.get("language"), str) else None)
        or "pt"
    )
    last_touch = _last_outbound_touch(target) or {}
    ctx = {
        "last_subject": last_touch.get("subject"),
        "touch_num": last_touch.get("touch_num") or target.get("current_touch"),
        "practice": target.get("practice_fit") or target.get("funnel_id"),
        "language": language,
    }

    classification = await classify_reply_intent(
        body_text=norm.get("text", ""),
        subject=norm.get("subject", ""),
        sender=sender,
        original_context=ctx,
    )

    # 4. Persist signal + history --------------------------------------------
    await _record_inbound_signal(
        lead_id=lead_id,
        prospect=prospect,
        classification=classification,
        payload_meta={
            "message_id": message_id,
            "in_reply_to": norm.get("in_reply_to"),
            "subject": norm.get("subject"),
        },
    )

    # 5. Decide + act ---------------------------------------------------------
    intent = classification.get("intent", "no")
    confidence = float(classification.get("confidence") or 0.0)
    action = _resolve_action(
        intent=intent,
        confidence=confidence,
        suggested=str(classification.get("suggested_action") or ""),
    )
    log.info(
        "reply_classify: sender=%s intent=%s conf=%.2f action=%s",
        sender, intent, confidence, action,
    )

    action_result: Dict[str, Any] = {"action": action}

    if action == "auto_reply":
        msg_id = await _send_auto_reply(
            target_email=sender,
            sender_name=norm.get("from_name") or target.get("name"),
            inbound_subject=norm.get("subject", ""),
            inbound_text=norm.get("text", ""),
            inbound_message_id=message_id,
            references=norm.get("references") or [],
            classification=classification,
            language=language,
            lead_id=lead_id,
            prospect_id=prospect_id,
        )
        action_result["resend_id"] = msg_id
        await _pause_sequence(lead_id=lead_id, prospect=prospect)

    elif action == "book_discovery":
        msg_id = await _send_book_discovery(
            target_email=sender,
            sender_name=norm.get("from_name") or target.get("name"),
            inbound_subject=norm.get("subject", ""),
            inbound_message_id=message_id,
            references=norm.get("references") or [],
            language=language,
            lead_id=lead_id,
            prospect_id=prospect_id,
        )
        action_result["resend_id"] = msg_id
        # Extra signal so downstream queries can find "interested" quickly.
        if lead_id:
            try:
                await session_append_signal(
                    lead_id,
                    kind="interested",
                    value="reply",
                    source="reply_classify",
                )
            except Exception:  # noqa: BLE001
                log.exception("reply_classify: interested signal write failed")
        await _pause_sequence(lead_id=lead_id, prospect=prospect)
        # Also tell Mila so she can prep for the call.
        await _escalate_to_slack(
            target=target,
            target_kind=target_kind,
            sender_email=sender,
            sender_name=norm.get("from_name"),
            inbound_subject=norm.get("subject", ""),
            inbound_text=norm.get("text", ""),
            classification=classification,
        )

    elif action == "escalate_slack":
        await _escalate_to_slack(
            target=target,
            target_kind=target_kind,
            sender_email=sender,
            sender_name=norm.get("from_name"),
            inbound_subject=norm.get("subject", ""),
            inbound_text=norm.get("text", ""),
            classification=classification,
        )

    elif action == "stop_sequence":
        await _stop_sequence(lead_id=lead_id, prospect=prospect, intent=intent)

    elif action == "ignore":
        # Out-of-office: do nothing. Sequence keeps running.
        pass

    # 6. Final agent_history entry (always, mirrors the contract) -------------
    if lead_id:
        try:
            await session_append_history(
                lead_id=lead_id,
                agent="reply_classify",
                action=action,
                result=intent,
                detail=(classification.get("summary") or "")[:500],
            )
        except Exception:  # noqa: BLE001
            log.exception("reply_classify: final history append failed")

    return {
        "ok": True,
        "lead_id": lead_id,
        "prospect_id": prospect_id,
        "intent": intent,
        "confidence": confidence,
        **action_result,
    }


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/reply", tags=["reply"])


@router.post("/inbound")
async def inbound(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Receive an inbound email webhook (Resend / Cloudflare / Mailgun shape).

    Auth: optional ``INBOUND_WEBHOOK_SECRET`` env var. When set, the request
    must carry either:

      * ``X-Webhook-Signature: sha256=<hmac-sha256-hex-of-body>``  OR
      * ``?key=<secret>`` query parameter (for forwarders that can't sign).

    Returns 200 ``{"ok": true}`` quickly; the real work runs in a background
    task so the webhook caller doesn't time out on Claude / Resend latency.
    """
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    query_key = request.query_params.get("key")

    if not _verify_inbound_signature(raw, headers, query_key):
        log.warning("reply_classify: rejected inbound webhook — bad signature")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    # Quick sanity log — full processing happens in background.
    norm_preview = _normalise_payload(payload)
    log.info(
        "reply_classify: inbound from=%s subject=%r msgid=%s",
        norm_preview.get("from_email"),
        norm_preview.get("subject", "")[:80],
        norm_preview.get("message_id"),
    )

    background_tasks.add_task(_safe_process_inbound, payload)
    return {"ok": True}


async def _safe_process_inbound(payload: dict) -> None:
    """Wrapper that NEVER raises — keeps the background task slot clean."""
    try:
        result = await _process_inbound(payload)
        log.info("reply_classify: processed inbound: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("reply_classify: background processing crashed")
