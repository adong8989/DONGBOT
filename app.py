import os
import logging
import re
import random
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, abort

from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, ReplyMessageRequest, FlexMessage, FlexContainer,
    PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging.models import QuickReply, QuickReplyItem, MessageAction
from linebot.v3.exceptions import InvalidSignatureError

# ===== 1. 初始化與配置 =====
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== 2. 工具函數 =====
def get_tz_now(): 
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

# ===== 3. 核心解析邏輯 (針對今日/30天優化) =====
def extract_today_block(text):
    """定位截圖中的『今日』區塊，排除 30 天數據干擾"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if any(k in line for k in ["今日", "得分", "得分率", "下注"]):
            # 抓取該行及其後 8 行
            block = " ".join(lines[i:i+8])
            if "近30天" in block:
                block = block.split("近30天")[0]
            return block
    return text

def parse_ocr_refined(text):
    block = extract_today_block(text)
    
    # 房號辨識
    room = "未知"
    m = re.search(r"(\d{3,5})\s*(?:機台|機台號|機)", text)
    if m: room = m.group(1)
    else:
        nums = re.findall(r"\b\d{4}\b", text)
        if nums: room = nums[-1]

    # RTP 辨識 (放寬百分比符號，過濾合理範圍)
    rtp = 0.0
    rtp_candidates = re.findall(r"(\d{2,3}\.\d+)(?:\s*%?)", block)
    for val in rtp_candidates:
        v = float(val)
        if 50 <= v <= 350:
            rtp = v
            break

    # 今日下注金額 (排除與 RTP 重疊的數字)
    bet = 0.0
    nums_with_decimal = re.findall(r"([\d,]+\.\d{2})", block)
    clean_nums = []
    for n in nums_with_decimal:
        v = float(n.replace(",", ""))
        if abs(v - rtp) > 1.0 and 100 < v < 20000000:
            clean_nums.append(v)
    if clean_nums: bet = min(clean_nums)

    # 未開轉數
    spins = 0
    m_s = re.search(r"未開\s*(\d+)", text)
    if m_s: spins = int(m_s.group(1))

    return room, spins, bet, rtp

# ===== 4. Flex Message 視覺美化 =====
def get_flex_card(room, n, r, b, trend_text, trend_color, seed_hash):
    random.seed(seed_hash)
    if n > 250 or r > 125:
        base_color, label, risk_p, status = "#D50000", "🚨 高風險 / 建議換房", "100%", "high"
    elif n > 150 or r > 110:
        base_color, label, risk_p, status = "#FFAB00", "⚠️ 中風險 / 謹慎進場", "65%", "mid"
    else:
        base_color, label, risk_p, status = "#00C853", "✅ 低風險 / 數據優良", "25%", "low"
    
    items = [("眼睛", 6), ("弓箭", 6), ("權杖", 6), ("紅寶石", 6), ("聖甲蟲", 3)]
    sel = random.sample(items, 2)
    combo = "、".join([f"{name}{random.randint(1, lim)}顆" for name, lim in sel])
    
    tips = {
        "high": [f"❌ 盤面較硬，建議避開此房。"],
        "mid": [f"⚖️ 盤面拉鋸中，若出現「{combo}」可嘗試。"],
        "low": [f"🔥 氣場極強！盤面已出現「{combo}」組合，準備大噴發。"]
    }
    random.seed(None)
    
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"🎰 賽特房號 {room} 分析", "color": "#FFFFFF", "weight": "bold"}], "backgroundColor": base_color},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "lg", "weight": "bold", "color": base_color},
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "box", "layout": "vertical", "width": risk_p, "backgroundColor": base_color, "height": "6px", "cornerRadius": "4px", "contents": []}
            ], "backgroundColor": "#EEEEEE", "height": "6px", "margin": "sm"},
            {"type": "text", "text": trend_text, "size": "sm", "color": trend_color, "weight": "bold"},
            {"type": "separator", "margin": "md"},
            {"type": "box", "layout": "vertical", "spacing": "xs", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "sm"},
                {"type": "text", "text": f"📈 今日 RTP：{r}%", "size": "sm"},
                {"type": "text", "text": f"💰 今日下注：{b:,.2f}", "size": "sm"}
            ]},
            {"type": "box", "layout": "vertical", "backgroundColor": "#F0F0F0", "paddingAll": "8px", "contents": [
                {"type": "text", "text": "🔮 AI 訊號提示", "weight": "bold", "size": "xs"},
                {"type": "text", "text": random.choice(tips[status]), "size": "xs", "wrap": True}
            ]}
        ]}
    }

# ===== 5. 核心處理邏輯 =====
def sync_image_analysis(user_id, message_id, base_limit):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            res = requests.post('https://api.ocr.space/parse/image', 
                                files={'file': ('img.jpg', img_bytes)}, 
                                data={'apikey': OCR_SPACE_API_KEY, 'language': 'chs', 'OCREngine': 2, 'scale': True}, 
                                timeout=15).json()
            
            if res.get("OCRExitCode") != 1: return [TextMessage(text="❌ 辨識系統忙碌。")]
            
            raw_text = res["ParsedResults"][0]["ParsedText"]
            room, n, b, r = parse_ocr_refined(raw_text)
            
            if r <= 0: return [TextMessage(text="❓ 辨識失敗，請確保數據區清晰無遮擋。")]

            today_str = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{int(b)}_{int(r)}"
            
            # 防重算
            dup = supabase.table("usage_logs").select("id").eq("line_user_id", user_id).eq("used_at", today_str).eq("data_hash", data_hash).execute()
            if dup.data: return [TextMessage(text="⚠️ 此截圖今日已分析過。")]

            # 額度處理
            m_res = supabase.table("members").select("extra_limit").eq("line_user_id", user_id).maybe_single().execute()
            extra = m_res.data.get("extra_limit", 0) if m_res.data else 0
            
            is_extra = False
            if extra > 0:
                supabase.table("members").update({"extra_limit": extra - 1}).eq("line_user_id", user_id).execute()
                is_extra = True

            supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "rtp_value": r, "room_id": room, "data_hash": data_hash}).execute()

            # 趨勢
            trend_text, trend_color = "🆕 首次分析", "#888888"
            last = supabase.table("usage_logs").select("rtp_value").eq("room_id", room).order("created_at", desc=True).limit(2).execute()
            if len(last.data) > 1:
                diff = r - float(last.data[1]['rtp_value'])
                trend_text, trend_color = (f"🔥 趨勢上升 (+{diff:.2f}%)", "#D50000") if diff > 0 else (f"❄️ 趨勢下降 ({diff:.2f}%)", "#1976D2")

            used_today = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute().count or 0
            rem = max(0, base_limit + (extra - 1 if is_extra else extra) - used_today + 1)

            return [
                FlexMessage(alt_text="分析結果", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color, data_hash))),
                TextMessage(text=f"📊 剩餘額度：{rem} 次", quick_reply=get_main_menu())
            ]
        except Exception as e:
            logger.error(e)
            return [TextMessage(text="❌ 分析錯誤，請聯繫管理員。")]

# ===== 6. Webhook 路由 =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        is_admin = (user_id == ADMIN_LINE_ID)
        
        # 獲取權限
        m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        is_approved = is_admin
        base_limit = 15
        
        if m_res.data:
            if m_res.data.get("status") == "approved":
                is_approved = True
                base_limit = 50 if m_res.data.get("member_level") == "vip" else 15

        if event.message.type == "text":
            msg = event.message.text.strip()
            # 管理員指令
            if is_admin and msg.startswith("#"):
                if "#核准_" in msg:
                    p = msg.split("_")
                    supabase.table("members").upsert({"line_user_id": p[2], "status": "approved", "member_level": p[1]}, on_conflict="line_user_id").execute()
                    line_api.push_message(PushMessageRequest(to=p[2], messages=[TextMessage(text="✅ 帳號已核准開通！")]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 核准成功")]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                used = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute().count or 0
                extra = m_res.data.get("extra_limit", 0) if m_res.data else 0
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日剩餘：{max(0, base_limit + extra - used)} 次", quick_reply=get_main_menu())]))
            elif msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 申請：#核准_normal_{user_id}")]))
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出。")]))

        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請點選選單「我要開通」以獲得權限。")]))
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=sync_image_analysis(user_id, event.message.id, base_limit)))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
