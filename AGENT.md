# RouteMaster AI - Agent Context

**Attention AI Developers (OpenCode, Gemini, Copilot, etc.):**
Read this file carefully before making architectural changes or writing code for the RouteMaster AI project.

## 1. Project Overview

RouteMaster AI is a real-time, voice-native technical support agent for the Linksys EA6350 router. It uses a custom WebSocket pipeline, combining Groq (Whisper STT), Gemini (Multimodal LLM), and ElevenLabs (TTS) into a seamless voice experience.

## 2. Architecture & Data Flow

- **WebSocket Audio Pipeline:** Audio from the React frontend streams via WebSockets to the Python `VoiceAgent` (`backend/voice_handler.py`).
- **STT (Speech-to-Text):** Handled by `groq.AsyncGroq` hitting **Groq's API** using `whisper-large-v3`.
- **LLM (Reasoning):** Handled by Google's official `genai.Client` using `gemini-2.5-flash`.
- **TTS (Text-to-Speech):** Handled by `elevenlabs.client.ElevenLabs` using `eleven_multilingual_v2`.
- **Multimodal Input (Images):**
  - Frontend users upload images via the React UI.
  - The frontend sends these images to the backend via REST or WebSockets, and the backend injects them into the conversation history.

## 3. RAG Engine & Vector DB (Pinecone)

- **Embeddings:** Google `models/gemini-embedding-001` (Dimension size: **3072**).
- **Index:** Serverless Pinecone index named `routemaster-manuals` using cosine similarity.
- **Ingestion Logic (`scripts/ingest_manual.py`):** Uses PyMuPDF. It extracts both text and images. Images are passed to Gemini 1.5 Flash to generate technical descriptions (diagram captioning), which are fused with the text before chunking via Langchain's `RecursiveCharacterTextSplitter` and embedding.

## 4. State Machine & Guardrails (`backend/state_manager.py`)

The agent operates via a comprehensive, multi-phase System Prompt that gives Gemini the authority to manage the conversation flow dynamically:

1.  **Phase 1: Qualification:** Ask questions to see if a reboot is needed. If NOT appropriate, apologize and exit gracefully.
2.  **Phase 2: Reboot Instructions:** If appropriate, get permission, then use the `query_manual` tool to walk through the reboot ONE STEP AT A TIME, waiting for user confirmation.
3.  **Phase 3: Verification:** Check if the issue is resolved post-reboot.
4.  **Phase 4: Resolution & Exit:** If resolved, sign off gracefully. If not resolved, apologize and advise contacting human support.

**CRITICAL SAFETY GUARDRAIL:** Be sure to maintain checks for safety hazard keywords (e.g., "smoke", "fire", "sparks") to immediately force the LLM to output an emergency abort message if detected.

## 5. Coding Standards for AI

- **Tool Calling:** The `RouteMasterTools.query_manual()` function in `backend/tools.py` provides RAG capabilities. The `rag_tools` instance is initialized in `voice_handler.py` but tools are currently passed as empty. To enable RAG tool calling, pass tools to `RouteMasterTools(tools=[...])` and integrate with the LLM provider's tool calling mechanism.
- **Async/Await:** The WebSocket connection operates asynchronously. Ensure all custom API calls or long-running tasks within the `VoiceAgent` loop are properly awaited.
- **Environment Variables:** All secrets are loaded strictly via `pydantic-settings` in `config/settings.py`. Never hardcode keys.
