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
from datetime import datetime

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

# === 主要邏輯與函式 ===
def get_member(line_user_id):
    res = supabase.table("members").select("status, usage_quota, last_reset_at, line_user_id").eq("line_user_id", line_user_id).maybe_single().execute()
    return res.data if res and res.data else None

def reset_quota_if_needed(member):
    try:
        last_reset_str = member.get("last_reset_at")
        if not last_reset_str:
            return member
        last_reset = datetime.fromisoformat(last_reset_str.replace("Z", "+00:00"))
        now = datetime.utcnow()
        if last_reset.date() < now.date():
            supabase.table("members").update({
                "usage_quota": 50,
                "last_reset_at": now.isoformat()
            }).eq("line_user_id", member["line_user_id"]).execute()
            member["usage_quota"] = 50
            member["last_reset_at"] = now.isoformat()
        return member
    except Exception as e:
        print(f"[錯誤] reset_quota_if_needed: {e}")
        return member

def add_member(line_user_id, code="SET2024"):
    res = supabase.table("members").insert({
        "line_user_id": line_user_id,
        "status": "pending",
        "code": code,
        "usage_quota": 50,
        "last_reset_at": datetime.utcnow().isoformat()
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

def update_member_preference(line_user_id, strategy):
    supabase.table("member_preferences").upsert({
        "line_user_id": line_user_id,
        "preferred_strategy": strategy
    }, on_conflict=["line_user_id"]).execute()

def fake_human_like_reply(msg, line_user_id):
    signals_pool = [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("綠倍數球", 1), ("藍倍數球", 1), ("紫倍數球", 1), ("紅倍數球", 1),
        ("聖甲蟲", 3)
    ]
    chosen = random.sample(signals_pool, k=2 if random.random() < 0.5 else 3)
    signals = '\n'.join([f"{s[0]}：{random.randint(1, s[1])}顆" for s in chosen])

    try:
        lines = {line.split(':')[0].strip(): line.split(':')[1].strip() for line in msg.split('\n') if ':' in line}
        not_open = int(lines.get("未開轉數", 0))
        prev1 = int(lines.get("前一轉開", 0))
        prev2 = int(lines.get("前二轉開", 0))
        rtp_today = int(lines.get("今日RTP%數", 0))
        bets_today = int(lines.get("今日總下注額", 0))
        rtp_30 = int(lines.get("30日RTP%數", 0))
        bets_30 = int(lines.get("30日總下注額", 0))
    except:
        return "❌ 分析失敗，請確認格式與數值是否正確。"

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
        risk_level = random.choice(["🚨 高風險", "🔥 可能爆分過", "⚠️ 危險等級高"])
        strategies = [
            "高風險 - 建議平轉 100 轉後觀察",
            "高風險 - 小心進場，觀察平轉回分",
            "高風險 - 建議試水溫轉轉看"
        ]
        advices = [
            "這房可能已經被吃分或爆分過，建議你先用 100 轉觀察回分情況。",
            "風險偏高，不建議立即大注投入，可先試探性小額下注。",
            "此類型 RTP 組合不太妙，建議觀察回分後再做決定。"
        ]
    elif score >= 2:
        risk_level = random.choice(["⚠️ 中風險", "🟠 風險可控", "📉 中等偏穩"])
        strategies = [
            "中風險 - 小注額觀察",
            "中風險 - 觀察型打法",
            "中風險 - 可視情況試免遊"
        ]
        advices = [
            "可以先小額下注觀察，回分還不錯就再進一步。",
            "此房間 RTP 有一定潛力，但建議保守試轉。",
            "整體偏中性，觀察幾轉後再決定是否屯房或免遊。"
        ]
    else:
        risk_level = random.choice(["✅ 低風險", "🟢 穩定場", "💎 安全房"])
        strategies = [
            "低風險 - 可屯房買免遊",
            "低風險 - 可直接嘗試免遊策略",
            "低風險 - 推薦屯房後試免遊"
        ]
        advices = [
            "整體數據良好，建議進場屯房 50-100 轉觀察回分後買免遊。",
            "是個不錯的機會房間，建議穩紮穩打進場。",
            "回分條件佳，可考慮免遊開局。"
        ]

    strategy = random.choice(strategies)
    advice = random.choice(advices)
    update_member_preference(line_user_id, strategy)

    return (
        f"📊 初步分析結果如下：\n"
        f"風險評估：{risk_level}\n"
        f"建議策略：{advice}\n"
        f"推薦訊號組合：\n{signals}\n"
        f"✨ 若需進一步打法策略，可聯絡阿東超人：LINE ID adong8989"
    )

# 其餘 webhook 與訊息處理邏輯略
# 已整合 reset_quota_if_needed 功能於會員查詢後每日更新使用次數
