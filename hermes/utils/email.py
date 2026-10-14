import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import logging

logger = logging.getLogger(__name__)

def send_verification_code(email: str, code: str) -> None:
    """
    Send a verification code via email.
    
    Args:
        email: Recipient email address
        code: 6-digit verification code to send
    """
    # Get SMTP configuration from environment variables
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)
    
    if not smtp_user or not smtp_password:
        logger.error("SMTP credentials not configured")
        raise ValueError("SMTP credentials not configured")
    
    # Create message
    msg = MIMEMultipart()
    msg['From'] = smtp_from
    msg['To'] = email
    msg['Subject'] = "Your Hermes Verification Code"
    
    body = f"""
    Hello,
    
    Your Hermes verification code is: {code}
    
    This code will expire in 15 minutes.
    
    If you didn't request this code, please ignore this email.
    
    Best regards,
    Hermes AI Assistant
    """
    
    msg.attach(MIMEText(body, 'plain'))
    
    try:
        # Create SMTP session
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()  # Enable security
        server.login(smtp_user, smtp_password)
        text = msg.as_string()
        server.sendmail(smtp_from, email, text)
        server.quit()
        logger.info(f"Verification code sent to {email}")
    except Exception as e:
        logger.error(f"Failed to send verification code to {email}: {str(e)}")
        raise