"""
Engagement lifecycle dashboard — admin-only read view.

Mila uses this to monitor every active engagement at a glance: 1 row per
engagement, columns for client, practice, contract value, phase progress,
status, intake state, delivery mode, last activity, next action. Each row
expands to a detail view showing intake_data, artifacts, deliverables URLs
and booked sessions.

This is **read-only** — no edits, no buttons that mutate state. Purely a
window into the funnel.

Routes (all under /admin):
  GET /admin/engagements?token=<admin>          — list + filter + search
  GET /admin/engagement/{engagement_id}?token=<admin>
                                                — full detail for one row

Auth: same HMAC pattern as lib.admin_smoke._verify_admin_token (HMAC-SHA256
of "admin_smoke" with CONTRACT_HMAC_SECRET). Grab the token via
``GET /api/_admin/smoke/token?key=<key>`` (admin_smoke.py).
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import json
import logging
import os
from datetime import datetime, timezone
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


def _verify_admin_token(token: str) -> bool:
    if not HMAC_SECRET or not token:
        return False
    expected = hmac.new(
        HMAC_SECRET.encode("utf-8"),
        b"admin_smoke",
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Practice → display label + accent color
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
    "review": ("#b45309", "#fef3c7"),
    "delivered": ("#15803d", "#dcfce7"),
    "invoiced": ("#15803d", "#dcfce7"),
    "closed": ("#525252", "#e7e5e4"),
    "cancelled": ("#b91c1c", "#fee2e2"),
}


def _status_badge(status: str) -> str:
    fg, bg = _STATUS_TONE.get(status, ("#525252", "#e7e5e4"))
    if status and status.startswith("blocked"):
        fg, bg = "#b91c1c", "#fee2e2"
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
        # Postgres returns ISO with microseconds + offset. Trim TZ for compact.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value[:16]


def _is_active(status: str) -> bool:
    return status not in ("delivered", "invoiced", "closed", "cancelled")


def _is_blocked(status: str) -> bool:
    return bool(status) and status.startswith("blocked")


def _intake_check_glyph(has_intake: bool) -> str:
    if has_intake:
        return '<span class="check">✓</span>'
    return '<span class="nope">✗</span>'


# ---------------------------------------------------------------------------
# Supabase fetch
# ---------------------------------------------------------------------------


async def _fetch_engagements() -> list[dict]:
    """Fetch all engagements with their lead embedded.

    Uses the PostgREST embedded-resource syntax (works since lead_id has an
    FK constraint to leads.id — see migrations/2026-05-14_contracts.sql).
    """
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


# ---------------------------------------------------------------------------
# HTML scaffold
# ---------------------------------------------------------------------------

_PAGE = """<!DOCTYPE html>
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
         background:var(--bg); color:var(--ink); margin:0; padding:32px 24px;
         line-height:1.5; }}
  .wrap {{ max-width:1320px; margin:0 auto; }}
  header.top {{ display:flex; align-items:baseline; justify-content:space-between;
                margin-bottom:24px; gap:16px; flex-wrap:wrap; }}
  .eyebrow {{ font-size:11px; letter-spacing:0.18em; text-transform:uppercase;
              color:var(--accent); font-weight:600; margin:0 0 4px; }}
  h1 {{ font-family:'Playfair Display', Georgia, serif; font-weight:600;
        font-size:30px; margin:0; letter-spacing:-0.01em; }}
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
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def _render_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=title, body=body), status_code=status)


# ---------------------------------------------------------------------------
# Row rendering
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

    # Phase progress bar
    pct = int(round((current / total) * 100)) if total else 0
    pct = max(0, min(100, pct))

    # Filter dataset attributes (for client-side JS filters)
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

    # Detail row — collapsed by default, expanded inline on click
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
    """Render intake_data as a readable kv list (or a JSON dump fallback)."""
    if not isinstance(intake_data, dict) or not intake_data:
        return '<p class="muted" style="font-size:12px;">Intake não submetido.</p>'

    # Stakeholders rendered separately
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
    """Render artifacts dict — list URLs / files / sessions / timestamps."""
    if not isinstance(artifacts, dict) or not artifacts:
        return '<p class="muted" style="font-size:12px;">Sem artifacts ainda.</p>'

    # Pull common deliverable URL patterns
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

    # Sessions
    sessions = artifacts.get("sessions") or []
    if not sessions and isinstance(artifacts.get("phase_sessions"), list):
        sessions = artifacts["phase_sessions"]
    sess_html = ""
    if sessions:
        s_items = "".join(
            f'<li><span class="ts">{_html.escape(str(s.get("at") or s.get("when") or ""))}</span> '
            f'— {_html.escape(str(s.get("title") or s.get("phase") or ""))}</li>'
            for s in sessions if isinstance(s, dict)
        )
        if s_items:
            sess_html = (
                '<h3 style="margin-top:14px;">Sessões agendadas</h3>'
                f'<ul style="font-size:12px;padding-left:18px;margin:6px 0;">{s_items}</ul>'
            )

    # JSON dump as fallback
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
    """Detail panel — rendered inline (after the row) AND on standalone page."""
    eid = eng.get("id", "")
    intake_data = eng.get("intake_data") or {}
    artifacts = eng.get("artifacts") or {}
    lead = eng.get("lead") or {}

    # Header strip
    lead_name = lead.get("name") or "—"
    lead_email = lead.get("email") or "—"
    lead_company = lead.get("company") or "—"
    standalone_link = (
        f'<a class="detail-link" href="/admin/engagement/{_html.escape(eid)}'
        f'?token={_html.escape(token)}" target="_blank">'
        f'abrir em nova aba ↗</a>'
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
# Filter / search JS (inline)
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
    var q = (searchInput.value || "").toLowerCase().trim();
    var shown = 0;
    rows.forEach(function(r){
      var bucket = r.getAttribute("data-bucket");
      var blob = r.getAttribute("data-search") || "";
      var bucketOk = (current === "all") ||
                     (current === "active" && bucket === "active") ||
                     (current === "blocked" && bucket === "blocked") ||
                     (current === "delivered" && bucket === "delivered");
      var qOk = !q || blob.indexOf(q) !== -1;
      var show = bucketOk && qOk;
      r.style.display = show ? "" : "none";
      // Hide its detail too
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

  searchInput.addEventListener("input", applyFilters);

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
# Routes
# ---------------------------------------------------------------------------


@router.get("/engagements")
async def engagements_list(request: Request):
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")

    engagements = await _fetch_engagements()

    # Bucket counts
    total = len(engagements)
    active = sum(1 for e in engagements if _is_active(e.get("status") or ""))
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
    return _render_page("Engagements — Anuvia admin", body)


@router.get("/engagement/{engagement_id}")
async def engagement_detail(engagement_id: str, request: Request):
    token = request.query_params.get("token", "")
    if not _verify_admin_token(token):
        raise HTTPException(401, "bad admin token")
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
        <a class="detail-link" href="/admin/engagements?token={_html.escape(token)}">← voltar</a>
      </div>
    </header>
    <div style="background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:24px;">
      {_render_detail_body(eng, token)}
    </div>
    """
    return _render_page("Engagement detail — Anuvia admin", body)
