import os
import tempfile
import logging
import re
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, abort

# Supabase & LINE SDK v3
from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, ReplyMessageRequest, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging.models import (
    QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.exceptions import InvalidSignatureError

# Google Cloud Vision
try:
    from google.cloud import vision
except ImportError:
    vision = None

# === 基礎設定 ===
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Vision Client 初始化
vision_client = None
if GCP_SA_KEY_JSON and vision:
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_file.name
        vision_client = vision.ImageAnnotatorClient()
        logger.info("✅ Google Cloud Vision 服務已就緒")
    except Exception as e:
        logger.error(f"❌ Vision 初始化失敗: {e}")

# === 工具函數 ===
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def clean_num(text):
    if not text: return "0"
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', ''))
    return cleaned if cleaned else "0"

# 定義統一的快速選單
def get_global_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

# === 數據抓取與 Flex 生成 (縮減版，邏輯同前) ===
def ocr_extract(message_id, api_client):
    try:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        if not full_text: return None, "圖片模糊或找不到文字"
        res = {"未開": "0", "RTP": "0", "總下注": "0"}
        m1 = re.search(r"未開\s*(\d+)", full_text)
        if m1: res["未開"] = m1.group(1)
        m2 = re.search(r"得分率\s*(\d+\.\d+)%", full_text)
        if m2: res["RTP"] = m2.group(1)
        m3 = re.search(r"總下注額\s*([\d,]+\.\d+)", full_text)
        if m3: res["總下注"] = clean_num(m3.group(1))
        return f"未開轉數 : {res['未開']}\n今日RTP%數 : {res['RTP']}\n今日總下注額 : {res['總下注']}", None
    except Exception as e: return None, f"⚠️ 辨識過程出錯: {str(e)}"

def get_flex_output(not_open, rtp, bets):
    # (此處省略詳細 Flex JSON 以節省空間，與前一版本一致)
    # 風險評估邏輯與顏色判斷...
    return { "type": "bubble", "header": { "type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "分析報告", "color": "#FFFFFF"}] }, "body": { "type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"RTP: {rtp}%", "weight": "bold"}] } }

# === Webhook 處理 ===
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
        
        # 檢查會員
        res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        member = res.data
        is_approved = member.get("status") == "approved" if member else False
        limit = 50 if (member and member.get("member_level") == "vip") else 15

        analysis_input = ""

        # 1. 處理文字指令
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            if msg == "dong8989":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}).execute()
                return line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token, 
                    messages=[TextMessage(text="✅ 帳號已自動開通！", quick_reply=get_global_quick_reply())]
                ))
            
            if msg == "使用說明":
                guide = "📘 賽特選房助手使用說明：\n1. 直接傳送房間資訊截圖\n2. 系統會自動辨識今日 RTP 與總下注\n3. 提供紅/黃/綠燈風險評估與訊號組合。"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=guide, quick_reply=get_global_quick_reply())]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                usage = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).maybe_single().execute()
                count = usage.data["used_count"] if usage.data else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日使用統計：{count} / {limit}", quick_reply=get_global_quick_reply())]))

            if msg == "我要開通":
                if is_approved: reply = "您已是會員。"
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                    reply = f"申請已送出，UserID: {user_id}"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply, quick_reply=get_global_quick_reply())]))
            
            analysis_input = msg

        # 2. 處理圖片
        elif event.message.type == "image":
            analysis_input, err = ocr_extract(event.message.id, api_client)
            if err: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=err)]))

        # 3. 執行分析
        if "RTP" in analysis_input or "未開" in analysis_input:
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先開通帳號。")]))
            
            # (省略紀錄次數與 Flex 生成邏輯，與前一版相同)
            # ...
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="分析完成！(此處會發送 Flex 卡片)", quick_reply=get_global_quick_reply())]
            ))
        else:
            # 這裡就是你的快速選單預設位置
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="歡迎使用賽特選房助手！請傳送截圖或選擇下方功能：", quick_reply=get_global_quick_reply())]
            ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
