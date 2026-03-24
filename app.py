import os
import logging
import re
import random
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

# === 精準解析邏輯 (New!) ===
def extract_today_block(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if "今日" in line:
            block = " ".join(lines[i:i+8])
            if "近30天" in block: block = block.split("近30天")[0]
            return block
    return text

def parse_ocr_refined(text):
    block = extract_today_block(text)
    # 房號辨識
    room = "未知"
    m = re.search(r"(\d{3,5})\s*機台", text)
    if m: room = m.group(1)
    else:
        nums = re.findall(r"\b\d{4}\b", text)
        if nums: room = nums[-1]

    # RTP 辨識 (過濾合理區間)
    rtp = 0.0
    rtp_candidates = re.findall(r"(\d{2,3}\.\d+)\s*%", block)
    for val in rtp_candidates:
        v = float(val)
        if 70 <= v <= 200:
            rtp = v
            break

    # 今日下注金額
    bet = 0.0
    nums = re.findall(r"([\d,]+\.\d{2})", block)
    clean_nums = [float(n.replace(",", "")) for n in nums if 1000 < float(n.replace(",", "")) < 10000000]
    if clean_nums: bet = min(clean_nums)

    # 未開轉數
    spins = 0
    m_s = re.search(r"未開\s*(\d+)", text)
    if m_s: spins = int(m_s.group(1))

    return room, spins, bet, rtp

# === 視覺美化卡片 ===
def get_flex_card(room, n, r, b, trend_text, trend_color, seed_hash):
    random.seed(seed_hash)
    if n > 250 or r > 120:
        base_color, label, risk_percent, status = "#D50000", "🚨 高風險 / 建議換房", "100%", "high"
    elif n > 150 or r > 110:
        base_color, label, risk_percent, status = "#FFAB00", "⚠️ 中風險 / 謹慎進場", "60%", "mid"
    else:
        base_color, label, risk_percent, status = "#00C853", "✅ 低風險 / 數據優良", "30%", "low"
    
    all_items = [("眼睛", 6), ("弓箭", 6), ("權杖蛇", 6), ("彎刀", 6), ("紅寶石", 6), ("藍寶石", 6), ("綠寶石", 6), ("黃寶石", 6), ("紫寶石", 6), ("聖甲蟲", 3)]
    selected_items = random.sample(all_items, 2)
    combo = "、".join([f"{name}{random.randint(1, limit)}顆" for name, limit in selected_items])
    
    tips = {
        "high": [f"❌ 盤面較硬，雖然出現「{combo}」，但分布太散容易咬分。", f"⚠️ 偵測到回收訊號，目前「{combo}」氣場不足。"],
        "mid": [f"⚖️ 盤面拉鋸中，若看到「{combo}」頻繁出現，可小試幾轉。", f"🔍 觀察中：目前「{combo}」頻率尚可。"],
        "low": [f"✅ 氣場極強！盤面出現「{combo}」組合，大噴發機率攀升。", f"🔥 訊號亮起！出現「{combo}」帶動，大獎將至。"]
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
                {"type": "text", "text": f"💰 今日下注：{b:,.2f}", "size": "md", "weight": "bold"}
            ]},
            {"type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F8F8", "paddingAll": "10px", "contents": [
                {"type": "text", "text": "🔮 AI 進場訊號", "weight": "bold", "size": "xs", "color": "#555555"},
                {"type": "text", "text": f"{current_tip}", "size": "sm", "wrap": True}
            ]}
        ]}
    }

# === 核心分析流程 ===
def sync_image_analysis(user_id, message_id, base_limit):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        try:
            img_bytes = blob_api.get_message_content(message_id)
            
            # OCR 呼叫 (Engine 2)
            payload = {'apikey': OCR_SPACE_API_KEY, 'language': 'chs', 'OCREngine': 2, 'scale': True}
            files = {'filename': ('img.jpg', img_bytes, 'image/jpeg')}
            res = requests.post('https://api.ocr.space/parse/image', files=files, data=payload, timeout=15).json()
            
            if res.get("OCRExitCode") != 1: return [TextMessage(text="❌ 辨識服務異常，請稍後。")]
            
            # 使用新版解析
            raw_text = res["ParsedResults"][0]["ParsedText"]
            room, n, b, r = parse_ocr_refined(raw_text)
            
            if r <= 0: return [TextMessage(text="❓ 辨識失敗，請確保數據區(今日)清晰無遮擋。")]

            # 防重複與額度處理
            today_str = get_tz_now().strftime('%Y-%m-%d')
            data_hash = f"{room}_{int(b)}_{int(r)}"
            
            dup = supabase.table("usage_logs").select("id").eq("line_user_id", user_id).eq("used_at", today_str).eq("data_hash", data_hash).execute()
            if dup.data: return [TextMessage(text="⚠️ 此截圖已分析過。", quick_reply=get_main_menu())]

            m_res = supabase.table("members").select("extra_limit").eq("line_user_id", user_id).maybe_single().execute()
            current_extra = m_res.data.get("extra_limit", 0) if m_res and m_res.data else 0
            
            is_extra_use = False
            if current_extra > 0:
                current_extra -= 1
                supabase.table("members").update({"extra_limit": current_extra}).eq("line_user_id", user_id).execute()
                is_extra_use = True

            supabase.table("usage_logs").insert({"line_user_id": user_id, "used_at": today_str, "rtp_value": r, "room_id": room, "data_hash": data_hash}).execute()

            # 趨勢計算
            trend_text, trend_color = "🆕 今日首次分析", "#AAAAAA"
            last = supabase.table("usage_logs").select("rtp_value").eq("room_id", room).order("created_at", desc=True).limit(2).execute()
            if len(last.data) > 1:
                diff = r - float(last.data[1]['rtp_value'])
                if diff > 0.01: trend_text, trend_color = f"🔥 趨勢升溫 (+{diff:.2f}%)", "#D50000"
                elif diff < -0.01: trend_text, trend_color = f"❄️ 數據冷卻 ({diff:.2f}%)", "#1976D2"
                else: trend_text, trend_color = "➡️ 數據平穩", "#555555"

            # 額度計算顯示
            used_today = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today_str).execute().count or 0
            rem_base = max(0, base_limit - (used_today - 1 if is_extra_use else used_today))

            return [
                FlexMessage(alt_text="分析結果", contents=FlexContainer.from_dict(get_flex_card(room, n, r, b, trend_text, trend_color, data_hash))),
                TextMessage(text=f"📊 剩餘額度：{rem_base + current_extra} 次", quick_reply=get_main_menu())
            ]
        except Exception as e:
            logger.error(f"Error: {e}"); return [TextMessage(text="分析出錯，請洽管理員。")]

# === 路由與 Webhook ===
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
        user_data, is_approved, base_limit, extra_limit = None, is_admin, 15, 0

        try:
            m_res = supabase.table("members").select("*").eq("line_user_id", user_id).maybe_single().execute()
            if m_res and m_res.data:
                user_data = m_res.data
                if user_data.get("status") == "approved":
                    is_approved = True
                    base_limit = 50 if user_data.get("member_level") == "vip" else 15
                    extra_limit = user_data.get("extra_limit", 0)
        except: pass

        if event.message.type == "text":
            msg = event.message.text.strip()
            if is_admin and msg.startswith("#"):
                if msg.startswith("#核准_"):
                    p = msg.split("_")
                    supabase.table("members").upsert({"line_user_id": p[2], "status": "approved", "member_level": p[1]}, on_conflict="line_user_id").execute()
                    line_api.push_message(PushMessageRequest(to=p[2], messages=[TextMessage(text="🎉 帳號已核准開通！")]))
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 已完成核准。")]))
                if msg.startswith("#加次數_"):
                    p = msg.split("_")
                    cur = supabase.table("members").select("extra_limit").eq("line_user_id", p[2]).maybe_single().execute()
                    new_e = (cur.data.get("extra_limit", 0) if cur.data else 0) + int(p[1])
                    supabase.table("members").update({"extra_limit": new_e}).eq("line_user_id", p[2]).execute()
                    return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 已增加額度。")]))

            if msg == "我的額度":
                today = get_tz_now().strftime('%Y-%m-%d')
                used = supabase.table("usage_logs").select("id", count="exact").eq("line_user_id", user_id).eq("used_at", today).execute().count or 0
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"📊 剩餘額度：{max(0, base_limit + extra_limit - used)} 次", quick_reply=get_main_menu())]))
            elif msg == "我要開通":
                if is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ 帳號已開通。")]))
                supabase.table("members").upsert({"line_user_id": user_id, "status": "pending"}, on_conflict="line_user_id").execute()
                if ADMIN_LINE_ID: line_api.push_message(PushMessageRequest(to=ADMIN_LINE_ID, messages=[FlexMessage(alt_text="新申請", contents=FlexContainer.from_dict(get_admin_approve_flex(user_id)))]))
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"✅ 申請已送出！\n您的 ID：\n{user_id}")]))
            else:
                line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 賽特 AI：請傳送截圖分析。", quick_reply=get_main_menu())]))

        elif event.message.type == "image":
            if not is_approved: return line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 請先申請開通。")]))
            line_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=sync_image_analysis(user_id, event.message.id, base_limit)))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
