"""After hang-up: an owner-facing summary plus quality flags, on a stronger model where speed doesn't matter."""
from __future__ import annotations

import json

import anthropic

from . import prompts

FLAG_TYPES = ["made_up_fact", "price_quoted", "denied_being_ai", "took_card_details", "unsafe_advice",
              "ignored_request_for_person", "rude_or_robotic", "other"]

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two sentences for the business owner."},
        "caller_name": {"type": "string"},
        "callback_number": {"type": "string"},
        "intent": {"type": "string", "enum": ["new_job", "existing_appointment", "question", "emergency",
                                               "billing", "spam", "other"]},
        "outcome": {"type": "string", "enum": ["booked", "transferred", "lead", "message", "info_only",
                                                "spam", "abandoned"]},
        "urgency": {"type": "string", "enum": ["emergency", "this_week", "flexible", "none"]},
        "follow_up": {"type": "string", "description": "What the office should do next, or an empty string."},
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": FLAG_TYPES},
                    "quote": {"type": "string", "description": "The agent's words that caused the flag."},
                    "explanation": {"type": "string"},
                },
                "required": ["type", "quote", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "caller_name", "callback_number", "intent", "outcome", "urgency", "follow_up", "flags"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You review phone calls handled by the virtual receptionist of a small business. First comes what the agent was
given: its instructions, the playbook with its approved safety lines, the business facts and, when available, this
call's details. Then the transcript, with tool calls in brackets. Fill in the JSON fields.

Flag only real problems in what the agent said or did:
- made_up_fact: a statement about the business, prices, availability or policy that the facts and tool results don't support.
- price_quoted: any price other than those listed in the business facts.
- denied_being_ai, took_card_details, unsafe_advice (advice on gas, electrical work beyond the approved tips,
  opening appliances, tools or chemicals, or a diagnosis of the cause),
  ignored_request_for_person, rude_or_robotic, other.
Use empty strings for unknown names or numbers. An empty flags list is the normal case."""


def summarize(client: anthropic.Anthropic, cfg: dict, biz: dict, transcript: str, call_state: str | None = None,
              usage: dict | None = None) -> dict:
    """Pass a `usage` dict to have this call's token counts added to it (for cost tracking)."""
    m = cfg["models"]
    resp = client.messages.create(
        model=m["postcall"],
        max_tokens=4096,
        output_config={"effort": m["postcall_effort"], "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        # The business's instructions are identical on every call, so they're cached; only the call itself is new.
        system=[{"type": "text", "text": INSTRUCTIONS},
                {"type": "text", "text": f"<agent_was_given>\n{prompts.stable_prompt(biz)}\n</agent_was_given>",
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"{call_state or ''}\n\n# Transcript\n{transcript}".strip()}],
    )
    if usage is not None:
        usage["input"] = usage.get("input", 0) + (resp.usage.input_tokens or 0)
        usage["output"] = usage.get("output", 0) + (resp.usage.output_tokens or 0)
        usage["cache_read"] = usage.get("cache_read", 0) + (getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
        usage["cache_write"] = usage.get("cache_write", 0) + (getattr(resp.usage, "cache_creation_input_tokens", 0) or 0)
    if resp.stop_reason == "refusal":
        return {"summary": "Summary unavailable (the review model declined).", "outcome": None, "flags": []}
    return json.loads(next(b.text for b in resp.content if b.type == "text"))
