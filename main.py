# File: main.py — бот Salesplan для MAX (бесплатная диагностика + челлендж)

import asyncio
import logging
import sqlite3
import os
import json
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
import aiohttp
import requests
import uvicorn

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
SITE_API_URL = os.getenv("SITE_API_URL", "https://realplanninig-oss-salesplan-web-7eb2.twc1.net")
REVIEWS_URL = os.getenv("REVIEWS_URL", "https://vk.ru/topic-164421538_39653658")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://max.ru/u/f9LHodD0cOL1ttBGofp6mcEX6K6JaHd_qndKbBG0prUpl4foZEiL-tzu8go")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

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

# === БАЗА ДАННЫХ ДЛЯ СОСТОЯНИЙ ===
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

# === ЧЕЛЛЕНДЖ ===
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
async def call_deepseek_diagnostic(name: str, description: str, answers: dict) -> str:
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
    prompt = f"""Сделай профессиональный маркетинговый разбор онлайн-бизнеса.

ДАННЫЕ О БИЗНЕСЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши отчет в деловом, практичном стиле:
- Уверенный, прямой, без воды
- Используй конкретные примеры
- Обращайся на "ты"
- НЕ используй символы форматирования
- Для списков используй просто дефис (-)

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
    if not DEEPSEEK_API_KEY:
        return f"💪 ЗАДАНИЕ ДЕНЬ {day}\n\nИзучи свой маркетинговый план и найди 1 пункт, который можно сделать сегодня, чтобы привлечь первых клиентов.\n\n📝 ЧЕК-ЛИСТ:\n- Открой план\n- Выбери один пункт\n- Сделай его\n\n🎯 ЗАЧЕМ ЭТО: Маленькие шаги ведут к большим результатам."
    
    prompt = f"""Ты — профессиональный бизнес-наставник. Цель — помочь пользователю получить первых клиентов за 2 недели.

Вот маркетинговый план пользователя:
{report_text[:3000]}

День {day} из 14.

Придумай конкретное, выполнимое задание, которое приблизит пользователя к первой продаже.

Требования:
- Задание должно быть конкретным и измеримым
- Должно занимать не более 2 часов
- Фокус на привлечение первых клиентов

Формат ответа (без лишних слов):

💪 ЗАДАНИЕ ДЕНЬ {day}

[Опиши действие одним-двумя предложениями]

📝 ЧЕК-ЛИСТ ДНЯ:
- [ ] Шаг 1
- [ ] Шаг 2
- [ ] Шаг 3

🎯 ПОЧЕМУ ЭТО ВАЖНО:
[1 предложение о том, как это поможет получить клиента]

👇 ВНИЗУ КНОПКА «Помощь продюсера» — нажми, если нужна поддержка."""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный бизнес-наставник. Давай чёткие, выполнимые задания. Цель: первые клиенты за 14 дней."},
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
            return f"💪 ЗАДАНИЕ ДЕНЬ {day}\n\nНапиши 3 идеи для привлечения первых клиентов и выбери одну для внедрения.\n\n📝 ЧЕК-ЛИСТ ДНЯ:\n- Запиши 3 идеи\n- Выбери лучшую\n- Составь план действий\n\n🎯 ПОЧЕМУ ЭТО ВАЖНО: Первые клиенты — это деньги и уверенность."
    except Exception as e:
        logger.error(f"Generate task error: {e}")
        return f"💪 ЗАДАНИЕ ДЕНЬ {day}\n\nПрочитай свой маркетинговый план и найди 1 пункт, который можно сделать сегодня для привлечения клиентов.\n\n📝 ЧЕК-ЛИСТ ДНЯ:\n- Открой план\n- Выбери один пункт\n- Сделай его\n\n🎯 ПОЧЕМУ ЭТО ВАЖНО: Действие сегодня = клиент завтра."

# === КЛАВИАТУРЫ ===
def get_main_menu_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "📊 Пройти диагностику",
                "payload": CALLBACK_AUDIT,
                "intent": "default"
            }
        ],
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
                "text": "🏆 Челлендж 14 дней",
                "payload": CALLBACK_CHALLENGE_TASK,
                "intent": "default"
            }
        ]
    ]

def get_after_diagnostic_keyboard():
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
                "text": "🏆 Начать челлендж 14 дней",
                "payload": CALLBACK_CHALLENGE_TASK,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🔄 Пройти диагностику заново",
                "payload": CALLBACK_AUDIT,
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

def get_challenge_with_help_keyboard():
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
        ],
        [
            {
                "type": "callback",
                "text": "🎓 Помощь продюсера",
                "payload": CALLBACK_HELP,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏠 Главное меню",
                "payload": CALLBACK_MENU,
                "intent": "default"
            }
        ]
    ]

def get_ai_keyboard():
    return [
        [
            {
                "type": "callback",
                "text": "🏆 Челлендж 14 дней",
                "payload": CALLBACK_CHALLENGE_TASK,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏠 Главное меню",
                "payload": CALLBACK_MENU,
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
                "payload": CALLBACK_IMPLEMENTATION,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏠 Главное меню",
                "payload": CALLBACK_MENU,
                "intent": "default"
            }
        ]
    ]

def get_help_keyboard():
    return [
        [
            {
                "type": "link",
                "text": "📸 Оставить отзыв",
                "url": REVIEWS_URL
            }
        ],
        [
            {
                "type": "callback",
                "text": "🏠 Главное меню",
                "payload": CALLBACK_MENU,
                "intent": "default"
            }
        ]
    ]

async def send_animation(user_id: str):
    steps = [
        "🔍 Анализируем бизнес...\n\n⏳ 1/4",
        "📊 Изучаем целевую аудиторию...\n\n⏳ 2/4",
        "🎯 Ищем точки роста...\n\n⏳ 3/4",
        "📝 Формируем рекомендации...\n\n⏳ 4/4"
    ]
    for step in steps:
        await send_message(user_id, step, None)
        await asyncio.sleep(3)

# === ОТПРАВКА СООБЩЕНИЙ ===
async def send_message(chat_id: str, text: str, keyboard: list = None):
    url = f"https://platform-api.max.ru/messages?user_id={chat_id}"
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
    url = f"https://platform-api.max.ru/answers?callback_id={callback_id}"
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
    if not ADMIN_CHANNEL_ID or ADMIN_CHANNEL_ID == "None":
        logger.warning(f"ADMIN_CHANNEL_ID not configured, skipping notification")
        return
    
    url = f"https://platform-api.max.ru/messages?channel_id={ADMIN_CHANNEL_ID}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_notification_to_channel failed: {resp.status} - {error_text}")
            return await resp.json()

# === ОБРАБОТЧИКИ ===
async def process_callback(chat_id: str, callback_id: str, callback_data: str):
    state, data = get_user_state(chat_id)
    log_event(chat_id, f"callback_{callback_data}")

    if callback_data == CALLBACK_MENU:
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id,
            "🏠 Главное меню\n\nЧто хочешь сделать?",
            get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_AUDIT:
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {})
        await send_callback_answer(callback_id,
            "🚀 Отлично! Давай разберём твой бизнес.\n\n"
            "Напиши название своего проекта (как ты представляешь его клиентам):",
            None)
        return

    if callback_data == CALLBACK_ASK_AI:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id,
                "💬 Ты ещё не прошёл диагностику.\n\n"
                "Сначала пройди бесплатный аудит — 2 минуты, и я подготовлю твой персональный маркетинговый план.\n\n"
                "👇 Начни сейчас",
                [[{"type": "callback", "text": "📊 Пройти диагностику", "payload": CALLBACK_AUDIT, "intent": "default"}]])
            return
        
        save_user_state(chat_id, STATE_AI_CHAT, {})
        await send_callback_answer(callback_id,
            "💬 Отлично! Теперь ты можешь задавать вопросы по своему маркетинговому плану.\n\n"
            "Что тебя интересует? Я на связи 24/7.\n\n"
            "⚠️ Если спросишь про внедрение — я направлю к продюсеру.",
            None)
        return

    if callback_data == CALLBACK_CHALLENGE_TASK:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id,
                "🏆 Челлендж доступен после прохождения диагностики.\n\n"
                "Сначала пройди бесплатный аудит, получи маркетинговый план, а потом начнём 14-дневный марафон к первым клиентам!\n\n"
                "👇 Начни диагностику",
                [[{"type": "callback", "text": "📊 Пройти диагностику", "payload": CALLBACK_AUDIT, "intent": "default"}]])
            return
        
        report_text = report["text"]
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            challenge_id = start_new_challenge(chat_id)
            task_text = await generate_challenge_task(chat_id, 1, report_text)
            save_challenge_task(challenge_id, 1, task_text)
            await send_callback_answer(callback_id,
                f"🏆 ПОЕХАЛИ! Челлендж «Первые клиенты за 14 дней» начался!\n\n{task_text}\n\n"
                f"👇 Когда сделаешь — нажми «Выполнил задание»",
                get_challenge_with_help_keyboard())
        else:
            current_task = get_current_task(challenge["id"], challenge["current_day"])
            if current_task and not current_task["is_completed"]:
                await send_callback_answer(callback_id,
                    f"📋 ЗАДАНИЕ НА ДЕНЬ {challenge['current_day']}\n\n{current_task['task_text']}\n\n"
                    f"👇 Когда сделаешь — нажми «Выполнил задание»",
                    get_challenge_with_help_keyboard())
            else:
                remaining = 14 - challenge["current_day"]
                await send_callback_answer(callback_id,
                    f"🏆 Твой прогресс: день {challenge['current_day']} из 14, выполнено {challenge['tasks_completed']} заданий.\n\n"
                    f"🎯 Осталось дней: {remaining}\n\n"
                    f"👇 Продолжай выполнять задания — каждый шаг приближает тебя к первым клиентам!",
                    get_challenge_with_help_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_DONE:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id,
                "🏆 Челлендж доступен после прохождения диагностики.",
                [[{"type": "callback", "text": "📊 Пройти диагностику", "payload": CALLBACK_AUDIT, "intent": "default"}]])
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Челлендж 14 дней»",
                get_challenge_with_help_keyboard())
            return
        
        current_task = get_current_task(challenge["id"], challenge["current_day"])
        if not current_task or current_task["is_completed"]:
            await send_callback_answer(callback_id,
                "✅ Задание на сегодня уже выполнено! Завтра получишь новое.",
                get_challenge_with_help_keyboard())
            return
        
        mark_task_completed(challenge["id"], challenge["current_day"])
        report_text = report["text"]
        
        if challenge["current_day"] >= 14:
            complete_challenge(challenge["id"])
            await send_callback_answer(callback_id,
                f"🎉 ПОЗДРАВЛЯЮ! Ты прошёл 14-дневный челлендж!\n\n"
                f"✅ Выполнено заданий: {challenge['tasks_completed'] + 1} из 14\n\n"
                f"🔥 Теперь у тебя есть всё, чтобы получать клиентов регулярно!\n\n"
                f"👇 Продолжай задавать вопросы AI и внедряй план",
                get_after_diagnostic_keyboard())
        else:
            new_day = challenge["current_day"] + 1
            advance_challenge_day(challenge["id"], new_day)
            
            task_text = await generate_challenge_task(chat_id, new_day, report_text)
            save_challenge_task(challenge["id"], new_day, task_text)
            
            await send_callback_answer(callback_id,
                f"✅ Отлично! Задание дня {challenge['current_day']} выполнено!\n\n"
                f"🏆 Прогресс: {challenge['tasks_completed'] + 1} заданий сделано\n\n"
                f"💪 ЗАДАНИЕ ДЕНЬ {new_day}\n\n{task_text}\n\n"
                f"👇 Продолжай в том же духе!",
                get_challenge_with_help_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_PROGRESS:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id,
                "🏆 Челлендж доступен после прохождения диагностики.",
                [[{"type": "callback", "text": "📊 Пройти диагностику", "payload": CALLBACK_AUDIT, "intent": "default"}]])
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Челлендж 14 дней»",
                get_challenge_with_help_keyboard())
            return
        
        await send_callback_answer(callback_id,
            f"🏆 ТВОЙ ПРОГРЕСС В ЧЕЛЛЕНДЖЕ «ПЕРВЫЕ КЛИЕНТЫ ЗА 14 ДНЕЙ»\n\n"
            f"📅 День {challenge['current_day']} из 14\n"
            f"✅ Выполнено заданий: {challenge['tasks_completed']}\n"
            f"🎯 Осталось дней: {14 - challenge['current_day']}\n\n"
            f"Продолжай выполнять задания — каждый шаг приближает тебя к первым клиентам! 💪",
            get_challenge_with_help_keyboard())
        return

    if callback_data == CALLBACK_IMPLEMENTATION:
        save_user_state(chat_id, STATE_AWAITING_IMPLEMENTATION, {})
        await send_callback_answer(callback_id,
            "🔥 ВНЕДРЕНИЕ ПОД КЛЮЧ\n\n"
            "Расскажи подробнее о своём бизнесе и что нужно внедрить.\n\n"
            "Я передам информацию продюсеру, и он свяжется с тобой.\n\n"
            "👇 Напиши свой запрос одним сообщением",
            None)
        return

    if callback_data == CALLBACK_HELP:
        await send_callback_answer(callback_id,
            "🎓 ПОМОЩЬ ПРОДЮСЕРА\n\n"
            "Ты получил маркетинговый план? Сколько уже заработал?\n\n"
            "Я предлагаю тебе БЕСПЛАТНУЮ консультацию в обмен на честный отзыв.\n\n"
            "👇 Оставь отзыв, и я свяжусь с тобой",
            get_help_keyboard())
        return

    # Обработка ответов на опросник
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
                biz_data = get_business_data(chat_id)
                if not biz_data:
                    await send_callback_answer(callback_id,
                        "❌ Что-то пошло не так. Начни заново.",
                        get_main_menu_keyboard())
                    return

                await send_callback_answer(callback_id, "🔍 Запускаю анализ...", None)
                await send_animation(chat_id)
                
                report_text = await call_deepseek_diagnostic(
                    biz_data["name"], biz_data["description"], user_data["answers"])
                
                if report_text:
                    save_report(chat_id, "premium", report_text)
                    
                    await send_message(chat_id, "✅ ТВОЙ МАРКЕТИНГОВЫЙ ПЛАН ГОТОВ!\n\n" + report_text, None)
                    
                    await asyncio.sleep(2)
                    
                    await send_message(chat_id,
                        "🎯 Ты прошёл первый шаг и понял, что нужно изменить.\n\n"
                        "Сейчас у тебя есть шанс внедрить новую стратегию:\n"
                        "- Задавай любые вопросы по плану AI\n"
                        "- Начни 14-дневный челлендж «Первые клиенты за 2 недели»\n\n"
                        "👇 Что хочешь сделать?",
                        get_after_diagnostic_keyboard())
                else:
                    await send_message(chat_id,
                        "❌ Что-то пошло не так. Попробуй позже.",
                        get_main_menu_keyboard())
        return

async def process_message(user_id: str, text: str):
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")

    if state == STATE_MENU:
        await send_message(str(user_id),
            "👋 Привет! Я Вероника, продюсер экспертов.\n\n"
            "Что я умею:\n"
            "✅ Бесплатный аудит бизнеса за 2 минуты\n"
            "✅ Персональный маркетинговый план\n"
            "✅ AI-чат — отвечаю на вопросы по плану 24/7\n"
            "✅ Челлендж «Первые клиенты за 14 дней»\n\n"
            "👇 Начни с диагностики",
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
        save_user_state(str(user_id), STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(str(user_id), SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0))
        return

    if state == STATE_AI_CHAT:
        report = get_report(str(user_id), "premium")
        if not report or report["status"] != "ready":
            await send_message(str(user_id),
                "💬 Ты ещё не прошёл диагностику.\n\n"
                "Сначала пройди бесплатный аудит и получи маркетинговый план.\n\n"
                "👇 Начни сейчас",
                [[{"type": "callback", "text": "📊 Пройти диагностику", "payload": CALLBACK_AUDIT, "intent": "default"}]])
            save_user_state(str(user_id), STATE_MENU, {})
            return
        
        save_chat_message(str(user_id), "user", text)
        
        report_text = report["text"]
        
        hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши скрипт"]
        is_hard = any(keyword in text.lower() for keyword in hard_keywords)
        
        if is_hard:
            answer = "🔥 Это задача для профессионального внедрения.\n\nЕсли хочешь сделать это правильно и без ошибок — оставь заявку. Я свяжусь с тобой и помогу внедрить.\n\n👇 Нажми кнопку"
            await send_message(str(user_id), answer, get_implementation_keyboard())
        else:
            await send_message(str(user_id), "🤔 Думаю...", None)
            history = get_chat_history(str(user_id), 10)
            answer = await call_deepseek_chat(text, str(user_id), report_text, history)
            await send_message(str(user_id), answer, get_ai_keyboard())
        
        save_chat_message(str(user_id), "assistant", answer)
        return

    if state == STATE_AWAITING_IMPLEMENTATION:
        await send_notification_to_channel(
            f"📞 ЗАЯВКА НА ВНЕДРЕНИЕ\n\n"
            f"Пользователь: {user_id}\n"
            f"Запрос: {text}\n"
            f"⏰ {format_moscow_time()}"
        )
        await send_message(str(user_id),
            "✅ Заявка принята! Я свяжусь с тобой в ближайшее время.\n\n"
            "👇 Вернуться в меню",
            get_main_menu_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

# === СОЗДАНИЕ ПРИЛОЖЕНИЯ FASTAPI ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started (free diagnostic + challenge)")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "8.0", "mode": "free_diagnostic"}

@app.get("/health")
async def health():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"Webhook received")

        if "message" in payload and "callback" not in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            body = msg.get("body", {})
            text = body.get("text")
            if user_id and text and text.strip():
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
