-- 2026-05-13 — Delete smoke-test leads accumulated during E2E testing.
-- Re-runnable; only deletes rows that are unambiguously test fixtures.

DELETE FROM public.leads
WHERE
  email LIKE 'smoke+%@%'
  OR email LIKE 'smoke-b%@%'
  OR email LIKE 'smoke-b2+%'
  OR email LIKE 'smoke-b3+%'
  OR email LIKE 'smoke-b4+%'
  OR email LIKE 'smoke-b5+%'
  OR email LIKE 'smoke-b6+%'
  OR email LIKE 'smoke-b7+%'
  OR email LIKE 'smoke-b8+%'
  OR email LIKE 'anon-%@diagnostic.anuvia.local';

-- Report how many rows remain so we know the cleanup landed.
SELECT count(*) AS leads_remaining FROM public.leads;
