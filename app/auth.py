"""Signup, login and the @auth() decorator (JWT, HS256)."""
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Blueprint, current_app, g, jsonify, request

from .db import get_db
from .utils import EMAIL_RE, ApiError, now, validate

bp = Blueprint("auth", __name__, url_prefix="/auth")


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    salt_hex, digest_hex = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def auth(admin=False):
    """Protect a route. Sets g.user. admin=True also requires the admin flag."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise ApiError(401, "unauthorized", "Missing 'Authorization: Bearer <token>' header")
            try:
                claims = jwt.decode(
                    header[7:], current_app.config["SECRET_KEY"], algorithms=["HS256"],
                    options={"require": ["exp", "sub"]},
                )
                user_id = int(claims["sub"])
            except (jwt.PyJWTError, ValueError):
                raise ApiError(401, "invalid_token", "Invalid or expired token")
            user = get_db().execute("SELECT id, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise ApiError(401, "invalid_token", "User no longer exists")
            if admin and not user["is_admin"]:
                raise ApiError(403, "forbidden", "Admin access required")
            g.user = user
            return fn(*args, **kwargs)

        return wrapper

    return decorator


@bp.post("/signup")
def signup():
    d = validate(request.get_json(silent=True), {"name": str, "email": str, "password": str})
    email = d["email"].lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "validation_error", "'email' is not a valid email address")
    if len(d["password"]) < 8:
        raise ApiError(400, "validation_error", "'password' must be at least 8 characters")
    admins = {e.strip().lower() for e in current_app.config["ADMIN_EMAILS"].split(",") if e.strip()}
    try:
        cur = get_db().execute(
            "INSERT INTO users(name, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (d["name"], email, hash_password(d["password"]), int(email in admins), now()),
        )
    except sqlite3.IntegrityError:
        raise ApiError(409, "email_taken", "Email already registered")
    return jsonify(id=cur.lastrowid, name=d["name"], email=email), 201


@bp.post("/login")
def login():
    d = validate(request.get_json(silent=True), {"email": str, "password": str})
    user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?", (d["email"].lower(),)).fetchone()
    if user is None or not verify_password(d["password"], user["password_hash"]):
        raise ApiError(401, "invalid_credentials", "Invalid email or password")  # same message for both
    ttl = current_app.config["JWT_TTL_MINUTES"]
    exp = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    token = jwt.encode({"sub": str(user["id"]), "exp": exp}, current_app.config["SECRET_KEY"], algorithm="HS256")
    return jsonify(access_token=token, token_type="Bearer", expires_in=ttl * 60)
