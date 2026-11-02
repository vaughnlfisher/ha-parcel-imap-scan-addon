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
the HA REST API, authenticated with a long-lived access token the user
generates and pastes into the add-on's own configuration (never entered by
Claude).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import Message
from pathlib import Path

import aioimaplib
import requests
from aiohttp import web

from extract import extract_tracking

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

IMAP_EMAIL = OPTIONS["imap_email"]
IMAP_PASSWORD = OPTIONS["imap_password"]
IMAP_SERVER = OPTIONS.get("imap_server", "imap.gmail.com")
IMAP_PORT = int(OPTIONS.get("imap_port", 993))
IMAP_FOLDER = OPTIONS.get("imap_folder", "INBOX")
HA_URL = str(OPTIONS.get("ha_url", "http://supervisor/core")).rstrip("/")
HA_TOKEN = OPTIONS["ha_token"]
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


def _extract_body(msg: Message) -> str:
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
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
        return "\n".join(plain_parts + html_parts)
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return _html_to_text(decoded)
    return decoded


def _parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    subject = _decode_header_value(msg.get("Subject"))
    sender = _decode_header_value(msg.get("From"))
    try:
        body = _extract_body(msg)
    except Exception:  # noqa: BLE001
        body = ""
    return {"subject": subject, "sender": sender, "body": body}


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
# On-demand backfill scan (blocking, run in an executor thread)
# ---------------------------------------------------------------------------

def run_backfill_scan(days_back: int) -> dict:
    found = 0
    added = 0
    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    try:
        conn.login(IMAP_EMAIL, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)
        since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK" or not data or not data[0]:
            log.warning("Backfill scan: search returned no messages (status=%s)", status)
            return {"found": 0, "added": 0}
        uids = data[0].split()
        log.info("Backfill scan: scanning %d messages since %s", len(uids), since)
        for uid in uids:
            status, msg_data = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            parsed = _parse_message(raw)
            matches = extract_tracking(parsed["subject"], parsed["sender"], parsed["body"])
            found += len(matches)
            if matches:
                added += process_matches(parsed)
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
    res = await client.uid("search", "ALL")
    if res.result != "OK" or not res.lines or not res.lines[0]:
        return 0
    uids = res.lines[0].split()
    return int(uids[-1]) if uids else 0


async def _fetch_and_process_new(client: "aioimaplib.IMAP4_SSL", last_uid: int) -> int:
    res = await client.uid("search", f"UID {last_uid + 1}:*")
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

async def handle_scan(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    days_back = int(body.get("days_back", DAYS_BACK_DEFAULT))
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_backfill_scan, days_back)
    return web.json_response(result)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main() -> None:
    app = web.Application()
    app.add_routes([web.post("/scan", handle_scan), web.get("/health", handle_health)])
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
