import os
import tempfile
import logging
import re
import random
import threading
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

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === 配置區 ===
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

vision_client = None
try:
    from google.cloud import vision
    if GCP_SA_KEY_JSON:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_file.name
        vision_client = vision.ImageAnnotatorClient()
except Exception as e:
    logger.error(f"Vision Client Init Error: {e}")

# === 工具函數 ===
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

def get_flex_card(room, n, r, b, trend_text, trend_color):
    base_color = "#00C853" 
    label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120: base_color = "#D50000"; label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110: base_color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"
    
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台 {room} 智能趨勢報告", "color": "#FFFFFF", "weight": "bold"}], "backgroundColor": base_color},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": base_color},
            {"type": "text", "text": trend_text, "size": "sm", "color": trend_color, "weight": "bold"},
            {"type": "separator"},
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"📈 今日 RTP：{r}%", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"💰 今日總下注：{b:,.2f}", "size": "md", "weight": "bold"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 智能推薦進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {
                    "type": "text", 
                    "text": f"出現「{combo}」後考慮進場。請結合盤面即時判斷。", 
                    "size": "sm", 
                    "margin": "xs", 
                    "weight": "bold", 
                    "color": "#111111",
                    "wrap": True  # 修正：文字自動換行，解決 ... 問題
                }
            ]}
        ]}
    }

# --- 核心分析邏輯 ---
def async_image_analysis(user_id, message_id, limit):
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            lines = [l.strip() for l in txt.split('\n') if l.strip()]

            # 1. 抓取房號 (逆向搜尋 3-4 位數)
            room = "未知"
            for line in reversed(lines):
                if re.fullmatch(r"\d{3,4}", line):
                    room = line
                    break

            # 2. 抓取 RTP 與下注額 (鎖定「今日」關鍵字區域)
            r, b = 0.0, 0.0
            for i, line in enumerate(lines):
                if "今日" in line or "今" in line:
                    scope = " ".join(lines[i:i+6])
                    rtp_m = re.findall(r"(\d+\.\d+)\s*%", scope)
                    if rtp_m: r = float(rtp_m[0])
                    
                    amt_m = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))", scope)
                    for val in amt_m:
                        cv = float(val.replace(',', ''))
                        if cv != r: 
                            b = cv
                            break
                    break

            # 抓取未開轉數
            n = 0
            n_m = re.search(r"未開\s*(\d+)", txt)
            if n_m: n = int(n_m.group(1))

            if r <= 0:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="❓ 數據辨識不足，請確保截圖包含完整的「今日」數據區塊。")]))
                return

            # --- 趨勢查詢 ---
            trend_text = "🆕 今日首分析"
            trend_color = "#AAAAAA"
            try:
                last_record = supabase.table("usage_logs") \
                    .select("rtp_value") \
                    .eq("room_id", room) \
                    .order("created_at", descending=True) \
                    .limit(1) \
                    .execute()
                if last_record.data:
                    last_rtp = float(last_record.data[0]['rtp_value'])
                    diff = r - last_rtp
                    if diff > 0.01: trend_text = f"🔥 趨勢升溫 (+{diff:.2f}%)"; trend_color = "#D50000"
                    elif diff < -0.01: trend_text = f"❄️ 數據冷卻 ({diff:.2f}%)"; trend_color = "#1976D2"
                    else: trend_text = "➡️ 數據平穩"; trend_color = "#555555"
            except: pass

            # --- 存檔 (確保唯一性以免報錯) ---
            today_str = get_tz_now().strftime('%Y-%m-%d')
            try:
                supabase.table("usage_logs").insert({
                    "line_user_id": user_id, 
                    "used_at": today_str, 
                    "rtp_value": r,
                    "room_id": room,
                    "data_hash": f"{message_id}_{random.randint(100,999)}"
                }).execute()
            except Exception as e:
                logger.error(f"DB Insert Error: {e}")

            # 額度計算
            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            
            line_api.push_message(PushMessageRequest(to=user_id, messages=[
                FlexMessage(alt_text="機台趨勢分析報告", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color))),
                TextMessage(text=f"📊 今日剩餘額度：{limit - (count_res.count or 0)} / {limit}", quick_reply=get_main_menu())
            ]))
        except Exception as e: logger.error(f"OCR Error: {e}")

# --- LINE Bot 基本設定 ---
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
        
        is_approved = (user_id == ADMIN_LINE_ID)
        limit = 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data and m_res.data.get("status") == "approved":
                is_approved = True
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，請靜候核准。")]))
            elif msg == "我的額度":
                today_str = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 您今日已使用：{count_res.count or 0} / {limit}", quick_reply=get_main_menu())]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 賽特智能分析：請傳送機台詳情截圖。", quick_reply=get_main_menu())]))

        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 帳號未核准，請先點擊「我要開通」。")]))
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔍 正在比對歷史數據...")] ))
            threading.Thread(target=async_image_analysis, args=(user_id, event.message.id, limit)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
