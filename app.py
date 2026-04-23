import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

app = Flask(__name__)

SLACK_TOKEN    = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL  = os.environ["SLACK_CHANNEL_ID"]
FILEAI_API_KEY = os.environ["FILEAI_API_KEY"]   # fileAI のAPIキー（後述）
JST = timezone(timedelta(hours=9))


def post_to_slack(text, blocks=None):
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


def get_file_name(file_id: str) -> str:
    """fileAI API からファイル名を取得する"""
    url = f"https://api.orion.file.ai/prod/v1/files/{file_id}/values"
    resp = requests.get(
        url,
        headers={"Authorization": f"x-api-key {FILEAI_API_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("fileName", file_id)   # 取得できなければ fileId をフォールバック


# ── ① 接続確認（Ver1 から継続） ──────────────────────────
@app.post("/hello")
def hello():
    try:
        post_to_slack("👋 Hello from Render + Flask! 接続確認OK")
        return jsonify(ok=True, message="Slackに送信しました")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ② Ver1：外部アプリからの通知（継続） ─────────────────
@app.post("/notify")
def notify():
    body    = request.get_json(force=True)
    summary = body.get("summary", "（概要なし）")
    status  = body.get("status", "info")
    url     = body.get("url", "")
    emoji   = {"success": "✅", "error": "❌"}.get(status, "⚠️")
    now     = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"{emoji} 処理結果通知"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*概要:* {summary}"}},
        {"type": "actions",
         "elements": [{"type": "button",
                        "text": {"type": "plain_text", "text": "詳細を見る →"},
                        "url": url, "style": "primary"}]},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"実行時刻: {now} JST"}]},
    ]
    try:
        post_to_slack(f"{emoji} {summary}", blocks)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ③ Ver2：fileAI Webhook ────────────────────────────────
@app.post("/webhook")
def webhook():
    body = request.get_json(force=True)

    step     = body.get("step", "")
    status   = body.get("status", "")
    file_ids = body.get("fileIds", [])

    # 対象イベント以外は即座に 200 を返して無視
    if step != "processing_finished" or status != "completed":
        return jsonify(ok=True, skipped=True)

    # fileIds が文字列で来るケースも吸収
    if isinstance(file_ids, str):
        file_ids = [file_ids]

    errors = []
    for file_id in file_ids:
        try:
            file_name = get_file_name(file_id)
            detail_url = (
                f"https://orion.file.ai/en/projects/drive/{file_id}/{file_id}"
            )
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{file_name}* has been processed.\n"
                            f"Please click this link for more details."
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View details →"},
                        "url": detail_url,
                        "style": "primary",
                    },
                }
            ]
            fallback = (
                f"{file_name} has been processed. "
                f"Please click this link for more details: {detail_url}"
            )
            post_to_slack(fallback, blocks)

        except Exception as e:
            errors.append({"fileId": file_id, "error": str(e)})

    if errors:
        return jsonify(ok=False, errors=errors), 500

    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)