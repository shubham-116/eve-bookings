"""Bookings. Every route is scoped to the logged-in user."""
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from .auth import auth
from .db import get_db
from .services import move_booking
from .utils import ApiError, now, page_params, validate

bp = Blueprint("bookings", __name__, url_prefix="/bookings")


def own_booking(db, booking_id):
    """Someone else's booking looks exactly like a missing one (404), so ids can't be probed."""
    row = db.execute("SELECT * FROM bookings WHERE id = ? AND user_id = ?", (booking_id, g.user["id"])).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Booking not found")
    return row


def _future_utc(text):
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "validation_error", "'appointment_at' must be ISO-8601, e.g. 2026-12-01T10:30:00Z")
    when = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    if when <= datetime.now(timezone.utc):
        raise ApiError(400, "validation_error", "'appointment_at' must be in the future")
    return when.isoformat(timespec="seconds")


@bp.post("")
@auth()
def create_booking():
    d = validate(request.get_json(silent=True), {"centre_id": int, "test_id": int, "appointment_at": str})
    when = _future_utc(d["appointment_at"])
    db = get_db()
    offer = db.execute("SELECT price FROM centre_tests WHERE centre_id = ? AND test_id = ?",
                       (d["centre_id"], d["test_id"])).fetchone()
    if offer is None:
        raise ApiError(404, "not_found", "This centre does not offer that test")
    ts = now()
    try:
        cur = db.execute(
            """INSERT INTO bookings(user_id, centre_id, test_id, appointment_at, amount, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (g.user["id"], d["centre_id"], d["test_id"], when, offer["price"], ts, ts))
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate_booking", "You already have an active booking for this slot")
    return jsonify(dict(own_booking(db, cur.lastrowid))), 201


@bp.get("")
@auth()
def list_bookings():
    """GET /bookings?status=PENDING&page=1&page_size=20 (own bookings, newest first)"""
    db = get_db()
    page, size, offset = page_params(request.args)
    where, args = "user_id = ?", [g.user["id"]]
    status = request.args.get("status", "").upper()
    if status:
        where, args = where + " AND status = ?", args + [status]
    total = db.execute(f"SELECT COUNT(*) FROM bookings WHERE {where}", args).fetchone()[0]
    rows = db.execute(f"SELECT * FROM bookings WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                      args + [size, offset]).fetchall()
    return jsonify(items=[dict(r) for r in rows], page=page, page_size=size, total=total)


@bp.get("/<int:booking_id>")
@auth()
def get_booking(booking_id):
    db = get_db()
    booking = dict(own_booking(db, booking_id))
    pays = db.execute("SELECT id, provider_ref, amount, status, created_at FROM payments "
                      "WHERE booking_id = ? ORDER BY id", (booking_id,)).fetchall()
    return jsonify(**booking, payments=[dict(p) for p in pays])


@bp.post("/<int:booking_id>/cancel")
@auth()
def cancel_booking(booking_id):
    db = get_db()
    own_booking(db, booking_id)
    if not move_booking(db, booking_id, "CANCELLED"):
        raise ApiError(409, "invalid_state", "Booking is already cancelled")
    return jsonify(dict(own_booking(db, booking_id)))
