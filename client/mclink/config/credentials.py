"""
MC-Link client -- optional "remember my password".

============================================================================
 WHAT THIS PROTECTS AGAINST, AND WHAT IT DOES NOT
============================================================================

Stated plainly, because the alternative is a false sense of safety:

PROTECTS AGAINST
    * somebody reading the config file over your shoulder, or finding it in a
      backup, a screenshot, or a synced folder, and seeing your password as
      plain text
    * the password being visible to a casual `type config.json`

DOES NOT PROTECT AGAINST
    * malware already running as you. Any key this program can derive, that
      program can derive too.
    * somebody with your unlocked machine. They can just open MC-Link and read
      the password out of the login box.

There is no way to do better than this without a real crypto dependency and a
passphrase you type every launch -- at which point you may as well type the
password. So this is a convenience feature with a speed bump, and it is
described as such in the UI.

============================================================================
 HOW IT WORKS
============================================================================

    key   = PBKDF2-HMAC-SHA256(machine secret, random salt, 200000 iterations)
    blob  = password XOR keystream derived from `key`
    auth  = HMAC-SHA256(key, salt || blob)

The machine secret is the Windows MachineGuid (or a per-user fallback), which
means the file is useless when copied to another computer.

The HMAC is what makes tampering detectable: editing the blob to a different
password produces a mismatch and the entry is treated as absent, rather than
producing a wrong password that the user then blames on the server.

============================================================================
 VERSIONING: THE PASSWORD DIES ON EVERY UPDATE, THE CHOICE DOES NOT
============================================================================

The stored entry records the app version that wrote it. On launch, an entry
from a different version is DISCARDED rather than migrated, so every update
asks for the password exactly once more.

The user's CHOICE to remember is kept separately and survives the update, in
a tiny file that contains no secret. That distinction matters: without it,
"记住密码" would silently switch itself off after every update, and somebody
who never notices the checkbox would have to retype their password forever
while believing the feature was on.

So the behaviour after an update is:

    1. stored password is deleted, checkbox stays ticked
    2. user types the password once
    3. it is stored again, and remembered from then on

`clear()` deliberately forgets the CHOICE as well -- unticking the box has to
mean "stop remembering", not "stop remembering until you next log in".
"""

import base64
import hashlib
import hmac
import json
import os
import sys

#: Bump to invalidate every stored credential (format change).
FORMAT_VERSION = 1

#: Deliberately modest. This is not defending against an offline attack by
#: somebody who already has the file AND the machine -- see the docstring.
#: 200k iterations takes a noticeable fraction of a second to derive, which is
#: the only thing it is really buying: it makes bulk decryption annoying.
ITERATIONS = 200_000

SALT_BYTES = 16
KEY_BYTES = 32

#: Suffix for the preference file. It holds one boolean and no secret.
PREFS_SUFFIX = ".prefs"


def _storage_path():
    """Where the credential file lives. Sibling of the config file."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    directory = os.path.join(base, "MC-Link")

    return os.path.join(directory, "credentials.dat")


def _prefs_path():
    return _storage_path() + PREFS_SUFFIX


def _machine_secret():
    """
    A per-machine, per-user secret used to derive the key.

    On Windows the MachineGuid is a stable per-installation identifier, so the
    credential file stops working if it is copied to another computer. On other
    platforms (and if the registry read fails) the user name plus the home
    directory is used, which still separates accounts on one machine but does
    not travel-proof the file. That limitation is stated rather than hidden.
    """
    parts = ["mc-link-credentials-v1"]

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")

                if guid:
                    parts.append(str(guid))
        except Exception:
            # Falls through to the portable ingredients below.
            pass

    parts.append(os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    parts.append(os.path.expanduser("~"))

    return "|".join(parts).encode("utf-8")


def _derive_key(salt):
    return hashlib.pbkdf2_hmac(
        "sha256", _machine_secret(), salt, ITERATIONS, dklen=KEY_BYTES
    )


def _keystream(key, length, nonce):
    """
    A keystream from the key. SHA-256 in counter mode.

    Not a cipher anybody should design a protocol around; it is here because a
    password needs to be hidden from a text editor, not protected from a
    cryptanalyst. The HMAC below is what actually matters for correctness.
    """
    out = bytearray()
    counter = 0

    while len(out) < length:
        out.extend(
            hashlib.sha256(
                key + nonce + counter.to_bytes(4, "big")
            ).digest()
        )
        counter += 1

    return bytes(out[:length])


# ============================================================
# Public API
# ============================================================

def save(username, password, app_version):
    """
    Store a credential. Returns True on success. Never raises.

    `app_version` is recorded so a later launch can tell that the file was
    written by a different build and discard the password. The user's choice
    to remember is stored separately, so it survives that discard.
    """
    username = (username or "").strip()

    if not username or not password:
        return False

    try:
        salt = os.urandom(SALT_BYTES)
        nonce = os.urandom(16)
        key = _derive_key(salt)

        raw = password.encode("utf-8")
        blob = bytes(
            a ^ b for a, b in zip(raw, _keystream(key, len(raw), nonce))
        )

        auth = hmac.new(key, salt + nonce + blob, hashlib.sha256).digest()

        payload = {
            "format": FORMAT_VERSION,
            "app_version": str(app_version or ""),
            "username": username,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "blob": base64.b64encode(blob).decode("ascii"),
            "auth": base64.b64encode(auth).decode("ascii"),
        }

        path = _storage_path()
        directory = os.path.dirname(path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

        # Owner-only where the OS supports it. Best effort: on Windows the
        # ACL is inherited from the profile directory, which is already
        # restricted to the user.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        # The choice survives version changes, so it is recorded next to the
        # secret rather than inside it.
        save_remember_choice(True)

        return True

    except Exception:
        return False


def save_remember_choice(remember):
    """
    Remember whether the user wants their password stored. No secret here.

    Kept in its own file so that discarding the password after an update does
    not also discard the intent to keep one -- otherwise the checkbox would
    quietly untick itself on every release, and the user would retype their
    password forever while believing the feature was on.
    """
    try:
        path = _prefs_path()
        directory = os.path.dirname(path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"remember": bool(remember)}, fh)

        return True
    except Exception:
        return False


def remember_choice():
    """
    Whether the user last asked to be remembered. Defaults to False.

    Defaulting to False is deliberate: the checkbox must never tick itself for
    somebody who never opted in.
    """
    try:
        with open(_prefs_path(), "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        return bool(payload.get("remember"))
    except Exception:
        return False


def load(app_version):
    """
    Load a stored credential. Returns (username, password) or (None, None).

    Discards the entry when any of these is true, which is the whole point of
    storing the version:

      * it was written by a DIFFERENT app version
      * the format version changed
      * the HMAC does not verify (tampered with, or a different machine)

    A discarded entry is deleted from disk, so a stale file does not linger.
    """
    path = _storage_path()

    try:
        if not os.path.isfile(path):
            return None, None

        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        if not isinstance(payload, dict):
            clear_password_only()
            return None, None

        if int(payload.get("format") or 0) != FORMAT_VERSION:
            clear_password_only()
            return None, None

        # Version change => the stored password is dropped, not migrated. The
        # user's CHOICE to remember is kept (see save_remember_choice), so they
        # type the password once more and it is stored again.
        if str(payload.get("app_version") or "") != str(app_version or ""):
            clear_password_only()
            return None, None

        username = str(payload.get("username") or "")
        salt = base64.b64decode(payload["salt"])
        nonce = base64.b64decode(payload["nonce"])
        blob = base64.b64decode(payload["blob"])
        auth = base64.b64decode(payload["auth"])

        key = _derive_key(salt)

        expected = hmac.new(
            key, salt + nonce + blob, hashlib.sha256
        ).digest()

        if not hmac.compare_digest(expected, auth):
            # Tampered with, or written on another machine. Treat as absent
            # rather than handing over a wrong password.
            clear_password_only()
            return None, None

        raw = bytes(
            a ^ b for a, b in zip(blob, _keystream(key, len(blob), nonce))
        )

        password = raw.decode("utf-8")

        return username, password

    except Exception:
        # Corrupt file, unreadable, bad base64: all mean "nothing stored".
        clear_password_only()
        return None, None


def clear_password_only():
    """
    Delete the stored PASSWORD but keep the remember choice.

    Used for the version change, where the password must not survive but the
    user's intent should. Everything else (tampering, corruption) uses
    clear(), because in those cases the stored state is untrustworthy.
    """
    try:
        os.remove(_storage_path())
        return True
    except OSError:
        return False


def clear():
    """
    Forget everything: the password AND the choice to remember it.

    Unticking the checkbox calls this, because "don't remember my password"
    has to actually mean that.
    """
    clear_password_only()

    try:
        os.remove(_prefs_path())
        return True
    except OSError:
        return False


def exists():
    return os.path.isfile(_storage_path())


__all__ = [
    "save",
    "load",
    "clear",
    "clear_password_only",
    "exists",
    "save_remember_choice",
    "remember_choice",
    "FORMAT_VERSION",
    "_storage_path",
    "_prefs_path",
]
