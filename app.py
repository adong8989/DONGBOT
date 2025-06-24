from flask import Flask, request, abort
import os
import logging
from db import get_member, add_member  # 使用 Supabase 的會員查詢
from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest

app = Flask(__name__)

channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

configuration = Configuration(access_token=channel_access_token)
handler = WebhookHandler(channel_secret)

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        logging.exception("Webhook handler error")
        abort(400)
    return "OK"

@handler.add(MessageEvent)
def handle_message(event):
    user_id = event.source.user_id if event.source else "unknown"
    msg_type = event.message.type

    if msg_type != "text":
        return  # 只處理文字訊息

    msg = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 查詢會員資料
        member_data = get_member(user_id)

        if not member_data:
            # 沒有會員資料，表示尚未開通或申請過
            if msg == "我要開通":
                add_member(user_id)
                reply_text = "你已申請開通，請等待管理員審核。"
            else:
                reply_text = "您尚未開通，請傳送「我要開通」申請審核。"
        else:
            # 有會員資料，依狀態回覆
            status = member_data.get("status", "未知狀態")
            if msg == "我要開通":
                reply_text = f"你已經申請過囉！狀態：{status}"
            elif "RTP" in msg:
                reply_text = "這是 RTP 文字分析的回覆（尚未實作）。"
            else:
                reply_text = "功能選單：圖片分析 / 文字分析 / 我要開通"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
