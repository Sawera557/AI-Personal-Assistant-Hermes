from fastapi import APIRouter, Request, HTTPException, Cookie, Response
from hermes.db import cursor
from hermes.utils.security import generate_token, hash_token, verify_token, encrypt_payload, decrypt_payload, generate_device_fingerprint
from hermes.utils.email import send_verification_code
import os
import secrets
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Configuration
SESSION_EXPIRY_DAYS = int(os.environ.get('HERMES_SESSION_EXPIRY_DAYS', 30))
CODE_EXPIRY_MINUTES = int(os.environ.get('HERMES_CODE_EXPIRY_MINUTES', 15))
MAX_CODE_ATTEMPTS = int(os.environ.get('HERMES_MAX_CODE_ATTEMPTS', 5))

@router.post("/send-code")
async def send_code(request: Request):
    data = await request.json()
    if not data:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    email = data.get('email', '').lower().strip()
    
    # Basic validation
    if not email or '@' not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    
    # Generate cryptographically secure 6-digit code
    code = str(secrets.SystemRandom().randint(100000, 999999))
    hashed_code = hash_token(str(code))  # SHA-256 with salt
    
    # Store in DB with expiry
    try:
        with cursor() as cur:
            cur.execute("""
                INSERT INTO auth_codes (email, code_hash, expires_at, attempts)
                VALUES (?, ?, datetime('now', ?), 0)
            """, (email, hashed_code, f'+{CODE_EXPIRY_MINUTES} minutes'))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to process request")
    
    # Send via email
    try:
        send_verification_code(email, code)
    except Exception as e:
        # We still return success to avoid leaking whether email exists
        # but log the error
        print(f"Failed to send verification code to {email}: {str(e)}")  # In production, use proper logging
    
    return {"message": "Verification code sent"}

@router.post("/verify-code")
async def verify_code(request: Request, response: Response):
    data = await request.json()
    if not data:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    email = data.get('email', '').lower().strip()
    code = data.get('code', '').strip()
    
    if not email or not code or len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail="Email and 6-digit code required")
    
    # Fetch stored code attempt (most recent)
    row = None
    try:
        with cursor() as cur:
            cur.execute("""
                SELECT code_hash, attempts, expires_at 
                FROM auth_codes 
                WHERE email = ? 
                ORDER BY created_at DESC 
                LIMIT 1
            """, (email,))
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to process request")
    
    if not row:
        raise HTTPException(status_code=400, detail="No pending verification request")
    
    # Check expiry
    try:
        expires_at = datetime.fromisoformat(row['expires_at'])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expiry timestamp")
    
    if datetime.now() > expires_at:
        # Clean up expired code
        try:
            with cursor() as cur:
                cur.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        except Exception as e:
            print(f"Failed to delete expired auth code for {email}: {str(e)}")
        raise HTTPException(status_code=400, detail="Code expired. Request new one")
    
    # Check attempt limit
    if row['attempts'] >= MAX_CODE_ATTEMPTS:
        try:
            with cursor() as cur:
                cur.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        except Exception as e:
            print(f"Failed to delete auth code due to attempts for {email}: {str(e)}")
        raise HTTPException(status_code=429, detail="Too many attempts. Request new code")
    
    # Verify code
    if not verify_token(code, row['code_hash']):
        # Increment attempt counter
        try:
            with cursor() as cur:
                cur.execute("""
                    UPDATE auth_codes 
                    SET attempts = attempts + 1 
                    WHERE email = ? AND code_hash = ?
                """, (email, row['code_hash']))
        except Exception as e:
            print(f"Failed to increment auth code attempts for {email}: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid code")
    
    # Code valid → create session
    session_id = generate_token()  # random string for session ID
    hashed_session_id = hash_token(session_id)
    device_fingerprint = generate_device_fingerprint(request)
    current_project = request.headers.get('X-Current-Project', 'all')
    if not current_project:
        current_project = 'all'
    
    # Store session
    try:
        with cursor() as cur:
            cur.execute("""
                INSERT INTO user_sessions 
                (token_hash, email, project_scope, device_fingerprint, created_at, expires_at)
                VALUES (?, ?, ?, ?, datetime('now'), datetime('now', ?))
            """, (hashed_session_id, email, current_project, device_fingerprint, f'+{SESSION_EXPIRY_DAYS} days'))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to create session")
    
    # Clean up used code
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
    except Exception as e:
        print(f"Failed to delete used auth code for {email}: {str(e)}")
    
    # Set cookie: encrypt the session_id
    encrypted_token = encrypt_payload({'session_id': session_id})
    response.set_cookie(
        key="hermes_session",
        value=encrypted_token,
        httponly=True,      # Not accessible via JS (XSS protection)
        secure=True,        # HTTPS-only in production (should be set by proxy)
        samesite="Lax",     # CSRF protection
        max_age=SESSION_EXPIRY_DAYS*24*60*60, # in seconds
        path="/",
    )
    return {
        "message": "Authenticated",
        "user": {"email": email},
        "project_scope": current_project
    }

@router.get("/status")
async def auth_status(request: Request, hermes_session: str = Cookie(None)):
    if not hermes_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Decrypt token to get session_id
    try:
        payload = decrypt_payload(hermes_session)
        session_id = payload.get('session_id')
        if not session_id:
            raise HTTPException(status_code=401, detail="Invalid session")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    # Hash the session_id to look up in database
    hashed_session_id = hash_token(session_id)
    
    # Verify session still exists in DB and is valid
    try:
        with cursor() as cur:
            cur.execute(
                """
                SELECT email FROM user_sessions
                WHERE token_hash = ? AND expires_at > datetime('now')
                """,
                (hashed_session_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    if not row:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    
    return {"authenticated": True, "email": row["email"]}

@router.post("/logout")
async def logout(request: Request, response: Response, hermes_session: str = Cookie(None)):
    if hermes_session:
        try:
            payload = decrypt_payload(hermes_session)
            session_id = payload.get('session_id')
            if session_id:
                hashed_session_id = hash_token(session_id)
                with cursor() as cur:
                    cur.execute("DELETE FROM user_sessions WHERE token_hash = ?", (hashed_session_id,))
        except Exception as e:
            print(f"Error during logout: {str(e)}")
    
    response.delete_cookie(key="hermes_session", path="/")
    return {"message": "Logged out"}