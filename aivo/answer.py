"""Validate final reply objects before they reach the UI, speech, or history."""


def parse_final_answer(raw):
    """Accept only a complete final-response object, before any text is emitted."""
    import json

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate response key")
            result[key] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw) > 12000:
            raise ValueError("Invalid response size or type")
        data = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or set(data) != {"reply"}:
            raise ValueError("Expected only a reply field")
        reply = data["reply"]
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("Expected a nonempty reply string")
        lowered = reply.lower()
        if any(marker in lowered for marker in ("<think", "</think", "<analysis", "</analysis", "<|im_", "<|endoftext|>")):
            raise ValueError("Reasoning or chat control tags in final reply")
        return reply.strip()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Model returned an invalid final response; no answer was emitted") from exc
