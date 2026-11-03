"""Morpho Radar Telegram bot: real HTTP webhook for Render Free Web Service.

Commands:
  /start  - introduction
  /check <market_id> <wallet_address> - estimate Morpho Blue Base position health

This bot does NOT continuously monitor positions or send unsolicited alerts.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

TOKEN = os.environ.get('TELEGRAM_TOKEN', '').strip()
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
PORT = int(os.environ.get('PORT', '10000'))
TELEGRAM_API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
MORPHO_API = 'https://api.morpho.org'
CHAIN_ID = 8453  # Base mainnet
MARKET_RE = re.compile(r'^0x[a-fA-F0-9]{64}$')
ADDRESS_RE = re.compile(r'^0x[a-fA-F0-9]{40}$')
# Stable secret derived from the environment-only token. Never print it.
WEBHOOK_SECRET = hashlib.sha256(('morpho-radar-webhook-v1:' + TOKEN).encode()).hexdigest() if TOKEN else ''

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('morpho_telegram')


def morpho_get(path, *, allow_404=False):
    """Read one Morpho REST record. REST records are wrapped in a data object."""
    response = requests.get(f'{MORPHO_API}{path}', headers={'Accept': 'application/json'}, timeout=12)
    if response.status_code == 404 and allow_404:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise ValueError('Unexpected Morpho response')
    return payload['data']


def estimate_health(market_id, wallet_address):
    """Use Morpho's published conservative integer calculation, not an invented HF field."""
    selector = f'{CHAIN_ID}:{market_id}'
    base = f'/v0/blue/markets/{selector}'
    position = morpho_get(f'{base}/users/{wallet_address}/position', allow_404=True)
    if position is None or int(position.get('borrow_shares', '0')) == 0:
        return None

    market = morpho_get(base)
    state = morpho_get(f'{base}/state')
    oracle_address = market['oracle_address']
    if not ADDRESS_RE.fullmatch(oracle_address):
        raise ValueError('Invalid market oracle address')
    oracle = morpho_get(f'/v0/oracles/{CHAIN_ID}:{oracle_address}/state')
    if oracle.get('price') is None:
        raise ValueError('Oracle price unavailable')

    shares = int(position['borrow_shares'])
    # Morpho's VIRTUAL_ASSETS=1 and VIRTUAL_SHARES=1_000_000; round debt upward.
    num = shares * (int(state['total_borrow_assets']) + 1)
    den = int(state['total_borrow_shares']) + 1_000_000
    debt = (num + den - 1) // den
    if debt == 0:
        return None
    collateral_value = int(position['collateral_assets']) * int(oracle['price']) // 10**36
    max_debt = collateral_value * int(market['lltv_wad']) // 10**18
    health = Decimal(max_debt) / Decimal(debt)
    return health


def send_message(chat_id, text):
    result = requests.post(
        f'{TELEGRAM_API}/sendMessage',
        json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True},
        timeout=12,
    )
    result.raise_for_status()
    if not result.json().get('ok'):
        raise ValueError('Telegram rejected sendMessage')


def command_reply(message):
    text = (message.get('text') or '').strip()
    parts = text.split()
    if not parts:
        return None
    command = parts[0].split('@', 1)[0].lower()
    if command in ('/start', '/help'):
        return (
            '🛡️ Morpho Radar — Base\n\n'
            'Check a Morpho Blue position with:\n'
            '/check <market_id> <wallet_address>\n\n'
            'The market ID must be 0x plus 64 hexadecimal characters. '
            'The wallet address must be 0x plus 40 hexadecimal characters.\n\n'
            'This is a manual, approximate risk check—not automatic alerts.\n'
            'Explore: https://rarefrequencyworks.com/morpho-radar'
        )
    if command != '/check':
        return None
    if len(parts) != 3 or not MARKET_RE.fullmatch(parts[1]) or not ADDRESS_RE.fullmatch(parts[2]):
        return (
            'Usage: /check <market_id> <wallet_address>\n\n'
            'Use a full Base Morpho Blue market ID (0x + 64 hex characters) '
            'and a full wallet address (0x + 40 hex characters).'
        )

    try:
        health = estimate_health(parts[1], parts[2])
    except (requests.RequestException, ValueError, KeyError, ArithmeticError, TypeError) as error:
        logger.warning('Morpho check failed: %s', type(error).__name__)
        return '⚠️ Morpho data was unavailable. Please try again later; do not rely on this bot for urgent risk decisions.'

    if health is None:
        return 'ℹ️ No active borrowing debt found in this Base market, or the position is unavailable.'
    if health <= Decimal('1'):
        status = '🔴 At or past estimated liquidation threshold'
    elif health <= Decimal('1.05'):
        status = '🔴 Very high risk'
    elif health <= Decimal('1.20'):
        status = '🟠 Elevated risk'
    else:
        status = '🟢 Above warning level'
    return (
        f'📊 Morpho Blue — Base\n'
        f'{status}\n'
        f'Estimated health factor: {health:.3f}\n'
        f'Liquidation threshold: 1.000\n\n'
        'Based on separate indexed REST reads; interest, oracle changes and transaction timing '
        'can change the true on-chain risk. This is not financial advice or a 24/7 alert.\n\n'
        'https://rarefrequencyworks.com/morpho-radar'
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/health'):
            return self.respond(404, {'error': 'not_found'})
        self.respond(200, {
            'service': 'Morpho Radar Telegram Bot',
            'ok': True,
            'token_configured': bool(TOKEN),
            'webhook_url_configured': bool(RENDER_URL),
            'mode': 'telegram_webhook',
        })

    def do_POST(self):
        if self.path != '/telegram':
            return self.respond(404, {'error': 'not_found'})
        secret = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not TOKEN or not hmac.compare_digest(secret, WEBHOOK_SECRET):
            return self.respond(403, {'error': 'forbidden'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                return self.respond(413, {'error': 'invalid_size'})
            update = json.loads(self.rfile.read(size))
            message = update.get('message') or {}
            reply = command_reply(message)
            if reply and message.get('chat', {}).get('id') is not None:
                send_message(message['chat']['id'], reply)
        except (ValueError, requests.RequestException, json.JSONDecodeError, TypeError) as error:
            logger.warning('Webhook processing error: %s', type(error).__name__)
            return self.respond(500, {'ok': False})
        self.respond(200, {'ok': True})

    def respond(self, code, body):
        encoded = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        # Keep access logs minimal (never log the Telegram token or full payload).
        logger.info('%s', format % args)


def register_webhook():
    if not TOKEN:
        logger.warning('TELEGRAM_TOKEN missing; /health works, Telegram commands will not.')
        return
    if not RENDER_URL:
        logger.warning('RENDER_EXTERNAL_URL missing; webhook registration skipped (local mode).')
        return
    response = requests.post(
        f'{TELEGRAM_API}/setWebhook',
        json={'url': f'{RENDER_URL}/telegram', 'secret_token': WEBHOOK_SECRET,
              'allowed_updates': ['message'], 'max_connections': 2},
        timeout=20,
    )
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram webhook setup rejected by Telegram')
    logger.info('Telegram webhook registered (token kept secret).')


if __name__ == '__main__':
    with ThreadingHTTPServer(('0.0.0.0', PORT), Handler) as server:
        logger.info('HTTP service listening on port %d.', PORT)
        try:
            register_webhook()
        except (requests.RequestException, ValueError, RuntimeError) as error:
            logger.error('Webhook setup failed: %s', type(error).__name__)
            logger.error('Confirm TELEGRAM_TOKEN is valid in Render Environment, then redeploy.')
        server.serve_forever()
