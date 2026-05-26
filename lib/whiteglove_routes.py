"""White-glove delivery — Slack button → release client materials.

Companion to :mod:`lib.delivery._sessions`. When Mila clicks the
``Apresentei → enviar materiais ao cliente`` button on the Slack DM, the
Slack message routes to this endpoint, which:

  1. Verifies the HMAC token (purpose: ``release:{engagement_id}:{phase}``).
  2. Calls :func:`lib.delivery._sessions.send_client_materials` to fire
     the artifacts email (uses the existing ``_phaseN_email_html``
     templates so the email is identical to the legacy autonomous mode).
  3. Stamps ``phase_N_email_sent_at`` on the engagement (done inside
     ``send_client_materials`` — guarantees idempotency).
  4. Slack DMs Mila a confirmation: ``Email enviado pro cliente``.
  5. Returns a simple HTML page (the button URL is GET-only so Slack's
     in-app browser handles the click without an OAuth roundtrip).

The endpoint is admin-only by virtue of the HMAC token — only the Slack
DM holds a freshly-minted token, and tokens are scoped per
(engagement_id, phase).

Route registered on ``app.py`` import: ``app.include_router(router)``
(see the bottom of ``app.py``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from lib.delivery._sessions import (
    send_client_materials,
    slack_dm_text,
    verify_release_token,
)

log = logging.getLogger("anuvia-whiteglove")

router = APIRouter(prefix="/api/_admin", tags=["whiteglove"])


# ---------------------------------------------------------------------------
# Tiny HTML shell — Slack opens the URL in its in-app browser, so we render
# an unstyled-but-friendly confirmation page. NO need for Tailwind / fonts.
# ---------------------------------------------------------------------------


def _html_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = f"""<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family:-apple-system,Inter,sans-serif;background:#fafaf9;color:#1a1a1a;
          margin:0;padding:48px 24px;line-height:1.6;}}
  .card {{ max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;
          border-radius:12px;padding:32px;}}
  .eyebrow {{ font-size:11px;letter-spacing:0.18em;text-transform:uppercase;
            color:#0c4a6e;margin:0 0 6px;}}
  h1 {{ font-family:Georgia,serif;font-size:24px;margin:0 0 14px;color:#0f172a;}}
  p {{ color:#475569;margin:0 0 12px;}}
  .ok {{ color:#15803d;font-weight:600;}}
  .err {{ color:#b91c1c;font-weight:600;}}
  code {{ background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px;}}
</style></head><body>
<div class="card">
  <p class="eyebrow">Anuvia · White-glove delivery</p>
  {body}
</div></body></html>"""
    return HTMLResponse(html, status_code=status)


# ---------------------------------------------------------------------------
# GET /api/_admin/whiteglove/release/{engagement_id}/{phase}?token=<hmac>
# ---------------------------------------------------------------------------


@router.get("/whiteglove/release/{engagement_id}/{phase}")
async def release_materials(
    engagement_id: str, phase: int, request: Request
) -> HTMLResponse:
    """Slack-button handler. Mila clicks → this fires the client email.

    HMAC-protected. Idempotent: re-clicking after a successful send returns
    the ``already_sent`` confirmation page and does not re-fire Resend.
    """
    token = request.query_params.get("token", "")
    if not verify_release_token(engagement_id, int(phase), token):
        return _html_page(
            "Token inválido",
            f'<h1>Token inválido ou expirado</h1>'
            f'<p>Engagement: <code>{engagement_id}</code></p>'
            f'<p>Phase: <code>{phase}</code></p>'
            f'<p class="err">Não foi possível validar o link. '
            f"Verifique se você abriu o botão Slack mais recente.</p>",
            status=401,
        )

    try:
        result = await send_client_materials(str(engagement_id), int(phase))
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "whiteglove: send_client_materials crashed eng=%s phase=%s",
            engagement_id, phase,
        )
        return _html_page(
            "Erro ao enviar",
            f'<h1>Erro no envio</h1>'
            f'<p>Engagement: <code>{engagement_id}</code> · Phase: '
            f'<code>{phase}</code></p>'
            f'<p class="err">{type(exc).__name__}: {exc}</p>'
            f'<p>Materiais não foram enviados ao cliente. Veja os logs '
            f"do servidor pra detalhes.</p>",
            status=500,
        )

    if not result.get("ok"):
        reason = result.get("reason") or "unknown"
        # Slack-notify so Mila sees the failure without checking logs.
        try:
            await slack_dm_text(
                f":x: White-glove release falhou — engagement "
                f"`{engagement_id}` phase `{phase}`. Motivo: `{reason}`."
            )
        except Exception:  # noqa: BLE001
            pass
        return _html_page(
            "Não foi possível enviar",
            f'<h1>Falha ao liberar materiais</h1>'
            f'<p>Engagement: <code>{engagement_id}</code> · Phase: '
            f'<code>{phase}</code></p>'
            f'<p class="err">Motivo: {reason}</p>',
            status=400,
        )

    # Already sent earlier? Idempotent path — no second Slack alert.
    if result.get("reason") == "already_sent":
        return _html_page(
            "Materiais já entregues",
            f'<h1>Materiais já entregues</h1>'
            f'<p>Engagement: <code>{engagement_id}</code> · Phase: '
            f'<code>{phase}</code></p>'
            f'<p class="ok">✓ Email enviado em {result.get("sent_at")}</p>'
            f"<p>Nenhuma ação extra necessária.</p>",
        )

    # Happy path — fresh send. Fire the confirmation Slack DM.
    msg_to = result.get("to") or "(cliente)"
    sent_at = result.get("sent_at") or datetime.now(timezone.utc).isoformat()
    try:
        await slack_dm_text(
            f":white_check_mark: Email enviado pro cliente — engagement "
            f"`{engagement_id}` phase `{phase}` → {msg_to} "
            f"(em {sent_at})."
        )
    except Exception:  # noqa: BLE001
        log.warning("whiteglove: confirmation slack DM failed (non-fatal)")

    return _html_page(
        "Materiais enviados",
        f'<h1>Email enviado pro cliente</h1>'
        f'<p>Engagement: <code>{engagement_id}</code> · Phase: '
        f'<code>{phase}</code></p>'
        f'<p class="ok">✓ Materiais entregues a {msg_to} em {sent_at}</p>'
        f'<p>O cliente recebeu o email com os PDFs / PPTX da fase. '
        f"Bom trabalho, Mila.</p>",
    )


# ---------------------------------------------------------------------------
# GET /api/_admin/whiteglove/release/{engagement_id}/{phase}/status
# Convenience JSON endpoint for debugging — returns whether the email has
# been released without firing it.
# ---------------------------------------------------------------------------


@router.get("/whiteglove/release/{engagement_id}/{phase}/status")
async def release_status(
    engagement_id: str, phase: int, request: Request
) -> JSONResponse:
    """Read-only JSON dump of the release state. HMAC-protected."""
    token = request.query_params.get("token", "")
    if not verify_release_token(engagement_id, int(phase), token):
        return JSONResponse({"ok": False, "reason": "bad_token"}, status_code=401)

    from lib.delivery._sessions import _engagement_get  # type: ignore

    eng = await _engagement_get(str(engagement_id))
    if not eng:
        return JSONResponse(
            {"ok": False, "reason": "engagement_not_found"}, status_code=404
        )
    artifacts = eng.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    sent_key = f"phase_{int(phase)}_email_sent_at"
    pending_key = f"phase_{int(phase)}_pending_presentation_at"
    sessions_map = artifacts.get("sessions") or {}
    if not isinstance(sessions_map, dict):
        sessions_map = {}
    return JSONResponse({
        "ok": True,
        "engagement_id": str(engagement_id),
        "phase": int(phase),
        "sent_at": artifacts.get(sent_key),
        "pending_presentation_at": artifacts.get(pending_key),
        "session": sessions_map.get(f"phase_{int(phase)}"),
    })
