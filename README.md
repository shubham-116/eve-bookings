# EVE Healthcare — Diagnostic Bookings API

> A production-grade REST API for booking diagnostic tests, built with Flask, SQLite, and Docker.

---

## Table of Contents

1. [Overview](#overview)
2. [Tech Stack](#tech-stack)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [Database Schema](#database-schema)
6. [Authentication](#authentication)
7. [Booking State Machine](#booking-state-machine)
8. [Payment Flow](#payment-flow)
9. [Webhook & Idempotency](#webhook--idempotency)
10. [API Reference](#api-reference)
11. [Setup & Running](#setup--running)
12. [Running Tests](#running-tests)
13. [Environment Variables](#environment-variables)
14. [Design Decisions & Assumptions](#design-decisions--assumptions)

---

## Overview

EVE Healthcare is a backend service that allows users to:

- Register and authenticate via JWT
- Browse diagnostic centres and their available tests
- Book a test appointment at a specific centre
- Pay for bookings through a simulated payment gateway
- Cancel bookings at any point (subject to state machine rules)
- Retry failed payments

Administrators can create centres, manage test catalogues, update pricing, and view system-wide stats.

---

## Tech Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Language | Python | 3.12 |
| Web Framework | Flask | >= 3.0 |
| Database | SQLite | (bundled) |
| Auth Tokens | PyJWT | >= 2.8 |
| Production Server | Gunicorn | >= 21.2 |
| Containerisation | Docker + Compose | - |

---

## Architecture

```
+---------------------------------------------------------+
|                      Client (Browser)                   |
+---------------------------+-----------------------------+
                            | HTTP
+---------------------------v-----------------------------+
|                   Gunicorn (2 workers)                  |
|  +----------------------------------------------------+ |
|  |                  Flask Application                 | |
|  |  auth.py | catalog.py | bookings.py | payments.py  | |
|  |               services.py | utils.py               | |
|  +------------------------+---------------------------+ |
+---------+-----------------|---------------------------+-+
                            |
+--------------------------+v-----------------------------+
|              SQLite Database  (eve.db)                  |
|  users | centres | tests | centre_tests | bookings      |
|  payments | webhook_events                              |
+---------------------------------------------------------+
```

Each incoming request is handled by the appropriate Flask blueprint. Every blueprint delegates database work through `db.py`, and state transitions for bookings are exclusively managed by `services.py`.

---

## Project Structure

```
eve-bookings/
+-- app/
|   +-- __init__.py       # Application factory -- create_app()
|   +-- schema.sql        # DDL: all tables, indexes, constraints
|   +-- db.py             # Connection lifecycle, init_db(), seed()
|   +-- utils.py          # ApiError, validate(), page_params(), tx()
|   +-- auth.py           # Signup, login, @auth() decorator
|   +-- catalog.py        # Centres & tests CRUD, admin stats
|   +-- services.py       # Booking state-machine: move_booking()
|   +-- bookings.py       # Create, list, view, cancel bookings
|   +-- payments.py       # Mock payment + webhook handler
|   +-- static/
|       +-- index.html    # Single-page web UI
|       +-- swagger.html  # Swagger UI
|       +-- openapi.yaml  # OpenAPI 3 specification
+-- tests/
|   +-- test_api.py       # 17 end-to-end API tests
+-- requirements.txt
+-- Dockerfile
+-- docker-compose.yml
+-- .gitignore
```

---

## Database Schema

### Entity Relationship

```
users --< bookings >-- centre_tests >-- centres
                  +-- centre_tests >-- tests
bookings --< payments
webhook_events  (standalone idempotency ledger)
```

### Tables

| Table | Purpose |
|-------|---------|
| `users` | Account records; password stored as scrypt hash |
| `centres` | Diagnostic centre locations |
| `tests` | Master catalogue of test types |
| `centre_tests` | Junction table: which centre offers which test, at what price (in paise) |
| `bookings` | Appointment records with a price snapshot and status |
| `payments` | Individual payment attempts (one booking -> many attempts) |
| `webhook_events` | Idempotency ledger -- event IDs written here on first receipt |

### Key Constraints

| Constraint | Effect |
|-----------|--------|
| `uq_active_slot` | Prevents double-booking the same (user, centre, test, time) slot for PENDING or CONFIRMED bookings |
| `uq_one_success_per_booking` | Partial unique index; only one SUCCESS payment may exist per booking |
| `CHECK (price > 0)` | Rejects zero or negative prices at the database level |
| `CHECK (status IN (...))` | Enforces the allowed status vocabulary |
| Composite FK `(centre_id, test_id)` | Guarantees a booking can only reference a test that the chosen centre actually offers |

> **Why paise?** Money is stored as integer paise (1 INR = 100 paise) to eliminate floating-point arithmetic errors. `350.00 INR -> 35000 paise`.

---

## Authentication

All protected routes require an `Authorization: Bearer <token>` header.

### Signup

```http
POST /auth/signup
Content-Type: application/json

{ "name": "Alice", "email": "alice@example.com", "password": "secret123" }
```

The password is hashed with **scrypt** (memory-hard, salted). The raw password is never persisted.

### Login

```http
POST /auth/login
Content-Type: application/json

{ "email": "alice@example.com", "password": "secret123" }
```

Returns:

```json
{ "access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600 }
```

Tokens expire after **1 hour**. The JWT payload contains `sub` (user ID) and `exp`.

### Admin Access

Any email listed in the `ADMIN_EMAILS` environment variable is granted admin privileges at signup time. Admin endpoints additionally check `is_admin = 1` on the user record.

---

## Booking State Machine

```
              +---------------------------------------------+
              |                PENDING                      |
              |         (created, awaiting payment)         |
              +------+----------------+----------+----------+
                     |                |          |
              pay SUCCESS      pay FAILED     cancel
                     |                |          |
                     v                v          v
              CONFIRMED            FAILED    CANCELLED
                     |                |
                  cancel         retry -> CONFIRMED
                     |              cancel
                     v                |
               CANCELLED             v
                                  CANCELLED
```

State transitions are exclusively managed by `services.move_booking()`, which uses a single atomic `UPDATE ... WHERE id = ? AND status IN (...)` statement. If the row's current status is not in the allowed set, `rowcount = 0` and a `409 Conflict` is returned.

---

## Payment Flow

```
POST /payments/   { "booking_id": 1 }

 1. Verify booking belongs to the requesting user
 2. Assert booking status is PENDING or FAILED
 3. Generate a unique provider_ref (UUID4)
 4. Simulate outcome: 80% SUCCESS / 20% FAILED
    (override with { "simulate": "SUCCESS" | "FAILED" } for tests/demos)
 5. BEGIN IMMEDIATE TRANSACTION
    a. INSERT INTO payments (status = outcome)
    b. move_booking(id, "CONFIRMED" | "FAILED")
 6. COMMIT
 7. Return { provider_ref, status, booking_status }
```

Both the payment record and the booking status change are committed atomically.

---

## Webhook & Idempotency

The `POST /payments/webhook` endpoint receives provider callbacks and is designed to be safely re-deliverable.

**Verification** -- Every incoming request must carry an `X-Signature: sha256=<hex>` header. The server computes `HMAC-SHA256(WEBHOOK_SECRET, request_body)` and rejects mismatches with `401`.

**Idempotency** -- On first receipt, `event_id` is written to `webhook_events` with `INSERT ... ON CONFLICT DO NOTHING`. If `rowcount = 0`, the event was seen before; the handler returns `{"status": "duplicate"}` immediately without re-processing.

**Safety on error** -- If the event is new but the `provider_ref` is unknown, the entire transaction rolls back -- including the `webhook_events` row -- so the provider can retry later.

**No downgrade** -- A SUCCESS payment status is never overwritten to FAILED (`WHERE status != 'SUCCESS'`). The same protection applies to CONFIRMED bookings via the state machine.

---

## API Reference

> **Base URL**: `http://localhost:8000`  
> **Money**: integer paise (`50000` = INR 500.00)  
> **Timestamps**: UTC ISO-8601 (e.g., `2027-01-15T10:30:00Z`)  
> **Errors**: `{ "error": { "code": "...", "message": "..." } }`  
> **Pagination**: `?page=1&page_size=20` (max 100 per page)

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check -- returns `{ "status": "ok" }` |
| `POST` | `/auth/signup` | Create a new user account |
| `POST` | `/auth/login` | Authenticate and receive a JWT |
| `GET` | `/centres` | List all centres with tests and prices |
| `GET` | `/centres/:id` | Get a single centre |

### User Endpoints _(JWT required)_

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/bookings` | Create a booking `{ centre_id, test_id, appointment_at }` |
| `GET` | `/bookings` | List the authenticated user's bookings |
| `GET` | `/bookings/:id` | Get a booking with its full payment history |
| `POST` | `/bookings/:id/cancel` | Cancel a booking |
| `POST` | `/payments/` | Pay for a booking `{ booking_id, simulate? }` |

### Admin Endpoints _(Admin JWT required)_

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/centres` | Create a centre `{ name, location }` |
| `POST` | `/centres/:id/tests` | Add or update a test `{ name, price }` |
| `DELETE` | `/centres/:id/tests/:tid` | Remove a test from a centre |
| `GET` | `/admin/stats` | System-wide booking and revenue statistics |

### Webhook

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/payments/webhook` | Provider callback `{ event_id, provider_ref, status }` |

---

## Setup & Running

### Option 1 -- Docker (Recommended)

**Prerequisites**: Docker Desktop (or Docker Engine + Compose plugin) installed and running.

```bash
# 1. Clone the repository
git clone <repository-url>
cd eve-bookings

# 2. Build the image and start the container
docker compose up --build

# 3. In a second terminal, seed demo data (5 centres, 10 tests)
docker compose exec api flask --app app seed

# 4. Open the web UI
#    http://localhost:8000
```

To stop the service:

```bash
docker compose down
```

The SQLite database is stored in a named Docker volume (`db-data`) and survives container restarts.

---

### Option 2 -- Local Python

**Prerequisites**: Python 3.12+ installed.

```bash
# 1. Clone the repository
git clone <repository-url>
cd eve-bookings

# 2. Create and activate a virtual environment
python -m venv .venv
```

**Windows (PowerShell):**

```powershell
.venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
source .venv/bin/activate
```

```bash
# 3. Install dependencies
pip install -r requirements.txt
```

**Set environment variables -- Windows (PowerShell):**

```powershell
$env:SECRET_KEY     = "replace-with-a-long-random-string"
$env:WEBHOOK_SECRET = "replace-with-another-secret"
$env:ADMIN_EMAILS   = "admin@example.com"
```

**Set environment variables -- macOS / Linux:**

```bash
export SECRET_KEY="replace-with-a-long-random-string"
export WEBHOOK_SECRET="replace-with-another-secret"
export ADMIN_EMAILS="admin@example.com"
```

```bash
# 4. Seed the database with demo data
flask --app app seed

# 5. Start the development server
flask --app app run
# Server available at http://127.0.0.1:5000
```

### Admin Login

Sign up using the email configured in `ADMIN_EMAILS` (default: `admin@example.com`). That account is automatically granted admin privileges. Password must be at least 8 characters.

### Interactive API Documentation

Swagger UI is available at:

```
http://localhost:8000/docs/swagger
```

---

## Running Tests

The test suite uses Python's built-in `unittest` framework. Each test case spins up a fresh Flask application pointed at a temporary SQLite database, exercises the full HTTP stack without mocking, and tears down the database after the test.

```bash
python -m unittest -v
```

### Test Coverage

| Test | Scenario |
|------|---------|
| `test_signup_validation_and_duplicates` | Invalid email, short password, duplicate registration |
| `test_login_and_token_checks` | Wrong password, missing / expired / malformed tokens |
| `test_catalog_admin_only_write_public_read` | Non-admin write rejection; public read access |
| `test_price_update_and_bad_price` | Re-posting a test updates price; rejects negative prices |
| `test_create_booking_and_edge_cases` | Past dates, invalid ISO format, non-existent test |
| `test_bookings_are_private` | Returns 404 (not 403) for another user's booking |
| `test_cancel` | Cancel succeeds; double-cancel rejected; slot freed for re-booking |
| `test_payment_success_confirms_booking` | Pay -> CONFIRMED; second payment attempt rejected |
| `test_failed_payment_then_retry` | FAILED booking can be retried and confirmed |
| `test_webhook_rejects_bad_signature` | Wrong HMAC secret -> 401 |
| `test_webhook_is_idempotent` | Same event delivered three times -> processed exactly once |
| `test_replayed_event_id_cannot_flip_state` | Duplicate `event_id` with different body is still a duplicate |
| `test_late_failure_event_never_downgrades` | SUCCESS payment cannot be downgraded to FAILED |
| `test_success_event_for_cancelled_booking` | Cancelled booking stays cancelled even if a SUCCESS arrives |
| `test_unknown_payment_ref` | Unknown `provider_ref` returns 404; event ID is rolled back |
| `test_second_success_for_same_booking` | Two SUCCESS payments for one booking -> 409 |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | **Yes** | `dev-secret` | Secret used to sign JWT tokens. **Must be changed in production.** |
| `WEBHOOK_SECRET` | **Yes** | `webhook-secret` | HMAC secret for verifying incoming webhook payloads. |
| `ADMIN_EMAILS` | No | _(none)_ | Comma-separated list of emails that receive admin privileges on signup (e.g. `admin@example.com,ops@example.com`). |
| `DATABASE` | No | `eve.db` (cwd) | Absolute path to the SQLite database file. The Docker image sets this to `/data/eve.db`. |

> In production, set `SECRET_KEY` and `WEBHOOK_SECRET` to long, randomly generated values. The placeholder values in `docker-compose.yml` (`change-me`) must not be used in any exposed environment.

---

## Design Decisions & Assumptions

The following decisions were made where the requirements left room for interpretation.

### 1. SQLite over PostgreSQL

**Decision**: SQLite is used as the sole database engine.

**Rationale**: The service is self-contained and intended to run as a single instance. SQLite in WAL mode (`PRAGMA journal_mode = WAL`) supports concurrent readers with a single writer, which is sufficient for the stated use case. Switching to PostgreSQL would require only a driver change and minor SQL dialect adjustments.

**Assumption**: Horizontal scaling and multi-instance deployments are out of scope for this version.

---

### 2. Money stored as integer paise

**Decision**: All monetary values are stored and transmitted as integer paise (1 INR = 100 paise).

**Rationale**: Floating-point types cannot represent decimal fractions exactly (`0.1 + 0.2 != 0.3`). Integer arithmetic is exact. This is the standard approach used by payment processors such as Razorpay and Stripe.

**Assumption**: The system operates in Indian Rupees only. Multi-currency support is not required.

---

### 3. Price snapshot on booking creation

**Decision**: The price at booking time is copied into the `bookings.amount` column.

**Rationale**: If an admin later changes the price of a test, existing confirmed bookings must retain the amount the user was shown and charged. Relying on a live join to `centre_tests` would silently change historical records.

**Assumption**: Price changes should never retroactively affect existing bookings.

---

### 4. Atomic state transitions

**Decision**: `services.move_booking()` performs a single `UPDATE ... WHERE id = ? AND status IN (...)` rather than a read-then-write pair.

**Rationale**: A read-check-write sequence under concurrent load introduces a TOCTOU race condition. The atomic `WHERE` clause guarantees that only one concurrent request can succeed for a given transition without application-level locks.

---

### 5. 404 instead of 403 for other users' bookings

**Decision**: `GET /bookings/:id` returns `404 Not Found` when a booking exists but belongs to a different user.

**Rationale**: Returning `403 Forbidden` confirms to an attacker that a resource with that ID exists. Returning `404` reveals nothing, preventing sequential enumeration of booking IDs.

---

### 6. Simulated payment gateway (80/20 split)

**Decision**: Payment outcomes are simulated with an 80% success / 20% failure ratio. The optional `simulate` field in the request body overrides this for deterministic testing.

**Assumption**: Integration with a real payment provider is outside the scope of this project. The simulation layer in `payments.py` is designed to be replaced with an actual HTTP call without changing any other module.

---

### 7. Webhook idempotency via primary-key insert

**Decision**: Idempotency is enforced by inserting `event_id` into `webhook_events` inside the same transaction as the payment update. `ON CONFLICT DO NOTHING` followed by a `rowcount` check determines whether the event is new.

**Rationale**: This is transactional -- if any subsequent step fails, the `event_id` row is also rolled back, allowing the provider to retry cleanly. A "mark as processed after success" pattern would leave a window where a retry could re-process an event.

---

### 8. Application factory pattern

**Decision**: The Flask app is created via `create_app(config=None)` rather than as a module-level global.

**Rationale**: The factory pattern allows the test suite to instantiate a fresh app (and fresh database) for each test without shared state leaking between tests. It also makes configuration injection straightforward.

---

### 9. Raw SQL over an ORM

**Decision**: All database interactions use `sqlite3` directly with parameterised queries.

**Rationale**: The schema is stable and well-defined. Raw SQL gives precise control over query shape, partial indexes, and `ON CONFLICT` clauses that are awkward to express in most Python ORMs. The codebase is small enough that the abstraction benefits of an ORM do not outweigh the added complexity.

---

### 10. WAL mode and foreign keys enabled on every connection

**Decision**: `PRAGMA journal_mode = WAL` is set in `init_db()`. `PRAGMA foreign_keys = ON` is set on every connection in `get_db()`.

**Rationale**: WAL mode allows readers and writers to operate concurrently, which is important under Gunicorn's multi-worker setup. Foreign key enforcement is off by default in SQLite and must be re-enabled per connection; the `get_db()` helper guarantees it is always active.

---

*Built with Python 3.12 - Flask 3 - SQLite - Docker*
