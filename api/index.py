## api/index.py
from flask import Flask, request, jsonify
import httpx
import json
import logging
import os
from datetime import datetime, timedelta

# logging.basicConfig(level=logging.INFO) - уровень логирования
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── КОНФІГ ────────────────────────────────────────────────
TOKEN    = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL", "https://colorflowtg.vercel.app")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN", "")

# ── ЗАВАНТАЖЕННЯ КОНФІГУ ─────────────────────────────────
def load_config():
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load config.json: {e}, using defaults")
        return {}

CONFIG = load_config()

# ── Функция проверки initData ────────────────────────────

import hashlib
import hmac
from urllib.parse import parse_qs

def verify_telegram_init_data(init_data: str) -> bool:
    """Проверяет подпись initData от Telegram WebApp."""
    if not init_data:
        return False
    data = parse_qs(init_data)
    if not data.get('hash'):
        return False
    received_hash = data['hash'][0]
    # Убираем hash из словаря для вычисления
    check_pairs = sorted([(k, v[0]) for k, v in data.items() if k != 'hash'])
    check_string = '\n'.join([f"{k}={v}" for k, v in check_pairs])
    # Вычисляем HMAC-SHA256
    secret_key = hashlib.sha256(TOKEN.encode()).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return computed_hash == received_hash

# ── функция для получения initData из запроса ──────

def get_init_data_from_request():
    """Извлекает initData из заголовка или тела запроса."""
    # Сначала проверяем заголовок
    init_data = request.headers.get("X-Telegram-InitData")
    if init_data:
        return init_data
    # Если нет – смотрим в теле (на случай, если передают в JSON)
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data.get("initData", "")
    return ""

ECONOMY = CONFIG.get('economy', {})

REWARD_PER_AD = ECONOMY.get('rewardPerAd', 5)
MIN_WITHDRAW_COINS = ECONOMY.get('minWithdrawCoins', 20)
WITHDRAW_THRESHOLD_USDT = ECONOMY.get('withdrawThresholdUSDT', 500)
REFERRAL_BONUS = ECONOMY.get('referralBonus', 50)
REFERRAL_NEW_USER_BONUS = ECONOMY.get('referralNewUserBonus', 150)
LEVEL_REWARD_BASE = ECONOMY.get('levelReward', {}).get('base', 10)
LEVEL_REWARD_BONUS = ECONOMY.get('levelReward', {}).get('bonusPerLevel', 0.5)

DAILY_BONUS_CFG = ECONOMY.get('dailyBonus', {})
DAILY_BASE = DAILY_BONUS_CFG.get('base', 10)
DAILY_STREAK_BONUS = DAILY_BONUS_CFG.get('streakBonus', 2)
DAILY_TIERS = DAILY_BONUS_CFG.get('tiers', [
    {"minStreak": 0, "maxStreak": 3, "baseBonus": 10},
    {"minStreak": 3, "maxStreak": 7, "baseBonus": 15},
    {"minStreak": 7, "maxStreak": 14, "baseBonus": 20},
    {"minStreak": 14, "maxStreak": 30, "baseBonus": 30},
    {"minStreak": 30, "maxStreak": 9999, "baseBonus": 50}
])

if TURSO_AUTH_TOKEN:
    logger.info(f"Turso token: {TURSO_AUTH_TOKEN[:10]}...")
else:
    logger.warning("⚠️ TURSO_AUTH_TOKEN not set — using in-memory")

app = Flask(__name__)

# ── IN-MEMORY FALLBACK ────────────────────────────────────
user_balances: dict = {}
user_bot_balances: dict = {}
user_refs:     dict = {}

# ── TURSO HELPERS ─────────────────────────────────────────
def query_turso(sql: str, params: list = None):
    logger.debug(f"QUERY: {sql} | PARAMS: {params}")
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return None
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type":  "application/json",
    }
    stmt = {"sql": sql}
    if params:
        stmt["args"] = [
            {"type": "integer" if isinstance(p, int) else "text", "value": str(p)}
            for p in params
        ]
    payload = {
        "requests": [
            {"type": "execute", "stmt": stmt},
            {"type": "close"},
        ]
    }
    try:
        r = httpx.post(f"{TURSO_DATABASE_URL}/v2/pipeline",
                       headers=headers, json=payload, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if "results" in data and len(data["results"]) > 0:
                result = data["results"][0]
                if result.get("type") == "error":
                    logger.error(f"Turso error: {result.get('error', {}).get('message', 'Unknown error')}")
                    return None
                if "response" in result:
                    return data
                else:
                    logger.error(f"Turso response missing 'response' key: {result}")
                    return None
            return data
        logger.error(f"Turso {r.status_code}: {r.text}")
        return None
    except Exception as e:
        logger.error(f"Turso exception: {e}")
        return None

# ── DATABASE INIT ─────────────────────────────────────────
def init_db():
    query_turso("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            balance     INTEGER DEFAULT 100,
            bot_balance INTEGER DEFAULT 0,
            ref_count   INTEGER DEFAULT 0,
            active_skin INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 1,
            username    TEXT,
            language    TEXT DEFAULT 'ru',
            referred_by INTEGER DEFAULT NULL
        )
    """)
    
    query_turso("""
        CREATE TABLE IF NOT EXISTS daily_bonus (
            user_id         INTEGER PRIMARY KEY,
            last_claim_date TEXT,
            streak          INTEGER DEFAULT 0
        )
    """)
    
    query_turso("""
        CREATE TABLE IF NOT EXISTS skins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            color       TEXT,
            bg_color    TEXT,
            bg_gradient TEXT,
            price       INTEGER,
            emoji       TEXT,
            is_default  INTEGER DEFAULT 0
        )
    """)

    # Создание таблицы user_skins для отслеживания купленных скинов
    query_turso("""
        CREATE TABLE IF NOT EXISTS user_skins (
            user_id INTEGER,
            skin_id INTEGER,
            PRIMARY KEY (user_id, skin_id)
        )
    """)

    # Добавление колонки referred_by, если её нет (для существующих БД)
      # Добавление колонки referred_by, если её нет (для существующих БД)
    try:
        pragma_res = query_turso("PRAGMA table_info(users)")
        if pragma_res:
            rows = pragma_res["results"][0].get("response", {}).get("result", {}).get("rows", [])
            columns = [row[1]["value"] for row in rows if len(row) > 1]  # имя колонки на позиции 1
            if "referred_by" not in columns:
                query_turso("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
    except Exception:
        pass

    # Индекс для ускорения поиска по referred_by
    query_turso("CREATE INDEX IF NOT EXISTS idx_referred_by ON users(referred_by)")

    query_turso("""
        INSERT OR IGNORE INTO skins (id, name, color, bg_color, bg_gradient, price, emoji, is_default)
        VALUES 
            (0, 'Класичний', '#FF453A', '#0a0a14', 'radial-gradient(circle at 20% 20%, #1a1a2e 0%, #0a0a14 90%)', 0, '🔴', 1),
            (1, 'Океан', '#007AFF', '#0a1628', 'radial-gradient(circle at 20% 20%, #0a2a4a 0%, #0a1628 90%)', 30, '🔵', 0),
            (2, 'Ліс', '#34C759', '#0a1a0a', 'radial-gradient(circle at 20% 20%, #1a3a1a 0%, #0a1a0a 90%)', 30, '🟢', 0),
            (3, 'Сонце', '#FF9F0A', '#2a1a0a', 'radial-gradient(circle at 20% 20%, #4a3a1a 0%, #2a1a0a 90%)', 30, '🟠', 0),
            (4, 'Фіолет', '#AF52DE', '#1a0a2a', 'radial-gradient(circle at 20% 20%, #3a1a4a 0%, #1a0a2a 90%)', 40, '🟣', 0),
            (5, 'Рожевий', '#FF2D92', '#2a0a1a', 'radial-gradient(circle at 20% 20%, #4a1a3a 0%, #2a0a1a 90%)', 40, '🌸', 0),
            (6, 'Золотий', '#FFD700', '#1a1a0a', 'radial-gradient(circle at 20% 20%, #3a3a1a 0%, #1a1a0a 90%)', 60, '⭐', 0),
            (7, 'Космос', '#5AC8FA', '#0a0a1a', 'radial-gradient(circle at 20% 20%, #1a1a4a 0%, #0a0a1a 90%)', 80, '🌌', 0),
            (8, 'Вогонь', '#FF6B35', '#2a0a00', 'radial-gradient(circle at 20% 20%, #4a1a0a 0%, #2a0a00 90%)', 50, '🔥', 0),
            (9, 'Крига', '#4FC3F7', '#0a1a2a', 'radial-gradient(circle at 20% 20%, #1a3a5a 0%, #0a1a2a 90%)', 50, '❄️', 0)
    """)
    
    logger.info("✅ DB initialized with username, language, referred_by and user_skins")

# ── BALANCE FUNCTIONS ────────────────────────────────────
def get_game_balance(user_id: int) -> int:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return user_balances.get(user_id, 100)
    res = query_turso("SELECT balance FROM users WHERE user_id = ?", [user_id])
    if res:
        try:
            result = res["results"][0].get("response")
            if not result:
                return 100
            rows = result.get("result", {}).get("rows", [])
            if rows:
                return int(rows[0][0]["value"])
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"get_game_balance parse error: {e}")
    return 100

def set_game_balance(user_id: int, balance: int) -> bool:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        user_balances[user_id] = balance
        return True
    logger.info(f"🔄 SET GAME BALANCE: user={user_id}, balance={balance}")
    result = query_turso(
        "UPDATE users SET balance = ? WHERE user_id = ?",
        [balance, user_id]
    )
    if result is None:
        logger.error(f"❌ set_game_balance FAILED for user {user_id}, balance {balance}")
        return False
    logger.info(f"✅ set_game_balance SUCCESS for user {user_id}, balance {balance}")
    return True
    
def get_bot_balance(user_id: int) -> int:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return user_bot_balances.get(user_id, 0)
    res = query_turso("SELECT bot_balance FROM users WHERE user_id = ?", [user_id])
    if res:
        try:
            result = res["results"][0].get("response")
            if not result:
                return 0
            rows = result.get("result", {}).get("rows", [])
            if rows:
                return int(rows[0][0]["value"])
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"get_bot_balance parse error: {e}")
    return 0

def set_bot_balance(user_id: int, balance: int) -> bool:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        user_bot_balances[user_id] = balance
        return True
    logger.info(f"🔄 SET BOT BALANCE: user={user_id}, balance={balance}")
    result = query_turso(
        "UPDATE users SET bot_balance = ? WHERE user_id = ?",
        [balance, user_id]
    )
    if result is None:
        logger.error(f"❌ set_bot_balance FAILED for user {user_id}, balance {balance}")
        return False
    logger.info(f"✅ set_bot_balance SUCCESS for user {user_id}, balance {balance}")
    return True

def add_game_balance(user_id: int, amount: int) -> int:
    current = get_game_balance(user_id)
    new_bal = current + amount
    set_game_balance(user_id, new_bal)
    return new_bal

def add_bot_balance(user_id: int, amount: int) -> int:
    current = get_bot_balance(user_id)
    new_bal = current + amount
    set_bot_balance(user_id, new_bal)
    return new_bal

def ensure_user(user_id: int, username: str = None, lang: str = 'ru'):
    """Создаёт пользователя в БД, если его ещё нет."""
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        if user_id not in user_balances:
            user_balances[user_id] = 100
        if user_id not in user_bot_balances:
            user_bot_balances[user_id] = 0
        return

    # Проверяем существование
    res = query_turso("SELECT user_id FROM users WHERE user_id = ?", [user_id])
    exists = False
    if res:
        try:
            rows = res["results"][0]["response"]["result"]["rows"]
            if rows:
                exists = True
        except (KeyError, IndexError, TypeError):
            pass

    if exists:
        if username:
            query_turso("UPDATE users SET username = ? WHERE user_id = ?", [username, user_id])
        # Также проверим, есть ли у пользователя дефолтный скин в user_skins
        # Если нет – добавим (это для старых пользователей)
        check_skin = query_turso("SELECT 1 FROM user_skins WHERE user_id = ? AND skin_id = 0", [user_id])
        if not check_skin or not check_skin.get("results") or not check_skin["results"][0].get("response", {}).get("result", {}).get("rows"):
            query_turso("INSERT OR IGNORE INTO user_skins (user_id, skin_id) VALUES (?, ?)", [user_id, 0])
        return

    # Создаём нового пользователя
    result = query_turso(
        "INSERT INTO users (user_id, username, language, balance, bot_balance, level) VALUES (?, ?, ?, ?, ?, ?)",
        [user_id, username, lang, 100, 0, 1]
    )
    if result is None:
        logger.error(f"❌ Failed to create user {user_id} (INSERT into users returned None)")
        return

    # Добавляем дефолтный скин
    skin_result = query_turso(
        "INSERT OR IGNORE INTO user_skins (user_id, skin_id) VALUES (?, ?)",
        [user_id, 0]
    )
    if skin_result is None:
        logger.warning(f"⚠️ Could not add default skin for user {user_id} (user_skins insert failed)")

    logger.info(f"✅ Created new user: {user_id} (username: {username})")
    
# ── LEVEL FUNCTIONS ──────────────────────────────────────
def get_level(user_id: int) -> int:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return 1
    res = query_turso("SELECT level FROM users WHERE user_id = ?", [user_id])
    if res:
        try:
            result = res["results"][0].get("response")
            if not result:
                return 1
            rows = result.get("result", {}).get("rows", [])
            if rows:
                return int(rows[0][0]["value"])
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"get_level parse error: {e}")
    return 1

def set_level(user_id: int, level: int):
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return
    query_turso(
        "UPDATE users SET level = ? WHERE user_id = ?",
        [level, user_id]
    )

# ── USERNAME & LANGUAGE FUNCTIONS ──────────────────────
def get_username(user_id: int) -> str:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return str(user_id)
    res = query_turso("SELECT username FROM users WHERE user_id = ?", [user_id])
    if res:
        try:
            rows = res["results"][0]["response"]["result"]["rows"]
            if rows and rows[0][0]["value"]:
                return rows[0][0]["value"]
        except (KeyError, IndexError, TypeError):
            pass
    return str(user_id)

def set_username(user_id: int, username: str):
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return
    query_turso(
        "UPDATE users SET username = ? WHERE user_id = ?",
        [username, user_id]
    )

def get_user_language(user_id: int) -> str:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return 'ru'
    res = query_turso("SELECT language FROM users WHERE user_id = ?", [user_id])
    if res:
        try:
            rows = res["results"][0]["response"]["result"]["rows"]
            if rows and rows[0][0]["value"]:
                return rows[0][0]["value"]
        except:
            pass
    return 'ru'

def set_user_language(user_id: int, lang: str):
    if lang not in ['uk', 'ru']:
        lang = 'ru'
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return
    query_turso(
        "UPDATE users SET language = ? WHERE user_id = ?",
        [lang, user_id]
    )

# ── ЛОКАЛІЗАЦІЯ ────────────────────────────────────────
LOCALES = {}
def load_locales():
    global LOCALES
    for lang in ['uk', 'ru']:
        path = os.path.join('locales', f'{lang}.json')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                LOCALES[lang] = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load locale {lang}: {e}")
            LOCALES[lang] = {}
    if 'ru' not in LOCALES:
        LOCALES['ru'] = {}
    if 'uk' not in LOCALES:
        LOCALES['uk'] = {}

def get_text(key: str, lang: str = 'ru', **kwargs) -> str:
    text = LOCALES.get(lang, {}).get(key) or LOCALES.get('ru', {}).get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            pass
    return text

load_locales()

# ── DAILY BONUS FUNCTIONS ──────────────────────────────
def get_daily_bonus_info(user_id: int) -> dict:
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        return {"can_claim": True, "streak": 0, "next_claim": None}
    res = query_turso(
        "SELECT last_claim_date, streak FROM daily_bonus WHERE user_id = ?",
        [user_id]
    )
    if res:
        try:
            rows = res["results"][0]["response"]["result"]["rows"]
            if rows:
                last_claim = rows[0][0]["value"]
                streak = int(rows[0][1]["value"])
                last_date = datetime.fromisoformat(last_claim)
                today = datetime.now().date()
                if last_date.date() == today:
                    return {"can_claim": False, "streak": streak, "next_claim": "Завтра"}
                elif last_date.date() == today - timedelta(days=1):
                    streak += 1
                else:
                    streak = 0
                return {"can_claim": True, "streak": streak, "next_claim": None}
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    return {"can_claim": True, "streak": 0, "next_claim": None}

def claim_daily_bonus(user_id: int) -> dict:
    info = get_daily_bonus_info(user_id)
    if not info["can_claim"]:
        return {"ok": False, "message": "Бонус вже отримано сьогодні!"}
    streak = info["streak"]
    tier_bonus = DAILY_BASE
    for tier in DAILY_TIERS:
        if tier["minStreak"] <= streak < tier["maxStreak"]:
            tier_bonus = tier["baseBonus"]
            break
    total_bonus = tier_bonus + streak * DAILY_STREAK_BONUS
    # Сначала проверяем, что баланс обновился
    old_balance = get_game_balance(user_id)
    new_balance = add_game_balance(user_id, total_bonus)
    if get_game_balance(user_id) != old_balance + total_bonus:
        return {"ok": False, "message": "Ошибка начисления бонуса"}
    # Теперь записываем факт получения
    now = datetime.now().isoformat()
    new_streak = streak + 1
    query_turso(
        "INSERT OR REPLACE INTO daily_bonus (user_id, last_claim_date, streak) VALUES (?, ?, ?)",
        [user_id, now, new_streak]
    )
    return {
        "ok": True,
        "bonus": total_bonus,
        "streak": new_streak,
        "new_balance": new_balance,
        "message": f"🎁 Ви отримали {total_bonus} монет за щоденний бонус! (Стрік: {new_streak} днів)"
    }

# ── SKINS FUNCTIONS ───────────────────────────────────────
def get_skins_from_db():
    try:
        res = query_turso("SELECT id, name, color, bg_color, bg_gradient, price, emoji, is_default FROM skins ORDER BY price")
        if not res:
            return {"ok": False, "error": "No response from database"}
        results = res.get("results", [])
        if not results:
            return {"ok": False, "error": "No results from database"}
        result = results[0]
        if result.get("type") == "error":
            error_msg = result.get("error", {}).get("message", "Unknown error")
            return {"ok": False, "error": error_msg}
        response = result.get("response")
        if not response:
            return {"ok": False, "error": "Invalid database response"}
        rows = response.get("result", {}).get("rows", [])
        skins = []
        for row in rows:
            skins.append({
                "id": row[0]["value"],
                "name": row[1]["value"],
                "color": row[2]["value"],
                "bg_color": row[3]["value"],
                "bg_gradient": row[4]["value"],
                "price": row[5]["value"],
                "emoji": row[6]["value"],
                "is_default": row[7]["value"]
            })
        return {"ok": True, "skins": skins}
    except Exception as e:
        logger.error(f"Get skins error: {e}")
        return {"ok": False, "error": str(e)}

# ── TELEGRAM HELPERS ──────────────────────────────────────
TG = f"https://api.telegram.org/bot{TOKEN}"

def tg_post(method: str, payload: dict) -> dict:
    try:
        r = httpx.post(f"{TG}/{method}", json=payload, timeout=10.0)
        return r.json()
    except Exception as e:
        logger.error(f"TG {method}: {e}")
        return {}

def send_msg(chat_id, text, keyboard=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        p["reply_markup"] = keyboard
    return tg_post("sendMessage", p)

def answer_cb(cb_id: str):
    return tg_post("answerCallbackQuery", {"callback_query_id": cb_id})

# ── ГОЛОВНЕ МЕНЮ ──────────────────────────────────────────
def main_kb(lang='ru'):
    return {
        "inline_keyboard": [
            [{"text": get_text("btn_open_game", lang), "web_app": {"url": f"{BASE_URL}?lang={lang}"}}],
            [
                {"text": get_text("btn_bot_balance", lang), "callback_data": "bot_balance"},
                {"text": get_text("btn_referral", lang), "callback_data": "referral"}
            ],
            [
                {"text": get_text("btn_top", lang), "callback_data": "top"},
                {"text": get_text("btn_daily", lang), "callback_data": "daily_bonus"}
            ],
            [
                {"text": get_text("btn_help", lang), "callback_data": "help"},
                {"text": get_text("btn_language", lang), "callback_data": "language"}
            ]
        ]
    }

# ── HANDLERS ──────────────────────────────────────────────
def handle_start(message: dict):
    user_id  = message["from"]["id"]
    username = message["from"].get("username") or message["from"].get("first_name", str(user_id))
    chat_id  = message["chat"]["id"]
    text     = message.get("text", "")

    lang = message["from"].get("language_code", "ru")
    if lang not in ['uk', 'ru']:
        lang = 'ru'
    set_user_language(user_id, lang)
    set_username(user_id, username)
    
    # ── создать пользователя, если его нет ──
    ensure_user(user_id, username, lang)
    
    parts = text.strip().split(" ", 1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref_id = int(parts[1].split("_")[1])
            if ref_id != user_id:
                already_referred = False
                if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
                    try:
                        res = query_turso("SELECT referred_by FROM users WHERE user_id = ?", [user_id])
                        if res:
                            # Безопасное извлечение данных
                            rows = []
                            try:
                                rows = res["results"][0].get("response", {}).get("result", {}).get("rows", [])
                            except (KeyError, IndexError, TypeError):
                                rows = []
                            if rows and len(rows) > 0 and len(rows[0]) > 0:
                                val = rows[0][0].get("value") if isinstance(rows[0][0], dict) else rows[0][0]
                                if val is not None:
                                    already_referred = True
                                    logger.info(f"User {user_id} already has referrer, ignoring referral")
                    except Exception as e:
                        logger.error(f"Error checking referred_by for {user_id}: {e}")
                        # В случае ошибки БД лучше не начислять бонус, чтобы избежать дублей?
                        # Решаем не начислять при ошибке, чтобы не рисковать.
                        already_referred = True

                if not already_referred and user_id not in user_refs:
                    try:
                        ensure_user(ref_id)
                        add_game_balance(ref_id, REFERRAL_BONUS)
                        set_game_balance(user_id, 100 + REFERRAL_NEW_USER_BONUS)
                        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
                            query_turso("UPDATE users SET ref_count = ref_count + 1 WHERE user_id = ?", [ref_id])
                            query_turso("UPDATE users SET referred_by = ? WHERE user_id = ?", [ref_id, user_id])
                        user_refs[user_id] = ref_id
                        logger.info(f"Referral: {user_id} from {ref_id}")
                    except Exception as e:
                        logger.error(f"Error processing referral bonus: {e}")
        except (ValueError, IndexError) as e:
            logger.error(f"Referral parse error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in referral handling: {e}")

    bal = get_game_balance(user_id)
    bot_bal = get_bot_balance(user_id)

    text_msg = get_text("welcome", lang,
                        username=username,
                        balance=bal,
                        bot_balance=bot_bal)
    send_msg(chat_id, text_msg, main_kb(lang))

def handle_callback(cb: dict):
    cb_id   = cb["id"]
    data    = cb["data"]
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]

    answer_cb(cb_id)
    lang = get_user_language(user_id)

    if data == "bot_balance":
        bot_bal = get_bot_balance(user_id)
        text = get_text("bot_balance", lang, balance=bot_bal, threshold=WITHDRAW_THRESHOLD_USDT)
        send_msg(chat_id, text, main_kb(lang))

    elif data == "referral":
        bot_info = tg_post("getMe", {})
        bot_name = bot_info.get("result", {}).get("username", "ColorFlowBot")
        ref_link = f"https://t.me/{bot_name}?start=ref_{user_id}"
        text = get_text("referral", lang,
                        link=ref_link,
                        bonus=REFERRAL_BONUS,
                        start_bonus=100+REFERRAL_BONUS)
        send_msg(chat_id, text, main_kb(lang))

    elif data == "help":
        text = get_text("help", lang, threshold=WITHDRAW_THRESHOLD_USDT)
        send_msg(chat_id, text, main_kb(lang))

    elif data == "top":
        try:
            response = httpx.get(f"{BASE_URL}/api/top", timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    top = data.get("top", [])
                    if top:
                        message = get_text("top_title", lang) + "\n\n"
                        for i, player in enumerate(top, 1):
                            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                            name = player.get('username') or str(player['user_id'])
                            message += get_text("top_entry", lang,
                                                medal=medal,
                                                name=name,
                                                balance=player['bot_balance']) + "\n"
                    else:
                        message = get_text("top_empty", lang)
                else:
                    message = "❌ Помилка отримання топу. Спробуй пізніше."
            else:
                message = "❌ Помилка отримання топу. Спробуй пізніше."
        except Exception as e:
            logger.error(f"Error getting top: {e}")
            message = "❌ Помилка отримання топу. Спробуй пізніше."
        send_msg(chat_id, message, main_kb(lang))

    elif data == "daily_bonus":
        info = get_daily_bonus_info(user_id)
        if info["can_claim"]:
            result = claim_daily_bonus(user_id)
            if result["ok"]:
                text = get_text("daily_claimed", lang,
                                bonus=result["bonus"],
                                streak=result["streak"],
                                new_balance=result["new_balance"])
                send_msg(chat_id, text, main_kb(lang))
            else:
                send_msg(chat_id, f"❌ {result['message']}", main_kb(lang))
        else:
            streak = info.get("streak", 0)
            text = get_text("daily_already", lang, streak=streak)
            send_msg(chat_id, text, main_kb(lang))

    elif data == "language":
        keyboard = {
            "inline_keyboard": [
                [{"text": "Українська", "callback_data": "setlang_uk"},
                 {"text": "Русский", "callback_data": "setlang_ru"}]
            ]
        }
        send_msg(chat_id, get_text("select_language", lang), keyboard)

    elif data.startswith("setlang_"):
        new_lang = data.split("_")[1]
        set_user_language(user_id, new_lang)
        lang = new_lang
        text = get_text("language_changed", lang)
        send_msg(chat_id, text, main_kb(lang))

# ── TOP ENDPOINT ──────────────────────────────────────────
@app.route("/api/top", methods=["GET"])
def api_top():
    try:
        if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
            return jsonify({"ok": False, "error": "Turso not available"}), 500
        res = query_turso(
            "SELECT user_id, username, bot_balance FROM users ORDER BY bot_balance DESC LIMIT 10"
        )
        if not res:
            return jsonify({"ok": True, "top": []})
        results = res.get("results")
        if not results:
            return jsonify({"ok": True, "top": []})
        response = results[0].get("response")
        if not response:
            return jsonify({"ok": True, "top": []})
        rows = response.get("result", {}).get("rows", [])
        top = []
        for row in rows:
            def safe_value(item):
                if isinstance(item, dict):
                    return item.get("value")
                return item
            user_id = safe_value(row[0]) if len(row) > 0 else None
            username = safe_value(row[1]) if len(row) > 1 else None
            bot_balance = safe_value(row[2]) if len(row) > 2 else None
            top.append({
                "user_id": user_id,
                "username": username,
                "bot_balance": bot_balance
            })
        return jsonify({"ok": True, "top": top})
    except Exception as e:
        logger.error(f"Error getting top: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── DAILY BONUS ENDPOINTS ──────────────────────────────
@app.route("/api/daily_bonus", methods=["GET"])
def api_daily_bonus():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        info = get_daily_bonus_info(user_id)
        return jsonify({"ok": True, **info})
    except Exception as e:
        logger.error(f"Daily bonus info error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/claim_daily_bonus", methods=["POST"])
def api_claim_daily_bonus():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        result = claim_daily_bonus(user_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Claim daily bonus error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── CONFIG ENDPOINT ──────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_config():
    try:
        return jsonify({
            "ok": True,
            "rewardPerAd": REWARD_PER_AD,
            "minWithdrawCoins": MIN_WITHDRAW_COINS,
            "withdrawThresholdUSDT": WITHDRAW_THRESHOLD_USDT,
            "referralBonus": REFERRAL_BONUS,
            "levelRewardBase": LEVEL_REWARD_BASE,
            "levelRewardBonus": LEVEL_REWARD_BONUS,
            "dailyBonusBase": DAILY_BASE,
            "dailyStreakBonus": DAILY_STREAK_BONUS,
        })
    except Exception as e:
        logger.error(f"Config error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── FLASK МАРШРУТИ ─-─────────────────────────────────────
@app.route("/api/bot", methods=["GET", "POST"])
def bot_webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "webhook": f"{BASE_URL}/api/bot"})
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"ok": True}), 200
        if "message" in data:
            msg = data["message"]
            if msg.get("text", "").startswith("/start"):
                handle_start(msg)
        elif "callback_query" in data:
            handle_callback(data["callback_query"])
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"ok": False, "error": str(e)}), 200

@app.route("/api/balance", methods=["GET"])
def api_balance():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        bal = get_game_balance(user_id)
        return jsonify({"ok": True, "balance": bal, "user_id": user_id})
    except Exception as e:
        logger.exception("Balance error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/bot_balance", methods=["GET"])
def api_bot_balance():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        bal = get_bot_balance(user_id)
        return jsonify({"ok": True, "bot_balance": bal, "user_id": user_id})
    except Exception as e:
        logger.exception("Bot balance error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/add_coins", methods=["POST"])
def api_add_coins():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        amount = int(data.get("amount", 0))
        if not user_id or amount <= 0:
            return jsonify({"ok": False, "error": "user_id and amount required"}), 400
        if amount > 1000:
            return jsonify({"ok": False, "error": "Amount too large"}), 400
        new_bal = add_game_balance(user_id, amount)
        logger.info(f"ADD COINS: user={user_id} +{amount} → {new_bal}")
        return jsonify({"ok": True, "new_balance": new_bal, "added": amount})
    except Exception as e:
        logger.exception("Add coins error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/level", methods=["GET"])
def api_level():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        level = get_level(user_id)
        return jsonify({"ok": True, "level": level, "user_id": user_id})
    except Exception as e:
        logger.exception("Level error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/update_level", methods=["POST"])
def api_update_level():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        level = int(data.get("level", 1))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        if level < 1:
            return jsonify({"ok": False, "error": "level must be >= 1"}), 400
        set_level(user_id, level)
        logger.info(f"UPDATE LEVEL: user={user_id} level={level}")
        return jsonify({"ok": True, "level": level, "user_id": user_id})
    except Exception as e:
        logger.exception("Update level error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/adsgram_reward", methods=["GET", "POST"])
def api_adsgram_reward():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        if request.method == "GET":
            user_id = request.args.get("user_id")
        else:
            data = request.get_json(force=True, silent=True) or {}
            user_id = data.get("user_id")
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        user_id = int(user_id)
        reward = REWARD_PER_AD
        new_bal = add_game_balance(user_id, reward)
        logger.info(f"ADSGRAM REWARD: user={user_id} +{reward} → {new_bal}")
        lang = get_user_language(user_id)
        text = get_text("ad_reward", lang, reward=reward, new_balance=new_bal)
        try:
            send_msg(user_id, text)
        except Exception:
            pass
        return jsonify({"ok": True, "new_balance": new_bal, "reward": reward})
    except Exception as e:
        logger.exception("Adsgram reward error")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── WITHDRAW (оновлена логіка з перевірками) ──────────
@app.route("/api/withdraw", methods=["POST"])
def api_withdraw():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        amount = int(data.get("amount", 0))
        logger.info(f"📥 WITHDRAW REQUEST: user_id={user_id}, amount={amount}")

        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        if amount < MIN_WITHDRAW_COINS:
            return jsonify({"ok": False, "error": f"Мінімум {MIN_WITHDRAW_COINS} монет для виведення"}), 400

        # 1. Поточний баланс
        game_bal_before = get_game_balance(user_id)
        logger.info(f"💰 GAME BALANCE BEFORE: user={user_id}, balance={game_bal_before}")

        if game_bal_before < amount:
            return jsonify({"ok": False, "error": "Недостатньо монет на ігровому балансі"}), 400
        
        # 2. Рассчитываем новый баланс
        new_game_bal = game_bal_before - amount
        new_bot_bal = get_bot_balance(user_id) + amount
        logger.info(f"🔢 NEW BALANCES: game={new_game_bal}, bot={new_bot_bal}")

        # 3. Атомарное обновление обоих балансов
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            sql = "UPDATE users SET balance = ?, bot_balance = ? WHERE user_id = ?"
            result = query_turso(sql, [new_game_bal, new_bot_bal, user_id])
            if result is None:
                logger.error(f"Atomic update failed for user {user_id}")
                return jsonify({"ok": False, "error": "Не вдалося оновити баланси"}), 500
            # Считываем свежие значения для проверки
            final_game_bal = get_game_balance(user_id)
            final_bot_bal = get_bot_balance(user_id)
        else:
            # in-memory fallback
            user_balances[user_id] = new_game_bal
            user_bot_balances[user_id] = new_bot_bal
            final_game_bal = new_game_bal
            final_bot_bal = new_bot_bal

        logger.info(f"✅ FINAL: user={user_id} game={final_game_bal} bot={final_bot_bal}")

        lang = get_user_language(user_id)
        text = get_text("withdraw_success", lang,
                        amount=amount,
                        game_balance=final_game_bal,
                        bot_balance=final_bot_bal,
                        threshold=WITHDRAW_THRESHOLD_USDT)
        try:
            send_msg(user_id, text)
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "withdrawn": amount,
            "new_balance": final_game_bal,
            "bot_balance": final_bot_bal,
        })
    except Exception as e:
        logger.exception("Withdraw error")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── SKINS ENDPOINTS ──────────────────────────────────────
@app.route("/api/skins", methods=["GET"])
def get_skins_endpoint():
    try:
        user_id = request.args.get("user_id", type=int)
        # Получаем список всех скинов
        res = query_turso("SELECT id, name, color, bg_color, bg_gradient, price, emoji, is_default FROM skins ORDER BY price")
        if not res:
            return jsonify({"ok": False, "error": "No response from database"}), 500
        result = res["results"][0]
        if result.get("type") == "error":
            return jsonify({"ok": False, "error": result.get("error", {}).get("message", "Unknown error")}), 500
        response = result.get("response")
        if not response:
            return jsonify({"ok": False, "error": "Invalid response"}), 500
        rows = response.get("result", {}).get("rows", [])
        skins = []
        for row in rows:
            skin = {
                "id": row[0]["value"],
                "name": row[1]["value"],
                "color": row[2]["value"],
                "bg_color": row[3]["value"],
                "bg_gradient": row[4]["value"],
                "price": row[5]["value"],
                "emoji": row[6]["value"],
                "is_default": row[7]["value"]
            }
            skins.append(skin)

        # Если передан user_id, добавляем флаг owned
        if user_id:
            # Получаем список купленных скинов пользователя
            owned_res = query_turso("SELECT skin_id FROM user_skins WHERE user_id = ?", [user_id])
            owned_ids = set()
            if owned_res:
                owned_rows = owned_res["results"][0].get("response", {}).get("result", {}).get("rows", [])
                for row in owned_rows:
                    owned_ids.add(row[0]["value"])
            for skin in skins:
                # Дефолтный скин всегда считается купленным
                if skin["id"] == 0:
                    skin["owned"] = True
                else:
                    skin["owned"] = skin["id"] in owned_ids
        else:
            # Если user_id не передан, owned не добавляем (или false по умолчанию)
            for skin in skins:
                skin["owned"] = False

        return jsonify({"ok": True, "skins": skins})
    except Exception as e:
        logger.error(f"Get skins error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/buy_skin", methods=["POST"])
def buy_skin():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        skin_id = int(data.get("skin_id", -1))
        if not user_id or skin_id < 0:
            return jsonify({"ok": False, "error": "user_id and skin_id required"}), 400
        if skin_id == 0:
            return jsonify({"ok": False, "error": "Дефолтный скин уже доступен"}), 400

        # Теперь запрос к БД – на том же уровне, что и if, но вне блока
        res = query_turso("SELECT price FROM skins WHERE id = ?", [skin_id])
        if not res:
            return jsonify({"ok": False, "error": "Skin not found"}), 404
        result = res["results"][0].get("response")
        if not result:
            return jsonify({"ok": False, "error": "Invalid response"}), 500
        rows = result.get("result", {}).get("rows", [])
        if not rows:
            return jsonify({"ok": False, "error": "Skin not found"}), 404
        price = int(rows[0][0]["value"])
        current_balance = get_game_balance(user_id)
        if current_balance < price:
            return jsonify({"ok": False, "error": f"Недостатньо монет! Потрібно {price}"}), 400

        # Проверяем, не куплен ли уже этот скин
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            check_res = query_turso(
                "SELECT 1 FROM user_skins WHERE user_id = ? AND skin_id = ?",
                [user_id, skin_id]
            )
            if check_res:
                rows = check_res["results"][0].get("response", {}).get("result", {}).get("rows", [])
                if rows:
                    return jsonify({"ok": False, "error": "Скин уже куплен"}), 400

        new_balance = current_balance - price
        set_game_balance(user_id, new_balance)
        query_turso("UPDATE users SET active_skin = ? WHERE user_id = ?", [skin_id, user_id])
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            query_turso(
                "INSERT INTO user_skins (user_id, skin_id) VALUES (?, ?)",
                [user_id, skin_id]
            )
        logger.info(f"USER {user_id} bought skin {skin_id} for {price} coins")
        return jsonify({
            "ok": True,
            "new_balance": new_balance,
            "skin_id": skin_id,
            "message": f"🎉 Скін куплено за {price} монет!"
        })
    except Exception as e:
        logger.error(f"Buy skin error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/activate_skin", methods=["POST"])
def activate_skin():
    init_data = get_init_data_from_request()
    if not verify_telegram_init_data(init_data):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = int(data.get("user_id", 0))
        skin_id = int(data.get("skin_id", -1))
        if not user_id or skin_id < 0:
            return jsonify({"ok": False, "error": "user_id and skin_id required"}), 400

        # Разрешаем активацию дефолтного скина (id=0) без проверки
        if skin_id != 0:
            # Проверяем, куплен ли скин (этот блок имеет отступ)
            if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
                check_res = query_turso(
                    "SELECT 1 FROM user_skins WHERE user_id = ? AND skin_id = ?",
                    [user_id, skin_id]
                )
                if not check_res:
                    return jsonify({"ok": False, "error": "Скин не куплен"}), 400
                rows = check_res["results"][0].get("response", {}).get("result", {}).get("rows", [])
                if not rows:
                    return jsonify({"ok": False, "error": "Скин не куплен"}), 400

        # Обновляем активный скин
        query_turso("UPDATE users SET active_skin = ? WHERE user_id = ?", [skin_id, user_id])
        logger.info(f"User {user_id} activated skin {skin_id}")
        return jsonify({"ok": True, "skin_id": skin_id, "message": "Скин активирован"})
    except Exception as e:
        logger.error(f"Activate skin error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

#эндпоинт для получения списка купленных скинов
@app.route("/api/user_skins", methods=["GET"])
def get_user_skins():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        res = query_turso(
            "SELECT s.id, s.name, s.color, s.bg_color, s.bg_gradient, s.price, s.emoji, s.is_default "
            "FROM user_skins us JOIN skins s ON us.skin_id = s.id "
            "WHERE us.user_id = ? ORDER BY s.price",
            [user_id]
        )
        if not res:
            return jsonify({"ok": True, "skins": []})
        rows = res["results"][0].get("response", {}).get("result", {}).get("rows", [])
        skins = []
        for row in rows:
            skins.append({
                "id": row[0]["value"],
                "name": row[1]["value"],
                "color": row[2]["value"],
                "bg_color": row[3]["value"],
                "bg_gradient": row[4]["value"],
                "price": row[5]["value"],
                "emoji": row[6]["value"],
                "is_default": row[7]["value"]
            })
        return jsonify({"ok": True, "skins": skins})
    except Exception as e:
        logger.error(f"Get user skins error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500    

@app.route("/api/active_skin", methods=["GET"])
def get_active_skin():
    try:
        user_id = int(request.args.get("user_id", 0))
        if not user_id:
            return jsonify({"ok": False, "error": "user_id required"}), 400
        res = query_turso("SELECT active_skin FROM users WHERE user_id = ?", [user_id])
        if res:
            result = res["results"][0].get("response")
            if result:
                rows = result.get("result", {}).get("rows", [])
                if rows:
                    skin_id = int(rows[0][0]["value"])
                    return jsonify({"ok": True, "skin_id": skin_id})
        return jsonify({"ok": True, "skin_id": 0})
    except Exception as e:
        logger.error(f"Get active skin error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/")
def root():
    return jsonify({
        "status": "running",
        "game": BASE_URL,
        "webhook": f"{BASE_URL}/api/bot",
        "turso": bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN),
    })

# ── INIT ──────────────────────────────────────────────────
try:
    init_db()
except Exception as e:
    logger.error(f"DB init: {e}")