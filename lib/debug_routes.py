"""Debug routes — admin-only diagnostics for third-party integrations.

These endpoints exist to surface raw responses from external APIs (Resend,
Stripe, Mercado Pago, Google) when something fails silently in the
production flow. They never run automatically — only when Mila hits them
manually to diagnose a specific problem.

Mounted in :mod:`app` via ``app.include_router(debug_router)``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("anuvia-debug")

router = APIRouter(prefix="/api/_admin/debug", tags=["debug"])


def _admin_auth(request: Request) -> None:
    key = (
        request.query_params.get("key", "")
        or request.headers.get("authorization", "").replace("Bearer ", "")
    )
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected or key != expected:
        raise HTTPException(status_code=401, detail="bad admin key")


@router.post("/resend_test")
async def debug_resend_test(request: Request):
    """Send a test email via Resend and return the FULL response."""
    _admin_auth(request)

    body_in = await request.json()
    to = (body_in.get("to") or "").strip()
    if not to:
        raise HTTPException(400, "to required")

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
    from_name = os.environ.get("RESEND_FROM_NAME", "Anuvia · Mila Vernazza")
    reply_to_email = os.environ.get(
        "RESEND_REPLY_TO_EMAIL", "mila@anuvia.com.br"
    )
    reply_to_name = os.environ.get(
        "RESEND_REPLY_TO_NAME", "Anuvia · Mila Vernazza"
    )

    if not api_key:
        return JSONResponse({
            "ok": False,
            "reason": "RESEND_API_KEY env var is empty",
            "from_address": f"{from_name} <{from_email}>",
        })

    payload = {
        "from": f"{from_name} <{from_email}>",
        "to": [to],
        "reply_to": f"{reply_to_name} <{reply_to_email}>",
        "subject": "Anuvia · Resend debug test",
        "html": (
            "<p>This is a debug test email from Anuvia's diagnostic "
            "endpoint. If you receive this, Resend delivery is working "
            f"for the recipient <code>{to}</code>.</p>"
            "<p>If you don't receive it, check Resend's dashboard for the "
            "actual delivery status (bounced, delayed, suppressed).</p>"
        ),
        "tags": [{"name": "category", "value": "debug_test"}],
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        log.exception("debug_resend_test: network failed")
        return JSONResponse({
            "ok": False,
            "reason": "network_exception",
            "exception": f"{type(exc).__name__}: {exc}",
            "from_address": payload["from"],
        })

    body_text = r.text or ""
    try:
        body_json = r.json() if body_text else None
    except Exception:
        body_json = None

    return JSONResponse({
        "ok": 200 <= r.status_code < 300,
        "status": r.status_code,
        "body_json": body_json,
        "body_text": body_text[:500] if not body_json else None,
        "from_address": payload["from"],
        "reply_to": payload["reply_to"],
        "to": to,
        "api_key_prefix": api_key[:8] + "...",
    })


@router.get("/resend_config")
async def debug_resend_config(request: Request):
    """Surface the current Resend env config without exposing the API key."""
    _admin_auth(request)
    api_key = os.environ.get("RESEND_API_KEY", "")
    return JSONResponse({
        "ok": True,
        "RESEND_API_KEY_set": bool(api_key),
        "RESEND_API_KEY_prefix": api_key[:8] + "..." if api_key else None,
        "RESEND_FROM_EMAIL": os.environ.get(
            "RESEND_FROM_EMAIL", "(unset — defaults to onboarding@resend.dev)"
        ),
        "RESEND_FROM_NAME": os.environ.get(
            "RESEND_FROM_NAME", "(unset — defaults to 'Anuvia · Mila Vernazza')"
        ),
        "RESEND_REPLY_TO_EMAIL": os.environ.get(
            "RESEND_REPLY_TO_EMAIL", "(unset — defaults to mila@anuvia.com.br)"
        ),
        "RESEND_REPLY_TO_NAME": os.environ.get(
            "RESEND_REPLY_TO_NAME", "(unset)"
        ),
    })
