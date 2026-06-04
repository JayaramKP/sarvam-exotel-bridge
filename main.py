import os
import json
import asyncio
import base64
import audioop
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI()

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_tcrxdvzl_c3m4CuhdXiuE3vL1rhKnCKY8')
SARVAM_WS_URL = 'wss://api.sarvam.ai/v1/realtime'
SYSTEM_PROMPT = 'You are Aria, Outbound SDR for Briskinfosec. Qualify cybersecurity needs and book a 20-min discovery meeting. Be professional and consultative.'


@app.get('/')
async def health():
    return {'status': 'Sarvam-Exotel Bridge running'}


@app.websocket('/sarvam-ws')
async def voicebot_websocket(websocket: WebSocket):
    """
    Exotel Voicebot applet connects here with bidirectional audio.
    Exotel sends raw mulaw-8 (8kHz, 8-bit) audio as binary frames.
    We convert to PCM16 for Sarvam and back to mulaw for Exotel.
    """
    await websocket.accept()
    print('Exotel Voicebot connected')

    sarvam_headers = {
        'Authorization': f'Bearer {SARVAM_API_KEY}',
        'Content-Type': 'application/json'
    }

    try:
        async with websockets.connect(SARVAM_WS_URL, extra_headers=sarvam_headers) as sarvam_ws:
            print('Connected to Sarvam Realtime API')

            # Initialize Sarvam session
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
                """Receive mulaw audio from Exotel, convert to PCM16, send to Sarvam"""
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=30.0)
                        except asyncio.TimeoutError:
                            break
                        if not data:
                            continue
                        # Convert mulaw-8 (8kHz) to PCM16 (8kHz)
                        pcm16 = audioop.ulaw2lin(data, 2)
                        # Base64 encode for Sarvam
                        audio_b64 = base64.b64encode(pcm16).decode('utf-8')
                        await sarvam_ws.send(json.dumps({
                            'type': 'input_audio_buffer.append',
                            'audio': audio_b64
                        }))
                except (WebSocketDisconnect, Exception) as e:
                    print(f'exotel_to_sarvam ended: {e}')

            async def sarvam_to_exotel():
                """Receive PCM16 audio from Sarvam, convert to mulaw, send to Exotel"""
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
                                # Convert PCM16 back to mulaw-8
                                mulaw = audioop.lin2ulaw(pcm16, 2)
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
        print('Bridge session ended')
