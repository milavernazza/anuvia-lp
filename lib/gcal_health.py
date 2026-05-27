"""Gcal health check — proactive monitoring so refresh-token expiry never
breaks the booking workflow silently.

A scheduled job (see ``mcp__scheduled-tasks``) hits ``/api/admin/gcal/health``
every morning. Any ``red`` account triggers a Slack alert with the
one-click reconnect URL so reconnection happens BEFORE the next client
tries to book.

Mounted onto the main FastAPI app via:

    from lib.gcal_health import router as gcal_health_router
    app.include_router(gcal_health_router)

Status grades per account:
    * ``green``  — refresh→access exchange succeeded in the last call
    * ``red``    — exchange failed (refresh_token revoked / expired)
    * ``yellow`` — account exists but is_active=False (intentionally disabled)
"""

from __future__ import annotations

import logging
import os
import time as _t
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("anuvia-gcal-health")

router = APIRouter(prefix="/api/admin/gcal", tags=["gcal-health"])


def _admin_auth_check(request: Request) -> None:
    """Reuse the same ADMIN_API_KEY gate as the rest of app.py.

    Accepts ``?key=`` query string OR ``Authorization: Bearer`` header.
    """
    key = (
        request.query_params.get("key", "")
        or request.headers.get("authorization", "").replace("Bearer ", "")
    )
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status_code=401, detail="bad admin key")


async def _exchange_refresh(client: httpx.AsyncClient, refresh_token: str):
    """Stripped-down refresh→access exchange (copy of app._exchange_refresh_token).

    Kept local so this module is importable even before app.py imports it.
    """
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (cid and csec and refresh_token):
        return None
    try:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": cid,
                "client_secret": csec,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
        log.warning(
            "gcal_health: refresh non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
    except Exception:  # noqa: BLE001
        log.exception("gcal_health: refresh crashed")
    return None


async def _probe_gcal_account(
    client: httpx.AsyncClient, account: dict
) -> dict:
    """Single-account probe: tries the refresh→access exchange.

    NEVER raises — caller relies on best-effort summarisation.
    """
    email = account.get("email") or "(unknown)"
    refresh_token = account.get("refresh_token") or ""
    is_active = bool(account.get("is_active", False))
    cal_id = account.get("calendar_id") or "primary"
    base = {
        "email": email,
        "calendar_id": cal_id,
        "is_active": is_active,
    }
    if not is_active:
        return {**base, "status": "yellow", "reason": "inactive"}
    if not refresh_token:
        return {**base, "status": "red", "reason": "no_refresh_token"}
    t0 = _t.time()
    try:
        token = await _exchange_refresh(client, refresh_token)
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "status": "red",
            "reason": f"exchange_exception: {type(exc).__name__}",
            "latency_ms": int((_t.time() - t0) * 1000),
        }
    if not token:
        return {
            **base,
            "status": "red",
            "reason": "exchange_returned_none",
            "latency_ms": int((_t.time() - t0) * 1000),
        }
    return {
        **base,
        "status": "green",
        "latency_ms": int((_t.time() - t0) * 1000),
    }


async def _run_gcal_health(send_alerts: bool = False) -> dict:
    """Probe every Gcal account. Optionally Slack-alert on any ``red``.

    Returns ``{ok, overall, summary, accounts, alerts_sent}``.
    """
    supa_url = os.environ.get("SUPABASE_URL", "")
    supa_key = (
        os.environ.get("SUPABASE_SERVICE_KEY", "")
        or os.environ.get("SUPABASE_KEY", "")
    )
    if not (supa_url and supa_key):
        return {
            "ok": False,
            "reason": "supabase_env_missing",
            "overall": "red",
        }
    headers = {
        "apikey": supa_key,
        "Authorization": f"Bearer {supa_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{supa_url}/rest/v1/admin_gcal_accounts"
            "?select=id,email,refresh_token,calendar_id,is_active",
            headers=headers,
        )
        accounts = r.json() if r.status_code == 200 else []
        results = []
        for a in accounts:
            results.append(await _probe_gcal_account(client, a))

    red = [r for r in results if r.get("status") == "red"]
    green = [r for r in results if r.get("status") == "green"]
    yellow = [r for r in results if r.get("status") == "yellow"]
    overall = (
        "red" if red
        else ("green" if green else "yellow")
    )

    alerts_sent = 0
    if send_alerts and red:
        try:
            from lib.orchestrator import _send_slack_alert  # type: ignore
        except Exception:  # noqa: BLE001
            _send_slack_alert = None  # type: ignore
        if _send_slack_alert is not None:
            base_url = (
                os.environ.get("BASE_URL") or "https://anuvia.com.br"
            ).rstrip("/")
            admin_key = os.environ.get("ADMIN_API_KEY", "")
            for entry in red:
                em = entry.get("email", "?")
                reason = entry.get("reason", "?")
                reconnect = (
                    f"{base_url}/api/admin/gcal/connect"
                    f"?key={admin_key}&email={em}"
                ) if admin_key else (
                    f"{base_url}/api/admin/gcal/connect?email={em}"
                )
                try:
                    await _send_slack_alert(
                        ":rotating_light: *Gcal token expired* — "
                        f"`{em}` está em estado `red` "
                        f"(motivo: `{reason}`). "
                        "Reconectar antes do próximo cliente real: "
                        f"<{reconnect}|Reconectar agora>"
                    )
                    alerts_sent += 1
                except Exception:  # noqa: BLE001
                    log.exception(
                        "gcal_health: slack alert failed for %s", em
                    )

    return {
        "ok": True,
        "overall": overall,
        "summary": {
            "total": len(results),
            "green": len(green),
            "yellow": len(yellow),
            "red": len(red),
        },
        "accounts": results,
        "alerts_sent": alerts_sent,
    }


@router.get("/health")
async def admin_gcal_health(request: Request):
    """Probe every active Gcal account's refresh→access exchange.

    Query param ``alert=1`` also Slack-alerts on any ``red`` accounts.
    """
    _admin_auth_check(request)
    send_alerts = (request.query_params.get("alert", "0") or "0").strip() in (
        "1", "true", "yes"
    )
    result = await _run_gcal_health(send_alerts=send_alerts)
    return JSONResponse(result)


@router.get("/health/view", response_class=HTMLResponse)
async def admin_gcal_health_view(request: Request):
    """Browser-friendly health view — Mila opens this to sanity-check token state."""
    _admin_auth_check(request)
    result = await _run_gcal_health(send_alerts=False)
    rows = []
    base_url = (
        os.environ.get("BASE_URL") or "https://anuvia.com.br"
    ).rstrip("/")
    key = request.query_params.get("key", "")
    for a in result.get("accounts", []):
        status = a.get("status", "?")
        color = {
            "green": "#15803d", "yellow": "#a16207", "red": "#b91c1c",
        }.get(status, "#525252")
        bg = {
            "green": "#dcfce7", "yellow": "#fef3c7", "red": "#fee2e2",
        }.get(status, "#f5f5f4")
        em = a.get("email", "?")
        reconnect = (
            f"{base_url}/api/admin/gcal/connect?key={key}&email={em}"
        )
        rows.append(
            f"<tr><td><strong>{em}</strong>"
            f"<div style='font-size:11px;color:#78716c;'>"
            f"{a.get('calendar_id','primary')}</div></td>"
            f"<td><span style='display:inline-block;padding:2px 8px;"
            f"border-radius:9999px;background:{bg};color:{color};"
            f"font-size:11px;letter-spacing:0.04em;"
            f"text-transform:uppercase;'>{status}</span>"
            f"<div style='font-size:11px;color:#78716c;margin-top:4px;'>"
            f"{a.get('reason','')}</div></td>"
            f"<td style='font-family:JetBrains Mono,monospace;font-size:11px;"
            f"color:#78716c;'>{a.get('latency_ms','—')} ms</td>"
            f"<td><a class='detail-link' href='{reconnect}'>"
            f"Reconectar</a></td></tr>"
        )
    overall = result.get("overall", "?")
    overall_color = {
        "green": "#15803d", "yellow": "#a16207", "red": "#b91c1c",
    }.get(overall, "#525252")
    summary = result.get("summary", {"green": 0, "yellow": 0, "red": 0})
    html = (
        "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<title>Anuvia · Gcal health</title>"
        "<style>body{font-family:Inter,-apple-system,sans-serif;"
        "background:#fafaf9;color:#1a1a1a;margin:0;padding:48px 24px;"
        "line-height:1.55;}.card{max-width:780px;margin:0 auto;"
        "background:#fff;border:1px solid #e7e5e4;border-radius:12px;"
        "padding:32px;}h1{font-family:Georgia,serif;font-size:24px;"
        "font-weight:500;margin:0 0 6px;}.eyebrow{font-size:11px;"
        "letter-spacing:0.18em;text-transform:uppercase;color:#0c4a6e;"
        "margin:0 0 6px;}table{width:100%;border-collapse:collapse;"
        "margin-top:18px;}th,td{text-align:left;padding:10px 8px;"
        "border-bottom:1px solid #e7e5e4;font-size:13px;"
        "vertical-align:top;}th{font-size:11px;letter-spacing:0.04em;"
        "text-transform:uppercase;color:#78716c;font-weight:500;}"
        f".pill{{display:inline-block;padding:3px 10px;border-radius:9999px;"
        f"background:{overall_color}22;color:{overall_color};font-size:12px;"
        "letter-spacing:0.05em;text-transform:uppercase;}"
        ".detail-link{color:#0c4a6e;text-decoration:none;"
        "border-bottom:1px solid #0c4a6e44;font-size:12px;}"
        "</style></head><body><div class='card'>"
        "<p class='eyebrow'>Anuvia · monitoring</p>"
        "<h1>Google Calendar — health</h1>"
        f"<p style='color:#78716c;font-size:13px;'>Status geral: "
        f"<span class='pill'>{overall}</span> · "
        f"{summary['green']} verdes · {summary['yellow']} amarelos · "
        f"{summary['red']} vermelhos</p>"
        "<table><thead><tr><th>Conta</th><th>Status</th>"
        "<th>Latência</th><th></th></tr></thead><tbody>"
        + (
            "".join(rows)
            or "<tr><td colspan=4 style='color:#78716c;'>"
            "Sem contas registradas.</td></tr>"
        )
        + "</tbody></table></div></body></html>"
    )
    return HTMLResponse(html)
