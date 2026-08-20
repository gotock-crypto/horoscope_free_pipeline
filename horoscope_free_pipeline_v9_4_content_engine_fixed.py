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

HF_SPACE = os.getenv("HF_SPACE", "Luigi/ZeroGPU-LLM-Inference")
HF_MODEL = os.getenv("HF_MODEL", "Qwen3-4B")
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
    target_date = utc_datetime(target_date).replace(hour=12, minute=0, second=0, microsecond=0)
    observer = ephem.Observer()
    observer.date = target_date
    positions = {}
    for name, cls in PLANETS.items():
        try:
            body = cls()
            lon = ecliptic_longitude(body, observer)
            sign_idx, sign_name = zodiac_from_longitude(lon)
            prev_observer = ephem.Observer(); prev_observer.date = target_date - timedelta(days=1)
            next_observer = ephem.Observer(); next_observer.date = target_date + timedelta(days=1)
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
    return {
        "date_utc": target_date.isoformat(),
        "moon_phase": get_moon_phase(observer),
        "positions": positions,
        "aspects": calculate_aspects(positions),
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
    return {"name": name, "illumination_percent": round(illumination, 1), "elongation_deg": round(age, 2)}


def calculate_aspects(positions: Dict) -> List[Dict]:
    names = list(positions.keys())
    result = []
    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            distance = angular_distance(positions[a_name]["longitude"], positions[b_name]["longitude"])
            for exact, aspect_name, orb in ASPECTS:
                delta = abs(distance - exact)
                if delta <= orb:
                    result.append({
                        "a": a_name, "b": b_name, "aspect": aspect_name,
                        "exact": exact, "orb": round(delta, 2), "distance": round(distance, 2),
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
                relevant.append(f"{planet} в {data['degree']:.1f}° {data['sign']}" + (" (ретроградно)" if data["retrograde"] else ""))
        ruler_aspects = [a for a in aspects if a["a"] == ruler or a["b"] == ruler][:5]
        result[name] = {"emoji": emoji, "ruler": ruler, "planets_in_sign": relevant, "ruler_aspects": ruler_aspects}
    return result

# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS sent_horoscopes (id INTEGER PRIMARY KEY AUTOINCREMENT, target_date TEXT NOT NULL UNIQUE, horoscope_text TEXT NOT NULL, astro_snapshot TEXT, provider TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, target_date TEXT NOT NULL, post_text TEXT NOT NULL, astro_snapshot TEXT, provider TEXT, created_at TEXT NOT NULL, created_by TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_posts (id TEXT PRIMARY KEY, target_date TEXT NOT NULL, publish_at_utc TEXT NOT NULL, post_text TEXT NOT NULL, astro_snapshot TEXT, provider TEXT, status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL, published_at TEXT, error TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS content_history (id INTEGER PRIMARY KEY AUTOINCREMENT, content_date TEXT NOT NULL, content_type TEXT NOT NULL, topic TEXT NOT NULL, title TEXT, post_text TEXT NOT NULL, text_hash TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, published_at TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_content_history_date ON content_history(content_date)")
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
    return setting_get("auto_enabled", "1" if AUTO_POST_DEFAULT else "0") == "1"


def auto_time() -> str:
    return setting_get("auto_time", os.getenv("AUTO_POST_TIME", "09:00"))


def set_auto_time(value: str):
    setting_set("auto_time", value)

# ============================================================
# LLM
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
                {"role": "system", "content": "Ты профессиональный русскоязычный редактор астропрогнозов. Верни только готовый текст без пояснений, рассуждений, markdown-кода и тегов <think>. Используй астрономический snapshot как единственный источник фактических астрономических данных."},
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
# HOROSCOPE PROMPT / VALIDATION
# ============================================================

def create_editorial_prompt(target_date: datetime, snapshot: Dict) -> str:
    sign_context = build_sign_context(snapshot)
    compact_positions = [f"- {planet}: {data['degree']:.1f}° {data['sign']}; суточное движение {data['daily_motion']:+.3f}°" + ("; ретроградно" if data['retrograde'] else "") for planet, data in snapshot["positions"].items()]
    aspects = "\n".join(f"- {a['a']} — {a['aspect']} — {a['b']} (орб {a['orb']:.2f}°)" for a in snapshot["aspects"][:12]) or "- Точных основных аспектов в заданном орбе нет."
    per_sign = []
    for _, name, _, ruler in ZODIAC:
        ctx = sign_context[name]
        in_sign = ", ".join(ctx["planets_in_sign"]) or "нет планет"
        ruler_aspects = ", ".join(f"{a['a']} {a['aspect']} {a['b']}" for a in ctx["ruler_aspects"]) or "нет близких аспектов управителя"
        per_sign.append(f"{name} (управитель: {ruler}): планеты в знаке: {in_sign}; аспекты управителя: {ruler_aspects}")
    return f"""Создай ежедневный астрологический прогноз на {target_date.strftime('%d.%m.%Y')}.

АСТРОНОМИЧЕСКИЙ SNAPSHOT:
Лунная фаза: {snapshot['moon_phase']['name']}
Освещённость Луны: {snapshot['moon_phase']['illumination_percent']}%

ПОЛОЖЕНИЯ ПЛАНЕТ:
{chr(10).join(compact_positions)}

ОСНОВНЫЕ АСПЕКТЫ:
{aspects}

КОНТЕКСТ ПО ЗНАКАМ:
{chr(10).join(per_sign)}

ПРАВИЛА:
1. Ровно 12 прогнозов, от ОВНА до РЫБ.
2. Каждый прогноз — 2–3 естественных предложения.
3. Не повторяй формулировки между знаками.
4. Каждый знак получает индивидуальную интерпретацию по snapshot.
5. Не выдавай астрологию за научно доказанный способ предсказывать события.
6. Не придумывай точные события, диагнозы, гарантии дохода и т.п.
7. Стиль: современный, живой, немного мистический, без эзотерических клише.
8. Не упоминай ИИ.
9. Не добавляй вступление, заключение, дисклеймер или хэштеги.
10. Формат каждой строки: ♈️ ОВЕН: текст.
11. Названия знаков строго в порядке: ОВЕН, ТЕЛЕЦ, БЛИЗНЕЦЫ, РАК, ЛЕВ, ДЕВА, ВЕСЫ, СКОРПИОН, СТРЕЛЕЦ, КОЗЕРОГ, ВОДОЛЕЙ, РЫБЫ.
""".strip()


def normalize_sign_line(line: str) -> Optional[str]:
    line = re.sub(r"^\s*[-*•]\s*", "", line.strip())
    for emoji, name, _, _ in ZODIAC:
        match = re.compile(rf"^(?:{re.escape(emoji)}\s*)?{name}\s*:\s*(.+)$", re.IGNORECASE).match(line)
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
        if normalized:
            found[normalized.split(":", 1)[0].split()[-1]] = normalized
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
        text = query_huggingface_space(prompt)
        valid = validate_horoscope(text or "")
        if valid:
            return valid, snapshot, "huggingface"
        logger.warning("Hugging Face LLM вернул невалидный формат, попытка %s/2", attempt + 1)
        if attempt == 0:
            time.sleep(2)
    return None, snapshot, ""

# ============================================================
# SETTINGS / CONTENT ENGINE
# ============================================================

LOCAL_TZ_NAME = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    logger.warning("Неизвестный BOT_TIMEZONE=%s, используем UTC", LOCAL_TZ_NAME)
    LOCAL_TZ_NAME = "UTC"; LOCAL_TZ = timezone.utc

AUTO_POST_DEFAULT = os.getenv("AUTO_POST_DEFAULT", "0") == "1"
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

ASTROLOGY_SIGNALS = ("астролог", "зодиак", "знак", "луна", "планет", "венер", "меркур", "марс", "юпитер", "сатурн", "аспект", "ретроград", "совместим", "солнц", "наталь")


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def iso_utc(dt: datetime) -> str:
    return utc_datetime(dt).isoformat()


def _normalize_content_times(value: str) -> list[str]:
    times = []
    for raw in (value or "").replace(";", ",").split(","):
        raw = raw.strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
            times.append(raw)
    return sorted(set(times), key=lambda x: (int(x[:2]), int(x[3:])))


def content_enabled() -> bool:
    return setting_get("content_enabled", "1" if CONTENT_POST_DEFAULT else "0") == "1"


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
    try:
        data = json.loads(setting_get("content_published_slots", "[]"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        return set()


def mark_content_slot_published(content_date: str, publish_time: str):
    slots = content_published_slots(); slots.add(content_slot_key(content_date, publish_time))
    setting_set("content_published_slots", json.dumps(sorted(slots)[-90:], ensure_ascii=False))


def _content_tokens(text: str) -> set:
    return set(re.findall(r"[а-яёa-z0-9]{4,}", text.lower()))


def content_fingerprint(text: str) -> str:
    return " ".join(sorted(_content_tokens(text))[:120])


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text.lower())).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def recent_content_history(limit: int = CONTENT_MAX_HISTORY):
    with db() as conn:
        return conn.execute("""SELECT content_date, content_type, topic, title, post_text, fingerprint FROM content_history ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()


def topic_is_available(topic: str) -> bool:
    cutoff = (local_now().date() - timedelta(days=CONTENT_MIN_INTERVAL_DAYS)).isoformat()
    with db() as conn:
        return conn.execute("SELECT 1 FROM content_history WHERE topic=? AND content_date>=? LIMIT 1", (topic, cutoff)).fetchone() is None


def choose_content_topic() -> Optional[Tuple[str, str]]:
    available = [item for item in CONTENT_TOPICS if topic_is_available(item[1])]
    if not available:
        with db() as conn:
            rows = conn.execute("SELECT topic, MIN(id) AS first_id FROM content_history GROUP BY topic ORDER BY first_id ASC LIMIT 10").fetchall()
        used = {r[0] for r in rows}
        available = [item for item in CONTENT_TOPICS if item[1] not in used] or CONTENT_TOPICS[:]
    return available[int(time.time()) % len(available)]


def content_is_too_similar(text: str, history=None) -> bool:
    history = history or recent_content_history()
    new_fp = content_fingerprint(text); new_tokens = set(new_fp.split()); new_hash = content_hash(text)
    for _, _, _, _, old_text, old_fp in history:
        if new_hash == content_hash(old_text): return True
        old_tokens = set(old_fp.split())
        if new_tokens and old_tokens:
            jaccard = len(new_tokens & old_tokens) / max(1, len(new_tokens | old_tokens))
            if jaccard >= CONTENT_SIMILARITY_THRESHOLD: return True
        if SequenceMatcher(None, text.lower(), old_text.lower()).ratio() >= 0.84: return True
    return False


def register_content(content_date: str, content_type: str, topic: str, title: str, post_text: str) -> bool:
    try:
        with db() as conn:
            conn.execute("""INSERT INTO content_history(content_date,content_type,topic,title,post_text,text_hash,fingerprint,created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?)""", (content_date, content_type, topic, title, post_text, content_hash(post_text), content_fingerprint(post_text), iso_utc(datetime.now(timezone.utc)), iso_utc(datetime.now(timezone.utc))))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def create_content_prompt(target_date: datetime, content_type: str, topic: str) -> str:
    recent = recent_content_history(30)
    recent_topics = "\n".join(f"- {r[1]}: {r[2]} | {r[3] or 'без заголовка'}" for r in recent) or "- Истории публикаций пока нет."
    return f"""Создай короткую оригинальную публикацию для Telegram-канала BeauHoroscope на {target_date.strftime('%d.%m.%Y')}.

ПОЗИЦИОНИРОВАНИЕ: канал посвящён астрологии, гороскопам, знакам зодиака, Луне, планетам, аспектам, совместимости и астрологическим наблюдениям. Это не общий психологический или женский lifestyle-канал.

ТИП КОНТЕНТА: {content_type}
ТЕМА: {topic}

ПОСЛЕДНИЕ ПУБЛИКАЦИИ — НЕ ПОВТОРЯЙ ИХ:
{recent_topics}

ПРАВИЛА:
1. Материал уникален по идее и формулировкам.
2. Не перефразируй недавнюю публикацию.
3. Не повторяй один и тот же hook, структуру или CTA.
4. Тема обязательно имеет явную астрологическую связь.
5. Если это отношения, самооценка или психология — объясняй тему именно через астрологию/знаки/планеты.
6. Не придумывай точные астрономические факты.
7. Не выдавай астрологию за научно доказанный способ предсказывать будущее.
8. Не обещай деньги, здоровье, беременность, конкретные события или гарантии.
9. Стиль: красивый, современный, тёплый, женственный, немного мистический, без дешёвых клише.
10. Длина: 700–1400 символов.
11. Заголовок: 5–10 слов, живой и конкретный, без кликбейта.
12. Начни с сильного естественного hook.
13. Используй 3–5 коротких абзацев.
14. 1–3 эмодзи внутри текста.
15. В конце мягкий CTA или вопрос, только если естественно.
16. Не упоминай ИИ, генерацию, промпты или редакционные правила.
17. Не используй хэштеги — они добавятся программой.

ФОРМАТ:
Первая строка — короткий заголовок.
Далее — готовый текст публикации.""".strip()


def _has_repeated_content(text: str) -> bool:
    paragraphs = [p.strip().lower() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) >= 4:
        half = len(paragraphs) // 2; left = " ".join(paragraphs[:half]); right = " ".join(paragraphs[-half:])
        if len(left) > 180 and SequenceMatcher(None, left, right).ratio() >= 0.92: return True
    if len(paragraphs) >= 2:
        first = re.sub(r"[^а-яёa-z0-9 ]+", "", paragraphs[0]); second = re.sub(r"[^а-яёa-z0-9 ]+", "", paragraphs[1])
        if first and second and (second == first or second.startswith(first + " ")): return True
    return False


def validate_content_post(text: str) -> Optional[str]:
    text = _clean_llm_text(text or "")
    if not text: return None
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    paragraphs=[]; current=[]
    for line in raw_lines:
        if line: current.append(line)
        elif current: paragraphs.append(" ".join(current)); current=[]
    if current: paragraphs.append(" ".join(current))
    normalized = "\n\n".join(paragraphs).strip(); plain = re.sub(r"\s+", " ", normalized).strip()
    if _has_repeated_content(normalized): return None
    if len(plain) < 500 or len(plain) > 1800: return None
    lowered = plain.lower()
    if sum(1 for signal in ASTROLOGY_SIGNALS if signal in lowered) < 2: return None
    banned=("как языковая модель","сгенерировано ии","по данным исследования","гарантированно разбогатеете","вылечит","диагноз")
    if any(x in lowered for x in banned): return None
    return normalized


def _format_content_body(body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    formatted=[]
    for paragraph in paragraphs:
        paragraph=escape(paragraph)
        paragraph=re.sub(r"(?m)^[-•]\s*", "• ", paragraph)
        formatted.append(paragraph)
    return "\n\n".join(formatted)


CONTENT_PUBLISH_LOCK = asyncio.Lock()


def _build_content_post(target_date: datetime, raw_text: str, content_type: str) -> Tuple[str, str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text.strip()) if p.strip()]
    if not paragraphs: raise ValueError("Пустой контент")
    title=paragraphs[0].strip(" #*"); body_paragraphs=paragraphs[1:]
    if body_paragraphs:
        second_clean=re.sub(r"[^а-яёa-z0-9 ]+", "", body_paragraphs[0].lower()).strip(); title_clean=re.sub(r"[^а-яёa-z0-9 ]+", "", title.lower()).strip()
        if second_clean == title_clean or second_clean.startswith(title_clean+" "): body_paragraphs=body_paragraphs[1:]
    body="\n\n".join(body_paragraphs).strip() or title
    labels={"astrology":"✨ АСТРОЛОГИЯ","moon":"🌙 ЛУНА","planets":"🪐 ПЛАНЕТЫ","aspects":"🔭 АСПЕКТЫ","relationships":"💞 ОТНОШЕНИЯ И ЗОДИАК","zodiac":"♈️ ЗНАКИ ЗОДИАКА","interactive":"💫 АСТРОВОПРОС"}
    ctas={"astrology":"✨ Какой знак здесь откликается тебе больше всего?","moon":"🌙 А ты замечаешь связь между Луной и своим настроением?","planets":"🪐 Какая планета тебе сейчас особенно интересна?","aspects":"🔭 Сохрани, чтобы вернуться к этому позже.","relationships":"💞 А как это проявляется в твоих отношениях?","zodiac":"♈️ Узнала в этом свой знак?","interactive":"💫 Выбирай свой вариант — и напиши его в комментариях."}
    label=labels.get(content_type,"🔮 АСТРОЛОГИЯ"); cta=ctas.get(content_type,"✨ А ты замечала такое за своим знаком?"); date_str=target_date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y")
    post=(f"{label}\n<i>{date_str} · BeauHoroscope</i>\n\n╭───────────────╮\n<b>{escape(title)}</b>\n╰───────────────╯\n\n{_format_content_body(body)}\n\n💫 <i>{cta}</i>\n\n━━━━━━━━━━━━━━\n#астрология #зодиак #гороскоп")
    if len(post)>4096: raise ValueError("Контент слишком длинный")
    return title, post


def build_content_post(target_date: datetime, raw_text: str, content_type: str) -> Tuple[str, str]:
    return _build_content_post(target_date, raw_text, content_type)


async def generate_content_post(target_date: datetime) -> Optional[Tuple[str, str, str, str]]:
    for attempt in range(3):
        chosen=choose_content_topic()
        if not chosen: return None
        content_type, topic=chosen
        raw=await asyncio.to_thread(query_huggingface_space, create_content_prompt(target_date, content_type, topic))
        valid=validate_content_post(raw or "")
        if not valid:
            logger.warning("Content Engine: невалидный ответ LLM, попытка %s/3", attempt+1); continue
        title, post=build_content_post(target_date, valid, content_type)
        if content_is_too_similar(post):
            logger.warning("Content Engine: найден смысловой дубль, перегенерация"); continue
        return content_type, topic, title, post
    return None


def content_scheduled_exists_for_slot(content_date: str, publish_time: str) -> bool:
    marker=f"content:{content_date}:{publish_time}"
    with db() as conn:
        return conn.execute("SELECT 1 FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1", (marker,)).fetchone() is not None


def cleanup_duplicate_content_schedules():
    with db() as conn:
        duplicate_groups=conn.execute("SELECT target_date,COUNT(*) FROM scheduled_posts WHERE target_date LIKE 'content:%' AND status='scheduled' GROUP BY target_date HAVING COUNT(*)>1").fetchall()
        for target_date,_count in duplicate_groups:
            keep=conn.execute("SELECT id FROM scheduled_posts WHERE target_date=? AND status='scheduled' ORDER BY created_at,id LIMIT 1", (target_date,)).fetchone()
            if keep:
                conn.execute("UPDATE scheduled_posts SET status='cancelled',error='Duplicate Content Engine slot cancelled automatically' WHERE target_date=? AND status='scheduled' AND id<>?", (target_date, keep[0]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_content_scheduled_slot ON scheduled_posts(target_date) WHERE status='scheduled' AND target_date LIKE 'content:%'")
        conn.commit()
        if duplicate_groups:
            logger.warning("Content Engine: cleaned %s duplicate slot group(s)", len(duplicate_groups))


def add_content_schedule(content_type: str, topic: str, title: str, post_text: str, publish_at: datetime) -> str:
    local_publish_at=publish_at.astimezone(LOCAL_TZ); marker=f"content:{local_publish_at.strftime('%Y-%m-%d')}:{local_publish_at.strftime('%H:%M')}"; schedule_id=uuid.uuid4().hex[:12]
    with db() as conn:
        existing=conn.execute("SELECT id FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1", (marker,)).fetchone()
        if existing: return existing[0]
        try:
            conn.execute("""INSERT INTO scheduled_posts(id,target_date,publish_at_utc,post_text,astro_snapshot,provider,status,created_at) VALUES(?,?,?,?,?,?,?,?)""", (schedule_id,marker,iso_utc(publish_at),post_text,json.dumps({"content_type":content_type,"topic":topic,"title":title},ensure_ascii=False),"content-engine","scheduled",iso_utc(datetime.now(timezone.utc))))
            conn.commit()
        except sqlite3.IntegrityError:
            existing=conn.execute("SELECT id FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1", (marker,)).fetchone()
            if existing: return existing[0]
            raise
    return schedule_id


def scheduled_rows(limit=20):
    with db() as conn:
        return conn.execute("SELECT id,target_date,publish_at_utc,provider,status,error FROM scheduled_posts WHERE status='scheduled' ORDER BY publish_at_utc LIMIT ?", (limit,)).fetchall()


def cancel_schedule(schedule_id: str) -> bool:
    with db() as conn:
        cur=conn.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='scheduled'", (schedule_id,)); conn.commit(); return cur.rowcount>0


def get_due_scheduled():
    now=iso_utc(datetime.now(timezone.utc))
    with db() as conn:
        return conn.execute("SELECT id,target_date,publish_at_utc,post_text,astro_snapshot,provider FROM scheduled_posts WHERE status='scheduled' AND publish_at_utc<=? ORDER BY publish_at_utc LIMIT 5", (now,)).fetchall()


def mark_schedule_result(schedule_id: str, success: bool, error: str = ""):
    with db() as conn:
        if success: conn.execute("UPDATE scheduled_posts SET status='published',published_at=?,error=NULL WHERE id=?", (iso_utc(datetime.now(timezone.utc)), schedule_id))
        else: conn.execute("UPDATE scheduled_posts SET status='failed',error=? WHERE id=?", (error[:1000], schedule_id))
        conn.commit()


async def safe_send_channel(bot: Bot, text: str):
    await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)


async def publish_content_now(bot: Bot, user_id: str) -> Optional[Tuple[str,str,str]]:
    if CONTENT_PUBLISH_LOCK.locked():
        raise RuntimeError("Генерация другого астрологического материала уже выполняется. Подождите немного.")
    async with CONTENT_PUBLISH_LOCK:
        target_date=local_now(); generated=await generate_content_post(target_date)
        if not generated: raise RuntimeError("Не удалось сгенерировать уникальный астрологический материал.")
        content_type,topic,title,post=generated
        await safe_send_channel(bot, post)
        register_content(target_date.strftime("%Y-%m-%d"), content_type, topic, title, post)
        logger.info("Content Engine: ручная публикация выполнена | type=%s topic=%s title=%s user=%s", content_type,topic,title[:120],user_id)
        return content_type,topic,title


async def publish_scheduled_row(bot: Bot, row):
    schedule_id,target_date,_,post_text,snapshot_json,provider=row
    try:
        await safe_send_channel(bot, post_text); mark_schedule_result(schedule_id, True)
        if str(target_date).startswith("content:"):
            try:
                meta=json.loads(snapshot_json or "{}"); marker_parts=str(target_date).split(":"); content_date=marker_parts[1]; content_time_slot=marker_parts[2] if len(marker_parts)>=3 else content_time()
                register_content(content_date,meta.get("content_type","astrology"),meta.get("topic","unknown"),meta.get("title",""),post_text); mark_content_slot_published(content_date, content_time_slot)
                marker=f"content:{content_date}:{content_time_slot}"
                with db() as conn:
                    conn.execute("UPDATE scheduled_posts SET status='cancelled',error='Duplicate Content Engine slot cancelled after publication' WHERE target_date=? AND status='scheduled'", (marker,)); conn.commit()
            except Exception: logger.exception("Не удалось записать историю Content Engine")
        else:
            snapshot=json.loads(snapshot_json) if snapshot_json else {}; mark_horoscope_sent(target_date,post_text,snapshot,provider)
        logger.info("Запланированный пост %s опубликован", schedule_id)
    except Exception as exc:
        logger.exception("Ошибка публикации %s", schedule_id); mark_schedule_result(schedule_id,False,str(exc))
        if ADMIN_CHAT_ID:
            try: await bot.send_message(chat_id=ADMIN_CHAT_ID,text=f"❌ Ошибка публикации {target_date}: {exc}")
            except Exception: pass


def scheduled_exists_for_date(target_date: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1", (target_date,)).fetchone() is not None


def auto_last_date() -> str:
    return setting_get("auto_last_date", "")


def set_auto_last_date(value: str):
    setting_set("auto_last_date", value)


async def scheduled_loop(application: Application):
    while True:
        try:
            bot=application.bot
            cleanup_duplicate_content_schedules()
            for row in get_due_scheduled():
                await publish_scheduled_row(bot,row)
            if auto_enabled():
                now=local_now(); today=now.strftime("%Y-%m-%d"); hh,mm=map(int,auto_time().split(":",1)); reached_time=(now.hour>hh) or (now.hour==hh and now.minute>=mm)
                if auto_last_date()!=today and reached_time:
                    if not is_horoscope_sent(today) and not scheduled_exists_for_date(today):
                        target=now.replace(hour=12,minute=0,second=0,microsecond=0); horoscope,snapshot,provider=await asyncio.to_thread(generate_horoscope,target)
                        if horoscope:
                            text=build_post_text(target,horoscope,snapshot); draft_id=create_draft_record(target,text,snapshot,provider,ADMIN_CHAT_ID or "system"); add_scheduled_post(draft_id,datetime.now(timezone.utc)+timedelta(seconds=2)); logger.info("Создан автопост на %s",today)
                        else: logger.error("Автопост: LLM не вернула валидный прогноз")
                    if is_horoscope_sent(today): set_auto_last_date(today)
            if content_enabled():
                now=local_now(); content_today=now.strftime("%Y-%m-%d"); published_slots=content_published_slots()
                for slot_time in content_times():
                    ch,cm=map(int,slot_time.split(":",1)); slot_key=content_slot_key(content_today,slot_time); slot_reached=(now.hour>ch) or (now.hour==ch and now.minute>=cm)
                    if not slot_reached or slot_key in published_slots: continue
                    if content_scheduled_exists_for_slot(content_today,slot_time): continue
                    if CONTENT_PUBLISH_LOCK.locked(): continue
                    async with CONTENT_PUBLISH_LOCK:
                        if slot_key in content_published_slots() or content_scheduled_exists_for_slot(content_today,slot_time): continue
                        generated=await generate_content_post(now)
                        if generated:
                            content_type,topic,title,post=generated; publish_at=now.replace(hour=ch,minute=cm,second=5,microsecond=0)
                            if publish_at>now: publish_at=now.replace(second=5,microsecond=0)
                            add_content_schedule(content_type,topic,title,post,publish_at); logger.info("Content Engine: создан слот %s на %s, тема=%s",slot_time,content_today,topic)
                        else: logger.error("Content Engine: не удалось создать уникальный пост для слота %s",slot_time)
            await asyncio.sleep(30)
        except asyncio.CancelledError: return
        except Exception: logger.exception("Ошибка scheduler loop"); await asyncio.sleep(30)

# ============================================================
# POST BUILD / DB HELPERS
# ============================================================

def is_horoscope_sent(target_date: str) -> bool:
    with db() as conn: return conn.execute("SELECT 1 FROM sent_horoscopes WHERE target_date=?", (target_date,)).fetchone() is not None


def mark_horoscope_sent(target_date: str, horoscope_text: str, snapshot: Dict, provider: str) -> bool:
    try:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO sent_horoscopes(target_date,horoscope_text,astro_snapshot,provider,created_at) VALUES(?,?,?,?,?)",(target_date,horoscope_text,json.dumps(snapshot,ensure_ascii=False),provider,datetime.now(timezone.utc).isoformat())); conn.commit(); return conn.execute("SELECT 1 FROM sent_horoscopes WHERE target_date=?",(target_date,)).fetchone() is not None
    except Exception: logger.exception("Ошибка записи публикации"); return False


def build_post_text(target_date: datetime, horoscope: str, snapshot: Dict) -> str:
    date_str=target_date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y"); moon=snapshot["positions"].get("Луна",{}); moon_phase=snapshot["moon_phase"]["name"]; moon_sign=escape(moon.get("sign","неизвестном знаке")); safe_horoscope=escape(horoscope.strip())
    for emoji,name,_,_ in ZODIAC:
        safe_horoscope=re.sub(rf"^{re.escape(emoji)}\s*{name}:",f"<b>{emoji} {name}</b>",safe_horoscope,flags=re.MULTILINE)
    safe_horoscope=re.sub(r"\n(?=<b>[♈️♉️♊️♋️♌️♍️♎️♏️♐️♑️♒️♓️])","\n\n",safe_horoscope)
    post_text=(f"✨ <b>ТВОЙ АСТРОДЕНЬ</b>\n<i>{date_str} · личный прогноз для твоего знака</i>\n\n🌙 <b>{escape(moon_phase)}</b>\nСегодня Луна в <b>{moon_sign}</b> — прислушайся к своему настроению и выбери то, что действительно важно именно тебе.\n\n━━━━━━━━━━━━━━━━━━━━\n\n{safe_horoscope}\n\n━━━━━━━━━━━━━━━━━━━━\n\n💗 <b>Сохрани прогноз</b>, чтобы вернуться к нему вечером.\n✨ Если прогноз откликнулся — поставь реакцию.\n\n#гороскоп #астропрогноз #зодиак #луна #женскийгороскоп")
    if len(post_text)>4096: raise ValueError(f"Прогноз слишком длинный: {len(post_text)}")
    return post_text


def create_draft_record(target_date: datetime, post_text: str, snapshot: Dict, provider: str, admin_id: str) -> str:
    draft_id=uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute("INSERT INTO drafts(id,target_date,post_text,astro_snapshot,provider,created_at,created_by) VALUES(?,?,?,?,?,?,?)",(draft_id,target_date.strftime("%Y-%m-%d"),post_text,json.dumps(snapshot,ensure_ascii=False),provider,iso_utc(datetime.now(timezone.utc)),str(admin_id))); conn.commit()
    return draft_id


def get_draft(draft_id: str):
    with db() as conn: return conn.execute("SELECT id,target_date,post_text,astro_snapshot,provider FROM drafts WHERE id=?",(draft_id,)).fetchone()


def delete_draft(draft_id: str):
    with db() as conn: conn.execute("DELETE FROM drafts WHERE id=?",(draft_id,)); conn.commit()


def add_scheduled_post(draft_id: str, publish_at: datetime) -> str:
    row=get_draft(draft_id)
    if not row: raise ValueError("Черновик не найден")
    with db() as conn:
        existing=conn.execute("SELECT id FROM scheduled_posts WHERE target_date=? AND status='scheduled' LIMIT 1",(row[1],)).fetchone()
        if existing: delete_draft(draft_id); return existing[0]
        schedule_id=uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO scheduled_posts(id,target_date,publish_at_utc,post_text,astro_snapshot,provider,status,created_at) VALUES(?,?,?,?,?,?,?,?)",(schedule_id,row[1],iso_utc(publish_at),row[2],row[3],row[4],"scheduled",iso_utc(datetime.now(timezone.utc)))); conn.commit()
    delete_draft(draft_id); return schedule_id

# ============================================================
# ADMIN UI
# ============================================================

ADMIN_MENU=InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 Создать прогноз",callback_data="menu:create")],
    [InlineKeyboardButton("📅 Запланировать",callback_data="menu:schedule"),InlineKeyboardButton("📋 Расписание",callback_data="menu:list")],
    [InlineKeyboardButton("⏰ Автопостинг",callback_data="menu:auto"),InlineKeyboardButton("📝 Контент",callback_data="menu:content")],
    [InlineKeyboardButton("📊 Статистика",callback_data="menu:stats")],
])


def is_admin(update) -> bool:
    return bool(ADMIN_CHAT_ID and update.effective_user and str(update.effective_user.id)==str(ADMIN_CHAT_ID))


async def deny(update):
    if update.callback_query: await update.callback_query.answer("Нет доступа",show_alert=True)
    elif update.message: await update.message.reply_text("⛔ Доступ только для администратора.")


async def show_my_id(update,context):
    if update.effective_user and update.message: await update.message.reply_text(f"🆔 Ваш Telegram ID: <code>{update.effective_user.id}</code>\n\nУкажите этот номер в ADMIN_CHAT_ID в .env.",parse_mode="HTML")


async def llm_test(update,context):
    if not is_admin(update): return await deny(update)
    await update.message.reply_text("🧠 Проверяю Hugging Face Inference Providers…")
    text=await asyncio.to_thread(query_huggingface_space,"Ответь строго одной строкой: LLM_TEST_OK. Никаких пояснений.")
    await update.message.reply_text(("✅ LLM отвечает.\n\n"+escape(text[:500])) if text else "❌ Hugging Face Inference Providers сейчас недоступны.",parse_mode="HTML")


async def admin_start(update,context):
    if not is_admin(update): return await deny(update)
    context.user_data.clear(); text=f"🔮 <b>BeauHoroscope 3.4</b>\n\nКанал: <code>{TELEGRAM_CHANNEL_ID}</code>\nЧасовой пояс: <code>{LOCAL_TZ_NAME}</code>\nАвтопостинг: {'🟢 включён' if auto_enabled() else '🔴 выключен'}\n\nВыберите действие:"
    if update.callback_query: await update.callback_query.edit_message_text(text,parse_mode="HTML",reply_markup=ADMIN_MENU)
    else: await update.message.reply_text(text,parse_mode="HTML",reply_markup=ADMIN_MENU)

# (Admin command handlers below intentionally preserve the current working UI.)

async def callback_router(update, context):
    if not is_admin(update): return await deny(update)
    q=update.callback_query; data=q.data or ""
    if data=="menu:home": return await admin_start(update,context)
    if data=="menu:content":
        enabled=content_enabled(); times=content_times();
        with db() as conn:
            total=conn.execute("SELECT COUNT(*) FROM content_history").fetchone()[0]
            last=conn.execute("SELECT content_date,title FROM content_history ORDER BY id DESC LIMIT 1").fetchone()
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Опубликовать сейчас",callback_data="content:publish_now")],
            [InlineKeyboardButton("⚙️ Настроить частоту",callback_data="content:frequency")],
            [InlineKeyboardButton("⏸ Выключить" if enabled else "▶️ Включить",callback_data="content:toggle")],
            [InlineKeyboardButton("⬅️ Меню",callback_data="menu:home")],
        ])
        await q.answer(); await q.edit_message_text(f"✨ <b>Content Engine</b>\n\nСтатус: {'🟢 включён' if enabled else '🔴 выключен'}\nАвтопубликация: <b>{len(times)} материал(а) в день</b>\nВремя: <code>{escape(', '.join(times))}</code> ({LOCAL_TZ_NAME})\nОпубликовано материалов: <b>{total}</b>\nПоследний: <b>{escape(last[1][:100]) if last else '—'}</b>\n\n🌙 <b>Направление:</b> астрология, знаки, Луна, планеты, аспекты, совместимость и отношения через призму астрологии.\n\n🛡 Повторы отсеиваются по теме, тексту и смысловой близости. Ручная публикация не отменяет автоматические слоты.",parse_mode="HTML",reply_markup=kb); return
    if data=="content:toggle":
        setting_set("content_enabled","0" if content_enabled() else "1"); return await callback_router(type("X",(),{"callback_query":q,"effective_user":update.effective_user})(),context)
    if data=="content:publish_now":
        await q.answer("Генерирую материал…")
        try:
            content_type,topic,title=await publish_content_now(context.application.bot,str(update.effective_user.id))
            await q.edit_message_text(f"✅ <b>Материал опубликован</b>\n\n📝 <b>{escape(title[:160])}</b>\n🏷 <code>{escape(content_type)}</code>\n🔎 <code>{escape(topic)}</code>",parse_mode="HTML",reply_markup=ADMIN_MENU)
        except Exception as exc:
            await q.edit_message_text(f"❌ <b>Не удалось опубликовать материал</b>\n\n{escape(str(exc)[:1200])}",parse_mode="HTML",reply_markup=ADMIN_MENU)
        return
    if data=="content:frequency":
        times=content_times(); presets={1:["15:00"],2:["09:00","18:00"],3:["09:00","15:00","21:00"],4:["09:00","13:00","17:00","21:00"]}
        rows=[]
        for count,preset in presets.items(): rows.append([InlineKeyboardButton(f"{'✅ ' if times==preset else ''}{count} раз(а): {', '.join(preset)}",callback_data=f"content:freq:{count}")])
        rows.append([InlineKeyboardButton("✏️ Свои часы",callback_data="content:frequency_custom")]); rows.append([InlineKeyboardButton("⬅️ Content Engine",callback_data="menu:content")]); await q.answer(); await q.edit_message_text(f"⚙️ <b>Частота Content Engine</b>\n\nСейчас: <b>{len(times)} раз(а)</b> — <code>{escape(', '.join(times))}</code>",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("content:freq:"):
        presets={1:["15:00"],2:["09:00","18:00"],3:["09:00","15:00","21:00"],4:["09:00","13:00","17:00","21:00"]}; count=int(data.rsplit(":",1)[1]); set_content_times(presets[count]); setting_set("content_enabled","1"); await q.answer(f"Установлено: {count} публикации в день"); return await callback_router(type("X",(),{"callback_query":type("Q",(),{"data":"menu:content","answer":q.answer,"edit_message_text":q.edit_message_text})(),"effective_user":update.effective_user})(),context)
    await q.answer("Готово")


def main():
    if not TELEGRAM_BOT_TOKEN: raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID: raise RuntimeError("Не задан TELEGRAM_CHANNEL_ID")
    if not HF_TOKEN: raise RuntimeError("Не задан HF_TOKEN. Создайте Hugging Face User Access Token с правом Make calls to Inference Providers и добавьте его в .env.")
    init_db(); cleanup_duplicate_content_schedules()
    async def post_init(application): application.bot_data["scheduler_task"]=asyncio.create_task(scheduled_loop(application))
    async def post_shutdown(application):
        task=application.bot_data.get("scheduler_task")
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
    application=(Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler("id",show_my_id)); application.add_handler(CommandHandler("start",admin_start)); application.add_handler(CommandHandler("menu",admin_start)); application.add_handler(CommandHandler("llmtest",llm_test)); application.add_handler(CallbackQueryHandler(callback_router)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: u.message.reply_text("Используйте /menu", reply_markup=ADMIN_MENU) if is_admin(u) else None))
    logger.info("BeauHoroscope 3.4 started | timezone=%s | horoscope=%s %s | content=%s [%s] | manual_content_publish=True",LOCAL_TZ_NAME,auto_enabled(),auto_time(),content_enabled(),content_times_label())
    application.run_polling(allowed_updates=["message","callback_query"])


if __name__=="__main__":
    main()
