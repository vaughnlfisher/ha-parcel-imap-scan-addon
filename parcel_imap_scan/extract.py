"""Tracking-number extraction patterns for Parcel IMAP Scan.

Kept in its own module so patterns can be extended without touching the
IMAP/service plumbing in __init__.py. Add a new tuple to TRACKING_PATTERNS
for a new carrier; add it to SENDER_GATED + SENDER_KEYWORDS if its number
format is too generic to trust without corroborating sender/subject text.
"""
from __future__ import annotations

import re

# (carrier label, compiled regex). Order matters only for SENDER_GATED dedup
# (first match for a given number string wins).
TRACKING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Amazon Logistics", re.compile(r"\bTBA[0-9]{12}\b", re.IGNORECASE)),
    ("UPS", re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE)),
    ("Royal Mail / Parcelforce", re.compile(r"\b[A-Z]{2}[0-9]{9}GB\b", re.IGNORECASE)),
    ("USPS", re.compile(r"\b(?:92|93|94|95)[0-9]{18,20}\b")),
    ("AliExpress / Cainiao", re.compile(r"\b(?:LP|LX|RR|RB|RC)[0-9]{9}CN\b", re.IGNORECASE)),
    ("DHL", re.compile(r"\b[0-9]{10,11}\b")),
    # FedEx numbers aren't limited to a 96/61/62 prefix (e.g. door-tag/Express
    # numbers like 879560352732) -- any 12, 15, or 20-22 digit run, gated by
    # sender/subject since that range alone is too generic to trust blind.
    ("FedEx", re.compile(r"\b(?:[0-9]{12}|[0-9]{15}|[0-9]{20,22})\b")),
    ("DPD", re.compile(r"\b[0-9]{14}\b")),
    # Evri (formerly Hermes) numbers mix letters and digits after the leading
    # H (e.g. H06R4A0243030310) -- a digits-only pattern missed these.
    ("Evri / Hermes", re.compile(r"\bH[0-9A-Z]{14,19}\b", re.IGNORECASE)),
    ("Yodel", re.compile(r"\b[0-9]{16}\b")),
]

# Patterns broad enough to collide with invoice numbers, phone numbers, etc.
# Only counted as a match when the sender or subject also mentions the carrier.
SENDER_GATED = {"DHL", "FedEx", "DPD", "Yodel", "Evri / Hermes"}
SENDER_KEYWORDS: dict[str, list[str]] = {
    "DHL": ["dhl"],
    "FedEx": ["fedex"],
    "DPD": ["dpd"],
    "Yodel": ["yodel"],
    "Evri / Hermes": ["evri", "hermes"],
}


def extract_tracking(subject: str, sender: str, body: str) -> list[tuple[str, str]]:
    """Return deduplicated (carrier, tracking_number) matches found in one email."""
    haystack = f"{subject or ''}\n{body or ''}"
    sender_lower = (sender or "").lower()
    subject_lower = (subject or "").lower()

    found: list[tuple[str, str]] = []
    seen_numbers: set[str] = set()

    for carrier, pattern in TRACKING_PATTERNS:
        if carrier in SENDER_GATED:
            keywords = SENDER_KEYWORDS.get(carrier, [])
            if not any(k in sender_lower or k in subject_lower for k in keywords):
                continue
        for match in pattern.findall(haystack):
            number = match.upper()
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            found.append((carrier, number))

    return found
