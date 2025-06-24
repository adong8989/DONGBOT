# app.py
from flask import Flask, request, abort
import os
import logging
from dotenv import load_dotenv
from supabase import create_client  # 修正 import 錯誤
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction
import openai

# === 初始化 ===
load_dotenv()

# 環境變數
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 驗證環境變數
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定。請確認 .env 檔案或系統環境變數。")

# 初始化 SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

# === 查會員 ===
def get_member(line_user_id):
    res = supabase.table("members").select("status").eq("line_user_id", line_user_id).maybe_single().execute()
    return res.data if res and res.data else None

# === 新增會員 ===
def add_member(line_user_id, code="SET2024"):
    res = supabase.table("members").insert({
        "line_user_id": line_user_id,
        "status": "pending",
        "code": code
    }).execute()
    return res.data

# === 文字分析功能 ===
def analyze_text_with_gpt(msg):
    instruction = (
        "你是一位RTP分析顧問，請根據以下數據回覆建議：\n"
        "1. 是否建議進場（高、中、低風險級別）\n"
        "2. 建議轉幾次\n"
        "3. 給出簡短原因\n\n"
        "格式如下：\n"
        "未開轉數 :\n"
        "前一轉開 :\n"
        "前二轉開 :\n"
        "今日RTP%數 :\n"
        "今日總下注額 :\n"
        "30日RTP%數 :\n"
        "30日總下注額 :\n"
    )
    prompt = instruction + "\n\n分析內容：\n" + msg
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"分析錯誤：{str(e)}"

# === 快速選單 ===
def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="🧠 文字分析", text="文字分析範例")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

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
    user_id = event.source.user_id if event.source else "unknown"
    msg_type = event.message.type

    if msg_type != "text":
        return  # 只處理文字訊息

    msg = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 會員資料
        member_data = get_member(user_id)

        if msg == "我要開通":
            if member_data:
                reply = f"你已經申請過囉，狀態是：{member_data['status']}"
            else:
                add_member(user_id)
                reply = f"申請成功！請管理員審核。你的 user_id 是：{user_id}"

        elif not member_data or member_data["status"] != "approved":
            reply = "您尚未開通，請先傳送「我要開通」來申請審核。"

        elif "RTP" in msg or "轉" in msg:
            reply = analyze_text_with_gpt(msg)

        elif msg == "使用說明":
            reply = (
                "📘 使用說明：\n"
                "請依下列格式輸入 RTP 資訊進行分析：\n\n"
                "未開轉數 :\n"
                "前一轉開 :\n"
                "前二轉開 :\n"
                "今日RTP%數 :\n"
                "今日總下注額 :\n"
                "30日RTP%數 :\n"
                "30日總下注額 :\n\n"
                "⚠️ 建議：\n"
                "1️⃣ 先進入房間再截圖或記錄，避免房間被搶走。\n"
                "2️⃣ 提供的數據越完整，分析越準確。\n"
                "3️⃣ 分析結果會依據風險級別：高風險 / 中風險 / 低風險\n"
                "4️⃣ 圖片分析功能測試中，建議先使用文字分析。"
            )
        else:
            reply = "請傳送 RTP 資訊或點選下方快速選單進行操作。"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
        ))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
