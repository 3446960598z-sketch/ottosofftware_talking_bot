#!/usr/bin/env python3
# main.py - Fully optimized Telegram bot with short messages, error handling, and [NEWMSG] support

import os
import asyncio
import logging
import json
import random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

import aiohttp
from aiohttp import ClientTimeout

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters
)

# Optional async redis
try:
    import redis.asyncio as aioredis
except Exception:
    aioredis = None

# -------------------- Config --------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
REDIS_URL = os.getenv("REDIS_URL")

ALLOWED_USERS = set(int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip())
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else 0
ADMINS = set(int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip())
if OWNER_ID and OWNER_ID not in ALLOWED_USERS:
    ALLOWED_USERS.add(OWNER_ID)

MAX_HISTORY_ITEMS = int(os.getenv("MAX_HISTORY_ITEMS", "40"))
HISTORY_SAVE_ITEMS = int(os.getenv("HISTORY_SAVE_ITEMS", "30"))
RANDOM_PUSH_MIN = int(os.getenv("RANDOM_PUSH_MIN", "3600"))
RANDOM_PUSH_MAX = int(os.getenv("RANDOM_PUSH_MAX", "10800"))
MSG_MAX_LEN = int(os.getenv("MSG_MAX_LEN", "10"))
MSG_MAX_PARTS = int(os.getenv("MSG_MAX_PARTS", "5"))

PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts.txt")

# -------------------- Timezone --------------------
TZ_CN = timezone(timedelta(hours=8))

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-bot")

# -------------------- Global state --------------------
redis_client = None
use_redis = False
_mem_context = {}
_mem_mode = {}
_pending_confirmations = {}
PROMPTS = {}

http_timeout = ClientTimeout(total=60)
http_session: aiohttp.ClientSession = None

# -------------------- Helpers --------------------
def load_prompts() -> bool:
    global PROMPTS
    if not os.path.exists(PROMPTS_FILE):
        logger.error("prompts.txt not found.")
        return False
    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    data = {}
    cur = None
    buf = []
    for raw in lines:
        if raw.startswith("[") and raw.endswith("]"):
            if cur:
                data[cur] = "\n".join(buf).strip()
            cur = raw[1:-1]
            buf = []
        else:
            if cur:
                buf.append(raw)
    if cur:
        data[cur] = "\n".join(buf).strip()
    required = ["SYSTEM_PROMPT", "START_MESSAGE", "PERMISSION_DENIED", "RANDOM_PUSH_TEMPLATE"]
    for k in required:
        if k not in data:
            logger.error(f"Missing key in prompts.txt: {k}")
            return False
    PROMPTS = data
    logger.info("Prompts loaded.")
    return True

def user_is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS or user_id == OWNER_ID or user_id in ADMINS

def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMINS

# -------------------- Redis --------------------
async def init_redis():
    global redis_client, use_redis
    if REDIS_URL and aioredis:
        try:
            redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await redis_client.ping()
            use_redis = True
            logger.info("Redis connected.")
        except Exception as e:
            logger.warning("Redis init failed: %s", e)
            redis_client = None
            use_redis = False
    else:
        use_redis = False
        logger.info("Redis not used.")

async def save_user_context(user_id: int, history: List[Dict[str, Any]]):
    key = f"user:ctx:{user_id}"
    data = json.dumps(history, ensure_ascii=False)
    if use_redis:
        try:
            await redis_client.set(key, data)
            return
        except Exception:
            pass
    _mem_context[str(user_id)] = history

async def load_user_context(user_id: int) -> List[Dict[str, Any]]:
    key = f"user:ctx:{user_id}"
    if use_redis:
        try:
            raw = await redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    return _mem_context.get(str(user_id), [])

async def save_user_mode(user_id: int, mode: str):
    key = f"user:mode:{user_id}"
    if use_redis:
        try:
            await redis_client.set(key, mode)
            return
        except Exception:
            pass
    _mem_mode[user_id] = mode

async def load_user_mode(user_id: int) -> str:
    key = f"user:mode:{user_id}"
    if use_redis:
        try:
            raw = await redis_client.get(key)
            if raw:
                return raw
        except Exception:
            pass
    return _mem_mode.get(user_id, "default")

# -------------------- Text splitting --------------------
def auto_split_reply(text: str, max_len: int = MSG_MAX_LEN, max_parts: int = MSG_MAX_PARTS):
    if not text:
        return []
    raw_parts = [x.strip() for x in text.split("[NEWMSG]") if x.strip()]
    out = []
    for part in raw_parts:
        if len(out) >= max_parts:
            break
        i = 0
        while i < len(part) and len(out) < max_parts:
            out.append(part[i:i+max_len])
            i += max_len
    return out[:max_parts]

def get_current_time_data():
    now = datetime.now(TZ_CN)
    return now.strftime("%H:%M"), now.strftime("%Y-%m-%d")

# -------------------- LLM call --------------------
async def call_openai(messages):
    url = f"{OPENAI_API_BASE}/v1/chat/completions"
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_tokens": 150,
        "temperature": 0.6,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with http_session.post(url, json=payload, headers=headers) as resp:
            txt = await resp.text()
            if resp.status != 200:
                logger.error("LLM error %s: %s", resp.status, txt)
                return f"LLM调用失败: {resp.status}"
            data = json.loads(txt)
            return data["choices"][0]["message"].get("content", "")
    except Exception as e:
        logger.exception("LLM call failed")
        return f"LLM调用异常: {str(e)}"

async def chat_with_openai(user_id: int, text: str, mode: str):
    history = await load_user_context(user_id)
    if not history or history[0]["role"] != "system":
        history.insert(0, {"role": "system", "content": PROMPTS["SYSTEM_PROMPT"]})
    if mode and not any(m["role"] == "system" and m["content"].startswith("[MODE]") for m in history):
        history.insert(1, {"role": "system", "content": f"[MODE] {mode}"})
    ts_short, ts_date = get_current_time_data()
    history.append({"role": "user", "content": text, "timestamp": ts_short, "date": ts_date})
    if len(history) > MAX_HISTORY_ITEMS:
        history = history[-HISTORY_SAVE_ITEMS:]
    api_messages = [{"role": m["role"], "content": m["content"]} for m in history]
    reply = await call_openai(api_messages)
    ts_short_end, ts_date_end = get_current_time_data()
    history.append({"role": "assistant", "content": reply, "timestamp": ts_short_end, "date": ts_date_end})
    await save_user_context(user_id, history)
    return reply

# -------------------- Command Handlers --------------------
async def start_handler(update, context):
    uid = update.effective_user.id
    try:
        if not user_is_allowed(uid):
            return await update.message.reply_text(PROMPTS.get("PERMISSION_DENIED", "权限不足"))
        await save_user_context(uid, [])
        await update.message.reply_text(PROMPTS.get("START_MESSAGE", "欢迎使用!"))
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def message_handler(update, context):
    uid = update.effective_user.id
    try:
        if not user_is_allowed(uid):
            return await update.message.reply_text(PROMPTS.get("PERMISSION_DENIED", "权限不足"))
        text = (update.message.text or "").strip()
        if not text:
            return
        mode = await load_user_mode(uid)
        reply = await chat_with_openai(uid, text, mode)
        parts = auto_split_reply(reply)
        for p in parts:
            await update.message.reply_text(p)
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def clear_today_handler(update, context):
    uid = update.effective_user.id
    try:
        if not user_is_allowed(uid):
            return await update.message.reply_text(PROMPTS.get("PERMISSION_DENIED", "权限不足"))
        _pending_confirmations[uid] = "clear_today"
        await update.message.reply_text("确认清除今天的记录？ /confirm 或 /cancel")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def clear_all_handler(update, context):
    uid = update.effective_user.id
    try:
        if not is_admin(uid):
            return await update.message.reply_text("仅管理员可执行。")
        _pending_confirmations[uid] = "clear_all"
        await update.message.reply_text("确认清除所有记录？ /confirm 或 /cancel")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def confirm_handler(update, context):
    uid = update.effective_user.id
    try:
        action = _pending_confirmations.pop(uid, None)
        if not action:
            return await update.message.reply_text("无待确认操作。")
        if action == "clear_today":
            hist = await load_user_context(uid)
            today_str = datetime.now(TZ_CN).strftime("%Y-%m-%d")
            new_hist = [m for m in hist if m.get("date") != today_str or m["role"] == "system"]
            await save_user_context(uid, new_hist)
            await update.message.reply_text("已清除今天记录。")
        elif action == "clear_all":
            await save_user_context(uid, [])
            await update.message.reply_text("已清除全部记录。")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def cancel_handler(update, context):
    uid = update.effective_user.id
    try:
        if uid in _pending_confirmations:
            _pending_confirmations.pop(uid)
            return await update.message.reply_text("操作已取消。")
        await update.message.reply_text("没有可取消的操作。")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def mode_handler(update, context):
    uid = update.effective_user.id
    try:
        if not user_is_allowed(uid):
            return await update.message.reply_text(PROMPTS.get("PERMISSION_DENIED", "权限不足"))
        args = context.args or []
        if not args:
            cur = await load_user_mode(uid)
            return await update.message.reply_text(f"当前模式：{cur}")
        new = args[0].lower()
        await save_user_mode(uid, new)
        await update.message.reply_text(f"已设定模式：{new}")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

async def status_handler(update, context):
    uid = update.effective_user.id
    try:
        hist = await load_user_context(uid)
        mode = await load_user_mode(uid)
        await update.message.reply_text(f"模式：{mode}\n历史条数：{len(hist)}\n模型：{OPENAI_MODEL}")
    except Exception as e:
        await update.message.reply_text(f"出错了: {str(e)}")

# -------------------- Random push --------------------
async def random_push_task(app):
    await asyncio.sleep(5)
    logger.info("Random push started.")
    while True:
        try:
            delay = random.randint(RANDOM_PUSH_MIN, RANDOM_PUSH_MAX)
            await asyncio.sleep(delay)
            now_str = datetime.now(TZ_CN).strftime("%H:%M")
            msg = PROMPTS["RANDOM_PUSH_TEMPLATE"].format(time=now_str)
            for uid in ALLOWED_USERS:
                try:
                    await app.bot.send_message(uid, msg)
                except Exception:
                    pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("push error: %s", e)

# -------------------- Lifecycle --------------------
async def on_startup(app):
    global http_session
    logger.info("Initializing...")
    if not load_prompts():
        raise RuntimeError("Failed loading prompts.txt")
    http_session = aiohttp.ClientSession(timeout=http_timeout)
    await init_redis()
    app.bot_data["push_task"] = asyncio.create_task(random_push_task(app))
    logger.info("Startup completed.")

async def on_shutdown(app):
    logger.info("Shutting down...")
    task = app.bot_data.get("push_task")
    if task:
        task.cancel()
    if http_session:
        await http_session.close()
    if redis_client:
        await redis_client.close()
    logger.info("Shutdown completed.")

# -------------------- Application --------------------
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("clear_today", clear_today_handler))
    app.add_handler(CommandHandler("clear_all", clear_all_handler))
    app.add_handler(CommandHandler("confirm", confirm_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(CommandHandler("mode", mode_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    return app

if __name__ == "__main__":
    logger.info("Bot starting with polling...")
    app = build_app()
    app.run_polling()
