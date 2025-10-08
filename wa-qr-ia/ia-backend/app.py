import base64, os, hmac, hashlib, time
from io import BytesIO
from collections import deque
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from PIL import Image
    import pytesseract
except Exception:
    Image = None
    pytesseract = None

try:
    from utils_s3 import put_bytes
except Exception:
    put_bytes = None

app = FastAPI()

# Static media dir and recent messages buffer
TMP_MEDIA_DIR = "tmp_media"
os.makedirs(TMP_MEDIA_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=TMP_MEDIA_DIR), name="media")

RECENT_BUFFER_SIZE = int(os.getenv("RECENT_BUFFER_SIZE", "500"))
recent_messages = deque(maxlen=RECENT_BUFFER_SIZE)

# Last QR cache
latest_qr: str | None = None
latest_qr_ts: float | None = None


class Message(BaseModel):
    from_: str | None = None
    author: str | None = None
    timestamp: int | None = None
    isGroup: bool = False
    groupName: str | None = None
    type: str  # "text" | "media"
    text: str | None = None
    mimetype: str | None = None
    filename: str | None = None
    data_base64: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


def _verify_hmac(raw_body: bytes, signature: str | None) -> None:
    secret = os.getenv("HMAC_SECRET")
    if not secret:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # Constant time compare
    if not hmac.compare_digest(mac, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


def _maybe_ocr_image(raw: bytes, mimetype: str | None) -> str | None:
    if os.getenv("OCR_ENABLED", "0") != "1":
        return None
    if not mimetype or not mimetype.startswith("image/"):
        return None
    if not Image or not pytesseract:
        return None
    try:
        img = Image.open(BytesIO(raw))
        text = pytesseract.image_to_string(img)
        return text.strip() or None
    except Exception:
        return None


@app.post("/ingesta")
async def ingesta(request: Request):
    # HMAC
    raw = await request.body()
    _verify_hmac(raw, request.headers.get("x-signature"))

    # Parse payload
    try:
        data = await request.json()
        msg = Message(**data)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad payload")

    # Filtro por grupo (opcional)
    wl = [g.strip() for g in os.getenv("GROUP_WHITELIST", "").split(",") if g.strip()]
    if msg.isGroup and wl and (msg.groupName not in wl):
        return {"status": "skipped", "reason": "group not whitelisted", "group": msg.groupName}

    # —— Texto
    if msg.type == "text":
        event = {
            "type": "text",
            "timestamp": msg.timestamp,
            "from": msg.from_,
            "author": msg.author,
            "isGroup": msg.isGroup,
            "groupName": msg.groupName,
            "text": msg.text,
        }
        recent_messages.append(event)
        return {"status": "ok", "kind": "text", "echo": msg.text}

    # —— Media
    if msg.type == "media" and msg.data_base64 and msg.mimetype:
        raw_bytes = base64.b64decode(msg.data_base64)
        max_size = int(os.getenv("MAX_MEDIA_SIZE", "0"))
        if max_size and len(raw_bytes) > max_size:
            raise HTTPException(status_code=413, detail="Media too large")

        # S3 si configurado, si no local
        bucket = os.getenv("S3_BUCKET")
        if bucket and put_bytes:
            url = put_bytes(
                bucket=bucket,
                key_prefix="incoming",
                content=raw_bytes,
                mime=msg.mimetype,
            )
            ocr_text = _maybe_ocr_image(raw_bytes, msg.mimetype)
            resp = {"status": "ok", "kind": "media", "s3_url": url}
            if ocr_text is not None:
                resp["ocr_text"] = ocr_text
            event = {
                "type": "media",
                "timestamp": msg.timestamp,
                "from": msg.from_,
                "author": msg.author,
                "isGroup": msg.isGroup,
                "groupName": msg.groupName,
                "mimetype": msg.mimetype,
                "filename": msg.filename,
                "media_url": url,
                "ocr_text": ocr_text,
            }
            recent_messages.append(event)
            return resp
        else:
            outdir = TMP_MEDIA_DIR
            os.makedirs(outdir, exist_ok=True)
            fname = msg.filename or "file.bin"
            base_name, ext = os.path.splitext(fname)
            path = os.path.join(outdir, fname)
            i = 1
            while os.path.exists(path):
                path = os.path.join(outdir, f"{base_name}_{i}{ext}")
                i += 1
            with open(path, "wb") as f:
                f.write(raw_bytes)
            ocr_text = _maybe_ocr_image(raw_bytes, msg.mimetype)
            media_url = f"/media/{os.path.basename(path)}"
            resp = {"status": "ok", "kind": "media", "stored": path}
            if ocr_text is not None:
                resp["ocr_text"] = ocr_text
            event = {
                "type": "media",
                "timestamp": msg.timestamp,
                "from": msg.from_,
                "author": msg.author,
                "isGroup": msg.isGroup,
                "groupName": msg.groupName,
                "mimetype": msg.mimetype,
                "filename": os.path.basename(path),
                "media_url": media_url,
                "ocr_text": ocr_text,
            }
            recent_messages.append(event)
            return resp

    raise HTTPException(status_code=400, detail="Bad payload")


@app.get("/messages/recent")
def get_recent_messages(limit: int = Query(100, ge=1, le=1000)):
    items = list(recent_messages)
    if limit:
        items = items[-limit:]
    # devolver del más reciente al más antiguo
    return {"items": items[::-1]}


@app.get("/media/list")
def list_media():
    entries = []
    if os.path.isdir(TMP_MEDIA_DIR):
        try:
            names = os.listdir(TMP_MEDIA_DIR)
            for name in sorted(names, reverse=True):
                path = os.path.join(TMP_MEDIA_DIR, name)
                if os.path.isfile(path):
                    try:
                        entries.append({
                            "name": name,
                            "url": f"/media/{name}",
                            "size": os.path.getsize(path),
                        })
                    except Exception:
                        continue
        except Exception:
            pass
    return {"items": entries}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>wa-qr-ia — Dashboard</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 20px; }
      .item { border: 1px solid #ddd; padding: 10px; margin-bottom: 10px; border-radius: 6px; }
      .meta { color: #666; font-size: 12px; margin-bottom: 6px; }
      .text { white-space: pre-wrap; }
      img { max-width: 240px; height: auto; display: block; margin-top: 6px; }
      .row { display: flex; gap: 16px; align-items: center; }
      .pill { background: #f2f2f2; border-radius: 999px; padding: 2px 8px; font-size: 12px; }
    </style>
  </head>
  <body>
    <h1>wa-qr-ia — Dashboard</h1>
    <div>
      <span class=\"pill\" id=\"lastUpdate\">Actualizando...</span>
      <span class=\"pill\">Polling: 5s</span>
      <a class=\"pill\" href=\"/media/list\" target=\"_blank\">/media/list</a>
      <a class=\"pill\" href=\"/health\" target=\"_blank\">/health</a>
    </div>
    <div class=\"row\" style=\"margin-top: 16px;\">
      <div id=\"qrWrap\" style=\"min-width:260px;\">
        <h3>QR</h3>
        <div id=\"qr\"></div>
        <div class=\"meta\" id=\"qrMeta\"></div>
      </div>
      <div style=\"flex:1;\">
        <h3>Feed</h3>
        <div id=\"feed\"></div>
      </div>
    </div>
    <script src=\"https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js\"></script>
    <script>
      let qrInstance = null;
      function ensureQRInstance() {
        if (!qrInstance) {
          const el = document.getElementById('qr');
          qrInstance = new QRCode(el, { width: 240, height: 240 });
        }
        return qrInstance;
      }

      async function fetchQR() {
        try {
          const res = await fetch('/qr');
          if (!res.ok) { return; }
          const data = await res.json();
          const q = data.qr;
          const ts = data.ts ? new Date(data.ts * 1000).toLocaleTimeString() : '';
          const meta = document.getElementById('qrMeta');
          if (q) {
            ensureQRInstance().makeCode(q);
            meta.textContent = 'QR actualizado: ' + ts;
          } else {
            const el = document.getElementById('qr');
            el.innerHTML = '<span class=\\'pill\\'>Conectado</span>';
            meta.textContent = '';
            qrInstance = null;
          }
        } catch (e) { console.error(e); }
      }

      async function fetchRecent() {
        try {
          const res = await fetch('/messages/recent?limit=50');
          const data = await res.json();
          const feed = document.getElementById('feed');
          document.getElementById('lastUpdate').textContent = 'Última actualización: ' + new Date().toLocaleTimeString();
          feed.innerHTML = '';
          (data.items || []).forEach((it) => {
            const wrap = document.createElement('div');
            wrap.className = 'item';
            const ts = it.timestamp ? new Date(it.timestamp * 1000).toLocaleString() : '-';
            wrap.innerHTML = `
              <div class=\"meta\">${ts} · ${it.isGroup ? ('Grupo: ' + (it.groupName||'-')) : 'Privado'} · from: ${(it.from||'-')} · author: ${(it.author||'-')}</div>
              <div><span class=\"pill\">${it.type}</span> ${it.mimetype ? ('<span class=\\'pill\\'>' + it.mimetype + '</span>') : ''}</div>
              <div class=\"text\">${it.text ? it.text.replace(/</g,'&lt;') : ''}</div>
              ${it.media_url ? ('<a href=\\'' + it.media_url + '\\' target=\\'_blank\\'>Abrir archivo</a>') : ''}
            `;
            if (it.media_url && (it.mimetype||'').startsWith('image/')) {
              const img = document.createElement('img');
              img.src = it.media_url;
              wrap.appendChild(img);
            }
            if (it.ocr_text) {
              const o = document.createElement('div');
              o.className = 'text';
              o.innerHTML = '<b>OCR:</b> ' + it.ocr_text.replace(/</g,'&lt;');
              wrap.appendChild(o);
            }
            feed.appendChild(wrap);
          });
        } catch (e) {
          console.error(e);
        }
      }
      fetchRecent();
      fetchQR();
      setInterval(fetchRecent, 5000);
      setInterval(fetchQR, 5000);
    </script>
  </body>
</html>
"""


@app.post("/qr")
async def set_qr(request: Request):
    raw = await request.body()
    _verify_hmac(raw, request.headers.get("x-signature"))
    try:
        payload = await request.json()
        qr_value = payload.get("qr")
    except Exception:
        raise HTTPException(status_code=400, detail="Bad payload")
    global latest_qr, latest_qr_ts
    latest_qr = qr_value if qr_value else None
    latest_qr_ts = time.time() if latest_qr else None
    return {"ok": True}


@app.get("/qr")
def get_qr():
    return {"qr": latest_qr, "ts": latest_qr_ts}

