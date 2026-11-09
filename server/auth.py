"""
MC-Link relay -- password hashing.

The hash format is shared with the PHP admin panel so that an account created
in either place can log in through the other.

Format (single column, self describing):

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

* PBKDF2-HMAC-SHA256, 260000 iterations (matches PHP's
  hash_pbkdf2('sha256', $pw, $salt, 260000, 32, true))
* 16 byte random salt
* 32 byte derived key

Standard library only -- no extra dependency, and identical results in Python
and PHP (both are UTF-8 byte strings, no normalisation applied).
"""

import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 260000
SALT_BYTES = 16
KEY_BYTES = 32


def hash_password(password, iterations=DEFAULT_ITERATIONS, salt=None):
    """Return a self describing hash string for `password`."""
    if password is None:
        password = ""

    if salt is None:
        salt = os.urandom(SALT_BYTES)

    if isinstance(salt, str):
        salt = bytes.fromhex(salt)

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        salt,
        int(iterations),
        dklen=KEY_BYTES,
    )

    return (
        f"{ALGORITHM}${int(iterations)}${salt.hex()}${derived.hex()}"
    )


def verify_password(password, stored):
    """
    Constant time verification.

    Returns (ok, needs_rehash). Never raises.
    """
    if not stored or not isinstance(stored, str):
        return False, False

    parts = stored.split("$")

    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False, False

    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False, False

    try:
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
    except Exception:
        return False, False

    ok = hmac.compare_digest(derived, expected)

    needs_rehash = ok and iterations < DEFAULT_ITERATIONS

    return ok, needs_rehash
