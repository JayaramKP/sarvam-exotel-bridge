import asyncio
import audioop
import base64
import io
import json
import logging
import os
import wave

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aria-bridge")

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")
SARVAM_BASE = "https://api.sarvam.ai"

EXOTEL_RATE = 8000
STT_RATE = 16000

ARIA_INSTRUCTIONS = """
You are Aria, an Outbound Sales Development Representative (SDR) for Briskinfosec,
a cybersecurity services company that holds CREST and CERT-In credentials.

# YOUR GOAL
Cold call IT and Security leaders, qualify their cybersecurity environment
(VAPT, compliance, infrastructure), position Briskinfosec's CREST / CERT-In
credentials, and book a 20-minute virtual discovery meeting.

# PERSONALITY
Professional, consultative, and highly respectful of the person's time. You sound
like a knowledgeable peer in the security industry, NOT a pushy salesperson.
Warm, calm, and concise. You speak naturally, as in a real phone conversation.

# CALL FLOW (follow this order)
1. OPENING - Greet, introduce yourself and Briskinfosec briefly, and ASK
   PERMISSION to continue ("Did I catch you at an okay time for a quick minute?").
2. CONTEXT - Reference a relevant trigger (e.g. something from their LinkedIn /
   their company / their industry) to explain why you are reaching out.
3. PROBING - Ask ONE question at a time to understand their environment. Topics:
   ISO 27001, SOC 2, RBI compliance, how often they run VAPT, current security
   posture and infrastructure. Listen, then follow up naturally.
4. POSITIONING - Where relevant, position Briskinfosec's CREST and CERT-In
   accreditations as differentiators.
5. CLOSE - Aim to book a 20-minute virtual discovery meeting with a senior
   consultant. Offer a couple of specific time options.

# HARD RULES
- Ask only ONE probing question at a time. Never stack questions.
- NEVER guess or invent technical answers. If asked something technical you are
  unsure about, say you'll loop in a senior consultant who can answer precisely.
- Be respectful of their time. If they are busy or not interested, thank them
  politely and offer to follow up another time. Do not be pushy.
- Keep responses short and conversational - this is a live phone call.
- End calls professionally and warmly.
"""

GREETING = (
    "Hi, this is Aria calling from Briskinfosec, a cybersecurity services company. "
    "Did I catch you at an okay time for a quick minute?"
)

app = FastAPI()


@app.get("/")
async def health():
    return {"status": "ok", "service": "aria-exotel-bridge"}


async def sarvam_stt(pcm16k: bytes) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STT_RATE)
        wf.writeframes(pcm16k)
    buf.seek(0)
    files = {"file": ("audio.wav", buf, "audio/wav")}
    data = {"model": "saaras:v3", "mode": "translate", "language_code": "unknown"}
    headers = {"api-subscription-key": SARVAM_API_KEY}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{SARVAM_BASE}/speech-to-text", headers=headers, data=data, files=files)
    if r.status_code != 200:
        logger.error("STT error %s: %s", r.status_code, r.text[:300])
        return ""
    return (r.json() or {}).get("transcript", "").strip()


async def sarvam_llm(history: list) -> str:
    messages = [{"role": "system", "content": ARIA_INSTRUCTIONS}] + history
    payload = {"model": "sarvam-105b", "messages": messages, "temperature": 0.5, "max_tokens": 200}
    headers = {"Authorization": f"Bearer {SARVAM_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{SARVAM_BASE}/v1/chat/completions", headers=headers, json=payload)
    if r.status_code != 200:
        logger.error("LLM error %s: %s", r.status_code, r.text[:300])
        return "I'm sorry, could you say that again?"
    return r.json()["choices"][0]["message"]["content"].strip()


async def sarvam_tts(text: str) -> bytes:
    payload = {
        "text": text[:2400],
        "target_language_code": "en-IN",
        "model": "bulbul:v3",
        "speaker": "priya",
        "speech_sample_rate": EXOTEL_RATE,
    }
    headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{SARVAM_BASE}/text-to-speech", headers=headers, json=payload)
    if r.status_code != 200:
        logger.error("TTS error %s: %s", r.status_code, r.text[:300])
        return b""
    b64 = r.json()["audios"][0]
    wav_bytes = base64.b64decode(b64)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())


async def send_audio(ws: WebSocket, stream_sid: str, pcm8k: bytes):
    CHUNK = 3200
    for i in range(0, len(pcm8k), CHUNK):
        chunk = pcm8k[i:i + CHUNK]
        if len(chunk) % 320 != 0:
            chunk += b"\x00" * (320 - (len(chunk) % 320))
        await ws.send_text(json.dumps({
            "event": "media",
            "stream_sid": stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode("ascii")},
        }))
        await asyncio.sleep(0.09)


@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    logger.info("WS connected")
    stream_sid = None
    history = []
    audio_buf = bytearray()
    silence_ms = 0
    speaking = False
    try:
        while True:
            raw = await ws.receive_text()
            ev = json.loads(raw)
            etype = ev.get("event")

            if etype == "connected":
                logger.info("connected")

            elif etype == "start":
                stream_sid = ev["start"]["stream_sid"]
                logger.info("start stream_sid=%s", stream_sid)
                pcm = await sarvam_tts(GREETING)
                history.append({"role": "assistant", "content": GREETING})
                if pcm:
                    await send_audio(ws, stream_sid, pcm)

            elif etype == "media":
                chunk = base64.b64decode(ev["media"]["payload"])
                rms = audioop.rms(chunk, 2)
                if rms > 500:
                    speaking = True
                    silence_ms = 0
                    audio_buf.extend(chunk)
                elif speaking:
                    audio_buf.extend(chunk)
                    silence_ms += len(chunk) / 2 / EXOTEL_RATE * 1000
                    if silence_ms > 700 and len(audio_buf) > EXOTEL_RATE:
                        utter = bytes(audio_buf)
                        audio_buf = bytearray()
                        speaking = False
                        silence_ms = 0
                        pcm16k, _ = audioop.ratecv(utter, 2, 1, EXOTEL_RATE, STT_RATE, None)
                        text = await sarvam_stt(pcm16k)
                        if text:
                            logger.info("USER: %s", text)
                            history.append({"role": "user", "content": text})
                            reply = await sarvam_llm(history)
                            logger.info("ARIA: %s", reply)
                            history.append({"role": "assistant", "content": reply})
                            out = await sarvam_tts(reply)
                            if out:
                                await send_audio(ws, stream_sid, out)

            elif etype == "stop":
                logger.info("stop")
                break
    except WebSocketDisconnect:
        logger.info("WS disconnected")
    except Exception as e:
        logger.exception("WS error: %s", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
