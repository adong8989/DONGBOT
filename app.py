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

# ---------- Google Vision ----------
vision_client = None
if GCP_SA_KEY_JSON:
    from google.cloud import vision
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(GCP_SA_KEY_JSON)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    vision_client = vision.ImageAnnotatorClient()

# ---------- 工具 ----------
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

# ---------- 賽特 OCR 解析 ----------
def parse_seth_ocr(txt: str):
    room = "未知"

    m = re.search(r"(\d{3,5})\s*機台", txt)
    if m:
        room = m.group(1)

    n = 0
    m = re.search(r"未\s*開\s*(\d+)", txt)
    if m:
        n = int(m.group(1))

    r = 0.0
    m = re.search(r"得分率[^\d]{0,5}(\d{2,3}(?:\.\d+)?)\s*%", txt)
    if m:
        r = float(m.group(1))
    else:
        ps = re.findall(r"(\d{2,3}(?:\.\d+)?)\s*%", txt)
        for p in ps:
            v = float(p)
            if 70 <= v <= 200:
                r = v
                break

    b = 0.0
    m = re.search(r"今日[^\d]{0,5}([\d,]+(?:\.\d+)?)", txt)
    if m:
        b = float(m.group(1).replace(",", ""))

    return room, n, b, r

# ---------- LINE Callback ----------
@app.route("/callback", methods=["POST"])
def callback():
    handler.handle(request.get_data(as_text=True), request.headers["X-Line-Signature"])
    return "OK", 200

# ---------- 主事件 ----------
@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # ---------- 權限 ----------
        is_approved = user_id == ADMIN_LINE_ID
        mem = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        if mem.data and mem.data.get("status") == "approved":
            is_approved = True

        limit = 50 if is_approved else 15

        # ---------- 文字 ----------
        if event.message.type == "text":
            msg = event.message.text.strip()

            if msg == "我要開通":
                if is_approved:
                    return line_api.reply_message(
                        ReplyMessageRequest(event.reply_token, [TextMessage(text="✅ 已開通")])
                    )
                supabase.table("members").upsert({
                    "line_user_id": user_id,
                    "status": "pending"
                }).execute()
                return line_api.reply_message(
                    ReplyMessageRequest(event.reply_token, [TextMessage(text="📩 已送出申請")])
                )

            if msg == "我的額度":
                today = get_tz_now().strftime("%Y-%m-%d")
                res = supabase.table("usage_logs") \
                    .select("id", count="exact") \
                    .eq("line_user_id", user_id) \
                    .eq("used_at", today) \
                    .execute()
                used = res.count or 0
                return line_api.reply_message(
                    ReplyMessageRequest(event.reply_token, [
                        TextMessage(text=f"📊 今日使用 {used}/{limit}", quick_reply=get_main_menu())
                    ])
                )

        # ---------- 圖片 ----------
        if event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(
                    ReplyMessageRequest(event.reply_token, [TextMessage(text="⚠️ 尚未開通")])
                )

            image_id = event.message.id
            today = get_tz_now().strftime("%Y-%m-%d")

            # 🔒 防重算
            dup = supabase.table("usage_logs") \
                .select("id") \
                .eq("image_id", image_id) \
                .maybe_single() \
                .execute()
            if dup.data:
                logger.info("重複圖片略過")
                return "OK", 200

            blob = MessagingApiBlob(api_client)
            img_bytes = blob.get_message_content(image_id)

            res = vision_client.document_text_detection(
                image=vision.Image(content=img_bytes)
            )
            txt = res.full_text_annotation.text if res.full_text_annotation else ""

            room, n, b, r = parse_seth_ocr(txt)

            if room == "未知" or b <= 0 or r <= 0:
                return line_api.reply_message(
                    ReplyMessageRequest(event.reply_token, [
                        TextMessage(text="❓ 辨識失敗，請包含下方資訊區")
                    ])
                )

            supabase.table("usage_logs").insert({
                "line_user_id": user_id,
                "used_at": today,
                "image_id": image_id,
                "data_hash": f"{room}_{r}_{b}",
                "rtp_value": r
            }).execute()

            return line_api.reply_message(
                ReplyMessageRequest(event.reply_token, [
                    TextMessage(
                        text=f"🎰 房號 {room}\n📈 RTP {r}%\n💰 今日下注 {int(b):,}",
                        quick_reply=get_main_menu()
                    )
                ])
