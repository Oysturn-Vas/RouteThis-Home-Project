import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM Provider
    GROQ_MODEL_ID: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # ASR Provider
    ASR_PROVIDER: str = "cloud"

    # Google Gemini
    GOOGLE_API_KEY: str

    # Groq (STT)
    GROQ_API_KEY: str

    # ElevenLabs (TTS)
    ELEVENLABS_API_KEY: str
    ELEVENLABS_VOICE_ID: str = "56bWURjYFHyYyVf490Dp"
    ELEVENLABS_VOICE_MODEL: str = "eleven_flash_v2"

    # Pinecone Vector DB
    PINECONE_API_KEY: str
    PINECONE_INDEX_NAME: str = "routemaster-manuals"

    # AWS
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    DYNAMODB_SESSIONS_TABLE: str = "routemaster-sessions"

    # Server config
    BACKEND_PORT: int = 8000
    FRONTEND_URL: str = "http://localhost:5173"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
