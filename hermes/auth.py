from flask import Blueprint, request, jsonify, make_response, current_app
from hermes.db import db
from hermes.utils.security import generate_token, hash_token, verify_token, encrypt_payload, decrypt_payload, generate_device_fingerprint
from hermes.utils.email import send_verification_code
import os
import secrets
from datetime import datetime, timedelta

auth_bp = Blueprint('auth', __name__)

# Configuration
SESSION_EXPIRY_DAYS = int(os.environ.get('HERMES_SESSION_EXPIRY_DAYS', 30))
CODE_EXPIRY_MINUTES = int(os.environ.get('HERMES_CODE_EXPIRY_MINUTES', 15))
MAX_CODE_ATTEMPTS = int(os.environ.get('HERMES_MAX_CODE_ATTEMPTS', 5))

@auth_bp.route('/api/auth/send-code', methods=['POST'])
def send_code():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    
    email = data.get('email', '').lower().strip()
    
    # Basic validation
    if not email or '@' not in email:
        return jsonify({"error": "Valid email required"}), 400
    
    # Generate cryptographically secure 6-digit code
    code = str(secrets.SystemRandom().randint(100000, 999999))
    hashed_code = hash_token(str(code))  # SHA-256 with salt
    
    # Store in DB with expiry
    try:
        db.execute("""
            INSERT INTO auth_codes (email, code_hash, expires_at, attempts)
            VALUES (?, ?, datetime('now', ?), 0)
        """, (email, hashed_code, f'+{CODE_EXPIRY_MINUTES} minutes'))
    except Exception as e:
        current_app.logger.error(f"Failed to store auth code for {email}: {str(e)}")
        return jsonify({"error": "Failed to process request"}), 500
    
    # Send via email
    try:
        send_verification_code(email, code)
    except Exception as e:
        # We still return success to avoid leaking whether email exists
        # but log the error
        current_app.logger.error(f"Failed to send verification code to {email}: {str(e)}")
    
    return jsonify({"message": "Verification code sent"}), 200

@auth_bp.route('/api/auth/verify-code', methods=['POST'])
def verify_code():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    
    email = data.get('email', '').lower().strip()
    code = data.get('code', '').strip()
    
    if not email or not code or len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Email and 6-digit code required"}), 400
    
    # Fetch stored code attempt (most recent)
    row = db.fetchone("""
        SELECT code_hash, attempts, expires_at 
        FROM auth_codes 
        WHERE email = ? 
        ORDER BY created_at DESC 
        LIMIT 1
    """, (email,))
    
    if not row:
        return jsonify({"error": "No pending verification request"}), 400
    
    # Check expiry
    try:
        expires_at = datetime.fromisoformat(row['expires_at'])
    except ValueError:
        return jsonify({"error": "Invalid expiry timestamp"}), 500
    
    if datetime.now() > expires_at:
        # Clean up expired code
        db.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        return jsonify({"error": "Code expired. Request new one"}), 400
    
    # Check attempt limit
    if row['attempts'] >= MAX_CODE_ATTEMPTS:
        db.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        return jsonify({"error": "Too many attempts. Request new code"}), 429
    
    # Verify code
    if not verify_token(code, row['code_hash']):
        # Increment attempt counter
        db.execute("""
            UPDATE auth_codes 
            SET attempts = attempts + 1 
            WHERE email = ? AND code_hash = ?
        """, (email, row['code_hash']))
        return jsonify({"error": "Invalid code"}), 400
    
    # Code valid → create session
    session_id = generate_token()  # random string for session ID
    hashed_session_id = hash_token(session_id)
    device_fingerprint = generate_device_fingerprint(request)
    current_project = request.headers.get('X-Current-Project', 'all')
    if not current_project:
        current_project = 'all'
    
    # Store session
    try:
        db.execute("""
            INSERT INTO user_sessions 
            (token_hash, email, project_scope, device_fingerprint, created_at, expires_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now', ?))
        """, (hashed_session_id, email, current_project, device_fingerprint, f'+{SESSION_EXPIRY_DAYS} days'))
    except Exception as e:
        current_app.logger.error(f"Failed to create session for {email}: {str(e)}")
        return jsonify({"error": "Failed to create session"}), 500
    
    # Clean up used code
    db.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
    
    # Set cookie: encrypt the session_id
    resp = make_response(jsonify({
        "message": "Authenticated",
        "user": {"email": email},
        "project_scope": current_project
    }))
    resp.set_cookie(
        'hermes_session',
        encrypt_payload({'session_id': session_id}),
        httponly=True,      # Not accessible via JS (XSS protection)
        secure=True,        # HTTPS-only in production (should be set via proxy in dev)
        samesite='Lax',     # CSRF protection
        max_age=SESSION_EXPIRY_DAYS*24*60*60, # in seconds
        path='/'
    )
    return resp, 200

@auth_bp.route('/api/auth/status', methods=['GET'])
def auth_status():
    encrypted_token = request.cookies.get('hermes_session')
    if not encrypted_token:
        return jsonify({"authenticated": False}), 401
    
    # Decrypt token to get session_id
    try:
        payload = decrypt_payload(encrypted_token)
        session_id = payload.get('session_id')
        if not session_id:
            return jsonify({"authenticated": False}), 401
    except Exception as e:
        current_app.logger.warning(f"Failed to decrypt session cookie: {str(e)}")
        return jsonify({"authenticated": False}), 401
    
    # Hash the session_id to look up in database
    hashed_session_id = hash_token(session_id)
    
    # Verify session still exists in DB and is valid
    row = db.fetchone("""
        SELECT 1 FROM user_sessions 
        WHERE token_hash = ? AND expires_at > datetime('now')
    """, (hashed_session_id,))
    
    if not row:
        return jsonify({"authenticated": False}), 401
    
    # Optionally, we could return more user info, but for now just authenticated status
    return jsonify({"authenticated": True}), 200

@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    encrypted_token = request.cookies.get('hermes_session')
    if encrypted_token:
        try:
            payload = decrypt_payload(encrypted_token)
            session_id = payload.get('session_id')
            if session_id:
                hashed_session_id = hash_token(session_id)
                db.execute("DELETE FROM user_sessions WHERE token_hash = ?", (hashed_session_id,))
        except Exception as e:
            current_app.logger.warning(f"Error during logout: {str(e)}")
    
    resp = make_response(jsonify({"message": "Logged out"}))
    resp.set_cookie('hermes_session', '', expires=0)  # Delete cookie
    return resp, 200