import smtplib
from email.message import EmailMessage
from app.config import settings
import logging

logger = logging.getLogger(__name__)

def send_otp_email(to_email: str, otp: str) -> bool:
    """
    Sends an OTP verification email using SMTP.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = 'Your Verification Code for Job Ready'
        msg['From'] = settings.EMAIL_FROM
        msg['To'] = to_email

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #2563eb; text-align: center;">Welcome to Job Ready!</h2>
                <p>Hello,</p>
                <p>Thank you for registering. Please use the following One-Time Password (OTP) to verify your email address:</p>
                <div style="background-color: #f3f4f6; padding: 15px; text-align: center; border-radius: 6px; margin: 20px 0;">
                    <span style="font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #1f2937;">{otp}</span>
                </div>
                <p>This code is valid for <strong>15 minutes</strong>.</p>
                <p>If you did not request this, please ignore this email.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;" />
                <p style="font-size: 12px; color: #6b7280; text-align: center;">The Job Ready Team</p>
            </div>
        </body>
        </html>
        """
        
        # Plain text fallback
        msg.set_content(f"Your verification code is: {otp}. It is valid for 15 minutes.")
        
        # HTML content
        msg.add_alternative(html_content, subtype='html')

        with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
            
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
        msg['From'] = settings.EMAIL_FROM
        msg['To'] = to_email

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #2563eb; text-align: center;">Password Reset Request</h2>
                <p>Hello,</p>
                <p>We received a request to reset your password for your Job Ready account. Please use the following One-Time Password (OTP) to reset it:</p>
                <div style="background-color: #f3f4f6; padding: 15px; text-align: center; border-radius: 6px; margin: 20px 0;">
                    <span style="font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #1f2937;">{otp}</span>
                </div>
                <p>This code is valid for <strong>15 minutes</strong>.</p>
                <p>If you did not request a password reset, please safely ignore this email. Your password will remain unchanged.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;" />
                <p style="font-size: 12px; color: #6b7280; text-align: center;">The Job Ready Team</p>
            </div>
        </body>
        </html>
        """
        
        # Plain text fallback
        msg.set_content(f"Your password reset code is: {otp}. It is valid for 15 minutes.")
        
        # HTML content
        msg.add_alternative(html_content, subtype='html')

        with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
            
        logger.info(f"Password reset email sent successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset email to {to_email}: {str(e)}")
        return False
