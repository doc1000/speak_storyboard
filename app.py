#!/usr/bin/env python3
"""
Minimal Flask app for the Speak in Pictures demo.

Routes:
  GET  /               - serve speak_in_pictures_demo.html
  GET  /img/<path>     - serve files from the img/ directory
  GET  /api/history    - return up to 10 recent items (seeds from v11 on first call)
  POST /api/generate   - run planner + HF image gen, persist to history.json
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory

from storyboard_planner_minimal import call_openai_structured, DEFAULT_SERVICE_PROMPT
from gen_image_minimal import build_card_prompt, make_filename, save_image

BASE_DIR = Path(__file__).parent
STORYBOARDS_JSON = BASE_DIR / "storyboards.json"
V11_MANIFEST = BASE_DIR / "img" / "v11" / "manifest.json"
IMG_DIR = BASE_DIR / "img"
WEB_IMG_DIR = IMG_DIR / "web"
HISTORY_JSON = BASE_DIR / "history.json"
SERVICE_PROMPT_FILE = BASE_DIR / "service_prompt_storyboard.txt"
HTML_FILE = "speak_in_pictures_demo.html"

HF_MODEL = "black-forest-labs/FLUX.1-schnell"
IMG_WIDTH = 1024
IMG_HEIGHT = 1024
IMG_STEPS = 4
HISTORY_CAP = 50
HISTORY_DISPLAY = 10

app = Flask(__name__, static_folder=None)

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
def index():
    return send_from_directory(str(BASE_DIR), HTML_FILE)


@app.get("/img/<path:filename>")
def serve_image(filename: str):
    return send_from_directory(str(IMG_DIR), filename)


@app.get("/api/history")
def api_history():
    items = _read_history()
    items_sorted = sorted(items, key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify({"items": items_sorted[:HISTORY_DISPLAY]})


@app.post("/api/generate")
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
