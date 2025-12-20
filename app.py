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

# 風險判定門檻
NOT_OPEN_HIGH = 250
NOT_OPEN_MED = 150
RTP_HIGH = 120
RTP_MED = 110

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

vision_client = None
if GCP_SA_KEY_JSON and vision:
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_file.name
        vision_client = vision.ImageAnnotatorClient()
        logger.info("✅ Google Vision 準備就緒")
    except Exception as e:
        logger.error(f"❌ Vision 啟動錯誤: {e}")

# === 工具函數 ===
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def clean_num(text):
    if not text: return "0"
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', ''))
    return cleaned if cleaned else "0"

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

def ocr_extract(message_id, api_client):
    try:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        if response.error.message:
            return None, f"Google API 錯誤: {response.error.message}"
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        return full_text, None
    except Exception as e:
        return None, str(e)

def get_flex_card(n, r, b):
    color = "#00C853" # 綠
    label = "✅ 低風險 / 數據優異"
    if n > NOT_OPEN_HIGH or r > RTP_HIGH:
        color = "#D50000" # 紅
        label = "🚨 高風險 / 建議換房"
    elif n > NOT_OPEN_MED or r > RTP_MED:
        color = "#FFAB00" # 橘
        label = "⚠️ 中風險 / 謹慎進場"
        
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5), ("紫寶石", 7)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])

    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "賽特選房智能分析", "color": "#FFFFFF", "weight": "bold"}], "backgroundColor": color},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": color},
            {"type": "separator"},
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "sm"},
                {"type": "text", "text": f"📈 今日RTP：{r}%", "size": "sm"},
                {"type": "text", "text": f"💰 今日總下注：{b}", "size": "sm"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 推薦進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": combo, "size": "sm", "margin": "xs", "weight": "bold"}
            ]}
        ]}
    }

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
        
        m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        member = m_res.data
        is_approved = member.get("status") == "approved" if member else False
        limit = 50 if (member and member.get("member_level") == "vip") else 15

        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "dong8989":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}).execute()
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 帳號自動開通成功！", quick_reply=get_main_menu())]))
            
            if msg == "使用說明":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="傳送機台截圖，系統會自動分析下方黑色區域的今日數據。", quick_reply=get_main_menu())]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                usage = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).maybe_single().execute()
                count = usage.data["used_count"] if usage.data else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{count} / {limit}", quick_reply=get_main_menu())]))

            if msg == "我要開通":
                if is_approved: reply = "您已是正式會員。"
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                    reply = f"申請已送出。UserID: {user_id}"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply, quick_reply=get_main_menu())]))

        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先開通帳號。", quick_reply=get_main_menu())]))
            
            full_text, err = ocr_extract(event.message.id, api_client)
            if err: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ OCR 錯誤: {err}")]))
            
            # 移除所有空白以防斷字
            flat_text = "".join(full_text.split())
            
            try:
                # 1. 抓未開 (通常在最前面或中間)
                n = 0
                n_match = re.search(r"未開(\d+)", flat_text)
                if n_match: n = int(n_match.group(1))

                # 2. 抓數據策略：鎖定最後出現的數據 (今日與近30天)
                r = 0.0
                b = 0.0
                
                # 尋找所有百分比 (RTP)
                all_pcts = re.findall(r"(\d+\.\d+)%", flat_text)
                # 尋找所有金額格式 (下注額)
                all_amounts = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", flat_text)

                if len(all_pcts) >= 2:
                    # 倒數第2個通常是今日，倒數第1個是近30天
                    r = float(all_pcts[-2])
                elif all_pcts:
                    r = float(all_pcts[0])

                if len(all_amounts) >= 2:
                    # 同理，下注額取倒數第2個
                    b = float(clean_num(all_amounts[-2]))
                elif all_amounts:
                    b = float(clean_num(all_amounts[0]))

                if r == 0.0:
                    # 最終備案：如果還是沒抓到，噴出最後50字除錯
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ 數據抓取失敗。\n辨識片段：{flat_text[-50:]}", quick_reply=get_main_menu())]))

                # 紀錄並發送
                today = get_tz_now().strftime('%Y-%m-%d')
                u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).maybe_single().execute()
                new_count = (u_res.data["used_count"] + 1) if u_res.data else 1
                supabase.table("usage_logs").upsert({"line_user_id": user_id, "used_at": today, "used_count": new_count}).execute()

                flex = get_flex_card(n, r, b)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(flex)),
                        TextMessage(text=f"📊 今日分析：{new_count} / {limit}", quick_reply=get_main_menu())
                    ]
                ))
            except Exception as e:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ 解析失敗: {str(e)}", quick_reply=get_main_menu())]))
        
        else:
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="請傳送截圖開始分析！", quick_reply=get_main_menu())]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
