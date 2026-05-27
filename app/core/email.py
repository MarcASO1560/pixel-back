import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlencode

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_resend_configured() -> bool:
    return bool(settings.RESEND_API_KEY and settings.RESEND_FROM_EMAIL)


def is_smtp_configured() -> bool:
    return bool(
        settings.SMTP_HOST
        and settings.SMTP_FROM_EMAIL
        and settings.SMTP_USERNAME
        and settings.SMTP_PASSWORD
    )


def is_email_configured() -> bool:
    return is_resend_configured() or is_smtp_configured()


def build_password_reset_url(reset_token: str) -> str:
    base_url = settings.FRONTEND_URL.rstrip("/")
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'reset_token': reset_token})}"


def build_password_reset_content(reset_url: str) -> tuple[str, str, str]:
    subject = "Reset your Sefkira Studio password"
    text = "\n".join(
        [
            "Reset your Sefkira Studio password",
            "",
            "Open this link to choose a new password:",
            reset_url,
            "",
            "If you did not request this, you can ignore this email.",
        ]
    )
    html = f"""
        <html>
          <body style="font-family: sans-serif; line-height: 1.5;">
            <h1>Reset your Sefkira Studio password</h1>
            <p>Open this link to choose a new password:</p>
            <p><a href="{reset_url}">Reset password</a></p>
            <p>If you did not request this, you can ignore this email.</p>
          </body>
        </html>
        """
    return subject, text, html


def send_password_reset_email_with_resend(email: str, reset_url: str) -> bool:
    if not is_resend_configured():
        return False

    subject, text, html = build_password_reset_content(reset_url)
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.RESEND_FROM_EMAIL,
                "to": [email],
                "subject": subject,
                "text": text,
                "html": html,
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Could not send password reset email with Resend.")
        return False

    return True


def send_password_reset_email_with_smtp(email: str, reset_url: str) -> bool:
    if not is_smtp_configured():
        return False

    sender = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    if not sender:
        return False

    subject, text, html = build_password_reset_content(reset_url)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.SMTP_FROM_NAME, sender))
    message["To"] = email
    message.set_content(text)
    message.add_alternative(
        html,
        subtype="html",
    )

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send password reset email.")
        return False

    return True


def send_password_reset_email(email: str, reset_token: str) -> bool:
    reset_url = build_password_reset_url(reset_token)
    return send_password_reset_email_with_resend(
        email,
        reset_url,
    ) or send_password_reset_email_with_smtp(email, reset_url)
