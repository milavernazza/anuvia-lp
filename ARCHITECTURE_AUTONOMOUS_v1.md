# Anuvia Autonomous Funnel — Architecture v1

**Goal:** visitor → discovery-call (Track A) OR closed deal (Track B) without human intervention, with shared agent memory and 24/7 reliability.

**Date:** 2026-05-13
**Status:** Building. Four parallel sub-agents executing in isolated modules.

---

## 1. Unified Lead Session — the shared memory contract

We extend the existing `leads` table (Supabase Postgres). Every agent reads/writes the same row.

### Schema additions

```sql
ALTER TABLE leads ADD COLUMN IF NOT EXISTS track text;
  -- 'discovery' | 'autonomous' | NULL (not yet classified)

ALTER TABLE leads ADD COLUMN IF NOT EXISTS lifecycle_status text DEFAULT 'new';
  -- new | qualified | in_discovery | discovery_booked | discovery_done
  -- | proposal_sent | proposal_opened | proposal_signed
  -- | won | lost | ghosted | error

ALTER TABLE leads ADD COLUMN IF NOT EXISTS next_action text;
  -- Free-form key registered in orchestrator dispatcher.
  -- Examples: 'classify_track', 'send_diagnostic_email', 'generate_proposal_v1',
  -- 'followup_proposal_d2', 'followup_proposal_d5', 'close_ghosted_d10'

ALTER TABLE leads ADD COLUMN IF NOT EXISTS next_action_at timestamptz;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_touch_at timestamptz DEFAULT now();

ALTER TABLE leads ADD COLUMN IF NOT EXISTS agent_history jsonb DEFAULT '[]'::jsonb;
  -- Append-only log. Each entry:
  -- { ts: ISO8601, agent: 'orchestrator'|'track_b'|'proposal'|'followup'|...,
  --   action: 'generate_proposal_v1', result: 'ok'|'retry'|'failed',
  --   detail: string, error: string|null, latency_ms: int }

ALTER TABLE leads ADD COLUMN IF NOT EXISTS artifacts jsonb DEFAULT '[]'::jsonb;
  -- [{ ts, type: 'brief'|'proposal_pdf'|'email_sent'|'contract',
  --    url: string|null, meta: {...} }]

ALTER TABLE leads ADD COLUMN IF NOT EXISTS signals jsonb DEFAULT '[]'::jsonb;
  -- [{ ts, kind: 'email_open'|'email_click'|'proposal_view'|'reply'|'page_view',
  --    value: string, source: string }]

CREATE INDEX IF NOT EXISTS leads_next_action_at_idx
  ON leads (next_action_at)
  WHERE next_action_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS leads_lifecycle_status_idx
  ON leads (lifecycle_status);
```

### Lifecycle state machine

```
new
 └── classify_track ──► track='discovery'  → in_discovery → discovery_booked → discovery_done
                  ──► track='autonomous'   → qualified    → proposal_sent     → proposal_opened
                                                                              → proposal_signed → won
                                                                              → ghosted | lost
```

`next_action_at` is the orchestrator's queue key. Agents NEVER call each other directly — they update `next_action` + `next_action_at` and the orchestrator fires the next handler.

---

## 2. Module layout (parallel-safe)

```
04-agents/lp/
├── app.py                            # main FastAPI app (existing; only add include_router)
├── lib/
│   ├── __init__.py
│   ├── sessions.py                   # Agent A: schema helpers + session API
│   ├── orchestrator.py               # Agent B: tick endpoint + handler registry
│   └── track_b.py                    # Agent C: classify + autonomous-close handlers
├── migrations/
│   └── 2026-05-13_lead_sessions.sql  # Agent A
├── scripts/
│   └── smoke_e2e.py                  # Agent D
└── ARCHITECTURE_AUTONOMOUS_v1.md     # this file
```

Each module exposes `router = APIRouter(prefix=..., tags=[...])`. `app.py` mounts via `app.include_router(router)`. No agent edits `app.py` directly except adding the include statements.

---

## 3. Module contracts

### `lib/sessions.py` — Agent A

Functions (all use Supabase REST via `httpx`, NOT direct psycopg):

```python
async def session_get(lead_id: str) -> dict | None
async def session_create_or_get(email: str, **fields) -> dict
async def session_update(lead_id: str, **fields) -> dict
async def session_append_history(lead_id: str, agent: str, action: str,
                                  result: str, detail: str = "",
                                  error: str | None = None,
                                  latency_ms: int = 0) -> None
async def session_append_artifact(lead_id: str, type: str,
                                   url: str | None = None,
                                   meta: dict | None = None) -> None
async def session_append_signal(lead_id: str, kind: str,
                                 value: str = "", source: str = "") -> None
async def session_set_next(lead_id: str, next_action: str | None,
                            next_action_at: datetime | None) -> None
async def session_set_status(lead_id: str, status: str) -> None
async def session_due(limit: int = 50) -> list[dict]
  # SELECT * FROM leads WHERE next_action IS NOT NULL
  # AND next_action_at <= now() ORDER BY next_action_at LIMIT 50
```

Also exposes `router = APIRouter(prefix="/api/session", tags=["sessions"])` with:
- `GET /api/session/{lead_id}` → full session (admin-only, behind CF Access)

Migration SQL goes in `migrations/2026-05-13_lead_sessions.sql`. Must be idempotent (`IF NOT EXISTS`).

### `lib/orchestrator.py` — Agent B

```python
from lib.sessions import session_due, session_append_history, session_set_next, session_set_status

HANDLERS: dict[str, Callable[[dict], Awaitable[dict]]] = {}

def register(name: str):
    """Decorator. Handlers must be async, take a lead dict,
    return {'next_action': str|None, 'next_action_at': datetime|None,
            'status': str|None, 'detail': str}."""
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco

async def tick(limit: int = 50) -> dict:
    """Pull due leads, dispatch handlers, retry 3x with backoff,
    alert Slack on 3rd failure, update agent_history every call."""
    ...

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])

@router.post("/tick")
async def http_tick(secret: str, limit: int = 50):
    """Called by external cron (n8n or Coolify cron). Secret matches env ORCHESTRATOR_SECRET."""
    ...
```

Slack alert: POST to `os.environ['SLACK_ALERTS_WEBHOOK']` (fallback to existing `SLACK_NEW_LEAD_WEBHOOK`).

### `lib/track_b.py` — Agent C

```python
from lib.orchestrator import register
from lib.sessions import *

def classify_track(lead: dict) -> str:
    """Returns 'discovery' or 'autonomous'.
    
    Autonomous IF:
      - track originally 'BR_GROWTH' or 'US_GROWTH', AND
      - estimated deal size < 8000 USD (or 40000 BRL), AND
      - qualification_data shows: budget_declared=true OR urgency=high OR team_size <= 10
    
    Otherwise 'discovery'.
    """

@register('classify_track')
async def h_classify_track(lead): ...

@register('generate_proposal_v1')
async def h_generate_proposal_v1(lead):
    """Build proposal HTML, render to PDF via existing Gotenberg sidecar,
    send via Resend, append artifact, set next_action='followup_proposal_d2'
    at now() + 2 days."""

@register('followup_proposal_d2')
async def h_followup_proposal_d2(lead):
    """If proposal_opened or replied → set next_action=None, status='proposal_opened'.
    Else send nudge email, set next_action='followup_proposal_d5' at now()+3 days."""

@register('followup_proposal_d5')
async def h_followup_proposal_d5(lead):
    """Final nudge. Sets next_action='close_ghosted_d10' at now()+5 days."""

@register('close_ghosted_d10')
async def h_close_ghosted_d10(lead):
    """Marks status='ghosted', next_action=None, stop."""

router = APIRouter(prefix="/api/track-b", tags=["track-b"])
# Webhook endpoint to receive Resend open/click events:
# POST /api/track-b/email-event → updates signals
```

Also: the existing `/contact` endpoint must call `classify_track()` after creating the lead. **This requires a small edit to `app.py`** — I will do this myself after Agent C delivers, NOT Agent C.

### `scripts/smoke_e2e.py` — Agent D

Standalone async script. Run via `python scripts/smoke_e2e.py` or scheduled via cron.

Steps:
1. POST `/analyze` with synthetic payload (use practice=cloud, BR locale)
2. Assert HTML response contains expected sections
3. POST `/contact` with synthetic email `smoke+{ts}@anuvia.test`
4. Assert 200 + Supabase row exists with PII
5. GET `/api/slots?days=14` → assert ≥ 1 slot
6. POST `/api/contact-book` with the first slot → assert booking created
7. Query Supabase: assert `lifecycle_status='discovery_booked'` and `artifacts` includes the brief
8. Cleanup: DELETE the synthetic lead row (`email LIKE 'smoke+%@anuvia.test'`)

If any step fails: POST to Slack `SLACK_ALERTS_WEBHOOK` with "Smoke E2E FAIL at step N: {error}" and exit 1.

Optional flag `--track autonomous` runs Track B path instead (skip booking, wait for proposal artifact).

Cron: every day at 7am BRT. Wire via existing n8n cron or new Coolify task.

---

## 4. Integration responsibility (me, not subagents)

After the 4 subagents complete, I do:

1. Add to `app.py`:
   ```python
   from lib.sessions import router as sessions_router
   from lib.orchestrator import router as orchestrator_router, register  # noqa: F401
   from lib.track_b import router as track_b_router, classify_track
   import lib.track_b  # ensure handlers register on import
   
   app.include_router(sessions_router)
   app.include_router(orchestrator_router)
   app.include_router(track_b_router)
   ```

2. Edit existing `/contact` POST handler to call `classify_track(lead)` immediately after lead row is created, set `track`, `next_action='classify_track'`, `next_action_at=now()`.

3. Run migration SQL against Supabase.

4. Set env vars in Coolify: `ORCHESTRATOR_SECRET`, `SLACK_ALERTS_WEBHOOK`.

5. Add cron in n8n: `*/10 * * * *` → POST `/api/orchestrator/tick?secret=...`.

6. Add cron in n8n: `0 7 * * *` → run smoke E2E.

7. Smoke test the whole loop with a real synthetic lead.

---

## 5. Constraints all agents must respect

- **Idempotency.** Re-running a handler must not duplicate emails, artifacts, or rows. Use deterministic external IDs where possible (e.g., proposal_id = `lead_id + '_v1'`).
- **Append-only history.** Never overwrite `agent_history`, `artifacts`, `signals`. Always read-modify-write the jsonb array with the current value.
- **Retries.** Network calls wrapped in `httpx_with_retry(url, ..., max_attempts=3, backoff=2)`. Failures after 3 attempts → mark row `lifecycle_status='error'`, append history with `result='failed'`, alert Slack.
- **No direct cross-module imports of handlers.** Track B handlers call only `lib.sessions` + external APIs. Orchestrator dispatches by name string lookup.
- **Test fixtures.** Smoke test uses `email LIKE 'smoke+%@anuvia.test'` so they're trivially identified and deleted.
- **Lang-aware.** Email subject/body, proposal copy must respect `leads.language` (pt|en).
