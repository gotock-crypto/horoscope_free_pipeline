"""
BeauHoroscope — бесплатный пайплайн ежедневных астропрогнозов.

Архитектура:
1. Реальные эфемериды рассчитываются локально через PyEphem.
2. Из эфемерид строится структурированный астрологический snapshot:
   положения планет, знаки, ретроградность, фаза Луны и основные аспекты.
3. LLM получает только этот snapshot и правила редакции.
4. Ответ валидируется: ровно 12 знаков, без лишних разделов, без выдуманных
   астрономических фактов.
5. Telegram публикует готовый текст.
6. SQLite гарантирует идемпотентность по дате.

LLM работает через Hugging Face Inference Providers. Telegram-секреты и HF_TOKEN — только через переменные окружения.
"""

import os
import re
import json
import time
import uuid
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo
from html import escape

import requests
import ephem
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@tvoigoroskopchik")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

# LLM: Hugging Face Inference Providers.
# API-ключ хранится только в .env и никогда не выводится в логи.
HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen3-8B").strip()
HF_MAX_TOKENS = int(os.getenv("HF_MAX_TOKENS", "2200"))
HF_TEMPERATURE = float(os.getenv("HF_TEMPERATURE", "0.72"))
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_PROVIDER = os.getenv("HF_PROVIDER", "auto").strip() or "auto"

DB_FILE = os.getenv("DB_FILE", "horoscopes.db")
CONFIG_FILE = os.getenv("CONFIG_FILE", "bot_config.json")

POST_HOUR_UTC = int(os.getenv("POST_HOUR_UTC", "6"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("beauhoroscope")


# ============================================================
# ZODIAC / ASTROLOGY
# ============================================================

ZODIAC = [
    ("♈️", "ОВЕН", "Овна", "Марс"),
    ("♉️", "ТЕЛЕЦ", "Тельца", "Венера"),
    ("♊️", "БЛИЗНЕЦЫ", "Близнецов", "Меркурий"),
    ("♋️", "РАК", "Рака", "Луна"),
    ("♌️", "ЛЕВ", "Льва", "Солнце"),
    ("♍️", "ДЕВА", "Девы", "Меркурий"),
    ("♎️", "ВЕСЫ", "Весов", "Венера"),
    ("♏️", "СКОРПИОН", "Скорпиона", "Плутон"),
    ("♐️", "СТРЕЛЕЦ", "Стрельца", "Юпитер"),
    ("♑️", "КОЗЕРОГ", "Козерога", "Сатурн"),
    ("♒️", "ВОДОЛЕЙ", "Водолея", "Уран"),
    ("♓️", "РЫБЫ", "Рыб", "Нептун"),
]

PLANETS = {
    "Солнце": ephem.Sun,
    "Луна": ephem.Moon,
    "Меркурий": ephem.Mercury,
    "Венера": ephem.Venus,
    "Марс": ephem.Mars,
    "Юпитер": ephem.Jupiter,
    "Сатурн": ephem.Saturn,
    "Уран": ephem.Uranus,
    "Нептун": ephem.Neptune,
    "Плутон": ephem.Pluto,
}

SIGN_NAMES = [
    "Овне", "Тельце", "Близнецах", "Раке", "Льве", "Деве",
    "Весах", "Скорпионе", "Стрельце", "Козероге", "Водолее", "Рыбах",
]

SIGN_INDEX = {
    "ОВЕН": 0, "ТЕЛЕЦ": 1, "БЛИЗНЕЦЫ": 2, "РАК": 3,
    "ЛЕВ": 4, "ДЕВА": 5, "ВЕСЫ": 6, "СКОРПИОН": 7,
    "СТРЕЛЕЦ": 8, "КОЗЕРОГ": 9, "ВОДОЛЕЙ": 10, "РЫБЫ": 11,
}

ASPECTS = [
    (0, "соединение", 7),
    (60, "секстиль", 5),
    (90, "квадрат", 6),
    (120, "тригон", 6),
    (180, "оппозиция", 7),
]


def utc_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ecliptic_longitude(body, observer) -> float:
    body.compute(observer)
    ecl = ephem.Ecliptic(body)
    return float(ecl.lon) * 180.0 / 3.141592653589793


def zodiac_from_longitude(lon: float) -> Tuple[int, str]:
    idx = int((lon % 360.0) // 30.0)
    return idx, SIGN_NAMES[idx]


def angular_distance(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def get_ephemeris_snapshot(target_date: datetime) -> Dict:
    target_date = utc_datetime(target_date).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    observer = ephem.Observer()
    observer.date = target_date

    positions = {}
    for name, cls in PLANETS.items():
        try:
            body = cls()
            lon = ecliptic_longitude(body, observer)
            sign_idx, sign_name = zodiac_from_longitude(lon)

            prev_observer = ephem.Observer()
            prev_observer.date = target_date - timedelta(days=1)
            next_observer = ephem.Observer()
            next_observer.date = target_date + timedelta(days=1)

            prev_lon = ecliptic_longitude(cls(), prev_observer)
            next_lon = ecliptic_longitude(cls(), next_observer)
            daily_motion = ((next_lon - prev_lon + 540.0) % 360.0) - 180.0
            retrograde = daily_motion < -0.01

            positions[name] = {
                "longitude": round(lon, 3),
                "degree": round(lon % 30.0, 2),
                "sign_index": sign_idx,
                "sign": sign_name,
                "daily_motion": round(daily_motion, 3),
                "retrograde": retrograde,
            }
        except Exception as exc:
            logger.exception("Ошибка расчёта %s: %s", name, exc)

    moon_phase = get_moon_phase(observer)
    aspects = calculate_aspects(positions)

    return {
        "date_utc": target_date.isoformat(),
        "moon_phase": moon_phase,
        "positions": positions,
        "aspects": aspects,
    }


def get_moon_phase(observer) -> Dict:
    moon = ephem.Moon(observer)
    moon.compute(observer)
    illumination = float(moon.phase)
    age = float(ephem.Moon(observer).elong) * 180.0 / 3.141592653589793

    if illumination < 5:
        name = "Новолуние"
    elif illumination < 45:
        name = "Растущий серп"
    elif illumination < 55:
        name = "Первая четверть"
    elif illumination < 95:
        name = "Растущая Луна"
    elif illumination >= 99:
        name = "Полнолуние"
    elif illumination >= 55:
        name = "Убывающая Луна"
    else:
        name = "Лунная фаза"

    return {
        "name": name,
        "illumination_percent": round(illumination, 1),
        "elongation_deg": round(age, 2),
    }


def calculate_aspects(positions: Dict) -> List[Dict]:
    names = list(positions.keys())
    result = []

    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            a = positions[a_name]["longitude"]
            b = positions[b_name]["longitude"]
            distance = angular_distance(a, b)

            for exact, aspect_name, orb in ASPECTS:
                delta = abs(distance - exact)
                if delta <= orb:
                    result.append({
                        "a": a_name,
                        "b": b_name,
                        "aspect": aspect_name,
                        "exact": exact,
                        "orb": round(delta, 2),
                        "distance": round(distance, 2),
                    })
                    break

    return sorted(result, key=lambda x: x["orb"])


def build_sign_context(snapshot: Dict) -> Dict[str, Dict]:
    positions = snapshot["positions"]
    aspects = snapshot["aspects"]
    result = {}

    for emoji, name, in_case, ruler in ZODIAC:
        relevant = []
        for planet, data in positions.items():
            if data["sign_index"] == SIGN_INDEX[name]:
                relevant.append(
                    f"{planet} в {data['degree']:.1f}° {data['sign']}"
                    + (" (ретроградно)" if data["retrograde"] else "")
                )

        ruler_aspects = [a for a in aspects if a["a"] == ruler or a["b"] == ruler][:5]

        result[name] = {
            "emoji": emoji,
            "ruler": ruler,
            "planets_in_sign": relevant,
            "ruler_aspects": ruler_aspects,
        }

    return result


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_horoscopes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_date TEXT NOT NULL UNIQUE,
                horoscope_text TEXT NOT NULL,
                astro_snapshot TEXT,
                provider TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                target_date TEXT NOT NULL,
                post_text TEXT NOT NULL,
                astro_snapshot TEXT,
                provider TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id TEXT PRIMARY KEY,
                target_date TEXT NOT NULL,
                publish_at_utc TEXT NOT NULL,
                post_text TEXT NOT NULL,
                astro_snapshot TEXT,
                provider TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at TEXT NOT NULL,
                published_at TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def is_horoscope_sent(target_date: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM sent_horoscopes WHERE target_date=?",
            (target_date,),
        ).fetchone() is not None


def mark_horoscope_sent(target_date: str, horoscope_text: str, snapshot: Dict, provider: str) -> bool:
    try:
        with db() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO sent_horoscopes
                    (target_date, horoscope_text, astro_snapshot, provider, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                target_date,
                horoscope_text,
                json.dumps(snapshot, ensure_ascii=False),
                provider,
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            return conn.execute(
                "SELECT 1 FROM sent_horoscopes WHERE target_date=?",
                (target_date,),
            ).fetchone() is not None
    except Exception:
        logger.exception("Ошибка записи публикации")
        return False


# ============================================================
# LLM PROVIDER — HUGGING FACE INFERENCE PROVIDERS
# ============================================================

def _clean_llm_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    return text.strip()


def query_huggingface(prompt: str) -> Optional[str]:
    if not HF_TOKEN:
        logger.error("HF_TOKEN is not configured")
        return None

    try:
        from huggingface_hub import InferenceClient
    except Exception as exc:
        logger.error("huggingface_hub unavailable: %s", exc)
        return None

    try:
        client = InferenceClient(provider=HF_PROVIDER, api_key=HF_TOKEN)
        completion = client.chat.completions.create(
            model=HF_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты профессиональный русскоязычный редактор астропрогнозов. "
                        "Верни только готовый текст без пояснений, рассуждений, "
                        "markdown-кода и тегов <think>. Используй астрономический "
                        "snapshot как единственный источник фактических астрономических данных."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=HF_MAX_TOKENS,
            temperature=HF_TEMPERATURE,
        )
        text = _clean_llm_text(completion.choices[0].message.content or "")
        if text:
            logger.info("LLM success: provider=%s model=%s", HF_PROVIDER, HF_MODEL)
            return text
        logger.warning("LLM returned empty response")
        return None
    except Exception as exc:
        logger.exception("Hugging Face inference failed: %s", str(exc)[:500])
        return None


def query_huggingface_space(prompt: str) -> Optional[str]:
    return query_huggingface(prompt)


# ============================================================
# PROMPT + VALIDATION
# ============================================================

def create_editorial_prompt(target_date: datetime, snapshot: Dict) -> str:
    sign_context = build_sign_context(snapshot)
    compact_positions = []
    for planet, data in snapshot["positions"].items():
        compact_positions.append(
            f"- {planet}: {data['degree']:.1f}° {data['sign']}; "
            f"суточное движение {data['daily_motion']:+.3f}°"
            + ("; ретроградно" if data["retrograde"] else "")
        )

    aspects = "\n".join(
        f"- {a['a']} — {a['aspect']} — {a['b']} (орб {a['orb']:.2f}°)"
        for a in snapshot["aspects"][:12]
    ) or "- Точных основных аспектов в заданном орбе нет."

    per_sign = []
    for _, name, _, ruler in ZODIAC:
        ctx = sign_context[name]
        in_sign = ", ".join(ctx["planets_in_sign"]) or "нет планет"
        ruler_aspects = ", ".join(
            f"{a['a']} {a['aspect']} {a['b']}" for a in ctx["ruler_aspects"]
        ) or "нет близких аспектов управителя"
        per_sign.append(
            f"{name} (управитель: {ruler}): планеты в знаке: {in_sign}; "
            f"аспекты управителя: {ruler_aspects}"
        )

    return f"""
Создай ежедневный астрологический прогноз на {target_date.strftime('%d.%m.%Y')}.

Все астрономические данные уже рассчитаны программой. Твоя задача — сделать качественную астрологическую интерпретацию этих данных для 12 солнечных знаков.

АСТРОНОМИЧЕСКИЙ SNAPSHOT:
Лунная фаза: {snapshot['moon_phase']['name']}
Освещённость Луны: {snapshot['moon_phase']['illumination_percent']}%

ПОЛОЖЕНИЯ ПЛАНЕТ:
{chr(10).join(compact_positions)}

ОСНОВНЫЕ АСПЕКТЫ:
{aspects}

КОНТЕКСТ ПО ЗНАКАМ:
{chr(10).join(per_sign)}

РЕДАКЦИОННЫЕ ПРАВИЛА:
1. Дай ровно 12 прогнозов, по порядку от ОВНА до РЫБ.
2. Каждый прогноз — 2–3 естественных предложения.
3. Не повторяй одинаковые формулировки между знаками.
4. Каждый знак должен получать индивидуальную интерпретацию на основе snapshot.
5. Не утверждай, что астрология научно предсказывает события.
6. Не придумывай точные события: увольнение, встречу, деньги, болезнь, беременность, выигрыш и т.п.
7. Здоровье — только мягкие бытовые рекомендации; никаких диагнозов.
8. Финансы — никаких обещаний дохода или гарантированных результатов.
9. Любовь — без категоричных утверждений о конкретном человеке.
10. Стиль: современный, живой, немного мистический, но без эзотерического мусора и клише.
11. Не упоминай, что текст сгенерирован ИИ.
12. Не добавляй вступление, заключение, дисклеймер или хэштеги.
13. Формат каждой строки: ♈️ ОВЕН: текст.
14. Названия знаков строго в порядке: ОВЕН, ТЕЛЕЦ, БЛИЗНЕЦЫ, РАК, ЛЕВ, ДЕВА, ВЕСЫ, СКОРПИОН, СТРЕЛЕЦ, КОЗЕРОГ, ВОДОЛЕЙ, РЫБЫ.

Сгенерируй только 12 блоков.
""".strip()


def normalize_sign_line(line: str) -> Optional[str]:
    line = re.sub(r"^\s*[-*•]\s*", "", line.strip())
    for emoji, name, _, _ in ZODIAC:
        pattern = re.compile(rf"^(?:{re.escape(emoji)}\s*)?{name}\s*:\s*(.+)$", re.IGNORECASE)
        match = pattern.match(line)
        if match:
            text = re.sub(r"\s+", " ", match.group(1)).strip()
            if text:
                return f"{emoji} {name}: {text}"
    return None


def validate_horoscope(text: str) -> Optional[str]:
    if not text:
        return None
    found = {}
    for raw_line in text.splitlines():
        normalized = normalize_sign_line(raw_line)
        if not normalized:
            continue
        name = normalized.split(":", 1)[0].split()[-1]
        found[name] = normalized

    ordered = []
    for _, name, _, _ in ZODIAC:
        if name not in found:
            return None
        ordered.append(found[name])

    if any(len(x.split(":", 1)[1].strip()) < 45 for x in ordered):
        return None
    return "\n".join(ordered)


def generate_horoscope(target_date: datetime) -> Tuple[Optional[str], Dict, str]:
    snapshot = get_ephemeris_snapshot(target_date)
    prompt = create_editorial_prompt(target_date, snapshot)

    for attempt in range(2):
        text = query_huggingface(prompt)
        valid = validate_horoscope(text or "")
        if valid:
            return valid, snapshot, "huggingface"
        logger.warning("Hugging Face LLM вернул невалидный формат, попытка %s/2", attempt + 1)
        if attempt == 0:
            time.sleep(2)

    return None, snapshot, ""


# ============================================================
# STORAGE / SCHEDULING / ADMIN PANEL
# ============================================================

LOCAL_TZ_NAME = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    logger.warning("Неизвестный BOT_TIMEZONE=%s, используем UTC", LOCAL_TZ_NAME)
    LOCAL_TZ_NAME = "UTC"
    LOCAL_TZ = timezone.utc

AUTO_POST_DEFAULT = os.getenv("AUTO_POST_DEFAULT", "0") == "1"


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def iso_utc(dt: datetime) -> str:
    return utc_datetime(dt).isoformat()


def parse_local_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    return None


def setting_get(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def setting_set(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def auto_enabled() -> bool:
    raw = setting_get("auto_enabled", "1" if AUTO_POST_DEFAULT else "0")
    return raw == "1"


def auto_time() -> str:
    return setting_get("auto_time", os.getenv("AUTO_POST_TIME", "09:00"))


def set_auto_time(value: str):
    setting_set("auto_time", value)


def build_post_text(target_date: datetime, horoscope: str, snapshot: Dict) -> str:
    """Build a polished Telegram HTML post without exposing the LLM/provider."""
    date_str = target_date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y")
    moon = snapshot["positions"].get("Луна", {})
    moon_phase = snapshot["moon_phase"]["name"]
    moon_sign = escape(moon.get("sign", "неизвестном знаке"))

    safe_horoscope = escape(horoscope.strip())
    for emoji, name, _, _ in ZODIAC:
        safe_horoscope = re.sub(
            rf"^{re.escape(emoji)}\s*{name}:",
            f"<b>{emoji} {name}</b>",
            safe_horoscope,
            flags=re.MULTILINE,
        )

    post_text = (
        f"🔮 <b>АСТРОПРОГНОЗ</b>\n"
        f"<i>{date_str}</i>\n\n"
        f"🌙 <b>{escape(moon_phase)}</b> • Луна в <b>{moon_sign}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{safe_horoscope}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "#гороскоп #астропрогноз #зодиак #луна"
    )
    if len(post_text) > 4096:
        raise ValueError(f"Прогноз слишком длинный: {len(post_text)} символов")
    return post_text


def create_draft_record(target_date: datetime, post_text: str, snapshot: Dict, provider: str, admin_id: str) -> str:
    draft_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute("""
            INSERT INTO drafts(id,target_date,post_text,astro_snapshot,provider,created_at,created_by)
            VALUES(?,?,?,?,?,?,?)
        """, (
            draft_id,
            target_date.strftime("%Y-%m-%d"),
            post_text,
            json.dumps(snapshot, ensure_ascii=False),
            provider,
            iso_utc(datetime.now(timezone.utc)),
            str(admin_id),
        ))
        conn.commit()
    return draft_id


def get_draft(draft_id: str):
    with db() as conn:
        return conn.execute(
            "SELECT id,target_date,post_text,astro_snapshot,provider FROM drafts WHERE id=?",
            (draft_id,),
        ).fetchone()


def delete_draft(draft_id: str):
    with db() as conn:
        conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        conn.commit()


def add_scheduled_post(draft_id: str, publish_at: datetime) -> str:
    row = get_draft(draft_id)
    if not row:
        raise ValueError("Черновик не найден")
    schedule_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute("""
            INSERT INTO scheduled_posts(id,target_date,publish_at_utc,post_text,astro_snapshot,provider,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            schedule_id,
            row[1],
            iso_utc(publish_at),
            row[2],
            row[3],
            row[4],
            "scheduled",
            iso_utc(datetime.now(timezone.utc)),
        ))
        conn.commit()
    delete_draft(draft_id)
    return schedule_id


def scheduled_rows(limit=20):
    with db() as conn:
        return conn.execute("""
            SELECT id,target_date,publish_at_utc,provider,status,error
            FROM scheduled_posts
            WHERE status='scheduled'
            ORDER BY publish_at_utc
            LIMIT ?
        """, (limit,)).fetchall()


def cancel_schedule(schedule_id: str) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='scheduled'",
            (schedule_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_due_scheduled():
    now = iso_utc(datetime.now(timezone.utc))
    with db() as conn:
        return conn.execute("""
            SELECT id,target_date,publish_at_utc,post_text,astro_snapshot,provider
            FROM scheduled_posts
            WHERE status='scheduled' AND publish_at_utc <= ?
            ORDER BY publish_at_utc
            LIMIT 5
        """, (now,)).fetchall()


def mark_schedule_result(schedule_id: str, success: bool, error: str = ""):
    with db() as conn:
        if success:
            conn.execute(
                "UPDATE scheduled_posts SET status='published',published_at=?,error=NULL WHERE id=?",
                (iso_utc(datetime.now(timezone.utc)), schedule_id),
            )
        else:
            conn.execute(
                "UPDATE scheduled_posts SET status='failed',error=? WHERE id=?",
                (error[:1000], schedule_id),
            )
        conn.commit()


async def safe_send_channel(bot: Bot, text: str):
    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def publish_scheduled_row(bot: Bot, row):
    schedule_id, target_date, _, post_text, snapshot_json, provider = row
    try:
        await safe_send_channel(bot, post_text)
        mark_schedule_result(schedule_id, True)
        logger.info("Запланированный пост %s опубликован", schedule_id)
        if ADMIN_CHAT_ID:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"✅ Пост на {target_date} опубликован в канал.")
    except Exception as exc:
        logger.exception("Ошибка публикации %s", schedule_id)
        mark_schedule_result(schedule_id, False, str(exc))
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Ошибка публикации {target_date}: {exc}")
            except Exception:
                pass


async def scheduled_loop(application: Application):
    while True:
        try:
            bot = application.bot

            for row in get_due_scheduled():
                await publish_scheduled_row(bot, row)

            if auto_enabled():
                today = local_now().strftime("%Y-%m-%d")
                hh, mm = map(int, auto_time().split(":", 1))
                now = local_now()
                if setting_get("auto_last_date", "") != today and (now.hour > hh or (now.hour == hh and now.minute >= mm)):
                    target = now + timedelta(days=1)
                    horoscope, snapshot, provider = await asyncio.to_thread(generate_horoscope, target)
                    if horoscope:
                        text = build_post_text(target, horoscope, snapshot)
                        if not is_horoscope_sent(target.strftime("%Y-%m-%d")):
                            await safe_send_channel(bot, text)
                            mark_horoscope_sent(target.strftime("%Y-%m-%d"), text, snapshot, provider)
                            logger.info("Автопост на %s опубликован", target.strftime("%d.%m.%Y"))
                        else:
                            logger.info("Автопост на %s пропущен: уже опубликован", target.strftime("%d.%m.%Y"))
                    else:
                        logger.error("Автопост: LLM не вернула валидный прогноз")
                    setting_set("auto_last_date", today)

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Ошибка scheduler loop")
            await asyncio.sleep(30)


# ------------------------------------------------------------
# ADMIN UI
# ------------------------------------------------------------

ADMIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Создать прогноз", callback_data="menu:create")],
    [InlineKeyboardButton("📅 Запланировать", callback_data="menu:schedule"), InlineKeyboardButton("📋 Расписание", callback_data="menu:list")],
    [InlineKeyboardButton("⏰ Автопостинг", callback_data="menu:auto"), InlineKeyboardButton("📊 Статистика", callback_data="menu:stats")],
])


def is_admin(update) -> bool:
    if not ADMIN_CHAT_ID or not update.effective_user:
        return False
    return str(update.effective_user.id) == str(ADMIN_CHAT_ID)


async def deny(update):
    if update.callback_query:
        await update.callback_query.answer("Нет доступа", show_alert=True)
    elif update.message:
        await update.message.reply_text("⛔ Доступ только для администратора.")


async def show_my_id(update, context):
    if update.effective_user and update.message:
        await update.message.reply_text(
            f"🆔 Ваш Telegram ID: <code>{update.effective_user.id}</code>\n\n"
            "Укажите этот номер в ADMIN_CHAT_ID в .env.",
            parse_mode="HTML",
        )


async def llm_test(update, context):
    if not is_admin(update):
        return await deny(update)
    await update.message.reply_text("🧠 Проверяю Hugging Face Inference Providers…")
    probe = "Ответь строго одной строкой: LLM_TEST_OK. Никаких пояснений."
    text = await asyncio.to_thread(query_huggingface, probe)
    if text:
        await update.message.reply_text("✅ LLM отвечает.\n\n" + escape(text[:500]), parse_mode="HTML")
    else:
        await update.message.reply_text(
            "❌ Hugging Face Inference Providers сейчас недоступны.\n\n"
            "Подробная причина находится в journalctl сервиса."
        )


async def admin_start(update, context):
    if not is_admin(update):
        return await deny(update)
    context.user_data.clear()
    text = (
        "🔮 <b>BeauHoroscope 3.0</b>\n\n"
        f"Канал: <code>{TELEGRAM_CHANNEL_ID}</code>\n"
        f"Часовой пояс: <code>{LOCAL_TZ_NAME}</code>\n"
        f"Автопостинг: {'🟢 включён' if auto_enabled() else '🔴 выключен'}\n\n"
        "Выберите действие:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=ADMIN_MENU)


async def create_forecast_for_date(target_date: datetime, admin_id: str) -> Tuple[str, Dict, str]:
    horoscope, snapshot, provider = await asyncio.to_thread(generate_horoscope, target_date)
    if not horoscope:
        raise RuntimeError("Hugging Face сейчас не вернул валидный прогноз. Проверьте /llmtest и журнал сервиса.")
    text = build_post_text(target_date, horoscope, snapshot)
    return text, snapshot, provider


def parse_date_input(text: str) -> Optional[datetime]:
    t = text.strip().lower()
    now = local_now()
    if t in ("сегодня", "сегодняшний день"):
        return now.replace(hour=12, minute=0, second=0, microsecond=0)
    if t == "завтра":
        return (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            d = datetime.strptime(t, fmt).replace(tzinfo=LOCAL_TZ)
            return d.replace(hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            pass
    return None


async def menu_create(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "await_create_date"
    await q.edit_message_text(
        "🚀 <b>Создать прогноз</b>\n\n"
        "Напишите дату:\n"
        "• <code>сегодня</code>\n"
        "• <code>завтра</code>\n"
        "• <code>19.08.2026</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]]),
    )


async def menu_schedule(update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["state"] = "await_schedule_date"
    await q.edit_message_text(
        "📅 <b>Запланировать публикацию</b>\n\n"
        "Сначала укажите дату прогноза:\n"
        "• <code>сегодня</code>\n"
        "• <code>завтра</code>\n"
        "• <code>19.08.2026</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]]),
    )


async def handle_text(update, context):
    if not is_admin(update):
        return
    state = context.user_data.get("state")
    text = update.message.text.strip()

    if state == "await_create_date":
        target = parse_date_input(text)
        if not target:
            await update.message.reply_text("❌ Не понял дату. Пример: <code>19.08.2026</code>", parse_mode="HTML")
            return
        await update.message.reply_text("🧠 Считаю эфемериды и генерирую прогноз…")
        try:
            post_text, snapshot, provider = await create_forecast_for_date(target, str(update.effective_user.id))
            context.user_data.clear()

            target_key = target.strftime("%Y-%m-%d")
            if is_horoscope_sent(target_key):
                await update.message.reply_text(
                    "⚠️ Прогноз на эту дату уже опубликован.\n\nПовторная публикация отменена.",
                    reply_markup=ADMIN_MENU,
                )
                return

            await update.message.reply_text("📢 Публикую прогноз в канал…")
            await safe_send_channel(context.application.bot, post_text)
            mark_horoscope_sent(target_key, post_text, snapshot, provider)
            await update.message.reply_text(
                "✅ <b>Прогноз опубликован</b>\n\n"
                f"📅 {target.strftime('%d.%m.%Y')}\n"
                f"📢 <code>{escape(TELEGRAM_CHANNEL_ID)}</code>",
                parse_mode="HTML",
                reply_markup=ADMIN_MENU,
            )
        except Exception as exc:
            logger.exception("Ошибка генерации/публикации из панели")
            await update.message.reply_text(
                f"❌ Не удалось опубликовать прогноз:\n{str(exc)[:1000]}",
                reply_markup=ADMIN_MENU,
            )
        return

    if state == "await_schedule_date":
        target = parse_date_input(text)
        if not target:
            await update.message.reply_text("❌ Не понял дату. Пример: <code>19.08.2026</code>", parse_mode="HTML")
            return
        await update.message.reply_text("🧠 Генерирую прогноз для расписания…")
        try:
            post_text, snapshot, provider = await create_forecast_for_date(target, str(update.effective_user.id))
            draft_id = create_draft_record(target, post_text, snapshot, provider, str(update.effective_user.id))
            context.user_data["state"] = "await_schedule_datetime"
            context.user_data["draft_id"] = draft_id
            await update.message.reply_text(
                "📅 <b>Время публикации</b>\n\n"
                f"Часовой пояс: <code>{LOCAL_TZ_NAME}</code>\n"
                "Введите дату и время:\n"
                "<code>19.08.2026 09:00</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]]),
            )
        except Exception as exc:
            logger.exception("Ошибка генерации для расписания")
            context.user_data.clear()
            await update.message.reply_text(
                f"❌ Не удалось подготовить прогноз:\n{str(exc)[:1000]}",
                reply_markup=ADMIN_MENU,
            )
        return

    if state == "await_schedule_datetime":
        dt = parse_local_datetime(text)
        if not dt:
            await update.message.reply_text("❌ Формат: <code>19.08.2026 09:00</code>", parse_mode="HTML")
            return
        if dt <= local_now():
            await update.message.reply_text("❌ Время уже прошло. Укажите будущее время.")
            return
        draft_id = context.user_data.get("draft_id")
        try:
            sid = add_scheduled_post(draft_id, dt)
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ <b>Запланировано</b>\n\n🆔 <code>{sid}</code>\n"
                f"📅 {dt.strftime('%d.%m.%Y %H:%M')} ({LOCAL_TZ_NAME})",
                parse_mode="HTML",
                reply_markup=ADMIN_MENU,
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось запланировать: {exc}")
        return

    if state == "await_auto_time":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            await update.message.reply_text("❌ Формат времени: <code>09:00</code>", parse_mode="HTML")
            return
        set_auto_time(text)
        setting_set("auto_enabled", "1")
        context.user_data.clear()
        await update.message.reply_text(
            f"🟢 Автопостинг включён. Каждый день в {text} ({LOCAL_TZ_NAME}).",
            reply_markup=ADMIN_MENU,
        )
        return

    if text == "🏠 Меню":
        return await admin_start(update, context)


def preview_keyboard(draft_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Опубликовать сейчас", callback_data=f"draft:publish:{draft_id}")],
        [InlineKeyboardButton("📅 Запланировать", callback_data=f"draft:schedule:{draft_id}")],
        [InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"draft:regen:{draft_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"draft:delete:{draft_id}"), InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
    ])


def get_due_scheduled_rows(limit=20):
    return scheduled_rows(limit)


async def menu_list(update, context):
    q = update.callback_query
    await q.answer()
    rows = scheduled_rows(20)
    if not rows:
        await q.edit_message_text("📋 <b>Расписание пусто.</b>", parse_mode="HTML", reply_markup=ADMIN_MENU)
        return
    buttons = []
    lines = ["📋 <b>Запланированные публикации</b>\n"]
    for sid, target_date, pub, provider, status, error in rows:
        dt = datetime.fromisoformat(pub).astimezone(LOCAL_TZ)
        lines.append(f"• {target_date} — {dt.strftime('%d.%m %H:%M')}")
        buttons.append([InlineKeyboardButton(f"🗑 Отменить {target_date}", callback_data=f"sched:cancel:{sid}")])
    buttons.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")])
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def menu_auto(update, context):
    q = update.callback_query
    await q.answer()
    enabled = auto_enabled()
    t = auto_time()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕘 Изменить время", callback_data="auto:time")],
        [InlineKeyboardButton("⏸ Выключить" if enabled else "▶️ Включить", callback_data="auto:toggle")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
    ])
    await q.edit_message_text(
        "⏰ <b>Автопубликация</b>\n\n"
        f"Статус: {'🟢 включена' if enabled else '🔴 выключена'}\n"
        f"Время: <code>{t}</code>\n"
        f"Часовой пояс: <code>{LOCAL_TZ_NAME}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def menu_stats(update, context):
    q = update.callback_query
    await q.answer()
    with db() as conn:
        sent = conn.execute("SELECT COUNT(*) FROM sent_horoscopes").fetchone()[0]
        scheduled = conn.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='scheduled'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='failed'").fetchone()[0]
        last = conn.execute("SELECT target_date FROM sent_horoscopes ORDER BY id DESC LIMIT 1").fetchone()
    await q.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"Опубликовано: <b>{sent}</b>\n"
        f"В очереди: <b>{scheduled}</b>\n"
        f"Ошибок: <b>{failed}</b>\n"
        f"Последняя дата: <b>{last[0] if last else '—'}</b>",
        parse_mode="HTML",
        reply_markup=ADMIN_MENU,
    )


async def draft_publish(update, context, draft_id: str):
    q = update.callback_query
    row = get_draft(draft_id)
    if not row:
        await q.answer("Черновик не найден", show_alert=True)
        return
    await q.answer("Публикую…")
    try:
        if is_horoscope_sent(row[1]):
            delete_draft(draft_id)
            await q.edit_message_text("⚠️ Прогноз на эту дату уже опубликован.", reply_markup=ADMIN_MENU)
            return
        await safe_send_channel(context.application.bot, row[2])
        snapshot = json.loads(row[3]) if row[3] else {}
        mark_horoscope_sent(row[1], row[2], snapshot, row[4])
        delete_draft(draft_id)
        await q.edit_message_text(f"✅ <b>Опубликовано</b>\n\nДата: {row[1]}", parse_mode="HTML", reply_markup=ADMIN_MENU)
    except Exception as exc:
        logger.exception("Ручная публикация не удалась")
        await q.edit_message_text(
            f"❌ <b>Не опубликовано</b>\n\n{str(exc)[:700]}",
            parse_mode="HTML",
            reply_markup=preview_keyboard(draft_id),
        )


async def draft_schedule(update, context, draft_id: str):
    q = update.callback_query
    if not get_draft(draft_id):
        await q.answer("Черновик не найден", show_alert=True)
        return
    context.user_data["state"] = "await_schedule_datetime"
    context.user_data["draft_id"] = draft_id
    await q.answer()
    await q.edit_message_text(
        "📅 <b>Время публикации</b>\n\n"
        f"Введите дату и время по часовому поясу <code>{LOCAL_TZ_NAME}</code>:\n"
        "<code>19.08.2026 09:00</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]]),
    )


async def callback_router(update, context):
    if not is_admin(update):
        return await deny(update)
    q = update.callback_query
    data = q.data or ""
    if data == "menu:home":
        return await admin_start(update, context)
    if data == "menu:create":
        return await menu_create(update, context)
    if data == "menu:schedule":
        return await menu_schedule(update, context)
    if data == "menu:list":
        return await menu_list(update, context)
    if data == "menu:auto":
        return await menu_auto(update, context)
    if data == "menu:stats":
        return await menu_stats(update, context)
    if data == "auto:toggle":
        setting_set("auto_enabled", "0" if auto_enabled() else "1")
        return await menu_auto(update, context)
    if data == "auto:time":
        await q.answer()
        context.user_data["state"] = "await_auto_time"
        await q.edit_message_text(
            "🕘 Введите время ежедневной автопубликации, например <code>09:00</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu:auto")]]),
        )
        return
    if data.startswith("draft:publish:"):
        return await draft_publish(update, context, data.split(":", 2)[2])
    if data.startswith("draft:schedule:"):
        return await draft_schedule(update, context, data.split(":", 2)[2])
    if data.startswith("draft:delete:"):
        draft_id = data.split(":", 2)[2]
        delete_draft(draft_id)
        await q.answer("Удалено")
        return await admin_start(update, context)
    if data.startswith("sched:cancel:"):
        sid = data.split(":", 2)[2]
        ok = cancel_schedule(sid)
        await q.answer("Отменено" if ok else "Не найдено")
        return await menu_list(update, context)


async def post_init(application: Application):
    application.bot_data["scheduler_task"] = asyncio.create_task(scheduled_loop(application))


async def post_shutdown(application: Application):
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID:
        raise RuntimeError("Не задан TELEGRAM_CHANNEL_ID")
    if not HF_TOKEN:
        raise RuntimeError("Не задан HF_TOKEN. Создайте Hugging Face User Access Token с правом Make calls to Inference Providers и добавьте его в .env.")

    init_db()
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("id", show_my_id))
    application.add_handler(CommandHandler("start", admin_start))
    application.add_handler(CommandHandler("menu", admin_start))
    application.add_handler(CommandHandler("llmtest", llm_test))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info(
        "BeauHoroscope 3.0 started | timezone=%s | auto=%s %s",
        LOCAL_TZ_NAME,
        auto_enabled(),
        auto_time(),
    )
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
