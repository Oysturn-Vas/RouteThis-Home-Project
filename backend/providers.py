import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any

import groq
from google import genai

from config import settings

logger = logging.getLogger("routemaster-providers")


@dataclass
class LLMResponse:
    content: str
    raw: Optional[Any] = None


class LLMProvider(ABC):
    @abstractmethod
    async def generate_text_only(self, messages: list[dict]) -> str:
        pass

    def _format_role(self, role: str) -> str:
        return "model" if role == "assistant" else role


class GeminiProvider(LLMProvider):
    def __init__(self, model: str = None):
        self.model = model or settings.GEMINI_MODEL_ID
        self.client = genai.Client(api_key=settings.GOOGLE_API_KEY)

    async def generate_text_only(self, messages: list[dict]) -> str:
        from google.genai import types
        
        contents = []
        for m in messages:
            text = m["content"] if isinstance(m["content"], str) else str(m["content"])
            role = "user" if m["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=contents
        )
        return response.text.strip() if response.text else ""


class GroqProvider(LLMProvider):
    def __init__(self, model: str = "llama-3.3-70b-versatile"):
        self.model = model
        self.client = groq.AsyncGroq(api_key=settings.GROQ_API_KEY)

    async def generate_text_only(self, messages: list[dict]) -> str:
        formatted = []
        for m in messages:
            role = "assistant" if m["role"] == "model" else m["role"]
            content = m["content"] if isinstance(m["content"], str) else str(m["content"])
            formatted.append({"role": role, "content": content})
        
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=formatted
        )
        
        if not response.choices:
            raise ValueError("Groq returned no choices")
        
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Groq returned empty content")
        
        return content.strip()


_providers = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}


_frontend_to_backend = {
    "local": "groq",
    "cloud": "gemini",
}


def get_provider(provider_name: str | None = None) -> LLMProvider:
    if provider_name is None:
        provider_name = getattr(settings, 'DEFAULT_PROVIDER', 'gemini')
    
    provider_name = provider_name.lower()
    
    provider_name = _frontend_to_backend.get(provider_name, provider_name)
    
    if provider_name not in _providers:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {list(_providers.keys())}")
    
    return _providers[provider_name]()
