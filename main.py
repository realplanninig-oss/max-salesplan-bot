# File: main.py — бот Salesplan для MAX (ПРАВИЛЬНЫЙ формат inline-кнопок)

import asyncio
import logging
import os
import json
import traceback
from datetime import datetime
from pathlib import Path

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
    """Отправка сообщения с inline-клавиатурой (правильный формат)"""
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
    
    logger.info(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"Response: {resp.status} - {response_text}")
            return resp.status

async def answer_callback(callback_id: str, text: str):
    """Ответ на callback запрос"""
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            logger.info(f"Answer callback response: {resp.status}")
            return resp.status

# === ПРАВИЛЬНЫЙ ФОРМАТ КНОПОК (по документации MAX) ===
def get_main_menu_buttons():
    """Главное меню - правильный формат для MAX API"""
    return [
        [
            {
                "text": "📊 Бесплатный аудит",
                "callback": {
                    "text": "📊 Бесплатный аудит",
                    "payload": "audit"
                }
            },
            {
                "text": "🔥 План продаж за 490 ₽",
                "callback": {
                    "text": "🔥 План продаж за 490 ₽",
                    "payload": "premium"
                }
            }
        ],
        [
            {
                "text": "👩‍💼 Бесплатная консультация",
                "callback": {
                    "text": "👩‍💼 Бесплатная консультация",
                    "payload": "consult"
                }
            },
            {
                "text": "❓ Помощь",
                "callback": {
                    "text": "❓ Помощь",
                    "payload": "help"
                }
            }
        ]
    ]

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"Process message: {user_id} -> {text}")
    
    if text == "/start":
        await send_message(
            str(user_id),
            "Привет! Я Вероника, продюсер экспертов.\n\n"
            "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
            "👇 Нажми на кнопку:",
            get_main_menu_buttons()
        )
    else:
        await send_message(str(user_id), f"Ты написал: {text}\n\nИспользуй /start для начала.")

async def process_callback(user_id: str, callback_id: str, payload: str):
    """Обработка нажатия на кнопку"""
    logger.info(f"Process callback: user={user_id}, payload={payload}")
    
    if payload == "audit":
        await answer_callback(callback_id, "✅ Начинаем аудит!")
        await send_message(str(user_id), "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса:")
    elif payload == "premium":
        await answer_callback(callback_id, "🔥 План продаж")
        await send_message(str(user_id), "🔥 План продаж за 490 ₽\n\nСкоро здесь будет оплата.")
    elif payload == "consult":
        await answer_callback(callback_id, "👩‍💼 Консультация")
        await send_message(str(user_id), "Напиши в одном сообщении:\n🔗 Ссылку на бизнес\n👤 Твой username\n🕐 Удобное время")
    elif payload == "help":
        await answer_callback(callback_id, "❓ Помощь")
        await send_message(
            str(user_id),
            "Доступные команды:\n• Бесплатный аудит\n• План продаж\n• Консультация",
            get_main_menu_buttons()
        )
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
        logger.info(f"Webhook received")
        
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
            # Правильный путь: callback.payload
            callback_obj = cb.get("callback", {})
            payload_data = callback_obj.get("payload")
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
