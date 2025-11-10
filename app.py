# app.py - LINE Bot RTP 分析器
# 整合功能：LINE Webhook 處理、Supabase 資料庫、Google Cloud Vision OCR、環境變數憑證安全處理

from flask import Flask, request, abort, jsonify
import os
import logging
import io
import re # 用於 OCR 文字提取
import json # 用於處理 JSON 字串
import tempfile # 用於創建臨時文件，確保憑證寫入安全
from dotenv import load_dotenv
from supabase import create_client
# 修正後的導入：移除了不存在的 ImageMessage
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest, QuickReply, QuickReplyItem, MessageAction, URIAction
from datetime import datetime, timezone, timedelta
import hashlib
import random
from google.api_core import exceptions as gcp_exceptions # 導入 GCP 異常處理

# === 載入環境變數與基礎設定 ===
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "")  # 管理員 Line ID
AUTO_SAVE_SIGNALS = os.getenv("AUTO_SAVE_SIGNALS", "false").lower() in ("1", "true", "yes") # 是否自動儲存訊號
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON") # Google Service Account JSON 字串

# 訊號池環境變數 (格式: 名稱:上限,名稱:上限,...)
SIGNALS_POOL_ENV = os.getenv("SIGNALS_POOL", "")

# 風險評估門檻 (環境變數或預設值)
NOT_OPEN_HIGH = int(os.getenv("NOT_OPEN_HIGH", 250))
NOT_OPEN_MED = int(os.getenv("NOT_OPEN_MED", 150))
NOT_OPEN_LOW = int(os.getenv("NOT_OPEN_LOW", 50))
RTP_HIGH = int(os.getenv("RTP_HIGH", 120))
RTP_MED = int(os.getenv("RTP_MED", 110))
RTP_LOW = int(os.getenv("RTP_LOW", 90))
BETS_HIGH = int(os.getenv("BETS_HIGH", 80000))
BETS_LOW = int(os.getenv("BETS_LOW", 30000))

# 設置基礎日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
required_envs = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"]

# === Google Cloud Vision Client 初始化與憑證設定 ===
vision_client = None
# 使用臨時文件來安全地處理 JSON 憑證字串
VISION_CREDENTIALS_FILE = None # 稍後會被設定為臨時檔案路徑

if GCP_SA_KEY_JSON:
    try:
        # 1. 確保 JSON 格式正確
        json.loads(GCP_SA_KEY_JSON)
        # 2. 創建一個臨時文件，並將 JSON 字串寫入，供 Google 函式庫讀取
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            VISION_CREDENTIALS_FILE = tmp_file.name
        
        # 3. 設定 GOOGLE_APPLICATION_CREDENTIALS 環境變數指向此臨時文件
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = VISION_CREDENTIALS_FILE
        logger.info(f"GCP credentials set up successfully using temporary file: {VISION_CREDENTIALS_FILE}")
        
        # 確保 Google Cloud Vision 函式庫已經安裝
        from google.cloud import vision
        # 嘗試初始化 Vision Client (現在它會使用上面設定的環境變數)
        vision_client = vision.ImageAnnotatorClient()
        logger.info("Google Cloud Vision 客戶端初始化成功。")

    except ImportError:
        logger.error("❌ 缺少 Google Cloud Vision 函式庫 (pip install google-cloud-vision)。圖片分析功能將被禁用。")
    except json.JSONDecodeError:
        logger.error("❌ GCP_SA_KEY_JSON 環境變數不是有效的 JSON 格式。")
    except Exception as e:
        logger.error(f"❌ Google Cloud Vision 客戶端初始化失敗 (請檢查身份驗證/憑證): {e}")
else:
    logger.warning("⚠️ GCP_SA_KEY_JSON 環境變數未找到。圖片分析功能將無法使用。")


# === 解析訊號池 ===
def load_signals_pool():
    """從環境變數載入訊號池設定，或使用預設值"""
    if SIGNALS_POOL_ENV:
        pool = []
        for item in SIGNALS_POOL_ENV.split(','):
            if ':' in item:
                name, maxn = item.split(':', 1)
                try:
                    pool.append((name.strip(), int(maxn)))
                except ValueError:
                    continue # 忽略格式錯誤的項目
        if pool:
            return pool
    # 預設訊號池
    return [
        ("眼睛", 7), ("刀子", 7), ("弓箭", 7), ("蛇", 7),
        ("紅寶石", 7), ("藍寶石", 7), ("黃寶石", 7), ("綠寶石", 7), ("紫寶石", 7),
        ("聖甲蟲", 3)
    ]

SIGNALS_POOL = load_signals_pool()

# === 初始化服務 ===
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL 或 KEY 尚未正確設定")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

# === 用於儲存最新生成訊號的臨時記憶體 (Ephemeral Store) ===
LATEST_SIGNALS = {}

# === Supabase 輔助函數 (為簡潔程式碼，將所有資料庫操作包裝於此) ===

def get_member(line_user_id):
    """查詢會員資料"""
    try:
        res = supabase.table("members").select("*").eq("line_user_id", line_user_id).maybe_single().execute()
        return res.data if res and res.data else None
    except Exception:
        logger.exception("[get_member error]")
        return None

def add_member(line_user_id, code="SET2024"):
    """新增會員申請記錄"""
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
    """取得今日使用次數 (使用 UTC+8 台北時區)"""
    # 設置時區為 UTC+8 (台北時間)
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime('%Y-%m-%d')
    try:
        res = supabase.table("usage_logs").select("used_count").eq("line_user_id", line_user_id).eq("used_at", today).maybe_single().execute()
        return res.data["used_count"] if res and res.data and "used_count" in res.data else 0
    except Exception:
        logger.exception("[get_usage_today error]")
        return 0

def increment_usage(line_user_id):
    """增加今日使用次數 (使用 UTC+8 台北時區)"""
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime('%Y-%m-%d')
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
    """檢查是否已分析過此資料"""
    try:
        res = supabase.table("analysis_logs").select("reply").eq("line_user_id", line_user_id).eq("msg_hash", msg_hash).maybe_single().execute()
        return res.data["reply"] if res and res.data and "reply" in res.data else None
    except Exception:
        logger.exception("[get_previous_reply error]")
        return None

def save_analysis_log(line_user_id, msg_hash, reply):
    """儲存分析結果 (台北時區 UTC+8)"""
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
    """儲存訊號統計資料"""
    try:
        if not signals:
            return
        # 將多層次的 signals 組合攤平
        flat = []
        # 檢查 signals 結構是否是 [(s1, qty1), (s2, qty2)]
        if all(isinstance(x, tuple) and len(x) == 2 for x in signals):
            flat = signals
        else:
            # 假設 signals 結構是 [[(s1, qty1), ...], [(sA, qtyA), ...]]
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
    """更新會員偏好策略 (非關鍵功能)"""
    try:
        supabase.table("member_preferences").upsert({
            "line_user_id": line_user_id,
            "preferred_strategy": strategy
        }, on_conflict=["line_user_id"]).execute()
    except Exception:
        logger.exception("[update_member_preference error]")
        
# === OCR 提取函數 (已優化，解決浮點數和上下文問題) ===
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
        # 修正: 使用 .read() 方法來獲取串流物件中的所有位元組資料，避免 AttributeError
        image_bytes = message_content.read() 
        
    except Exception as e:
        logger.error(f"❌ LINE 圖片下載失敗: {e}")
        return None, f"❌ 圖片下載失敗，請檢查 LINE 憑證和存取權限。詳細錯誤: {e.__class__.__name__}"
        
    try:
        # 2. 執行 OCR
        image = vision.Image(content=image_bytes)
        # 使用 DOCUMENT_TEXT_DETECTION 以獲得更好的文本結構和準確度
        response = vision_client.document_text_detection(image=image)
        
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        if not full_text:
            return None, "❌ 圖片辨識失敗，未偵測到任何文字，請確認圖片清晰度。"
            
        logger.info(f"[OCR_RESULT] Full Text (First 300 chars): \n{full_text[:300]}...")

        # 3. 優化提取數據 (鎖定今日數據，處理浮點數)
        
        # 浮點數/整數匹配模式: 匹配數字、逗號、可選的小數點及其後數字
        # FLOAT_PATTERN = r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?)' 
        # 修正: 確保能匹配所有有效的數字，包括純整數、帶小數點、帶逗號的數字
        FLOAT_PATTERN = r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)' 

        # 1. 提取未開轉數 (在左上角，優先匹配 '未開' 和 '轉')
        # 圖片中 '未開0轉' 結構清晰，且為整數
        match_not_open = re.search(r'(未開)\s*(\d+)\s*轉', full_text)
        val_not_open = match_not_open.group(2) if match_not_open else None
        
        # 2. 提取今日總下注額 和 今日得分率/RTP%
        # 使用非貪婪匹配 `.*?` 來尋找最近的數值
        
        # 提取所有 '總下注額' 數值
        all_bets = re.findall(r'(總下注額|總下注|TotalBet).*?' + FLOAT_PATTERN, full_text, re.DOTALL | re.IGNORECASE)
        # 提取所有 '得分率' 或 'RTP' 數值
        all_rtp = re.findall(r'(得分率|RTP%).*?' + FLOAT_PATTERN, full_text, re.DOTALL | re.IGNORECASE)

        # 假設第一個匹配到的就是 '今日' 的數值 
        val_bets = all_bets[0][1].replace(',', '') if all_bets else None
        val_rtp = all_rtp[0][1].replace(',', '') if all_rtp else None

        # 如果提取到多組，可以嘗試通過上下文鎖定 "今日" 的數值，但對於您的圖片結構，取第一個通常是正確的。

        extracted_data = {
            "未開轉數": val_not_open,
            "今日RTP%數": val_rtp,
            "今日總下注額": val_bets
        }
        
        # 4. 檢查並格式化
        # 檢查 Not Open: 必須是純數字 (整數)
        if not val_not_open or not val_not_open.isdigit():
             # 使用一個通用錯誤碼，讓用戶知道哪個欄位失敗
             return None, f"❌ 辨識結果不完整或格式錯誤：無法提取「未開轉數」的純數字（OCR 提取: {val_not_open}）。"

        # 檢查 Bets: 必須是非空且是有效數字 (浮點數)
        try:
            float(val_bets)
        except (ValueError, TypeError):
             return None, f"❌ 辨識結果不完整或格式錯誤：無法提取「今日總下注額」的數字（OCR 提取: {val_bets}）。"
        
        # 檢查 RTP: 必須是非空且是有效數字 (浮點數)
        try:
            float(val_rtp)
        except (ValueError, TypeError):
             return None, f"❌ 辨識結果不完整或格式錯誤：無法提取「今日RTP%數」的數字（OCR 提取: {val_rtp}）。"

        # 格式化輸出，去除小數點後的無用零位
        val_bets_clean = f"{float(val_bets):.2f}".rstrip('0').rstrip('.')
        val_rtp_clean = f"{float(val_rtp):.2f}".rstrip('0').rstrip('.')
        
        text_for_analysis = (
            f"未開轉數 : {val_not_open}\n"
            f"今日RTP%數 : {val_rtp_clean}\n"
            f"今日總下注額 : {val_bets_clean}"
        )
        return text_for_analysis, None
        
    except gcp_exceptions.PermissionDenied as e:
        logger.error(f"❌ Google Cloud 權限被拒: {e}")
        return None, "❌ Google Cloud Vision API 權限被拒。請檢查 GCP 服務帳號權限或 API 是否啟用。"
    except gcp_exceptions.InvalidArgument as e:
        logger.error(f"❌ Google Cloud: 無效的圖片格式/內容: {e}")
        return None, "❌ Google Vision: 無效的圖片格式或內容。請確認圖片大小不超過 4MB。"
    except Exception:
        logger.exception("[OCR_ERROR] 圖片處理失敗")
        return None, "❌ 圖片處理失敗，可能是 OCR 伺服器錯誤或數據提取時的結構錯誤。請重試。"

# === 假人為分析函數 (生成風險分析與推薦訊號) ===
def fake_human_like_reply(msg, line_user_id):
    """
    解析輸入文字，進行風險評估，並產生兩組隨機訊號組合。
    """
    # 解析輸入行到字典
    lines = {}
    for raw in msg.split('\n'):
        if ':' in raw:
            k, v = raw.split(':', 1)
            lines[k.strip()] = v.strip()

    try:
        # 清理數字並轉型
        # 未開轉數 (純整數)
        not_open = int(re.sub(r'[^\d]', '', lines.get("未開轉數", "0")))
        # RTP (浮點數，分析時取整數部分)
        rtp_str = re.sub(r'[^\d\.]', '', lines.get("今日RTP%數", "0")).split('.')[0] # 僅取整數部分進行風險評估
        rtp_today = int(rtp_str)
        # 總下注額 (浮點數，分析時取整數部分)
        bets_str = re.sub(r'[^\d\.]', '', lines.get("今日總下注額", "0")).split('.')[0] # 僅取整數部分進行風險評估
        bets_today = int(bets_str)
        
    except Exception:
        return "❌ 分析失敗，請確認輸入格式及數值正確。\n\n範例：\n未開轉數 : 120\n今日RTP%數 : 105.38\n今日總下注額 : 45000.55"

    # 生成兩組訊號組合
    all_combos = []
    for _ in range(2):
        attempts = 0
        while True:
            attempts += 1
            # 隨機選擇 2 到 3 個訊號
            chosen = random.sample(SIGNALS_POOL, k=random.choice([2, 3]))
            # 為每個訊號分配隨機數量 (在上限範圍內)
            combo = [(s[0], random.randint(1, s[1])) for s in chosen]
            # 確保總顆數不超過 12
            if sum(q for _, q in combo) <= 12:
                all_combos.append(combo)
                break
            if attempts > 30: # 防止無限循環
                all_combos.append([(s[0], 1) for s in chosen])
                break

    # 儲存到臨時記憶體 (用於後續的「儲存訊號」指令)
    LATEST_SIGNALS[line_user_id] = {
        "combos": all_combos,
        "generated_at": datetime.utcnow().isoformat()
    }

    # 如果自動儲存開啟，則寫入資料庫
    if AUTO_SAVE_SIGNALS:
        try:
            # save_signal_stats 接收的是多層次的 all_combos
            save_signal_stats(all_combos)
        except Exception:
            pass

    # 格式化訊號組合
    sums = [sum(q for _, q in combo) for combo in all_combos]
    labels = ["組合 A", "組合 B"]
    combo_texts = []
    for idx, combo in enumerate(all_combos):
        lines_combo = '\n'.join([f"{s}：{q}顆" for s, q in combo])
        combo_texts.append((labels[idx], lines_combo, sums[idx]))

    # 判斷優先順序
    priority = ""
    if sums[0] > sums[1]:
        priority = "組合 A 優先（顆數較多）"
    elif sums[1] > sums[0]:
        priority = "組合 B 優先（顆數較多）"
    else:
        priority = "兩組同等優先（顆數相同）"

    # 風險評估 (基於環境變數門檻)
    risk_score = 0
    if not_open > NOT_OPEN_HIGH: risk_score += 2
    elif not_open > NOT_OPEN_MED: risk_score += 1
    elif not_open < NOT_OPEN_LOW: risk_score -= 1

    if rtp_today > RTP_HIGH: risk_score += 2
    elif rtp_today > RTP_MED: risk_score += 1
    elif rtp_today < RTP_LOW: risk_score -= 1

    if bets_today >= BETS_HIGH: risk_score -= 1
    elif bets_today < BETS_LOW: risk_score += 1

    # 分類風險等級與建議
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

    # 更新會員偏好
    try: update_member_preference(line_user_id, strategy)
    except Exception: pass

    # 組合最終回覆文本
    formatted_signals = []
    for label, body_text, total in combo_texts:
        formatted_signals.append(f"{label}（總顆數：{total}）：\n{body_text}")
    signals_block = "\n\n".join(formatted_signals)

    return (
        f"📊 房間分析結果如下：\n"
        f"風險等級：{risk_level}\n"
        f"建議策略：{strategy}\n"
        f"說明：{advice}\n\n"
        f"🔎 推薦訊號（兩組）：\n{signals_block}\n\n"
        f"➡️ 優先建議：{priority}\n\n"
        f"若滿意此組合並想儲存，請傳送「儲存訊號」。\n"
        f"✨ 若需進一步打法策略，請聯絡阿東超人：LINE ID adong8989"
    )

# === 快速回覆按鈕 ===
def build_quick_reply():
    """創建包含常用指令的快速回覆選單"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🔓 我要開通", text="我要開通")),
        QuickReplyItem(action=URIAction(label="🧠 註冊按我", uri="https://wek002.welove777.com")),
        QuickReplyItem(action=MessageAction(label="📘 使用說明", text="使用說明")),
        QuickReplyItem(action=MessageAction(label="📋 房間資訊表格", text="房間資訊表格"))
    ])

# === Health Check 端點 ===
@app.route("/health", methods=["GET"])
def health():
    """用於檢查服務運行狀態與環境變數設定"""
    return jsonify({
        "status": "ok",
        "env_set": {name: bool(os.getenv(name)) for name in required_envs},
        "auto_save_signals": AUTO_SAVE_SIGNALS,
        "ocr_enabled": vision_client is not None,
        "vision_cred_path": VISION_CREDENTIALS_FILE
    }), 200

# === LINE Webhook 處理 ===
@app.route("/callback", methods=["POST"])
def callback():
    """接收來自 LINE 平台的訊息與事件"""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info(f"Received /callback - signature present: {bool(signature)}, body length: {len(body)}")
    try:
        handler.handle(body, signature)
    except Exception:
        logger.exception("Webhook 處理錯誤 (訊息已截斷):\n%s", body[:1000])
        abort(400)
    return "OK", 200

@handler.add(MessageEvent)
def handle_message(event):
    """處理接收到的所有訊息事件"""
    user_id = getattr(event.source, "user_id", "unknown")
    
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
            logger.info(f"[DEBUG] user_id: {user_id}, 收到圖片訊息。")
            
            # 執行 OCR 和數據提取
            text_for_analysis, error_msg = ocr_and_extract_data(event.message.id, line_bot_api)
            
            if error_msg:
                reply = error_msg
            elif text_for_analysis:
                msg_for_analysis = text_for_analysis
                # 為 OCR 提取的內容生成 Hash
                msg_hash = hashlib.sha256(msg_for_analysis.encode("utf-8")).hexdigest()
                logger.info(f"[DEBUG] OCR 提取文字:\n{msg_for_analysis}")
            
        else:
            reply = "目前只支援文字或圖片的房間資訊分析。"
        
        # 2. 處理固定指令 (僅對原始文字訊息執行，避免 OCR 錯誤觸發)
        # 檢查原始訊息是否為文字類型
        if event.message.type == "text":
            msg = event.message.text.strip()

            if msg == "我要開通":
                if member_data:
                    if member_data.get("status") == "approved":
                        reply = "✅ 您已開通完成，歡迎使用選房分析功能。"
                    else:
                        reply = f"你已申請過囉，請找管理員審核 LINE ID :adong8989。\n目前狀態：{member_data.get('status')}，您的 LINE User ID：{user_id}"
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
                    "請依下列格式輸入 RTP 資訊（可直接傳送包含這些資訊的圖片）：\n\n"
                    "未開轉數 :\n"
                    "今日RTP%數 :\n"
                    "今日總下注額 :\n\n"
                    "⚠️ 注意事項：\n"
                    "1️⃣ 所有數值請填整數（無小數点或 % 符號）\n"
                    "2️⃣ 分析結果分為高 / 中 / 低風險\n"
                    "3️⃣ 每日使用次數：normal 15 次，vip 50 次\n"
                    "4️⃣ 若要儲存剛剛系統產生的訊號，請傳「儲存訊號」\n"
                    "5️⃣ 圖片分析功能已開啟，可直接傳送遊戲畫面。"
                )

            # 儲存訊號 (用戶發起)
            elif msg == "儲存訊號":
                latest = LATEST_SIGNALS.get(user_id)
                if not latest:
                    reply = "找不到最近產生的訊號，請先送出房間資訊以產生推薦訊號，再傳「儲存訊號」。"
                else:
                    try:
                        # save_signal_stats 接收的是多層次的 all_combos
                        save_signal_stats(latest["combos"])
                        del LATEST_SIGNALS[user_id]
                        reply = "✅ 已儲存剛剛的推薦訊號到資料庫。"
                    except Exception:
                        reply = "❌ 儲存失敗，請稍後再試。"

            # 管理員強制儲存
            elif msg == "管理員儲存訊號":
                if ADMIN_LINE_ID and user_id == ADMIN_LINE_ID:
                    saved_count = 0
                    for uid, data in list(LATEST_SIGNALS.items()):
                        try:
                            # save_signal_stats 接收的是多層次的 all_combos
                            save_signal_stats(data["combos"])
                            saved_count += 1
                            del LATEST_SIGNALS[uid]
                        except Exception:
                            logger.exception("[admin save_signal_stats error]")
                    reply = f"管理員操作完成，已嘗試儲存 {saved_count} 位使用者的推薦訊號。"
                else:
                    reply = "❌ 你不是管理員，無法執行此操作。"
            
        # 3. 處理分析流程 (適用於 OCR 成功的圖片和文字 RTP 訊息)
        is_analysis_request = msg_for_analysis and ("RTP" in msg_for_analysis or "轉" in msg_for_analysis or "注額" in msg_for_analysis)
        
        if is_analysis_request and not reply: # 確保沒有被固定指令覆蓋，且是分析請求
            
            prev = get_previous_reply(user_id, msg_hash)
            if prev:
                # 已經分析過: 回傳舊結果，不扣除額度
                reply = f"此資料已分析過（避免重複分析）：\n\n{prev}"
            else:
                # 檢查使用額度與會員狀態
                level = member_data.get("member_level", "normal") if member_data and member_data.get("status") == "approved" else "normal"
                limit = 50 if level == "vip" else 15
                used = get_usage_today(user_id)

                if used >= limit:
                    reply = f"⚠️ 今日已達使用上限（{limit}次，您的級別是 {level}），請明日再試或升級 VIP。"
                elif not member_data or member_data.get("status") != "approved":
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

        # 回覆用戶
        try:
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
            ))
        except Exception:
            logger.exception("[reply_message error]")

# === 執行伺服器 ===
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 10000))
        # ⚠️ 注意: 在 Render 這類生產環境中，請使用 Gunicorn 或其他 WSGI 服務器運行此應用。
        app.run(host="0.0.0.0", port=port, debug=True)
    finally:
        # 清理臨時憑證文件 (如果已創建)
        if VISION_CREDENTIALS_FILE and os.path.exists(VISION_CREDENTIALS_FILE):
            try:
                os.remove(VISION_CREDENTIALS_FILE)
                logger.info(f"臨時憑證文件 {VISION_CREDENTIALS_FILE} 已清理。")
            except Exception as e:
                logger.error(f"無法清理臨時憑證文件: {e}")
