"""
Anuvia · Unified admin monitoring console.

Single window into the entire Anuvia operation:

  GET /admin                            — landing page (navigation cards)
  GET /admin/engagements                — engagement lifecycle (1 row/eng)
  GET /admin/engagement/{id}            — single engagement deep view
  GET /admin/leads                      — qualified/discovery leads table
  GET /admin/pipeline                   — funnel counts at each stage
  GET /admin/bookings                   — agenda of upcoming sessions

Editorial-light Anuvia aesthetic shared across every view (Playfair + Inter
+ stone palette, white cards with #e7e5e4 borders, color-coded badges).
A sticky nav bar at the top links the four sections together so Mila can
flip between them in one click.

Auth: ALL routes accept EITHER
  * ``?token=<admin_smoke_hmac>``  (HMAC-SHA256("admin_smoke", CONTRACT_HMAC_SECRET))
  * ``?key=<ADMIN_API_KEY>``       (legacy bookings-view shared key)

Mint the HMAC token via ``GET /api/_admin/smoke/token?key=<key>``.
Read-only — no mutating buttons.
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from lib.sessions import SUPA_HEADERS, SUPA_URL

log = logging.getLogger("anuvia-admin-dashboard")

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])

HMAC_SECRET = (
    os.environ.get("CONTRACT_HMAC_SECRET", "")
    or os.environ.get("TRACK_B_HMAC_SECRET", "")
)
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")


# ---------------------------------------------------------------------------
# Auth — accept HMAC token OR legacy ADMIN_API_KEY
# ---------------------------------------------------------------------------


def _verify_admin_token(token: str) -> bool:
    """Verify the admin_smoke HMAC token used by the new admin dashboard."""
    if not HMAC_SECRET or not token:
        return False
    expected = hmac.new(
        HMAC_SECRET.encode("utf-8"),
        b"admin_smoke",
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


def _verify_admin_key(key: str) -> bool:
    """Verify the legacy ADMIN_API_KEY query param / Bearer header."""
    if not ADMIN_API_KEY or not key:
        return False
    return hmac.compare_digest(key, ADMIN_API_KEY)


def _check_auth(request: Request) -> str:
    """Validate auth and return the *token* (HMAC) used in onward links.

    Accepts:
      * ?token=<hmac>      preferred (admin_dashboard pattern)
      * ?key=<ADMIN_KEY>   legacy bookings-view pattern
      * Authorization: Bearer <ADMIN_KEY>   legacy

    Raises HTTPException(401) if neither matches. If only the legacy key was
    supplied, we still mint the HMAC token for forward links so the nav bar
    can use the modern auth path without re-prompting.
    """
    token = request.query_params.get("token", "")
    if token and _verify_admin_token(token):
        return token

    key = request.query_params.get("key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if key and _verify_admin_key(key):
        if HMAC_SECRET:
            return hmac.new(
                HMAC_SECRET.encode("utf-8"),
                b"admin_smoke",
                hashlib.sha256,
            ).hexdigest()
        return ""  # legacy mode — no HMAC available; nav links fall back

    raise HTTPException(401, "bad admin token")


def _auth_qs(token: str) -> str:
    """Build the auth query-string suffix for inter-view nav links."""
    if token:
        return f"?token={_html.escape(token)}"
    # Fallback when only the legacy key path is available — use ?key=
    if ADMIN_API_KEY:
        return f"?key={_html.escape(ADMIN_API_KEY)}"
    return ""


# ---------------------------------------------------------------------------
# Practice / status / stage formatting
# ---------------------------------------------------------------------------

_PRACTICE_LABEL = {
    "cloud_finops": "FinOps",
    "finops": "FinOps",
    "ai": "AI Readiness",
    "devops": "DevOps",
    "growth": "Sales Ops",
    "growth_salesops": "Sales Ops",
    "industry": "Industry",
}
_PRACTICE_COLOR = {
    "cloud_finops": "#0c4a6e",
    "finops": "#0c4a6e",
    "ai": "#6d28d9",
    "devops": "#0f766e",
    "growth": "#b45309",
    "growth_salesops": "#b45309",
    "industry": "#9f1239",
}

_STATUS_TONE = {
    "kickoff": ("#0c4a6e", "#e0f2fe"),
    "in_progress": ("#0f766e", "#ccfbf1"),
    "running": ("#0f766e", "#ccfbf1"),
    "review": ("#b45309", "#fef3c7"),
    "delivered": ("#15803d", "#dcfce7"),
    "invoiced": ("#15803d", "#dcfce7"),
    "closed": ("#525252", "#e7e5e4"),
    "cancelled": ("#b91c1c", "#fee2e2"),
}

# Lifecycle / stage tone for leads table
_LIFECYCLE_TONE = {
    "new": ("#525252", "#e7e5e4"),
    "qualified": ("#0c4a6e", "#e0f2fe"),
    "discovery_scheduled": ("#6d28d9", "#ede9fe"),
    "discovery_booked": ("#6d28d9", "#ede9fe"),
    "discovery_done": ("#0f766e", "#ccfbf1"),
    "proposal_sent": ("#b45309", "#fef3c7"),
    "contract_signed": ("#15803d", "#dcfce7"),
    "won": ("#15803d", "#dcfce7"),
    "lost": ("#b91c1c", "#fee2e2"),
    "error": ("#b91c1c", "#fee2e2"),
}


def _status_badge(status: str) -> str:
    fg, bg = _STATUS_TONE.get(status, ("#525252", "#e7e5e4"))
    if status and status.startswith("blocked"):
        fg, bg = "#b91c1c", "#fee2e2"
    return (
        f'<span class="badge" style="background:{bg};color:{fg};">'
        f'{_html.escape(status or "—")}</span>'
    )


def _lifecycle_badge(status: str) -> str:
    fg, bg = _LIFECYCLE_TONE.get(status, ("#525252", "#e7e5e4"))
    return (
        f'<span class="badge" style="background:{bg};color:{fg};">'
        f'{_html.escape(status or "—")}</span>'
    )


def _practice_badge(practice: str) -> str:
    label = _PRACTICE_LABEL.get(practice, practice or "—")
    color = _PRACTICE_COLOR.get(practice, "#525252")
    return (
        f'<span class="practice-badge" style="border-color:{color};color:{color};">'
        f'{_html.escape(label)}</span>'
    )


def _fmt_brl(value: Any) -> str:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return "—"
    s = f"{n:,}".replace(",", ".")
    return f"R$ {s}"


def _fmt_ts(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value[:16]


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_active(status: str) -> bool:
    return status not in ("delivered", "invoiced", "closed", "cancelled")


def _is_blocked(status: str) -> bool:
    return bool(status) and status.startswith("blocked")


def _intake_check_glyph(has_intake: bool) -> str:
    if has_intake:
        return '<span class="check">&#10003;</span>'
    return '<span class="nope">&#10007;</span>'


# ---------------------------------------------------------------------------
# Supabase fetchers
# ---------------------------------------------------------------------------


async def _fetch_engagements() -> list[dict]:
    url = (
        f"{SUPA_URL}/engagements"
        f"?select=*,lead:leads(id,name,email,company,next_action,next_action_at)"
        f"&order=updated_at.desc.nullslast"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning("dashboard: fetch_engagements %s %s", r.status_code, r.text[:300])
        return []
    rows = r.json()
    return rows if isinstance(rows, list) else []


async def _fetch_engagement(engagement_id: str) -> Optional[dict]:
    url = (
        f"{SUPA_URL}/engagements?id=eq.{engagement_id}"
        f"&select=*,lead:leads(*),contract:contracts(*)"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


async def _fetch_leads(limit: int = 200) -> list[dict]:
    """All leads, newest activity first — used by /admin/leads and /admin/pipeline."""
    url = (
        f"{SUPA_URL}/leads"
        f"?select=id,email,name,company,phone_e164,funnel_id,"
        f"current_stage,lifecycle_status,score,enrichment_data,"
        f"created_at,updated_at,last_touch_at,next_action,next_action_at,"
        f"qualification_data,agent_history,signals"
        f"&order=updated_at.desc.nullslast"
        f"&limit={int(limit)}"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning("dashboard: fetch_leads %s %s", r.status_code, r.text[:300])
        return []
    rows = r.json()
    return rows if isinstance(rows, list) else []


async def _fetch_leads_for_pipeline(days: int = 30) -> list[dict]:
    """Slim payload for funnel counting — no jsonb columns to keep it cheap."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = (
        f"{SUPA_URL}/leads"
        f"?select=id,lifecycle_status,current_stage,created_at,updated_at"
        f"&order=created_at.desc"
        f"&limit=5000"
    )
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning("dashboard: fetch_leads_pipeline %s %s", r.status_code, r.text[:300])
        return []
    rows = r.json() or []
    # Filter to window
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for row in rows:
        c = _parse_iso(row.get("created_at"))
        if c is None or c >= cutoff:
            out.append(row)
    return out


async def _fetch_engagements_for_pipeline() -> list[dict]:
    """Engagement status + practice for funnel bottom rows."""
    url = (
        f"{SUPA_URL}/engagements"
        f"?select=id,status,practice,delivery_mode,created_at,updated_at"
        f"&order=updated_at.desc.nullslast"
        f"&limit=5000"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        return []
    rows = r.json()
    return rows if isinstance(rows, list) else []


async def _fetch_engagements_with_sessions() -> list[dict]:
    """For /admin/bookings — engagements with artifacts.sessions populated."""
    url = (
        f"{SUPA_URL}/engagements"
        f"?select=id,lead_id,practice,status,delivery_mode,artifacts,"
        f"lead:leads(id,name,email,company)"
        f"&order=updated_at.desc.nullslast"
        f"&limit=300"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning("dashboard: fetch_eng_sessions %s %s", r.status_code, r.text[:300])
        return []
    rows = r.json()
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Shared page scaffold + nav bar
# ---------------------------------------------------------------------------

_PAGE_BASE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#fafaf9; --ink:#1c1917; --ink-soft:#44403c; --muted:#78716c;
    --card:#ffffff; --line:#e7e5e4; --line-strong:#d6d3d1; --accent:#0c4a6e;
    --accent-soft:#e0f2fe; --row-hover:#f5f5f4;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:var(--bg); color:var(--ink); margin:0;
         line-height:1.5; }}
  .navbar {{ position:sticky; top:0; z-index:30; background:rgba(250,250,249,0.92);
             backdrop-filter:saturate(140%) blur(8px);
             -webkit-backdrop-filter:saturate(140%) blur(8px);
             border-bottom:1px solid var(--line); }}
  .navbar-inner {{ max-width:1320px; margin:0 auto; padding:14px 24px;
                    display:flex; align-items:center; justify-content:space-between;
                    gap:24px; flex-wrap:wrap; }}
  .brand {{ font-family:'Playfair Display', Georgia, serif; font-weight:600;
            font-size:15px; letter-spacing:0.04em; color:var(--ink);
            text-decoration:none; }}
  .brand .dot {{ color:var(--muted); margin:0 6px; font-weight:400; }}
  .brand .sub {{ font-family:'Inter', sans-serif; font-size:11px;
                  text-transform:uppercase; letter-spacing:0.18em;
                  color:var(--muted); font-weight:500; }}
  .nav-links {{ display:flex; gap:6px; flex-wrap:wrap; }}
  .nav-link {{ font-size:13px; color:var(--ink-soft); text-decoration:none;
               padding:6px 12px; border-radius:18px;
               border:1px solid transparent;
               font-weight:500; }}
  .nav-link:hover {{ background:var(--row-hover); color:var(--ink); }}
  .nav-link.active {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
  .page {{ max-width:1320px; margin:0 auto; padding:32px 24px 48px; }}
  header.top {{ display:flex; align-items:baseline; justify-content:space-between;
                margin-bottom:24px; gap:16px; flex-wrap:wrap; }}
  .eyebrow {{ font-size:11px; letter-spacing:0.18em; text-transform:uppercase;
              color:var(--accent); font-weight:600; margin:0 0 4px; }}
  h1 {{ font-family:'Playfair Display', Georgia, serif; font-weight:600;
        font-size:30px; margin:0; letter-spacing:-0.01em; }}
  h2 {{ font-family:'Playfair Display', Georgia, serif; font-weight:500;
        font-size:22px; margin:24px 0 12px; letter-spacing:-0.005em; }}
  .top-meta {{ color:var(--muted); font-size:13px; }}
  .toolbar {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap;
              margin:18px 0 16px; }}
  .filter {{ display:inline-flex; align-items:center; gap:6px;
             padding:6px 14px; border-radius:20px; background:#fff;
             border:1px solid var(--line-strong); font-size:13px;
             cursor:pointer; user-select:none; color:var(--ink-soft);
             font-weight:500; }}
  .filter.active {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
  .filter .count {{ font-size:11px; padding:0 6px; border-radius:10px;
                     background:rgba(0,0,0,0.06); color:inherit; }}
  .filter.active .count {{ background:rgba(255,255,255,0.18); }}
  .search-box {{ flex:1; min-width:240px; max-width:360px; }}
  .search-box input {{ width:100%; padding:8px 14px; border:1px solid var(--line-strong);
                        border-radius:20px; background:#fff; font-size:13px;
                        font-family:inherit; }}
  .search-box input:focus {{ outline:none; border-color:var(--accent); }}
  table.engagements {{ width:100%; background:var(--card); border:1px solid var(--line);
                        border-radius:12px; border-collapse:separate;
                        border-spacing:0; overflow:hidden; }}
  table.engagements thead th {{ text-align:left; font-size:11px;
                                  text-transform:uppercase; letter-spacing:0.06em;
                                  color:var(--muted); font-weight:600;
                                  padding:12px 14px; border-bottom:1px solid var(--line);
                                  background:#fafaf9; }}
  table.engagements td {{ padding:14px; border-bottom:1px solid var(--line);
                           font-size:13px; vertical-align:top; }}
  tr.eng-row {{ cursor:pointer; transition:background 0.1s; }}
  tr.eng-row:hover {{ background:var(--row-hover); }}
  tr.eng-row.active {{ background:var(--accent-soft); }}
  td.client-cell {{ min-width:200px; }}
  td.client-cell .client-name {{ font-weight:600; color:var(--ink); }}
  td.client-cell .client-meta {{ color:var(--muted); font-size:12px;
                                   margin-top:2px; }}
  td.value-cell {{ font-variant-numeric:tabular-nums; white-space:nowrap;
                    font-weight:500; }}
  td.phase-cell {{ font-variant-numeric:tabular-nums; }}
  .phase-bar {{ display:inline-block; width:54px; height:6px; border-radius:3px;
                 background:var(--line); position:relative; overflow:hidden;
                 vertical-align:middle; margin-right:6px; }}
  .phase-bar span {{ position:absolute; left:0; top:0; bottom:0;
                      background:var(--accent); border-radius:3px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px;
             font-size:11px; font-weight:600; letter-spacing:0.02em; }}
  .practice-badge {{ display:inline-block; padding:3px 10px; border-radius:12px;
                      font-size:11px; font-weight:600; border:1px solid;
                      background:#fff; }}
  .mode-pill {{ display:inline-block; padding:2px 8px; border-radius:10px;
                 font-size:11px; font-weight:600; background:#f5f5f4;
                 color:var(--ink-soft); }}
  .ts {{ font-variant-numeric:tabular-nums; color:var(--ink-soft);
          font-family:'JetBrains Mono', monospace; font-size:12px; }}
  .check {{ color:#15803d; font-weight:700; }}
  .nope {{ color:#b91c1c; font-weight:700; }}
  tr.detail-row td {{ background:#fafaf9; padding:0; border-bottom:1px solid var(--line); }}
  tr.detail-row .detail-inner {{ padding:20px 22px; }}
  .detail-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px;
                   margin-top:8px; }}
  .detail-section h3 {{ font-family:'Playfair Display', Georgia, serif;
                          font-size:15px; font-weight:500; margin:0 0 8px;
                          color:var(--ink); }}
  .detail-section pre {{ background:#fff; border:1px solid var(--line);
                          border-radius:8px; padding:12px; font-size:12px;
                          color:var(--ink-soft); overflow:auto; max-height:320px;
                          font-family:'JetBrains Mono', monospace;
                          margin:0; }}
  .kv {{ font-size:12px; }}
  .kv dt {{ color:var(--muted); font-weight:500; margin-top:6px;
            text-transform:uppercase; letter-spacing:0.04em; font-size:10px; }}
  .kv dd {{ margin:0; color:var(--ink); }}
  .pill-list {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }}
  .pill-list .pill {{ background:#fff; border:1px solid var(--line-strong);
                       border-radius:14px; padding:2px 10px; font-size:12px; }}
  .empty {{ text-align:center; color:var(--muted); padding:48px 0;
             font-size:14px; }}
  a.detail-link {{ color:var(--accent); font-size:12px; }}
  .stk-table {{ width:100%; border-collapse:collapse; margin-top:6px;
                 background:#fff; border:1px solid var(--line);
                 border-radius:8px; overflow:hidden; }}
  .stk-table th, .stk-table td {{ text-align:left; padding:8px 10px;
                                    font-size:12px; border-bottom:1px solid var(--line); }}
  .stk-table th {{ background:#fafaf9; color:var(--muted); font-weight:600;
                    text-transform:uppercase; font-size:10px; letter-spacing:0.04em; }}
  .stk-table tr:last-child td {{ border-bottom:none; }}
  .footer-note {{ color:var(--muted); font-size:12px; text-align:center;
                   margin-top:20px; }}

  /* Landing cards (/admin) */
  .card-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
                 gap:16px; margin-top:24px; }}
  a.nav-card {{ display:block; background:var(--card); border:1px solid var(--line);
                 border-radius:12px; padding:24px; text-decoration:none;
                 color:inherit; transition:border-color 0.15s, transform 0.15s; }}
  a.nav-card:hover {{ border-color:var(--accent); transform:translateY(-1px); }}
  a.nav-card .nc-eyebrow {{ font-size:10px; letter-spacing:0.18em;
                              text-transform:uppercase; color:var(--accent);
                              font-weight:600; }}
  a.nav-card h3 {{ font-family:'Playfair Display', Georgia, serif;
                    font-size:22px; font-weight:600; margin:6px 0 8px;
                    color:var(--ink); letter-spacing:-0.005em; }}
  a.nav-card p {{ color:var(--muted); font-size:13px; margin:0 0 14px; }}
  a.nav-card .cta {{ font-size:12px; color:var(--accent); font-weight:600;
                      letter-spacing:0.02em; }}

  /* Pipeline funnel */
  .funnel {{ background:var(--card); border:1px solid var(--line);
              border-radius:12px; padding:20px; }}
  .funnel-row {{ display:grid; grid-template-columns:220px 1fr 90px 120px;
                  gap:14px; align-items:center; padding:12px 4px;
                  border-bottom:1px solid var(--line); }}
  .funnel-row:last-child {{ border-bottom:none; }}
  .funnel-row .stage-name {{ font-weight:500; font-size:14px;
                              color:var(--ink); text-decoration:none; }}
  .funnel-row .stage-name:hover {{ color:var(--accent); }}
  .funnel-row .stage-bar {{ background:#f5f5f4; height:14px;
                             border-radius:7px; overflow:hidden;
                             position:relative; }}
  .funnel-row .stage-bar span {{ position:absolute; left:0; top:0; bottom:0;
                                   border-radius:7px; }}
  .funnel-row .stage-count {{ font-variant-numeric:tabular-nums;
                                font-weight:600; font-size:18px;
                                font-family:'Playfair Display', Georgia, serif; }}
  .funnel-row .stage-conv {{ font-size:12px; color:var(--muted);
                               text-align:right; font-variant-numeric:tabular-nums; }}
  .range-picker {{ display:inline-flex; gap:6px; }}

  /* Bookings agenda */
  .agenda-section {{ margin-top:24px; }}
  .agenda-section h2 {{ display:flex; align-items:baseline; gap:10px; }}
  .agenda-section h2 .count-tag {{ font-size:11px; letter-spacing:0.08em;
                                     text-transform:uppercase; color:var(--muted);
                                     font-family:'Inter', sans-serif; font-weight:500; }}
  .session-card {{ background:var(--card); border:1px solid var(--line);
                    border-radius:10px; padding:14px 18px; margin-bottom:8px;
                    display:grid; grid-template-columns:120px 1fr auto;
                    gap:16px; align-items:center; }}
  .session-card .when {{ font-family:'JetBrains Mono', monospace;
                          font-size:13px; color:var(--ink-soft);
                          font-variant-numeric:tabular-nums; }}
  .session-card .who {{ font-size:14px; }}
  .session-card .who .name {{ font-weight:600; color:var(--ink); }}
  .session-card .who .meta {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  .session-card .actions a {{ display:inline-block; font-size:12px;
                                padding:6px 12px; margin-left:6px;
                                border-radius:14px; text-decoration:none;
                                border:1px solid var(--line-strong);
                                color:var(--ink-soft); font-weight:500; }}
  .session-card .actions a:hover {{ border-color:var(--accent); color:var(--accent); }}
  .session-card .actions a.primary {{ background:var(--ink); color:#fff;
                                        border-color:var(--ink); }}
  .session-card.past {{ opacity:0.55; }}

  /* Leads detail JSON blocks */
  .json-block {{ background:#fff; border:1px solid var(--line);
                  border-radius:8px; padding:12px; font-size:12px;
                  color:var(--ink-soft); overflow:auto; max-height:280px;
                  font-family:'JetBrains Mono', monospace;
                  white-space:pre-wrap; word-break:break-word; margin:0; }}
</style>
</head>
<body>
{nav}
<div class="page">
{body}
</div>
</body>
</html>
"""


_NAV_ITEMS = [
    ("admin",       "Home",        "/admin"),
    ("engagements", "Engagements", "/admin/engagements"),
    ("leads",       "Leads",       "/admin/leads"),
    ("pipeline",    "Pipeline",    "/admin/pipeline"),
    ("bookings",    "Bookings",    "/admin/bookings"),
]


def _render_nav(active: str, token: str) -> str:
    qs = _auth_qs(token)
    links: list[str] = []
    for key, label, path in _NAV_ITEMS:
        cls = "nav-link active" if key == active else "nav-link"
        links.append(
            f'<a class="{cls}" href="{path}{qs}">{_html.escape(label)}</a>'
        )
    return (
        '<nav class="navbar"><div class="navbar-inner">'
        f'<a class="brand" href="/admin{qs}">'
        '<span>ANUVIA</span><span class="dot">·</span>'
        '<span class="sub">Admin Console</span>'
        '</a>'
        f'<div class="nav-links">{"".join(links)}</div>'
        '</div></nav>'
    )


def _render_page(
    title: str,
    body: str,
    active: str,
    token: str,
    status: int = 200,
) -> HTMLResponse:
    nav = _render_nav(active, token)
    return HTMLResponse(
        _PAGE_BASE.format(title=title, nav=nav, body=body),
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Engagement row rendering (kept from original)
# ---------------------------------------------------------------------------


def _render_engagement_row(eng: dict, token: str) -> str:
    eid = eng.get("id", "")
    lead = eng.get("lead") or {}
    client_name = (lead.get("name") or "—").strip()
    company = (lead.get("company") or "").strip()
    email = (lead.get("email") or "").strip()
    practice = eng.get("practice") or ""
    value = eng.get("total_value_brl")
    current = eng.get("current_phase") or 0
    total = eng.get("total_phases") or 0
    status = eng.get("status") or ""
    delivery_mode = (eng.get("delivery_mode") or "whiteglove")
    intake_data = eng.get("intake_data") or {}
    has_intake = bool(intake_data) and (
        not isinstance(intake_data, dict) or len(intake_data) > 0
    )
    next_phase_at = eng.get("next_phase_at")
    next_action_at = (lead.get("next_action_at") if isinstance(lead, dict) else None)
    next_at_display = next_phase_at or next_action_at
    updated_at = eng.get("updated_at") or eng.get("created_at")

    pct = int(round((current / total) * 100)) if total else 0
    pct = max(0, min(100, pct))

    bucket = "active" if _is_active(status) else "delivered"
    if _is_blocked(status):
        bucket = "blocked"

    search_blob = " ".join([
        client_name, company, email, practice, status, str(eid)
    ]).lower()

    row_html = (
        f'<tr class="eng-row" data-eid="{_html.escape(eid)}" '
        f'data-bucket="{bucket}" data-search="{_html.escape(search_blob)}" '
        f'onclick="toggleRow(this)">'
        f'<td class="client-cell">'
        f'<div class="client-name">{_html.escape(client_name)}</div>'
        f'<div class="client-meta">'
        f'{_html.escape(company) if company else _html.escape(email)}'
        f'</div>'
        f'</td>'
        f'<td>{_practice_badge(practice)}</td>'
        f'<td class="value-cell">{_fmt_brl(value)}</td>'
        f'<td class="phase-cell">'
        f'<span class="phase-bar"><span style="width:{pct}%;"></span></span>'
        f'{current}/{total or "?"}'
        f'</td>'
        f'<td>{_status_badge(status)}</td>'
        f'<td style="text-align:center;">'
        f'{_intake_check_glyph(has_intake)}'
        f'</td>'
        f'<td><span class="mode-pill">{_html.escape(delivery_mode)}</span></td>'
        f'<td class="ts">{_fmt_ts(updated_at)}</td>'
        f'<td class="ts">{_fmt_ts(next_at_display)}</td>'
        f'</tr>'
    )

    detail_html = (
        f'<tr class="detail-row" id="detail-{_html.escape(eid)}" style="display:none;">'
        f'<td colspan="9">'
        f'<div class="detail-inner">'
        f'{_render_detail_body(eng, token, link_only=True)}'
        f'</div>'
        f'</td>'
        f'</tr>'
    )

    return row_html + detail_html


def _render_intake_summary(intake_data: Any) -> str:
    if not isinstance(intake_data, dict) or not intake_data:
        return '<p class="muted" style="font-size:12px;">Intake não submetido.</p>'

    stakeholders = intake_data.get("stakeholders")
    rows: list[str] = []
    label_overrides = {
        "executive_sponsor_name": "Executive sponsor",
        "executive_sponsor_email": "Sponsor email",
        "company_name": "Empresa",
        "industry_vertical": "Vertical",
        "arr_range_brl": "ARR",
        "growth_stage": "Estágio",
        "urgency": "Urgência",
        "aws_spend_brl_monthly": "AWS spend mensal",
        "aws_account_count": "AWS accounts",
        "aws_organizations_structure": "Org structure",
        "aws_regions": "Regions",
        "ri_sp_coverage": "Cobertura RI/SP",
        "primary_services": "Serviços principais",
        "observability_stack": "Observability",
        "tagging_strategy": "Tagging",
        "biggest_concerns": "Preocupações",
        "why_now": "Por que agora",
        "compliance_frames": "Compliance",
        "remediation_choice": "Remediação",
    }

    for key, raw in intake_data.items():
        if key == "stakeholders":
            continue
        label = label_overrides.get(key, key.replace("_", " "))
        if isinstance(raw, list):
            value = (
                '<div class="pill-list">'
                + "".join(
                    f'<span class="pill">{_html.escape(str(v))}</span>'
                    for v in raw
                )
                + '</div>'
            )
        elif key.endswith("_brl") or key.startswith("aws_spend"):
            value = _html.escape(_fmt_brl(raw))
        elif isinstance(raw, str) and len(raw) > 120:
            value = (
                f'<div style="white-space:pre-wrap;font-size:12px;">'
                f'{_html.escape(raw)}</div>'
            )
        else:
            value = _html.escape(str(raw))
        rows.append(f"<dt>{_html.escape(label)}</dt><dd>{value}</dd>")

    stk_html = ""
    if isinstance(stakeholders, list) and stakeholders:
        stk_rows = "".join(
            f'<tr>'
            f'<td>{_html.escape(str(s.get("name", "")))}</td>'
            f'<td>{_html.escape(str(s.get("email", "")))}</td>'
            f'<td>{_html.escape(str(s.get("role", "")))}</td>'
            f'</tr>'
            for s in stakeholders if isinstance(s, dict)
        )
        stk_html = (
            '<h3 style="margin-top:14px;">Stakeholders</h3>'
            f'<table class="stk-table"><thead><tr>'
            f'<th>Nome</th><th>Email</th><th>Papel</th>'
            f'</tr></thead><tbody>{stk_rows}</tbody></table>'
        )

    return f'<dl class="kv">{"".join(rows)}</dl>{stk_html}'


def _render_artifacts_summary(artifacts: Any) -> str:
    if not isinstance(artifacts, dict) or not artifacts:
        return '<p class="muted" style="font-size:12px;">Sem artifacts ainda.</p>'

    urls = []
    for key, val in artifacts.items():
        if isinstance(val, str) and val.startswith("http"):
            urls.append((key, val))
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                if isinstance(v2, str) and v2.startswith("http"):
                    urls.append((f"{key}.{k2}", v2))

    url_html = ""
    if urls:
        items = "".join(
            f'<li><a class="detail-link" href="{_html.escape(u)}" target="_blank">'
            f'{_html.escape(k)}</a></li>'
            for (k, u) in urls
        )
        url_html = (
            '<h3 style="margin-top:14px;">Deliverables</h3>'
            f'<ul style="font-size:12px;padding-left:18px;margin:6px 0;">{items}</ul>'
        )

    sessions = artifacts.get("sessions") or []
    if not sessions and isinstance(artifacts.get("phase_sessions"), list):
        sessions = artifacts["phase_sessions"]
    sess_html = ""
    if sessions:
        if isinstance(sessions, dict):
            iter_sessions = list(sessions.values())
        else:
            iter_sessions = sessions
        s_items = "".join(
            f'<li><span class="ts">{_html.escape(str(s.get("scheduled_at_br") or s.get("scheduled_at") or s.get("at") or ""))}</span> '
            f'— {_html.escape(str(s.get("phase") or s.get("title") or ""))}</li>'
            for s in iter_sessions if isinstance(s, dict)
        )
        if s_items:
            sess_html = (
                '<h3 style="margin-top:14px;">Sessões agendadas</h3>'
                f'<ul style="font-size:12px;padding-left:18px;margin:6px 0;">{s_items}</ul>'
            )

    try:
        dump = json.dumps(artifacts, indent=2, ensure_ascii=False, default=str)
    except Exception:
        dump = str(artifacts)

    return (
        url_html
        + sess_html
        + '<h3 style="margin-top:14px;">Artifacts (raw)</h3>'
        + f'<pre>{_html.escape(dump)}</pre>'
    )


def _render_detail_body(eng: dict, token: str, link_only: bool = False) -> str:
    eid = eng.get("id", "")
    intake_data = eng.get("intake_data") or {}
    artifacts = eng.get("artifacts") or {}
    lead = eng.get("lead") or {}

    lead_name = lead.get("name") or "—"
    lead_email = lead.get("email") or "—"
    lead_company = lead.get("company") or "—"
    standalone_link = (
        f'<a class="detail-link" href="/admin/engagement/{_html.escape(eid)}'
        f'{_auth_qs(token)}" target="_blank">'
        f'abrir em nova aba &#8599;</a>'
    )

    header = (
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:baseline;flex-wrap:wrap;gap:12px;">'
        f'<div>'
        f'<strong>{_html.escape(lead_name)}</strong> '
        f'<span class="muted" style="font-size:12px;">· {_html.escape(lead_email)}'
        f' · {_html.escape(lead_company)}</span>'
        f'</div>'
        f'<div style="font-family:JetBrains Mono,monospace;font-size:11px;'
        f'color:#78716c;">engagement: {_html.escape(eid)}</div>'
        f'</div>'
        f'<div style="margin-top:6px;">{standalone_link}</div>'
    )

    body = (
        f'<div class="detail-grid">'
        f'<div class="detail-section">'
        f'<h3>Intake</h3>'
        f'{_render_intake_summary(intake_data)}'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>Artifacts &amp; deliverables</h3>'
        f'{_render_artifacts_summary(artifacts)}'
        f'</div>'
        f'</div>'
    )
    return header + body


# ---------------------------------------------------------------------------
# Filter / search JS — used by /admin/engagements + /admin/leads (same DOM shape)
# ---------------------------------------------------------------------------

_DASHBOARD_JS = r"""
<script>
(function(){
  function $$(sel){ return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  var rows = $$("tr.eng-row");
  var filterBtns = $$(".filter");
  var searchInput = document.getElementById("search-input");
  var emptyEl = document.getElementById("empty-state");
  var current = "all";

  function applyFilters(){
    var q = (searchInput && searchInput.value || "").toLowerCase().trim();
    var shown = 0;
    rows.forEach(function(r){
      var bucket = r.getAttribute("data-bucket");
      var blob = r.getAttribute("data-search") || "";
      var bucketOk = (current === "all") || (bucket === current);
      var qOk = !q || blob.indexOf(q) !== -1;
      var show = bucketOk && qOk;
      r.style.display = show ? "" : "none";
      var detail = document.getElementById("detail-" + r.getAttribute("data-eid"));
      if(detail && !show){ detail.style.display = "none"; r.classList.remove("active"); }
      if(show) shown++;
    });
    if(emptyEl){ emptyEl.style.display = shown ? "none" : ""; }
  }

  filterBtns.forEach(function(btn){
    btn.addEventListener("click", function(){
      filterBtns.forEach(function(b){ b.classList.remove("active"); });
      btn.classList.add("active");
      current = btn.getAttribute("data-filter");
      applyFilters();
    });
  });

  if(searchInput) searchInput.addEventListener("input", applyFilters);

  window.toggleRow = function(row){
    var eid = row.getAttribute("data-eid");
    var detail = document.getElementById("detail-" + eid);
    if(!detail) return;
    var isOpen = detail.style.display !== "none";
    if(isOpen){
      detail.style.display = "none";
      row.classList.remove("active");
    } else {
      detail.style.display = "";
      row.classList.add("active");
    }
  };
})();
</script>
"""


# ---------------------------------------------------------------------------
# /admin — landing page
# ---------------------------------------------------------------------------


_LANDING_CARDS = [
    {
        "eyebrow": "Operação",
        "title": "Engagements",
        "desc": "Cada engagement ativo em uma linha. Fases, status, intake, próxima ação.",
        "key": "engagements",
        "path": "/admin/engagements",
    },
    {
        "eyebrow": "Topo do funil",
        "title": "Leads",
        "desc": "Leads qualificados, discovery agendadas, discovery concluídas. Filtros e busca.",
        "key": "leads",
        "path": "/admin/leads",
    },
    {
        "eyebrow": "Conversão",
        "title": "Pipeline",
        "desc": "Funil de ponta a ponta — contagens e taxas de conversão por estágio.",
        "key": "pipeline",
        "path": "/admin/pipeline",
    },
    {
        "eyebrow": "Calendário",
        "title": "Bookings",
        "desc": "Agenda das próximas sessões com Meet URL e link do pre-call brief.",
        "key": "bookings",
        "path": "/admin/bookings",
    },
]


@router.get("")
@router.get("/")
async def admin_index(request: Request):
    token = _check_auth(request)
    qs = _auth_qs(token)
    cards_html = "".join(
        f'<a class="nav-card" href="{c["path"]}{qs}">'
        f'<div class="nc-eyebrow">{_html.escape(c["eyebrow"])}</div>'
        f'<h3>{_html.escape(c["title"])}</h3>'
        f'<p>{_html.escape(c["desc"])}</p>'
        f'<div class="cta">Abrir &rarr;</div>'
        f'</a>'
        for c in _LANDING_CARDS
    )
    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Painel interno</p>
        <h1>Console</h1>
      </div>
      <div class="top-meta">
        atualizado {_fmt_ts(datetime.now(timezone.utc).isoformat())}
      </div>
    </header>
    <p style="color:var(--muted);font-size:14px;max-width:680px;">
      Uma janela única para a operação Anuvia. Tudo abaixo é
      read-only — para mutar estado, use ações do Slack ou
      <code style="font-family:'JetBrains Mono',monospace;font-size:12px;">/api/_admin/smoke</code>.
    </p>
    <div class="card-grid">{cards_html}</div>
    <p class="footer-note">Anuvia &middot; Admin Console</p>
    """
    return _render_page("Anuvia · Admin", body, "admin", token)


# ---------------------------------------------------------------------------
# /admin/engagements
# ---------------------------------------------------------------------------


@router.get("/engagements")
async def engagements_list(request: Request):
    token = _check_auth(request)
    engagements = await _fetch_engagements()

    total = len(engagements)
    active = sum(1 for e in engagements if _is_active(e.get("status") or "")
                 and not _is_blocked(e.get("status") or ""))
    blocked = sum(1 for e in engagements if _is_blocked(e.get("status") or ""))
    delivered = sum(1 for e in engagements if not _is_active(e.get("status") or ""))

    rows_html = "".join(
        _render_engagement_row(e, token) for e in engagements
    )

    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Painel interno</p>
        <h1>Engagements</h1>
      </div>
      <div class="top-meta">
        {total} engagement{'s' if total != 1 else ''} ·
        atualizado {_fmt_ts(datetime.now(timezone.utc).isoformat())}
      </div>
    </header>

    <div class="toolbar">
      <span class="filter active" data-filter="all">Todos <span class="count">{total}</span></span>
      <span class="filter" data-filter="active">Ativos <span class="count">{active}</span></span>
      <span class="filter" data-filter="blocked">Blocked <span class="count">{blocked}</span></span>
      <span class="filter" data-filter="delivered">Delivered <span class="count">{delivered}</span></span>
      <div class="search-box">
        <input id="search-input" type="search"
               placeholder="Buscar por cliente, empresa, email…" autocomplete="off"/>
      </div>
    </div>

    <table class="engagements">
      <thead>
        <tr>
          <th>Cliente</th>
          <th>Prática</th>
          <th>Valor</th>
          <th>Fase</th>
          <th>Status</th>
          <th style="text-align:center;">Intake</th>
          <th>Mode</th>
          <th>Última atualização</th>
          <th>Próxima ação</th>
        </tr>
      </thead>
      <tbody>
        {rows_html if rows_html else ''}
      </tbody>
    </table>
    <div id="empty-state" class="empty" style="display:{('none' if total else '')};">
      Nenhum engagement encontrado.
    </div>
    <p class="footer-note">Read-only. Para mutar estado, use Slack actions ou /api/_admin/smoke.</p>
    {_DASHBOARD_JS}
    """
    return _render_page("Engagements — Anuvia admin", body, "engagements", token)


@router.get("/engagement/{engagement_id}")
async def engagement_detail(engagement_id: str, request: Request):
    token = _check_auth(request)
    eng = await _fetch_engagement(engagement_id)
    if not eng:
        raise HTTPException(404, "engagement not found")
    lead = eng.get("lead") or {}
    practice = eng.get("practice") or ""
    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Engagement</p>
        <h1>{_html.escape(lead.get('name') or '—')} <span style="font-size:18px;color:#78716c;font-family:Inter,sans-serif;">· {_practice_badge(practice)}</span></h1>
      </div>
      <div class="top-meta">
        <a class="detail-link" href="/admin/engagements{_auth_qs(token)}">&larr; voltar</a>
      </div>
    </header>
    <div style="background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:24px;">
      {_render_detail_body(eng, token)}
    </div>
    """
    return _render_page("Engagement detail — Anuvia admin", body, "engagements", token)


# ---------------------------------------------------------------------------
# /admin/leads
# ---------------------------------------------------------------------------


_LEAD_BUCKETS = [
    ("all",                 "Todos"),
    ("qualified",           "Qualificados"),
    ("discovery_scheduled", "Discovery agendada"),
    ("discovery_done",      "Discovery concluída"),
    ("proposal_sent",       "Proposta enviada"),
    ("lost",                "Perdidos"),
]


def _lead_bucket_of(lead: dict) -> str:
    """Classify a lead row into a filter bucket key."""
    life = (lead.get("lifecycle_status") or "").lower()
    stage = (lead.get("current_stage") or "").lower()

    if life in ("lost", "error", "unsubscribed"):
        return "lost"
    if life in ("proposal_sent", "proposal_signed", "contract_signed", "won"):
        return "proposal_sent"
    if life in ("discovery_done",) or stage == "discovery_done":
        return "discovery_done"
    if life in ("discovery_scheduled", "discovery_booked") or stage == "discovery_scheduled":
        return "discovery_scheduled"
    if life == "qualified" or stage == "qualified":
        return "qualified"
    return "new"


def _render_lead_row(lead: dict, token: str) -> str:
    lid = lead.get("id") or ""
    name = (lead.get("name") or "—").strip()
    company = (lead.get("company") or "").strip()
    email = (lead.get("email") or "").strip()
    life = lead.get("lifecycle_status") or lead.get("current_stage") or "—"
    score = lead.get("score")
    score_display = "—" if score is None else str(score)
    updated = lead.get("updated_at") or lead.get("last_touch_at") or lead.get("created_at")
    next_action = lead.get("next_action") or ""
    next_action_at = lead.get("next_action_at")
    next_display = (
        f'<div>{_html.escape(next_action)}</div>'
        f'<div class="ts" style="margin-top:2px;">{_fmt_ts(next_action_at)}</div>'
        if next_action or next_action_at else '<span class="ts">—</span>'
    )

    bucket = _lead_bucket_of(lead)
    search_blob = " ".join([
        name, company, email, str(lead.get("funnel_id") or ""),
        str(life), str(lid)
    ]).lower()

    qd = lead.get("qualification_data") or {}
    gcal = qd.get("gcal") if isinstance(qd, dict) else None
    gcal = gcal if isinstance(gcal, dict) else {}
    meet_url = (
        gcal.get("meet_url")
        or (qd.get("meet_url") if isinstance(qd, dict) else None)
        or (qd.get("meeting_url") if isinstance(qd, dict) else None)
        or ""
    )
    html_link = gcal.get("html_link") or ""

    row_html = (
        f'<tr class="eng-row" data-eid="{_html.escape(lid)}" '
        f'data-bucket="{bucket}" data-search="{_html.escape(search_blob)}" '
        f'onclick="toggleRow(this)">'
        f'<td class="client-cell">'
        f'<div class="client-name">{_html.escape(name)}</div>'
        f'<div class="client-meta">{_html.escape(company) if company else _html.escape(email)}</div>'
        f'</td>'
        f'<td>'
        f'<div>{_html.escape(email)}</div>'
        f'<div class="client-meta">{_html.escape(company)}</div>'
        f'</td>'
        f'<td>{_lifecycle_badge(life)}</td>'
        f'<td class="value-cell">{_html.escape(score_display)}</td>'
        f'<td>{next_display}</td>'
        f'<td class="ts">{_fmt_ts(updated)}</td>'
        f'</tr>'
    )

    # Detail row: qualification_data, agent_history, signals, gcal links
    links_html: list[str] = []
    if meet_url:
        links_html.append(
            f'<a class="detail-link" href="{_html.escape(meet_url)}" target="_blank">Meet ↗</a>'
        )
    if html_link:
        links_html.append(
            f'<a class="detail-link" href="{_html.escape(html_link)}" target="_blank">Gcal ↗</a>'
        )
    links_strip = (
        '<div style="margin-top:8px;">' + ' &middot; '.join(links_html) + '</div>'
    ) if links_html else ""

    def _pretty(blob: Any) -> str:
        if not blob:
            return '<p class="muted" style="font-size:12px;">—</p>'
        try:
            return f'<pre class="json-block">{_html.escape(json.dumps(blob, indent=2, ensure_ascii=False, default=str))}</pre>'
        except Exception:
            return f'<pre class="json-block">{_html.escape(str(blob))}</pre>'

    detail_inner = (
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:12px;">'
        f'<div><strong>{_html.escape(name)}</strong>'
        f' <span class="muted" style="font-size:12px;">· {_html.escape(email)} · {_html.escape(company)}</span></div>'
        f'<div style="font-family:JetBrains Mono,monospace;font-size:11px;color:#78716c;">'
        f'lead: {_html.escape(str(lid))}</div>'
        f'</div>'
        f'{links_strip}'
        f'<div class="detail-grid" style="grid-template-columns:1fr 1fr;">'
        f'<div class="detail-section">'
        f'<h3>Qualification data</h3>{_pretty(qd)}'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>Score &amp; sinais</h3>'
        f'<dl class="kv">'
        f'<dt>Score</dt><dd>{_html.escape(score_display)}</dd>'
        f'<dt>Funnel</dt><dd>{_html.escape(str(lead.get("funnel_id") or "—"))}</dd>'
        f'<dt>Lifecycle</dt><dd>{_html.escape(str(lead.get("lifecycle_status") or "—"))}</dd>'
        f'<dt>Stage</dt><dd>{_html.escape(str(lead.get("current_stage") or "—"))}</dd>'
        f'<dt>Phone</dt><dd>{_html.escape(str(lead.get("phone_e164") or "—"))}</dd>'
        f'<dt>Created</dt><dd>{_fmt_ts(lead.get("created_at"))}</dd>'
        f'<dt>Last touch</dt><dd>{_fmt_ts(lead.get("last_touch_at"))}</dd>'
        f'</dl>'
        f'<h3 style="margin-top:14px;">Signals</h3>{_pretty(lead.get("signals"))}'
        f'</div>'
        f'</div>'
        f'<div class="detail-section" style="margin-top:18px;">'
        f'<h3>Agent history</h3>{_pretty(lead.get("agent_history"))}'
        f'</div>'
        f'<div class="detail-section" style="margin-top:18px;">'
        f'<h3>Enrichment</h3>{_pretty(lead.get("enrichment_data"))}'
        f'</div>'
    )

    detail_html = (
        f'<tr class="detail-row" id="detail-{_html.escape(lid)}" style="display:none;">'
        f'<td colspan="6"><div class="detail-inner">{detail_inner}</div></td>'
        f'</tr>'
    )
    return row_html + detail_html


@router.get("/leads")
async def leads_list(request: Request):
    token = _check_auth(request)
    leads = await _fetch_leads(limit=200)

    counts = {key: 0 for key, _ in _LEAD_BUCKETS}
    counts["all"] = len(leads)
    for lead in leads:
        b = _lead_bucket_of(lead)
        if b in counts:
            counts[b] += 1
        else:
            # 'new' bucket isn't a filter chip — counted only in 'all'
            pass

    filter_chips = "".join(
        f'<span class="filter{" active" if key == "all" else ""}" data-filter="{key}">'
        f'{_html.escape(label)} <span class="count">{counts.get(key, 0)}</span>'
        f'</span>'
        for key, label in _LEAD_BUCKETS
    )

    rows_html = "".join(_render_lead_row(lead, token) for lead in leads)
    total = len(leads)

    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Topo do funil</p>
        <h1>Leads</h1>
      </div>
      <div class="top-meta">
        {total} lead{'s' if total != 1 else ''} ·
        atualizado {_fmt_ts(datetime.now(timezone.utc).isoformat())}
      </div>
    </header>

    <div class="toolbar">
      {filter_chips}
      <div class="search-box">
        <input id="search-input" type="search"
               placeholder="Buscar por nome, empresa, email…" autocomplete="off"/>
      </div>
    </div>

    <table class="engagements">
      <thead>
        <tr>
          <th>Cliente</th>
          <th>Email / Empresa</th>
          <th>Estágio</th>
          <th>Score</th>
          <th>Próxima ação</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <div id="empty-state" class="empty" style="display:{('none' if total else '')};">
      Nenhum lead encontrado.
    </div>
    <p class="footer-note">Read-only. Para criar/editar lead, use os agents ou Supabase Studio.</p>
    {_DASHBOARD_JS}
    """
    return _render_page("Leads — Anuvia admin", body, "leads", token)


# ---------------------------------------------------------------------------
# /admin/pipeline
# ---------------------------------------------------------------------------


def _normalize_lifecycle(s: Optional[str]) -> str:
    return (s or "").lower().strip()


def _build_funnel_counts(leads: list[dict], engagements: list[dict]) -> list[dict]:
    """Return ordered list of {key, label, count} for the funnel rows.

    The pipeline collapses synonymous stages (e.g. discovery_scheduled and
    discovery_booked) and treats lifecycle_status as the primary signal,
    falling back to current_stage when lifecycle is missing.
    """
    def _stage_of(lead: dict) -> str:
        return (
            _normalize_lifecycle(lead.get("lifecycle_status"))
            or _normalize_lifecycle(lead.get("current_stage"))
        )

    new_total = len(leads)
    qualified = sum(
        1 for l in leads if _stage_of(l) in
        ("qualified", "discovery_scheduled", "discovery_booked",
         "discovery_done", "proposal_sent", "contract_signed", "won")
    )
    discovery_booked = sum(
        1 for l in leads if _stage_of(l) in
        ("discovery_scheduled", "discovery_booked",
         "discovery_done", "proposal_sent", "contract_signed", "won")
    )
    discovery_done = sum(
        1 for l in leads if _stage_of(l) in
        ("discovery_done", "proposal_sent", "contract_signed", "won")
    )
    proposal_sent = sum(
        1 for l in leads if _stage_of(l) in
        ("proposal_sent", "contract_signed", "won")
    )
    contract_signed = sum(
        1 for l in leads if _stage_of(l) in
        ("contract_signed", "won")
    )

    eng_live = sum(
        1 for e in engagements
        if (e.get("status") or "") in ("kickoff", "in_progress", "running", "review")
    )
    eng_delivered = sum(
        1 for e in engagements
        if (e.get("status") or "") in ("delivered", "invoiced", "closed")
    )

    def _conv(numer: int, denom: int) -> str:
        if denom <= 0:
            return "—"
        pct = round(numer / denom * 100)
        return f"{pct}%"

    return [
        {"key": "new",            "label": "Novos leads",        "count": new_total,
         "conv": "100%",          "from": None,                  "color": "#0c4a6e"},
        {"key": "qualified",      "label": "Qualified",          "count": qualified,
         "conv": _conv(qualified, new_total),                    "from": "new",
         "color": "#0c4a6e"},
        {"key": "discovery_booked","label": "Discovery booked",  "count": discovery_booked,
         "conv": _conv(discovery_booked, qualified),             "from": "qualified",
         "color": "#6d28d9"},
        {"key": "discovery_done", "label": "Discovery done",     "count": discovery_done,
         "conv": _conv(discovery_done, discovery_booked),        "from": "discovery_booked",
         "color": "#6d28d9"},
        {"key": "proposal_sent",  "label": "Proposal sent",      "count": proposal_sent,
         "conv": _conv(proposal_sent, discovery_done),           "from": "discovery_done",
         "color": "#b45309"},
        {"key": "contract_signed","label": "Contract signed",    "count": contract_signed,
         "conv": _conv(contract_signed, proposal_sent),          "from": "proposal_sent",
         "color": "#15803d"},
        {"key": "engagement_live","label": "Engagement live",    "count": eng_live,
         "conv": _conv(eng_live, contract_signed),               "from": "contract_signed",
         "color": "#0f766e"},
        {"key": "delivered",      "label": "Delivered",          "count": eng_delivered,
         "conv": _conv(eng_delivered, max(eng_live + eng_delivered, 1)),
         "from": "engagement_live", "color": "#15803d"},
    ]


def _funnel_link(stage_key: str, token: str) -> str:
    """Where to drill down for a given funnel row."""
    qs = _auth_qs(token).lstrip("?")
    qs_amp = ("&" + qs) if qs else ""
    if stage_key in ("engagement_live", "delivered"):
        return f"/admin/engagements{_auth_qs(token)}"
    # Map to lead bucket filters where possible
    mapping = {
        "qualified": "qualified",
        "discovery_booked": "discovery_scheduled",
        "discovery_done": "discovery_done",
        "proposal_sent": "proposal_sent",
        "contract_signed": "proposal_sent",
        "new": "all",
    }
    bucket = mapping.get(stage_key, "all")
    return f"/admin/leads{_auth_qs(token)}{qs_amp and ''}#bucket={bucket}"


_RANGE_OPTIONS = [(30, "30 dias"), (60, "60 dias"), (90, "90 dias"), (365, "12 meses")]


@router.get("/pipeline")
async def pipeline_view(request: Request):
    token = _check_auth(request)
    try:
        days = int(request.query_params.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    leads = await _fetch_leads_for_pipeline(days=days)
    engagements = await _fetch_engagements_for_pipeline()

    funnel = _build_funnel_counts(leads, engagements)

    max_count = max((row["count"] for row in funnel), default=1) or 1

    rows_html = []
    for row in funnel:
        width = max(2, int(round(row["count"] / max_count * 100)))
        link = _funnel_link(row["key"], token)
        rows_html.append(
            f'<div class="funnel-row">'
            f'<a class="stage-name" href="{link}">{_html.escape(row["label"])}</a>'
            f'<div class="stage-bar"><span style="width:{width}%;background:{row["color"]};"></span></div>'
            f'<div class="stage-count">{row["count"]}</div>'
            f'<div class="stage-conv">{_html.escape(row["conv"])}</div>'
            f'</div>'
        )
    funnel_html = "".join(rows_html)

    qs_base = _auth_qs(token)
    qs_sep = "&" if "?" in qs_base else "?"
    range_chips = "".join(
        f'<a class="filter{" active" if d == days else ""}" '
        f'href="/admin/pipeline{qs_base}{qs_sep}days={d}">'
        f'{_html.escape(label)}</a>'
        for d, label in _RANGE_OPTIONS
    )

    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Conversão</p>
        <h1>Pipeline funnel</h1>
      </div>
      <div class="top-meta">
        Janela: últimos {days} dias para novos leads &middot; engagements no estoque atual
      </div>
    </header>

    <div class="toolbar"><span class="range-picker">{range_chips}</span></div>

    <div class="funnel">
      <div class="funnel-row" style="border-bottom:1px solid var(--line-strong);">
        <div style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Estágio</div>
        <div></div>
        <div style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Total</div>
        <div style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);text-align:right;">Conv.</div>
      </div>
      {funnel_html}
    </div>

    <p class="footer-note">
      Conv. = taxa de conversão do estágio imediatamente anterior. Clique em qualquer
      linha para abrir o drill-down (Leads / Engagements).
    </p>
    """
    return _render_page("Pipeline — Anuvia admin", body, "pipeline", token)


# ---------------------------------------------------------------------------
# /admin/bookings — agenda view
# ---------------------------------------------------------------------------


def _extract_sessions_for_agenda(engagements: list[dict]) -> list[dict]:
    """Flatten engagements.artifacts.sessions into one list of session items."""
    out: list[dict] = []
    for eng in engagements:
        artifacts = eng.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            continue
        sessions = artifacts.get("sessions")
        if not isinstance(sessions, dict):
            continue
        lead = eng.get("lead") or {}
        for phase_key, sess in sessions.items():
            if not isinstance(sess, dict):
                continue
            scheduled = (
                sess.get("scheduled_at")
                or sess.get("scheduled_at_iso")
                or sess.get("when")
            )
            dt = _parse_iso(scheduled)
            out.append({
                "engagement_id": eng.get("id"),
                "lead_name": lead.get("name") or sess.get("client_name") or "—",
                "lead_company": lead.get("company") or "",
                "lead_email": lead.get("email") or sess.get("client_email") or "",
                "practice": eng.get("practice") or "",
                "phase_key": phase_key,
                "scheduled_at": scheduled,
                "scheduled_at_br": sess.get("scheduled_at_br"),
                "dt": dt,
                "duration_min": sess.get("duration_min") or 30,
                "meet_url": sess.get("meet_url"),
                "gcal_html_link": sess.get("gcal_html_link"),
                "brief_url": sess.get("brief_url") or sess.get("operator_brief_url"),
                "brief_snippet": sess.get("operator_brief_snippet") or "",
            })
    out.sort(key=lambda s: s["dt"] or datetime.max.replace(tzinfo=timezone.utc))
    return out


def _agenda_buckets(sessions: list[dict]) -> dict[str, list[dict]]:
    """Group sessions into Hoje / Amanhã / Próximos 7 dias / Futuro / Passado."""
    now = datetime.now(timezone.utc)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)

    buckets = {
        "today": [],
        "tomorrow": [],
        "this_week": [],
        "later": [],
        "past": [],
    }
    for s in sessions:
        dt = s.get("dt")
        if dt is None:
            buckets["later"].append(s)
            continue
        d = dt.astimezone(timezone.utc).date()
        if dt < now - timedelta(hours=2):
            buckets["past"].append(s)
        elif d == today:
            buckets["today"].append(s)
        elif d == tomorrow:
            buckets["tomorrow"].append(s)
        elif d <= week_end:
            buckets["this_week"].append(s)
        else:
            buckets["later"].append(s)
    return buckets


def _render_session_card(s: dict, token: str, past: bool = False) -> str:
    dt = s.get("dt")
    when_text = "—"
    if dt is not None:
        when_text = dt.astimezone(timezone.utc).strftime("%H:%M UTC")
        if s.get("scheduled_at_br"):
            when_text = _html.escape(str(s["scheduled_at_br"]))
    elif s.get("scheduled_at_br"):
        when_text = _html.escape(str(s["scheduled_at_br"]))

    actions: list[str] = []
    if s.get("meet_url"):
        actions.append(
            f'<a class="primary" href="{_html.escape(s["meet_url"])}" target="_blank">Meet ↗</a>'
        )
    if s.get("gcal_html_link"):
        actions.append(
            f'<a href="{_html.escape(s["gcal_html_link"])}" target="_blank">Gcal ↗</a>'
        )
    if s.get("brief_url"):
        actions.append(
            f'<a href="{_html.escape(s["brief_url"])}" target="_blank">Brief ↗</a>'
        )
    if s.get("engagement_id"):
        actions.append(
            f'<a href="/admin/engagement/{_html.escape(str(s["engagement_id"]))}{_auth_qs(token)}">'
            f'Engagement ↗</a>'
        )
    actions_html = "".join(actions) if actions else '<span class="ts">sem links</span>'

    name = s.get("lead_name") or "—"
    company = s.get("lead_company") or ""
    email = s.get("lead_email") or ""
    practice = s.get("practice") or ""
    phase = s.get("phase_key") or ""
    duration = s.get("duration_min") or 30
    meta_bits: list[str] = []
    if company:
        meta_bits.append(_html.escape(company))
    if email:
        meta_bits.append(_html.escape(email))
    meta_bits.append(f"{_html.escape(str(phase))} · {duration}min")
    if practice:
        meta_bits.append(_PRACTICE_LABEL.get(practice, practice))
    meta_html = ' &middot; '.join(meta_bits)

    cls = "session-card past" if past else "session-card"
    return (
        f'<div class="{cls}">'
        f'<div class="when">{when_text}</div>'
        f'<div class="who">'
        f'<div class="name">{_html.escape(name)}</div>'
        f'<div class="meta">{meta_html}</div>'
        f'</div>'
        f'<div class="actions">{actions_html}</div>'
        f'</div>'
    )


def _render_agenda_section(title: str, sessions: list[dict], token: str, past: bool = False) -> str:
    if not sessions:
        return ""
    cards = "".join(_render_session_card(s, token, past=past) for s in sessions)
    return (
        f'<section class="agenda-section">'
        f'<h2>{_html.escape(title)} <span class="count-tag">{len(sessions)} sessão{"ões" if len(sessions) != 1 else ""}</span></h2>'
        f'{cards}'
        f'</section>'
    )


@router.get("/bookings")
async def bookings_view(request: Request):
    token = _check_auth(request)
    engagements = await _fetch_engagements_with_sessions()
    sessions = _extract_sessions_for_agenda(engagements)
    buckets = _agenda_buckets(sessions)

    upcoming_count = (
        len(buckets["today"])
        + len(buckets["tomorrow"])
        + len(buckets["this_week"])
        + len(buckets["later"])
    )

    sections_html = (
        _render_agenda_section("Hoje", buckets["today"], token)
        + _render_agenda_section("Amanhã", buckets["tomorrow"], token)
        + _render_agenda_section("Próximos 7 dias", buckets["this_week"], token)
        + _render_agenda_section("Mais tarde", buckets["later"], token)
        + _render_agenda_section("Concluídas (últimas 30)", buckets["past"][:30], token, past=True)
    )
    if not sections_html.strip():
        sections_html = (
            '<p class="empty">Nenhuma sessão agendada nos engagements ativos.</p>'
        )

    body = f"""
    <header class="top">
      <div>
        <p class="eyebrow">Anuvia · Calendário</p>
        <h1>Bookings</h1>
      </div>
      <div class="top-meta">
        {upcoming_count} sessão(ões) futura(s) &middot;
        atualizado {_fmt_ts(datetime.now(timezone.utc).isoformat())}
      </div>
    </header>
    <p style="color:var(--muted);font-size:13px;">
      Agenda de sessões agendadas via white-glove em
      <code style="font-family:'JetBrains Mono',monospace;font-size:12px;">engagements.artifacts.sessions</code>.
      Clique em Meet para entrar na chamada ou em Brief para ler o pre-call.
    </p>
    {sections_html}
    """
    return _render_page("Bookings — Anuvia admin", body, "bookings", token)
