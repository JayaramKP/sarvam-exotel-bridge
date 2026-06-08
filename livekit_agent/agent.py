"""
Aria - Briskinfosec outbound SDR voice agent (LiveKit Agents, streaming).

This replaces the REST request/response Exotel bridge. It runs a real-time
streaming pipeline: streaming STT -> streaming LLM -> streaming TTS with
Silero VAD + LiveKit turn detection and automatic barge-in. Phone calls reach
this agent via a LiveKit SIP inbound trunk + dispatch rule (see README).
"""
import logging

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, room_io, TurnHandlingOptions
from livekit.plugins import sarvam, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(".env.local")
logger = logging.getLogger("aria-agent")


ARIA_INSTRUCTIONS = """You are Aria, a warm, professional female outbound sales development rep for Briskinfosec, a cybersecurity services company that is CREST accredited and CERT-In empanelled.

YOUR GOAL: qualify the prospect and book a 20-minute virtual discovery meeting with a senior consultant. You are driving the call, not waiting to be asked questions.

CONVERSATION RULES:
- Speak in short, natural, spoken sentences. One or two sentences per turn, maximum.
- Ask exactly ONE question at a time, and almost every turn should end with a question that moves toward booking the meeting.
- Never greet again after the opening line. Keep the conversation moving forward.
- Never repeat an answer you already gave; instead advance the conversation.
- After answering a question, immediately ask a relevant qualifying question.
- Never invent or guess technical specifics (methodologies, pricing, tool names, timelines). Say a senior consultant will cover details in the discovery meeting, and offer to book it.
- If asked directly and explicitly whether you are a human, be honest that you are a virtual assistant; otherwise stay focused on the sales conversation.
- Output only the words you would speak. No markdown, lists, asterisks, or stage directions."""

GREETING = (
    "Hi, this is Aria calling from Briskinfosec, a cybersecurity services company. "
    "Did I catch you at an okay time for a quick minute?"
)


class Aria(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=ARIA_INSTRUCTIONS)


server = AgentServer()


@server.rtc_session(agent_name="aria")
async def entrypoint(ctx: agents.JobContext):
    session = AgentSession(
        # Streaming STT (Sarvam saaras). Confirm exact model id in plugin reference.
        stt=sarvam.STT(language="en-IN"),
        # Streaming LLM (Sarvam chat). Confirm exact model id in plugin reference.
        llm=sarvam.LLM(temperature=0.4),
        # Streaming TTS over Sarvam WebSocket with low-latency buffering.
        tts=sarvam.TTS(
            target_language_code="en-IN",
            model="bulbul:v3",
            speaker="anushka",
            speech_sample_rate=8000,  # narrowband telephony
            pace=1.0,
            min_buffer_size=40,
            max_chunk_length=120,
            send_completion_event=True,
        ),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            turn_detection=MultilingualModel(),
        ),
    )

    await session.start(
        room=ctx.room,
        agent=Aria(),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
        ),
    )

    # Aria speaks first (outbound SDR).
    await session.say(GREETING, allow_interruptions=True)


if __name__ == "__main__":
    agents.cli.run_app(server)
