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


@app.api_route('/exotel-webhook', methods=['GET', 'POST'])
async def exotel_webhook(request: Request):
    # Exotel Passthru sends GET with query params; support both GET and POST
    if request.method == 'GET':
        params = dict(request.query_params)
    else:
        form = await request.form()
        params = dict(form)
    call_sid = params.get('CallSid', 'unknown')
    from_number = params.get('From', 'unknown')
    host = request.headers.get('host', 'localhost')
    ws_url = f'wss://{host}/sarvam-ws/{call_sid}'
    xml = f'<?xml version="1.0"?><Response><Connect><Stream url="{ws_url}"><Parameter name="caller" value="{from_number}"/></Stream></Connect></Response>'
    return Response(content=xml, media_type='application/xml')


@app.websocket('/sarvam-ws/{call_sid}')
async def sarvam_websocket(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    caller = 'unknown'
    try:
        headers = {
            'Authorization': f'Bearer {SARVAM_API_KEY}',
            'Content-Type': 'application/json'
        }
        async with websockets.connect(SARVAM_WS_URL, extra_headers=headers) as sarvam_ws:
            init_msg = {
                'type': 'session.update',
                'session': {
                    'instructions': SYSTEM_PROMPT,
                    'voice': 'priya',
                    'input_audio_format': 'pcm16',
                    'output_audio_format': 'pcm16',
                    'turn_detection': {'type': 'server_vad'}
                }
            }
            await sarvam_ws.send(json.dumps(init_msg))

            async def exotel_to_sarvam():
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get('event') == 'media':
                        audio_b64 = data['media']['payload']
                        await sarvam_ws.send(json.dumps({
                            'type': 'input_audio_buffer.append',
                            'audio': audio_b64
                        }))
                    elif data.get('event') == 'start':
                        caller = data.get('start', {}).get('customParameters', {}).get('caller', 'unknown')

            async def sarvam_to_exotel():
                async for msg in sarvam_ws:
                    data = json.loads(msg)
                    if data.get('type') == 'response.audio.delta':
                        audio_b64 = data.get('delta', '')
                        if audio_b64:
                            await websocket.send_text(json.dumps({
                                'event': 'media',
                                'media': {'payload': audio_b64}
                            }))

            await asyncio.gather(exotel_to_sarvam(), sarvam_to_exotel())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f'WebSocket error for {call_sid}: {e}')
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
