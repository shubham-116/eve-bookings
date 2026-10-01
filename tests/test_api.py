"""Run with:  python -m unittest -v   (pytest also works)"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app import create_app

WH_SECRET = "whsec"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(timespec="seconds")


class ApiTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app({"DATABASE": self.path, "SECRET_KEY": "k" * 32, "WEBHOOK_SECRET": WH_SECRET,
                               "ADMIN_EMAILS": "admin@x.com", "TESTING": True})
        self.c = self.app.test_client()
        self.admin = self.login("admin@x.com")
        self.user = self.login("alice@x.com")
        self.other = self.login("bob@x.com")
        # one centre offering one test at 500.00 (50000 paise)
        self.centre = self.c.post("/centres", json={"name": "Lab", "location": "Ranchi"}, headers=self.admin).json["id"]
        tests = self.c.post(f"/centres/{self.centre}/tests", json={"name": "CBC", "price": 50000}, headers=self.admin)
        self.test_id = tests.json["tests"][0]["id"]

    def tearDown(self):
        os.remove(self.path)

    # ---- helpers ----
    def login(self, email):
        self.c.post("/auth/signup", json={"name": email, "email": email, "password": "password123"})
        token = self.c.post("/auth/login", json={"email": email, "password": "password123"}).json["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def book(self, headers=None, when=FUTURE):
        return self.c.post("/bookings", headers=headers or self.user, json={
            "centre_id": self.centre, "test_id": self.test_id, "appointment_at": when})

    def pay(self, booking_id, simulate="SUCCESS", headers=None):
        return self.c.post("/payments/", headers=headers or self.user,
                           json={"booking_id": booking_id, "simulate": simulate})

    def hook(self, secret=WH_SECRET, **payload):
        raw = json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.c.post("/payments/webhook/", data=raw,
                           headers={"X-Signature": sig, "Content-Type": "application/json"})

    def status(self, booking_id):
        return self.c.get(f"/bookings/{booking_id}", headers=self.user).json["status"]

    def payment_count(self, booking_id):
        return len(self.c.get(f"/bookings/{booking_id}", headers=self.user).json["payments"])

    # ---- auth ----
    def test_signup_validation_and_duplicates(self):
        bad = self.c.post("/auth/signup", json={"name": "x", "email": "nope", "password": "password123"})
        self.assertEqual(bad.status_code, 400)
        short = self.c.post("/auth/signup", json={"name": "x", "email": "a@b.co", "password": "123"})
        self.assertEqual(short.status_code, 400)
        dup = self.c.post("/auth/signup", json={"name": "x", "email": "ALICE@x.com", "password": "password123"})
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(self.c.post("/auth/signup", data="not json").status_code, 400)

    def test_login_and_token_checks(self):
        r = self.c.post("/auth/login", json={"email": "alice@x.com", "password": "wrong-password"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get("/bookings").status_code, 401)
        self.assertEqual(self.c.get("/bookings", headers={"Authorization": "Bearer junk"}).status_code, 401)

    # ---- catalog ----
    def test_catalog_admin_only_write_public_read(self):
        r = self.c.post("/centres", json={"name": "N", "location": "L"}, headers=self.user)
        self.assertEqual(r.status_code, 403)
        listing = self.c.get("/centres?location=ranchi").json
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["tests"][0]["price"], 50000)
        self.assertEqual(self.c.get("/centres?location=delhi").json["total"], 0)
        self.assertEqual(self.c.get("/centres?page_size=0").status_code, 400)
        self.assertEqual(self.c.get("/centres/999").status_code, 404)

    def test_price_update_and_bad_price(self):
        self.c.post(f"/centres/{self.centre}/tests", json={"name": "cbc", "price": 40000}, headers=self.admin)
        tests = self.c.get(f"/centres/{self.centre}").json["tests"]
        self.assertEqual([(t["name"], t["price"]) for t in tests], [("CBC", 40000)])  # same test, new price
        bad = self.c.post(f"/centres/{self.centre}/tests", json={"name": "X", "price": -5}, headers=self.admin)
        self.assertEqual(bad.status_code, 400)

    # ---- bookings ----
    def test_create_booking_and_edge_cases(self):
        b = self.book()
        self.assertEqual((b.status_code, b.json["status"], b.json["amount"]), (201, "PENDING", 50000))
        self.assertEqual(self.book().status_code, 409)  # same slot twice
        self.assertEqual(self.book(when="2020-01-01T10:00:00Z").status_code, 400)  # past
        self.assertEqual(self.book(when="tomorrow").status_code, 400)  # not ISO
        wrong = self.c.post("/bookings", headers=self.user,
                            json={"centre_id": self.centre, "test_id": 999, "appointment_at": FUTURE})
        self.assertEqual(wrong.status_code, 404)  # centre doesn't offer it
        self.assertEqual(self.c.post("/bookings", headers=self.user, json={"centre_id": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/bookings", json={}).status_code, 401)

    def test_bookings_are_private(self):
        bid = self.book().json["id"]
        self.assertEqual(self.c.get(f"/bookings/{bid}", headers=self.other).status_code, 404)
        self.assertEqual(self.c.post(f"/bookings/{bid}/cancel", headers=self.other).status_code, 404)
        self.assertEqual(self.pay(bid, headers=self.other).status_code, 404)
        self.assertEqual(self.c.get("/bookings", headers=self.other).json["total"], 0)
        self.assertEqual(self.c.get("/bookings/abc", headers=self.user).status_code, 404)
        self.assertEqual(self.status(bid), "PENDING")

    def test_cancel(self):
        bid = self.book().json["id"]
        self.assertEqual(self.c.post(f"/bookings/{bid}/cancel", headers=self.user).json["status"], "CANCELLED")
        self.assertEqual(self.c.post(f"/bookings/{bid}/cancel", headers=self.user).status_code, 409)
        self.assertEqual(self.pay(bid).status_code, 409)  # can't pay a cancelled booking
        self.assertEqual(self.book().status_code, 201)  # slot is free again

    # ---- payments ----
    def test_payment_success_confirms_booking(self):
        bid = self.book().json["id"]
        r = self.pay(bid)
        self.assertEqual((r.status_code, r.json["status"], r.json["booking_status"]), (201, "SUCCESS", "CONFIRMED"))
        self.assertEqual(self.status(bid), "CONFIRMED")
        self.assertEqual(self.pay(bid).status_code, 409)  # no double payment

    def test_failed_payment_then_retry(self):
        bid = self.book().json["id"]
        self.assertEqual(self.pay(bid, "FAILED").json["booking_status"], "FAILED")
        self.assertEqual(self.pay(bid, "FAILED").status_code, 201)  # failing again is fine
        self.assertEqual(self.pay(bid, "SUCCESS").json["booking_status"], "CONFIRMED")
        self.assertEqual(self.payment_count(bid), 3)

    def test_payment_validation(self):
        self.assertEqual(self.c.post("/payments/", headers=self.user, json={"booking_id": 999}).status_code, 404)
        self.assertEqual(self.c.post("/payments/", headers=self.user, json={"booking_id": "x"}).status_code, 400)
        bid = self.book().json["id"]
        self.assertEqual(self.pay(bid, "MAYBE").status_code, 400)
        self.assertEqual(self.c.post("/payments/", json={"booking_id": bid}).status_code, 401)

    # ---- webhook ----
    def test_webhook_rejects_bad_signature_and_bad_payload(self):
        bid = self.book().json["id"]
        ref = self.pay(bid, "FAILED").json["provider_ref"]
        r = self.hook(secret="wrong", event_id="e1", provider_ref=ref, status="SUCCESS")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.hook(event_id="e1", provider_ref=ref, status="WEIRD").status_code, 400)
        self.assertEqual(self.hook(event_id="e1", status="SUCCESS").status_code, 400)
        self.assertEqual(self.status(bid), "FAILED")  # nothing changed

    def test_webhook_is_idempotent(self):
        bid = self.book().json["id"]
        ref = self.pay(bid, "FAILED").json["provider_ref"]
        first = self.hook(event_id="evt_1", provider_ref=ref, status="SUCCESS")
        self.assertEqual((first.status_code, first.json["status"], first.json["booking_status"]),
                         (200, "processed", "CONFIRMED"))
        for _ in range(3):  # provider retries the exact same event
            again = self.hook(event_id="evt_1", provider_ref=ref, status="SUCCESS")
            self.assertEqual((again.status_code, again.json["status"]), (200, "duplicate"))
        self.assertEqual(self.payment_count(bid), 1)  # no duplicate payments
        self.assertEqual(self.status(bid), "CONFIRMED")

    def test_replayed_event_id_cannot_flip_state(self):
        bid = self.book().json["id"]
        ref = self.pay(bid, "FAILED").json["provider_ref"]
        self.hook(event_id="evt_1", provider_ref=ref, status="SUCCESS")
        # same event_id, different (bogus) body: still treated as a duplicate
        self.assertEqual(self.hook(event_id="evt_1", provider_ref=ref, status="FAILED").json["status"], "duplicate")
        self.assertEqual(self.status(bid), "CONFIRMED")

    def test_late_failure_event_never_downgrades_confirmed(self):
        bid = self.book().json["id"]
        ref = self.pay(bid, "SUCCESS").json["provider_ref"]
        r = self.hook(event_id="evt_late", provider_ref=ref, status="FAILED")  # different event id
        self.assertEqual(r.json["booking_status"], "CONFIRMED")
        pay = self.c.get(f"/bookings/{bid}", headers=self.user).json["payments"][0]
        self.assertEqual(pay["status"], "SUCCESS")

    def test_success_event_for_cancelled_booking_keeps_it_cancelled(self):
        bid = self.book().json["id"]
        ref = self.pay(bid, "SUCCESS").json["provider_ref"]
        self.c.post(f"/bookings/{bid}/cancel", headers=self.user)
        r = self.hook(event_id="evt_x", provider_ref=ref, status="SUCCESS")
        self.assertEqual(r.json["booking_status"], "CANCELLED")  # cancelled stays cancelled

    def test_unknown_payment_ref_is_404_and_not_recorded(self):
        r = self.hook(event_id="evt_9", provider_ref="pay_nope", status="SUCCESS")
        self.assertEqual(r.status_code, 404)
        bid = self.book().json["id"]
        ref = self.pay(bid, "FAILED").json["provider_ref"]
        # same event_id later works, because the failed attempt was rolled back
        self.assertEqual(self.hook(event_id="evt_9", provider_ref=ref, status="SUCCESS").json["status"], "processed")

    def test_second_success_for_same_booking_conflicts(self):
        bid = self.book().json["id"]
        ref1 = self.pay(bid, "FAILED").json["provider_ref"]
        self.pay(bid, "SUCCESS")
        r = self.hook(event_id="evt_dup", provider_ref=ref1, status="SUCCESS")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.status(bid), "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
