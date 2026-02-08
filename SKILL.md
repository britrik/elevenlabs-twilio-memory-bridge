---
name: elevenlabs-twilio-memory-bridge
version: "1.0.0"
author: britrik
description: "Lightweight FastAPI bridge that adds persistent caller memory, dynamic context injection, and session continuity to ElevenLabs Conversational AI agents connected via Twilio. Uses ElevenLabs native Twilio integration with a personalization webhook — no audio proxying. Supports OpenClaw or any OpenAI-compatible LLM backend, Cloudflare Tunnel, and file-based persistence."
tags: ["elevenlabs", "twilio", "voice-agent", "telephony", "conversational-ai", "memory-injection", "persistent-session", "long-term-memory", "openclaw", "fastapi"]
emoji: "📞"
---

# elevenlabs-twilio-memory-bridge

Personalization webhook service for ElevenLabs + Twilio voice agents with persistent caller memory.

## What It Does

When a call arrives on your Twilio number, ElevenLabs' native integration triggers this webhook. The bridge looks up the caller's history, loads long-term memory facts and daily context notes, combines them with a customizable soul/personality template, and returns everything as a system prompt override — so your agent greets each caller with full context.

## Architecture

- **No audio proxying** — ElevenLabs ↔ Twilio handle media directly
- **Webhook only** — called once per inbound call to inject context
- **File-based persistence** — JSON files in `./data/`, zero external dependencies
- **OpenClaw compatible** — works with any OpenAI-compatible LLM endpoint

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhook/personalize` | POST | ElevenLabs calls this on inbound call — returns dynamic variables + system prompt override |
| `/webhook/post-call` | POST | Optional post-call cleanup |
| `/api/memory/{phone_hash}` | POST | Add long-term fact about a caller |
| `/api/notes` | POST | Add global or caller-scoped context note |
| `/health` | GET | Health check |

## Setup

1. Clone repo, `pip install -r requirements.txt`
2. Copy `.env.example` → `.env`, fill in secrets
3. Configure ElevenLabs agent with Custom LLM pointing to your OpenClaw instance
4. Enable system prompt + first message overrides in agent Security settings
5. Add webhook URL `https://your-domain/webhook/personalize` in ElevenLabs settings
6. Import Twilio number in ElevenLabs dashboard
7. Run: `uvicorn app:app --host 0.0.0.0 --port 8000`

## Required Environment Variables

- `ELEVENLABS_API_KEY` — scoped ElevenLabs key
- `ELEVENLABS_AGENT_ID` — your agent ID
- `OPENCLAW_API_BASE_URL` — your OpenClaw instance URL
- `PUBLIC_BASE_URL` — publicly reachable URL of this service

## Security

- All caller phone numbers are SHA-256 hashed before storage/logging
- Secrets loaded exclusively from environment variables
- Optional HMAC webhook signature verification
- Safe for public GitHub repos — no secrets in source
