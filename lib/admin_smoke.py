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
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

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

    base_email = (body.get("email") or "").strip().lower()
    practice = (body.get("practice") or "cloud_finops").strip().lower()
    max_phase = int(body.get("max_phase", 4))
    skip_phases = bool(body.get("skip_phases", False))
    # White-glove mode (task #56): default for new engagements. When True,
    # the smoke creates the engagement in delivery_mode='whiteglove' AND
    # defaults skip_intake_submit=True so Mila can manually fill the
    # intake form via the URL returned in the response (real client UX).
    # Pass delivery_mode='autonomous' for the legacy fire-and-forget flow
    # that drives the daily E2E smoke test.
    delivery_mode = str(body.get("delivery_mode") or "whiteglove").strip().lower()
    if delivery_mode not in ("whiteglove", "autonomous"):
        delivery_mode = "whiteglove"
    # Default skip_intake_submit: True for whiteglove (Mila fills it as
    # a real client would), False for autonomous (smoke fills it itself).
    skip_intake_submit = bool(
        body.get(
            "skip_intake_submit",
            delivery_mode == "whiteglove",
        )
    )
    # Gmail-style + alias so repeated smokes don't collide on
    # (funnel_id, email) unique. Real inbox still receives.
    if "+smoke-" not in base_email and "@" in base_email:
        local, dom = base_email.split("@", 1)
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        email = f"{local}+smoke-{practice}-{suffix}@{dom}"
    else:
        email = base_email

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
            "track": "autonomous",  # NOT NULL — 'discovery' | 'autonomous'
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
            "delivery_mode": delivery_mode,
        }
        try:
            engagement = await _supa_insert(client, "engagements", engagement_row)
        except RuntimeError as e:
            # Migration 2026-05-17_delivery_mode.sql not applied yet?
            # Retry without the delivery_mode column for backward compat.
            if "delivery_mode" in str(e):
                engagement_row.pop("delivery_mode", None)
                engagement = await _supa_insert(client, "engagements", engagement_row)
                steps.append({
                    "step": "create_engagement_warn",
                    "warn": "delivery_mode column missing — apply migrations/2026-05-17_delivery_mode.sql",
                })
            else:
                raise
        engagement_id = engagement["id"]
        steps.append({
            "step": "create_engagement",
            "engagement_id": engagement_id,
            "delivery_mode": delivery_mode,
        })

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
                "delivery_mode": delivery_mode,
                "steps": steps,
            }

        # White-glove smoke path (default): fire phase 1 (data collection
        # wait state) ONLY, then bail with the intake URL so Mila can fill
        # the form manually as a real client would. The phase 1 handler is
        # idempotent — when Mila submits the intake (via delivery_routes
        # POST), it sets the intake_submitted_at marker AND patches
        # lead.next_action = phase_2, so the next orchestrator tick picks
        # up the engagement and walks the rest of the funnel autonomously.
        if skip_intake_submit:
            # Fire phase 1 once so the agent_history shows the "waiting for
            # intake" entry — gives Mila a clean operational trail.
            ph1_action = cfg["phase_actions"][0] if cfg["phase_actions"] else None
            if ph1_action:
                await _supa_patch(
                    client,
                    "leads",
                    f"id=eq.{lead_id}",
                    {"next_action": ph1_action, "next_action_at": past_iso},
                )
                lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead
                ph1_result = await _run_handler_direct(ph1_action, lead)
                steps.append({"step": "fire_phase_1_waiting", **ph1_result})

            # Compute the client-facing intake URL using the same HMAC
            # pattern as the delivery modules.
            form_slug = {
                "cloud_finops": "finops",
                "ai": "ai",
                "devops": "devops",
                "growth_salesops": "growth",
                "industry": "industry",
            }.get(practice, "finops")
            intake_token = ""
            if HMAC_SECRET:
                intake_token = hmac.new(
                    HMAC_SECRET.encode("utf-8"),
                    f"{engagement_id}:intake".encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
            base_url = (
                os.environ.get("BASE_URL")
                or os.environ.get("CONTRACT_HOST")
                or "https://anuvia.com.br"
            ).rstrip("/")
            intake_url = (
                f"{base_url}/api/delivery/{form_slug}/intake"
                f"?engagement_id={engagement_id}&token={intake_token}"
            )
            return {
                "ok": True,
                "mode": "whiteglove_intake_pending",
                "lead_id": lead_id,
                "contract_id": contract_id,
                "engagement_id": engagement_id,
                "delivery_mode": delivery_mode,
                "intake_url": intake_url,
                "note": (
                    "Engagement criado. Fase 1 está em waiting_for_intake. "
                    "Abra intake_url, preencha como um cliente real, submeta. "
                    "O tick do orchestrator vai pegar nos próximos 10 min e "
                    "rodar as fases 2/3/4 com delivery_mode='%s'." % delivery_mode
                ),
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
        "delivery_mode": delivery_mode,
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


@router.post("/smoke/fire_phase_sync_traced")
async def smoke_fire_phase_sync_traced(request: Request):
    """Run a handler SYNCHRONOUSLY with full exception trace. Use for debugging.

    Body: {engagement_id, phase_action}
    Returns timing per Claude call + any exception trace.
    """
    import time as _time
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    body = await request.json()
    engagement_id = body.get("engagement_id")
    phase_action = body.get("phase_action")

    timings = []
    t_start = _time.time()
    try:
        async with httpx.AsyncClient() as client:
            eng = await _supa_get(
                client, "engagements", f"id=eq.{engagement_id}&select=*"
            )
            if not eng:
                return {"ok": False, "error": "engagement not found"}
            lead = await _supa_get(client, "leads", f"id=eq.{eng['lead_id']}&select=*")
            if not lead:
                return {"ok": False, "error": "lead not found"}
            past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            await _supa_patch(
                client, "leads", f"id=eq.{lead['id']}",
                {"next_action": phase_action, "next_action_at": past_iso},
            )
            lead = await _supa_get(client, "leads", f"id=eq.{lead['id']}&select=*") or lead
        timings.append({"step": "setup", "elapsed_s": _time.time() - t_start})

        from lib.orchestrator import HANDLERS
        fn = HANDLERS.get(phase_action)
        if not fn:
            return {"ok": False, "error": f"handler {phase_action} not registered"}

        t_handler = _time.time()
        result = await fn(lead)
        timings.append({"step": "handler", "elapsed_s": _time.time() - t_handler})

        return {
            "ok": True,
            "total_elapsed_s": _time.time() - t_start,
            "timings": timings,
            "result": result,
        }
    except Exception as e:
        import traceback
        return {
            "ok": False,
            "total_elapsed_s": _time.time() - t_start,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc().splitlines()[-15:],
            "timings": timings,
        }


@router.get("/smoke/slack_test")
async def smoke_slack_test(request: Request):
    """Test EVERY Slack code path to isolate where the failure is.

    Returns:
        - direct_diag_post: orchestrator-style ping (already known to work)
        - sessions_slack_dm_text: _sessions._slack_post via slack_dm_text helper
        - sessions_full_dm: _sessions.slack_dm_materials_ready with mock data

    Each result shows status + ok flag + any error.
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    results = {}
    webhook = (
        os.environ.get("SLACK_ALERTS_WEBHOOK", "")
        or os.environ.get("SLACK_NEW_LEAD_WEBHOOK", "")
    )
    results["webhook_url_prefix"] = webhook[:50] + "..." if webhook else "(none)"

    # Test 1 — direct curl to webhook with same payload diag uses
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook, json={"text": "Anuvia slack_test #1: DIRECT POST (should arrive)"})
        results["direct_post"] = {"status": r.status_code, "body": r.text[:100], "ok": r.status_code == 200 and r.text.strip().lower() == "ok"}
    except Exception as e:
        results["direct_post"] = {"error": f"{type(e).__name__}: {e}"}

    # Test 2 — call _sessions.slack_dm_text helper
    try:
        from lib.delivery._sessions import slack_dm_text
        ok = await slack_dm_text("Anuvia slack_test #2: via _sessions.slack_dm_text (text only)")
        results["sessions_text"] = {"ok": ok}
    except Exception as e:
        results["sessions_text"] = {"error": f"{type(e).__name__}: {e}"}

    # Test 3 — call full materials_ready DM
    try:
        from lib.delivery._sessions import slack_dm_materials_ready
        ok = await slack_dm_materials_ready(
            engagement_id="00000000-0000-0000-0000-000000000000",
            phase=2,
            client_name="Slack Test Client",
            findings_summary="R$ 100k savings (testing only)",
            scheduled_at_br="Test slot",
            duration_min=60,
            meet_url="https://meet.google.com/test-test-test",
            materials=[("Test PDF", "https://example.com/test.pdf")],
            brief_snippet="Test brief snippet",
        )
        results["sessions_full_dm"] = {"ok": ok}
    except Exception as e:
        import traceback
        results["sessions_full_dm"] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-500:]}

    return results


@router.get("/smoke/token")
async def smoke_token():
    """Convenience endpoint to compute the admin token. NO AUTH on purpose so
    Mila can grab the token via curl. Token == HMAC(CONTRACT_HMAC_SECRET, 'admin_smoke').
    """
    if not HMAC_SECRET:
        raise HTTPException(500, "CONTRACT_HMAC_SECRET not configured")
    tok = hmac.new(HMAC_SECRET.encode("utf-8"), b"admin_smoke", hashlib.sha256).hexdigest()
    return {"token": tok}


# ---------------------------------------------------------------------------
# Intake preview — render a blank intake form as a client would see it
# ---------------------------------------------------------------------------

# Map from the practice slugs used by smoke / engagements (cloud_finops,
# ai, devops, growth_salesops, industry) to the slugs used by
# ``lib.delivery_routes._render_intake_form`` (finops, ai, devops, growth,
# industry). Mila pastes either form into the URL and we normalise.
_PRACTICE_TO_FORM_SLUG = {
    "cloud_finops": "finops",
    "finops": "finops",
    "ai": "ai",
    "devops": "devops",
    "growth_salesops": "growth",
    "growth": "growth",
    "industry": "industry",
}


@router.get("/intake_preview")
async def intake_preview(request: Request):
    """Render the blank intake form for Mila to preview as a client would see
    it. Admin-only (token == admin_smoke hash).

    Query params:
        practice : one of cloud_finops|ai|devops|growth_salesops|industry
        token    : admin_smoke HMAC

    Returns the same HTML produced by the real intake GET endpoint but with
    a placeholder engagement_id (``PREVIEW``) and the form action pointed
    at ``/dev/null`` so submitting the preview is a no-op.
    """
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    practice_raw = (request.query_params.get("practice") or "cloud_finops").strip().lower()
    form_slug = _PRACTICE_TO_FORM_SLUG.get(practice_raw)
    if not form_slug:
        raise HTTPException(
            400,
            f"unknown practice; choose from {list(_PRACTICE_TO_FORM_SLUG)}",
        )

    # Lazy import the renderer + intake schema so this module stays loadable
    # when delivery_routes has an import-time issue.
    try:
        from lib.delivery_routes import _render_intake_form  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.exception("intake_preview: delivery_routes not importable")
        raise HTTPException(500, f"delivery_routes unavailable: {exc}")

    # Render with placeholder engagement_id + a bogus token. The form
    # action URL will be /api/delivery/{slug}/intake?engagement_id=PREVIEW
    # &token=PREVIEW_NO_SUBMIT — which the live POST endpoint will reject
    # (invalid token), so submitting the preview is a no-op even if Mila
    # accidentally clicks the button.
    response = _render_intake_form(
        form_slug, "PREVIEW", "PREVIEW_NO_SUBMIT"
    )
    # Inject a visible banner so Mila knows this is preview mode.
    banner = (
        '<div style="background:#fef3c7;border:1px solid #f59e0b;'
        'padding:14px 18px;border-radius:6px;margin-bottom:18px;'
        'font-size:14px;color:#92400e;">'
        '<strong>PREVIEW MODE</strong> — Esta é a visualização do formulário '
        f'de intake da prática <code>{practice_raw}</code>. '
        'Submeter NÃO grava nada (token inválido por design).'
        '</div>'
    )
    body = response.body.decode("utf-8")
    # Inject the banner right after the opening <div class="card">.
    body = body.replace('<div class="card">', f'<div class="card">{banner}', 1)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(body, status_code=200)


async def _smoke_fire_phase_background(
    engagement_id: str, phase_action: str, submit_intake_first: bool, intake: dict
) -> None:
    """Background worker — runs the handler without blocking the HTTP response.

    Errors are swallowed (logged) so background tasks never raise.
    """
    try:
        async with httpx.AsyncClient() as client:
            eng = await _supa_get(
                client, "engagements", f"id=eq.{engagement_id}&select=*"
            )
            if not eng:
                log.warning("smoke_bg: engagement %s not found", engagement_id)
                return
            lead_id = eng["lead_id"]
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*")
            if not lead:
                log.warning("smoke_bg: lead %s not found", lead_id)
                return

            if submit_intake_first:
                practice = eng.get("practice")
                if not intake:
                    cfg = _PRACTICE_CONFIG.get(practice) or _PRACTICE_CONFIG.get("cloud_finops")
                    intake = cfg["intake_sample"]
                cur = eng.get("artifacts") or {}
                if not isinstance(cur, dict):
                    cur = {"_legacy": cur}
                cur["intake_submitted_at"] = datetime.now(timezone.utc).isoformat()
                cur["intake"] = intake
                await _supa_patch(
                    client, "engagements", f"id=eq.{engagement_id}",
                    {"intake_data": intake, "artifacts": cur},
                )

            past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            await _supa_patch(
                client, "leads", f"id=eq.{lead_id}",
                {"next_action": phase_action, "next_action_at": past_iso},
            )
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead

        result = await _run_handler_direct(phase_action, lead)
        log.info("smoke_bg: %s done ok=%s", phase_action, result.get("ok"))
    except Exception as exc:
        log.exception("smoke_bg: %s crashed: %s", phase_action, exc)


@router.post("/smoke/fire_phase")
async def smoke_fire_phase(request: Request, bg: BackgroundTasks):
    """Fire one handler against an existing engagement.

    Returns IMMEDIATELY — handler runs as a FastAPI BackgroundTask so curl
    never gets cancelled mid-flight. Poll the engagement debug endpoint to
    see progress.

    Body: {engagement_id, phase_action, submit_intake_first?: bool, intake?: dict, wait?: bool}
    Query: ?token=<hex>

    Pass ``wait: true`` to fall back to synchronous behavior (legacy mode).
    """
    try:
        token = request.query_params.get("token", "")
        if not _verify_admin_token(token):
            raise HTTPException(401, "bad admin token")
        body = await request.json()
        engagement_id = body.get("engagement_id")
        phase_action = body.get("phase_action")
        if not engagement_id or not phase_action:
            raise HTTPException(400, "engagement_id + phase_action required")

        submit_first = bool(body.get("submit_intake_first"))
        intake_override = body.get("intake") or {}

        # Default mode: dispatch to background, return immediately.
        if not body.get("wait"):
            bg.add_task(
                _smoke_fire_phase_background,
                engagement_id,
                phase_action,
                submit_first,
                intake_override,
            )
            return {
                "ok": True,
                "mode": "background",
                "phase_action": phase_action,
                "engagement_id": engagement_id,
                "note": "Poll /api/delivery/debug/engagement/{id} for progress",
            }

        # Legacy synchronous mode
        async with httpx.AsyncClient() as client:
            eng = await _supa_get(
                client, "engagements", f"id=eq.{engagement_id}&select=*"
            )
            if not eng:
                raise HTTPException(404, "engagement not found")
            lead_id = eng["lead_id"]
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*")
            if not lead:
                raise HTTPException(404, "lead not found")

            # Optional: submit fake intake before firing the phase
            if body.get("submit_intake_first"):
                practice = eng.get("practice")
                intake = body.get("intake") or {}
                # If no intake supplied, use the practice default
                if not intake:
                    cfg = _PRACTICE_CONFIG.get(practice) or _PRACTICE_CONFIG.get("cloud_finops")
                    intake = cfg["intake_sample"]
                cur_artifacts = eng.get("artifacts") or {}
                if not isinstance(cur_artifacts, dict):
                    cur_artifacts = {"_legacy": cur_artifacts}
                cur_artifacts["intake_submitted_at"] = datetime.now(timezone.utc).isoformat()
                cur_artifacts["intake"] = intake
                await _supa_patch(
                    client,
                    "engagements",
                    f"id=eq.{engagement_id}",
                    {"intake_data": intake, "artifacts": cur_artifacts},
                )

            # Force lead due NOW for THIS action
            past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            await _supa_patch(
                client,
                "leads",
                f"id=eq.{lead_id}",
                {"next_action": phase_action, "next_action_at": past_iso},
            )
            lead = await _supa_get(client, "leads", f"id=eq.{lead_id}&select=*") or lead

        result = await _run_handler_direct(phase_action, lead)
        # Reload engagement for current state
        async with httpx.AsyncClient() as client:
            eng2 = await _supa_get(
                client, "engagements", f"id=eq.{engagement_id}&select=*"
            ) or {}
        art_keys = list((eng2.get("artifacts") or {}).keys()) if isinstance(
            eng2.get("artifacts"), dict
        ) else []
        return {
            "ok": True,
            "phase_action": phase_action,
            "result": result,
            "engagement_status": eng2.get("status"),
            "current_phase": eng2.get("current_phase"),
            "artifact_keys": art_keys,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc().splitlines()[-10:],
        }
