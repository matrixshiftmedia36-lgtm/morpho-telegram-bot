"""Morpho Radar Telegram bot: Base Morpho Blue position checks + pilot alerts.

Runs on Render's Free Web Service. Telegram updates are received by webhook;
cron-job.org calls a separately authenticated monitor trigger. Subscriptions are
stored in Upstash Redis REST, never in Render's ephemeral filesystem.

Risk estimates come from four non-atomic indexed Morpho REST reads and are NOT
real-time or guaranteed liquidation protection. No funds or wallet keys needed.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
MORPHO_API = "https://api.morpho.org"
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MONITOR_SECRET = os.environ.get("MONITOR_SECRET", "")
CHAIN_ID = 8453  # Base mainnet
MARKET_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
WEBHOOK_SECRET = hashlib.sha256(("morpho-radar-webhook-v1:" + TOKEN).encode()).hexdigest() if TOKEN else ""
STORE_ENABLED = bool(UPSTASH_URL and UPSTASH_TOKEN)
ALERTS_ENABLED = STORE_ENABLED and len(MONITOR_SECRET) >= 24
GLOBAL_SET = "radar:v1:watches"
RUN_LOCK = "radar:v1:monitor:lock"
RUN_CURSOR = "radar:v1:monitor:cursor"
RUN_RESULT = "radar:v1:monitor:last"
MAX_WATCHES_PER_USER = 3
MAX_WATCHES_TOTAL = 30  # safety cap for free hosting / Morpho public API
MAX_WATCHES_PER_RUN = 10
DEFAULT_THRESHOLD = Decimal("1.20")
LOG = logging.getLogger("morpho_radar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class StorageError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redis(*args):
    """Upstash Redis REST command; no token is ever included in a URL or log."""
    if not STORE_ENABLED:
        raise StorageError("Persistent storage is not configured")
    try:
        response = requests.post(
            UPSTASH_URL,
            headers={"Authorization": "Bearer " + UPSTASH_TOKEN},
            json=list(args),
            timeout=6,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or "error" in result or "result" not in result:
            raise StorageError("Redis returned an error")
        return result["result"]
    except (requests.RequestException, ValueError) as exc:
        raise StorageError("Persistent storage unavailable") from exc


def market_get(path, allow_404=False):
    response = requests.get(
        f"{MORPHO_API}{path}",
        headers={"Accept": "application/json"}, timeout=8,
    )
    if allow_404 and response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Unexpected Morpho response")
    return payload["data"]


def estimate_health(market_id, wallet_address):
    """Conservative estimated health factor, following Morpho's REST documentation."""
    selector = f"{CHAIN_ID}:{market_id}"
    market_base = f"/v0/blue/markets/{selector}"
    position = market_get(f"{market_base}/users/{wallet_address}/position", allow_404=True)
    if position is None or int(position.get("borrow_shares", "0")) <= 0:
        return None
    market = market_get(market_base)
    state = market_get(f"{market_base}/state")
    oracle_address = market["oracle_address"]
    if not ADDRESS_RE.fullmatch(oracle_address):
        raise ValueError("Invalid oracle address")
    oracle = market_get(f"/v0/oracles/{CHAIN_ID}:{oracle_address}/state")
    if oracle.get("price") is None:
        raise ValueError("Oracle price unavailable")
    shares = int(position["borrow_shares"])
    numerator = shares * (int(state["total_borrow_assets"]) + 1)
    denominator = int(state["total_borrow_shares"]) + 1_000_000
    debt = (numerator + denominator - 1) // denominator
    if debt <= 0:
        return None
    collateral_value = int(position["collateral_assets"]) * int(oracle["price"]) // 10**36
    max_debt = collateral_value * int(market["lltv_wad"]) // 10**18
    return Decimal(max_debt) / Decimal(debt)


def send_telegram(chat_id, text):
    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=10,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise ValueError("Telegram rejected message")


def watch_id(chat_id, market_id, address):
    material = f"{chat_id}:{market_id.lower()}:{address.lower()}"
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def watch_key(key):
    return "radar:v1:watch:" + key


def chat_key(chat_id):
    return "radar:v1:chat:" + str(chat_id)


def get_watch(wid):
    raw = redis("GET", watch_key(wid))
    return json.loads(raw) if isinstance(raw, str) else None


def save_watch(wid, record):
    redis("SET", watch_key(wid), json.dumps(record, separators=(",", ":")))


def delete_watch(wid, chat_id):
    # Global index removed first; orphan record is harmless if a request fails.
    redis("SREM", GLOBAL_SET, wid)
    redis("SREM", chat_key(chat_id), wid)
    redis("DEL", watch_key(wid))


def severity(hf, threshold):
    if hf is None:
        return "NO_DEBT"
    if hf <= Decimal("1.00"):
        return "LIQUIDATION_ZONE"
    if hf <= Decimal("1.05"):
        return "CRITICAL"
    if hf <= threshold:
        return "WARNING"
    return "SAFE"


RANK = {"SAFE": 0, "WARNING": 1, "CRITICAL": 2, "LIQUIDATION_ZONE": 3}


def risk_message(sev, hf, market_id, address, *, simulated=False):
    label = {"SAFE": "🟢 Above warning level", "WARNING": "🟠 Risk warning",
             "CRITICAL": "🔴 Critical risk", "LIQUIDATION_ZONE": "🚨 At/past estimated liquidation threshold"}[sev]
    prefix = "🧪 SIMULATED TEST — NOT A REAL POSITION\n\n" if simulated else "🛡️ Morpho Radar automatic alert\n\n"
    return (
        prefix + label + f"\nEstimated health factor: {hf:.3f}\n"
        f"Base market: {market_id[:10]}…\nWallet: {address[:8]}…{address[-4:]}\n\n"
        "Indexed values may lag on-chain conditions. Verify directly onchain before acting. "
        "This alert does not guarantee liquidation protection."
    )


def parse_identity(args):
    if len(args) < 2 or not MARKET_RE.fullmatch(args[0]) or not ADDRESS_RE.fullmatch(args[1]):
        return None
    return args[0].lower(), args[1].lower()


def format_health(hf):
    if hf is None:
        return "No active borrowing debt found in this market."
    sev = severity(hf, DEFAULT_THRESHOLD)
    return (
        f"Estimated Base Morpho Blue health factor: {hf:.3f}\n"
        f"Status: {sev.replace('_', ' ')}\n"
        "Liquidation threshold: 1.000\n\n"
        "Risk is an estimate using separate, indexed REST reads—not a live on-chain guarantee."
    )


def reply(message):
    text = (message.get("text") or "").strip()
    chunks = text.split()
    if not chunks:
        return None
    command = chunks[0].split("@", 1)[0].lower()
    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return None
    # Only private chats are allowed to create or view alert subscriptions.
    private_chat = message.get("chat", {}).get("type") == "private"

    if command in ("/start", "/help"):
        return (
            "🛡️ Morpho Radar — Base\n\n"
            "/check <market_id> <wallet_address> — manual health estimate\n"
            "/watch <market_id> <wallet_address> [threshold] — subscribe to alerts\n"
            "/watches — list your saved watches\n"
            "/unwatch <watch_id> — stop one watch\n"
            "/status — monitoring setup/status\n"
            "/demoalert — test a clearly simulated Telegram alert\n\n"
            "Example threshold: 1.20 (default). Full market IDs and wallet addresses required.\n"
            "Alerts are best-effort 10-minute checks, not real-time liquidation protection."
        )
    if command == "/demoalert":
        return risk_message("WARNING", Decimal("1.15"), "0x" + "1"*64,
                            "0x" + "2"*40, simulated=True)
    if command == "/check":
        identity = parse_identity(chunks[1:])
        if not identity or len(chunks) != 3:
            return "Usage: /check <full_market_id> <full_wallet_address>"
        try:
            return format_health(estimate_health(*identity))
        except (requests.RequestException, ValueError, ArithmeticError, KeyError, TypeError):
            return "⚠️ Morpho risk data is unavailable. Please check again later or directly onchain."
    if command == "/status":
        if not ALERTS_ENABLED:
            return "ℹ️ Telegram works, but automatic alerts are not yet configured. Storage and a monitor secret are required."
        try:
            last = redis("GET", RUN_RESULT)
            latest = json.loads(last).get("finished_at", "never") if last else "never"
            return ("🛡️ Monitoring is configured.\n"
                    f"Last completed scheduled scan: {latest}\n"
                    "Schedule: configured separately at cron-job.org; configuration alone does not prove that jobs are running.\n"
                    "Alerts are best-effort, not real-time.")
        except (StorageError, ValueError):
            return "⚠️ Monitoring storage is temporarily unavailable."
    if command not in ("/watch", "/watches", "/unwatch"):
        return None
    if not private_chat:
        return "To manage alerts, please send this command in a private chat with the bot."
    if not ALERTS_ENABLED:
        return "Automatic alerts are not configured yet. The bot still supports /start, /check and /demoalert."

    try:
        if command == "/watches":
            ids = redis("SMEMBERS", chat_key(chat_id)) or []
            if not ids:
                return "You have no active watches. Use /watch <market_id> <wallet_address> [threshold]."
            lines = ["Your active Morpho Radar watches:"]
            for wid in sorted(ids):
                record = get_watch(wid)
                if record and str(record.get("chat_id")) == str(chat_id):
                    lines.append(f"{wid} • {record['market_id'][:10]}… • {record['wallet_address'][:10]}… • HF ≤ {record['threshold']}")
            return "\n".join(lines) + "\n\nRemove with /unwatch <watch_id>"

        if command == "/unwatch":
            if len(chunks) != 2 or not re.fullmatch(r"[a-f0-9]{20}", chunks[1]):
                return "Usage: /unwatch <watch_id> (find IDs with /watches)"
            wid = chunks[1]
            record = get_watch(wid)
            if not record or str(record.get("chat_id")) != str(chat_id):
                return "Watch not found in your chat."
            delete_watch(wid, chat_id)
            return f"✅ Watch {wid} removed. You will no longer receive alerts for that watch."

        identity = parse_identity(chunks[1:3])
        if identity is None or len(chunks) not in (3, 4):
            return "Usage: /watch <full_market_id> <full_wallet_address> [threshold]\nExample threshold: 1.20"
        threshold = DEFAULT_THRESHOLD
        if len(chunks) == 4:
            try:
                threshold = Decimal(chunks[3])
            except InvalidOperation:
                return "Threshold must be a number, for example 1.20."
            if not threshold.is_finite() or not Decimal("1.01") <= threshold <= Decimal("3.00"):
                return "Threshold must be between 1.01 and 3.00."
        market_id, address = identity
        wid = watch_id(chat_id, market_id, address)
        current = get_watch(wid)
        if current:
            return f"Already watching this position (ID {wid}). To change its threshold, /unwatch it and register again."
        if int(redis("SCARD", chat_key(chat_id)) or 0) >= MAX_WATCHES_PER_USER:
            return f"Pilot limit: {MAX_WATCHES_PER_USER} watches per chat. Remove one with /unwatch first."
        if int(redis("SCARD", GLOBAL_SET) or 0) >= MAX_WATCHES_TOTAL:
            return "The free pilot watchlist is full. Please try again later."
        # Don't accept fake, inactive, or unverifiable loans as real risk watches.
        try:
            hf = estimate_health(market_id, address)
        except (requests.RequestException, ValueError, ArithmeticError, KeyError, TypeError):
            return "⚠️ Couldn't verify this borrowing position with Morpho. Watch was not saved."
        if hf is None:
            return "No active Morpho Blue borrowing debt found in that market. Watch not created."
        state = severity(hf, threshold)
        record = {
            "chat_id": str(chat_id), "market_id": market_id, "wallet_address": address,
            "threshold": str(threshold), "last_state": state, "last_health": str(hf),
            "registered_at": utc_now(), "checked_at": utc_now(),
        }
        save_watch(wid, record)
        redis("SADD", GLOBAL_SET, wid)
        redis("SADD", chat_key(chat_id), wid)
        caution = "\n⚠️ Position is ALREADY at/under your threshold." if state != "SAFE" else ""
        return (
            f"✅ Watch saved: {wid}\n"
            f"Estimated health factor: {hf:.3f}\n"
            f"Warning threshold: {threshold}\n"
            "Automatic messages are sent when the risk level worsens during a scheduled scan."
            + caution + "\n\nScheduled monitoring must be enabled separately; delivery is not guaranteed."
        )
    except StorageError:
        return "⚠️ Watch storage is temporarily unavailable. Please try again later."


def run_monitor():
    """Best-effort monitor. Runs after fast 202 response from the cron endpoint."""
    checked = alerted = failures = closed = 0
    try:
        ids = sorted(redis("SMEMBERS", GLOBAL_SET) or [])
        if ids:
            cursor = int(redis("GET", RUN_CURSOR) or 0) % len(ids)
            selected = [ids[(cursor + i) % len(ids)] for i in range(min(MAX_WATCHES_PER_RUN, len(ids)))]
            redis("SET", RUN_CURSOR, str((cursor + len(selected)) % len(ids)))
            for wid in selected:
                try:
                    record = get_watch(wid)
                    if not record:
                        redis("SREM", GLOBAL_SET, wid)
                        continue
                    chat_id = record["chat_id"]
                    # Avoid sending a stale alert if the user has unsubscribed.
                    if wid not in (redis("SMEMBERS", chat_key(chat_id)) or []):
                        redis("SREM", GLOBAL_SET, wid)
                        continue
                    hf = estimate_health(record["market_id"], record["wallet_address"])
                    checked += 1
                    if hf is None:
                        # Do not auto-remove the watch: a temporary index discrepancy
                        # must not delete a user's monitoring subscription.
                        record["last_health"] = None
                        record["checked_at"] = utc_now()
                        save_watch(wid, record)
                        continue
                    previous = record.get("last_state", "SAFE")
                    current = severity(hf, Decimal(record["threshold"]))
                    record["last_health"] = str(hf)
                    record["checked_at"] = utc_now()
                    if RANK.get(current, 0) > RANK.get(previous, 0):
                        # Send BEFORE saving state: if Telegram fails, next run retries.
                        send_telegram(chat_id, risk_message(current, hf, record["market_id"], record["wallet_address"]))
                        alerted += 1
                    record["last_state"] = current
                    save_watch(wid, record)
                except (StorageError, requests.RequestException, ValueError,
                        ArithmeticError, KeyError, TypeError) as exc:
                    LOG.warning("Watch check failed, type=%s", type(exc).__name__)
                    failures += 1
        redis("SET", RUN_RESULT, json.dumps({
            "finished_at": utc_now(), "checked": checked,
            "alerts_sent": alerted, "failures": failures,
            "total_watches": len(ids),
        }))
        LOG.info("Monitor finished: checked=%s alerts=%s failures=%s", checked, alerted, failures)
    except Exception as exc:
        LOG.error("Monitor failed, error_type=%s", type(exc).__name__)
        try:
            redis("SET", RUN_RESULT, json.dumps({"finished_at": utc_now(), "error": "monitor_failed"}))
        except StorageError:
            pass


def schedule_monitor():
    """Acquire a short lock so repeated cron pings can't run overlapping scans."""
    lock = redis("SET", RUN_LOCK, secrets.token_hex(8), "NX", "EX", 420)
    if lock != "OK":
        return False
    threading.Thread(target=run_monitor, daemon=True, name="morpho-monitor").start()
    return True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            return self.respond(200, {
                "service": "Morpho Radar Telegram Bot", "ok": True,
                "token_configured": bool(TOKEN),
                "webhook_url_configured": bool(RENDER_URL),
                "alerts_configured": bool(ALERTS_ENABLED),
                "mode": "telegram_webhook",
            })
        if self.path == "/monitor":
            # Never accept a secret in the URL/query string.
            secret = self.headers.get("X-Monitor-Secret", "")
            if not ALERTS_ENABLED:
                return self.respond(503, {"error": "monitor_not_configured"})
            if not hmac.compare_digest(secret, MONITOR_SECRET):
                return self.respond(403, {"error": "forbidden"})
            try:
                started = schedule_monitor()
            except StorageError:
                return self.respond(503, {"error": "storage_unavailable"})
            return self.respond(202, {"accepted": started, "status": "scheduled" if started else "recent_scan"})
        return self.respond(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/telegram":
            return self.respond(404, {"error": "not_found"})
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not TOKEN or not hmac.compare_digest(secret, WEBHOOK_SECRET):
            return self.respond(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.respond(413, {"error": "invalid_size"})
            update = json.loads(self.rfile.read(length))
            message = update.get("message") or {}
            result = reply(message)
            if result and message.get("chat", {}).get("id") is not None:
                send_telegram(message["chat"]["id"], result)
        except (requests.RequestException, ValueError, TypeError, StorageError) as exc:
            LOG.warning("Webhook processing error_type=%s", type(exc).__name__)
            return self.respond(500, {"error": "update_failed"})
        return self.respond(200, {"ok": True})

    def respond(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.info("HTTP: %s", fmt % args)


def register_webhook():
    if not TOKEN:
        LOG.warning("TELEGRAM_TOKEN missing: Telegram commands disabled")
        return
    if not RENDER_URL:
        LOG.warning("RENDER_EXTERNAL_URL missing: webhook registration skipped")
        return
    response = requests.post(
        f"{TELEGRAM_API}/setWebhook",
        json={"url": f"{RENDER_URL}/telegram", "secret_token": WEBHOOK_SECRET,
              "allowed_updates": ["message"], "max_connections": 2},
        timeout=20,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise ValueError("Telegram rejected webhook registration")
    LOG.info("Telegram webhook registered")


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as server:
        LOG.info("HTTP server listening on port %s", PORT)
        try:
            register_webhook()
        except (requests.RequestException, ValueError) as exc:
            LOG.error("Telegram webhook registration failed, type=%s", type(exc).__name__)
        server.serve_forever()