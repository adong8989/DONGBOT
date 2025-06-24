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
        reply_text = "申請成功，請等候管理員審核！"
elif not member_data:
    reply_text = "您尚未開通，請先輸入「我要開通」申請。"
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
