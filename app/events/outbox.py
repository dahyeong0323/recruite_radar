from __future__ import annotations
from datetime import datetime
import httpx
from app.events.health import read_json
from app.vault.frontmatter import atomic_write_text
import json


def load_outbox(path):
    return read_json(path, {'deliveries': {}})


def save_outbox(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def classify_delivery_error(error):
    """Only failures known to precede sending or explicit API rejection are retriable."""
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return 'failed'
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
        return 'failed'
    if isinstance(error, httpx.HTTPStatusError) and 400 <= error.response.status_code < 500:
        return 'rejected'
    return 'delivery_unknown'


async def send_event(client, chat_id, text, markup):
    # Reuse TelegramClient's configured HTTP client. Keep exception type internally
    # to distinguish definite failures; never log the token-bearing request URL.
    response = await client.client.post(client.base_url + '/sendMessage', json={
        'chat_id': chat_id, 'text': text, 'reply_markup': markup, 'disable_web_page_preview': False})
    response.raise_for_status()
    data = response.json()
    if not data.get('ok'):
        code = data.get('error_code', 500)
        synthetic = httpx.Response(code, request=response.request)
        raise httpx.HTTPStatusError('Telegram rejected event message', request=response.request, response=synthetic)
    message_id = data.get('result', {}).get('message_id')
    if message_id is None: raise ValueError('Telegram receipt missing')
    return message_id
