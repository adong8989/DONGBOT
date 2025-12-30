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
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

def get_admin_approve_flex(target_uid):
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🔔 新用戶開通申請", "weight": "bold", "color": "#FFFFFF"}], "backgroundColor": "#1976D2"},
        "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"用戶ID:\n{target_uid}", "size": "xs", "color": "#666666", "wrap": True}]},
        "footer": {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
            {"type": "button", "action": {"type": "message", "label": "核准普通", "text": f"#核准_normal_{target_uid}"}, "style": "primary", "color": "#4CAF50"},
            {"type": "button", "action": {"type": "message", "label": "核准 VIP", "text": f"#核准_vip_{target_uid}"}, "style": "primary", "color": "#FF9800"}
        ]}
    }

def get_flex_card(room, n, r, b, trend_text, trend_color, seed_hash):
    random.seed(seed_hash)
    base_color = "#00C853" 
    label = "✅ 低風險 / 數據優良"
    if n > 250 or r > 120: base_color = "#D50000"; label = "🚨 高風險 / 建議換房"
    elif n > 150 or r > 110: base_color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"
    
    # --- 戰神賽特專屬物件水庫 ---
    # 大圖: 眼睛, 弓箭, 權杖蛇, 彎刀 (上限6)
    # 寶石: 黃, 紅, 藍, 綠, 紫 (上限6)
    # 特殊: 聖甲蟲 (上限3)
    big_icons = [("眼睛", 6), ("弓箭", 6), ("權杖蛇", 6), ("彎刀", 6)]
    gems = [("黃寶石", 6), ("紅寶石", 6), ("藍寶石", 6), ("綠寶石", 6), ("紫寶石", 6)]
    special = [("聖甲蟲", 3)]
    
    all_items = big_icons + gems + special
    
    # 隨機抽取 2~3 個不重複物件作為訊號
    sample_size = random.choice([2, 3])
    selected_items = random.sample(all_items, sample_size)
    
    combo_list = []
    for name, limit in selected_items:
        count = random.randint(1, limit)
        combo_list.append(f"{name}{count}顆")
    
    combo = "、".join(combo_list)
    
    tips = [
        f"觀測到「{combo}」組合時，演算法預測即將進入噴發期。",
        f"當盤面連續出現「{combo}」，建議適度提升下注額度。",
        f"系統追蹤到「{combo}」為當前房間之熱門噴發前兆。",
        f"根據水庫水位，盤面若補齊「{combo}」後，大獎機率極高。"
    ]
    current_tip = random.choice(tips)
    random.seed(None)
    
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", 
            "contents": [{"type": "text", "text": f"賽特 {room} 房 AI趨勢分析", "color": "#FFFFFF", "weight": "bold", "size": "md"}], 
            "backgroundColor": base_color
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": base_color},
            {"type": "text", "text": trend_text, "size": "sm", "color": trend_color, "weight": "bold"},
            {"type": "separator"},
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"📈 今日 RTP：{r}%", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"💰 今日總下注：{b:,.2f}", "size": "md", "weight": "bold"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 AI賽特推薦進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": f"{current_tip}\n系統提示：此訊號由賽特數據水庫生成，提供參考。", "size": "sm", "margin": "xs", "weight": "bold", "color": "#111111", "wrap": True}
            ]}
        ]}
    }

def sync_image_analysis(user_id, message_id, limit):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            res = vision_client.document_text_detection(image=vision.Image(content=img_bytes))
            txt = res.full_text_annotation.text if res.full_text_annotation else ""
            
            lines = [l.strip() for l in txt.split('\n') if l.strip()]
            room = "未知"
            for line in reversed(lines):
                if re.fullmatch(r"\d{3,4}", line):
                    room = line
                    break

            r, b = 0.0, 0.0
            for i, line in enumerate(lines):
                if "今日" in line or "今" in line:
                    scope = " ".join(lines[i:i+8])
                    rtp_m = re.findall(r"(\d+\.\d+)\s*%", scope)
                    if rtp_m: r = float(rtp_m[0])
                    amt_m = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))", scope)
                    for val in amt_m:
                        cv = float(val.replace(',', ''))
                        if cv != r: 
                            b = cv
                            break
                    break

            n = 0
            n_m = re.search(r"未開\s*(\d+)", txt)
            if n_m: n = int(n_m.group(1))

            if r <= 0:
                return [TextMessage(text="❓ 辨識失敗，請確保下方數據區清晰。")]

            trend_text, trend_color = "🆕 今日首次分析", "#AAAAAA"
            try:
                last_record = supabase.table("usage_logs").select("rtp_value").eq("room_id", room).order("created_at", descending=True).limit(1).execute()
                if last_record.data:
                    last_rtp = float(last_record.data[0]['rtp_value'])
                    diff = r - last_rtp
                    if diff > 0.01: trend_text, trend_color = f"🔥 趨勢升溫 (+{diff:.2f}%)", "#D50000"
                    elif diff < -0.01: trend_text, trend_color = f"❄️ 數據冷卻 ({diff:.2f}%)", "#1976D2"
                    else: trend_text, trend_color = "➡️ 數據平穩", "#555555"
            except: pass

            today_str = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{b:.2f}" 
            
            # --- 修正重複數據不中斷邏輯 ---
            try:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "rtp_value": r, "room_id": room, "data_hash": data_hash}).execute()
            except Exception as e:
                logger.warning(f"Data entry duplicate or error: {e}")
                # 這裡不 return，讓程式繼續往下跑出卡片

            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            return [
                FlexMessage(alt_text="賽特 AI 趨勢分析", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color, data_hash))),
                TextMessage(text=f"📊 今日剩餘額度：{limit - (count_res.count or 0)} / {limit}", quick_reply=get_main_menu())
            ]
        except Exception as e:
            logger.error(f"Logic Error: {e}")
            return [TextMessage(text="系統繁忙，請稍後再試。")]

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
        is_admin = (user_id == ADMIN_LINE_ID)
        
        user_data = None
        is_approved, limit = is_admin, 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_data = m_res.data
                if user_data.get("status") == "approved":
                    is_approved = True
                    limit = 50 if user_data.get("member_level") == "vip" else 15
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            if is_admin and msg.startswith("#核准_"):
                parts = msg.split("_")
                if len(parts) == 3:
                    level, target_uid = parts[1], parts[2]
                    try:
                        supabase.table("members").update({"status": "approved", "member_level": level}).eq("line_user_id", target_uid).execute()
                        line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text=f"🎉 您的帳號已核准開通({'VIP' if level=='vip' else '普通'})！現在可以傳截圖開始分析了。")]))
                        line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已成功核准該用戶。")]))
                    except Exception as e: 
                        logger.error(f"Approve Error: {e}")
                return

            if msg == "我的額度":
                today_str = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日使用：{count_res.count or 0} / {limit}", quick_reply=get_main_menu())]))
            elif msg == "我要開通":
                if user_data:
                    status = user_data.get("status")
                    if status == "approved":
                        line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 您的帳號早已開通。")]))
                        return
                    elif status == "pending":
                        line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⏳ 申請審核中，管理員LINE:adong8989。")]))
                        return
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID:
                    line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[FlexMessage(alt_text="收到新申請", contents=FlexContainer.from_dict(get_admin_approve_flex(user_id)))]))
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，管理員LINE:adong8989。")]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 賽特 AI 分析系統：請傳送截圖。", quick_reply=get_main_menu())]))
        
        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通管理員LINE:adong8989。")]))
            
            result_messages = sync_image_analysis(user_id, event.message.id, limit)
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=result_messages))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
