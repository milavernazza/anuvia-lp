#!/usr/bin/env python3
"""
End-to-end smoke test for the Anuvia autonomous funnel.

Runs against a deployed instance (default https://anuvia.com.br). Exercises:

  1. /api/<diag>/analyze     — synthetic anonymous diagnostic
  2. /api/<diag>/contact     — upgrade with PII (smoke email)
  3. Supabase REST           — verify the lead row exists
  4a. discovery track:        /api/slots + /api/contact-book + re-verify
  4b. autonomous track:       patch funnel + qualification + /api/orchestrator/tick

Always attempts cleanup at the end (DELETE the smoke lead via Supabase REST)
unless --keep is set. Posts a Slack alert on failure if SLACK_ALERTS_WEBHOOK
(or SLACK_NEW_LEAD_WEBHOOK) is set.

Usage:
    python scripts/smoke_e2e.py
    python scripts/smoke_e2e.py --track autonomous --practice growth --locale br
    python scripts/smoke_e2e.py --base-url https://staging.anuvia.com.br --keep --verbose

Designed to be standalone — does NOT import app.py or lib.*.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


# ---------------------------------------------------------------------------
# Configuration: practice → diagnostic endpoint + synthetic payload shape
# ---------------------------------------------------------------------------

# Maps the CLI --practice flag onto the actual deployed /api/<slug>/analyze
# endpoints and the form fields each one requires (read from app.py).
PRACTICE_CONFIG: dict[str, dict[str, Any]] = {
    "cloud": {
        "diag_slug": "finops-audit",
        "payload_br": {
            "role": "cto",
            "aws_spend": "10k_30k",
            "main_pain": "no_visibility",
            "aws_tenure": "1_3y",
            "ri_coverage": "low",
            "cost_dominant": "ec2",
            "context": "Smoke test — pré-análise FinOps automatizada.",
        },
        "payload_us": {
            "role": "cto",
            "aws_spend": "10k_30k",
            "main_pain": "no_visibility",
            "aws_tenure": "1_3y",
            "ri_coverage": "low",
            "cost_dominant": "ec2",
            "context": "Smoke test — automated FinOps pre-analysis.",
        },
    },
    "engineering": {
        "diag_slug": "devops-maturity",
        "payload_br": {
            "role": "head_eng",
            "team_size": "11_30",
            "deploy_freq": "weekly",
            "main_pain": "slow_releases",
            "stack": "aws_k8s",
            "mttr": "hours",
            "context": "Smoke test — diagnóstico DevOps automatizado.",
        },
        "payload_us": {
            "role": "head_eng",
            "team_size": "11_30",
            "deploy_freq": "weekly",
            "main_pain": "slow_releases",
            "stack": "aws_k8s",
            "mttr": "hours",
            "context": "Smoke test — automated DevOps diagnostic.",
        },
    },
    "ai": {
        "diag_slug": "ai-readiness",
        "payload_br": {
            "role": "cto",
            "ai_stage": "pilots",
            "main_pain": "no_strategy",
            "revenue_tier": "10m_50m",
            "data_readiness": "partial",
            "build_vs_buy": "mixed",
            "context": "Smoke test — diagnóstico AI readiness automatizado.",
        },
        "payload_us": {
            "role": "cto",
            "ai_stage": "pilots",
            "main_pain": "no_strategy",
            "revenue_tier": "10m_50m",
            "data_readiness": "partial",
            "build_vs_buy": "mixed",
            "context": "Smoke test — automated AI readiness diagnostic.",
        },
    },
    "growth": {
        "diag_slug": "growth-sales-ops",
        "payload_br": {
            "role": "head_growth",
            "team_size": "5_15",
            "ticket_size": "10k_50k",
            "main_pain": "low_conversion",
            "crm": "hubspot",
            "sales_cycle": "30_90d",
            "context": "Smoke test — diagnóstico Growth/SalesOps automatizado.",
        },
        "payload_us": {
            "role": "head_growth",
            "team_size": "5_15",
            "ticket_size": "10k_50k",
            "main_pain": "low_conversion",
            "crm": "hubspot",
            "sales_cycle": "30_90d",
            "context": "Smoke test — automated Growth/SalesOps diagnostic.",
        },
    },
    "industry": {
        "diag_slug": "industry-assessment",
        "payload_br": {
            "role": "cto",
            "vertical": "saas",
            "company_size": "50_200",
            "ai_maturity": "early",
            "main_pain": "no_strategy",
            "compliance": "lgpd",
            "context": "Smoke test — assessment de indústria automatizado.",
        },
        "payload_us": {
            "role": "cto",
            "vertical": "saas",
            "company_size": "50_200",
            "ai_maturity": "early",
            "main_pain": "no_strategy",
            "compliance": "soc2",
            "context": "Smoke test — automated industry assessment.",
        },
    },
}


# ---------------------------------------------------------------------------
# Step tracking
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    name: str
    duration_ms: int = 0
    ok: bool = False
    error: Optional[str] = None
    detail: str = ""


@dataclass
class SmokeContext:
    base_url: str
    track: str
    practice: str
    locale: str
    verbose: bool
    keep: bool
    timestamp: int
    smoke_email: str
    client: httpx.AsyncClient
    supa_url: str
    supa_headers: dict
    orchestrator_secret: str
    results: list[StepResult] = field(default_factory=list)
    lead_id: Optional[str] = None  # set after /analyze
    lead_row: Optional[dict] = None  # latest Supabase row


def _log(ctx: SmokeContext, msg: str) -> None:
    if ctx.verbose:
        print(f"  {msg}", flush=True)


@contextlib.asynccontextmanager
async def step(ctx: SmokeContext, name: str):
    """Context manager: logs ENTER/OK/FAIL with timing. Raises propagate."""
    print(f"[ENTER] {name}", flush=True)
    started = time.monotonic()
    result = StepResult(name=name)
    ctx.results.append(result)
    try:
        yield result
        result.ok = True
        result.duration_ms = int((time.monotonic() - started) * 1000)
        print(f"[ OK  ] {name}  ({result.duration_ms} ms){' — ' + result.detail if result.detail else ''}", flush=True)
    except Exception as e:
        result.ok = False
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.error = f"{type(e).__name__}: {e}"
        print(f"[FAIL ] {name}  ({result.duration_ms} ms) — {result.error}", flush=True)
        if ctx.verbose:
            traceback.print_exc()
        raise


# ---------------------------------------------------------------------------
# Supabase helpers (direct REST, no app.py import)
# ---------------------------------------------------------------------------


async def supa_get_lead_by_email(ctx: SmokeContext, email: str) -> list[dict]:
    url = f"{ctx.supa_url}/leads"
    # email contains '+' which is meaningful in URLs; use httpx params for proper encoding
    params = {"email": f"eq.{email}", "select": "*"}
    r = await ctx.client.get(url, headers=ctx.supa_headers, params=params, timeout=30.0)
    r.raise_for_status()
    return r.json()


async def supa_get_lead_by_id(ctx: SmokeContext, lead_id: str) -> Optional[dict]:
    url = f"{ctx.supa_url}/leads"
    params = {"id": f"eq.{lead_id}", "select": "*"}
    r = await ctx.client.get(url, headers=ctx.supa_headers, params=params, timeout=30.0)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


async def supa_patch_lead(ctx: SmokeContext, lead_id: str, fields: dict) -> dict:
    url = f"{ctx.supa_url}/leads"
    params = {"id": f"eq.{lead_id}"}
    r = await ctx.client.patch(
        url, headers=ctx.supa_headers, params=params, json=fields, timeout=30.0
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {}


async def supa_delete_leads_by_email_pattern(ctx: SmokeContext, pattern: str) -> int:
    """Deletes leads whose email matches the LIKE pattern (e.g., smoke+%@anuvia.test).
    Returns number of deleted rows (best-effort)."""
    url = f"{ctx.supa_url}/leads"
    params = {"email": f"like.{pattern}"}
    r = await ctx.client.delete(url, headers=ctx.supa_headers, params=params, timeout=30.0)
    if r.status_code in (200, 204):
        try:
            rows = r.json()
            return len(rows) if isinstance(rows, list) else 0
        except Exception:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Slack alerting (best-effort)
# ---------------------------------------------------------------------------


async def slack_alert(ctx: SmokeContext, text: str) -> None:
    webhook = (
        os.environ.get("SLACK_ALERTS_WEBHOOK")
        or os.environ.get("SLACK_NEW_LEAD_WEBHOOK")
    )
    if not webhook:
        print(f"[slack-alert (no webhook configured)] {text}", flush=True)
        return
    try:
        await ctx.client.post(webhook, json={"text": text}, timeout=10.0)
    except Exception as e:
        print(f"[slack-alert send failed] {e}", flush=True)


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


async def step_analyze(ctx: SmokeContext) -> None:
    cfg = PRACTICE_CONFIG[ctx.practice]
    diag_slug = cfg["diag_slug"]
    payload = cfg["payload_us"] if ctx.locale == "us" else cfg["payload_br"]
    url = f"{ctx.base_url}/api/{diag_slug}/analyze"

    async with step(ctx, f"POST /api/{diag_slug}/analyze") as r:
        _log(ctx, f"-> {url}")
        # Set Accept-Language so get_locale picks the right market
        headers = {"Accept-Language": "en-US,en;q=0.9" if ctx.locale == "us" else "pt-BR,pt;q=0.9"}
        resp = await ctx.client.post(url, json=payload, headers=headers, timeout=30.0)
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:300]}"
        # Response is JSON with html + lead_id
        try:
            body = resp.json()
        except Exception:
            raise AssertionError(f"expected JSON body, got: {resp.text[:300]}")
        html = body.get("html") or ""
        assert "<" in html, f"response html does not look like HTML: {html[:200]!r}"
        anon_lead_id = body.get("lead_id")
        assert anon_lead_id, f"no lead_id returned: {body}"
        ctx.lead_id = anon_lead_id
        r.detail = f"anon lead_id={anon_lead_id[:8]}…"


async def step_contact(ctx: SmokeContext) -> None:
    cfg = PRACTICE_CONFIG[ctx.practice]
    diag_slug = cfg["diag_slug"]
    url = f"{ctx.base_url}/api/{diag_slug}/contact"
    payload = {
        "lead_id": ctx.lead_id,
        "name": "Smoke Test",
        "email": ctx.smoke_email,
        # BR whatsapp pattern that normalize_phone() will accept
        "whatsapp": "+5511999990000",
        "company": "Anuvia Smoke Tests",
    }

    async with step(ctx, f"POST /api/{diag_slug}/contact") as r:
        _log(ctx, f"-> {url}  email={ctx.smoke_email}")
        resp = await ctx.client.post(url, json=payload, timeout=30.0)
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:300]}"
        # Endpoint returns JSON {ok, lead_upgraded, email_sent}.
        # Try to find lead UUID — fallback to regex if structure differs.
        lead_id: Optional[str] = ctx.lead_id  # carried from analyze
        try:
            body = resp.json()
            # If the response surfaces a lead_id explicitly, prefer it
            if isinstance(body, dict):
                cand = body.get("lead_id") or body.get("id")
                if cand:
                    lead_id = cand
        except Exception:
            # Page response — try to find a UUID
            m = re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                resp.text, re.IGNORECASE,
            )
            if m:
                lead_id = m.group(0)
        assert lead_id, "could not determine lead_id after /contact"
        ctx.lead_id = lead_id
        r.detail = f"lead_id={lead_id[:8]}…"


async def step_verify_supabase(ctx: SmokeContext) -> None:
    async with step(ctx, "Supabase: fetch lead by email") as r:
        # Brief wait for write-after-read consistency (Supabase REST is strong but
        # the contact handler does fire-and-mostly-forget writes).
        rows = await supa_get_lead_by_email(ctx, ctx.smoke_email)
        # Retry once if no row yet (rare but harmless)
        if len(rows) == 0:
            await asyncio.sleep(1.0)
            rows = await supa_get_lead_by_email(ctx, ctx.smoke_email)
        assert len(rows) == 1, f"expected exactly 1 lead with email={ctx.smoke_email}, got {len(rows)}"
        ctx.lead_row = rows[0]
        # If anonymous flow produced a different id, sync it
        ctx.lead_id = rows[0].get("id") or ctx.lead_id
        r.detail = f"id={ctx.lead_id[:8]}… stage={rows[0].get('current_stage')}"


async def step_discovery_slots_and_book(ctx: SmokeContext) -> None:
    async with step(ctx, "GET /api/slots?days=14") as r:
        url = f"{ctx.base_url}/api/slots"
        resp = await ctx.client.get(url, params={"days": 14}, timeout=30.0)
        assert resp.status_code == 200, f"slots non-200: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        days = data.get("days") or data.get("slots") or []
        # Find the first day with at least one slot
        first_day = None
        first_time = None
        for d in days:
            slots = d.get("slots") or []
            if slots:
                first_day = d.get("date")
                first_time = slots[0]
                break
        assert first_day and first_time, f"no available slots in response: {json.dumps(data)[:300]}"
        ctx._first_day = first_day  # type: ignore[attr-defined]
        ctx._first_time = first_time  # type: ignore[attr-defined]
        r.detail = f"first slot {first_day} {first_time}"

    async with step(ctx, "POST /api/contact-book") as r:
        url = f"{ctx.base_url}/api/contact-book"
        # NOTE: deployed /api/contact-book creates its own lead from the
        # form. The spec mentions start_iso/end_iso, but the actual endpoint
        # expects `date` + `time` (HH:MM) — we use those. This will create a
        # SECOND smoke lead with the same email pattern; cleanup removes
        # both via the LIKE filter.
        payload = {
            "name": "Smoke Test",
            "email": ctx.smoke_email,
            "whatsapp": "+5511999990000",
            "company": "Anuvia Smoke Tests",
            "context": "Automated smoke test booking",
            "date": ctx._first_day,  # type: ignore[attr-defined]
            "time": ctx._first_time,  # type: ignore[attr-defined]
            "source": "smoke_e2e",
            "practice": ctx.practice,
        }
        resp = await ctx.client.post(url, json=payload, timeout=30.0)
        assert resp.status_code == 200, f"contact-book non-200: {resp.status_code} {resp.text[:300]}"
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        r.detail = f"ok={body.get('ok')}, appt_id={body.get('appointment_id')}"

    async with step(ctx, "Supabase: re-fetch lead, check lifecycle") as r:
        await asyncio.sleep(3.0)
        rows = await supa_get_lead_by_email(ctx, ctx.smoke_email)
        assert rows, "no lead rows found after booking"
        # Take the most recent row
        rows_sorted = sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True)
        latest = rows_sorted[0]
        ctx.lead_row = latest
        ctx.lead_id = latest.get("id") or ctx.lead_id
        lifecycle = latest.get("lifecycle_status")
        stage = latest.get("current_stage")
        accepted_lifecycles = {None, "", "discovery_booked", "in_discovery", "qualified", "new"}
        # lifecycle_status may not be populated yet (orchestrator may not have ticked).
        # Per spec: assert it's in ('discovery_booked','in_discovery','qualified') OR
        # not yet set (orchestrator hasn't run) — both acceptable.
        assert (
            lifecycle in accepted_lifecycles or stage in ("qualified", "discovery_booked")
        ), f"unexpected lifecycle_status={lifecycle!r} current_stage={stage!r}"
        r.detail = f"lifecycle_status={lifecycle!r} current_stage={stage!r}"


async def step_autonomous_track(ctx: SmokeContext) -> None:
    funnel_id = "US_GROWTH" if ctx.locale == "us" else "BR_GROWTH"

    async with step(ctx, "Supabase: patch lead → autonomous candidate") as r:
        assert ctx.lead_id, "no lead_id to patch"
        # classify_track per spec accepts when ≥2 of 3 signals are present.
        qualification_data = {
            "budget_declared": True,
            "urgency": "high",
            "team_size": 8,
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        patch = {
            "funnel_id": funnel_id,
            "qualification_data": qualification_data,
            "next_action": "classify_track",
            "next_action_at": now_iso,
        }
        row = await supa_patch_lead(ctx, ctx.lead_id, patch)
        ctx.lead_row = row
        r.detail = f"funnel_id={funnel_id} next_action=classify_track"

    async with step(ctx, "POST /api/orchestrator/tick") as r:
        secret = ctx.orchestrator_secret
        assert secret, "ORCHESTRATOR_SECRET env var not set — cannot tick orchestrator"
        url = f"{ctx.base_url}/api/orchestrator/tick"
        resp = await ctx.client.post(url, params={"secret": secret}, timeout=30.0)
        assert resp.status_code == 200, f"tick non-200: {resp.status_code} {resp.text[:300]}"
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        # Per spec: "response shows ok>=1"
        ok_count = body.get("ok")
        if isinstance(ok_count, dict):  # accept {ok: {...counts...}} shape
            ok_count = ok_count.get("count") or ok_count.get("ran") or 0
        if ok_count is None:
            # Some implementations return {ran: N, ok: N, failed: N}
            ok_count = body.get("ran") or body.get("dispatched") or 0
        assert (ok_count or 0) >= 1, f"orchestrator did not run any handlers: {json.dumps(body)[:300]}"
        r.detail = f"orchestrator processed ok={ok_count}"

    async with step(ctx, "Supabase: verify track=autonomous + next_action") as r:
        await asyncio.sleep(1.0)
        row = await supa_get_lead_by_id(ctx, ctx.lead_id) if ctx.lead_id else None
        assert row, f"could not refetch lead {ctx.lead_id}"
        ctx.lead_row = row
        track = row.get("track")
        next_action = row.get("next_action")
        assert track == "autonomous", f"expected track=autonomous, got {track!r}"
        assert (
            next_action == "generate_proposal_v1"
        ), f"expected next_action=generate_proposal_v1, got {next_action!r}"
        r.detail = f"track=autonomous next_action={next_action}"


async def cleanup(ctx: SmokeContext) -> None:
    """Delete any leads with the unique smoke email — best-effort."""
    async with step(ctx, "Cleanup: delete smoke lead(s)") as r:
        try:
            # Delete by exact email match (more precise than LIKE)
            url = f"{ctx.supa_url}/leads"
            params = {"email": f"eq.{ctx.smoke_email}"}
            resp = await ctx.client.delete(
                url, headers=ctx.supa_headers, params=params, timeout=30.0
            )
            deleted = 0
            if resp.status_code in (200, 204):
                try:
                    rows = resp.json()
                    deleted = len(rows) if isinstance(rows, list) else 0
                except Exception:
                    deleted = 0
            r.detail = f"deleted {deleted} row(s) for {ctx.smoke_email}"
        except Exception as e:
            r.detail = f"cleanup error (ignored): {e}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(ctx: SmokeContext) -> None:
    print("\n" + "=" * 72)
    print(f"Smoke E2E summary  ({ctx.base_url}, track={ctx.track}, practice={ctx.practice}, locale={ctx.locale})")
    print("=" * 72)
    name_w = max((len(r.name) for r in ctx.results), default=20)
    for r in ctx.results:
        status = "PASS" if r.ok else "FAIL"
        line = f"  {status:4}  {r.name.ljust(name_w)}  {r.duration_ms:>6} ms"
        if r.detail:
            line += f"  · {r.detail}"
        if r.error:
            line += f"  · {r.error}"
        print(line)
    total_ms = sum(r.duration_ms for r in ctx.results)
    n_pass = sum(1 for r in ctx.results if r.ok)
    n_fail = sum(1 for r in ctx.results if not r.ok)
    print("-" * 72)
    print(f"  total: {total_ms} ms  ·  pass={n_pass}  fail={n_fail}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Anuvia funnel E2E smoke test")
    p.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", "https://anuvia.com.br"),
        help="LP base URL (default: $SMOKE_BASE_URL or https://anuvia.com.br)",
    )
    p.add_argument(
        "--track",
        choices=["discovery", "autonomous"],
        default="discovery",
        help="Which funnel track to exercise.",
    )
    p.add_argument(
        "--practice",
        choices=list(PRACTICE_CONFIG.keys()),
        default="cloud",
        help="Which practice diagnostic to exercise.",
    )
    p.add_argument(
        "--locale",
        choices=["br", "us"],
        default="br",
        help="Locale for synthetic payloads + Accept-Language hint.",
    )
    p.add_argument("--keep", action="store_true", help="Skip cleanup (leave smoke rows in Supabase).")
    p.add_argument("--verbose", action="store_true", help="Print every step's URL + payload hints.")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    supa_url = os.environ.get("SUPABASE_URL", "https://api.anuvia.com.br/rest/v1").rstrip("/")
    supa_key = os.environ.get("SUPABASE_KEY", "")
    if not supa_key:
        print("ERROR: SUPABASE_KEY env var is required.", file=sys.stderr)
        return 1
    supa_headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    timestamp = int(time.time())
    smoke_email = f"smoke+{timestamp}@anuvia.test"

    base_url = args.base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        ctx = SmokeContext(
            base_url=base_url,
            track=args.track,
            practice=args.practice,
            locale=args.locale,
            verbose=args.verbose,
            keep=args.keep,
            timestamp=timestamp,
            smoke_email=smoke_email,
            client=client,
            supa_url=supa_url,
            supa_headers=supa_headers,
            orchestrator_secret=os.environ.get("ORCHESTRATOR_SECRET", ""),
        )

        print(
            f"Smoke E2E start  ·  base={ctx.base_url}  track={ctx.track}  "
            f"practice={ctx.practice}  locale={ctx.locale}  email={ctx.smoke_email}",
            flush=True,
        )

        failed_step_name: Optional[str] = None
        failure_message: Optional[str] = None
        try:
            await step_analyze(ctx)
            await step_contact(ctx)
            await step_verify_supabase(ctx)
            if ctx.track == "discovery":
                await step_discovery_slots_and_book(ctx)
            elif ctx.track == "autonomous":
                await step_autonomous_track(ctx)
        except AssertionError as e:
            failed = next((r for r in ctx.results if not r.ok), None)
            failed_step_name = failed.name if failed else "(unknown)"
            failure_message = str(e)
        except Exception as e:
            failed = next((r for r in ctx.results if not r.ok), None)
            failed_step_name = failed.name if failed else "(unknown)"
            failure_message = f"{type(e).__name__}: {e}"
            if ctx.verbose:
                traceback.print_exc()

        # Always attempt cleanup unless --keep
        if not ctx.keep:
            try:
                await cleanup(ctx)
            except Exception as e:
                print(f"[cleanup raised, ignoring] {e}", flush=True)

        print_summary(ctx)

        if failed_step_name:
            alert_text = (
                f"Smoke E2E FAIL at step {failed_step_name!r} "
                f"({ctx.base_url}, track={ctx.track}): {failure_message}"
            )
            await slack_alert(ctx, alert_text)
            return 1
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[interrupted]", flush=True)
        sys.exit(130)
