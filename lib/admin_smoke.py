"""
Admin smoke endpoint — full E2E simulation of an engagement.

Single HTTP call exercises:
  1. lead creation (fake email, configured practice)
  2. contract creation (signed status, value from PRACTICE_CONFIG)
  3. engagement creation (kickoff status, total_phases per practice)
  4. fires every delivery handler in sequence (bypassing orchestrator scheduling)
  5. simulates client intake form submission
  6. returns artifacts summary

HMAC-protected via CONTRACT_HMAC_SECRET (same as the rest of contract.py).

Usage:
    curl -X POST "https://anuvia.com.br/api/_admin/smoke/engagement?token=$T" \
         -H "Content-Type: application/json" \
         -d '{"email":"milavernazza@gmail.com","practice":"cloud_finops","max_phase":4}'

Where $T = HMAC-SHA256("admin_smoke", CONTRACT_HMAC_SECRET) hex digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

# Reuse sessions.py defaults (same SUPA_URL fallback used by track_b + contract).
from lib.sessions import SUPA_HEADERS, SUPA_URL, SUPA_KEY

log = logging.getLogger("anuvia-admin-smoke")

router = APIRouter(prefix="/api/_admin", tags=["admin-smoke"])

# Reuse the contract HMAC secret as the admin gate.
HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)


_PRACTICE_CONFIG = {
    "cloud_finops": {
        "funnel_id": "BR_FINOPS",
        "value_brl": 52000,
        "total_phases": 4,
        "kickoff_action": "engagement_kickoff_cloud_finops",
        "phase_actions": [
            "finops_phase_1_data_collection",
            "finops_phase_2_analysis",
            "finops_phase_3_quickwins",
            "finops_phase_4_roadmap",
        ],
        "intake_sample": {
            "executive_sponsor_name": "Mila Vernazza",
            "executive_sponsor_email": "milavernazza@gmail.com",
            "aws_spend_brl_monthly": 95000,
            "aws_account_count": 4,
            "primary_services": ["EC2", "RDS", "S3", "CloudFront", "NAT Gateway"],
            "tagging_strategy": "partial — environment + owner tags on most resources",
            "biggest_concerns": [
                "RDS sobre-provisionado",
                "NAT Gateway custo crescendo",
                "snapshots órfãos acumulados",
            ],
        },
    },
    "ai": {
        "funnel_id": "BR_AI",
        "value_brl": 32000,
        "total_phases": 3,
        "kickoff_action": "engagement_kickoff_ai",
        "phase_actions": [
            "ai_phase_1_discovery",
            "ai_phase_2_scoring",
            "ai_phase_3_roadmap",
        ],
        "intake_sample": {
            "executive_sponsor_name": "Mila Vernazza",
            "executive_sponsor_email": "milavernazza@gmail.com",
            "stakeholders": [
                {"area": "marketing", "head": "Ana Costa"},
                {"area": "atendimento", "head": "João Lima"},
                {"area": "operações", "head": "Pedro Santos"},
                {"area": "antifraude", "head": "Lucia Rocha"},
                {"area": "tech", "head": "Mila Vernazza"},
            ],
            "past_pocs": [
                {"name": "chatbot atendimento", "status": "stalled"},
                {"name": "recomendação produto", "status": "live"},
            ],
            "data_assets": ["CRM HubSpot histórico 3 anos", "transaction logs Postgres"],
            "compliance_constraints": ["LGPD"],
            "annual_ai_budget_brl": 280000,
            "internal_ai_capability": "1-2 engineers",
        },
    },
    "devops": {
        "funnel_id": "BR_DEVOPS",
        "value_brl": 42000,
        "total_phases": 4,
        "kickoff_action": "engagement_kickoff_devops",
        "phase_actions": [
            "devops_phase_1_baseline",
            "devops_phase_2_maturity",
            "devops_phase_3_roadmap",
            "devops_phase_4_handoff",
        ],
        "intake_sample": {
            "executive_sponsor_name": "Mila Vernazza",
            "executive_sponsor_email": "milavernazza@gmail.com",
            "engineering_team_size": 38,
            "squads_count": 6,
            "production_services_count": 14,
            "ci_tool": "GitHub Actions",
            "incident_tracker": "Linear",
            "oncall_tool": "Opsgenie",
            "self_reported_deploy_frequency": "weekly",
            "self_reported_lead_time_days": 5,
            "self_reported_mttr_hours": 11,
            "self_reported_cfr_pct": 40,
            "feature_flag_tool": None,
            "observability_stack": "CloudWatch + Datadog APM (parcial)",
            "post_mortem_culture": "some",
        },
    },
    "growth_salesops": {
        "funnel_id": "BR_GROWTH_OPS",
        "value_brl": 20000,
        "total_phases": 2,
        "kickoff_action": "engagement_kickoff_growth_salesops",
        "phase_actions": [
            "growth_phase_1_funnel",
            "growth_phase_2_automation",
        ],
        "intake_sample": {
            "executive_sponsor_name": "Mila Vernazza",
            "executive_sponsor_email": "milavernazza@gmail.com",
            "crm_in_use": "HubSpot",
            "sales_team_composition": "founder + 2 SDRs + 1 AE",
            "sales_cycle_median_days": 75,
            "avg_ticket_brl": 8500,
            "lead_sources": ["form fill", "WhatsApp inbound", "referral"],
            "monthly_volume": {
                "leads": 140,
                "qualified": 38,
                "closed": 6,
            },
            "response_time_sla": "<30 min goal, ~3h actual",
            "existing_automation": "HubSpot sequences (3 active)",
            "top_pain_points": ["WhatsApp leads esfriando", "follow-up inconsistente"],
        },
    },
    "industry": {
        "funnel_id": "BR_INDUSTRY",
        "value_brl": 55000,
        "total_phases": 3,
        "kickoff_action": "engagement_kickoff_industry",
        "phase_actions": [
            "industry_phase_1_discovery",
            "industry_phase_2_pov",
            "industry_phase_3_validation",
        ],
        "intake_sample": {
            "executive_sponsor_name": "Mila Vernazza",
            "executive_sponsor_email": "milavernazza@gmail.com",
            "vertical": "manufacturing",
            "company_revenue_brl": 80000000,
            "main_pain": "OEE inconsistency + maintenance unplanned downtime",
            "compliance_named": ["ISO 27001", "ISO 9001"],
            "sensor_data_centralized": True,
            "mes_integration_existing": True,
            "ai_maturity": "exploring",
        },
    },
}


def _verify_admin_token(token: str) -> bool:
    if not HMAC_SECRET or not token:
        return False
    expected = hmac.new(
        HMAC_SECRET.encode("utf-8"),
        b"admin_smoke",
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


async def _supa_insert(client: httpx.AsyncClient, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    r = await client.post(f"{SUPA_URL}/{table}", json=row, headers=SUPA_HEADERS, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"supa insert {table}: {r.status_code} {r.text}")
    rows = r.json()
    return rows[0] if isinstance(rows, list) else rows


async def _supa_patch(
    client: httpx.AsyncClient, table: str, where: str, patch: Dict[str, Any]
) -> Dict[str, Any]:
    r = await client.patch(
        f"{SUPA_URL}/{table}?{where}",
        json=patch,
        headers=SUPA_HEADERS,
        timeout=20,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"supa patch {table}: {r.status_code} {r.text}")
    rows = r.json() if r.text else []
    return rows[0] if isinstance(rows, list) and rows else {}


async def _supa_get(client: httpx.AsyncClient, table: str, where: str) -> Optional[Dict[str, Any]]:
    r = await client.get(
        f"{SUPA_URL}/{table}?{where}",
        headers=SUPA_HEADERS,
        timeout=20,
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


async def _run_handler_direct(action: str, lead: Dict[str, Any]) -> Dict[str, Any]:
    """Run a registered handler directly (bypass scheduler)."""
    from lib.orchestrator import HANDLERS

    fn = HANDLERS.get(action)
    if not fn:
        return {"action": action, "ok": False, "error": "handler not registered"}
    try:
        result = await fn(lead)
        return {"action": action, "ok": True, "result": result}
    except Exception as e:
        log.exception("smoke: handler %s raised", action)
        return {"action": action, "ok": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/smoke/engagement")
async def smoke_engagement(request: Request):
    """Run a full E2E delivery simulation. Errors are surfaced in JSON, not 500."""
    try:
        return await _smoke_engagement_impl(request)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc().splitlines()[-10:],
        }


async def _smoke_engagement_impl(request: Request):
    if not SUPA_URL or not SUPA_KEY:
        raise HTTPException(500, "SUPABASE_URL / SUPABASE_KEY not configured")

    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json body")

    email = (body.get("email") or "").strip().lower()
    practice = (body.get("practice") or "cloud_finops").strip().lower()
    max_phase = int(body.get("max_phase", 4))
    skip_phases = bool(body.get("skip_phases", False))

    if practice not in _PRACTICE_CONFIG:
        raise HTTPException(400, f"unknown practice; choose from {list(_PRACTICE_CONFIG)}")

    cfg = _PRACTICE_CONFIG[practice]
    steps = []
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=2)
    past_iso = past.isoformat()
    now_iso = now.isoformat()

    async with httpx.AsyncClient() as client:
        # 1) Create lead row. Schema: leads has `email`, `name`, `company`,
        # `funnel_id`, `qualification_data` (jsonb), `lifecycle_status`, plus
        # the append-only jsonb columns (signals/artifacts/agent_history) which
        # we don't seed at insert time.
        lead_row = {
            "email": email,
            "name": "Mila Vernazza (smoke)",
            "company": f"Smoke {practice} {now.strftime('%H%M%S')}",
            "funnel_id": cfg["funnel_id"],
            "market": "BR",  # NOT NULL constraint
            "language": "pt",
            "qualification_data": {
                "smoke_test": True,
                "smoke_practice": practice,
                "smoke_started_at": now_iso,
            },
            "lifecycle_status": "proposal_signed",
            "next_action": None,
            "next_action_at": None,
        }
        try:
            lead = await _supa_insert(client, "leads", lead_row)
        except RuntimeError as e:
            # Schema mismatch likely. Surface concrete column hint.
            return {"ok": False, "stage": "create_lead", "error": str(e), "row_keys": list(lead_row.keys())}
        lead_id = lead["id"]
        steps.append({"step": "create_lead", "lead_id": lead_id})

        # 2) Create contract row in signed state
        contract_row = {
            "lead_id": lead_id,
            "practice": practice,
            "value_brl": cfg["value_brl"],
            "currency": "BRL",
            "payment_method": "pix",
            "status": "signed",
            "sent_at": past_iso,
            "signed_at": now_iso,
            "hmac_token": uuid.uuid4().hex,
        }
        contract = await _supa_insert(client, "contracts", contract_row)
        contract_id = contract["id"]
        steps.append({"step": "create_contract_signed", "contract_id": contract_id})

        # 3) Create engagement row in kickoff state
        engagement_row = {
            "lead_id": lead_id,
            "practice": practice,
            "contract_signed_at": now_iso,
            "current_phase": 0,
            "total_phases": cfg["total_phases"],
            "status": "kickoff",
            "intake_data": {},
            "artifacts": {},
            "total_value_brl": cfg["value_brl"],
        }
        engagement = await _supa_insert(client, "engagements", engagement_row)
        engagement_id = engagement["id"]
        steps.append({"step": "create_engagement", "engagement_id": engagement_id})

        # 4) Tie engagement back to lead for delivery agents' _resolve_engagement_id
        qd = dict(lead.get("qualification_data") or {})
        qd["active_engagement_id"] = engagement_id
        await _supa_patch(
            client,
            "leads",
            f"id=eq.{lead_id}",
            {
                "qualification_data": qd,
                "next_action": cfg["kickoff_action"],
                "next_action_at": past_iso,
            },
        )
        lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead
        steps.append({"step": "set_kickoff_next_action", "action": cfg["kickoff_action"]})

        # 5) Fire kickoff handler directly
        kickoff_result = await _run_handler_direct(cfg["kickoff_action"], lead)
        steps.append({"step": "fire_kickoff", **kickoff_result})

        if skip_phases:
            return {
                "ok": True,
                "mode": "kickoff_only",
                "lead_id": lead_id,
                "contract_id": contract_id,
                "engagement_id": engagement_id,
                "steps": steps,
            }

        # 6) Submit fake intake (PATCH intake_data on engagement)
        intake = cfg["intake_sample"]
        # Phase 1 handler typically waits for intake submitted flag. We add both
        # styles of marker so each agent's checker sees what it expects.
        intake_marker = {
            "intake_submitted_at": now_iso,
            "intake": intake,
        }
        eng = await _supa_get(
            client, "engagements", f"id=eq.{engagement_id}&select=*"
        ) or engagement
        cur_artifacts = eng.get("artifacts") or {}
        if not isinstance(cur_artifacts, dict):
            cur_artifacts = {"_legacy": cur_artifacts}
        cur_artifacts.update(intake_marker)
        await _supa_patch(
            client,
            "engagements",
            f"id=eq.{engagement_id}",
            {
                "intake_data": intake,
                "artifacts": cur_artifacts,
            },
        )
        steps.append({"step": "submit_intake", "intake_keys": list(intake.keys())})

        # 7) Walk through phases
        for i, phase_action in enumerate(cfg["phase_actions"], start=1):
            if i > max_phase:
                steps.append({"step": f"stop_at_phase_{max_phase}"})
                break

            # Reload lead to get the latest next_action set by the previous handler
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead
            # Force lead to be due now for THIS action (handlers expect to dispatch
            # on lead.next_action).
            await _supa_patch(
                client,
                "leads",
                f"id=eq.{lead_id}",
                {"next_action": phase_action, "next_action_at": past_iso},
            )
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead

            phase_result = await _run_handler_direct(phase_action, lead)
            steps.append({"step": f"fire_phase_{i}", **phase_result})

            # Small breather to let any async storage uploads settle.
            await asyncio.sleep(0.5)

        # 8) Final engagement state
        eng_final = await _supa_get(
            client, "engagements", f"id=eq.{engagement_id}&select=*"
        ) or {}
        art_keys = list((eng_final.get("artifacts") or {}).keys()) if isinstance(
            eng_final.get("artifacts"), dict
        ) else []

    return {
        "ok": True,
        "lead_id": lead_id,
        "contract_id": contract_id,
        "engagement_id": engagement_id,
        "practice": practice,
        "max_phase_requested": max_phase,
        "steps": steps,
        "engagement_final_status": eng_final.get("status"),
        "engagement_current_phase": eng_final.get("current_phase"),
        "artifact_keys": art_keys,
    }


@router.post("/smoke/cleanup")
async def smoke_cleanup(request: Request):
    """Delete all smoke test rows for a given email.

    Body: {email}
    Query: ?token=<hex>
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json body")

    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")

    from urllib.parse import quote

    email_enc = quote(email, safe="@.")
    deleted = {"leads": 0, "contracts": 0, "engagements": 0}

    async with httpx.AsyncClient() as client:
        # Find lead IDs
        r = await client.get(
            f"{SUPA_URL}/leads?email=eq.{email_enc}&select=id&qualification_data->>smoke_test=eq.true",
            headers=SUPA_HEADERS,
            timeout=20,
        )
        lead_ids = [row["id"] for row in (r.json() if r.status_code == 200 else [])]
        for lid in lead_ids:
            # Delete contracts + engagements first to keep FK happy
            r1 = await client.delete(
                f"{SUPA_URL}/contracts?lead_id=eq.{lid}",
                headers=SUPA_HEADERS,
                timeout=20,
            )
            if r1.status_code in (200, 204):
                deleted["contracts"] += 1
            r2 = await client.delete(
                f"{SUPA_URL}/engagements?lead_id=eq.{lid}",
                headers=SUPA_HEADERS,
                timeout=20,
            )
            if r2.status_code in (200, 204):
                deleted["engagements"] += 1
            r3 = await client.delete(
                f"{SUPA_URL}/leads?id=eq.{lid}",
                headers=SUPA_HEADERS,
                timeout=20,
            )
            if r3.status_code in (200, 204):
                deleted["leads"] += 1

    return {"ok": True, "email": email, "deleted_lead_ids": lead_ids, "counts": deleted}


@router.get("/smoke/token")
async def smoke_token():
    """Convenience endpoint to compute the admin token. NO AUTH on purpose so
    Mila can grab the token via curl. Token == HMAC(CONTRACT_HMAC_SECRET, 'admin_smoke').
    """
    if not HMAC_SECRET:
        raise HTTPException(500, "CONTRACT_HMAC_SECRET not configured")
    tok = hmac.new(HMAC_SECRET.encode("utf-8"), b"admin_smoke", hashlib.sha256).hexdigest()
    return {"token": tok}
