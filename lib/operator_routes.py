"""Operator routes — manual operations Mila performs between autonomous
phases of the funnel.

The main lever right now is **converting a qualified lead into an engagement**
after a successful discovery call. Today FinOps and the other senior-ticket
practices have ``autonomous_enabled=False`` (see :mod:`lib.track_b`), so
that conversion is a deliberate operator action — Mila reviews the lead,
agrees on a price, and clicks "Convert to engagement" in the admin
dashboard. This file contains the endpoint that backs that button.

End-to-end behaviour:

1. Verify the admin HMAC token (same gate as the smoke endpoint).
2. Load the lead by id; bail if missing or already converted (idempotency
   on ``qualification_data.active_engagement_id``).
3. Insert a contract row with ``status='signed'`` — we assume the operator
   only clicks this AFTER closing the deal verbally / via signed PDF /
   Stripe checkout / etc. The value defaults to the practice's
   ``ticket_max_brl`` from ``track_b._PRACTICE_DEFAULTS`` so pricing v2
   (100% upfront) is the default.
4. Insert an engagement row with ``status='kickoff'``,
   ``delivery_mode='whiteglove'`` (default) and ``current_phase=0``.
5. Patch the lead to point at the new engagement
   (``qualification_data.active_engagement_id``) and queue the kickoff
   handler (``next_action='engagement_kickoff_<practice>'``).
6. Fire the kickoff handler synchronously so the client gets the kickoff
   email (with intake form link) immediately.
7. Fire phase 1 once so the agent_history shows ``waiting_for_intake`` —
   gives the operator a clean trail in the engagement dashboard.
8. Slack-DM Mila a confirmation with the intake URL + engagement link so
   she can monitor progress without having to dig in the dashboard.
9. Return the engagement_id + intake_url so the admin dashboard can
   surface them in the response.

Mounted in :mod:`app` via ``app.include_router(operator_router)``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("anuvia-operator")

router = APIRouter(prefix="/api/_admin/operator", tags=["operator"])

HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)


# Practice config mirrors lib.admin_smoke._PRACTICE_CONFIG. We keep a local
# slim copy so this module doesn't import admin_smoke (which carries a much
# larger surface). Update both files when pricing v2 changes.
_PRACTICE_CONFIG: Dict[str, Dict[str, Any]] = {
    "cloud_finops": {
        "value_brl": 60000,
        "total_phases": 4,
        "kickoff_action": "engagement_kickoff_cloud_finops",
        "phase_1_action": "finops_phase_1_data_collection",
        "intake_slug": "finops",
    },
    "ai": {
        "value_brl": 40000,
        "total_phases": 3,
        "kickoff_action": "engagement_kickoff_ai",
        "phase_1_action": "ai_phase_1_discovery",
        "intake_slug": "ai",
    },
    "devops": {
        "value_brl": 50000,
        "total_phases": 3,
        "kickoff_action": "engagement_kickoff_devops",
        "phase_1_action": "devops_phase_1_baseline",
        "intake_slug": "devops",
    },
    "growth_salesops": {
        "value_brl": 35000,
        "total_phases": 3,
        "kickoff_action": "engagement_kickoff_growth_salesops",
        "phase_1_action": "growth_phase_1_pipeline_audit",
        "intake_slug": "growth",
    },
    "industry": {
        "value_brl": 55000,
        "total_phases": 4,
        "kickoff_action": "engagement_kickoff_industry",
        "phase_1_action": "industry_phase_1_vertical_diagnostic",
        "intake_slug": "industry",
    },
}


def _verify_admin_token(token: str) -> bool:
    """HMAC token check — same shape as admin_smoke (purpose=admin_smoke).
    Operator endpoints reuse the same gate so Mila doesn't juggle multiple
    secrets in the dashboard JS."""
    if not HMAC_SECRET or not token:
        return False
    expected = hmac.new(
        HMAC_SECRET.encode("utf-8"),
        "admin_smoke".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def _intake_token(engagement_id: str) -> str:
    """HMAC token for the client-facing intake form URL."""
    if not HMAC_SECRET:
        return ""
    return hmac.new(
        HMAC_SECRET.encode("utf-8"),
        f"{engagement_id}:intake".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _supa(client: httpx.AsyncClient):
    """Lazy import of SUPA constants — avoids a circular import at module load."""
    from lib.sessions import SUPA_URL, SUPA_HEADERS  # noqa: WPS433
    return SUPA_URL, SUPA_HEADERS


async def _supa_get(client: httpx.AsyncClient, table: str, query: str):
    url, headers = await _supa(client)
    r = await client.get(f"{url}/{table}?{query}", headers=headers)
    if r.status_code != 200:
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def _supa_insert(
    client: httpx.AsyncClient, table: str, row: dict
) -> dict:
    url, headers = await _supa(client)
    h = {**headers, "Prefer": "return=representation"}
    r = await client.post(f"{url}/{table}", headers=h, json=row)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"supa insert {table} {r.status_code}: {r.text[:300]}"
        )
    data = r.json()
    return data[0] if isinstance(data, list) else data


async def _supa_patch(
    client: httpx.AsyncClient, table: str, query: str, row: dict
):
    url, headers = await _supa(client)
    h = {**headers, "Prefer": "return=representation"}
    r = await client.patch(f"{url}/{table}?{query}", headers=h, json=row)
    if r.status_code not in (200, 204):
        log.warning(
            "operator supa_patch %s %s: %s",
            table, r.status_code, r.text[:200],
        )
    return r


async def _run_handler(action: str, lead: dict) -> dict:
    """Dispatch a handler by name via the orchestrator registry."""
    from lib.orchestrator import HANDLERS  # noqa: WPS433
    fn = HANDLERS.get(action)
    if not fn:
        return {"ok": False, "error": f"handler {action} not registered"}
    try:
        return await fn(lead)
    except Exception as exc:  # noqa: BLE001
        log.exception("operator handler %s failed", action)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _slack_alert(text: str) -> None:
    """Best-effort Slack ping — never raises."""
    webhook = (
        os.environ.get("SLACK_ALERTS_WEBHOOK")
        or os.environ.get("SLACK_NEW_LEAD_WEBHOOK")
    )
    if not webhook:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook, json={"text": text})
    except Exception:  # noqa: BLE001
        log.exception("operator slack alert failed")


@router.post("/send_contract")
async def operator_send_contract(request: Request):
    """Generate + email a contract for the lead.

    This is the *production* path. Versus ``/convert_lead_to_engagement``
    (which assumes the contract is signed + paid outside the system),
    this endpoint runs the full real flow:

    1. Call :func:`lib.contract.generate_contract` to mint a contract
       row (status='pending'), render the PDF via Gotenberg, attach a
       sign-link (HMAC or Google eSign) and a payment URL (Pix for BRL,
       Stripe US for USD).
    2. Call :func:`lib.contract.send_contract_email` to email the lead
       with the PDF + sign link + ROI guarantee blurb.
    3. Patch the lead's ``current_stage`` to ``contract_sent`` and
       record ``qualification_data.contract_sent_at`` for the operator
       timeline.
    4. Slack alert the operator with the contract URLs so Mila can
       monitor / re-send / chase manually if needed.

    The CLIENT side handles the rest automatically:
        * /sign → review the contract online
        * /accept → mark contract signed, redirect to payment
        * /pix/{id} or Stripe Checkout → pay
        * On payment confirmation (Stripe webhook for cards, manual
          /pix/confirm for Pix), :func:`lib.contract._kickoff_engagement`
          fires automatically: creates engagement + queues the kickoff
          handler, which sends the intake form email to the client.

    Body:
        lead_id (str, required) — UUID of the lead in supabase.leads
        practice (str, optional) — defaults to "cloud_finops"
        value_brl (int, optional) — overrides practice default ticket
        payment_method (str, optional) — 'auto' (default) | 'pix' |
            'stripe_br' | 'stripe_us'. 'auto' picks Pix for BRL when
            PIX_NUBANK_KEY is set, else Stripe BR.
        currency (str, optional) — 'BRL' (default) or 'USD'
        scope_overrides (dict, optional) — per-engagement scope tweaks
            (e.g. extra sessions, custom deliverables)

    Returns ``{ok, contract_id, lead_id, pdf_url, sign_url, payment_url,
    payment_method, status, email_sent, message_id?, intake_url?}``.

    The intake_url is included only after payment confirmation triggers
    ``_kickoff_engagement`` (won't be present in the initial response).
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    body = await request.json()
    lead_id = (body.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(400, "lead_id required")
    practice = (body.get("practice") or "cloud_finops").strip()
    if practice not in _PRACTICE_CONFIG:
        raise HTTPException(
            400, f"unknown practice {practice}"
        )
    cfg = _PRACTICE_CONFIG[practice]
    value_brl = int(body.get("value_brl") or cfg["value_brl"])
    payment_method = (body.get("payment_method") or "auto").strip().lower()
    currency = (body.get("currency") or "BRL").strip().upper()
    scope_overrides = body.get("scope_overrides") or {}

    # Lazy import: lib.contract has heavy module-load side effects (Stripe SDK,
    # Google Workspace SDK probing) so we keep it out of the top-level imports.
    try:
        from lib.contract import (  # noqa: WPS433
            generate_contract as _generate_contract,
            send_contract_email as _send_contract_email,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("operator: contract module import failed")
        raise HTTPException(500, f"contract module unavailable: {exc}")

    # 1) Generate the contract (PDF + sign URL + payment URL persisted)
    gen = await _generate_contract(
        lead_id=lead_id,
        practice=practice,
        value_brl=value_brl,
        scope_overrides=scope_overrides,
        payment_method=payment_method,
        currency=currency,
    )
    if not gen.get("ok"):
        return {
            "ok": False,
            "stage": "generate_contract",
            "error": gen.get("reason") or "unknown",
            "detail": gen,
        }
    contract_id = gen.get("contract_id")
    pdf_url = gen.get("pdf_url") or ""
    sign_url = gen.get("sign_url") or ""
    payment_url = gen.get("payment_url") or ""

    # 2) Patch the lead so the admin dashboard reflects "contract_sent"
    async with httpx.AsyncClient(timeout=15) as client:
        lead = await _supa_get(
            client, "leads", f"id=eq.{lead_id}&select=*"
        )
        if lead:
            qd = dict(lead.get("qualification_data") or {})
            qd["contract_sent_at"] = datetime.now(timezone.utc).isoformat()
            qd["active_contract_id"] = contract_id
            await _supa_patch(
                client,
                "leads",
                f"id=eq.{lead_id}",
                {
                    "qualification_data": qd,
                    "current_stage": "contract_sent",
                },
            )

    # 3) Send the contract email via Resend
    email_result = await _send_contract_email(contract_id)
    email_sent = bool(email_result.get("ok"))

    # 4) Slack alert the operator with the URLs (so Mila can re-send or
    # confirm Pix manually if needed)
    base_url = (
        os.environ.get("BASE_URL")
        or os.environ.get("CONTRACT_HOST")
        or "https://anuvia.com.br"
    ).rstrip("/")
    client_name = (lead or {}).get("name") or "?"
    client_email = (lead or {}).get("email") or "?"
    client_company = (lead or {}).get("company") or ""
    payment_label = gen.get("payment_method") or payment_method
    value_label = f"R$ {value_brl:,}".replace(",", ".") if currency == "BRL" else f"$ {value_brl:,}"
    await _slack_alert(
        ":scroll: *Contrato enviado* — "
        f"`{client_name}` ({client_email})"
        + (f" · *{client_company}*" if client_company else "")
        + f" · *{practice}* {value_label} · `{payment_label}`\n"
        f"• PDF: <{pdf_url}|abrir>\n"
        f"• Sign URL: <{sign_url}|enviar manualmente se precisar>\n"
        f"• Email enviado: {'sim' if email_sent else 'FALHOU — checar logs'}"
    )

    return {
        "ok": True,
        "contract_id": contract_id,
        "lead_id": lead_id,
        "pdf_url": pdf_url,
        "sign_url": sign_url,
        "payment_url": payment_url,
        "payment_method": payment_label,
        "currency": currency,
        "value_brl": value_brl,
        "status": gen.get("status"),
        "email_sent": email_sent,
        "message_id": email_result.get("message_id"),
        "note": (
            "Cliente recebeu email com PDF + sign link. "
            "Quando assinar + pagar, engagement starts automaticamente "
            "via lib.contract._kickoff_engagement."
        ),
    }


@router.get("/check_contract_status")
async def operator_check_contract_status(request: Request):
    """Poll the lifecycle state of a contract.

    Returns the contract's current status + any downstream engagement
    pointer so the operator can see at a glance:

        contract.status: pending | sent | signed | paid | refunded
        engagement_id: present once :func:`_kickoff_engagement` fired
        intake_submitted_at: present once the client submitted the intake
        current_phase: 0..4 (FinOps) — where the engagement is now

    Query: ``?contract_id=<UUID>&token=<HMAC>``.
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    contract_id = (request.query_params.get("contract_id") or "").strip()
    if not contract_id:
        raise HTTPException(400, "contract_id required")

    async with httpx.AsyncClient(timeout=15) as client:
        contract = await _supa_get(
            client,
            "contracts",
            f"id=eq.{contract_id}&select=id,status,signed_at,paid_at,"
            "lead_id,practice,value_brl,currency,payment_method,pdf_url,"
            "sign_url,payment_url",
        )
        if not contract:
            raise HTTPException(404, "contract not found")

        engagement = await _supa_get(
            client,
            "engagements",
            f"contract_id=eq.{contract_id}&select=id,status,current_phase,"
            "delivery_mode,intake_data,artifacts",
        )

    eng_summary = None
    if engagement:
        intake = engagement.get("intake_data") or {}
        artifacts = engagement.get("artifacts") or {}
        eng_summary = {
            "engagement_id": engagement.get("id"),
            "status": engagement.get("status"),
            "current_phase": engagement.get("current_phase"),
            "delivery_mode": engagement.get("delivery_mode"),
            "intake_submitted": bool(intake.get("intake_submitted_at")),
            "phase_2_findings_ready": bool(artifacts.get("phase_2_findings")),
            "phase_3_change_log_ready": bool(artifacts.get("phase_3_change_log_md")),
            "final_report_ready": bool(artifacts.get("final_report_url")),
        }

    return {
        "ok": True,
        "contract_id": contract_id,
        "contract_status": contract.get("status"),
        "signed_at": contract.get("signed_at"),
        "paid_at": contract.get("paid_at"),
        "payment_method": contract.get("payment_method"),
        "value_brl": contract.get("value_brl"),
        "currency": contract.get("currency"),
        "pdf_url": contract.get("pdf_url"),
        "sign_url": contract.get("sign_url"),
        "payment_url": contract.get("payment_url"),
        "engagement": eng_summary,
    }


@router.post("/convert_lead_to_engagement")
async def operator_convert_lead_to_engagement(request: Request):
    """Convert a qualified lead into a signed contract + kickoff engagement.

    Body:
        lead_id (str, required) — UUID of the lead in supabase.leads
        practice (str, optional) — defaults to "cloud_finops"
        delivery_mode (str, optional) — "whiteglove" (default) or "autonomous"
        total_value_brl (int, optional) — overrides practice default

    Returns ``{ok, lead_id, contract_id, engagement_id, intake_url, ...}``.
    The intake_url is the client-facing URL the kickoff email will already
    contain — surfaced here so the operator can verify or hand it off.
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    body = await request.json()
    lead_id = (body.get("lead_id") or "").strip()
    if not lead_id:
        raise HTTPException(400, "lead_id required")
    practice = (body.get("practice") or "cloud_finops").strip()
    if practice not in _PRACTICE_CONFIG:
        raise HTTPException(
            400, f"unknown practice {practice} (known: {list(_PRACTICE_CONFIG)})"
        )
    cfg = _PRACTICE_CONFIG[practice]
    delivery_mode = (body.get("delivery_mode") or "whiteglove").strip()
    if delivery_mode not in ("whiteglove", "autonomous"):
        raise HTTPException(
            400, f"delivery_mode must be 'whiteglove' or 'autonomous'"
        )
    total_value_brl = int(body.get("total_value_brl") or cfg["value_brl"])

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    past_iso = (now - timedelta(minutes=2)).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
        # 1) Load the lead
        lead = await _supa_get(
            client, "leads", f"id=eq.{lead_id}&select=*"
        )
        if not lead:
            raise HTTPException(404, f"lead {lead_id} not found")

        # 2) Idempotency: if this lead already has an active engagement,
        # return it instead of creating a duplicate.
        qd = dict(lead.get("qualification_data") or {})
        existing_engagement_id = qd.get("active_engagement_id")
        if existing_engagement_id:
            existing = await _supa_get(
                client,
                "engagements",
                f"id=eq.{existing_engagement_id}&select=id,status,delivery_mode",
            )
            if existing:
                return {
                    "ok": True,
                    "idempotent": True,
                    "lead_id": lead_id,
                    "engagement_id": existing_engagement_id,
                    "status": existing.get("status"),
                    "delivery_mode": existing.get("delivery_mode"),
                    "note": "lead already converted — returned existing engagement",
                }

        # 3) Create contract (signed state — operator only fires this
        # after deal close + payment confirmation outside the system).
        contract_row = {
            "lead_id": lead_id,
            "practice": practice,
            "value_brl": total_value_brl,
            "currency": "BRL",
            "payment_method": "pix",
            "status": "signed",
            "sent_at": past_iso,
            "signed_at": now_iso,
            "hmac_token": uuid.uuid4().hex,
        }
        try:
            contract = await _supa_insert(client, "contracts", contract_row)
        except RuntimeError as e:
            return {
                "ok": False,
                "stage": "create_contract",
                "error": str(e),
            }
        contract_id = contract["id"]

        # 4) Create engagement
        engagement_row = {
            "lead_id": lead_id,
            "contract_id": contract_id,
            "practice": practice,
            "contract_signed_at": now_iso,
            "current_phase": 0,
            "total_phases": cfg["total_phases"],
            "status": "kickoff",
            "intake_data": {},
            "artifacts": {},
            "total_value_brl": total_value_brl,
            "delivery_mode": delivery_mode,
        }
        try:
            engagement = await _supa_insert(
                client, "engagements", engagement_row
            )
        except RuntimeError as e:
            # Backward-compat: contracts.contract_id and engagements.delivery_mode
            # might not exist if migrations are pending. Try without them.
            retry_row = dict(engagement_row)
            if "delivery_mode" in str(e):
                retry_row.pop("delivery_mode", None)
            if "contract_id" in str(e):
                retry_row.pop("contract_id", None)
            try:
                engagement = await _supa_insert(
                    client, "engagements", retry_row
                )
            except RuntimeError as e2:
                return {
                    "ok": False,
                    "stage": "create_engagement",
                    "error": str(e2),
                }
        engagement_id = engagement["id"]

        # 5) Update lead with engagement pointer + queue kickoff handler
        qd["active_engagement_id"] = engagement_id
        qd["operator_converted_at"] = now_iso
        await _supa_patch(
            client,
            "leads",
            f"id=eq.{lead_id}",
            {
                "qualification_data": qd,
                "current_stage": "contract_signed",
                "next_action": cfg["kickoff_action"],
                "next_action_at": past_iso,
            },
        )
        lead = await _supa_get(
            client, "leads", f"id=eq.{lead_id}&select=*"
        ) or lead

        # 6) Fire kickoff handler — sends the kickoff email to the client
        kickoff_result = await _run_handler(cfg["kickoff_action"], lead)

        # 7) Fire phase 1 — moves engagement to "waiting for intake" state
        # and sends the intake form email (the kickoff email may have
        # already included the link; this is the formal phase-1 waiting
        # marker).
        await _supa_patch(
            client,
            "leads",
            f"id=eq.{lead_id}",
            {
                "next_action": cfg["phase_1_action"],
                "next_action_at": past_iso,
            },
        )
        lead = await _supa_get(
            client, "leads", f"id=eq.{lead_id}&select=*"
        ) or lead
        phase_1_result = await _run_handler(cfg["phase_1_action"], lead)

        # 8) Build the intake URL for the operator to verify / share
        base_url = (
            os.environ.get("BASE_URL")
            or os.environ.get("CONTRACT_HOST")
            or "https://anuvia.com.br"
        ).rstrip("/")
        intake_url = (
            f"{base_url}/api/delivery/{cfg['intake_slug']}/intake"
            f"?engagement_id={engagement_id}&token={_intake_token(engagement_id)}"
        )

        # 9) Slack-DM the operator with a clean summary
        client_name = (lead.get("name") or "?")
        client_email = (lead.get("email") or "?")
        client_company = (lead.get("company") or "")
        admin_dashboard_url = (
            f"{base_url}/admin/engagement/{engagement_id}"
        )
        await _slack_alert(
            ":handshake: *Engagement criado* — "
            f"`{client_name}` ({client_email})"
            + (f" · *{client_company}*" if client_company else "")
            + f" · *{practice}* R$ {total_value_brl:,}".replace(",", ".")
            + f" · `{delivery_mode}`\n"
            f"• Intake: <{intake_url}|preencher>\n"
            f"• Engagement dashboard: <{admin_dashboard_url}|abrir>"
        )

        return {
            "ok": True,
            "lead_id": lead_id,
            "contract_id": contract_id,
            "engagement_id": engagement_id,
            "delivery_mode": delivery_mode,
            "total_value_brl": total_value_brl,
            "intake_url": intake_url,
            "kickoff_result": {
                "ok": kickoff_result.get("ok", True),
                "next_action": kickoff_result.get("next_action"),
            },
            "phase_1_result": {
                "ok": phase_1_result.get("ok", True),
                "status": phase_1_result.get("status"),
            },
            "note": (
                "Cliente recebeu o email do intake form. Quando submeter, "
                "o orchestrator pega na próxima tick (10 min) e dispara "
                f"fase 2/3/4 com delivery_mode='{delivery_mode}'."
            ),
        }
