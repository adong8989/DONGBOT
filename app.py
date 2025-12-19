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
    Configuration, ApiClient, MessagingApi, 
    TextMessage, ReplyMessageRequest, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging.models import (
    QuickReply, QuickReplyItem, MessageAction, URIAction
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

# 環境變數載入
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "")
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON")

# 初始化客戶端
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Vision Client 初始化 (處理圖片 OCR)
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

# === 工具函式 ===

def get_tz_now():
    """獲取台北時間 (UTC+8)"""
    return datetime.now(timezone(timedelta(hours=8)))

def clean_num(text):
    """提取字串中的純數字與小數點"""
    if not text: return "0"
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', ''))
    return cleaned if cleaned else "0"

# === 核心分析與 UI 邏輯 ===

def ocr_extract(message_id, messaging_api):
    """從截圖中提取關鍵數據 (未開、RTP、下注)"""
    if not vision_client:
        return None, "❌ 系統未啟用 OCR 功能"
    try:
        with messaging_api.get_message_content(message_id) as content:
            image_bytes = b"".join(content.read_chunk())
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        if not full_text: return None, "❌ 圖片辨識失敗，請確保圖片清晰"

        res = {"未開": "0", "RTP": "0", "總下注": "0"}
        m1 = re.search(r"未開.*?(\d+)", full_text)
        if m1: res["未開"] = m1.group(1)
        m2 = re.search(r"(RTP|得分率).*?(\d+\.?\d*)", full_text, re.I)
        if m2: res["RTP"] = m2.group(2)
        m3 = re.search(r"(下注|Total).*?(\d[\d,.]*)", full_text, re.I)
        if m3: res["總下注"] = clean_num(m3.group(2))

        formatted_text = f"未開轉數 : {res['未開']}\n今日RTP%數 : {res['RTP']}\n今日總下注額 : {res['總下注']}"
        return formatted_text, None
    except Exception as e:
        return None, f"⚠️ 辨識過程出錯: {str(e)}"

def get_flex_output(not_open, rtp, bets):
    """生成專業的分析結果卡片 (Flex Message)"""
    score = 0
    if not_open > 250: score += 1
    if rtp > 115: score += 1
    if bets < 30000: score += 1
    
    colors = ["#00C853", "#FFAB00", "#D50000"] # 綠(低風險), 橘(中), 紅(高)
    labels = ["✅ 低風險 / 建議操作", "⚠️ 中風險 / 小心試探", "🚨 高風險 / 建議觀察"]
    lv = min(score, 2)
    
    # 隨機訊號生成
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5), ("刀子", 7)]
    chosen = random.sample(s_pool, 2)
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in chosen])

    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "賽特選房智能分析報告", "weight": "bold", "color": "#FFFFFF", "size": "md"}
            ], "backgroundColor": colors[lv]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md", "contents": [
                {"type": "text", "text": labels[lv], "size": "xl", "weight": "bold", "color": colors[lv]},
                {"type": "separator"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "未開轉數", "color": "#888888", "size": "sm"},
                        {"type": "text", "text": str(not_open), "align": "end", "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "今日 RTP", "color": "#888888", "size": "sm"},
                        {"type": "text", "text": f"{rtp}%", "align": "end", "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "總下注額", "color": "#888888", "size": "sm"},
                        {"type": "text", "text": str(bets), "align": "end", "size": "sm", "weight": "bold"}
                    ]}
                ]},
                {"type": "box", "layout": "vertical", "backgroundColor": "#F8F9FA", "paddingAll": "12px", "cornerRadius": "md", "contents": [
                    {"type": "text", "text": "🔮 推薦進場訊號", "weight": "bold", "size": "xs", "color": "#444444"},
                    {"type": "text", "text": combo, "size": "sm", "margin": "xs", "color": "#111111", "weight": "bold"}
                ]}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "⚠️ 分析結果僅供參考，請衡量風險。", "size": "xxs", "color": "#AAAAAA", "align": "center"}
            ]
        }
    }
    return flex_json

# === 會員與資料庫管理 ===

def get_member_info(line_id):
    res = supabase.table("members").select("*").eq("line_user_id", line_id).maybe_single().execute()
    return res.data if res.data else None

def log_and_check_usage(line_id, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_id).eq("used_at", today).maybe_single().execute()
    used = res.data["used_count"] if res.data else 0
    if used >= limit: return False, used
    
    if used == 0:
        supabase.table("usage_logs").insert({"line_user_id": line_id, "used_at": today, "used_count": 1}).execute()
    else:
        supabase.table("usage_logs").update({"used_count": used + 1}).eq("line_user_id", line_id).eq("used_at", today).execute()
    return True, used + 1

# === LINE Webhook 進入點 ===

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
        
        # 取得會員狀態
        member = get_member_info(user_id)
        is_approved = member.get("status") == "approved" if member else False
        lvl = member.get("member_level", "normal") if member else "guest"
        limit = 50 if lvl == "vip" else 15

        input_text = ""
        # --- 處理文字訊息 ---
        if event.message.type == "text":
            input_text = event.message.text.strip()
            
            # 專屬自動開通功能
            if input_text == "dong8989":
                supabase.table("members").upsert({
                    "line_user_id": user_id, 
                    "status": "approved", 
                    "member_level": "normal"
                }).execute()
                return line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="✅ 恭喜！專屬代碼驗證成功，您的帳號已自動開通正式會員權限。")]
                ))

            if input_text == "我要開通":
                if is_approved:
                    reply = f"您已經是正式會員了唷！\n今日額度：{limit} 次"
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                    reply = f"已為您送出開通申請。\n管理員審核中，或請聯繫 ID: adong8989\nUserID: {user_id}"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

        # --- 處理圖片訊息 ---
        elif event.message.type == "image":
            input_text, err = ocr_extract(event.message.id, line_api)
            if err:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=err)]))

        # --- 分析邏輯觸發 ---
        if "RTP" in input_text or "未開" in input_text:
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 您的帳號尚未開通，請先點選「我要開通」或輸入專屬密碼。")]))
            
            allowed, current_count = log_and_check_usage(user_id, limit)
            if not allowed:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"今日額度已用完 ({limit}/{limit})，請升級 VIP 或明天再試。")]))
            
            try:
                # 提取數據並產生卡片
                n = int(clean_num(re.search(r"未開.*?(\d+)", input_text).group(1)))
                r = float(clean_num(re.search(r"RTP.*?(\d+\.?\d*)", input_text).group(1)))
                b = float(clean_num(re.search(r"下注.*?(\d+\.?\d*)", input_text).group(1)))
                
                flex_content = get_flex_output(n, r, b)
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        FlexMessage(alt_text="賽特選房分析報告", contents=FlexContainer.from_dict(flex_content)),
                        TextMessage(text=f"📊 今日使用統計：{current_count} / {limit}")
                    ]
                ))
            except Exception:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 辨識結果格式異常，請確保圖片包含完整的房間 RTP 資訊。")]))
        else:
            # 預設選單
            qr = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
                QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
            ])
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請傳送「賽特選房資訊截圖」開始自動分析，或是輸入專屬開通代碼。", quick_reply=qr)]
            ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
