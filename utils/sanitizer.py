# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from __future__ import annotations

import re

# Email addresses: user@domain.tld
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Phone numbers: international and domestic formats
# Matches +1-800-555-1234, (800) 555-1234, 800.555.1234, etc.
_PHONE_RE = re.compile(
    r"(?<!\w)(\+?\d[\d\s\-().]{7,}\d)(?!\w)"
)

# Potential API keys / tokens / secrets: bare alphanumeric strings ≥ 32 chars
# Excludes URLs, UUIDs already handled via email/phone, and common prose words
_TOKEN_RE = re.compile(
    r"(?<![/\w])([A-Za-z0-9+/]{32,}={0,2})(?![/\w])"
)


def redact_pii(text: str) -> str:
    """
    Replace common PII patterns in *text* with safe placeholders.

    Patterns redacted:
    - Email addresses      → [EMAIL REDACTED]
    - Phone numbers        → [PHONE REDACTED]
    - Long token strings   → [TOKEN REDACTED]

    Returns the sanitised string. If *text* is empty or None, returns it unchanged.
    """
    if not text:
        return text

    text = _EMAIL_RE.sub("[EMAIL REDACTED]", text)
    text = _PHONE_RE.sub("[PHONE REDACTED]", text)
    text = _TOKEN_RE.sub("[TOKEN REDACTED]", text)
    return text
