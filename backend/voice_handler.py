import asyncio
import base64
import io
import json
import logging
import tempfile
import uuid
import os
import re
from typing import Optional, List, Dict, Any

from faster_whisper import WhisperModel
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from elevenlabs.client import ElevenLabs
import groq
from google import genai

from config import settings
from state_manager import StateManager, DynamoDBSessionManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("routemaster-voice")

router = APIRouter()

STT_CORRECTIONS = {
    "daughter": "router",
    "smother": "router",
    "mother": "router",
    "brother": "router",
}

END_SESSION_MARKER = "[END_SESSION]"
DISCONNECT_DELAY = 5


def correct_stt_text(text: str) -> str:
    for wrong, correct in STT_CORRECTIONS.items():
        text = text.replace(wrong, correct)
        text = text.replace(wrong.capitalize(), correct.capitalize())
    return text


def is_gibberish(text: str) -> bool:
    text_lower = text.lower().strip()
    
    if len(text_lower.split()) <= 3:
        router_keywords = ["router", "wifi", "internet", "network", "connection", "password", "device", "firmware", "speed", "signal"]
        if not any(kw in text_lower for kw in router_keywords):
            return True
    
    if re.match(r'^[a-zA-Z]{3,}\d{4,}$', text_lower):
        return True
    
    return False


def strip_markdown_for_tts(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    return text


elevenlabs_client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
groq_client = groq.AsyncGroq(api_key=settings.GROQ_API_KEY)
genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)

asr_model = None
if settings.ASR_PROVIDER == "local":
    try:
        model_size = "base.en"
        asr_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.info(f"Successfully loaded local ASR model: {model_size}")
    except Exception as e:
        logger.exception(f"Failed to load local ASR model. Please check your installation. Error: {e}")

state_manager = StateManager()
session_manager = DynamoDBSessionManager()


class VoiceAgent:
    def __init__(self, websocket: WebSocket, session_id: str, chat_history: List[Dict[str, Any]] = None, troubleshooting_step: str = "start", provider_preference: str = "local"):
        self.websocket = websocket
        self.session_id = session_id
        self.troubleshooting_step = troubleshooting_step
        self.chat_history = chat_history if chat_history is not None else []
        self.provider_preference = provider_preference
        self.audio_buffer = bytearray()
        self.silence_timer: Optional[asyncio.Task] = None
        self.processing_audio: bool = False
        self.gibberish_attempts: int = 0

    async def send_error(self, message: str):
        await self.websocket.send_text(
            json.dumps({"type": "error", "message": message})
        )

    async def save_state(self):
        await asyncio.to_thread(
            session_manager.save_session,
            self.session_id,
            self.chat_history,
            self.troubleshooting_step
        )

    async def handle_connection(self):
        try:
            if not self.chat_history:
                self.troubleshooting_step = "qualification"
                greeting = "Hello! I'm RouteMaster AI. I see you're calling about your Linksys EA6350 router. How can I help you today?"
                await self.websocket.send_text(
                    json.dumps({"type": "transcript", "role": "model", "text": greeting})
                )
                await self.speak_text(greeting)
                await self.save_state()

            if self.troubleshooting_step == "awaiting_reconnect_after_reboot":
                self.troubleshooting_step = "verification"
                greeting = "Welcome back! Did the reboot solve the issue with your router?"
                await self.websocket.send_text(
                    json.dumps({"type": "transcript", "role": "model", "text": greeting})
                )
                await self.speak_text(greeting)
                await self.save_state()

            while True:
                data = await self.websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "text":
                    user_text = message.get("data")
                    logger.info(f"[{self.session_id}] Received text: {user_text}")

                    if user_text:
                        await self.websocket.send_text(
                            json.dumps({"type": "status", "status": "processing"})
                        )
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
                        await self.websocket.send_text(
                            json.dumps({"type": "status", "status": "processing_complete"})
                        )
                elif message.get("type") == "provider_change":
                    new_provider = message.get("provider", "local")
                    self.provider_preference = new_provider
                    logger.info(f"[{self.session_id}] Provider changed to: {new_provider}")
                    await self.websocket.send_text(
                        json.dumps({"type": "provider_changed", "provider": new_provider})
                    )
                elif message.get("type") == "audio":
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
        except WebSocketDisconnect:
            logger.info(f"[{self.session_id}] Client disconnected.")
        except Exception as e:
            error_message = f"An unexpected error occurred in the main connection handler: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)

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

            logger.info(f"[{self.session_id}] Silence detected. Transcribing {len(audio_data)} bytes...")
            await self.websocket.send_text(
                json.dumps({"type": "status", "status": "processing"})
            )
            user_text = await self.transcribe_audio(audio_data)
            user_text = correct_stt_text(user_text) if user_text else None

            if user_text:
                if is_gibberish(user_text):
                    self.gibberish_attempts += 1
                    if self.gibberish_attempts == 1:
                        clarification = "I didn't quite catch that clearly. Could you please repeat what you said?"
                        await self.websocket.send_text(
                            json.dumps({"type": "transcript", "role": "user", "text": user_text})
                        )
                        await self.websocket.send_text(
                            json.dumps({"type": "transcript", "role": "model", "text": clarification})
                        )
                        await self.speak_text(clarification)
                        return
                    else:
                        self.gibberish_attempts = 0
                        user_text = f"[User had trouble being heard clearly, please suggest they type instead if this continues]: {user_text}"
                
                await self.websocket.send_text(
                    json.dumps({"type": "transcript", "role": "user", "text": user_text})
                )
                response_text = await self.get_llm_response(user_text)
                await self.websocket.send_text(
                    json.dumps({"type": "transcript", "role": "model", "text": response_text})
                )
                await self.speak_text(response_text)
            else:
                await self.websocket.send_text(
                    json.dumps({"type": "status", "status": "transcription_failed", "message": "Could not understand audio. Please try again."})
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

            segments, _ = await asyncio.to_thread(asr_model.transcribe, tmpfile_path, beam_size=5)
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
            logger.warning(f"[{self.session_id}] Audio data too small for transcription ({len(audio_data)} bytes).")
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
            audio_generator = await asyncio.to_thread(
                elevenlabs_client.text_to_speech.convert,
                text=text_for_tts,
                voice_id=settings.ELEVENLABS_VOICE_ID,
                model_id=settings.ELEVENLABS_VOICE_MODEL,
            )
            audio_bytes = b"".join([chunk for chunk in audio_generator])
            await self.websocket.send_text(
                json.dumps({
                    "type": "audio",
                    "data": base64.b64encode(audio_bytes).decode("utf-8"),
                })
            )
        except Exception as e:
            error_message = f"ElevenLabs text-to-speech failed: {e}"
            logger.exception(f"[{self.session_id}] {error_message}")
            await self.send_error(error_message)

    async def get_llm_response(self, user_text: str) -> str:
        try:
            self.chat_history.append({"role": "user", "text": user_text})

            def to_message_role(role: str) -> str:
                return "assistant" if role == "model" else role

            messages = [{"role": "system", "content": state_manager.get_system_prompt()}] + \
                       [{"role": to_message_role(h["role"]), "content": h["text"]} for h in self.chat_history]

            if self.provider_preference == "gemini":
                response = await genai_client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=messages
                )
                response_text = response.text.strip()
            else:
                response = await groq_client.chat.completions.create(
                    model=settings.GROQ_MODEL_ID,
                    messages=messages
                )
                response_text = response.choices[0].message.content.strip()

            should_end_session = END_SESSION_MARKER in response_text
            response_text = response_text.replace(END_SESSION_MARKER, "").strip()

            self.chat_history.append({"role": "model", "text": response_text})
            await self.save_state()

            if should_end_session:
                await self.send_disconnect_message()

            return response_text
        except Exception as e:
            logger.exception(f"[{self.session_id}] LLM response failed: {e}")
            await self.send_error(f"LLM error: {e}")
            return "I'm sorry, I encountered an error. Please try again."

    async def send_disconnect_message(self):
        await self.websocket.send_text(
            json.dumps({"type": "disconnect", "delay": DISCONNECT_DELAY, "message": "Ending session..."})
        )
        await asyncio.sleep(DISCONNECT_DELAY)
        await self.websocket.close()


@router.websocket("/ws/voice")
async def voice_endpoint(websocket: WebSocket, sessionId: Optional[str] = Query(None), provider: Optional[str] = Query(None)):
    await websocket.accept()

    agent = None
    if sessionId:
        session_data = await asyncio.to_thread(session_manager.load_session, sessionId)
        if session_data:
            agent = VoiceAgent(
                websocket=websocket,
                session_id=sessionId,
                chat_history=session_data.get('chat_history', []),
                troubleshooting_step=session_data.get('troubleshooting_step', 'start'),
                provider_preference=provider or "local"
            )
            logger.info(f"Resuming session: {sessionId}")
            await websocket.send_text(json.dumps({"type": "full_history", "history": agent.chat_history}))

    if agent is None:
        new_session_id = str(uuid.uuid4())
        agent = VoiceAgent(
            websocket=websocket,
            session_id=new_session_id,
            provider_preference=provider or "local"
        )
        logger.info(f"Starting new session: {new_session_id}")
        await websocket.send_text(json.dumps({"type": "session_created", "sessionId": new_session_id}))
        await agent.save_state()

    await agent.handle_connection()
