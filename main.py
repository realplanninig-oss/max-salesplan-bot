# File: main.py — бот Salesplan для MAX (с РАБОЧИМИ reply-кнопками)

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

# === КОМАНДЫ ===
COMMAND_AUDIT = "📊 Бесплатный аудит"
COMMAND_PREMIUM = "🔥 План продаж за 490 ₽"
COMMAND_CONSULT = "👩‍💼 Бесплатная консультация"
COMMAND_HELP = "❓ Помощь"

# === ОТПРАВКА СООБЩЕНИЙ С КЛАВИАТУРОЙ ===
async def send_message_with_keyboard(chat_id: str, text: str, buttons: list):
    """Отправка сообщения с reply-клавиатурой"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {
        "text": text,
        "attachments": [
            {
                "type": "reply_keyboard",
                "payload": {
                    "buttons": buttons,
                    "resize": True,
                    "one_time": False
                }
            }
        ]
    }
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    logger.info(f"Sending with keyboard to {chat_id}")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            if resp.status != 200:
                logger.error(f"send_message failed: {resp.status} - {response_text}")
            return {"status": resp.status, "body": response_text}

async def send_simple_message(chat_id: str, text: str):
    """Отправка простого текстового сообщения без клавиатуры"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"send_simple_message failed: {resp.status} - {error_text}")
            return {"status": resp.status}

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"Process message: {user_id} -> {text}")
    
    if text == "/start":
        # Отправляем сообщение с клавиатурой
        buttons = [
            [{"text": COMMAND_AUDIT}],
            [{"text": COMMAND_PREMIUM}],
            [{"text": COMMAND_CONSULT}],
            [{"text": COMMAND_HELP}]
        ]
        await send_message_with_keyboard(
            str(user_id),
            "Привет! Я Вероника, продюсер экспертов.\n\n"
            "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
            "Давай сделаем бесплатный аудит твоего канала — 2 минуты, и узнаешь, что теряешь.\n\n"
            "👇 Нажми на кнопку:",
            buttons
        )
    elif text == COMMAND_AUDIT:
        await send_simple_message(str(user_id), 
            "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса:")
    elif text == COMMAND_PREMIUM:
        await send_simple_message(str(user_id), 
            "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата и генерация плана.")
    elif text == COMMAND_CONSULT:
        await send_simple_message(str(user_id), 
            "👩‍💼 Бесплатная консультация\n\nНапиши в одном сообщении:\n"
            "🔗 Ссылку на твой бизнес\n"
            "👤 Твой username\n"
            "🕐 Удобное время для звонка")
    elif text == COMMAND_HELP:
        buttons = [
            [{"text": COMMAND_AUDIT}],
            [{"text": COMMAND_PREMIUM}],
            [{"text": COMMAND_CONSULT}]
        ]
        await send_message_with_keyboard(
            str(user_id),
            "❓ Помощь\n\nДоступные команды:\n"
            "• 📊 Бесплатный аудит\n"
            "• 🔥 План продаж за 490 ₽\n"
            "• 👩‍💼 Бесплатная консультация\n\n"
            "👇 Выбери действие:",
            buttons
        )
    else:
        await send_simple_message(str(user_id), f"Ты написал: {text}\n\nИспользуй /start для начала.")

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
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {"status": "MAX Bot is running", "version": "3.0"}

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

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
