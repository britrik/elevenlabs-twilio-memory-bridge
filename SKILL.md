---
name: elevenlabs-twilio-memory-bridge
description: "FastAPI personalization webhook that adds persistent caller memory and dynamic context injection to ElevenLabs Conversational AI agents on Twilio. No audio proxying, file-based persistence, OpenClaw compatible."
version: "1.2.0"
author: britrik
tags: ["elevenlabs", "twilio", "voice-agent", "telephony", "conversational-ai", "memory-injection", "fastapi"]
metadata:
  openclaw:
    requires:
      env:
        - ELEVENLABS_API_KEY
        - ELEVENLABS_AGENT_ID
        - OPENCLAW_API_BASE_URL
        - PUBLIC_BASE_URL
        - ADMIN_API_KEY
      bins:
        - python3
        - pip
        - uvicorn
    primaryEnv: ELEVENLABS_API_KEY
    envVars:
      - name: ELEVENLABS_API_KEY
        required: true
        description: Scoped ElevenLabs API key for agent access.
      - name: ELEVENLABS_AGENT_ID
        required: true
        description: ElevenLabs Conversational AI Agent ID.
      - name: OPENCLAW_API_BASE_URL
        required: true
        description: Base URL of your OpenClaw instance (HTTPS).
      - name: PUBLIC_BASE_URL
        required: true
        description: Publicly reachable URL of this bridge service.
      - name: ADMIN_API_KEY
        required: true
        description: Secret key for admin endpoint authentication (Bearer token).
      - name: WEBHOOK_SECRET
        required: true
        description: Shared secret for webhook HMAC verification. Must be set in production.
      - name: PHONE_HASH_SALT
        required: true
        description: Salt for bcrypt phone number hashing. Generate with openssl rand -base64 32.
      - name: DATA_ENCRYPTION_KEY
        required: false
        description: Fernet key for encrypting memories and notes at rest. When unset, data is stored as plain JSON.
      - name: SOUL_TEMPLATE_PATH
        required: false
        description: Path to personality template file (default ./soul_template.md).
      - name: DATA_DIR
        required: false
        description: Directory for JSON persistence (default ./data).
      - name: ALLOWED_ORIGINS
        required: false
        description: Comma-separated CORS origins (leave unset to disable CORS).
      - name: HOST
        required: false
        description: Bind address for uvicorn (default 0.0.0.0).
      - name: PORT
        required: false
        description: Listen port (default 8000).
      - name: LOG_LEVEL
        required: false
        description: Python logging level (default INFO).
    emoji: "\U0001F4DE"
    homepage: https://github.com/britrik/elevenlabs-twilio-memory-bridge
---

# elevenlabs-twilio-memory-bridge

Personalization webhook service for ElevenLabs + Twilio voice agents with persistent caller memory.

## What It Does

When a call arrives on your Twilio number, ElevenLabs' native integration triggers this webhook. The bridge looks up the caller's history, loads long-term memory facts and daily context notes, combines them with a customizable soul/personality template, and returns everything as a system prompt override so your agent greets each caller with full context.

## Architecture

- **No audio proxying** — ElevenLabs and Twilio handle media directly
- **Webhook only** — called once per inbound call to inject context
- **File-based persistence** — JSON files in `./data/`, zero external dependencies
- **OpenClaw compatible** — works with any OpenAI-compatible LLM endpoint

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhook/personalize` | POST | ElevenLabs calls this on inbound call |
| `/webhook/post-call` | POST | Optional post-call cleanup |
| `/api/memory/{phone_hash}` | POST | Add long-term fact about a caller |
| `/api/notes` | POST | Add global or caller-scoped context note |
| `/health` | GET | Health check |

## Setup

1. Clone repo, `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`, fill in secrets
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
- `ADMIN_API_KEY` — secret for admin endpoint auth

## Optional Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PHONE_HASH_SALT` | _(unset)_ | **Required.** Salt for bcrypt phone number hashing. Generate with `openssl rand -base64 32`. Changing this invalidates all existing caller data. |
| `DATA_ENCRYPTION_KEY` | _(unset)_ | Fernet key for encrypting memories and notes at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. When unset, data is stored as plain JSON. |
| `WEBHOOK_SECRET` | _(unset)_ | **Required.** Shared secret for HMAC webhook verification. |
| `SOUL_TEMPLATE_PATH` | `./soul_template.md` | Path to personality template file |
| `DATA_DIR` | `./data` | Directory for JSON persistence |
| `ALLOWED_ORIGINS` | _(unset)_ | Comma-separated CORS origins |
| `HOST` | `0.0.0.0` | Bind address for uvicorn |
| `PORT` | `8000` | Listen port |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Security

- **bcrypt phone hashing** — Phone numbers are hashed with bcrypt (salted, slow) before storage. The salt is set via `PHONE_HASH_SALT` env var. Changing the salt invalidates existing caller data.
- **Encryption at rest** — Memories and notes are encrypted with Fernet when `DATA_ENCRYPTION_KEY` is set. Data is stored as plain JSON when the key is not configured.
- **Mandatory webhook verification** — `WEBHOOK_SECRET` is required in production. Webhooks are rejected when the secret is not set (fail-closed).
- **Rate limiting** — Admin endpoints (`/api/memory`, `/api/notes`) are limited to 30 requests per minute. Webhook endpoints are limited to 60 per minute.
- **Input sanitization** — Memory facts and notes are HTML-escaped before storage to prevent XSS.
- **File locking** — JSON reads and writes use POSIX advisory locks (`fcntl`) to prevent race conditions under concurrent access.
- **Restrictive permissions** — Data directory (`DATA_DIR`) is created with `0o700` permissions. JSON files are written with `0o600`.
- **Secrets from env vars only** — No hardcoded credentials in source.
- **Safe for public GitHub repos** — All sensitive config is loaded from environment variables.

## Migration from v1.1.x to v1.2.x

v1.2.0 introduces **breaking security changes**:

1. **PHONE_HASH_SALT is now required** — Previously, phone numbers were hashed with unsalted SHA-256. Now bcrypt with a salt is required. **Existing caller data will be unreachable** after upgrading because the hash keys change. You must either:
   - Accept data loss and start fresh, or
   - Run the provided migration script to re-hash existing data with the new algorithm.

2. **WEBHOOK_SECRET is now required** — Previously optional for development. Now mandatory in all environments. Webhooks without a valid secret are rejected.

3. **DATA_ENCRYPTION_KEY is optional but recommended** — When set, all new memories and notes are encrypted at rest. Existing unencrypted data remains readable.

### Migration checklist

1. Generate a salt: `openssl rand -base64 32`
2. Generate an encryption key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
3. Add both to `.env`
4. Review CORS settings — `ALLOWED_ORIGINS` now defaults to disabled
5. Update ElevenLabs webhook settings with the new `WEBHOOK_SECRET`
