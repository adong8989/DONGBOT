# app.py
from flask import Flask, request, abort, jsonify
import os
import logging
import io
import re # 用於 OCR 文字提取
from dotenv import load_dotenv
from supabase import create_client
from linebot.v3.webhook import WebhookHandler, MessageEvent, ImageMessage # 導入 ImageMessage
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction, URIAction
from datetime import datetime, timezone, timedelta
import hashlib
import random

# === load env & basic debug info ===
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "")  # optional
AUTO_SAVE_SIGNALS = os.getenv("AUTO_SAVE_SIGNALS", "false").lower() in ("1", "true", "yes")

# Signals pool env (format: 名稱:上限,名稱:上限,...)
SIGNALS_POOL_ENV = os.getenv("SIGNALS_POOL", "")

# Threshold envs with defaults
NOT_OPEN_HIGH = int(os.getenv("NOT_OPEN_HIGH", 250))
NOT_OPEN_MED = int(os.getenv("NOT_OPEN_MED", 150))
NOT_OPEN_LOW = int(os.getenv("NOT_OPEN_LOW", 50))
RTP_HIGH = int(os.getenv("RTP_HIGH", 120))
RTP_MED = int(os.getenv("RTP_MED", 110))
RTP_LOW = int(os.getenv("RTP_LOW", 90))
BETS_HIGH = int(os.getenv("BETS_HIGH", 80000))
BETS_LOW = int(os.getenv("BETS_LOW", 30000))

# Simple env sanity check (do not print secrets)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
required_envs = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"]
for name in required_envs:
    logger.info(f"ENV {name} set: {bool(os.getenv(name))}")

# === Google Cloud Vision Client Initialization ===
vision_client = None
try:
    from google.cloud import vision
    # 嘗試初始化 Vision Client (會自動尋找環境中的憑證)
    vision_client = vision.ImageAnnotatorClient()
    logger.info("Google Cloud Vision client initialized successfully.")
except ImportError:
    logger.error("Google Cloud Vision library not found. OCR functionality will be disabled. (pip install google-cloud-vision)")
except Exception as e:
    logger.error(f"Google Cloud Vision client failed to initialize (Check authentication/credentials): {e}")

# === parse signals pool ===
def load_signals_pool():
    if SIGNALS_POOL_ENV:
        pool = []
        for item in SIGNALS_POOL_ENV.split(','):
            if ':' in item:
                name, maxn = item.split(':', 1)
                try:
                    pool.append((name.strip(), int(maxn)))
                except Exception:
                    continue
        if pool:
            return pool
    # default
    return [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("聖甲蟲", 3)
    ]

SIGNALS_POOL = load_signals_pool()

# === check required envs presence early ===
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

# === ephemeral store for latest generated signals per user ===
LATEST_SIGNALS = {}

# === Supabase helper functions (with try/except logging) ===
# (Function definitions remain the same)
def get_member(line_user_id):
    try:
        res = supabase.table("members").select("*").eq("line_user_id", line_user_id).maybe_single().execute()
        return res.data if res and res.data else None
    except Exception:
        logger.exception("[get_member error]")
        return None

def add_member(line_user_id, code="SET2024"):
    try:
        res = supabase.table("members").insert({
            "line_user_id": line_user_id,
            "status": "pending",
            "code": code
        }).execute()
        return res.data
    except Exception:
        logger.exception("[add_member error]")
        return None

def get_usage_today(line_user_id):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_user_id).eq("used_at", today).maybe_single().execute()
        return res.data["used_count"] if res and res.data and "used_count" in res.data else 0
    except Exception:
        logger.exception("[get_usage_today error]")
        return 0

def increment_usage(line_user_id):
    today = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        used = get_usage_today(line_user_id)
        if used == 0:
            supabase.table("usage_logs").insert({
                "line_user_id": line_user_id,
                "used_at": today,
                "used_count": 1
            }).execute()
        else:
            supabase.table("usage_logs").update({
                "used_count": used + 1
            }).eq("line_user_id", line_user_id).eq("used_at", today).execute()
    except Exception:
        logger.exception("[increment_usage error]")

def get_previous_reply(line_user_id, msg_hash):
    try:
        res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
        return res.data["reply"] if res and res.data and "reply" in res.data else None
    except Exception:
        logger.exception("[get_previous_reply error]")
        return None

def save_analysis_log(line_user_id, msg_hash, reply):
    """
    儲存分析結果到 Supabase，加上分析時間 analyzed_at (台北時區 UTC+8)
    """
    try:
        tz = timezone(timedelta(hours=8))
        analyzed_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        supabase.table("analysis_logs").insert({
            "line_user_id": line_user_id,
            "msg_hash": msg_hash,
            "reply": reply,
            "analyzed_at": analyzed_at
        }).execute()
    except Exception:
        logger.exception("[save_analysis_log error]")

def save_signal_stats(signals):
    """
    signals: list of combos (each combo is list of (name, qty))
    """
    try:
        if not signals:
            return
        flat = []
        if all(isinstance(x, tuple) and len(x) == 2 for x in signals):
            flat = signals
        else:
            for group in signals:
                for s, qty in group:
                    flat.append((s, qty))
        for s, qty in flat:
            supabase.table("signal_stats").insert({
                "signal_name": s,
                "quantity": qty
            }).execute()
    except Exception:
        logger.exception("[save_signal_stats error]")

def update_member_preference(line_user_id, strategy):
    try:
        supabase.table("member_preferences").upsert({
            "line_user_id": line_user_id,
            "preferred_strategy": strategy
        }, on_conflict=["line_user_id"]).execute()
    except Exception:
        logger.exception("[update_member_preference error]")
        
# === NEW: OCR Extraction Function ===
def ocr_and_extract_data(message_id, line_bot_api):
    """
    從 LINE 下載圖片，使用 Google Cloud Vision 執行 OCR，並提取所需的數字。
    返回 (text_for_analysis, error_msg)
    """
    if not vision_client:
        return None, "❌ 圖片分析服務未啟用或缺少 Google Cloud Vision 函式庫/憑證。"
        
    try:
        # 1. 下載圖片內容 (以 bytes 格式)
        message_content = line_bot_api.get_message_content(message_id=message_id)
        image_bytes = message_content.content
        
        # 2. 執行 OCR
        image = vision.Image(content=image_bytes)
        # 使用 DOCUMENT_TEXT_DETECTION 以獲得更好的文本結構和準確度
        response = vision_client.document_text_detection(image=image)
        
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        if not full_text:
            return None, "❌ 圖片辨識失敗，未偵測到任何文字，請確認圖片清晰度。"
            
        logger.info(f"[OCR_RESULT] Full Text (First 300 chars): \n{full_text[:300]}...")

        # 3. 提取數據 (關鍵字匹配與數字提取)
        
        # 簡單的提取邏輯：尋找關鍵字，並從包含關鍵字的行中提取數字
        def simple_extract_value(text, keywords):
            text_lines = text.split('\n')
            
            for line in text_lines:
                # 嘗試將中文冒號替換為英文冒號，增加匹配率
                line = line.replace('：', ':')
                
                for keyword in keywords:
                    if keyword in line:
                        # 查找所有可能的數字串（可以包含逗號作為千分位分隔符）
                        # 尋找關鍵字後面的數字，且數字可能與關鍵字之間有空格或符號
                        # pattern: 關鍵字 + 任意非數字/非逗號/非換行符 + 數字串
                        nums = re.findall(r'(\d{1,3}(?:,\d{3})*)', line) 
                        if nums:
                            # 取最後一個被找到的數字作為結果 (通常是數值)
                            return nums[-1].replace(',', '')
            return None

        # 由於 OCR 結果中 "未開轉數" 可能被辨識為 "轉數" 或其他
        val_not_open = simple_extract_value(full_text, ["未開轉數", "轉數", "未開", "SpinLeft"])
        # RTP 相關
        val_rtp = simple_extract_value(full_text, ["今日RTP", "RTP%"])
        # 下注額相關
        val_bets = simple_extract_value(full_text, ["今日總下注額", "總下注", "TotalBet"])
        
        extracted_data = {
            "未開轉數": val_not_open,
            "今日RTP%數": val_rtp,
            "今日總下注額": val_bets
        }
        
        # 4. 檢查並格式化
        missing_fields = [k for k, v in extracted_data.items() if not v or not v.isdigit()]
        if missing_fields:
             return None, f"❌ 圖片辨識結果不完整或格式錯誤，無法提取以下純數字資訊：{', '.join(missing_fields)}。\n請傳送更清晰的圖片或手動輸入。"

        text_for_analysis = (
            f"未開轉數 : {extracted_data['未開轉數']}\n"
            f"今日RTP%數 : {extracted_data['今日RTP%數']}\n"
            f"今日總下注額 : {extracted_data['今日總下注額']}"
        )
        return text_for_analysis, None
        
    except Exception:
        logger.exception("[OCR_ERROR] 圖片處理失敗")
        return None, "❌ 圖片處理失敗，請確認圖片清晰度、檔案大小或 LINE API 存取權限。"

# === Fake analysis function (parses 3 fields, returns risk + 2 combos) ===
# (Function definition remains the same)
def fake_human_like_reply(msg, line_user_id):
    """
    Parse only:
      - 未開轉數
      - 今日RTP%數
      - 今日總下注額
    Produce two signal combos (組合 A / B) and risk analysis.
    """
    # parse lines into dict
    lines = {}
    for raw in msg.split('\n'):
        if ':' in raw:
            k, v = raw.split(':', 1)
            lines[k.strip()] = v.strip()

    try:
        # 確保數字是純淨的整數
        not_open = int(re.sub(r'\D', '', lines.get("未開轉數", "0")))
        rtp_today = int(re.sub(r'\D', '', lines.get("今日RTP%數", "0")))
        bets_today = int(re.sub(r'\D', '', lines.get("今日總下注額", "0")))
    except Exception:
        return "❌ 分析失敗，請確認輸入格式及數值正確（整數、無小數點或符號）。\n\n範例：\n未開轉數 : 120\n今日RTP%數 : 105\n今日總下注額 : 45000"

    # generate two combos (each 2~3 signals, total qty <= 12)
    all_combos = []
    for _ in range(2):
        attempts = 0
        while True:
            attempts += 1
            chosen = random.sample(SIGNALS_POOL, k=random.choice([2, 3]))
            combo = [(s[0], random.randint(1, s[1])) for s in chosen]
            if sum(q for _, q in combo) <= 12:
                all_combos.append(combo)
                break
            if attempts > 30:
                # fallback
                all_combos.append([(s[0], 1) for s in chosen])
                break

    # save ephemeral
    LATEST_SIGNALS[line_user_id] = {
        "combos": all_combos,
        "generated_at": datetime.utcnow().isoformat()
    }

    # auto-save if enabled
    if AUTO_SAVE_SIGNALS:
        try:
            save_signal_stats(all_combos)
        except Exception:
            pass

    # sums and labeling
    sums = [sum(q for _, q in combo) for combo in all_combos]
    labels = ["組合 A", "組合 B"]
    combo_texts = []
    for idx, combo in enumerate(all_combos):
        lines_combo = '\n'.join([f"{s}：{q}顆" for s, q in combo])
        combo_texts.append((labels[idx], lines_combo, sums[idx]))

    if sums[0] > sums[1]:
        priority = "組合 A 優先（顆數較多）"
    elif sums[1] > sums[0]:
        priority = "組合 B 優先（顆數較多）"
    else:
        priority = "兩組同等優先（顆數相同）"

    # risk scoring (env thresholds)
    risk_score = 0
    # not_open
    if not_open > NOT_OPEN_HIGH:
        risk_score += 2
    elif not_open > NOT_OPEN_MED:
        risk_score += 1
    elif not_open < NOT_OPEN_LOW:
        risk_score -= 1
    # rtp
    if rtp_today > RTP_HIGH:
        risk_score += 2
    elif rtp_today > RTP_MED:
        risk_score += 1
    elif rtp_today < RTP_LOW:
        risk_score -= 1
    # bets
    if bets_today >= BETS_HIGH:
        risk_score -= 1
    elif bets_today < BETS_LOW:
        risk_score += 1

    # classify
    if risk_score >= 3:
        risk_level = "🚨 高風險"
        strategy = "建議僅觀察，暫不進場。"
        advice = "風險偏高，可能已爆分或被吃分過。"
    elif risk_score >= 1:
        risk_level = "⚠️ 中風險"
        strategy = "可小額觀察，視情況再加注。"
        advice = "回分條件一般，適合保守打法。"
    else:
        risk_level = "✅ 低風險"
        strategy = "建議可進場觀察，適合穩定操作。"
        advice = "房間數據良好，可考慮逐步提高注額。"

    # save member preference (non-critical)
    try:
        update_member_preference(line_user_id, strategy)
    except Exception:
        pass

    # build text
    formatted_signals = []
    for label, body_text, total in combo_texts:
        formatted_signals.append(f"{label}（總顆數：{total}）:\n{body_text}")
    signals_block = "\n\n".join(formatted_signals)

    return (
        f"📊 房間分析結果如下：\n"
        f"風險等級：{risk_level}\n"
        f"建議策略：{strategy}\n"
        f"說明：{advice}\n\n"
        f"🔎 推薦訊號（兩組）：\n{signals_block}\n\n"
        f"➡️ 優先建議：{priority}\n\n"
        f"若滿意此組合並想儲存，請傳送「儲存訊號」。\n"
        f"管理員可傳送「管理員儲存訊號」強制儲存（需 ADMIN_LINE_ID）。\n"
        f"✨ 若需進一步打法策略，請聯絡阿東超人：LINE ID adong8989"
    )

# === quick reply builder ===
def build_quick_reply():
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

# === health endpoint for quick checks ===
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "env": {name: bool(os.getenv(name)) for name in required_envs},
        "auto_save_signals": AUTO_SAVE_SIGNALS,
        "ocr_enabled": vision_client is not None
    }), 200

# === webhook callback with improved logging ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info(f"Received /callback - signature present: {bool(signature)}, body length: {len(body)}")
    try:
        handler.handle(body, signature)
    except Exception:
        logger.exception("Webhook handler error with body (truncated):\n%s", body[:1000])
        abort(400)
    return "OK", 200

@handler.add(MessageEvent)
def handle_message(event):
    user_id = getattr(event.source, "user_id", "unknown")
    
    # 統一用來進行分析的文字輸入，可以來自文字訊息或 OCR 結果
    msg_for_analysis = ""
    msg_hash = ""
    reply = ""

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        member_data = get_member(user_id)

        # 1. 處理訊息類型 (文字或圖片)
        if event.message.type == "text":
            msg = event.message.text.strip()
            msg_for_analysis = msg
            msg_hash = hashlib.sha256(msg_for_analysis.encode("utf-8")).hexdigest()
        
        elif event.message.type == "image":
            logger.info(f"[DEBUG] user_id: {user_id}, received image message.")
            
            # 執行 OCR 和數據提取
            text_for_analysis, error_msg = ocr_and_extract_data(event.message.id, line_bot_api)
            
            if error_msg:
                reply = error_msg
                # 圖片處理失敗，跳過分析流程
            elif text_for_analysis:
                msg_for_analysis = text_for_analysis
                # 為 OCR 提取的內容生成 Hash
                msg_hash = hashlib.sha256(msg_for_analysis.encode("utf-8")).hexdigest()
                logger.info(f"[DEBUG] OCR extracted text:\n{msg_for_analysis}")
            
        else:
            # 非文字和非圖片訊息，不做處理
            reply = "目前只支援文字或圖片的房間資訊分析。"
        
        logger.info(f"[DEBUG] user_id: {user_id}, msg_hash: {msg_hash}, msg_type: {event.message.type}, msg_len: {len(msg_for_analysis)}")
        
        # 2. 處理固定指令 (僅對文字訊息執行)
        # 為了避免 OCR 提取的文本意外觸發指令，這裡只檢查原始的文字訊息 (如果 msg_hash 是基於原始文字)
        # 由於原始程式碼將所有指令放在 elif 結構中，我們需要確保它們在分析流程之前被檢查。
        
        if event.message.type == "text":
            msg = event.message.text.strip()
            if msg == "我要開通":
                if member_data:
                    if member_data.get("status") == "approved":
                        reply = "✅ 您已開通完成，歡迎使用選房分析功能。"
                    else:
                        reply = f"你已申請過囉，請找管理員審核 LINE ID :adong8989。\n目前狀態：{member_data.get('status')}"
                else:
                    add_member(user_id)
                    reply = f"申請成功！請加管理員 LINE:adong8989 並提供此 user_id：{user_id}"

            elif msg == "房間資訊表格":
                reply = (
                    "未開轉數 :\n"
                    "今日RTP%數 :\n"
                    "今日總下注額 :"
                )

            elif msg == "使用說明":
                reply = (
                    "📘 使用說明：\n"
                    "請依下列格式輸入 RTP 資訊：\n\n"
                    "未開轉數 :\n"
                    "今日RTP%數 :\n"
                    "今日總下注額 :\n\n"
                    "⚠️ 注意事項：\n"
                    "1️⃣ 所有數值請填整數（無小數點或 % 符號）\n"
                    "2️⃣ 分析結果分為高 / 中 / 低風險\n"
                    "3️⃣ 每日使用次數：normal 15 次，vip 50 次\n"
                    "4️⃣ 若要儲存剛剛系統產生的訊號，請傳「儲存訊號」；管理員可用「管理員儲存訊號」"
                )

            # Save signals (user-initiated)
            elif msg == "儲存訊號":
                latest = LATEST_SIGNALS.get(user_id)
                if not latest:
                    reply = "找不到最近產生的訊號，請先送出房間資訊以產生推薦訊號，再傳「儲存訊號」。"
                else:
                    try:
                        save_signal_stats(latest["combos"])
                        del LATEST_SIGNALS[user_id]
                        reply = "✅ 已儲存剛剛的推薦訊號到資料庫。"
                    except Exception:
                        reply = "❌ 儲存失敗，請稍後再試。"

            # Admin force save
            elif msg == "管理員儲存訊號":
                if ADMIN_LINE_ID and user_id == ADMIN_LINE_ID:
                    saved_count = 0
                    for uid, data in list(LATEST_SIGNALS.items()):
                        try:
                            save_signal_stats(data["combos"])
                            saved_count += 1
                            del LATEST_SIGNALS[uid]
                        except Exception:
                            logger.exception("[admin save_signal_stats error]")
                    reply = f"管理員操作完成，已嘗試儲存 {saved_count} 位使用者的推薦訊號。"
                else:
                    reply = "❌ 你不是管理員，無法執行此操作。"
            
        # 3. 處理分析流程 (對 OCR 成功的圖片和文字 RTP 訊息都適用)
        if msg_for_analysis and not reply and ("RTP" in msg_for_analysis or "轉" in msg_for_analysis or "注額" in msg_for_analysis):
            
            prev = get_previous_reply(user_id, msg_hash)
            if prev:
                # 已經分析過: 回傳舊結果，不扣除額度
                reply = f"此資料已分析過（避免重複分析）：\n\n{prev}"
            else:
                # 檢查使用額度
                level = member_data.get("member_level", "normal") if member_data else "normal"
                limit = 50 if level == "vip" else 15
                used = get_usage_today(user_id)

                if used >= limit:
                    reply = f"⚠️ 今日已達使用上限（{limit}次），請明日再試或升級 VIP。"
                else:
                    # 檢查會員狀態
                    if not member_data:
                        reply = "⚠️ 尚未開通會員資格，請先傳送「我要開通」申請使用分析功能。"
                    elif member_data.get("status") != "approved":
                        current_status = member_data.get("status", "pending")
                        reply = f"⚠️ 您的會員尚未通過審核（目前狀態：{current_status}）。\n請加管理員 LINE: adong8989 申請開通。"
                    else:
                        # 執行分析
                        reply = fake_human_like_reply(msg_for_analysis, user_id)
                        save_analysis_log(user_id, msg_hash, reply)
                        increment_usage(user_id)
                        used_after = get_usage_today(user_id)
                        reply += f"\n\n✅ 分析完成（今日剩餘 {limit - used_after} / {limit} 次）"

        # 4. 處理無法識別的訊息
        if not reply:
            reply = "請傳送房間資訊或使用下方快速選單進行操作。"

        # reply to user
        try:
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
            ))
        except Exception:
            logger.exception("[reply_message error]")

# === run server ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # In production use a WSGI server (gunicorn). debug=True only for local dev.
    app.run(host="0.0.0.0", port=port, debug=True)
