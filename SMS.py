from flask import Blueprint, request
from twilio.twiml.messaging_response import MessagingResponse

sms_bp = Blueprint("sms_bp", __name__)

@sms_bp.route("/sms/webhook", methods=["POST"])
def sms_webhook():
    sender = request.form.get("From")
    message = request.form.get("Body")

    from app import process_ai_message
    reply_text = process_ai_message("sms", sender, message)

    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)
