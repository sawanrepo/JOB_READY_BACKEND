import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from app.config import settings
import logging

logger = logging.getLogger(__name__)
EMAIL_LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "email-logo.png"
SUPPORT_EMAIL = "support@jobreadyai.in"

def _apply_sender_headers(msg: EmailMessage) -> None:
    msg['From'] = settings.EMAIL_FROM
    if settings.EMAIL_REPLY_TO:
        msg['Reply-To'] = str(settings.EMAIL_REPLY_TO)

def _send_message(msg: EmailMessage) -> None:
    smtp_class = smtplib.SMTP_SSL if settings.SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
        if not settings.SMTP_USE_SSL:
            server.starttls()
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)

def _support_email() -> str:
    return str(settings.EMAIL_REPLY_TO or SUPPORT_EMAIL)

def _logo_header() -> tuple[str, str | None]:
    if not EMAIL_LOGO_PATH.exists():
        return '<h2 style="color: #2563eb; text-align: center; margin: 0;">Job Ready</h2>', None

    logo_cid = make_msgid(domain="jobreadyai.in")
    html = f"""
                <div style="text-align: center; margin-bottom: 18px;">
                    <img src="cid:{logo_cid[1:-1]}" alt="Job Ready" style="max-width: 210px; width: 100%; height: auto;" />
                </div>
    """
    return html, logo_cid

def _attach_logo(msg: EmailMessage, logo_cid: str | None) -> None:
    if not logo_cid or not EMAIL_LOGO_PATH.exists():
        return

    html_part = msg.get_payload()[-1]
    with EMAIL_LOGO_PATH.open("rb") as logo_file:
        html_part.add_related(
            logo_file.read(),
            maintype="image",
            subtype="png",
            cid=logo_cid,
        )

def send_otp_email(to_email: str, otp: str) -> bool:
    """
    Sends an OTP verification email using SMTP.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = 'Your Verification Code for Job Ready'
        _apply_sender_headers(msg)
        msg['To'] = to_email
        support_email = _support_email()
        logo_header, logo_cid = _logo_header()

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                {logo_header}
                <h2 style="color: #2563eb; text-align: center;">Welcome to Job Ready!</h2>
                <p>Hello,</p>
                <p>Thank you for registering. Please use the following One-Time Password (OTP) to verify your email address:</p>
                <div style="background-color: #f3f4f6; padding: 15px; text-align: center; border-radius: 6px; margin: 20px 0;">
                    <span style="font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #1f2937;">{otp}</span>
                </div>
                <p>This code is valid for <strong>15 minutes</strong>.</p>
                <p>If you did not request this, please ignore this email.</p>
                <p style="font-size: 14px; color: #4b5563;">Please do not reply to the no-reply sender address. For help, contact <a href="mailto:{support_email}" style="color: #2563eb;">{support_email}</a>.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;" />
                <p style="font-size: 12px; color: #6b7280; text-align: center;">The Job Ready Team</p>
            </div>
        </body>
        </html>
        """
        
        # Plain text fallback
        msg.set_content(
            f"Your verification code is: {otp}. It is valid for 15 minutes.\n\n"
            f"Please do not reply to the no-reply sender address. For help, contact {support_email}."
        )
        
        # HTML content
        msg.add_alternative(html_content, subtype='html')
        _attach_logo(msg, logo_cid)

        _send_message(msg)
            
        logger.info(f"OTP email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send OTP email to {to_email}: {str(e)}")
        return False

def send_password_reset_email(to_email: str, otp: str) -> bool:
    """
    Sends a password reset OTP email using SMTP.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = 'Password Reset Request - Job Ready'
        _apply_sender_headers(msg)
        msg['To'] = to_email
        support_email = _support_email()
        logo_header, logo_cid = _logo_header()

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                {logo_header}
                <h2 style="color: #2563eb; text-align: center;">Password Reset Request</h2>
                <p>Hello,</p>
                <p>We received a request to reset your password for your Job Ready account. Please use the following One-Time Password (OTP) to reset it:</p>
                <div style="background-color: #f3f4f6; padding: 15px; text-align: center; border-radius: 6px; margin: 20px 0;">
                    <span style="font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #1f2937;">{otp}</span>
                </div>
                <p>This code is valid for <strong>15 minutes</strong>.</p>
                <p>If you did not request a password reset, please safely ignore this email. Your password will remain unchanged.</p>
                <p style="font-size: 14px; color: #4b5563;">Please do not reply to the no-reply sender address. For help, contact <a href="mailto:{support_email}" style="color: #2563eb;">{support_email}</a>.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;" />
                <p style="font-size: 12px; color: #6b7280; text-align: center;">The Job Ready Team</p>
            </div>
        </body>
        </html>
        """
        
        # Plain text fallback
        msg.set_content(
            f"Your password reset code is: {otp}. It is valid for 15 minutes.\n\n"
            f"Please do not reply to the no-reply sender address. For help, contact {support_email}."
        )
        
        # HTML content
        msg.add_alternative(html_content, subtype='html')
        _attach_logo(msg, logo_cid)

        _send_message(msg)
            
        logger.info(f"Password reset email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email to {to_email}: {str(e)}")
        return False
