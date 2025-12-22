import os
import tempfile
import logging
import re
import random
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

# === Google Vision 初始化 ===
vision_client = None
if GCP_SA_KEY_JSON:
    try:
        from google.cloud import vision
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(GCP_SA_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        vision_client = vision.ImageAnnotatorClient()
        logger.info("✅ Google Vision Client 初始化成功")
    except Exception as e:
        logger.error(f"❌ Vision 初始化失敗: {e}")

# ---------- 解析邏輯強化 ----------
def parse_seth_ocr(txt: str):
    room = "0000"
    n = 0
    b = 0.0
    r = 0.0

    # 1. 房號
    room_match = re.search(r"(\d{4})\s*機台", txt)
    if room_match: room = room_match.group(1)
    
    # 2. 未開轉數
    n_match = re.search(r"未\s*開\s*(\d+)", txt)
    if n_match: n = int(n_match.group(1))

    # 3. 數據區域定位 (今日 vs 近30天)
    # 針對賽特截圖：今日數據通常在「今日」標籤後
    sections = re.split(r"今日|近30天", txt)
    target_text = sections[1] if len(sections) > 1 else txt

    # 找 RTP (%) - 只要是 0.00% ~ 999.99% 都抓
    rtps = re.findall(r"(\d{1,3}\.\d{2})\s*%", target_text)
    if rtps:
        r = float(rtps[0])

    # 找下注額 - 排除掉 RTP 數字後的剩餘大數字
    nums = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", target_text)
    for val in nums:
        clean_val = float(val.replace(',', ''))
        if clean_val != r and clean_val != float(room if room.isdigit() else 0):
            if clean_val > 10: # 過濾掉零星雜訊
                b = clean_val
                break

    return room, n, b, r

def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

def get_flex_card(room, n, r, b, trend_text):
    main_color = "#4CAF50"
    main_label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120:
        main_color = "#F44336"; main_label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110:
        main_color = "#FFC107"; main_label = "⚠️ 中風險 / 謹慎進場"

    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    
    return {
      "type": "bubble", "size": "giga",
      "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台分析: {room}", "weight": "bold", "color": "#FFFFFF", "size": "lg", "align": "center"}], "backgroundColor": main_color, "paddingAll": "15px"},
      "body": {"type": "box", "layout": "vertical", "contents": [
          {"type": "text", "text": main_label, "weight": "bold", "size": "xl", "color": main_color},
          {"type": "separator", "margin": "lg"},
          {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📍 未開轉數", "flex": 2}, {"type": "text", "text": f"{n} 轉", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📈 今日 RTP", "flex": 2}, {"type": "text", "text": f"{r}%", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "💰 今日下注", "flex": 2}, {"type": "text", "text": f"{int(b):,} 元", "weight": "bold", "align": "end", "flex": 3}]}
          ]},
          {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#F5F5F5", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "📊 趨勢分析", "weight": "bold", "size": "sm"},
              {"type": "text", "text": trend_text, "wrap": True, "margin": "xs", "weight": "bold", "color": "#333333"}
          ]},
          {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#E8F5E9", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "🔮 推薦訊號", "weight": "bold", "size": "sm", "color": "#388E3C"},
              {"type": "text", "text": combo, "margin": "xs", "weight": "bold", "color": "#2E7D32"}
          ]}
      ]}
    }

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        logger.error(f"❌ Callback Error: {e}")
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res.data and m_res.data.get("status") == "approved":
                is_approved = True
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出！")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{cnt}", quick_reply=get_main_menu())]))

        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 尚未開通。")]))

            # --- 立即擷取內容與 OCR ---
            blob_api = MessagingApiBlob(api_client)
            img_bytes = blob_api.get_message_content(event.message.id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            
            room, n, b, r = parse_seth_ocr(txt)

            if r <= 0:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❓ 辨識失敗，請確保圖片清晰。")]))

            # --- 重複檢查與儲存 ---
            today = get_tz_now().strftime('%Y-%m-%d')
            fp = f"{room}_{n}_{b}_{r}"
            try:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
            except:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 此截圖已分析過。")]))

            # --- 趨勢計算 ---
            trend_text = "📊 房間初次分析。"
            prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
            if prev.data:
                diff = r - float(prev.data[0]['rtp_value'])
                trend_text = f"📈 較上次：{'上升' if diff >= 0 else '下降'} {abs(diff):.2f}%"

            # --- 正式回覆 ---
            flex_content = get_flex_card(room, n, r, b, trend_text)
            return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
                FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(flex_content))
            ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
