"""Royal Mail Tracking API v2 client for Parcel IMAP Scan.

Royal Mail's own public tracking page is hCaptcha-gated, so there is no
scrape-friendly fallback for live status -- this only works once Vaughn's
own Royal Mail developer-portal app has been subscribed/approved for the
Tracking API and its client_id/client_secret are set in the add-on's own
options (never typed by Claude, same as every other credential here). Auth
is two static headers per request (IBM API Connect gateway), not an OAuth
token exchange.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("parcel_imap_scan.royal_mail")

BASE_URL = "https://api.royalmail.net/mailpieces/v2"


def get_summary(client_id: str, client_secret: str, tracking_number: str) -> dict | None:
    """Return the `summary` object for one tracking number, or None on any
    error (bad/unknown number, API outage, bad credentials -- all logged,
    none raised, so one bad parcel never breaks the poll loop for the rest).
    """
    headers = {
        "X-IBM-Client-Id": client_id,
        "X-IBM-Client-Secret": client_secret,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"{BASE_URL}/{tracking_number}/events",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        mail_pieces = data.get("mailPieces") or {}
        return mail_pieces.get("summary")
    except Exception:  # noqa: BLE001
        log.exception("Royal Mail lookup failed for %s", tracking_number)
        return None
