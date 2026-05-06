#!/usr/bin/env python3
"""
Generate one image per minimal storyboard image card.

Expected input shape from storyboard_planner_minimal.py:

{
  "items": [
    {
      "source_id": "1",
      "original_text": "...",
      "simplified_message": "...",
      "cards": [
        {
          "card_index": 1,
          "scene_label": "Lunch choice",
          "reuse_key": "lunch_choice",
          "prompt": "warm clean elder-care illustration, ... no text, no captions, no labels, minimal clutter",
          "avoid": ["text", "captions", "labels"]
        }
      ]
    }
  ]
}

Design principle:
- Do NOT build metadata-heavy prompts.
- Do NOT prepend "Panel goal", "Storyboard strategy", "Composition", etc.
- Use only the optional global service prompt plus the card prompt.
"""

import os
import json
import time
import argparse
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image
from huggingface_hub import InferenceClient


DEFAULT_FINAL_NEGATIVE_REMINDER = (
    "single standalone image, no text, no captions, no labels, no speech bubbles, "
    "no signs, no handwriting, no logos, no UI, no comic layout, minimal clutter"
)


def load_items(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]

    if isinstance(data, list):
        return data

    raise ValueError("Input JSON must be an object with an 'items' list or a raw list.")


def slugify(value: str, max_len: int = 60) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:max_len].strip("-") or "image"


def make_filename(source_id: str, card_index: int, label: str, prompt: str) -> str:
    slug = slugify(label)
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
    return f"{source_id}_c{card_index}_{slug}_{digest}.png"


def read_manifest(manifest_path: Path) -> Dict[str, Any]:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"created_at": int(time.time()), "schema": "generated_image_manifest_v2", "items": []}


def append_manifest(manifest_path: Path, record: Dict[str, Any]) -> None:
    manifest = read_manifest(manifest_path)
    manifest["items"].append(record)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def save_image(image: Image.Image, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")


def build_card_prompt(
    service_prompt: str,
    card: Dict[str, Any],
    add_final_reminder: bool = True,
) -> str:
    """
    Build a diffusion-friendly prompt.

    Important:
    - This intentionally avoids headings and metadata labels.
    - It does not include original_text, simplified_message, scene_label, or reuse_key.
    - Those fields are useful for filenames/manifests, not image generation.
    """
    parts: List[str] = []

    service_prompt = service_prompt.strip()
    if service_prompt:
        parts.append(service_prompt)

    card_prompt = str(card.get("prompt") or "").strip()
    if not card_prompt:
        raise ValueError("Card is missing required field: prompt")

    parts.append(card_prompt)

    if add_final_reminder:
        parts.append(DEFAULT_FINAL_NEGATIVE_REMINDER)

    # Comma-separated prompt is less likely to resemble an instructional document/poster.
    return ", ".join(part for part in parts if part).strip()


def get_cards(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Primary supported shape: item['cards'].

    A tiny backward-compatible fallback is included for old storyboard files,
    but minimal planner output should use cards.
    """
    cards = item.get("cards")
    if isinstance(cards, list) and cards:
        return cards

    # Backward-compatible fallback for older files.
    panels = item.get("panels")
    if isinstance(panels, list) and panels:
        converted = []
        for idx, panel in enumerate(panels, start=1):
            prompt = panel.get("prompt") or panel.get("prompt_fragment") or ""
            converted.append({
                "card_index": panel.get("card_index") or panel.get("panel_index") or idx,
                "scene_label": panel.get("scene_label") or panel.get("short_caption") or f"card-{idx}",
                "reuse_key": panel.get("reuse_key") or slugify(panel.get("scene_label") or panel.get("short_caption") or f"card-{idx}"),
                "prompt": prompt,
                "avoid": panel.get("avoid", []),
            })
        return converted

    return []


def generate_from_storyboards(
    service_prompt: str,
    input_json: Path,
    output_dir: Path,
    model: str,
    provider: str,
    api_key: str,
    width: Optional[int],
    height: Optional[int],
    guidance_scale: Optional[float],
    num_inference_steps: Optional[int],
    overwrite: bool,
    sleep_seconds: float,
    add_final_reminder: bool,
) -> Path:
    items = load_items(input_json)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"

    client = InferenceClient(api_key=api_key)

    for item_idx, item in enumerate(items, start=1):
        source_id = str(item.get("source_id") or item.get("id") or f"item_{item_idx:03d}")
        cards = get_cards(item)

        if not cards:
            append_manifest(manifest_path, {
                "source_id": source_id,
                "status": "skipped_no_cards",
                "model": model,
                "provider": provider,
                "input_json": str(input_json.resolve()),
                "original_text": item.get("original_text"),
                "simplified_message": item.get("simplified_message"),
            })
            continue

        for card_idx, card in enumerate(cards, start=1):
            card_index = int(card.get("card_index") or card_idx)
            scene_label = str(card.get("scene_label") or f"card-{card_index}")
            reuse_key = str(card.get("reuse_key") or slugify(scene_label))

            try:
                combined_prompt = build_card_prompt(
                    service_prompt=service_prompt,
                    card=card,
                    add_final_reminder=add_final_reminder,
                )
            except Exception as e:
                append_manifest(manifest_path, {
                    "source_id": source_id,
                    "card_index": card_index,
                    "scene_label": scene_label,
                    "reuse_key": reuse_key,
                    "status": "error_build_prompt",
                    "error": str(e),
                    "card": card,
                })
                continue

            out_name = make_filename(source_id, card_index, reuse_key or scene_label, combined_prompt)
            out_path = output_dir / out_name

            parameters: Dict[str, Any] = {}
            if width is not None:
                parameters["width"] = width
            if height is not None:
                parameters["height"] = height
            if guidance_scale is not None:
                parameters["guidance_scale"] = guidance_scale
            if num_inference_steps is not None:
                parameters["num_inference_steps"] = num_inference_steps

            base_record = {
                "source_id": source_id,
                "card_index": card_index,
                "scene_label": scene_label,
                "reuse_key": reuse_key,
                "model": model,
                "provider": provider,
                "input_json": str(input_json.resolve()),
                "output_file": str(out_path.resolve()),
                "output_file_relative": str(out_path),
                "filename": out_name,
                "original_text": item.get("original_text"),
                "simplified_message": item.get("simplified_message"),
                "card": card,
                "combined_prompt": combined_prompt,
                "parameters": parameters,
            }

            if out_path.exists() and not overwrite:
                append_manifest(manifest_path, {**base_record, "status": "skipped_exists"})
                continue

            try:
                kwargs: Dict[str, Any] = {"model": model, **parameters}

                # Provider is kept in the CLI/API for tracking, but huggingface_hub's
                # InferenceClient.text_to_image currently routes from the model argument.
                image = client.text_to_image(combined_prompt, **kwargs)

                save_image(image, out_path)
                append_manifest(manifest_path, {**base_record, "status": "ok"})

            except Exception as e:
                append_manifest(manifest_path, {
                    **base_record,
                    "status": "error",
                    "error": str(e),
                })

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    return manifest_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate one image per minimal storyboard card using Hugging Face routed inference."
    )
    parser.add_argument("--service-prompt-file", type=Path, help="Optional global image prompt file.")
    parser.add_argument("--service-prompt", type=str, default="", help="Optional global image prompt string.")
    parser.add_argument("--input-json", type=Path, required=True, help="Minimal storyboard JSON containing items[].cards[].")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to save PNG files and manifest.json.")
    parser.add_argument("--model", type=str, default="black-forest-labs/FLUX.1-schnell", help="HF model id.")
    parser.add_argument("--provider", type=str, default="auto", help="Stored in manifest for tracking.")
    parser.add_argument("--api-key", type=str, default=os.environ.get("HF_TOKEN", ""), help="HF token or set HF_TOKEN.")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--guidance-scale", type=float, default=None)
    parser.add_argument("--num-inference-steps", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument(
        "--no-final-reminder",
        action="store_true",
        help="Do not append the short final no-text/no-caption reminder to each prompt.",
    )
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Pass --api-key or set HF_TOKEN.")

    service_prompt = args.service_prompt
    if args.service_prompt_file:
        service_prompt = args.service_prompt_file.read_text(encoding="utf-8").strip()

    manifest_path = generate_from_storyboards(
        service_prompt=service_prompt,
        input_json=args.input_json,
        output_dir=args.output_dir,
        model=args.model,
        provider=args.provider,
        api_key=args.api_key,
        width=args.width,
        height=args.height,
        guidance_scale=args.guidance_scale,
        num_inference_steps=args.num_inference_steps,
        overwrite=args.overwrite,
        sleep_seconds=args.sleep_seconds,
        add_final_reminder=not args.no_final_reminder,
    )

    print(str(manifest_path.resolve()))


if __name__ == "__main__":
    main()
