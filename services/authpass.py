"""Salted password hashing for the host's unattended-access password.

Only a PBKDF2 hash is ever persisted (never the plaintext), and verification is
constant-time so a wrong guess leaks no timing signal."""

import os
import hmac
import hashlib

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password):
    """Return a self-describing hash string 'pbkdf2_sha256$iters$salt$hash'."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                             salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    """True iff `password` matches the stored hash. False on any malformed or
    empty stored value."""
    if not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk, bytes.fromhex(hash_hex))
    except (ValueError, AttributeError):
        return False
