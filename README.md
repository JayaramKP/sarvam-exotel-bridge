# Aria Voice SDR — sarvam-exotel-bridge

Aria is an outbound voice agent for **Briskinfosec** that conducts cold sales calls over the phone. A telephony provider streams the caller audio to a custom Python bridge, which converts speech to text, generates a reply with an LLM, converts that reply back to speech, and streams it to the caller — in a continuous loop for the whole call.

---

## 1. System Overview

The system is built from four independent services: **Exotel** (telephony), a **FastAPI Python bridge** (the orchestrator, the only custom code), **Sarvam AI** (speech + language models), and **Railway** (hosting). Everything except the bridge is configuration of third-party platforms.

## 2. Architecture (data flow)

```
  ┌──────────┐        PSTN / phone call         ┌───────────────────┐
  │  Caller  │◄────────────────────────────────►│  Exotel telephony  │
  │ (phone)  │     09513886363 (ExoPhone)        │  Flow ID 1261547   │
  └──────────┘                                   └─────────┬─────────┘
                                                           │
                          Exotel "Voicebot" applet opens a WebSocket and
                          streams call audio (8 kHz PCM, base64 JSON frames)
                                                           │
                                                           ▼
         wss://sarvam-exotel-bridge-production.up.railway.app/media
                                                           │
  ┌────────────────────────────────────────────────────────────────────────┐
  │              FastAPI Bridge  (main.py, Python 3.11.9)                    │
  │                  hosted on Railway, port 8080                           │
  │                                                                         │
  │   1. Receive audio frames  ─►  VAD (detect end of caller speech)        │
  │   2. Buffer speech         ─►  POST /speech-to-text   (Sarvam STT)      │
  │   3. Append to history     ─►  POST /v1/chat/completions (Sarvam LLM)   │
  │   4. Reply text            ─►  POST /text-to-speech   (Sarvam TTS)      │
  │   5. Stream audio back to Exotel over the same WebSocket                │
  └────────────────────────────────────────────────────────────────────────┘
                                                           │
                                                           ▼
                                                  ┌────────────────┐
                                                  │   Sarvam AI     │
                                                  │  api.sarvam.ai  │
                                                  │ STT · LLM · TTS │
                                                  └────────────────┘
```

Steps 1–5 repeat for every conversational turn until the caller hangs up.

## 3. Components

| Layer | Service | Responsibility |
|---|---|---|
| Telephony | Exotel | Owns the phone number, answers/places calls, runs a Flow with one Voicebot applet that streams audio to our WebSocket. |
| Orchestration | FastAPI bridge (`main.py`) | VAD, sample-rate conversion, calls the 3 Sarvam endpoints, keeps conversation history, echo suppression. |
| AI | Sarvam AI | STT (`saaras:v3`), LLM (`sarvam-105b`), TTS (`bulbul:v3` / voice `priya`). |
| Hosting | Railway | Runs the bridge as an always-on container built from GitHub. Chosen over Render to avoid cold-start delay. |

## 4. The Conversation Loop

On the WebSocket `start` event the bridge greets the caller (TTS) and begins listening. For each inbound audio frame it runs voice-activity detection, buffering speech. When it detects end-of-turn (see §5.4) it: sends the buffered audio to STT, appends the transcript to history, sends history to the LLM, gets a short reply, appends it to history, converts the reply to speech, and streams it back. Then it resets and waits for the next turn.

## 5. Configuration Reference

### 5.1 Exotel
- **Flow:** "Briskinfosec SDR Outbound - Sarvam AI" — Flow ID `1261547`
- **Test number (ExoPhone):** `09513886363`
- **Applet:** Voicebot
- **WebSocket URL:** `wss://sarvam-exotel-bridge-production.up.railway.app/media`
- Record / Encrypt DTMF: off

### 5.2 Sarvam AI models (set in `main.py`)

**Speech-to-text** — `POST /speech-to-text`
- model `saaras:v3`, `mode: transcribe`, `language_code: en-IN`
- audio sent as 16 kHz mono WAV
- (was `translate`/`unknown` which mangled proper nouns; `transcribe`+`en-IN` fixed it)

**LLM** — `POST /v1/chat/completions`
- model `sarvam-105b`, `temperature: 0.4`, `max_tokens: 80` (one short sentence)
- `reasoning_effort: None` (disables slow reasoning mode for fast replies)

**Text-to-speech** — `POST /text-to-speech`
- model `bulbul:v3`, `speaker: priya`, `target_language_code: en-IN`, `speech_sample_rate: 8000`

### 5.3 Audio pipeline
- Exotel wire rate 8000 Hz; STT rate 16000 Hz (bridge up-samples before STT).
- Outbound audio chunked at 3200 bytes, padded to multiples of 320, 0.09s pause between chunks.

### 5.4 Voice-activity detection / endpointing (current)
- A **dynamic noise floor** continuously estimates ambient line noise.
- Speech detected when loudness exceeds `max(noise_floor * 2.2, 350)`.
- A turn ends after **1.2 s** of relative silence, **or** is force-flushed once the buffer reaches **7 s** (hard ceiling that prevents long hangs).
- **Echo suppression:** while Aria speaks plus a 0.6 s cooldown, inbound audio is dropped and the buffer flushed, so she never transcribes her own voice.

### 5.5 Railway hosting
- Project **meticulous-manifestation** / Service **sarvam-exotel-bridge**, US West, 1 replica.
- Public domain `sarvam-exotel-bridge-production.up.railway.app` -> port 8080.
- **Custom start command:** `python main.py` — runs uvicorn with `ws_ping_interval=None` / `ws_ping_timeout=None` to prevent WebSocket keepalive disconnects.
- **Env vars:** `SARVAM_API_KEY` (secret), `MISE_PYTHON_GITHUB_ATTESTATIONS=false` (Railway build-platform workaround).
- Python pinned to **3.11.9** via `runtime.txt`.
- Auto-deploys on every push to GitHub `main`.

### 5.6 Source control
- Repo `JayaramKP/sarvam-exotel-bridge`, branch `main`. Files: `main.py`, `requirements.txt`, `runtime.txt`.
- Backup branch `backup-livekit-agent` preserves the original LiveKit agent for recovery.

## 6. Setup From Scratch

1. Create the GitHub repo with `main.py`, `requirements.txt` (fastapi, uvicorn, httpx), `runtime.txt` (`python-3.11.9`).
2. In Railway, create a project from the repo; add env vars `SARVAM_API_KEY` and `MISE_PYTHON_GITHUB_ATTESTATIONS=false`; set start command `python main.py`; generate a public domain on port 8080.
3. Confirm deploy logs show **Application startup complete**.
4. In Exotel, build a Flow with one Voicebot applet and paste `wss://<railway-domain>/media`; save.
5. Place a test call to the ExoPhone to verify the full loop.

## 7. Change History

- Switched STT to `transcribe`/`en-IN` and capped replies (accuracy + latency).
- Added echo suppression (mute-while-speaking + cooldown).
- Migrated hosting Render -> Railway to remove cold-start delay.
- Set `python main.py` start command and generated public domain.
- Rewrote VAD with dynamic noise floor + 1.2 s silence + 7 s hard ceiling to remove long response delays.

## 8. Known Limitations

- ~2–5 s TTS latency per turn (Sarvam renders full audio before playback); would need streaming TTS to remove.
- No barge-in (cannot interrupt Aria mid-sentence).
- `audioop` is deprecated in Python 3.13 — Python pinned to 3.11.9 for now.
- AI-disclosure behaviour is a product decision to be confirmed by the owner.

---
*This document reflects the deployed configuration. Update it alongside any change to models, hosting, or the Exotel flow.*
