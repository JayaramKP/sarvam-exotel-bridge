import logging

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import sarvam

load_dotenv()

logger = logging.getLogger("aria-sdr-agent")
logger.setLevel(logging.INFO)


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


class AriaSDRAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=ARIA_INSTRUCTIONS,
            # Saaras v3 STT - speech to text (auto-detect Indian languages + English)
            stt=sarvam.STT(
                language="unknown",
                model="saaras:v3",
                mode="transcribe",
                flush_signal=True,
            ),
            # Sarvam LLM - the reasoning brain (OpenAI-compatible, tuned for India)
            llm=sarvam.LLM(
                model="sarvam-30b",
            ),
            # Bulbul v3 TTS - text to speech
            tts=sarvam.TTS(
                target_language_code="en-IN",
                model="bulbul:v3",
                speaker="anand",
            ),
        )

    async def on_enter(self):
        """Aria starts the conversation when the call connects."""
        self.session.generate_reply()


async def entrypoint(ctx: JobContext):
    logger.info(f"User connected to room: {ctx.room.name}")

    session = AgentSession(
        turn_detection="stt",
        min_endpointing_delay=0.07,
    )
    await session.start(
        agent=AriaSDRAgent(),
        room=ctx.room,
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
