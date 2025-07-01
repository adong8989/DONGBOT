from flask import Flask, request, abort
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction, URIAction
import hashlib
import random

# 初始化環境變數
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not (SUPABASE_URL and SUPABASE_KEY):
    raise ValueError("Supabase URL 或 KEY 尚未正確設定。")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

# ... 省略前面函式 (get_member, add_member, reset_quota_if_needed, 等等) ...

def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

@app.route("/")
def home():
    return "LINE Bot is running."

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        logging.exception("Webhook handler error")
        abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    msg = event.message.text.strip()
    msg_hash = hashlib.sha256(msg.encode()).hexdigest()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        member = get_member(user_id)
        print("DEBUG member:", member)  # 重要除錯輸出

        if not member:
            if msg == "我要開通":
                add_member(user_id)
                reply = f"申請成功！請加管理員 LINE:adong8989。你的 ID：{user_id}"
            else:
                reply = "您尚未開通，請先傳送「我要開通」。"
        else:
            member = reset_quota_if_needed(member)
            status = member.get("status")
            print("DEBUG status:", status)

            if not status or status.strip().lower() != "approved":
                reply = "⛔️ 您尚未開通，請先申請通過才能使用分析功能。"
            else:
                if msg == "我要開通":
                    reply = "✅ 您已開通完成，歡迎使用。"
                elif msg == "房間資訊表格":
                    reply = (
                        "未開轉數 :\n前一轉開 :\n前二轉開 :\n"
                        "今日RTP%數 :\n今日總下注額 :\n"
                        "30日RTP%數 :\n30日總下注額 :"
                    )
                elif msg == "使用說明":
                    reply = (
                        "📘 使用說明：\n請依下列格式輸入：\n\n"
                        "未開轉數 :\n前一轉開 :\n前二轉開 :\n"
                        "今日RTP%數 :\n今日總下注額 :\n"
                        "30日RTP%數 :\n30日總下注額 :\n\n"
                        "⚠️ 建議：\n1️⃣ 請進入房間後使用分析，避免房間被搶。\n"
                        "2️⃣ 數據越完整越準確。\n3️⃣ 分析有風險等級與建議。\n"
                        "4️⃣ 數字請用整數格式。\n5️⃣ 範例請按『房間資訊表格』取得。"
                    )
                elif "RTP" in msg or "轉" in msg:
                    if member.get("usage_quota", 0) <= 0:
                        reply = "⛔️ 今日分析次數已用完。如需加購請聯絡阿東。"
                    else:
                        prev = get_previous_reply(user_id, msg_hash)
                        if prev:
                            reply = f"這份資料已分析過：\n\n{prev}"
                        else:
                            reply = fake_human_like_reply(msg, user_id)
                            save_analysis_log(user_id, msg_hash, reply)
                            supabase.table("members").update({
                                "usage_quota": member["usage_quota"] - 1
                            }).eq("line_user_id", user_id).execute()
                else:
                    reply = "請輸入房間資訊或使用下方選單。"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
        ))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)

