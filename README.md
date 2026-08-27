# BeauHoroscope 4.0 — Groq + Mistral

## Что изменено

- Hugging Face полностью удалён из runtime-пайплайна.
- Основной LLM: Groq `qwen/qwen3.6-27b`.
- Fallback: Mistral `mistral-small-latest`.
- Оба API вызываются через обычный OpenAI-compatible `/chat/completions`.
- Один общий LLM adapter используется и ежедневным гороскопом, и Content Engine.
- Добавлен `LLM_LOCK`, чтобы scheduler не запускал несколько внешних LLM-запросов одновременно.
- `/llmtest` проверяет Groq и Mistral отдельно.
- Scheduler остаётся одним: Content Engine + ежедневный Horoscope работают в одном `horoscope-bot.service`.
- `AUTO_POST_DEFAULT=1` для новой установки. Существующее значение `auto_enabled` в SQLite сохраняется.
- При ошибке генерации `auto_last_date` не обновляется, поэтому следующий проход scheduler повторяет попытку.
- Существующая SQLite не заменяется архивом: при деплое сохраняется production DB.

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

Telegram и существующие настройки проекта остаются без изменений.

## Production deployment

1. Сделать backup `/opt/horoscope-bot` и отдельно `horoscopes.db`.
2. Распаковать только код/requirements, не заменяя `.env` и `horoscopes.db`.
3. Добавить `GROQ_API_KEY` и `MISTRAL_API_KEY` в `/opt/horoscope-bot/.env`.
4. Проверить `python3 -m py_compile main.py`.
5. Перезапустить `horoscope-bot.service`.
6. Выполнить `/llmtest` в Telegram.
7. Проверить `journalctl -u horoscope-bot.service -f`.
8. Для проверки автоматического гороскопа можно временно выставить `auto_time` на ближайшие минуты через существующее меню/SQLite, затем вернуть `23:00`.

## Безопасность

Не включать HF_TOKEN. Не коммитить `.env` и SQLite в Git. API-ключи хранить только в environment/secrets.
