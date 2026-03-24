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
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== 工具 =====
def get_tz_now():
    return datetime.now(timezone(timedelta(hours=8)))

def safe_get_extra(res):
    if res and isinstance(res.data, dict):
        return res.data.get("extra_limit", 0)
    return 0

def get_main_menu():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔥 熱門戰報", text="熱門戰報")),
        QuickReplyItem(action=MessageAction(label="📊 我的額度", text="我的額度")),
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通"))
    ])

# ===== OCR 強化 =====
def extract_today_block(lines):
    for i, line in enumerate(lines):
        if "今日" in line:
            block = " ".join(lines[i:i+8])
            if "近30天" in block:
                block = block.split("近30天")[0]
            return block
    return " ".join(lines)

def parse_ocr(txt):
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    block = extract_today_block(lines)

    room = "未知"
    for line in reversed(lines):
        if re.fullmatch(r"\d{3,4}", line):
            room = line
            break

    r = 0
    for v in re.findall(r"(\d{2,3}\.\d+)\s*%", block):
        val = float(v)
        if 70 <= val <= 200:
            r = val
            break

    b = 0
    nums = re.findall(r"([\d,]+\.\d{2})", block)
    valid = [float(n.replace(",", "")) for n in nums if 1000 < float(n.replace(",", "")) < 10000000]
    if valid:
        b = min(valid)

    n = 0
    m = re.search(r"未開\s*(\d+)", txt)
    if m:
        n = int(m.group(1))

    return room, n, b, r

# ===== OCR API =====
def do_ocr(img):
    try:
        res = requests.post(
            "https://api.ocr.space/parse/image",
            files={"file": ("img.jpg", img)},
            data={"apikey": OCR_SPACE_API_KEY, "language": "chs", "OCREngine": 2},
            timeout=15
        )
        data = res.json()
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return None

    if data.get("OCRExitCode") != 1:
        return None

    return data["ParsedResults"][0]["ParsedText"]

# ===== Flex =====
def get_flex(room, n, r, b, trend, color, seed):
    random.seed(seed)

    items = [("眼睛",6),("紅寶石",6),("藍寶石",6),("聖甲蟲",3)]
    pick = random.sample(items,2)
    combo = "、".join([f"{i}{random.randint(1,c)}顆" for i,c in pick])

    tip = f"🔥 訊號：「{combo}」可能觸發爆分"

    random.seed(None)

    return {
        "type":"bubble",
        "body":{
            "type":"box",
            "layout":"vertical",
            "contents":[
                {"type":"text","text":f"🎰 房號 {room}","weight":"bold","size":"xl"},
                {"type":"text","text":trend,"color":color},
                {"type":"separator"},
                {"type":"text","text":f"未開：{n}"},
                {"type":"text","text":f"RTP：{r}%"},
                {"type":"text","text":f"下注：{int(b):,}"},
                {"type":"separator"},
                {"type":"text","text":"🔮 推薦進場"},
                {"type":"text","text":tip,"wrap":True}
            ]
        }
    }

# ===== 分析 =====
def analyze(user_id, message_id, base_limit):
    with ApiClient(configuration) as api_client:
        blob = MessagingApiBlob(api_client)
        img = blob.get_message_content(message_id)

    txt = do_ocr(img)
    if not txt:
        return [TextMessage(text="❌ OCR失敗")]

    room,n,b,r = parse_ocr(txt)
    if r == 0:
        return [TextMessage(text="❌ 無法辨識")]

    today = get_tz_now().strftime("%Y-%m-%d")
    data_hash = f"{room}_{int(b)}_{int(r)}"

    dup = supabase.table("usage_logs").select("id").eq("line_user_id",user_id).eq("used_at",today).eq("data_hash",data_hash).execute()
    if dup.data:
        return [TextMessage(text="⚠️ 已分析過", quick_reply=get_main_menu())]

    supabase.table("usage_logs").insert({
        "line_user_id":user_id,
        "used_at":today,
        "room_id":room,
        "rtp_value":r,
        "data_hash":data_hash
    }).execute()

    trend = "📊 初次分析"
    color = "#888888"

    res = supabase.table("usage_logs").select("rtp_value").eq("room_id",room).order("created_at",desc=True).limit(2).execute()
    if res.data and len(res.data)>1:
        diff = r - float(res.data[1]["rtp_value"])
        if diff>0: trend,color=f"🔥 上升 {diff:.2f}%","#D50000"
        elif diff<0: trend,color=f"❄️ 下降 {diff:.2f}%","#1976D2"

    return [
        FlexMessage(alt_text="分析", contents=FlexContainer.from_dict(get_flex(room,n,r,b,trend,color,data_hash))),
        TextMessage(text="📊 分析完成", quick_reply=get_main_menu())
    ]

# ===== Webhook =====
@app.route("/callback",methods=["POST"])
def callback():
    handler.handle(request.get_data(as_text=True),request.headers["X-Line-Signature"])
    return "OK"

@handler.add(MessageEvent)
def handle(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        if event.message.type=="image":
            msgs = analyze(user_id,event.message.id,15)
            api.reply_message(ReplyMessageRequest(reply_token=event.reply_token,messages=msgs))
        else:
            api.reply_message(ReplyMessageRequest(reply_token=event.reply_token,messages=[TextMessage(text="請傳截圖",quick_reply=get_main_menu())]))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",10000)))
