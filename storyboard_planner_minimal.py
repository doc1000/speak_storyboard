#!/usr/bin/env python3
"""
Minimal storyboard planner for caregiver visual communication demos.

Purpose:
- Convert caregiver utterances into 1-4 reusable semantic image cards.
- Keep JSON small.
- Produce image prompts that can be passed directly to gen_image.py.
- Avoid metadata-heavy wording that encourages captions/posters in image models.
"""

import os
import json
import time
import argparse
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field
from openai import OpenAI


class ImageCard(BaseModel):
    card_index: int = Field(..., ge=1, le=4)
    scene_label: str = Field(..., max_length=60)
    reuse_key: str = Field(..., max_length=80)
    prompt: str = Field(..., max_length=500)
    


class StoryboardItem(BaseModel):
    source_id: str
    original_text: str
    simplified_message: str = Field(..., max_length=180)
    cards: List[ImageCard] = Field(..., min_length=1, max_length=4)


class StoryboardBatch(BaseModel):
    items: List[StoryboardItem]


DEFAULT_SERVICE_PROMPT = """
You create simple image-card plans for caregiver communication with an older adult.

Convert each caregiver utterance into 1 to 4 reusable visual image cards.
clearly consider the steps needed to communicate the message.  
While the people in the image are important, the key actions or objects are more important.

When there are open ended questions, consider offering options if there are enough cards available.  For example, if asked "what would you like for lunch?", you may show gramma eating lunch, THEN show two lunch options.
When there is a sequence of events, try to identify the key transitions and place in order.  For example, if caretaker says "I will be back here after I run to the store", you cam show the caretaker leaving, then her at the store, then back with gramma.

The output is for an image generator, so describe the scene to be capture core concept is being communicated.
Do not get too wordy, just describe the scene to be captured - make sure the key actions or objects are identified in the prompt.
The output can be cartoonish, but still capture the core concept.

Rules:
- One card = one standalone image scene.
- Do not create comic strips, panels, captions, posters, infographics, or multi-scene images.
- Prefer concrete home-care moments: food, medicine, chair, doorway, caregiver, patient, kitchen, bathroom, visitor, car, checkbook.
- Use 2 to 3 cards when possible. Use 4 only when the message truly needs it.
- Each card should show one clear action, object, choice, or state.
- No image should depend on written text.
- Avoid all readable text, labels, signs, handwriting, speech bubbles, logos, numbers, UI, arrows, question marks, or captions inside images.
- If a check, paper, phone, pill bottle, calendar, or package appears, it must be blank with no visible writing.
- Use physical gesture, facial expression, gaze direction, and object placement instead of symbols.
- Style should be warm, clean, emotionally readable, elder-care focused.

For each card:
- scene_label: short internal label only, not intended to appear in image.
- reuse_key: lowercase semantic key using snake_case.
- prompt: a single clean diffusion-friendly prompt. No headings. No metadata labels.
- avoid: short list of visual failures to avoid.

Prompt style:
Use phrases like:
"warm clean elder-care illustration, elderly grandmother with gray hair, caring middle-aged caregiver, ..."

Every prompt must include:
- single standalone image
- simple home setting
- warm clean elder-care illustration
- no text
- no captions
- no labels
- minimal clutter

Output only schema-conforming JSON.
""".strip()


def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "items" in data:
        items = data["items"]
    else:
        raise ValueError("Input must be a list or an object with an 'items' key.")

    normalized = []
    for idx, item in enumerate(items, start=1):
        if isinstance(item, str):
            normalized.append({"source_id": str(idx), "text": item})
        elif isinstance(item, dict):
            source_id = str(item.get("source_id") or item.get("id") or idx)
            text = item.get("text") or item.get("question") or item.get("utterance") or item.get("prompt")
            if not text:
                raise ValueError(f"Missing text in item {idx}")
            normalized.append({"source_id": source_id, "text": text})
        else:
            raise ValueError(f"Unsupported item type at index {idx}: {type(item)}")
    return normalized


def build_user_payload(items: list[dict]) -> str:
    return json.dumps({"items": items}, ensure_ascii=False, indent=2)


def call_openai_structured(
    client: OpenAI,
    model: str,
    service_prompt: str,
    items: list[dict],
    temperature: Optional[float] = None,
) -> StoryboardBatch:
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": service_prompt},
            {
                "role": "user",
                "content": (
                    "Create minimal image-card plans. Preserve source_id and original_text. "
                    "Return compact JSON only.\n\n"
                    + build_user_payload(items)
                ),
            },
        ],
        response_format=StoryboardBatch,
        **({"temperature": temperature} if temperature is not None else {}),
    )
    return completion.choices[0].message.parsed


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def batch_questions(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def main():
    parser = argparse.ArgumentParser(
        description="Create minimal reusable image-card plans from caregiver utterances."
    )
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--service-prompt-file", type=Path, default=None)
    parser.add_argument("--model", type=str, default="gpt-4.1")
    parser.add_argument("--api-key", type=str, default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Pass --api-key or set OPENAI_API_KEY.")

    service_prompt = DEFAULT_SERVICE_PROMPT
    if args.service_prompt_file:
        service_prompt = args.service_prompt_file.read_text(encoding="utf-8").strip()

    items = load_questions(args.input_json)
    client = OpenAI(api_key=args.api_key)

    all_items = []
    for batch_index, chunk in enumerate(batch_questions(items, args.batch_size), start=1):
        result = call_openai_structured(
            client=client,
            model=args.model,
            service_prompt=service_prompt,
            items=chunk,
            temperature=args.temperature,
        )
        all_items.extend(item.model_dump(mode="json") for item in result.items)

        if args.pause_seconds > 0 and batch_index * args.batch_size < len(items):
            time.sleep(args.pause_seconds)

    output = {
        "meta": {
            "model": args.model,
            "generated_at": int(time.time()),
            "input_json": str(args.input_json.resolve()),
            "item_count": len(all_items),
            "schema": "minimal_image_cards_v1",
        },
        "items": all_items,
    }

    save_json(args.output_json, output)
    print(str(args.output_json.resolve()))


if __name__ == "__main__":
    main()
