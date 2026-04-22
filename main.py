# File: main.py — бот Salesplan для MAX (оптимизированная версия)

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

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
YKASSA_SHOP_ID = os.getenv("YKASSA_SHOP_ID", "1325473")
YKASSA_SECRET_KEY = os.getenv("YKASSA_SECRET_KEY")
YKASSA_TEST_MODE = os.getenv("YKASSA_TEST_MODE", "true").lower() == "true"

MAX_API_URL = "https://platform-api.max.ru"
YKASSA_API_URL = "https://api.yookassa.ru/v3"

if not MAX_BOT_TOKEN:
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found in .env")

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

DB_PATH = "salesplan.db"
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(exist_ok=True)

# === СОСТОЯНИЯ ===
STATE_MENU = "menu"
STATE_AWAITING_BUSINESS_NAME = "awaiting_business_name"
STATE_AWAITING_BUSINESS_DESCRIPTION = "awaiting_business_description"
STATE_SURVEY = "survey"
STATE_WAITING_CALL = "waiting_call"
STATE_WAITING_PAYMENT = "waiting_payment"

# === CALLBACK DATA ===
CALLBACK_START_AUDIT = "start_audit"
CALLBACK_MY_PREMIUM = "my_premium"
CALLBACK_BOOK_CALL = "book_call"
CALLBACK_DOWNLOAD_REPORT = "download_report"
CALLBACK_HELP = "help"

# === ОПРОСНИК ===
Q1_SERVICE = "q1_service"
Q1_INFO = "q1_info"
Q1_CONSULT = "q1_consult"
Q1_NONE = "q1_none"
Q2_LT5 = "q2_lt5"
Q2_5_20 = "q2_5_20"
Q2_20_50 = "q2_20_50"
Q2_50P = "q2_50p"
Q3_LT10 = "q3_lt10"
Q3_10_50 = "q3_10_50"
Q3_50_200 = "q3_50_200"
Q3_200P = "q3_200p"
Q4_300 = "q4_300"
Q4_500 = "q4_500"
Q4_1M = "q4_1m"
Q4_SCALE = "q4_scale"
Q5_YES = "q5_yes"
Q5_NO = "q5_no"
Q5_PROGRESS = "q5_progress"

SURVEY_QUESTIONS = [
    {"key": "q1", "text": "Что ты продаёшь?", "options": [
        (Q1_SERVICE, "Услугу"),
        (Q1_INFO, "Инфопродукт"),
        (Q1_CONSULT, "Консультацию"),
        (Q1_NONE, "Пока не продаю"),
    ]},
    {"key": "q2", "text": "Средний чек (₽)", "options": [
        (Q2_LT5, "до 5 000 ₽"),
        (Q2_5_20, "5 000 - 20 000 ₽"),
        (Q2_20_50, "20 000 - 50 000 ₽"),
        (Q2_50P, "более 50 000 ₽"),
    ]},
    {"key": "q3", "text": "Клиентов в месяц (примерно)", "options": [
        (Q3_LT10, "менее 10"),
        (Q3_10_50, "10-50"),
        (Q3_50_200, "50-200"),
        (Q3_200P, "более 200"),
    ]},
    {"key": "q4", "text": "Цель на 2026", "options": [
        (Q4_300, "300 000 ₽/мес"),
        (Q4_500, "500 000 ₽/мес"),
        (Q4_1M, "1 000 000 ₽/мес"),
        (Q4_SCALE, "Масштабирование"),
    ]},
    {"key": "q5", "text": "Уже есть автоворонка?", "options": [
        (Q5_YES, "Да"),
        (Q5_NO, "Нет"),
        (Q5_PROGRESS, "В разработке"),
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            consent_type TEXT NOT NULL,
            consent_given_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip TEXT,
            user_agent TEXT
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

def update_payment_status(payment_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
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
async def send_message(chat_id: str, text: str, keyboard: list = None):
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    if keyboard:
        payload["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": keyboard
                }
            }
        ]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_message failed: {resp.status} - {error_text}")
            return await resp.json()

async def send_callback_answer(callback_id: str, text: str, keyboard: list = None):
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    if keyboard:
        payload["message"]["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": keyboard
                }
            }
        ]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_callback_answer failed: {resp.status} - {error_text}")
            return await resp.json()

async def send_notification(chat_id: str, text: str, keyboard: list = None):
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    if keyboard:
        payload["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": keyboard
                }
            }
        ]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_notification failed: {resp.status} - {error_text}")
            return await resp.json()

# === ФАЙЛЫ ===
async def upload_file_to_max(file_path: str, file_type: str = "file"):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MAX_API_URL}/uploads?type={file_type}",
            headers={"Authorization": MAX_BOT_TOKEN}
        ) as resp:
            if resp.status != 200:
                logger.error(f"Failed to get upload URL: {await resp.text()}")
                return None
            data = await resp.json()
            upload_url = data.get("url")
            if not upload_url:
                return None
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            form_data = aiohttp.FormData()
            form_data.add_field('data', f, filename=Path(file_path).name)
            async with session.post(upload_url, data=form_data) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to upload file: {await resp.text()}")
                    return None
                result = await resp.json()
                if "token" in result:
                    return {"type": file_type, "payload": {"token": result["token"]}}
                elif "result" in result and "url" in result["result"]:
                    return {"type": file_type, "payload": {"url": result["result"]["url"]}}
                else:
                    return {"type": file_type, "payload": result}

async def send_file_message(chat_id: str, text: str, file_path: str, file_type: str = "file"):
    attachment = await upload_file_to_max(file_path, file_type)
    if not attachment:
        await send_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
        return
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

# === ПЛАТЕЖИ ===
async def create_yookassa_payment(amount: int, description: str, user_id: str):
    if not YKASSA_SHOP_ID or not YKASSA_SECRET_KEY or YKASSA_SECRET_KEY == "test":
        logger.warning(f"YooKassa credentials missing or in test mode. Using mock payment")
        if YKASSA_TEST_MODE:
            logger.info(f"Using mock payment for user {user_id}")
            return {
                "payment_id": f"mock_{uuid.uuid4().hex}",
                "confirmation_url": "https://yookassa.ru/payment-mock"
            }
        return None
    
    payment_id = f"salesplan_{user_id}_{uuid.uuid4().hex[:8]}"
    payload = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": f"https://realplanninig-oss-max-salesplan-bot-1a18.twc1.net/"},
        "description": description,
        "capture": True,
        "metadata": {"user_id": user_id, "payment_id": payment_id},
        "receipt": {
            "customer": {"phone": user_id[:10] if len(user_id) >= 10 else "79000000000"},
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {"value": f"{amount}.00", "currency": "RUB"},
                    "vat_code": "6",
                    "payment_mode": "full_payment",
                    "payment_subject": "service"
                }
            ]
        }
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
    if payment_id.startswith("mock_"):
        return "succeeded"
    
    if not YKASSA_SECRET_KEY or YKASSA_SECRET_KEY == "test":
        return "pending"
    
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

# === КЛАВИАТУРЫ ===
def get_main_menu_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "📊 Бесплатный аудит",
                "payload": CALLBACK_START_AUDIT,
                "intent": "default"
            }
        ]
    ]

def get_survey_keyboard(question_index: int):
    if question_index >= len(SURVEY_QUESTIONS):
        return None
    q = SURVEY_QUESTIONS[question_index]
    keyboard = []
    for payload, label in q["options"]:
        keyboard.append([
            {
                "type": "callback",
                "text": label,
                "payload": payload,
                "intent": "default"
            }
        ])
    return keyboard

def get_payment_keyboard(confirmation_url: str):
    return [
        [
            {
                "type": "link",
                "text": "💳 Оплатить 490 ₽",
                "url": confirmation_url
            }
        ]
    ]

def get_download_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "📥 Скачать план продаж",
                "payload": CALLBACK_DOWNLOAD_REPORT,
                "intent": "default"
            }
        ]
    ]

def get_after_download_keyboard(is_subscribed: bool = False):
    if is_subscribed:
        return [
            [
                {
                    "type": "callback",
                    "text": "👩‍💼 Разобрать план (30 мин)",
                    "payload": CALLBACK_BOOK_CALL,
                    "intent": "default"
                }
            ]
        ]
    else:
        return [
            [
                {
                    "type": "link",
                    "text": "📢 Подписаться на канал",
                    "url": "https://max.ru/id781407988795_biz"
                }
            ],
            [
                {
                    "type": "callback",
                    "text": "👩‍💼 Разобрать план (30 мин)",
                    "payload": CALLBACK_BOOK_CALL,
                    "intent": "default"
                }
            ]
        ]

def get_channel_subscribe_keyboard():
    return [
        [
            {
                "type": "link",
                "text": "📢 Подписаться на канал в MAX",
                "url": "https://max.ru/id781407988795_biz"
            }
        ]
    ]

# === DEEPSEEK API ===
async def call_deepseek_diagnostic(name: str, description: str, answers: dict):
    q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
    q2_map = {Q2_LT5: "до 5000 ₽", Q2_5_20: "5000-20000 ₽", Q2_20_50: "20000-50000 ₽", Q2_50P: "более 50000 ₽"}
    q3_map = {Q3_LT10: "менее 10", Q3_10_50: "10-50", Q3_50_200: "50-200", Q3_200P: "более 200"}
    q4_map = {Q4_300: "300 000 ₽/мес", Q4_500: "500 000 ₽/мес", Q4_1M: "1 000 000 ₽/мес", Q4_SCALE: "масштабирование"}
    q5_map = {Q5_YES: "да", Q5_NO: "нет", Q5_PROGRESS: "в разработке"}
    
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {q1_map.get(answers.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(answers.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(answers.get('q3'), 'не указано')}
• Цель на 2026: {q4_map.get(answers.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(answers.get('q5'), 'не указано')}
"""
    prompt = f"""Сделай профессиональный маркетинговый разбор онлайн-бизнеса.

ДАННЫЕ О БИЗНЕСЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши отчет в разговорном стиле Вероники. НЕ ИСПОЛЬЗУЙ символы *, #, _, `, ~. Для списков используй просто дефис. Для заголовков используй ЗАГЛАВНЫЕ БУКВЫ.

1. ОБЩАЯ ИНФОРМАЦИЯ (ниша, ЦА, оценка 0-100)
2. АНАЛИЗ (3 сильные стороны, 3 зоны роста)
3. РЕКОМЕНДАЦИИ (3 конкретных шага)"""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов. Говоришь разговорно, с эмодзи, на 'ты'. НИКОГДА не используй символы *, #, _, `, ~. Только обычный текст и эмодзи."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek error: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

async def generate_premium_report(user_id: str, name: str, description: str, answers: dict, report_id: int):
    logger.info(f"Generating premium report for {user_id}")
    q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
    q2_map = {Q2_LT5: "до 5k", Q2_5_20: "5k-20k", Q2_20_50: "20k-50k", Q2_50P: ">50k"}
    q3_map = {Q3_LT10: "<10", Q3_10_50: "10-50", Q3_50_200: "50-200", Q3_200P: ">200"}
    q4_map = {Q4_300: "300k/мес", Q4_500: "500k/мес", Q4_1M: "1M/мес", Q4_SCALE: "Масштаб"}
    q5_map = {Q5_YES: "да", Q5_NO: "нет", Q5_PROGRESS: "в разработке"}
    
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {q1_map.get(answers.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(answers.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(answers.get('q3'), 'не указано')}
• Цель: {q4_map.get(answers.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(answers.get('q5'), 'не указано')}
"""
    prompt = f"""Сделай профессиональный план запуска продаж для онлайн-бизнеса.

ДАННЫЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши план в разговорном стиле Вероники. НЕ ИСПОЛЬЗУЙ символы *, #, _, `, ~. Для списков используй просто дефис. Для заголовков используй ЗАГЛАВНЫЕ БУКВЫ.

1. ОЦЕНКА СИТУАЦИИ
2. АНАЛИЗ КОНКУРЕНТОВ (3-5 игроков)
3. КОМУ ПРОДАВАТЬ (ЦА)
4. СИЛЬНЫЕ И СЛАБЫЕ СТОРОНЫ
5. ВОРОНКА ПРОДАЖ ШАГ ЗА ШАГОМ
6. ПЛАН ДЕЙСТВИЙ НА МЕСЯЦ"""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов. НИКОГДА не используй символы *, #, _, `, ~. Только обычный текст и эмодзи."},
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
            filename = f"Premium_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = REPORTS_DIR / filename
            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(report_text)
            update_report_status(report_id, 'ready', str(filepath))
            logger.info(f"Premium report generated for {user_id}")
        else:
            update_report_status(report_id, 'failed')
            logger.error(f"Premium report API error: {response.status_code}")
    except Exception as e:
        logger.error(f"Premium report error: {e}")
        update_report_status(report_id, 'failed')

async def send_analysis_animation(chat_id: str):
    """Отправляет анимацию процесса анализа"""
    steps = [
        "🔍 Анализируем нишу — кто ваши клиенты и где они тусуются",
        "📊 Изучаем целевую аудиторию — что они хотят на самом деле",
        "🎯 Ищем точки роста — где вы теряете деньги",
        "📝 Формируем рекомендации — что делать прямо сейчас"
    ]
    
    for i, step in enumerate(steps):
        await send_message(chat_id, f"🔄 {step}\n\n⏳ {i+1}/4", None)
        await asyncio.sleep(8)
    
    await send_message(chat_id, "⏳ Осталось 5 секунд...", None)
    await asyncio.sleep(5)

# === ОБРАБОТЧИКИ ===
async def process_callback(chat_id: str, callback_id: str, callback_data: str, username: str = None):
    state, data = get_user_state(chat_id)
    log_event(chat_id, f"callback_{callback_data}")
    
    if data is None:
        data = {}
    
    # Сохраняем username в состояние
    if username:
        data["username"] = username
        save_user_state(chat_id, state, data)

    if callback_data == CALLBACK_START_AUDIT:
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {"answers": {}, "survey_step": 0})
        user_name = username or chat_id
        await send_callback_answer(callback_id,
            f"Окей, погнали! 🚀\n\n@{user_name}, напиши название своего онлайн-бизнеса (как ты представляешь его клиентам):",
            None)
        return

    if callback_data == CALLBACK_MY_PREMIUM:
        biz_data = get_business_data(chat_id)
        form_data = get_form(chat_id)
        if not biz_data or not form_data:
            await send_callback_answer(callback_id,
                "Ой, стоп! Сначала нужно пройти бесплатную диагностику.\n\n"
                "Это быстро — 2 минуты, честно 👇",
                get_main_menu_keyboard())
            return

        payment = await create_yookassa_payment(490, "План продаж Salesplan", chat_id)
        if payment and payment.get("confirmation_url"):
            save_pending_payment(chat_id, payment["payment_id"])
            save_user_state(chat_id, STATE_WAITING_PAYMENT, {"payment_id": payment["payment_id"]})
            
            await send_callback_answer(callback_id,
                f"🔍 Запускаю генерацию плана продаж... 1-2 минуты — и всё готово.\n\n"
                f"🔥 Твой план будет содержать:\n"
                f"• Разбор 5 конкурентов\n"
                f"• Анализ ЦА\n"
                f"• Готовую воронку\n"
                f"• План действий на месяц\n\n"
                f"👇 Оплати 490 ₽ — и сразу скачаешь результат",
                get_payment_keyboard(payment["confirmation_url"]))
        else:
            await send_callback_answer(callback_id,
                "❌ Ошибка при создании платежа. Попробуй позже или нажми «Помощь».",
                get_main_menu_keyboard())
            return
        
        report_id = save_report_request(chat_id)
        asyncio.create_task(generate_premium_report(chat_id, biz_data["name"], biz_data["description"], form_data, report_id))
        return

    if callback_data == CALLBACK_DOWNLOAD_REPORT:
        report_status = get_report_status(chat_id)
        if report_status and report_status['status'] == 'ready' and report_status['file_path']:
            filepath = Path(report_status['file_path'])
            if filepath.exists():
                await send_file_message(
                    chat_id,
                    "📄 Держи свой план продаж!",
                    str(filepath),
                    "file"
                )
                await asyncio.sleep(2)
                await send_message(chat_id,
                    "🔥 Ну что, изучила план?\n\n"
                    "Давай честно: ты сможешь всё это внедрить сама?\n\n"
                    "Я знаю эту боль — информации много, а результата нет.\n\n"
                    "Поэтому я предлагаю БЕСПЛАТНЫЙ 30-минутный разбор твоего плана.\n\n"
                    "За 30 минут я:\n"
                    "✅ Найду ТВОЁ одно действие, которое принесёт деньги прямо сейчас\n"
                    "✅ Покажу, где теряешь клиентов\n"
                    "✅ Дам конкретный план на неделю",
                    get_after_download_keyboard(False))
            else:
                await send_callback_answer(callback_id,
                    "❌ Файл не найден. Напиши мне — поможем.",
                    get_main_menu_keyboard())
        else:
            await send_callback_answer(callback_id,
                "⏳ План ещё готовится. Обычно 1-2 минуты.\n\nКак будет готов — я пришлю уведомление.",
                None)
        return

    if callback_data == CALLBACK_HELP:
        await send_notification(ADMIN_CHAT_ID, f"❓ Запрос помощи от {chat_id}\n⏰ {format_moscow_time()}")
        await send_callback_answer(callback_id,
            "✅ Запрос отправлен! Я свяжусь с тобой в ближайшее время.",
            None)
        return

    if callback_data == CALLBACK_BOOK_CALL:
        save_user_state(chat_id, STATE_WAITING_CALL, {})
        await send_callback_answer(callback_id,
            "🔥 Только для подписчиков канала!\n\n"
            "Подпишись на мой канал в MAX — там я делюсь:\n"
            "• Кейсами с цифрами\n"
            "• Разборами ошибок\n"
            "• Скриптами, которые продают\n\n"
            "После подписки напиши мне — и получишь 30 минут БЕСПЛАТНОГО разбора твоего плана.\n\n"
            "👇 Жми кнопку, подписывайся",
            get_channel_subscribe_keyboard())
        return

    if callback_data in [Q1_SERVICE, Q1_INFO, Q1_CONSULT, Q1_NONE,
                         Q2_LT5, Q2_5_20, Q2_20_50, Q2_50P,
                         Q3_LT10, Q3_10_50, Q3_50_200, Q3_200P,
                         Q4_300, Q4_500, Q4_1M, Q4_SCALE,
                         Q5_YES, Q5_NO, Q5_PROGRESS]:
        _, user_data = get_user_state(chat_id)
        
        if user_data is None:
            user_data = {}
        if "answers" not in user_data:
            user_data["answers"] = {}
        if "survey_step" not in user_data:
            user_data["survey_step"] = 0
            
        step = user_data.get("survey_step", 0)
        
        if step < len(SURVEY_QUESTIONS):
            key = SURVEY_QUESTIONS[step]["key"]
            user_data["answers"][key] = callback_data
            user_data["survey_step"] = step + 1
            save_user_state(chat_id, STATE_SURVEY, user_data)

            if step + 1 < len(SURVEY_QUESTIONS):
                await send_callback_answer(callback_id,
                    SURVEY_QUESTIONS[step + 1]["text"],
                    get_survey_keyboard(step + 1))
            else:
                save_form(chat_id, user_data["answers"])
                log_event(chat_id, "survey_completed")
                biz_data = get_business_data(chat_id)
                if not biz_data:
                    await send_callback_answer(callback_id,
                        "❌ Ошибка. Начни заново.",
                        get_main_menu_keyboard())
                    save_user_state(chat_id, STATE_MENU, {})
                    return

                await send_callback_answer(callback_id,
                    "🔍 Запускаю анализ...",
                    None)
                
                # Анимация анализа
                await send_analysis_animation(chat_id)
                
                report_text = await call_deepseek_diagnostic(
                    biz_data["name"], biz_data["description"], user_data["answers"])
                
                if report_text:
                    log_event(chat_id, "free_report_generated")
                    save_user_state(chat_id, STATE_MENU, {})
                    
                    # Отправляем отчёт сразу, без лишних вопросов
                    max_len = 3800
                    if len(report_text) > max_len:
                        await send_message(chat_id, f"✅ Твоя диагностика:\n\n{report_text[:max_len]}", None)
                        await send_notification(chat_id, report_text[max_len:max_len+max_len])
                    else:
                        await send_message(chat_id, f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(2)
                    
                    # Апсейл на платный план
                    await send_message(chat_id,
                        "🔥 Ну как тебе?\n\n"
                        "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
                        "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.\n\n"
                        "👇 А если хочешь, чтобы я лично разобрала твой бизнес — подпишись на канал и напиши мне",
                        [
                            [
                                {
                                    "type": "callback",
                                    "text": "🔥 План продаж за 490 ₽",
                                    "payload": CALLBACK_MY_PREMIUM,
                                    "intent": "default"
                                }
                            ],
                            [
                                {
                                    "type": "link",
                                    "text": "📢 Подписаться на канал",
                                    "url": "https://max.ru/id781407988795_biz"
                                }
                            ]
                        ])
                else:
                    await send_message(chat_id,
                        "⚠️ Диагностика готова (по шаблону).",
                        get_main_menu_keyboard())
        return

async def process_message(user_id: str, text: str, username: str = None):
    logger.info(f"PROCESS_MESSAGE called: user_id={user_id}, text={text}")
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")
    
    # Сохраняем username в состояние
    if username:
        data["username"] = username
        save_user_state(str(user_id), state, data)

    if state == STATE_MENU:
        if text == "/start":
            user_name = username or user_id
            await send_message(str(user_id),
                f"Привет, @{user_name}! Я Вероника, продюсер экспертов.\n\n"
                "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
                "Давай сделаем бесплатный аудит твоего бизнеса — 2 минуты, и узнаешь, что теряешь.",
                get_main_menu_keyboard())
            save_user_state(str(user_id), STATE_MENU, data)
        else:
            await send_message(str(user_id), "Используй кнопки меню или напиши /start")
        return

    if state == STATE_AWAITING_BUSINESS_NAME:
        if len(text) > 100:
            await send_message(str(user_id), "Слишком длинное название. Напиши покороче (до 100 символов):")
            return
        save_user_state(str(user_id), STATE_AWAITING_BUSINESS_DESCRIPTION, {"business_name": text})
        await send_message(str(user_id), "Ок, записала! Теперь напиши краткое описание бизнеса — что ты делаешь, кому помогаешь:")
        return

    if state == STATE_AWAITING_BUSINESS_DESCRIPTION:
        if len(text) > 500:
            await send_message(str(user_id), "Описание слишком длинное. Напиши покороче (до 500 символов):")
            return
        business_name = data.get("business_name")
        save_business_data(str(user_id), business_name, text)
        log_event(str(user_id), "business_data_collected")
        save_user_state(str(user_id), STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(str(user_id), SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0))
        return

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
                await send_message(str(user_id), SURVEY_QUESTIONS[step + 1]["text"], get_survey_keyboard(step + 1))
            else:
                save_form(str(user_id), answers)
                log_event(str(user_id), "survey_completed")
                biz_data = get_business_data(str(user_id))
                if not biz_data:
                    await send_message(str(user_id), "❌ Ошибка. Начни заново.", get_main_menu_keyboard())
                    save_user_state(str(user_id), STATE_MENU, {})
                    return

                await send_message(str(user_id), "🔍 Запускаю анализ...", None)
                
                # Анимация анализа
                await send_analysis_animation(str(user_id))
                
                report_text = await call_deepseek_diagnostic(biz_data["name"], biz_data["description"], answers)
                if report_text:
                    log_event(str(user_id), "free_report_generated")
                    save_user_state(str(user_id), STATE_MENU, {})
                    
                    # Отправляем отчёт сразу
                    max_len = 3800
                    if len(report_text) > max_len:
                        await send_message(str(user_id), f"✅ Твоя диагностика:\n\n{report_text[:max_len]}", None)
                        await send_notification(str(user_id), report_text[max_len:max_len+max_len])
                    else:
                        await send_message(str(user_id), f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(2)
                    
                    # Апсейл на платный план
                    await send_message(str(user_id),
                        "🔥 Ну как тебе?\n\n"
                        "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
                        "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.\n\n"
                        "👇 А если хочешь, чтобы я лично разобрала твой бизнес — подпишись на канал и напиши мне",
                        [
                            [
                                {
                                    "type": "callback",
                                    "text": "🔥 План продаж за 490 ₽",
                                    "payload": CALLBACK_MY_PREMIUM,
                                    "intent": "default"
                                }
                            ],
                            [
                                {
                                    "type": "link",
                                    "text": "📢 Подписаться на канал",
                                    "url": "https://max.ru/id781407988795_biz"
                                }
                            ]
                        ])
                else:
                    await send_message(str(user_id),
                        "⚠️ Диагностика готова (по шаблону).",
                        get_main_menu_keyboard())
        return

    if state == STATE_WAITING_CALL:
        biz_data = get_business_data(str(user_id))
        form_data = get_form(str(user_id))
        channel_info = f"Название: {biz_data['name']}\nОписание: {biz_data['description'][:200]}..." if biz_data else "Нет данных"
        survey_info = "Нет данных"
        if form_data:
            q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
            q2_map = {Q2_LT5: "до 5k", Q2_5_20: "5k-20k", Q2_20_50: "20k-50k", Q2_50P: ">50k"}
            q3_map = {Q3_LT10: "<10", Q3_10_50: "10-50", Q3_50_200: "50-200", Q3_200P: ">200"}
            q4_map = {Q4_300: "300k/мес", Q4_500: "500k/мес", Q4_1M: "1M/мес", Q4_SCALE: "Масштаб"}
            q5_map = {Q5_YES: "Да", Q5_NO: "Нет", Q5_PROGRESS: "В разработке"}
            survey_info = f"""• Продаёт: {q1_map.get(form_data.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(form_data.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(form_data.get('q3'), 'не указано')}
• Цель: {q4_map.get(form_data.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(form_data.get('q5'), 'не указано')}"""
        await send_message(ADMIN_CHAT_ID,
            f"📞 НОВАЯ ЗАЯВКА НА РАЗБОР\n\nПользователь: {user_id}\nСообщение: {text}\n\nДанные бизнеса:\n{channel_info}\n\nАнкета:\n{survey_info}\n\n⏰ {format_moscow_time()}")
        await send_message(str(user_id),
            "✅ Заявка принята! Я получила твои данные.\n\n"
            "А пока ждёшь ответа, загляни в мой канал — там я делюсь тем, что реально работает:\n"
            "🔥 Кейсы с цифрами\n"
            "🔍 Разборы ошибок\n"
            "📝 Скрипты фраз, которые продают\n\n"
            "👇 Жми кнопку, подписывайся")
        await send_message(str(user_id),
            "👇 Подписывайся на канал",
            get_channel_subscribe_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

    await send_message(str(user_id), "Используй кнопки меню или напиши /start")

# === СОЗДАНИЕ ПРИЛОЖЕНИЯ FASTAPI ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

# === ЭНДПОИНТЫ ===
@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "3.0"}

@app.get("/health")
async def health():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"FULL PAYLOAD: {payload}")

        if "message" in payload and "callback" not in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            username = msg.get("sender", {}).get("username")
            body = msg.get("body", {})
            text = body.get("text")
            if user_id and text:
                await process_message(str(user_id), text, username)

        elif "callback" in payload:
            cb = payload["callback"]
            user_id = cb.get("user", {}).get("user_id")
            username = cb.get("user", {}).get("username")
            callback_id = cb.get("callback_id")
            data = cb.get("payload")
            logger.info(f"CALLBACK RECEIVED: user_id={user_id}, callback_id={callback_id}, payload={data}")
            if user_id and data:
                await process_callback(str(user_id), str(callback_id), data, username)

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/yookassa-webhook")
async def yookassa_webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"YooKassa webhook received: {payload}")
        
        event = payload.get("event")
        if event == "payment.succeeded":
            payment_obj = payload.get("object", {})
            payment_id = payment_obj.get("id")
            metadata = payment_obj.get("metadata", {})
            user_id = metadata.get("user_id")
            
            if user_id and payment_id:
                update_payment_status(payment_id, "succeeded")
                clear_pending_payment(user_id)
                
                # Проверяем, готов ли отчёт
                report_status = get_report_status(user_id)
                if report_status and report_status['status'] == 'ready':
                    await send_notification(user_id,
                        "🎉 Ура! Твой план продаж готов!\n\n"
                        "👇 Жми кнопку ниже — и забирай результат",
                        get_download_keyboard())
                else:
                    await send_notification(user_id,
                        "✅ Оплата прошла, спасибо!\n\n"
                        "План ещё готовится — 1-2 минуты. Я пришлю уведомление, как только всё будет готово.")
                
                await send_notification(ADMIN_CHAT_ID,
                    f"💰 ПОЛУЧЕНА ОПЛАТА\n\nПользователь: {user_id}\nСумма: 490 ₽\n⏰ {format_moscow_time()}")
        
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"YooKassa webhook error: {e}")
        return Response(status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
