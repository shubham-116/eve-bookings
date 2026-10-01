-- Money is stored as integer paise (no float rounding). Timestamps are UTC ISO-8601 text.
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS centres (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL,
  location TEXT NOT NULL,
  UNIQUE (name, location)
);

CREATE TABLE IF NOT EXISTS tests (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

-- Which centre offers which test, and at what price (many-to-many + price).
CREATE TABLE IF NOT EXISTS centre_tests (
  centre_id INTEGER NOT NULL REFERENCES centres(id) ON DELETE CASCADE,
  test_id   INTEGER NOT NULL REFERENCES tests(id),
  price     INTEGER NOT NULL CHECK (price > 0),
  PRIMARY KEY (centre_id, test_id)
);

CREATE TABLE IF NOT EXISTS bookings (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL REFERENCES users(id),
  centre_id      INTEGER NOT NULL,
  test_id        INTEGER NOT NULL,
  appointment_at TEXT NOT NULL,
  amount         INTEGER NOT NULL CHECK (amount > 0),   -- price snapshot at booking time
  status         TEXT NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING','CONFIRMED','FAILED','CANCELLED')),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  -- a booking can only reference a test the centre really offers
  FOREIGN KEY (centre_id, test_id) REFERENCES centre_tests(centre_id, test_id)
);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id, id);
-- no double-booking the same slot while the booking is still alive
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot
  ON bookings(user_id, centre_id, test_id, appointment_at)
  WHERE status IN ('PENDING','CONFIRMED');

CREATE TABLE IF NOT EXISTS payments (
  id           INTEGER PRIMARY KEY,
  booking_id   INTEGER NOT NULL REFERENCES bookings(id),
  provider_ref TEXT NOT NULL UNIQUE,                    -- id the "provider" gave us
  amount       INTEGER NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('SUCCESS','FAILED')),
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id);
-- DB-level guarantee: a booking can never be paid successfully twice
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_success_per_booking
  ON payments(booking_id) WHERE status = 'SUCCESS';

-- Idempotency ledger: every webhook event_id is processed at most once
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id    TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  received_at TEXT NOT NULL
);
