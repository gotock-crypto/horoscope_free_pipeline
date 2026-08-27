# BeauHoroscope 4.0 — Groq + Mistral

Production snapshot: 2026-08-27.

## What changed

- Hugging Face removed from the runtime LLM pipeline.
- Primary LLM: Groq `qwen/qwen3.6-27b`.
- Fallback: Mistral `mistral-small-latest`.
- Both providers use OpenAI-compatible `/chat/completions` endpoints.
- One LLM adapter is shared by daily Horoscope and Content Engine.
- A single scheduler runs Horoscope + Content Engine in `horoscope-bot.service`.
- `LLM_LOCK` prevents overlapping external LLM requests.
- `/llmtest` checks Groq and Mistral independently.
- Failed horoscope generation does not advance the last-success date, so the scheduler can retry later.
- Existing production SQLite is preserved during deployment.

## Environment

```env
GROQ_API_KEY=...
GROQ_MODEL=qwen/qwen3.6-27b
GROQ_MAX_TOKENS=2200
GROQ_TEMPERATURE=0.72

MISTRAL_API_KEY=...
MISTRAL_MODEL=mistral-small-latest
MISTRAL_MAX_TOKENS=2200
MISTRAL_TEMPERATURE=0.72

LLM_TIMEOUT=90
LLM_RETRIES_PER_PROVIDER=1
```

## Production archive

Exact production archive:

`BeauHoroscope-4.0-groq-mistral.tar.gz`

SHA-256:

`56fdf284ccff942a1e6bb258f0513f4c71482a3d46ab1c98800fd8f87862ef01`

## Deployment rule

Do not replace `.env` or `horoscopes.db` when deploying the archive. Back up both first.

## Known production observation

On 2026-08-27 the production service successfully passed `/llmtest` for both providers and successfully published Content Engine posts. Groq may return HTTP 429 when the free TPM limit is reached; the production fallback to Mistral was observed working. A future 4.1 optimization should reduce prompt/token consumption and avoid repeated Groq retries after rate-limit responses.

## Security

Never commit Telegram tokens or LLM API keys. Revoke and rotate any secret that has been exposed.
