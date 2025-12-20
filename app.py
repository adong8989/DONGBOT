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

# === 基礎設定與門檻設定 ===
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")

# 風險評估門檻 (可依需求調整)
NOT_OPEN_HIGH = 250
NOT_OPEN_MED = 150
RTP_HIGH = 120
RTP_MED = 110

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
        logger.info("✅ Google Cloud Vision 服務已啟動")
    except Exception as e:
        logger.error(f"❌ Vision 啟動失敗: {e}")

# === 工具函數 ===
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def clean_num(text):
    if not text: return "0"
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', ''))
    return cleaned if cleaned else "0"

def get_main_menu():
    """定義全局快速選單按鈕"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

# === 核心分析邏輯 ===
def ocr_extract(message_id, api_client):
    """下載 LINE 圖片並透過 Google Vision 辨識文字"""
    if not vision_client:
        return None, "❌ 系統未偵測到 Vision API 金鑰"
    try:
        # SDK v3 下載圖片必須使用 MessagingApiBlob
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id)
        
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        if not full_text: return None, "❌ 辨識失敗，圖片可能太模糊"

        # 針對賽特截圖提取數據
        res = {"未開": "0", "RTP": "0", "總下注": "0"}
        m1 = re.search(r"未開\s*(\d+)", full_text)
        if m1: res["未開"] = m1.group(1)
        m2 = re.search(r"得分率\s*(\d+\.\d+)%", full_text)
        if m2: res["RTP"] = m2.group(1)
        m3 = re.search(r"總下注額\s*([\d,]+\.\d+)", full_text)
        if m3: res["總下注"] = clean_num(m3.group(1))

        formatted = f"未開轉數 : {res['未開']}\n今日RTP%數 : {res['RTP']}\n今日總下注額 : {res['總下注']}"
        return formatted, None
    except Exception as e:
        return None, f"⚠️ 辨識過程出錯: {str(e)}"

def get_flex_card(n, r, b):
    """根據數據生成風險卡片"""
    color = "#00C853" # 預設綠色 (低風險)
    label = "✅ 低風險 / 數據優異"
    
    if n > NOT_OPEN_HIGH or r > RTP_HIGH:
        color = "#D50000" # 紅色
        label = "🚨 高風險 / 建議觀察"
    elif n > NOT_OPEN_MED or r > RTP_MED:
        color = "#FFAB00" # 橘色
        label = "⚠️ 中風險 / 謹慎進場"
        
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5), ("紫寶石", 7)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "賽特選房智能分析", "weight": "bold", "color": "#FFFFFF", "size": "md"}
            ], "backgroundColor": color
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "contents": [
                {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": color},
                {"type": "separator"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                    {"type": "text", "text": f"📍 未開轉數：{n}", "size": "sm"},
                    {"type": "text", "text": f"📈 今日RTP：{r}%", "size": "sm"},
                    {"type": "text", "text": f"💰 總下注額：{b}", "size": "sm"}
                ]},
                {"type": "box", "layout": "vertical", "backgroundColor": "#F0F0F0", "paddingAll": "10px", "contents": [
                    {"type": "text", "text": "💡 推薦進場訊號", "weight": "bold", "size": "xs"},
                    {"type": "text", "text": combo, "size": "sm", "margin": "xs"}
                ]}
            ]
        }
    }
    return flex_json

# === Webhook 與訊息處理 ===
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
        
        # 檢查會員狀態
        member_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
        member = member_res.data
        is_approved = member.get("status") == "approved" if member else False
        lvl = member.get("member_level", "normal") if member else "guest"
        limit = 50 if lvl == "vip" else 15

        analysis_input = ""

        # 1. 處理文字指令
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            if msg == "dong8989":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}).execute()
                return line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token, 
                    messages=[TextMessage(text="✅ 帳號已自動開通正式權限！", quick_reply=get_main_menu())]
                ))
            
            if msg == "使用說明":
                guide = "📘 賽特選房助手：\n1. 直接傳送機台截圖。\n2. 系統自動抓取今日數據。\n3. 提供紅/黃/綠燈風險建議。"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=guide, quick_reply=get_main_menu())]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).maybe_single().execute()
                used = u_res.data["used_count"] if u_res.data else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析次數：{used} / {limit}", quick_reply=get_main_menu())]))

            if msg == "我要開通":
                if is_approved: reply = "您已是正式會員。"
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                    reply = f"申請已送出，請連繫管理員。\nID: {user_id}"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply, quick_reply=get_main_menu())]))
            
            analysis_input = msg

        # 2. 處理圖片辨識
        elif event.message.type == "image":
            analysis_input, err = ocr_extract(event.message.id, api_client)
            if err:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=err, quick_reply=get_main_menu())]))

        # 3. 分析與發送 Flex 卡片
        if "RTP" in analysis_input or "未開" in analysis_input:
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 帳號未開通，請先點選下方「我要開通」或輸入密碼。", quick_reply=get_main_menu())]))
            
            # 記錄使用量
            today = get_tz_now().strftime('%Y-%m-%d')
            u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).maybe_single().execute()
            count = (u_res.data["used_count"] + 1) if u_res.data else 1
            supabase.table("usage_logs").upsert({"line_user_id": user_id, "used_at": today, "used_count": count}).execute()
            
            if count > limit:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="今日分析額度已用完。")]))

            try:
                # 提取數值
                n = int(clean_num(re.search(r"未開\s*(\d+)", analysis_input).group(1)))
                r = float(clean_num(re.search(r"RTP.*?(\d+\.?\d*)", analysis_input).group(1)))
                b = float(clean_num(re.search(r"下注.*?(\d+\.?\d*)", analysis_input).group(1)))
                
                # 生成卡片並發送
                flex_content = get_flex_card(n, r, b)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(flex_content)),
                        TextMessage(text=f"📊 今日已分析：{count} / {limit} 次", quick_reply=get_main_menu())
                    ]
                ))
            except Exception as e:
                logger.error(f"解析失敗: {e}")
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 數據解析失敗，請確保截圖顯示今日得分率。", quick_reply=get_main_menu())]))
        else:
            # 沒傳圖片或關鍵字時的回覆
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請傳送「賽特選房截圖」進行即時分析！", quick_reply=get_main_menu())]
            ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
