"""EVE Healthcare - diagnostic bookings API (Flask + SQLite)."""
import logging
import os

from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from .db import close_db, init_db, seed
from .utils import ApiError


def create_app(config=None):
    _static = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, static_folder=_static, static_url_path="/static")
    app.url_map.strict_slashes = False  # /payments and /payments/ both work
    app.config.update(
        DATABASE=os.getenv("DATABASE", "eve.db"),
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-change-me-in-production"),
        WEBHOOK_SECRET=os.getenv("WEBHOOK_SECRET", "dev-webhook-secret"),
        ADMIN_EMAILS=os.getenv("ADMIN_EMAILS", ""),  # comma-separated; these signups become admins
        JWT_TTL_MINUTES=int(os.getenv("JWT_TTL_MINUTES", "60")),
        PAYMENT_SUCCESS_RATE=float(os.getenv("PAYMENT_SUCCESS_RATE", "0.8")),
    )
    app.config.update(config or {})
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    from . import auth, bookings, catalog, payments
    for module in (auth, catalog, bookings, payments):
        app.register_blueprint(module.bp)
    app.register_blueprint(catalog.admin_bp)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    # ── Web UI & Swagger ──────────────────────────────────────────────────────
    @app.get("/")
    def ui_index():
        return send_from_directory(_static, "index.html")

    @app.get("/docs/swagger")
    def swagger_ui():
        return send_from_directory(_static, "swagger.html")

    @app.cli.command("seed")
    def seed_command():
        print("seeded" if seed(app.config["DATABASE"]) else "already has data")

    # every error, expected or not, is JSON in the same shape
    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(error={"code": e.code, "message": e.message}), e.status

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error={"code": e.name.lower().replace(" ", "_"), "message": e.description}), e.code

    @app.errorhandler(Exception)
    def unexpected(e):
        app.logger.exception("unhandled error")
        return jsonify(error={"code": "internal_error", "message": "Internal server error"}), 500

    return app
