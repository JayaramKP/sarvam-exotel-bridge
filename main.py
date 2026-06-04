import os
import json
import asyncio
import base64
import struct
import time
import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_tcrxdvzl_c3m4CuhdXiuE3vL1rhKnCKY8')

# Sarvam Samvaad session URL endpoint (dashboard proxy accepts api-subscription-key header)
SARVAM_SESSION_URL = (
    'https://dashboard.sarvam.ai/api/proxy/orgs/sarvam-internal.ai'
    '/workspaces/sarvam-internal-ai-9191fd'
    '/apps/api-dashboa-4adc0b2d-9e5c/url'
)

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

# G.711 mu-law decode/encode - pure Python (audioop removed in Python 3.13+)
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
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            SARVAM_SESSION_URL,
            params={'interaction_type': 'call', 'sample_rate': str(sample_rate)},
            headers={'api-subscription-key': SARVAM_API_KEY}
        )
        resp.raise_for_status()
        data = resp.json()
        ws_url = data['url']
        # Append required query params
        sep = '&' if '?' in ws_url else '?'
        ws_url += f'{sep}sample_rate={sample_rate}&interaction_type=call'
        print(f'Got Sarvam WS URL: {ws_url[:80]}...')
        return ws_url

@app.get('/')
async def health():
    return {'status': 'Sarvam-Exotel Bridge running'}

@app.websocket('/sarvam-ws')
async def voicebot_websocket(websocket: WebSocket):
    await websocket.accept()
    print('Exotel Voicebot connected')

    try:
        # Step 1: Get a fresh Sarvam session URL
        sarvam_ws_url = await get_sarvam_ws_url(sample_rate=8000)
    except Exception as e:
        print(f'Failed to get Sarvam session URL: {e}')
        await websocket.close()
        return

    try:
        async with websockets.connect(sarvam_ws_url, ping_interval=20, ping_timeout=10) as sarvam_ws:
            print('Connected to Sarvam Realtime API')

            # Step 2: Send interaction_start message with Aria persona
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
            print('Sent interaction_start to Sarvam')

            async def exotel_to_sarvam():
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
                        except asyncio.TimeoutError:
                            print('Exotel audio timeout - ending session')
                            break
                        if not data:
                            continue
                        # Convert mu-law 8kHz -> PCM16 8kHz
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
                    print('Exotel disconnected')
                except Exception as e:
                    print(f'exotel_to_sarvam error: {e}')

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
                            print('Sarvam interaction connected!')
                        elif msg_type == 'server.action.interaction_end':
                            print('Sarvam ended interaction')
                            break
                        elif msg_type == 'server.event.user_interrupt':
                            print('User interrupt detected')
                except Exception as e:
                    print(f'sarvam_to_exotel error: {e}')

            await asyncio.gather(exotel_to_sarvam(), sarvam_to_exotel())

    except Exception as e:
        print(f'Bridge error: {e}')
    finally:
        print('Session ended')
        try:
            await websocket.close()
        except Exception:
            pass
