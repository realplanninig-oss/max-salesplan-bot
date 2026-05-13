# File: main.py — бот Salesplan (версия 10.15: добавлено логирование callback и исправлена подписка)

import asyncio
import logging
import sqlite3
import os
import json
import traceback
import re
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import aiohttp
import requests
import uvicorn

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
HELP_URL = os.getenv("HELP_URL", "https://max.ru/u/f9LHodD0cOJp3NEa7OYZr1MKfUuC1hYDyKh2f4HFkfTXT88W3txWaBaFQmU")
CONSULT_LINK = "https://max.ru/u/f9LHodD0cOJmqGaOJJxBthmX1NCjnOXHlsnYzYTc83uuDLwN4j08I-fmU4U"

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
STATE_AWAITING_FEEDBACK_REASON = "awaiting_feedback_reason"

CALLBACK_AUDIT = "audit"
CALLBACK_ASK_AI = "ask_ai"
CALLBACK_CHALLENGE_TASK = "challenge_task"
CALLBACK_CHALLENGE_DONE = "challenge_done"
CALLBACK_CHALLENGE_PROGRESS = "challenge_progress"
CALLBACK_IMPLEMENTATION = "implementation"
CALLBACK_MENU = "menu"
CALLBACK_RESET = "reset"
CALLBACK_FEEDBACK_YES = "feedback_yes"
CALLBACK_FEEDBACK_NO = "feedback_no"
CALLBACK_START_SURVEY = "start_survey"
CALLBACK_BOOK_CONSULT = "book_consult"

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
    {"key": "q1", "text": "Что вы продаёте?", "options": [(Q1_SERVICE, "Услугу"), (Q1_INFO, "Инфопродукт"), (Q1_CONSULT, "Консультацию"), (Q1_NONE, "Пока не продаю")]},
    {"key": "q2", "text": "Средний чек (₽)", "options": [(Q2_LT5, "до 5 000 ₽"), (Q2_5_20, "5 000 - 20 000 ₽"), (Q2_20_50, "20 000 - 50 000 ₽"), (Q2_50P, "более 50 000 ₽")]},
    {"key": "q3", "text": "Клиентов в месяц", "options": [(Q3_LT10, "менее 10"), (Q3_10_50, "10-50"), (Q3_50_200, "50-200"), (Q3_200P, "более 200")]},
    {"key": "q4", "text": "Цель на 2026", "options": [(Q4_300, "300 000 ₽/мес"), (Q4_500, "500 000 ₽/мес"), (Q4_1M, "1 000 000 ₽/мес"), (Q4_SCALE, "Масштабирование")]},
    {"key": "q5", "text": "Уже есть автоворонка?", "options": [(Q5_YES, "Да"), (Q5_NO, "Нет"), (Q5_PROGRESS, "В разработке")]},
]

def init_bot_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS user_state (user_id TEXT PRIMARY KEY, state TEXT, data TEXT, updated_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS business_data (user_id TEXT PRIMARY KEY, business_name TEXT, business_description TEXT, created_at TIMESTAMP, reminder_sent_24h BOOLEAN DEFAULT 0, reminder_sent_7d BOOLEAN DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS forms (user_id TEXT PRIMARY KEY, q1 TEXT, q2 TEXT, q3 TEXT, q4 TEXT, q5 TEXT, completed_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, report_type TEXT NOT NULL, report_text TEXT, status TEXT DEFAULT 'generating', created_at TIMESTAMP, ready_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, start_date TIMESTAMP, current_day INTEGER, tasks_completed INTEGER, status TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS challenge_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id INTEGER NOT NULL, day_number INTEGER NOT NULL, task_text TEXT NOT NULL, is_completed BOOLEAN, completed_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, role TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS deepseek_queries (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, query_type TEXT NOT NULL, prompt TEXT, created_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, rating INTEGER, reason TEXT, created_at TIMESTAMP)")
    conn.commit()
    conn.close()

init_bot_db()

def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def format_moscow_time(dt=None):
    if dt is None: dt = get_moscow_time()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def log_event(user_id: str, event_type: str, event_data: str = None):
    logger.info(f"Event: {event_type} | User: {user_id} | Data: {event_data}")

def log_deepseek_query(user_id: str, query_type: str, prompt: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO deepseek_queries (user_id, query_type, prompt) VALUES (?, ?, ?)", (user_id, query_type, prompt))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log: {e}")

def get_user_state(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT state, data FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return row[0], json.loads(row[1]) if row[1] else {}
    return STATE_MENU, {}

def save_user_state(user_id: str, state: str, data: dict = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO user_state (user_id, state, data, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                 (user_id, state, json.dumps(data or {}, ensure_ascii=False)))
    conn.commit()
    conn.close()

def save_business_data(user_id: str, name: str, description: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO business_data (user_id, business_name, business_description, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                 (user_id, name, description))
    conn.commit()
    conn.close()

def get_business_data(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT business_name, business_description, reminder_sent_24h, reminder_sent_7d FROM business_data WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return {"name": row[0], "description": row[1], "reminder_sent_24h": bool(row[2]), "reminder_sent_7d": bool(row[3])}
    return None

def update_reminder_flags(user_id: str, reminder_24h: bool = None, reminder_7d: bool = None):
    conn = sqlite3.connect(DB_PATH)
    if reminder_24h is not None:
        conn.execute("UPDATE business_data SET reminder_sent_24h = ? WHERE user_id = ?", (reminder_24h, user_id))
    if reminder_7d is not None:
        conn.execute("UPDATE business_data SET reminder_sent_7d = ? WHERE user_id = ?", (reminder_7d, user_id))
    conn.commit()
    conn.close()

def save_form(user_id: str, answers: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO forms (user_id, q1, q2, q3, q4, q5, completed_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                 (user_id, answers["q1"], answers["q2"], answers["q3"], answers["q4"], answers["q5"]))
    conn.commit()
    conn.close()

def save_report(user_id: str, report_type: str, report_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO reports (user_id, report_type, report_text, status, ready_at) VALUES (?, ?, ?, 'ready', CURRENT_TIMESTAMP)",
                 (user_id, report_type, report_text))
    conn.commit()
    conn.close()

def update_report_status(user_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE reports SET status = ? WHERE user_id = ? AND report_type = 'premium'", (status, user_id))
    conn.commit()
    conn.close()

def get_report(user_id: str, report_type: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT report_text, status FROM reports WHERE user_id = ? AND report_type = ? ORDER BY created_at DESC LIMIT 1", (user_id, report_type)).fetchone()
    conn.close()
    return {"text": row[0], "status": row[1]} if row else None

def save_feedback(user_id: str, rating: int, reason: str = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO feedback (user_id, rating, reason) VALUES (?, ?, ?)", (user_id, rating, reason))
    conn.commit()
    conn.close()

def save_chat_message(user_id: str, role: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)", (user_id, role, message))
    conn.commit()
    conn.close()

def get_chat_history(user_id: str, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT role, message FROM chat_history WHERE user_id = ? ORDER BY created_at ASC LIMIT ?", (user_id, limit)).fetchall()
    conn.close()
    return [{"role": r[0], "message": r[1]} for r in rows]

def get_active_challenge(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, current_day, tasks_completed FROM challenges WHERE user_id = ? AND status = 'active' ORDER BY start_date DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()
    return {"id": row[0], "current_day": row[1], "tasks_completed": row[2]} if row else None

def start_new_challenge(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO challenges (user_id, start_date, current_day, tasks_completed, status) VALUES (?, CURRENT_TIMESTAMP, 1, 0, 'active')", (user_id,))
    conn.commit()
    return cur.lastrowid

def save_challenge_task(challenge_id: int, day: int, task_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO challenge_tasks (challenge_id, day_number, task_text) VALUES (?, ?, ?)", (challenge_id, day, task_text))
    conn.commit()
    conn.close()

def get_current_task(challenge_id: int, day: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, task_text, is_completed FROM challenge_tasks WHERE challenge_id = ? AND day_number = ?", (challenge_id, day)).fetchone()
    conn.close()
    return {"id": row[0], "task_text": row[1], "is_completed": bool(row[2])} if row else None

def mark_task_completed(challenge_id: int, day: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE challenge_tasks SET is_completed = 1, completed_at = CURRENT_TIMESTAMP WHERE challenge_id = ? AND day_number = ?", (challenge_id, day))
    conn.execute("UPDATE challenges SET tasks_completed = tasks_completed + 1 WHERE id = ?", (challenge_id,))
    conn.commit()
    conn.close()

def advance_challenge_day(challenge_id: int, new_day: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE challenges SET current_day = ? WHERE id = ?", (new_day, challenge_id))
    conn.commit()
    conn.close()

def complete_challenge(challenge_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE challenges SET status = 'completed' WHERE id = ?", (challenge_id,))
    conn.commit()
    conn.close()

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

async def send_animation(user_id: str):
    steps = ["🔍 Готовим ваш маркетинговый план...\n\n⏳ 1/4", "📊 Раскладываем точки роста...\n\n⏳ 2/4", "🎯 Настраиваем прицел на первые деньги...\n\n⏳ 3/4", "📝 Формируем дорожную карту...\n\n⏳ 4/4"]
    for step in steps:
        await send_message(user_id, step, None)
        await asyncio.sleep(2)

async def call_deepseek_marketing_plan(name: str, description: str, answers: dict, user_id: str = None) -> str:
    if not DEEPSEEK_API_KEY:
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
• Цель: {q4_map.get(answers.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(answers.get('q5'), 'не указано')}
"""
    prompt = f"""Сделай профессиональный маркетинговый план для онлайн-бизнеса.

Название: {name}
Описание: {description}
{survey_info}

ВАЖНО: НЕ используй Instagram, Telegram, WhatsApp. Только VK, Яндекс.Директ, автоворонка в MAX.
Требования: только конкретные шаги, без общих фраз. Приведи 1-2 примера. Не используй форматирование.
Структура: 1. РЕАЛЬНОСТЬ 2. КОНКУРЕНТЫ 3. ТВОЙ КЛИЕНТ 4. СИЛЬНЫЕ И СЛАБЫЕ СТОРОНЫ 5. ВОРОНКА 6. ПЛАН НА МЕСЯЦ"""
    if user_id:
        log_deepseek_query(user_id, "marketing_plan", prompt)
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": [{"role": "system", "content": "Ты — бизнес-консультант."}, {"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": 4000},
            timeout=120)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        return None
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return None

async def call_deepseek_chat(question: str, user_id: str, report_text: str, history: list) -> str:
    history_text = "\n".join([f"{m['role']}: {m['message']}" for m in history[-5:]])
    prompt = f"""Вот план: {report_text[:3000]} \nИстория: {history_text}\nВопрос: {question}\nОтветь по делу, без воды. Если просит настройку — скажи оставить заявку.
Ограничения: без Instagram/Telegram, только VK, Яндекс.Директ, MAX."""
    log_deepseek_query(user_id, "chat_question", prompt)
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": [{"role": "system", "content": "Ты — консультант."}, {"role": "user", "content": prompt}], "temperature": 0.5, "max_tokens": 1000},
            timeout=60)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        return "Ошибка, попробуй позже."
    except Exception as e:
        return f"Ошибка: {e}"

async def generate_challenge_task(user_id: str, day: int, report_text: str) -> str:
    if not DEEPSEEK_API_KEY:
        return fallback_task(day)
    prompt = f"""Дай ОДНО конкретное действие (не список) на день {day} из 14, чтобы получить первых клиентов.
Ниже план пользователя: {report_text[:3000]}
Ограничения: только VK, MAX, Яндекс.Директ. Без Instagram/Telegram.
Формат:
ЗАДАНИЕ ДЕНЬ {day}
[одно действие]
КАК СДЕЛАТЬ:
[2-3 шага]
ПОЧЕМУ ЭТО ВАЖНО:
[одно предложение]"""
    log_deepseek_query(user_id, "challenge_task", prompt)
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": [{"role": "system", "content": "Ты — наставник. Только одно действие, без списков."}, {"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 600},
            timeout=60)
        if resp.status_code == 200:
            task = resp.json()["choices"][0]["message"]["content"]
            if len(task) < 50:
                return fallback_task(day)
            return task
        return fallback_task(day)
    except Exception:
        return fallback_task(day)

def fallback_task(day: int) -> str:
    return f"""ЗАДАНИЕ ДЕНЬ {day}
Создай пост в VK о проблеме клиента и предложи решение.

КАК СДЕЛАТЬ:
1. Открой VK, напиши пост на 300-500 символов.
2. В конце добавь: «Напиши "разбор" в комментариях — сделаю бесплатный разбор».
3. Опубликуй и ответь трём первым комментаторам.

ПОЧЕМУ ЭТО ВАЖНО:
Ты увидишь реальный спрос и начнёшь получать заявки."""

def get_main_menu_keyboard():
    return [
        [{"type": "callback", "text": "📊 Получить маркетинговый план", "payload": CALLBACK_AUDIT}],
        [{"type": "callback", "text": "💬 Задать вопрос AI (круглосуточно)", "payload": CALLBACK_ASK_AI}],
        [{"type": "callback", "text": "🏆 Челлендж «Первые деньги за 14 дней»", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "🎯 Записаться на консультацию", "payload": CALLBACK_BOOK_CONSULT}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}]
    ]

def get_after_plan_keyboard():
    return [
        [{"type": "callback", "text": "💬 Задать вопрос AI", "payload": CALLBACK_ASK_AI}],
        [{"type": "callback", "text": "🏆 Старт челленджа", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "🎯 Консультация", "payload": CALLBACK_BOOK_CONSULT}],
        [{"type": "callback", "text": "🔄 Пройти анкету заново", "payload": CALLBACK_AUDIT}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}]
    ]

def get_survey_keyboard(step: int):
    if step >= len(SURVEY_QUESTIONS):
        return None
    q = SURVEY_QUESTIONS[step]
    kb = [[{"type": "callback", "text": label, "payload": p}] for p, label in q["options"]]
    kb.append([{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}])
    return kb

def get_challenge_keyboard():
    return [
        [{"type": "callback", "text": "📋 Задание на сегодня", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "✅ Выполнил задание", "payload": CALLBACK_CHALLENGE_DONE}],
        [{"type": "callback", "text": "📊 Мой прогресс", "payload": CALLBACK_CHALLENGE_PROGRESS}],
        [{"type": "callback", "text": "🎯 Записаться к продюсеру", "payload": CALLBACK_BOOK_CONSULT}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Главное меню", "payload": CALLBACK_MENU}]
    ]

def get_ai_keyboard():
    return [
        [{"type": "callback", "text": "🏆 Челлендж", "payload": CALLBACK_CHALLENGE_TASK}],
        [{"type": "callback", "text": "🎯 Консультация", "payload": CALLBACK_BOOK_CONSULT}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Меню", "payload": CALLBACK_MENU}]
    ]

def get_implementation_keyboard():
    return [
        [{"type": "callback", "text": "🎯 Записаться", "payload": CALLBACK_BOOK_CONSULT}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}],
        [{"type": "callback", "text": "🏠 Меню", "payload": CALLBACK_MENU}]
    ]

def get_feedback_keyboard():
    return [
        [{"type": "callback", "text": "👍 Полезно", "payload": CALLBACK_FEEDBACK_YES}],
        [{"type": "callback", "text": "👎 Не помогло", "payload": CALLBACK_FEEDBACK_NO}]
    ]

def get_start_keyboard():
    return [
        [{"type": "callback", "text": "✅ Да, хочу маркетинговый план за 2 минуты", "payload": CALLBACK_START_SURVEY}],
        [{"type": "link", "text": "🎯 Записаться на консультацию", "url": CONSULT_LINK}],
        [{"type": "link", "text": "🆘 Помощь", "url": HELP_URL}]
    ]

CONSULTATION_TEXT = """🎯 Консультация с Вероникой Макаревич

Что вы получите за 30 минут:
- Чёткий план первой продажи
- Ответ, на каком этапе воронки теряете деньги
- Честный разбор ошибок

Как записаться:
1. Перейдите по ссылке ниже
2. Напишите в личные сообщения:
   - Удобное время для звонка
   - Ваш запрос / проблему

👇 Нажмите на кнопку, чтобы перейти в диалог"""

WELCOME_TEXT = """🔥 Привет, предприниматель! Я Вероника Макаревич — продюсер, который знает, как превратить хаос в прибыль.

Многие эксперты тонут в бесконечных задачах: контент, воронка, реклама, клиенты… А денег нет. 
Знакомо? Тогда ты по адресу.

⚡️ Что я тебе даю:

📊 Маркетинговый план — не теория, а конкретная дорожная карта «бери и делай». За 5 вопросов AI разложит твой бизнес по полочкам и покажет, где ты теряешь деньги.

💬 AI-чат 24/7 — задавай любые вопросы по плану. Без вот этих «подожди, я отвечу завтра».

🏆 Челлендж 14 дней — получай одно чёткое задание в день, шаг за шагом иди к первым деньгам.

🎯 Консультация со мной — разберём твой случай, найду узкое место и скажу, как его пробить.

Зачем тебе маркетинговый план? 
Большинство экспертов продают впустую: постят, снимают рилс, но клиент не идёт. Потому что нет системы. План — это компас. Он показывает, где прячутся твои деньги.

Поехали? 👇"""

# === ОБРАБОТЧИКИ ===
async def process_message(user_id: str, text: str):
    state, data = get_user_state(user_id)

    if not text or text.strip() == "":
        text = "/start"

    if text == "/stats" and user_id == os.getenv("PRODUCER_USER_ID", "24585087"):
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT id, user_id, query_type, substr(prompt,1,80), created_at FROM deepseek_queries ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if rows:
            msg = "📊 Последние запросы:\n" + "\n".join([f"{r[0]}. {r[1]} | {r[2]} | {r[3]}... | {r[4]}" for r in rows])
            await send_message(user_id, msg[:3900], None)
        else:
            await send_message(user_id, "Нет запросов.", None)
        return

    if text == "/start":
        save_user_state(user_id, STATE_MENU, {})
        await send_message(user_id, WELCOME_TEXT, get_start_keyboard())
        return

    if state == STATE_AWAITING_BUSINESS_NAME:
        if len(text) > 100:
            await send_message(user_id, "Название слишком длинное, сократите (до 100 символов):")
            return
        save_user_state(user_id, STATE_AWAITING_BUSINESS_DESCRIPTION, {"business_name": text})
        await send_message(user_id, "Отлично! Теперь опишите бизнес: что вы делаете, кому помогаете, какая ваша уникальность? (макс 500 символов)")
        return

    if state == STATE_AWAITING_BUSINESS_DESCRIPTION:
        if len(text) > 500:
            await send_message(user_id, "Сократите описание до 500 символов:")
            return
        name = data.get("business_name")
        save_business_data(user_id, name, text)
        save_user_state(user_id, STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(user_id, "📋 Короткая анкета из 5 вопросов. Это поможет AI точнее подобрать план.\n\n" + SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0))
        return

    if state == STATE_AI_CHAT:
        report = get_report(user_id, "premium")
        if not report or report["status"] != "ready":
            await send_message(user_id, "Сначала пройдите анкету и получите план.", [[{"type": "callback", "text": "📊 Получить план", "payload": CALLBACK_AUDIT}]])
            save_user_state(user_id, STATE_MENU, {})
            return
        save_chat_message(user_id, "user", text)
        if any(kw in text.lower() for kw in ["настрой", "сделай", "воронку", "таргет", "внедрение", "яндекс директ"]):
            ans = "🔥 Это задача для профессионального внедрения. Если хотите, чтобы я лично настроил вам воронку или рекламу — запишитесь на консультацию через кнопку в меню «🎯 Записаться». Я свяжусь с вами."
            await send_message(user_id, ans, get_implementation_keyboard())
        else:
            await send_message(user_id, "🤔 Думаю...", None)
            hist = get_chat_history(user_id, 10)
            ans = await call_deepseek_chat(text, user_id, report["text"], hist)
            ans += "\n\n📌 *Листай вверх к началу плана, если нужны детали*"
            await send_message(user_id, ans, get_ai_keyboard())
        save_chat_message(user_id, "assistant", ans)
        return

    if state == STATE_AWAITING_IMPLEMENTATION:
        logger.info(f"Implementation request from {user_id}: {text}")
        await send_message(user_id, "✅ Заявка принята! Продюсер свяжется с вами. А пока можете задать вопрос AI или пройти челлендж.", get_main_menu_keyboard())
        save_user_state(user_id, STATE_MENU, {})
        return

    if state == STATE_AWAITING_FEEDBACK_REASON:
        save_feedback(user_id, 0, text)
        await send_message(user_id, "Спасибо за честность! Я учту это.\n\nПопробуете пройти анкету заново или записаться на консультацию?", get_start_keyboard())
        save_user_state(user_id, STATE_MENU, {})
        return

    save_user_state(user_id, STATE_MENU, {})
    await send_message(user_id, "Выберите действие:", get_start_keyboard())

async def process_callback(chat_id: str, callback_id: str, callback_data: str):
    logger.info(f"Callback: user={chat_id}, data={callback_data}")
    state, _ = get_user_state(chat_id)

    if not callback_data or callback_data in ("start", "get_started", "START", "null", "None", ""):
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id, WELCOME_TEXT, get_start_keyboard())
        return

    # Обработчик кнопки "Да, хочу маркетинговый план"
    if callback_data == CALLBACK_START_SURVEY or callback_data == "start_survey":
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {})
        await send_callback_answer(callback_id, "Введите название вашего проекта:", None)
        return

    if callback_data == CALLBACK_MENU:
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id, "🏠 Главное меню", get_main_menu_keyboard())
        return

    if callback_data == CALLBACK_BOOK_CONSULT:
        await send_callback_answer(callback_id, CONSULTATION_TEXT, [[{"type": "link", "text": "✍️ Перейти к записи", "url": CONSULT_LINK}], [{"type": "callback", "text": "🏠 Меню", "payload": CALLBACK_MENU}]])
        return

    if callback_data == CALLBACK_RESET:
        save_user_state(chat_id, STATE_MENU, {})
        await send_callback_answer(callback_id, "Начнём сначала.", get_start_keyboard())
        return

    if callback_data == CALLBACK_AUDIT:
        if state in (STATE_SURVEY, STATE_AWAITING_BUSINESS_NAME, STATE_AWAITING_BUSINESS_DESCRIPTION):
            await send_callback_answer(callback_id, "Вы уже в процессе анкеты. Если хотите начать заново — нажмите сброс.", [[{"type": "callback", "text": "🔄 Сбросить", "payload": CALLBACK_RESET}]])
            return
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {})
        await send_callback_answer(callback_id, "Введите название вашего проекта:", None)
        return

    if callback_data == CALLBACK_ASK_AI:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id, "Сначала получите план.", [[{"type": "callback", "text": "📊 Получить план", "payload": CALLBACK_AUDIT}]])
            return
        save_user_state(chat_id, STATE_AI_CHAT, {})
        await send_callback_answer(callback_id, "💬 Задавайте вопросы по вашему плану. Я на связи 24/7.", None)
        return

    if callback_data == CALLBACK_FEEDBACK_YES:
        save_feedback(chat_id, 1)
        await send_callback_answer(callback_id, "Отлично! Рад, что помогло. Что дальше?", get_after_plan_keyboard())
        return

    if callback_data == CALLBACK_FEEDBACK_NO:
        await send_callback_answer(callback_id, "Напишите кратко, чего не хватило (2-3 слова):", None)
        save_user_state(chat_id, STATE_AWAITING_FEEDBACK_REASON, {})
        return

    if callback_data == CALLBACK_IMPLEMENTATION:
        save_user_state(chat_id, STATE_AWAITING_IMPLEMENTATION, {})
        await send_callback_answer(callback_id, "Опишите, что именно нужно внедрить (воронка, реклама, скрипты):", None)
        return

    if callback_data == CALLBACK_CHALLENGE_TASK:
        report = get_report(chat_id, "premium")
        if not report or report["status"] != "ready":
            await send_callback_answer(callback_id, "Сначала получите план.", [[{"type": "callback", "text": "📊 Получить план", "payload": CALLBACK_AUDIT}]])
            return
        chall = get_active_challenge(chat_id)
        if not chall:
            cid = start_new_challenge(chat_id)
            task = await generate_challenge_task(chat_id, 1, report["text"])
            save_challenge_task(cid, 1, task)
            await send_callback_answer(callback_id, f"🏆 Челлендж начался!\n\n{task}", get_challenge_keyboard())
        else:
            cur = get_current_task(chall["id"], chall["current_day"])
            if cur and not cur["is_completed"]:
                await send_callback_answer(callback_id, f"📋 Задание дня {chall['current_day']}:\n\n{cur['task_text']}", get_challenge_keyboard())
            else:
                await send_callback_answer(callback_id, f"Прогресс: день {chall['current_day']} из 14, выполнено {chall['tasks_completed']}", get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_DONE:
        chall = get_active_challenge(chat_id)
        if not chall:
            await send_callback_answer(callback_id, "Нет активного челленджа.", get_main_menu_keyboard())
            return
        cur = get_current_task(chall["id"], chall["current_day"])
        if not cur or cur["is_completed"]:
            await send_callback_answer(callback_id, "Задание уже выполнено.", get_challenge_keyboard())
            return
        mark_task_completed(chall["id"], chall["current_day"])
        if chall["current_day"] >= 14:
            complete_challenge(chall["id"])
            await send_callback_answer(callback_id, "🎉 Поздравляю! Челлендж пройден!", get_after_plan_keyboard())
        else:
            new_day = chall["current_day"] + 1
            advance_challenge_day(chall["id"], new_day)
            report = get_report(chat_id, "premium")
            new_task = await generate_challenge_task(chat_id, new_day, report["text"])
            save_challenge_task(chall["id"], new_day, new_task)
            await send_callback_answer(callback_id, f"✅ Задание дня {chall['current_day']} выполнено!\n\nЗадание дня {new_day}:\n{new_task}", get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_PROGRESS:
        chall = get_active_challenge(chat_id)
        if not chall:
            await send_callback_answer(callback_id, "Нет активного челленджа.", get_main_menu_keyboard())
            return
        await send_callback_answer(callback_id, f"Прогресс: день {chall['current_day']} из 14, выполнено {chall['tasks_completed']}", get_challenge_keyboard())
        return

    if callback_data in [Q1_SERVICE, Q1_INFO, Q1_CONSULT, Q1_NONE, Q2_LT5, Q2_5_20, Q2_20_50, Q2_50P,
                         Q3_LT10, Q3_10_50, Q3_50_200, Q3_200P, Q4_300, Q4_500, Q4_1M, Q4_SCALE,
                         Q5_YES, Q5_NO, Q5_PROGRESS]:
        _, ud = get_user_state(chat_id)
        if ud is None: ud = {}
        ud.setdefault("answers", {})
        ud.setdefault("survey_step", 0)
        step = ud["survey_step"]
        if step < len(SURVEY_QUESTIONS):
            key = SURVEY_QUESTIONS[step]["key"]
            ud["answers"][key] = callback_data
            ud["survey_step"] = step + 1
            save_user_state(chat_id, STATE_SURVEY, ud)
            if step + 1 < len(SURVEY_QUESTIONS):
                await send_callback_answer(callback_id, SURVEY_QUESTIONS[step+1]["text"], get_survey_keyboard(step+1))
            else:
                save_form(chat_id, ud["answers"])
                biz = get_business_data(chat_id)
                if not biz:
                    await send_callback_answer(callback_id, "Ошибка, начните заново.", get_start_keyboard())
                    return
                existing = get_report(chat_id, "premium")
                if existing and existing["status"] == "ready":
                    report_text = existing["text"]
                elif existing and existing["status"] == "generating":
                    await send_callback_answer(callback_id, "План уже генерируется, подождите...", None)
                    return
                else:
                    save_report(chat_id, "premium", "")
                    await send_callback_answer(callback_id, "🔍 Запускаю анализ...", None)
                    await send_animation(chat_id)
                    report_text = await call_deepseek_marketing_plan(biz["name"], biz["description"], ud["answers"], chat_id)
                    if not report_text:
                        await send_message(chat_id, "❌ Не удалось сгенерировать план. Попробуйте позже.", get_main_menu_keyboard())
                        update_report_status(chat_id, "failed")
                        return
                    save_report(chat_id, "premium", report_text)
                final = report_text + "\n\n📜 *Листай вверх к началу плана*"
                await send_long_message(chat_id, "✅ ВАШ МАРКЕТИНГОВЫЙ ПЛАН ГОТОВ!\n\n" + final, None)
                await asyncio.sleep(2)
                await send_message(chat_id, "Было полезно? Поделитесь мнением.", get_feedback_keyboard())
        return

    await send_callback_answer(callback_id, "Выберите действие:", get_start_keyboard())

# === НАПОМИНАНИЯ ===
async def reminders_task():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("""
                SELECT b.user_id, r.ready_at, b.reminder_sent_24h, b.reminder_sent_7d
                FROM business_data b
                JOIN reports r ON b.user_id = r.user_id AND r.report_type = 'premium' AND r.status = 'ready'
                WHERE b.reminder_sent_24h = 0 OR b.reminder_sent_7d = 0
            """).fetchall()
            for user_id, ready_at, sent24, sent7 in rows:
                delta = get_moscow_time() - datetime.fromisoformat(ready_at)
                if not sent24 and delta >= timedelta(hours=24):
                    await send_message(user_id, "📌 Напоминаю: твой маркетинговый план ждёт внедрения. Выбери один пункт и сделай сегодня. Если застрял — задай вопрос AI или запишись на консультацию.", None)
                    update_reminder_flags(user_id, reminder_24h=True)
                    await asyncio.sleep(2)
                if not sent7 and delta >= timedelta(days=7):
                    await send_message(user_id, "🔥 7 дней! Большинство моих клиентов получают первые деньги через 2 недели. Продолжай выполнять задания челленджа. Если результат ещё не пришёл — самое время записаться на разбор со мной. Кнопка в меню.", None)
                    update_reminder_flags(user_id, reminder_7d=True)
                    await asyncio.sleep(2)
            conn.close()
        except Exception as e:
            logger.error(f"Reminders error: {e}")
        await asyncio.sleep(21600)

# === FASTAPI ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(reminders_task())
    yield

app = FastAPI(lifespan=lifespan)

# Временный эндпоинт для настройки подписки (удалить после использования)
@app.get("/subscribe_me")
async def subscribe_to_bot_events():
    token = MAX_BOT_TOKEN
    url = "https://platform-api.max.ru/subscriptions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "url": "https://realplanninig-oss-max-salesplan-bot-1a18.twc1.net/webhook",
        "update_types": ["message_created", "bot_started", "callback_query"]
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        return {
            "status": response.status_code,
            "response": response.json() if response.text else None,
            "text": response.text
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
async def root():
    return {"status": "Salesplan bot running", "version": "10.15"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        logger.info(f"RAW BODY: {body[:1000]}")
        payload = await request.json()
        logger.info(f"FULL PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:1000]}")
        if payload.get("update_type") == "bot_started" or payload.get("event") == "bot_started":
            user_id = payload.get("user", {}).get("user_id") or payload.get("sender", {}).get("user_id")
            if user_id:
                await send_message(str(user_id), WELCOME_TEXT, get_start_keyboard())
            return Response(status_code=200)
        if "message" in payload and "callback" not in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            text = msg.get("body", {}).get("text")
            if user_id and text is not None:
                await process_message(str(user_id), text.strip())
            elif user_id:
                await process_message(str(user_id), "/start")
        elif "callback" in payload:
            cb = payload["callback"]
            user_id = cb.get("user", {}).get("user_id")
            callback_id = cb.get("callback_id")
            data = cb.get("payload")
            logger.info(f"CALLBACK RECEIVED: user_id={user_id}, callback_id={callback_id}, data={data}")
            if user_id:
                await process_callback(str(user_id), str(callback_id), str(data) if data else "")
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        return Response(status_code=200)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
