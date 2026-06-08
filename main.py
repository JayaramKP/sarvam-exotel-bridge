import asyncio
import audioop
import base64
import io
import json
import logging
import os
import time
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

ARIA_INSTRUCTIONS = """You are Aria, a warm, professional woman making an outbound phone sales call for Briskinfosec, a cybersecurity services company with CREST and CERT-In credentials. You are talking live with an IT or Security leader.

Your job: briefly understand their security situation, mention how Briskinfosec can help, and book a 20-minute discovery call with a senior consultant.

About Briskinfosec (use only this, do not invent other product names):
- Services: penetration testing (VAPT), web, mobile, network and cloud security testing, source code review, red teaming, and compliance support such as ISO 27001, SOC 2 and PCI DSS.
- Credentials: CREST accredited and CERT-In empanelled.

How to behave:
- Reply with ONLY one short, natural spoken sentence per turn. Keep it under 25 words. Sound human and warm.
- Respond directly to what the person just said. Ask only ONE question at a time, then wait for their answer.
- Do not repeat yourself. If you already asked something, move on.
- Gently move toward booking a short 20-minute call when the moment is right.
- If the caller mentions a product or name you do not recognise, do NOT pretend to know it; politely ask them to clarify what they mean.
- Never invent prices, product names, or technical claims. For exact pricing or deep technical detail, offer to bring in a senior consultant.
- If they are busy or not interested, thank them warmly and offer to follow up.

Never output analysis, planning, numbered steps, lists, headings, markdown, asterisks, or any description of your own thinking. Output only the exact words you would speak."""

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
    data = {"model": "saaras:v3", "mode": "transcribe", "language_code": "en-IN"}
    headers = {"api-subscription-key": SARVAM_API_KEY}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{SARVAM_BASE}/speech-to-text", headers=headers, data=data, files=files)
    if r.status_code != 200:
        logger.error("STT error %s: %s", r.status_code, r.text[:300])
        return ""
    return (r.json() or {}).get("transcript", "").strip()


async def sarvam_llm(history):
    messages = [{"role": "system", "content": ARIA_INSTRUCTIONS}] + history
    payload = {
        "model": "sarvam-105b",
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 80,
        "reasoning_effort": None,
    }
    headers = {"Authorization": f"Bearer {SARVAM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{SARVAM_BASE}/v1/chat/completions", headers=headers, json=payload)
    except Exception as e:
        logger.error("LLM request failed: %s", e)
        return "Sorry, could you say that again?"
    if r.status_code != 200:
        logger.error("LLM error %s: %s", r.status_code, r.text[:300])
        return "Sorry, could you say that again?"
    try:
        msg = r.json()["choices"][0]["message"]
    except Exception as e:
        logger.error("LLM parse error: %s | %s", e, r.text[:300])
        return "Sorry, could you say that again?"
    reply = (msg.get("content") or "").strip()
    if not reply:
        reply = "Sorry, I missed that. Could you repeat?"
    return reply


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
    # Guard: while the bot is talking (and a short cooldown after), ignore
    # inbound audio so the bot does not transcribe its own echoed voice.
    bot_busy = False
    mute_until = 0.0
    noise_floor = 200.0  # adaptive ambient RMS estimate

    import re as _re
    def _split_sentences(t):
        parts = _re.split(r"(?<=[.!?])\s+", t.strip())
        out = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # merge very short fragments into previous to avoid choppy TTS
            if out and len(p) < 12:
                out[-1] = out[-1] + " " + p
            else:
                out.append(p)
        return out or [t.strip()]

    async def say(text):
        nonlocal bot_busy, mute_until, audio_buf, speaking, silence_ms
        bot_busy = True
        sentences = _split_sentences(text)
        # Pipeline: synthesize sentence N+1 while sentence N is being played,
        # so audio starts after only the first sentence's TTS latency.
        next_task = asyncio.create_task(sarvam_tts(sentences[0])) if sentences else None
        for idx in range(len(sentences)):
            out = await next_task if next_task else b""
            # kick off TTS for the following sentence before we start playing this one
            if idx + 1 < len(sentences):
                next_task = asyncio.create_task(sarvam_tts(sentences[idx + 1]))
            else:
                next_task = None
            if out:
                await send_audio(ws, stream_sid, out)
        # flush any audio captured during playback (echo) and mute briefly
        audio_buf = bytearray()
        speaking = False
        silence_ms = 0
        mute_until = time.monotonic() + 0.6
        bot_busy = False

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
                history.append({"role": "assistant", "content": GREETING})
                await say(GREETING)

            elif etype == "media":
                # Drop inbound audio while bot is speaking or during cooldown
                if bot_busy or time.monotonic() < mute_until:
                    continue
                chunk = base64.b64decode(ev["media"]["payload"])
                rms = audioop.rms(chunk, 2)
                frame_ms = len(chunk) / 2 / EXOTEL_RATE * 1000
                # Dynamic speech gate: speech is clearly louder than ambient.
                speech_gate = max(noise_floor * 2.2, 350)
                if rms > speech_gate:
                    speaking = True
                    silence_ms = 0
                    audio_buf.extend(chunk)
                elif speaking:
                    audio_buf.extend(chunk)
                    silence_ms += frame_ms
                else:
                    noise_floor = 0.95 * noise_floor + 0.05 * rms
                buf_ms = len(audio_buf) / 2 / EXOTEL_RATE * 1000
                end_by_silence = speaking and silence_ms >= 1200 and buf_ms >= 400
                end_by_length = speaking and buf_ms >= 7000
                if end_by_silence or end_by_length:
                    utter = bytes(audio_buf)
                    audio_buf = bytearray()
                    speaking = False
                    silence_ms = 0
                    pcm16k, _ = audioop.ratecv(utter, 2, 1, EXOTEL_RATE, STT_RATE, None)
                    text = await sarvam_stt(pcm16k)
                    text = text.strip()
                    if len(text) < 2:
                        continue
                    logger.info("USER: %s", text)
                    history.append({"role": "user", "content": text})
                    reply = await sarvam_llm(history)
                    logger.info("ARIA: %s", reply)
                    history.append({"role": "assistant", "content": reply})
                    await say(reply)

            elif etype == "stop":
                logger.info("stop")
                break
    except WebSocketDisconnect:
        logger.info("WS disconnected")
    except Exception as e:
        logger.exception("WS error: %s", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        ws_ping_interval=None,
        ws_ping_timeout=None,
)
