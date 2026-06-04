 import os
import json
import asyncio
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

app = FastAPI()

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_tcrxdvzl_c3m4CuhdXiuE3vL1rhKnCKY8')
SARVAM_WS_URL = 'wss://api.sarvam.ai/v1/realtime'
SYSTEM_PROMPT = 'You are Aria, Outbound SDR for Briskinfosec. Qualify cybersecurity needs and book a 20-min discovery meeting. Be professional and consultative.'


@app.get('/')
async def health():
    return {'status': 'Sarvam-Exotel Bridge running'}


@app.post('/exotel-webhook')
async def exotel_webhook(request: Request):
    form = await request.form()
    call_sid = form.get('CallSid', 'unknown')
    from_number = form.get('From', 'unknown')
    host = request.headers.get('host', 'localhost')
    ws_url = f'wss://{host}/sarvam-ws/{call_sid}'
    xml = f'<?xml version="1.0"?><Response><Connect><Stream url="{ws_url}"><Parameter name="caller" value="{from_number}"/></Stream></Connect></Response>'
    return Response(content=xml, media_type='application/xml')


@app.websocket('/sarvam-ws/{call_sid}')
async def sarvam_ws_handler(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    headers = {'Authorization': f'Bearer {SARVAM_API_KEY}'}
    cfg = {'type': 'session.update', 'session': {'instructions': SYSTEM_PROMPT, 'voice': 'priya', 'input_audio_format': 'pcm16', 'output_audio_format': 'pcm16', 'turn_detection': {'type': 'server_vad'}}}
    try:
        async with websockets.connect(SARVAM_WS_URL, extra_headers=headers) as sw:
            await sw.send(json.dumps(cfg))
            async def e2s():
                async for msg in websocket.iter_text():
                    d = json.loads(msg)
                    if d.get('event') == 'media':
                        await sw.send(json.dumps({'type': 'input_audio_buffer.append', 'audio': d['media']['payload']}))
            async def s2e():
                async for msg in sw:
                    d = json.loads(msg)
                    if d.get('type') == 'response.audio.delta':
                        await websocket.send_text(json.dumps({'event': 'media', 'media': {'payload': d.get('delta', '')}}))
            await asyncio.gather(e2s(), s2e())
    except Exception as e:
        print(f'Error: {e}')
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
