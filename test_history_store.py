"""面试历史存储与统计（history_store.py）的单元测试。

被测对象：持久化（`load_records` / `append_record` / `delete_record` /
`clear_records`）与统计（`trend_series` / `dimension_groups` /
`dimension_average` / `weakest_dimension`）。

为什么要单独测：**删除记录会真的动用户数据**。这类操作一旦下标算错，
表现是"点了删除、结果删掉了另一场"，而且不可撤销。原先这段逻辑写在
`app.py` 里（import 阶段就调 `st.set_page_config`，没法在 unittest 里导入），
所以先挪出来才谈得上验证。

测试原则：
    1. 全部离线：用 tempfile 建临时目录，不碰真实工程目录；
    2. 断言"行为"而不是"实现"：重点是越界不误删、损坏文件不崩、跨专业不混比；
    3. 覆盖反例：`bool` 不是分数、维度缺失不能让整组错位。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_history_store -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import history_store


def make_record(
    time="2026-10-11T10:00:00",
    overall=70,
    adjusted=None,
    major="软件工程",
    interview_type="技术面",
    dimensions=None,
    **profile_overrides,
):
    """造一场最小可用的历史记录。"""
    profile = {
        "scene": "应届生校招",
        "major": major,
        "level": "B",
        "interview_type": interview_type,
        "mood_mode": "友善型",
    }
    profile.update(profile_overrides)
    record = {
        "time": time,
        "profile": profile,
        "overall_score": overall,
        "level": "待定",
    }
    if adjusted is not None:
        record["adjusted_score"] = adjusted
    if dimensions is not None:
        record["evaluation"] = {
            "dimensions": [{"name": n, "score": s} for n, s in dimensions.items()]
        }
    return record


class TempBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "history.json"

    def tearDown(self):
        self._tmp.cleanup()


class TestPersistence(TempBase):
    """读写与删除。"""

    def test_missing_file_returns_empty(self):
        self.assertEqual(history_store.load_records(self.path), [])

    def test_corrupt_file_returns_empty(self):
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(history_store.load_records(self.path), [])

    def test_wrong_shape_returns_empty(self):
        # 顶层不是列表（例如被人手改成了字典）
        self.path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        self.assertEqual(history_store.load_records(self.path), [])

    def test_non_dict_items_are_dropped(self):
        self.path.write_text(json.dumps([{"time": "x"}, "脏数据", 3]), encoding="utf-8")
        records = history_store.load_records(self.path)
        self.assertEqual(len(records), 1, "非字典条目要被剔除，而不是让后续 .get 崩掉")

    def test_append_then_load(self):
        history_store.append_record(self.path, make_record(overall=60))
        history_store.append_record(self.path, make_record(overall=80))
        records = history_store.load_records(self.path)
        self.assertEqual([r["overall_score"] for r in records], [60, 80])

    def test_append_creates_file(self):
        self.assertFalse(self.path.exists())
        history_store.append_record(self.path, make_record())
        self.assertTrue(self.path.exists())

    def test_append_leaves_no_temp_file(self):
        history_store.append_record(self.path, make_record())
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], "原子写不该留下临时文件")

    def test_delete_by_index(self):
        for score in (50, 60, 70):
            history_store.append_record(self.path, make_record(overall=score))

        self.assertTrue(history_store.delete_record(self.path, 1))
        records = history_store.load_records(self.path)
        self.assertEqual([r["overall_score"] for r in records], [50, 70], "删的必须是中间那场")

    def test_delete_out_of_range_is_safe(self):
        history_store.append_record(self.path, make_record(overall=50))
        for bad in (-1, 1, 99):
            with self.subTest(index=bad):
                self.assertFalse(history_store.delete_record(self.path, bad))
        self.assertEqual(len(history_store.load_records(self.path)), 1, "越界绝不能误删")

    def test_delete_from_empty_is_safe(self):
        self.assertFalse(history_store.delete_record(self.path, 0))

    def test_delete_first_and_last(self):
        for score in (50, 60, 70):
            history_store.append_record(self.path, make_record(overall=score))
        history_store.delete_record(self.path, 0)
        history_store.delete_record(self.path, 1)
        records = history_store.load_records(self.path)
        self.assertEqual([r["overall_score"] for r in records], [60])

    def test_clear_records(self):
        history_store.append_record(self.path, make_record())
        history_store.clear_records(self.path)
        self.assertEqual(history_store.load_records(self.path), [])


class TestTrendSeries(TempBase):
    """跨场趋势：按时间排序，只保留真的有分数的场次。"""

    def test_empty(self):
        series = history_store.trend_series([])
        self.assertEqual(series["labels"], [])
        self.assertEqual(series["overall"], [])

    def test_sorted_by_time(self):
        records = [
            make_record(time="2026-10-13T10:00:00", overall=80),
            make_record(time="2026-10-11T10:00:00", overall=60),
            make_record(time="2026-10-12T10:00:00", overall=70),
        ]
        series = history_store.trend_series(records)
        self.assertEqual(series["overall"], [60, 70, 80], "必须按时间正序，否则趋势线是乱的")

    def test_skips_records_without_score(self):
        records = [
            make_record(time="2026-10-11T10:00:00", overall=60),
            make_record(time="2026-10-12T10:00:00", overall=None),
        ]
        series = history_store.trend_series(records)
        self.assertEqual(series["overall"], [60], "没生成报告的场次不该出现在趋势里")

    def test_adjusted_series_is_optional(self):
        records = [
            make_record(time="2026-10-11T10:00:00", overall=60, adjusted=75),
            make_record(time="2026-10-12T10:00:00", overall=70),
        ]
        series = history_store.trend_series(records)
        self.assertEqual(series["overall"], [60, 70])
        self.assertEqual(series["adjusted"], [75, None], "缺 adjusted 用 None 占位，不能错位")

    def test_limit_keeps_the_newest(self):
        records = [
            make_record(time=f"2026-10-{day:02d}T10:00:00", overall=day)
            for day in range(1, 11)
        ]
        series = history_store.trend_series(records, limit=3)
        self.assertEqual(series["overall"], [8, 9, 10], "截断要保留最近的几场")

    def test_bool_is_not_a_score(self):
        # True 在 Python 里也是 int，必须排除，否则会被当成 1 分
        records = [make_record(overall=True)]
        self.assertEqual(history_store.trend_series(records)["overall"], [])

    def test_numeric_string_is_not_a_score(self):
        records = [make_record(overall="70")]
        self.assertEqual(history_store.trend_series(records)["overall"], [])


class TestDimensionGroups(TempBase):
    """维度分组：不同专业的维度口径不同，不能混着比。"""

    def test_groups_by_major_and_type(self):
        records = [
            make_record(major="软件工程", interview_type="技术面", dimensions={"专业基础": 70}),
            make_record(major="人工智能", interview_type="技术面", dimensions={"算法能力": 60}),
            make_record(major="软件工程", interview_type="行为面", dimensions={"表达结构": 80}),
        ]
        groups = history_store.dimension_groups(records)
        self.assertEqual(
            set(groups.keys()),
            {"软件工程 / 技术面", "人工智能 / 技术面", "软件工程 / 行为面"},
            "专业与面试类型都必须参与分组，否则维度分会被错误地放在一起比",
        )

    def test_records_without_dimensions_are_skipped(self):
        records = [make_record(dimensions=None), make_record(dimensions={"专业基础": 70})]
        groups = history_store.dimension_groups(records)
        self.assertEqual(len(groups), 1)

    def test_sessions_sorted_within_group(self):
        records = [
            make_record(time="2026-10-13T10:00:00", dimensions={"专业基础": 80}),
            make_record(time="2026-10-11T10:00:00", dimensions={"专业基础": 60}),
        ]
        group = history_store.dimension_groups(records)["软件工程 / 技术面"]
        scores = [s["dimensions"]["专业基础"] for s in group["sessions"]]
        self.assertEqual(scores, [60, 80], "组内也要按时间正序")

    def test_missing_dimension_does_not_shift_others(self):
        # 按场存而不是按维度存数组，就是为了避免这种错位
        records = [
            make_record(time="2026-10-11T10:00:00", dimensions={"A": 60, "B": 70}),
            make_record(time="2026-10-12T10:00:00", dimensions={"A": 80}),
        ]
        group = history_store.dimension_groups(records)["软件工程 / 技术面"]
        sessions = group["sessions"]
        self.assertEqual(sessions[0]["dimensions"]["B"], 70)
        self.assertNotIn("B", sessions[1]["dimensions"], "缺的维度就是缺，不能补 0 或错位")

    def test_bool_score_is_dropped(self):
        records = [make_record(dimensions={"专业基础": True})]
        self.assertEqual(history_store.dimension_groups(records), {})


class TestDimensionStats(unittest.TestCase):
    """维度名提取、平均分、最弱项。"""

    def _group(self):
        records = [
            make_record(time="2026-10-11T10:00:00", dimensions={"A": 60, "B": 90}),
            make_record(time="2026-10-12T10:00:00", dimensions={"A": 80, "B": 70}),
        ]
        return history_store.dimension_groups(records)["软件工程 / 技术面"]

    def test_dimension_names_preserve_first_seen_order(self):
        self.assertEqual(history_store.dimension_names(self._group()), ["A", "B"])

    def test_dimension_names_dedupes(self):
        names = history_store.dimension_names(self._group())
        self.assertEqual(len(names), len(set(names)))

    def test_average(self):
        group = self._group()
        self.assertEqual(history_store.dimension_average(group, "A"), 70.0)
        self.assertEqual(history_store.dimension_average(group, "B"), 80.0)

    def test_average_of_unknown_dimension_is_none(self):
        self.assertIsNone(history_store.dimension_average(self._group(), "不存在"))

    def test_average_ignores_missing_sessions(self):
        # B 只在第一场出现，平均分应只按那一场算，不能把缺失当 0
        records = [
            make_record(time="2026-10-11T10:00:00", dimensions={"A": 60, "B": 90}),
            make_record(time="2026-10-12T10:00:00", dimensions={"A": 80}),
        ]
        group = history_store.dimension_groups(records)["软件工程 / 技术面"]
        self.assertEqual(history_store.dimension_average(group, "B"), 90.0)

    def test_weakest_dimension(self):
        # A 均分 70、B 均分 80 → 最弱是 A
        name, score = history_store.weakest_dimension(self._group())
        self.assertEqual(name, "A")
        self.assertEqual(score, 70.0)

    def test_weakest_dimension_on_empty_group(self):
        self.assertIsNone(history_store.weakest_dimension({"sessions": []}))


class TestRecordLabel(unittest.TestCase):
    def test_label_contains_key_fields(self):
        label = history_store.record_label(make_record())
        self.assertIn("软件工程", label)
        self.assertIn("70 分", label)
        self.assertIn("友善型", label)

    def test_label_tolerates_missing_fields(self):
        label = history_store.record_label({"time": "x", "profile": {}})
        self.assertIsInstance(label, str)  # 不该抛异常


class TestSetOutcome(TempBase):
    """结局回填：按落盘时间定位记录，只改这一场的 outcome，不动别的。"""

    def _seed(self):
        history_store.append_record(self.path, make_record(time="2026-10-11T10:00:00", overall=70))
        history_store.append_record(self.path, make_record(time="2026-10-12T10:00:00", overall=80))

    def test_writes_outcome_and_note(self):
        self._seed()
        self.assertTrue(history_store.set_outcome(self.path, "2026-10-11T10:00:00", "被拒", " 自我介绍太长 "))
        record = history_store.load_records(self.path)[0]
        self.assertEqual(record["outcome"]["value"], "被拒")
        self.assertEqual(record["outcome"]["note"], "自我介绍太长", "备注首尾空白要清掉")
        self.assertTrue(record["outcome"]["time"], "要记录回填时间")

    def test_only_targets_the_matching_record(self):
        self._seed()
        history_store.set_outcome(self.path, "2026-10-12T10:00:00", "拿到 offer")
        records = history_store.load_records(self.path)
        self.assertNotIn("outcome", records[0], "另一场不能被顺带写上结局")
        self.assertEqual(records[1]["outcome"]["value"], "拿到 offer")

    def test_repeated_backfill_overwrites(self):
        self._seed()
        history_store.set_outcome(self.path, "2026-10-11T10:00:00", "还没结果")
        history_store.set_outcome(self.path, "2026-10-11T10:00:00", "进入下一轮")
        self.assertEqual(
            history_store.load_records(self.path)[0]["outcome"]["value"], "进入下一轮"
        )

    def test_unknown_time_returns_false_and_keeps_data(self):
        self._seed()
        self.assertFalse(history_store.set_outcome(self.path, "没有这个时间", "被拒"))
        self.assertFalse(history_store.set_outcome(self.path, "", "被拒"))
        records = history_store.load_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertTrue(all("outcome" not in r for r in records))

    def test_survives_corrupt_history_file(self):
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertFalse(history_store.set_outcome(self.path, "任意", "被拒"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
