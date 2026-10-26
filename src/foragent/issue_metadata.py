"""Read and render the versioned body's optional YAML path reservations."""

import yaml


def exclusive_paths(body: str) -> list[str]:
    marker = "<!-- foragent:v1 -->"
    if marker not in body:
        return []
    header = body.split(marker, 1)[1].split("\n## ", 1)[0]
    try:
        metadata = yaml.safe_load(header)
    except yaml.YAMLError:
        return []
    paths = metadata.get("exclusive", []) if isinstance(metadata, dict) else []
    return paths if isinstance(paths, list) and all(isinstance(path, str) for path in paths) else []


def exclusive_header(paths: list[str]) -> list[str]:
    return yaml.safe_dump({"exclusive": paths}, sort_keys=False).splitlines() if paths else []
