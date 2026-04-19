# File: main.py — бот Salesplan для MAX (ПРАВИЛЬНЫЙ ФОРМАТ ИЗ GO-БИБЛИОТЕКИ)

import asyncio
import logging
import os
import json
import traceback
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
import aiohttp
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
print(f"PORT: {os.getenv('PORT', '8000')}")
print("=" * 60)

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
if not MAX_BOT_TOKEN:
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found")

MAX_API_URL = "https://platform-api.max.ru"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
    
    logger.info(f"Sending to {chat_id}")
    logger.info(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"Response: {resp.status} - {response_text}")
            return resp.status

async def answer_callback(callback_id: str, text: str):
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            logger.info(f"Answer callback: {resp.status}")
            return resp.status

# === ПРАВИЛЬНЫЙ ФОРМАТ КНОПОК (ИЗ GO-БИБЛИОТЕКИ MAX) ===
def get_main_menu_buttons():
    """Формат кнопок из официальной Go-библиотеки MAX"""
    return [
        [
            {
                "type": "callback",
                "text": "📊 Бесплатный аудит",
                "payload": "audit",
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "🔥 План продаж за 490 ₽",
                "payload": "premium",
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "👩‍💼 Бесплатная консультация",
                "payload": "consult",
                "intent": "default"
            }
        ],
        [
            {
                "type": "callback",
                "text": "❓ Помощь",
                "payload": "help",
                "intent": "default"
            }
        ]
    ]

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"Process message: {user_id} -> {text}")
    
    if text == "/start":
        await send_message(
            str(user_id),
            "Привет! Я Вероника, продюсер экспертов.\n\n👇 Нажми на кнопку:",
            get_main_menu_buttons()
        )
    else:
        await send_message(str(user_id), f"Ты написал: {text}\n\nИспользуй /start для начала.")

async def process_callback(user_id: str, callback_id: str, payload: str):
    logger.info(f"Process callback: user={user_id}, payload={payload}")
    
    if payload == "audit":
        await answer_callback(callback_id, "✅ Начинаем аудит!")
        await send_message(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего бизнеса:")
    elif payload == "premium":
        await answer_callback(callback_id, "🔥 План продаж")
        await send_message(str(user_id), "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата.")
    elif payload == "consult":
        await answer_callback(callback_id, "👩‍💼 Консультация")
        await send_message(str(user_id), "Напиши в одном сообщении:\n🔗 Ссылку на бизнес\n👤 Твой username\n🕐 Удобное время")
    elif payload == "help":
        await answer_callback(callback_id, "❓ Помощь")
        await send_message(str(user_id), "Доступные команды:\n• Бесплатный аудит\n• План продаж\n• Консультация\n\nНапиши /start", get_main_menu_buttons())
    else:
        await answer_callback(callback_id, "❌ Неизвестная команда")

# === ЗАПУСК ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bot started")
    yield
    logger.info("Bot stopped")

app = FastAPI(title="MAX Bot", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        
        if "message" in payload:
            msg = payload["message"]
            user_id = msg.get("sender", {}).get("user_id")
            body = msg.get("body", {})
            text = body.get("text")
            if user_id and text:
                await process_message(str(user_id), text)
        
        elif "callback_query" in payload:
            cb = payload["callback_query"]
            user_id = cb.get("user", {}).get("id")
            callback_id = cb.get("callback_id")
            # Правильный путь: payload находится на верхнем уровне
            payload_data = cb.get("payload")
            logger.info(f"Callback: user={user_id}, payload={payload_data}")
            if user_id and callback_id and payload_data:
                await process_callback(str(user_id), str(callback_id), payload_data)

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
# File: main.py — бот Salesplan для MAX (с полным логированием callback)

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

# === БАЗА ДАННЫХ ===
DB_PATH = "salesplan.db"
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(exist_ok=True)

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
    return "menu", {}

def save_user_state(user_id: str, state: str, data: dict = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO user_state (user_id, state, data, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, state, json.dumps(data or {}, ensure_ascii=False)))
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
    
    logger.info(f"📤 Sending to {chat_id}")
    logger.info(f"📤 Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"📥 Response: {resp.status}")
            if resp.status != 200:
                logger.error(f"Error: {response_text}")
            return resp.status

async def answer_callback(callback_id: str, text: str):
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            logger.info(f"📥 Answer callback response: {resp.status}")
            return resp.status

# === КНОПКИ ===
def get_main_menu_buttons():
    return [
        [{"type": "callback", "text": "📊 Бесплатный аудит", "payload": "audit", "intent": "default"}],
        [{"type": "callback", "text": "🔥 План продаж за 490 ₽", "payload": "premium", "intent": "default"}],
        [{"type": "callback", "text": "👩‍💼 Бесплатная консультация", "payload": "consult", "intent": "default"}],
        [{"type": "callback", "text": "❓ Помощь", "payload": "help", "intent": "default"}]
    ]

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"📨 Process message: {user_id} -> {text}")
    state, data = get_user_state(str(user_id))
    
    if text == "/start":
        save_user_state(str(user_id), "menu", {})
        await send_message(
            str(user_id),
            "Привет! Я Вероника, продюсер экспертов.\n\n👇 Нажми на кнопку:",
            get_main_menu_buttons()
        )
        return
    
    if state == "menu":
        if text == "📊 Бесплатный аудит":
            save_user_state(str(user_id), "awaiting_business_name", {"answers": {}})
            await send_message(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса:")
        else:
            await send_message(str(user_id), f"Используй /start для начала.")
        return
    
    if state == "awaiting_business_name":
        save_user_state(str(user_id), "awaiting_business_description", {"business_name": text})
        await send_message(str(user_id), "Теперь напиши краткое описание бизнеса:")
        return
    
    if state == "awaiting_business_description":
        logger.info(f"Business data saved: {data.get('business_name')} -> {text}")
        save_user_state(str(user_id), "menu", {})
        await send_message(str(user_id), f"✅ Спасибо! Твой бизнес: {data.get('business_name')}\n\nОписание: {text}\n\nСкоро здесь будет диагностика от DeepSeek!")
        return

async def process_callback(user_id: str, callback_id: str, payload: str):
    logger.info(f"🔘 CALLBACK RECEIVED! user={user_id}, payload={payload}")
    
    if payload == "audit":
        logger.info("✅ Audit button pressed!")
        await answer_callback(callback_id, "✅ Начинаем аудит!")
        save_user_state(str(user_id), "awaiting_business_name", {"answers": {}})
        await send_message(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса:")
    elif payload == "premium":
        await answer_callback(callback_id, "🔥 План продаж")
        await send_message(str(user_id), "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата.")
    elif payload == "consult":
        await answer_callback(callback_id, "👩‍💼 Консультация")
        await send_message(str(user_id), "Напиши в одном сообщении:\n🔗 Ссылку на бизнес\n👤 Твой username\n🕐 Удобное время")
    elif payload == "help":
        await answer_callback(callback_id, "❓ Помощь")
        await send_message(str(user_id), "Доступные команды:\n• Бесплатный аудит\n• План продаж\n• Консультация\n\nНапиши /start", get_main_menu_buttons())
    else:
        await answer_callback(callback_id, "❌ Неизвестная команда")

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
    return {"status": "alive"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        logger.info(f"📥 FULL WEBHOOK BODY: {json.dumps(body, ensure_ascii=False)}")
        
        if "message" in body:
            msg = body["message"]
            user_id = msg.get("sender", {}).get("user_id")
            text = msg.get("body", {}).get("text")
            if user_id and text:
                await process_message(str(user_id), text)
        
        elif "callback_query" in body:
            cb = body["callback_query"]
            user_id = cb.get("user", {}).get("id")
            callback_id = cb.get("callback_id")
            payload = cb.get("payload")
            logger.info(f"🔘 CALLBACK: user={user_id}, callback_id={callback_id}, payload={payload}")
            if user_id and callback_id and payload:
                await process_callback(str(user_id), str(callback_id), payload)
            else:
                logger.warning(f"⚠️ Invalid callback: {cb}")

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"❌ Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
