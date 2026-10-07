"""
Email, WhatsApp, and tamper alerts. Same mechanics as the original
script, generalized to say which camera and which zone/class triggered
the alert, since there can be several of each now.
"""
import os
import smtplib
import threading
from email.message import EmailMessage
import requests

import config as cfg


def send_email_alert(camera_name, label, zone_name, image_path, timestamp_str, zoom_image_path=None):
    """Sends an email alert with the snapshot attached. Runs in a background
    thread — call via threading.Thread(target=send_email_alert, ...)."""
    try:
        print(f"[EMAIL] Preparing alert for {camera_name}...")
        msg = EmailMessage()
        msg["Subject"] = f"🚨 {camera_name}: {label} detected at {timestamp_str}"
        msg["From"] = cfg.SENDER_EMAIL
        msg["To"] = cfg.RECEIVER_EMAIL
        body = (
            f"{camera_name} detected a {label} in zone '{zone_name}' at {timestamp_str}.\n\n"
            f"A recording clip and snapshot have been logged to your dashboard gallery."
        )
        if zoom_image_path:
            body += "\n\nThe subject was far from the camera, so a zoomed-in close-up is attached as well."
        msg.set_content(body)

        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype="jpeg", filename=os.path.basename(image_path))

        if zoom_image_path and os.path.exists(zoom_image_path):
            with open(zoom_image_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="image", subtype="jpeg", filename=os.path.basename(zoom_image_path))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(cfg.SENDER_EMAIL, cfg.SENDER_PASSWORD)
            server.send_message(msg)

        print(f"[EMAIL] Alert sent for {camera_name}.")
    except Exception as e:
        print(f"[EMAIL] Failed to send alert: {e}")


def send_whatsapp_alert(camera_name, label, zone_name, timestamp_str):
    if not cfg.WHATSAPP_ENABLED:
        return
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg.TWILIO_ACCOUNT_SID}/Messages.json"
        body = (
            f"🚨 {camera_name}: {label} detected in zone '{zone_name}' at {timestamp_str}. "
            f"Check the dashboard gallery for the clip."
        )
        response = requests.post(
            url,
            data={"From": cfg.TWILIO_WHATSAPP_FROM, "To": cfg.TWILIO_WHATSAPP_TO, "Body": body},
            auth=(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN),
            timeout=10,
        )
        if response.status_code >= 300:
            print(f"[WHATSAPP] Twilio error {response.status_code}: {response.text}")
        else:
            print(f"[WHATSAPP] Alert sent for {camera_name}.")
    except Exception as e:
        print(f"[WHATSAPP] Failed to send alert: {e}")


def send_tamper_alert(camera_name, reason, timestamp_str):
    subject = f"⚠️ {camera_name}: TAMPER/OFFLINE ALERT"
    body = (
        f"{camera_name} flagged a possible tamper or offline condition at "
        f"{timestamp_str}: {reason}.\n\nCheck the camera in person if you're able to."
    )

    def _email():
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = cfg.SENDER_EMAIL
            msg["To"] = cfg.RECEIVER_EMAIL
            msg.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(cfg.SENDER_EMAIL, cfg.SENDER_PASSWORD)
                server.send_message(msg)
            print(f"[TAMPER] Email alert sent for {camera_name}.")
        except Exception as e:
            print(f"[TAMPER] Failed to send email alert: {e}")

    def _whatsapp():
        if not cfg.WHATSAPP_ENABLED:
            return
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg.TWILIO_ACCOUNT_SID}/Messages.json"
            response = requests.post(
                url,
                data={"From": cfg.TWILIO_WHATSAPP_FROM, "To": cfg.TWILIO_WHATSAPP_TO, "Body": f"{subject}\n{body}"},
                auth=(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN),
                timeout=10,
            )
            if response.status_code >= 300:
                print(f"[TAMPER] WhatsApp error {response.status_code}: {response.text}")
            else:
                print(f"[TAMPER] WhatsApp alert sent for {camera_name}.")
        except Exception as e:
            print(f"[TAMPER] Failed to send WhatsApp alert: {e}")

    threading.Thread(target=_email, daemon=True).start()
    threading.Thread(target=_whatsapp, daemon=True).start()
