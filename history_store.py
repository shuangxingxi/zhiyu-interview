"""面试历史的存储与统计 —— 与 Streamlit 解耦，便于离线测试。

分两部分：

1. **持久化**（`load_records` / `append_record` / `delete_record` / `clear_records` / `set_outcome`）
   读写 `history.json`。此前这部分逻辑直接写在 `app.py` 里，而 `app.py` 在 import
   阶段就调用 `st.set_page_config`、根本没法在 unittest 里导入——所以"删除记录"
   这种会**真的动用户数据**的操作，值得挪出来测。

2. **统计**（`trend_series` / `dimension_groups`）
   给「跨场横向对比」用的纯函数。数据本来就全在 `history.json` 里
   （每场都有总分 / 剥离干扰分 / 各维度分），此前只做了"逐场回看"，
   没有把它变成"我这几场有没有进步"。

   注意一个容易被忽略的点：**不同专业的维度名完全不同**（计算机是
   「编程基础与数据结构」，人工智能是另一套），跨专业比较维度分没有意义。
   所以 `dimension_groups` 按「专业 / 面试类型」分组，让界面只在**同一组内**比。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from session_store import atomic_write_text, file_lock

# 单场最多看这么多场，避免图表被几十场记录压扁
DEFAULT_LIMIT = 20


# ---------------------------------------------------------------------------
# 归属隔离：谁产生的记录，只有谁看得见
# ---------------------------------------------------------------------------

# 本机访问时客户端 IP 的几种形态。旧版记录没有 owner 字段，一律视为本机产生。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _owner_of(record: dict) -> str:
    return str(record.get("owner") or "").strip()


def visible_to(record: dict, owner: str) -> bool:
    """这条记录该不该给 owner 这个人看。

    不传 owner 时全部可见 —— 保持老调用方（以及离线单测）的行为不变。

    本机访问（loopback）也全部可见。理由很实际：数据文件就躺在主人自己的
    硬盘上，`notepad history.json` 就能看，再在本机做过滤只是自欺欺人；
    而且同一个人换个地址访问（localhost / 局域网 IP）不该看到两份不同的历史。

    其余情况按 owner 严格隔离。没有 owner 字段的记录是升级前留下的，
    产生在这台机器上，所以对外部来客一律隐藏。
    """
    owner = str(owner or "").strip()
    if not owner or owner in _LOOPBACK:
        return True
    record_owner = _owner_of(record)
    if not record_owner:
        return False
    return record_owner == owner


def _visible_indexes(records: list[dict], owner: str) -> list[int]:
    """可见记录在**原始列表**里的下标。

    界面上的下标是过滤后视图的下标，直接拿去删原始列表会删错人 ——
    中间隔着一批别人的记录，偏移量完全对不上。所有按下标写操作的地方
    都必须先经过这把映射。
    """
    return [i for i, record in enumerate(records) if visible_to(record, owner)]


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def load_records(path: Path, owner: str = "") -> list[dict]:
    """读取历史记录；文件缺失或损坏都返回空列表，绝不抛异常。

    历史只是回看与去重用的，任何异常都不该挡住正常面试。

    owner 非空时只返回这个人自己的记录（见 visible_to 的归属规则）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and visible_to(item, owner)]


def _read_all(path: Path) -> list[dict]:
    """不做归属过滤地读全量 —— 只有"读-改-写"内部才该用它。"""
    return load_records(path, owner="")


def _write_records(path: Path, records: list[dict]) -> None:
    atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=2))


def append_record(path: Path, record: dict, owner: str = "") -> None:
    """追加一场记录。读-改-写全程持锁。

    不加锁时两个人同时交卷：A 读到 10 条、B 也读到 10 条，
    A 写回 11 条、B 写回 11 条 —— 其中一份成绩凭空消失，而且不报错。

    owner 会被写进记录，之后别人读不到它。
    """
    with file_lock(path):
        records = _read_all(path)
        if owner:
            record = {**record, "owner": str(owner).strip()}
        records.append(record)
        _write_records(path, records)


def delete_record(path: Path, index: int, owner: str = "") -> bool:
    """按下标删掉一场记录，返回是否真的删掉了。

    下标越界时返回 False 而不是抛异常——界面上的下标可能因为并发操作过期。
    持锁读改写，避免和另一个会话的 append 撞车后把整份历史写回旧版本。

    index 是**可见视图**里的下标：别人的记录不占位，也就删不到。
    """
    with file_lock(path):
        records = _read_all(path)
        indexes = _visible_indexes(records, owner)
        if not 0 <= index < len(indexes):
            return False
        del records[indexes[index]]
        _write_records(path, records)
        return True



def clear_records(path: Path, owner: str = "") -> None:
    """清空历史记录。

    刻意写成"写入空列表"而不是删文件：`load_records` 对两者都返回空列表，
    但保留文件能避免某些平台上"删了又被别的进程重建"的竞态困惑。

    owner 非空时只清掉自己那些，别人的记录原地保留 —— 否则一个人点
    "清空历史"，把整个局域网里所有人的成绩单一起抹了。
    """
    with file_lock(path):
        if not owner:
            _write_records(path, [])
            return
        kept = [record for record in _read_all(path) if not visible_to(record, owner)]
        _write_records(path, kept)


def set_outcome(
    path: Path, time_key: str, outcome: str, note: str = "", owner: str = ""
) -> bool:
    """按落盘时间回填这一场的真实结局；找不到对应记录返回 False。

    为什么用 time 当键而不是下标：界面上的下标会因为"删掉一场"而整体前移，
    用户手上那个下标可能已经指向另一场了；而 `time` 是落盘时写死、
    并且已经显示在列表标题里的字段，用它定位不存在错位的可能。

    outcome 的取值由调用方（界面）从 calibration.OUTCOMES 里选，
    这里只负责原样落盘；读取侧的兜底在 calibration.outcome_of，
    所以即使历史文件被手改成乱码，也不会把界面搞崩。

    owner 非空时只在**自己可见**的记录里找：别人的场次即使 time 撞上，
    也不该被顺手改掉结局。
    """
    key = str(time_key or "").strip()
    if not key:
        return False
    with file_lock(path):
        records = _read_all(path)
        for record in records:
            if str(record.get("time") or "").strip() != key:
                continue
            if not visible_to(record, owner):
                continue
            record["outcome"] = {
                "value": str(outcome or "").strip(),
                "note": str(note or "").strip(),
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            _write_records(path, records)
            return True
    return False


# ---------------------------------------------------------------------------
# 统计（纯函数，不碰文件）
# ---------------------------------------------------------------------------


def _as_score(value: Any) -> float | None:
    """把分值规整成 float；非数值（含 bool）一律返回 None。

    `True` 在 Python 里也是 int，必须显式排除，否则会被当成 1 分。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def group_key(profile: dict[str, Any]) -> str:
    """分组键：不同专业 / 面试类型的维度口径不同，不能混着比。"""
    major = str((profile or {}).get("major") or "").strip() or "未指定专业"
    kind = str((profile or {}).get("interview_type") or "").strip() or "技术面"
    return f"{major} / {kind}"


def record_label(record: dict[str, Any]) -> str:
    """一场记录的单行标签（历史列表与图表标题共用）。"""
    profile = record.get("profile") or {}
    parts = [
        str(record.get("time") or ""),
        str(profile.get("scene") or ""),
        str(profile.get("major") or ""),
        f"{profile.get('level', '')} 级" if profile.get("level") else "",
        f"{record.get('overall_score', '-')} 分",
        f"JD 定向 {len(profile['jd_topics'])} 条" if profile.get("jd_topics") else "",
        f"情绪 {profile.get('mood_mode')}" if profile.get("mood_mode") else "",
    ]
    return " · ".join(p for p in parts if p and p.strip())


def trend_series(records: list[dict], limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """按时间顺序取出总分与剥离干扰分，供跨场趋势图使用。

    只保留**真的有分数**的记录（没生成报告的场次会被跳过）。
    返回 {"labels": [...], "overall": [...], "adjusted": [...]}，
    全部为空时 labels 为空列表，调用方据此决定要不要画图。
    """
    rows: list[tuple[str, float, float | None]] = []
    for record in records:
        score = _as_score(record.get("overall_score"))
        if score is None:
            continue
        rows.append(
            (
                str(record.get("time") or ""),
                score,
                _as_score(record.get("adjusted_score")),
            )
        )

    # 时间字符串是 ISO 格式，字典序即时间序
    rows.sort(key=lambda item: item[0])
    rows = rows[-max(1, limit) :]

    return {
        "labels": [time for time, _, _ in rows],
        "overall": [overall for _, overall, _ in rows],
        "adjusted": [adj for _, _, adj in rows],
    }


def dimension_groups(
    records: list[dict], limit: int = DEFAULT_LIMIT
) -> dict[str, dict[str, Any]]:
    """按「专业 / 面试类型」分组，组内按时间排出各维度分。

    返回 {组名: {"sessions": [{"time","overall","adjusted","dimensions"}...]}}。

    用 sessions 列表而不是"维度名 → 分数数组"，是为了**避免对齐错位**：
    万一某一场的模型漏输出了某个维度，按数组存会让后续所有值错位一格，
    而按场存则只是那一个格子缺失（界面上显示为「—」）。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        evaluation = record.get("evaluation") or {}
        dims = evaluation.get("dimensions") or []
        if not isinstance(dims, list):
            continue

        scores: dict[str, float] = {}
        for item in dims:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = _as_score(item.get("score"))
            if name and value is not None:
                scores[name] = value
        if not scores:
            continue

        key = group_key(record.get("profile") or {})
        buckets.setdefault(key, []).append(
            {
                "time": str(record.get("time") or ""),
                "overall": _as_score(record.get("overall_score")),
                "adjusted": _as_score(record.get("adjusted_score")),
                "dimensions": scores,
            }
        )

    groups: dict[str, dict[str, Any]] = {}
    for key, sessions in buckets.items():
        sessions.sort(key=lambda s: s["time"])
        groups[key] = {"sessions": sessions[-max(1, limit) :]}
    return groups


def dimension_names(group: dict[str, Any]) -> list[str]:
    """组内出现过的维度名，按首次出现顺序去重（保持模型给的顺序）。"""
    names: list[str] = []
    for session in group.get("sessions") or []:
        for name in (session.get("dimensions") or {}):
            if name not in names:
                names.append(name)
    return names


def dimension_average(group: dict[str, Any], name: str) -> float | None:
    """某个维度在组内的平均分；一次都没出现时返回 None。"""
    values = [
        session["dimensions"][name]
        for session in (group.get("sessions") or [])
        if name in (session.get("dimensions") or {})
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def weakest_dimension(group: dict[str, Any]) -> tuple[str, float] | None:
    """组内平均分最低的维度 —— 用来直接回答"我一直卡在哪"。

    样本不足（一个维度都没有）时返回 None。
    """
    names = dimension_names(group)
    if not names:
        return None
    scored = [
        (name, avg)
        for name in names
        if (avg := dimension_average(group, name)) is not None
    ]
    if not scored:
        return None
    return min(scored, key=lambda item: item[1])
