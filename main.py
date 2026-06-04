import os
import json
import asyncio
import base64
import struct
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_tcrxdvzl_c3m4CuhdXiuE3vL1rhKnCKY8')
SARVAM_WS_URL = 'wss://api.sarvam.ai/v1/realtime'
SYSTEM_PROMPT = 'You are Aria, Outbound SDR for Briskinfosec. Qualify cybersecurity needs and book a 20-min discovery meeting. Be professional and consultative.'

# G.711 mu-law (ulaw) decode/encode - pure Python (audioop removed in Python 3.13+)
ULAW_BIAS = 33
ULAW_CLIP = 32635
EXP_TABLE = [0, 132, 396, 924, 1980, 4092, 8316, 16764]


def ulaw2lin(data: bytes) -> bytes:
    """Convert u-law bytes to 16-bit signed PCM bytes."""
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
    """Convert 16-bit signed PCM bytes to u-law bytes."""
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


@app.get('/')
async def health():
    return {'status': 'Sarvam-Exotel Bridge running'}


@app.websocket('/sarvam-ws')
async def voicebot_websocket(websocket: WebSocket):
    await websocket.accept()
    print('Exotel Voicebot connected')

    sarvam_headers = {
        'Authorization': f'Bearer {SARVAM_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        async with websockets.connect(SARVAM_WS_URL, extra_headers=sarvam_headers) as sarvam_ws:
            print('Connected to Sarvam Realtime API')

            await sarvam_ws.send(json.dumps({
                'type': 'session.update',
                'session': {
                    'instructions': SYSTEM_PROMPT,
                    'voice': 'priya',
                    'input_audio_format': 'pcm16',
                    'output_audio_format': 'pcm16',
                    'input_audio_transcription': {'model': 'saarika:v2'},
                    'turn_detection': {
                        'type': 'server_vad',
                        'threshold': 0.5,
                        'silence_duration_ms': 500
                    }
                }
            }))

            async def exotel_to_sarvam():
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
                        except asyncio.TimeoutError:
                            break
                        if not data:
                            continue
                        pcm16 = ulaw2lin(data)
                        audio_b64 = base64.b64encode(pcm16).decode('utf-8')
                        await sarvam_ws.send(json.dumps({
                            'type': 'input_audio_buffer.append',
                            'audio': audio_b64
                        }))
                except Exception as e:
                    print(f'exotel_to_sarvam ended: {e}')

            async def sarvam_to_exotel():
                try:
                    async for msg in sarvam_ws:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            continue
                        if data.get('type') == 'response.audio.delta':
                            audio_b64 = data.get('delta', '')
                            if audio_b64:
                                pcm16 = base64.b64decode(audio_b64)
                                mulaw = lin2ulaw(pcm16)
                                await websocket.send_bytes(mulaw)
                        elif data.get('type') == 'error':
                            print(f'Sarvam error: {data}')
                except Exception as e:
                    print(f'sarvam_to_exotel ended: {e}')

            await asyncio.gather(exotel_to_sarvam(), sarvam_to_exotel())

    except WebSocketDisconnect:
        print('Exotel disconnected')
    except Exception as e:
        print(f'Bridge error: {e}')
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
