# 🔮 BeauHoroscope — Telegram-бот астропрогнозов

Бесплатный production-пайплайн ежедневных астропрогнозов на русском языке.

## Production 4.0

Текущий production entrypoint — `main.py`.

- реальные астрономические эфемериды рассчитываются локально через **PyEphem**;
- LLM: **Groq / Qwen** как основной провайдер;
- **Mistral** — fallback;
- Hugging Face полностью исключён из runtime-пайплайна;
- ежедневный гороскоп и Content Engine работают в одном Telegram-сервисе;
- единый scheduler поддерживает автопостинг гороскопов и дополнительных астрологических материалов;
- SQLite хранит историю, расписание, настройки и защищает от повторной публикации;
- `/llmtest` проверяет Groq и Mistral независимо;
- API-ключи хранятся только в `.env`/environment.

> Production snapshot 4.0: `BeauHoroscope-4.0-groq-mistral.tar.gz`.

## Архитектура

```text
PyEphem
   │
   ▼
Астрономический snapshot
   │
   ▼
Groq / Qwen
   │
   ├── success ──────────┐
   │                     │
   └── error/rate-limit  ▼
           Mistral ─────► Validation
                              │
                              ▼
                         Telegram
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
          Daily Horoscope            Content Engine
                 │                         │
                 └──────── Scheduler ─────┘
```

LLM не рассчитывает эфемериды самостоятельно: фактический астрономический snapshot сначала строится локально, затем передаётся модели для редакционной интерпретации.

## Требования

- Python 3.10+;
- Telegram-бот с правами публикации в канале;
- Groq API key и/или Mistral API key.

**HF Token не требуется.**

## Переменные окружения

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@tvoigoroskopchik
ADMIN_CHAT_ID=...

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

Не коммитьте `.env`, токены, SQLite-базы, логи и резервные копии.

## Установка

```bash
git clone https://github.com/gotock-crypto/horoscope_free_pipeline.git
cd horoscope_free_pipeline
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 -m py_compile main.py
python3 main.py
```

## systemd

Production-сервис запускает:

```ini
[Service]
User=root
WorkingDirectory=/opt/horoscope-bot
EnvironmentFile=/opt/horoscope-bot/.env
Environment="PATH=/opt/horoscope-bot/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/horoscope-bot/venv/bin/python3 /opt/horoscope-bot/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=horoscope-bot
```

Проверка:

```bash
systemctl status horoscope-bot.service --no-pager
journalctl -u horoscope-bot.service -f
```

## Команды

- `/start` — панель администратора;
- `/menu` — панель администратора;
- `/id` — Telegram ID;
- `/llmtest` — отдельная проверка Groq и Mistral.

Через панель можно создавать прогнозы вручную, планировать публикации, включать ежедневный автопостинг и управлять Content Engine.

## Важное ограничение бесплатного Groq

Бесплатный Groq может возвращать HTTP 429 при превышении TPM. В production 4.0 предусмотрен fallback на Mistral. Следующая оптимизация 4.1 — уменьшение размера LLM-запросов и более экономная стратегия повторов.

## Безопасность

Никогда не храните реальные значения `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY` или `MISTRAL_API_KEY` в Git. Если секрет был опубликован или раскрыт, его следует немедленно отозвать и перевыпустить.

## Примечание

Астрологические прогнозы являются развлекательной интерпретацией астрологических традиций и не являются научными предсказаниями будущего.
