# File: main.py — бот Salesplan для MAX

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
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, HTTPException
import aiohttp
import aiofiles

load_dotenv()

# === КОНФИГУРАЦИЯ ===
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
YKASSA_SHOP_ID = os.getenv("YKASSA_SHOP_ID", "test")
YKASSA_SECRET_KEY = os.getenv("YKASSA_SECRET_KEY", "test")
YKASSA_TEST_MODE = os.getenv("YKASSA_TEST_MODE", "true").lower() == "true"

MAX_API_URL = "https://platform-api.max.ru"
YKASSA_API_URL = "https://api.yookassa.ru/v3"
PAYMENT_URL = "https://yookassa.ru/my/i/adO_-KVsYKuY/l"

if not MAX_BOT_TOKEN:
    raise RuntimeError("ERROR: MAX_BOT_TOKEN not found in .env")

LOGS_DIR = Path("./logs")
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOGS_DIR / "salesplan.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = "salesplan.db"
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(exist_ok=True)

STATE_MENU = "menu"
STATE_AWAITING_BUSINESS_NAME = "awaiting_business_name"
STATE_AWAITING_BUSINESS_DESCRIPTION = "awaiting_business_description"
STATE_SURVEY = "survey"
STATE_WAITING_CALL = "waiting_call"

CALLBACK_START_AUDIT = "start_audit"
CALLBACK_MY_PREMIUM = "my_premium"
CALLBACK_I_PAID = "i_paid"
CALLBACK_BOOK_CALL = "book_call"
CALLBACK_DOWNLOAD_REPORT = "download_report"
CALLBACK_SEND_AS_TEXT = "send_as_text"
CALLBACK_SEND_AS_FILE = "send_as_file"
CALLBACK_HELP = "help"

Q1_SERVICE = "q1_service"
Q1_INFO = "q1_info"
Q1_CONSULT = "q1_consult"
Q1_NONE = "q1_none"
Q2_LT5 = "q2_lt5"
Q2_5_20 = "q2_5_20"
Q2_20_50 = "q2_20_50"
Q2_50P = "q2_50p"
Q3_LT1K = "q3_lt1k"
Q3_1_5K = "q3_1_5k"
Q3_5_20K = "q3_5_20k"
Q3_20KP = "q3_20kp"
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
        (Q2_LT5, "<5k"),
        (Q2_5_20, "5k-20k"),
        (Q2_20_50, "20k-50k"),
        (Q2_50P, ">50k"),
    ]},
    {"key": "q3", "text": "Клиентов/мес (примерно)", "options": [
        (Q3_LT1K, "<10"),
        (Q3_1_5K, "10-50"),
        (Q3_5_20K, "50-200"),
        (Q3_20KP, ">200"),
    ]},
    {"key": "q4", "text": "Цель на 2026", "options": [
        (Q4_300, "300k/мес"),
        (Q4_500, "500k/мес"),
        (Q4_1M, "1M/мес"),
        (Q4_SCALE, "Масштаб"),
    ]},
    {"key": "q5", "text": "Уже есть автоворонка?", "options": [
        (Q5_YES, "Да"),
        (Q5_NO, "Нет"),
        (Q5_PROGRESS, "В разработке"),
    ]},
]

def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def format_moscow_time(dt=None):
    if dt is None:
        dt = get_moscow_time()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            payment_id TEXT UNIQUE,
            amount INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            user_id TEXT PRIMARY KEY,
            payment_id TEXT,
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
    cursor = conn.execute(
        "SELECT business_name, business_description FROM business_data WHERE user_id = ?",
        (user_id,)
    )
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
    cursor = conn.execute(
        "SELECT q1, q2, q3, q4, q5 FROM forms WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"q1": row[0], "q2": row[1], "q3": row[2], "q4": row[3], "q5": row[4]}
    return None

def save_report_request(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        INSERT INTO reports (user_id, report_type, status)
        VALUES (?, 'premium', 'generating')
    """, (user_id,))
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return report_id

def update_report_status(report_id: int, status: str, file_path: str = None):
    conn = sqlite3.connect(DB_PATH)
    if status == 'ready':
        conn.execute("""
            UPDATE reports SET status = ?, file_path = ?, ready_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (status, file_path, report_id))
    else:
        conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
    conn.commit()
    conn.close()

def get_report_status(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT id, status, file_path, ready_at FROM reports
        WHERE user_id = ? AND report_type = 'premium'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "status": row[1], "file_path": row[2], "ready_at": row[3]}
    return None

def save_pending_payment(user_id: str, payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO pending_payments (user_id, payment_id, created_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (user_id, payment_id))
    conn.commit()
    conn.close()

def get_pending_payment(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT payment_id FROM pending_payments WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def clear_pending_payment(user_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending_payments WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def log_event(user_id: str, event_type: str, event_data: str = None):
    logger.info(f"Event: {event_type} | User: {user_id} | Data: {event_data}")

async def send_message(chat_id: str, text: str, keyboard: list = None):
    url = f"{MAX_API_URL}/messages"
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"send_message failed: {await resp.text()}")
            return await resp.json()

async def send_callback_answer(callback_id: str, text: str, keyboard: list = None):
    """Отправка ответа на callback через POST /answers"""
    url = f"{MAX_API_URL}/answers?callback_id={callback_id}"
    payload = {"message": {"text": text}}
    if keyboard:
        payload["message"]["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": keyboard}}]
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"send_callback_answer failed: {await resp.text()}")
            return await resp.json()

async def send_notification(chat_id: str, text: str):
    """Отправка одноразового уведомления пользователю"""
    url = f"{MAX_API_URL}/messages"
    payload = {"chat_id": chat_id, "text": text}
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"send_notification failed: {await resp.text()}")
            return await resp.json()

async def upload_file_to_max(file_path: str, file_type: str = "file"):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MAX_API_URL}/uploads?type={file_type}",
            headers={"Authorization": MAX_BOT_TOKEN}
        ) as resp:
            if resp.status != 200:
                logger.error(f"Failed to get upload URL: {await resp.text()}")
                return None
            data = await resp.json()
            upload_url = data.get("url")
            if not upload_url:
                return None
    
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            form_data = aiohttp.FormData()
            form_data.add_field('data', f, filename=Path(file_path).name)
            async with session.post(upload_url, data=form_data) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to upload file: {await resp.text()}")
                    return None
                result = await resp.json()
                if "token" in result:
                    return {"type": file_type, "payload": {"token": result["token"]}}
                elif "result" in result and "url" in result["result"]:
                    return {"type": file_type, "payload": {"url": result["result"]["url"]}}
                else:
                    return {"type": file_type, "payload": result}

async def send_file_message(chat_id: str, text: str, file_path: str, file_type: str = "file"):
    attachment = await upload_file_to_max(file_path, file_type)
    if not attachment:
        await send_message(chat_id, f"{text}\n\n❌ Файл временно недоступен")
        return
    url = f"{MAX_API_URL}/messages"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "attachments": [attachment]
    }
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers=headers
        ) as resp:
            if resp.status != 200:
                logger.error(f"Failed to send message with attachment: {await resp.text()}")
                await send_message(chat_id, f"{text}\n\n❌ Не удалось отправить файл")
            return await resp.json()

async def create_yookassa_payment(amount: int, description: str, user_id: str):
    payment_id = f"salesplan_{user_id}_{uuid.uuid4().hex[:8]}"
    payload = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": "https://max.ru"},
        "description": description,
        "capture": True,
        "metadata": {"user_id": user_id, "payment_id": payment_id}
    }
    auth = aiohttp.BasicAuth(YKASSA_SHOP_ID, YKASSA_SECRET_KEY)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{YKASSA_API_URL}/payments",
            json=payload,
            auth=auth,
            headers={"Idempotence-Key": payment_id}
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return {"payment_id": data["id"], "confirmation_url": data["confirmation"]["confirmation_url"]}
            else:
                error_text = await resp.text()
                logger.error(f"YooKassa error: {resp.status} - {error_text}")
                return None

async def check_yookassa_payment(payment_id: str):
    auth = aiohttp.BasicAuth(YKASSA_SHOP_ID, YKASSA_SECRET_KEY)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{YKASSA_API_URL}/payments/{payment_id}",
            auth=auth
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("status")
            else:
                logger.error(f"Failed to check payment: {await resp.text()}")
                return None

def get_main_menu_keyboard():
    return [[[{"text": "📊 Бесплатный аудит", "callback_data": CALLBACK_START_AUDIT}]]]

def get_after_diagnostic_keyboard():
    return [
        [[{"text": "🔥 План продаж за 490 ₽", "callback_data": CALLBACK_MY_PREMIUM}]],
        [[{"text": "👩‍💼 Бесплатная консультация", "callback_data": CALLBACK_BOOK_CALL}]]
    ]

def get_survey_keyboard(question_index: int):
    if question_index >= len(SURVEY_QUESTIONS):
        return None
    q = SURVEY_QUESTIONS[question_index]
    keyboard = []
    for callback_data, label in q["options"]:
        keyboard.append([{"text": label, "callback_data": callback_data}])
    return keyboard

def get_format_choice_keyboard():
    return [
        [[{"text": "📝 В сообщении", "callback_data": CALLBACK_SEND_AS_TEXT}]],
        [[{"text": "📄 В файле .txt", "callback_data": CALLBACK_SEND_AS_FILE}]]
    ]

def get_consultation_keyboard():
    return [[[{"text": "📝 Оставить заявку", "callback_data": CALLBACK_BOOK_CALL}]]]

def get_post_download_keyboard():
    return [
        [[{"text": "👩‍💼 Разобрать план (30 мин)", "callback_data": CALLBACK_BOOK_CALL}]],
        [[{"text": "📚 Получить мини-курс", "url": "https://t.me/zapuskintelega_bot"}]]
    ]

def get_channel_subscribe_keyboard():
    return [[[{"text": "📢 Подписаться на канал", "url": "https://max.ru/id781407988795_biz"}]]]

def get_payment_keyboard(confirmation_url: str):
    return [
        [{"text": f"💳 Оплатить 490 ₽", "url": confirmation_url}],
        [{"text": "✅ Я оплатил(а)", "callback_data": CALLBACK_I_PAID}],
        [{"text": "❓ Помощь", "callback_data": CALLBACK_HELP}]
    ]

async def call_deepseek_diagnostic(name: str, description: str, answers: dict):
    q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
    q2_map = {Q2_LT5: "до 5k", Q2_5_20: "5k-20k", Q2_20_50: "20k-50k", Q2_50P: ">50k"}
    q3_map = {Q3_LT1K: "<10 клиентов", Q3_1_5K: "10-50", Q3_5_20K: "50-200", Q3_20KP: ">200"}
    q4_map = {Q4_300: "300k/мес", Q4_500: "500k/мес", Q4_1M: "1M/мес", Q4_SCALE: "Масштаб"}
    q5_map = {Q5_YES: "да", Q5_NO: "нет", Q5_PROGRESS: "в разработке"}
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {q1_map.get(answers.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(answers.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(answers.get('q3'), 'не указано')}
• Цель на 2026: {q4_map.get(answers.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(answers.get('q5'), 'не указано')}
"""
    prompt = f"""Сделай профессиональный маркетинговый разбор онлайн-бизнеса на основе предоставленных данных.

ДАННЫЕ О БИЗНЕСЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши структурированный отчет на русском языке в разговорном стиле, включив:

1. ОБЩАЯ ИНФОРМАЦИЯ
   - Ниша бизнеса
   - Целевая аудитория (кто, их главная боль, какое решение ищут)
   - Оценка текущего уровня от 0 до 100

2. АНАЛИЗ
   - 3 сильные стороны
   - 3 зоны роста

3. ПЕРСОНАЛЬНЫЕ РЕКОМЕНДАЦИИ
   - 3 конкретных шага для увеличения продаж

ВАЖНО:
- Пиши как Вероника, продюсер экспертов. Живо, с эмодзи, с обращением на "ты"
- Не используй символы *, #, _ для форматирования
- Для списков используй дефисы (-)
- В разделе "Целевая аудитория" обязательно опиши: кто это, их главная проблема, какое решение ищут
- В третьем разделе обязательно дай рекомендацию по настройке простой воронки продаж"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов с 8-летним опытом. Говоришь разговорно, с эмодзи, на 'ты'."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek error: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

async def generate_premium_report(user_id: str, name: str, description: str, answers: dict, report_id: int):
    logger.info(f"Generating premium report for {user_id}")
    q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
    q2_map = {Q2_LT5: "до 5k", Q2_5_20: "5k-20k", Q2_20_50: "20k-50k", Q2_50P: ">50k"}
    q3_map = {Q3_LT1K: "<10", Q3_1_5K: "10-50", Q3_5_20K: "50-200", Q3_20KP: ">200"}
    q4_map = {Q4_300: "300k/мес", Q4_500: "500k/мес", Q4_1M: "1M/мес", Q4_SCALE: "Масштаб"}
    q5_map = {Q5_YES: "да", Q5_NO: "нет", Q5_PROGRESS: "в разработке"}
    survey_info = f"""
ДАННЫЕ О БИЗНЕСЕ:
• Продаёт: {q1_map.get(answers.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(answers.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(answers.get('q3'), 'не указано')}
• Цель: {q4_map.get(answers.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(answers.get('q5'), 'не указано')}
"""
    prompt = f"""Сделай план продаж для онлайн-бизнеса.

ДАННЫЕ:
Название: {name}
Описание: {description}
{survey_info}

Напиши структурированный план на русском языке в разговорном стиле Вероники:

1. ОЦЕНКА СИТУАЦИИ
2. АНАЛИЗ РЫНКА И КОНКУРЕНТОВ (3-5 игроков)
3. КОМУ ПРОДАВАТЬ (ЦА)
4. СИЛЬНЫЕ И СЛАБЫЕ СТОРОНЫ
5. ВОРОНКА ПРОДАЖ ШАГ ЗА ШАГОМ
6. ПЛАН ДЕЙСТВИЙ НА МЕСЯЦ

Оформление:
- Заголовки ЗАГЛАВНЫМИ БУКВАМИ, отступы пустыми строками
- Списки через дефисы
- Пиши живо, с эмодзи, как Вероника"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Ты Вероника, продюсер экспертов."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=180)
        if response.status_code == 200:
            result = response.json()
            report_text = result["choices"][0]["message"]["content"]
            filename = f"Premium_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = REPORTS_DIR / filename
            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(report_text)
            update_report_status(report_id, 'ready', str(filepath))
        else:
            update_report_status(report_id, 'failed')
    except Exception as e:
        logger.error(f"Premium report error: {e}")
        update_report_status(report_id, 'failed')

async def process_callback(chat_id: str, callback_id: str, callback_data: str):
    state, data = get_user_state(chat_id)
    log_event(chat_id, f"callback_{callback_data}")

    if callback_data == CALLBACK_START_AUDIT:
        save_user_state(chat_id, STATE_AWAITING_BUSINESS_NAME, {"answers": {}, "survey_step": 0})
        await send_callback_answer(callback_id,
            "Окей, погнали! 🚀\n\nНапиши название своего онлайн-бизнеса (как ты представляешь его клиентам):",
            None)
        return

    if callback_data == CALLBACK_MY_PREMIUM:
        biz_data = get_business_data(chat_id)
        form_data = get_form(chat_id)
        if not biz_data or not form_data:
            await send_callback_answer(callback_id,
                "Ой, стоп! Сначала нужно пройти бесплатную диагностику, чтобы я поняла, про что твой бизнес.\n\n"
                "Это быстро — 2 минуты, честно 👇",
                [[[{"text": "📊 Пройти диагностику", "callback_data": CALLBACK_START_AUDIT}]]])
            return

        payment = await create_yookassa_payment(490, "План продаж Salesplan", chat_id)
        if payment and payment.get("confirmation_url"):
            save_pending_payment(chat_id, payment["payment_id"])
            await send_callback_answer(callback_id,
                "🔍 Так, уже запускаю генерацию твоего плана продаж... Обычно это занимает 5-10 минут.\n\n"
                "А пока план готовится, давай честно.\n\n"
                "Ты получила бесплатную диагностику — и что дальше? \n"
                "Часто бывает: информации много, а внедрить не получается. Руки не доходят, непонятно с чего начать.\n\n"
                "Я это знаю не понаслышке — 8 лет с экспертами работаю.\n\n"
                "Поэтому я — Вероника — сделала план продаж, который:\n"
                "✅ Не теория, а конкретные шаги под ТВОЙ бизнес\n"
                "✅ С разбором конкурентов — увидишь, как их обойти\n"
                "✅ С готовыми связками, которые уже принесли деньги моим клиентам\n\n"
                "490 рублей — это смешная цена за готовую стратегию. Серьёзно.\n\n"
                "👇 Оплати по кнопке — и план сразу станет доступен для скачивания",
                get_payment_keyboard(payment["confirmation_url"]))
        else:
            await send_callback_answer(callback_id,
                "❌ Ошибка при создании платежа. Попробуй позже или нажми «Помощь».",
                [[[{"text": "❓ Помощь", "callback_data": CALLBACK_HELP}]]])
            return
        
        report_id = save_report_request(chat_id)
        asyncio.create_task(generate_premium_report(chat_id, biz_data["name"], biz_data["description"], form_data, report_id))
        return

    if callback_data == CALLBACK_I_PAID:
        payment_id = get_pending_payment(chat_id)
        if payment_id:
            status = await check_yookassa_payment(payment_id)
            if status == "succeeded":
                log_event(chat_id, "payment_made", payment_id)
                clear_pending_payment(chat_id)
                biz_data = get_business_data(chat_id)
                await send_notification(ADMIN_CHAT_ID,
                    f"💰 ПОЛУЧЕНА ОПЛАТА\n\nПользователь: {chat_id}\nБизнес: {biz_data['name'] if biz_data else 'не указан'}\nСумма: 490 ₽\n⏰ {format_moscow_time()}")
                report_status = get_report_status(chat_id)
                if report_status and report_status['status'] == 'ready':
                    await send_callback_answer(callback_id,
                        "🎉 Ура! Твой план продаж готов!\n\n"
                        "Я подготовила для тебя персональную стратегию с анализом конкурентов и пошаговым планом.\n\n"
                        "👇 Жми кнопку ниже — и забирай результат",
                        [[[{"text": "📥 Скачать план", "callback_data": CALLBACK_DOWNLOAD_REPORT}]]])
                else:
                    await send_callback_answer(callback_id,
                        "✅ Оплата прошла, спасибо!\n\n"
                        "План ещё готовится — обычно 5-10 минут. Я пришлю уведомление, как только всё будет готово.",
                        [[[{"text": "❓ Помощь", "callback_data": CALLBACK_HELP}]]])
            elif status == "pending":
                await send_callback_answer(callback_id,
                    "⏳ Платёж ещё не подтверждён. Подожди немного и нажми «Я оплатил(а)» снова.\n\n"
                    "Если деньги уже списались — нажми «Помощь», я проверю вручную.",
                    [[[{"text": "❓ Помощь", "callback_data": CALLBACK_HELP}]]])
            else:
                await send_callback_answer(callback_id,
                    "❌ Платёж не найден или отменён. Попробуй оплатить снова.",
                    [[[{"text": "💳 Оплатить 490 ₽", "callback_data": CALLBACK_MY_PREMIUM}]]])
        else:
            await send_callback_answer(callback_id,
                "❌ Не могу найти информацию о платеже. Попробуй оплатить снова.",
                [[[{"text": "💳 Оплатить 490 ₽", "callback_data": CALLBACK_MY_PREMIUM}]]])
        return

    if callback_data == CALLBACK_DOWNLOAD_REPORT:
        report_status = get_report_status(chat_id)
        if report_status and report_status['status'] == 'ready' and report_status['file_path']:
            filepath = Path(report_status['file_path'])
            if filepath.exists():
                await send_file_message(
                    chat_id,
                    "📄 Держи свой план продаж",
                    str(filepath),
                    "file"
                )
                await send_notification(chat_id,
                    "🔥 Ну что, прочитала план?\n\n"
                    "Давай начистоту — ты сможешь всё это внедрить сама?\n"
                    "Я ж знаю эту боль: информации много, а результата нет.\n\n"
                    "Поэтому я предлагаю:\n"
                    "✅ Приходи на 30-минутный разбор плана\n"
                    "✅ Я найду ТВОЁ одно действие, которое принесёт деньги прямо сейчас\n"
                    "🎁 А пока думаешь, забери бесплатный мини-курс «3 шага к первой продаже»")
                await send_message(chat_id, "👇 Жми кнопку, получи мини-курс", get_post_download_keyboard())
            else:
                await send_callback_answer(callback_id,
                    "❌ Ой, файл не найден. Напиши мне в личные сообщения — поможем.",
                    get_main_menu_keyboard())
        else:
            await send_callback_answer(callback_id,
                "⏳ План ещё готовится. Обычно 5-10 минут.\n\nЕсли прошло больше — нажми кнопку помощи 👇",
                [[[{"text": "❓ Помощь", "callback_data": CALLBACK_HELP}]]])
        return

    if callback_data == CALLBACK_HELP:
        await send_notification(ADMIN_CHAT_ID, f"❓ Запрос помощи от {chat_id}\n⏰ {format_moscow_time()}")
        await send_callback_answer(callback_id,
            "✅ Запрос отправлен! Я свяжусь с тобой в ближайшее время.",
            None)
        return

    if callback_data == CALLBACK_BOOK_CALL:
        save_user_state(chat_id, STATE_WAITING_CALL, {})
        await send_callback_answer(callback_id,
            "Привет! Я Вероника Макаревич.\n\n"
            "8 лет назад я начинала с нуля, а сегодня у моих клиентов запуски на 2 млн за 2 недели.\n\n"
            "Я помогаю экспертам перестать мучиться и начать просто продавать.\n\n"
            "Посмотри на моих ребят:\n"
            "🔥 Психолог Елена — 7 клиентов за 2 недели, доход с 0 до 180 000 ₽\n"
            "🔥 Мастер Фен Шуй Анна — первый запуск 200 000 ₽ при рекламе 30 000 ₽\n"
            "🔥 Эксперт по китайскому — 120 000 ₽ за 2 недели вообще без блога\n"
            "🔥 Онлайн-школа — 2 млн за 2 недели через марафон\n\n"
            "Почему я предлагаю тебе 30 минут бесплатно?\n\n"
            "Потому что за 30 минут я:\n"
            "✅ Найду твою точку роста\n"
            "✅ Покажу, почему сейчас не продаётся\n"
            "✅ Дам конкретный план на неделю\n\n"
            "Напиши в одном сообщении:\n"
            "🔗 Ссылку на твой бизнес (канал, сайт)\n"
            "👤 Твой username\n"
            "🕐 Удобное время для созвона (по Москве)\n\n"
            "👇 Жду",
            None)
        return

    if callback_data in [Q1_SERVICE, Q1_INFO, Q1_CONSULT, Q1_NONE,
                         Q2_LT5, Q2_5_20, Q2_20_50, Q2_50P,
                         Q3_LT1K, Q3_1_5K, Q3_5_20K, Q3_20KP,
                         Q4_300, Q4_500, Q4_1M, Q4_SCALE,
                         Q5_YES, Q5_NO, Q5_PROGRESS]:
        _, user_data = get_user_state(chat_id)
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
                log_event(chat_id, "survey_completed")
                biz_data = get_business_data(chat_id)
                if not biz_data:
                    await send_callback_answer(callback_id,
                        "❌ Ошибка: данные бизнеса не найдены. Начни заново.",
                        get_main_menu_keyboard())
                    save_user_state(chat_id, STATE_MENU, {})
                    return

                await send_callback_answer(callback_id,
                    "🔍 Запускаю диагностику... Это займёт до 60 секунд.",
                    None)
                report_text = await call_deepseek_diagnostic(
                    biz_data["name"], biz_data["description"], user_data["answers"])
                if report_text:
                    log_event(chat_id, "free_report_generated")
                    save_user_state(chat_id, STATE_MENU, {"generated_report": report_text, "report_title": biz_data["name"]})
                    await send_callback_answer(callback_id,
                        "✅ Диагностика готова! Как тебе удобнее получить?",
                        get_format_choice_keyboard())
                else:
                    await send_callback_answer(callback_id,
                        "⚠️ Диагностика готова (по шаблону). Как удобнее получить?",
                        get_format_choice_keyboard())
        return

    if callback_data == CALLBACK_SEND_AS_TEXT:
        _, user_data = get_user_state(chat_id)
        report_text = user_data.get("generated_report")
        if report_text:
            max_len = 3800
            if len(report_text) > max_len:
                await send_callback_answer(callback_id, "✅ Твоя диагностика:\n\n" + report_text[:max_len], None)
                await send_notification(chat_id, report_text[max_len:max_len+max_len])
            else:
                await send_callback_answer(callback_id, "✅ Твоя диагностика:\n\n" + report_text, None)
        await send_message(chat_id,
            "🔥 Ну как тебе?\n\n"
            "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
            "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.",
            get_after_diagnostic_keyboard())
        save_user_state(chat_id, STATE_MENU, {})
        return

    if callback_data == CALLBACK_SEND_AS_FILE:
        _, user_data = get_user_state(chat_id)
        report_text = user_data.get("generated_report")
        title = user_data.get("report_title", "business")
        if report_text:
            filename = f"Diagnostic_{title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = REPORTS_DIR / filename
            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(report_text)
            await send_file_message(
                chat_id,
                "📄 Твоя бесплатная диагностика",
                str(filepath),
                "file"
            )
        await send_message(chat_id,
            "🔥 Ну как тебе?\n\n"
            "Это только бесплатная версия. Хочешь полный разбор с конкурентами и готовым планом?\n\n"
            "Закажи план продаж за 490 ₽ — и получишь стратегию, которая реально работает.",
            get_after_diagnostic_keyboard())
        save_user_state(chat_id, STATE_MENU, {})
        return

async def process_message(user_id: str, text: str):
    logger.info(f"PROCESS_MESSAGE called: user_id={user_id}, text={text}")
    state, data = get_user_state(str(user_id))
    log_event(str(user_id), f"message: {text[:50]}")

    if state == STATE_MENU:
        if text == "/start":
            await send_message(str(user_id),
                "Привет! Я Вероника, продюсер экспертов.\n\n"
                "Контент вроде делаешь, подписчики есть, а денег нет? Знакомо.\n\n"
                "Давай сделаем бесплатный аудит твоего канала — 2 минуты, и узнаешь, что теряешь.",
                get_main_menu_keyboard())
            save_user_state(str(user_id), STATE_MENU, {})
        else:
            await send_message(str(user_id), "Используй кнопки меню или напиши /start")
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
        log_event(str(user_id), "business_data_collected")
        save_user_state(str(user_id), STATE_SURVEY, {"answers": {}, "survey_step": 0})
        await send_message(str(user_id), SURVEY_QUESTIONS[0]["text"], get_survey_keyboard(0))
        return

    if state == STATE_SURVEY:
        step = data.get("survey_step", 0)
        if step < len(SURVEY_QUESTIONS):
            key = SURVEY_QUESTIONS[step]["key"]
            answers = data.get("answers", {})
            answers[key] = text
            data["answers"] = answers
            data["survey_step"] = step + 1
            save_user_state(str(user_id), STATE_SURVEY, data)
            
            if step + 1 < len(SURVEY_QUESTIONS):
                await send_message(str(user_id), SURVEY_QUESTIONS[step + 1]["text"], get_survey_keyboard(step + 1))
            else:
                save_form(str(user_id), answers)
                log_event(str(user_id), "survey_completed")
                biz_data = get_business_data(str(user_id))
                if not biz_data:
                    await send_message(str(user_id), "❌ Ошибка: данные бизнеса не найдены. Начни заново.", get_main_menu_keyboard())
                    save_user_state(str(user_id), STATE_MENU, {})
                    return

                await send_message(str(user_id), "🔍 Запускаю диагностику... Это займёт до 60 секунд.", None)
                report_text = await call_deepseek_diagnostic(biz_data["name"], biz_data["description"], answers)
                if report_text:
                    log_event(str(user_id), "free_report_generated")
                    save_user_state(str(user_id), STATE_MENU, {"generated_report": report_text, "report_title": biz_data["name"]})
                    await send_message(str(user_id), "✅ Диагностика готова! Как тебе удобнее получить?", get_format_choice_keyboard())
                else:
                    await send_message(str(user_id), "⚠️ Диагностика готова (по шаблону). Как удобнее получить?", get_format_choice_keyboard())
        return

    if state == STATE_WAITING_CALL:
        biz_data = get_business_data(str(user_id))
        form_data = get_form(str(user_id))
        channel_info = f"Название: {biz_data['name']}\nОписание: {biz_data['description'][:200]}..." if biz_data else "Нет данных"
        survey_info = "Нет данных"
        if form_data:
            q1_map = {Q1_SERVICE: "Услугу", Q1_INFO: "Инфопродукт", Q1_CONSULT: "Консультацию", Q1_NONE: "Пока не продаю"}
            q2_map = {Q2_LT5: "до 5k", Q2_5_20: "5k-20k", Q2_20_50: "20k-50k", Q2_50P: ">50k"}
            q3_map = {Q3_LT1K: "<10", Q3_1_5K: "10-50", Q3_5_20K: "50-200", Q3_20KP: ">200"}
            q4_map = {Q4_300: "300k/мес", Q4_500: "500k/мес", Q4_1M: "1M/мес", Q4_SCALE: "Масштаб"}
            q5_map = {Q5_YES: "Да", Q5_NO: "Нет", Q5_PROGRESS: "В разработке"}
            survey_info = f"""• Продаёт: {q1_map.get(form_data.get('q1'), 'не указано')}
• Средний чек: {q2_map.get(form_data.get('q2'), 'не указано')}
• Клиентов/мес: {q3_map.get(form_data.get('q3'), 'не указано')}
• Цель: {q4_map.get(form_data.get('q4'), 'не указано')}
• Автоворонка: {q5_map.get(form_data.get('q5'), 'не указано')}"""
        await send_message(ADMIN_CHAT_ID,
            f"📞 НОВАЯ ЗАЯВКА НА РАЗБОР\n\nПользователь: {user_id}\nСообщение: {text}\n\nДанные бизнеса:\n{channel_info}\n\nАнкета:\n{survey_info}\n\n⏰ {format_moscow_time()}")
        await send_message(str(user_id),
            "✅ Заявка принята! Я получила твои данные.\n\n"
            "А пока ждёшь ответа, загляни в мой канал — там я делюсь тем, что реально работает:\n"
            "🔥 Кейсы с цифрами\n"
            "🔍 Разборы ошибок\n"
            "📝 Скрипты фраз, которые продают\n\n"
            "После подписки зайди в закреп — там мини-курс «3 шага к первой продаже» в подарок 🎁")
        await send_message(str(user_id),
            "👇 Жми кнопку, подписывайся и забирай мини-курс",
            get_channel_subscribe_keyboard())
        save_user_state(str(user_id), STATE_MENU, {})
        return

    await send_message(str(user_id), "Используй кнопки меню или напиши /start")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Salesplan bot started")
    yield
    logger.info("Salesplan bot stopped")

app = FastAPI(title="Salesplan Bot for MAX", lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        logger.info(f"FULL PAYLOAD: {payload}")

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
            data = cb.get("data")
            if user_id and data:
                await process_callback(str(user_id), str(callback_id), data)

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"status": "Salesplan bot is running", "version": "2.0"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
