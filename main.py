# File: main.py — бот Salesplan для MAX (активация по телефону)

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
import uvicorn

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
SITE_API_URL = os.getenv("SITE_API_URL", "https://realplanninig-oss-salesplan-web-7eb2.twc1.net")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://max.ru/u/f9LHodD0cOL1ttBGofp6mcEX6K6JaHd_qndKbBG0prUpl4foZEiL-tzu8go")

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
STATE_AWAITING_PHONE = "awaiting_phone"
STATE_AI_CHAT = "ai_chat"
STATE_AWAITING_IMPLEMENTATION = "awaiting_implementation"

# === CALLBACK DATA ===
CALLBACK_ASK_AI = "ask_ai"
CALLBACK_CHALLENGE_TASK = "challenge_task"
CALLBACK_CHALLENGE_DONE = "challenge_done"
CALLBACK_CHALLENGE_PROGRESS = "challenge_progress"
CALLBACK_IMPLEMENTATION = "implementation"
CALLBACK_MENU = "menu"

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
        CREATE TABLE IF NOT EXISTS user_access (
            user_id TEXT PRIMARY KEY,
            phone TEXT,
            site_user_id TEXT,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
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

def format_phone(phone: str) -> str:
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('7') or digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 11 and digits.startswith('7'):
        return '+' + digits
    if len(digits) == 10:
        return '+7' + digits
    return phone

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

def save_user_access(user_id: str, phone: str, site_user_id: str = None, days: int = 30):
    expires_at = get_moscow_time() + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO user_access (user_id, phone, site_user_id, expires_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, phone, site_user_id, expires_at.isoformat()))
    conn.commit()
    conn.close()

def has_active_access(user_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT expires_at FROM user_access WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        expires_at = datetime.fromisoformat(row[0])
        return get_moscow_time() < expires_at
    return False

def get_user_phone(user_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT phone FROM user_access WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

# === ЧЕЛЛЕНДЖ ===
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

# === API САЙТА ===
async def check_premium_access_by_phone(phone: str) -> tuple:
    try:
        url = f"{SITE_API_URL}/api/check_premium_by_phone?phone={phone}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("has_access", False), data.get("user_id")
                else:
                    logger.warning(f"API check returned {resp.status}")
    except Exception as e:
        logger.error(f"API check failed: {e}")
    return False, None

async def download_report_from_site(user_id: str, report_type: str = "premium") -> Optional[str]:
    try:
        url = f"{SITE_API_URL}/download/{user_id}/{report_type}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    logger.warning(f"Download report failed: {resp.status}")
    except Exception as e:
        logger.error(f"Download report error: {e}")
    return None

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

# === КЛАВИАТУРЫ ===
def get_main_menu_keyboard():
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
                "payload": CALLBACK_CHALLENGE_TASK,
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🔥 Внедрение под ключ",
                "payload": CALLBACK_IMPLEMENTATION,
                "intent": "default"
            }
        ],
        [
            {
                "type": "link",
                "text": "❓ Поддержка",
                "url": SUPPORT_URL
            }
        ],
        [
            {
                "type": "link",
                "text": "🌐 Перейти на сайт",
                "url": SITE_API_URL
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
                "text": "🏆 Челлендж 7 дней",
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

def get_error_keyboard():
    return [
        [
            {
                "type": "link",
                "text": "❓ Написать в поддержку",
                "url": SUPPORT_URL
            }
        ]
    ]

# === DEEPSEEK API ===
async def call_deepseek_chat(question: str, user_id: str, report_text: str, history: list) -> str:
    import requests
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
    headers = {"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}", "Content-Type": "application/json"}
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
    import requests
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
    headers = {"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}", "Content-Type": "application/json"}
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

    if callback_data == CALLBACK_ASK_AI:
        if not has_active_access(chat_id):
            await send_callback_answer(callback_id,
                "⛔ У тебя нет активного Premium-доступа.\n\nЧтобы получить доступ:\n1️⃣ Перейди на сайт и пройди бесплатную диагностику\n2️⃣ Оплати тариф «План + AI + Челлендж» — 1 490 ₽\n3️⃣ После оплаты напиши /start и введи номер телефона",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
            return
        
        save_user_state(chat_id, STATE_AI_CHAT, {})
        await send_callback_answer(callback_id,
            "💬 Отлично! Задавай вопросы по плану. Я на связи 24/7.\n\n⚠️ Если спросишь про внедрение — я направлю к продюсеру.",
            None)
        return

    if callback_data == CALLBACK_CHALLENGE_TASK:
        if not has_active_access(chat_id):
            await send_callback_answer(callback_id,
                "⛔ Челлендж доступен только после активации Premium-доступа.\n\nНапиши /start и введи номер телефона",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
            return
        
        phone = get_user_phone(chat_id)
        if not phone:
            await send_callback_answer(callback_id,
                "❌ Не найден номер телефона. Напиши /start и введи номер телефона, указанный при оплате.",
                None)
            return
        
        report_text = await download_report_from_site(chat_id, "premium")
        if not report_text:
            await send_callback_answer(callback_id,
                "❌ Не удалось загрузить план. Проверь, что план готов на сайте, или обратись в поддержку.",
                [[{"type": "link", "text": "❓ Поддержка", "url": SUPPORT_URL}]])
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            challenge_id = start_new_challenge(chat_id)
            task_text = await generate_challenge_task(chat_id, 1, report_text)
            save_challenge_task(challenge_id, 1, task_text)
            await send_callback_answer(callback_id,
                f"🏆 ПОЕХАЛИ! Челлендж «7 дней внедрения» начался!\n\n{task_text}",
                get_challenge_keyboard())
        else:
            current_task = get_current_task(challenge["id"], challenge["current_day"])
            if current_task and not current_task["is_completed"]:
                await send_callback_answer(callback_id,
                    f"📋 ТВОЁ ЗАДАНИЕ НА ДЕНЬ {challenge['current_day']}\n\n{current_task['task_text']}",
                    get_challenge_keyboard())
            else:
                await send_callback_answer(callback_id,
                    f"🏆 Прогресс: день {challenge['current_day']} из 7, выполнено {challenge['tasks_completed']} заданий.",
                    get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_DONE:
        if not has_active_access(chat_id):
            await send_callback_answer(callback_id,
                "⛔ Челлендж доступен только после активации Premium-доступа.",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Челлендж 7 дней»",
                get_challenge_keyboard())
            return
        
        current_task = get_current_task(challenge["id"], challenge["current_day"])
        if not current_task or current_task["is_completed"]:
            await send_callback_answer(callback_id,
                "✅ Задание на сегодня уже выполнено! Жди завтрашнее задание.",
                get_challenge_keyboard())
            return
        
        mark_task_completed(challenge["id"], challenge["current_day"])
        
        report_text = await download_report_from_site(chat_id, "premium")
        
        if challenge["current_day"] >= 7:
            complete_challenge(challenge["id"])
            await send_callback_answer(callback_id,
                f"🎉 ПОЗДРАВЛЯЮ! Ты прошла челлендж!\n\n✅ Выполнено: {challenge['tasks_completed'] + 1} из 7",
                [[{"type": "link", "text": "🌐 Купить продление", "url": SITE_API_URL}]])
        else:
            new_day = challenge["current_day"] + 1
            advance_challenge_day(challenge["id"], new_day)
            
            if report_text:
                task_text = await generate_challenge_task(chat_id, new_day, report_text)
                save_challenge_task(challenge["id"], new_day, task_text)
            else:
                task_text = f"💪 ЗАДАНИЕ ДЕНЬ {new_day}\nПрочитай план и найди 1 пункт для внедрения.\n\n📝 ЧЕК-ЛИСТ:\n- Открой план\n- Выбери один пункт\n- Сделай его"
                save_challenge_task(challenge["id"], new_day, task_text)
            
            await send_callback_answer(callback_id,
                f"✅ Отлично! Задание дня {challenge['current_day']} выполнено!\n\n🏆 Прогресс: {challenge['tasks_completed'] + 1} заданий\n\n💪 ЗАДАНИЕ ДЕНЬ {new_day}\n\n{task_text}",
                get_challenge_keyboard())
        return

    if callback_data == CALLBACK_CHALLENGE_PROGRESS:
        if not has_active_access(chat_id):
            await send_callback_answer(callback_id,
                "⛔ Челлендж доступен только после активации Premium-доступа.",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
            return
        
        challenge = get_active_challenge(chat_id)
        if not challenge:
            await send_callback_answer(callback_id,
                "❌ У тебя нет активного челленджа. Нажми «Челлендж 7 дней»",
                get_challenge_keyboard())
            return
        
        await send_callback_answer(callback_id,
            f"🏆 ТВОЙ ПРОГРЕСС\n\n📅 День {challenge['current_day']} из 7\n✅ Выполнено: {challenge['tasks_completed']}\n🎯 Осталось: {7 - challenge['current_day']}",
            get_challenge_keyboard())
        return

    if callback_data == CALLBACK_IMPLEMENTATION:
        save_user_state(chat_id, STATE_AWAITING_IMPLEMENTATION, {})
        await send_callback_answer(callback_id,
            "🔥 ВНЕДРЕНИЕ ПОД КЛЮЧ\n\nРасскажи о своём бизнесе и что нужно внедрить.\n\nЯ передам информацию продюсеру.",
            None)
        return

async def process_message(user_id: str, text: str):
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")

    if text == "/start":
        save_user_state(str(user_id), STATE_AWAITING_PHONE, {})
        await send_message(str(user_id),
            "👋 Добро пожаловать в Salesplan!\n\nДля активации Premium-доступа введите номер телефона, который вы указали при оплате на сайте.\n\n📞 Пример: +79816920888",
            None)
        return

    if state == STATE_AWAITING_PHONE:
        phone = format_phone(text)
        if not phone:
            await send_message(str(user_id), "❌ Неверный формат. Введите номер как +7XXXXXXXXXX", None)
            return
        
        has_access, site_user_id = await check_premium_access_by_phone(phone)
        
        if has_access:
            save_user_access(str(user_id), phone, site_user_id, 30)
            save_user_state(str(user_id), STATE_MENU, {"phone": phone, "premium_activated": True})
            
            await download_report_from_site(str(user_id), "premium")
            
            await send_message(str(user_id),
                "✅ Доступ активирован! 🎉\n\nТеперь доступны:\n💬 AI-чат по плану\n🏆 7-дневный челлендж\n\n👇 Что хочешь сделать?",
                get_main_menu_keyboard())
        else:
            await send_message(str(user_id),
                "❌ Доступ не найден.\n\nВозможные причины:\n• Оплата ещё не обработана (подождите 1-2 минуты)\n• Вы ввели не тот номер\n• Вы оплатили тариф 490 ₽ (без AI-доступа)\n\n👇 Если уверены, что оплатили Premium — напишите в поддержку",
                get_error_keyboard())
        return

    if state == STATE_MENU:
        if has_active_access(str(user_id)):
            save_user_state(str(user_id), STATE_AI_CHAT, {})
            await process_message(user_id, text)
            return
        else:
            await send_message(str(user_id),
                "👋 Привет! Для активации Premium-доступа:\n\n1️⃣ Перейди на сайт\n2️⃣ Пройди бесплатную диагностику\n3️⃣ Оплати тариф «План + AI + Челлендж» — 1 490 ₽\n4️⃣ Напиши /start и введи номер телефона",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
        return

    if state == STATE_AI_CHAT:
        if not has_active_access(str(user_id)):
            await send_message(str(user_id),
                "⛔ Доступ закончился. Оплати продление на сайте.",
                [[{"type": "link", "text": "🌐 Перейти на сайт", "url": SITE_API_URL}]])
            save_user_state(str(user_id), STATE_MENU, {})
            return
        
        save_chat_message(str(user_id), "user", text)
        
        report_text = await download_report_from_site(str(user_id), "premium")
        if not report_text:
            await send_message(str(user_id),
                "❌ Не удалось загрузить план. Обратись в поддержку.",
                get_error_keyboard())
            save_user_state(str(user_id), STATE_MENU, {})
            return
        
        hard_keywords = ["настрой", "сделай", "запусти", "воронку", "таргет", "внедрение", "помоги сделать", "напиши скрипт"]
        is_hard = any(keyword in text.lower() for keyword in hard_keywords)
        
        if is_hard:
            answer = "🔥 Это задача для профессионального внедрения.\n\nОставь заявку, я свяжусь с тобой."
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
            f"Телефон: {get_user_phone(str(user_id)) or 'не указан'}\n"
            f"Запрос: {text}\n"
            f"⏰ {format_moscow_time()}"
        )
        await send_message(str(user_id),
            "✅ Заявка принята! Я свяжусь с тобой в ближайшее время.",
            get_main_menu_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

# === СОЗДАНИЕ ПРИЛОЖЕНИЯ FASTAPI ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started (activation by phone)")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "7.0", "mode": "activation_by_phone"}

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

@app.get("/get_channel_id")
async def get_channel_id():
    url = f"https://platform-api.max.ru/channels"
    headers = {"Authorization": MAX_BOT_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            return data

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
