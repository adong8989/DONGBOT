# app.py
from flask import Flask, request, abort
import os
import logging
from dotenv import load_dotenv
from supabase import create_client
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction
import hashlib
import random

# === 初始化 ===
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定。請確認 .env 檔案或系統環境變數。")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

def get_member(line_user_id):
    res = supabase.table("members").select("status").eq("line_user_id", line_user_id).maybe_single().execute()
    return res.data if res and res.data else None

def add_member(line_user_id, code="SET2024"):
    res = supabase.table("members").insert({
        "line_user_id": line_user_id,
        "status": "pending",
        "code": code
    }).execute()
    return res.data

def save_analysis_log(line_user_id, msg_hash, reply):
    supabase.table("analysis_logs").insert({
        "line_user_id": line_user_id,
        "msg_hash": msg_hash,
        "reply": reply
    }).execute()

def get_previous_reply(line_user_id, msg_hash):
    res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
    return res.data["reply"] if res and res.data else None

def fake_human_like_reply(msg):
    signals_pool = [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("綠倍球", 1), ("藍倍球", 1), ("紫倍球", 1), ("紅倍球", 1),
        ("聖甲蟲", 3)
    ]

    def generate_signals():
        chosen = random.sample(signals_pool, k=2 if random.random() < 0.5 else 3)
        signals_with_counts = []
        for s in chosen:
            count = random.randint(1, s[1])
            signals_with_counts.append((s[0], count))
        return signals_with_counts

    # 重試機制，最多5次避免死迴圈
    for _ in range(5):
        signals = generate_signals()
        gem_signals = ["眼睛", "刀子", "弓箭", "蛇", "紅寶石", "藍寶石", "黃寶石", "綠寶石", "紫寶石"]
        total_gems = sum(count for name, count in signals if name in gem_signals)
        scarabs = sum(count for name, count in signals if name == "聖甲蟲")
        if total_gems <= 7 and scarabs <= 3:
            break
    else:
        signals = [
            (name, min(count, 7) if name in gem_signals else count)
            for name, count in signals
        ]
        signals = [
            (name, min(count, 3) if name == "聖甲蟲" else count)
            for name, count in signals
        ]

    signal_text = '\n'.join([f"{name}：{count}顆" for name, count in signals])

    lines = {line.split(':')[0].strip(): line.split(':')[1].strip() for line in msg.split('\n') if ':' in line}
    try:
        not_open = int(lines.get("未開轉數", 0))
        prev1 = int(lines.get("前一轉開", 0))
        prev2 = int(lines.get("前二轉開", 0))
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
        rtp_30 = int(lines.get("30日RTP%數", 0))
        bets_30 = int(lines.get("30日總下注額", 0))
    except:
        return "❌ 分析失敗，請確認格式與數值是否正確。"

    risk_score = 0
    if rtp_today > 120: risk_score += 3
    elif rtp_today > 110: risk_score += 2
    elif rtp_today < 90: risk_score -= 1

    if bets_today >= 80000: risk_score -= 1
    elif bets_today < 30000: risk_score += 1

    if not_open > 250: risk_score += 2
    elif not_open < 100: risk_score -= 1

    if prev1 > 50: risk_score += 1
    if prev2 > 60: risk_score += 1

    if rtp_30 < 85: risk_score += 1
    elif rtp_30 > 100: risk_score -= 1

    if risk_score >= 4:
        risk = "🚨 高風險"
        advice = "這房可能已被爆分過，建議平轉100轉如回分不好就換房或小買一場免遊試試看。"
    elif risk_score >= 2:
        risk = "⚠️ 中風險"
        advice = "可以先小注額試轉觀察平轉回分狀況，回分可能不錯但仍需謹慎。"
    else:
        risk = "✅ 低風險"
        advice = "看起來有機會，建議先進場屯房50-100轉看回分，回分可以的話就買一場免遊看看。"

    return (
        f"📊 初步分析結果如下：\n"
        f"風險評估：{risk}\n"
        f"建議策略：{advice}\n"
        f"推薦訊號組合：\n{signal_text}\n"
        f"✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"
    )

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
        return

    msg = event.message.text.strip()
    msg_hash = hashlib.sha256(msg.encode()).hexdigest()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        member_data = get_member(user_id)

        if msg == "我要開通":
            if member_data:
                reply = f"你已經申請過囉趕緊找管理員審核LINE ID :adong8989，狀態是：{member_data['status']}"
            else:
                add_member(user_id)
                reply = f"申請成功！請加管理員LINE:adong8989給你的USER ID 申請審核。你的 user_id 是：{user_id}"

        elif not member_data or member_data["status"] != "approved":
            reply = "您尚未開通，請先傳送「我要開通」來申請審核。"

        elif "RTP" in msg or "轉" in msg:
            previous = get_previous_reply(user_id, msg_hash)
            if previous:
                reply = f"這份資料已經分析過囉，請勿重複提交相同內容唷：\n\n{previous}"
            else:
                reply = fake_human_like_reply(msg)
                save_analysis_log(user_id, msg_hash, reply)

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
                "1️⃣ 先進入房間再來使用分析，可避免房間被搶走哦。\n"
                "2️⃣ 提供的數據越完整，分析越準確。\n"
                "3️⃣ 分析結果會依據房間風險級別：高風險 / 中風險 / 低風險\n"
                "4️⃣ 房間所有的資訊只需提供小數點前面的數字不能加小數點與 % 符號。"
            )

        else:
            reply = "請傳送房間資訊或點選下方快速選單進行操作。"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
        ))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
