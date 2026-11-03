"""Amazon order-status email parsing, ported from koconnorgit/ha-amazon-order-status
(custom_components/amazon_order_status/coordinator.py + const.py, MIT-style HA
custom integration) into this add-on.

Why port instead of installing that integration: it's a separate HA
custom_component with its own config flow that requires typing IMAP
credentials into HA's UI a second time. This add-on already has a live,
working IMAP connection (its own options.json, entered once by the user via
the add-on's Configuration tab) -- so instead of a second credential entry,
we reuse the same connection and the same scanner.py parse loop, and just
add one more thing to look for per message.

Unlike tracking-number extraction (extract.py), Amazon's own order emails
almost never contain a real carrier tracking number -- only Amazon's own
order ID and an Amazon-hosted order-tracking URL (see the upstream
integration's README, confirmed by inspecting live Amazon shipping emails:
the "Track package" link resolves through amazon.co.uk/your-orders or
amazon.co.uk/progress-tracker, not a carrier site). So this is reported
into Home Assistant as its own line per order (keyed by Amazon order ID,
status updated in place as new status emails arrive), not merged into the
carrier-tracking-number todo items extract.py produces.
"""
from __future__ import annotations

import re

# Amazon order ID: ddd-ddddddd-ddddddd (e.g. 204-4392814-1759516).
ORDER_REGEX = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")

# Subject substring -> status, checked case-insensitively against the email
# subject. Order matters: more specific phrases first, since a subject can
# contain more than one of these loosely (e.g. "shipped" inside a longer
# "out for delivery" subject on some locales) -- first match wins.
STATUS_MAP: list[tuple[str, str]] = [
    ("out for delivery", "Out for delivery"),
    ("delivered", "Delivered"),
    ("shipped", "Shipped"),
    ("has shipped", "Shipped"),
    ("dispatched", "Shipped"),
    ("order confirmation", "Ordered"),
    ("ordered", "Ordered"),
    ("your order", "Ordered"),
]

# Only treat a message as an Amazon order-status email if it both looks like
# one by sender/subject AND contains a recognisable order ID -- keeps this
# from ever matching unrelated mail just because it contains a dash-grouped
# number by coincidence.
_SENDER_KEYWORDS = ("amazon",)

# Matches an <a ...>LINK TEXT</a> anchor so we can look for Amazon's own
# order-tracking links specifically (not every href in the email -- Amazon
# marketing emails are full of unrelated links). No external HTML-parser
# dependency (BeautifulSoup) needed for this narrow case: we only care about
# the href itself, not the anchor text, and Amazon's tracking links always
# contain one of a few fixed path fragments.
_TRACKING_HREF_RE = re.compile(
    r'href\s*=\s*["\']([^"\']*(?:progress-tracker|your-orders|order-history|'
    r'package-tracking)[^"\']*)["\']',
    re.IGNORECASE,
)


def extract_order_status(subject: str, sender: str, html_body: str) -> dict | None:
    """Return {"order_id", "status", "subject", "tracking_url"} or None.

    html_body should be the *raw* HTML part (before scanner.py's
    _html_to_text strips tags) so the tracking-link regex has tags to match
    against; pass "" if only plain text was available (tracking_url will
    just come back None in that case).
    """
    sender_lower = (sender or "").lower()
    subject_lower = (subject or "").lower()

    if not any(k in sender_lower for k in _SENDER_KEYWORDS):
        return None

    order_match = ORDER_REGEX.search(f"{subject or ''} {html_body or ''}")
    if not order_match:
        return None

    status = None
    for phrase, mapped in STATUS_MAP:
        if phrase in subject_lower:
            status = mapped
            break
    if status is None:
        return None

    tracking_url = None
    href_match = _TRACKING_HREF_RE.search(html_body or "")
    if href_match:
        tracking_url = href_match.group(1).replace("&amp;", "&")

    return {
        "order_id": order_match.group(0),
        "status": status,
        "subject": subject or "",
        "tracking_url": tracking_url,
    }
