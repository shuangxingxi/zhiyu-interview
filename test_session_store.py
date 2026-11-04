"""会话快照存储层（session_store.py）的单元测试。

被测对象：`session_store.py` 的路径推导、原子写、单场读写、
"最近一场"挑选、以及清除逻辑。

为什么要单独测这一层：`app.py` 在 import 阶段就调用 `st.set_page_config`，
没法在 unittest 里导入；而快照读写本身与 Streamlit 无关，抽出来后可以离线直测。

测试原则：
    1. 全部离线、零依赖：用 tempfile 建临时目录，不碰真实工程目录；
    2. 断言"行为"而不是"实现"：重点是**多标签页不再互相覆盖**这条核心修复，
       以及损坏文件不会让续面功能崩掉；
    3. 兼容旧版：升级前留下的单文件快照仍要能被扫到。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_session_store -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import session_store


def sample_snapshot(saved_at: str = "2026-10-11T10:00:00", **overrides) -> dict:
    """造一份最小可用的快照（profile 非空才算有效）。"""
    data = {
        "saved_at": saved_at,
        "started_at": saved_at,
        "profile": {"scene": "应届生校招", "major": "软件工程"},
        "had_resume": False,
        "history": [{"role": "user", "content": "我做了个推荐系统。"}],
    }
    data.update(overrides)
    return data


class TempBase(unittest.TestCase):
    """每个用例一个独立临时目录，互不干扰。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestPaths(TempBase):
    """路径推导：一场一个文件，且不能跑出快照目录。"""

    def test_path_is_inside_snapshot_dir(self):
        path = session_store.snapshot_path(self.base, "abc123")
        self.assertEqual(path.parent, session_store.snapshot_dir(self.base))
        self.assertEqual(path.name, "abc123.json")

    def test_unsafe_session_id_is_sanitised(self):
        # 会话 id 由我们自己生成，但这是最后一道防线
        path = session_store.snapshot_path(self.base, "../../etc/passwd")
        self.assertEqual(path.parent, session_store.snapshot_dir(self.base))
        self.assertNotIn("..", path.name)
        self.assertNotIn("/", path.name)
        self.assertNotIn("\\", path.name)

    def test_empty_session_id_falls_back(self):
        path = session_store.snapshot_path(self.base, "")
        self.assertEqual(path.name, "default.json")


class TestWriteRead(TempBase):
    """写入与读回。"""

    def test_roundtrip(self):
        session_store.write_snapshot(self.base, "s1", sample_snapshot())
        data = session_store.read_snapshot_file(
            session_store.snapshot_path(self.base, "s1")
        )
        self.assertIsNotNone(data)
        self.assertEqual(data["profile"]["major"], "软件工程")

    def test_write_creates_directory(self):
        # 首次运行时 .snapshots/ 还不存在，写入要能自己建出来
        self.assertFalse(session_store.snapshot_dir(self.base).exists())
        session_store.write_snapshot(self.base, "s1", sample_snapshot())
        self.assertTrue(session_store.snapshot_dir(self.base).is_dir())

    def test_write_leaves_no_temp_file(self):
        session_store.write_snapshot(self.base, "s1", sample_snapshot())
        leftovers = list(session_store.snapshot_dir(self.base).glob("*.tmp"))
        self.assertEqual(leftovers, [], "原子写不该留下临时文件")

    def test_two_sessions_do_not_overwrite_each_other(self):
        # 核心修复：原先共用一个文件，开第二个标签页会顶掉第一个
        session_store.write_snapshot(self.base, "tab-a", sample_snapshot())
        session_store.write_snapshot(
            self.base, "tab-b", sample_snapshot(profile={"scene": "考研复试"})
        )

        a = session_store.read_snapshot_file(
            session_store.snapshot_path(self.base, "tab-a")
        )
        b = session_store.read_snapshot_file(
            session_store.snapshot_path(self.base, "tab-b")
        )
        self.assertEqual(a["profile"]["scene"], "应届生校招")
        self.assertEqual(b["profile"]["scene"], "考研复试")


class TestReadRobustness(TempBase):
    """损坏 / 缺失 / 结构不对，一律返回 None，绝不抛异常。"""

    def test_missing_file(self):
        self.assertIsNone(
            session_store.read_snapshot_file(self.base / "nope.json")
        )

    def test_corrupt_json(self):
        path = session_store.snapshot_path(self.base, "bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(session_store.read_snapshot_file(path))

    def test_wrong_shape(self):
        path = session_store.snapshot_path(self.base, "shape")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["不是字典"]), encoding="utf-8")
        self.assertIsNone(session_store.read_snapshot_file(path))

    def test_empty_profile_is_not_resumable(self):
        # 没有 profile 就恢复不出一场面试，应当视为无效
        path = session_store.snapshot_path(self.base, "empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": "x", "profile": {}}), encoding="utf-8")
        self.assertIsNone(session_store.read_snapshot_file(path))


class TestLatestSnapshot(TempBase):
    """挑"最近一场"：按 saved_at，并带上来源会话 id。"""

    def test_none_when_empty(self):
        self.assertIsNone(session_store.latest_snapshot(self.base))

    def test_picks_newest_by_saved_at(self):
        session_store.write_snapshot(
            self.base, "old", sample_snapshot(saved_at="2026-10-11T09:00:00")
        )
        session_store.write_snapshot(
            self.base, "new", sample_snapshot(saved_at="2026-10-11T18:00:00")
        )
        latest = session_store.latest_snapshot(self.base)
        self.assertEqual(latest[session_store.SESSION_KEY], "new")

    def test_reports_source_session_id(self):
        session_store.write_snapshot(self.base, "s7", sample_snapshot())
        latest = session_store.latest_snapshot(self.base)
        self.assertEqual(latest[session_store.SESSION_KEY], "s7")

    def test_exclude_skips_own_session(self):
        # 正在面试的那个会话，不该在首页看到自己的快照
        session_store.write_snapshot(
            self.base, "mine", sample_snapshot(saved_at="2026-10-11T18:00:00")
        )
        session_store.write_snapshot(
            self.base, "other", sample_snapshot(saved_at="2026-10-11T09:00:00")
        )
        latest = session_store.latest_snapshot(self.base, exclude_session="mine")
        self.assertEqual(latest[session_store.SESSION_KEY], "other")

    def test_exclude_can_leave_nothing(self):
        session_store.write_snapshot(self.base, "mine", sample_snapshot())
        self.assertIsNone(
            session_store.latest_snapshot(self.base, exclude_session="mine")
        )

    def test_owner_filters_other_people(self):
        """局域网多人同时用：A 不该在首页看到 B 那场没做完的面试。

        两个人各自有快照文件，只按 saved_at 挑最近的会让 A 看到 B 的对话，
        点"继续"就等于接手别人的面试 —— 数据直接串味。
        """
        session_store.write_snapshot(
            self.base, "b_session", sample_snapshot(saved_at="2026-10-11T18:00:00", owner="10.0.0.9")
        )
        session_store.write_snapshot(
            self.base, "a_session", sample_snapshot(saved_at="2026-10-11T09:00:00", owner="10.0.0.2")
        )

        # A 只看得到自己那场（哪怕 B 的更新）
        latest = session_store.latest_snapshot(self.base, owner="10.0.0.2")
        self.assertEqual(latest[session_store.SESSION_KEY], "a_session")

        # B 同理
        latest = session_store.latest_snapshot(self.base, owner="10.0.0.9")
        self.assertEqual(latest[session_store.SESSION_KEY], "b_session")

        # 没有匹配来源时返回 None，而不是退而求其次拿别人的
        self.assertIsNone(session_store.latest_snapshot(self.base, owner="10.0.0.99"))

    def test_owner_empty_falls_back_to_old_behaviour(self):
        """拿不到 IP 时不能把续面功能弄没了：空 owner = 不按来源过滤。"""
        session_store.write_snapshot(
            self.base, "s1", sample_snapshot(saved_at="2026-10-11T18:00:00", owner="10.0.0.9")
        )
        latest = session_store.latest_snapshot(self.base, owner="")
        self.assertEqual(latest[session_store.SESSION_KEY], "s1")

    def test_legacy_snapshot_without_owner_is_still_visible(self):
        """升级前留下的快照没有 owner 字段，不能被当成"别人的"而隐藏。"""
        session_store.write_snapshot(self.base, "legacy", sample_snapshot())
        latest = session_store.latest_snapshot(self.base, owner="10.0.0.2")
        self.assertEqual(latest[session_store.SESSION_KEY], "legacy")

    def test_skips_corrupt_file(self):
        # 一个坏文件不该让整条续面路径失效
        bad = session_store.snapshot_path(self.base, "bad")
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{ 坏掉了", encoding="utf-8")
        session_store.write_snapshot(self.base, "good", sample_snapshot())

        latest = session_store.latest_snapshot(self.base)
        self.assertEqual(latest[session_store.SESSION_KEY], "good")

    def test_finds_legacy_single_file(self):
        # 兼容升级前留下的旧版快照（单文件），不能因为换存储就丢掉
        legacy = self.base / session_store.LEGACY_SNAPSHOT_NAME
        legacy.write_text(
            json.dumps(sample_snapshot(saved_at="2026-10-11T07:00:00")),
            encoding="utf-8",
        )
        latest = session_store.latest_snapshot(self.base)
        self.assertIsNotNone(latest, "旧版单文件快照必须仍可被扫到")
        self.assertEqual(latest["profile"]["major"], "软件工程")


class TestClear(TempBase):
    """清除：只清自己那一场，或按需全清。"""

    def test_clear_one(self):
        session_store.write_snapshot(self.base, "a", sample_snapshot())
        session_store.write_snapshot(self.base, "b", sample_snapshot())
        session_store.clear_snapshot(self.base, "a")

        self.assertFalse(session_store.snapshot_path(self.base, "a").exists())
        self.assertTrue(session_store.snapshot_path(self.base, "b").exists())

    def test_clear_missing_is_silent(self):
        session_store.clear_snapshot(self.base, "never-existed")  # 不该抛

    def test_clear_legacy_snapshot_actually_removes_it(self):
        # 旧文件的 stem 与 sanitise 后的名字不同，必须显式处理，
        # 否则用户点了"放弃"、横幅下一轮又冒出来
        legacy = self.base / session_store.LEGACY_SNAPSHOT_NAME
        legacy.write_text(json.dumps(sample_snapshot()), encoding="utf-8")

        session_store.clear_snapshot(self.base, legacy.stem)
        self.assertFalse(legacy.exists(), "旧版快照必须真的被删掉")

    def test_clear_all_includes_legacy(self):
        session_store.write_snapshot(self.base, "a", sample_snapshot())
        legacy = self.base / session_store.LEGACY_SNAPSHOT_NAME
        legacy.write_text(json.dumps(sample_snapshot()), encoding="utf-8")

        session_store.clear_all(self.base)
        self.assertIsNone(session_store.latest_snapshot(self.base))


if __name__ == "__main__":
    unittest.main()
