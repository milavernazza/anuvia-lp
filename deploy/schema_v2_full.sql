-- =============================================================================
-- ANUVIA — Schema consolidado v2 (rebuild 2026-07-30)
-- =============================================================================
-- Roda LIMPO num projeto Supabase novo (supabase.com free tier).
-- Consolida: base tables + as 7 migrations de maio/2026, na ordem.
-- Re-runnable (IF NOT EXISTS em tudo).
--
-- COMO RODAR: supabase.com → SQL Editor → colar tudo → RUN.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. LEADS — tabela raiz do funil
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT DEFAULT 'anuvia',
  funnel_id TEXT,
    -- 'BR_SMB' | 'BR_ENG' | 'BR_BRAND' | vertical-specific
  market TEXT DEFAULT 'BR',
  language TEXT DEFAULT 'pt-BR',
  name TEXT,
  email TEXT,
  phone_e164 TEXT,
  company TEXT,
  source TEXT,
  source_detail JSONB DEFAULT '{}'::jsonb,
  qualification_data JSONB DEFAULT '{}'::jsonb,
  consent JSONB DEFAULT '{}'::jsonb,
  tags TEXT[] DEFAULT '{}',
  current_stage TEXT DEFAULT 'new',
  session_token TEXT,
  score INT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),

  -- lead_sessions (2026-05-13): shared agent memory
  track TEXT,
  lifecycle_status TEXT DEFAULT 'new',
  next_action TEXT,
  next_action_at TIMESTAMPTZ,
  last_touch_at TIMESTAMPTZ DEFAULT now(),
  agent_history JSONB DEFAULT '[]'::jsonb,
  artifacts JSONB DEFAULT '[]'::jsonb,
  signals JSONB DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS leads_next_action_at_idx
  ON leads (next_action_at) WHERE next_action_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS leads_lifecycle_status_idx
  ON leads (lifecycle_status);
CREATE INDEX IF NOT EXISTS leads_email_idx ON leads (email);
CREATE INDEX IF NOT EXISTS leads_current_stage_idx ON leads (current_stage);

-- ---------------------------------------------------------------------------
-- 2. CONTRACTS (2026-05-14 + payment refactor 2026-05-15)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contracts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  engagement_id UUID,
  practice TEXT,
    -- 'growth' | 'cloud_finops' | 'ai' | 'devops' | 'industry'
  value_brl NUMERIC,
  currency TEXT DEFAULT 'BRL',
  status TEXT DEFAULT 'sent',
    -- draft | sent | viewed | signed | paid | refunded | cancelled
  sent_at TIMESTAMPTZ DEFAULT now(),
  signed_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ,
  pdf_url TEXT,
  sign_url TEXT,
  stripe_session_id TEXT,
  mp_preference_id TEXT,
  pix_txid TEXT,
  invoice_id TEXT,
  hmac_token TEXT NOT NULL,
  scope JSONB DEFAULT '{}'::jsonb,
  signer_meta JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS contracts_lead_id_idx ON contracts (lead_id);
CREATE INDEX IF NOT EXISTS contracts_status_idx ON contracts (status);

-- ---------------------------------------------------------------------------
-- 3. ENGAGEMENTS (2026-05-14 + delivery_mode 2026-05-17)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  contract_id UUID REFERENCES contracts(id),
  practice TEXT,
  started_at TIMESTAMPTZ DEFAULT now(),
  contract_signed_at TIMESTAMPTZ,
  first_payment_at TIMESTAMPTZ,
  current_phase INT DEFAULT 1,
  total_phases INT,
  status TEXT DEFAULT 'kickoff',
    -- kickoff | in_progress | review | delivered | invoiced | closed
  intake_data JSONB DEFAULT '{}'::jsonb,
  artifacts JSONB DEFAULT '{}'::jsonb,
    -- NOTA: dict (não array) — email_drafts, kickoff_email_msg_id, phase payloads
  next_phase_at TIMESTAMPTZ,
  total_value_brl NUMERIC,
  delivery_mode TEXT DEFAULT 'whiteglove',
    -- 'whiteglove' (default seguro) | 'autonomous'
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS engagements_status_idx ON engagements (status);
CREATE INDEX IF NOT EXISTS engagements_lead_id_idx ON engagements (lead_id);
CREATE INDEX IF NOT EXISTS engagements_contract_id_idx ON engagements (contract_id);

-- ---------------------------------------------------------------------------
-- 4. PROSPECTS (2026-05-14 outbound)
-- ---------------------------------------------------------------------------
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
  source TEXT,
  status TEXT DEFAULT 'new',
    -- new | sequence_running | replied | bounced | complained
    -- | unsubscribed | converted | stopped
  current_touch INT DEFAULT 0,
  sequence_id TEXT,
  next_touch_at TIMESTAMPTZ,
  last_engaged_at TIMESTAMPTZ,
  converted_lead_id UUID REFERENCES leads(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS prospects_email_uq ON prospects (lower(email));
CREATE INDEX IF NOT EXISTS prospects_next_touch_idx
  ON prospects (next_touch_at) WHERE next_touch_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. ADMIN_GCAL_ACCOUNTS — OAuth tokens Google Calendar
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_gcal_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE,
  refresh_token TEXT,
  calendar_id TEXT DEFAULT 'primary',
  is_active BOOLEAN DEFAULT true,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- RLS lockdown (2026-05-13): default-deny; app usa service role (bypassa RLS)
ALTER TABLE admin_gcal_accounts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Allow anon read" ON admin_gcal_accounts;
DROP POLICY IF EXISTS "Allow public read" ON admin_gcal_accounts;

-- RLS nas outras tabelas: mesmo padrão default-deny
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE engagements ENABLE ROW LEVEL SECURITY;
ALTER TABLE prospects ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 6. updated_at triggers
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['leads','contracts','engagements','prospects','admin_gcal_accounts']
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I_updated_at ON %I;
       CREATE TRIGGER %I_updated_at BEFORE UPDATE ON %I
       FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
      t, t, t, t
    );
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 7. Storage bucket (rodar via dashboard OU esta query)
-- ---------------------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public)
VALUES ('anuvia-deliverables', 'anuvia-deliverables', true)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Verificação final
-- ---------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM information_schema.tables
   WHERE table_schema='public'
     AND table_name IN ('leads','contracts','engagements','prospects','admin_gcal_accounts')
  ) AS tables_created_of_5;
