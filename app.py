import os
import tempfile
import logging
import re
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request

from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, ReplyMessageRequest, FlexMessage, FlexContainer,
    PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging.models import QuickReply, QuickReplyItem, MessageAction

# ---------- 基本設定 ----------
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Google Vision 初始化 ----------
vision_client = None
if GCP_SA_KEY_JSON:
    try:
        from google.cloud import vision
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(GCP_SA_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        vision_client = vision.ImageAnnotatorClient()
    except Exception as e:
        logger.error(f"Vision Client Error: {e}")

# ---------- 工具函式 ----------
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

# ---------- 核心解析邏輯 ----------
def parse_seth_ocr(txt: str):
    room = "未知"
    n = 0
    b = 0.0
    r = 0.0

    room_match = re.search(r"(\d{4})\s*機台", txt)
    if room_match:
        room = room_match.group(1)
    else:
        rooms = re.findall(r"\b\d{4}\b", txt)
        if rooms: room = rooms[-1]

    n_match = re.search(r"未\s*開\s*(\d+)", txt)
    if n_match:
        n = int(n_match.group(1))

    rtp_list = re.findall(r"(\d{1,3}\.\d{2})\s*%", txt)
    if rtp_list:
        r = float(rtp_list[0])

    bet_patterns = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", txt)
    valid_bets = []
    for val in bet_patterns:
        clean_val = float(val.replace(',', ''))
        if clean_val != r and clean_val != float(room if room.isdigit() else 0):
            if 10 < clean_val < 5000000:
                valid_bets.append(clean_val)
    
    if valid_bets:
        b = valid_bets[0]

    return room, n, b, r

# ---------- 回覆卡片樣式 ----------
def get_flex_card(room, n, r, b, trend):
    color = "#4CAF50"
    status = "✅ 數據優異"
    if n > 200 or r > 120: 
        color = "#F44336"; status = "🚨 風險偏高"
    elif n > 100 or r > 110: 
        color = "#FFC107"; status = "⚠️ 觀察進場"

    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台分析: {room}", "weight": "bold", "color": "#FFFFFF"}], "backgroundColor": color},
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": status, "weight": "bold", "size": "xl", "color": color},
            {"type": "separator", "margin": "md"},
            {"type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": [
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📍 未開轉數"}, {"type": "text", "text": f"{n} 轉", "align": "end", "weight": "bold"}]},
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📈 今日 RTP"}, {"type": "text", "text": f"{r}%", "align": "end", "weight": "bold"}]},
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "💰 今日下注"}, {"type": "text", "text": f"{int(b):,} 元", "align": "end", "weight": "bold"}]}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F0F0F0", "paddingAll": "sm", "contents": [
                {"type": "text", "text": trend, "size": "xs", "color": "#666666"}
            ]}
        ]}
    }

# ---------- LINE Callback ----------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        logger.error(f"Callback Error: {e}")
    return "OK", 200

# ---------- 訊息事件處理 ----------
@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 1. 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        mem = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        if mem.data and mem.data.get("status") == "approved":
            is_approved = True
        
        limit = 50 if is_approved else 15

        # 2. 文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                if is_approved:
                    return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="✅ 您已開通權限。")]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="📩 申請已送出，請靜候核准。")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime("%Y-%m-%d")
                res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                used = res.count if res.count else 0
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text=f"📊 今日使用：{used}/{limit}", quick_reply=get_main_menu())]))

        # 3. 圖片訊息 (解析核心 + 效能優化順序)
        if event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="⚠️ 尚未開通，請先點選『我要開通』。")]))

            try:
                # 取得圖片內容
                blob_api = MessagingApiBlob(api_client)
                img_bytes = blob_api.get_message_content(event.message.id)
                
                # OCR 辨識
                res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
                txt = res.full_text_annotation.text if res.full_text_annotation else ""
                
                room, n, b, r = parse_seth_ocr(txt)
                if r <= 0:
                    return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="❓ 無法辨識機台數據，請確保截圖完整。")]))

                # --- 趨勢分析 (放在回覆前，但僅執行一次快速 Query) ---
                trend = "📊 房間初次分析"
                try:
                    prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").order("created_at", desc=True).limit(1).execute()
                    if prev.data:
                        diff = r - float(prev.data[0]['rtp_value'])
                        trend = f"📈 較上次：{'上升' if diff >= 0 else '下降'} {abs(diff):.2f}%"
                except: pass

                # --- 關鍵：先執行 LINE 回覆，避免 Reply Token 過期 ---
                flex_content = get_flex_card(room, n, r, b, trend)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="機台分析報告", contents=FlexContainer.from_dict(flex_content)),
                        TextMessage(text="點擊下方選單查看更多功能", quick_reply=get_main_menu())
                    ]
                ))

                # --- 回覆完後，再寫入資料庫 ---
                today = get_tz_now().strftime("%Y-%m-%d")
                supabase.table("usage_logs").insert({
                    "line_user_id": user_id,
                    "used_at": today,
                    "rtp_value": r,
                    "data_hash": f"{room}_{r}_{b}_{get_tz_now().timestamp()}"
                }).execute()

            except Exception as e:
                logger.error(f"Image Process Error: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
