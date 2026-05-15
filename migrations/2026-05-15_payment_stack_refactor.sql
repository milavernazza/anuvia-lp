-- 2026-05-15 — Payment stack refactor.
-- Owned by Agent A3 (lib/contract.py). Per Mila's decisions (2026-05-15):
--   * Drop Mercado Pago entirely → replace with Pix via Nubank (CNPJ Anuvia Ltda,
--     static Pix key, manual reconciliation for now).
--   * Stripe becomes dual-account: Anuvia Ltda (BR/BRL) + Anuvia LLC (US/USD).
--     Currency in the contract determines which Stripe account to use.
--   * E-signature: replace placeholder with Google Workspace eSignature
--     (Google Docs eSignature via Drive API).
--
-- We intentionally do NOT drop the legacy ``mp_preference_id`` column. Old rows
-- may still reference it and downstream readers tolerate NULL. New code will
-- simply stop writing to it.
--
-- Re-runnable.

-- ---------------------------------------------------------------------------
-- contracts: new columns for the new stack
-- ---------------------------------------------------------------------------

-- Currency the contract is denominated in. Drives Stripe account selection
-- and the default payment method (BRL → Pix preferred, USD → Stripe US).
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'BRL';

-- The chosen payment rail for this contract. One of:
--   'stripe_br' | 'stripe_us' | 'pix'
-- Resolved at /accept time when caller passes payment_method='auto'.
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_method TEXT;

-- Which Stripe account fielded this contract: 'BR' (Anuvia Ltda) or 'US'
-- (Anuvia LLC). Mirrors payment_method but kept separate so future webhooks
-- that arrive without metadata can still be routed correctly.
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS stripe_account TEXT;

-- Pix payload string (BR Code spec) generated at contract creation time.
-- Static — encodes our Nubank Pix key, the value, and the contract id as
-- transaction id. Frontends regenerate the QR from this string client-side
-- if pix_qr_image_url is missing.
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS pix_payload TEXT;

-- Public URL to the rendered QR PNG (Supabase Storage or static dir).
-- Optional — the Pix page can render the QR client-side from pix_payload
-- using any QR library.
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS pix_qr_image_url TEXT;

-- Google Workspace eSignature integration columns.
-- google_doc_id is the Drive file id of the contract Doc we created for
-- the signer. We index it for webhook lookup (push notifications carry
-- the resource id of the watched doc).
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS google_doc_id TEXT;
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS google_esign_request_id TEXT;
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS google_watch_channel_id TEXT;
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS google_watch_resource_id TEXT;
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS google_watch_expires_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- Indexes — webhook + reconciliation lookups
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_contracts_google_doc_id
  ON contracts (google_doc_id)
  WHERE google_doc_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contracts_google_watch_resource_id
  ON contracts (google_watch_resource_id)
  WHERE google_watch_resource_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contracts_stripe_session_id
  ON contracts (stripe_session_id)
  WHERE stripe_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contracts_currency
  ON contracts (currency);

CREATE INDEX IF NOT EXISTS idx_contracts_payment_method
  ON contracts (payment_method)
  WHERE payment_method IS NOT NULL;
