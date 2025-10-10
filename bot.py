import os
import requests
from telegram import Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackContext

# 从环境变量读取
TELEGRAM_TOKEN = os.environ["TG_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]

def call_deepseek(prompt):
    """调用 DeepSeek API 获取回复"""
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个友好、有趣的聊天助手。"},
            {"role": "user", "content": prompt}
        ]
    }
    r = requests.post(url, headers=headers, json=payload)
    data = r.json()
    return data["choices"][0]["message"]["content"]

def handle_message(update: Update, context: CallbackContext):
    """接收 Telegram 消息并回复"""
    user_text = update.message.text
    reply = call_deepseek(user_text)
    update.message.reply_text(reply)

def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    updater.start_polling()  # 轮询方式获取消息
    updater.idle()

if __name__ == "__main__":
    main()
