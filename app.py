#!/usr/bin/env python3
"""
Minimal Flask app for the Speak in Pictures demo.

Routes:
  GET/POST /login      - sign-in form (2 users, env-driven creds)
  GET      /logout     - clear session and redirect to /login
  GET      /           - serve speak_in_pictures_demo.html  [login required]
  GET      /img/<path> - serve files from the img/ directory  [login required]
  GET      /api/history    - return up to 10 recent items  [login required]
  POST     /api/generate   - run planner + HF image gen    [login required]
"""

import hmac
import json
import os
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for

from storyboard_planner_minimal import call_openai_structured, DEFAULT_SERVICE_PROMPT
from gen_image_minimal import build_card_prompt, make_filename, save_image
from dotenv import load_dotenv
load_dotenv()


BASE_DIR = Path(__file__).parent
# STORAGE_DIR=/data on Fly (volume mount); unset locally falls back to BASE_DIR
_storage = Path(os.environ["STORAGE_DIR"]) if os.environ.get("STORAGE_DIR") else BASE_DIR
STORYBOARDS_JSON = BASE_DIR / "storyboards.json"   # read-only seed, always baked in
V11_MANIFEST = BASE_DIR / "img" / "v11" / "manifest.json"  # read-only seed
IMG_DIR = _storage / "img"          # serves both seed + generated images
WEB_IMG_DIR = _storage / "img" / "web"
HISTORY_JSON = _storage / "history.json"
SERVICE_PROMPT_FILE = BASE_DIR / "service_prompt_storyboard.txt"
HTML_FILE = "speak_in_pictures_demo.html"

HF_MODEL = "black-forest-labs/FLUX.1-schnell"
IMG_WIDTH = 1024
IMG_HEIGHT = 1024
IMG_STEPS = 4
HISTORY_CAP = 50
HISTORY_DISPLAY = 30

app = Flask(__name__, static_folder="static")

_secret_key = os.environ.get("APP_SECRET_KEY", "")
if not _secret_key:
    raise RuntimeError("APP_SECRET_KEY environment variable is not set.")
app.secret_key = _secret_key
# SESSION_COOKIE_SECURE defaults True (production HTTPS); set to "false" in .env for local dev
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Lazy singletons — initialized on first /api/generate call so startup never fails
_openai_client = None
_hf_client = None


def _get_openai_client():
    global _openai_client
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is not set.")
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _get_hf_client():
    global _hf_client
    api_key = os.environ.get("HF_TOKEN", "")
    if not api_key:
        raise ValueError("HF_TOKEN environment variable is not set.")
    if _hf_client is None:
        from huggingface_hub import InferenceClient
        _hf_client = InferenceClient(api_key=api_key)
    return _hf_client


# --- Auth ---

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Speak in Pictures \u2014 Sign In</title>
  <link rel="manifest" href="/static/manifest.json" />
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="theme-color" content="#1a1510" />
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Georgia,'Times New Roman',serif;
          background:radial-gradient(ellipse 120% 80% at 50% 0%,#2a2218 0%,#1a1510 48%,#0f0c0a 100%);
          color:#e8e0d5;min-height:100vh;display:flex;align-items:center;justify-content:center}}
    .card{{background:#252018;border:1px solid rgba(184,154,107,.35);border-radius:10px;
           padding:36px 40px;width:100%;max-width:380px}}
    h1{{font-size:1.2rem;color:#b89a6b;margin-bottom:28px;text-align:center}}
    label{{display:block;font-size:.8rem;color:#a89888;margin-bottom:4px}}
    input{{width:100%;background:#1a1510;border:1px solid rgba(184,154,107,.3);border-radius:6px;
           color:#e8e0d5;font-family:inherit;font-size:1rem;padding:9px 12px;margin-bottom:18px;outline:none}}
    input:focus{{border-color:#b89a6b}}
    button{{width:100%;background:rgba(184,154,107,.15);border:1px solid rgba(184,154,107,.5);
            border-radius:6px;color:#b89a6b;font-family:inherit;font-size:1rem;padding:10px;cursor:pointer}}
    button:hover{{background:rgba(184,154,107,.28)}}
    .err{{color:#e8a898;font-size:.85rem;margin-bottom:14px;text-align:center}}
  </style>
</head>
<body>
  <div class="card">
    <h1>Speak in Pictures</h1>
    {error_html}
    <form method="post" action="/login">
      <label for="u">Username</label>
      <input type="text" id="u" name="username" autocomplete="username" autocapitalize="none" />
      <label for="p">Password</label>
      <input type="password" id="p" name="password" autocomplete="current-password" />
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>"""


def _login_page(error: str = "") -> str:
    error_html = f'<p class="err">{error}</p>' if error else ""
    return _LOGIN_HTML.format(error_html=error_html)


def _valid_users() -> list[tuple[str, str]]:
    pairs = []
    for i in ("1", "2"):
        u = os.environ.get(f"AUTH_USER_{i}", "")
        p = os.environ.get(f"AUTH_PASS_{i}", "")
        if u:
            pairs.append((u, p))
    return pairs


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        for stored_user, stored_pass in _valid_users():
            user_ok = hmac.compare_digest(username, stored_user.lower())
            pass_ok = hmac.compare_digest(password, stored_pass)
            if user_ok and pass_ok:
                session.permanent = True
                session["user"] = stored_user
                return redirect(url_for("index"))
        return _login_page(error="Invalid credentials.")
    return _login_page()


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- History helpers ---

def _seed_from_v11() -> list[dict]:
    """Build seed history items from storyboards.json + img/v11/manifest.json."""
    storyboards = json.loads(STORYBOARDS_JSON.read_text(encoding="utf-8"))
    manifest = json.loads(V11_MANIFEST.read_text(encoding="utf-8"))

    image_lookup: dict[tuple[str, int], str] = {}
    for entry in manifest.get("items", []):
        if entry.get("status") == "ok":
            key = (str(entry["source_id"]), int(entry["card_index"]))
            image_lookup[key] = f"/img/v11/{entry['filename']}"

    items = []
    for item in storyboards.get("items", []):
        source_id = str(item["source_id"])
        cards = []
        for card in item.get("cards", []):
            card_index = int(card["card_index"])
            cards.append({
                "card_index": card_index,
                "scene_label": card.get("scene_label", ""),
                "image_url": image_lookup.get((source_id, card_index)),
            })
        items.append({
            "source_id": source_id,
            "original_text": item.get("original_text", ""),
            "simplified_message": item.get("simplified_message", ""),
            "created_at": 0,  # seed items sort below any real generated item
            "cards": cards,
        })
    return items


def _read_history() -> list[dict]:
    """Read history.json, creating and seeding it from v11 data if absent."""
    if not HISTORY_JSON.exists():
        items = _seed_from_v11()
        _write_history(items)
        return items
    data = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
    return data.get("items", [])


def _write_history(items: list[dict]) -> None:
    HISTORY_JSON.write_text(
        json.dumps({"items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _append_history(item: dict) -> None:
    items = _read_history()
    items.append(item)
    if len(items) > HISTORY_CAP:
        items = items[-HISTORY_CAP:]
    _write_history(items)


def _load_service_prompt() -> str:
    if SERVICE_PROMPT_FILE.exists():
        return SERVICE_PROMPT_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_SERVICE_PROMPT


# --- Routes ---

@app.get("/")
@login_required
def index():
    return send_from_directory(str(BASE_DIR), HTML_FILE)


@app.get("/img/<path:filename>")
@login_required
def serve_image(filename: str):
    return send_from_directory(str(IMG_DIR), filename)


@app.get("/api/history")
@login_required
def api_history():
    items = _read_history()
    items_sorted = sorted(items, key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify({"items": items_sorted[:HISTORY_DISPLAY]})


@app.post("/api/generate")
@login_required
def api_generate():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    try:
        openai_client = _get_openai_client()
    except ValueError as e:
        return jsonify({"error": str(e)}), 500

    try:
        hf_client = _get_hf_client()
    except ValueError as e:
        return jsonify({"error": str(e)}), 500

    source_id = f"web_{int(time.time())}"
    service_prompt = _load_service_prompt()

    # Step 1: Generate storyboard plan via OpenAI
    try:
        batch = call_openai_structured(
            client=openai_client,
            model="gpt-4o-mini",
            service_prompt=service_prompt,
            items=[{"source_id": source_id, "text": text}],
        )
    except Exception as e:
        return jsonify({"error": f"Storyboard planning failed: {e}"}), 500

    if not batch.items:
        return jsonify({"error": "Planner returned no items"}), 500

    plan_item = batch.items[0]

    # Step 2: Generate one image per card
    WEB_IMG_DIR.mkdir(parents=True, exist_ok=True)
    cards_out = []

    for card in plan_item.cards:
        card_dict = card.model_dump(mode="json")
        image_url: Optional[str] = None
        try:
            prompt = build_card_prompt(service_prompt="", card=card_dict, add_final_reminder=True)
            filename = make_filename(source_id, card.card_index, card.reuse_key or card.scene_label, prompt)
            out_path = WEB_IMG_DIR / filename
            img = hf_client.text_to_image(
                prompt,
                model=HF_MODEL,
                width=IMG_WIDTH,
                height=IMG_HEIGHT,
                num_inference_steps=IMG_STEPS,
            )
            save_image(img, out_path)
            image_url = f"/img/web/{filename}"
        except Exception as e:
            app.logger.warning("Image generation failed for card %s: %s", card.card_index, e)

        cards_out.append({
            "card_index": card.card_index,
            "scene_label": card.scene_label,
            "image_url": image_url,
        })

    # Step 3: Persist and return
    record = {
        "source_id": source_id,
        "original_text": text,
        "simplified_message": plan_item.simplified_message,
        "created_at": int(time.time()),
        "cards": cards_out,
    }
    _append_history(record)
    return jsonify(record)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
