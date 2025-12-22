import os
import tempfile
import logging
import re
import random
import threading
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
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

def get_flex_card(room, n, r, b):
    color = "#00C853" # 綠色
    label = "✅ 低風險 / 數據優異"
    if n > 250 or r > 120: color = "#D50000"; label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110: color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"
    
    s_pool = [("聖甲蟲", 3), ("紅寶石", 7), ("藍寶石", 7), ("眼睛", 5)]
    combo = "、".join([f"{s[0]}{random.randint(1,s[1])}顆" for s in random.sample(s_pool, 2)])
    
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"機台 {room} 智能分析報告", "color": "#FFFFFF", "weight": "bold"}], "backgroundColor": color},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": color},
            {"type": "separator"},
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"📈 今日 RTP：{r}%", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"💰 今日總下注：{b:,.2f}", "size": "md", "weight": "bold"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 智能推薦進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": f"出現「{combo}」後考慮進場", "size": "sm", "margin": "xs", "weight": "bold", "color": "#111111"}
            ]}
        ]}
    }

# --- 核心邏輯：逆向掃描 + 區域對位分析 ---
def async_image_analysis(user_id, message_id, limit):
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            lines = [l.strip() for l in txt.split('\n') if l.strip()]

            # 1. 房號：從最後一行往回找第一個 3-4 位數
            room = "未知"
            for line in reversed(lines):
                if re.fullmatch(r"\d{3,4}", line):
                    room = line
                    break

            # 2. 定位底部詳情數據塊
            target_block = ""
            for i, line in enumerate(lines):
                if any(k in line for k in ["得分率", "今日", "總下注"]):
                    # 鎖定該行及其後 4 行
                    target_block = " ".join(lines[i:i+5])
                    break

            # 3. 提取數據 (RTP 與 下注額)
            r = 0.0
            rtp_m = re.search(r"(\d+\.\d+)\s*%", target_block)
            if rtp_m:
                r = float(rtp_m.group(1))
            else:
                # 備援：全圖最後一個百分比
                all_rtp = re.findall(r"(\d+\.\d+)\s*%", txt)
                if all_rtp: r = float(all_rtp[-1])

            b = 0.0
            # 尋找帶小數點的金額數字
            amounts = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))", target_block)
            for amt in amounts:
                val = float(amt.replace(',', ''))
                if val != r: # 排除 RTP 數值
                    b = val
                    break

            # 4. 提取未開轉數 (維持全圖正向搜尋)
            n = 0
            n_m = re.search(r"未開\s*(\d+)", txt)
            if n_m: n = int(n_m.group(1))

            # 數據完整性檢查
            if r <= 0:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="❓ 辨識出錯，請確保截圖包含完整的底部詳情區域。")]))
                return

            # --- 儲存與發送結果 ---
            today = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{n}_{b}"
            
            try:
                supabase.table("usage_logs").insert({
                    "line_user_id": user_id, 
                    "used_at": today, 
                    "data_hash": data_hash, 
                    "rtp_value": r
                }).execute()
            except:
                line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text="🚫 偵測到重複截圖，請勿重複分析同一機台數據。")]))
                return

            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
            new_cnt = count_res.count if count_res.count is not None else 1

            line_api.push_message(PushMessageRequest(to=user_id, messages=[
                FlexMessage(alt_text="機台分析報告", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b))),
                TextMessage(text=f"📊 今日分析次數：{new_cnt} / {limit}", quick_reply=get_main_menu())
            ]))

        except Exception as e:
            logger.error(f"Async Image Error: {e}")

# --- LINE Bot 基本設定與 Handler 保持不變 ---
@app.route("/", methods=["GET"])
def index(): return "Bot is Active"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        # 權限檢查
        is_approved = (user_id == ADMIN_LINE_ID)
        limit = 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                if m_res.data.get("status") == "approved": is_approved = True
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            # (處理「我要開通」、「核准」、「我的額度」等文字指令，維持之前邏輯)
            if msg == "我要開通":
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 申請開通通知：\n{user_id}")]))
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，請等待管理員核准。")]))
            elif msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日剩餘額度：{limit - (res.count or 0)} / {limit}", quick_reply=get_main_menu())]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="💡 請直接傳送「點開詳情後」的機台截圖進行分析。", quick_reply=get_main_menu())]))

        elif event.message.type == "image":
            if not is_approved:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 您的帳號尚未核准，請點選「我要開通」。")]))
                return
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔍 正在精準分析數據，請稍候...")] ))
            threading.Thread(target=async_image_analysis, args=(user_id, event.message.id, limit)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
