# app.py
from flask import Flask, request, abort
import os
import logging
from dotenv import load_dotenv
from supabase import create_client  # 修正 import 錯誤
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction

# === 初始化 ===
load_dotenv()

# 環境變數
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 驗證環境變數
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定。請確認 .env 檔案或系統環境變數。")

# 初始化 SDK
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# === 替代 GPT 模擬分析 ===
def fake_human_like_reply(msg):
    import random
    signals_pool = ["眼睛", "刀子", "弓箭", "蛇", "紅寶石", "藍寶石", "黃寶石", "綠寶石", "紫寶石", "綠倍球", "藍倍球", "紫倍球", "紅倍球", "聖甲蟲"]
    chosen_signals = random.sample(signals_pool, k=2 if random.random() < 0.5 else 3)

    # 風險判斷簡易規則
    lines = {line.split(':')[0].strip(): line.split(':')[1].strip() for line in msg.split('\n') if ':' in line}
    try:
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
        not_open = int(lines.get("未開轉數", 0))
    except:
        return "❌ 分析失敗，請確認格式與數值是否正確。"

    risk = "高風險" if rtp_today > 110 else ("低風險" if rtp_today < 85 and bets_today > 80000 else "中風險")
    advice = "建議先觀察看看或換房比較保險～" if risk == "高風險" else ("可以考慮進場屯房喔～" if risk == "低風險" else "可以先小額轉轉看，觀察是否有回分。")

    return (
        f"📊 初步分析結果如下：\n"
        f"風險評估：{risk}\n"
        f"建議策略：{advice}\n"
        f"推薦訊號組合：{', '.join(chosen_signals)}\n"
        f"✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"
    )

# === 快速選單 ===
def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="🧠 註冊按我", text="https://wek002.welove777.com")),
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
            reply = fake_human_like_reply(msg)

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
