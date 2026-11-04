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

import contextlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SNAPSHOT_DIR_NAME = ".snapshots"
LEGACY_SNAPSHOT_NAME = ".session_snapshot.json"
SESSION_KEY = "_session_id"

# 本机访问时客户端 IP 的几种形态。
# 加账号体系之前，快照与历史记录的 `owner` 就是这些值（或干脆没有），
# 判断"这条数据是不是登录功能之前的本机产物"要靠它。
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def is_local_owner(value: str) -> bool:
    """这个 owner 标记是否属于"加登录之前的本机来源"（空 或 loopback）。"""
    text = str(value or "").strip()
    return not text or text in LOOPBACK

# 跨线程 / 跨进程的写锁超时。正常人不会在前一个人写盘的同时点保存，
# 5 秒都拿不到锁说明有别的进程卡死了，宁可报错也不要把界面永久挂住。
LOCK_TIMEOUT = 5.0
_LOCK_POLL = 0.02


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

    临时文件名**必须每个写入者各不相同**。原先固定叫 `<目标>.tmp`，
    多人同时用同一份 history.json 时，后到的写入会撞上别人正打开的临时
    文件（Windows 上直接 PermissionError），实测 40 个并发 append 丢了 39 条。
    加上 pid + 线程 id + 随机串之后，各写各的临时文件，replace 才是纯原子操作。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        tmp.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp, path)
    finally:
        # replace 成功时 tmp 已经不在了；失败时由这里清掉，不留垃圾。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_with_retry(src: Path, dst: Path, attempts: int = 25) -> None:
    """os.replace，遇 Windows 的瞬时占用错误就重试。

    目标文件正被另一个线程读（Scan 病毒 / 索引器 / 并发的 load_records）
    时，Windows 会拒绝替换并抛 PermissionError。这属于"等一会儿就好"，
    不是真的没权限，所以短暂重试而不是把异常丢给界面。
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(_LOCK_POLL)


# ---------------------------------------------------------------------------
# 跨线程 / 跨进程写锁
# ---------------------------------------------------------------------------

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


def _thread_lock(key: str) -> threading.RLock:
    """同一个目标路径共用一个可重入锁（进程内）。"""
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


def _os_lock(fd: int, timeout: float) -> None:
    """对已打开的 fd 加排他锁，拿不到就轮询直到超时。

    Windows 与 POSIX 的语义不同，但都够用来串行化"读-改-写"：
    我们只在几毫秒内持有它，不需要公平性。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待文件写锁超时，可能有另一个进程卡住了")
            time.sleep(_LOCK_POLL)


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """锁住某个数据文件的"读-改-写"整段。

    两道锁缺一不可：进程内的 RLock 管同进程多会话（Streamlit 一个进程跑
    所有标签页，这是最常见的并发来源），文件锁管用户开了两个启动器、
    跑起两个 Streamlit 进程的情况。只加文件锁的话，同进程内还要为
    每个 fd 的锁互相等待，白白多出超时风险。

    用法：
        with file_lock(history_path):
            records = load_records(history_path)
            ...改 records...
            _write_records(history_path, records)
    """
    key = str(path.resolve()).lower()
    tlock = _thread_lock(key)
    if not tlock.acquire(timeout=timeout):
        raise TimeoutError(f"等待 {path.name} 的写锁超时")

    fd: int | None = None
    try:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        _os_lock(fd, timeout)
        try:
            yield
        finally:
            if fd is not None:
                _os_unlock(fd)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        tlock.release()


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


def latest_snapshot(
    base: Path, exclude_session: str = "", owner: str = ""
) -> dict[str, Any] | None:
    """返回最近保存的那一场未完成面试（附 `_session_id`）；没有则 None。

    exclude_session 用来跳过"自己"：正在面试的那个会话，不该在首页看到自己。

    owner 用来隔离**不同的人**。局域网分享出去之后，多个浏览器同时连这台
    机器，各自都有快照文件；只按时间挑最近的，就会让 A 看到 B 那场没做完的
    面试、点"继续"直接接手别人的对话。传 owner 时只接受同一来源的快照，
    空 owner 的快照（升级前留下的，没有这个字段）仍然兼容。
    """
    best: dict[str, Any] | None = None
    best_key: tuple[str, float] | None = None
    owner = str(owner or "").strip()

    for path in _candidate_files(base):
        if exclude_session and path.stem == exclude_session:
            continue
        data = read_snapshot_file(path)
        if data is None:
            continue
        file_owner = str(data.get("owner") or "").strip()
        if owner and file_owner and file_owner != owner:
            continue  # 别人的场次，不提示、不恢复
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


def claim_local_snapshots(base: Path, owner: str) -> int:
    """把"加登录之前"留下的本机快照认领给 owner，返回改了几份。

    和 history_store.claim_local_records 是同一个理由：换成按账号隔离后，
    升级前那场没做完的面试对谁都不可见了，用户会觉得"我的进度丢了"。

    只认领 owner 为空或 loopback 的。别人通过局域网留下的快照不动。
    """
    owner = str(owner or "").strip()
    if not owner:
        return 0
    changed = 0
    for path in _candidate_files(base):
        data = read_snapshot_file(path)
        if data is None:
            continue
        if not is_local_owner(str(data.get("owner") or "")):
            continue
        if str(data.get("owner") or "").strip() == owner:
            continue
        data["owner"] = owner
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError:
            continue
        changed += 1
    return changed
