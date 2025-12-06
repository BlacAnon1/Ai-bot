from flask import Blueprint, request, jsonify
import os
import requests

instagram_bp = Blueprint("instagram_bp", __name__)

PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.getenv("FB_VERIFY_TOKEN")


# Instagram uses the SAME verification endpoint rules
@instagram_bp.route("/instagram/webhook", methods=["GET"])
def instagram_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


# Instagram message receiver
@instagram_bp.route("/instagram/webhook", methods=["POST"])
def instagram_messages():
    data = request.json

    if "entry" in data:
        for entry in data["entry"]:
            for msg in entry.get("messaging", []):
                sender_id = msg["sender"]["id"]

                if "message" in msg:
                    message_text = msg["message"].get("text", "")

                    # Connect to your AI processor
                    from app import process_ai_message
                    reply = process_ai_message("instagram", sender_id, message_text)

                    send_instagram_message(sender_id, reply)

    return "OK", 200


def send_instagram_message(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    requests.post(url, json=payload)
