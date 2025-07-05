# app.py
from flask import Flask, request, abort
import os
import logging
from dotenv import load_dotenv
from supabase import create_client
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction, URIAction
import hashlib
import json
import random
from datetime import datetime, date

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

# === 資料庫函數 ===
def get_member(line_user_id):
    res = supabase.table("members").select("status", "member_level").eq("line_user_id", line_user_id).maybe_single().execute()
    return res.data if res and res.data else None

def add_member(line_user_id, code="SET2024"):
    return supabase.table("members").insert({
        "line_user_id": line_user_id,
        "status": "pending",
        "code": code
    }).execute()

def save_analysis_log(line_user_id, msg_hash, reply):
    supabase.table("analysis_logs").insert({
        "line_user_id": line_user_id,
        "msg_hash": msg_hash,
        "reply": reply
    }).execute()

def get_previous_reply(line_user_id, msg_hash):
    res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
    return res.data["reply"] if res and res.data else None

def save_signal_stats(signals):
    for s, qty in signals:
        supabase.table("signal_stats").insert({
            "signal_name": s,
            "quantity": qty
        }).execute()

def update_member_preference(line_user_id, strategy):
    supabase.table("member_preferences").upsert({
        "line_user_id": line_user_id,
        "preferred_strategy": strategy
    }, on_conflict=["line_user_id"]).execute()

def get_usage_today(line_user_id):
    today_str = date.today().isoformat()
    res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_user_id).eq("used_at_date", today_str).maybe_single().execute()
    return res.data["used_count"] if res.data else 0

def increment_usage(line_user_id):
    today_str = date.today().isoformat()
    used = get_usage_today(line_user_id)
    if used:
        supabase.table("usage_logs").update({"used_count": used + 1}).eq("line_user_id", line_user_id).eq("used_at_date", today_str).execute()
    else:
        supabase.table("usage_logs").insert({"line_user_id": line_user_id, "used_at_date": today_str, "used_count": 1}).execute()

# === 假分析訊息邏輯 ===
def fake_human_like_reply(msg, line_user_id):
    signals_pool = [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("聖甲蟲", 3)
    ]
    groups = []
    for _ in range(2):
        while True:
            chosen = random.sample(signals_pool, k=random.choice([2, 3]))
            selected = [(s[0], random.randint(1, s[1])) for s in chosen]
            if sum(q for _, q in selected) <= 12:
                groups.append(selected)
                break

    signal_text = "\n\n".join("\n".join([f"{s}：{q}顆" for s, q in group]) for group in groups)
    for group in groups:
        save_signal_stats(group)

    lines = {line.split(":")[0].strip(): line.split(":")[1].strip() for line in msg.split('\n') if ':' in line}
    try:
        not_open = int(lines.get("未開轉數", 0))
        prev1 = int(lines.get("前一轉開", 0))
        prev2 = int(lines.get("前二轉開", 0))
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
        rtp_30 = int(lines.get("30日RTP%數", 0))
        bets_30 = int(lines.get("30日總下注額", 0))
    except:
        return "❌ 分析失敗，請確認格式與數值(不能有小數點)是否正確。"

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
        level = random.choice(["🚨 高風險", "🔥 可能被爆分過", "⚠️ 危險等級高"])
        strategy = random.choice(["高風險 - 建議平轉 100 轉後觀察", "高風險 - 小心進場，觀察平轉回分"])
        advice = random.choice(["建議先用 100 轉觀察回分情況。", "此類型 RTP 組合不太妙，建議保守應對。"])
    elif risk_score >= 2:
        level = random.choice(["⚠️ 中風險", "🟠 風險可控"])
        strategy = random.choice(["中風險 - 小注額觀察", "中風險 - 觀察型打法"])
        advice = random.choice(["可先小額下注觀察。", "RTP 有潛力，建議保守試轉。"])
    else:
        level = random.choice(["✅ 低風險", "🟢 穩定場"])
        strategy = random.choice(["低風險 - 可進房試買免遊", "低風險 - 可直接嘗試免遊"])
        advice = random.choice(["建議進場屯房後買免遊。", "是個不錯的房間，建議穩紮穩打進場。"])

    update_member_preference(line_user_id, strategy)
    return f"📊 初步分析結果如下：\n風險評估：{level}\n建議策略：{advice}\n推薦訊號組合：\n{signal_text}\n\n✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"

# === LINE Bot ===
def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception:
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
        member = get_member(user_id)

        if msg == "我要開通":
            if member:
                if member["status"] == "approved":
                    reply = "✅ 您已開通完成，歡迎使用選房分析功能。"
                else:
                    reply = f"你已經申請過囉，狀態為：{member['status']}。請聯絡管理員 LINE ID: adong8989"
            else:
                add_member(user_id)
                reply = f"申請成功！請加管理員 LINE:adong8989，提供 user_id：{user_id} 申請審核。"

        elif msg == "房間資訊表格":
            reply = "未開轉數 :\n前一轉開 :\n前二轉開 :\n今日RTP%數 :\n今日總下注額 :\n30日RTP%數 :\n30日總下注額 :"

        elif not member or member["status"] != "approved":
            reply = "❌ 您尚未開通，請先傳送「我要開通」來申請審核。"

        elif "RTP" in msg or "轉" in msg:
            level = member.get("member_level", "normal")
            limit = 15 if level == "normal" else 50
            used = get_usage_today(user_id)
            if used >= limit:
                reply = f"⚠️ 您今天的使用次數已達上限 ({limit} 次)，請明天再試，或升級為 VIP 使用更多次數。"
            else:
                previous = get_previous_reply(user_id, msg_hash)
                if previous:
                    reply = f"這份資料已經分析過囉：\n\n{previous}"
                else:
                    reply = fake_human_like_reply(msg, user_id)
                    save_analysis_log(user_id, msg_hash, reply)
                    increment_usage(user_id)

        elif msg == "使用說明":
            reply = (
                "📘 使用說明：\n請依下列格式輸入 RTP 資訊：\n\n"
                "未開轉數 :\n前一轉開 :\n前二轉開 :\n今日RTP%數 :\n今日總下注額 :\n30日RTP%數 :\n30日總下注額 :\n\n"
                "⚠️ 注意：數值請填整數，勿使用小數點與 % 符號。\n"
                "建議分析前先進入房間避免被搶走。"
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
