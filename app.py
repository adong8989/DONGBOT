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

def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

def get_flex_card(n, r, b, trend_text, trend_diff, room_id):
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
      "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"賽特選房智能分析 (房號:{room_id})", "weight": "bold", "color": "#FFFFFF", "size": "md", "align": "center"}], "backgroundColor": main_color, "paddingAll": "15px"},
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
              {"type": "text", "text": trend_text, "wrap": True, "margin": "xs", "weight": "bold"}
          ]},
          {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#E8F5E9", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "🔮 推薦訊號", "weight": "bold", "size": "sm"},
              {"type": "text", "text": combo, "margin": "xs", "weight": "bold"}
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

        # 1. 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if user_id == ADMIN_LINE_ID:
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}, on_conflict="line_user_id").execute()
                is_approved = True
            elif m_res.data and m_res.data.get("status") == "approved":
                is_approved = True
        except: pass

        limit = 50 if (is_approved and user_id == ADMIN_LINE_ID) else 15

        # 2. 文字處理
        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                if is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 您已開通權限。")]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 新申請！\nID: {user_id}\n核准 {user_id}")]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出！")]))

            if user_id == ADMIN_LINE_ID and msg.startswith("核准 "):
                target_uid = msg.split(" ")[1]
                supabase.table("members").update({"status": "approved", "approved_at": get_tz_now().isoformat()}).eq("line_user_id", target_uid).execute()
                line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text="🎉 帳號已核准！", quick_reply=get_main_menu())]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已核准：{target_uid}")]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{cnt} / {limit}")]))

        # 3. 圖片分析
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通。")]))

            try:
                blob_api = MessagingApiBlob(api_client)
                img_bytes = blob_api.get_message_content(event.message.id)
                res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
                txt = res.full_text_annotation.text if res.full_text_annotation else ""
                
                # --- 強化房號抓取 (針對左下角或獨立 4 位數) ---
                # 我們優先找有沒有類似 1234 這種獨立數字
                rooms = re.findall(r"\b\d{4}\b", txt)
                # 如果有多個，通常房號在後面（因為 OCR 由上而下讀取）
                room = rooms[-1] if rooms else str(random.randint(1000, 9999))
                
                n = int(re.search(r"未開\s*(\d+)", txt).group(1)) if re.search(r"未開\s*(\d+)", txt) else 0
                r_match = re.search(r"(\d+\.\d+)\s*%", txt)
                r = float(r_match.group(1)) if r_match else 0.0
                b_match = re.search(r"下注\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", txt)
                b = float(b_match.group(1).replace(',', '')) if b_match else 0.0

                if r > 0:
                    return process_analysis(line_api, event, user_id, room, n, b, r, limit)
                else:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❓ 辨識不到數據，請確保截圖完整。")]))
            except Exception as e:
                logger.error(f"OCR Error: {e}")

def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    # 加入隨機因子，防止在同一秒內重複點擊造成的鎖死
    # 房號 + 未開 + RTP 組合成唯一指紋
    fp = f"{room}_{n}_{r}_{b}"
    
    try:
        supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
    except:
        # 如果真的重複了，我們回饋一封訊息
        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 此數據已分析過，請變動數據後再試。")]))

    count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = count_res.count if count_res.count else 1
    
    trend_text = "📊 房間初次分析。"
    diff = 0
    # 尋找該房間上一筆記錄
    prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
    if prev.data:
        diff = r - float(prev.data[0]['rtp_value'])
        trend_text = f"📈 較上次：{'上升' if diff > 0 else '下降'} {abs(diff):.1f}%"

    flex_content = get_flex_card(n, r, b, trend_text, diff, room)
    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(flex_content)),
        TextMessage(text=f"📊 今日分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
