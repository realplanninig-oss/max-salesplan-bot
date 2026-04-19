# File: main.py — бот Salesplan для MAX (на основе реальных рабочих примеров)

import asyncio
import logging
import sqlite3
import os
import json
import requests
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
import aiohttp
import aiofiles
import uvicorn

load_dotenv()

# === ДИАГНОСТИКА ===
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
print(f"PORT: {os.getenv('PORT', '8000')}")
print("=" * 60)

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not MAX_BOT_TOKEN:
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found")

MAX_API_URL = "https://platform-api.max.ru"

LOGS_DIR = Path("./logs")
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOGS_DIR / "bot.log", encoding='utf-8'),
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

# === ВОПРОСЫ ОПРОСНИКА ===
SURVEY_QUESTIONS = [
    {"key": "q1", "text": "Что ты продаёшь?", "options": ["Услугу", "Инфопродукт", "Консультацию", "Пока не продаю"]},
    {"key": "q2", "text": "Средний чек (₽)", "options": ["<5k", "5k-20k", "20k-50k", ">50k"]},
    {"key": "q3", "text": "Клиентов в месяц", "options": ["<10", "10-50", "50-200", ">200"]},
    {"key": "q4", "text": "Цель на 2026", "options": ["300k/мес", "500k/мес", "1M/мес", "Масштаб"]},
    {"key": "q5", "text": "Есть автоворонка?", "options": ["Да", "Нет", "В разработке"]},
]

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
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
        CREATE TABLE IF NOT EXISTS consultations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            message TEXT,
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
    cursor = conn.execute("SELECT business_name, business_description FROM business_data WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return {"name": row[0], "description": row[1]} if row else None

def save_form(user_id: str, answers: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO forms (user_id, q1, q2, q3, q4, q5)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, answers.get("q1"), answers.get("q2"), answers.get("q3"),
          answers.get("q4"), answers.get("q5")))
    conn.commit()
    conn.close()

def save_consultation(user_id: str, message: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO consultations (user_id, message) VALUES (?, ?)", (user_id, message))
    conn.commit()
    conn.close()

# === ОТПРАВКА СООБЩЕНИЙ ===
async def send_message(chat_id: str, text: str, buttons: list = None):
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    
    if buttons:
        payload["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": buttons
                }
            }
        ]
    
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"Send failed: {resp.status} - {error_text}")
            return resp.status

async def send_simple_message(chat_id: str, text: str):
    return await send_message(chat_id, text, None)

async def answer_callback(callback_id: str, text: str):
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"Answer callback failed: {resp.status}")
            return resp.status

async def send_file_message(chat_id: str, text: str, file_path: str):
    # Загрузка файла
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MAX_API_URL}/uploads?type=file",
            headers={"Authorization": MAX_BOT_TOKEN}
        ) as resp:
            if resp.status != 200:
                await send_simple_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
                return
            data = await resp.json()
            upload_url = data.get("url")
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            form_data = aiohttp.FormData()
            form_data.add_field('data', f, filename=Path(file_path).name)
            async with session.post(upload_url, data=form_data) as resp:
                if resp.status != 200:
                    await send_simple_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
                    return
                result = await resp.json()
                token = result.get("token")
    
    # Отправка файла
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {
        "text": text,
        "attachments": [{"type": "file", "payload": {"token": token}}]
    }
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            logger.info(f"File sent: {resp.status}")

# === КНОПКИ (ПРАВИЛЬНЫЙ ФОРМАТ ИЗ РЕАЛЬНЫХ ДАННЫХ) ===
def get_main_menu_buttons():
    """Формат кнопок из реальных рабочих примеров MAX API"""
    return [
        [
            {
                "payload": "audit",
                "text": "📊 Бесплатный аудит",
                "intent": "default",
                "type": "callback"
            }
        ],
        [
            {
                "payload": "premium",
                "text": "🔥 План продаж за 490 ₽",
                "intent": "default",
                "type": "callback"
            }
        ],
        [
            {
                "payload": "consult",
                "text": "👩‍💼 Бесплатная консультация",
                "intent": "default",
                "type": "callback"
            }
        ],
        [
            {
                "payload": "help",
                "text": "❓ Помощь",
                "intent": "default",
                "type": "callback"
            }
        ]
    ]

def get_survey_buttons(step: int):
    if step >= len(SURVEY_QUESTIONS):
        return None
    buttons = []
    for option in SURVEY_QUESTIONS[step]["options"]:
        buttons.append([
            {
                "payload": f"survey_{step}_{option}",
                "text": option,
                "intent": "default",
                "type": "callback"
            }
        ])
    return buttons

# === DEEPSEEK API ===
async def call_deepseek_diagnostic(name: str, description: str, answers: dict):
    if not DEEPSEEK_API_KEY:
        return None
    
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {answers.get('q1', '-')}
• Средний чек: {answers.get('q2', '-')}
• Клиентов/мес: {answers.get('q3', '-')}
• Цель: {answers.get('q4', '-')}
• Автоворонка: {answers.get('q5', '-')}
"""
    prompt = f"""Сделай профессиональный маркетинговый разбор онлайн-бизнеса.

ДАННЫЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши отчет в разговорном стиле Вероники. Не используй *, #, _. Для списков используй дефисы.

1. ОБЩАЯ ИНФОРМАЦИЯ (ниша, ЦА, оценка)
2. АНАЛИЗ (3 сильные стороны, 3 зоны роста)
3. РЕКОМЕНДАЦИИ (3 конкретных шага)"""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов. Говоришь разговорно, с эмодзи, на 'ты'."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        return None
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

# === ОБРАБОТЧИК ===
async def process_callback(user_id: str, callback_id: str, payload: str):
    logger.info(f"🔘 Callback: user={user_id}, payload={payload}")
    
    if payload == "audit":
        await answer_callback(callback_id, "✅ Начинаем аудит!")
        save_user_state(str(user_id), "awaiting_business_name", {"answers": {}, "survey_step": 0})
        await send_simple_message(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса:")
    
    elif payload == "premium":
        await answer_callback(callback_id, "🔥 План продаж")
        biz = get_business_data(str(user_id))
        form = get_form(str(user_id))
        if not biz or not form:
            await send_simple_message(str(user_id), "Сначала нужно пройти бесплатный аудит! Нажми /start")
        else:
            await send_simple_message(str(user_id), "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата через ЮKassa.")
    
    elif payload == "consult":
        await answer_callback(callback_id, "👩‍💼 Консультация")
        save_user_state(str(user_id), "waiting_call", {})
        await send_simple_message(str(user_id), "Напиши в одном сообщении:\n🔗 Ссылку на бизнес\n👤 Твой username\n🕐 Удобное время для звонка")
    
    elif payload == "help":
        await answer_callback(callback_id, "❓ Помощь")
        await send_message(str(user_id), 
            "Доступные команды:\n• Бесплатный аудит\n• План продаж\n• Консультация\n\nНапиши /start",
            get_main_menu_buttons())
    
    elif payload.startswith("survey_"):
        parts = payload.split("_")
        if len(parts) == 3:
            step = int(parts[1])
            answer = parts[2]
            await answer_callback(callback_id, f"✅ Выбрано: {answer}")
            
            state, data = get_user_state(str(user_id))
            if state == "survey":
                key = SURVEY_QUESTIONS[step]["key"]
                answers = data.get("answers", {})
                answers[key] = answer
                data["answers"] = answers
                data["survey_step"] = step + 1
                save_user_state(str(user_id), "survey", data)
                
                if step + 1 < len(SURVEY_QUESTIONS):
                    q = SURVEY_QUESTIONS[step + 1]
                    await send_message(str(user_id), q["text"], get_survey_buttons(step + 1))
                else:
                    save_form(str(user_id), answers)
                    biz = get_business_data(str(user_id))
                    await send_simple_message(str(user_id), "🔍 Запускаю диагностику... 1-2 минуты")
                    report = await call_deepseek_diagnostic(biz["name"], biz["description"], answers)
                    if report:
                        save_user_state(str(user_id), STATE_MENU, {})
                        filename = f"diagnostic_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                        filepath = REPORTS_DIR / filename
                        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                            await f.write(report)
                        await send_file_message(str(user_id), "✅ Твоя диагностика готова!", str(filepath))
                        await send_message(str(user_id), 
                            "🔥 Ну как тебе? Хочешь полный план продаж за 490 ₽? Нажми /start",
                            get_main_menu_buttons())
                    else:
                        await send_simple_message(str(user_id), "⚠️ Ошибка генерации. Попробуй позже /start")
    
    else:
        await answer_callback(callback_id, "❌ Неизвестная команда")

async def process_message(user_id: str, text: str):
    logger.info(f"📨 Message: {user_id} -> {text}")
    state, data = get_user_state(str(user_id))
    
    if text == "/start":
        save_user_state(str(user_id), STATE_MENU, {})
        await send_message(str(user_id), 
            "Привет! Я Вероника, продюсер экспертов.\n\n"
            "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
            "👇 Нажми на кнопку:",
            get_main_menu_buttons())
        return
    
    if state == "awaiting_business_name":
        save_user_state(str(user_id), "awaiting_business_description", {"business_name": text})
        await send_simple_message(str(user_id), "Отлично! Теперь напиши краткое описание бизнеса:")
        return
    
    if state == "awaiting_business_description":
        business_name = data.get("business_name")
        save_business_data(str(user_id), business_name, text)
        save_user_state(str(user_id), "survey", {"answers": {}, "survey_step": 0})
        q = SURVEY_QUESTIONS[0]
        await send_message(str(user_id), q["text"], get_survey_buttons(0))
        return
    
    if state == "waiting_call":
        save_consultation(str(user_id), text)
        save_user_state(str(user_id), STATE_MENU, {})
        if ADMIN_CHAT_ID:
            await send_simple_message(ADMIN_CHAT_ID, f"📞 НОВАЯ ЗАЯВКА НА КОНСУЛЬТАЦИЮ!\n\nПользователь: {user_id}\nСообщение: {text}")
        await send_message(str(user_id), 
            "✅ Заявка принята! Я свяжусь с тобой в ближайшее время.\n\n"
            "А пока подпишись на мой канал: https://max.ru/id781407988795_biz",
            get_main_menu_buttons())
        return
    
    if state == STATE_MENU:
        await send_message(str(user_id), "Используй /start или нажми на кнопку", get_main_menu_buttons())
    else:
        await send_message(str(user_id), "Напиши /start для начала", get_main_menu_buttons())

# === ЗАПУСК ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Bot started")
    yield
    logger.info("🛑 Bot stopped")

app = FastAPI(title="MAX Bot", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        # Обработка обычного сообщения
        if "message" in body and "callback" not in body:
            msg = body["message"]
            user_id = msg.get("sender", {}).get("user_id")
            text = msg.get("body", {}).get("text")
            if user_id and text:
                await process_message(str(user_id), text)
        
        # Обработка callback от кнопки (update_type: message_callback)
        elif "callback" in body:
            cb = body["callback"]
            user_id = cb.get("user", {}).get("id")
            callback_id = cb.get("callback_id")
            payload = cb.get("payload")  # ключевое поле!
            logger.info(f"🔘 Callback received: user={user_id}, payload={payload}")
            if user_id and callback_id and payload:
                await process_callback(str(user_id), str(callback_id), payload)

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
