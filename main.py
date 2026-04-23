# File: main.py — бот Salesplan для MAX (полная версия с чатом и челленджем)

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
from fastapi import FastAPI, Request, Response, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import aiohttp
import aiofiles
import uvicorn

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
YKASSA_SHOP_ID = os.getenv("YKASSA_SHOP_ID", "1310983")
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
CALLBACK_ASK_AI = "ask_ai"
CALLBACK_CHALLENGE = "challenge"
CALLBACK_TASK = "task"
CALLBACK_DONE = "done"
CALLBACK_PROGRESS = "progress"

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
            ready_at TIMESTAMP,
            paid_at TIMESTAMP
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            current_day INTEGER DEFAULT 1,
            tasks_completed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenge_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            task_text TEXT NOT NULL,
            is_completed BOOLEAN DEFAULT 0,
            completed_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS implementation_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            phone TEXT,
            question TEXT,
            status TEXT DEFAULT 'new',
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

def save_form(user_id: str, answers: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO forms (user_id, q1, q2, q3, q4, q5)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, answers.get("q1"), answers.get("q2"), answers.get("q3"),
          answers.get("q4"), answers.get("q5")))
    conn.commit()
    conn.close()

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

def update_report_status(report_id: int, status: str, file_path: str = None, paid_at: str = None):
    conn = sqlite3.connect(DB_PATH)
    if status == 'ready':
        if paid_at:
            conn.execute("""
                UPDATE reports SET status = ?, file_path = ?, ready_at = CURRENT_TIMESTAMP, paid_at = ?
                WHERE id = ?
            """, (status, file_path, paid_at, report_id))
        else:
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
        SELECT id, status, file_path, ready_at, paid_at FROM reports
        WHERE user_id = ? AND report_type = 'premium'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "status": row[1], "file_path": row[2], "ready_at": row[3], "paid_at": row[4]}
    return None

def get_premium_report_text(user_id: str) -> str:
    """Возвращает текст премиум-отчёта пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT report_text, file_path FROM reports 
        WHERE user_id = ? AND report_type = 'premium' AND status = 'ready'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    report_text = row[0]
    if not report_text and row[1]:
        try:
            with open(row[1], 'r', encoding='utf-8') as f:
                report_text = f.read()
        except:
            pass
    
    return report_text

def has_active_access(user_id: str) -> bool:
    """Проверяет, есть ли у пользователя активный доступ (оплачено и не прошло 30 дней)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT paid_at FROM reports 
        WHERE user_id = ? AND report_type = 'premium' AND status = 'ready'
        ORDER BY paid_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return False
    
    paid_at = datetime.fromisoformat(row[0])
    days_left = 30 - (get_moscow_time() - paid_at).days
    return days_left > 0

def get_days_left(user_id: str) -> int:
    """Возвращает количество оставшихся дней доступа"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT paid_at FROM reports 
        WHERE user_id = ? AND report_type = 'premium' AND status = 'ready'
        ORDER BY paid_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return 0
    
    paid_at = datetime.fromisoformat(row[0])
    days_left = 30 - (get_moscow_time() - paid_at).days
    return max(0, days_left)

def save_chat_message(user_id: str, role: str, message: str):
    """Сохраняет сообщение в историю чата"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO chat_history (user_id, role, message)
        VALUES (?, ?, ?)
    """, (user_id, role, message))
    conn.commit()
    conn.close()

def get_chat_history(user_id: str, limit: int = 10) -> list:
    """Возвращает последние сообщения чата"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT role, message FROM chat_history 
        WHERE user_id = ? 
        ORDER BY created_at ASC LIMIT ?
    """, (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return [{"role": r[0], "message": r[1]} for r in rows]

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
    if not YKASSA_SECRET_KEY or YKASSA_SECRET_KEY == "test" or YKASSA_SECRET_KEY == "":
        logger.error(f"YooKassa SECRET_KEY is missing or invalid!")
        return None
    
    payment_id = f"salesplan_{user_id}_{uuid.uuid4().hex[:8]}"
    payload = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": f"https://realplanninig-oss-max-salesplan-bot-1a18.twc1.net/chat?user_id={user_id}"},
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
    
    logger.info(f"Creating YooKassa payment: shop_id={YKASSA_SHOP_ID}, amount={amount}, user_id={user_id}")
    
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
                logger.info(f"YooKassa payment created: {data['id']}")
                return {"payment_id": data["id"], "confirmation_url": data["confirmation"]["confirmation_url"]}
            else:
                error_text = await resp.text()
                logger.error(f"YooKassa error: {resp.status} - {error_text}")
                return None

async def check_yookassa_payment(payment_id: str):
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

def get_after_payment_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "💬 Задать вопрос AI",
                "payload": CALLBACK_ASK_AI,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏆 Челлендж 7 дней",
                "payload": CALLBACK_CHALLENGE,
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
                "text": "💳 Оплатить 1490 ₽",
                "url": confirmation_url
            }
        ]
    ]

def get_chat_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "💬 Задать вопрос",
                "payload": CALLBACK_ASK_AI,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏆 Мой челлендж",
                "payload": CALLBACK_PROGRESS,
                "intent": "default"
            }
        ]
    ]

def get_challenge_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "📋 Получить задание",
                "payload": CALLBACK_TASK,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "✅ Выполнил задание",
                "payload": CALLBACK_DONE,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "📊 Мой прогресс",
                "payload": CALLBACK_PROGRESS,
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

async def call_deepseek_chat(question: str, user_id: str, report_text: str, history: list) -> str:
    """AI-ответ на вопрос пользователя с контекстом плана и истории"""
    
    # Формируем контекст из истории чата
    history_text = ""
    for msg in history[-5:]:  # последние 5 сообщений
        role = "Пользователь" if msg["role"] == "user" else "Вероника"
        history_text += f"{role}: {msg['message']}\n"
    
    prompt = f"""Ты Вероника, продюсер экспертов. Ты уже подготовила для пользователя профессиональный маркетинговый план.
Вот план пользователя:
{report_text[:3000]}

История диалога:
{history_text}

Теперь пользователь спрашивает:
{question}

Ответь по-дружески, с эмодзи, на 'ты'. Помоги разобраться с вопросом, используя контекст плана.
Если вопрос не связан с бизнесом, вежливо направь в тему.
Если вопрос сложный (просит настроить рекламу, сделать воронку, написать скрипты) — напиши: 
"🔥 Это сложная задача, лучше сделать под ключ. Оставь телефон, я свяжусь с тобой и помогу внедрить."
"""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов. Отвечаешь дружелюбно, с эмодзи, на 'ты'."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 1000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek chat error: {response.status_code}")
            return "Ой, что-то пошло не так. Попробуй переформулировать вопрос."
    except Exception as e:
        logger.error(f"DeepSeek chat failed: {e}")
        return "Не могу ответить сейчас. Попробуй позже."

async def generate_challenge_task(user_id: str, day: int, report_text: str) -> str:
    """Генерирует задание для челленджа на основе плана пользователя"""
    
    prompt = f"""Ты Вероника, продюсер экспертов. У пользователя есть маркетинговый план.
Вот его план:
{report_text[:2000]}

День {day} из 7.

Придумай конкретное, выполнимое задание на сегодня, которое приблизит пользователя к внедрению плана.
Задание должно быть:
- Конкретным (что именно сделать)
- Измеримым (как понять, что сделано)
- Реалистичным (можно сделать за 15-30 минут)

Напиши задание в формате:
🎯 ЗАДАНИЕ ДЕНЬ {day}
[текст задания]
📝 Чек-лист:
- пункт 1
- пункт 2
- пункт 3

Без лишних слов, только задание."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника. Пиши коротко, конкретно, с эмодзи."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8,
        "max_tokens": 500
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"🎯 ЗАДАНИЕ ДЕНЬ {day}\nНапиши 3 идеи для улучшения своего бизнеса и выбери одну для внедрения.\n📝 Чек-лист:\n- Запиши 3 идеи\n- Выбери лучшую\n- Напиши план действий"
    except Exception as e:
        logger.error(f"Generate task error: {e}")
        return f"🎯 ЗАДАНИЕ ДЕНЬ {day}\nПрочитай свой маркетинговый план и найди 1 пункт, который можно сделать сегодня."

# === AI ЧАТ ===
async def ask_ai(chat_id: str, question: str, username: str = None):
    """Обработка вопроса к AI"""
    
    # Проверяем доступ
    if not has_active_access(chat_id):
        await send_message(chat_id,
            "⏰ Доступ к AI-чату закончился или ещё не оплачен.\n\n"
            "Чтобы продолжить, оплати профессиональный маркетинговый план — 1490 ₽.\n\n"
            "👇 Нажми кнопку, чтобы оплатить",
            get_payment_keyboard("https://example.com"))  # TODO: заменить на реальную ссылку
        return
    
    # Сохраняем вопрос пользователя
    save_chat_message(chat_id, "user", question)
    
    # Получаем план пользователя
    report_text = get_premium_report_text(chat_id)
    if not report_text:
        await send_message(chat_id, "❌ Не найден твой план. Обратись в поддержку.")
        return
    
    # Получаем историю чата
    history = get_chat_history(chat_id, 10)
    
    # Проверяем, сложный ли вопрос
    hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши"]
    is_hard = any(keyword in question.lower() for keyword in hard_keywords)
    
    if is_hard:
        answer = "🔥 Это сложная задача, которую лучше сделать под ключ.\n\nОставь свой телефон, и я свяжусь с тобой, чтобы помочь внедрить всё правильно."
        # Сохраняем заявку
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO implementation_leads (user_id, question, status)
            VALUES (?, ?, 'new')
        """, (chat_id, question))
        conn.commit()
        conn.close()
        await send_notification(ADMIN_CHAT_ID, f"📞 НОВАЯ ЗАЯВКА НА ВНЕДРЕНИЕ\n\nПользователь: {chat_id}\nВопрос: {question}")
    else:
        # Отправляем в DeepSeek
        await send_message(chat_id, "🤔 Думаю...", None)
        answer = await call_deepseek_chat(question, chat_id, report_text, history)
    
    # Сохраняем ответ
    save_chat_message(chat_id, "assistant", answer)
    
    # Отправляем ответ
    await send_message(chat_id, answer, get_chat_keyboard())

# === ЧЕЛЛЕНДЖ ===
async def start_or_continue_challenge(chat_id: str):
    """Начинает или продолжает челлендж"""
    
    # Проверяем, есть ли активный челлендж
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, current_day, tasks_completed, status FROM challenges
        WHERE user_id = ? AND status = 'active'
        ORDER BY start_date DESC LIMIT 1
    """, (chat_id,))
    challenge = cursor.fetchone()
    
    if challenge:
        # Продолжаем существующий
        challenge_id, current_day, tasks_completed, status = challenge
        days_left = 7 - current_day
        await send_message(chat_id,
            f"🏆 Твой челлендж в процессе!\n\n"
            f"📅 День {current_day} из 7\n"
            f"✅ Выполнено заданий: {tasks_completed}\n"
            f"⏳ Осталось дней: {days_left}\n\n"
            f"👇 Что хочешь сделать?",
            get_challenge_keyboard())
    else:
        # Начинаем новый
        cursor = conn.execute("""
            INSERT INTO challenges (user_id, current_day, tasks_completed, status)
            VALUES (?, 1, 0, 'active')
        """, (chat_id,))
        challenge_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        report_text = get_premium_report_text(chat_id)
        task_text = await generate_challenge_task(chat_id, 1, report_text)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO challenge_tasks (challenge_id, day_number, task_text)
            VALUES (?, ?, ?)
        """, (challenge_id, 1, task_text))
        conn.commit()
        conn.close()
        
        await send_message(chat_id,
            f"🏆 ПОЕХАЛИ! Челлендж «7 дней внедрения» начался!\n\n"
            f"{task_text}\n\n"
            f"👇 Когда сделаешь — нажми кнопку «Выполнил задание»",
            get_challenge_keyboard())

async def get_current_task(chat_id: str):
    """Получает текущее задание"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT c.id, c.current_day, ct.task_text, ct.is_completed
        FROM challenges c
        LEFT JOIN challenge_tasks ct ON c.id = ct.challenge_id AND c.current_day = ct.day_number
        WHERE c.user_id = ? AND c.status = 'active'
        ORDER BY c.start_date DESC LIMIT 1
    """, (chat_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        await send_message(chat_id, "❌ У тебя нет активного челленджа. Напиши /challenge чтобы начать.")
        return
    
    challenge_id, current_day, task_text, is_completed = row
    
    if is_completed:
        await send_message(chat_id,
            f"✅ Задание дня {current_day} уже выполнено!\n\n"
            f"Завтра будет новое задание. Продолжай в том же духе! 💪")
    else:
        await send_message(chat_id,
            f"📋 ТВОЁ ЗАДАНИЕ НА ДЕНЬ {current_day}\n\n{task_text}\n\n"
            f"👇 Когда сделаешь — нажми кнопку «Выполнил задание»",
            get_challenge_keyboard())

async def mark_task_done(chat_id: str):
    """Отмечает задание выполненным"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT c.id, c.current_day, c.tasks_completed
        FROM challenges c
        WHERE c.user_id = ? AND c.status = 'active'
        ORDER BY c.start_date DESC LIMIT 1
    """, (chat_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        await send_message(chat_id, "❌ У тебя нет активного челленджа.")
        return
    
    challenge_id, current_day, tasks_completed = row
    
    # Отмечаем задание выполненным
    conn.execute("""
        UPDATE challenge_tasks SET is_completed = 1, completed_at = CURRENT_TIMESTAMP
        WHERE challenge_id = ? AND day_number = ?
    """, (challenge_id, current_day))
    
    tasks_completed += 1
    
    if current_day >= 7:
        # Челлендж завершён
        conn.execute("""
            UPDATE challenges SET status = 'completed', tasks_completed = ?
            WHERE id = ?
        """, (tasks_completed, challenge_id))
        conn.commit()
        conn.close()
        
        await send_message(chat_id,
            f"🎉 ПОЗДРАВЛЯЮ! Ты прошёл челлендж «7 дней внедрения»!\n\n"
            f"✅ Выполнено заданий: {tasks_completed} из 7\n\n"
            f"🔥 Хочешь, чтобы я помогла внедрить всё под ключ? Напиши — сделаем скидку!")
    else:
        # Переходим к следующему дню
        new_day = current_day + 1
        conn.execute("""
            UPDATE challenges SET current_day = ?, tasks_completed = ?
            WHERE id = ?
        """, (new_day, tasks_completed, challenge_id))
        
        # Генерируем задание на следующий день
        report_text = get_premium_report_text(chat_id)
        task_text = await generate_challenge_task(chat_id, new_day, report_text)
        
        conn.execute("""
            INSERT INTO challenge_tasks (challenge_id, day_number, task_text)
            VALUES (?, ?, ?)
        """, (challenge_id, new_day, task_text))
        conn.commit()
        conn.close()
        
        await send_message(chat_id,
            f"✅ Отлично! Задание дня {current_day} выполнено!\n\n"
            f"🏆 Прогресс: {tasks_completed} заданий сделано\n\n"
            f"📋 Твоё задание на день {new_day}:\n\n{task_text}\n\n"
            f"👇 Продолжай в том же духе!",
            get_challenge_keyboard())

async def show_progress(chat_id: str):
    """Показывает прогресс челленджа"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT c.current_day, c.tasks_completed, 
               GROUP_CONCAT(ct.day_number || '|' || ct.is_completed) as tasks_status
        FROM challenges c
        LEFT JOIN challenge_tasks ct ON c.id = ct.challenge_id
        WHERE c.user_id = ? AND c.status = 'active'
        GROUP BY c.id
        ORDER BY c.start_date DESC LIMIT 1
    """, (chat_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        await send_message(chat_id, "❌ У тебя нет активного челленджа. Напиши /challenge чтобы начать.")
        return
    
    current_day, tasks_completed, tasks_status = row
    
    # Создаём визуальный прогресс
    progress_bar = ""
    for i in range(1, 8):
        if i < current_day:
            progress_bar += "✅ "
        elif i == current_day:
            progress_bar += "🟡 "
        else:
            progress_bar += "⬜ "
    
    await send_message(chat_id,
        f"🏆 ТВОЙ ПРОГРЕСС В ЧЕЛЛЕНДЖЕ\n\n"
        f"{progress_bar}\n\n"
        f"📅 День {current_day} из 7\n"
        f"✅ Выполнено заданий: {tasks_completed}\n"
        f"🎯 Осталось дней: {7 - current_day}\n\n"
        f"Продолжай выполнять задания — каждый шаг приближает тебя к результату! 💪",
        get_challenge_keyboard())

# === СТРАНИЦА ЧАТА ===
HTML_CHAT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>AI Чат | Salesplan</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", Helvetica, sans-serif;
            background: #f5f5f7;
            color: #1d1d1f;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: #fff;
            padding: 16px 20px;
            border-bottom: 1px solid #e5e5e5;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .header h1 {
            font-size: 20px;
            font-weight: 600;
        }
        .days-left {
            font-size: 14px;
            color: #007aff;
            margin-top: 4px;
        }
        .chat-container {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .message {
            max-width: 85%;
            padding: 12px 16px;
            border-radius: 20px;
            font-size: 15px;
            line-height: 1.4;
        }
        .message.user {
            background: #007aff;
            color: white;
            align-self: flex-end;
            border-bottom-right-radius: 4px;
        }
        .message.assistant {
            background: #e5e5ea;
            color: #1d1d1f;
            align-self: flex-start;
            border-bottom-left-radius: 4px;
        }
        .message.assistant pre {
            background: #fff;
            padding: 8px;
            border-radius: 8px;
            overflow-x: auto;
            font-size: 12px;
            margin-top: 8px;
        }
        .input-container {
            background: #fff;
            padding: 12px 20px;
            border-top: 1px solid #e5e5e5;
            display: flex;
            gap: 12px;
            align-items: center;
        }
        .input-container input {
            flex: 1;
            padding: 12px 16px;
            border: 1px solid #ccc;
            border-radius: 25px;
            font-size: 16px;
            font-family: inherit;
            outline: none;
        }
        .input-container input:focus {
            border-color: #007aff;
        }
        .input-container button {
            background: #007aff;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 25px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .input-container button:hover {
            background: #005fc5;
            transform: scale(1.02);
        }
        .input-container button:disabled {
            background: #ccc;
            transform: none;
        }
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 2px solid #e5e5e5;
            border-top-color: #007aff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .plan-message {
            background: #e8f0fe;
            border-left: 3px solid #007aff;
        }
        @media (max-width: 700px) {
            .message {
                max-width: 90%;
            }
            .input-container input {
                font-size: 14px;
            }
            .input-container button {
                padding: 10px 20px;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>💬 AI Чат с Вероникой</h1>
        <div class="days-left" id="daysLeft">Загрузка...</div>
    </div>
    
    <div class="chat-container" id="chatContainer">
        <div class="message assistant plan-message">
            📄 <strong>Твой план готов!</strong><br><br>
            <div id="planText">Загрузка плана...</div>
        </div>
        <div class="message assistant">
            ⬆️ План закреплён вверху. Задавай вопросы ниже — AI поможет разобраться.
        </div>
    </div>
    
    <div class="input-container">
        <input type="text" id="questionInput" placeholder="Задай вопрос о своём бизнесе..." autocomplete="off">
        <button id="sendBtn" onclick="sendQuestion()">Отправить</button>
    </div>

    <script>
        const user_id = "{user_id}";
        const chatContainer = document.getElementById('chatContainer');
        const questionInput = document.getElementById('questionInput');
        const sendBtn = document.getElementById('sendBtn');
        
        let isLoading = false;
        
        // Загрузка дней доступа
        async function loadDaysLeft() {
            const res = await fetch(`/api/days-left?user_id=${user_id}`);
            const data = await res.json();
            document.getElementById('daysLeft').innerHTML = `⏰ Осталось дней AI-доступа: ${data.days_left}`;
        }
        
        // Загрузка плана
        async function loadPlan() {
            const res = await fetch(`/api/premium-plan?user_id=${user_id}`);
            const data = await res.json();
            if (data.plan) {
                document.getElementById('planText').innerHTML = data.plan.replace(/\n/g, '<br>');
            } else {
                document.getElementById('planText').innerHTML = 'План не найден. Обратись в поддержку.';
            }
        }
        
        // Загрузка истории чата
        async function loadHistory() {
            const res = await fetch(`/api/chat-history?user_id=${user_id}`);
            const data = await res.json();
            
            for (const msg of data.history) {
                addMessage(msg.message, msg.role);
            }
        }
        
        function addMessage(text, role) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${role}`;
            messageDiv.innerHTML = text.replace(/\n/g, '<br>');
            chatContainer.appendChild(messageDiv);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
        
        async function sendQuestion() {
            const question = questionInput.value.trim();
            if (!question || isLoading) return;
            
            isLoading = true;
            sendBtn.disabled = true;
            sendBtn.innerHTML = '<span class="loading"></span>';
            
            // Показываем вопрос пользователя
            addMessage(question, 'user');
            questionInput.value = '';
            
            // Отправляем запрос
            const res = await fetch('/api/ask', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: user_id, question: question })
            });
            const data = await res.json();
            
            // Показываем ответ
            addMessage(data.answer, 'assistant');
            
            isLoading = false;
            sendBtn.disabled = false;
            sendBtn.innerHTML = 'Отправить';
        }
        
        questionInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sendQuestion();
        });
        
        loadDaysLeft();
        loadPlan();
        loadHistory();
    </script>
</body>
</html>
"""

# === ЭНДПОИНТЫ ===
@app.get("/chat")
async def chat_page(user_id: str):
    """Страница чата с AI"""
    
    # Проверяем доступ
    if not has_active_access(user_id):
        # Перенаправляем на диагностику или оплату
        return RedirectResponse(url=f"/payment?user_id={user_id}", status_code=303)
    
    return HTMLResponse(content=HTML_CHAT.replace("{user_id}", user_id))

@app.get("/api/days-left")
async def api_days_left(user_id: str):
    """API: количество оставшихся дней"""
    days_left = get_days_left(user_id)
    return {"days_left": days_left}

@app.get("/api/premium-plan")
async def api_premium_plan(user_id: str):
    """API: текст премиум-плана"""
    report_text = get_premium_report_text(user_id)
    return {"plan": report_text or "План не найден"}

@app.get("/api/chat-history")
async def api_chat_history(user_id: str):
    """API: история чата"""
    history = get_chat_history(user_id, 50)
    return {"history": history}

@app.post("/api/ask")
async def api_ask(request: Request):
    """API: задать вопрос AI"""
    data = await request.json()
    user_id = data.get("user_id")
    question = data.get("question")
    
    if not user_id or not question:
        return {"answer": "Ошибка: не хватает данных"}
    
    # Проверяем доступ
    if not has_active_access(user_id):
        return {"answer": "⏰ Доступ к AI-чату закончился. Оплатите план, чтобы продолжить."}
    
    # Сохраняем вопрос
    save_chat_message(user_id, "user", question)
    
    # Получаем план
    report_text = get_premium_report_text(user_id)
    if not report_text:
        return {"answer": "❌ План не найден. Обратитесь в поддержку."}
    
    # Получаем историю
    history = get_chat_history(user_id, 10)
    
    # Проверяем сложный вопрос
    hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши скрипт"]
    is_hard = any(keyword in question.lower() for keyword in hard_keywords)
    
    if is_hard:
        answer = "🔥 Это сложная задача, которую лучше сделать под ключ.\n\nОставь свой телефон, и я свяжусь с тобой, чтобы помочь внедрить всё правильно."
        # Сохраняем заявку
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO implementation_leads (user_id, question, status)
            VALUES (?, ?, 'new')
        """, (user_id, question))
        conn.commit()
        conn.close()
        await send_notification(ADMIN_CHAT_ID, f"📞 НОВАЯ ЗАЯВКА НА ВНЕДРЕНИЕ\n\nПользователь: {user_id}\nВопрос: {question}")
    else:
        answer = await call_deepseek_chat(question, user_id, report_text, history)
    
    # Сохраняем ответ
    save_chat_message(user_id, "assistant", answer)
    
    return {"answer": answer}

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
    
    user_name = username if username else f"гость"

    if callback_data == CALLBACK_START_AUDIT:
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {"answers": {}, "survey_step": 0})
        await send_callback_answer(callback_id,
            f"Окей, погнали! 🚀\n\n{user_name}, напиши название своего онлайн-бизнеса (как ты представляешь его клиентам):",
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

        payment = await create_yookassa_payment(1490, "Профессиональный маркетинговый план", chat_id)
        if payment and payment.get("confirmation_url"):
            save_pending_payment(chat_id, payment["payment_id"])
            save_user_state(chat_id, STATE_WAITING_PAYMENT, {"payment_id": payment["payment_id"]})
            
            await send_callback_answer(callback_id,
                f"🔍 Запускаю генерацию плана... 1-2 минуты — и всё готово.\n\n"
                f"🔥 Твой профессиональный маркетинговый план будет содержать:\n"
                f"• Разбор 5 конкурентов\n"
                f"• Анализ ЦА\n"
                f"• Готовую воронку\n"
                f"• План действий на месяц\n\n"
                f"👇 Оплати 1490 ₽ — и получи доступ к AI-чату и челленджу",
                get_payment_keyboard(payment["confirmation_url"]))
        else:
            await send_callback_answer(callback_id,
                "❌ Ошибка при создании платежа. Попробуй позже или нажми «Помощь».",
                get_main_menu_keyboard())
            return
        
        report_id = save_report_request(chat_id)
        asyncio.create_task(generate_premium_report(chat_id, biz_data["name"], biz_data["description"], form_data, report_id))
        return

    if callback_data == CALLBACK_ASK_AI:
        if has_active_access(chat_id):
            await send_callback_answer(callback_id,
                "💬 Напиши свой вопрос в чате:\n\n"
                f"https://realplanninig-oss-max-salesplan-bot-1a18.twc1.net/chat?user_id={chat_id}",
                None)
        else:
            await send_callback_answer(callback_id,
                "⏰ Доступ к AI-чату закончился или ещё не оплачен.\n\n"
                "Оплати профессиональный маркетинговый план — 1490 ₽, чтобы получить 30 дней доступа к AI и челленджу.",
                get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE:
        if has_active_access(chat_id):
            await start_or_continue_challenge(chat_id)
            await send_callback_answer(callback_id, "🚀 Запускаю челлендж...", None)
        else:
            await send_callback_answer(callback_id,
                "⏰ Челлендж доступен только после оплаты плана.\n\n"
                "Оплати 1490 ₽ — и получи доступ к AI-чату и 7-дневному челленджу.",
                get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_TASK:
        if has_active_access(chat_id):
            await get_current_task(chat_id)
            await send_callback_answer(callback_id, "📋 Задание на сегодня:", None)
        else:
            await send_callback_answer(callback_id, "⏰ Доступ к челленджу только после оплаты.", get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_DONE:
        if has_active_access(chat_id):
            await mark_task_done(chat_id)
            await send_callback_answer(callback_id, "✅ Отлично!", None)
        else:
            await send_callback_answer(callback_id, "⏰ Доступ к челленджу только после оплаты.", get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_PROGRESS:
        if has_active_access(chat_id):
            await show_progress(chat_id)
            await send_callback_answer(callback_id, "📊 Твой прогресс:", None)
        else:
            await send_callback_answer(callback_id, "⏰ Доступ к челленджу только после оплаты.", get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_DOWNLOAD_REPORT:
        report_status = get_report_status(chat_id)
        if report_status and report_status['status'] == 'ready' and report_status['file_path']:
            filepath = Path(report_status['file_path'])
            if filepath.exists():
                await send_file_message(
                    chat_id,
                    "📄 Держи свой профессиональный маркетинговый план!",
                    str(filepath),
                    "file"
                )
                await asyncio.sleep(2)
                await send_message(chat_id,
                    "🔥 Отлично! Теперь у тебя есть:\n"
                    "✅ Профессиональный маркетинговый план\n"
                    "✅ 30 дней доступа к AI-чату\n"
                    "✅ 7-дневный челлендж внедрения\n\n"
                    "👇 Задавай вопросы AI или начинай челлендж",
                    get_after_payment_keyboard())
            else:
                await send_callback_answer(callback_id, "❌ Файл не найден.", get_main_menu_keyboard())
        else:
            await send_callback_answer(callback_id, "⏳ План ещё готовится. 1-2 минуты.", None)
        return

    if callback_data == CALLBACK_HELP:
        await send_notification(ADMIN_CHAT_ID, f"❓ Запрос помощи от {chat_id}\n⏰ {format_moscow_time()}")
        await send_callback_answer(callback_id, "✅ Запрос отправлен! Я свяжусь с тобой в ближайшее время.", None)
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

                await send_callback_answer(callback_id, "🔍 Запускаю анализ...", None)
                
                await send_analysis_animation(chat_id)
                
                report_text = await call_deepseek_diagnostic(
                    biz_data["name"], biz_data["description"], user_data["answers"])
                
                if report_text:
                    log_event(chat_id, "free_report_generated")
                    save_user_state(chat_id, STATE_MENU, {})
                    
                    max_len = 3800
                    if len(report_text) > max_len:
                        await send_message(chat_id, f"✅ Твоя диагностика:\n\n{report_text[:max_len]}", None)
                        await send_notification(chat_id, report_text[max_len:max_len+max_len])
                    else:
                        await send_message(chat_id, f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(30)
                    
                    await send_message(chat_id,
                        "🔥 Ну как тебе?\n\n"
                        "Это только бесплатная версия. Хочешь полный разбор с конкурентами, AI-чат и челлендж?\n\n"
                        "Закажи профессиональный маркетинговый план за 1490 ₽.\n\n"
                        "👇 Оплати сейчас — и получи 30 дней доступа к AI и челленджу",
                        [
                            [
                                {
                                    "type": "callback",
                                    "text": "🔥 Профессиональный план за 1490 ₽",
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
                    await send_message(chat_id, "⚠️ Диагностика готова (по шаблону).", get_main_menu_keyboard())
        return

async def process_message(user_id: str, text: str, username: str = None):
    logger.info(f"PROCESS_MESSAGE called: user_id={user_id}, text={text}")
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")
    
    user_name = username if username else f"гость"

    if state == STATE_MENU:
        if text == "/start":
            await send_message(str(user_id),
                f"Привет, {user_name}! Я Вероника, продюсер экспертов.\n\n"
                "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
                "Давай сделаем бесплатный аудит твоего бизнеса — 2 минуты, и узнаешь, что теряешь.",
                get_main_menu_keyboard())
            save_user_state(str(user_id), STATE_MENU, data)
        elif text == "/chat":
            if has_active_access(str(user_id)):
                await send_message(str(user_id),
                    f"💬 Переходи в чат:\n\n"
                    f"https://realplanninig-oss-max-salesplan-bot-1a18.twc1.net/chat?user_id={user_id}")
            else:
                await send_message(str(user_id), "⏰ Доступ к чату только после оплаты плана.")
        elif text == "/challenge":
            if has_active_access(str(user_id)):
                await start_or_continue_challenge(str(user_id))
            else:
                await send_message(str(user_id), "⏰ Челлендж доступен только после оплаты плана.")
        else:
            await send_message(str(user_id), "Используй кнопки меню или команды: /start, /chat, /challenge")
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
                
                await send_analysis_animation(str(user_id))
                
                report_text = await call_deepseek_diagnostic(biz_data["name"], biz_data["description"], answers)
                if report_text:
                    log_event(str(user_id), "free_report_generated")
                    save_user_state(str(user_id), STATE_MENU, {})
                    
                    max_len = 3800
                    if len(report_text) > max_len:
                        await send_message(str(user_id), f"✅ Твоя диагностика:\n\n{report_text[:max_len]}", None)
                        await send_notification(str(user_id), report_text[max_len:max_len+max_len])
                    else:
                        await send_message(str(user_id), f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(30)
                    
                    await send_message(str(user_id),
                        "🔥 Ну как тебе?\n\n"
                        "Это только бесплатная версия. Хочешь полный разбор с конкурентами, AI-чат и челлендж?\n\n"
                        "Закажи профессиональный маркетинговый план за 1490 ₽.\n\n"
                        "👇 Оплати сейчас — и получи 30 дней доступа к AI и челленджу",
                        [
                            [
                                {
                                    "type": "callback",
                                    "text": "🔥 Профессиональный план за 1490 ₽",
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
                    await send_message(str(user_id), "⚠️ Диагностика готова (по шаблону).", get_main_menu_keyboard())
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
        await send_message(str(user_id), "👇 Подписывайся на канал", get_channel_subscribe_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

    await send_message(str(user_id), "Используй кнопки меню или команды: /start, /chat, /challenge")

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
    return {"status": "Salesplan bot is running", "version": "4.0"}

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
                
                # Обновляем paid_at в reports
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    UPDATE reports SET paid_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND report_type = 'premium' AND status = 'ready'
                """, (user_id,))
                conn.commit()
                conn.close()
                
                report_status = get_report_status(user_id)
                if report_status and report_status['status'] == 'ready':
                    await send_notification(user_id,
                        "🎉 Ура! Твой профессиональный маркетинговый план готов!\n\n"
                        "👇 Жми кнопку ниже — и забирай результат",
                        get_download_keyboard())
                else:
                    await send_notification(user_id,
                        "✅ Оплата прошла, спасибо!\n\n"
                        "План ещё готовится — 1-2 минуты. Я пришлю уведомление, как только всё будет готово.")
                
                await send_notification(ADMIN_CHAT_ID,
                    f"💰 ПОЛУЧЕНА ОПЛАТА\n\nПользователь: {user_id}\nСумма: 1490 ₽\nТовар: Профессиональный маркетинговый план + AI-чат + Челлендж\n⏰ {format_moscow_time()}")
        
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"YooKassa webhook error: {e}")
        return Response(status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
