import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Optional, List, Dict, Any

from faster_whisper import WhisperModel
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from elevenlabs.client import ElevenLabs
import groq

from config import settings
from state_manager import StateManager, DynamoDBSessionManager
from tools import query_knowledge_base
from providers import LLMProvider, get_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("routemaster-voice")

router = APIRouter()


STT_CORRECTIONS = {
    "daughter": "router",
    "smother": "router",
    "mother": "router",
    "brother": "router",
    "report": "router",
}

END_SESSION_MARKER = "[END_SESSION]"
POST_SPEECH_WAIT = 5
DISCONNECT_DELAY = 10


elevenlabs_client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
groq_client = groq.AsyncGroq(api_key=settings.GROQ_API_KEY)

asr_model = None
if settings.ASR_PROVIDER == "local":
    try:
        model_size = "base.en"
        asr_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.info(f"Successfully loaded local ASR model: {model_size}")
    except Exception as e:
        logger.exception(
            f"Failed to load local ASR model. Please check your installation. Error: {e}"
        )

state_manager = StateManager()
session_manager = DynamoDBSessionManager()


def correct_stt_text(text: str) -> str:
    for wrong, correct in STT_CORRECTIONS.items():
        text = text.replace(wrong, correct)
        text = text.replace(wrong.capitalize(), correct.capitalize())
    return text


def is_gibberish(text: str) -> bool:
    text_lower = text.lower().strip()

    if len(text_lower.split()) <= 3:
        router_keywords = [
            "router",
            "wifi",
            "internet",
            "network",
            "connection",
            "password",
            "device",
            "firmware",
            "speed",
            "signal",
        ]
        if not any(kw in text_lower for kw in router_keywords):
            return True

    if re.match(r"^[a-zA-Z]{3,}\d{4,}$", text_lower):
        return True

    return False


def strip_markdown_for_tts(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    return text


class VoiceAgent:
    def __init__(
        self,
        websocket: WebSocket,
        session_id: str,
        chat_history: List[Dict[str, Any]] = None,
        troubleshooting_step: str = "start",
        provider: LLMProvider | None = None,
    ):
        self.websocket = websocket
        self.session_id = session_id
        self.troubleshooting_step = troubleshooting_step
        self.chat_history = chat_history if chat_history is not None else []
        self.provider = provider or get_provider()
        self.audio_buffer = bytearray()
        self.silence_timer: Optional[asyncio.Task] = None
        self.processing_audio: bool = False
        self.gibberish_attempts: int = 0
        self.waiting_for_tts_complete: bool = False

    async def send_error(self, message: str):
        await self.websocket.send_text(
            json.dumps({"type": "error", "message": message})
        )

    async def save_state(self):
        await asyncio.to_thread(
            session_manager.save_session,
            self.session_id,
            self.chat_history,
            self.troubleshooting_step,
        )

    def _format_messages(self, rag_context: str = "") -> list[dict]:
        system_prompt = state_manager.get_system_prompt()
        if rag_context:
            system_prompt = system_prompt + rag_context
        messages = [{"role": "system", "content": system_prompt}]
        for h in self.chat_history:
            role = "assistant" if h["role"] == "assistant" else h["role"]
            messages.append({"role": role, "content": h["text"]})
        return messages

    async def handle_connection(self):
        try:
            if not self.chat_history:
                self.troubleshooting_step = "qualification"
                greeting = "Hello! I'm RouteMaster AI. I see you're calling about your Linksys EA6350 router. How can I help you today?"
                await self.websocket.send_text(
                    json.dumps(
                        {"type": "transcript", "role": "model", "text": greeting}
                    )
                )
                await self.speak_text(greeting)
                self.chat_history.append({"role": "assistant", "text": greeting})
                await self.save_state()

            if self.troubleshooting_step == "awaiting_reconnect_after_reboot":
                self.troubleshooting_step = "verification"
                greeting = (
                    "Welcome back! Did the reboot solve the issue with your router?"
                )
                await self.websocket.send_text(
                    json.dumps(
                        {"type": "transcript", "role": "model", "text": greeting}
                    )
                )
                await self.speak_text(greeting)
                self.chat_history.append({"role": "assistant", "text": greeting})
                await self.save_state()

            while True:
                data = await self.websocket.receive_text()
                message = json.loads(data)
                message_type = message.get("type")

                if message_type == "text":
                    user_text = message.get("data")
                    if user_text:
                        await self.websocket.send_text(
                            json.dumps({"type": "status", "status": "processing"})
                        )
                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "type": "transcript",
                                    "role": "user",
                                    "text": user_text,
                                }
                            )
                        )
                        response_text, should_end = await self.get_llm_response(
                            user_text
                        )

                        if should_end:
                            break

                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "type": "transcript",
                                    "role": "model",
                                    "text": response_text,
                                }
                            )
                        )
                        await self.speak_text(response_text)
                        await self.websocket.send_text(
                            json.dumps(
                                {"type": "status", "status": "processing_complete"}
                            )
                        )
                elif message_type == "provider_change":
                    new_provider = message.get("provider", "gemini")
                    try:
                        self.provider = get_provider(new_provider)
                    except ValueError:
                        pass
                    await self.websocket.send_text(
                        json.dumps(
                            {"type": "provider_changed", "provider": new_provider}
                        )
                    )
                elif message_type == "audio":
                    audio_str = message.get("data")
                    if audio_str.startswith("data:"):
                        audio_str = audio_str.split(",")[1]
                    audio_data = base64.b64decode(audio_str)

                    self.audio_buffer.clear()
                    self.audio_buffer.extend(audio_data)

                    if self.silence_timer:
                        self.silence_timer.cancel()
                    self.silence_timer = asyncio.create_task(
                        self.process_audio_after_silence()
                    )
                elif message_type == "audio_complete":
                    logger.info(
                        f"[{self.session_id}] Received audio_complete, waiting_for_tts_complete={self.waiting_for_tts_complete}"
                    )
                    if self.waiting_for_tts_complete:
                        self.waiting_for_tts_complete = False
                        await self.send_disconnect()
                        break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            error_message = (
                f"An unexpected error occurred in the main connection handler: {e}"
            )
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)

        if self.waiting_for_tts_complete:
            logger.info(f"[{self.session_id}] Waiting for audio_complete...")
            try:
                while True:
                    data = await self.websocket.receive_text()
                    message = json.loads(data)
                    message_type = message.get("type")
                    logger.info(
                        f"[{self.session_id}] Received message type: {message_type}"
                    )
                    if message_type == "audio_complete":
                        logger.info(f"[{self.session_id}] audio_complete received!")
                        break
            except WebSocketDisconnect:
                pass
            await self.send_disconnect()

        logger.info(f"[{self.session_id}] handle_connection ending")

    async def process_audio_after_silence(self):
        if self.processing_audio:
            return
        self.processing_audio = True
        try:
            await asyncio.sleep(0.5)

            if not self.audio_buffer:
                return

            audio_data = bytes(self.audio_buffer)
            self.audio_buffer.clear()

            await self.websocket.send_text(
                json.dumps({"type": "status", "status": "processing"})
            )

            user_text = await self.transcribe_audio(audio_data)

            if user_text:
                user_text = correct_stt_text(user_text)

            if user_text:
                if is_gibberish(user_text):
                    self.gibberish_attempts += 1
                    if self.gibberish_attempts == 1:
                        clarification = "I didn't quite catch that clearly. Could you please repeat what you said?"
                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "type": "transcript",
                                    "role": "user",
                                    "text": user_text,
                                }
                            )
                        )
                        await self.websocket.send_text(
                            json.dumps(
                                {
                                    "type": "transcript",
                                    "role": "model",
                                    "text": clarification,
                                }
                            )
                        )
                        await self.speak_text(clarification)
                        return
                    else:
                        self.gibberish_attempts = 0
                        user_text = f"[User had trouble being heard clearly, please suggest they type instead if this continues]: {user_text}"

                await self.websocket.send_text(
                    json.dumps(
                        {"type": "transcript", "role": "user", "text": user_text}
                    )
                )
                response_text = await self.get_llm_response(user_text)
                await self.websocket.send_text(
                    json.dumps(
                        {"type": "transcript", "role": "model", "text": response_text}
                    )
                )
                await self.speak_text(response_text)
            else:
                await self.websocket.send_text(
                    json.dumps(
                        {
                            "type": "status",
                            "status": "transcription_failed",
                            "message": "Could not understand audio. Please try again.",
                        }
                    )
                )
            await self.websocket.send_text(
                json.dumps({"type": "status", "status": "processing_complete"})
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_message = f"Failed to process audio after silence: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)
        finally:
            self.processing_audio = False

    async def transcribe_audio_local(self, audio_data: bytes) -> Optional[str]:
        if not asr_model:
            await self.send_error("Local ASR model is not available.")
            return None
        tmpfile_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmpfile:
                tmpfile_path = tmpfile.name
                tmpfile.write(audio_data)

            segments, _ = await asyncio.to_thread(
                asr_model.transcribe, tmpfile_path, beam_size=5
            )
            text = " ".join([segment.text for segment in segments])
            return text.strip()

        except Exception as e:
            error_message = f"Local transcription failed: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)
            return None
        finally:
            if tmpfile_path and os.path.exists(tmpfile_path):
                os.remove(tmpfile_path)

    async def transcribe_audio(self, audio_data: bytes) -> Optional[str]:
        if settings.ASR_PROVIDER == "local":
            return await self.transcribe_audio_local(audio_data)

        if len(audio_data) < 1000:
            return None
        try:
            audio_file = io.BytesIO(audio_data)
            audio_file.name = "audio.wav"
            response = await groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                language="en",
                file=audio_file,
                response_format="text",
            )
            return response.text
        except Exception as e:
            error_message = f"Groq transcription failed: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)
            return None

    async def speak_text(self, text: str):
        try:
            text_for_tts = strip_markdown_for_tts(text)
            logger.info(f"[{self.session_id}] Generating TTS audio for: {text}")
            audio_generator = await asyncio.to_thread(
                elevenlabs_client.text_to_speech.convert,
                text=text_for_tts,
                voice_id=settings.ELEVENLABS_VOICE_ID,
                model_id=settings.ELEVENLABS_VOICE_MODEL,
            )
            audio_bytes = b"".join([chunk for chunk in audio_generator])
            logger.info(
                f"[{self.session_id}] TTS audio generated, sending to frontend (size: {len(audio_bytes)} bytes)"
            )

            await self.websocket.send_text(
                json.dumps(
                    {
                        "type": "audio",
                        "data": base64.b64encode(audio_bytes).decode("utf-8"),
                    }
                )
            )
            logger.info(f"[{self.session_id}] TTS audio sent to frontend")
        except Exception as e:
            error_message = f"ElevenLabs text-to-speech failed: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)

    async def get_llm_response(self, user_text: str) -> tuple[str, bool]:
        """Returns (response_text, should_end_session)"""
        try:
            self.chat_history.append({"role": "user", "text": user_text})

            rag_context = ""
            
            # Only skip RAG for very short greeting messages (≤2 words)
            words = user_text.lower().split()
            if len(words) <= 2:
                logger.info(f"[{self.session_id}] Skipping RAG for greeting: '{user_text}'")
            else:
                logger.info(f"[{self.session_id}] Calling RAG for: '{user_text}'")
                rag_result = await query_knowledge_base(user_text)
                
                if rag_result.get("success") and rag_result.get("answer"):
                    rag_context = f"""
=== LINKSYS EA6350 MANUAL REFERENCE ===
{rag_result['answer']}
=== END MANUAL REFERENCE ===

Use the above information from the official Linksys EA6350 manual if it's relevant to the user's question. If it's not relevant or helpful, ignore it and answer from your general knowledge.
"""
                    logger.info(f"[{self.session_id}] RAG returned: {rag_result['answer'][:100]}...")

            response = await self.provider.generate_text_only(
                self._format_messages(rag_context)
            )

            response = re.sub(
                r"<system-reminder>.*?</system-reminder>", "", response, flags=re.DOTALL
            )
            should_end_session = END_SESSION_MARKER in response
            response = response.replace(END_SESSION_MARKER, "").strip()

            if should_end_session:
                logger.info(
                    f"[{self.session_id}] END_SESSION marker detected - sending message and waiting for TTS"
                )
                response = response.strip()
                await self.websocket.send_text(
                    json.dumps(
                        {"type": "transcript", "role": "model", "text": response}
                    )
                )
                await self.speak_text(response)
                self.waiting_for_tts_complete = True
                logger.info(
                    f"[{self.session_id}] waiting_for_tts_complete set to True - waiting for frontend to send audio_complete"
                )

            self.chat_history.append({"role": "assistant", "text": response})
            await self.save_state()

            return response, should_end_session
        except Exception as e:
            logger.exception(f"[{self.session_id}] LLM response failed: {e}")
            await self.send_error(f"LLM error: {e}")
            return "I'm sorry, I encountered an error. Please try again.", False

    async def send_disconnect(self):
        logger.info(
            f"[{self.session_id}] Waiting {POST_SPEECH_WAIT}s after speech ended"
        )
        await asyncio.sleep(POST_SPEECH_WAIT)
        logger.info(
            f"[{self.session_id}] Sending disconnect message with {DISCONNECT_DELAY}s countdown"
        )
        await self.websocket.send_text(
            json.dumps(
                {
                    "type": "disconnect",
                    "delay": DISCONNECT_DELAY,
                    "message": "Ending session...",
                }
            )
        )
        await asyncio.sleep(DISCONNECT_DELAY)
        logger.info(f"[{self.session_id}] Closing websocket")
        await self.websocket.close()


@router.websocket("/ws/voice")
async def voice_endpoint(
    websocket: WebSocket,
    sessionId: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
):
    await websocket.accept()

    agent = None
    if sessionId:
        session_data = await asyncio.to_thread(session_manager.load_session, sessionId)
        if session_data:
            agent = VoiceAgent(
                websocket=websocket,
                session_id=sessionId,
                chat_history=session_data.get("chat_history", []),
                troubleshooting_step=session_data.get("troubleshooting_step", "start"),
                provider=get_provider(provider) if provider else None,
            )
            await websocket.send_text(
                json.dumps({"type": "full_history", "history": agent.chat_history})
            )

    if agent is None:
        new_session_id = str(uuid.uuid4())
        agent = VoiceAgent(
            websocket=websocket,
            session_id=new_session_id,
            provider=get_provider(provider) if provider else None,
        )
        await websocket.send_text(
            json.dumps({"type": "session_created", "sessionId": new_session_id})
        )
        await agent.save_state()

    await agent.handle_connection()
