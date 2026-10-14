import os
import hashlib
import secrets
from cryptography.fernet import Fernet

# Server-secret key (set via HERMES_SESSION_KEY env var, 32-byte base64)
# If not set, generate a key (but note: this will change on every restart, invalidating sessions)
_SESSION_KEY = os.environ.get("HERMES_SESSION_KEY")
if not _SESSION_KEY:
    # Generate a key and log a warning (in production, this must be set)
    _SESSION_KEY = Fernet.generate_key()
    # In a real app, we would log this, but for now we'll just set it.
    # Important: For production, set HERMES_SESSION_KEY to a fixed 32-byte base64 string.
_CIPHER = Fernet(_SESSION_KEY)

# Hash salt (for token hashing)
_HASH_SALT = os.environ.get("HERMES_HASH_SALT", "hermes-salt").encode()

def generate_token(length=32) -> str:
    """Generate cryptographically random token (for session IDs, etc.)"""
    return secrets.token_urlsafe(length)

def hash_token(token: str) -> str:
    """One-way hash for storage (with server-side salt)"""
    return hashlib.pbkdf2_hmac('sha256', token.encode(), _HASH_SALT, 100000).hex()

def verify_token(token: str, token_hash: str) -> bool:
    """Verify token against stored hash"""
    return secrets.compare_digest(hash_token(token), token_hash)

def encrypt_payload(payload: dict) -> str:
    """Encrypt session payload (JWE)"""
    import json
    plaintext = json.dumps(payload).encode()
    return _CIPHER.encrypt(plaintext).decode()

def decrypt_payload(encrypted: str) -> dict:
    """Decrypt and validate session payload"""
    try:
        plaintext = _CIPHER.decrypt(encrypted.encode())
        import json
        return json.loads(plaintext)
    except Exception:
        return None  # Invalid tampered token

def generate_device_fingerprint(request) -> str:
    """Create non-reversible device fingerprint"""
    components = [
        request.headers.get('User-Agent', ''),
        request.headers.get('Accept-Language', ''),
        str(request.remote_addr or '')[:15]  # Truncate IP for privacy
    ]
    raw = "|".join(components).encode()
    return hashlib.sha256(raw).hexdigest()[:16]  # Truncate for storage