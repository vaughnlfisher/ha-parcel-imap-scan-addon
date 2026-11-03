"""Parcel IMAP Scan add-on.

Runs as a standalone Home Assistant add-on (its own Docker container) rather
than a custom_components integration, specifically so IMAP stays permanently
connected, debugging is a normal container log stream, and code changes only
need the add-on restarted (seconds) instead of a full HA core restart.

Two modes, both enabled by default:
  * Live watch: a persistent IMAP IDLE connection picks up new parcel emails
    within seconds of arrival.
  * On-demand backfill: POST /scan {"days_back": N} re-scans the last N days
    of mail (for catching up on a gap, or re-running after a pattern tweak).

Found tracking numbers are reported into Home Assistant's own to-do list via
the HA Core REST API proxy at http://supervisor/core/api, authenticated with
the SUPERVISOR_TOKEN Supervisor automatically injects into every add-on
container (once homeassistant_api: true is set in config.yaml) -- NOT a
manually-created long-lived access token. (An earlier version of this add-on
required the user to paste in their own long-lived token, which never worked
against the supervisor/core proxy: that endpoint only accepts the add-on's
own Supervisor-issued token. The ha_token option is kept only as an optional
override/fallback for running this script outside of Supervisor.)
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import Message
from pathlib import Path

import aioimaplib
import requests
from aiohttp import web

from extract import extract_tracking
from amazon_orders import extract_order_status

OPTIONS_PATH = Path("/data/options.json")


def load_options() -> dict:
    with OPTIONS_PATH.open() as f:
        return json.load(f)


OPTIONS = load_options()

logging.basicConfig(
    level=getattr(logging, str(OPTIONS.get("log_level", "info")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("parcel_imap_scan")

# aioimaplib logs every raw IMAP line (including full message bodies) at
# DEBUG, which floods the add-on's log and buries our own messages. Keep it
# at WARNING regardless of our own log_level setting.
logging.getLogger("aioimaplib.aioimaplib").setLevel(logging.WARNING)

IMAP_EMAIL = OPTIONS["imap_email"]
IMAP_PASSWORD = OPTIONS["imap_password"]
IMAP_SERVER = OPTIONS.get("imap_server", "imap.gmail.com")
IMAP_PORT = int(OPTIONS.get("imap_port", 993))
IMAP_FOLDER = OPTIONS.get("imap_folder", "INBOX")
HA_URL = str(OPTIONS.get("ha_url", "http://supervisor/core")).rstrip("/")
# Prefer the Supervisor-injected token (this is what http://supervisor/core
# actually authenticates against); fall back to a manually-configured token
# only if SUPERVISOR_TOKEN isn't present (e.g. running outside Supervisor).
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or OPTIONS.get("ha_token", "")
if not HA_TOKEN:
    log.warning(
        "No SUPERVISOR_TOKEN in the environment and no ha_token option set; "
        "calls to Home Assistant will fail with 401."
    )
TODO_ENTITY = OPTIONS.get("todo_entity", "todo.parcels")
DAYS_BACK_DEFAULT = int(OPTIONS.get("days_back_default", 90))
IDLE_ENABLED = bool(OPTIONS.get("idle_enabled", True))

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Email body extraction (ported from the custom_components integration, with
# the HTML-fallback fix: many retail/shipping emails are HTML-only or have a
# near-empty plain-text alternative, with the tracking number only present
# as link text or inside an href.)
# ---------------------------------------------------------------------------

def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    decoded = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    hrefs = " ".join(_HREF_RE.findall(html))
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _WS_RE.sub(" ", text)
    return f"{text}\n{hrefs}"


def _extract_body(msg: Message) -> tuple[str, str]:
    """Return (body_text, raw_html). raw_html is the first text/html part's
    untouched markup (empty string if the message has none) -- kept
    alongside the existing flattened body_text so amazon_orders.py can match
    hrefs by surrounding tag context without re-parsing the message."""
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        raw_html = ""
        for part in msg.walk():
            if part.get_filename():
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            decoded = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
            if content_type == "text/plain":
                plain_parts.append(decoded)
            else:
                html_parts.append(_html_to_text(decoded))
                if not raw_html:
                    raw_html = decoded
        return "\n".join(plain_parts + html_parts), raw_html
    payload = msg.get_payload(decode=True)
    if not payload:
        return "", ""
    decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return _html_to_text(decoded), decoded
    return decoded, ""


def _parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    subject = _decode_header_value(msg.get("Subject"))
    sender = _decode_header_value(msg.get("From"))
    try:
        body, raw_html = _extract_body(msg)
    except Exception:  # noqa: BLE001
        body, raw_html = "", ""
    return {"subject": subject, "sender": sender, "body": body, "raw_html": raw_html}


# ---------------------------------------------------------------------------
# Home Assistant REST API helpers
# ---------------------------------------------------------------------------

def ha_get_existing_summaries() -> str:
    try:
        resp = requests.post(
            f"{HA_URL}/api/services/todo/get_items?return_response",
            headers=HA_HEADERS,
            json={"entity_id": TODO_ENTITY, "status": "needs_action"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        service_response = data.get("service_response", data) if isinstance(data, dict) else {}
        items = service_response.get(TODO_ENTITY, {}).get("items", [])
        return " ".join(item.get("summary", "") for item in items)
    except Exception:  # noqa: BLE001
        log.exception("Failed to fetch existing todo items")
        return ""


def ha_add_item(carrier: str, number: str, sender: str, subject: str) -> None:
    try:
        resp = requests.post(
            f"{HA_URL}/api/services/todo/add_item",
            headers=HA_HEADERS,
            json={
                "entity_id": TODO_ENTITY,
                "item": f"{carrier}: {number}",
                "description": (
                    f"From: {sender} | Subject: {subject} | "
                    "Found by Parcel IMAP Scan add-on"
                ),
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Added %s: %s", carrier, number)
    except Exception:  # noqa: BLE001
        log.exception("Failed to add todo item for %s: %s", carrier, number)


def process_matches(parsed: dict) -> int:
    """Extract tracking numbers from one parsed message and add any new ones."""
    matches = extract_tracking(parsed["subject"], parsed["sender"], parsed["body"])
    if not matches:
        return 0
    existing = ha_get_existing_summaries()
    added = 0
    for carrier, number in matches:
        if number in existing:
            continue
        ha_add_item(carrier, number, parsed["sender"], parsed["subject"])
        existing += f" {number}"
        added += 1
    return added


# ---------------------------------------------------------------------------
# Amazon order-status upsert (ported logic, see amazon_orders.py)
#
# Unlike carrier tracking numbers (one add, never updated again), an Amazon
# order's status changes over its lifetime (Ordered -> Shipped -> Out for
# delivery -> Delivered), each stage its own email with the same order ID.
# So this keys off the order ID and updates the existing todo item's
# summary/description in place rather than adding a new item each time.
# ---------------------------------------------------------------------------

AMAZON_PREFIX = "Amazon: "


def ha_get_amazon_items() -> dict[str, dict]:
    """Return {order_id: {"uid": ..., "status": ...}} for existing Amazon todo items."""
    try:
        resp = requests.post(
            f"{HA_URL}/api/services/todo/get_items?return_response",
            headers=HA_HEADERS,
            json={"entity_id": TODO_ENTITY, "status": "needs_action"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        service_response = data.get("service_response", data) if isinstance(data, dict) else {}
        items = service_response.get(TODO_ENTITY, {}).get("items", [])
    except Exception:  # noqa: BLE001
        log.exception("Failed to fetch existing todo items for Amazon upsert")
        return {}
    result: dict[str, dict] = {}
    for item in items:
        summary = item.get("summary", "")
        if not summary.startswith(AMAZON_PREFIX):
            continue
        rest = summary[len(AMAZON_PREFIX):]
        order_id = rest.split(" ", 1)[0]
        result[order_id] = {"uid": item.get("uid"), "summary": summary}
    return result


def ha_add_amazon_item(info: dict) -> None:
    summary = f"{AMAZON_PREFIX}{info['order_id']} — {info['status']}"
    description_parts = [f"Subject: {info['subject']}"]
    if info.get("tracking_url"):
        description_parts.append(f"Track: {info['tracking_url']}")
    description_parts.append("Found by Parcel IMAP Scan add-on (Amazon order parser)")
    try:
        resp = requests.post(
            f"{HA_URL}/api/services/todo/add_item",
            headers=HA_HEADERS,
            json={
                "entity_id": TODO_ENTITY,
                "item": summary,
                "description": " | ".join(description_parts),
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Added Amazon order %s: %s", info["order_id"], info["status"])
    except Exception:  # noqa: BLE001
        log.exception("Failed to add Amazon todo item for order %s", info["order_id"])


def ha_update_amazon_item(uid: str, info: dict) -> None:
    summary = f"{AMAZON_PREFIX}{info['order_id']} — {info['status']}"
    description_parts = [f"Subject: {info['subject']}"]
    if info.get("tracking_url"):
        description_parts.append(f"Track: {info['tracking_url']}")
    description_parts.append("Found by Parcel IMAP Scan add-on (Amazon order parser)")
    try:
        resp = requests.post(
            f"{HA_URL}/api/services/todo/update_item",
            headers=HA_HEADERS,
            json={
                "entity_id": TODO_ENTITY,
                "item": uid,
                "rename": summary,
                "description": " | ".join(description_parts),
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Updated Amazon order %s -> %s", info["order_id"], info["status"])
    except Exception:  # noqa: BLE001
        log.exception("Failed to update Amazon todo item for order %s", info["order_id"])


_AMAZON_STATUS_ORDER = ["Ordered", "Shipped", "Out for delivery", "Delivered"]


def process_amazon(parsed: dict) -> int:
    """Extract Amazon order status from one parsed message and upsert it.

    Returns 1 if a new item was added or an existing one's status advanced,
    else 0 (nothing Amazon-related found, or status email arrived out of
    order / is a re-send of a status we've already recorded or passed).
    """
    info = extract_order_status(parsed["subject"], parsed["sender"], parsed.get("raw_html", ""))
    if not info:
        return 0
    existing = ha_get_amazon_items()
    current = existing.get(info["order_id"])
    if current is None:
        ha_add_amazon_item(info)
        return 1
    current_status = current["summary"].rsplit("—", 1)[-1].strip()
    try:
        if _AMAZON_STATUS_ORDER.index(info["status"]) <= _AMAZON_STATUS_ORDER.index(current_status):
            return 0  # not a forward move (duplicate/out-of-order email); leave as-is
    except ValueError:
        pass  # unrecognised current status text; update anyway
    ha_update_amazon_item(current["uid"], info)
    return 1


# ---------------------------------------------------------------------------
# On-demand backfill scan (blocking, run in an executor thread)
# ---------------------------------------------------------------------------

IMAP_SOCKET_TIMEOUT = 60  # seconds; imaplib has no default and will hang forever
FETCH_CHUNK_SIZE = 100  # messages per IMAP round-trip


def run_backfill_scan(days_back: int) -> dict:
    found = 0
    added = 0
    processed = 0
    # Without an explicit timeout, imaplib will block forever on a stalled or
    # throttled connection -- a single hung FETCH used to silently kill the
    # whole backfill with no error logged at all.
    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_SOCKET_TIMEOUT)
    try:
        conn.login(IMAP_EMAIL, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)
        since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK" or not data or not data[0]:
            log.warning("Backfill scan: search returned no messages (status=%s)", status)
            return {"found": 0, "added": 0}
        uids = [u.decode() if isinstance(u, bytes) else u for u in data[0].split()]
        total = len(uids)
        log.info("Backfill scan: scanning %d messages since %s", total, since)
        # Fetch in batches (one IMAP round-trip per chunk, not per message) --
        # with ~12k messages, one-at-a-time fetching was the main reason a
        # scan could take hours; batching cuts round-trips by ~100x.
        for start in range(0, total, FETCH_CHUNK_SIZE):
            chunk = uids[start : start + FETCH_CHUNK_SIZE]
            status, msg_data = conn.fetch(",".join(chunk), "(RFC822)")
            if status != "OK" or not msg_data:
                processed += len(chunk)
                continue
            for item in msg_data:
                if not (isinstance(item, tuple) and len(item) >= 2):
                    continue
                raw = item[1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                parsed = _parse_message(raw)
                matches = extract_tracking(parsed["subject"], parsed["sender"], parsed["body"])
                found += len(matches)
                if matches:
                    added += process_matches(parsed)
                added += process_amazon(parsed)
            processed += len(chunk)
            log.info("Backfill scan: %d/%d messages processed (found %d, added %d so far)",
                      processed, total, found, added)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    log.info("Backfill scan complete: found %d, added %d new", found, added)
    return {"found": found, "added": added}


# ---------------------------------------------------------------------------
# Live IMAP IDLE watch (async)
# ---------------------------------------------------------------------------

async def _get_highest_uid(client: "aioimaplib.IMAP4_SSL") -> int:
    # aioimaplib's uid() wrapper only supports FETCH/STORE/COPY/MOVE/EXPUNGE;
    # UID SEARCH has its own dedicated method. charset=None avoids a
    # "CHARSET utf-8" prefix some IMAP servers are picky about for plain
    # criteria like "ALL".
    res = await client.uid_search("ALL", charset=None)
    if res.result != "OK" or not res.lines or not res.lines[0]:
        return 0
    uids = res.lines[0].split()
    return int(uids[-1]) if uids else 0


async def _fetch_and_process_new(client: "aioimaplib.IMAP4_SSL", last_uid: int) -> int:
    res = await client.uid_search(f"UID {last_uid + 1}:*", charset=None)
    if res.result != "OK" or not res.lines or not res.lines[0]:
        return last_uid
    uids = sorted({int(u) for u in res.lines[0].split() if int(u) > last_uid})
    loop = asyncio.get_event_loop()
    for uid in uids:
        fres = await client.uid("fetch", str(uid), "(RFC822)")
        if fres.result != "OK":
            continue
        raw = None
        for line in fres.lines:
            if isinstance(line, (bytes, bytearray)) and len(line) > 100:
                raw = line
                break
        if raw is None:
            continue
        parsed = _parse_message(raw)
        added = await loop.run_in_executor(None, process_matches, parsed)
        added += await loop.run_in_executor(None, process_amazon, parsed)
        if added:
            log.info(
                "IDLE: new mail from %s matched and added %d item(s)",
                parsed["sender"],
                added,
            )
    return uids[-1] if uids else last_uid


async def idle_watch_loop() -> None:
    backoff = 5
    while True:
        try:
            client = aioimaplib.IMAP4_SSL(host=IMAP_SERVER, port=IMAP_PORT)
            await client.wait_hello_from_server()
            await client.login(IMAP_EMAIL, IMAP_PASSWORD)
            await client.select(IMAP_FOLDER)
            last_uid = await _get_highest_uid(client)
            log.info("IDLE watch started, baseline UID=%d", last_uid)
            backoff = 5
            while True:
                idle_task = await client.idle_start(timeout=300)
                try:
                    pushed = await asyncio.wait_for(client.wait_server_push(), timeout=290)
                except asyncio.TimeoutError:
                    pushed = None
                client.idle_done()
                await asyncio.wait_for(idle_task, timeout=15)
                if pushed and any(
                    b"EXISTS" in line for line in pushed if isinstance(line, (bytes, bytearray))
                ):
                    last_uid = await _fetch_and_process_new(client, last_uid)
        except Exception:  # noqa: BLE001
            log.exception("IDLE watch loop error, reconnecting in %ds", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


# ---------------------------------------------------------------------------
# HTTP trigger endpoint for on-demand backfill
# ---------------------------------------------------------------------------

_scan_status = {"state": "idle", "days_back": None, "result": None}


async def _run_backfill_background(days_back: int) -> None:
    _scan_status.update(state="running", days_back=days_back, result=None)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, run_backfill_scan, days_back)
        _scan_status.update(state="done", result=result)
    except Exception:  # noqa: BLE001
        log.exception("Backfill scan failed")
        _scan_status.update(state="error", result=None)


async def handle_scan(request: web.Request) -> web.Response:
    """Kick off a backfill scan in the background and return immediately.

    A full mailbox scan can take tens of minutes; this used to block the HTTP
    response for that whole time, which meant the scan died if the client
    (or its connection) went away before it finished. Now it just starts a
    background task and the caller polls /scan (GET) or the todo list for
    progress.
    """
    if _scan_status["state"] == "running":
        return web.json_response({"status": "already_running", **_scan_status}, status=409)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    days_back = int(body.get("days_back", DAYS_BACK_DEFAULT))
    asyncio.create_task(_run_backfill_background(days_back))
    return web.json_response({"status": "started", "days_back": days_back})


async def handle_scan_status(request: web.Request) -> web.Response:
    return web.json_response(_scan_status)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main() -> None:
    app = web.Application()
    app.add_routes(
        [
            web.post("/scan", handle_scan),
            web.get("/scan", handle_scan_status),
            web.get("/health", handle_health),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    log.info("HTTP trigger endpoint listening on :8099 (POST /scan {\"days_back\": N})")

    if IDLE_ENABLED:
        await idle_watch_loop()
    else:
        log.info("idle_enabled is false; running in on-demand-only mode")
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
