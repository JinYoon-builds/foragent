"""Reading of check-run reports that CI may compress to fit GitHub's 65,535-byte output limit."""

import base64
import json
import zlib


def decode_report(text):
    value = json.loads(text)
    if isinstance(value, dict) and value.get("encoding") == "zlib+base64":
        payload = json.loads(zlib.decompress(base64.b64decode(value["data"], validate=True)))
        if not isinstance(payload, dict) or payload.get("excluded") != value.get("excluded"):
            raise ValueError("compressed report exclusions do not match")
        return payload
    return value
