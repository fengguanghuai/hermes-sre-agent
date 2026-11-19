"""Zero-dependency YAML/JSON loader for declarative policy files.

Uses PyYAML (`yaml.safe_load`) when installed; otherwise falls back to a strict
stdlib parser supporting the YAML subset used by policy files (nested mappings,
sequences of scalars/mappings, inline comments, booleans, ints, floats, strings,
and empty `[]`/`{}`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_structured_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected YAML mapping in {path}")
        return data
    except ImportError:
        parsed = parse_simple_yaml(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected YAML mapping in {path}")
        return parsed


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _parse_scalar(text: str) -> Any:
    val = text.strip()
    if val == "[]":
        return []
    if val == "{}":
        return {}
    if val in ("null", "Null", "NULL", "~", ""):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        if val.startswith('"'):
            return json.loads(val)
        return val[1:-1]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _split_key_value(text: str) -> tuple[str, str]:
    in_single = False
    in_double = False
    for idx, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            if idx + 1 == len(text) or text[idx + 1].isspace():
                key = text[:idx].strip()
                if (key.startswith('"') and key.endswith('"')) or (
                    key.startswith("'") and key.endswith("'")
                ):
                    key = key[1:-1]
                return key, text[idx + 1 :].strip()
    raise ValueError(f"Invalid YAML mapping line: {text!r}")


def parse_simple_yaml(raw: str) -> Any:
    tokens: list[tuple[int, str]] = []
    for line in raw.splitlines():
        cleaned = _strip_comment(line)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        tokens.append((indent, cleaned.lstrip(" ")))

    if not tokens:
        return {}

    def parse_block(index: int, min_indent: int) -> tuple[Any, int]:
        indent, content = tokens[index]
        if content.startswith("- "):
            return parse_list(index, indent)
        return parse_map(index, indent)

    def parse_list(index: int, list_indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(tokens):
            indent, content = tokens[index]
            if indent < list_indent or (indent == list_indent and not content.startswith("- ")):
                break
            if indent > list_indent:
                raise ValueError(f"Unexpected indentation in list at: {content!r}")
            item_text = content[2:].strip()
            index += 1
            if not item_text:
                if index < len(tokens) and tokens[index][0] > list_indent:
                    nested, index = parse_block(index, tokens[index][0])
                    items.append(nested)
                else:
                    items.append(None)
                continue

            if ":" in item_text and not (
                item_text.startswith('"') or item_text.startswith("'")
            ):
                try:
                    k, v = _split_key_value(item_text)
                    entry: dict[str, Any] = {}
                    if v:
                        entry[k] = _parse_scalar(v)
                    elif index < len(tokens) and tokens[index][0] > list_indent:
                        entry[k], index = parse_block(index, tokens[index][0])
                    else:
                        entry[k] = None
                    if index < len(tokens) and tokens[index][0] > list_indent:
                        sub_indent = tokens[index][0]
                        sub_map, index = parse_map(index, sub_indent)
                        entry.update(sub_map)
                    items.append(entry)
                    continue
                except ValueError:
                    pass
            items.append(_parse_scalar(item_text))
        return items, index

    def parse_map(index: int, map_indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(tokens):
            indent, content = tokens[index]
            if indent < map_indent:
                break
            if indent > map_indent:
                raise ValueError(f"Unexpected indentation in mapping at: {content!r}")
            if content.startswith("- "):
                break
            key, val_text = _split_key_value(content)
            index += 1
            if val_text:
                mapping[key] = _parse_scalar(val_text)
            elif index < len(tokens) and tokens[index][0] > map_indent:
                child, index = parse_block(index, tokens[index][0])
                mapping[key] = child
            else:
                mapping[key] = None
        return mapping, index

    result, _ = parse_block(0, tokens[0][0])
    return result
