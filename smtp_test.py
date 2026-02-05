# smtp_smoke_test.py
import os
import smtplib
from email.message import EmailMessage

host = os.environ["SMTP_HOST"]
port = int(os.environ.get("SMTP_PORT", "587"))
user = os.environ["SMTP_USER"]
pw = os.environ["SMTP_PASS"]
to = os.environ["EMAIL_TO"]
frm = os.environ.get("EMAIL_FROM", user)

msg = EmailMessage()
msg["Subject"] = "LinkedIn triage - SMTP smoke test"
msg["From"] = frm
msg["To"] = to
msg.set_content("If you received this, SMTP is working.")

with smtplib.SMTP(host, port, timeout=20) as s:
    s.ehlo()
    s.starttls()
    s.login(user, pw)
    s.send_message(msg)

print("Sent OK")
