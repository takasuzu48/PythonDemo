import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

app = Flask(__name__)

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL_ID"]
JST = timezone(timedelta(hours=9))

def post_to_slack(text, blocks=None):
    """Slack の chat.postMessage を呼ぶ共通関数"""
    payload = {"channel": SLACK_CHANNEL, "text": text}
    if blocks:
        payload["blocks"] = blocks

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return data


# ── ① 接続確認用：Hello World ─────────────────────────────
# curl -X POST https://your-app.onrender.com/hello
@app.post("/hello")
def hello():
    try:
        post_to_slack("👋 Hello from Render + Flask! 接続確認OK")
        return jsonify(ok=True, message="Slackに送信しました")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ② Ver1 本番用：外部アプリから結果を受け取って通知 ────
# curl -X POST https://your-app.onrender.com/notify \
#   -H "Content-Type: application/json" \
#   -d '{"summary":"バッチ完了","status":"success","url":"https://example.com/result/1"}'
@app.post("/notify")
def notify():
    body    = request.get_json(force=True)
    summary = body.get("summary", "（概要なし）")
    status  = body.get("status", "info")
    url     = body.get("url", "")

    emoji = {"success": "✅", "error": "❌"}.get(status, "⚠️")
    now   = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} 処理結果通知"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*概要:* {summary}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "詳細を見る →"},
                    "url": url,
                    "style": "primary",
                }
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"実行時刻: {now} JST"}],
        },
    ]

    try:
        post_to_slack(f"{emoji} {summary}", blocks)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    app.run(debug=True)
