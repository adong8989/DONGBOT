import os
import logging
import re
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, abort

from supabase import create_client
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    TextMessage, ReplyMessageRequest
)
from linebot.v3.webhooks import MessageEvent
from linebot.v3.exceptions import InvalidSignatureError

# ===== 初始化 =====
load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== 工具 =====
def now():
    return datetime.now(timezone(timedelta(hours=8)))

# ===== OCR 強化解析 =====
def extract_today_block(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        if "今日" in line:
            block = " ".join(lines[i:i+8])

            # 避開近30天
            if "近30天" in block:
                block = block.split("近30天")[0]

            return block

    return text


def parse_ocr(text):
    block = extract_today_block(text)

    # ===== 房號 =====
    room = "未知"
    m = re.search(r"(\d{3,5})\s*機台", text)
    if m:
        room = m.group(1)
    else:
        nums = re.findall(r"\b\d{4}\b", text)
        if nums:
            room = nums[-1]

    # ===== RTP =====
    rtp = 0.0
    rtp_candidates = re.findall(r"(\d{2,3}\.\d+)\s*%", block)
    for val in rtp_candidates:
        v = float(val)
        if 70 <= v <= 200:
            rtp = v
            break

    # ===== 今日下注 =====
    bet = 0.0
    nums = re.findall(r"([\d,]+\.\d{2})", block)

    clean_nums = []
    for n in nums:
        v = float(n.replace(",", ""))
        if 1000 < v < 10000000:  # 過濾小數 / RTP
            clean_nums.append(v)

    if clean_nums:
        bet = min(clean_nums)  # 今日通常比30天小

    # ===== 未開 =====
    spins = 0
    m = re.search(r"未開\s*(\d+)", text)
    if m:
        spins = int(m.group(1))

    return room, spins, bet, rtp


# ===== OCR 呼叫 =====
def do_ocr(image_bytes):
    payload = {
        "apikey": OCR_SPACE_API_KEY,
        "language": "chs",
        "OCREngine": 2
    }

    files = {"file": ("img.jpg", image_bytes)}

    try:
        res = requests.post(
            "https://api.ocr.space/parse/image",
            files=files,
            data=payload,
            timeout=15
        )
        data = res.json()
    except Exception as e:
        logger.error(f"OCR Error: {e}")
        return None

    if data.get("OCRExitCode") != 1:
        logger.error(data)
        return None

    return data["ParsedResults"][0]["ParsedText"]


# ===== 防重算 =====
def is_duplicate(user_id, data_hash, today):
    res = supabase.table("usage_logs") \
        .select("id") \
        .eq("line_user_id", user_id) \
        .eq("used_at", today) \
        .eq("data_hash", data_hash) \
        .execute()

    return bool(res.data)


# ===== 主分析 =====
def analyze(user_id, message_id):
    with ApiClient(configuration) as api_client:
        blob = MessagingApiBlob(api_client)
        img = blob.get_message_content(message_id)

    text = do_ocr(img)
    if not text:
        return "❌ OCR 失敗"

    room, spins, bet, rtp = parse_ocr(text)

    if rtp == 0:
        return "❌ 無法辨識數據"

    today = now().strftime("%Y-%m-%d")
    data_hash = f"{room}_{int(bet)}_{int(rtp)}"

    if is_duplicate(user_id, data_hash, today):
        return "⚠️ 此圖已分析過"

    supabase.table("usage_logs").insert({
        "line_user_id": user_id,
        "used_at": today,
        "room_id": room,
        "rtp_value": rtp,
        "data_hash": data_hash
    }).execute()

    risk = "🔥 低風險" if rtp >= 100 else "⚠️ 注意"

    return f"""🎰 賽特分析
房號：{room}

未開：{spins}
今日下注：{int(bet):,}
RTP：{rtp}%

判定：{risk}"""


# ===== LINE Webhook =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent)
def handle(event):
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)

        if event.message.type == "image":
            result = analyze(user_id, event.message.id)

            line_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=result)]
                )
            )

        else:
            line_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請傳送截圖")]
                )
            )


# ===== 啟動 =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
