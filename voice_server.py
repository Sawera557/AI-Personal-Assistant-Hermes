"""Hermes voice service — real-time, read-only voice mode (Pipecat + Groq).

Runs as its OWN process, alongside Flask (default ws://0.0.0.0:8765):

    pip install -r requirements.txt -r requirements-voice.txt
    python voice_server.py

Pipeline:  browser mic --(WebSocket, raw PCM16)--> Silero VAD (turn-taking)
           --> Groq STT (whisper-large-v3-turbo) --> Groq LLM (read-only Maton
           tools) --> Groq TTS --> raw PCM16 --> browser speaker.  Barge-in on.

SAFETY: the LLM's tools route through `hermes.voice_tools.voice_dispatch`, which
is hard-locked to read-only (GET) Maton calls. There is NO write path here.

GROQ_API_KEY is read server-side from the existing Config; it never reaches the
browser. The browser only ever talks to THIS service, never to api.groq.com.

──────────────────────────────────────────────────────────────────────────────
VERIFY-ON-FIRST-RUN (I could not execute torch/audio in my build environment):
Pipecat's module paths shift between releases. This file targets pipecat-ai
1.4.x. If an import fails on your host, it's almost always one of the lines in
the `# [pipecat-import]` block below — send me the traceback and I'll pin it.
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import sys

from loguru import logger

from hermes.config import Config
from hermes.voice_tools import VOICE_TOOL_SPECS, voice_dispatch

# ---- [pipecat-import] version-sensitive imports (pipecat-ai 1.4.x) ----------
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.groq.tts import GroqTTSService
from pipecat.transports.network.websocket_server import (
    WebsocketServerParams,
    WebsocketServerTransport,
)
# ----------------------------------------------------------------------------

IN_SAMPLE_RATE = 16_000   # mic -> STT
OUT_SAMPLE_RATE = 24_000  # TTS -> speaker (browser must play at this rate)

SYSTEM_PROMPT = (
    "You are Hermes, a friendly voice assistant on a phone-style call. You are "
    "READ-ONLY: you can look up and summarize the user's emails, files, "
    "calendar, invoices, contacts and so on through the provided tools, but you "
    "cannot send, create, edit, or delete anything. If the user asks you to "
    "perform an action that changes something, briefly say you can only read in "
    "voice mode and that they can use the typed assistant for changes. "
    "Keep answers short and conversational — you are being spoken aloud, so "
    "avoid lists, markdown, or long quotes. Lead with the answer."
)


class RawPCMSerializer(FrameSerializer):
    """Minimal wire format we fully control (no protobuf, no build step).

    Outbound: audio frames -> raw little-endian PCM16 bytes (binary WS frame).
              interruptions -> a tiny JSON text frame so the browser can stop
              playback instantly (barge-in).
    Inbound:  raw PCM16 bytes from the mic -> InputAudioRawFrame.
    """

    @property
    def type(self) -> FrameSerializerType:
        return FrameSerializerType.BINARY

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame):
        if isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            return frame.audio  # raw PCM16 bytes -> binary frame
        if isinstance(frame, StartInterruptionFrame):
            return json.dumps({"type": "interrupt"})  # str -> text frame
        return None

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data), sample_rate=IN_SAMPLE_RATE, num_channels=1
            )
        return None


async def _handle_tool(*args, **kwargs):
    """Tool handler bridging the LLM to the read-only Maton guard.

    Written for the broadly-supported register_function signature
    (function_name, tool_call_id, arguments, llm, context, result_callback).
    On pipecat builds that pass a single FunctionCallParams object, see the
    note in _register_tools().
    """
    # Support both calling conventions defensively.
    if len(args) == 1 and hasattr(args[0], "result_callback"):
        params = args[0]
        name = params.function_name
        arguments = params.arguments or {}
        result_callback = params.result_callback
    else:
        name = args[0]
        arguments = args[2] or {}
        result_callback = args[5]

    content, is_error = voice_dispatch(name, arguments)
    try:
        payload = json.loads(content)
    except Exception:  # noqa: BLE001
        payload = {"result": content}
    await result_callback(payload)


def _register_tools(llm: GroqLLMService) -> None:
    for spec in VOICE_TOOL_SPECS:
        llm.register_function(spec["function"]["name"], _handle_tool)


async def main() -> None:
    if not Config.GROQ_API_KEY:
        logger.error("GROQ_API_KEY is not set — add it to your .env. Aborting.")
        sys.exit(1)

    transport = WebsocketServerTransport(
        host=Config.VOICE_WS_HOST,
        port=Config.VOICE_WS_PORT,
        params=WebsocketServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=RawPCMSerializer(),
            session_timeout=60 * 5,
        ),
    )

    stt = GroqSTTService(
        api_key=Config.GROQ_API_KEY, model=Config.GROQ_STT_MODEL, language="en"
    )
    llm = GroqLLMService(api_key=Config.GROQ_API_KEY, model=Config.GROQ_LLM_MODEL)
    tts = GroqTTSService(
        api_key=Config.GROQ_API_KEY,
        model_name=Config.GROQ_TTS_MODEL,
        voice_id=Config.GROQ_TTS_VOICE,
        sample_rate=OUT_SAMPLE_RATE,
    )

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=VOICE_TOOL_SPECS,
    )
    context_aggregator = llm.create_context_aggregator(context)
    _register_tools(llm)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,  # barge-in
            audio_in_sample_rate=IN_SAMPLE_RATE,
            audio_out_sample_rate=OUT_SAMPLE_RATE,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):  # noqa: ANN001
        logger.info("Voice client connected")
        await task.queue_frames(
            [TTSSpeakFrame("Hi, I'm Hermes. I can read and summarize from your "
                           "apps. What would you like to know?")]
        )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):  # noqa: ANN001
        logger.info("Voice client disconnected")

    logger.info(
        f"Hermes voice service listening on ws://{Config.VOICE_WS_HOST}:"
        f"{Config.VOICE_WS_PORT}  (LLM={Config.GROQ_LLM_MODEL}, read-only)"
    )
    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())
