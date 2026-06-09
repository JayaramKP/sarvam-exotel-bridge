# Aria - LiveKit Streaming Voice Agent

Aria is an outbound SDR voice agent for Briskinfosec, rebuilt on the
**LiveKit Agents** streaming platform. This replaces the older REST bridge
(still on the `main` branch) which had a hard latency floor caused by the
STT -> LLM -> TTS round-trip per turn. LiveKit streams audio in real time,
runs voice-activity detection and turn detection continuously, and supports
true barge-in, removing the dead-air and 2-4s lag.

Pipeline: Sarvam STT + Sarvam LLM + Sarvam TTS (bulbul:v3, en-IN, 8kHz),
Silero VAD, and the multilingual turn detector.

---

## Files

- `agent.py` - the agent (entrypoint, Aria persona, pipeline wiring).
- `requirements.txt` - Python dependencies.
- `.env.example` - template for secrets. Copy to `.env.local` and fill in.

---

## Does this depend on my laptop being on?

**No - not in production.** There are two ways to run the agent:

1. **Local (testing only).** You run it on your machine with `.env.local`.
   If your machine sleeps or turns off, the agent stops. Use this only to
   verify it works before going live.
2. **LiveKit Cloud (production, always-on).** You deploy the agent to
   LiveKit's servers. It runs 24/7 with autoscaling, independent of your
   laptop. Secrets live as LiveKit Cloud deployment secrets, not in a local
   file. This is the real production setup.

---

## Project facts (already provisioned)

- LiveKit project ID: `p_5vfmp4mnabg`
- LIVEKIT_URL: `wss://voice-ai-u0razux7.livekit.cloud`
- SIP URI (for inbound telephony): `sip:5vfmp4mnabg.sip.livekit.cloud`
- Agent name (used by dispatch rules): `aria`

---

## Part A - Local test (your machine)

These are USER steps (I cannot run a terminal or paste secrets).

1. Install the LiveKit CLI (`lk`). See https://docs.livekit.io/home/cli/
2. Authenticate: `lk cloud auth` (opens browser, links the CLI to your
   project p_5vfmp4mnabg).
3. In the `livekit_agent/` folder, create a virtual env and install deps:
   - `python -m venv .venv`
   - `source .venv/bin/activate`  (Windows: `.venv\Scripts\activate`)
   - `pip install -r requirements.txt`
4. Copy the env template and fill in the three blanks:
   - `cp .env.example .env.local`
   - LIVEKIT_API_KEY / LIVEKIT_API_SECRET: from LiveKit dashboard ->
     Settings -> Keys (create an API key if none exists).
   - SARVAM_API_KEY: your Sarvam key.
5. Download model files (Silero VAD + turn detector) once:
   - `python agent.py download-files`
6. Run in dev mode:
   - `python agent.py dev`
7. Test the voice without a phone using the Agents Playground or
   `lk` console, then connect a phone number (Part C).

---

## Part B - Production deploy (LiveKit Cloud, always-on)

USER steps. Once this is done the agent runs without your machine.

1. From `livekit_agent/`, initialise cloud config (first time only):
   - `lk agent create`
   This registers the agent and generates a `livekit.toml`.
2. Set the secrets as DEPLOYMENT secrets (not committed, not local):
   - `lk agent update-secrets`  (or set them in the dashboard under the
     agent's Secrets). Provide SARVAM_API_KEY (LIVEKIT_URL/KEY/SECRET are
     injected automatically for cloud-run agents).
3. Deploy:
   - `lk agent deploy`
4. Confirm it is running:
   - `lk agent status`  (should show a healthy/running worker).
5. Redeploy after any code change: commit, then `lk agent deploy` again.

---

## Part B-Alt - Production deploy WITHOUT the LiveKit CLI (self-host)

You do NOT have to use the LiveKit CLI at all. The agent is a worker that
connects OUTBOUND to LiveKit Cloud over a WebSocket - it never needs inbound
ports and never needs `lk cloud auth`. This sidesteps the firewall block on
`cloud-api.livekit.io` and reuses the Railway/Render setup already in place.

How it works: `python agent.py start` registers the worker with LiveKit
server using the three env vars below; LiveKit dispatches jobs to it. The
LiveKit Cloud dashboard still shows the agent (under self-hosted) for
observability.

USER steps (run on Railway OR Render - pick one, both are already wired up):

1. Point the service at this branch/folder: repo root, branch `livekit-agent`,
   working dir `livekit_agent/`.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python agent.py start`   (use `start`, not `dev`, for prod).
4. Set these service environment variables (paste the secret values yourself):
   - `LIVEKIT_URL` = wss://voice-ai-u0razux7.livekit.cloud
   - `LIVEKIT_API_KEY` = <your LiveKit API key>
   - `LIVEKIT_API_SECRET` = <your LiveKit API secret>
   - `SARVAM_API_KEY` = <your Sarvam key>
5. Deploy. In the logs you should see the worker register and report
   `registered worker` / a healthy status. No CLI, no browser auth needed.
6. Health check: the worker serves a 200 on port 8081 at `/` once connected.
   On Railway/Render this can be the platform health check (optional).
7. Redeploy after code changes: push to `livekit-agent`, platform rebuilds.

Sizing note (from LiveKit load tests): ~4 cores / 8GB handles 10-25 concurrent
calls. For a POC a smaller instance is fine; one call needs far less.

When to use which:
- Part B (CLI deploy): fully managed by LiveKit Cloud, auto-scaling, but needs
  the CLI + reachability to `cloud-api.livekit.io` (currently firewall-blocked).
- Part B-Alt (self-host): no CLI, no firewall issue, reuses Railway/Render. You
  manage the container. Recommended given the blocked CLI auth.

---

## Part C - Connect a phone number (telephony)

Two options. Pick ONE. Both are USER steps (buying numbers / SIP config
are paid and account-level actions I cannot perform).

### Option 1 - LiveKit phone number (simplest)

1. Dashboard -> Telephony -> Phone Numbers -> buy/import a number.
2. Create an **inbound dispatch rule** that routes calls to the agent:
   - Agent name: `aria`
   - Rule type: dispatch to a new room per call.
   CLI equivalent: `lk sip dispatch-rule create` with `agent_name=aria`.
3. Call the number - Aria answers.

### Option 2 - Reuse the Exotel number via SIP trunk

1. Create an **inbound SIP trunk** in LiveKit pointing at your SIP URI
   `sip:5vfmp4mnabg.sip.livekit.cloud`.
   - CLI: `lk sip inbound-trunk create`.
2. In Exotel, route the ExoPhone (09513886363) to that LiveKit SIP URI
   instead of the current Voicebot/WebSocket applet.
3. Add the same dispatch rule as Option 1 (agent_name `aria`).
4. Call the Exotel number - Aria answers.

---

## Notes / caveats

- Sarvam STT and LLM model identifiers in `agent.py` are left as plugin
  DEFAULTS. If the first run errors on an unknown model string, set the
  exact identifier from the Sarvam plugin docs and redeploy.
- TTS is pinned to `bulbul:v3`, speaker `anushka`, `en-IN`, 8kHz for
  telephony, with `allow_interruptions=True` so callers can barge in.
- The `main` branch (REST bridge on Railway) and the Render service remain
  live as fallbacks. Nothing here touches them.
- Disclosure: Aria identifies as a virtual assistant if a caller directly
  and explicitly asks whether she is human. Adjust the persona in
  `agent.py` if your compliance policy requires a different stance.
