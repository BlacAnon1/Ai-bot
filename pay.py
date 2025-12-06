# payments.py
import os
import logging
import sqlite3
import json
from flask import Blueprint, request, jsonify, abort
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("reficulbot.payments")

# Stripe optional import (install: pip install stripe)
try:
    import stripe
except Exception:
    stripe = None
    logger.warning("stripe library not installed. Install with: pip install stripe")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DEFAULT_PRICE_ID = os.getenv("STRIPE_DEFAULT_PRICE_ID")  # price id for subscription
DB_PATH = os.getenv("DB_PATH", "whatsapp_logs.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_token")

bp = Blueprint("payments", __name__, url_prefix="/payments")

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.close()
    except Exception:
        logger.exception("pragmas failed")
    return conn

# Create payments/subscriptions tables if they don't exist (migration helper)
def init_payments_db():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        stripe_customer_id TEXT,
        stripe_payment_intent TEXT,
        amount REAL,
        currency TEXT,
        status TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        stripe_sub_id TEXT,
        stripe_price_id TEXT,
        status TEXT,
        current_period_end INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()

init_payments_db()

def require_admin(headers):
    token = headers.get("Authorization", "") or ""
    if not token.startswith("Bearer ") or token.split(" ", 1)[1] != ADMIN_TOKEN:
        abort(401)

@bp.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create Stripe Checkout session for a business subscription.
    Payload: { "business_id": X, "price_id": optional }"""
    require_admin(request.headers)
    data = request.json or {}
    business_id = data.get("business_id")
    price_id = data.get("price_id") or DEFAULT_PRICE_ID
    if not stripe or not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 500
    if not business_id or not price_id:
        return jsonify({"error": "business_id and price_id required"}), 400

    stripe.api_key = STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"business_id": str(business_id)},
            success_url=os.getenv("STRIPE_SUCCESS_URL", "https://yourdomain.com/success?session_id={CHECKOUT_SESSION_ID}"),
            cancel_url=os.getenv("STRIPE_CANCEL_URL", "https://yourdomain.com/cancel"),
        )
        return jsonify({"sessionId": session["id"], "url": session["url"]})
    except Exception as e:
        logger.exception("create_checkout_session failed")
        return jsonify({"error": str(e)}), 500

@bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Receive Stripe webhook events (configure STRIPE_WEBHOOK_SECRET)"""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    event = None
    if stripe and STRIPE_WEBHOOK_SECRET:
        stripe.api_key = STRIPE_SECRET_KEY
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            logger.exception("Stripe webhook signature verification failed")
            return jsonify({"error": "invalid signature"}), 400
    else:
        # If no signature secret, attempt to parse body (not recommended for prod)
        try:
            event = json.loads(payload)
        except Exception:
            return jsonify({"error": "invalid payload"}), 400

    typ = event.get("type")
    logger.info("Stripe event received: %s", typ)

    # handle subscription created/updated/expired and payment succeeded
    if typ == "checkout.session.completed":
        session = event["data"]["object"]
        customer = session.get("customer")
        metadata = session.get("metadata") or {}
        business_id = metadata.get("business_id")
        # link the customer to business in subscriptions table if subscription created
        if session.get("mode") == "subscription" and session.get("subscription"):
            sub_id = session["subscription"]
            # store subscription
            conn = get_db_conn(); c = conn.cursor()
            c.execute("""
                INSERT INTO subscriptions (business_id, stripe_sub_id, stripe_price_id, status)
                VALUES (?, ?, ?, ?)
            """, (business_id, sub_id, None, "active"))
            conn.commit(); conn.close()
    elif typ == "invoice.paid":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        amount = invoice.get("amount_paid")/100.0 if invoice.get("amount_paid") is not None else None
        currency = invoice.get("currency")
        # record payment
        conn = get_db_conn(); c = conn.cursor()
        c.execute("SELECT business_id FROM subscriptions WHERE stripe_sub_id = ?", (sub_id,))
        row = c.fetchone()
        business_id = row["business_id"] if row else None
        c.execute("""
            INSERT INTO payments (business_id, stripe_payment_intent, amount, currency, status)
            VALUES (?, ?, ?, ?, ?)
        """, (business_id, invoice.get("payment_intent"), amount, currency, "paid"))
        conn.commit(); conn.close()
    elif typ == "invoice.payment_failed":
        invoice = event["data"]["object"]
        # record failed payment
        conn = get_db_conn(); c = conn.cursor()
        c.execute("INSERT INTO payments (business_id, stripe_payment_intent, amount, currency, status) VALUES (?, ?, ?, ?, ?)",
                  (None, invoice.get("payment_intent"), (invoice.get("amount_due")/100 if invoice.get("amount_due") else None), invoice.get("currency"), "failed"))
        conn.commit(); conn.close()
    # Add other event handlers as needed
    return jsonify({"ok": True}), 200

@bp.route("/subscriptions/<int:business_id>", methods=["GET"])
def get_subscription(business_id):
    require_admin(request.headers)
    conn = get_db_conn(); c = conn.cursor()
    c.execute("SELECT id, stripe_sub_id, stripe_price_id, status, current_period_end FROM subscriptions WHERE business_id = ?", (business_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

# End of payments.py
