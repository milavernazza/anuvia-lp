-- 2026-05-17 — Add `delivery_mode` column to engagements.
--
-- Two values supported:
--   'whiteglove'  (default for new rows) — Anuvia auto-books a presentation
--                  meeting, Slack-DMs Mila with materials + a "Apresentei →
--                  enviar materiais" button. Client email only fires AFTER
--                  Mila clicks the button. This makes the human moment
--                  (the presentation) the actual delivery, and keeps the
--                  pipeline autonomous around it.
--
--   'autonomous'  — legacy mode, kept for backward compatibility and for
--                   automated smoke tests. The phase handler composes the
--                   deliverables and emails them directly to the client at
--                   phase boundary, exactly like the old flow.
--
-- Default is 'whiteglove' per Mila's strategic direction (see task #56):
-- the bottleneck for premium engagements is the genuinely human touch of
-- presenting findings, not the artifact generation itself.
--
-- Idempotent: safe to re-run.

ALTER TABLE engagements
    ADD COLUMN IF NOT EXISTS delivery_mode TEXT DEFAULT 'whiteglove';

CREATE INDEX IF NOT EXISTS idx_engagements_delivery_mode
    ON engagements(delivery_mode);

-- Optional: backfill any existing rows that landed before this column
-- existed (postgres set them to NULL despite the DEFAULT clause when
-- ADD COLUMN ... DEFAULT was applied without rewriting the table on
-- older versions). Belt-and-suspenders.
UPDATE engagements
   SET delivery_mode = 'whiteglove'
 WHERE delivery_mode IS NULL;
