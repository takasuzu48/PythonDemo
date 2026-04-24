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
FILEAI_SCHEMA_ID     = os.environ.get("FILEAI_SCHEMA_ID", "")
RENDER_BASE_URL      = os.environ["RENDER_BASE_URL"]
JST = timezone(timedelta(hours=9))

processed_event_ids = set()


# ── Post message to Slack ─────────────────────────────────
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


# ── Get file name from fileAI API ─────────────────────────
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


# ── Verify Slack request signature ────────────────────────
def verify_slack_signature(req) -> bool:
    signing_secret = SLACK_SIGNING_SECRET.encode("utf-8")
    timestamp      = req.headers.get("X-Slack-Request-Timestamp", "")
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


# ── Download file from Slack via files.info API ───────────
def download_slack_file_by_id(file_id: str) -> tuple[bytes, str]:
    print(f"Fetching file info for file_id: {file_id}", flush=True)

    info_resp = requests.get(
        "https://slack.com/api/files.info",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        params={"file": file_id},
        timeout=10,
    )
    info_resp.raise_for_status()
    info = info_resp.json()

    print(f"files.info full response ok: {info.get('ok')}", flush=True)

    if not info.get("ok"):
        raise RuntimeError(f"files.info error: {info.get('error')}")

    file_obj     = info.get("file", {})
    download_url = file_obj.get("url_private_download")
    file_type    = file_obj.get("mimetype", "application/octet-stream")

    print(f"files.info download_url: {download_url}", flush=True)
    print(f"files.info file size: {file_obj.get('size')} bytes", flush=True)

    resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()

    print(f"Downloaded file size: {len(resp.content)} bytes", flush=True)
    return resp.content, file_type


# ── Upload file to fileAI ─────────────────────────────────
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
    if FILEAI_SCHEMA_ID:
        payload["schemaId"] = FILEAI_SCHEMA_ID

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
            headers={"Content-Type": file_type},
            timeout=60,
        )
        print(f"fileAI PUT - status: {put_resp.status_code}", flush=True)
        print(f"fileAI PUT - response body: {put_resp.text}", flush=True)
        put_resp.raise_for_status()
    else:
        print(f"fileAI upload - presignedUploadURL not found in response", flush=True)

    return data


# ── Process file in background thread ────────────────────
def process_file_background(file_info: dict):
    file_id   = file_info.get("id")
    file_name = file_info.get("name", "unknown")

    print(f"[BG] Processing file - id:{file_id} name:{file_name}", flush=True)

    try:
        file_content, file_type = download_slack_file_by_id(file_id)
        print(f"[BG] Downloaded size: {len(file_content)} bytes (expected: {file_info.get('size')})", flush=True)

        result = upload_to_fileai(file_content, file_name, file_type)
        print(f"[BG] fileAI upload result: {result}", flush=True)

        post_to_slack(f"⏳ *{file_name}* has been uploaded to fileAI. You will be notified when processing is complete.")

    except Exception as e:
        print(f"[BG] Error processing file {file_name}: {e}", flush=True)
        post_to_slack(f"❌ Failed to upload *{file_name}* to fileAI.\nError: {str(e)}")


# ── ① Health check ───────────────────────────────────────
@app.get("/health")
def health():
    return jsonify(ok=True, status="running")


# ── ② Hello World connection test ────────────────────────
@app.post("/hello")
def hello():
    try:
        post_to_slack("👋 Hello from Render + Flask! Connection OK")
        return jsonify(ok=True, message="Message sent to Slack")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ③ Ver1: Notification from external app ───────────────
@app.post("/notify")
def notify():
    body    = request.get_json(force=True)
    summary = body.get("summary", "(no summary)")
    status  = body.get("status", "info")
    url     = body.get("url", "")
    emoji   = {"success": "✅", "error": "❌"}.get(status, "⚠️")
    now     = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"{emoji} Process Result Notification"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Summary:* {summary}"}},
        {"type": "actions",
         "elements": [{"type": "button",
                        "text": {"type": "plain_text", "text": "View details →"},
                        "url": url}]},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"Executed at: {now} JST"}]},
    ]
    try:
        post_to_slack(f"{emoji} {summary}", blocks)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── ④ Ver2: fileAI Webhook ───────────────────────────────
@app.post("/webhook")
def webhook():
    raw_body = request.get_data(as_text=True)
    print(f"Webhook raw body: {raw_body}", flush=True)

    body         = request.get_json(force=True)
    step         = body.get("step", "")
    status       = body.get("status", "")
    file_ids     = body.get("fileIds", [])
    upload_id    = body.get("uploadId", "")
    error_reason = body.get("errorReason", "")

    print(f"Webhook - step:{step} status:{status} file_ids:{file_ids} upload_id:{upload_id}", flush=True)

    # Processing complete
    if step == "processing_finished" and status == "completed":
        pass

    # Processing failed
    elif step == "processing_failed" or status == "error":
        print(f"Webhook - processing failed: {error_reason}", flush=True)
        post_to_slack(
            f"❌ fileAI processing failed.\n"
            f"Upload ID: `{upload_id}`\n"
            f"Reason: `{error_reason}`"
        )
        return jsonify(ok=True)

    # Other steps - skip
    else:
        print(f"Webhook - skipped. step={step}, status={status}", flush=True)
        return jsonify(ok=True, skipped=True)

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

            detail_url = f"https://orion.file.ai/en/projects/drive/{file_id}/{file_name}"
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "📣 Notification from fileAI!!"},
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
                f"📣 Notification from fileAI!!\n"
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


# ── ⑤ Ver2: Slack Events API ─────────────────────────────
@app.post("/slack/events")
def slack_events():
    body = request.get_json(force=True)

    # URL verification on initial setup
    if body.get("type") == "url_verification":
        return jsonify(challenge=body["challenge"])

    # Verify Slack signature
    if not verify_slack_signature(request):
        print(f"Signature verification failed", flush=True)
        return jsonify(error="invalid signature"), 403

    # Ignore Slack retry requests
    if request.headers.get("X-Slack-Retry-Reason") == "http_timeout":
        print(f"Slack retry request ignored", flush=True)
        return jsonify(ok=True)

    event    = body.get("event", {})
    event_id = body.get("event_id", "")

    print(f"★ /slack/events called - event_id:{event_id} type:{event.get('type')}", flush=True)

    # Ignore duplicate events
    if event_id in processed_event_ids:
        print(f"Duplicate event_id ignored: {event_id}", flush=True)
        return jsonify(ok=True)
    processed_event_ids.add(event_id)

    if event.get("type") != "message" or "files" not in event:
        print(f"Skipped - type:{event.get('type')} has_files:{'files' in event}", flush=True)
        return jsonify(ok=True)

    # Start background thread and return 200 immediately
    for file_info in event.get("files", []):
        thread = threading.Thread(
            target=process_file_background,
            args=(file_info,),
            daemon=True,
        )
        thread.start()
        print(f"Background thread started for file: {file_info.get('name')}", flush=True)

    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)