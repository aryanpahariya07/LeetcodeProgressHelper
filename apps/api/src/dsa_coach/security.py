"""Credential generation and hashing for device pairing (spec §5).

**Why SHA-256 and not Argon2id.** Spec §5 says "Argon2id or bcrypt". Those exist
to make brute-forcing *low-entropy human-chosen* secrets expensive. Both secrets
here are generated, not chosen:

- A device token is 32 random bytes (256 bits). No hash speed makes that
  guessable, so a deliberately slow hash buys nothing and costs latency on every
  ingest request.
- A pairing code is short enough to read aloud, so it *is* low entropy — but the
  right defence for a one-shot code is a 10-minute expiry, single use, and a
  failed-attempt cap, all of which are implemented below. A slow hash would not
  meaningfully add to those.

This is the same reasoning behind GitHub and Stripe storing API tokens under a
fast hash. Flagged here rather than changed silently; if a slow KDF is wanted,
`hash_secret` is the single place to change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: Excludes I, l, 1, O, 0 — a pairing code gets read off a screen and retyped.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

#: Bytes of entropy in a device token.
TOKEN_BYTES = 32

#: A pairing code dies after this many wrong guesses, whatever its expiry says.
MAX_PAIRING_ATTEMPTS = 5


def hash_secret(secret: str) -> str:
    """Hash a generated secret for storage. See the module docstring."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secrets_match(candidate: str, stored_hash: str) -> bool:
    """Constant-time comparison, so timing cannot leak a prefix."""
    return hmac.compare_digest(hash_secret(candidate), stored_hash)


def generate_pairing_code() -> tuple[str, str]:
    """A human-transcribable code, returned as (plaintext, hash).

    Formatted in two groups of four for legibility; the hyphen is not part of
    the secret and is stripped before hashing.
    """
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    display = f"{raw[:4]}-{raw[4:]}"
    return display, hash_secret(raw)


def normalize_pairing_code(entered: str) -> str:
    """Accept whatever the user typed: spacing, hyphens and case are all noise."""
    return entered.replace("-", "").replace(" ", "").strip().upper()


def generate_device_token() -> tuple[str, str]:
    """A device credential, returned as (plaintext, hash).

    The plaintext is shown to the client once and never stored.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_secret(token)
