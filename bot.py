import os
import asyncio
import httpx
import psycopg
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# =============================
# 加载环境变量
# =============================
load_dotenv()
TELEGRAM_TOKEN = os.environ["TG_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

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
    """从数据库获取当天的聊天记录"""
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
# Telegram 消息处理函数
# =============================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_text = update.message.text
    
    db_conn = context.bot_data["db_conn"]
    http_client = context.bot_data["http_client"]

    await add_to_chat_history(db_conn, chat_id, "user", user_text)

    system_prompt = read_context_from_file('context.txt')
    messages = [{"role": "system", "content": system_prompt}]
    
    history = await get_chat_history(db_conn, chat_id)
    for role, content in history:
        messages.append({"role": role, "content": content})
    
    reply = await call_deepseek(messages, http_client)

    await add_to_chat_history(db_conn, chat_id, "assistant", reply)

    await update.message.reply_text(reply)

# =============================
# 主程序入口
# =============================
def main() -> None:
    """设置并运行机器人"""

    # 定义在启动时运行的异步函数
    async def post_init(application: Application):
        db_conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
        await create_table(db_conn)
        application.bot_data["db_conn"] = db_conn
        application.bot_data["http_client"] = httpx.AsyncClient()

    # 定义在关闭时运行的异步函数
    async def post_shutdown(application: Application):
        if "http_client" in application.bot_data:
            await application.bot_data["http_client"].aclose()
        if "db_conn" in application.bot_data:
            await application.bot_data["db_conn"].close()

    # 使用 ApplicationBuilder 创建应用
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)          # 注册启动钩子
        .post_shutdown(post_shutdown)  # 注册关闭钩子
        .build()
    )

    # 注册消息处理器
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # 启动机器人 (这是一个阻塞式调用，直到程序停止)
    application.run_polling()

if __name__ == "__main__":
    main()
