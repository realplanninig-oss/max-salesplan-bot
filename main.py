# File: main.py — бот Salesplan для MAX (с reply-кнопками, исправленный)

import asyncio
import logging
import sqlite3
import os
import json
import requests
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
import aiohttp
import aiofiles
import uvicorn

load_dotenv()

# === ДИАГНОСТИКА ПРИ ЗАПУСКЕ ===
print("=" * 60)
print("ENVIRONMENT VARIABLES CHECK - MAX Bot")
print("=" * 60)
print(f"MAX_BOT_TOKEN: {'✓ SET' if os.getenv('MAX_BOT_TOKEN') else '✗ MISSING'}")
if os.getenv('MAX_BOT_TOKEN'):
    token = os.getenv('MAX_BOT_TOKEN')
    print(f"  Length: {len(token)} characters")
    print(f"  First 10 chars: {token[:10]}...")
print(f"ADMIN_CHAT_ID: {os.getenv('ADMIN_CHAT_ID', '✗ MISSING')}")
print(f"DEEPSEEK_API_KEY: {'✓ SET' if os.getenv('DEEPSEEK_API_KEY') else '✗ MISSING'}")
print(f"YKASSA_SHOP_ID: {os.getenv('YKASSA_SHOP_ID', '✗ MISSING')}")
print(f"YKASSA_SECRET_KEY: {'✓ SET' if os.getenv('YKASSA_SECRET_KEY') else '✗ MISSING'}")
print(f"PORT: {os.getenv('PORT', '8000')}")
print("=" * 60)

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
YKASSA_SHOP_ID = os.getenv("YKASSA_SHOP_ID", "test")
YKASSA_SECRET_KEY = os.getenv("YKASSA_SECRET_KEY", "test")
YKASSA_TEST_MODE = os.getenv("YKASSA_TEST_MODE", "true").lower() == "true"

if not MAX_BOT_TOKEN:
    print("❌ ERROR: MAX_BOT_TOKEN not found in environment variables")
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found in environment variables")

MAX_API_URL = "https://platform-api.max.ru"
YKASSA_API_URL = "https://api.yookassa.ru/v3"

LOGS_DIR = Path("./logs")
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOGS_DIR / "salesplan.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logger.info("=" * 50)
logger.info("APPLICATION STARTING WITH CONFIGURATION:")
logger.info(f"MAX_BOT_TOKEN: {'✓ SET' if MAX_BOT_TOKEN else '✗ MISSING'}")
logger.info(f"ADMIN_CHAT_ID: {ADMIN_CHAT_ID if ADMIN_CHAT_ID else '✗ MISSING'}")
logger.info(f"DEEPSEEK_API_KEY: {'✓ SET' if DEEPSEEK_API_KEY else '✗ MISSING'}")
logger.info("=" * 50)

DB_PATH = "salesplan.db"
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(exist_ok=True)

# === СОСТОЯНИЯ ===
STATE_MENU = "menu"
STATE_AWAITING_BUSINESS_NAME = "awaiting_business_name"
STATE_AWAITING_BUSINESS_DESCRIPTION = "awaiting_business_description"
STATE_SURVEY = "survey"
STATE_WAITING_CALL = "waiting_call"

# === КОМАНДЫ ДЛЯ REPLY-КНОПОК ===
COMMAND_AUDIT = "📊 Бесплатный аудит"
COMMAND_PREMIUM = "🔥 План продаж за 490 ₽"
COMMAND_CONSULT = "👩‍💼 Бесплатная консультация"
COMMAND_HELP = "❓ Помощь"

# === ОПРОСНИК ===
Q1_SERVICE = "Услугу"
Q1_INFO = "Инфопродукт"
Q1_CONSULT = "Консультацию"
Q1_NONE = "Пока не продаю"
Q2_LT5 = "<5k"
Q2_5_20 = "5k-20k"
Q2_20_50 = "20k-50k"
Q2_50P = ">50k"
Q3_LT1K = "<10"
Q3_1_5K = "10-50"
Q3_5_20K = "50-200"
Q3_20KP = ">200"
Q4_300 = "300k/мес"
Q4_500 = "500k/мес"
Q4_1M = "1M/мес"
Q4_SCALE = "Масштаб"
Q5_YES = "Да"
Q5_NO = "Нет"
Q5_PROGRESS = "В разработке"

SURVEY_QUESTIONS = [
    {"key": "q1", "text": "Что ты продаёшь?", "options": [
        Q1_SERVICE,
        Q1_INFO,
        Q1_CONSULT,
        Q1_NONE,
    ]},
    {"key": "q2", "text": "Средний чек (₽)", "options": [
        Q2_LT5,
        Q2_5_20,
        Q2_20_50,
        Q2_50P,
    ]},
    {"key": "q3", "text": "Клиентов/мес (примерно)", "options": [
        Q3_LT1K,
        Q3_1_5K,
        Q3_5_20K,
        Q3_20KP,
    ]},
    {"key": "q4", "text": "Цель на 2026", "options": [
        Q4_300,
        Q4_500,
        Q4_1M,
        Q4_SCALE,
    ]},
    {"key": "q5", "text": "Уже есть автоворонка?", "options": [
        Q5_YES,
        Q5_NO,
        Q5_PROGRESS,
    ]},
]

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            payment_id TEXT UNIQUE,
            amount INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            report_text TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'generating',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ready_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS business_data (
            user_id TEXT PRIMARY KEY,
            business_name TEXT,
            business_description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forms (
            user_id TEXT PRIMARY KEY,
            q1 TEXT, q2 TEXT, q3 TEXT, q4 TEXT, q5 TEXT,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            user_id TEXT PRIMARY KEY,
            payment_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_state(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT state, data FROM user_state WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], json.loads(row[1]) if row[1] else {}
    return STATE_MENU, {}

def save_user_state(user_id: str, state: str, data: dict = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO user_state (user_id, state, data, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, state, json.dumps(data or {}, ensure_ascii=False)))
    conn.commit()
    conn.close()

def save_business_data(user_id: str, name: str, description: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO business_data (user_id, business_name, business_description)
        VALUES (?, ?, ?)
    """, (user_id, name, description))
    conn.commit()
    conn.close()

def get_business_data(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT business_name, business_description FROM business_data WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"name": row[0], "description": row[1]}
    return None

def save_form(user_id: str, answers: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO forms (user_id, q1, q2, q3, q4, q5)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, answers.get("q1"), answers.get("q2"), answers.get("q3"),
          answers.get("q4"), answers.get("q5")))
    conn.commit()
    conn.close()

def get_form(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT q1, q2, q3, q4, q5 FROM forms WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"q1": row[0], "q2": row[1], "q3": row[2], "q4": row[3], "q5": row[4]}
    return None

def save_report_request(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        INSERT INTO reports (user_id, report_type, status)
        VALUES (?, 'premium', 'generating')
    """, (user_id,))
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return report_id

def update_report_status(report_id: int, status: str, file_path: str = None):
    conn = sqlite3.connect(DB_PATH)
    if status == 'ready':
        conn.execute("""
            UPDATE reports SET status = ?, file_path = ?, ready_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (status, file_path, report_id))
    else:
        conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
    conn.commit()
    conn.close()

def get_report_status(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, status, file_path, ready_at FROM reports
        WHERE user_id = ? AND report_type = 'premium'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "status": row[1], "file_path": row[2], "ready_at": row[3]}
    return None

def save_pending_payment(user_id: str, payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO pending_payments (user_id, payment_id, created_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (user_id, payment_id))
    conn.commit()
    conn.close()

def get_pending_payment(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT payment_id FROM pending_payments WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def clear_pending_payment(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending_payments WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def format_moscow_time(dt=None):
    if dt is None:
        dt = get_moscow_time()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def log_event(user_id: str, event_type: str, event_data: str = None):
    logger.info(f"Event: {event_type} | User: {user_id} | Data: {event_data}")

# === ОТПРАВКА СООБЩЕНИЙ ===
async def send_message(chat_id: str, text: str, keyboard: list = None, one_time_keyboard: bool = False):
    """Отправка сообщения с reply-кнопками"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    if keyboard and len(keyboard) > 0:
        payload["attachments"] = [
            {
                "type": "reply_keyboard",
                "payload": {
                    "buttons": keyboard,
                    "resize": True,
                    "one_time": one_time_keyboard
                }
            }
        ]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_message failed: {resp.status} - {error_text}")
            try:
                return await resp.json()
            except:
                return {"error": "Failed to parse response"}

async def send_notification(chat_id: str, text: str):
    """Одноразовое уведомление без кнопок"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_notification failed: {resp.status} - {error_text}")
            try:
                return await resp.json()
            except:
                return {"error": "Failed to parse response"}

async def send_file_message(chat_id: str, text: str, file_path: str, file_type: str = "file"):
    """Отправка файла"""
    # Загружаем файл
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MAX_API_URL}/uploads?type={file_type}",
            headers={"Authorization": MAX_BOT_TOKEN}
        ) as resp:
            if resp.status != 200:
                logger.error(f"Failed to get upload URL: {await resp.text()}")
                await send_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
                return
            data = await resp.json()
            upload_url = data.get("url")
            if not upload_url:
                await send_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
                return
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            form_data = aiohttp.FormData()
            form_data.add_field('data', f, filename=Path(file_path).name)
            async with session.post(upload_url, data=form_data) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to upload file: {await resp.text()}")
                    await send_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
                    return
                result = await resp.json()
                if "token" in result:
                    attachment = {"type": file_type, "payload": {"token": result["token"]}}
                elif "result" in result and "url" in result["result"]:
                    attachment = {"type": file_type, "payload": {"url": result["result"]["url"]}}
                else:
                    attachment = {"type": file_type, "payload": result}
    
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {
        "text": text,
        "attachments": [attachment]
    }
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"Failed to send message with attachment: {await resp.text()}")
                await send_message(chat_id, f"{text}\n\n❌ Не удалось отправить файл")
            return await resp.json()

# === КЛАВИАТУРЫ (REPLY-КНОПКИ) ===
def get_main_menu_keyboard():
    """Главное меню с reply-кнопками"""
    return [
        [{"text": COMMAND_AUDIT}],
        [{"text": COMMAND_PREMIUM}],
        [{"text": COMMAND_CONSULT}],
        [{"text": COMMAND_HELP}]
    ]

def get_survey_keyboard(question_index: int):
    """Клавиатура для опросника (временная, одноразовая)"""
    if question_index >= len(SURVEY_QUESTIONS):
        return None
    q = SURVEY_QUESTIONS[question_index]
    keyboard = []
    for option in q["options"]:
        keyboard.append([{"text": option}])
    return keyboard

def get_format_choice_keyboard():
    """Выбор формата получения диагностики"""
    return [
        [{"text": "📝 В сообщении"}],
        [{"text": "📄 В файле .txt"}]
    ]

def get_post_download_keyboard():
    """Клавиатура после скачивания"""
    return [
        [{"text": COMMAND_CONSULT}],
        [{"text": "📚 Получить мини-курс"}]
    ]

def get_channel_subscribe_keyboard():
    """Клавиатура с подпиской на канал"""
    return [
        [{"text": "📢 Подписаться на канал"}]
    ]

# === ПЛАТЕЖИ ===
async def create_yookassa_payment(amount: int, description: str, user_id: str):
    payment_id = f"salesplan_{user_id}_{uuid.uuid4().hex[:8]}"
    payload = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": "https://max.ru"},
        "description": description,
        "capture": True,
        "metadata": {"user_id": user_id, "payment_id": payment_id}
    }
    auth = aiohttp.BasicAuth(YKASSA_SHOP_ID, YKASSA_SECRET_KEY)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{YKASSA_API_URL}/payments",
            json=payload,
            auth=auth,
            headers={"Idempotence-Key": payment_id}
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return {"payment_id": data["id"], "confirmation_url": data["confirmation"]["confirmation_url"]}
            else:
                error_text = await resp.text()
                logger.error(f"YooKassa error: {resp.status} - {error_text}")
                return None

async def check_yookassa_payment(payment_id: str):
    auth = aiohttp.BasicAuth(YKASSA_SHOP_ID, YKASSA_SECRET_KEY)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{YKASSA_API_URL}/payments/{payment_id}",
            auth=auth
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("status")
            else:
                logger.error(f"Failed to check payment: {await resp.text()}")
                return None

# === DEEPSEEK API ===
async def call_deepseek_diagnostic(name: str, description: str, answers: dict):
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not configured")
        return None
    
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {answers.get('q1', 'не указано')}
• Средний чек: {answers.get('q2', 'не указано')}
• Клиентов/мес: {answers.get('q3', 'не указано')}
• Цель на 2026: {answers.get('q4', 'не указано')}
• Автоворонка: {answers.get('q5', 'не указано')}
"""
    prompt = f"""Сделай профессиональный маркетинговый разбор онлайн-бизнеса на основе предоставленных данных.

ДАННЫЕ О БИЗНЕСЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши структурированный отчет на русском языке в разговорном стиле, включив:

1. ОБЩАЯ ИНФОРМАЦИЯ
   - Ниша бизнеса
   - Целевая аудитория (кто, их главная боль, какое решение ищут)
   - Оценка текущего уровня от 0 до 100

2. АНАЛИЗ
   - 3 сильные стороны
   - 3 зоны роста

3. ПЕРСОНАЛЬНЫЕ РЕКОМЕНДАЦИИ
   - 3 конкретных шага для увеличения продаж

ВАЖНО:
- Пиши как Вероника, продюсер экспертов. Живо, с эмодзи, с обращением на "ты"
- Не используй символы *, #, _ для форматирования
- Для списков используй дефисы (-)"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов с 8-летним опытом. Говоришь разговорно, с эмодзи, на 'ты'."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek error: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

async def generate_premium_report(user_id: str, name: str, description: str, answers: dict, report_id: int):
    logger.info(f"Generating premium report for {user_id}")
    if not DEEPSEEK_API_KEY:
        update_report_status(report_id, 'failed')
        logger.error("Cannot generate premium report: DEEPSEEK_API_KEY missing")
        return
    
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {answers.get('q1', 'не указано')}
• Средний чек: {answers.get('q2', 'не указано')}
• Клиентов/мес: {answers.get('q3', 'не указано')}
• Цель: {answers.get('q4', 'не указано')}
• Автоворонка: {answers.get('q5', 'не указано')}
"""
    prompt = f"""Сделай план продаж для онлайн-бизнеса.

ДАННЫЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши структурированный план на русском языке в разговорном стиле Вероники:

1. ОЦЕНКА СИТУАЦИИ
2. АНАЛИЗ РЫНКА И КОНКУРЕНТОВ (3-5 игроков)
3. КОМУ ПРОДАВАТЬ (ЦА)
4. СИЛЬНЫЕ И СЛАБЫЕ СТОРОНЫ
5. ВОРОНКА ПРОДАЖ ШАГ ЗА ШАГОМ
6. ПЛАН ДЕЙСТВИЙ НА МЕСЯЦ

Оформление:
- Заголовки ЗАГЛАВНЫМИ БУКВАМИ, отступы пустыми строками
- Списки через дефисы
- Пиши живо, с эмодзи, как Вероника"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=180)
        if response.status_code == 200:
            result = response.json()
            report_text = result["choices"][0]["message"]["content"]
            filename = f"Premium_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = REPORTS_DIR / filename
            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(report_text)
            update_report_status(report_id, 'ready', str(filepath))
            logger.info(f"Premium report generated for user {user_id}")
        else:
            update_report_status(report_id, 'failed')
            logger.error(f"Premium report API error: {response.status_code}")
    except Exception as e:
        logger.error(f"Premium report error: {e}")
        update_report_status(report_id, 'failed')

# === ОБРАБОТЧИК СООБЩЕНИЙ ===
async def process_message(user_id: str, text: str):
    """Обработка текстовых сообщений (включая нажатия на reply-кнопки)"""
    logger.info(f"PROCESS_MESSAGE called: user_id={user_id}, text={text}")
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")

    # Главное меню
    if state == STATE_MENU:
        if text == "/start":
            await send_message(str(user_id),
                "Привет! Я Вероника, продюсер экспертов.\n\n"
                "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
                "Давай сделаем бесплатный аудит твоего канала — 2 минуты, и узнаешь, что теряешь.\n\n"
                "👇 Нажми на кнопку ниже:",
                get_main_menu_keyboard())
            save_user_state(str(user_id), STATE_MENU, {})
            return
        
        elif text == COMMAND_AUDIT:
            save_user_state(str(user_id), STATE_AWAITING_BUSINESS_NAME, {"answers": {}, "survey_step": 0})
            await send_message(str(user_id),
                "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса (как ты представляешь его клиентам):",
                None)
            return
        
        elif text == COMMAND_PREMIUM:
            biz_data = get_business_data(str(user_id))
            form_data = get_form(str(user_id))
            if not biz_data or not form_data:
                await send_message(str(user_id),
                    "Ой, стоп! Сначала нужно пройти бесплатную диагностику, чтобы я поняла, про что твой бизнес.\n\n"
                    "Это быстро — 2 минуты, честно 👇",
                    get_main_menu_keyboard())
                return
            
            # Создаем платеж
            payment = await create_yookassa_payment(490, "План продаж Salesplan", str(user_id))
            if payment and payment.get("confirmation_url"):
                save_pending_payment(str(user_id), payment["payment_id"])
                await send_message(str(user_id),
                    f"🔍 Так, уже запускаю генерацию твоего плана продаж... Обычно это занимает 5-10 минут.\n\n"
                    f"Оплати 490 ₽ по ссылке: {payment['confirmation_url']}\n\n"
                    f"После оплаты напиши «Оплатил» или нажми /start",
                    None)
                
                report_id = save_report_request(str(user_id))
                asyncio.create_task(generate_premium_report(str(user_id), biz_data["name"], biz_data["description"], form_data, report_id))
            else:
                await send_message(str(user_id),
                    "❌ Ошибка при создании платежа. Попробуй позже.",
                    get_main_menu_keyboard())
            return
        
        elif text == COMMAND_CONSULT:
            save_user_state(str(user_id), STATE_WAITING_CALL, {})
            await send_message(str(user_id),
                "Привет! Я Вероника Макаревич.\n\n"
                "8 лет назад я начинала с нуля, а сегодня у моих клиентов запуски на 2 млн за 2 недели.\n\n"
                "Напиши в одном сообщении:\n"
                "🔗 Ссылку на твой бизнес (канал, сайт)\n"
                "👤 Твой username\n"
                "🕐 Удобное время для созвона (по Москве)\n\n"
                "👇 Жду",
                None)
            return
        
        elif text == COMMAND_HELP:
            await send_message(str(user_id),
                "❓ Помощь\n\n"
                "Доступные команды:\n"
                "• /start - начать сначала\n"
                "• 📊 Бесплатный аудит - пройти диагностику\n"
                "• 🔥 План продаж за 490 ₽ - получить полный план\n"
                "• 👩‍💼 Бесплатная консультация - записаться на разбор\n\n"
                "По всем вопросам: @veronika_makarevich",
                get_main_menu_keyboard())
            return
        
        # Проверяем, есть ли сохраненный отчет для выбора формата
        if "generated_report" in data:
            if text == "📝 В сообщении":
                report_text = data.get("generated_report")
                if report_text:
                    max_len = 3800
                    if len(report_text) > max_len:
                        await send_message(str(user_id), "✅ Твоя диагностика:\n\n" + report_text[:max_len], None)
                        await send_notification(str(user_id), report_text[max_len:max_len+max_len])
                    else:
                        await send_message(str(user_id), "✅ Твоя диагностика:\n\n" + report_text, None)
                await send_message(str(user_id),
                    "🔥 Ну как тебе?\n\n"
                    "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
                    "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.",
                    get_main_menu_keyboard())
                save_user_state(str(user_id), STATE_MENU, {})
                return
            
            elif text == "📄 В файле .txt":
                report_text = data.get("generated_report")
                title = data.get("report_title", "business")
                if report_text:
                    filename = f"Diagnostic_{title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    filepath = REPORTS_DIR / filename
                    async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                        await f.write(report_text)
                    await send_file_message(
                        str(user_id),
                        "📄 Твоя бесплатная диагностика",
                        str(filepath),
                        "file"
                    )
                await send_message(str(user_id),
                    "🔥 Ну как тебе?\n\n"
                    "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
                    "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.",
                    get_main_menu_keyboard())
                save_user_state(str(user_id), STATE_MENU, {})
                return
        
        # Обработка "Оплатил"
        if text.lower() in ["оплатил", "я оплатил", "оплатила", "я оплатила"]:
            payment_id = get_pending_payment(str(user_id))
            if payment_id:
                status = await check_yookassa_payment(payment_id)
                if status == "succeeded":
                    log_event(str(user_id), "payment_made", payment_id)
                    clear_pending_payment(str(user_id))
                    biz_data = get_business_data(str(user_id))
                    await send_notification(ADMIN_CHAT_ID,
                        f"💰 ПОЛУЧЕНА ОПЛАТА\n\nПользователь: {user_id}\nБизнес: {biz_data['name'] if biz_data else 'не указан'}\nСумма: 490 ₽\n⏰ {format_moscow_time()}")
                    
                    report_status = get_report_status(str(user_id))
                    if report_status and report_status['status'] == 'ready' and report_status['file_path']:
                        filepath = Path(report_status['file_path'])
                        if filepath.exists():
                            await send_file_message(
                                str(user_id),
                                "🎉 Ура! Твой план продаж готов!\n\n"
                                "Я подготовила для тебя персональную стратегию с анализом конкурентов и пошаговым планом.",
                                str(filepath),
                                "file"
                            )
                            await send_message(str(user_id),
                                "👇 Жми кнопку, получи мини-курс",
                                get_post_download_keyboard())
                        else:
                            await send_message(str(user_id),
                                "❌ Ой, файл не найден. Напиши мне в личные сообщения — поможем.",
                                get_main_menu_keyboard())
                    else:
                        await send_message(str(user_id),
                            "✅ Оплата прошла, спасибо!\n\n"
                            "План ещё готовится — обычно 5-10 минут. Я пришлю уведомление, как только всё будет готово.",
                            get_main_menu_keyboard())
                elif status == "pending":
                    await send_message(str(user_id),
                        "⏳ Платёж ещё не подтверждён. Подожди немного и напиши «Оплатил» снова.\n\n"
                        "Если деньги уже списались — напиши «Помощь».",
                        get_main_menu_keyboard())
                else:
                    await send_message(str(user_id),
                        "❌ Платёж не найден или отменён. Попробуй оплатить снова.",
                        get_main_menu_keyboard())
            else:
                await send_message(str(user_id),
                    "❌ Не могу найти информацию о платеже. Попробуй оплатить снова.",
                    get_main_menu_keyboard())
            return
        
        # Обработка "Подписаться на канал"
        if text == "📢 Подписаться на канал":
            await send_message(str(user_id),
                "🔗 Подпишись на мой канал в MAX:\nhttps://max.ru/id781407988795_biz\n\n"
                "После подписки напиши «Подписался», и я пришлю мини-курс!",
                None)
            return
        
        if text == "📚 Получить мини-курс":
            await send_message(str(user_id),
                "🎁 Мини-курс «3 шага к первой продаже»:\n\n"
                "1. Определи свою целевую аудиторию\n"
                "2. Создай оффер, от которого нельзя отказаться\n"
                "3. Настрой простую воронку из 3 сообщений\n\n"
                "Хочешь подробный разбор? Запишись на консультацию!",
                get_main_menu_keyboard())
            return

        await send_message(str(user_id), "Используй кнопки меню или напиши /start", get_main_menu_keyboard())
        return

    # Ожидание названия бизнеса
    if state == STATE_AWAITING_BUSINESS_NAME:
        if len(text) > 100:
            await send_message(str(user_id), "Слишком длинное название. Напиши покороче (до 100 символов):")
            return
        save_user_state(str(user_id), STATE_AWAITING_BUSINESS_DESCRIPTION, {"business_name": text})
        await send_message(str(user_id), "Ок, записала! Теперь напиши краткое описание бизнеса — что ты делаешь, кому помогаешь:")
        return

    # Ожидание описания бизнеса
    if state == STATE_AWAITING_BUSINESS_DESCRIPTION:
        if len(text) > 500:
            await send_message(str(user_id), "Описание слишком длинное. Напиши покороче (до 500 символов):")
            return
        business_name = data.get("business_name")
        save_business_data(str(user_id), business_name, text)
        log_event(str(user_id), "business_data_collected")
        save_user_state(str(user_id), STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(str(user_id), SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0), one_time_keyboard=True)
        return

    # Опросник
    if state == STATE_SURVEY:
        step = data.get("survey_step", 0)
        if step < len(SURVEY_QUESTIONS):
            key = SURVEY_QUESTIONS[step]["key"]
            answers = data.get("answers", {})
            answers[key] = text
            data["answers"] = answers
            data["survey_step"] = step + 1
            save_user_state(str(user_id), STATE_SURVEY, data)
            
            if step + 1 < len(SURVEY_QUESTIONS):
                await send_message(str(user_id), SURVEY_QUESTIONS[step + 1]["text"], get_survey_keyboard(step + 1), one_time_keyboard=True)
            else:
                save_form(str(user_id), answers)
                log_event(str(user_id), "survey_completed")
                biz_data = get_business_data(str(user_id))
                if not biz_data:
                    await send_message(str(user_id), "❌ Ошибка: данные бизнеса не найдены. Начни заново.", get_main_menu_keyboard())
                    save_user_state(str(user_id), STATE_MENU, {})
                    return

                await send_message(str(user_id), "🔍 Запускаю диагностику... Это займёт до 60 секунд.", None)
                report_text = await call_deepseek_diagnostic(biz_data["name"], biz_data["description"], answers)
                if report_text:
                    log_event(str(user_id), "free_report_generated")
                    save_user_state(str(user_id), STATE_MENU, {"generated_report": report_text, "report_title": biz_data["name"]})
                    await send_message(str(user_id), "✅ Диагностика готова! Как тебе удобнее получить?", get_format_choice_keyboard(), one_time_keyboard=True)
                else:
                    await send_message(str(user_id), "⚠️ Диагностика готова (по шаблону). Как удобнее получить?", get_format_choice_keyboard(), one_time_keyboard=True)
        return

    # Ожидание заявки на консультацию
    if state == STATE_WAITING_CALL:
        biz_data = get_business_data(str(user_id))
        form_data = get_form(str(user_id))
        channel_info = f"Название: {biz_data['name']}\nОписание: {biz_data['description'][:200]}..." if biz_data else "Нет данных"
        survey_info = "Нет данных"
        if form_data:
            survey_info = f"""• Продаёт: {form_data.get('q1', 'не указано')}
• Средний чек: {form_data.get('q2', 'не указано')}
• Клиентов/мес: {form_data.get('q3', 'не указано')}
• Цель: {form_data.get('q4', 'не указано')}
• Автоворонка: {form_data.get('q5', 'не указано')}"""
        
        await send_notification(ADMIN_CHAT_ID,
            f"📞 НОВАЯ ЗАЯВКА НА РАЗБОР\n\nПользователь: {user_id}\nСообщение: {text}\n\nДанные бизнеса:\n{channel_info}\n\nАнкета:\n{survey_info}\n\n⏰ {format_moscow_time()}")
        
        await send_message(str(user_id),
            "✅ Заявка принята! Я получила твои данные.\n\n"
            "А пока ждёшь ответа, загляни в мой канал — там я делюсь тем, что реально работает:\n"
            "🔥 Кейсы с цифрами\n"
            "🔍 Разборы ошибок\n"
            "📝 Скрипты фраз, которые продают\n\n"
            "После подписки зайди в закреп — там мини-курс «3 шага к первой продаже» в подарок 🎁",
            get_channel_subscribe_keyboard())
        
        save_user_state(str(user_id), STATE_MENU, {})
        return

    await send_message(str(user_id), "Используй кнопки меню или напиши /start", get_main_menu_keyboard())

# === ЗАПУСК ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started")
    logger.info(f"Bot token configured: {'✓' if MAX_BOT_TOKEN else '✗'}")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

# === HEALTH CHECK ENDPOINT ===
@app.get("/health")
async def health():
    return {
        "status": "alive",
        "timestamp": datetime.now().isoformat(),
        "configuration": {
            "MAX_BOT_TOKEN": "configured" if MAX_BOT_TOKEN else "missing",
            "ADMIN_CHAT_ID": "configured" if ADMIN_CHAT_ID else "missing",
            "DEEPSEEK_API_KEY": "configured" if DEEPSEEK_API_KEY else "missing",
            "YKASSA_SHOP_ID": "configured" if YKASSA_SHOP_ID != "test" else "test_mode",
            "YKASSA_SECRET_KEY": "configured" if YKASSA_SECRET_KEY != "test" else "test_mode",
            "YKASSA_TEST_MODE": YKASSA_TEST_MODE
        }
    }

@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "3.0"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"FULL PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:500]}")

        if "message" in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            body = msg.get("body", {})
            text = body.get("text")
            if user_id and text:
                await process_message(str(user_id), text)

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
