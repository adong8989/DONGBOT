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
from linebot.v3.messaging.models import (
    QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.exceptions import InvalidSignatureError

try:
    from google.cloud import vision
except ImportError:
    vision = None

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
if GCP_SA_KEY_JSON and vision:
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_file.name
        vision_client = vision.ImageAnnotatorClient()
    except Exception as e:
        logger.error(f"Vision Init Error: {e}")

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

def get_flex_card(n, r, b):
    color = "#00C853"
    label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120:
        color = "#D50000"
        label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110:
        color = "#FFAB00"
        label = "⚠️ 中風險 / 謹慎進場"
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
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

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"❌ Callback Error: {e}")
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        is_approved = False
        user_status = "none"
        limit = 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_status = m_res.data.get("status", "none")
                is_approved = (user_status == "approved")
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            if user_id == ADMIN_LINE_ID and msg.startswith("核准 "):
                target_uid = msg.split(" ")[1]
                supabase.table("members").upsert({"line_user_id": target_uid, "status": "approved"}, on_conflict="line_user_id").execute()
                line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text="🎉 您的帳號已核准開通！", quick_reply=get_main_menu())]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已成功核准：{target_uid}")]))

            if msg == "我要開通":
                if user_status == "blocked": return 
                if is_approved:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="您已是正式會員。", quick_reply=get_main_menu())]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID:
                    try: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 申請：{user_id}\n核准 {user_id}")]))
                    except: pass
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"⚠️ 尚未開通。請聯絡管理員提供 ID：\n\n{user_id}", quick_reply=get_main_menu())]))

            if msg == "(房間資訊表格)" or msg == "房間資訊表格":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="📝 **表格輸入示範**\n格式：房號 轉數 下注 RTP\n示範：`8012 150 7100 85.5`")]))

            if msg == "使用說明":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="📘 使用說明：\n1. 直接傳截圖自動分析。\n2. 輸入「房間資訊表格」看格式。")]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count = 0
                u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).execute()
                if u_res and u_res.data: count = u_res.data[0]["used_count"]
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{count} / {limit}", quick_reply=get_main_menu())]))

            nums = re.findall(r"(\d+(?:\.\d+)?)", msg)
            if len(nums) >= 4 and is_approved:
                room, n, b, r = nums[0], int(nums[1]), float(nums[2]), float(nums[3])
                fingerprint = f"{room}_{n}_{b}"
                today_str = get_tz_now().strftime('%Y-%m-%d')
                if supabase.table("usage_logs").select("*").eq("data_hash", fingerprint).eq("used_at", today_str).execute().data:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 數據重複。")]))
                new_count = 1
                u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                if u_res and u_res.data: new_count = u_res.data[0]["used_count"] + 1
                if new_count > limit: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="額度已滿。")]))
                supabase.table("usage_logs").upsert({"line_user_id": user_id, "used_at": today_str, "used_count": new_count, "data_hash": fingerprint}, on_conflict="line_user_id,used_at").execute()
                flex = get_flex_card(n, r, b)
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="報告", contents=FlexContainer.from_dict(flex)), TextMessage(text=f"📊 今日：{new_count}/{limit}")]))

        elif event.message.type == "image" and is_approved:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(event.message.id)
            image = vision.Image(content=image_bytes)
            response = vision_client.document_text_detection(image=image)
            flat_text = "".join((response.full_text_annotation.text if response.full_text_annotation else "").split())
            n = int(re.search(r"未開(\d+)", flat_text).group(1)) if re.search(r"未開(\d+)", flat_text) else 0
            room = re.search(r"(\d{4})", flat_text).group(1) if re.search(r"(\d{4})", flat_text) else "0000"
            r, b = 0.0, 0.0
            today_idx = flat_text.find("今日")
            if today_idx != -1:
                after_today = flat_text[today_idx:]
                amt_m = re.search(r"(\d{1,3}(?:,\d{3})*\.\d{2})", after_today)
                if amt_m: b = float(clean_num(amt_m.group(1)))
                pct_m = re.search(r"(\d+\.\d+)%", after_today)
                if pct_m: r = float(pct_m.group(1))
            if r == 0.0: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 無法定位數據。")]))
            fingerprint = f"{room}_{n}_{b}"
            today_str = get_tz_now().strftime('%Y-%m-%d')
            if supabase.table("usage_logs").select("*").eq("data_hash", fingerprint).eq("used_at", today_str).execute().data:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 數據重複。")]))
            new_count = 1
            u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            if u_res and u_res.data: new_count = u_res.data[0]["used_count"] + 1
            if new_count > limit: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="額度已滿。")]))
            supabase.table("usage_logs").upsert({"line_user_id": user_id, "used_at": today_str, "used_count": new_count, "data_hash": fingerprint}, on_conflict="line_user_id,used_at").execute()
            flex = get_flex_card(n, r, b)
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="報告", contents=FlexContainer.from_dict(flex)), TextMessage(text=f"📊 今日：{new_count}/{limit}")]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
