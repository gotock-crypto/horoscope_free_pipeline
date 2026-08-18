# 🔮 BeauHoroscope — Telegram-бот астропрогнозов

Бесплатный Telegram-пайплайн для ежедневных гороскопов на русском языке.

Проект сочетает **реальные астрономические эфемериды**, рассчитанные локально через PyEphem, и LLM через **Hugging Face Inference Providers**.

## Возможности

- 🌙 расчёт положения Луны и планет по эфемеридам;
- ♈️ персонализированная интерпретация для всех 12 знаков зодиака;
- 🔭 расчёт ретроградности и основных аспектов;
- 🤖 генерация текста через Hugging Face;
- 🛡️ проверка ответа LLM: должны присутствовать все 12 знаков;
- 📢 публикация готового красиво оформленного поста прямо в Telegram-канал;
- 📅 планирование публикации на конкретную дату и время;
- ⏰ ежедневный автопостинг;
- 📋 просмотр и отмена запланированных публикаций;
- 📊 простая статистика публикаций;
- 🧪 команда `/llmtest` для проверки LLM;
- 💾 SQLite для хранения публикаций, расписания и настроек;
- 🔐 секреты не хранятся в исходном коде — используются переменные окружения.

## Архитектура

```text
PyEphem
   │
   ▼
Астрономический snapshot
   │
   ▼
Hugging Face Inference Providers
   │
   ▼
Валидация 12 знаков
   │
   ▼
Красивый Telegram-пост
   │
   ├── публикация сейчас
   ├── планировщик
   └── ежедневный автопостинг
```

LLM **не рассчитывает эфемериды самостоятельно**. Фактические астрономические данные сначала рассчитываются локально, после чего передаются модели для интерпретации.

## Требования

- Python 3.10+;
- Telegram-бот;
- права администратора у бота в целевом Telegram-канале;
- Hugging Face User Access Token с доступом к Inference Providers.

## Установка

```bash
git clone https://github.com/gotock-crypto/horoscope_free_pipeline.git
cd horoscope_free_pipeline
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Переменные окружения

Создайте `.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_ID=@tvoigoroskopchik
ADMIN_CHAT_ID=

HF_TOKEN=
HF_MODEL=Qwen/Qwen3-8B
HF_PROVIDER=auto
HF_MAX_TOKENS=2200
HF_TEMPERATURE=0.72

BOT_TIMEZONE=Europe/Moscow
AUTO_POST_DEFAULT=0
AUTO_POST_TIME=09:00
DB_FILE=horoscopes.db
```

### Что означает `ADMIN_CHAT_ID`

Это **числовой Telegram ID администратора**, которому доступна панель управления.

После запуска можно отправить боту:

```text
/id
```

Бот вернёт ваш ID.

## Hugging Face Token

В настройках Hugging Face создайте **User Access Token**, которому разрешены вызовы Inference Providers.

После этого добавьте его только в `.env`:

```env
HF_TOKEN=ваш_токен
```

Не добавляйте токен в Git, README, исходный код или логи.

## Запуск

```bash
source venv/bin/activate
python3 horoscope_free_pipeline_v7_adminpublish.py
```

## Команды администратора

| Команда | Назначение |
|---|---|
| `/start` | открыть панель управления |
| `/menu` | открыть панель управления |
| `/id` | показать Telegram ID |
| `/llmtest` | проверить доступность LLM |

## Панель управления

В панели доступны:

### 🚀 Создать прогноз

Выбираете дату:

```text
сегодня
завтра
19.08.2026
```

Бот рассчитывает эфемериды, генерирует прогноз и **сразу публикует готовый пост в канал**.

### 📅 Запланировать

Можно создать прогноз и указать точное время публикации, например:

```text
19.08.2026 09:00
```

Время интерпретируется в `BOT_TIMEZONE`.

### 📋 Расписание

Показывает запланированные публикации и позволяет отменить их.

### ⏰ Автопостинг

Можно включить ежедневную автоматическую публикацию и задать время, например `09:00`.

### 📊 Статистика

Показывает количество опубликованных, запланированных и неудачных публикаций.

## Формат публикации

Пост автоматически оформляется примерно так:

```text
🔮 АСТРОПРОГНОЗ
19.08.2026

🌙 Растущий серп • Луна в Скорпионе

━━━━━━━━━━━━━━━━━━━━

♈️ ОВЕН: ...
♉️ ТЕЛЕЦ: ...
...
♓️ РЫБЫ: ...

━━━━━━━━━━━━━━━━━━━━
#гороскоп #астропрогноз #зодиак #луна
```

Название модели и технические данные **не публикуются в канале**.

## Безопасность

В репозитории не должны находиться:

- `TELEGRAM_BOT_TOKEN`;
- `HF_TOKEN`;
- `ADMIN_CHAT_ID`, если вы не хотите публиковать его;
- `.env`;
- локальная SQLite-база;
- резервные копии с секретами.

Рекомендуемый `.gitignore`:

```gitignore
.env
*.db
*.sqlite
*.sqlite3
__pycache__/
*.pyc
venv/
.venv/
*.log
```

## systemd

Для постоянного запуска на Ubuntu создайте `/etc/systemd/system/horoscope-bot.service`:

```ini
[Unit]
Description=Telegram Horoscope Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/horoscope-bot
EnvironmentFile=/opt/horoscope-bot/.env
Environment="PATH=/opt/horoscope-bot/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/horoscope-bot/venv/bin/python3 /opt/horoscope-bot/horoscope_free_pipeline_v7_adminpublish.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=horoscope-bot

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
systemctl daemon-reload
systemctl enable horoscope-bot.service
systemctl restart horoscope-bot.service
systemctl status horoscope-bot.service --no-pager
```

Логи:

```bash
journalctl -u horoscope-bot.service -f
```

## Проверка перед деплоем

```bash
python3 -m py_compile horoscope_free_pipeline_v7_adminpublish.py
```

Проверить, что в исходнике нет случайно записанных секретов:

```bash
grep -nEi 'TELEGRAM_BOT_TOKEN\s*=\s*["'"']?[0-9]{8,}:|HF_TOKEN\s*=\s*["'"'][^"'"']{20,}|sk-[A-Za-z0-9_-]{20,}' horoscope_free_pipeline_v7_adminpublish.py
```

Команда не должна находить реальные значения секретов.

## Важно

Астрологические прогнозы являются развлекательной интерпретацией астрологических традиций и не являются научными предсказаниями будущего.
