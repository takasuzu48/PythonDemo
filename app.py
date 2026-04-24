import os
import hmac
import hashlib
import time
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

load_dotenv()

app = Flask(__name__)

SLACK_TOKEN          = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL        = os.environ["SLACK_CHANNEL_ID"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
FILEAI_API_KEY       = os.environ["FILEAI_API_KEY"]
FILEAI_DIRECTORY_ID  = os.environ.get("FILEAI_DIRECTORY_ID", "")
RENDER_BASE_URL      = os.environ["RENDER_BASE_URL"]
JST = timezone(timedelta(hours=9))

# ── 処理済みイベントIDを記録（リトライ重複防止） ──────────
processed_event_ids = set()


# ── Slack にメッセージを送る ──────────────────────────────
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


# ── Slack リクエストの署名検証 ────────────────────────────
def verify_slack_signature(req) -> bool:
    signing_secret = SLACK_SIGNING_SECRET.encode("utf-8")
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    slack_signature = req.headers.get("X-Slack-Signature", "")

    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_signature = (
        "v0="
        + hmac.HMAC(
            signing_secret,
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
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

    upload_url = data.get("presignedUploadURL") or data.get("uploadUrl") or data.get("url")
    print(f"fileAI upload - uploadUrl: {upload_url}", flush=True)
         
    if upload_url:
        print(f"fileAI PUT - starting upload, size: {len(file_content)} bytes", flush=True)
        put_resp = requests.put(
            upload_url,
            data=file_content,
            headers={"Content-Type": file_type},  # ← Content-Typeのみ
            timeout=60,
        )
        print(f"fileAI PUT - status: {put_resp.status_code}", flush=True)
        print(f"fileAI PUT - response body: {put_resp.text}", flush=True)
        put_resp.raise_for_status()
        
    else:
        print(f"fileAI upload - presignedUploadURL not found in response", flush=True)

    return data


# ── ファイル処理をバックグラウンドで実行 ─────────────────
def process_file_background(file_info: dict):
    file_id      = file_info.get("id")
    file_name    = file_info.get("name", "unknown")
    file_type    = file_info.get("mimetype", "application/octet-stream")

    # url_private_download → url_private に変更（オリジナルサイズ）
    download_url = file_info.get("url_private") or file_info.get("url_private_download")

    print(f"[BG] Processing file - id:{file_id} name:{file_name} type:{file_type}", flush=True)
    print(f"[BG] download_url: {download_url}", flush=True)

    # 以下はそのまま

    try:
        file_content = download_slack_file(download_url)
        result = upload_to_fileai(file_content, file_name, file_type)
        print(f"[BG] fileAI upload result: {result}", flush=True)
        post_to_slack(f"⏳ *{file_name}* をfileAIにアップロードしました。処理完了後に通知します。")

    except Exception as e:
        print(f"[BG] Error processing file {file_name}: {e}", flush=True)
        post_to_slack(f"❌ *{file_name}* のアップロードに失敗しました。\nエラー: {str(e)}")


# ── ① 接続確認用：Hello World ─────────────────────────────
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


# ── ③ Ver2：fileAI Webhook ────────────────────────────────
@app.post("/webhook")
def webhook():
    raw_body = request.get_data(as_text=True)
    print(f"Webhook raw body: {raw_body}", flush=True)

    body      = request.get_json(force=True)
    step      = body.get("step", "")
    status    = body.get("status", "")
    file_ids  = body.get("fileIds", [])
    upload_id = body.get("uploadId", "")  # ← uploadId も取得

    print(f"Webhook - step:{step} status:{status} file_ids:{file_ids} upload_id:{upload_id}", flush=True)

    if step != "processing_finished" or status != "completed":
        print(f"Webhook - skipped. step={step}, status={status}", flush=True)
        return jsonify(ok=True, skipped=True)

    # fileIds が空の場合は uploadId を代わりに使う
    if isinstance(file_ids, str):
        file_ids = [file_ids]

    if not file_ids and upload_id:
        print(f"Webhook - fileIds empty, using uploadId: {upload_id}", flush=True)
        file_ids = [upload_id]

    if not file_ids:
        print(f"Webhook - no fileIds or uploadId found, skipping", flush=True)
        return jsonify(ok=True, skipped=True)

    errors = []
    for file_id in file_ids:
        try:
            file_name = get_file_name(file_id)
            print(f"file_id: {file_id}, file_name: {file_name}", flush=True)

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
                            f"Please click this link for more details."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "View details →"},
                            "url": detail_url,
                            "action_id": "view_details",
                        }
                    ],
                },
            ]
            fallback = (
                f"📣Notification from file AI!!\n"
                f"{file_name} has been processed. "
                f"Please click this link for more details: {detail_url}"
            )
            post_to_slack(fallback, blocks)

        except Exception as e:
            print(f"Error for file_id {file_id}: {e}", flush=True)
            errors.append({"fileId": file_id, "error": str(e)})

    if errors:
        return jsonify(ok=False, errors=errors), 500

    return jsonify(ok=True)


# ── ④ Ver2：Slack Events API ──────────────────────────────
@app.post("/slack/events")
def slack_events():
    raw_body = request.get_data(as_text=True)

    body = request.get_json(force=True)

    # URL検証（初回設定時）
    if body.get("type") == "url_verification":
        return jsonify(challenge=body["challenge"])

    # 署名検証
    if not verify_slack_signature(request):
        print(f"★ signature verification failed", flush=True)
        return jsonify(error="invalid signature"), 403

    # リトライリクエストを無視 ← ここがポイント
    if request.headers.get("X-Slack-Retry-Reason") == "http_timeout":
        print(f"★ Slack retry request ignored", flush=True)
        return jsonify(ok=True)

    event     = body.get("event", {})
    event_id  = body.get("event_id", "")

    print(f"★ /slack/events called - event_id:{event_id} type:{event.get('type')}", flush=True)

    # 重複イベントを無視
    if event_id in processed_event_ids:
        print(f"★ duplicate event_id ignored: {event_id}", flush=True)
        return jsonify(ok=True)
    processed_event_ids.add(event_id)

    if event.get("type") != "message" or "files" not in event:
        print(f"★ skipped - type:{event.get('type')} has_files:{'files' in event}", flush=True)
        return jsonify(ok=True)

    # バックグラウンドで処理してSlackに即200を返す
    for file_info in event.get("files", []):
        thread = threading.Thread(
            target=process_file_background,
            args=(file_info,),
            daemon=True,
        )
        thread.start()
        print(f"★ background thread started for file: {file_info.get('name')}", flush=True)

    return jsonify(ok=True)  # ← 即座に200を返す


if __name__ == "__main__":
    app.run(debug=True)