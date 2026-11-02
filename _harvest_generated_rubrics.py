# -*- coding: utf-8 -*-
"""把「现拟参照」采集出来，供人工复核后回写参照库。

背景：v17 起，面试官偶尔会问出库里没有的考点（自拟题）。评估官对这些题会
现场现拟 3~5 条必须命中的要点再判分，落盘时标 `source: "generated"`。
这些现拟要点是「一次性的」——同样的题第二次出现，又要重新现拟一遍，
不同场的判分标准还可能不一致。

这个脚本做两件事：
1. 采集：扫 history.json，把出现的现拟考点按（专业, 考点）聚合，输出复核清单；
2. 回写：读人工确认过的清单（JSON），把条目并进 rubrics.json。

**为什么不自动回写**：库里每一条都宣称"人工核对过"，veto 更是逐条判过的。
让模型写的东西自己进库，等于把这个宣称变成谎话。所以回写必须带一份
人工确认的清单；脚本只负责校验格式与合并，不替你判断要点对不对。

用法：
    py -3.12 _harvest_generated_rubrics.py                 # 只采集，写复核清单
    py -3.12 _harvest_generated_rubrics.py --apply 待核参照.json   # 合并已确认条目
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "history.json"
RUBRIC_FILE = BASE_DIR / "rubrics.json"
REVIEW_MD = BASE_DIR / "待核参照清单.md"
REVIEW_JSON = BASE_DIR / "待核参照.json"

MIN_MUST_HIT = 3
MAX_MUST_HIT = 8
MIN_TOPIC_LEN = 4


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[harvest] 读取失败 {path.name}：{exc}", file=sys.stderr)
        return default


def collect(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 generated 行按（专业, 考点）聚合。"""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        profile = record.get("profile") or {}
        major = str(profile.get("major") or "").strip()
        level = str(profile.get("level") or "B").strip() or "B"
        when = str(record.get("time") or "")
        for row in (record.get("evaluation") or {}).get("rubric_check") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("source") or "").strip() != "generated":
                continue
            topic = str(row.get("topic") or "").strip()
            if not topic:
                continue
            key = (major, topic)
            item = buckets.setdefault(
                key,
                {"major": major, "topic": topic, "count": 0, "times": [], "levels": set(),
                 "hit": [], "miss": [], "errors": []},
            )
            item["count"] += 1
            item["times"].append(when)
            item["levels"].add(level)
            for field, bucket in (("must_hit", "hit"), ("missed", "miss"), ("errors", "errors")):
                for value in row.get(field) or []:
                    text = str(value).strip()
                    if text and text not in item[bucket]:
                        item[bucket].append(text)
    out = []
    for item in buckets.values():
        item["levels"] = sorted(item["levels"])
        out.append(item)
    out.sort(key=lambda x: (-x["count"], x["major"], x["topic"]))
    return out


def write_review(candidates: list[dict[str, Any]]) -> None:
    lines = [
        "# 待核参照清单（现拟 → 入库）",
        "",
        "由 `_harvest_generated_rubrics.py` 自动生成，**不要手改本文件**——",
        "要补什么请改 `待核参照.json`，再跑一次脚本。",
        "",
        "## 怎么处理",
        "",
        "1. 逐条判断：这个考点是不是真的缺？要点写得对不对？",
        "2. 要入库的，把该条从 `待核参照.json` 的 `rejected` 移到 `approved`,",
        "   并把 `must_hit` 改写成你认为正确的 3~8 条（**模型写的只是初稿**）。",
        "3. 跑 `py -3.12 _harvest_generated_rubrics.py --apply 待核参照.json`。",
        "",
        "## 采集结果",
        "",
    ]
    if not candidates:
        lines += ["暂无现拟参照（历史里没有 `source: generated` 的行）。", ""]
    else:
        lines += [
            "| 专业 | 考点 | 出现次数 | 档位 | 已命中要点 | 遗漏要点 |",
            "|---|---|---|---|---|---|",
        ]
        for item in candidates:
            lines.append(
                f"| {item['major']} | {item['topic']} | {item['count']} | "
                f"{'/'.join(item['levels'])} | {len(item['hit'])} | {len(item['miss'])} |"
            )
        lines.append("")
        for item in candidates:
            lines += [f"### {item['topic']}", "", f"- 专业：{item['major']}", ""]
            if item["hit"]:
                lines.append("- 候选取命中要点（评估官现场现拟）：")
                lines += [f"  - {x}" for x in item["hit"]]
            if item["miss"]:
                lines.append("- 候选取遗漏要点：")
                lines += [f"  - {x}" for x in item["miss"]]
            if item["errors"]:
                lines.append("- 曾经踩中的误区：")
                lines += [f"  - {x}" for x in item["errors"]]
            lines.append("")
    REVIEW_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"复核清单已写入 {REVIEW_MD.name}（{len(candidates)} 条候选）")


def write_manifest(candidates: list[dict[str, Any]]) -> None:
    if REVIEW_JSON.exists():
        print(f"{REVIEW_JSON.name} 已存在，保留人工改动不覆盖")
        return
    manifest = {
        "_说明": "把要入库的条目从 rejected 移到 approved，并改写 must_hit；level 必填（A/B/C）。",
        "approved": [],
        "rejected": [
            {
                "major": item["major"],
                "topic": item["topic"],
                "level": item["levels"][0] if item["levels"] else "B",
                "reason": "待人工判断",
                "must_hit": item["hit"][:MAX_MUST_HIT],
            }
            for item in candidates
        ],
    }
    REVIEW_JSON.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"待核清单已写入 {REVIEW_JSON.name}")


def apply_manifest(manifest_path: Path) -> int:
    manifest = load_json(manifest_path, None)
    if not isinstance(manifest, dict):
        print(f"[apply] {manifest_path.name} 不是合法 JSON 对象", file=sys.stderr)
        return 2
    approved = manifest.get("approved") or []
    if not approved:
        print("[apply] approved 为空，没有要写入的条目")
        return 0

    rubrics = load_json(RUBRIC_FILE, {})
    if not isinstance(rubrics, dict) or not rubrics:
        print("[apply] rubrics.json 读取失败", file=sys.stderr)
        return 2

    added = 0
    for item in approved:
        if not isinstance(item, dict):
            print("[apply] 跳过非对象条目")
            continue
        major = str(item.get("major") or "").strip()
        topic = str(item.get("topic") or "").strip()
        level = str(item.get("level") or "").strip().upper()
        must_hit = [str(x).strip() for x in (item.get("must_hit") or []) if str(x).strip()]
        if major not in rubrics:
            print(f"[apply] 跳过：专业不存在 {major!r}")
            continue
        if len(topic) < MIN_TOPIC_LEN:
            print(f"[apply] 跳过：考点名太短 {topic!r}")
            continue
        if level not in ("A", "B", "C"):
            print(f"[apply] 跳过：level 非法 {level!r}（{topic}）")
            continue
        if not (MIN_MUST_HIT <= len(must_hit) <= MAX_MUST_HIT):
            print(f"[apply] 跳过：must_hit {len(must_hit)} 条，要求 {MIN_MUST_HIT}~{MAX_MUST_HIT}（{topic}）")
            continue
        if topic in rubrics[major]:
            print(f"[apply] 跳过：库里已有该考点（{topic}）")
            continue
        # veto 一律不写：现拟参照没有经过人工逐条核对，不配拥有否决权。
        rubrics[major][topic] = {"level": level, "must_hit": must_hit, "bonus": [], "common_errors": []}
        added += 1

    if added:
        RUBRIC_FILE.write_text(
            json.dumps(rubrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"[apply] 新增 {added} 条；当前总条目 {sum(len(v) for v in rubrics.values())}")
    return 0


def main(argv: list[str]) -> int:
    if "--apply" in argv:
        idx = argv.index("--apply")
        target = argv[idx + 1] if idx + 1 < len(argv) else REVIEW_JSON.name
        return apply_manifest(BASE_DIR / target)

    records = load_json(HISTORY_FILE, [])
    candidates = collect(records if isinstance(records, list) else [])
    write_review(candidates)
    write_manifest(candidates)
    for item in candidates:
        print(f"  · [{item['major']}] {item['topic']} ×{item['count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
