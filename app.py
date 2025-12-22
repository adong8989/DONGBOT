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

        # 1. 自動清理過期用戶
        try:
            limit_time = (get_tz_now() - timedelta(days=3)).isoformat()
            supabase.table("members").update({"status": "expired"}).eq("status", "approved").neq("member_level", "vip").lt("approved_at", limit_time).execute()
        except: pass

        # 2. 權限檢核
        is_approved, limit = False, 15
        try:
            m = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m and m.data:
                user_status = m.data.get("status", "none")
                is_approved = (user_status == "approved")
                limit = 50 if m.data.get("member_level") == "vip" else 15
                if user_status == "expired":
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⏰ 您的試用期已結束，請聯絡管理員升級正式會員。")]))
        except: pass

        # 3. 處理文字訊息
        if event.message.type == "text":
            msg = event.message.text.strip()
            
            # --- 管理員指令 ---
            if user_id == ADMIN_LINE_ID:
                if msg.startswith("核准 "):
                    tid = msg.split(" ")[1]
                    supabase.table("members").upsert({"line_user_id": tid, "status": "approved", "approved_at": get_tz_now().isoformat()}, on_conflict="line_user_id").execute()
                    line_api.push_message(PushMessageRequest(to=tid, messages=[TextMessage(text="🎉 您的帳號已核准開通！", quick_reply=get_main_menu())]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已核准：{tid}")]))
                
                if msg == "今日戰報":
                    res = supabase.table("daily_hot_rooms").select("*").limit(5).execute()
                    if not res.data: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="今日尚無任何分析數據。")]))
                    
                    report_lines = []
                    for i, r_data in enumerate(res.data):
                        # 修正：處理 NULL RTP 的防呆邏輯
                        avg_rtp = float(r_data.get('avg_rtp') or 0.0)
                        report_lines.append(f"{i+1}. 房 {r_data['room_id']}：{r_data['check_count']}次 (均RTP {avg_rtp:.1f}%)")
                    
                    report = "📊 今日熱門房號排行：\n" + "\n".join(report_lines)
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=report)]))

                if msg.startswith("備註 "):
                    parts = msg.split(" ", 2)
                    if len(parts) >= 3:
                        target_uid, content = parts[1], parts[2]
                        supabase.table("members").update({"remark": content}).eq("line_user_id", target_uid).execute()
                        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已完成備註：\nID: {target_uid}")]))

            # --- 基礎選單 ---
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，請截圖您的 ID 並聯繫管理員。")]))
            
            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                u = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).execute()
                cnt = u.data[0]['used_count'] if u.data else 0
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析次數：{cnt} / {limit}", quick_reply=get_main_menu())]))

            if msg == "使用說明":
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="📘 使用說明：\n1. 直接傳截圖自動分析。\n2. 手動輸入：房號 轉數 下注 RTP", quick_reply=get_main_menu())]))

            # --- 數據分析判定 ---
            if not any(k in msg for k in ["備註", "核准", "U2400"]):
                nums = re.findall(r'(?<![a-zA-Z])\d+(?:\.\d+)?(?![a-zA-Z])', msg)
                if len(nums) == 4 and is_approved:
                    room, n, b, r = nums[0], int(float(nums[1])), float(nums[2]), float(nums[3])
                    return process_analysis(line_api, event, user_id, room, n, b, r, limit)

        # 4. 圖片分析處理
        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先開通權限再使用。")]))
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
                if bm: b = float(bm.group(1).replace(',', ''))
                pm = re.search(r"(\d+\.\d+)%", p)
                if pm: r = float(pm.group(1))
            
            if r == 0.0 or r > 1000.0: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 截圖辨識失敗，請重傳更清晰的選房畫面。")]))
            return process_analysis(line_api, event, user_id, room, n, b, r, limit)

# 5. 核心分析與寫入函式
def process_analysis(line_api, event, user_id, room, n, b, r, limit):
    today = get_tz_now().strftime('%Y-%m-%d')
    fp = f"{room}_{n}_{b}"
    
    # --- 趨勢檢查 ---
    trend = "📊 此房今日初次分析。"
    # 搜尋該房號今日最近的一筆紀錄
    prev = supabase.table("usage_logs").select("rtp_value").like("data_hash", f"{room}%").eq("used_at", today).order("created_at", desc=True).limit(1).execute()
    if prev.data and prev.data[0].get('rtp_value'):
        diff = r - float(prev.data[0]['rtp_value'])
        if diff > 3: trend = f"📈 趨勢：RTP 上升 {diff:.1f}% (轉旺中🔥)"
        elif diff < -3: trend = f"📉 趨勢：RTP 下降 {abs(diff):.1f}% (稍微冷卻🧊)"
        else: trend = "📊 趨勢：表現持平穩定。"

    # --- 重複數據與額度檢查 ---
    dup = supabase.table("usage_logs").select("*").eq("data_hash", fp).eq("used_at", today).execute()
    if dup.data: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 此數據已分析過，不重複計算額度。")]))
    
    u = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).execute()
    new_cnt = (u.data[0]['used_count'] + 1) if u.data else 1
    if new_cnt > limit: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 您的今日額度已滿。")]))
    
    # --- 執行寫入 ---
    supabase.table("usage_logs").upsert({"line_user_id": user_id, "used_at": today, "used_count": new_cnt, "data_hash": fp, "rtp_value": r}, on_conflict="line_user_id,used_at").execute()
    
    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[
        FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(get_flex_card(n, r, b))),
        TextMessage(text=f"{trend}\n📊 今日分析：{new_cnt} / {limit}", quick_reply=get_main_menu())
    ]))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
