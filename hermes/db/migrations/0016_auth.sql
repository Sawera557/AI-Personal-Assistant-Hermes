-- Hermes — 0016: Passwordless email login (OTP codes + server-side sessions).
--
-- The app is single-user (one allowlisted email). Auth is a one-time 6-digit
-- code emailed to that inbox, exchanged for a long-lived, revocable session.
--
-- Nothing sensitive is stored in the clear:
--   * OTP codes are kept only as an HMAC (auth_login_codes.code_hash) — the
--     plaintext lives only in the email that was sent.
--   * Session tokens are kept only as an HMAC (auth_sessions.token_hash) — the
--     raw token lives only in the user's HttpOnly cookie.
-- So a database leak alone can neither reveal a code nor forge a session.

CREATE TABLE IF NOT EXISTS auth_login_codes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email       text        NOT NULL,
    code_hash   text        NOT NULL,          -- HMAC-SHA256(secret, email|code)
    expires_at  timestamptz NOT NULL,
    attempts    integer     NOT NULL DEFAULT 0,-- wrong-code tries; locked past a cap
    consumed_at timestamptz,                    -- set once redeemed (single-use)
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Verify looks up the newest live code for an email; send rate-limiting counts
-- recent rows per email. Both are (email, created_at) scans.
CREATE INDEX IF NOT EXISTS idx_auth_login_codes_email_created
    ON auth_login_codes (email, created_at DESC);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash   text        NOT NULL UNIQUE,   -- HMAC-SHA256(secret, raw token)
    email        text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,                    -- set on logout / manual kill
    user_agent   text,
    ip           text
);

-- Every gated request validates the cookie by token_hash (already UNIQUE, but a
-- partial index over live sessions keeps that lookup tight).
CREATE INDEX IF NOT EXISTS idx_auth_sessions_live
    ON auth_sessions (token_hash)
    WHERE revoked_at IS NULL;
