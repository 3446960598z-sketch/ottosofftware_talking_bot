
import os
import asyncio
import httpx
import psycopg
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# =============================
# 加载环境变量
# =============================
load_dotenv()
TELEGRAM_TOKEN = os.environ["TG_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Railway 会自动提供这个环境变量

# =============================
# 数据库设置
# =============================
async def init_db():
    """初始化数据库，创建表"""
    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
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
    return conn

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
    """从指定的 .txt 文件中读取内容作为 system prompt。"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return "你是一个乐于助人的AI助手。"

# =============================
# DeepSeek API 调用函数
# =============================
async def call_deepseek(prompt_messages: list, client: httpx.AsyncClient) -> str:
    """使用 httpx 异步调用 DeepSeek API"""
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": prompt_messages
    }
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

    # 1. 将用户消息存入数据库
    await add_to_chat_history(db_conn, chat_id, "user", user_text)

    # 2. 准备 API 请求的 messages
    system_prompt = read_context_from_file('context.txt')
    messages = [{"role": "system", "content": system_prompt}]
    
    # 3. 从数据库获取历史记录并添加到 messages
    history = await get_chat_history(db_conn, chat_id)
    for role, content in history:
        messages.append({"role": role, "content": content})
    
    # 4. 调用 API 获取回复
    reply = await call_deepseek(messages, http_client)

    # 5. 将机器人回复存入数据库
    await add_to_chat_history(db_conn, chat_id, "assistant", reply)

    # 6. 发送回复
    await update.message.reply_text(reply)

# =============================
# 主程序入口
# =============================
async def main():
    """启动机器人"""
    # 将资源初始化放在 async with 块中，以便自动管理
    async with httpx.AsyncClient() as http_client, await init_db() as db_connection:
        
        # 使用 ApplicationBuilder 创建应用
        builder = Application.builder().token(TELEGRAM_TOKEN)
        app = builder.build()

        # 将数据库连接和 http 客户端存入 bot_data，供所有 handler 使用
        app.bot_data["db_conn"] = db_connection
        app.bot_data["http_client"] = http_client

        # 注册消息处理器
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

        # 使用 run_polling 启动机器人
        # 它会自动处理异步循环，并在接收到停止信号时优雅地关闭
        await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
