import os, re, uuid, hashlib, base64, datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, ImageMessage, TextSendMessage
from supabase import create_client
import openai
import cv2
import numpy as np

# ================= 基本設定 =================

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

openai.api_key = os.getenv("OPENAI_API_KEY")

# ================= 使用次數 / 防重算 =================

def make_analysis_id(user_id, image_bytes):
    return f"{user_id}:{hashlib.md5(image_bytes).hexdigest()}"

def used_today(user_id, analysis_id):
    today = datetime.date.today().isoformat()
    r = supabase.table("usage_logs") \
        .select("id") \
        .eq("user_id", user_id) \
        .eq("date", today) \
        .eq("analysis_id", analysis_id) \
        .execute()
    return len(r.data) > 0

def log_usage(user_id, analysis_id):
    supabase.table("usage_logs").insert({
        "user_id": user_id,
        "date": datetime.date.today().isoformat(),
        "analysis_id": analysis_id
    }).execute()

# ================= 裁切 OCR（自適應） =================

def smart_crop(image_bytes):
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    h, w, _ = img.shape

    # 判斷直式 / 橫式
    is_portrait = h >= w

    # 下方比例（直式裁多一點）
    crop_ratio = 0.45 if is_portrait else 0.4
    y_start = int(h * (1 - crop_ratio))

    cropped = img[y_start:h, 0:w]
    _, buf = cv2.imencode(".png", cropped)
    return buf.tobytes()

# ================= OCR =================

def vision_ocr(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    res = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "請只輸出圖片中的文字"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]
        }]
    )
    return res.choices[0].message.content

# ================= OCR 解析（下方資訊區） =================

def parse_seth(txt):
    block = ""
    for k in ["時間", "總下注額", "得分率", "近30天"]:
        if k in txt:
            block = txt[txt.find(k):]
            break
    if not block:
        block = txt

    room = "未知"
    m = re.search(r"(\d{3,5})\s*機台", block)
    if m:
        room = m.group(1)

    bet = 0
    m = re.search(r"今日[^\d]{0,10}([\d,]+(?:\.\d+)?)", block)
    if m:
        bet = float(m.group(1).replace(",", ""))

    rtp = 0
    m = re.search(r"今日[\s\S]{0,40}?(\d{2,3}(?:\.\d+)?)\s*%", block)
    if m:
        rtp = float(m.group(1))

    spins = 0
    m = re.search(r"未\s*開\s*(\d+)", block)
    if m:
        spins = int(m.group(1))

    return room, spins, bet, rtp

# ================= LINE Webhook =================

@app.route("/callback", methods=["POST"])
def callback():
    handler.handle(
        request.get_data(as_text=True),
        request.headers["X-Line-Signature"]
    )
    return "OK"

# ================= 圖片分析主流程 =================

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    content = line_bot_api.get_message_content(event.message.id)
    img_bytes = content.content

    analysis_id = make_analysis_id(user_id, img_bytes)

    if used_today(user_id, analysis_id):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="⚠️ 此圖片已分析過，不重複扣次")
        )
        return

    # ① 先裁切 OCR
    cropped = smart_crop(img_bytes)
    txt = vision_ocr(cropped)

    room, spins, bet, rtp = parse_seth(txt)

    # ② 若裁切失敗 → fallback 整張
    if rtp == 0:
        txt = vision_ocr(img_bytes)
        room, spins, bet, rtp = parse_seth(txt)

    log_usage(user_id, analysis_id)

    risk = "低風險 / 數據優異" if rtp >= 100 else "注意風險"

    reply = f"""🎰 賽特分析
房號：{room}

未開轉數：{spins}
今日下注：{int(bet):,}
今日 RTP：{rtp:.2f}%

判定：{risk}"""

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# ================= 啟動 =================

if __name__ == "__main__":
    app.run()
