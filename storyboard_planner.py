#!/usr/bin/env python3
import os
import json
import time
import argparse
from pathlib import Path
from typing import List, Optional, Literal

from pydantic import BaseModel, Field
from openai import OpenAI


class StoryboardPanel(BaseModel):
    panel_index: int = Field(..., ge=1, le=4)
    role: Literal[
        "context",
        "person",
        "choice",
        "action",
        "location",
        "object",
        "reassurance",
        "confirmation"
    ]
    semantic_tags: List[str] = Field(default_factory=list, max_length=8)
    reuse_key: str = Field(..., max_length=80)
    scene_label: str = Field(..., max_length=80)
    visual_elements: List[str] = Field(..., min_length=1, max_length=8)
    composition_hint: str = Field(..., max_length=200)
    prompt_fragment: str = Field(..., max_length=500)
    avoid: List[str] = Field(default_factory=list, max_length=8)
    


class StoryboardItem(BaseModel):
    source_id: str
    original_text: str
    simplified_text: str
    intent_type: Literal[
        "choice_question",
        "yes_no_question",
        "reassurance_status",
        "instruction_transition",
        "time_event",
        "statement",
        "other"
    ]
    is_question: bool
    question_marker_at_end: bool
    panel_count: int = Field(..., ge=1, le=4)
    summary_visual_strategy: str = Field(..., max_length=240)
    panels: List[StoryboardPanel] = Field(..., min_length=1, max_length=4)
    notes_for_caregiver: List[str] = Field(default_factory=list, max_length=5)


class StoryboardBatch(BaseModel):
    items: List[StoryboardItem]


DEFAULT_SERVICE_PROMPT = """
You are designing visual communication storyboard plans for an older adult patient who responds better to simple pictures than text.

Your job is to convert caregiver questions or statements into a small sequence of up to 4 storyboard panels.

Core rules:
- Prefer concrete, familiar visuals over abstraction.
- Reuse a limited visual grammar: person, object, place, arrow, clock/time cue, return cue, question cue.
- Keep each panel visually simple and uncluttered.
- Do not rely on written text inside the image.
- Avoid symbolism unless it is extremely common and visually obvious.
- If the utterance is a choice question, show the person/context first, then one option per panel, and optionally end with a question cue.
- If the utterance describes a sequence like leaving and returning, break it into clear left-to-right steps.
- Use no more than 4 panels.
- Use panel roles consistently.
- If a big question mark would help, set question_marker_at_end=true and include a final question_cue panel only if there is room and it adds clarity.
- Simplify language so a caregiver could also say it aloud in short, clear speech.
- Favor patient-centered scenes, familiar home settings, and large obvious objects.
- For food questions, depict specific foods clearly and separately.
- For time/event cues, prefer simple visual references like a clock, workplace, sun, meal table, or return-to-home cue.
- Avoid visually dense scenes, multiple unrelated objects, tiny details, text labels, or hard-to-interpret metaphors.

Panel guidance:
- scene_label: brief human-readable description.
- visual_elements: noun phrases only.
- composition_hint: one sentence on layout/composition.
- prompt_fragment: concise image-generation phrase for that panel.
- avoid: things that would make the panel confusing.

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
            normalized.append({**item, "source_id": source_id, "text": text})
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
                    "Convert these caregiver utterances into storyboard plans. "
                    "Return one item per input item, preserving source_id and original_text.\n\n"
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
    parser = argparse.ArgumentParser(description="Create storyboard JSON plans from caregiver questions/statements using OpenAI structured outputs.")
    parser.add_argument("--input-json", type=Path, required=True, help="Input list of questions/statements.")
    parser.add_argument("--output-json", type=Path, required=True, help="Where to save storyboard JSON.")
    parser.add_argument("--service-prompt-file", type=Path, default=None, help="Optional file overriding the default service prompt.")
    parser.add_argument("--model", type=str, default="gpt-4o-mini", help="OpenAI model supporting structured outputs.")
    parser.add_argument("--api-key", type=str, default=os.environ.get("OPENAI_API_KEY", ""), help="OpenAI API key or set OPENAI_API_KEY.")
    parser.add_argument("--batch-size", type=int, default=20, help="Number of utterances per API call.")
    parser.add_argument("--temperature", type=float, default=None, help="Optional temperature.")
    parser.add_argument("--pause-seconds", type=float, default=0.0, help="Optional pause between batches.")
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
        },
        "service_prompt": service_prompt,
        "items": all_items,
    }
    save_json(args.output_json, output)
    print(str(args.output_json.resolve()))


if __name__ == "__main__":
    main()
