# RouteMaster AI

RouteMaster AI is a **voice-native, multimodal technical support agent** designed to assist users with the Linksys EA6350 router. It features a scalable, microservice-based architecture with swappable AI providers, persistent session management, and a modern user interface.

## Core Features

- **Seamless Voice Conversations:** Guides users through complex troubleshooting flows (like router reboots) using real-time, low-latency voice with automatic speech recognition and text-to-speech.
- **Persistent Sessions:** User conversations are stored in AWS DynamoDB, allowing users to refresh the page or reconnect after a router reboot and continue exactly where they left off. Sessions expire after 24 hours via TTL.
- **Swappable AI Providers:** Switch between cloud-based (Google Gemini) and local (Groq Llama 3.3 70B) LLM models via a toggle in the UI. ASR is configurable via environment variables for cloud (Groq Whisper) or local (faster-whisper) processing.
- **Robust Audio Processing:** Features silence detection, race condition prevention with asyncio locks, STT corrections for common mishearings, and gibberish detection to handle imperfect audio input gracefully.
- **RAG & Vision Pipeline:** Processes PDF manuals including diagrams using multimodal AI to provide accurate, context-aware answers based on official documentation stored in Pinecone vector database.
- **Guardrails System:** Comprehensive input/output validation including prompt injection detection, sensitive content masking, toxicity filtering, and behavioral guidelines. RAG responses are validated against source material to prevent hallucinations.
- **Auto-Disconnect:** LLM-controlled session termination with user-facing countdown timer when troubleshooting is complete.

## System Architecture

### Architecture Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                        PRESENTATION LAYER                        │
│                   React + Vite + Tailwind CSS                    │
│  - Microphone capture (Web Speech API)                           │
│  - Audio playback (Web Audio API)                               │
│  - Real-time transcript display (ReactMarkdown)                 │
│  - Provider toggle (Local/Cloud LLM)                            │
│  - Session management (sessionStorage)                           │
└─────────────────────────────────────────────────────────────────┘
                              ↕ WebSocket
┌─────────────────────────────────────────────────────────────────┐
│                      BUSINESS LOGIC LAYER                          │
│                    FastAPI + Python Backend                      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    VoiceAgent Class                       │   │
│  │  - handle_connection()     - WebSocket message loop      │   │
│  │  - process_audio_after_silence() - VAD & processing     │   │
│  │  - transcribe_audio()       - STT (cloud/local)        │   │
│  │  - speak_text()             - TTS (ElevenLabs)          │   │
│  │  - get_llm_response()      - LLM (Groq/Gemini)         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  │
│  │ StateManager   │  │   providers    │  │   guardrails   │  │
│  │ - System prompt│  │ - GeminiProv  │  │ - Input valid. │  │
│  │ - Troubleshooting flow │  │ - GroqProv  │  │ - Output san. │  │
│  │ - Anti-jailbreak│  │ - get_provider │  │ - Toxicity    │  │
│  └────────────────┘  └────────────────┘  └────────────────┘  │
│                                                                  │
│  ┌────────────────┐  ┌───────────────────────────────────┐  │
│  │ DynamoDB       │  │  tools (RAG)                     │  │
│  │ Session Mgr    │  │ - Pinecone query                  │  │
│  │ - Save/Load   │  │ - Hallucination validation        │  │
│  │ - TTL 24h     │  │                                   │  │
│  └────────────────┘  └───────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↕
┌─────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                               │
│                                                                  │
│  ┌─────────────────┐              ┌─────────────────────────┐   │
│  │   AWS DynamoDB  │              │      Pinecone          │   │
│  │   (Sessions)    │              │    (Vector DB)         │   │
│  │                 │              │                        │   │
│  │ - session_id PK │              │ - 3072-dim embeddings  │   │
│  │ - chat_history  │              │ - Router manual chunks │   │
│  │ - troubleshooting_step          │ - Cosine similarity    │   │
│  │ - expiration_time│              │                        │   │
│  └─────────────────┘              └─────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow: Voice Conversation

```
1. USER SPEAKS
   Browser (Mic) ──recognition.onresult()──► {type: "audio", data: base64}
                                                 │
2. AUDIO RECEIVED (voice_handler.py)            │
   │                                                │
   ├─► audio_buffer.clear()      // Keep only latest audio
   │   audio_buffer.extend(audio_data)
   │
   ├─► silence_timer.cancel()     // Cancel previous timer
   │   asyncio.create_task(process_audio_after_silence())
   │
   └─► await asyncio.sleep(0.5)   // Wait for silence

3. SILENCE DETECTED - Processing Begins
   │
   ├─► processing_audio = True    // Prevent race conditions
   │
   ├─► Send {type: "status", status: "processing"}
   │   └─► Frontend stops mic, shows "processing"
   │
   ├─► TRANSCRIBE (cloud: Groq / local: faster-whisper)
   │   │
   │   ├─► STT Corrections (STT_CORRECTIONS dict)
   │   │      "daughter" → "router"
   │   │      "smother"  → "router"
   │   │
   │   └─► Gibberish Detection
   │          if <3 words AND no router keywords:
   │             ask clarification
   │
   ├─► GET LLM RESPONSE
   │   │
   │   ├─► chat_history.append({role: "user", text: user_text})
   │   │
   │   ├─► Build messages with system prompt + history
   │   │
   │   ├─► if provider == "gemini":
   │   │      genai_client.aio.models.generate_content()
   │   │   else (local):
   │   │      groq_client.chat.completions.create()
   │   │
   │   ├─► Check for [END_SESSION] marker
   │   │   └─► Trigger auto-disconnect
   │   │
   │   └─► session_manager.save_session()  // Persist to DynamoDB
   │
   ├─► Send {type: "transcript", role: "user", text: user_text}
   │   └─► Frontend displays user message
   │
   ├─► Send {type: "transcript", role: "model", text: response}
   │   └─► Frontend displays AI message
   │
   ├─► SPEAK TEXT (TTS)
   │   │
   │   ├─► strip_markdown_for_tts()        // Remove *, -, 1.
   │   │
   │   └─► elevenlabs_client.text_to_speech.convert()
   │          └─► {type: "audio", data: base64}
   │              └─► Frontend plays audio
   │
   └─► Send {type: "status", status: "processing_complete"}
       └─► Frontend enables mic for next input
```

### Session Persistence Flow

```
NEW SESSION:
Backend ────────► Frontend: session_created
     │
     save_state()
       └─► DynamoDB:
           session_id = uuid
           chat_history = []
           troubleshooting_step = "qualification"
           expiration_time = now + 24h

RECONNECTION (page refresh):
Frontend ────► Backend: sessionId query
     │              │
     │              load_session()
     │                └─► DynamoDB get_item
     │                    └─► chat_history, troubleshooting_step
     │◄───────────────────
     │              full_history
     └────────────────────
```

### State Machine (Conversation Flow)

```
                    ┌─────────────────┐
                    │  qualification  │ ◄─── Initial state
                    │  (start)        │
                    └────────┬────────┘
                             │
                             │ User describes issue
                             ▼
                    ┌─────────────────┐
          ┌────────►│ reboot_needed?  │
          │         └────────┬────────┘
          │                  │
          │ No               │ Yes
          │                  ▼
          │         ┌─────────────────┐
          │         │ ask_permission   │
          │         └────────┬────────┘
          │                  │
          │                  │ User agrees
          │                  ▼
          │         ┌─────────────────┐
          │         │ provide_reboot   │
          │         │ _instructions   │
          │         └────────┬────────┘
          │                  │
          │                  │ "reboot complete"
          │                  ▼
          │         ┌─────────────────────────┐
          │         │ awaiting_reconnect_      │◄─── User reboots router
          │         │ after_reboot            │
          │         └────────┬────────────────┘
          │                  │
          │                  │ User reconnects
          │                  ▼
          │         ┌─────────────────┐
          │         │   verification   │
          │         └────────┬────────┘
          │                  │
          │                  │ Issue resolved?
          │         ┌────────┴────────┐
          │         │                 │
          │    Resolved            Not resolved
          │         │                 │
          │         ▼                 ▼
          │  ┌─────────────┐  ┌─────────────┐
          │  │ wrap_up &   │  │ apologize & │
          │  │ farewell    │  │ escalate    │
          │  └──────┬──────┘  └──────┬─────┘
          │         │                 │
          │         ▼                 │
          │  [END_SESSION] ─────────►│
          │         │                 │
          └─────────┴─────────────────┘
                             │
                             ▼
                  ┌─────────────────┐
                  │   Disconnect     │
                  │   (5s countdown) │
                  └─────────────────┘
```

## Swappable Providers

The backend uses a provider abstraction layer (`backend/providers.py`) that allows swapping LLM providers seamlessly:

| Service                  | Cloud Provider          | Local Provider       | Config Location        |
| :----------------------- | :---------------------- | :------------------- | :--------------------- |
| **LLM & Reasoning**      | Google Gemini 2.5 Flash | Groq (Llama 3.3 70B) | **Frontend UI Toggle** |
| **Speech-to-Text (ASR)** | Groq (WhisperV3)        | `faster-whisper`     | Environment variable   |
| **Text-to-Speech (TTS)** | ElevenLabs              | -                    | -                      |
| **Vector DB (RAG)**      | Pinecone Serverless     | -                    | -                      |
| **Embeddings**           | Google Gemini           | -                    | -                      |

### Provider Implementation

- **GeminiProvider** (`providers.py`): Uses Google's `genai` SDK with proper message role handling
- **GroqProvider** (`providers.py`): Uses Groq's chat completions API
- **get_provider()**: Factory function that maps frontend labels to backend providers

## Project Structure

```
routemaster-ai/
├── backend/
│   ├── main.py              # FastAPI app entry point
│   ├── voice_handler.py     # VoiceAgent class + WebSocket handler
│   ├── state_manager.py     # System prompts + DynamoDB session manager
│   ├── providers.py         # LLM provider abstraction (Gemini/Groq)
│   ├── tools.py             # RAG pipeline with hallucination guardrails
│   └── guardrails/         # Input/output validation
│       ├── input_guardrails.py   # Prompt injection, PII masking
│       └── output_guardrails.py   # Toxicity detection, markdown stripping
├── config/
│   ├── __init__.py
│   └── settings.py          # Pydantic settings (environment variables)
├── frontend/
│   ├── src/
│   │   ├── App.tsx         # Main React component
│   │   ├── main.tsx        # React entry point
│   │   └── index.css        # Tailwind CSS styles
│   ├── package.json         # Node dependencies
│   ├── vite.config.ts       # Vite bundler config
│   ├── tailwind.config.js   # Tailwind CSS config
│   └── tsconfig.json        # TypeScript config
├── scripts/
│   ├── ingest_manual.py     # PDF → Pinecone vector DB ingestion
│   └── run_evals.py         # LLM-as-judge evaluation script
├── docs/                     # Router PDF manuals
├── requirements.txt          # Python dependencies
├── .env                      # Environment variables (API keys)
├── README.md                # This file
└── AGENT.md                 # AI developer context (for AI assistants)
```

## Setup Instructions

### 1. Prerequisites

- Python 3.11+ and Node.js
- AWS Account with credentials configured for DynamoDB access
- API keys for: Google AI Studio, Groq, ElevenLabs, and Pinecone

### 2. Configure Environment

Copy the `.env` file and add your API keys and AWS credentials.

```bash
# .env

# ASR Provider ("cloud" or "local")
# LLM provider is selected via the frontend toggle (Local: Groq, Cloud: Gemini)
ASR_PROVIDER="cloud"

# API Keys & Config
GOOGLE_API_KEY=your_google_key
GROQ_API_KEY=your_groq_key
ELEVENLABS_API_KEY=your_elevenlabs_key
PINECONE_API_KEY=your_pinecone_key
AWS_ACCESS_KEY_ID=your_aws_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
```

### 3. Create DynamoDB Table

Create a DynamoDB table named `routemaster-sessions` with `session_id` (String) as the primary key. Enable Time to Live (TTL) on an attribute named `expiration_time`.

```bash
# Create the table
aws dynamodb create-table \
    --table-name routemaster-sessions \
    --attribute-definitions \
        AttributeName=session_id,AttributeType=S \
    --key-schema \
        AttributeName=session_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST

# Enable TTL (sessions expire after 24 hours)
aws dynamodb update-time-to-live \
    --table-name routemaster-sessions \
    --time-to-live-specification \
        "Enabled=true, AttributeName=expiration_time"
```

### 4. Set Up Python Virtual Environment

```bash
# Create virtual environment
python -m venv venv

# Activate it
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate
```

### 5. Install Dependencies

```bash
# Main Application
pip install -r requirements.txt

# Frontend
cd frontend
npm install
```

### 6. Ingest the Manual (One-time setup)

Process the router's PDF manual to populate the Pinecone vector database for the RAG system.

```bash
python scripts/ingest_manual.py --pdf "docs/EA6350_UG_INTL_update.pdf"
```

This script:

- Extracts text and images from the PDF
- Uses Gemini to generate technical captions for diagrams
- Splits content into searchable chunks
- Embeds chunks with Gemini embeddings (3072 dimensions)
- Stores in Pinecone index `routemaster-manuals`

## Running the Application

You need to run two separate processes in two different terminals.

### Terminal 1: Start the Main Backend Server

This is the core application that handles WebSocket connections, session management, and AI logic.

```bash
uvicorn backend.main:app --port 8000
```

### Terminal 2: Start the React Frontend

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173` in your browser. Click "Connect to Agent" to start.

## Configuration Reference

### Environment Variables

| Variable                  | Description                                  | Default                   |
| ------------------------- | -------------------------------------------- | ------------------------- |
| `DEFAULT_PROVIDER`        | Default LLM provider ("gemini" or "groq")    | `gemini`                  |
| `GEMINI_MODEL_ID`         | Gemini model ID                              | `gemini-2.5-flash`        |
| `GROQ_MODEL_ID`           | Groq model ID                                | `llama-3.3-70b-versatile` |
| `ASR_PROVIDER`            | Speech-to-text provider ("cloud" or "local") | `cloud`                   |
| `GROQ_API_KEY`            | Groq API key (for STT and local LLM)         | -                         |
| `GOOGLE_API_KEY`          | Google AI Studio API key                     | -                         |
| `ELEVENLABS_API_KEY`      | ElevenLabs API key                           | -                         |
| `ELEVENLABS_VOICE_ID`     | ElevenLabs voice ID                          | `56bWURjYFHyYyVf490Dp`    |
| `ELEVENLABS_VOICE_MODEL`  | ElevenLabs TTS model                         | `eleven_flash_v2`         |
| `PINECONE_API_KEY`        | Pinecone API key                             | -                         |
| `PINECONE_INDEX_NAME`     | Pinecone index name                          | `routemaster-manuals`     |
| `PINECONE_ENVIRONMENT`    | Pinecone environment                         | `us-east-1`               |
| `AWS_ACCESS_KEY_ID`       | AWS access key                               | -                         |
| `AWS_SECRET_ACCESS_KEY`   | AWS secret key                               | -                         |
| `AWS_REGION`              | AWS region                                   | `us-east-1`               |
| `DYNAMODB_SESSIONS_TABLE` | DynamoDB sessions table                      | `routemaster-sessions`    |
| `BACKEND_PORT`            | Backend server port                          | `8000`                    |
| `FRONTEND_URL`            | Frontend URL for CORS                        | `http://localhost:5173`   |

## Troubleshooting

### Common Issues & Solutions

| Issue                                  | Cause                                | Solution                                                              |
| -------------------------------------- | ------------------------------------ | --------------------------------------------------------------------- |
| "Connect to Agent" does nothing        | Backend not running                  | Ensure `uvicorn backend.main:app --port 8000` is active               |
| Voice recording won't stop             | Silence detection delay              | Wait 0.5s after last speech; or refresh page                          |
| Double responses / Two agents speaking | Race condition in audio buffer       | Fixed with `processing_audio` flag and buffer clearing                |
| STT mishears "router" as "daughter"    | Whisper misrecognition               | Corrected via `STT_CORRECTIONS` dict automatically                    |
| Asterisks read aloud in TTS            | Markdown in LLM response             | Stripped via `strip_markdown_for_tts()` before TTS                    |
| Session lost on refresh                | DynamoDB not configured              | Verify AWS credentials and table `routemaster-sessions` exists        |
| RAG answers incorrect                  | Pinecone index empty                 | Run `python scripts/ingest_manual.py --pdf "docs/..."`                |
| ElevenLabs 401 error                   | Invalid API key or quota exceeded    | Check `.env` ELEVENLABS_API_KEY or add credits to ElevenLabs          |
| Groq 400 error                         | Invalid role format                  | Backend converts `"model"` → `"assistant"` for Groq API compatibility |
| Gibberish not detected                 | Short phrase with no router keywords | System asks for clarification on first instance                       |

### Debug Mode

Enable debug logging by setting the log level:

```python
# In voice_handler.py, line 22
logging.basicConfig(level=logging.DEBUG)
```

## RAG System Details

### How It Works

The RAG (Retrieval Augmented Generation) system provides accurate, grounded responses from the official router manual:

1. **Ingestion** (`scripts/ingest_manual.py`):
   - PDF is parsed with PyMuPDF
   - Images are extracted and captioned using Gemini Vision
   - Text is split into 2500-char chunks with 400-char overlap
   - Chunks are embedded with Google Gemini (3072 dimensions)
   - Stored in Pinecone with cosine similarity

2. **Query** (`backend/tools.py`):
   - User question is embedded
   - Top 3 most similar chunks retrieved from Pinecone
   - Gemini drafts answer from retrieved context
   - **Hallucination Guard**: Second Gemini call validates answer against source
   - Only returns answer if validation passes

3. **Guardrail Prompt**:
   ```
   Does the draft answer contain any steps, numbers, or instructions
   NOT explicitly present in the Original Source Text?
   Output PASS or FAIL.
   ```

### Why This Matters

Without hallucination guardrails, LLMs may confidently provide incorrect technical instructions that could damage hardware or mislead users. The two-step validation ensures all responses are grounded in official documentation.

## Evaluation

An LLM-as-judge evaluation script exists in `scripts/run_evals.py` for testing system responses against predefined personas and scenarios.

## Guardrails System

RouteMaster AI implements a comprehensive guardrails system to ensure safe and appropriate interactions:

### Input Guardrails (`backend/guardrails/input_guardrails.py`)

- **Prompt Injection Detection**: Monitors for attempts to override system instructions via patterns like "ignore previous instructions", injected system prompts, or template injection
- **Sensitive Content Masking**: Automatically masks PII such as SSNs, credit card numbers, and email addresses
- **Length Validation**: Enforces maximum text length (2000 characters) to prevent resource exhaustion

### Output Guardrails (`backend/guardrails/output_guardrails.py`)

- **Toxicity Filtering**: Detects and sanitizes potentially harmful instructions (e.g., instructions involving smoke, fire, electrical hazards)
- **Markdown Stripping**: Removes formatting syntax before TTS to ensure clean audio output
- **Warning Prefix**: When content is modified for safety, a warning prefix is added

### Behavioral Guardrails (`backend/state_manager.py`)

The system prompt includes guidelines for:

- **Refusal on Uncertainty**: Clearly state when information is uncertain or from general knowledge
- **Scope Limitation**: Only provide support for Linksys EA6350 router issues
- **Anti-Jailbreak**: Ignore attempts to change behavior or override guidelines
- **Privacy**: Never ask for or invent personal information

### Hazard Detection

For safety-critical scenarios, the LLM is instructed to immediately:

1. Instruct user to unplug the router
2. Advise contacting emergency services if needed
3. Terminate the call immediately

### Session Auto-Termination

When the LLM determines the conversation is complete, it includes `[END_SESSION]` marker in its response. The backend:

1. Detects the marker
2. Sends countdown message to frontend
3. Closes WebSocket after countdown
4. Clears session from memory

## Future Enhancements

### Deployment

- **Docker Containerization**: Package backend and frontend into Docker containers for easier deployment
- **AWS ECS Fargate**: Deploy to AWS ECS Fargate with Application Load Balancer for production scalability
- **Custom Domain**: Configure Route 53 and ACM certificate for custom domain with HTTPS

### Features

- **Multi-Router Support**: Extend beyond Linksys EA6350 to support additional router models
- **Conversation Analytics**: Store and analyze conversation patterns for continuous improvement
- **Multi-language Support**: Add support for non-English languages

### AI/ML Improvements

- **Fine-tuned RAG**: Fine-tune embedding model on router-specific technical documentation
- **Advanced Hallucination Detection**: Implement semantic similarity checks beyond keyword matching
- **Dynamic Troubleshooting Flows**: Use LLM to dynamically generate troubleshooting paths based on user input

### Security

- **WebSocket Rate Limiting**: Add connection and message rate limiting per session
- **API Authentication**: Add JWT or API key authentication for API endpoints
- **Input Sanitization**: Add additional sanitization layers for XSS and injection attacks

### Performance

- **Response Streaming**: Implement streaming TTS for faster perceived response times
- **Caching Layer**: Add Redis caching for frequently accessed RAG results
- **Connection Pooling**: Optimize database and API connection pooling
- **Audio Compression**: Implement audio compression for reduced bandwidth usage

## License

Proprietary - RouteThis Inc.
