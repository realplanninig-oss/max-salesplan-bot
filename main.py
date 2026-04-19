# File: main.py — бот Salesplan для MAX (минимальный тестовый формат)

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
async def send_text_only(chat_id: str, text: str):
    """Отправка только текста - без кнопок"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    logger.info(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"Response: {resp.status} - {response_text}")
            return resp.status

async def send_with_reply_keyboard(chat_id: str, text: str, buttons: list):
    """Отправка с reply-клавиатурой (кнопки в поле ввода)"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {
        "text": text,
        "attachments": [
            {
                "type": "reply_keyboard",
                "payload": {
                    "buttons": buttons,
                    "resize": True,
                    "one_time": True
                }
            }
        ]
    }
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    logger.info(f"Payload with reply keyboard: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"Response: {resp.status} - {response_text}")
            return resp.status

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"Process message: {user_id} -> {text}")
    
    if text == "/start":
        # Пробуем отправить reply-клавиатуру (кнопки в поле ввода)
        buttons = [
            [{"text": "📊 Бесплатный аудит"}],
            [{"text": "🔥 План продаж за 490 ₽"}],
            [{"text": "👩‍💼 Бесплатная консультация"}],
            [{"text": "❓ Помощь"}]
        ]
        await send_with_reply_keyboard(
            str(user_id),
            "Привет! Я Вероника, продюсер экспертов.\n\n👇 Нажми на кнопку:",
            buttons
        )
    elif text == "📊 Бесплатный аудит":
        await send_text_only(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего бизнеса:")
    elif text == "🔥 План продаж за 490 ₽":
        await send_text_only(str(user_id), "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата.")
    elif text == "👩‍💼 Бесплатная консультация":
        await send_text_only(str(user_id), "Напиши в одном сообщении:\n🔗 Ссылку на бизнес\n👤 Твой username\n🕐 Удобное время")
    elif text == "❓ Помощь":
        await send_text_only(str(user_id), "Доступные команды:\n• 📊 Бесплатный аудит\n• 🔥 План продаж за 490 ₽\n• 👩‍💼 Бесплатная консультация\n\nНапиши /start для главного меню")
    else:
        await send_text_only(str(user_id), f"Ты написал: {text}\n\nИспользуй /start для начала.")

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

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
