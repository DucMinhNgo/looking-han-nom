-- Hán-Nôm dataset — PostgreSQL schema
-- Chạy tự động lần đầu khi PostgreSQL container khởi động.
-- Idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.

-- ─── Extension ────────────────────────────────────────────────────────────────
-- pg_trgm: full-text / similarity search trên caption và ground_truth.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── Main table ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dataset_items (
    id            SERIAL PRIMARY KEY,

    -- Đường dẫn ảnh gốc (hoặc '' nếu không có ảnh).
    image         TEXT NOT NULL DEFAULT '',

    -- URL bài đăng Facebook (hoặc ID tùy export).
    post_id       TEXT NOT NULL DEFAULT '',

    -- URL permalink riêng nếu export giữ hai cột khác nhau.
    post_link     TEXT NOT NULL DEFAULT '',

    caption       TEXT NOT NULL DEFAULT '',
    ground_truth  TEXT NOT NULL DEFAULT '',

    -- Mọi cột bổ sung trong file JSONL (verified, verified_by, ...).
    extra         JSONB NOT NULL DEFAULT '{}',
    -- Optional integer used to order items in the UI. Older DBs may lack
    -- this column; the ALTER TABLE below ensures the column exists.
    display_index INTEGER NULL,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Unique constraints ───────────────────────────────────────────────────────
-- Dùng để dedup khi import: một ảnh = một row.
-- Rows không có ảnh được dedup theo post_id thay thế.
--
-- Hai partial unique indexes thay vì một unique constraint phức hợp,
-- vì một row có thể có cả image lẫn post_id và chúng tôi muốn
-- unique check chỉ kích hoạt trên cột có giá trị.

CREATE UNIQUE INDEX IF NOT EXISTS uq_dataset_image
    ON dataset_items (image)
    WHERE image <> '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_dataset_post_no_image
    ON dataset_items (post_id)
    WHERE image = '' AND post_id <> '';

-- ─── Search indexes ───────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_dataset_caption_trgm
    ON dataset_items USING GIN (caption gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_dataset_ground_truth_trgm
    ON dataset_items USING GIN (ground_truth gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_dataset_post_id
    ON dataset_items (post_id)
    WHERE post_id <> '';

-- ─── Auto-update updated_at ───────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_dataset_updated_at ON dataset_items;
CREATE TRIGGER trg_dataset_updated_at
    BEFORE UPDATE ON dataset_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ─── Backwards-compatible migration: add `display_index` if missing ────────
ALTER TABLE dataset_items
    ADD COLUMN IF NOT EXISTS display_index INTEGER;

-- ─── Users table ──────────────────────────────────────────────────────────────
-- Lưu tài khoản reviewer và admin.
-- Super admin (APP_USERNAME) vẫn ở env — không bao giờ lưu vào đây.
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,

    -- Luôn lowercase, tối đa 64 ký tự (đủ cho email).
    username        TEXT NOT NULL,

    -- 'admin' hoặc 'reviewer'
    role            TEXT NOT NULL DEFAULT 'reviewer'
                        CHECK (role IN ('admin', 'reviewer')),

    -- bcrypt hash $2b$12$... — KHÔNG BAO GIỜ lưu plaintext.
    password_hash   TEXT NOT NULL DEFAULT '',

    display_name    TEXT NOT NULL DEFAULT '',
    active          BOOLEAN NOT NULL DEFAULT TRUE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,

    -- Tên người tạo (username string, không phải FK — super admin tạo user
    -- nhưng không có row trong bảng này).
    created_by      TEXT NOT NULL DEFAULT '',

    CONSTRAINT uq_users_username UNIQUE (username)
);

CREATE INDEX IF NOT EXISTS idx_users_active
    ON users (active)
    WHERE active = TRUE;

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
