# channels.py
import os
import logging
import sqlite3
from flask import Blueprint, request, jsonify, abort
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("reficulbot.channels")
DB_PATH = os.getenv("DB_PATH", "whatsapp_logs.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_token")

bp = Blueprint("channels", __name__, url_prefix="/channels")

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_channels_table():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        provider TEXT,       -- e.g. "whatsapp", "instagram", "facebook", "x/twitter"
        config_json TEXT,    -- provider-specific config (webhook id, tokens)
        active INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit(); conn.close()

init_channels_table()

def require_admin(headers):
    token = headers.get("Authorization", "") or ""
    if not token.startswith("Bearer ") or token.split(" ", 1)[1] != ADMIN_TOKEN:
        abort(401)

@bp.route("/register", methods=["POST"])
def register_channel():
    require_admin(request.headers)
    data = request.json or {}
    business_id = data.get("business_id")
    provider = data.get("provider")
    config = data.get("config", {})
    if not business_id or not provider:
        return jsonify({"error":"business_id and provider required"}), 400
    conn = get_db_conn(); c = conn.cursor()
    c.execute("INSERT INTO channels (business_id, provider, config_json) VALUES (?, ?, ?)",
              (business_id, provider, json.dumps(config)))
    conn.commit()
    channel_id = c.lastrowid
    conn.close()
    return jsonify({"id": channel_id, "business_id": business_id, "provider": provider}), 201

@bp.route("/", methods=["GET"])
def list_channels():
    require_admin(request.headers)
    conn = get_db_conn(); c = conn.cursor()
    c.execute("SELECT id, business_id, provider, config_json, active FROM channels")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

# Webhook endpoints (stubs) - these are provider-specific; implement provider logic later
@bp.route("/webhook/instagram", methods=["POST"])
def instagram_webhook():
    # validate signature / subscription per Instagram Graph API
    data = request.json or {}
    logger.info("Instagram webhook received: %s", data)
    # TODO: parse message and forward to same handler used by WhatsApp
    return jsonify({"ok": True})

@bp.route("/webhook/facebook", methods=["POST"])
def facebook_webhook():
    data = request.json or {}
    logger.info("Facebook webhook: %s", data)
    return jsonify({"ok": True})

@bp.route("/webhook/twitter", methods=["POST"])
def twitter_webhook():
    data = request.json or {}
    logger.info("Twitter webhook: %s", data)
    return jsonify({"ok": True})

# End of channels.py
