# File: main.py — бот Salesplan для MAX (финальная версия)

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
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
YKASSA_SHOP_ID = os.getenv("YKASSA_SHOP_ID", "1310983")
YKASSA_SECRET_KEY = os.getenv("YKASSA_SECRET_KEY")
YKASSA_TEST_MODE = os.getenv("YKASSA_TEST_MODE", "false").lower() == "true"

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
CALLBACK_START = "start"
CALLBACK_AUDIT = "audit"
CALLBACK_PREMIUM_1490 = "premium_1490"
CALLBACK_PLAN_ONLY_490 = "plan_only_490"
CALLBACK_BOOK_CALL = "book_call"
CALLBACK_DOWNLOAD_REPORT = "download_report"
CALLBACK_HELP = "help"
CALLBACK_ASK_AI = "ask_ai"
CALLBACK_CHALLENGE_TASK = "challenge_task"
CALLBACK_CHALLENGE_DONE = "challenge_done"
CALLBACK_CHALLENGE_PROGRESS = "challenge_progress"
CALLBACK_RENEW_CHALLENGE = "renew_challenge"
CALLBACK_RENEW_AI = "renew_ai"

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
            status TEXT DEFAULT 'active',
            renewed_at TIMESTAMP
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

def save_report_request(user_id: str, report_type: str = 'premium') -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        INSERT INTO reports (user_id, report_type, status)
        VALUES (?, ?, 'generating')
    """, (user_id, report_type))
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

def get_report_status(user_id: str, report_type: str = 'premium'):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, status, file_path, ready_at, paid_at FROM reports
        WHERE user_id = ? AND report_type = ?
        ORDER BY created_at DESC LIMIT 1
    """, (user_id, report_type))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "status": row[1], "file_path": row[2], "ready_at": row[3], "paid_at": row[4]}
    return None

def get_premium_report_text(user_id: str) -> str:
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

def has_active_ai_access(user_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT paid_at FROM reports 
        WHERE user_id = ? AND report_type = 'premium' AND status = 'ready' AND paid_at IS NOT NULL
        ORDER BY paid_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return False
    
    paid_at = datetime.fromisoformat(row[0])
    days_left = 30 - (get_moscow_time() - paid_at).days
    return days_left > 0

def get_ai_days_left(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT paid_at FROM reports 
        WHERE user_id = ? AND report_type = 'premium' AND status = 'ready' AND paid_at IS NOT NULL
        ORDER BY paid_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return 0
    
    paid_at = datetime.fromisoformat(row[0])
    days_left = 30 - (get_moscow_time() - paid_at).days
    return max(0, days_left)

def get_active_challenge(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, current_day, tasks_completed, status, start_date, renewed_at
        FROM challenges 
        WHERE user_id = ? AND status = 'active'
        ORDER BY start_date DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "current_day": row[1], "tasks_completed": row[2], "status": row[3], "start_date": row[4], "renewed_at": row[5]}
    return None

def start_new_challenge(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        INSERT INTO challenges (user_id, start_date, current_day, tasks_completed, status)
        VALUES (?, CURRENT_TIMESTAMP, 1, 0, 'active')
    """, (user_id,))
    challenge_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return challenge_id

def renew_challenge(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE challenges SET status = 'completed'
        WHERE user_id = ? AND status = 'active'
    """, (user_id,))
    cursor = conn.execute("""
        INSERT INTO challenges (user_id, start_date, current_day, tasks_completed, status, renewed_at)
        VALUES (?, CURRENT_TIMESTAMP, 1, 0, 'active', CURRENT_TIMESTAMP)
    """, (user_id,))
    challenge_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return challenge_id

def save_challenge_task(challenge_id: int, day_number: int, task_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO challenge_tasks (challenge_id, day_number, task_text)
        VALUES (?, ?, ?)
    """, (challenge_id, day_number, task_text))
    conn.commit()
    conn.close()

def get_current_task(challenge_id: int, day_number: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, task_text, is_completed FROM challenge_tasks
        WHERE challenge_id = ? AND day_number = ?
    """, (challenge_id, day_number))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "task_text": row[1], "is_completed": row[2]}
    return None

def mark_task_completed(challenge_id: int, day_number: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE challenge_tasks SET is_completed = 1, completed_at = CURRENT_TIMESTAMP
        WHERE challenge_id = ? AND day_number = ?
    """, (challenge_id, day_number))
    conn.execute("""
        UPDATE challenges SET tasks_completed = tasks_completed + 1
        WHERE id = ?
    """, (challenge_id,))
    conn.commit()
    conn.close()

def advance_challenge_day(challenge_id: int, new_day: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE challenges SET current_day = ?
        WHERE id = ?
    """, (new_day, challenge_id))
    conn.commit()
    conn.close()

def complete_challenge(challenge_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE challenges SET status = 'completed'
        WHERE id = ?
    """, (challenge_id,))
    conn.commit()
    conn.close()

def save_chat_message(user_id: str, role: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO chat_history (user_id, role, message)
        VALUES (?, ?, ?)
    """, (user_id, role, message))
    conn.commit()
    conn.close()

def get_chat_history(user_id: str, limit: int = 10) -> list:
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

def save_implementation_lead(user_id: str, question: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO implementation_leads (user_id, question, status)
        VALUES (?, ?, 'new')
    """, (user_id, question))
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

async def send_notification_to_channel(text: str):
    """Отправка уведомления в канал MAX"""
    if not ADMIN_CHANNEL_ID:
        logger.error("ADMIN_CHANNEL_ID not configured")
        return
    
    url = f"{MAX_API_URL}/messages?channel_id={ADMIN_CHANNEL_ID}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_notification_to_channel failed: {resp.status} - {error_text}")
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
                "payload": CALLBACK_AUDIT,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "❓ Помощь",
                "payload": CALLBACK_HELP,
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

def get_upsell_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "🔥 План + AI + Челлендж — 1 490 ₽",
                "payload": CALLBACK_PREMIUM_1490,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "📄 Только план — 490 ₽",
                "payload": CALLBACK_PLAN_ONLY_490,
                "intent": "default"
            }
        ]
    ]

def get_payment_keyboard(confirmation_url: str, amount: int):
    return [
        [
            {
                "type": "link",
                "text": f"💳 Оплатить {amount} ₽",
                "url": confirmation_url
            }
        ],
        [
            {
                "type": "callback",
                "text": "❓ Помощь",
                "payload": CALLBACK_HELP,
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
                "text": "🏆 Начать челлендж",
                "payload": CALLBACK_CHALLENGE_TASK,
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
                "payload": CALLBACK_CHALLENGE_TASK,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "✅ Выполнил задание",
                "payload": CALLBACK_CHALLENGE_DONE,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "📊 Мой прогресс",
                "payload": CALLBACK_CHALLENGE_PROGRESS,
                "intent": "default"
            }
        ]
    ]

def get_renew_challenge_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "💰 Продлить челлендж — 490 ₽",
                "payload": CALLBACK_RENEW_CHALLENGE,
                "intent": "default"
            }
        ]
    ]

def get_renew_ai_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "💰 Продлить AI-доступ — 490 ₽",
                "payload": CALLBACK_RENEW_AI,
                "intent": "default"
            }
        ]
    ]

def get_consultation_keyboard():
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
                "text": "📞 Заказать консультацию",
                "payload": CALLBACK_BOOK_CALL,
                "intent": "default"
            }
        ]
    ]

def get_implementation_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "📞 Оставить заявку",
                "payload": CALLBACK_BOOK_CALL,
                "intent": "default"
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

Напиши отчет в деловом, практичном стиле:
- Уверенный, прямой, без воды
- Используй конкретные примеры
- Обращайся на "ты"
- НЕ используй символы форматирования (*, #, _, `, ~)
- Для списков используй просто дефис (-)
- Заголовки пиши ЗАГЛАВНЫМИ БУКВАМИ

Структура отчета:

1. ОБЩАЯ КАРТИНА
   - Ниша бизнеса — где ты находишься
   - Целевая аудитория — кто они, чего хотят, что им мешает
   - Оценка текущего состояния от 0 до 100

2. СИЛЬНЫЕ СТОРОНЫ И ТОЧКИ РОСТА
   - Что уже работает (3 пункта)
   - Что можно усилить (3 пункта)

3. ПЕРВЫЕ ШАГИ
   - 3 конкретных действия, которые можно сделать прямо сейчас

Пиши по делу, без лишних слов. Конкретно и полезно."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-консультант. Отвечай по делу, конкретно, без лишних слов."},
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

async def call_deepseek_premium_report(name: str, description: str, answers: dict):
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
    prompt = f"""Сделай профессиональный план запуска продаж для онлайн-бизнеса.

ДАННЫЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши план в деловом, практичном стиле:
- Уверенный, директивный, без воды
- Используй конкретные примеры
- Обращайся на "ты"
- НЕ используй символы форматирования (*, #, _, `, ~)
- Для списков используй просто дефис (-)
- Заголовки пиши ЗАГЛАВНЫМИ БУКВАМИ

Структура плана:

1. РЕАЛЬНОСТЬ
   - Честная оценка текущей ситуации с цифрами и фактами

2. ПОЛЕ БИТВЫ
   - Разбор 3-5 конкурентов: кто они, чем сильны, где их слабые места

3. ТВОЙ КЛИЕНТ
   - Психологический портрет идеального клиента
   - Его боли, страхи, истинные желания

4. ТОЧКИ ОПОРЫ
   - Что у тебя уже работает
   - Что разрушает твои продажи

5. ВОРОНКА
   - Пошаговый путь клиента от "кто это?" до "беру!"
   - Какие каналы использовать
   - Какие триггеры сработают

6. ПЛАН НА МЕСЯЦ
   - Что делать в первую неделю
   - Что делать во вторую
   - Что делать в третью
   - Что делать в четвёртую
   - Ключевые точки контроля

Пиши по делу, без лишних слов. Конкретно и полезно."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-консультант. Отвечай по делу, конкретно, без лишних слов."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=180)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek error: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

async def call_deepseek_chat(question: str, user_id: str, report_text: str, history: list) -> str:
    history_text = ""
    for msg in history[-5:]:
        role = "Пользователь" if msg["role"] == "user" else "Вероника"
        history_text += f"{role}: {msg['message']}\n"
    
    prompt = f"""Ты — профессиональный бизнес-консультант.

Вот маркетинговый план пользователя:
{report_text[:3000]}

История диалога (последние 5 сообщений):
{history_text}

Теперь пользователь спрашивает:
{question}

Твоя задача — ответить в деловом, практичном стиле:
- Уверенно, прямо, по делу
- Конкретные рекомендации
- Обращайся на "ты"
- Без воды, без пустых обещаний

Если вопрос сложный (просит настроить рекламу, сделать воронку, написать скрипты, внедрить) — скажи честно:

🔥 Это задача для профессионального внедрения. Оставь заявку, я свяжусь с тобой и помогу внедрить правильно.

Если вопрос простой и по бизнесу — ответь чётко, по делу, с конкретными рекомендациями, опираясь на план пользователя.

Если вопрос не по бизнесу — мягко направь в нужное русло.

Пиши по делу, без лишних слов. Конкретно и полезно."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-консультант. Отвечай по делу, конкретно, без лишних слов."},
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
            return "Ой, что-то пошло не так. Попробуй переформулировать вопрос."
    except Exception as e:
        logger.error(f"DeepSeek chat failed: {e}")
        return "Не могу ответить сейчас. Попробуй позже."

async def generate_challenge_task(user_id: str, day: int, report_text: str) -> str:
    prompt = f"""Ты — профессиональный бизнес-наставник.

Вот маркетинговый план пользователя:
{report_text[:2000]}

День {day} из 7.

Придумай задание в деловом, практичном стиле:
- Конкретно, выполнимо, измеримо
- С вызовом, но без давления
- НЕ используй символы форматирования

Формат задания:

💪 ЗАДАНИЕ ДЕНЬ {day}

[Конкретное действие, которое приближает к внедрению плана]

📝 ЧЕК-ЛИСТ:
- [ ] пункт 1
- [ ] пункт 2
- [ ] пункт 3

🎯 ЗАЧЕМ ЭТО: [объяснение ценности — 1-2 предложения]

Без лишних слов. Только задание, чек-лист и смысл."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-наставник. Давай чёткие, выполнимые задания."},
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
            return f"💪 ЗАДАНИЕ ДЕНЬ {day}\nНапиши 3 идеи для улучшения своего бизнеса и выбери одну для внедрения.\n\n📝 ЧЕК-ЛИСТ:\n- Запиши 3 идеи\n- Выбери лучшую\n- Напиши план действий\n\n🎯 ЗАЧЕМ ЭТО: Чтобы начать действовать, а не просто читать."
    except Exception as e:
        logger.error(f"Generate task error: {e}")
        return f"💪 ЗАДАНИЕ ДЕНЬ {day}\nПрочитай свой маркетинговый план и найди 1 пункт, который можно сделать сегодня.\n\n📝 ЧЕК-ЛИСТ:\n- Открой план\n- Выбери один пункт\n- Сделай его\n\n🎯 ЗАЧЕМ ЭТО: Маленькие шаги ведут к большим результатам."

async def send_analysis_animation(chat_id: str):
    steps = [
        "🔄 Анализируем бизнес...\n\n⏳ 1/3",
        "🔄 Изучаем целевую аудиторию...\n\n⏳ 2/3",
        "🔄 Формируем рекомендации...\n\n⏳ 3/3"
    ]
    
    for step in steps:
        await send_message(chat_id, step, None)
        await asyncio.sleep(5)
    
    await send_message(chat_id, "✅ Готово!", None)

# === ОБРАБОТЧИКИ ===
async def process_callback(chat_id: str, callback_id: str, callback_data: str):
    state, data = get_user_state(chat_id)
    log_event(chat_id, f"callback_{callback_data}")

    if callback_data == CALLBACK_AUDIT:
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {"answers": {}, "survey_step": 0})
        await send_callback_answer(callback_id,
            "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса (как ты представляешь его клиентам):",
            None)
        return

    if callback_data == CALLBACK_PREMIUM_1490:
        biz_data = get_business_data(chat_id)
        form_data = get_form(chat_id)
        if not biz_data or not form_data:
            await send_callback_answer(callback_id,
                "Ой, стоп! Сначала нужно пройти бесплатную диагностику.\n\n"
                "Это быстро — 2 минуты. Потом сможешь оплатить план.\n\n"
                "👇 Пройди диагностику",
                get_main_menu_keyboard())
            return

        payment = await create_yookassa_payment(1, "Профессиональный маркетинговый план", chat_id)
        if payment and payment.get("confirmation_url"):
            save_pending_payment(chat_id, payment["payment_id"])
            save_user_state(chat_id, STATE_WAITING_PAYMENT, {"payment_id": payment["payment_id"], "amount": 1490})
            
            await send_callback_answer(callback_id,
                "🔥 Отличный выбор!\n\n"
                "Ты получаешь:\n"
                "✅ Маркетинговый план\n"
                "✅ 30 дней AI-консультаций\n"
                "✅ 7-дневный челлендж\n"
                "✅ Доступ в закрытый канал\n\n"
                "💰 Цена: 1 490 ₽\n\n"
                "👇 Оплати, и я запущу генерацию",
                get_payment_keyboard(payment["confirmation_url"], 1))
        else:
            await send_callback_answer(callback_id,
                "❌ Ошибка при создании платежа. Попробуй позже.\n\n"
                "👇 Если проблема повторяется — нажми «Помощь», я проверю вручную",
                get_main_menu_keyboard())
            return
        
        report_id = save_report_request(chat_id, 'premium')
        asyncio.create_task(generate_premium_report(chat_id, biz_data["name"], biz_data["description"], form_data, report_id))
        return

    if callback_data == CALLBACK_PLAN_ONLY_490:
        biz_data = get_business_data(chat_id)
        form_data = get_form(chat_id)
        if not biz_data or not form_data:
            await send_callback_answer(callback_id,
                "Ой, стоп! Сначала нужно пройти бесплатную диагностику.\n\n"
                "Это быстро — 2 минуты. Потом сможешь оплатить план.\n\n"
                "👇 Пройди диагностику",
                get_main_menu_keyboard())
            return

        payment = await create_yookassa_payment(1, "Маркетинговый план", chat_id)
        if payment and payment.get("confirmation_url"):
            save_pending_payment(chat_id, payment["payment_id"])
            save_user_state(chat_id, STATE_WAITING_PAYMENT, {"payment_id": payment["payment_id"], "amount": 490})
            
            await send_callback_answer(callback_id,
                "📄 Ты выбрала только маркетинговый план.\n\n"
                "Ты получишь:\n"
                "✅ Персональный профессиональный маркетинговый план\n\n"
                "💰 Цена: 490 ₽\n\n"
                "👇 Оплати, и я запущу генерацию",
                get_payment_keyboard(payment["confirmation_url"], 1))
        else:
            await send_callback_answer(callback_id,
                "❌ Ошибка при создании платежа. Попробуй позже.\n\n"
                "👇 Если проблема повторяется — нажми «Помощь», я проверю вручную",
                get_main_menu_keyboard())
            return
        
        report_id = save_report_request(chat_id, 'premium')
        asyncio.create_task(generate_premium_report(chat_id, biz_data["name"], biz_data["description"], form_data, report_id))
        return

    if callback_data == CALLBACK_ASK_AI:
        if has_active_ai_access(chat_id):
            await send_callback_answer(callback_id,
                "💬 Отлично! Теперь ты можешь задавать вопросы по плану прямо здесь.\n\n"
                "Что тебя интересует? Я на связи 24/7.",
                None)
        else:
            await send_callback_answer(callback_id,
                "⏰ 30 дней прошло. За это время ты могла многое сделать.\n\n"
                "Рекомендую заново пройти диагностику — у тебя новый уровень бизнеса, и план нужно обновить.\n\n"
                "👇 Пройди диагностику за 2 минуты",
                get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_TASK:
        if not has_active_ai_access(chat_id):
            await send_callback_answer(callback_id,
                "⏰ Челлендж доступен только после оплаты плана.\n\n"
                "👇 Пройди диагностику и оплати план",
                get_main_menu_keyboard())
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            challenge_id = start_new_challenge(chat_id)
            report_text = get_premium_report_text(chat_id)
            task_text = await generate_challenge_task(chat_id, 1, report_text)
            save_challenge_task(challenge_id, 1, task_text)
            await send_callback_answer(callback_id,
                f"🏆 ПОЕХАЛИ! Челлендж «7 дней внедрения» начался!\n\n{task_text}\n\n"
                f"👇 Когда сделаешь — нажми «Выполнил задание»",
                get_challenge_keyboard())
        else:
            current_task = get_current_task(challenge["id"], challenge["current_day"])
            if current_task and not current_task["is_completed"]:
                await send_callback_answer(callback_id,
                    f"📋 ТВОЁ ЗАДАНИЕ НА ДЕНЬ {challenge['current_day']}\n\n{current_task['task_text']}\n\n"
                    f"👇 Когда сделаешь — нажми «Выполнил задание»",
                    get_challenge_keyboard())
            else:
                await send_callback_answer(callback_id,
                    f"🏆 Твой прогресс: день {challenge['current_day']} из 7, выполнено {challenge['tasks_completed']} заданий.\n\n"
                    f"👇 Продолжай!",
                    get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_DONE:
        if not has_active_ai_access(chat_id):
            await send_callback_answer(callback_id,
                "⏰ Челлендж доступен только после оплаты плана.",
                get_main_menu_keyboard())
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Начать челлендж»",
                get_after_payment_keyboard())
            return
        
        current_task = get_current_task(challenge["id"], challenge["current_day"])
        if not current_task or current_task["is_completed"]:
            await send_callback_answer(callback_id,
                "✅ Задание на сегодня уже выполнено! Жди завтрашнее задание.",
                get_challenge_keyboard())
            return
        
        mark_task_completed(challenge["id"], challenge["current_day"])
        
        if challenge["current_day"] >= 7:
            complete_challenge(challenge["id"])
            await send_callback_answer(callback_id,
                f"🎉 ПОЗДРАВЛЯЮ! Ты прошла челлендж «7 дней внедрения»!\n\n"
                f"✅ Выполнено заданий: {challenge['tasks_completed'] + 1} из 7\n\n"
                f"🔥 Хочешь продолжить? Следующая неделя — новые задания, новые вызовы.\n\n"
                f"💎 ПРОДЛЕНИЕ ЧЕЛЛЕНДЖА — 490 ₽/неделя\n\n"
                f"👇 Продлить на неделю",
                get_renew_challenge_keyboard())
        else:
            new_day = challenge["current_day"] + 1
            advance_challenge_day(challenge["id"], new_day)
            
            report_text = get_premium_report_text(chat_id)
            task_text = await generate_challenge_task(chat_id, new_day, report_text)
            save_challenge_task(challenge["id"], new_day, task_text)
            
            await send_callback_answer(callback_id,
                f"✅ Отлично! Задание дня {challenge['current_day']} выполнено!\n\n"
                f"🏆 Прогресс: {challenge['tasks_completed'] + 1} заданий сделано\n\n"
                f"💪 ЗАДАНИЕ ДЕНЬ {new_day}\n\n{task_text}\n\n"
                f"👇 Продолжай в том же духе!",
                get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_PROGRESS:
        if not has_active_ai_access(chat_id):
            await send_callback_answer(callback_id,
                "⏰ Челлендж доступен только после оплаты плана.",
                get_main_menu_keyboard())
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Начать челлендж»",
                get_after_payment_keyboard())
            return
        
        progress_bar = ""
        for i in range(1, 8):
            if i < challenge["current_day"]:
                progress_bar += "✅ "
            elif i == challenge["current_day"]:
                progress_bar += "🟡 "
            else:
                progress_bar += "⬜ "
        
        await send_callback_answer(callback_id,
            f"🏆 ТВОЙ ПРОГРЕСС В ЧЕЛЛЕНДЖЕ\n\n{progress_bar}\n\n"
            f"📅 День {challenge['current_day']} из 7\n"
            f"✅ Выполнено заданий: {challenge['tasks_completed']}\n"
            f"🎯 Осталось дней: {7 - challenge['current_day']}\n\n"
            f"Продолжай выполнять задания — каждый шаг приближает тебя к результату! 💪",
            get_challenge_keyboard())
        return

    if callback_data == CALLBACK_RENEW_CHALLENGE:
        await send_callback_answer(callback_id,
            "💎 ПРОДЛЕНИЕ ЧЕЛЛЕНДЖА — 490 ₽\n\n"
            "Оплати, и я запущу новую неделю с другими заданиями.\n\n"
            "👇 Оплатить",
            get_payment_keyboard("https://yookassa.ru/payment-mock", 490))
        return

    if callback_data == CALLBACK_RENEW_AI:
        await send_callback_answer(callback_id,
            "💎 ПРОДЛЕНИЕ AI-ДОСТУПА — 490 ₽\n\n"
            "Оплати, и я продлю доступ ещё на 30 дней.\n\n"
            "👇 Оплатить",
            get_payment_keyboard("https://yookassa.ru/payment-mock", 490))
        return

    if callback_data == CALLBACK_BOOK_CALL:
        save_user_state(chat_id, STATE_WAITING_CALL, {})
        await send_callback_answer(callback_id,
            "🔥 Бонус для подписчиков канала!\n\n"
            "Подпишись на мой канал в MAX — там я делюсь:\n"
            "• Кейсами с цифрами\n"
            "• Разборами ошибок\n"
            "• Скриптами, которые продают\n\n"
            "После подписки получишь 30 минут БЕСПЛАТНОГО разбора твоего плана действий.\n"
            "Найдём одно действие, которое принесёт тебе деньги прямо сейчас.\n\n"
            "👇 Подписывайся",
            get_consultation_keyboard())
        return

    if callback_data == CALLBACK_HELP:
        await send_notification_to_channel(f"❓ Запрос помощи\n\nПользователь: {chat_id}\n⏰ {format_moscow_time()}")
        await send_callback_answer(callback_id,
            "✅ Запрос отправлен! Я свяжусь с тобой в ближайшее время.",
            None)
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
                        "❌ Что-то пошло не так. Попробуй позже.",
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
                        await send_message(chat_id, report_text[max_len:max_len+max_len], None)
                    else:
                        await send_message(chat_id, f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(30)
                    
                    await send_message(chat_id,
                        "🔥 Как тебе диагностика?\n\n"
                        "Это только первый шаг. Чтобы РЕАЛЬНО внедрить план и начать продавать, нужна системная поддержка.\n\n"
                        "Вот что я предлагаю:\n\n"
                        "🚀 ПАКЕТ «ПЛАН + AI + ЧЕЛЛЕНДЖ» — 1 490 ₽\n\n"
                        "Ты получаешь:\n"
                        "✅ Персональный профессиональный маркетинговый план продаж\n"
                        "✅ 30 дней AI-консультаций — задавай любые вопросы по плану\n"
                        "✅ Челлендж на 7 дней с заданиями\n"
                        "✅ Доступ в закрытый канал MAX с кейсами и разборами\n\n"
                        "💰 Цена: 1 490 ₽ (вместо 4 900 ₽)\n\n"
                        "ИЛИ\n\n"
                        "📄 Только маркетинговый план — 490 ₽\n"
                        "(без AI, без челленджа, без чата)\n\n"
                        "Поверь: с AI ты внедришь в 10 раз быстрее.\n\n"
                        "👇 Выбирай",
                        get_upsell_keyboard())
                else:
                    await send_message(chat_id,
                        "❌ Что-то пошло не так. Попробуй позже.",
                        get_main_menu_keyboard())
        return

async def process_message(user_id: str, text: str):
    logger.info(f"PROCESS_MESSAGE called: user_id={user_id}, text={text}")
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")

    if state == STATE_MENU:
        # Приветствие на любое сообщение
        await send_message(str(user_id),
            "👋 Привет! Я Вероника, продюсер экспертов.\n\n"
            "Что умеет этот бот?\n\n"
            "✅ 1 мин — бесплатный аудит\n"
            "✅ 3 мин — маркетинговый план\n"
            "✅ 30 дней — AI-чат\n"
            "✅ 7 дней — челлендж\n\n"
            "👇 Жми кнопку, начнём",
            get_main_menu_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
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
                    await send_message(str(user_id), "❌ Что-то пошло не так. Попробуй позже.", get_main_menu_keyboard())
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
                        await send_message(str(user_id), report_text[max_len:max_len+max_len], None)
                    else:
                        await send_message(str(user_id), f"✅ Твоя диагностика:\n\n{report_text}", None)
                    
                    await asyncio.sleep(30)
                    
                    await send_message(str(user_id),
                        "🔥 Как тебе диагностика?\n\n"
                        "Это только первый шаг. Чтобы РЕАЛЬНО внедрить план и начать продавать, нужна системная поддержка.\n\n"
                        "Вот что я предлагаю:\n\n"
                        "🚀 ПАКЕТ «ПЛАН + AI + ЧЕЛЛЕНДЖ» — 1 490 ₽\n\n"
                        "Ты получаешь:\n"
                        "✅ Персональный профессиональный маркетинговый план продаж\n"
                        "✅ 30 дней AI-консультаций — задавай любые вопросы по плану\n"
                        "✅ Челлендж на 7 дней с заданиями\n"
                        "✅ Доступ в закрытый канал MAX с кейсами и разборами\n\n"
                        "💰 Цена: 1 490 ₽ (вместо 4 900 ₽)\n\n"
                        "ИЛИ\n\n"
                        "📄 Только маркетинговый план — 490 ₽\n"
                        "(без AI, без челленджа, без чата)\n\n"
                        "Поверь: с AI ты внедришь в 10 раз быстрее.\n\n"
                        "👇 Выбирай",
                        get_upsell_keyboard())
                else:
                    await send_message(str(user_id),
                        "❌ Что-то пошло не так. Попробуй позже.",
                        get_main_menu_keyboard())
        return

    if state == STATE_WAITING_CALL:
        save_implementation_lead(str(user_id), text)
        await send_notification_to_channel(
            f"📞 ЗАЯВКА НА ВНЕДРЕНИЕ\n\n"
            f"Пользователь: {user_id}\n"
            f"Вопрос: {text}\n"
            f"⏰ {format_moscow_time()}"
        )
        await send_message(str(user_id),
            "✅ Заявка принята! Я свяжусь с тобой в ближайшее время.\n\n"
            "А пока — подпишись на канал, там готовые решения",
            get_consultation_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

    if has_active_ai_access(str(user_id)):
        save_chat_message(str(user_id), "user", text)
        
        report_text = get_premium_report_text(str(user_id))
        history = get_chat_history(str(user_id), 10)
        
        hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши скрипт"]
        is_hard = any(keyword in text.lower() for keyword in hard_keywords)
        
        if is_hard:
            answer = "🔥 Это задача для профессионального внедрения.\n\nЕсли хочешь сделать это правильно и без ошибок — оставь заявку. Я свяжусь с тобой и помогу внедрить.\n\n👇 Нажми кнопку"
            await send_message(str(user_id), answer, get_implementation_keyboard())
        else:
            await send_message(str(user_id), "🤔 Думаю...", None)
            answer = await call_deepseek_chat(text, str(user_id), report_text, history)
            await send_message(str(user_id), answer, get_after_payment_keyboard())
        
        save_chat_message(str(user_id), "assistant", answer)
    else:
        await send_message(str(user_id),
            "⏰ 30 дней прошло. За это время ты могла многое сделать.\n\n"
            "Рекомендую заново пройти диагностику — у тебя новый уровень бизнеса, и план нужно обновить.\n\n"
            "👇 Пройди диагностику за 2 минуты",
            get_main_menu_keyboard())

async def generate_premium_report(user_id: str, name: str, description: str, answers: dict, report_id: int):
    logger.info(f"Generating premium report for {user_id}")
    
    report_text = await call_deepseek_premium_report(name, description, answers)
    
    if report_text:
        filename = f"premium_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = REPORTS_DIR / filename
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(report_text)
        update_report_status(report_id, 'ready', str(filepath))
        logger.info(f"Premium report generated for {user_id}")
    else:
        update_report_status(report_id, 'failed')
        logger.error(f"Premium report failed for {user_id}")

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
    return {"status": "Salesplan bot is running", "version": "5.0"}

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
            body = msg.get("body", {})
            text = body.get("text")
            if user_id and text:
                await process_message(str(user_id), text)

        elif "callback" in payload:
            cb = payload["callback"]
            user_id = cb.get("user", {}).get("user_id")
            callback_id = cb.get("callback_id")
            data = cb.get("payload")
            logger.info(f"CALLBACK RECEIVED: user_id={user_id}, callback_id={callback_id}, payload={data}")
            if user_id and data:
                await process_callback(str(user_id), str(callback_id), data)

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
                
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    UPDATE reports SET paid_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND report_type = 'premium' AND status = 'ready'
                """, (user_id,))
                conn.commit()
                conn.close()
                
                report_status = get_report_status(user_id)
                if report_status and report_status['status'] == 'ready':
                    report_text = get_premium_report_text(user_id)
                    if report_text:
                        await send_message(user_id, f"🎉 Твой маркетинговый план готов!\n\n{report_text}\n\n⬆️ План выше. Теперь у тебя есть:\n✅ 30 дней AI-чата\n✅ 7-дневный челлендж\n\nЗадавай вопросы в этом чате или начни челлендж 👇", get_after_payment_keyboard())
                    else:
                        await send_message(user_id, "🎉 Твой маркетинговый план готов!\n\nПлан временно недоступен. Я пришлю его через минуту.", get_after_payment_keyboard())
                else:
                    await send_message(user_id, "✅ Оплата прошла, спасибо!\n\nПлан ещё готовится. Страница обновится сама. Не уходи из чата.", None)
                
                await send_notification_to_channel(
                    f"💰 ПОЛУЧЕНА ОПЛАТА\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Сумма: 1490 ₽\n"
                    f"Товар: Профессиональный маркетинговый план + AI-чат + Челлендж\n"
                    f"⏰ {format_moscow_time()}"
                )
        
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"YooKassa webhook error: {e}")
        return Response(status_code=500)

@app.get("/get_channel_id")
async def get_channel_id():
    """Вспомогательный эндпоинт для получения ID каналов"""
    url = f"{MAX_API_URL}/channels"
    headers = {"Authorization": MAX_BOT_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            return data

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
