"""Lossless check-run transport within GitHub's 65,535-byte output limit."""

import base64
import json
import zlib

LIMIT = 65535


def encode_report(payload):
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(text.encode()) > LIMIT:
        text = json.dumps({"encoding": "zlib+base64", "excluded": payload["excluded"],
                           "data": base64.b64encode(zlib.compress(text.encode())).decode()}, separators=(",", ":"))
    if len(text.encode()) > LIMIT:
        raise ValueError("Acceptance shard report exceeds GitHub's check-run limit; increase ci_shards and regenerate")
    return text


def decode_report(text):
    value = json.loads(text)
    if isinstance(value, dict) and value.get("encoding") == "zlib+base64":
        payload = json.loads(zlib.decompress(base64.b64decode(value["data"], validate=True)))
        if not isinstance(payload, dict) or payload.get("excluded") != value.get("excluded"):
            raise ValueError("compressed report exclusions do not match")
        return payload
    return value
