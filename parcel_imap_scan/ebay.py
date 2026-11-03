"""eBay Trading API (GetOrders) client for Parcel IMAP Scan.

Covers the full 3-legged OAuth lifecycle needed to call the legacy Trading
API's GetOrders call with a modern OAuth user token, plus the GetOrders
call itself, parsed down to the handful of fields this add-on cares about.

Why the Trading API and not eBay's newer REST APIs: eBay's modern
Fulfillment API (sell/fulfillment/v1/order) is seller-order-management only
-- it will never show things the account *bought*. The legacy Trading
API's GetOrders call is explicitly documented to return orders "in which
the authenticated caller is either the buyer or seller", which is what's
needed here (Vaughn tracking his own purchases). GetOrders still accepts a
modern OAuth user access token via the X-EBAY-API-IAF-TOKEN header instead
of the old eBayAuthToken/RequesterCredentials scheme.

One-time setup (done by the human in their own browser, never by this
code): open the URL from build_authorize_url() in your browser, sign in
and consent, then copy the "code" query-string value from the page you
land on afterwards (the RuName's configured "Auth accepted URL" -- it does
not need to actually resolve to anything, you're only reading the URL the
browser redirected to) into the add-on's ebay_auth_code option. The add-on
exchanges that one-time code for a refresh token on its next restart
(exchange_code_for_tokens) and persists the refresh token into its own
options via the Supervisor API from then on, refresh_access_token() mints
a fresh ~2-hour access token from that refresh token (refresh tokens last
about 18 months before a fresh consent is needed again).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("parcel_imap_scan.ebay")

AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRADING_API_URL = "https://api.ebay.com/ws/api.dll"

# Basic default scope -- sufficient for Trading API calls authorized via
# X-EBAY-API-IAF-TOKEN; the Trading API doesn't use REST-style per-resource
# scopes the way the newer APIs do.
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay numeric site ID for the UK site (Vaughn's eBay/Amazon accounts are
# both .co.uk). See SiteCodeType in the Trading API docs for other values.
SITE_ID = 3
COMPATIBILITY_LEVEL = "1193"


def build_authorize_url(app_id: str, ru_name: str) -> str:
    """URL for the human to open in their OWN browser to grant consent."""
    from urllib.parse import quote

    return (
        f"{AUTHORIZE_URL}?client_id={quote(app_id)}"
        f"&redirect_uri={quote(ru_name)}"
        f"&response_type=code"
        f"&scope={quote(OAUTH_SCOPE)}"
    )


def _token_request(app_id: str, cert_id: str, data: dict) -> dict | None:
    import base64

    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }
    try:
        resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001
        log.exception("eBay OAuth token request failed")
        return None


def exchange_code_for_tokens(app_id: str, cert_id: str, ru_name: str, code: str) -> dict | None:
    """One-time: trade the authorization code from the consent redirect for
    an access token + refresh token. Returns None on failure (e.g. the code
    already expired -- eBay auth codes are only valid for a few minutes)."""
    return _token_request(
        app_id,
        cert_id,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ru_name,
        },
    )


def refresh_access_token(app_id: str, cert_id: str, refresh_token: str) -> dict | None:
    """Mint a fresh ~2-hour access token from the long-lived refresh token."""
    return _token_request(
        app_id,
        cert_id,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": OAUTH_SCOPE,
        },
    )


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find(elem: ET.Element, path: str) -> ET.Element | None:
    """Namespace-agnostic single-step-at-a-time find (eBay's response uses
    one flat namespace throughout, so stripping it from every tag as we
    walk is simpler than registering namespace maps)."""
    for step in path.split("/"):
        if elem is None:
            return None
        nxt = None
        for child in elem:
            if _strip_ns(child.tag) == step:
                nxt = child
                break
        elem = nxt
    return elem


def get_orders(access_token: str, number_of_days: int = 30) -> list[dict]:
    """Return recent orders (buyer or seller role) as a list of
    {order_id, title, status, tracking_number, carrier} dicts."""
    headers = {
        "X-EBAY-API-IAF-TOKEN": access_token,
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "X-EBAY-API-CALL-NAME": "GetOrders",
        "X-EBAY-API-SITEID": str(SITE_ID),
        "Content-Type": "text/xml",
    }
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<NumberOfDays>{int(number_of_days)}</NumberOfDays>"
        "<OrderStatus>All</OrderStatus>"
        "</GetOrdersRequest>"
    )
    try:
        resp = requests.post(TRADING_API_URL, headers=headers, data=body.encode("utf-8"), timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception:  # noqa: BLE001
        log.exception("eBay GetOrders request failed")
        return []

    ack = _find(root, "Ack")
    if ack is None or (ack.text or "").strip() not in ("Success", "Warning"):
        errors = []
        for err in root:
            if _strip_ns(err.tag) == "Errors":
                msg = _find(err, "LongMessage") or _find(err, "ShortMessage")
                if msg is not None:
                    errors.append(msg.text)
        log.error("eBay GetOrders returned Ack=%s errors=%s", ack.text if ack is not None else None, errors)
        return []

    order_array = _find(root, "OrderArray")
    if order_array is None:
        return []

    results: list[dict] = []
    for order in order_array:
        if _strip_ns(order.tag) != "Order":
            continue
        order_id_el = _find(order, "OrderID")
        order_id = order_id_el.text if order_id_el is not None else None
        if not order_id:
            continue

        status_el = _find(order, "OrderStatus")
        order_status = status_el.text if status_el is not None else ""

        tracking_number = None
        carrier = None
        shipping_details = _find(order, "ShippingDetails")
        if shipping_details is not None:
            std = _find(shipping_details, "ShipmentTrackingDetails")
            if std is not None:
                num_el = _find(std, "ShipmentTrackingNumber")
                carrier_el = _find(std, "ShippingCarrierUsed")
                tracking_number = num_el.text if num_el is not None else None
                carrier = carrier_el.text if carrier_el is not None else None

        title = None
        transaction_array = _find(order, "TransactionArray")
        if transaction_array is not None:
            for txn in transaction_array:
                if _strip_ns(txn.tag) != "Transaction":
                    continue
                item = _find(txn, "Item")
                if item is not None:
                    title_el = _find(item, "Title")
                    if title_el is not None:
                        title = title_el.text
                break

        if tracking_number:
            status = "Shipped"
        elif order_status == "Completed":
            status = "Paid"
        else:
            status = order_status or "Unknown"

        results.append(
            {
                "order_id": order_id,
                "title": title or "",
                "status": status,
                "tracking_number": tracking_number,
                "carrier": carrier,
            }
        )
    return results
