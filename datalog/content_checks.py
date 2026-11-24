"""Deterministic, local scoring for authored output rubrics."""

import re
import unicodedata

from datalog.validation import Invalid, text


def tokens(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c))))


def rules(body):
    result = {}
    for name in ("required", "forbidden"):
        phrases = body.get(name, [])
        if not isinstance(phrases, list) or len(phrases) > 20:
            raise Invalid("Provide up to 20 phrases per group")
        result[name] = list(dict.fromkeys(tokens(text(phrase, "phrase", limit=160)) for phrase in phrases))
        if "" in result[name]:
            raise Invalid("The phrase must contain words or numbers")
    if not any(result.values()) or set(result["required"]) & set(result["forbidden"]):
        raise Invalid("Provide content rules without contradictory phrases")
    return result


def value(output, config):
    if isinstance(output, list):
        parts = []
        for message in output:
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("content"), str):
                parts.append(message["content"])
            for part in message.get("parts", []) if isinstance(message.get("parts"), list) else []:
                if isinstance(part, dict) and isinstance(part.get("content"), str):
                    parts.append(part["content"])
        output = " ".join(parts)
    if not isinstance(output, str) or not output.strip():
        return None
    normalized = " " + tokens(output) + " "

    def includes(phrase):
        return f" {phrase} " in normalized

    return float(all(map(includes, config["required"])) and not any(map(includes, config["forbidden"])))
