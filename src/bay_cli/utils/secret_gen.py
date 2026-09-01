"""The one secret generator.

Both ``bin/bay secret`` and the setup wizard mint passwords here, so a
scaffolded project and a hand-generated secret have identical strength.
Anything that writes a secret into a consumer's vault must call this —
never ``secrets.token_urlsafe`` directly.
"""

from __future__ import annotations

import secrets

# token_urlsafe emits ~1.3 characters per byte, so ask for well more than
# the slice length and truncate. 48 bytes -> ~64 characters -> 32 kept.
_BYTES_PER_CHAR = 1.5


def generate_password(length: int = 32) -> str:
    """Return a URL-safe random password of exactly *length* characters."""
    if length < 8:
        raise ValueError("password length must be at least 8 characters")
    nbytes = int(length * _BYTES_PER_CHAR) + 8
    return secrets.token_urlsafe(nbytes)[:length]
