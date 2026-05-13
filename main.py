# File: main.py — бот Salesplan (версия 11.0) с использованием библиотеки maxapi

import asyncio
import logging
import sqlite3
import os
import json
import re
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import uvicorn

from maxapi import Bot, Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated, CallbackQuery
from maxapi.keyboards import InlineKeyboardBuilder

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
HELP_URL = os.getenv("HELP_URL", "https://max.ru/u/f9LHodD0cOJp3NEa7OYZr1MKfUuC1hYDyKh2f4HFkfTXT88W3txWaBaFQmU")
CONSULT_LINK = os.getenv("CONSULT_LINK", "https://max.ru/u/f9LHodD0cOJmqGaOJJxBthmX1NCjnOXHlsnYzYTc83uuDLwN4j08I-fmU4U")

if not MAX_BOT_TOKEN:
    raise RuntimeError("MAX_BOT_TOKEN not found in .env")

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

# === БАЗА ДАННЫХ ===
DB_PATH = "salesplan_bot.db"

def init_db():
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
            report_text TEXT,
            status TEXT DEFAULT 'generating',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ready_at TIMESTAMP
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
    conn.commit()
    conn.close()

init_db()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def get_user_state(user_id: str) -> tuple[str, Dict]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT state, data FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return row[0], json.loads(row[1]) if row[1] else {}
    return "menu", {}

def save_user_state(user_id: str, state: str, data: Dict = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO user_state (user_id, state, data, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (user_id, state, json.dumps(data or {}, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def save_business_data(user_id: str, name: str, description: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO business_data (user_id, business_name, business_description) VALUES (?, ?, ?)",
        (user_id, name, description)
    )
    conn.commit()
    conn.close()

def get_business_data(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT business_name, business_description FROM business_data WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return {"name": row[0], "description": row[1]}
    return None

def save_form(user_id: str, answers: Dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO forms (user_id, q1, q2, q3, q4, q5) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, answers.get("q1"), answers.get("q2"), answers.get("q3"), answers.get("q4"), answers.get("q5"))
    )
    conn.commit()
    conn.close()

def get_form(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT q1, q2, q3, q4, q5 FROM forms WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return {"q1": row[0], "q2": row[1], "q3": row[2], "q4": row[3], "q5": row[4]}
    return None

def save_report(user_id: str, report_text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO reports (user_id, report_text, status, ready_at) VALUES (?, ?, 'ready', CURRENT_TIMESTAMP)",
        (user_id, report_text)
    )
    conn.commit()
    conn.close()

def get_report(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT report_text, status FROM reports WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()
    if row:
        return {"text": row[0], "status": row[1]}
    return None

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
    if row:
        return {"id": row[0], "current_day": row[1], "tasks_completed": row[2]}
    return None

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
    if row:
        return {"id": row[0], "task_text": row[1], "is_completed": bool(row[2])}
    return None

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

# === DEEPSEEK API ===
async def call_deepseek_marketing_plan(name: str, description: str, answers: dict) -> str:
    if not DEEPSEEK_API_KEY:
        return None
    q1_map = {"q1_service": "Услугу", "q1_info": "Инфопродукт", "q1_consult": "Консультацию", "q1_none": "Пока не продаю"}
    q2_map = {"q2_lt5": "до 5000 ₽", "q2_5_20": "5000-20000 ₽", "q2_20_50": "20000-50000 ₽", "q2_50p": "более 50000 ₽"}
    q3_map = {"q3_lt10": "менее 10", "q3_10_50": "10-50", "q3_50_200": "50-200", "q3_200p": "более 200"}
    q4_map = {"q4_300": "300 000 ₽/мес", "q4_500": "500 000 ₽/мес", "q4_1m": "1 000 000 ₽/мес", "q4_scale": "масштабирование"}
    q5_map = {"q5_yes": "да", "q5_no": "нет", "q5_progress": "в разработке"}
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
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "Ты — бизнес-консультант."}, {"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 4000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        return None
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return None

async def call_deepseek_chat(question: str, report_text: str, history: list) -> str:
    if not DEEPSEEK_API_KEY:
        return "Извините, AI-чат временно недоступен."
    history_text = "\n".join([f"{m['role']}: {m['message']}" for m in history[-5:]])
    prompt = f"""Вот план: {report_text[:3000]} \nИстория: {history_text}\nВопрос: {question}\nОтветь по делу, без воды. Если просит настройку — скажи оставить заявку.
Ограничения: без Instagram/Telegram, только VK, Яндекс.Директ, MAX."""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "Ты — консультант."}, {"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 1000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        return "Ошибка, попробуйте позже."
    except Exception as e:
        logger.error(f"DeepSeek chat error: {e}")
        return "Ошибка соединения."

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
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "Ты — наставник. Только одно действие, без списков."}, {"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 600
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            task = response.json()["choices"][0]["message"]["content"]
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
Ты получишь первых лидов и обратную связь."""

# === КЛАВИАТУРЫ ===
def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Получить маркетинговый план", callback_data="survey")
    builder.button(text="💬 Задать вопрос AI", callback_data="ai_chat")
    builder.button(text="🏆 Челлендж 14 дней", callback_data="challenge")
    builder.button(text="🎯 Консультация", url=CONSULT_LINK)
    builder.button(text="🆘 Помощь", url=HELP_URL)
    builder.adjust(1)
    return builder.as_markup()

def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, хочу маркетинговый план за 2 минуты", callback_data="start_survey")
    builder.button(text="🎯 Записаться на консультацию", url=CONSULT_LINK)
    builder.button(text="🆘 Помощь", url=HELP_URL)
    builder.adjust(1)
    return builder.as_markup()

def get_survey_keyboard(step: int):
    # step: 0..4 для 5 вопросов
    questions = [
        ("Что вы продаёте?", [("Услугу", "q1_service"), ("Инфопродукт", "q1_info"), ("Консультацию", "q1_consult"), ("Пока не продаю", "q1_none")]),
        ("Средний чек (₽)", [("до 5 000", "q2_lt5"), ("5 000 - 20 000", "q2_5_20"), ("20 000 - 50 000", "q2_20_50"), ("более 50 000", "q2_50p")]),
        ("Клиентов в месяц", [("менее 10", "q3_lt10"), ("10-50", "q3_10_50"), ("50-200", "q3_50_200"), ("более 200", "q3_200p")]),
        ("Цель на 2026", [("300 000 ₽/мес", "q4_300"), ("500 000 ₽/мес", "q4_500"), ("1 000 000 ₽/мес", "q4_1m"), ("Масштабирование", "q4_scale")]),
        ("Уже есть автоворонка?", [("Да", "q5_yes"), ("Нет", "q5_no"), ("В разработке", "q5_progress")])
    ]
    text, options = questions[step]
    builder = InlineKeyboardBuilder()
    for label, data in options:
        builder.button(text=label, callback_data=data)
    builder.button(text="🆘 Помощь", url=HELP_URL)
    builder.adjust(1)
    return text, builder.as_markup()

def get_after_plan_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Задать вопрос AI", callback_data="ai_chat")
    builder.button(text="🏆 Начать челлендж", callback_data="challenge_start")
    builder.button(text="🎯 Консультация", url=CONSULT_LINK)
    builder.button(text="🔄 Пройти анкету заново", callback_data="survey")
    builder.button(text="🆘 Помощь", url=HELP_URL)
    builder.adjust(1)
    return builder.as_markup()

def get_challenge_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Получить задание", callback_data="challenge_task")
    builder.button(text="✅ Выполнил задание", callback_data="challenge_done")
    builder.button(text="📊 Мой прогресс", callback_data="challenge_progress")
    builder.button(text="🎯 Консультация", url=CONSULT_LINK)
    builder.button(text="🆘 Помощь", url=HELP_URL)
    builder.button(text="🏠 Главное меню", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()

def get_ai_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🏆 Челлендж", callback_data="challenge")
    builder.button(text="🎯 Консультация", url=CONSULT_LINK)
    builder.button(text="🏠 Меню", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()

def get_implementation_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Записаться на консультацию", url=CONSULT_LINK)
    builder.button(text="🏠 Меню", callback_data="menu")
    builder.adjust(1)
    return builder.as_markup()

WELCOME_TEXT = """🔥 Привет, предприниматель! Я Вероника Макаревич — продюсер, который знает, как превратить хаос в прибыль.

Многие эксперты тонут в задачах, а денег нет. Знакомо?

⚡️ Что я тебе даю:

📊 Маркетинговый план — не теория, а дорожная карта.
💬 AI-чат 24/7 — отвечаю на вопросы по плану.
🏆 Челлендж 14 дней — шаг за шагом к деньгам.
🎯 Консультация со мной — разберём твой случай.

Зачем тебе план? Большинство экспертов продают впустую, потому что нет системы.

Поехали? 👇"""

# === ОБРАБОТЧИКИ ===
bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher(bot)

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    logger.info(f"Bot started for user {event.user_id}")
    await event.bot.send_message(event.chat_id, WELCOME_TEXT, reply_markup=get_start_keyboard())

@dp.message_created(Command('start'))
async def on_start(event: MessageCreated):
    logger.info(f"Command /start from {event.user_id}")
    await event.message.answer(WELCOME_TEXT, reply_markup=get_start_keyboard())

@dp.callback_query(lambda c: c.data == "start_survey")
async def start_survey(event: CallbackQuery):
    await event.answer()
    state, _ = get_user_state(event.user_id)
    if state not in ("menu", "survey"):
        await event.message.answer("Вы уже в процессе анкеты. Пожалуйста, ответьте на текущий вопрос или нажмите /start для сброса.")
        return
    save_user_state(event.user_id, "awaiting_business_name", {})
    await event.message.answer("Введите название вашего проекта:")

@dp.message_created()
async def handle_message(event: MessageCreated):
    user_id = event.user_id
    text = event.message.text.strip()
    state, data = get_user_state(user_id)

    if state == "awaiting_business_name":
        if len(text) > 100:
            await event.message.answer("Название слишком длинное, сократите (до 100 символов):")
            return
        save_user_state(user_id, "awaiting_business_description", {"business_name": text})
        await event.message.answer("Отлично! Теперь опишите бизнес (что делаете, кому помогаете, уникальность), до 500 символов:")
        return

    if state == "awaiting_business_description":
        if len(text) > 500:
            await event.message.answer("Сократите описание до 500 символов:")
            return
        name = data.get("business_name")
        save_business_data(user_id, name, text)
        save_user_state(user_id, "survey", {"answers": {}, "step": 0})
        q_text, kb = get_survey_keyboard(0)
        await event.message.answer(q_text, reply_markup=kb)
        return

    if state == "ai_chat":
        report = get_report(user_id)
        if not report or report["status"] != "ready":
            await event.message.answer("Сначала пройдите анкету и получите план.", reply_markup=get_start_keyboard())
            save_user_state(user_id, "menu", {})
            return
        save_chat_message(user_id, "user", text)
        if any(kw in text.lower() for kw in ["настрой", "сделай", "воронку", "таргет", "внедрение", "яндекс директ"]):
            answer = "🔥 Это задача для профессионального внедрения. Запишитесь на консультацию по кнопке ниже."
            await event.message.answer(answer, reply_markup=get_implementation_keyboard())
        else:
            await event.message.answer("🤔 Думаю...")
            history = get_chat_history(user_id, 10)
            answer = await call_deepseek_chat(text, report["text"], history)
            answer += "\n\n📌 *Листай вверх к началу плана*"
            await event.message.answer(answer, reply_markup=get_ai_keyboard())
        save_chat_message(user_id, "assistant", answer)
        return

    if state == "implementation":
        # Заявка на внедрение – просто сохраняем в лог, но можно и продюсеру уведомление
        logger.info(f"Implementation request from {user_id}: {text}")
        await event.message.answer("✅ Заявка принята! Продюсер свяжется с вами.", reply_markup=get_main_keyboard())
        save_user_state(user_id, "menu", {})
        return

    if state == "feedback_reason":
        # Сохраняем отрицательный отзыв
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO feedback (user_id, rating, reason) VALUES (?, 0, ?)", (user_id, text))
        conn.commit()
        conn.close()
        await event.message.answer("Спасибо за честность! Учту это.\n\nПопробуете пройти анкету заново?", reply_markup=get_start_keyboard())
        save_user_state(user_id, "menu", {})
        return

    # Если состояние не распознано, сбрасываем в меню
    save_user_state(user_id, "menu", {})
    await event.message.answer("Выберите действие:", reply_markup=get_start_keyboard())

@dp.callback_query()
async def handle_callback(event: CallbackQuery):
    user_id = event.user_id
    data = event.data
    logger.info(f"Callback from {user_id}: {data}")

    if data == "menu":
        save_user_state(user_id, "menu", {})
        await event.message.edit_text("🏠 Главное меню", reply_markup=get_main_keyboard())
        await event.answer()
        return

    # Обработка опросника
    survey_prefixes = ("q1_", "q2_", "q3_", "q4_", "q5_")
    if data.startswith(survey_prefixes):
        state, ud = get_user_state(user_id)
        if state != "survey":
            await event.answer("Анкета не активна. Начните сначала.", show_alert=True)
            return
        step = ud.get("step", 0)
        answers = ud.get("answers", {})
        # Сохраняем ответ
        answers[data] = data
        ud["answers"] = answers
        step += 1
        ud["step"] = step
        save_user_state(user_id, "survey", ud)
        if step < 5:
            q_text, kb = get_survey_keyboard(step)
            await event.message.edit_text(q_text, reply_markup=kb)
            await event.answer()
        else:
            # Анкета завершена
            save_form(user_id, answers)
            biz = get_business_data(user_id)
            if not biz:
                await event.message.edit_text("Ошибка данных. Начните заново.", reply_markup=get_start_keyboard())
                save_user_state(user_id, "menu", {})
                await event.answer()
                return
            # Проверяем, есть ли уже готовый отчёт
            report = get_report(user_id)
            if report and report["status"] == "ready":
                report_text = report["text"]
                await event.message.edit_text("✅ Ваш план уже готов! Отправляю...")
                await event.message.answer(report_text[:3900])
                if len(report_text) > 3900:
                    await event.message.answer(report_text[3900:])
                await event.message.answer("Что дальше?", reply_markup=get_after_plan_keyboard())
                await event.answer()
                return
            # Генерируем новый план
            await event.message.edit_text("🔍 Запускаю анализ...\n\nПожалуйста, подождите 1-2 минуты.")
            report_text = await call_deepseek_marketing_plan(biz["name"], biz["description"], answers)
            if not report_text:
                await event.message.edit_text("❌ Не удалось сгенерировать план. Попробуйте позже.", reply_markup=get_start_keyboard())
                save_user_state(user_id, "menu", {})
                await event.answer()
                return
            save_report(user_id, report_text)
            final_text = report_text + "\n\n📜 *Листай вверх к началу плана*"
            # Отправляем длинный текст частями
            await event.message.edit_text("✅ ВАШ МАРКЕТИНГОВЫЙ ПЛАН ГОТОВ!")
            for part in [final_text[i:i+3900] for i in range(0, len(final_text), 3900)]:
                await event.message.answer(part)
            await event.message.answer("Было полезно? Поделитесь мнением:", reply_markup=get_feedback_keyboard())
            await event.answer()
        return

    # Остальные callback-обработчики (для краткости опущены, но их можно добавить по аналогии)
    # Например: ai_chat, challenge, challenge_start, challenge_task, challenge_done, challenge_progress, feedback_yes/no
    # Здесь мы реализуем основные, остальные можно добавить позже

    if data == "ai_chat":
        report = get_report(user_id)
        if not report or report["status"] != "ready":
            await event.answer("Сначала пройдите анкету и получите план.", show_alert=True)
            return
        save_user_state(user_id, "ai_chat", {})
        await event.message.edit_text("💬 Задавайте вопросы по вашему плану. Я на связи 24/7.", reply_markup=get_ai_keyboard())
        await event.answer()
        return

    if data == "challenge":
        report = get_report(user_id)
        if not report or report["status"] != "ready":
            await event.answer("Сначала получите план.", show_alert=True)
            return
        chall = get_active_challenge(user_id)
        if not chall:
            await event.message.edit_text("🏆 Челлендж ещё не начат. Нажмите «Начать челлендж».", reply_markup=get_challenge_keyboard())
        else:
            # Показываем текущий прогресс
            await event.message.edit_text(f"Прогресс: день {chall['current_day']} из 14, выполнено {chall['tasks_completed']} заданий.", reply_markup=get_challenge_keyboard())
        await event.answer()
        return

    if data == "challenge_start":
        report = get_report(user_id)
        if not report or report["status"] != "ready":
            await event.answer("Сначала получите план.", show_alert=True)
            return
        chall = get_active_challenge(user_id)
        if not chall:
            cid = start_new_challenge(user_id)
            task_text = await generate_challenge_task(user_id, 1, report["text"])
            save_challenge_task(cid, 1, task_text)
            await event.message.edit_text(f"🏆 Челлендж начался!\n\n{task_text}", reply_markup=get_challenge_keyboard())
        else:
            await event.answer("Челлендж уже активен.", show_alert=True)
        return

    if data == "challenge_task":
        chall = get_active_challenge(user_id)
        if not chall:
            await event.answer("Челлендж не активен. Начните его сначала.", show_alert=True)
            return
        cur = get_current_task(chall["id"], chall["current_day"])
        if cur:
            await event.message.edit_text(f"📋 Задание дня {chall['current_day']}:\n\n{cur['task_text']}", reply_markup=get_challenge_keyboard())
        else:
            await event.message.edit_text("Задание не найдено.", reply_markup=get_challenge_keyboard())
        await event.answer()
        return

    if data == "challenge_done":
        chall = get_active_challenge(user_id)
        if not chall:
            await event.answer("Челлендж не активен.", show_alert=True)
            return
        cur = get_current_task(chall["id"], chall["current_day"])
        if not cur or cur["is_completed"]:
            await event.answer("Задание уже выполнено или не найдено.", show_alert=True)
            return
        mark_task_completed(chall["id"], chall["current_day"])
        if chall["current_day"] >= 14:
            complete_challenge(chall["id"])
            await event.message.edit_text("🎉 ПОЗДРАВЛЯЮ! Вы прошли 14-дневный челлендж!", reply_markup=get_after_plan_keyboard())
        else:
            new_day = chall["current_day"] + 1
            advance_challenge_day(chall["id"], new_day)
            report = get_report(user_id)
            new_task = await generate_challenge_task(user_id, new_day, report["text"])
            save_challenge_task(chall["id"], new_day, new_task)
            await event.message.edit_text(f"✅ Задание дня {chall['current_day']} выполнено!\n\nЗадание дня {new_day}:\n{new_task}", reply_markup=get_challenge_keyboard())
        await event.answer()
        return

    if data == "challenge_progress":
        chall = get_active_challenge(user_id)
        if not chall:
            await event.answer("Челлендж не активен.", show_alert=True)
            return
        await event.message.edit_text(f"Прогресс: день {chall['current_day']} из 14, выполнено {chall['tasks_completed']} заданий.", reply_markup=get_challenge_keyboard())
        await event.answer()
        return

    if data == "feedback_yes":
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO feedback (user_id, rating) VALUES (?, 1)", (user_id,))
        conn.commit()
        conn.close()
        await event.message.edit_text("Отлично! Рад, что помогло. Что дальше?", reply_markup=get_after_plan_keyboard())
        await event.answer()
        return

    if data == "feedback_no":
        save_user_state(user_id, "feedback_reason", {})
        await event.message.edit_text("Напишите кратко, чего не хватило (2-3 слова):")
        await event.answer()
        return

    if data == "survey":
        # Повторная анкета – сбрасываем состояние
        save_user_state(user_id, "menu", {})
        await start_survey(event)
        return

    await event.answer("Неизвестная команда.")

def get_feedback_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="👍 Полезно", callback_data="feedback_yes")
    builder.button(text="👎 Не помогло", callback_data="feedback_no")
    builder.adjust(2)
    return builder.as_markup()

# === FASTAPI ===
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logger.info(f"Webhook body: {json.dumps(body, ensure_ascii=False)[:500]}")
    await dp.handle_webhook(body)
    return Response(status_code=200)

@app.get("/")
async def root():
    return {"status": "Salesplan bot running", "version": "11.0"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
