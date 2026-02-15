-- Idempotent games-table migration for retry/completion tracking.
-- Safe to run repeatedly.

ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_terminal BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE games ADD COLUMN IF NOT EXISTS analysis_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_send_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_last_send_at TIMESTAMPTZ;
ALTER TABLE games ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
