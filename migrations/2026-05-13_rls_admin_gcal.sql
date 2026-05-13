-- 2026-05-13 — Lock down admin_gcal_accounts. Stores live OAuth tokens.
-- Service role bypasses RLS; the app uses service role. Anon must be denied.

ALTER TABLE public.admin_gcal_accounts ENABLE ROW LEVEL SECURITY;

-- Defensive: drop any pre-existing permissive policy from earlier setups
DROP POLICY IF EXISTS "Allow anon read" ON public.admin_gcal_accounts;
DROP POLICY IF EXISTS "Allow public read" ON public.admin_gcal_accounts;

-- Explicit "no policy" = no access. Service role still works via bypass.
-- (We do NOT create any policy; default-deny is the goal.)

-- Idempotency check (will print 't' on success):
SELECT relrowsecurity FROM pg_class WHERE relname = 'admin_gcal_accounts';
