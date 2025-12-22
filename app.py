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

        # 1. 權限檢核 (管理員強制為 True)
        is_approved, limit = False, 15
        is_admin = (user_id == ADMIN_LINE_ID)
        try:
            m = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m and m.data:
                status = m.data.get("status")
                is_approved = (status == "approved")
                limit = 100 if m.data.get("member_level") == "vip" else 15
        except: pass
        if is_admin: is_approved = True

        # 2. 處理文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            # --- 我要開通 (所有人共用) ---
            if msg == "我要開通":
                if is_admin: 
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="👑 管理員權限已開啟，無需申請。")]))
                # 寫入申請
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                # 通知管理員 (關鍵！)
                line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 收到開通申請！\nID: {user_id}\n請輸入：核准 {user_id}")]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，請截圖您的 ID 並聯繫管理員。")]))

            # --- 管理員專屬指令 ---
            if is_admin:
                if msg.startswith("核准 "):
                    tid = msg.split(" ")[1]
                    supabase.table("members").update({"status": "approved", "approved_at": get_tz_now().isoformat()}).eq("line_user_id", tid).execute()
                    line_api.push_message(PushMessageRequest(to=tid, messages=[TextMessage(text="🎉 您的權限已開通！", quick_reply=get_main_menu())]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已成功核准：{tid}")]))
                
                if msg == "今日戰報":
                    res = supabase.table("daily_hot_rooms").select("*").limit(5).execute()
                    report = "📊 今日熱門排行：\n" + "\n".join([f"{i+1}. 房 {r['room_id']}：{r['check_count']}次 (均RTP {float(r.get('avg_rtp') or 0.0):.1f}%)" for i, r in enumerate(res.data)]) if res.data else "目前尚無數據。"
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=report)]))

            # --- 額度查詢 ---
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = count_res.count if count_res.count is not None else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{cnt} / {limit}", quick_reply=get_main_menu())]))

            # --- 手動分析 ---
            if is_approved:
                nums = re.findall(r'(?<![a-zA-Z])\d+(?:\.\d+)?(?![a-zA-Z])', msg)
                if len(nums) == 4:
                    return process_analysis(line_api, event, user_id, nums[0], int(float(nums[1])), float(nums[2]), float(nums[3]), limit)

        # 3. 圖片分析
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先開通權限。")]))
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

# 4. 核心處理 (修正重複寫入)
def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    fp = f"{room}_{n}_{b}" 
    
    # 嘗試插入 (依賴 SQL UNIQUE 約束)
    try:
        supabase.table("usage_logs").insert({
            "line_user_id": user_id, "used_at": today, 
            "data_hash": fp, "rtp_value": r
        }).execute()
    except:
        return # 重複則直接跳出，不回覆

    # 獲取最新筆數
    count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = count_res.count if count_res.count is not None else 1

    if new_cnt > limit and user_id != os.getenv("ADMIN_LINE_ID"):
        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"❌ 額度已滿 ({limit}次)。")]))

    # 趨勢分析
    trend = "📊 今日初次分析。"
    prev = supabase.table("usage_logs").select("rtp_value").like("data_hash", f"{room}%").eq("used_at", today).neq("data_hash", fp).order("created_at", desc=True).limit(1).execute()
    if prev.data:
        diff = r - float(prev.data[0].get('rtp_value') or 0)
        trend = f"📈 趨勢：上升 {diff:.1f}% 🔥" if diff > 3 else f"📉 趨勢：下降 {abs(diff):.1f}% 🧊" if diff < -3 else "📊 趨勢：平穩。"

    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="分析報告", contents=FlexContainer.from_dict(get_flex_card(n, r, b))),
        TextMessage(text=f"{trend}\n📊 今日分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
