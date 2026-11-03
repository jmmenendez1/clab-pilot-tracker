"""Offline check of the request stage 3 sends to the Messages API and of how it reads the answer.

Run from retro/:  python tests/test_stage3_api_shape.py
No key and no network needed: the Anthropic client is replaced by a fake that validates the request.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage3_screen_unreviewed as s3  # noqa: E402

FORBIDDEN = {"minimum", "maximum", "maxItems", "minLength", "maxLength"}


def walk(schema):
    if isinstance(schema, dict):
        assert not (FORBIDDEN & schema.keys()), f"unsupported keyword in {schema}"
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, f"object without additionalProperties=false: {schema}"
        for v in schema.values():
            walk(v)
    elif isinstance(schema, list):
        for v in schema:
            walk(v)


class FakeMessages:
    def __init__(self, answer):
        self.answer, self.last = answer, None

    def create(self, **kw):
        self.last = kw
        assert "tools" not in kw and "tool_choice" not in kw, "forced tool calls are rejected by Sonnet 5.5"
        assert kw["output_config"]["format"]["type"] == "json_schema"
        walk(kw["output_config"]["format"]["schema"])
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        content = [SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=json.dumps(self.answer))]
        return SimpleNamespace(content=content, usage=usage, stop_reason="end_turn")

    def stream(self, **kw):
        return FakeStream(self.create(**kw))


class FakeStream:
    """Stands in for the SDK's MessageStreamManager: context manager with get_final_message()."""
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def main():
    for tool in (s3.PROFILE_TOOL, s3.SCREEN_TOOL):
        walk(s3.strict_schema(tool["input_schema"]))

    m = s3.Model("claude-sonnet-5-5", mock=True)
    m.mock = False
    answer = {"document_subject": "x", "verdict": "belongs_to_validated_pilot", "rationale": "y",
              "matches": [{"pilot_id": "CLAB-P-001", "role": "primary", "confidence": 1.7, "evidence_quote": "q", "evidence_location": "page 1"}]}
    m.client = SimpleNamespace(messages=FakeMessages(answer))
    out, usage = m.call("system", "user", s3.SCREEN_TOOL)
    assert out["matches"][0]["confidence"] == 1.0, "confidence must be clamped to [0, 1]"
    assert usage["input_tokens"] == 10 and m.usage["calls"] == 1
    sent = m.client.messages.last
    assert sent["output_config"]["effort"] == "medium" and sent["max_tokens"] == 16000
    print("ok: request shape, schema, answer parsing, clamping")


if __name__ == "__main__":
    main()
