# app.py (Option A) - Priority 1 complete (rate-limits, welcome messages, cache, retries, dead-letter, rotating logs)
from flask import Flask, request, jsonify, abort
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
from dotenv import load_dotenv
import sqlite3
import logging
import traceback
import requests
import tempfile
import time
import hashlib
from logging.handlers import RotatingFileHandler

# --- load env ---
load_dotenv()

# --- config ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
DB_PATH = os.getenv("DB_PATH", "whatsapp_logs.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change_this_token")
PORT = int(os.getenv("PORT", 5600))

# Rate limit settings (configurable via env if you want)
PER_SENDER_LIMIT = int(os.getenv("PER_SENDER_LIMIT", 30))       # messages per WINDOW_PER_SECONDS per sender
PER_BUSINESS_LIMIT = int(os.getenv("PER_BUSINESS_LIMIT", 600))  # messages per WINDOW_PER_SECONDS per business
WINDOW_PER_SECONDS = int(os.getenv("WINDOW_PER_SECONDS", 60))   # sliding window seconds

# AI budgeting
BUDGET_MONTHLY_USD = float(os.getenv("BUDGET_MONTHLY_USD", 50.0))  # alert threshold
EST_COST_PER_CALL_USD = float(os.getenv("EST_COST_PER_CALL_USD", 0.002))  # estimated cost per call (config)

# Logging setup - rotating file
logger = logging.getLogger()
logger.setLevel(logging.INFO)
log_handler = RotatingFileHandler("app.log", maxBytes=5*1024*1024, backupCount=3)
log_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
log_handler.setFormatter(log_formatter)
logger.addHandler(log_handler)

# also add console handler for dev
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# --- Flask + OpenAI client ---
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- defaults and utils ---
DEFAULT_FAQS = {
    "hours": "We are open Mon-Sat, 9am-6pm.",
    "pricing": "Please check our website for the latest pricing.",
    "shipping": "Orders are delivered within 3-5 business days."
}

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

# --- DB init (adds tables required for Priority 1) ---
def init_db():
    conn = get_db_conn()
    c = conn.cursor()

    # leads + lead state
    c.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        sender TEXT,
        name TEXT,
        phone TEXT,
        email TEXT,
        service TEXT,
        budget TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS lead_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT UNIQUE,
        business_id INTEGER,
        step INTEGER DEFAULT 0,
        name TEXT,
        phone TEXT,
        email TEXT,
        service TEXT,
        budget TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # messages log
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        business_id INTEGER,
        message TEXT,
        response TEXT,
        is_faq INTEGER DEFAULT 0,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # businesses with welcome_message, language, monthly_budget
    c.execute("""
    CREATE TABLE IF NOT EXISTS businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        twilio_number TEXT UNIQUE,
        lead_mode INTEGER DEFAULT 0,
        welcome_message TEXT,
        language TEXT,
        monthly_budget REAL DEFAULT 0
    );
    """)

    # faqs (global or per business)
    c.execute("""
    CREATE TABLE IF NOT EXISTS faqs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        key TEXT,
        response TEXT,
        UNIQUE(business_id, key)
    );
    """)

    # rate limits table - simple sliding window buckets
    c.execute("""
    CREATE TABLE IF NOT EXISTS rate_limits (
        key TEXT PRIMARY KEY,
        window_start INTEGER,
        count INTEGER
    );
    """)

    # ai cache table for identical prompts (saves cost)
    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt_hash TEXT UNIQUE,
        prompt TEXT,
        response TEXT,
        hits INTEGER DEFAULT 0,
        last_used DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # ai usage tracking (monthly)
    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER,
        month INTEGER,
        calls INTEGER DEFAULT 0,
        estimated_cost REAL DEFAULT 0
    );
    """)

    # dead letters (messages that failed/need human review)
    c.execute("""
    CREATE TABLE IF NOT EXISTS dead_letters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        business_id INTEGER,
        message TEXT,
        error TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    conn.close()
    seed_global_faqs()
    ensure_monthly_usage_row()

def seed_global_faqs():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM faqs WHERE business_id IS NULL")
    row = c.fetchone()
    cnt = row["cnt"] if row else 0
    if cnt == 0:
        for k, v in DEFAULT_FAQS.items():
            c.execute("INSERT INTO faqs (business_id, key, response) VALUES (?, ?, ?)", (None, k, v))
        conn.commit()
    conn.close()

def ensure_monthly_usage_row():
    # ensure there's a row for current month/year
    now = time.localtime()
    y, m = now.tm_year, now.tm_mon
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM ai_usage WHERE year = ? AND month = ?", (y, m))
    if not c.fetchone():
        c.execute("INSERT INTO ai_usage (year, month, calls, estimated_cost) VALUES (?, ?, 0, 0)", (y, m))
        conn.commit()
    conn.close()

# initialize DB
init_db()

# --- Rate limiting helpers (SQLite-backed) ---
def _rl_key(scope, identifier):
    # scope = "sender" or "business"
    return f"rl:{scope}:{identifier}"

def check_and_increment_rate_limit(scope, identifier, limit, window_seconds=WINDOW_PER_SECONDS):
    """
    returns (allowed:bool, remaining:int)
    SQLite-backed sliding window using simple window_start buckets.
    Not perfect sliding, but okay for MVP and shared across workers.
    """
    if not identifier:
        return True, limit

    key = _rl_key(scope, identifier)
    now = int(time.time())
    window_start = now - (now % window_seconds)

    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT window_start, count FROM rate_limits WHERE key = ?", (key,))
        row = c.fetchone()
        if row:
            row_ws, row_count = row["window_start"], row["count"]
            if row_ws == window_start:
                if row_count >= limit:
                    # blocked
                    return False, 0
                # increment
                c.execute("UPDATE rate_limits SET count = count + 1 WHERE key = ?", (key,))
                conn.commit()
                return True, limit - (row_count + 1)
            else:
                # new window - reset
                c.execute("UPDATE rate_limits SET window_start = ?, count = 1 WHERE key = ?", (window_start, key))
                conn.commit()
                return True, limit - 1
        else:
            # create
            c.execute("INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, ?)", (key, window_start, 1))
            conn.commit()
            return True, limit - 1
    except Exception:
        logging.exception("Rate limit DB error")
        # Fail open on DB error
        return True, limit
    finally:
        conn.close()

# --- AI cache helpers ---
def prompt_hash(prompt):
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

def ai_cache_get(prompt):
    ph = prompt_hash(prompt)
    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT response, hits FROM ai_cache WHERE prompt_hash = ?", (ph,))
        row = c.fetchone()
        if row:
            # update hits + last_used
            c.execute("UPDATE ai_cache SET hits = hits + 1, last_used = CURRENT_TIMESTAMP WHERE prompt_hash = ?", (ph,))
            conn.commit()
            return row["response"]
        return None
    finally:
        conn.close()

def ai_cache_set(prompt, response):
    ph = prompt_hash(prompt)
    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT OR REPLACE INTO ai_cache (prompt_hash, prompt, response, hits, last_used) VALUES (?, ?, ?, COALESCE((SELECT hits FROM ai_cache WHERE prompt_hash = ?), 0) + 1, CURRENT_TIMESTAMP)", (ph, prompt, response, ph))
        conn.commit()
    except Exception:
        logging.exception("ai_cache_set failed")
    finally:
        conn.close()

# --- AI usage tracking ---
def record_ai_call(estimated_cost=BUDGET_MONTHLY_USD):
    now = time.localtime()
    y, m = now.tm_year, now.tm_mon
    conn = get_db_conn()
    c = conn.cursor()
    try:
        # increment calls and cost
        c.execute("UPDATE ai_usage SET calls = calls + 1, estimated_cost = estimated_cost + ? WHERE year = ? AND month = ?", (EST_COST_PER_CALL_USD, y, m))
        conn.commit()
        # check monthly
        c.execute("SELECT estimated_cost FROM ai_usage WHERE year = ? AND month = ?", (y, m))
        row = c.fetchone()
        est = float(row["estimated_cost"] or 0)
        if est >= BUDGET_MONTHLY_USD:
            logging.warning("Monthly AI estimated cost exceeded threshold: $%s >= $%s", est, BUDGET_MONTHLY_USD)
            # you can add admin notification code here (email/Slack) later
    except Exception:
        logging.exception("record_ai_call failed")
    finally:
        conn.close()

# --- Dead-letter logging ---
def log_dead_letter(sender, business_id, message, error_text):
    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO dead_letters (sender, business_id, message, error) VALUES (?, ?, ?, ?)", (sender, business_id, message, error_text))
        conn.commit()
    except Exception:
        logging.exception("log_dead_letter failed")
    finally:
        conn.close()

# --- OpenAI call with retry/backoff + caching ---
def call_openai_with_retry(prompt, max_retries=3):
    """
    - Check ai_cache first
    - Attempt OpenAI call with retry and exponential backoff.
    - On success, store in cache and record usage.
    - On final failure, return None and caller should handle dead-letter logging.
    """
    # check cache
    cached = ai_cache_get(prompt)
    if cached:
        logging.info("AI cache hit")
        return cached, True  # True -> cache hit

    # Not cached: call OpenAI with retries
    backoff = 1
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            logging.info("OpenAI call attempt %s", attempt)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=250
            )
            # defensive extraction
            bot_reply = None
            if getattr(resp, "choices", None):
                choice0 = resp.choices[0]
                if getattr(choice0, "message", None):
                    if isinstance(choice0.message, dict):
                        bot_reply = choice0.message.get("content")
                    else:
                        bot_reply = getattr(choice0.message, "content", None)
            if not bot_reply:
                bot_reply = getattr(resp, "text", None)
            if bot_reply:
                # record usage + cache
                record_ai_call()
                ai_cache_set(prompt, bot_reply)
                return bot_reply, False
            last_err = "Empty response"
        except Exception as e:
            last_err = str(e)
            logging.exception("OpenAI attempt %s failed: %s", attempt, last_err)
        time.sleep(backoff)
        backoff *= 2
    # All retries failed
    return None, False

# --- Existing features: fetch_faq, insert_message, business lookup, lead flow, menu (kept in same structure) ---
def fetch_faq(business_id, key):
    if not key:
        return None
    conn = get_db_conn()
    c = conn.cursor()
    try:
        if business_id is not None:
            c.execute("SELECT response FROM faqs WHERE business_id = ? AND key = ?", (business_id, key))
            row = c.fetchone()
            if row:
                return row["response"]
        c.execute("SELECT response FROM faqs WHERE business_id IS NULL AND key = ?", (key,))
        row = c.fetchone()
        return row["response"] if row else None
    finally:
        conn.close()

def insert_message(sender, business_id, message, response, is_faq):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages (sender, business_id, message, response, is_faq) VALUES (?, ?, ?, ?, ?)",
            (sender, business_id, message, response, int(bool(is_faq)))
        )
        conn.commit()
        conn.close()
        logging.info("Inserted message by %s business=%s", sender, business_id)
    except Exception:
        logging.exception("insert_message failed: %s", traceback.format_exc())

def get_business_id_from_twilio(to_number):
    if not to_number:
        return None
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM businesses WHERE twilio_number = ?", (to_number,))
        row = c.fetchone()
        conn.close()
        return row["id"] if row else None
    except Exception:
        logging.exception("get_business_id_from_twilio failed")
        return None

# lead flow - same as before
def handle_lead_flow(sender, business_id, incoming_msg):
    if not business_id:
        return None
    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT lead_mode FROM businesses WHERE id = ?", (business_id,))
        row = c.fetchone()
        if not row or int(row["lead_mode"]) != 1:
            return None
        c.execute("SELECT * FROM lead_states WHERE sender = ?", (sender,))
        state = c.fetchone()
        if not state:
            c.execute("INSERT OR REPLACE INTO lead_states (sender, business_id, step, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (sender, business_id, 1))
            conn.commit()
            return "Welcome! Let's get your details. What's your full name?"
        step = int(state["step"] or 0)
        if step == 1:
            c.execute("UPDATE lead_states SET name = ?, step = ?, updated_at = CURRENT_TIMESTAMP WHERE sender = ?", (incoming_msg.strip(), 2, sender))
            conn.commit()
            return "Great! Please share your phone number."
        if step == 2:
            c.execute("UPDATE lead_states SET phone = ?, step = ?, updated_at = CURRENT_TIMESTAMP WHERE sender = ?", (incoming_msg.strip(), 3, sender))
            conn.commit()
            return "Thanks! What's your email address?"
        if step == 3:
            c.execute("UPDATE lead_states SET email = ?, step = ?, updated_at = CURRENT_TIMESTAMP WHERE sender = ?", (incoming_msg.strip(), 4, sender))
            conn.commit()
            return "Got it! What service are you interested in?"
        if step == 4:
            c.execute("UPDATE lead_states SET service = ?, step = ?, updated_at = CURRENT_TIMESTAMP WHERE sender = ?", (incoming_msg.strip(), 5, sender))
            conn.commit()
            return "Great choice! Lastly, what's your budget?"
        if step == 5:
            budget = incoming_msg.strip()
            c.execute("UPDATE lead_states SET budget = ?, step = ?, updated_at = CURRENT_TIMESTAMP WHERE sender = ?", (budget, 6, sender))
            conn.commit()
            c.execute("""INSERT INTO leads (business_id, sender, name, phone, email, service, budget)
                         SELECT business_id, sender, name, phone, email, service, budget FROM lead_states WHERE sender = ?""", (sender,))
            conn.commit()
            c.execute("DELETE FROM lead_states WHERE sender = ?", (sender,))
            conn.commit()
            return "Perfect! Your details have been saved. Our team will contact you shortly."
        return None
    except Exception:
        logging.exception("Error in handle_lead_flow")
        return None
    finally:
        conn.close()

MENU_TEXT = (
    "Welcome to ReficulBot!\n"
    "Send a number to choose an option:\n"
    "1. Opening hours\n"
    "2. Pricing\n"
    "3. Shipping info\n"
    "4. Speak to a human\n"
    "Send 'help' or 'menu' to show this again."
)

def handle_command(lower_text, sender, business_id):
    if lower_text in ("menu", "help"):
        return MENU_TEXT, True
    if lower_text in ("1", "hours"):
        return fetch_faq(business_id, "hours") or DEFAULT_FAQS["hours"], True
    if lower_text in ("2", "pricing"):
        return fetch_faq(business_id, "pricing") or DEFAULT_FAQS["pricing"], True
    if lower_text in ("3", "shipping"):
        return fetch_faq(business_id, "shipping") or DEFAULT_FAQS["shipping"], True
    if lower_text in ("4", "human"):
        return "Okay — we've notified the business. Someone will contact you soon.", True
    return None, False

# media download and transcription (same pattern)
_temp_files = set()
def _register_temp(path):
    _temp_files.add(path)
def _cleanup_temp_files():
    for p in list(_temp_files):
        try:
            os.unlink(p)
        except Exception:
            pass
    _temp_files.clear()
import atexit
atexit.register(_cleanup_temp_files)

def download_twilio_media(media_url):
    if not media_url:
        return None
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logging.warning("Twilio credentials missing; cannot download media.")
        return None
    try:
        with requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), stream=True, timeout=30) as r:
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")
            suffix = ".ogg" if "ogg" in content_type else (".mp3" if "mpeg" in content_type or "audio/mpeg" in content_type else "")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    tmp.write(chunk)
            tmp.flush()
            tmp.close()
            _register_temp(tmp.name)
            return tmp.name
    except Exception:
        logging.exception("download_twilio_media failed")
        return None

def transcribe_audio_file(local_path):
    if not os.path.exists(local_path):
        return None
    try:
        with open(local_path, "rb") as f:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=f)
        # extract text defensively
        text = None
        if getattr(transcript, "text", None):
            text = transcript.text
        elif isinstance(transcript, dict) and transcript.get("text"):
            text = transcript.get("text")
        else:
            try:
                text = transcript["text"]
            except Exception:
                text = None
        return text
    except Exception:
        logging.exception("transcribe_audio_file failed")
        return None

# --- Webhook: main flow with rate-limits, welcome messages, command/menu, lead flow, faq, AI fallback, dead-letter ---
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = (request.form.get("Body") or "").strip()
    sender = request.form.get("From", "unknown")
    to_number = request.form.get("To")

    logging.info("Incoming from %s -> %s: %s", sender, to_number, incoming_msg)

    # rate-limit checks (sender + business)
    # per sender
    allowed_sender, rem = check_and_increment_rate_limit("sender", sender, PER_SENDER_LIMIT, WINDOW_PER_SECONDS)
    if not allowed_sender:
        logging.warning("Sender %s rate-limited", sender)
        insert_message(sender, None, incoming_msg, "rate_limited_sender", False)
        # brief polite message back
        resp = MessagingResponse()
        resp.message("You're sending messages too quickly. Please wait a bit and try again.")
        return str(resp)

    # per business
    business_id = get_business_id_from_twilio(to_number)
    business_key = f"biz:{business_id}" if business_id else "biz:unknown"
    allowed_biz, _ = check_and_increment_rate_limit("business", business_key, PER_BUSINESS_LIMIT, WINDOW_PER_SECONDS)
    if not allowed_biz:
        logging.warning("Business %s rate-limited", business_id)
        insert_message(sender, business_id, incoming_msg, "rate_limited_business", False)
        resp = MessagingResponse()
        resp.message("This business is receiving a lot of messages right now — please try later.")
        return str(resp)

    # create Twilio reply
    resp = MessagingResponse()
    reply = resp.message()

    # welcome message: if this sender is first time for this business, optionally send welcome. We'll detect by checking messages table
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM messages WHERE sender = ? AND business_id = ?", (sender, business_id))
        row = c.fetchone()
        prev_count = row["cnt"] if row else 0
        if prev_count == 0 and business_id:
            # fetch welcome message
            c.execute("SELECT welcome_message FROM businesses WHERE id = ?", (business_id,))
            br = c.fetchone()
            if br and br["welcome_message"]:
                reply.body(br["welcome_message"])
                insert_message(sender, business_id, "[system_welcome_sent]", br["welcome_message"], True)
                conn.close()
                return str(resp)
        conn.close()
    except Exception:
        logging.exception("Welcome message logic failed")

    # handle media (voice)
    num_media = int(request.values.get("NumMedia", 0))
    media_text = None
    if num_media > 0:
        media_url = request.values.get("MediaUrl0")
        media_type = request.values.get("MediaContentType0", "")
        logging.info("Incoming media: url=%s type=%s", media_url, media_type)
        if media_url and ("audio" in media_type or "ogg" in media_type or "mpeg" in media_type):
            local_path = download_twilio_media(media_url)
            if local_path:
                transcript = transcribe_audio_file(local_path)
                try:
                    os.unlink(local_path)
                    _temp_files.discard(local_path)
                except Exception:
                    pass
                if transcript:
                    incoming_msg = transcript.strip()
                    media_text = incoming_msg
                    logging.info("Transcribed audio -> %s", incoming_msg)
                else:
                    reply.body("Sorry, I couldn't transcribe your voice note. Please send a text message.")
                    insert_message(sender, business_id, "[voice note - transcript failed]", "transcription_failed", False)
                    return str(resp)
            else:
                reply.body("Sorry, I couldn't download your voice note. Try again.")
                insert_message(sender, business_id, "[voice note - download failed]", "download_failed", False)
                return str(resp)
        else:
            logging.info("Media received but not audio.")
            reply.body("I received a media message. Please send text or a voice note.")
            insert_message(sender, business_id, "[media received]", "media_not_supported", False)
            return str(resp)

    if not incoming_msg:
        reply.body("I didn't get any text. Please send your question again.")
        return str(resp)

    lower = incoming_msg.lower().strip()

    # 1) commands/menu
    cmd_reply, is_cmd = handle_command(lower, sender, business_id)
    if is_cmd:
        reply.body(cmd_reply)
        insert_message(sender, business_id, incoming_msg, cmd_reply, True)
        return str(resp)

    # 2) lead flow
    try:
        lead_reply = handle_lead_flow(sender, business_id, incoming_msg)
        if lead_reply:
            reply.body(lead_reply)
            insert_message(sender, business_id, incoming_msg, lead_reply, False)
            return str(resp)
    except Exception:
        logging.exception("Lead flow error")

    # 3) FAQ
    faq_resp = fetch_faq(business_id, lower)
    is_faq = faq_resp is not None
    if is_faq:
        bot_reply = faq_resp
        reply.body(bot_reply)
        insert_message(sender, business_id, incoming_msg, bot_reply, True)
        return str(resp)

    # 4) AI fallback - use cache + retry/backoff
    bot_reply, cached = call_openai_with_retry(incoming_msg, max_retries=3)
    if bot_reply is None:
        # failed after retries -> dead-letter
        err_text = "OpenAI failed after retries"
        log_dead_letter(sender, business_id, incoming_msg, err_text)
        reply.body("Sorry, we couldn't answer right now. A human will review your message shortly.")
        insert_message(sender, business_id, incoming_msg, "dead_letter", False)
        return str(resp)

    # success
    reply.body(bot_reply)
    insert_message(sender, business_id, incoming_msg, bot_reply, False)
    return str(resp)

# --- Admin & analytics (keeps same pattern) ---
def require_admin():
    token = request.headers.get("Authorization", "")
    if not token.startswith("Bearer ") or token.split(" ", 1)[1] != ADMIN_TOKEN:
        abort(401)

@app.route("/admin/business", methods=["POST"])
def admin_add_business():
    require_admin()
    data = request.json or {}
    name = data.get("name")
    twilio_number = data.get("twilio_number")
    welcome = data.get("welcome_message")
    language = data.get("language")
    monthly_budget = data.get("monthly_budget", 0)
    if not name or not twilio_number:
        return jsonify({"error": "name and twilio_number required"}), 400
    conn = get_db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO businesses (name, twilio_number, welcome_message, language, monthly_budget) VALUES (?, ?, ?, ?, ?)", (name, twilio_number, welcome, language, monthly_budget))
        conn.commit()
        business_id = c.lastrowid
        conn.close()
        return jsonify({"id": business_id, "name": name, "twilio_number": twilio_number}), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "twilio_number already exists"}), 409

@app.route("/admin/businesses", methods=["GET"])
def admin_list_businesses():
    require_admin()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT id, name, twilio_number, lead_mode, welcome_message, language, monthly_budget FROM businesses")
    rows = [{"id": r["id"], "name": r["name"], "twilio_number": r["twilio_number"], "lead_mode": bool(r["lead_mode"]), "welcome_message": r["welcome_message"], "language": r["language"], "monthly_budget": r["monthly_budget"]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/admin/faq", methods=["POST"])
def admin_add_faq():
    require_admin()
    data = request.json or {}
    key = (data.get("key") or "").strip().lower()
    response_text = data.get("response")
    business_id = data.get("business_id")
    if not key or not response_text:
        return jsonify({"error": "key and response required"}), 400
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO faqs (business_id, key, response) VALUES (?, ?, ?)
        ON CONFLICT(business_id, key) DO UPDATE SET response = excluded.response
    """, (business_id, key, response_text))
    conn.commit()
    conn.close()
    return jsonify({"business_id": business_id, "key": key, "response": response_text}), 201

@app.route("/admin/faqs", methods=["GET"])
def admin_get_faqs():
    require_admin()
    business_id = request.args.get("business_id")
    conn = get_db_conn()
    c = conn.cursor()
    if business_id:
        c.execute("SELECT id, business_id, key, response FROM faqs WHERE business_id = ?", (business_id,))
    else:
        c.execute("SELECT id, business_id, key, response FROM faqs")
    rows = [{"id": r["id"], "business_id": r["business_id"], "key": r["key"], "response": r["response"]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/admin/enable_leads", methods=["POST"])
def admin_enable_leads():
    require_admin()
    data = request.json or {}
    business_id = data.get("business_id")
    enable = data.get("enable", True)
    if not business_id:
        return jsonify({"error": "business_id required"}), 400
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("UPDATE businesses SET lead_mode = ? WHERE id = ?", (1 if enable else 0, business_id))
    conn.commit()
    conn.close()
    return jsonify({"business_id": business_id, "lead_mode": bool(enable)}), 200

@app.route("/admin/reset_lead_state", methods=["POST"])
def admin_reset_lead_state():
    require_admin()
    sender = request.json.get("sender")
    if not sender:
        return jsonify({"error": "sender required"}), 400
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM lead_states WHERE sender = ?", (sender,))
    conn.commit()
    conn.close()
    return jsonify({"sender": sender, "status": "reset"}), 200

# analytics
@app.route("/analytics/top_questions", methods=["GET"])
def analytics_top_questions():
    require_admin()
    limit = int(request.args.get("limit", 10))
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT message, COUNT(*) AS frequency
        FROM messages
        GROUP BY message
        ORDER BY frequency DESC
        LIMIT ?
    """, (limit,))
    rows = [{"message": r["message"], "count": r["frequency"]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/analytics/counts_per_business", methods=["GET"])
def analytics_counts_per_business():
    require_admin()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT COALESCE(b.name, 'Unknown') AS business, COUNT(m.id) AS total
        FROM messages m
        LEFT JOIN businesses b ON m.business_id = b.id
        GROUP BY b.id
    """)
    rows = [{"business": r["business"], "count": r["total"]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/analytics/ai_vs_faq", methods=["GET"])
def analytics_ai_vs_faq():
    require_admin()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""
        SELECT SUM(CASE WHEN is_faq = 1 THEN 1 ELSE 0 END) as faq_count,
               SUM(CASE WHEN is_faq = 0 THEN 1 ELSE 0 END) as ai_count
        FROM messages
    """)
    row = c.fetchone()
    conn.close()
    faq_count = int(row["faq_count"] or 0)
    ai_count = int(row["ai_count"] or 0)
    return jsonify({"faq_count": faq_count, "ai_count": ai_count})

@app.route("/admin/dead_letters", methods=["GET"])
def admin_get_dead_letters():
    require_admin()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT id, sender, business_id, message, error, timestamp FROM dead_letters ORDER BY timestamp DESC LIMIT 200")
    rows = [{"id": r["id"], "sender": r["sender"], "business_id": r["business_id"], "message": r["message"], "error": r["error"], "timestamp": r["timestamp"]} for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": DB_PATH}), 200

# --- run ---
if __name__ == "__main__":
    logging.info("Starting app on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
