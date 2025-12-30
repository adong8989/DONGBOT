import os
import tempfile
import logging
import re
import random
import json
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

from google.oauth2 import service_account

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

# === Vision Client 初始化 ===
vision_client = None
if GCP_SA_KEY_JSON:
    try:
        from google.cloud import vision
        key_dict = json.loads(GCP_SA_KEY_JSON)
        creds = service_account.Credentials.from_service_account_info(key_dict)
        vision_client = vision.ImageAnnotatorClient(credentials=creds)
        logger.info("✅ Google Vision Client 啟動成功")
    except Exception as e:
        logger.error(f"❌ Vision Client 啟動失敗: {e}")

# === 工具函數 ===
def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔥 熱門戰報", text="熱門戰報")),
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

# === 視覺化卡片邏輯 ===
def get_flex_card(room, n, r, b, trend_text, trend_color, seed_hash):
    random.seed(seed_hash)
    if n > 250 or r > 120:
        base_color = "#D50000"; label = "🚨 高風險 / 建議換房"; risk_percent = "100%"
    elif n > 150 or r > 110:
        base_color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"; risk_percent = "60%"
    else:
        base_color = "#00C853"; label = "✅ 低風險 / 數據優良"; risk_percent = "30%"
    
    all_items = [("眼睛", 6), ("弓箭", 6), ("權杖蛇", 6), ("彎刀", 6), ("紅寶石", 6), ("藍寶石", 6), ("聖甲蟲", 3)]
    selected_items = random.sample(all_items, 2)
    combo = "、".join([f"{name}{random.randint(1, limit)}顆" for name, limit in selected_items])
    current_tip = random.choice([f"觀測到「{combo}」組合時，即將進入噴發期。", f"當盤面連續出現「{combo}」，建議加碼。"])
    random.seed(None)
    
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"賽特 {room} 房 趨勢分析", "color": "#FFFFFF", "weight": "bold"}], "backgroundColor": base_color},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": label, "size": "xl", "weight": "bold", "color": base_color},
            {"type": "box", "layout": "vertical", "margin": "md", "contents": [
                {"type": "text", "text": "風險指數", "size": "xs", "color": "#888888"},
                {"type": "box", "layout": "vertical", "backgroundColor": "#EEEEEE", "height": "8px", "margin": "sm", "cornerRadius": "4px", "contents": [
                    {"type": "box", "layout": "vertical", "width": risk_percent, "backgroundColor": base_color, "height": "8px", "cornerRadius": "4px", "contents": []}
                ]}
            ]},
            {"type": "text", "text": trend_text, "size": "sm", "color": trend_color, "weight": "bold"},
            {"type": "separator"},
            {"type": "text", "text": f"📍 未開：{n} | 📈 RTP：{r}%", "weight": "bold"},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 AI 進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": f"{current_tip}", "size": "sm", "wrap": True}
            ]}
        ]}
    }

# === 新增功能：取得熱門房間戰報 ===
def get_trending_report():
    try:
        # 抓取過去 1 小時的數據 (UTC+8 修正)
        one_hour_ago = (get_tz_now() - timedelta(hours=1)).isoformat()
        res = supabase.table("usage_logs").select("room_id, rtp_value, created_at").gt("created_at", one_hour_ago).order("rtp_value", descending=True).execute()
        
        if not res.data:
            return "目前暫無 1 小時內的熱門數據，請稍後再試。"
        
        # 房間去重，只取最高的一筆
        rooms = {}
        for item in res.data:
            rid = item['room_id']
            if rid not in rooms or item['rtp_value'] > rooms[rid]['rtp']:
                rooms[rid] = {'rtp': item['rtp_value'], 'time': item['created_at']}
        
        report_text = "🔥 戰神賽特｜1H 熱門房間排行：\n"
        sorted_rooms = sorted(rooms.items(), key=lambda x: x[1]['rtp'], reverse=True)[:5] # 取前 5 名
        
        for i, (rid, data) in enumerate(sorted_rooms):
            medals = ["🥇", "🥈", "🥉", "▫️", "▫️"]
            report_text += f"{medals[i]} 房號: {rid} | RTP: {data['rtp']}%\n"
            
        report_text += "\n💡 數據由全體用戶即時貢獻。"
        return report_text
    except Exception as e:
        logger.error(f"Report Error: {e}")
        return "戰報生成失敗，請稍後再試。"

# === 核心分析邏輯 ===
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
                if re.fullmatch(r"\d{3,4}", line): room = line; break

            r, b = 0.0, 0.0
            for i, line in enumerate(lines):
                if "今日" in line or "今" in line:
                    scope = " ".join(lines[i:i+8])
                    rtp_m = re.findall(r"(\d+\.\d+)\s*%", scope)
                    if rtp_m: r = float(rtp_m[0])
                    amt_m = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d{2}))", scope)
                    for val in amt_m:
                        cv = float(val.replace(',', ''))
                        if cv != r: b = cv; break
                    break

            n = 0
            n_m = re.search(r"未開\s*(\d+)", txt)
            if n_m: n = int(n_m.group(1))
            if r <= 0: return [TextMessage(text="❓ 辨識失敗，請確保數據區清晰。")]

            trend_text, trend_color = "🆕 今日首次分析", "#AAAAAA"
            last_record = supabase.table("usage_logs").select("rtp_value").eq("room_id", room).order("created_at", descending=True).limit(1).execute()
            if last_record.data:
                diff = r - float(last_record.data[0]['rtp_value'])
                if diff > 0.01: trend_text, trend_color = f"🔥 趨勢升溫 (+{diff:.2f}%)", "#D50000"
                elif diff < -0.01: trend_text, trend_color = f"❄️ 數據冷卻 ({diff:.2f}%)", "#1976D2"
                else: trend_text, trend_color = "➡️ 數據平穩", "#555555"

            today_str = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{b:.2f}" 
            try:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "rtp_value": r, "room_id": room, "data_hash": data_hash}).execute()
            except: pass

            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            return [
                FlexMessage(alt_text="賽特 AI 分析", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color, data_hash))),
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
                    supabase.table("members").update({"status": "approved", "member_level": level}).eq("line_user_id", target_uid).execute()
                    line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text=f"🎉 您的帳號已核准開通！")]))
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 已核准。")]))
                return

            if msg == "熱門戰報":
                report = get_trending_report()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=report, quick_reply=get_main_menu())]))
            elif msg == "我的額度":
                today_str = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日使用：{count_res.count or 0} / {limit}", quick_reply=get_main_menu())]))
            elif msg == "我要開通":
                if user_data and user_data.get("status") == "approved":
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 您的帳號早已開通。")]))
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                    if ADMIN_LINE_ID:
                        line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[FlexMessage(alt_text="新申請", contents=FlexContainer.from_dict(get_admin_approve_flex(user_id)))]))
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 申請已送出，管理員 LINE:adong8989。")]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 賽特 AI 分析系統：請傳送截圖。", quick_reply=get_main_menu())]))
        
        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通管理員 LINE:adong8989。")]))
            result_messages = sync_image_analysis(user_id, event.message.id, limit)
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=result_messages))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
