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
    TextMessage, PushMessageRequest, ReplyMessageRequest, FlexMessage, FlexContainer
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
        logger.info("✅ Google Vision Client 初始化成功")
    except Exception as e: logger.error(f"Vision Init Error: {e}")

# === 工具函式 ===
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

def parse_seth_ocr(txt: str):
    room, n, b, r = "未知", 0, 0.0, 0.0
    # 房號辨識
    room_m = re.search(r"(\d{3,4})\s*[機机][台臺]", txt)
    if not room_m: room_m = re.search(r"[機机][台臺]\s*(\d{3,4})", txt)
    if room_m: room = room_m.group(1)
    else:
        all_nums = re.findall(r"\b\d{3,4}\b", txt)
        if all_nums: room = all_nums[-1]

    # 未開轉數
    n_match = re.search(r"未\s*開\s*(\d+)", txt)
    if n_match: n = int(n_match.group(1))

    # RTP 與 下注
    sections = re.split(r"今日|近30天", txt)
    target = sections[1] if len(sections) > 1 else txt
    rtps = re.findall(r"(\d{1,3}\.\d{2})\s*%", target)
    if rtps: r = float(rtps[0])
    
    nums = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", target)
    for v in nums:
        cv = float(v.replace(',', ''))
        if cv != r and cv != float(room if room.isdigit() else 0) and cv != float(n):
            if cv > 10: 
                b = cv
                break
    return room, n, b, r

def get_flex_card(room, n, r, b, trend):
    color = "#4CAF50"
    label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120: color = "#F44336"; label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110: color = "#FFC107"; label = "⚠️ 中風險 / 謹慎進場"

    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    
    return {
      "type": "bubble", "size": "giga",
      "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台分析: {room}", "weight": "bold", "color": "#FFFFFF", "size": "lg", "align": "center"}], "backgroundColor": color, "paddingAll": "15px"},
      "body": {"type": "box", "layout": "vertical", "contents": [
          {"type": "text", "text": label, "weight": "bold", "size": "xl", "color": color},
          {"type": "separator", "margin": "lg"},
          {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📍 未開轉數", "flex": 2}, {"type": "text", "text": f"{n} 轉", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📈 今日 RTP", "flex": 2}, {"type": "text", "text": f"{r}%", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "💰 今日下注", "flex": 2}, {"type": "text", "text": f"{int(b):,} 元", "weight": "bold", "align": "end", "flex": 3}]}
          ]},
          {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#F5F5F5", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "📊 趨勢分析", "weight": "bold", "size": "sm"},
              {"type": "text", "text": trend, "wrap": True, "margin": "xs", "weight": "bold", "color": "#333333"}
          ]},
          {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#E8F5E9", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "🔮 推薦訊號", "weight": "bold", "size": "sm", "color": "#388E3C"},
              {"type": "text", "text": combo, "margin": "xs", "weight": "bold", "color": "#2E7D32"}
          ]}
      ]}
    }

# === 背景異步處理程序 ===
def async_process_image(user_id, message_id):
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            
            room, n, b, r = parse_seth_ocr(txt)
            if r <= 0:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="❓ 辨識失敗，請確保圖片包含『今日RTP』。")]))
                return

            today = get_tz_now().strftime('%Y-%m-%d')
            fp = f"{room}_{n}_{b}_{r}"
            try:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
            except:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="🚫 偵測到重複截圖，請稍後再試。")]))
                return

            # 計算今日已使用次數
            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
            usage_count = count_res.count if count_res.count else 0

            trend = "📊 房間初次分析。"
            prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
            if prev.data:
                diff = r - float(prev.data[0]['rtp_value'])
                trend = f"📈 較上次：{'上升' if diff >= 0 else '下降'} {abs(diff):.2f}%"

            flex_content = get_flex_card(room, n, r, b, trend)
            line_api.push_message(PushMessageRequest(to=user_id, messages=[
                FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(flex_content)),
                TextMessage(text=f"✅ 分析完成！今日已使用 {usage_count} 次。", quick_reply=get_main_menu())
            ]))
            
        except Exception as e:
            logger.error(f"Async OCR Error: {e}")

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

        # 1. 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res.data and m_res.data.get("status") == "approved":
                is_approved = True
        except: pass

        # 2. 文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                # 通知管理員
                if ADMIN_LINE_ID:
                    line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 收到開通申請！\nUser ID: {user_id}")]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，請等待管理員審核。")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 您今日已分析 {cnt} 張圖片。\n本機器人目前不限次數，請安心使用！", quick_reply=get_main_menu())]))

            if msg == "使用說明":
                guide = (
                    "💡 【賽特分析助手】使用教學：\n\n"
                    "1. 請進入遊戲並點開「機台數據」。\n"
                    "2. 截圖該畫面（須包含未開轉數與今日RTP）。\n"
                    "3. 直接將圖片傳送至本聊天室。\n"
                    "4. 系統將自動分析數據並提供操作建議。"
                )
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=guide, quick_reply=get_main_menu())]))

        # 3. 圖片訊息
        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 您尚未獲得授權。\n請點選選單中的「我要開通」申請權限。", quick_reply=get_main_menu())]))
            
            # 立即回應「分析中」，避免用戶覺得沒反應
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔍 正在分析圖片，請稍候...")] ))
            
            # 啟動異步執行 OCR
            threading.Thread(target=async_process_image, args=(user_id, event.message.id)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
