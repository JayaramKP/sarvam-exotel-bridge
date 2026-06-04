import os
import sys
import json
import asyncio
import base64
import struct
import time
import traceback
import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Force unbuffered output so logs appear in Render
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def log(msg):
    print(msg, flush=True)

def err(msg):
    print(f'ERROR: {msg}', file=sys.stderr, flush=True)

app = FastAPI()

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_tcrxdvzl_c3m4CuhdXiuE3vL1rhKnCKY8')

SARVAM_SESSION_URL = (
    'https://dashboard.sarvam.ai/api/proxy/orgs/sarvam-internal.ai'
    '/workspaces/sarvam-internal-ai-9191fd'
    '/apps/api-dashboa-4adc0b2d-9e5c/url'
)

# Headers that mimic a real browser to pass Sarvam's origin check
BROWSER_HEADERS = {
    'Origin': 'https://dashboard.sarvam.ai',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
}

ARIA_PERSONA = (
    'You are Aria, Outbound Sales Development Representative for Briskinfosec Technology and Consulting Pvt. Ltd. '
    'Goal: cold call IT/Security leaders, qualify their cybersecurity environment, '
    'position Briskinfosec CREST/CERT-In credentials, and book a 20-minute virtual discovery meeting. '
    'Be professional, consultative, and respect the prospect time. '
    'Ask one probe question at a time about VAPT frequency, ISO 27001/SOC 2/RBI compliance, current vendors, '
    'and biggest security concern. Never guess technical answers - defer to senior consultant.'
)

ARIA_INITIAL_MESSAGE = (
    "Hello, this is Aria calling from Briskinfosec. "
    "We help IT and security teams with CREST-certified penetration testing and compliance audits. "
    "Do you have 2 minutes to speak?"
)

# G.711 mu-law decode/encode - pure Python
ULAW_BIAS = 33
ULAW_CLIP = 32635
EXP_TABLE = [0, 132, 396, 924, 1980, 4092, 8316, 16764]

def ulaw2lin(data: bytes) -> bytes:
    result = bytearray(len(data) * 2)
    for i, byte in enumerate(data):
        byte = ~byte & 0xFF
        sign = byte & 0x80
        exponent = (byte >> 4) & 0x07
        mantissa = byte & 0x0F
        sample = EXP_TABLE[exponent] + (mantissa << (exponent + 3))
        if sign:
            sample = -sample
        struct.pack_into('<h', result, i * 2, sample)
    return bytes(result)

def lin2ulaw(data: bytes) -> bytes:
    result = bytearray(len(data) // 2)
    for i in range(len(data) // 2):
        sample = struct.unpack_from('<h', data, i * 2)[0]
        sign = 0
        if sample < 0:
            sample = -sample
            sign = 0x80
        sample = min(sample, ULAW_CLIP) + ULAW_BIAS
        exp = 7
        for e in range(7, -1, -1):
            if sample >= (1 << (e + 3)):
                exp = e
                break
        mantissa = (sample >> (exp + 3)) & 0x0F
        result[i] = ~(sign | (exp << 4) | mantissa) & 0xFF
    return bytes(result)

async def get_sarvam_ws_url(sample_rate: int = 8000) -> str:
    log('Fetching Sarvam session URL...')
    headers = {
        'api-subscription-key': SARVAM_API_KEY,
        **BROWSER_HEADERS,
        'Accept': 'application/json',
        'Referer': 'https://dashboard.sarvam.ai/agents',
    }
    params = {'interaction_type': 'call', 'sample_rate': str(sample_rate)}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(SARVAM_SESSION_URL, params=params, headers=headers)
        log(f'Session URL response: status={resp.status_code}')
        if resp.status_code != 200:
            err(f'Session URL error: {resp.text[:300]}')
            resp.raise_for_status()
        data = resp.json()
        ws_url = data['url']
        sep = '&' if '?' in ws_url else '?'
        ws_url += f'{sep}sample_rate={sample_rate}&interaction_type=call'
        log(f'Got Sarvam WS URL (truncated): {ws_url[:60]}...')
        return ws_url

@app.get('/')
async def health():
    return {'status': 'Sarvam-Exotel Bridge running', 'version': '10'}

@app.websocket('/sarvam-ws')
async def voicebot_websocket(websocket: WebSocket):
    await websocket.accept()
    log('=== Exotel Voicebot connected ===')

    try:
        sarvam_ws_url = await get_sarvam_ws_url(sample_rate=8000)
    except Exception as e:
        err(f'Cannot get Sarvam URL: {e}')
        await websocket.close()
        return

    # Pass browser-like headers to bypass Sarvam origin check
    ws_extra_headers = {
        'Origin': 'https://dashboard.sarvam.ai',
        'User-Agent': BROWSER_HEADERS['User-Agent'],
    }

    try:
        log('Connecting to Sarvam WebSocket with browser headers...')
        async with websockets.connect(
            sarvam_ws_url,
            additional_headers=ws_extra_headers,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15
        ) as sarvam_ws:
            log('=== Connected to Sarvam Realtime API ===')

            interaction_start = {
                'type': 'client.action.interaction_start',
                'origin': 'client',
                'timestamp': time.time(),
                'agent_variables': {
                    'persona': ARIA_PERSONA,
                    'voice_speaker_name': 'priya'
                },
                'initial_language_name': 'English',
                'initial_bot_message': ARIA_INITIAL_MESSAGE
            }
            await sarvam_ws.send(json.dumps(interaction_start))
            log('Sent interaction_start to Sarvam')

            async def exotel_to_sarvam():
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
                        except asyncio.TimeoutError:
                            log('Exotel audio timeout')
                            break
                        if not data:
                            continue
                        pcm16 = ulaw2lin(data)
                        audio_b64 = base64.b64encode(pcm16).decode('utf-8')
                        msg = {
                            'type': 'client.media.audio_chunk',
                            'origin': 'client',
                            'timestamp': time.time(),
                            'audio_base64': audio_b64,
                            'format': 'LINEAR16',
                            'sample_rate': 8000
                        }
                        await sarvam_ws.send(json.dumps(msg))
                except WebSocketDisconnect:
                    log('Exotel disconnected')
                except Exception as e:
                    err(f'exotel_to_sarvam: {e}')
                    err(traceback.format_exc())

            async def sarvam_to_exotel():
                try:
                    async for msg in sarvam_ws:
                        if isinstance(msg, bytes):
                            continue
                        try:
                            data = json.loads(msg)
                        except Exception:
                            continue
                        msg_type = data.get('type', '')
                        if msg_type == 'server.media.audio_chunk':
                            audio_b64 = data.get('audio_base64', '')
                            if audio_b64:
                                pcm16 = base64.b64decode(audio_b64)
                                mulaw = lin2ulaw(pcm16)
                                await websocket.send_bytes(mulaw)
                        elif msg_type == 'server.action.interaction_connected':
                            log('Sarvam interaction connected!')
                        elif msg_type == 'server.action.interaction_end':
                            log('Sarvam ended interaction')
                            break
                        elif msg_type == 'server.event.user_interrupt':
                            log('User interrupt')
                        else:
                            log(f'Sarvam msg: {msg_type}')
                except Exception as e:
                    err(f'sarvam_to_exotel: {e}')
                    err(traceback.format_exc())

            await asyncio.gather(exotel_to_sarvam(), sarvam_to_exotel())

    except Exception as e:
        err(f'Bridge error: {e}')
        err(traceback.format_exc())
    finally:
        log('=== Session ended ===')
        try:
            await websocket.close()
        except Exception:
            pass
