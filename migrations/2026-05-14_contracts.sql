-- 2026-05-14 — Contracts + Engagements tables for autonomous close/delivery.
-- Re-runnable. Owned by Agent A3 (lib/contract.py). Per
-- ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Contract & payment contract" and
-- §"Delivery agents contract".
--
-- Lifecycle:
--   1. Lead accepts proposal (lib/track_b.py /accept) → handler creates row
--      in `contracts` via lib.contract.generate_contract(...).
--   2. Lead clicks sign link (HMAC-verified) → contracts.status = 'signed',
--      Stripe/MercadoPago checkout session created, stored in
--      stripe_session_id (or mp_preference_id).
--   3. Payment webhook fires → contracts.status = 'paid', engagement row
--      inserted, delivery kickoff queued on the lead's next_action.
--   4. Delivery agents (lib/delivery/*) advance engagements through phases.
--   5. Final phase completes → invoice issued (Conta Azul, stubbed for now).

-- ---------------------------------------------------------------------------
-- contracts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS contracts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  engagement_id UUID,
    -- FK set after the row is paid and the engagement is created.
    -- Nullable for the sent/signed states.
  practice TEXT,
    -- 'growth' | 'cloud_finops' | 'ai' | 'devops' | 'industry'
  value_brl NUMERIC,
  status TEXT DEFAULT 'sent',
    -- Status enum:
    --   draft     — created without Stripe (payment provider unconfigured)
    --   sent      — emailed to lead, awaiting signature
    --   viewed    — lead opened the sign page
    --   signed    — HMAC accept landed, checkout session created
    --   paid      — payment webhook fired
    --   refunded  — refund webhook fired (rare)
    --   cancelled — manually voided
  sent_at TIMESTAMPTZ DEFAULT now(),
  signed_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ,
  pdf_url TEXT,
  sign_url TEXT,
  stripe_session_id TEXT,
  mp_preference_id TEXT,
  invoice_id TEXT,
    -- Conta Azul NF-e id (or 'manual_TODO' until creds are provided).
  hmac_token TEXT NOT NULL,
  scope JSONB DEFAULT '{}'::jsonb,
    -- Snapshot of practice scope + any overrides at the moment of generation.
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS contracts_status_idx
  ON contracts (status);

CREATE INDEX IF NOT EXISTS contracts_lead_id_idx
  ON contracts (lead_id);

CREATE INDEX IF NOT EXISTS contracts_practice_idx
  ON contracts (practice);

CREATE INDEX IF NOT EXISTS contracts_stripe_session_id_idx
  ON contracts (stripe_session_id)
  WHERE stripe_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS contracts_mp_preference_id_idx
  ON contracts (mp_preference_id)
  WHERE mp_preference_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- engagements
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS engagements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  contract_id UUID REFERENCES contracts(id),
  practice TEXT,
    -- 'growth' | 'cloud_finops' | 'ai' | 'devops' | 'industry'
  started_at TIMESTAMPTZ DEFAULT now(),
  contract_signed_at TIMESTAMPTZ,
  first_payment_at TIMESTAMPTZ,
  current_phase INT DEFAULT 1,
  total_phases INT,
  status TEXT DEFAULT 'kickoff',
    -- Status enum:
    --   kickoff      — intake form sent, awaiting client data
    --   in_progress  — at least one delivery phase running
    --   review       — deliverable in client review
    --   delivered    — final report handed off
    --   invoiced     — NF-e issued
    --   closed       — engagement archived
  intake_data JSONB DEFAULT '{}'::jsonb,
  artifacts JSONB DEFAULT '[]'::jsonb,
  next_phase_at TIMESTAMPTZ,
  total_value_brl NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS engagements_status_idx
  ON engagements (status);

CREATE INDEX IF NOT EXISTS engagements_lead_id_idx
  ON engagements (lead_id);

CREATE INDEX IF NOT EXISTS engagements_contract_id_idx
  ON engagements (contract_id);

CREATE INDEX IF NOT EXISTS engagements_practice_idx
  ON engagements (practice);

CREATE INDEX IF NOT EXISTS engagements_next_phase_at_idx
  ON engagements (next_phase_at)
  WHERE next_phase_at IS NOT NULL;
