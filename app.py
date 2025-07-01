# app.py
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

# === 初始化環境變數 ===
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not (SUPABASE_URL and SUPABASE_KEY):
    raise ValueError("Supabase URL 或 KEY 尚未正確設定。請確認 .env 檔案或系統環境變數。")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)
ADMIN_USER_IDS = ["U34ea24babae0f2a6cbc09e02be4083d8"]  # 你的 LINE 管理員 user ID

# === 工具函式 ===

def get_member(user_id):
    try:
        res = supabase.table("members") \
            .select("status, usage_quota, last_reset_at, member_level, line_user_id") \
            .eq("line_user_id", user_id) \
            .maybe_single() \
            .execute()
        if res.status_code == 204 or not res.data:
            return None
        return res.data
    except Exception as e:
        print(f"Supabase 查詢會員錯誤: {e}")
        return None

def add_member(user_id):
    try:
        now_iso = datetime.utcnow().isoformat()
        res = supabase.table("members").insert({
            "line_user_id": user_id,
            "status": "pending",
            "code": "SET2024",
            "member_level": "normal",
            "usage_quota": 50,
            "last_reset_at": now_iso
        }).execute()
        return res.data
    except Exception as e:
        print(f"Supabase 新增會員錯誤: {e}")
        return None

def reset_quota_if_needed(member):
    try:
        if not member.get("last_reset_at") or not member.get("line_user_id"):
            return member
        last_reset = datetime.fromisoformat(member["last_reset_at"].replace("Z", "+00:00"))
        now = datetime.utcnow()
        if last_reset.date() < now.date():
            res = supabase.table("members").update({
                "usage_quota": 50,
                "last_reset_at": now.isoformat()
            }).eq("line_user_id", member["line_user_id"]).execute()
            if res.status_code == 200:
                member["usage_quota"] = 50
                member["last_reset_at"] = now.isoformat()
        return member
    except Exception as e:
        print(f"重置使用次數錯誤: {e}")
        return member

def save_analysis_log(user_id, msg_hash, reply):
    try:
        supabase.table("analysis_logs").insert({
            "line_user_id": user_id,
            "msg_hash": msg_hash,
            "reply": reply,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"記錄分析日誌錯誤: {e}")

def get_previous_reply(user_id, msg_hash):
    try:
        res = supabase.table("analysis_logs").select("reply") \
            .eq("line_user_id", user_id).eq("msg_hash", msg_hash).maybe_single().execute()
        if res.status_code == 204 or not res.data:
            return None
        return res.data.get("reply")
    except Exception as e:
        print(f"查詢先前回覆錯誤: {e}")
        return None

def update_member_preference(user_id, strategy):
    try:
        supabase.table("member_preferences").upsert({
            "line_user_id": user_id,
            "preferred_strategy": strategy
        }, on_conflict=["line_user_id"]).execute()
    except Exception as e:
        print(f"更新會員偏好錯誤: {e}")

def fake_human_like_reply(msg, user_id):
    try:
        signals_pool = [
            ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
            ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
            ("綠倍數球", 1), ("藍倍數球", 1), ("紫倍數球", 1), ("紅倍數球", 1),
            ("聖甲蟲", 3)
        ]
        chosen = random.sample(signals_pool, k=2 if random.random() < 0.5 else 3)
        signals = '\n'.join([f"{s[0]}：{random.randint(1, s[1])}顆" for s in chosen])

        lines = {line.split(":")[0].strip(): line.split(":")[1].strip() for line in msg.split('\n') if ':' in line}
        not_open = int(lines.get("未開轉數", 0))
        prev1 = int(lines.get("前一轉開", 0))
        prev2 = int(lines.get("前二轉開", 0))
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
        rtp_30 = int(lines.get("30日RTP%數", 0))
        bets_30 = int(lines.get("30日總下注額", 0))

        score = 0
        if rtp_today > 120: score += 3
        elif rtp_today > 110: score += 2
        elif rtp_today < 90: score -= 1
        if bets_today >= 80000: score -= 1
        elif bets_today < 30000: score += 1
        if not_open > 250: score += 2
        elif not_open < 100: score -= 1
        if prev1 > 50: score += 1
        if prev2 > 60: score += 1
        if rtp_30 < 85: score += 1
        elif rtp_30 > 100: score -= 1

        if score >= 4:
            risk, strategy, advice = "🚨 高風險", "高風險-建議平轉100轉後觀察", "這房可能已被爆分過，建議平轉100轉後觀察。"
        elif score >= 2:
            risk, strategy, advice = "⚠️ 中風險", "中風險-小注額觀察", "小注額試轉觀察平轉回分狀況。"
        else:
            risk, strategy, advice = "✅ 低風險", "低風險-可屯房買免遊", "先進場屯房50-100轉，回分可以就買免遊。"

        update_member_preference(user_id, strategy)

        return (
            f"📊 初步分析結果如下：\n"
            f"風險評估：{risk}\n"
            f"建議策略：{advice}\n"
            f"推薦訊號組合：\n{signals}\n"
            f"✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"
        )
    except Exception as e:
        print(f"分析失敗: {e}")
        return "❌ 分析失敗，請確認格式與數值是否正確。"

def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

# === Flask 路由 ===
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
print("DEBUG member:", member)  # 這行是關鍵，確保拿到的 member 內容
if not member:
    reply = "您尚未開通，請先傳送「我要開通」。"
else:
    print("DEBUG status:", member.get("status"))  # 這行確定 status 是多少
    if member.get("status") != "approved":
        reply = "⛔️ 您尚未開通，請先申請通過才能使用分析功能。"
    else:
        reply = "✅ 您已開通完成，歡迎使用。"

                else:
                    reply = f"你已經申請過囉，狀態是：{member['status']}"
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
            elif member["status"] != "approved":
                reply = "⛔️ 您尚未開通，請先申請通過才能使用分析功能。"
            elif "RTP" in msg or "轉" in msg:
                if member["usage_quota"] <= 0:
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

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
