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
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

def get_trend_info(diff_value):
    """根據趨勢差異返回圖標與顏色"""
    if diff_value > 3:
        return "https://cdn-icons-png.flaticon.com/512/2722/2722225.png", "#00C853" # 上升
    elif diff_value < -3:
        return "https://cdn-icons-png.flaticon.com/512/2722/2722224.png", "#FF1744" # 下降
    else:
        return "https://cdn-icons-png.flaticon.com/512/483/483650.png", "#FFD600" # 持平

def get_flex_card(n, r, b, trend_text, trend_diff):
    """設計精美的分析報告卡片"""
    # 風險判斷
    main_color = "#4CAF50"
    main_label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120:
        main_color = "#F44336"
        main_label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110:
        main_color = "#FFC107"
        main_label = "⚠️ 中風險 / 謹慎進場"

    trend_icon, trend_color = get_trend_info(trend_diff)
    
    # 隨機訊號
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    
    return {
      "type": "bubble",
      "size": "giga",
      "header": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {"type": "text", "text": "賽特選房智能分析", "weight": "bold", "color": "#FFFFFF", "size": "lg", "align": "center"}
        ],
        "backgroundColor": main_color,
        "paddingAll": "15px"
      },
      "body": {
        "type": "box", "layout": "vertical", "contents": [
          {
            "type": "box", "layout": "horizontal", "contents": [
              {"type": "text", "text": main_label, "weight": "bold", "size": "xl", "color": main_color, "flex": 5}
            ]
          },
          {"type": "separator", "margin": "lg"},
          {
            "type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📍 未開轉數", "flex": 2}, {"type": "text", "text": f"{n} 轉", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "📈 今日 RTP", "flex": 2}, {"type": "text", "text": f"{r}%", "weight": "bold", "align": "end", "flex": 3}]},
              {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "💰 今日下注", "flex": 2}, {"type": "text", "text": f"{int(b):,} 元", "weight": "bold", "align": "end", "flex": 3}]}
            ]
          },
          {"type": "separator", "margin": "lg"},
          {
            "type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#F5F5F5", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "📊 趨勢分析", "weight": "bold", "size": "sm", "color": "#555555"},
              {"type": "text", "text": trend_text, "wrap": True, "size": "md", "weight": "bold", "margin": "xs", "color": trend_color}
            ]
          },
          {
            "type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#E8F5E9", "cornerRadius": "md", "paddingAll": "md", "contents": [
              {"type": "text", "text": "🔮 推薦進場訊號", "weight": "bold", "size": "sm", "color": "#388E3C"},
              {"type": "text", "text": combo, "size": "md", "margin": "xs", "weight": "bold", "color": "#2E7D32"}
            ]
          }
        ]
      }
    }

@app.route("/", methods=["GET"])
def index(): return "Bot is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 1. 權限檢核
        is_approved = (user_id == ADMIN_LINE_ID)
        limit = 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res.data:
                if m_res.data["status"] == "approved": is_approved = True
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
                if m_res.data["status"] == "expired" and not is_approved:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⏰ 您的試用期已結束。")]))
        except: pass

        # 2. 文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                try:
                    line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 新申請！\nID: {user_id}\n請輸入：核准 {user_id}")]))
                except: pass
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出！管理員已收到通知。")]))

            if user_id == ADMIN_LINE_ID and msg.startswith("核准 "):
                target_uid = msg.split(" ")[1]
                supabase.table("members").update({"status": "approved", "approved_at": get_tz_now().isoformat()}).eq("line_user_id", target_uid).execute()
                line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text="🎉 帳號已開通！", quick_reply=get_main_menu())]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已核准：{target_uid}")]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{cnt} / {limit}", quick_reply=get_main_menu())]))

            if msg == "使用說明":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="📘 直接傳截圖即可分析數據！", quick_reply=get_main_menu())]))

            # 手動分析
            nums = re.findall(r'\d+(?:\.\d+)?', msg)
            if is_approved and len(nums) == 4:
                return process_analysis(line_api, event, user_id, nums[0], int(float(nums[1])), float(nums[2]), float(nums[3]), limit)

        # 3. 圖片分析
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通。")]))
            
            blob_api = MessagingApiBlob(api_client)
            img_bytes = blob_api.get_message_content(event.message.id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            
            # OCR 邏輯
            n = int(re.search(r"未開\s*(\d+)", txt).group(1)) if re.search(r"未開\s*(\d+)", txt) else 0
            room = re.search(r"(\d{4})", txt).group(1) if re.search(r"(\d{4})", txt) else "0000"
            r, b = 0.0, 0.0
            if "今日" in txt:
                p = txt.split("今日")[-1]
                bm = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))", p)
                pm = re.search(r"(\d+\.\d+)%", p)
                if bm: b = float(bm.group(1).replace(',', ''))
                if pm: r = float(pm.group(1))
            
            if r > 0:
                return process_analysis(line_api, event, user_id, room, n, b, r, limit)

def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    fp = f"{room}_{n}_{b}"
    
    # 防重複與計次
    try:
        supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today, "data_hash": fp, "rtp_value": r}).execute()
    except: return # 重複則跳過

    count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = count_res.count if count_res.count else 1

    if new_cnt > limit and user_id != ADMIN_LINE_ID:
        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ 額度已滿 ({limit}次)。")]))

    # 趨勢判斷
    trend_text = "🆕 今日首筆分析。"
    diff_value = 0
    prev = supabase.table("usage_logs").select("rtp_value").like("data_hash", f"{room}%").eq("used_at", today).neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
    if prev.data:
        diff_value = r - float(prev.data[0]['rtp_value'])
        if diff_value > 5: trend_text = f"🔥 訊號暴增：較上次上升 {diff_value:.1f}%"
        elif diff_value < -5: trend_text = f"❄️ 訊號冷卻：較上次下降 {abs(diff_value):.1f}%"
        else: trend_text = "📊 數據持平：表現穩定。"

    # 回傳精美卡片
    flex_content = get_flex_card(n, r, b, trend_text, diff_value)
    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(flex_content)),
        TextMessage(text=f"📊 今日分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
