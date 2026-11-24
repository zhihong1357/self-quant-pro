"""Append-only JSONL ledgers with a SHA-256 hash chain.

Every line is one canonical JSON object (sorted keys, no spaces, UTF-8) with:
  seq   1, 2, 3, ...
  prev  hash of the previous record (64 zeros for the first)
  at    UTC timestamp, ISO 8601
  hash  sha256 of the record without its own "hash" key
Editing any old line breaks the chain; CI also checks byte-for-byte that the base
version of the file is a prefix of the new one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

GENESIS = "0" * 64
RESERVED = ("seq", "prev", "hash")


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def record_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def verify(text: str) -> list[str]:
    """Problems found in a ledger, in Chinese; an empty list means the chain is intact."""
    if not text:
        return []
    if not text.endswith("\n"):
        return ["文件必须以换行结尾"]
    problems: list[str] = []
    prev = GENESIS
    for number, line in enumerate(text[:-1].split("\n"), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return [*problems, f"第 {number} 行不是合法的 JSON（{exc.msg}）"]
        if not isinstance(record, dict):
            return [*problems, f"第 {number} 行必须是 JSON 对象"]
        if line.encode() != canonical(record):
            problems.append(f"第 {number} 行不是规范格式（键要排序、不留空格）")
        if record.get("seq") != number:
            problems.append(f"第 {number} 行的 seq 应为 {number}")
        if record.get("prev") != prev:
            problems.append(f"第 {number} 行的 prev 和上一条的 hash 对不上")
        if not isinstance(record.get("at"), str):
            problems.append(f"第 {number} 行缺少时间 at")
        if record.get("hash") != record_hash(record):
            problems.append(f"第 {number} 行的 hash 和内容对不上")
        prev = record.get("hash", "")
    return problems


def build(text: str, payload: dict, now: str | None = None) -> dict:
    clash = [k for k in RESERVED if k in payload]
    if clash:
        raise ValueError(f"这些字段由账本自动生成，不能手写：{', '.join(clash)}")
    lines = text.splitlines() if text else []
    prev = json.loads(lines[-1])["hash"] if lines else GENESIS
    record = {"seq": len(lines) + 1, "prev": prev, **payload}
    if "at" not in record:
        stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
        record["at"] = stamp.replace("+00:00", "Z")
    record["hash"] = record_hash(record)
    return record


def append(path: Path, payload: dict) -> dict:
    text = path.read_text("utf-8") if path.exists() else ""
    problems = verify(text)
    if problems:
        raise ValueError("账本已损坏：" + "；".join(problems))
    record = build(text, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical(record).decode() + "\n")
    return record
