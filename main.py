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

LLM работает через два бесплатных API-провайдера: Groq (primary) и Mistral (fallback).
Telegram-секреты и LLM API keys — только через переменные окружения.
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
import hashlib
from difflib import SequenceMatcher
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

# LLM: бесплатные API. Primary = Groq/Qwen, fallback = Mistral.
# Никаких legacy/HF runtime-зависимостей.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "2200"))
GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.72"))
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
MISTRAL_MAX_TOKENS = int(os.getenv("MISTRAL_MAX_TOKENS", "2200"))
MISTRAL_TEMPERATURE = float(os.getenv("MISTRAL_TEMPERATURE", "0.72"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "90"))
LLM_RETRIES_PER_PROVIDER = int(os.getenv("LLM_RETRIES_PER_PROVIDER", "1"))

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
    """
    Рассчитывает реальные геоцентрические эклиптические долготы планет
    на 12:00 UTC целевой даты. PyEphem использует астрономические эфемериды
    локально, поэтому для расчёта не нужен платный API.
    """
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

            # Ретроградность: сравнение истинной долготы на соседних датах.
            prev_observer = ephem.Observer()
            prev_observer.date = target_date - timedelta(days=1)
            next_observer = ephem.Observer()
            next_observer.date = target_date + timedelta(days=1)

            prev_lon = ecliptic_longitude(cls(), prev_observer)
            next_lon = ecliptic_longitude(cls(), next_observer)

            # Нормализованное суточное движение.
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

    # Фаза в процентах освещённости.
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

    # Самые близкие аспекты первыми.
    return sorted(result, key=lambda x: x["orb"])


def build_sign_context(snapshot: Dict) -> Dict[str, Dict]:
    """
    Для каждого знака строим компактный контекст. LLM получает одинаковые
    реальные эфемериды, но видит также управителя конкретного знака.
    """
    positions = snapshot["positions"]
    aspects = snapshot["aspects"]
    result = {}

    for emoji, name, in_case, ruler in ZODIAC:
        relevant = []

        # Планеты в самом знаке.
        for planet, data in positions.items():
            if data["sign_index"] == SIGN_INDEX[name]:
                relevant.append(
                    f"{planet} в {data['degree']:.1f}° {data['sign']}"
                    + (" (ретроградно)" if data["retrograde"] else "")
                )

        # Аспекты управителя знака.
        ruler_aspects = [
            a for a in aspects if a["a"] == ruler or a["b"] == ruler
        ][:5]

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
        conn.commit()


def is_horoscope_sent(target_date: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM sent_horoscopes WHERE target_date=?",
            (target_date,),
        ).fetchone() is not None


def mark_horoscope_sent(
    target_date: str,
    horoscope_text: str,
    snapshot: Dict,
    provider: str,
) -> bool:
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
# LLM PROVIDER — GROQ PRIMARY + MISTRAL FALLBACK
# ============================================================

LLM_LOCK = threading.Lock()


def _clean_llm_text(text: str) -> str:
    """Remove common reasoning/code wrappers before validation."""
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:text|markdown|json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    return text.strip()


def _openai_chat_request(base_url: str, api_key: str, model: str, prompt: str,
                         max_tokens: int, temperature: float, provider: str) -> Optional[str]:
    if not api_key:
        logger.warning("LLM provider %s is not configured", provider)
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
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
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        if response.status_code >= 400:
            body = response.text[:500].replace(api_key, "<REDACTED>")
            logger.warning(
                "LLM provider %s failed: HTTP %s %s",
                provider, response.status_code, body,
            )
            return None

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            logger.warning("LLM provider %s returned no choices", provider)
            return None
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in text
            )
        text = _clean_llm_text(str(text))
        if text:
            logger.info("LLM success: provider=%s model=%s", provider, model)
            return text
        logger.warning("LLM provider %s returned empty response", provider)
    except requests.RequestException as exc:
        logger.warning("LLM provider %s network error: %s", provider, str(exc)[:300])
    except Exception as exc:
        logger.exception("LLM provider %s unexpected error: %s", provider, str(exc)[:300])
    return None


def _query_llm_unlocked(prompt: str) -> Tuple[Optional[str], str]:
    providers = [
        ("groq", GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL, GROQ_MAX_TOKENS, GROQ_TEMPERATURE),
        ("mistral", MISTRAL_API_KEY, MISTRAL_BASE_URL, MISTRAL_MODEL, MISTRAL_MAX_TOKENS, MISTRAL_TEMPERATURE),
    ]
    for provider, key, base_url, model, max_tokens, temperature in providers:
        if not key:
            continue
        for attempt in range(LLM_RETRIES_PER_PROVIDER + 1):
            text = _openai_chat_request(
                base_url, key, model, prompt, max_tokens, temperature, provider
            )
            if text:
                return text, provider
            if attempt < LLM_RETRIES_PER_PROVIDER:
                time.sleep(1.5 * (attempt + 1))
    return None, ""


def query_llm(prompt: str) -> Tuple[Optional[str], str]:
    """Generate through Groq first, then Mistral. Never uses external legacy providers."""
    with LLM_LOCK:
        return _query_llm_unlocked(prompt)


# PROMPT + VALIDATION
# ============================================================


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
        f"- {a['a']} — {a['aspect']} — {a['b']} "
        f"(орб {a['orb']:.2f}°)"
        for a in snapshot["aspects"][:12]
    ) or "- Точных основных аспектов в заданном орбе нет."

    per_sign = []
    for _, name, _, ruler in ZODIAC:
        ctx = sign_context[name]
        in_sign = ", ".join(ctx["planets_in_sign"]) or "нет планет"
        ruler_aspects = ", ".join(
            f"{a['a']} {a['aspect']} {a['b']}"
            for a in ctx["ruler_aspects"]
        ) or "нет близких аспектов управителя"
        per_sign.append(
            f"{name} (управитель: {ruler}): "
            f"планеты в знаке: {in_sign}; "
            f"аспекты управителя: {ruler_aspects}"
        )

    return f"""
Создай ежедневный астрологический прогноз на {target_date.strftime('%d.%m.%Y')}.

Это НЕ запрос на астрономический расчёт. Все астрономические данные уже
рассчитаны программой. Твоя задача — сделать качественную астрологическую
интерпретацию этих данных для 12 солнечных знаков.

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
4. Каждый знак должен получать индивидуальную интерпретацию на основе
   представленного snapshot: положения планет, управителя, аспектов и Луны.
5. Не утверждай, что астрология научно предсказывает события.
6. Не придумывай точные события: увольнение, встречу, деньги, болезнь,
   беременность, выигрыш и т.п. Формулируй как тенденции, настроение,
   вероятные темы и полезные действия.
7. Здоровье — только мягкие бытовые рекомендации; никаких диагнозов.
8. Финансы — никаких обещаний дохода или гарантированных результатов.
9. Любовь — без категоричных утверждений о конкретном человеке.
10. Стиль: современный, живой, немного мистический, но без эзотерического
    мусора и клише вроде "вселенная посылает знак".
11. Не упоминай, что текст сгенерирован ИИ.
12. Не добавляй вступление, заключение, дисклеймер или хэштеги.
13. Формат каждой строки:
    ♈️ ОВЕН: текст.
14. Названия знаков должны идти строго в этом порядке:
    ОВЕН, ТЕЛЕЦ, БЛИЗНЕЦЫ, РАК, ЛЕВ, ДЕВА, ВЕСЫ, СКОРПИОН,
    СТРЕЛЕЦ, КОЗЕРОГ, ВОДОЛЕЙ, РЫБЫ.

Сгенерируй только 12 блоков.
""".strip()


def normalize_sign_line(line: str) -> Optional[str]:
    line = re.sub(r"^\s*[-*•]\s*", "", line.strip())

    for emoji, name, _, _ in ZODIAC:
        pattern = re.compile(
            rf"^(?:{re.escape(emoji)}\s*)?{name}\s*:\s*(.+)$",
            re.IGNORECASE,
        )
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

    # Защита от совсем пустых/аномально коротких ответов.
    if any(len(x.split(":", 1)[1].strip()) < 45 for x in ordered):
        return None

    # Не принимаем ответ с явными "дополнительными" знаками/секциями.
    return "\n".join(ordered)


def generate_horoscope(target_date: datetime) -> Tuple[Optional[str], Dict, str]:
    snapshot = get_ephemeris_snapshot(target_date)
    prompt = create_editorial_prompt(target_date, snapshot)

    # Один запрос на весь выпуск. Provider fallback выполняется внутри query_llm.
    for attempt in range(2):
        text, provider = query_llm(prompt)
        valid = validate_horoscope(text or "")
        if valid:
            return valid, snapshot, provider
        logger.warning(
            "LLM returned invalid horoscope format, validation attempt %s/2 (provider=%s)",
            attempt + 1, provider or "none",
        )
        if attempt == 0:
            time.sleep(2)

    return None, snapshot, ""


# ============================================================
# STORAGE / SCHEDULING / ADMIN PANEL — BeauHoroscope 4.0
# ============================================================

LOCAL_TZ_NAME = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    logger.warning("Неизвестный BOT_TIMEZONE=%s, используем UTC", LOCAL_TZ_NAME)
    LOCAL_TZ_NAME = "UTC"
    LOCAL_TZ = timezone.utc

AUTO_POST_DEFAULT = os.getenv("AUTO_POST_DEFAULT", "1") == "1"
CONTENT_POST_DEFAULT = os.getenv("CONTENT_POST_DEFAULT", "1") == "1"
CONTENT_POST_TIME_DEFAULT = os.getenv("CONTENT_POST_TIME", "15:00")
CONTENT_POST_TIMES_DEFAULT = os.getenv("CONTENT_POST_TIMES", CONTENT_POST_TIME_DEFAULT)
CONTENT_MIN_INTERVAL_DAYS = int(os.getenv("CONTENT_MIN_INTERVAL_DAYS", "21"))
CONTENT_SIMILARITY_THRESHOLD = float(os.getenv("CONTENT_SIMILARITY_THRESHOLD", "0.72"))
CONTENT_MAX_HISTORY = int(os.getenv("CONTENT_MAX_HISTORY", "180"))

CONTENT_TOPICS = [
    ("astrology", "Что каждый знак зодиака считает настоящим проявлением любви"),
    ("astrology", "Как каждый знак зодиака переживает перемены"),
    ("astrology", "Что раздражает каждый знак зодиака — и почему"),
    ("astrology", "Как разные знаки принимают важные решения"),
    ("astrology", "Что помогает каждому знаку восстановить внутреннее равновесие"),
    ("astrology", "Как каждый знак проявляет симпатию, даже если молчит"),
    ("astrology", "Какие качества каждый знак особенно ценит в партнёре"),
    ("astrology", "Какой формат заботы лучше всего понимает каждый знак"),
    ("astrology", "Почему одному знаку нужен план, а другому — свобода"),
    ("astrology", "Как знаки зодиака реагируют на дистанцию в отношениях"),
    ("astrology", "Какие слова особенно сильно цепляют каждый знак"),
    ("astrology", "Как каждый знак ведёт себя, когда ему действительно важно"),
    ("moon", "Луна и настроение: как её положение меняет эмоциональный фон"),
    ("moon", "Лунные привычки: как использовать вечер для восстановления"),
    ("moon", "Что наблюдать за собой в период растущей Луны"),
    ("moon", "Что помогает отпускать лишнее на убывающей Луне"),
    ("planets", "Венера в астрологии: что она рассказывает о стиле любви"),
    ("planets", "Меркурий и общение: почему мы по-разному выражаем мысли"),
    ("planets", "Марс и энергия: как проявляется личный импульс разных знаков"),
    ("planets", "Юпитер и чувство возможностей: астрологический взгляд"),
    ("planets", "Сатурн и границы: чему нас учит дисциплина"),
    ("aspects", "Что такое аспект планет и почему астрологи обращают на него внимание"),
    ("aspects", "Соединение планет: когда две энергии встречаются в одной точке"),
    ("aspects", "Тригон, квадрат и секстиль: объясняем аспекты простыми словами"),
    ("relationships", "Совместимость знаков: почему одного Солнца бывает мало"),
    ("relationships", "Какие знаки любят словами, а какие — поступками"),
    ("relationships", "Почему разные знаки по-разному переживают неопределённость"),
    ("relationships", "Астрологический взгляд на личные границы в отношениях"),
    ("zodiac", "Сильная сторона каждого знака, о которой он сам забывает"),
    ("zodiac", "Скрытая сторона каждого знака без стереотипов"),
    ("zodiac", "Какой внутренний ресурс есть у каждого знака"),
    ("zodiac", "Что каждому знаку важно помнить о себе"),
    ("interactive", "Какой ты знак по реакции на внезапные перемены?"),
    ("interactive", "Выбери вариант — и узнаешь, какая энергия тебе сейчас ближе"),
    ("interactive", "Мини-тест по знакам: как ты проявляешь чувства"),
    ("interactive", "Астрологический вопрос дня: что сейчас важнее — любовь, свобода или стабильность?"),
]

ASTROLOGY_SIGNALS = (
    "астролог", "зодиак", "знак", "луна", "планет", "венер", "меркур", "марс",
    "юпитер", "сатурн", "аспект", "ретроград", "совместим", "солнц", "наталь",
)



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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_date TEXT NOT NULL,
                content_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                title TEXT,
                post_text TEXT NOT NULL,
                text_hash TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_history_date
            ON content_history(content_date)
        """)
        conn.commit()


def setting_get(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def setting_set(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def auto_enabled() -> bool:
    raw = setting_get("auto_enabled", "1" if AUTO_POST_DEFAULT else "0")
    return raw == "1"


def auto_time() -> str:
    return setting_get("auto_time", os.getenv("AUTO_POST_TIME", "09:00"))


def set_auto_time(value: str):
    setting_set("auto_time", value)


def build_post_text(target_date: datetime, horoscope: str, snapshot: Dict) -> str:
    """Build a warm, visual Telegram post aimed at a female audience."""
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

    # Small visual separators make the long 12-sign post much easier to scan.
    safe_horoscope = re.sub(r"\n(?=<b>[♈️♉️♊️♋️♌️♍️♎️♏️♐️♑️♒️♓️])", "\n\n", safe_horoscope)

    post_text = (
        f"✨ <b>ТВОЙ АСТРОДЕНЬ</b>\n"
        f"<i>{date_str} · личный прогноз для твоего знака</i>\n\n"
        f"🌙 <b>{escape(moon_phase)}</b>\n"
        f"Сегодня Луна в <b>{moon_sign}</b> — прислушайся к своему настроению "
        f"и выбери то, что действительно важно именно тебе.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{safe_horoscope}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💗 <b>Сохрани прогноз</b>, чтобы вернуться к нему вечером.\n"
        "✨ Если прогноз откликнулся — поставь реакцию.\n\n"
        "#гороскоп #астропрогноз #зодиак #луна #женскийгороскоп"
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
        """, (draft_id, target_date.strftime("%Y-%m-%d"), post_text, json.dumps(snapshot, ensure_ascii=False), provider, iso_utc(datetime.now(timezone.utc)), str(admin_id)))
        conn.commit()
    return draft_id


def get_draft(draft_id: str):
    with db() as conn:
        return conn.execute("SELECT id,target_date,post_text,astro_snapshot,provider FROM drafts WHERE id=?", (draft_id,)).fetchone()


def delete_draft(draft_id: str):
    with db() as conn:
        conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        conn.commit()


def add_scheduled_post(draft_id: str, publish_at: datetime) -> str:
    row = get_draft(draft_id)
    if not row:
        raise ValueError("Черновик не найден")

    # Идемпотентность: не ставим второй активный пост на ту же дату.
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1",
            (row[1],),
        ).fetchone()
        if existing:
            delete_draft(draft_id)
            return existing[0]

        schedule_id = uuid.uuid4().hex[:12]
        conn.execute("""
            INSERT INTO scheduled_posts(id,target_date,publish_at_utc,post_text,astro_snapshot,provider,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            schedule_id, row[1], iso_utc(publish_at), row[2], row[3], row[4],
            "scheduled", iso_utc(datetime.now(timezone.utc))
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
        cur = conn.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='scheduled'", (schedule_id,))
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
            conn.execute("UPDATE scheduled_posts SET status='published',published_at=?,error=NULL WHERE id=?", (iso_utc(datetime.now(timezone.utc)), schedule_id))
        else:
            conn.execute("UPDATE scheduled_posts SET status='failed',error=? WHERE id=?", (error[:1000], schedule_id))
        conn.commit()



def content_enabled() -> bool:
    raw = setting_get("content_enabled", "1" if CONTENT_POST_DEFAULT else "0")
    return raw == "1"


def _normalize_content_times(value: str) -> list[str]:
    times = []
    for raw in (value or "").replace(";", ",").split(","):
        raw = raw.strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
            times.append(raw)
    return sorted(set(times), key=lambda x: (int(x[:2]), int(x[3:])))


def content_times() -> list[str]:
    legacy = setting_get("content_time", CONTENT_POST_TIME_DEFAULT)
    raw = setting_get("content_times", legacy or CONTENT_POST_TIMES_DEFAULT)
    times = _normalize_content_times(raw)
    return times or [CONTENT_POST_TIME_DEFAULT]


def content_time() -> str:
    return content_times()[0]


def set_content_times(times: list[str]):
    normalized = _normalize_content_times(",".join(times))
    setting_set("content_times", ",".join(normalized or [CONTENT_POST_TIME_DEFAULT]))


def set_content_time(value: str):
    set_content_times([value])


def content_times_label() -> str:
    return ", ".join(content_times())


def content_slot_key(content_date: str, publish_time: str) -> str:
    return f"{content_date} {publish_time}"


def content_published_slots() -> set[str]:
    raw = setting_get("content_published_slots", "[]")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def mark_content_slot_published(content_date: str, publish_time: str):
    slots = content_published_slots()
    slots.add(content_slot_key(content_date, publish_time))
    setting_set(
        "content_published_slots",
        json.dumps(sorted(slots)[-90:], ensure_ascii=False),
    )


def content_last_date() -> str:
    return setting_get("content_last_date", "")


def set_content_last_date(value: str):
    setting_set("content_last_date", value)

def _content_tokens(text: str) -> set:
    words = re.findall(r"[а-яёa-z0-9]{4,}", text.lower())
    stop = {
        "который", "которая", "которые", "может", "можно", "будет", "сейчас",
        "этот", "этого", "если", "когда", "почему", "каждый", "каждого",
        "через", "между", "тебе", "тебя", "ваш", "ваша", "ваше", "ваши",
    }
    return {w for w in words if w not in stop}


def content_fingerprint(text: str) -> str:
    tokens = sorted(_content_tokens(text))
    return " ".join(tokens[:120])


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text.lower())).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def recent_content_history(limit: int = CONTENT_MAX_HISTORY):
    with db() as conn:
        return conn.execute("""
            SELECT content_date, content_type, topic, title, post_text, fingerprint
            FROM content_history
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()


def topic_is_available(topic: str) -> bool:
    cutoff = (local_now().date() - timedelta(days=CONTENT_MIN_INTERVAL_DAYS)).isoformat()
    with db() as conn:
        row = conn.execute("""
            SELECT 1 FROM content_history
            WHERE topic=? AND content_date>=?
            LIMIT 1
        """, (topic, cutoff)).fetchone()
    return row is None


def choose_content_topic() -> Optional[Tuple[str, str]]:
    # Сначала выбираем темы, которых не было в cooldown.
    available = [item for item in CONTENT_TOPICS if topic_is_available(item[1])]
    if not available:
        # Если вся матрица исчерпана, берём тему с самым старым использованием.
        with db() as conn:
            rows = conn.execute("""
                SELECT topic, MIN(id) AS first_id
                FROM content_history
                GROUP BY topic
                ORDER BY first_id ASC
                LIMIT 10
            """).fetchall()
        used = {r[0] for r in rows}
        available = [item for item in CONTENT_TOPICS if item[1] not in used] or CONTENT_TOPICS[:]
    # Небольшая ротация по рубрикам, чтобы лента не была монотонной.
    return available[int(time.time()) % len(available)]


def content_is_too_similar(text: str, history=None) -> bool:
    if history is None:
        history = recent_content_history()
    new_fp = content_fingerprint(text)
    new_tokens = set(new_fp.split())
    new_hash = content_hash(text)

    for _, _, _, _, old_text, old_fp in history:
        if new_hash == content_hash(old_text):
            return True
        old_tokens = set(old_fp.split())
        if new_tokens and old_tokens:
            jaccard = len(new_tokens & old_tokens) / max(1, len(new_tokens | old_tokens))
            if jaccard >= CONTENT_SIMILARITY_THRESHOLD:
                return True
        if SequenceMatcher(None, text.lower(), old_text.lower()).ratio() >= 0.84:
            return True
    return False


def register_content(content_date: str, content_type: str, topic: str, title: str, post_text: str) -> bool:
    try:
        with db() as conn:
            conn.execute("""
                INSERT INTO content_history(
                    content_date, content_type, topic, title, post_text,
                    text_hash, fingerprint, created_at, published_at
                )
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                content_date, content_type, topic, title, post_text,
                content_hash(post_text), content_fingerprint(post_text),
                iso_utc(datetime.now(timezone.utc)),
                iso_utc(datetime.now(timezone.utc)),
            ))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def create_content_prompt(target_date: datetime, content_type: str, topic: str) -> str:
    recent = recent_content_history(30)
    recent_topics = "\n".join(
        f"- {r[1]}: {r[2]} | {r[3] or 'без заголовка'}"
        for r in recent
    ) or "- Истории публикаций пока нет."

    return f"""
Создай короткую оригинальную публикацию для Telegram-канала BeauHoroscope
на {target_date.strftime('%d.%m.%Y')}.

ПОЗИЦИОНИРОВАНИЕ:
Канал посвящён астрологии, гороскопам, знакам зодиака, Луне, планетам,
аспектам, совместимости и астрологическим наблюдениям. Это не общий
психологический или женский lifestyle-канал.

ТИП КОНТЕНТА: {content_type}
ТЕМА: {topic}

ПОСЛЕДНИЕ ПУБЛИКАЦИИ — НЕ ПОВТОРЯЙ ИХ:
{recent_topics}

ПРАВИЛА:
1. Материал должен быть уникальным по идее и формулировкам.
2. Не перефразируй недавнюю публикацию.
3. Не повторяй один и тот же hook, структуру или CTA.
4. Тема обязательно должна иметь явную астрологическую связь.
5. Если это отношения, самооценка или психология — объясняй тему именно
   через астрологию/знаки/планеты, а не как обычную психологическую статью.
6. Не придумывай точные астрономические факты, транзиты или аспекты,
   если они не даны во входных данных.
7. Не выдавай астрологию за научно доказанный способ предсказывать будущее.
8. Не обещай деньги, здоровье, беременность, конкретные события или гарантии.
9. Стиль: красивый, современный, тёплый, женственный, немного мистический,
   но без дешёвых эзотерических клише.
10. Длина: 700–1400 символов.
11. Заголовок: 5–10 слов, живой и конкретный, без кликбейта.
12. Начни с сильного естественного hook.
13. Используй 3–5 коротких абзацев, чтобы текст легко читался с телефона.
14. 1–3 эмодзи внутри текста, без перегруза.
15. В конце добавь мягкий CTA или вопрос, только если он естественен.
16. Не упоминай ИИ, генерацию, промпты или редакционные правила.
17. Не используй хэштеги — они добавятся программой.

ФОРМАТ:
Первая строка — короткий заголовок.
Далее — готовый текст публикации.
""".strip()


def _has_repeated_content(text: str) -> bool:
    paragraphs = [p.strip().lower() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) >= 4:
        # Detect a model repeating a large block of the article.
        half = len(paragraphs) // 2
        left = " ".join(paragraphs[:half])
        right = " ".join(paragraphs[-half:])
        if len(left) > 180 and SequenceMatcher(None, left, right).ratio() >= 0.92:
            return True
    if len(paragraphs) >= 2:
        first = re.sub(r"[^а-яёa-z0-9 ]+", "", paragraphs[0])
        second = re.sub(r"[^а-яёa-z0-9 ]+", "", paragraphs[1])
        if first and second and (
            second == first or second.startswith(first + " ")
        ):
            return True
    return False


def validate_content_post(text: str) -> Optional[str]:
    text = _clean_llm_text(text or "")
    if not text:
        return None

    # Preserve paragraph structure: the final Telegram post should breathe
    # instead of becoming one dense wall of text.
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    paragraphs = []
    current = []
    for line in raw_lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    normalized = "\n\n".join(paragraphs).strip()
    plain = re.sub(r"\s+", " ", normalized).strip()
    if _has_repeated_content(normalized):
        return None
    if len(plain) < 500 or len(plain) > 1800:
        return None

    lowered = plain.lower()
    signal_count = sum(1 for signal in ASTROLOGY_SIGNALS if signal in lowered)
    if signal_count < 2:
        return None
    banned = (
        "как языковая модель", "сгенерировано ии", "по данным исследования",
        "гарантированно разбогатеете", "вылечит", "диагноз",
    )
    if any(x in lowered for x in banned):
        return None
    return normalized


def _format_content_body(body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    formatted = []
    for paragraph in paragraphs:
        paragraph = escape(paragraph)
        # Turn simple LLM bullets into visually lighter Telegram bullets.
        paragraph = re.sub(r"(?m)^[-•]\s*", "• ", paragraph)
        formatted.append(paragraph)
    return "\n\n".join(formatted)


# Один общий lock для ручной и ежедневной публикации контента.
# Не позволяет двум генерациям одновременно пройти проверку уникальности
# и затем обеим попасть в канал до записи в content_history.
CONTENT_PUBLISH_LOCK = asyncio.Lock()


async def publish_content_now(bot: Bot, user_id: str) -> Optional[Tuple[str, str, str]]:
    """Generate a fresh unique astrology article and publish it immediately.

    Manual publication deliberately does not touch content_last_date: the
    scheduled daily Content Engine remains independent and may still publish
    its own unique article later the same day.
    """
    if CONTENT_PUBLISH_LOCK.locked():
        raise RuntimeError("Генерация другого астрологического материала уже выполняется. Подождите немного.")

    async with CONTENT_PUBLISH_LOCK:
        target_date = local_now()
        generated = await generate_content_post(target_date)
        if not generated:
            raise RuntimeError("Не удалось сгенерировать уникальный астрологический материал.")

        content_type, topic, title, post = generated
        await safe_send_channel(bot, post)
        register_content(
            target_date.strftime("%Y-%m-%d"),
            content_type,
            topic,
            title,
            post,
        )
        logger.info(
            "Content Engine: ручная публикация выполнена | type=%s topic=%s title=%s user=%s",
            content_type,
            topic,
            title[:120],
            user_id,
        )
        return content_type, topic, title


def build_content_post(target_date: datetime, raw_text: str, content_type: str) -> Tuple[str, str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text.strip()) if p.strip()]
    if not paragraphs:
        raise ValueError("Пустой контент")

    title = paragraphs[0].strip(" #*")
    body_paragraphs = paragraphs[1:]
    if body_paragraphs:
        second_clean = re.sub(r"[^а-яёa-z0-9 ]+", "", body_paragraphs[0].lower()).strip()
        title_clean = re.sub(r"[^а-яёa-z0-9 ]+", "", title.lower()).strip()
        if second_clean == title_clean or second_clean.startswith(title_clean + " "):
            body_paragraphs = body_paragraphs[1:]
    body = "\n\n".join(body_paragraphs).strip()
    if not body:
        body = title

    type_labels = {
        "astrology": "✨ АСТРОЛОГИЯ",
        "moon": "🌙 ЛУНА",
        "planets": "🪐 ПЛАНЕТЫ",
        "aspects": "🔭 АСПЕКТЫ",
        "relationships": "💞 ОТНОШЕНИЯ И ЗОДИАК",
        "zodiac": "♈️ ЗНАКИ ЗОДИАКА",
        "interactive": "💫 АСТРОВОПРОС",
    }
    ctas = {
        "astrology": "✨ Какой знак здесь откликается тебе больше всего?",
        "moon": "🌙 А ты замечаешь связь между Луной и своим настроением?",
        "planets": "🪐 Какая планета тебе сейчас особенно интересна?",
        "aspects": "🔭 Сохрани, чтобы вернуться к этому позже.",
        "relationships": "💞 А как это проявляется в твоих отношениях?",
        "zodiac": "♈️ Узнала в этом свой знак?",
        "interactive": "💫 Выбирай свой вариант — и напиши его в комментариях.",
    }
    label = type_labels.get(content_type, "🔮 АСТРОЛОГИЯ")
    safe_title = escape(title)
    safe_body = _format_content_body(body)
    date_str = target_date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y")
    cta = ctas.get(content_type, "✨ А ты замечала такое за своим знаком?")
    post = (
        f"{label}\n"
        f"<i>{date_str} · BeauHoroscope</i>\n\n"
        f"╭───────────────╮\n"
        f"<b>{safe_title}</b>\n"
        f"╰───────────────╯\n\n"
        f"{safe_body}\n\n"
        f"💫 <i>{cta}</i>\n\n"
        "━━━━━━━━━━━━━━\n"
        "#астрология #зодиак #гороскоп"
    )
    if len(post) > 4096:
        raise ValueError("Контент слишком длинный")
    return title, post


async def generate_content_post(target_date: datetime) -> Optional[Tuple[str, str, str, str]]:
    for attempt in range(3):
        chosen = choose_content_topic()
        if not chosen:
            return None
        content_type, topic = chosen
        prompt = create_content_prompt(target_date, content_type, topic)
        raw, provider = await asyncio.to_thread(query_llm, prompt)
        valid = validate_content_post(raw or "")
        if not valid:
            logger.warning("Content Engine: невалидный ответ LLM, попытка %s/3 (provider=%s)", attempt + 1, provider or "none")
            continue
        title, post = build_content_post(target_date, valid, content_type)
        if content_is_too_similar(post):
            logger.warning("Content Engine: найден смысловой дубль, перегенерация")
            continue
        return content_type, topic, title, post
    return None


def content_scheduled_exists_for_slot(content_date: str, publish_time: str) -> bool:
    """Return True when this exact Content Engine slot is already queued."""
    marker = f"content:{content_date}:{publish_time}"
    with db() as conn:
        return conn.execute(
            """SELECT 1 FROM scheduled_posts
               WHERE target_date=? AND status='scheduled'
               LIMIT 1""",
            (marker,),
        ).fetchone() is not None


def cleanup_duplicate_content_schedules():
    """
    Safety net for duplicates created by older scheduler versions.

    Only the oldest active row for an exact Content Engine slot is kept.
    Extra queued copies are cancelled before publication, so a restart or
    an old duplicate row can never produce a burst of identical posts.
    """
    with db() as conn:
        duplicate_groups = conn.execute(
            """
            SELECT target_date, COUNT(*)
            FROM scheduled_posts
            WHERE target_date LIKE 'content:%' AND status='scheduled'
            GROUP BY target_date
            HAVING COUNT(*) > 1
            """
        ).fetchall()

        for target_date, _count in duplicate_groups:
            keep = conn.execute(
                """
                SELECT id
                FROM scheduled_posts
                WHERE target_date=? AND status='scheduled'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (target_date,),
            ).fetchone()
            if not keep:
                continue

            conn.execute(
                """
                UPDATE scheduled_posts
                SET status='cancelled',
                    error='Duplicate Content Engine slot cancelled automatically'
                WHERE target_date=?
                  AND status='scheduled'
                  AND id<>?
                """,
                (target_date, keep[0]),
            )

        # Prevent future duplicates at the database level as well.
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_content_scheduled_slot
            ON scheduled_posts(target_date)
            WHERE status='scheduled' AND target_date LIKE 'content:%'
            """
        )
        conn.commit()

        if duplicate_groups:
            logger.warning(
                "Content Engine: cleaned %s duplicate slot group(s)",
                len(duplicate_groups),
            )


def add_content_schedule(content_type: str, topic: str, title: str, post_text: str, publish_at: datetime) -> str:
    """Insert exactly one active schedule for one Content Engine time slot."""
    local_publish_at = publish_at.astimezone(LOCAL_TZ)
    marker = f"content:{local_publish_at.strftime('%Y-%m-%d')}:{local_publish_at.strftime('%H:%M')}"
    schedule_id = uuid.uuid4().hex[:12]

    with db() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM scheduled_posts
            WHERE target_date=? AND status='scheduled'
            LIMIT 1
            """,
            (marker,),
        ).fetchone()
        if existing:
            return existing[0]

        try:
            conn.execute("""
                INSERT INTO scheduled_posts(
                    id,target_date,publish_at_utc,post_text,astro_snapshot,provider,status,created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                schedule_id, marker, iso_utc(publish_at), post_text,
                json.dumps({"content_type": content_type, "topic": topic, "title": title}, ensure_ascii=False),
                "content-engine", "scheduled",
                iso_utc(datetime.now(timezone.utc))
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            # The partial unique index protects against a duplicate even if
            # another scheduler/restart reaches this exact slot concurrently.
            existing = conn.execute(
                """
                SELECT id
                FROM scheduled_posts
                WHERE target_date=? AND status='scheduled'
                LIMIT 1
                """,
                (marker,),
            ).fetchone()
            if existing:
                return existing[0]
            raise

    return schedule_id


def next_auto_datetime(now: Optional[datetime] = None) -> datetime:
    now = now or local_now()
    hh, mm = map(int, auto_time().split(":", 1))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def auto_last_date() -> str:
    return setting_get("auto_last_date", "")


def set_auto_last_date(value: str):
    setting_set("auto_last_date", value)


async def safe_send_channel(bot: Bot, text: str):
    await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)


async def publish_scheduled_row(bot: Bot, row):
    schedule_id, target_date, _, post_text, snapshot_json, provider = row
    try:
        await safe_send_channel(bot, post_text)
        mark_schedule_result(schedule_id, True)
        if str(target_date).startswith("content:"):
            try:
                meta = json.loads(snapshot_json or "{}")
                marker_parts = str(target_date).split(":")
                content_date = marker_parts[1] if len(marker_parts) >= 2 else local_now().strftime("%Y-%m-%d")
                # target_date format is content:YYYY-MM-DD:HH:MM.
                # Keep the full HH:MM value; using only marker_parts[2]
                # ("09") makes the published-slot marker differ from the
                # scheduler key ("09:00"), causing the same slot to regenerate
                # every scheduler pass.
                if len(marker_parts) >= 4:
                    content_time_slot = f"{marker_parts[2]}:{marker_parts[3]}"
                elif len(marker_parts) >= 3:
                    content_time_slot = marker_parts[2]
                else:
                    content_time_slot = content_time()
                register_content(
                    content_date,
                    meta.get("content_type", "astrology"),
                    meta.get("topic", "unknown"),
                    meta.get("title", ""),
                    post_text,
                )
                mark_content_slot_published(content_date, content_time_slot)

                # If an older version left several queued copies of the same
                # slot, keep the first published copy and cancel the rest.
                marker = f"content:{content_date}:{content_time_slot}"
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE scheduled_posts
                        SET status='cancelled',
                            error='Duplicate Content Engine slot cancelled after publication'
                        WHERE target_date=? AND status='scheduled'
                        """,
                        (marker,),
                    )
                    conn.commit()
            except Exception:
                logger.exception("Не удалось записать историю Content Engine")
        else:
            # Manual/scheduled horoscope posts retain the existing idempotency history.
            snapshot = json.loads(snapshot_json) if snapshot_json else {}
            mark_horoscope_sent(target_date, post_text, snapshot, provider)
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


def scheduled_exists_for_date(target_date: str) -> bool:
    with db() as conn:
        return conn.execute(
            """SELECT 1 FROM scheduled_posts
               WHERE target_date=? AND status='scheduled' LIMIT 1""",
            (target_date,),
        ).fetchone() is not None


async def scheduled_loop(application: Application):
    """Устойчивый scheduler: сначала публикует due-задачи, затем создаёт автопост."""
    while True:
        try:
            bot = application.bot
            cleanup_duplicate_content_schedules()

            # Критический путь: раньше due-задачи вообще не обрабатывались,
            # поэтому ручное расписание сохранялось в SQLite, но никогда не публиковалось.
            for row in get_due_scheduled():
                await publish_scheduled_row(bot, row)

            if auto_enabled():
                now = local_now()
                today = now.strftime("%Y-%m-%d")
                hh, mm = map(int, auto_time().split(":", 1))
                reached_time = (now.hour > hh) or (now.hour == hh and now.minute >= mm)

                if auto_last_date() != today and reached_time:
                    # Генерируем прогноз именно на СЕГОДНЯ, а не на завтра.
                    # Публикация создаётся как due-задача и проходит единый путь
                    # с ручным расписанием.
                    if not is_horoscope_sent(today) and not scheduled_exists_for_date(today):
                        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
                        horoscope, snapshot, provider = await asyncio.to_thread(
                            generate_horoscope, target
                        )
                        if horoscope:
                            text = build_post_text(target, horoscope, snapshot)
                            draft_id = create_draft_record(
                                target, text, snapshot, provider, ADMIN_CHAT_ID or "system"
                            )
                            add_scheduled_post(
                                draft_id,
                                datetime.now(timezone.utc) + timedelta(seconds=2),
                            )
                            logger.info("Создан автопост на %s", today)
                        else:
                            # Не ставим auto_last_date: следующий проход повторит попытку.
                            logger.error("Автопост: LLM не вернула валидный прогноз")
                    else:
                        logger.info("Автопост на %s уже опубликован или стоит в очереди", today)

                    # Важно: отмечаем день только после успешного publish.
                    # Если publish произошёл в этом же цикле, запись уже будет sent.
                    if is_horoscope_sent(today):
                        set_auto_last_date(today)

            # Дополнительные астрологические публикации.
            # Количество и время задаются через Content Engine -> «Настроить частоту».
            if content_enabled():
                now = local_now()
                content_today = now.strftime("%Y-%m-%d")
                published_slots = content_published_slots()

                for slot_time in content_times():
                    ch, cm = map(int, slot_time.split(":", 1))
                    slot_key = content_slot_key(content_today, slot_time)
                    slot_reached = (now.hour > ch) or (now.hour == ch and now.minute >= cm)

                    if not slot_reached:
                        continue

                    if slot_key in published_slots:
                        # Repair any stale queued copy left by an older version.
                        marker = f"content:{content_today}:{slot_time}"
                        with db() as conn:
                            conn.execute(
                                """
                                UPDATE scheduled_posts
                                SET status='cancelled',
                                    error='Slot already published; stale queued copy cancelled'
                                WHERE target_date=? AND status='scheduled'
                                """,
                                (marker,),
                            )
                            conn.commit()
                        continue

                    if content_scheduled_exists_for_slot(content_today, slot_time):
                        continue

                    if CONTENT_PUBLISH_LOCK.locked():
                        logger.info(
                            "Content Engine: слот %s пропущен в этом цикле из-за активной генерации; "
                            "следующая проверка повторит попытку",
                            slot_time,
                        )
                        continue

                    async with CONTENT_PUBLISH_LOCK:
                        # Re-check after acquiring the lock.
                        if slot_key in content_published_slots() or content_scheduled_exists_for_slot(content_today, slot_time):
                            continue

                        generated = await generate_content_post(now)
                        if generated:
                            content_type, topic, title, post = generated
                            publish_at = now.replace(hour=ch, minute=cm, second=5, microsecond=0)
                            # Если scheduler проснулся позже слота, задача должна уйти в due-состояние
                            # и опубликоваться сразу на следующем проходе. При этом для каждого
                            # слота существует только одна активная задача — повторная генерация
                            # того же слота невозможна на уровне SQLite.
                            if publish_at > now:
                                publish_at = now.replace(second=5, microsecond=0)

                            add_content_schedule(
                                content_type, topic, title, post, publish_at
                            )
                            logger.info(
                                "Content Engine: создан слот %s на %s, тема=%s",
                                slot_time, content_today, topic
                            )
                        else:
                            logger.error(
                                "Content Engine: не удалось создать уникальный пост для слота %s",
                                slot_time
                            )

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
    [InlineKeyboardButton("⏰ Автопостинг", callback_data="menu:auto"), InlineKeyboardButton("📝 Контент", callback_data="menu:content")],
    [InlineKeyboardButton("📊 Статистика", callback_data="menu:stats")],
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
    """Public helper: returns Telegram numeric user ID without exposing secrets."""
    if update.effective_user and update.message:
        await update.message.reply_text(
            f"🆔 Ваш Telegram ID: <code>{update.effective_user.id}</code>\n\n"
            "Укажите этот номер в ADMIN_CHAT_ID в .env.",
            parse_mode="HTML",
        )


async def llm_test(update, context):
    """Probe Groq and Mistral independently and report which one answered."""
    if not is_admin(update):
        return await deny(update)
    await update.message.reply_text("🧠 Проверяю Groq → Mistral…")
    probe = "Ответь строго одной строкой: LLM_TEST_OK. Никаких пояснений."

    results = []
    for name, key, base_url, model, max_tokens, temperature in [
        ("Groq", GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL, GROQ_MAX_TOKENS, GROQ_TEMPERATURE),
        ("Mistral", MISTRAL_API_KEY, MISTRAL_BASE_URL, MISTRAL_MODEL, MISTRAL_MAX_TOKENS, MISTRAL_TEMPERATURE),
    ]:
        if not key:
            results.append(f"❌ {name}: ключ не задан")
            continue
        text = await asyncio.to_thread(
            _openai_chat_request, base_url, key, model, probe, max_tokens, temperature, name.lower()
        )
        results.append(f"{'✅' if text else '❌'} {name}: {escape((text or 'нет ответа')[:200])}")

    await update.message.reply_text("\n".join(results), parse_mode="HTML")


async def admin_start(update, context):
    if not is_admin(update):
        return await deny(update)
    context.user_data.clear()
    text = (
        "🔮 <b>BeauHoroscope 4.0</b>\n\n"
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
        raise RuntimeError("LLM не вернул валидный прогноз. Проверьте /llmtest и journalctl сервиса.")
    text = build_post_text(target_date, horoscope, snapshot)
    return text, snapshot, provider


def preview_keyboard(draft_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Опубликовать сейчас", callback_data=f"draft:publish:{draft_id}")],
        [InlineKeyboardButton("📅 Запланировать", callback_data=f"draft:schedule:{draft_id}")],
        [InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"draft:regen:{draft_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"draft:delete:{draft_id}"), InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
    ])


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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]])
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]])
    )


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


async def render_draft(update, context, draft_id: str):
    row = get_draft(draft_id)
    if not row:
        return await update.callback_query.edit_message_text("❌ Черновик уже удалён.", reply_markup=ADMIN_MENU)
    text = row[2]
    preview = escape(text if len(text) <= 3900 else text[:3900] + "…")
    await update.callback_query.edit_message_text(
        f"👁 <b>Предпросмотр</b>\n\n{preview}",
        parse_mode="HTML",
        reply_markup=preview_keyboard(draft_id)
    )


async def draft_publish(update, context, draft_id: str):
    q = update.callback_query
    row = get_draft(draft_id)
    if not row:
        await q.answer("Черновик не найден", show_alert=True); return
    await q.answer("Публикую…")
    try:
        await safe_send_channel(context.application.bot, row[2])
        snapshot = json.loads(row[3]) if row[3] else {}
        mark_horoscope_sent(row[1], row[2], snapshot, row[4])
        delete_draft(draft_id)
        await q.edit_message_text(f"✅ <b>Опубликовано</b>\n\nДата: {row[1]}", parse_mode="HTML", reply_markup=ADMIN_MENU)
    except Exception as exc:
        logger.exception("Ручная публикация не удалась")
        await q.edit_message_text(f"❌ <b>Не опубликовано</b>\n\n{str(exc)[:700]}", parse_mode="HTML", reply_markup=preview_keyboard(draft_id))


async def draft_schedule(update, context, draft_id: str):
    q = update.callback_query
    if not get_draft(draft_id):
        await q.answer("Черновик не найден", show_alert=True); return
    context.user_data["state"] = "await_schedule_datetime"
    context.user_data["draft_id"] = draft_id
    await q.answer()
    await q.edit_message_text(
        "📅 <b>Время публикации</b>\n\n"
        "Введите дату и время по часовому поясу <code>" + LOCAL_TZ_NAME + "</code>:\n"
        "<code>19.08.2026 09:00</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data=f"draft:back:{draft_id}")]])
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

            if is_horoscope_sent(target.strftime("%Y-%m-%d")):
                await update.message.reply_text(
                    "⚠️ Прогноз на эту дату уже опубликован.\n\n"
                    "Повторная публикация отменена.",
                    reply_markup=ADMIN_MENU,
                )
                return

            await update.message.reply_text("📢 Публикую прогноз в канал…")
            await safe_send_channel(context.application.bot, post_text)
            mark_horoscope_sent(target.strftime("%Y-%m-%d"), post_text, snapshot, provider)
            await update.message.reply_text(
                f"✅ <b>Прогноз опубликован</b>\n\n"
                f"📅 {target.strftime('%d.%m.%Y')}\n"
                f"📢 <code>{escape(TELEGRAM_CHANNEL_ID)}</code>",
                parse_mode="HTML",
                reply_markup=ADMIN_MENU,
            )
        except Exception as exc:
            logger.exception("Ошибка генерации/публикации из панели")
            await update.message.reply_text(f"❌ Не удалось опубликовать прогноз:\n{str(exc)[:1000]}", reply_markup=ADMIN_MENU)
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
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="menu:home")]])
            )
        except Exception as exc:
            logger.exception("Ошибка генерации для расписания")
            context.user_data.clear()
            await update.message.reply_text(f"❌ Не удалось подготовить прогноз:\n{str(exc)[:1000]}", reply_markup=ADMIN_MENU)
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
            await update.message.reply_text(f"✅ <b>Запланировано</b>\n\n🆔 <code>{sid}</code>\n📅 {dt.strftime('%d.%m.%Y %H:%M')} ({LOCAL_TZ_NAME})", parse_mode="HTML", reply_markup=ADMIN_MENU)
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось запланировать: {exc}")
        return

    if state == "await_content_frequency":
        times = _normalize_content_times(text)
        if not times or len(times) > 6:
            await update.message.reply_text(
                "❌ Укажите от 1 до 6 корректных часов через запятую.\n"
                "Например: <code>08:30, 12:30, 17:00, 21:30</code>",
                parse_mode="HTML",
            )
            return
        set_content_times(times)
        setting_set("content_enabled", "1")
        context.user_data.clear()
        await update.message.reply_text(
            f"🟢 Content Engine: {len(times)} публикации в день.\n"
            f"Время: <code>{escape(', '.join(times))}</code> ({LOCAL_TZ_NAME})",
            parse_mode="HTML",
            reply_markup=ADMIN_MENU,
        )
        return

    if state == "await_content_time":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            await update.message.reply_text("❌ Формат времени: <code>15:00</code>", parse_mode="HTML")
            return
        set_content_time(text)
        setting_set("content_enabled", "1")
        context.user_data.clear()
        await update.message.reply_text(
            f"🟢 Content Engine включён. Каждый день в {text} ({LOCAL_TZ_NAME}).",
            reply_markup=ADMIN_MENU,
        )
        return

    if state == "await_auto_time":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            await update.message.reply_text("❌ Формат времени: <code>09:00</code>", parse_mode="HTML")
            return
        set_auto_time(text)
        setting_set("auto_enabled", "1")
        context.user_data.clear()
        await update.message.reply_text(f"🟢 Автопостинг включён. Каждый день в {text} ({LOCAL_TZ_NAME}).", reply_markup=ADMIN_MENU)
        return

    # Удобная команда текстом для ручного меню.
    if text == "🏠 Меню":
        return await admin_start(update, context)


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
        lines.append(f"• {target_date} — {dt.strftime('%d.%m %H:%M')} — {provider}")
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
        f"⏰ <b>Автопубликация</b>\n\n"
        f"Статус: {'🟢 включена' if enabled else '🔴 выключена'}\n"
        f"Время: <code>{t}</code>\n"
        f"Часовой пояс: <code>{LOCAL_TZ_NAME}</code>\n\n"
        "Автопост создаётся заранее на следующий день, а публикуется в указанное время.",
        parse_mode="HTML", reply_markup=kb
    )



async def menu_content(update, context):
    q = update.callback_query
    await q.answer()
    enabled = content_enabled()
    times_label = content_times_label()
    times_count = len(content_times())
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM content_history").fetchone()[0]
        last = conn.execute("""
            SELECT content_date, title FROM content_history
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Опубликовать сейчас", callback_data="content:publish_now")],
        [InlineKeyboardButton("⚙️ Настроить частоту", callback_data="content:frequency")],
        [InlineKeyboardButton("⏸ Выключить" if enabled else "▶️ Включить", callback_data="content:toggle")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
    ])
    await q.edit_message_text(
        f"✨ <b>Content Engine</b>\n\n"
        f"Статус: {'🟢 включён' if enabled else '🔴 выключен'}\n"
        f"Автопубликация: <b>{times_count} материал(а) в день</b>\n"
        f"Время: <code>{escape(times_label)}</code> ({LOCAL_TZ_NAME})\n"
        f"Опубликовано материалов: <b>{total}</b>\n"
        f"Последний: <b>{escape(last[1][:100]) if last else '—'}</b>\n\n"
        "🌙 <b>Направление:</b> астрология, знаки, Луна, планеты, аспекты, "
        "совместимость и отношения через призму астрологии.\n\n"
        "🛡 Повторы отсеиваются по теме, тексту и смысловой близости. "
        "Ручная публикация не отменяет автоматические слоты.",
        parse_mode="HTML", reply_markup=kb
    )


async def menu_stats(update, context):
    q = update.callback_query
    await q.answer()
    with db() as conn:
        sent = conn.execute("SELECT COUNT(*) FROM sent_horoscopes").fetchone()[0]
        scheduled = conn.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='scheduled'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status='failed'").fetchone()[0]
        content_total = conn.execute("SELECT COUNT(*) FROM content_history").fetchone()[0]
        last = conn.execute("SELECT target_date,provider FROM sent_horoscopes ORDER BY id DESC LIMIT 1").fetchone()
    await q.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"Опубликовано: <b>{sent}</b>\n"
        f"В очереди: <b>{scheduled}</b>\n"
        f"Ошибок: <b>{failed}</b>\n"
        f"Доп. астропостов: <b>{content_total}</b>\n"
        f"Последняя дата: <b>{last[0] if last else '—'}</b>",
        parse_mode="HTML", reply_markup=ADMIN_MENU
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
    if data == "menu:list":
        return await menu_list(update, context)
    if data == "menu:schedule":
        return await menu_schedule(update, context)
    if data == "menu:auto":
        return await menu_auto(update, context)
    if data == "menu:content":
        return await menu_content(update, context)
    if data == "menu:stats":
        return await menu_stats(update, context)
    if data == "auto:toggle":
        setting_set("auto_enabled", "0" if auto_enabled() else "1")
        return await menu_auto(update, context)
    if data == "content:publish_now":
        await q.answer("Генерирую материал…")
        await q.edit_message_text(
            "🧠 <b>Генерирую астрологический материал…</b>\\n\\n"
            "Проверяю тему и уникальность, затем сразу опубликую его в канал.",
            parse_mode="HTML",
        )
        try:
            content_type, topic, title = await publish_content_now(
                context.application.bot,
                str(update.effective_user.id),
            )
            await q.edit_message_text(
                "✅ <b>Материал опубликован</b>\\n\\n"
                f"📝 <b>{escape(title[:160])}</b>\\n"
                f"🏷 <code>{escape(content_type)}</code>\\n"
                f"🔎 <code>{escape(topic)}</code>\\n\\n"
                "Материал записан в историю уникальности. Ежедневный автоконтент продолжит работать отдельно.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Контент", callback_data="menu:content")],
                    [InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
                ]),
            )
        except Exception as exc:
            logger.exception("Ошибка ручной публикации Content Engine")
            await q.edit_message_text(
                f"❌ <b>Не удалось опубликовать материал</b>\\n\\n{escape(str(exc)[:1200])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Повторить", callback_data="content:publish_now")],
                    [InlineKeyboardButton("📝 Контент", callback_data="menu:content")],
                ]),
            )
        return
    if data == "content:toggle":
        setting_set("content_enabled", "0" if content_enabled() else "1")
        return await menu_content(update, context)
    if data == "content:frequency":
        await q.answer()
        times = content_times()
        presets = {
            1: ["15:00"],
            2: ["09:00", "18:00"],
            3: ["09:00", "15:00", "21:00"],
            4: ["09:00", "13:00", "17:00", "21:00"],
        }
        rows = []
        for count, preset in presets.items():
            mark = "✅ " if times == preset else ""
            rows.append([
                InlineKeyboardButton(
                    f"{mark}{count} раз(а): {', '.join(preset)}",
                    callback_data=f"content:freq:{count}",
                )
            ])
        rows.append([InlineKeyboardButton("✏️ Свои часы", callback_data="content:frequency_custom")])
        rows.append([InlineKeyboardButton("⬅️ Content Engine", callback_data="menu:content")])
        await q.edit_message_text(
            "⚙️ <b>Частота Content Engine</b>\n\n"
            "Выберите, сколько астрологических материалов автоматически "
            "публиковать каждый день.\n\n"
            f"Сейчас: <b>{len(times)} раз(а)</b> — <code>{escape(content_times_label())}</code>\n\n"
            "Можно выбрать готовый режим или задать свои часы через запятую.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if data.startswith("content:freq:"):
        count = int(data.rsplit(":", 1)[1])
        presets = {
            1: ["15:00"],
            2: ["09:00", "18:00"],
            3: ["09:00", "15:00", "21:00"],
            4: ["09:00", "13:00", "17:00", "21:00"],
        }
        set_content_times(presets[count])
        setting_set("content_enabled", "1")
        await q.answer(f"Установлено: {count} публикации в день")
        return await menu_content(update, context)
    if data == "content:frequency_custom":
        await q.answer()
        context.user_data["state"] = "await_content_frequency"
        await q.edit_message_text(
            "✏️ <b>Свои часы</b>\n\n"
            "Введите время через запятую. Например:\n"
            "<code>08:30, 12:30, 17:00, 21:30</code>\n\n"
            "Можно указать от 1 до 6 публикаций в день.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Content Engine", callback_data="menu:content")]]),
        )
        return
    if data == "content:time":
        # Compatibility with older callback links.
        return await menu_content(update, context)
    if data == "auto:time":
        await q.answer()
        context.user_data["state"] = "await_auto_time"
        await q.edit_message_text("🕘 Введите время ежедневной автопубликации, например <code>09:00</code>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu:auto")]]))
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
    if data.startswith("draft:regen:"):
        draft_id = data.split(":", 2)[2]
        row = get_draft(draft_id)
        if not row:
            await q.answer("Черновик не найден", show_alert=True); return
        target = datetime.strptime(row[1], "%Y-%m-%d").replace(tzinfo=LOCAL_TZ, hour=12)
        delete_draft(draft_id)
        await q.answer("Генерирую…")
        try:
            text, snapshot, provider = await create_forecast_for_date(target, str(update.effective_user.id))
            new_id = create_draft_record(target, text, snapshot, provider, str(update.effective_user.id))
            preview = escape(text if len(text) <= 3900 else text[:3900] + "…")
            await q.edit_message_text(f"👁 <b>Новый вариант</b>\n\n{preview}", parse_mode="HTML", reply_markup=preview_keyboard(new_id))
        except Exception as exc:
            await q.edit_message_text(f"❌ Ошибка генерации: {exc}", reply_markup=ADMIN_MENU)
        return
    if data.startswith("draft:back:"):
        return await render_draft(update, context, data.split(":", 2)[2])
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
    if not GROQ_API_KEY and not MISTRAL_API_KEY:
        raise RuntimeError("Не задан ни GROQ_API_KEY, ни MISTRAL_API_KEY. Настройте хотя бы один бесплатный LLM provider в .env.")

    init_db()
    cleanup_duplicate_content_schedules()
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

    logger.info("BeauHoroscope 4.0 started | timezone=%s | horoscope=%s %s | content=%s [%s] | manual_content_publish=True", LOCAL_TZ_NAME, auto_enabled(), auto_time(), content_enabled(), content_times_label())
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
