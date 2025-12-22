import os
import tempfile
import logging
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request

from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, ReplyMessageRequest, FlexMessage, FlexContainer
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

# ---------- 核心解析邏輯 (區域定位強化版) ----------
def parse_seth_ocr(txt: str):
    room = "未知"
    n = 0
    b = 0.0
    r = 0.0

    # 1. 房號辨識
    room_match = re.search(r"(\d{4})\s*機台", txt)
    if room_match:
        room = room_match.group(1)
    else:
        rooms = re.findall(r"\b\d{4}\b", txt)
        if rooms: room = rooms[-1]

    # 2. 未開轉數
    n_match = re.search(r"未\s*開\s*(\d+)", txt)
    if n_match:
        n = int(n_match.group(1))

    # 3. 數據區域切分 (精準定位今日數據)
    try:
        # 賽特 UI 特徵：今日數據夾在「今日」與「近30天」關鍵字之間
        if "今日" in txt:
            # 取得「今日」標籤後的內容，並在「近30天」處截斷
            today_section = txt.split("今日")[1].split("近30天")[0]
        else:
            today_section = txt

        # --- 在今日區域內找 RTP (%) ---
        rtp_match = re.search(r"(\d{1,3}\.\d{2})\s*%", today_section)
        if rtp_match:
            r = float(rtp_match.group(1))

        # --- 在今日區域內找下注額 ---
        # 找尋所有格式正確的數字 (含逗號或小數點)
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", today_section)
        for val in nums:
            clean_val = float(val.replace(',', ''))
            # 排除掉剛抓到的 RTP 數值、房號以及未開轉數
            if clean_val != r and clean_val != float(room if room.isdigit() else 0) and clean_val != float(n):
                # 今日下注通常大於 10 且小於 500 萬 (避開近30天的大數)
                if 10 < clean_val < 5000000:
                    b = clean_val
                    break
    except Exception as e:
        logger.error(f"Section Parse Error: {e}")

    # 備援邏輯：如果區域切分失敗導致沒抓到，改用全域抓取第一組符合合理範圍的數值
    if r == 0:
        rtps = re.findall(r"(\d{1,3}\.\d{2})\s*%", txt)
        if rtps: r = float(rtps[0])
    if b == 0:
        bets = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", txt)
        for val in bets:
            cv = float(val.replace(',', ''))
            if cv != r and 10 < cv < 3000000:
                b = cv
                break

    return room, n, b, r

# ---------- 卡片樣式 ----------
def get_flex_card(room, n, r, b, trend):
    # 判斷顏色邏輯
    color = "#4CAF50" # 綠色 (優)
    status = "✅ 數據優異"
    if n > 200 or r > 120: 
        color = "#F44336" # 紅色 (危)
        status = "🚨 風險偏高"
    elif n > 100 or r > 110: 
        color = "#FFC107" # 黃色 (警)
        status = "⚠️ 觀察進場"

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

# ---------- 回呼路由 ----------
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

        # 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        mem = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        if mem.data and mem.data.get("status") == "approved":
            is_approved = True
        
        limit = 50 if is_approved else 15

        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                if is_approved:
                    return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="✅ 您已開通。")]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="📩 申請已送出，請等待管理員核可。")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime("%Y-%m-%d")
                res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                used = res.count if res.count else 0
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text=f"📊 今日使用額度：{used}/{limit}", quick_reply=get_main_menu())]))

        if event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="⚠️ 尚未開通使用權限，請點選選單申請。")]))

            try:
                # 取得圖片內容
                blob_api = MessagingApiBlob(api_client)
                img_bytes = blob_api.get_message_content(event.message.id)
                
                # Google Vision OCR 辨識
                res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
                txt = res.full_text_annotation.text if res.full_text_annotation else ""
                
                # 解析數據
                room, n, b, r = parse_seth_ocr(txt)
                if r <= 0:
                    return line_api.reply_message(ReplyMessageRequest(event.reply_token, [TextMessage(text="❓ 無法讀取數據，請確保截圖包含完整的詳情面板。")]))

                # 趨勢計算
                trend = "📊 房間初次分析"
                try:
                    prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").order("created_at", desc=True).limit(1).execute()
                    if prev.data:
                        diff = r - float(prev.data[0]['rtp_value'])
                        trend = f"📈 較上次分析：{'上升' if diff >= 0 else '下降'} {abs(diff):.2f}%"
                except: pass

                # 【立即回覆】避免 LINE 伺服器逾時
                flex_content = get_flex_card(room, n, r, b, trend)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="機台分析報告", contents=FlexContainer.from_dict(flex_content)),
                        TextMessage(text="您可以繼續上傳截圖或查看額度", quick_reply=get_main_menu())
                    ]
                ))

                # 回覆完畢後再異步存入資料庫
                today = get_tz_now().strftime("%Y-%m-%d")
                supabase.table("usage_logs").insert({
                    "line_user_id": user_id,
                    "used_at": today,
                    "rtp_value": r,
                    "data_hash": f"{room}_{r}_{b}_{get_tz_now().timestamp()}"
                }).execute()

            except Exception as e:
                logger.error(f"OCR/DB Error: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
