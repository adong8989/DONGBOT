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
if GCP_SA_KEY_JSON:
    try:
        from google.cloud import vision
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(GCP_SA_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        vision_client = vision.ImageAnnotatorClient()
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

def get_flex_card(n, r, b, trend_text, room_id):
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
      "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"賽特分析 (房號:{room_id})", "weight": "bold", "color": "#FFFFFF", "size": "md", "align": "center"}], "backgroundColor": main_color, "paddingAll": "15px"},
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
    return "OK", 200

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 1. 權限與管理員自動開通
        is_approved = (user_id == ADMIN_LINE_ID)
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res.data and m_res.data.get("status") == "approved":
                is_approved = True
            elif user_id == ADMIN_LINE_ID:
                supabase.table("members").upsert({"line_user_id": user_id, "status": "approved"}).execute()
                is_approved = True
        except: pass

        limit = 50 if user_id == ADMIN_LINE_ID else 15

        # 2. 文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                if is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 權限已開通。")]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}).execute()
                line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 申請：核准 {user_id}")]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請中。")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日：{res.count if res.count else 0} / {limit}")]))

        # 3. 圖片訊息 (辨識核心)
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通。")]))

            try:
                # 取得圖片內容
                blob_api = MessagingApiBlob(api_client)
                img_bytes = blob_api.get_message_content(event.message.id)
                
                # 送往 Google Vision (最耗時的一步)
                res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
                txt = res.full_text_annotation.text if res.full_text_annotation else ""

                # --- 賽特數據提取邏輯 ---
                # 房號
                room_match = re.search(r"(\d{4})\s*機台", txt)
                room = room_match.group(1) if room_match else "未知"

                # 未開轉數
                n_match = re.search(r"未開\s*(\d+)", txt)
                n = int(n_match.group(1)) if n_match else 0

                # RTP (得分率)
                rtps = re.findall(r"(\d+\.\d+)\s*%", txt)
                r = float(rtps[0]) if rtps else 0.0

                # 下注金額 (過濾房號、RTP 與 千萬級數據)
                b = 0.0
                all_nums = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", txt)
                for val in all_nums:
                    v = float(val.replace(',', ''))
                    if v != r and 10 < v < 5000000 and v != float(room if room.isdigit() else 0):
                        b = v
                        break

                if r > 0:
                    return process_analysis(line_api, event, user_id, room, n, b, r, limit)
                else:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❓ 辨識失敗，請確保包含底部資訊區。")]))
            except Exception as e:
                logger.error(f"❌ Image Error: {e}")
                # 發生錯誤時嘗試回覆，避免 LINE 持續 Retry
                try:
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 系統忙碌，請重新傳送圖片。")]))
                except: pass

def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    now_ts = get_tz_now().strftime('%H%M%S')
    fp = f"{room}_{n}_{r}_{b}_{now_ts}"
    
    # 寫入紀錄 (異步想法：這裡可以嘗試不等待結果直接往下跑，縮短回覆時間)
    try:
        supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
    except: pass

    # 獲取今日次數
    cnt_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = cnt_res.count if cnt_res.count else 1
    
    # 簡單化趨勢分析 (減少查詢壓力)
    trend = "📊 房間初次分析。"
    try:
        prev = supabase.table("usage_logs").select("rtp_value").eq("line_user_id", user_id).like("data_hash", f"{room}%").order("created_at", desc=True).limit(2).execute()
        if prev.data and len(prev.data) > 1:
            diff = r - float(prev.data[1]['rtp_value'])
            trend = f"📈 較上次：{'上升' if diff > 0 else '下降'} {abs(diff):.1f}%"
    except: pass

    # 生成並發送卡片
    flex = get_flex_card(n, r, b, trend, room)
    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="分析結果", contents=FlexContainer.from_dict(flex)),
        TextMessage(text=f"📊 今日已分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
