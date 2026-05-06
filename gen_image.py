#!/usr/bin/env python3
import os
import json
import time
import argparse
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image
from huggingface_hub import InferenceClient


def load_storyboards(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "items" in data:
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Input JSON must be a storyboard object with an 'items' list or a raw list.")
    if not isinstance(items, list):
        raise ValueError("'items' must be a list.")
    return items


def slugify(value: str, max_len: int = 60) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:max_len].strip("-") or "image"


def make_filename(source_id: str, panel_index: int, label: str, prompt: str) -> str:
    slug = slugify(label)
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
    return f"{source_id}_p{panel_index}_{slug}_{digest}.png"


def append_manifest(manifest_path: Path, record: Dict[str, Any]) -> None:
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"created_at": int(time.time()), "items": []}

    manifest["items"].append(record)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def save_image(image: Image.Image, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")


def build_panel_prompt(
    service_prompt: str,
    storyboard_item: Dict[str, Any],
    panel: Dict[str, Any],
    include_context: bool = True,
) -> str:
    prompt_parts = []

    if service_prompt.strip():
        prompt_parts.append(service_prompt.strip())

    prompt_fragment = panel.get("prompt_fragment", "").strip()

    if prompt_fragment:
        prompt_parts.append(prompt_fragment)

    return ", ".join(prompt_parts)


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
    include_context: bool,
) -> Path:
    items = load_storyboards(input_json)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"

    client = InferenceClient(api_key=api_key)

    for item_idx, item in enumerate(items, start=1):
        source_id = str(item.get("source_id") or item.get("id") or f"item_{item_idx:03d}")
        panels = item.get("panels", [])

        if not isinstance(panels, list) or not panels:
            record = {
                "source_id": source_id,
                "status": "skipped_no_panels",
                "model": model,
                "provider": provider,
                "input_json": str(input_json.resolve()),
            }
            append_manifest(manifest_path, record)
            continue

        for panel_idx, panel in enumerate(panels, start=1):
            panel_index = int(panel.get("panel_index") or panel_idx)
            label = panel.get("scene_label") or panel.get("role") or f"panel-{panel_index}"
            combined_prompt = build_panel_prompt(
                service_prompt=service_prompt,
                storyboard_item=item,
                panel=panel,
                include_context=include_context,
            )
            out_name = make_filename(source_id, panel_index, label, combined_prompt)
            out_path = output_dir / out_name

            if out_path.exists() and not overwrite:
                record = {
                    "source_id": source_id,
                    "panel_index": panel_index,
                    "status": "skipped_exists",
                    "model": model,
                    "provider": provider,
                    "input_json": str(input_json.resolve()),
                    "output_file": str(out_path.resolve()),
                    "output_file_relative": str(out_path),
                    "filename": out_name,
                    "original_text": item.get("original_text"),
                    "simplified_text": item.get("simplified_text"),
                    "intent_type": item.get("intent_type"),
                    "panel": panel,
                    "combined_prompt": combined_prompt,
                }
                append_manifest(manifest_path, record)
                continue

            kwargs: Dict[str, Any] = {
                "model": model,
            }

            parameters: Dict[str, Any] = {}
            if width is not None:
                parameters["width"] = width
            if height is not None:
                parameters["height"] = height
            if guidance_scale is not None:
                parameters["guidance_scale"] = guidance_scale
            if num_inference_steps is not None:
                parameters["num_inference_steps"] = num_inference_steps

            kwargs.update(parameters)

            try:
                image = client.text_to_image(combined_prompt, **kwargs)
                save_image(image, out_path)

                record = {
                    "source_id": source_id,
                    "panel_index": panel_index,
                    "status": "ok",
                    "model": model,
                    "provider": provider,
                    "input_json": str(input_json.resolve()),
                    "output_file": str(out_path.resolve()),
                    "output_file_relative": str(out_path),
                    "filename": out_name,
                    "original_text": item.get("original_text"),
                    "simplified_text": item.get("simplified_text"),
                    "intent_type": item.get("intent_type"),
                    "summary_visual_strategy": item.get("summary_visual_strategy"),
                    "panel": panel,
                    "combined_prompt": combined_prompt,
                    "parameters": parameters,
                }
                append_manifest(manifest_path, record)

            except Exception as e:
                record = {
                    "source_id": source_id,
                    "panel_index": panel_index,
                    "status": "error",
                    "model": model,
                    "provider": provider,
                    "input_json": str(input_json.resolve()),
                    "output_file": str(out_path.resolve()),
                    "output_file_relative": str(out_path),
                    "filename": out_name,
                    "original_text": item.get("original_text"),
                    "simplified_text": item.get("simplified_text"),
                    "intent_type": item.get("intent_type"),
                    "summary_visual_strategy": item.get("summary_visual_strategy"),
                    "panel": panel,
                    "combined_prompt": combined_prompt,
                    "parameters": parameters,
                    "error": str(e),
                }
                append_manifest(manifest_path, record)

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    return manifest_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate one image per storyboard panel from storyboards.json using Hugging Face routed inference."
    )
    parser.add_argument("--service-prompt-file", type=Path, help="Text file containing the global image-generation service prompt.")
    parser.add_argument("--service-prompt", type=str, default="", help="Global service prompt string.")
    parser.add_argument("--input-json", type=Path, required=True, help="Storyboard JSON file containing items[].panels[].")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to save PNG files and manifest.json.")
    parser.add_argument("--model", type=str, default="black-forest-labs/FLUX.1-schnell", help="HF model id.")
    parser.add_argument("--provider", type=str, default="auto", help="Inference provider, e.g. auto, fal-ai, replicate.")
    parser.add_argument("--api-key", type=str, default=os.environ.get("HF_TOKEN", ""), help="HF token or set HF_TOKEN.")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--guidance-scale", type=float, default=None)
    parser.add_argument("--num-inference-steps", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Delay between requests.")
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Do not include storyboard-level context like simplified_text and intent_type in each panel prompt.",
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
        include_context=not args.no_context,
    )

    print(str(manifest_path.resolve()))


if __name__ == "__main__":
    main()