# -*- coding: utf-8 -*-
"""现拟参照回写工具（_harvest_generated_rubrics.py）的回归测试。

这个工具会**写 rubrics.json**，是仓库里少数几个能改判分数据的脚本之一，
所以把它的两道闸门钉住：采集只认 `source == "generated"`；回写必须过
「专业存在 / 考点名够长 / 档位合法 / must_hit 3~8 条 / 库里没有同名」五关，
并且**永远不写 veto**（现拟参照没经过人工逐条核对，不配拥有否决权）。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _harvest_generated_rubrics as harvest


def _record(major: str, level: str, when: str, rows: list[dict]) -> dict:
    return {
        "time": when,
        "profile": {"major": major, "level": level},
        "evaluation": {"rubric_check": rows},
    }


def _row(topic: str, source: str = "generated", **extra) -> dict:
    return {
        "topic": topic,
        "source": source,
        "must_hit": extra.get("must_hit", []),
        "missed": extra.get("missed", []),
        "errors": extra.get("errors", []),
    }


class TestCollect(unittest.TestCase):
    def test_groups_by_major_and_topic(self):
        records = [
            _record("计算机科学与技术", "B", "t1", [_row("服务端推送", missed=["a", "b"])]),
            _record("计算机科学与技术", "B", "t2", [_row("服务端推送", must_hit=["c"])]),
            _record("软件工程", "A", "t3", [_row("服务端推送", missed=["d"])]),
        ]
        items = harvest.collect(records)
        self.assertEqual(len(items), 2)
        cs = next(i for i in items if i["major"] == "计算机科学与技术")
        self.assertEqual(cs["count"], 2)
        self.assertEqual(cs["times"], ["t1", "t2"])
        self.assertEqual(cs["hit"], ["c"])
        self.assertEqual(cs["miss"], ["a", "b"])

    def test_ignores_library_rows_and_junk(self):
        records = [
            _record("计算机科学与技术", "B", "t1", [_row("x" * 6, source="library")]),
            _record("计算机科学与技术", "B", "t2", ["不是字典", _row("", source="generated")]),
            "不是记录",
        ]
        self.assertEqual(harvest.collect(records), [])

    def test_sorts_by_count_desc(self):
        records = [
            _record("计算机科学与技术", "B", "t1", [_row("低频考点甲")]),
            _record("计算机科学与技术", "B", "t2", [_row("高频考点乙")]),
            _record("计算机科学与技术", "B", "t3", [_row("高频考点乙")]),
        ]
        items = harvest.collect(records)
        self.assertEqual(items[0]["topic"], "高频考点乙")
        self.assertEqual(items[0]["count"], 2)


class TestApplyManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rubric = Path(self.tmp.name) / "rubrics.json"
        self.rubric.write_text(
            json.dumps({"计算机科学与技术": {"已有考点": {"level": "B", "must_hit": ["a"]}}}),
            encoding="utf-8",
        )
        patch = mock.patch.object(harvest, "RUBRIC_FILE", self.rubric)
        patch.start()
        self.addCleanup(patch.stop)

    def _apply(self, approved: list[dict]) -> int:
        manifest = Path(self.tmp.name) / "m.json"
        manifest.write_text(json.dumps({"approved": approved}, ensure_ascii=False), encoding="utf-8")
        return harvest.apply_manifest(manifest)

    def _rubrics(self) -> dict:
        return json.loads(self.rubric.read_text(encoding="utf-8"))

    def test_adds_entry_without_veto(self):
        code = self._apply([
            {"major": "计算机科学与技术", "topic": "服务端推送与长连接", "level": "B",
             "must_hit": ["说明长连接", "说明推送", "说明重连"]}
        ])
        self.assertEqual(code, 0)
        entry = self._rubrics()["计算机科学与技术"]["服务端推送与长连接"]
        self.assertEqual(entry["level"], "B")
        self.assertEqual(len(entry["must_hit"]), 3)
        self.assertNotIn("veto", entry)

    def test_rejects_too_few_must_hit(self):
        code = self._apply([
            {"major": "计算机科学与技术", "topic": "要点太少的考点", "level": "B",
             "must_hit": ["只有一条"]}
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("要点太少的考点", self._rubrics()["计算机科学与技术"])

    def test_rejects_bad_level_and_short_topic(self):
        self._apply([
            {"major": "计算机科学与技术", "topic": "档位非法考点", "level": "D",
             "must_hit": ["a", "b", "c"]},
            {"major": "计算机科学与技术", "topic": "短", "level": "B",
             "must_hit": ["a", "b", "c"]},
        ])
        bucket = self._rubrics()["计算机科学与技术"]
        self.assertNotIn("档位非法考点", bucket)
        self.assertEqual(len(bucket), 1)

    def test_skips_existing_and_unknown_major(self):
        self._apply([
            {"major": "计算机科学与技术", "topic": "已有考点", "level": "B",
             "must_hit": ["a", "b", "c"]},
            {"major": "不存在的专业", "topic": "某个考点名称", "level": "B",
             "must_hit": ["a", "b", "c"]},
        ])
        bucket = self._rubrics()["计算机科学与技术"]
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket["已有考点"]["must_hit"], ["a"])

    def test_empty_approved_is_a_noop(self):
        before = self.rubric.read_text(encoding="utf-8")
        self.assertEqual(self._apply([]), 0)
        self.assertEqual(self.rubric.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
