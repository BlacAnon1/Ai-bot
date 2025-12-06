from flask import Blueprint, request, jsonify
import os
import requests

facebook_bp = Blueprint("facebook_bp", __name__)

PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")

# Facebook Webhook Verification
@facebook_bp.route("/facebook/webhook", methods=["GET"])
def facebook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


# Facebook Message Receiver
@facebook_bp.route("/facebook/webhook", methods=["POST"])
def facebook_messages():
    data = request.json

    if "entry" in data:
        for entry in data["entry"]:
            for messaging in entry.get("messaging", []):
                sender_id = messaging["sender"]["id"]

                if "message" in messaging:
                    message_text = messaging["message"].get("text", "")

                    # CONNECT TO APP.PY (AI RESPONSE)
                    from app import process_ai_message
                    reply = process_ai_message("facebook", sender_id, message_text)

                    send_facebook_message(sender_id, reply)

    return "OK", 200


def send_facebook_message(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    requests.post(url, json=payload)
