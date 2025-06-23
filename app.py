
from flask import Flask, request, abort
import os
import logging

from linebot.v3.webhook import WebhookHandler, MessageEvent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient
from linebot.v3.messaging.models import TextMessage, ReplyMessageRequest

app = Flask(__name__)

channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

configuration = Configuration(access_token=channel_access_token)
handler = WebhookHandler(channel_secret)

# 記錄已開通的使用者 user_id
approved_users = {
    # "Uxxxxxxxxxxxxxxxx"  # ← 把已開通者 ID 寫這裡
}

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

        if msg == "我要開通":
            reply_text = f"申請成功，你的 user_id 是：{user_id}，請提供給管理員審核。"
            print(f"[DEBUG] 收到開通申請 user_id: {user_id}")
        elif user_id not in approved_users:
            reply_text = "您尚未開通，請傳送「我要開通」申請審核。"
        elif "RTP" in msg:
            reply_text = "這是 RTP 文字分析的回覆（尚未實作）。"
        else:
            reply_text = "功能選單：圖片分析 / 文字分析 / 我要開通"

        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
