import os
import tempfile
import logging
import io
import re
import json
import hashlib
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, abort, jsonify

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

# === 基礎設定與環境變數 ===
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")

# 風險評估門檻 (保留你原始的設定值)
NOT_OPEN_HIGH = int(os.getenv("NOT_OPEN_HIGH", 250))
NOT_OPEN_MED = int(os.getenv("NOT_OPEN_MED", 150))
NOT_OPEN_LOW = int(os.getenv("NOT_OPEN_LOW", 50))
RTP_HIGH = int(os.getenv("RTP_HIGH", 120))
RTP_MED = int(os.getenv("RTP_MED", 110))
RTP_LOW = int(os.getenv("RTP_LOW", 90))
BETS_HIGH = int(os.getenv("BETS_HIGH", 80000))
BETS_LOW = int(os.getenv("BETS_LOW", 30000))

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
        logger.info("✅ Google Cloud Vision Ready")
    except Exception as e:
        logger.error(f"❌ Vision Init Failed: {e}")

# === 工具函數 ===
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def clean_num(text):
    if not text: return "0"
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', ''))
    return cleaned if cleaned else "0"

# === 修正後的圖片辨識 (使用 MessagingApiBlob) ===
def ocr_extract(message_id, api_client):
    """修正點：使用 MessagingApiBlob 來獲取圖片內容"""
    if not vision_client:
        return None, "OCR 服務未啟動"
    
    try:
        # 修正：SDK v3 下載附件需使用 MessagingApiBlob
        blob_api = MessagingApiBlob(api_client)
        message_content = blob_api.get_message_content(message_id)
        image_bytes = message_content # v3 直接回傳 bytes
        
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        if not full_text: return None, "圖片模糊或找不到文字"

        # 針對截圖優化的提取邏輯
        res = {"未開": "0", "RTP": "0", "總下注": "0"}
        m1 = re.search(r"未開\s*(\d+)", full_text)
        if m1: res["未開"] = m1.group(1)
        
        # 抓取今日數據 (優先搜尋「今日」後方的數字)
        m2 = re.search(r"今日.*?(\d+\.\d+)%", full_text, re.DOTALL)
        if m2: res["RTP"] = m2.group(1)
        
        m3 = re.search(r"今日.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", full_text, re.DOTALL)
        if m3: res["總下注"] = clean_num(m3.group(1))

        return f"未開轉數 : {res['未開']}\n今日RTP%數 : {res['RTP']}\n今日總下注額 : {res['總下注']}", None
    except Exception as e:
        return None, f"⚠️ 辨識過程出錯: {str(e)}"

def get_flex_output(not_open, rtp, bets):
    """保留你原始的風險權重計算邏輯"""
    risk_score = 0
    if not_open > NOT_OPEN_HIGH: risk_score += 2
    elif not_open > NOT_OPEN_MED: risk_score += 1
    elif not_open < NOT_OPEN_LOW: risk_score -= 1

    if rtp > RTP_HIGH: risk_score += 2
    elif rtp > RTP_MED: risk_score += 1
    elif rtp < RTP_LOW: risk_score -= 1

    if bets >= BETS_HIGH: risk_score -= 1
    elif bets < BETS_LOW: risk_score += 1

    colors = ["#00C853", "#FFAB00", "#D50000"]
    labels = ["✅ 低風險", "⚠️ 中風險", "🚨 高風險"]
    lv = 0 if risk_score <= 0 else (1 if risk_score < 3 else 2)
    
    # 推薦訊號
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5), ("紫寶石", 7)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "賽特選房智能分析", "weight": "bold", "color": "#FFFFFF"}
            ], "backgroundColor": colors[lv]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": labels[lv], "size": "xl", "weight": "bold", "color": colors[lv]},
                {"type": "text", "text": f"📍 未開轉數：{not_open}", "size": "sm"},
                {"type": "text", "text": f"📈 今日RTP：{rtp}%", "size": "sm"},
                {"type": "text", "text": f"💰 總下注額：{bets}", "size": "sm"},
                {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F0F0F0", "paddingAll": "10px", "contents": [
                    {"type": "text", "text": "🔮 推薦訊號", "weight": "bold", "size": "xs"},
                    {"type": "text", "text": combo, "size": "sm"}
                ]}
            ]
        }
    }
    return flex_json

# === 資料庫操作 ===
def check_member(line_id):
    res = supabase.table("members").select("*").eq("line_user_id", line_id).maybe_single().execute()
    return res.data if res.data else None

def increment_usage(line_id):
    today = get_tz_now().strftime('%Y-%m-%d')
    res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_id).eq("used_at", today).maybe_single().execute()
    count = (res.data["used_count"] + 1) if res.data else 1
    supabase.table("usage_logs").upsert({"line_user_id": line_id, "used_at": today, "used_count": count}).execute()
    return count

# === Webhook ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        member = check_member(user_id)
        approved = member.get("status") == "approved" if member else False
        lvl = member.get("member_level", "normal") if member else "guest"
        limit = 50 if lvl == "vip" else 15

        input_text = ""
        if event.message.type == "text":
            msg = event.message.text.strip()
            # 自動開通功能
            if msg == "dong8989":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}).execute()
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 帳號已自動開通！")]))
            
            if msg == "我要開通":
                if approved: reply = "您已是會員。"
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                    reply = f"申請中，請洽管理員。\n您的ID: {user_id}"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)]))
            input_text = msg

        elif event.message.type == "image":
            # 傳入 api_client 而非 line_api
            input_text, err = ocr_extract(event.message.id, api_client)
            if err:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=err)]))

        # 分析判斷
        if "RTP" in input_text or "未開" in input_text:
            if not approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="請先開通帳號。")]))
            
            used = increment_usage(user_id)
            if used > limit:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="額度不足。")]))
            
            try:
                n = int(clean_num(re.search(r"未開\s*(\d+)", input_text).group(1)))
                r = float(clean_num(re.search(r"RTP.*?(\d+\.?\d*)", input_text).group(1)))
                b = float(clean_num(re.search(r"下注.*?(\d+\.?\d*)", input_text).group(1)))
                
                flex = get_flex_output(n, r, b)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(flex)),
                        TextMessage(text=f"今日剩餘：{limit - used} 次")
                    ]
                ))
            except:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="數據提取失敗，請確認截圖內容。")]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
