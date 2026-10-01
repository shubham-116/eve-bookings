"""Simulated payment + idempotent provider webhook."""
import hashlib
import hmac
import logging
import random
import sqlite3
import uuid

from flask import Blueprint, current_app, jsonify, request

from .auth import auth
from .bookings import own_booking
from .db import get_db
from .services import BOOKING_FOR, move_booking
from .utils import ApiError, now, tx, validate

bp = Blueprint("payments", __name__, url_prefix="/payments")
log = logging.getLogger("payments")


@bp.post("/")
@auth()
def pay():
    """Mock payment. Outcome is random (PAYMENT_SUCCESS_RATE), or forced with
    {"simulate": "SUCCESS" | "FAILED"} so demos and tests are deterministic."""
    d = validate(request.get_json(silent=True), {"booking_id": int}, {"simulate": str})
    forced = d.get("simulate", "").upper() or None
    if forced and forced not in BOOKING_FOR:
        raise ApiError(400, "validation_error", "'simulate' must be SUCCESS or FAILED")

    db = get_db()
    booking = own_booking(db, d["booking_id"])
    if booking["status"] not in ("PENDING", "FAILED"):  # FAILED = retry allowed
        raise ApiError(409, "invalid_state", f"Booking is {booking['status']}; it cannot be paid")

    outcome = forced or ("SUCCESS" if random.random() < current_app.config["PAYMENT_SUCCESS_RATE"] else "FAILED")
    ref, ts = "pay_" + uuid.uuid4().hex[:16], now()
    try:
        with tx(db):  # payment row + booking status change succeed or fail together
            cur = db.execute(
                "INSERT INTO payments(booking_id, provider_ref, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (booking["id"], ref, booking["amount"], outcome, ts, ts))
            if not move_booking(db, booking["id"], BOOKING_FOR[outcome]):
                raise ApiError(409, "invalid_state", "Booking changed while the payment was processing")
    except sqlite3.IntegrityError:
        raise ApiError(409, "conflict", "Booking already has a successful payment")
    log.info("payment booking=%s ref=%s status=%s", booking["id"], ref, outcome)
    return jsonify(id=cur.lastrowid, booking_id=booking["id"], provider_ref=ref, amount=booking["amount"],
                   status=outcome, booking_status=BOOKING_FOR[outcome]), 201


@bp.post("/webhook")
def webhook():
    """Provider callback: {"event_id": "...", "provider_ref": "pay_...", "status": "SUCCESS"|"FAILED"}
    signed with header X-Signature = HMAC-SHA256(WEBHOOK_SECRET, raw body), hex.

    Idempotency: event_id is the primary key of webhook_events and is inserted in the SAME
    transaction as the state change. A replay hits the conflict and does nothing."""
    raw = request.get_data()
    expected = hmac.new(current_app.config["WEBHOOK_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Signature", "").encode(), expected.encode()):
        raise ApiError(401, "invalid_signature", "Bad or missing X-Signature")

    d = validate(request.get_json(silent=True), {"event_id": str, "provider_ref": str, "status": str})
    status = d["status"].upper()
    if status not in BOOKING_FOR:
        raise ApiError(400, "validation_error", "'status' must be SUCCESS or FAILED")

    db = get_db()
    try:
        with tx(db):
            fresh = db.execute(
                "INSERT INTO webhook_events(event_id, payload, received_at) VALUES (?, ?, ?) "
                "ON CONFLICT(event_id) DO NOTHING", (d["event_id"], raw.decode(errors="replace"), now())).rowcount
            if not fresh:
                log.info("webhook duplicate event=%s", d["event_id"])
                return jsonify(status="duplicate"), 200  # 2xx so the provider stops retrying

            pay_row = db.execute("SELECT id, booking_id, status FROM payments WHERE provider_ref = ?",
                                 (d["provider_ref"],)).fetchone()
            if pay_row is None:  # raising rolls back the event row too, so a later retry can succeed
                raise ApiError(404, "not_found", "Unknown provider_ref")

            if pay_row["status"] != status:  # a SUCCESS payment is final and never downgraded
                db.execute("UPDATE payments SET status = ?, updated_at = ? WHERE id = ? AND status != 'SUCCESS'",
                           (status, now(), pay_row["id"]))
            move_booking(db, pay_row["booking_id"], BOOKING_FOR[status])  # no-op if not allowed
            booking_status = db.execute("SELECT status FROM bookings WHERE id = ?",
                                        (pay_row["booking_id"],)).fetchone()[0]
    except sqlite3.IntegrityError:
        raise ApiError(409, "conflict", "Booking already has a different successful payment")

    if status == "SUCCESS" and booking_status != "CONFIRMED":
        log.warning("payment %s succeeded but booking is %s - needs manual refund", d["provider_ref"], booking_status)
    log.info("webhook processed event=%s ref=%s status=%s", d["event_id"], d["provider_ref"], status)
    return jsonify(status="processed", booking_status=booking_status), 200
