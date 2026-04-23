import os
import hmac
import hashlib
import json
import time
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

app = Flask(__name__)

# ── Bot A：ファイルアップロード受付 ──────────────────────
SLACK_TOKEN          = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL        = os.environ["SLACK_CHANNEL_ID"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# ── Bot B：fileAI結果通知 ─────────────────────────────────
SLACK_TOKEN_B   = os.environ["SLACK_BOT_TOKEN_B"]
SLACK_CHANNEL_B = os.environ["SLACK_CHANNEL_ID_B"]

# ── 共通 ──────────────────────────────────────────────────
FILEAI_API_KEY      = os.environ["FILEAI_API_KEY"]
FILEAI_DIRECTORY_ID = os.environ.get("FILEAI_DIRECTORY_ID", "")
RENDER_BASE_URL     = os.environ["RENDER_BASE_URL"]
JST = timezone(timedelta(hours=9))


# ── Slack にメッセージを送る（Bot切り替え対応）───────────
def post_to_slack(text, blocks=None, token=None, channel=None):
    payload = {
        "channel": channel or SLACK_CHANNEL,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token or SLACK_TOKEN}"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return data


# ── fileAI API からファイル名を取得 ───────────────────────
def get_file_name(file_id: str) -> str:
    url = f"https://api.orion.file.ai/prod/v1/files/{file_id}/values"
    print(f"fileAI API request - url: {url}", flush=True)

    resp = requests.get(
        url,
        headers={"x-api-key": FILEAI_API_KEY},
        timeout=10,
    )
    print(f"fileAI API response - status: {resp.status_code}", flush=True)
    print(f"fileAI API response - body: {resp.text}", flush=True)

    if not resp.ok:
        print(f"fileAI API error - returning file_id as fallback", flush=True)
        return file_id

    data = resp.json()
    form_values = data.get("formValues", [])
    if not form_values:
        print(f"formValues is empty - fallback to file_id", flush=True)
        return file_id

    file_name = form_values[0].get("fileName", file_id)
    print(f"file_name: {file_name}", flush=True)
    return file_name


# ── 署名検証 ──────────────────────────────────────────────
def verify_slack_signature_raw(raw_body: bytes, headers) -> bool:
    signing_secret = SLACK_SIGNING_SECRET.encode("utf-8")
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    slack_signature = headers.get("X-Slack-Signature", "")

    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        print(f"★ timestamp check failed: {timestamp}", flush=True)
        return False

    sig_basestring = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    my_signature = (
        "v0="
        + hmac.HMAC(
            signing_secret,
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    print(f"★ my_signature:    {my_signature}", flush=True)
    print(f"★ slack_signature: {slack_signature}", flush=True)

    return hmac.compare_digest(my_signature, slack_signature)


# ── Slack からファイルをダウンロード ─────────────────────
def download_slack_file(url: str) -> bytes:
    print(f"Downloading file from Slack - url: {url}", flush=True)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        timeout=30,
    )
    print(f"Slack download - status: {resp.status_code}", flush=True)
    resp.raise_for_status()
    print(f"Slack download - file size: {len(resp.content)} bytes", flush=True)
    return resp.content


# ── fileAI にアップロード ─────────────────────────────────
def upload_to_fileai(file_content: bytes, file_name: str, file_type: str):
    payload = {
        "fileName":      file_name,
        "fileType":      file_type,
        "isSplit":       False,
        "isSplitExcel":  False,
        "callbackURL":   f"{RENDER_BASE_URL}/webhook",
        "ocrModel":      "Beethoven_ENG_O5.6",
        "schemaLocking": False,
        "isEphemeral":   False,
    }
    if FILEAI_DIRECTORY_ID:
        payload["directoryId"] = FILEAI_DIRECTORY_ID

    print(f"fileAI upload - payload: {payload}", flush=True)

    resp = requests.post(
        "https://api.orion.file.ai/prod/v1/files/upload",
        headers={
            "x-api-key":    FILEAI_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    print(f"fileAI upload - status: {resp.status_code}", flush=True)
    print(f"fileAI upload - response body: {resp.text}", flush=True)

    resp.raise_for_status()
    data = resp.json()

    upload_url = data.get("uploadUrl") or data.get("url")
    print(f"fileAI upload - uploadUrl: {upload_url}", flush=True)

    if upload_url:
        print(f"fileAI PUT - starting upload, size: {len(file_content)} bytes", flush=True)
        put_resp = requests.put(
            upload_url,
            data=file_content,
            headers={"Content-Type": file_type},
            timeout=60,
        )
        print(f"fileAI PUT - status: {put_resp.status_code}", flush=True)
        print(f"fileAI PUT - response body: {put_resp.text}", flush=True)
        put_resp.raise_for_status()
    else:
        print(f"fileAI upload - uploadUrl not found: {data}", flush=True)

    return data


# ── ① 接続確認 ────────────────────────────────────────────
@app.post("/hello")
def hello():
    try:
        post_to_slack("👋 Hello from Render + Flask! 接続確認OK")
        return jsonify(ok=True, message="Slackに送信しました")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ② Ver1：外部アプリからの通知 ─────────────────────────
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


# ── ③ Ver2：fileAI Webhook（Bot B で通知）────────────────
@app.post("/webhook")
def webhook():
    raw_body = request.get_data()
    print(f"Webhook received raw: {raw_body}", flush=True)
    body = json.loads(raw_body)
    print(f"Webhook received: {body}", flush=True)

    step     = body.get("step", "")
    status   = body.get("status", "")
    file_ids = body.get("fileIds", [])

    print(f"Webhook - step:{step} status:{status} file_ids:{file_ids}", flush=True)

    if step != "processing_finished" or status != "completed":
        print(f"Webhook - skipped", flush=True)
        return jsonify(ok=True, skipped=True)

    if isinstance(file_ids, str):
        file_ids = [file_ids]

    errors = []
    for file_id in file_ids:
        try:
            file_name  = get_file_name(file_id)
            detail_url = (
                f"https://orion.file.ai/en/projects/drive/{file_id}/{file_name}"
            )
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "📣Notification from file AI!!"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{file_name}* has been processed.\n"
                            "Please click this link for more details."
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View details →"},
                        "url": detail_url,
                        "style": "primary",
                    },
                },
            ]
            fallback = (
                f"📣Notification from file AI!!\n"
                f"{file_name} has been processed. "
                f"Please click this link for more details: {detail_url}"
            )
            # Bot B で通知
            post_to_slack(
                fallback,
                blocks,
                token=SLACK_TOKEN_B,
                channel=SLACK_CHANNEL_B,
            )

        except Exception as e:
            errors.append({"fileId": file_id, "error": str(e)})

    if errors:
        return jsonify(ok=False, errors=errors), 500

    return jsonify(ok=True)


# ── ④ Ver2：Slack Events API（Bot A でアップロード）──────
@app.post("/slack/events")
def slack_events():
    raw_body = request.get_data()
    print(f"★ /slack/events called", flush=True)

    body = json.loads(raw_body)

    if body.get("type") == "url_verification":
        return jsonify(challenge=body["challenge"])

    if not verify_slack_signature_raw(raw_body, request.headers):
        print(f"★ signature verification failed", flush=True)
        return jsonify(error="invalid signature"), 403

    event = body.get("event", {})
    print(f"★ event type: {event.get('type')}", flush=True)
    print(f"★ has files: {'files' in event}", flush=True)
    print(f"★ full event: {event}", flush=True)

    if event.get("type") != "message" or "files" not in event:
        print(f"★ skipped - type:{event.get('type')} has_files:{'files' in event}", flush=True)
        return jsonify(ok=True)

    for file_info in event.get("files", []):
        file_id      = file_info.get("id")
        file_name    = file_info.get("name", "unknown")
        file_type    = file_info.get("mimetype", "application/octet-stream")
        download_url = file_info.get("url_private_download")

        print(f"Processing file - id:{file_id} name:{file_name} type:{file_type}", flush=True)

        try:
            file_content = download_slack_file(download_url)
            result = upload_to_fileai(file_content, file_name, file_type)
            print(f"fileAI upload result: {result}", flush=True)

            post_to_slack(
                f"⏳ *{file_name}* をfileAIにアップロードしました。処理完了後に通知します。",
                token=SLACK_TOKEN_B,      # ← B に変更
                channel=SLACK_CHANNEL_B,  # ← B に変更
            )

        except Exception as e:
            print(f"Error processing file {file_name}: {e}", flush=True)
            post_to_slack(
                f"❌ *{file_name}* のアップロードに失敗しました。\nエラー: {str(e)}",
                token=SLACK_TOKEN_B,      # ← B に変更
                channel=SLACK_CHANNEL_B,  # ← B に変更
            )

    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)