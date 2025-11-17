import os
import asyncio
import httpx
import psycopg
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# =============================
# 加载环境变量
# =============================
load_dotenv()
TELEGRAM_TOKEN = os.environ["TG_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

# =============================
# Telegram 发送多条消息函数
# =============================
MAX_MESSAGE_LEN = 4000  # Telegram 单条消息上限约 4096 字符

async def send_long_message(update: Update, text: str):
    """
    将长文本自动拆分成多条消息发送。
    也会按换行拆分，避免单条消息太长。
    """
    lines = text.split("\n")
    buffer = ""

    for line in lines:
        # +1 代表换行符
        if len(buffer) + len(line) + 1 <= MAX_MESSAGE_LEN:
            buffer += line + "\n"
        else:
            await update.message.reply_text(buffer)
            buffer = line + "\n"

    if buffer.strip():
        await update.message.reply_text(buffer)

# =============================
# 数据库初始化 (仅建表)
# =============================
async def create_table(conn: psycopg.AsyncConnection):
    """如果表不存在，则创建它"""
    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.commit()

# =============================
# 数据库操作
# =============================
async def get_chat_history(conn: psycopg.AsyncConnection, chat_id: int, limit: int = 10):
    """
    从数据库获取当天的聊天记录，并确保它们按时间升序排列，
    以构成正确的对话历史（旧 -> 新）。
    """
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT role, content FROM (
                SELECT role, content, timestamp
                FROM chat_history
                WHERE chat_id = %s AND timestamp >= NOW() - INTERVAL '1 day'
                ORDER BY timestamp DESC
                LIMIT %s
            ) AS recent_history
            ORDER BY timestamp ASC;
        """, (chat_id, limit))
        return await cur.fetchall()

async def add_to_chat_history(conn: psycopg.AsyncConnection, chat_id: int, role: str, content: str):
    """向数据库添加一条聊天记录"""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO chat_history (chat_id, role, content) VALUES (%s, %s, %s)",
            (chat_id, role, content)
        )
    await conn.commit()

async def delete_today_history(conn: psycopg.AsyncConnection, chat_id: int):
    """删除指定 chat_id 当天的聊天记录"""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM chat_history WHERE chat_id = %s AND timestamp >= NOW() - INTERVAL '1 day'",
            (chat_id,)
        )
    await conn.commit()

async def delete_all_history(conn: psycopg.AsyncConnection, chat_id: int):
    """删除指定 chat_id 的所有聊天记录"""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM chat_history WHERE chat_id = %s", (chat_id,))
    await conn.commit()

# =============================
# 从文件读取 System Prompt
# =============================
def read_context_from_file(file_path: str) -> str:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

# =============================
# DeepSeek API 调用函数
# =============================
async def call_deepseek(prompt_messages: list, client: httpx.AsyncClient) -> str:
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {"model": "deepseek-chat", "messages": prompt_messages}
    try:
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        print(f"API 请求失败: {e}")
        return "抱歉，我在思考时遇到了点问题，请稍后再试。"
    except Exception as e:
        print(f"发生未知错误: {e}")
        return "抱歉，我好像出错了。"

# =============================
# Telegram 命令与消息处理
# =============================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_text = update.message.text
    
    # 并发锁
    lock_key = "processing_lock"
    if lock_key in context.chat_data:
        await update.message.reply_text("抱歉，我正在处理您上一条消息，请稍候...")
        return
    context.chat_data[lock_key] = True

    try:
        db_conn = context.bot_data["db_conn"]
        http_client = context.bot_data["http_client"]

        # 记录用户消息
        await add_to_chat_history(db_conn, chat_id, "user", user_text)

        # 构建上下文
        system_prompt = read_context_from_file('context.txt')
        messages = [{"role": "system", "content": system_prompt}]
        
        history = await get_chat_history(db_conn, chat_id)
        for role, content in history:
            messages.append({"role": role, "content": content})

        # 调用 AI
        reply = await call_deepseek(messages, http_client)

        # 记录机器人回复
        await add_to_chat_history(db_conn, chat_id, "assistant", reply)

        # 🔥 使用自动拆分多消息发送
        await send_long_message(update, reply)

    except Exception as e:
        print(f"处理消息时发生错误: {e}")
        await update.message.reply_text("抱歉，处理消息时出现未知错误。")
    
    finally:
        if lock_key in context.chat_data:
            del context.chat_data[lock_key]

async def clear_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    db_conn = context.bot_data["db_conn"]
    await delete_today_history(db_conn, chat_id)
    await update.message.reply_text("好的，我们今天重新开始吧！")

async def clear_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    db_conn = context.bot_data["db_conn"]
    await delete_all_history(db_conn, chat_id)
    await update.message.reply_text("你好，初次见面！很高兴认识你。")

# =============================
# 主程序入口
# =============================
def main() -> None:

    async def post_init(application: Application):
        db_conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
        await create_table(db_conn)
        application.bot_data["db_conn"] = db_conn
        application.bot_data["http_client"] = httpx.AsyncClient()

    async def post_shutdown(application: Application):
        if "http_client" in application.bot_data:
            await application.bot_data["http_client"].aclose()
        if "db_conn" in application.bot_data:
            await application.bot_data["db_conn"].close()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 注册命令
    application.add_handler(CommandHandler("clear_today", clear_today_command))
    application.add_handler(CommandHandler("clear_all", clear_all_command))
    
    # 注册文本消息处理
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # 启动机器人
    application.run_polling()

if __name__ == "__main__":
    main()
