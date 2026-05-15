-- 2026-05-14 — Prospects table for cold outbound engine.
-- Re-runnable. Owned by Agent A1 (outbound). Per
-- ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Outbound contract".
--
-- Prospects live outside the `leads` table because most never reply. When a
-- prospect engages they get promoted to a real lead row via
-- `lib.reply_classify` (Agent A4) and `prospects.converted_lead_id` is set.

CREATE TABLE IF NOT EXISTS prospects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL,
  first_name TEXT,
  last_name TEXT,
  title TEXT,
  company TEXT,
  company_size_band TEXT,
  vertical TEXT,
  country TEXT,
  enriched_data JSONB DEFAULT '{}'::jsonb,
  icp_score INT,
  practice_fit TEXT,
    -- which practice this prospect best matches:
    -- 'finops' | 'ai' | 'devops' | 'growth' | 'industry'
  source TEXT,
    -- 'apollo' | 'manual_csv' | 'linkedin' | 'referral' | etc
  status TEXT DEFAULT 'new',
    -- Status enum (owned by this module; A4 reads it):
    --   new              — just imported, no touch sent yet
    --   sequence_running — at least one touch sent, sequence active
    --   replied          — prospect replied; A4 took over
    --   bounced          — Resend reported email.bounced
    --   complained       — Resend reported email.complained
    --   unsubscribed     — prospect clicked unsubscribe or asked to stop
    --   converted        — promoted to a lead (see converted_lead_id)
    --   stopped          — exhausted 3-touch sequence with no reply
  current_touch INT DEFAULT 0,
  sequence_id TEXT,
  next_touch_at TIMESTAMPTZ,
  last_engaged_at TIMESTAMPTZ,
    -- Set by A4 when any engagement signal fires (open, click, reply).
    -- Outbound handlers check this to bail out of the sequence.
  converted_lead_id UUID REFERENCES leads(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- One prospect per email (idempotent CSV imports). Partial unique index
-- skips NULL emails so legacy rows without an address aren't a hard error.
CREATE UNIQUE INDEX IF NOT EXISTS prospects_email_uniq
  ON prospects (lower(email))
  WHERE email IS NOT NULL;

CREATE INDEX IF NOT EXISTS prospects_status_idx
  ON prospects (status);

CREATE INDEX IF NOT EXISTS prospects_next_touch_at_idx
  ON prospects (next_touch_at)
  WHERE next_touch_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS prospects_practice_fit_idx
  ON prospects (practice_fit);

-- Outbound sequence runner uses `leads.next_action` queueing for touches.
-- The handler keys it adds (must match @register names in lib/outbound.py):
--   outbound_touch_2
--   outbound_touch_3
--   outbound_stop
--
-- Note: outbound touches are scheduled against the leads table (via
-- session_set_next) using a synthetic "lead" row created per prospect when
-- the sequence kicks off. This keeps the orchestrator tick loop unchanged.
