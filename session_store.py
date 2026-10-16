"""会话快照的存储层 —— 与 Streamlit 无关的纯文件逻辑。

抽成独立模块有两个理由：

1. **可测**。`app.py` 在 import 阶段就调用 `st.set_page_config`，没法在
   unittest 里导入；而快照的读写本身完全不需要 Streamlit。分开之后，
   这部分逻辑可以用 `py -3.12 -m unittest test_session_store` 离线验证。

2. **多标签页不再互相覆盖**。原先所有会话共用一个
   `.session_snapshot.json`：开第二个标签页面试，就会把第一个标签页的
   快照顶掉，刷新回来恢复的是别人那一场。现在改成**一场一个文件**
   （`.snapshots/<会话id>.json`），首页再从所有残留场次里挑**最近保存的**
   那个来提示"继续上次面试"——既不丢恢复能力，也不再互相踩。

兼容性：旧版的单文件 `.session_snapshot.json` 仍会被扫描到（只读），
所以升级前留下的快照不会失效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SNAPSHOT_DIR_NAME = ".snapshots"
LEGACY_SNAPSHOT_NAME = ".session_snapshot.json"
SESSION_KEY = "_session_id"


def snapshot_dir(base: Path) -> Path:
    return base / SNAPSHOT_DIR_NAME


def snapshot_path(base: Path, session_id: str) -> Path:
    """某个会话的快照文件路径。

    会话 id 会被当成文件名，所以只保留安全字符——它由我们自己生成，
    但这是最后一道防线：脏字符不能让路径跑出快照目录。
    """
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")
    if not safe:
        safe = "default"
    return snapshot_dir(base) / f"{safe}.json"


def atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 os.replace 覆盖目标。

    直接 write_text 一旦写到一半崩掉（断电 / 进程被杀 / 磁盘满），
    留下的是半截 JSON，下次读会抛 JSONDecodeError，整场记录就没了。
    os.replace 在同一分区上是原子的：读到的要么是旧内容、要么是新内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_snapshot(base: Path, session_id: str, data: dict[str, Any]) -> None:
    """把一场进行中的面试写进它自己的快照文件。"""
    atomic_write_text(
        snapshot_path(base, session_id), json.dumps(data, ensure_ascii=False, indent=2)
    )


def read_snapshot_file(path: Path) -> dict[str, Any] | None:
    """读单个快照文件；不存在、损坏、或结构不对都返回 None。

    续面只是锦上添花，任何异常都只能返回 None，不能挡住正常使用。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("profile"):
        return None
    return data


def _candidate_files(base: Path) -> list[Path]:
    """所有可能是快照的文件：新目录里的 + 旧版单文件。"""
    files: list[Path] = []
    directory = snapshot_dir(base)
    if directory.is_dir():
        files.extend(sorted(directory.glob("*.json")))
    legacy = base / LEGACY_SNAPSHOT_NAME
    if legacy.is_file():
        files.append(legacy)
    return files


def latest_snapshot(base: Path, exclude_session: str = "") -> dict[str, Any] | None:
    """返回最近保存的那一场未完成面试（附 `_session_id`）；没有则 None。

    exclude_session 用来跳过"自己"：正在面试的那个会话，不该在首页看到自己。
    """
    best: dict[str, Any] | None = None
    best_key: tuple[str, float] | None = None

    for path in _candidate_files(base):
        if exclude_session and path.stem == exclude_session:
            continue
        data = read_snapshot_file(path)
        if data is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        # saved_at 是 ISO 字符串，字典序即时间序；mtime 用作并列时的兜底
        key = (str(data.get("saved_at") or ""), mtime)
        if best_key is None or key > best_key:
            best_key = key
            best = {**data, SESSION_KEY: path.stem}
    return best


def clear_snapshot(base: Path, session_id: str) -> None:
    """删掉某个会话自己的快照（正常结束、重新开始、或用户主动放弃）。

    旧版单文件快照的 stem 是 `.session_snapshot`，经 sanitise 后会变成
    `session_snapshot`、指不到真正的文件——所以这里显式补上旧文件，
    否则用户点了"放弃"，横幅下一轮又会冒出来。
    """
    targets = [snapshot_path(base, session_id)]
    if str(session_id) in (LEGACY_SNAPSHOT_NAME, Path(LEGACY_SNAPSHOT_NAME).stem):
        targets.append(base / LEGACY_SNAPSHOT_NAME)
    for path in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def clear_all(base: Path) -> None:
    """清掉所有快照，含旧版单文件。只在"放弃并重新开始"时用。"""
    for path in _candidate_files(base):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
