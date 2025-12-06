# dashboard_api.py
import os
import sqlite3
import logging
from flask import Blueprint, request, jsonify, abort
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("reficulbot.dashboard")
DB_PATH = os.getenv("DB_PATH", "whatsapp_logs.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_token")

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def require_admin(headers):
    token = headers.get("Authorization", "") or ""
    if not token.startswith("Bearer ") or token.split(" ", 1)[1] != ADMIN_TOKEN:
        abort(401)

@bp.route("/business/<int:business_id>/overview", methods=["GET"])
def business_overview(business_id):
    require_admin(request.headers)
    conn = get_db_conn(); c = conn.cursor()
    # message counts last 30 days
    c.execute("""
        SELECT COUNT(*) as total_messages FROM messages WHERE business_id = ?
    """, (business_id,))
    total_messages = c.fetchone()["total_messages"] if c.fetchone() is None else None
    # fetch subscription info (if payments module present)
    try:
        c.execute("SELECT status, stripe_price_id, current_period_end FROM subscriptions WHERE business_id = ? ORDER BY id DESC LIMIT 1", (business_id,))
        sub = c.fetchone()
        sub_info = dict(sub) if sub else None
    except Exception:
        sub_info = None
    # top FAQs asked
    c.execute("""
        SELECT message, COUNT(*) as freq FROM messages
        WHERE business_id = ?
        GROUP BY message ORDER BY freq DESC LIMIT 5
    """, (business_id,))
    top = [{"message": r["message"], "count": r["freq"]} for r in c.fetchall()]
    conn.close()
    return jsonify({"total_messages": total_messages, "subscription": sub_info, "top_questions": top})

@bp.route("/search/messages", methods=["GET"])
def search_messages():
    require_admin(request.headers)
    q = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 50))
    if not q:
        return jsonify([])
    conn = get_db_conn(); c = conn.cursor()
    c.execute("SELECT id, sender, business_id, message, response, timestamp FROM messages WHERE message LIKE ? OR response LIKE ? ORDER BY timestamp DESC LIMIT ?", (f"%{q}%", f"%{q}%", limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

# small helper to fetch billing summary
@bp.route("/billing/summary", methods=["GET"])
def billing_summary():
    require_admin(request.headers)
    conn = get_db_conn(); c = conn.cursor()
    c.execute("SELECT year, month, calls, estimated_cost FROM ai_usage ORDER BY year DESC, month DESC LIMIT 12")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

# End dashboard_api.py
