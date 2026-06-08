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

ARIA_INSTRUCTIONS = """You are Aria, a warm, professional female outbound sales development rep for Briskinfosec, a cybersecurity services company that is CREST accredited and CERT-In empanelled.

YOUR GOAL: qualify the prospect and book a 20-minute virtual discovery meeting with a senior consultant. You are driving this call, not waiting to be asked questions.

CONVERSATION RULES:
- Speak in short, natural, spoken sentences. One or two sentences per turn, maximum.
- Ask exactly ONE question at a time, and almost every turn should end with a question that moves toward booking the meeting.
- NEVER greet again after the opening. Do not say "Hello", "Hi there", or "How can I help you" after the first line. You called them; keep the conversation moving forward.
- NEVER repeat an answer you already gave. If you already said where the company is based or what it does, do not say it again; instead advance the conversation.
- Always steer back to the goal: understanding their security needs and booking the discovery meeting. After answering a question, immediately ask a relevant qualifying question.
- If the caller says only a filler word like "okay", "hmm", or "yeah", treat it as a cue to continue and ask your next qualifying question, not to re-explain.
- NEVER invent or guess technical specifics (methodologies, pricing, tool names, timelines). If asked something technical or detailed, say a senior consultant will cover it in the discovery meeting, and offer to book it.
- Be honest and never claim to be human if directly and explicitly asked whether you are a person; otherwise stay focused on the sales conversation.
- Keep momentum: if the caller is engaged, propose a specific next step ("Would you be open to a quick 20-minute call with one of our senior consultants this week?").

OUTPUT FORMAT: Output ONLY the exact words you would speak. Never output analysis, planning, numbered steps, lists, headings, markdown, asterisks, or any description of your own thinking."""

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
    # Barge-in state: while the bot speaks we keep a HIGH-threshold VAD
    # running; sustained caller speech sets interrupt and stops playback.
    interrupt = False
    barge_ms = 0.0
    pending_barge = bytearray()

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

    async def _send_pcm(pcm8k):
        # Stream 8k PCM to the caller in chunks; bail out early if the
        # caller barges in (interrupt flag set by the media handler).
        nonlocal interrupt
        CHUNK = 3200
        for i in range(0, len(pcm8k), CHUNK):
            if interrupt:
                return False
            chunk = pcm8k[i:i + CHUNK]
            if len(chunk) % 320 != 0:
                chunk += b"\x00" * (320 - (len(chunk) % 320))
            await ws.send_text(json.dumps({
                "event": "media",
                "stream_sid": stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode("ascii")},
            }))
            await asyncio.sleep(0.09)
        return True

    async def say(text):
        nonlocal bot_busy, mute_until, audio_buf, speaking, silence_ms
        nonlocal interrupt, barge_ms, pending_barge
        bot_busy = True
        interrupt = False
        barge_ms = 0.0
        pending_barge = bytearray()
        sentences = _split_sentences(text)
        # Pipeline: synthesize sentence N+1 while sentence N is being played,
        # so audio starts after only the first sentence's TTS latency.
        next_task = asyncio.create_task(sarvam_tts(sentences[0])) if sentences else None
        for idx in range(len(sentences)):
            out = await next_task if next_task else b""
            if idx + 1 < len(sentences):
                next_task = asyncio.create_task(sarvam_tts(sentences[idx + 1]))
            else:
                next_task = None
            if out:
                finished = await _send_pcm(out)
                if not finished:
                    break  # caller barged in; stop talking
        if next_task and not next_task.done():
            next_task.cancel()
        if interrupt:
            logger.info("BARGE-IN: caller interrupted Aria")
            audio_buf = bytearray(pending_barge)
            speaking = len(audio_buf) > 0
            silence_ms = 0
            mute_until = 0.0
        else:
            audio_buf = bytearray()
            speaking = False
            silence_ms = 0
            mute_until = time.monotonic() + 0.6
        interrupt = False
        barge_ms = 0.0
        pending_barge = bytearray()
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
                chunk = base64.b64decode(ev["media"]["payload"])
                # While Aria is speaking, listen for a real interruption.
                # Use a HIGH gate (well above her echo) and require sustained
                # speech so echo or a stray "mm-hmm" does not cut her off.
                if bot_busy:
                    rms = audioop.rms(chunk, 2)
                    frame_ms = len(chunk) / 2 / EXOTEL_RATE * 1000
                    barge_gate = max(noise_floor * 3.5, 900)
                    if rms > barge_gate:
                        barge_ms += frame_ms
                        pending_barge.extend(chunk)
                    else:
                        barge_ms = max(0.0, barge_ms - frame_ms)
                        if barge_ms <= 0:
                            pending_barge = bytearray()
                    if barge_ms >= 320:
                        interrupt = True
                    continue
                # Cooldown after Aria stops, to avoid trailing echo.
                if time.monotonic() < mute_until:
                    continue
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
                end_by_silence = speaking and silence_ms >= 700 and buf_ms >= 300
                end_by_length = speaking and buf_ms >= 4000
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
                    # Drop bare filler / echo so Aria does not re-greet or derail.
                    _norm = text.lower().strip(" .,!?")
                    _filler = {"hello", "hi", "hey", "okay", "ok", "yeah", "yep",
                               "hmm", "mhm", "uh", "um", "thank you", "thanks",
                               "thank you very much"}
                    if _norm in _filler:
                        logger.info("SKIP filler/echo: %s", text)
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
