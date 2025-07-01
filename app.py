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
import json
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

ADMIN_USER_IDS = ["U34ea24babae0f2a6cbc09e02be4083d8
"]  # 加上你的 LINE 管理員 user ID

# === 資料處理 ===
def get_member(line_user_id):
    res = supabase.table("members").select("status, member_level").eq("line_user_id", line_user_id).maybe_single().execute()
    return res.data if res and res.data else None

def add_member(line_user_id, code="SET2024"):
    res = supabase.table("members").insert({
        "line_user_id": line_user_id,
        "status": "pending",
        "code": code,
        "member_level": "normal"
    }).execute()
    return res.data

def save_analysis_log(line_user_id, msg_hash, reply):
    supabase.table("analysis_logs").insert({
        "line_user_id": line_user_id,
        "msg_hash": msg_hash,
        "reply": reply,
        "created_at": datetime.utcnow().isoformat()
    }).execute()

def get_previous_reply(line_user_id, msg_hash):
    res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
    return res.data["reply"] if res and res.data else None

def save_signal_stats(signals):
    for s, qty in signals:
        supabase.table("signal_stats").insert({
            "signal_name": s,
            "quantity": qty,
            "created_at": datetime.utcnow().isoformat()
        }).execute()

def update_member_preference(line_user_id, strategy):
    supabase.table("member_preferences").upsert({
        "line_user_id": line_user_id,
        "preferred_strategy": strategy
    }, on_conflict=["line_user_id"]).execute()

def count_today_analyses(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    res = supabase.table("analysis_logs").select("id").eq("line_user_id", user_id).gte("created_at", today).execute()
    return len(res.data) if res.data else 0

def is_vip_member(member_data):
    return member_data and member_data.get("member_level") == "vip"

def log_event(line_user_id, action, detail=""):
    supabase.table("event_logs").insert({
        "line_user_id": line_user_id,
        "action": action,
        "detail": detail,
        "created_at": datetime.utcnow().isoformat()
    }).execute()

def fake_human_like_reply(msg, line_user_id):
    signals_pool = [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("綠倍數球", 1), ("藍倍數球", 1), ("紫倍數球", 1), ("紅倍數球", 1),
        ("聖甲蟲", 3)
    ]
    chosen_signals = random.sample(signals_pool, k=2 if random.random() < 0.5 else 3)
    signal_text = '\n'.join([f"{s[0]}：{random.randint(1, s[1])}顆" for s in chosen_signals])
    save_signal_stats(chosen_signals)

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
        risk = "🚨 高風險"
        strategy = "高風險-建議平轉100轉後觀察"
        advice = "這房可能已被爆分過，建議平轉100轉如回分不好就換房或小買一場免遊試試看。"
    elif risk_score >= 2:
        risk = "⚠️ 中風險"
        strategy = "中風險-小注額觀察"
        advice = "可以先小注額試轉觀察平轉回分狀況，回分可能不錯但仍需謹慎。"
    else:
        risk = "✅ 低風險"
        strategy = "低風險-可屯房買免遊"
        advice = "看起來有機會，建議先進場屯房50-100轉看回分，回分可以的話就買一場免遊看看。"

    update_member_preference(line_user_id, strategy)

    return (
        "⏳ 正在為您分析...\n\n"
        f"📊 初步分析結果如下：\n"
        f"風險評估：{risk}\n"
        f"建議策略：{advice}\n"
        f"推薦訊號組合：\n{signal_text}\n"
        f"✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"
    )

# 省略 quick reply 與 handle_message 的改動以節省篇幅（已整合）
# 請參考 canvas 的完整程式碼內容
