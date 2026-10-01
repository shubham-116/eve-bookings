"""Diagnostic centres and the tests they offer. Reads are public; writes are admin-only."""
import sqlite3

from flask import Blueprint, jsonify, request

from .auth import auth
from .db import get_db
from .utils import ApiError, page_params, tx, validate

bp = Blueprint("catalog", __name__, url_prefix="/centres")


def _with_tests(db, centres):
    """Attach each centre's tests using ONE extra query (no N+1)."""
    ids = [c["id"] for c in centres]
    by_centre = {i: [] for i in ids}
    if ids:
        marks = ",".join("?" * len(ids))
        rows = db.execute(
            f"""SELECT ct.centre_id, t.id, t.name, ct.price
                FROM centre_tests ct JOIN tests t ON t.id = ct.test_id
                WHERE ct.centre_id IN ({marks}) ORDER BY t.name""", ids)
        for r in rows:
            by_centre[r["centre_id"]].append({"id": r["id"], "name": r["name"], "price": r["price"]})
    return [{**dict(c), "tests": by_centre[c["id"]]} for c in centres]


def _centre_or_404(db, centre_id):
    row = db.execute("SELECT id, name, location FROM centres WHERE id = ?", (centre_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Centre not found")
    return row


@bp.get("")
def list_centres():
    """GET /centres?location=ranchi&page=1&page_size=20"""
    db = get_db()
    page, size, offset = page_params(request.args)
    like = f"%{request.args.get('location', '').strip().lower()}%"
    total = db.execute("SELECT COUNT(*) FROM centres WHERE lower(location) LIKE ?", (like,)).fetchone()[0]
    rows = db.execute(
        "SELECT id, name, location FROM centres WHERE lower(location) LIKE ? ORDER BY id LIMIT ? OFFSET ?",
        (like, size, offset)).fetchall()
    return jsonify(items=_with_tests(db, rows), page=page, page_size=size, total=total)


@bp.get("/<int:centre_id>")
def get_centre(centre_id):
    db = get_db()
    return jsonify(_with_tests(db, [_centre_or_404(db, centre_id)])[0])


@bp.post("")
@auth(admin=True)
def create_centre():
    d = validate(request.get_json(silent=True), {"name": str, "location": str})
    try:
        cur = get_db().execute("INSERT INTO centres(name, location) VALUES (?, ?)", (d["name"], d["location"]))
    except sqlite3.IntegrityError:
        raise ApiError(409, "duplicate", "Centre already exists at this location")
    return jsonify(id=cur.lastrowid, **d, tests=[]), 201


@bp.post("/<int:centre_id>/tests")
@auth(admin=True)
def add_test(centre_id):
    """Offer a test at a centre (creates the test if new). Re-posting updates the price."""
    db = get_db()
    _centre_or_404(db, centre_id)
    d = validate(request.get_json(silent=True), {"name": str, "price": int})
    if d["price"] <= 0:
        raise ApiError(400, "validation_error", "'price' must be a positive integer (paise)")
    with tx(db):
        db.execute("INSERT INTO tests(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (d["name"],))
        test_id = db.execute("SELECT id FROM tests WHERE name = ?", (d["name"],)).fetchone()[0]
        db.execute(
            """INSERT INTO centre_tests(centre_id, test_id, price) VALUES (?, ?, ?)
               ON CONFLICT(centre_id, test_id) DO UPDATE SET price = excluded.price""",
            (centre_id, test_id, d["price"]))
    return jsonify(_with_tests(db, [_centre_or_404(db, centre_id)])[0]), 201


@bp.delete("/<int:centre_id>/tests/<int:test_id>")
@auth(admin=True)
def remove_test(centre_id, test_id):
    """Remove a test offering from a centre (admin only)."""
    db = get_db()
    _centre_or_404(db, centre_id)
    rows = db.execute(
        "DELETE FROM centre_tests WHERE centre_id = ? AND test_id = ?",
        (centre_id, test_id),
    ).rowcount
    if not rows:
        raise ApiError(404, "not_found", "This centre does not offer that test")
    return jsonify(message="Test removed from centre"), 200


# ── Admin dashboard stats ─────────────────────────────────────────────────────
from flask import Blueprint as _BP  # noqa – reuse existing import  # type: ignore
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.get("/stats")
@auth(admin=True)
def admin_stats():
    """System-wide stats for the admin dashboard."""
    db = get_db()
    stats = {}
    stats["centres"]      = db.execute("SELECT COUNT(*) FROM centres").fetchone()[0]
    stats["tests"]        = db.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    stats["users"]        = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stats["bookings"]     = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    stats["payments"]     = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]

    by_status = db.execute(
        "SELECT status, COUNT(*) as cnt FROM bookings GROUP BY status"
    ).fetchall()
    stats["bookings_by_status"] = {r["status"]: r["cnt"] for r in by_status}

    rev = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='SUCCESS'"
    ).fetchone()[0]
    stats["total_revenue_paise"] = rev

    recent = db.execute(
        """SELECT b.id, u.name AS user_name, c.name AS centre_name,
                  t.name AS test_name, b.status, b.amount, b.appointment_at
           FROM bookings b
           JOIN users u ON u.id = b.user_id
           JOIN centres c ON c.id = b.centre_id
           JOIN tests t ON t.id = b.test_id
           ORDER BY b.id DESC LIMIT 10"""
    ).fetchall()
    stats["recent_bookings"] = [dict(r) for r in recent]

    all_centres = db.execute(
        """SELECT c.id, c.name, c.location,
                  COUNT(DISTINCT ct.test_id) AS test_count,
                  COUNT(b.id) AS booking_count
           FROM centres c
           LEFT JOIN centre_tests ct ON ct.centre_id = c.id
           LEFT JOIN bookings b ON b.centre_id = c.id
           GROUP BY c.id ORDER BY c.id"""
    ).fetchall()
    stats["centres_list"] = [dict(r) for r in all_centres]

    all_tests = db.execute(
        """SELECT t.id, t.name, COUNT(ct.centre_id) AS offered_at
           FROM tests t
           LEFT JOIN centre_tests ct ON ct.test_id = t.id
           GROUP BY t.id ORDER BY t.name"""
    ).fetchall()
    stats["tests_list"] = [dict(r) for r in all_tests]

    return jsonify(stats)
