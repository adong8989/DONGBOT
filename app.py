import os
import tempfile
import logging
import re
import random
import threading
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request

from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, PushMessageRequest, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging.models import QuickReply, QuickReplyItem, MessageAction

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === 配置 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Google Vision 初始化 ===
vision_client = None
if GCP_SA_KEY_JSON:
    try:
        from google.cloud import vision
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as tmp: tmp.write(GCP_SA_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        vision_client = vision.ImageAnnotatorClient()
    except Exception as e: logger.error(f"Vision Init Error: {e}")

# === 工具函式 ===
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def parse_seth_ocr(txt: str):
    room, n, b, r = "0000", 0, 0.0, 0.0
    room_m = re.search(r"(\d{4})\s*機台", txt)
    if room_m: room = room_m.group(1)
    n_m = re.search(r"未\s*開\s*(\d+)", txt)
    if n_m: n = int(n_m.group(1))

    sections = re.split(r"今日|近30天", txt)
    target = sections[1] if len(sections) > 1 else txt
    
    rtps = re.findall(r"(\d{1,3}\.\d{2})\s*%", target)
    if rtps: r = float(rtps[0])
    
    nums = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", target)
    for v in nums:
        cv = float(v.replace(',', ''))
        if cv != r and cv != float(room if room.isdigit() else 0):
            if cv > 10: b = cv; break
    return room, n, b, r

def get_flex_card(room, n, r, b, trend):
    color = "#4CAF50"
    if n > 250 or r > 120: color = "#F44336"
    elif n > 150 or r > 110: color = "#FFC107"
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台分析: {room}", "weight": "bold", "color": "#FFFFFF", "align": "center"}], "backgroundColor": color},
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📍 未開轉數"}, {"type": "text", "text": f"{n} 轉", "weight": "bold", "align": "end"}]},
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📈 今日 RTP"}, {"type": "text", "text": f"{r}%", "weight": "bold", "align": "end"}]},
                {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "💰 今日下注"}, {"type": "text", "text": f"{int(b):,} 元", "weight": "bold", "align": "end"}]}
            ]},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": trend, "margin": "md", "size": "sm", "weight": "bold"},
            {"type": "text", "text": f"🔮 推薦：{combo}", "size": "xs", "color": "#388E3C", "margin": "sm"}
        ]}
    }

# === 背景處理邏輯 ===
def async_process_image(user_id, message_id):
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)
        try:
            # 1. OCR 辨識
            img_bytes = blob_api.get_message_content(message_id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            room, n, b, r = parse_seth_ocr(txt)
            
            if r <= 0:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="❌ 辨識失敗，請提供更清晰的截圖。")]))
                return

            # 2. 資料庫操作
            today = get_tz_now().strftime('%Y-%m-%d')
            fp = f"{room}_{n}_{b}_{r}"
            try:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
            except:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="🚫 此圖片已分析過，請勿重複傳送。")]))
                return

            # 3. 趨勢與發送
            trend = "📊 初次分析"
            prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
            if prev.data:
                diff = r - float(prev.data[0]['rtp_value'])
                trend = f"📈 較上次：{'上升' if diff >= 0 else '下降'} {abs(diff):.2f}%"

            flex_content = get_flex_card(room, n, r, b, trend)
            line_api.push_message(PushMessageRequest(to=user_id, messages=[FlexMessage(alt_text="分析結果", contents=FlexContainer.from_dict(flex_content))]))
            
        except Exception as e:
            logger.error(f"Async Error: {e}")

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except Exception as e: logger.error(f"Callback Error: {e}")
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    if event.message.type == "text":
        # ... (文字邏輯簡化處理)
        pass
    elif event.message.type == "image":
        # 關鍵點：開啟新執行緒，並立即回傳 OK 給 LINE
        threading.Thread(target=async_process_image, args=(user_id, event.message.id)).start()
        return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
