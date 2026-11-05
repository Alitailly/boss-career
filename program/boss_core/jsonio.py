"""JSON 读写与指纹：原子替换、稳定序列化、内容哈希。

所有用户数据文件都经这里落盘，避免半截文件被下游读到。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_MISSING = object()


def read_json(path: Path, default: Any = _MISSING) -> Any:
    """读 JSON 对象；缺文件返回 `default`，坏文件抛 `ValueError`（不静默吞掉）。"""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    except OSError as exc:
        raise ValueError(f"读不到 {path.name}：{exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{path.name} 不是合法 JSON：{exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 JSONL；空行跳过，坏行抛 `ValueError` 并带上行号。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="strict") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path.name} 第 {lineno} 行坏了：{exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path.name} 第 {lineno} 行不是对象")
            rows.append(item)
    return rows


def write_json(path: Path, value: Any) -> None:
    """原子写：先写同目录临时文件再 `os.replace`，读者永远看到完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """追加一行；只追加不改写，用于台账与会话回合。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def canonical(value: Any) -> str:
    """稳定序列化：键排序、不转义中文，用于计算指纹。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """`value` 的短指纹（sha256 前 16 位）。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


def digest_text(text: str) -> str:
    """文本全文 sha256（用于简历原文、招呼语正文）。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    """`prefix` + 12 位随机十六进制；不依赖时间，便于测试。"""
    return f"{prefix}_{os.urandom(6).hex()}"


def now_iso() -> str:
    """本地时区的 ISO 时间（秒精度），写进版本记录供人核对。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
