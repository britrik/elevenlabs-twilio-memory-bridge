---
name: elevenlabs-twilio-memory-bridge
version: "1.1.0"
author: britrik
description: "FastAPI personalization webhook that adds persistent caller memory and dynamic context injection to ElevenLabs Conversational AI agents on Twilio. No audio proxying, file-based persistence, OpenClaw compatible."
tags: ["elevenlabs", "twilio", "voice-agent", "telephony", "conversational-ai", "memory-injection", "fastapi"]
emoji: ":telephone_receiver:"
---

# elevenlabs-twilio-memory-bridge

Personalization webhook service for ElevenLabs + Twilio voice agents with persistent caller memory.

## What It Does

When a call arrives on your Twilio number, ElevenLabs' native integration triggers this webhook. The bridge looks up the caller's history, loads long-term memory facts and daily context notes, combines them with a customizable soul/personality template, and returns everything as a system prompt override so your agent greets each caller with full context.

## Architecture

- **No audio proxying** - ElevenLabs and Twilio handle media directly
- **Webhook only** - called once per inbound call to inject context
- **File-based persistence** - JSON files in `./data/`, zero external dependencies
- **OpenClaw compatible** - works with any OpenAI-compatible LLM endpoint

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

## Soul Template

The included `soul_template.md` is a safe generic example containing no personally identifiable information. However, soul templates can contain sensitive personal details depending on how they are customized.

**Before deploying to production**, review and customize `soul_template.md` to match your agent's personality and use case. Ensure no private or sensitive information is embedded in the template unless you have appropriate access controls in place.

## Data Storage (DATA_DIR)

The service persists data as JSON files in a local directory.

- **Configuration**: Set the `DATA_DIR` environment variable to specify the storage directory.
- **Default value**: `./data/`
- **Stored data types**: Caller memory facts (per phone hash), context notes (global and caller-scoped), and session history.

### Filesystem Permissions

Restrict access to the `DATA_DIR` directory so only the service process user can read and write:

```bash
chmod 700 /path/to/data
```

Avoid running the service as root. Use a dedicated service account with minimal privileges.

### Encryption at Rest

The JSON files are stored unencrypted by default. If your deployment handles sensitive caller data, consider:

- Placing `DATA_DIR` on an encrypted filesystem (e.g., LUKS, dm-crypt, or cloud-provider volume encryption)
- Using a cloud storage backend with server-side encryption enabled

### Retention and Erasure

There is no automatic data retention or expiration policy built in. Operators should:

- Define a retention period appropriate for their use case and jurisdiction
- Implement a periodic cleanup process to delete stale data from `DATA_DIR`
- Support data erasure requests by deleting the relevant phone hash JSON files

## Required Environment Variables

- `ELEVENLABS_API_KEY` - scoped ElevenLabs key
- `ELEVENLABS_AGENT_ID` - your agent ID
- `OPENCLAW_API_BASE_URL` - your OpenClaw instance URL
- `PUBLIC_BASE_URL` - publicly reachable URL of this service
- `ADMIN_API_KEY` - secret key for authenticating requests to admin endpoints (`/api/memory/{phone_hash}`, `/api/notes`). Required for admin access.

## Optional Environment Variables

- `ALLOWED_ORIGINS` - comma-separated list of allowed CORS origins. When not set, CORS is disabled (appropriate for webhook-only deployments). Example: `https://dashboard.example.com,https://admin.example.com`

## Security

- All caller phone numbers are SHA-256 hashed before storage/logging
- Secrets loaded exclusively from environment variables
- Optional HMAC webhook signature verification (configure `WEBHOOK_SECRET`)
- Safe for public GitHub repos, no secrets in source

### Admin Endpoint Authentication

The admin endpoints (`/api/memory/{phone_hash}` and `/api/notes`) require authentication via the `ADMIN_API_KEY` environment variable. Requests must include an `Authorization: Bearer <key>` header.

- If `ADMIN_API_KEY` is configured and the request provides a valid Bearer token, the request is processed normally.
- If `ADMIN_API_KEY` is configured but the Authorization header is missing or invalid, the request is rejected with HTTP 401.
- If `ADMIN_API_KEY` is not configured, all admin endpoint requests are rejected with HTTP 403.

Webhook endpoints (`/webhook/personalize`, `/webhook/post-call`) and the `/health` endpoint do not require admin authentication.
