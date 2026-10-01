"""Booking state machine, shared by the payment endpoint, the webhook and cancel."""
from .utils import now

# target status -> statuses it may be entered from. Anything else is ignored, which is
# what keeps late / duplicate / out-of-order events from corrupting a booking.
#   PENDING -> CONFIRMED | FAILED | CANCELLED     FAILED -> CONFIRMED (retry) | CANCELLED
#   CONFIRMED -> CANCELLED                        CANCELLED -> (terminal)
ALLOWED_FROM = {
    "CONFIRMED": ("PENDING", "FAILED"),
    "FAILED": ("PENDING", "FAILED"),
    "CANCELLED": ("PENDING", "FAILED", "CONFIRMED"),
}
BOOKING_FOR = {"SUCCESS": "CONFIRMED", "FAILED": "FAILED"}  # payment result -> booking status


def move_booking(db, booking_id, to):
    """Atomic compare-and-set (one UPDATE, no read-then-write race). True if it moved."""
    allowed = ALLOWED_FROM[to]
    marks = ",".join("?" * len(allowed))
    cur = db.execute(
        f"UPDATE bookings SET status = ?, updated_at = ? WHERE id = ? AND status IN ({marks})",
        (to, now(), booking_id, *allowed),
    )
    return cur.rowcount == 1
