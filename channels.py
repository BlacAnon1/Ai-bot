# channels.py
import os
import logging
import requests
from flask import Blueprint, request, jsonify

logger = logging.getLogger("reficulbot.channels")

channels_bp = Blueprint("channels", __name__)

FORWARD_WEBHOOK = os.getenv("LOCAL_WEBHOOK_URL", "http://127.0.0.1:5600/webhook")

FB_VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN", "")
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "")
IG_PAGE_TOKEN = os.getenv("IG_PAGE_TOKEN", "")      # IG uses same page token in many cases

TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")


# --------------------------------------------------------
# Helper: forward incoming message to /webhook
# --------------------------------------------------------
def _forward_to_local_webhook(form_data):
    try:
        r = requests.post(FORWARD_WEBHOOK, data=form_data, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.exception("Forward error: %s", e)
        return False


# --------------------------------------------------------
# Helper: SEND outgoing messages (FB, IG, TWILIO)
# --------------------------------------------------------
def send_facebook_message(psid, text):
    url = "https://graph.facebook.com/v19.0/me/messages"
    params = {"access_token": FB_PAGE_TOKEN}
    
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    return r.status_code == 200


def send_instagram_message(ig_user_id, text):
    # Instagram uses the same FB page token in many configurations
    url = f"https://graph.facebook.com/v19.0/{ig_user_id}/messages"
    params = {"access_token": IG_PAGE_TOKEN}

    payload = {
        "recipient": {"id": ig_user_id},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    return r.status_code == 200


def send_twilio_message(to_number, body):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    auth = (TWILIO_SID, TWILIO_AUTH)

    data = {
        "To": to_number,
        "From": TWILIO_NUMBER,
        "Body": body
    }

    r = requests.post(url, data=data, auth=auth)
    return r.status_code == 201 or r.status_code == 200


# --------------------------------------------------------
# 1. TWILIO SMS + WHATSAPP
# --------------------------------------------------------
@channels_bp.route("/twilio/sms", methods=["POST"])
def twilio_sms_webhook():
    body = request.form.get("Body", "")
    sender = request.form.get("From", "unknown")
    to_ = request.form.get("To", TWILIO_NUMBER)
    num_media = request.form.get("NumMedia", "0")

    form = {
        "Body": body,
        "From": sender,
        "To": to_,
        "NumMedia": num_media,
        "MediaUrl0": request.form.get("MediaUrl0", ""),
        "MediaContentType0": request.form.get("MediaContentType0", "")
    }

    _forward_to_local_webhook(form)
    return ("", 200)


# --------------------------------------------------------
# 2. FACEBOOK MESSENGER
# --------------------------------------------------------
@channels_bp.route("/facebook/webhook", methods=["GET", "POST"])
def facebook_webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == FB_VERIFY_TOKEN:
            return request.args.get("hub.challenge", ""), 200
        return ("Invalid token", 403)

    data = request.json or {}
    entries = data.get("entry", [])

    for entry in entries:
        for msg_event in entry.get("messaging", []):
            sender_psid = msg_event["sender"]["id"]
            msg_obj = msg_event.get("message", {})

            text = msg_obj.get("text", "")
            attachments = msg_obj.get("attachments", [])

            media_url = ""
            media_type = ""
            if attachments:
                att = attachments[0]
                media_type = att.get("type", "")
                payload = att.get("payload", {})
                media_url = payload.get("url", "")

            form = {
                "Body": text,
                "From": f"facebook:{sender_psid}",
                "To": "facebook_page",
                "NumMedia": "1" if media_url else "0",
                "MediaUrl0": media_url,
                "MediaContentType0": media_type
            }
            _forward_to_local_webhook(form)

    return ("OK", 200)


# --------------------------------------------------------
# 3. INSTAGRAM DM
# --------------------------------------------------------
@channels_bp.route("/instagram/webhook", methods=["GET", "POST"])
def instagram_webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == FB_VERIFY_TOKEN:
            return request.args.get("hub.challenge", ""), 200
        return ("Invalid token", 403)

    payload = request.json or {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            messages = val.get("messages", [])

            for m in messages:
                ig_from = m.get("from", "")
                text = m.get("text", "")

                form = {
                    "Body": text,
                    "From": f"instagram:{ig_from}",
                    "To": "instagram_account",
                    "NumMedia": "0",
                }

                _forward_to_local_webhook(form)

    return ("OK", 200)
