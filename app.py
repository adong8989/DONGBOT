import os
import tempfile
import logging
import re
import random
import json
import requests
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
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY") 
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

def get_flex_card(room, n, r, b, trend_text, trend_color, seed_hash):
    random.seed(seed_hash)
    if n > 250 or r > 120:
        base_color = "#D50000"; label = "🚨 高風險 / 建議換房"; risk_percent = "100%"; status = "high"
    elif n > 150 or r > 110:
        base_color = "#FFAB00"; label = "⚠️ 中風險 / 謹慎進場"; risk_percent = "60%"; status = "mid"
    else:
        base_color = "#00C853"; label = "✅ 低風險 / 數據優良"; risk_percent = "30%"; status = "low"
    
    all_items = [("眼睛", 6), ("弓箭", 6), ("權杖蛇", 6), ("彎刀", 6), ("紅寶石", 6), ("藍寶石", 6), ("綠寶石", 6), ("黃寶石", 6), ("紫寶石", 6), ("聖甲蟲", 3)]
    selected_items = random.sample(all_items, 2)
    combo = "、".join([f"{name}{random.randint(1, limit)}顆" for name, limit in selected_items])
    
    tips = {
        "high": [f"❌ 盤面較硬，雖然出現「{combo}」，但分布太散容易咬分，建議換房。", f"⚠️ 偵測到回收訊號，目前「{combo}」氣場不足，請小心操作。"],
        "mid": [f"⚖️ 盤面拉鋸中，若看到「{combo}」頻繁出現，可以考慮小試幾轉。", f"🔍 觀察中：目前「{combo}」頻率尚可，建議平注守好。"],
        "low": [f"✅ 氣場極強！盤面出現「{combo}」組合，大噴發機率攀升。", f"🔥 訊號亮起！出現「{combo}」帶動，大獎可能就在最近幾轉。"]
    }
    current_tip = random.choice(tips[status])
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
            {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"📍 未開轉數：{n}", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"📈 今日 RTP：{r}%", "size": "md", "weight": "bold"},
                {"type": "text", "text": f"💰 今日總下注：{b:,.2f}", "size": "md", "weight": "bold"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 AI 進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": f"{current_tip}", "size": "sm", "wrap": True}
            ]}
        ]}
    }

def get_trending_report():
    try:
        res = supabase.table("usage_logs").select("room_id, rtp_value").order("created_at", desc=True).limit(100).execute()
        if not res.data: return "目前暫無數據，請先傳送截圖。"
        rooms = {}
        for item in res.data:
            rid = str(item['room_id']); rtp = float(item['rtp_value'])
            if rid not in rooms or rtp > rooms[rid]: rooms[rid] = rtp
        sorted_rooms = sorted(rooms.items(), key=lambda x: x[1], reverse=True)[:5]
        report_text = "🔥 戰神賽特｜即時熱門排行：\n"
        medals = ["🥇", "🥈", "🥉", "▫️", "▫️"]
        for i, (rid, rtp) in enumerate(sorted_rooms):
            report_text += f"{medals[i]} 房號: {rid} | RTP: {rtp}%\n"
        return report_text + "\n💡 數據由全體用戶貢獻。"
    except Exception as e:
        logger.error(f"Report Error: {e}"); return f"戰報生成錯誤: {str(e)}"

def sync_image_analysis(user_id, message_id, base_limit):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            payload = {'apikey': OCR_SPACE_API_KEY, 'language': 'chs', 'scale': True, 'OCREngine': 2}
            files = {'filename': ('image.jpg', img_bytes, 'image/jpeg')}
            ocr_res = requests.post('https://api.ocr.space/parse/image', files=files, data=payload, timeout=15)
            ocr_result = ocr_res.json()
            
            if ocr_result.get("OCRExitCode") == 1:
                txt = ocr_result["ParsedResults"][0]["ParsedText"]
            else:
                return [TextMessage(text="❌ 辨識服務暫時不可用。")]

            lines = [l.strip() for l in txt.split('\n') if l.strip()]
            
            # --- 房號抓取修正區 ---
            room = "未知"
            for line in lines:
                # 尋找包含 3-4 位數字且有 "機台" 的行
                room_match = re.search(r"(\d{3,4})\s*機台", line)
                if room_match:
                    room = room_match.group(1)
                    break
            if room == "未知": # 備援：若沒抓到機台字樣，找整行純數字
                for line in reversed(lines):
                    if re.fullmatch(r"\d{3,4}", line): room = line; break
            # -----------------------

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

            today_str = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{b:.2f}" 
            
            dup_check = supabase.table("usage_logs").select("id").eq("line_user_id", user_id).eq("used_at", today_str).eq("data_hash", data_hash).execute()
            if dup_check.data:
                return [TextMessage(text="⚠️ 此截圖已分析過，請勿重複傳送。", quick_reply=get_main_menu())]

            m_res = supabase.table("members").select("extra_limit").eq("line_user_id", user_id).maybe_single().execute()
            current_extra = m_res.data.get("extra_limit", 0) if m_res and m_res.data else 0
            
            is_extra_use = False
            if current_extra > 0:
                current_extra -= 1
                supabase.table("members").update({"extra_limit": current_extra}).eq("line_user_id", user_id).execute()
                is_extra_use = True

            supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "rtp_value": r, "room_id": room, "data_hash": data_hash}).execute()

            trend_text, trend_color = "🆕 今日首次分析", "#AAAAAA"
            try:
                last_record = supabase.table("usage_logs").select("rtp_value").eq("room_id", room).order("created_at", desc=True).limit(2).execute()
                if len(last_record.data) > 1:
                    diff = r - float(last_record.data[1]['rtp_value'])
                    if diff > 0.01: trend_text, trend_color = f"🔥 趨勢升溫 (+{diff:.2f}%)", "#D50000"
                    elif diff < -0.01: trend_text, trend_color = f"❄️ 數據冷卻 ({diff:.2f}%)", "#1976D2"
                    else: trend_text, trend_color = "➡️ 數據平穩", "#555555"
            except: pass

            count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
            total_used_today = count_res.count or 0
            effective_base_used = total_used_today - 1 if is_extra_use else total_used_today
            remain_base = max(0, base_limit - effective_base_used)
            total_remaining = remain_base + current_extra

            return [
                FlexMessage(alt_text="賽特 AI 分析", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color, data_hash))),
                TextMessage(text=f"📊 剩餘總額度：{total_remaining} 次\n(基礎：{remain_base} + 額外：{current_extra})", quick_reply=get_main_menu())
            ]
        except Exception as e:
            logger.error(f"Logic Error: {e}"); return [TextMessage(text=f"分析失敗: {str(e)}")]

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
        base_limit = 15; extra_limit = 0; is_approved = is_admin

        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_data = m_res.data
                if user_data.get("status") == "approved":
                    is_approved = True
                    base_limit = 50 if user_data.get("member_level") == "vip" else 15
                    extra_limit = user_data.get("extra_limit", 0)
        except: pass

        total_limit = base_limit + extra_limit

        if event.message.type == "text":
            msg = event.message.text.strip()
            if is_admin:
                if msg.startswith("#核准_"):
                    p = msg.split("_")
                    if len(p) == 3:
                        supabase.table("members").update({"status": "approved", "member_level": p[1]}).eq("line_user_id", p[2]).execute()
                        line_api.push_message(PushMessageRequest(to=p[2], messages=[TextMessage(text="🎉 您的帳號已核准開通！")]))
                        line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 已核准。")]))
                    return
                if msg.startswith("#加次數_"):
                    p = msg.split("_")
                    if len(p) == 3:
                        try:
                            cur = supabase.table("members").select("extra_limit").eq("line_user_id", p[2]).maybe_single().execute()
                            new_val = (cur.data.get("extra_limit", 0) if cur.data else 0) + int(p[1])
                            supabase.table("members").update({"extra_limit": new_val}).eq("line_user_id", p[2]).execute()
                            line_api.push_message(PushMessageRequest(to=p[2], messages=[TextMessage(text=f"🎁 管理員已為您增加 {p[1]} 次臨時額度！")]))
                            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 已增加額度。")]))
                        except: pass
                    return

            if msg == "熱門戰報":
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=get_trending_report(), quick_reply=get_main_menu())]))
            elif msg == "我的額度":
                today_str = get_tz_now().strftime('%Y-%m-%d')
                count_res = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute()
                used_today = count_res.count or 0
                remain_total = max(0, total_limit - used_today)
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 剩餘總額度：{remain_total} 次\n(基礎: {base_limit} + 額外: {extra_limit})", quick_reply=get_main_menu())]))
            elif msg == "我要開通":
                if user_data and user_data.get("status") == "approved":
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 您的帳號已開通。")]))
                elif user_data and user_data.get("status") == "pending":
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⏳ 審核中，請截圖 ID 給管理員。")]))
                else:
                    supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                    if ADMIN_LINE_ID: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[FlexMessage(alt_text="新申請", contents=FlexContainer.from_dict(get_admin_approve_flex(user_id)))]))
                    line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 申請已送出！\n您的 ID：\n{user_id}\n請傳給管理員 LINE:adong8989。")]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 賽特 AI 分析系統：請傳送截圖。", quick_reply=get_main_menu())]))
        
        elif event.message.type == "image":
            if not is_approved:
                return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通管理員 LINE:adong8989。")]))
            
            result_messages = sync_image_analysis(user_id, event.message.id, base_limit)
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=result_messages))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
