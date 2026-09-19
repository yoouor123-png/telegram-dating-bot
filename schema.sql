CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age >= 18 AND age <= 120),
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female')),
    target_gender TEXT NOT NULL CHECK (target_gender IN ('male', 'female')),
    bio TEXT NOT NULL DEFAULT '',
    photos TEXT[] NOT NULL DEFAULT '{}',
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
    premium_until TIMESTAMPTZ,
    telegram_payment_charge_id TEXT,
    daily_likes_count INTEGER NOT NULL DEFAULT 0,
    last_like_reset TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS interactions (
    id BIGSERIAL PRIMARY KEY,
    from_user BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    to_user BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('yes', 'no')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_user, to_user)
);

CREATE TABLE IF NOT EXISTS matches (
    id BIGSERIAL PRIMARY KEY,
    user_a BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    user_b BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_a, user_b),
    CHECK (user_a < user_b)
);

CREATE TABLE IF NOT EXISTS blocked_users (
    blocker_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    blocked_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    telegram_payment_charge_id TEXT NOT NULL UNIQUE,
    provider_payment_charge_id TEXT,
    premium_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS users_location_gist_idx
    ON users USING GIST (location);
CREATE INDEX IF NOT EXISTS users_matching_idx
    ON users (is_active, gender, target_gender, is_premium);
CREATE INDEX IF NOT EXISTS interactions_from_to_idx
    ON interactions (from_user, to_user);
CREATE INDEX IF NOT EXISTS interactions_to_from_idx
    ON interactions (to_user, from_user);
CREATE INDEX IF NOT EXISTS matches_user_a_idx
    ON matches (user_a);
CREATE INDEX IF NOT EXISTS matches_user_b_idx
    ON matches (user_b);