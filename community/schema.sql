-- ExternalGameSync community game config database
-- Run this in the Supabase SQL editor (project → SQL Editor → New query)
--
-- After running, also configure in Supabase dashboard:
--   Authentication → URL Configuration → Redirect URLs
--   Add: http://localhost:54321/callback

-- ── Drop existing tables (safe re-run) ───────────────────────────────────────

DROP TABLE IF EXISTS game_config_votes CASCADE;
DROP TABLE IF EXISTS game_hashes       CASCADE;
DROP TABLE IF EXISTS game_configs      CASCADE;
DROP TABLE IF EXISTS submission_quota  CASCADE;


-- ── Tables ────────────────────────────────────────────────────────────────────

-- Server generates the primary key (BIGSERIAL) so IDs are globally unique
-- regardless of what names users choose locally.
-- Configs are visible immediately (no moderation queue). hidden=true is set by
-- service-role admins to remove spam. votes is a denormalized sum maintained by
-- trigger; the authoritative per-user votes live in game_config_votes.
CREATE TABLE game_configs (
    id             BIGSERIAL   NOT NULL PRIMARY KEY,
    name           TEXT        NOT NULL,
    exe_path       TEXT        NOT NULL DEFAULT '',
    save_path      TEXT        NOT NULL DEFAULT '',
    save_filter    TEXT        NOT NULL DEFAULT '',
    env_vars       TEXT        NOT NULL DEFAULT '',
    -- Stable Steam app ID for native Steam games only. NULL for non-Steam titles
    -- whose shortcut app IDs are machine-local CRC32 values.
    steam_app_id   TEXT,
    votes          BIGINT      NOT NULL DEFAULT 0,
    hidden         BOOLEAN     NOT NULL DEFAULT false,
    submitted_by   UUID        REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX game_configs_name_idx ON game_configs
    USING gin (to_tsvector('english', name));

CREATE INDEX game_configs_steam_app_id_idx ON game_configs (steam_app_id)
    WHERE steam_app_id IS NOT NULL;

-- One row per hash. UNIQUE on hash prevents duplicate entries and allows
-- ON CONFLICT DO NOTHING for idempotent contributions.
CREATE TABLE game_hashes (
    id             BIGSERIAL   NOT NULL PRIMARY KEY,
    game_id        BIGINT      NOT NULL REFERENCES game_configs(id) ON DELETE CASCADE,
    hash           TEXT        NOT NULL,
    hash_type      TEXT        NOT NULL CHECK (hash_type IN ('exe', 'installer')),
    platform       TEXT                 CHECK (platform IN ('windows', 'linux')),
    contributed_by UUID        REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (hash)
);

CREATE INDEX game_hashes_hash_idx    ON game_hashes (hash);
CREATE INDEX game_hashes_game_id_idx ON game_hashes (game_id);

-- One row per (user, config) pair — PRIMARY KEY enforces one vote per user.
-- vote: 1 = working, -1 = not working. Users may update or retract their vote.
CREATE TABLE game_config_votes (
    user_id        UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    game_config_id BIGINT      NOT NULL REFERENCES game_configs(id) ON DELETE CASCADE,
    vote           SMALLINT    NOT NULL CHECK (vote IN (1, -1)),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, game_config_id)
);

-- Per-user daily submission counter to rate-limit new config submissions.
CREATE TABLE submission_quota (
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    day     DATE NOT NULL DEFAULT CURRENT_DATE,
    count   INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);


-- ── Triggers ──────────────────────────────────────────────────────────────────

-- Stamp submitted_by / contributed_by from the session user so clients cannot
-- spoof another user's identity.
CREATE OR REPLACE FUNCTION _set_submitted_by()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    NEW.submitted_by = auth.uid();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION _set_contributed_by()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    NEW.contributed_by = auth.uid();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS game_configs_submitted_by ON game_configs;
CREATE TRIGGER game_configs_submitted_by
    BEFORE INSERT ON game_configs
    FOR EACH ROW EXECUTE FUNCTION _set_submitted_by();

DROP TRIGGER IF EXISTS game_hashes_contributed_by ON game_hashes;
CREATE TRIGGER game_hashes_contributed_by
    BEFORE INSERT ON game_hashes
    FOR EACH ROW EXECUTE FUNCTION _set_contributed_by();

-- Keep updated_at current on edits.
CREATE OR REPLACE FUNCTION _touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS game_configs_updated_at ON game_configs;
CREATE TRIGGER game_configs_updated_at
    BEFORE UPDATE ON game_configs
    FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();

DROP TRIGGER IF EXISTS game_config_votes_updated_at ON game_config_votes;
CREATE TRIGGER game_config_votes_updated_at
    BEFORE UPDATE ON game_config_votes
    FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();

-- Keep game_configs.votes in sync with game_config_votes.
-- Handles INSERT (new vote), UPDATE (changed vote), and DELETE (retracted vote).
CREATE OR REPLACE FUNCTION _sync_config_votes()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        UPDATE game_configs SET votes = votes - OLD.vote WHERE id = OLD.game_config_id;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        UPDATE game_configs SET votes = votes - OLD.vote + NEW.vote WHERE id = NEW.game_config_id;
        RETURN NEW;
    ELSE
        UPDATE game_configs SET votes = votes + NEW.vote WHERE id = NEW.game_config_id;
        RETURN NEW;
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS game_config_votes_sync ON game_config_votes;
CREATE TRIGGER game_config_votes_sync
    AFTER INSERT OR UPDATE OR DELETE ON game_config_votes
    FOR EACH ROW EXECUTE FUNCTION _sync_config_votes();

-- Enforce a daily cap of 10 new config submissions per user.
CREATE OR REPLACE FUNCTION _check_submission_quota()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    daily_limit CONSTANT INT := 10;
    current_count INT;
BEGIN
    INSERT INTO submission_quota (user_id, day, count)
    VALUES (auth.uid(), CURRENT_DATE, 1)
    ON CONFLICT (user_id, day)
    DO UPDATE SET count = submission_quota.count + 1
    RETURNING count INTO current_count;

    IF current_count > daily_limit THEN
        RAISE EXCEPTION 'Daily submission limit of % reached — try again tomorrow', daily_limit;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS game_configs_quota ON game_configs;
CREATE TRIGGER game_configs_quota
    BEFORE INSERT ON game_configs
    FOR EACH ROW EXECUTE FUNCTION _check_submission_quota();


-- ── Row-level security ────────────────────────────────────────────────────────

ALTER TABLE game_configs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE game_hashes       ENABLE ROW LEVEL SECURITY;
ALTER TABLE game_config_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_quota  ENABLE ROW LEVEL SECURITY;

-- Anyone can read non-hidden configs.
DROP POLICY IF EXISTS "public read configs" ON game_configs;
CREATE POLICY "public read configs"
    ON game_configs FOR SELECT
    USING (hidden = false);

-- Authenticated users can always read their own configs (including hidden ones).
DROP POLICY IF EXISTS "users read own configs" ON game_configs;
CREATE POLICY "users read own configs"
    ON game_configs FOR SELECT
    TO authenticated
    USING (submitted_by = auth.uid());

-- Authenticated users may submit new configs.
DROP POLICY IF EXISTS "authenticated insert configs" ON game_configs;
CREATE POLICY "authenticated insert configs"
    ON game_configs FOR INSERT
    TO authenticated
    WITH CHECK (true);

-- Authenticated users may update configs they submitted.
-- They cannot flip hidden (that's service-role only).
DROP POLICY IF EXISTS "users update own configs" ON game_configs;
CREATE POLICY "users update own configs"
    ON game_configs FOR UPDATE
    TO authenticated
    USING     (submitted_by = auth.uid() AND hidden = false)
    WITH CHECK (submitted_by = auth.uid() AND hidden = false);

-- Anyone can read hashes for visible configs.
DROP POLICY IF EXISTS "public read hashes" ON game_hashes;
CREATE POLICY "public read hashes"
    ON game_hashes FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM game_configs gc
            WHERE gc.id = game_hashes.game_id AND gc.hidden = false
        )
    );

-- Authenticated users may contribute hashes.
DROP POLICY IF EXISTS "authenticated insert hashes" ON game_hashes;
CREATE POLICY "authenticated insert hashes"
    ON game_hashes FOR INSERT
    TO authenticated
    WITH CHECK (true);

-- Authenticated users may read all votes (needed to show the user their own vote).
DROP POLICY IF EXISTS "authenticated read votes" ON game_config_votes;
CREATE POLICY "authenticated read votes"
    ON game_config_votes FOR SELECT
    TO authenticated
    USING (true);

-- Users may insert, update, or delete only their own vote row.
-- The PRIMARY KEY (user_id, game_config_id) enforces one vote per user at the DB level.
DROP POLICY IF EXISTS "users manage own vote" ON game_config_votes;
CREATE POLICY "users manage own vote"
    ON game_config_votes FOR ALL
    TO authenticated
    USING     (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- Users manage only their own quota rows.
DROP POLICY IF EXISTS "users own quota" ON submission_quota;
CREATE POLICY "users own quota"
    ON submission_quota FOR ALL
    TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());
