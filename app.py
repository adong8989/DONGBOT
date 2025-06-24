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

        # === 使用 Supabase 查詢會員狀態 ===
        member_data = get_member(user_id)

        if msg == "我要開通":
            if member_data:
                reply_text = f"你已經申請過囉！狀態：{member_data['status']}"
            else:
                add_member(user_id)
                reply_text_
