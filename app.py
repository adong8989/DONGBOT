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

def get_flex_card(n, r, b):
    color = "#00C853"
    label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120: color = "#D50000"; label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110: color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"
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

@app.route("/", methods=["GET"])
def index(): return "Bot is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    except Exception as e: logger.error(f"❌ Callback Error: {e}")
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 1. 自動清理過期權限 (非 VIP 且 超過 3 天)
        try:
            three_days_ago = (get_tz_now() - timedelta(days=3)).isoformat()
            supabase.table("members").update({"status": "expired"}).eq("status", "approved").neq("member_level", "vip").lt("approved_at", three_days_ago).execute()
        except Exception as e: logger.error(f"Cleanup Error: {e}")

        # 2. 檢核會員權限
        is_approved = (user_id == ADMIN_LINE_ID) # 管理員永遠核准
        user_status, limit = "none", 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_status = m_res.data.get("status", "none")
                if user_status == "approved": is_approved = True
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
                
                if user_status == "expired" and user_id != ADMIN_LINE_ID:
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⏰ 您的試用期已結束，請聯繫管理員。")]))
        except: pass

        # 3. 文字訊息處理
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            # --- 我要開通 (主動推播 ID 給管理員) ---
            if msg == "我要開通":
                if is_approved and user_id != ADMIN_LINE_ID: 
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="您已是正式會員。", quick_reply=get_main_menu())]))
                
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                
                # 推播給管理員
                try:
                    line_api.push_message(PushMessageRequest(
                        to=ADMIN_LINE_ID, 
                        messages=[TextMessage(text=f"🔔 收到新開通申請！\n用戶 ID: {user_id}\n請輸入：核准 {user_id}")]
                    ))
                except: pass
                
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 申請已送出！管理員已收到通知。")]))

            # --- 管理員專屬指令 ---
            if user_id == ADMIN_LINE_ID:
                if msg.startswith("核准 "):
                    target_uid = msg.split(" ")[1]
                    supabase.table("members").update({
                        "status": "approved",
                        "approved_at": get_tz_now().isoformat()
                    }).eq("line_user_id", target_uid).execute()
                    line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text="🎉 您的帳號已核准開通！", quick_reply=get_main_menu())]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已核准：{target_uid}")]))

            # --- 通用功能 ---
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count is not None else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{cnt} / {limit}", quick_reply=get_main_menu())]))

            if msg == "使用說明":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="📘 使用說明：\n1. 直接傳截圖。\n2. 手動：房號 轉數 下注 RTP", quick_reply=get_main_menu())]))

            # 手動數據分析
            if is_approved and not any(k in msg for k in ["核准", "備註"]):
                nums = re.findall(r'(?<![a-zA-Z])\d+(?:\.\d+)?(?![a-zA-Z])', msg)
                if len(nums) == 4:
                    return process_analysis(line_api, event, user_id, nums[0], int(float(nums[1])), float(nums[2]), float(nums[3]), limit)

        # 4. 圖片分析
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通。")]))
            
            blob_api = MessagingApiBlob(api_client)
            img_bytes = blob_api.get_message_content(event.message.id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            
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

# 5. 核心邏輯：修正計次與防重複
def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    fp = f"{room}_{n}_{b}"
    
    # 嘗試插入 (利用 SQL UNIQUE 約束)
    try:
        supabase.table("usage_logs").insert({
            "line_user_id": user_id, "used_at": today, 
            "data_hash": fp, "rtp_value": r
        }).execute()
    except:
        return # 重複數據直接忽略

    # 獲取真實筆數
    count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = count_res.count if count_res.count is not None else 1

    if new_cnt > limit and user_id != os.getenv("ADMIN_LINE_ID"):
        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ 額度已滿 ({limit}次)。")]))

    # 趨勢分析
    trend = "📊 今日初次分析。"
    prev = supabase.table("usage_logs").select("rtp_value").like("data_hash", f"{room}%").eq("used_at", today).neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
    if prev.data:
        diff = r - float(prev.data[0].get('rtp_value') or 0)
        trend = f"📈 趨勢：上升 {diff:.1f}% 🔥" if diff > 3 else f"📉 趨勢：下降 {abs(diff):.1f}% 🧊" if diff < -3 else "📊 趨勢：表現平穩。"

    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(get_flex_card(n, r, b))),
        TextMessage(text=f"{trend}\n📊 今日分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
