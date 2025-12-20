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

# 環境變數載入 (略，請保持原本設定)
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
    logger.error(f"Vision Client Error: {e}")

def get_tz_now(): return datetime.now(timezone(timedelta(hours=8)))
def clean_num(text): return re.sub(r'[^\d.]', '', text.replace(',', '')) if text else "0"
def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明"))
    ])

def get_flex_card(n, r, b):
    # (保持原本 Flex Card 邏輯...)
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
        
        # 1. 權限獲取
        is_approved = False
        user_status, limit = "none", 15
        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_status = m_res.data.get("status", "none")
                is_approved = (user_status == "approved")
                limit = 50 if m_res.data.get("member_level") == "vip" else 15
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            
            # --- 管理員區 (用 return 確保不往下跑) ---
            if user_id == ADMIN_LINE_ID:
                if msg.startswith("核准 "):
                    target_uid = msg.split(" ")[1]
                    supabase.table("members").upsert({"line_user_id": target_uid, "status": "approved"}, on_conflict="line_user_id").execute()
                    line_api.push_message(PushMessageRequest(to=target_uid, messages=[TextMessage(text="🎉 您的帳號已核准開通！", quick_reply=get_main_menu())]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已核准：{target_uid}")]))
                
                if msg.startswith("備註 "):
                    parts = msg.split(" ", 2)
                    if len(parts) >= 3:
                        target_uid, content = parts[1], parts[2]
                        supabase.table("members").update({"remark": content}).eq("line_user_id", target_uid).execute()
                        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已備註：\nID: {target_uid}\n內容: {content}")]))

            # --- 選單功能 ---
            if msg == "我要開通":
                if user_status == "blocked": return
                if is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="您已是正式會員。", quick_reply=get_main_menu())]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[TextMessage(text=f"🔔 申請：{user_id}\n核准 {user_id}")]))
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"⚠️ 未開通。請提供 ID 給管理員：\n\n{user_id}", quick_reply=get_main_menu())]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                count = 0
                u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today).execute()
                if u_res and u_res.data: count = u_res.data[0]["used_count"]
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 今日分析：{count} / {limit}", quick_reply=get_main_menu())]))

            if msg in ["(房間資訊表格)", "房間資訊表格", "使用說明"]:
                txt = "📘 使用說明：\n1. 傳截圖自動分析。\n2. 手動輸入：房號 轉數 下注 RTP"
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=txt)]))

            # --- 分析判定 ---
            is_line_id = msg.startswith('U') and len(msg) > 30
            clean_nums = re.findall(r'(?<![a-zA-Z])\d+(?:\.\d+)?(?![a-zA-Z])', msg)

            if len(clean_nums) >= 4 and is_approved and not is_line_id:
                try:
                    room, n, b, r = clean_nums[0], int(float(clean_nums[1])), float(clean_nums[2]), float(clean_nums[3])
                    today_str = get_tz_now().strftime('%Y-%m-%d')
                    fingerprint = f"{room}_{n}_{b}"
                    
                    # 檢查重複
                    dup = supabase.table("usage_logs").select("*").eq("data_hash", fingerprint).eq("used_at", today_str).execute()
                    if dup and dup.data:
                        return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 此數據已分析過。")]))
                    
                    # 計算額度
                    u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                    new_count = (u_res.data[0]["used_count"] + 1) if (u_res and u_res.data) else 1
                    if new_count > limit: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 今日額度已滿。")]))
                    
                    # 更新紀錄
                    if u_res and u_res.data:
                        supabase.table("usage_logs").update({"used_count": new_count, "data_hash": fingerprint}).eq("line_user_id", user_id).eq("used_at", today_str).execute()
                    else:
                        supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "used_count": new_count, "data_hash": fingerprint}).execute()
                    
                    return line_api.reply_message(ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(get_flex_card(n, r, b))),
                            TextMessage(text=f"📊 今日：{new_count} / {limit}", quick_reply=get_main_menu())
                        ]
                    ))
                except: pass

        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"⚠️ 未開通：{user_id}")]))
            
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

            if r == 0.0: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 無法辨識數據。")]))
            
            today_str = get_tz_now().strftime('%Y-%m-%d')
            fingerprint = f"{room}_{n}_{b}"
            
            dup = supabase.table("usage_logs").select("*").eq("data_hash", fingerprint).eq("used_at", today_str).execute()
            if dup and dup.data: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🚫 數據重複。")]))
            
            u_res = supabase.table("usage_logs").select("used_count").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            new_count = (u_res.data[0]["used_count"] + 1) if (u_res and u_res.data) else 1
            if new_count > limit: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 額度已滿。")]))
            
            if u_res and u_res.data:
                supabase.table("usage_logs").update({"used_count": new_count, "data_hash": fingerprint}).eq("line_user_id", user_id).eq("used_at", today_str).execute()
            else:
                supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "used_count": new_count, "data_hash": fingerprint}).execute()

            return line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    FlexMessage(alt_text="賽特分析報告", contents=FlexContainer.from_dict(get_flex_card(n, r, b))),
                    TextMessage(text=f"📊 今日：{new_count} / {limit}", quick_reply=get_main_menu())
                ]
            ))
