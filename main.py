# File: main.py — бот Salesplan для MAX (версия 9.8: исправлен порядок определения app)

import asyncio
import logging
import sqlite3
import os
import json
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import aiohttp
import requests
import uvicorn
import secrets

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
REVIEWS_URL = os.getenv("REVIEWS_URL", "https://vk.ru/topic-164421538_39653658")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
HELP_URL = os.getenv("HELP_URL", "https://max.ru/u/f9LHodD0cOJp3NEa7OYZr1MKfUuC1hYDyKh2f4HFkfTXT88W3txWaBaFQmU")

# Для админ-эндпоинта
ADMIN_USERNAME = os.getenv("BOT_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("BOT_ADMIN_PASSWORD")

if not MAX_BOT_TOKEN:
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found in .env")

LOGS_DIR = Path("./logs")
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOGS_DIR / "salesplan_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = "salesplan_bot.db"

# === СОСТОЯНИЯ ===
STATE_MENU = "menu"
STATE_AWAITING_BUSINESS_NAME = "awaiting_business_name"
STATE_AWAITING_BUSINESS_DESCRIPTION = "awaiting_business_description"
STATE_SURVEY = "survey"
STATE_AI_CHAT = "ai_chat"
STATE_AWAITING_IMPLEMENTATION = "awaiting_implementation"

# === CALLBACK DATA ===
CALLBACK_AUDIT = "audit"
CALLBACK_ASK_AI = "ask_ai"
CALLBACK_CHALLENGE_TASK = "challenge_task"
CALLBACK_CHALLENGE_DONE = "challenge_done"
CALLBACK_CHALLENGE_PROGRESS = "challenge_progress"
CALLBACK_IMPLEMENTATION = "implementation"
CALLBACK_MENU = "menu"
CALLBACK_RESET = "reset"

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
def init_bot_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deepseek_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            query_type TEXT NOT NULL,
            prompt TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_bot_db()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def format_moscow_time(dt=None):
    if dt is None:
        dt = get_moscow_time()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def log_event(user_id: str, event_type: str, event_data: str = None):
    logger.info(f"Event: {event_type} | User: {user_id} | Data: {event_data}")

def log_deepseek_query(user_id: str, query_type: str, prompt: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO deepseek_queries (user_id, query_type, prompt)
            VALUES (?, ?, ?)
        """, (user_id, query_type, prompt))
        conn.commit()
        conn.close()
        logger.info(f"DeepSeek query logged: user={user_id}, type={query_type}")
    except Exception as e:
        logger.error(f"Failed to log DeepSeek query: {e}")

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
    cursor = conn.execute("SELECT business_name, business_description FROM business_data WHERE user_id = ?", (user_id,))
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
    cursor = conn.execute("SELECT q1, q2, q3, q4, q5 FROM forms WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"q1": row[0], "q2": row[1], "q3": row[2], "q4": row[3], "q5": row[4]}
    return None

def save_report(user_id: str, report_type: str, report_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO reports (user_id, report_type, report_text, status, ready_at)
        VALUES (?, ?, ?, 'ready', CURRENT_TIMESTAMP)
    """, (user_id, report_type, report_text))
    conn.commit()
    conn.close()

def update_report_status(user_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE reports SET status = ?, ready_at = CASE WHEN ? = 'ready' THEN CURRENT_TIMESTAMP ELSE ready_at END
        WHERE user_id = ? AND report_type = 'premium' AND status != 'ready'
    """, (status, status, user_id))
    conn.commit()
    conn.close()

def get_report(user_id: str, report_type: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT report_text, status FROM reports 
        WHERE user_id = ? AND report_type = ? 
        ORDER BY created_at DESC LIMIT 1
    """, (user_id, report_type))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"text": row[0], "status": row[1]}
    return None

def get_active_challenge(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, current_day, tasks_completed, status, start_date
        FROM challenges 
        WHERE user_id = ? AND status = 'active'
        ORDER BY start_date DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "current_day": row[1], "tasks_completed": row[2], "status": row[3], "start_date": row[4]}
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

# === DEEPSEEK API ===
async def call_deepseek_marketing_plan(name: str, description: str, answers: dict, user_id: str = None) -> str:
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not configured")
        return None
    
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
    prompt = f"""Сделай профессиональный маркетинговый план для онлайн-бизнеса.

ДАННЫЕ О БИЗНЕСЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши план в деловом, практичном стиле:
- Уверенный, прямой, без воды
- Используй конкретные примеры
- Обращайся на "ты"
- НЕ используй символы форматирования
- Для списков используй просто дефис -

ВАЖНО: Исключи рекламу через Instagram и Telegram. Вместо них используй продвижение через VK, Яндекс.Директ и автоворонку через мессенджер MAX.

Структура плана:

1. РЕАЛЬНОСТЬ
2. КОНКУРЕНТЫ
3. ТВОЙ КЛИЕНТ
4. СИЛЬНЫЕ И СЛАБЫЕ СТОРОНЫ
5. ВОРОНКА
6. ПЛАН НА МЕСЯЦ"""
    
    if user_id:
        log_deepseek_query(user_id, "marketing_plan", prompt)
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-консультант."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
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
    history_text = ""
    for msg in history[-5:]:
        role = "Пользователь" if msg["role"] == "user" else "Вероника"
        history_text += f"{role}: {msg['message']}\n"
    
    prompt = f"""Ты — профессиональный бизнес-консультант.

Вот маркетинговый план пользователя:
{report_text[:3000]}

История диалога:
{history_text}

Теперь пользователь спрашивает:
{question}

Ответь в деловом, практичном стиле, без воды. Если вопрос сложный (просит настроить рекламу, сделать воронку) — скажи: «🔥 Это задача для профессионального внедрения. Оставь заявку, я свяжусь с тобой»."""
    
    log_deepseek_query(user_id, "chat_question", prompt)
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — бизнес-консультант."},
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
    if not DEEPSEEK_API_KEY:
        return f"ЗАДАНИЕ ДЕНЬ {day}\n\nИзучи свой маркетинговый план и найди 1 пункт, который можно сделать сегодня.\n\nЧЕК-ЛИСТ:\n- Открой план\n- Выбери один пункт\n- Сделай его"
    
    prompt = f"""Ты — бизнес-наставник. Цель — помочь пользователю получить первых клиентов за 2 недели.

Вот план пользователя:
{report_text[:3000]}

День {day} из 14.

Придумай конкретное, выполнимое задание (не более 2 часов), фокус на привлечение первых клиентов."""
    
    log_deepseek_query(user_id, "challenge_task", prompt)
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — бизнес-наставник."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8,
        "max_tokens": 800
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"ЗАДАНИЕ ДЕНЬ {day}\n\nНапиши 3 идеи для привлечения первых клиентов и выбери одну для внедрения."
    except Exception as e:
        logger.error(f"Generate task error: {e}")
        return f"ЗАДАНИЕ ДЕНЬ {day}\n\nПрочитай свой план и найди 1 пункт для привлечения клиентов."

# === ФУНКЦИИ ОТПРАВКИ СООБЩЕНИЙ ===
async def send_message(chat_id: str, text: str, keyboard: list = None):
    url = f"https://platform-api.max.ru/messages?user_id={chat_id}"
    payload = {"text": text}
    if keyboard:
        payload["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"send_message failed: {resp.status} - {await resp.text()}")
            return await resp.text()

async def send_long_message(chat_id: str, text: str, keyboard: list = None):
    max_len = 3900
    if len(text) <= max_len:
        await send_message(chat_id, text, keyboard)
        return
    await send_message(chat_id, text[:max_len], None)
    remaining = text[max_len:]
    while remaining:
        part = remaining[:max_len]
        await send_message(chat_id, part, None)
        remaining = remaining[max_len:]
    if keyboard:
        await send_message(chat_id, "⬆️ Продолжение выше. Что дальше?", keyboard)

async def send_callback_answer(callback_id: str, text: str, keyboard: list = None):
    url = f"https://platform-api.max.ru/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    if keyboard:
        payload["message"]["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"send_callback_answer failed: {resp.status} - {await resp.text()}")
            return await resp.text()

async def send_notification_to_channel(text: str):
    if not ADMIN_CHANNEL_ID or ADMIN_CHANNEL_ID == "None":
        return
    url = f"https://platform-api.max.ru/messages?channel_id={ADMIN_CHANNEL_ID}"
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={"text": text}, headers=headers)

# === КЛАВИАТУРЫ ===
def get_main_menu_keyboard():
    return [
        [{"type": "callback", "text": "📊 Пройти анкету", "payload": CALLBACK_AUDIT}],
        [{"type": "callback", "text": "💬 Задать вопрос AI", "payload": CALLBACK_ASK_AI}],
        [{"type": "callback", "text": "🏆 Челлендж 14 дней", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}]
    ]

def get_after_plan_keyboard():
    return [
        [{"type": "callback", "text": "💬 Задать вопрос AI", "payload": CALLBACK_ASK_AI}],
        [{"type": "callback", "text": "🏆 Начать челлендж 14 дней", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "🔄 Пройти анкету заново", "payload": CALLBACK_AUDIT}],
        [{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}]
    ]

def get_survey_keyboard(question_index: int):
    if question_index >= len(SURVEY_QUESTIONS):
        return None
    q = SURVEY_QUESTIONS[question_index]
    keyboard = [[{"type": "callback", "text": label, "payload": payload}] for payload, label in q["options"]]
    keyboard.append([{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}])
    return keyboard

def get_challenge_with_help_keyboard():
    return [
        [{"type": "callback", "text": "📋 Получить задание", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "✅ Выполнил задание", "payload": CALLBACK_CHALLENGE_DONE}],
        [{"type": "callback", "text": "📊 Мой прогресс", "payload": CALLBACK_CHALLENGE_PROGRESS}],
        [{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Главное меню", "payload": CALLBACK_MENU}]
    ]

def get_ai_keyboard():
    return [
        [{"type": "callback", "text": "🏆 Челлендж 14 дней", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Главное меню", "payload": CALLBACK_MENU}]
    ]

def get_implementation_keyboard():
    return [
        [{"type": "callback", "text": "📞 Оставить заявку", "payload": CALLBACK_IMPLEMENTATION}],
        [{"type": "link", "text": "🆘 Помощь / Связаться", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Главное меню", "payload": CALLBACK_MENU}]
    ]

async def send_animation(user_id: str):
    steps = [
        "🔍 Анализируем бизнес...\n\n⏳ 1/4",
        "📊 Изучаем целевую аудиторию...\n\n⏳ 2/4",
        "🎯 Ищем точки роста...\n\n⏳ 3/4",
        "📝 Формируем план...\n\n⏳ 4/4"
    ]
    for step in steps:
        await send_message(user_id, step, None)
        await asyncio.sleep(3)
    await send_message(user_id, "⏳ Осталось 5 секунд...", None)
    await asyncio.sleep(5)

# === ОБРАБОТЧИКИ СООБЩЕНИЙ И КОЛБЭКОВ ===
async def process_message(user_id: str, text: str):
    state, data = get_user_state(str(user_id))
    if text == "/start":
        save_user_state(str(user_id), STATE_MENU, {})
        await send_message(str(user_id),
            "👋 Привет! Я Вероника, продюсер экспертов.\n\n"
            "Что я умею:\n"
            "✅ Бесплатный маркетинговый план за 2 минуты\n"
            "✅ AI-чат — отвечаю на вопросы 24/7\n"
            "✅ Челлендж «Первые клиенты за 14 дней»\n\n"
            "👇 Начни с анкеты",
            get_main_menu_keyboard())
        return
    if state == STATE_AWAITING_BUSINESS_NAME:
        if len(text) > 100:
            await send_message(str(user_id), "Слишком длинное название. Напиши покороче:")
            return
        save_user_state(str(user_id), STATE_AWAITING_BUSINESS_DESCRIPTION, {"business_name": text})
        await send_message(str(user_id), "Ок, записала! Теперь напиши краткое описание бизнеса:")
        return
    if state == STATE_AWAITING_BUSINESS_DESCRIPTION:
        if len(text) > 500:
            await send_message(str(user_id), "Описание слишком длинное. Напиши покороче (до 500 символов):")
            return
        business_name = data.get("business_name")
        save_business_data(str(user_id), business_name, text)
        save_user_state(str(user_id), STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(str(user_id), SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0))
        return
    if state == STATE_AI_CHAT:
        report = get_report(str(user_id), "premium")
        if not report or report["status"] != "ready":
            await send_message(str(user_id),
                "💬 Ты ещё не получил план. Сначала заполни анкету.",
                [[{"type": "callback", "text": "📊 Пройти анкету", "payload": CALLBACK_AUDIT}]])
            save_user_state(str(user_id), STATE_MENU, {})
            return
        save_chat_message(str(user_id), "user", text)
        report_text = report["text"]
        hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши скрипт"]
        if any(kw in text.lower() for kw in hard_keywords):
            answer = "🔥 Это задача для профессионального внедрения. Оставь заявку, я свяжусь с тобой."
            await send_message(str(user_id), answer, get_implementation_keyboard())
        else:
            await send_message(str(user_id), "🤔 Думаю...", None)
            history = get_chat_history(str(user_id), 10)
            answer = await call_deepseek_chat(text, str(user_id), report_text, history)
            answer += "\n\n📜 *Листай вверх к началу плана*"
            await send_message(str(user_id), answer, get_ai_keyboard())
        save_chat_message(str(user_id), "assistant", answer)
        return
    if state == STATE_AWAITING_IMPLEMENTATION:
        await send_notification_to_channel(
            f"📞 ЗАЯВКА НА ВНЕДРЕНИЕ\nПользователь: {user_id}\nЗапрос: {text}\n⏰ {format_moscow_time()}"
        )
        await send_message(str(user_id), "✅ Заявка принята! Я свяжусь с тобой.", get_main_menu_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return
    save_user_state(str(user_id), STATE_MENU, {})
    await send_message(str(user_id), "👋 Привет! Я Вероника.\n\n👇 Нажми кнопку, чтобы начать", get_main_menu_keyboard())

async def process_callback(chat_id: str, callback_id: str, callback_data: str):
    state, data = get_user_state(chat_id)
    if callback_data == CALLBACK_MENU:
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id, "🏠 Главное меню", get_main_menu_keyboard())
        return
    if callback_data == CALLBACK_RESET:
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id, "👋 Привет! Я Вероника.\n👇 Начни с анкеты", get_main_menu_keyboard())
        return
    if callback_data == CALLBACK_AUDIT:
        if state in (STATE_SURVEY, STATE_AWAITING_BUSINESS_NAME, STATE_AWAITING_BUSINESS_DESCRIPTION):
            await send_callback_answer(callback_id,
                "⚠️ Анкета уже запущена.\nЕсли хочешь начать заново — нажми «🔄 Начать заново»",
                [[{"type": "callback", "text": "🔄 Начать заново", "payload": CALLBACK_RESET}]])
            return
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {})
        await send_callback_answer(callback_id, "🚀 Давай разберём твой бизнес.\nНапиши название проекта:", None)
        return
    if callback_data == CALLBACK_ASK_AI:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id,
                "💬 Ты ещё не прошёл анкету.",
                [[{"type": "callback", "text": "📊 Пройти анкету", "payload": CALLBACK_AUDIT}]])
            return
        save_user_state(chat_id, STATE_AI_CHAT, {})
        await send_callback_answer(callback_id, "💬 Задавай вопросы по своему маркетинговому плану.", None)
        return
    if callback_data == CALLBACK_CHALLENGE_TASK:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id, "🏆 Челлендж доступен после получения плана.",
                [[{"type": "callback", "text": "📊 Пройти анкету", "payload": CALLBACK_AUDIT}]])
            return
        report_text = report["text"]
        challenge = get_active_challenge(chat_id)
        if not challenge:
            challenge_id = start_new_challenge(chat_id)
            task_text = await generate_challenge_task(chat_id, 1, report_text)
            save_challenge_task(challenge_id, 1, task_text)
            await send_callback_answer(callback_id,
                f"🏆 Челлендж начался!\n\n{task_text}",
                get_challenge_with_help_keyboard())
        else:
            current_task = get_current_task(challenge["id"], challenge["current_day"])
            if current_task and not current_task["is_completed"]:
                await send_callback_answer(callback_id,
                    f"📋 Задание дня {challenge['current_day']}:\n\n{current_task['task_text']}",
                    get_challenge_with_help_keyboard())
            else:
                await send_callback_answer(callback_id,
                    f"🏆 Прогресс: день {challenge['current_day']} из 14, выполнено {challenge['tasks_completed']}",
                    get_challenge_with_help_keyboard())
        return
    if callback_data == CALLBACK_CHALLENGE_DONE:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id, "🏆 Сначала получи план.",
                [[{"type": "callback", "text": "📊 Пройти анкету", "payload": CALLBACK_AUDIT}]])
            return
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id, "❌ Нет активного челленджа.", get_challenge_with_help_keyboard())
            return
        current_task = get_current_task(challenge["id"], challenge["current_day"])
        if not current_task or current_task["is_completed"]:
            await send_callback_answer(callback_id, "✅ Задание на сегодня уже выполнено!", get_challenge_with_help_keyboard())
            return
        mark_task_completed(challenge["id"], challenge["current_day"])
        if challenge["current_day"] >= 14:
            complete_challenge(challenge["id"])
            await send_callback_answer(callback_id, "🎉 Поздравляю! Ты прошёл челлендж!", get_after_plan_keyboard())
        else:
            new_day = challenge["current_day"] + 1
            advance_challenge_day(challenge["id"], new_day)
            task_text = await generate_challenge_task(chat_id, new_day, report["text"])
            save_challenge_task(challenge["id"], new_day, task_text)
            await send_callback_answer(callback_id,
                f"✅ Задание дня {challenge['current_day']} выполнено!\n\nЗадание дня {new_day}:\n{task_text}",
                get_challenge_with_help_keyboard())
        return
    if callback_data == CALLBACK_CHALLENGE_PROGRESS:
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id, "❌ Нет активного челленджа.", get_challenge_with_help_keyboard())
            return
        await send_callback_answer(callback_id,
            f"🏆 Прогресс: день {challenge['current_day']} из 14, выполнено {challenge['tasks_completed']}",
            get_challenge_with_help_keyboard())
        return
    if callback_data == CALLBACK_IMPLEMENTATION:
        save_user_state(chat_id, STATE_AWAITING_IMPLEMENTATION, {})
        await send_callback_answer(callback_id, "🔥 Расскажи, что нужно внедрить, и я передам продюсеру.", None)
        return
    # Обработка опросника
    if callback_data in [Q1_SERVICE, Q1_INFO, Q1_CONSULT, Q1_NONE, Q2_LT5, Q2_5_20, Q2_20_50, Q2_50P,
                         Q3_LT10, Q3_10_50, Q3_50_200, Q3_200P, Q4_300, Q4_500, Q4_1M, Q4_SCALE,
                         Q5_YES, Q5_NO, Q5_PROGRESS]:
        _, user_data = get_user_state(chat_id)
        if user_data is None:
            user_data = {}
        user_data.setdefault("answers", {})
        user_data.setdefault("survey_step", 0)
        step = user_data["survey_step"]
        if step < len(SURVEY_QUESTIONS):
            key = SURVEY_QUESTIONS[step]["key"]
            user_data["answers"][key] = callback_data
            user_data["survey_step"] = step + 1
            save_user_state(chat_id, STATE_SURVEY, user_data)
            if step + 1 < len(SURVEY_QUESTIONS):
                await send_callback_answer(callback_id, SURVEY_QUESTIONS[step + 1]["text"], get_survey_keyboard(step + 1))
            else:
                save_form(chat_id, user_data["answers"])
                biz = get_business_data(chat_id)
                if not biz:
                    await send_callback_answer(callback_id, "❌ Ошибка, начни заново.", get_main_menu_keyboard())
                    return
                existing = get_report(chat_id, "premium")
                if existing and existing["status"] == "ready":
                    report_text = existing["text"]
                elif existing and existing["status"] == "generating":
                    await send_callback_answer(callback_id, "⏳ План уже генерируется, подождите...", None)
                    return
                else:
                    save_report(chat_id, "premium", "")
                    await send_callback_answer(callback_id, "🔍 Запускаю анализ...", None)
                    await send_animation(chat_id)
                    report_text = await call_deepseek_marketing_plan(biz["name"], biz["description"], user_data["answers"], chat_id)
                    if not report_text:
                        await send_message(chat_id, "❌ Не удалось сгенерировать план.", get_main_menu_keyboard())
                        update_report_status(chat_id, "failed")
                        return
                    save_report(chat_id, "premium", report_text)
                final_text = report_text + "\n\n📜 *Листай вверх к началу плана*"
                await send_long_message(chat_id, "✅ ТВОЙ МАРКЕТИНГОВЫЙ ПЛАН ГОТОВ!\n\n" + final_text, None)
                await asyncio.sleep(2)
                await send_message(chat_id, "🎯 Что хочешь сделать дальше?", get_after_plan_keyboard())
        return

# === СОЗДАНИЕ ПРИЛОЖЕНИЯ FASTAPI (ПОСЛЕ ВСЕХ ФУНКЦИЙ) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

# Админ-эндпоинт
security = HTTPBasic()
def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Admin not configured")
    if not (secrets.compare_digest(credentials.username, ADMIN_USERNAME) and
            secrets.compare_digest(credentials.password, ADMIN_PASSWORD)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

@app.get("/admin/queries")
async def admin_queries(limit: int = 100, auth: bool = Depends(verify_admin)):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, user_id, query_type, substr(prompt, 1, 200) as prompt_preview, created_at
        FROM deepseek_queries ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return {"queries": [{"id": r[0], "user_id": r[1], "type": r[2], "prompt_preview": r[3], "created_at": r[4]} for r in rows]}

@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "9.8"}

@app.get("/health")
async def health():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        if "message" in payload and "callback" not in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            text = msg.get("body", {}).get("text")
            if user_id and text:
                await process_message(str(user_id), text.strip())
        elif "callback" in payload:
            cb = payload["callback"]
            user_id = cb.get("user", {}).get("user_id")
            callback_id = cb.get("callback_id")
            data = cb.get("payload")
            if user_id and data:
                await process_callback(str(user_id), str(callback_id), data)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
