# File: main.py — максимально упрощенная версия (без клавиатур)

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
print("ENVIRONMENT VARIABLES CHECK - MAX Bot (NO KEYBOARD)")
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

# === ОТПРАВКА СООБЩЕНИЙ (БЕЗ КЛАВИАТУР) ===
async def send_message(chat_id: str, text: str):
    """Отправка простого текстового сообщения - БЕЗ КЛАВИАТУРЫ"""
    url = f"{MAX_API_URL}/messages?user_id={chat_id}"
    payload = {"text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    
    logger.info(f"Sending to {url}")
    logger.info(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            response_text = await resp.text()
            logger.info(f"Response status: {resp.status}")
            logger.info(f"Response body: {response_text}")
            
            if resp.status != 200:
                logger.error(f"send_message failed: {resp.status} - {response_text}")
            return {"status": resp.status, "body": response_text}

# === ОБРАБОТЧИК ===
async def process_message(user_id: str, text: str):
    logger.info(f"Process message: {user_id} -> {text}")
    
    if text == "/start":
        await send_message(str(user_id), 
            "Привет! Я Вероника, продюсер экспертов.\n\n"
            "Я пока не умею показывать кнопки, но базовая отправка работает!\n\n"
            "Проблема с клавиатурами решается. Подожди немного, я доделываю бота.")
    else:
        await send_message(str(user_id), f"Ты написал: {text}\n\nСкоро здесь будут кнопки!")

# === ЗАПУСК ===
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bot started (no keyboard version)")
    yield
    logger.info("Bot stopped")

app = FastAPI(title="MAX Bot No Keyboard", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {"status": "MAX Bot is running", "version": "no-keyboard"}

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

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
