-- 2026-05-13 — Lead Sessions: shared agent memory for autonomous funnel.
-- Re-runnable. Extends existing `leads` table; never drops or recreates it.

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
