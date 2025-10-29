# app.py
from flask import Flask, request, abort
import os
import logging
from dotenv import load_dotenv
from supabase import create_client
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction, URIAction
from datetime import datetime
import hashlib
import random

# === 初始化 ===
load_dotenv()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Supabase 資料庫操作 ===
def get_member(line_user_id):
    try:
        res = supabase.table("members").select("*").eq("line_user_id", line_user_id).maybe_single().execute()
        return res.data if res and res.data else None
    except Exception as e:
        logger.exception("[get_member error]")
        return None

def add_member(line_user_id, code="SET2024"):
    try:
        res = supabase.table("members").insert({
            "line_user_id": line_user_id,
            "status": "pending",
            "code": code
        }).execute()
        return res.data
    except Exception as e:
        logger.exception("[add_member error]")
        return None

def get_usage_today(line_user_id):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_user_id).eq("used_at", today).maybe_single().execute()
        return res.data["used_count"] if res and res.data and "used_count" in res.data else 0
    except Exception as e:
        logger.exception("[get_usage_today error]")
        return 0

def increment_usage(line_user_id):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        used = get_usage_today(line_user_id)
        if used == 0:
            supabase.table("usage_logs").insert({
                "line_user_id": line_user_id,
                "used_at": today,
                "used_count": 1
            }).execute()
        else:
            supabase.table("usage_logs").update({
                "used_count": used + 1
            }).eq("line_user_id", line_user_id).eq("used_at", today).execute()
    except Exception as e:
        logger.exception("[increment_usage error]")

def get_previous_reply(line_user_id, msg_hash):
    try:
        res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
        return res.data["reply"] if res and res.data and "reply" in res.data else None
    except Exception as e:
        logger.exception("[get_previous_reply error]")
        return None

def save_analysis_log(line_user_id, msg_hash, reply):
    try:
        supabase.table("analysis_logs").insert({
            "line_user_id": line_user_id,
            "msg_hash": msg_hash,
            "reply": reply
        }).execute()
    except Exception as e:
        logger.exception("[save_analysis_log error]")

def update_member_preference(line_user_id, strategy):
    try:
        supabase.table("member_preferences").upsert({
            "line_user_id": line_user_id,
            "preferred_strategy": strategy
        }, on_conflict=["line_user_id"]).execute()
    except Exception as e:
        logger.exception("[update_member_preference error]")

# === 假人分析函數（僅使用三項） ===
def fake_human_like_reply(msg, line_user_id):
    """
    只分析以下三項：
      1. 未開轉數
      2. 今日RTP%數
      3. 今日總下注額
    範例輸入：
      未開轉數 : 120
      今日RTP%數 : 105
      今日總下注額 : 45000
    """

    # 解析輸入文字
    lines = {}
    for line in msg.split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            lines[k.strip()] = v.strip()

    try:
        not_open = int(lines.get("未開轉數", 0))
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
    except Exception:
        return "❌ 分析失敗，請確認輸入格式及數值正確（整數、無小數點或符號）。\n\n範例：\n未開轉數 : 120\n今日RTP%數 : 105\n今日總下注額 : 45000"

    # === 分析邏輯 ===
    risk_score = 0

    # 未開轉數判斷
    if not_open > 250:
        risk_score += 2
    elif not_open > 150:
        risk_score += 1
    elif not_open < 50:
        risk_score -= 1

    # RTP%數判斷
    if rtp_today > 120:
        risk_score += 2
    elif rtp_today > 110:
        risk_score += 1
    elif rtp_today < 90:
        risk_score -= 1

    # 今日總下注額判斷
    if bets_today >= 80000:
        risk_score -= 1
    elif bets_today < 30000:
        risk_score += 1

    # === 分析結果分類 ===
    if risk_score >= 3:
        risk_level = "🚨 高風險"
        strategy = "建議僅觀察，暫不進場。"
        advice = "風險偏高，可能已爆分或吃分過。"
    elif risk_score >= 1:
        risk_level = "⚠️ 中風險"
        strategy = "可小額觀察，視情況再加注。"
        advice = "回分條件一般，適合保守打法。"
    else:
        risk_level = "✅ 低風險"
        strategy = "建議可進場觀察，適合穩定操作。"
        advice = "房間數據良好，可考慮逐步提高注額。"

    update_member_preference(line_user_id, strategy)

    return (
        f"📊 房間分析結果如下：\n"
        f"風險等級：{risk_level}\n"
        f"建議策略：{strategy}\n"
        f"說明：{advice}\n\n"
        f"✨ 若需進一步打法策略，請聯絡阿東超人：LINE ID adong8989"
    )

# === 快速回覆 ===
def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

# === LINE Webhook ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception:
        logger.exception("Webhook handler error")
        abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id if event.source else "unknown"
    msg = getattr(event.message, "text", "").strip()
    msg_hash = hashlib.sha256(msg.encode("utf-8")).hexdigest()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        member_data = get_member(user_id)
        reply = ""

        if msg == "我要開通":
            if member_data:
                if member_data.get("status") == "approved":
                    reply = "✅ 您已開通完成，歡迎使用選房分析功能。"
                else:
                    reply = f"你已申請過囉，請找管理員審核 LINE ID :adong8989。\n目前狀態：{member_data.get('status')}"
            else:
                add_member(user_id)
                reply = f"申請成功！請加管理員 LINE:adong8989 並提供此 user_id：{user_id}"

        elif msg == "房間資訊表格":
            reply = (
                "請依以下格式輸入三項資料進行分析：\n\n"
                "未開轉數 :\n"
                "今日RTP%數 :\n"
                "今日總下注額 :"
            )

        elif not member_data or member_data.get("status") != "approved":
            reply = "您尚未開通，請先傳送「我要開通」來申請審核。"

        elif "RTP" in msg or "轉" in msg:
            level = member_data.get("member_level", "normal")
            limit = 50 if level == "vip" else 15
            used = get_usage_today(user_id)

            if used >= limit:
                reply = f"⚠️ 今日已達使用上限（{limit}次），請明日再試或升級 VIP。"
            else:
                prev = get_previous_reply(user_id, msg_hash)
                if prev:
                    reply = f"此資料已分析過：\n\n{prev}"
                else:
                    reply = fake_human_like_reply(msg, user_id)
                    save_analysis_log(user_id, msg_hash, reply)
                    increment_usage(user_id)
                    used += 1
                    reply += f"\n\n✅ 分析完成（今日剩餘 {limit - used} / {limit} 次）"

        elif msg == "使用說明":
            reply = (
                "📘 使用說明：\n"
                "請依下列格式輸入 RTP 資訊：\n\n"
                "未開轉數 :\n"
                "今日RTP%數 :\n"
                "今日總下注額 :\n\n"
                "⚠️ 注意事項：\n"
                "1️⃣ 所有數值請填整數（無小數點或 % 符號）\n"
                "2️⃣ 分析結果分為高 / 中 / 低風險\n"
                "3️⃣ 每日使用次數：normal 15 次，vip 50 次"
            )

        else:
            reply = "請傳送房間資訊或使用下方快速選單進行操作。"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
        ))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
