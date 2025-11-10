import os
import tempfile
import logging
import io
import re 
import json 
import hashlib
import random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from flask import Flask, request, abort, jsonify

# Supabase SDK
from supabase import create_client

# LINE SDK v3 
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ContentApi, # 正確的內容下載 API
    TextMessage,
    ReplyMessageRequest
)
from linebot.v3.messaging.models import (
    QuickReply, 
    QuickReplyItem, 
    MessageAction, 
    URIAction
)
from linebot.v3.messaging.exceptions import ApiException
from linebot.v3.exceptions import InvalidSignatureError

# Google Cloud Vision SDK
try:
    from google.cloud import vision
    from google.api_core import exceptions as gcp_exceptions 
except ImportError:
    vision = None
    gcp_exceptions = None
    print("WARNING: google-cloud-vision SDK not found. OCR functionality will be disabled.")


# === 載入環境變數與基礎設定 ===
load_dotenv()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_LINE_ID = os.getenv("ADMIN_LINE_ID", "") 
AUTO_SAVE_SIGNALS = os.getenv("AUTO_SAVE_SIGNALS", "false").lower() in ("1", "true", "yes") 
GCP_SA_KEY_JSON = os.getenv("GCP_SA_KEY_JSON") 

# 訊號池環境變數
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

# === Google Cloud Vision Client 初始化與憑證設定 ===
vision_client = None
VISION_CREDENTIALS_FILE = None 

if GCP_SA_KEY_JSON and vision:
    try:
        # 嘗試解析 JSON 並寫入臨時文件
        json.loads(GCP_SA_KEY_JSON)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_file:
            tmp_file.write(GCP_SA_KEY_JSON)
            VISION_CREDENTIALS_FILE = tmp_file.name
            
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = VISION_CREDENTIALS_FILE
        vision_client = vision.ImageAnnotatorClient()
        logger.info("✅ Google Cloud Vision 客戶端初始化成功。")

    except json.JSONDecodeError:
        logger.error("❌ GCP_SA_KEY_JSON 環境變數不是有效的 JSON 格式。")
    except Exception as e:
        logger.error(f"❌ Google Cloud Vision 客戶端初始化失敗 (請檢查身份驗證/憑證): {e}")
else:
    logger.warning("⚠️ 圖片分析服務未啟用 (缺少 GCP_SA_KEY_JSON 或 google-cloud-vision 函式庫)。")


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
                    continue
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
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("LINE Channel 憑證尚未正確設定")


configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

# === 用於儲存最新生成訊號的臨時記憶體 (Ephemeral Store) ===
LATEST_SIGNALS = {}

# === Supabase 輔助函數 ===

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
        flat = []
        if all(isinstance(x, tuple) and len(x) == 2 for x in signals):
            flat = signals
        else:
            for group in signals:
                if isinstance(group, list):
                    for s, qty in group:
                        flat.append((s, qty))
        
        # 避免插入空數據
        if flat:
            insert_data = []
            for s, qty in flat:
                insert_data.append({
                    "signal_name": str(s),
                    "quantity": int(qty)
                })
            supabase.table("signal_stats").insert(insert_data).execute()
            
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
        
# === OCR 提取函數 (使用 ContentApi) ===
def ocr_and_extract_data(message_id, line_content_api: ContentApi):
    """
    從 LINE 下載圖片，使用 Google Cloud Vision 執行 OCR，並提取所需的數字。
    返回 (text_for_analysis, error_msg)
    """
    if not vision_client:
        return None, "❌ 圖片分析服務未啟用或缺少 Google Cloud Vision 函式庫/憑證。"
        
    image_bytes = None
    
    try:
        # 1. 下載圖片內容 (使用 ContentApi 的 get_message_content)
        # ContentApi 的 get_message_content() 返回一個 context manager
        with line_content_api.get_message_content(message_id=message_id) as message_content:
            
            # 使用 read_chunk() 讀取內容流，確保處理大文件時不會 OOM
            image_stream = io.BytesIO()
            for chunk in message_content.read_chunk():
                image_stream.write(chunk)
            image_bytes = image_stream.getvalue()
        
        # 確認圖片位元組已獲取
        if not image_bytes:
            raise ValueError("獲取的圖片位元組為空，可能是下載失敗。")

    except ApiException as e:
        logger.error(f"❌ LINE API 錯誤 (ApiException): {e}")
        return None, f"❌ LINE API 錯誤 (ApiException)。請確認訊息 ID 是否仍在有效期內，或檢查 LINE Channel 憑證和權限。\n詳細錯誤: {e}"

    except Exception as e:
        logger.error(f"❌ 圖片下載或讀取失敗 (Exception): {e}")
        error_msg = f"❌ 圖片下載或讀取失敗。請檢查 LINE 憑證和存取權限。詳細錯誤: {e.__class__.__name__}。"
        return None, error_msg
        
    try:
        # 2. 執行 OCR
        image = vision.Image(content=image_bytes)
        response = vision_client.document_text_detection(image=image)
        
        full_text = response.full_text_annotation.text if response.full_text_annotation else ""
        
        if not full_text:
            return None, "❌ 圖片辨識失敗，未偵測到任何文字，請確認圖片清晰度。"
            
        logger.info(f"[OCR_RESULT] Full Text (First 300 chars): \n{full_text[:300]}...")

        # 3. 優化提取數據 (鎖定今日數據，處理浮點數)
        FLOAT_PATTERN = r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)'  

        # 尋找未開轉數 (數字前面是未開，後面是轉)
        match_not_open = re.search(r'(未開)[^0-9]*' + FLOAT_PATTERN + r'[^0-9]*轉', full_text, re.DOTALL)
        val_not_open_raw = match_not_open.group(1) if match_not_open and match_not_open.groups() and len(match_not_open.groups()) > 1 else None

        # 尋找總下注額 (包含 TotalBet 等關鍵字)
        all_bets = re.findall(r'(總下注額|總下注|TotalBet)[^0-9\.]*?' + FLOAT_PATTERN, full_text, re.DOTALL | re.IGNORECASE)
        # 尋找 RTP% (包含 得分率, RTP% 等關鍵字)
        all_rtp = re.findall(r'(得分率|RTP%|RTP)[^0-9\.]*?' + FLOAT_PATTERN, full_text, re.DOTALL | re.IGNORECASE)

        # 提取第一個匹配的數字，並移除千位分隔符
        val_not_open = re.sub(r'[^\d]', '', val_not_open_raw) if val_not_open_raw else None
        val_bets = all_bets[0][1].replace(',', '') if all_bets else None
        val_rtp = all_rtp[0][1].replace(',', '') if all_rtp else None

        # 4. 檢查並格式化
        if not val_not_open or not val_not_open.isdigit():
            return None, f"❌ 辨識結果不完整：無法提取「未開轉數」的純數字（OCR 提取: {val_not_open_raw}）。"

        try:
            float(val_bets)
        except (ValueError, TypeError):
            return None, f"❌ 辨識結果不完整：無法提取「今日總下注額」的數字（OCR 提取: {val_bets}）。"
            
        try:
            float(val_rtp)
        except (ValueError, TypeError):
            return None, f"❌ 辨識結果不完整：無法提取「今日RTP%數」的數字（OCR 提取: {val_rtp}）。"

        # 格式化輸出，移除小數點後多餘的 0
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
    lines = {}
    for raw in msg.split('\n'):
        if ':' in raw:
            k, v = raw.split(':', 1)
            lines[k.strip()] = v.strip()

    try:
        # 清理數字並轉型
        # 確保 RTP 和 Bets 是浮點數，然後轉為整數用於判斷
        not_open = int(re.sub(r'[^\d]', '', lines.get("未開轉數", "0")))
        rtp_float = float(re.sub(r'[^\d\.]', '', lines.get("今日RTP%數", "0")))
        rtp_today = int(rtp_float)
        bets_float = float(re.sub(r'[^\d\.]', '', lines.get("今日總下注額", "0")))
        bets_today = int(bets_float)
        
    except Exception:
        return "❌ 分析失敗，請確認輸入格式及數值正確。\n\n範例：\n未開轉數 : 120\n今日RTP%數 : 105.38\n今日總下注額 : 45000.55"

    # 生成兩組訊號組合
    all_combos = []
    for _ in range(2):
        attempts = 0
        while True:
            attempts += 1
            # 隨機選擇 2 到 3 種訊號
            chosen = random.sample(SIGNALS_POOL, k=random.choice([2, 3]))
            # 隨機分配數量，不超過單個訊號的上限
            combo = [(s[0], random.randint(1, s[1])) for s in chosen]
            # 確保總顆數不會過高 (例如 <= 12)
            if sum(q for _, q in combo) <= 12 or attempts > 30:
                all_combos.append(combo)
                break

    # 儲存到臨時記憶體 (用於後續的「儲存訊號」指令)
    LATEST_SIGNALS[line_user_id] = {
        "combos": all_combos,
        "generated_at": datetime.utcnow().isoformat()
    }

    # 如果自動儲存開啟，則寫入資料庫
    if AUTO_SAVE_SIGNALS:
        try:
            save_signal_stats(all_combos)
        except Exception:
            logger.exception("[auto_save_signal_stats error]")

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
    required_envs = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"]
    return jsonify({
        "status": "ok",
        "env_set": {name: bool(os.getenv(name)) for name in required_envs},
        "auto_save_signals": AUTO_SAVE_SIGNALS,
        "ocr_enabled": vision_client is not None,
        "vision_cred_path": VISION_CREDENTIALS_FILE if VISION_CREDENTIALS_FILE else "N/A"
    }), 200

# === LINE Webhook 處理 ===
@app.route("/callback", methods=["POST"])
def callback():
    """接收來自 LINE 平台的訊息與事件"""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"Received /callback - signature present: {bool(signature)}, body length: {len(body)}")
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Check your channel secret.")
        abort(400)
    except Exception as e:
        app.logger.exception(f"Webhook 處理錯誤: {e}")
        abort(400)
    return "OK", 200

@handler.add(MessageEvent)
def handle_message(event):
    """處理接收到的所有訊息事件 (文字或圖片)"""
    user_id = getattr(event.source, "user_id", "unknown")
    
    msg_for_analysis = ""
    msg_hash = ""
    reply = ""

    # 使用 ApiClient context manager 來確保連線資源被妥善管理
    with ApiClient(configuration) as api_client:
        # 1. 初始化 Messaging 和 Content 客戶端
        line_bot_api = MessagingApi(api_client) # 用於發送回覆
        line_content_api = ContentApi(api_client) # 用於下載圖片/內容
        
        member_data = get_member(user_id)
        
        try:
            # --- 步驟 1: 處理訊息類型 (文字或圖片) ---
            if event.message.type == "text":
                msg = event.message.text.strip()
                msg_for_analysis = msg
                # 針對原始文字訊息生成 Hash
                msg_hash = hashlib.sha256(msg_for_analysis.encode("utf-8")).hexdigest()
                
            elif event.message.type == "image":
                app.logger.info(f"[DEBUG] user_id: {user_id}, 收到圖片訊息。")
                
                # 執行 OCR 和數據提取
                text_for_analysis, error_msg = ocr_and_extract_data(event.message.id, line_content_api)
                
                if error_msg:
                    reply = error_msg
                elif text_for_analysis:
                    msg_for_analysis = text_for_analysis
                    # 為 OCR 提取的內容生成 Hash
                    msg_hash = hashlib.sha256(msg_for_analysis.encode("utf-8")).hexdigest()
                    app.logger.info(f"[DEBUG] OCR 提取文字:\n{msg_for_analysis}")
                    
            else:
                reply = "目前只支援文字或圖片的房間資訊分析。"


            # --- 步驟 2: 處理固定指令 (僅對原始文字訊息執行) ---
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
                        "未開轉數 : 120\n"
                        "今日RTP%數 : 105.38\n"
                        "今日總下注額 : 45000.55\n\n"
                        "⚠️ 注意事項：\n"
                        "1️⃣ 分析結果分為高 / 中 / 低風險\n"
                        "2️⃣ 每日使用次數：normal 15 次，vip 50 次\n"
                        "3️⃣ 若要儲存剛剛系統產生的訊號，請傳「儲存訊號」\n"
                        "4️⃣ 圖片分析功能已開啟，可直接傳送遊戲畫面。"
                    )

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
                            app.logger.exception("[save_signal_stats error]")
                            reply = "❌ 儲存失敗，請稍後再試。"

                elif msg == "管理員儲存訊號":
                    if ADMIN_LINE_ID and user_id == ADMIN_LINE_ID:
                        saved_count = 0
                        for uid, data in list(LATEST_SIGNALS.items()):
                            try:
                                save_signal_stats(data["combos"])
                                saved_count += 1
                                del LATEST_SIGNALS[uid]
                            except Exception:
                                app.logger.exception("[admin save_signal_stats error]")
                        reply = f"管理員操作完成，已嘗試儲存 {saved_count} 位使用者的推薦訊號。"
                    else:
                        reply = "❌ 你不是管理員，無法執行此操作。"
                    
            # --- 步驟 3: 處理分析流程 ---
            # 確保沒有被固定指令覆蓋，且是分析請求 (圖片 OCR 成功也視為分析請求)
            is_analysis_request = msg_for_analysis and ("RTP" in msg_for_analysis or "轉" in msg_for_analysis or "注額" in msg_for_analysis)
            
            if is_analysis_request and not reply: 
                
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

            # --- 步驟 4: 處理無法識別的訊息 ---
            if not reply:
                reply = "請傳送房間資訊或使用下方快速選單進行操作。"

            # --- 步驟 5: 回覆用戶 ---
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply, quick_reply=build_quick_reply())]
            ))
            
        except ApiException as e:
            app.logger.error(f"[LINE_API_ERROR] Failed to reply: {e}")
        except Exception as e:
            app.logger.exception(f"[GENERAL_ERROR] Failed to handle message: {e}")

# === 執行伺服器 ===
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port, debug=True)
    finally:
        # 清理臨時憑證文件 (如果已創建)
        if VISION_CREDENTIALS_FILE and os.path.exists(VISION_CREDENTIALS_FILE):
            try:
                os.remove(VISION_CREDENTIALS_FILE)
                logger.info(f"臨時憑證文件 {VISION_CREDENTIALS_FILE} 已清理。")
            except Exception as e:
                logger.error(f"無法清理臨時憑證文件: {e}")
