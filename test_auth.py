"""账号与会话（auth.py）的单元测试。

被测对象：注册、登录、改密码、会话 token 的签发/校验/吊销，
以及"本机老数据认领"（history_store.claim_local_records /
session_store.claim_local_snapshots）。

为什么要单独测：这套逻辑写错的后果不是报错，而是**别人的面试记录能被看见**
或者**自己的账号登不进去**——两种都不会在页面上抛异常，只会在用户眼里
表现为"数据丢了"或"又被要求登录"。

测试原则：
    1. 全部离线：tempfile 建临时目录，不碰真实 `.auth/`；
    2. 钉住安全契约：明文密码与明文 token 绝不落盘、公开用户信息不带盐和哈希；
    3. 覆盖失败路径：错密码、不存在的账号、过期 token、被删账号的 token。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_auth -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import auth
import history_store
import session_store


class TempBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


class TestPasswordHashing(unittest.TestCase):
    def test_same_password_different_salt_gives_different_hash(self):
        salt1, hash1, _ = auth.hash_password("secret123")
        salt2, hash2, _ = auth.hash_password("secret123")
        self.assertNotEqual(salt1, salt2, "每条密码必须有独立的随机盐")
        self.assertNotEqual(hash1, hash2, "同密码不同盐必须得到不同哈希")

    def test_verify_roundtrip(self):
        salt, stored, rounds = auth.hash_password("secret123")
        self.assertTrue(auth.verify_password("secret123", salt, stored, rounds))

    def test_verify_rejects_wrong_password(self):
        salt, stored, rounds = auth.hash_password("secret123")
        for wrong in ("secret124", "", "SECRET123", "secret123 "):
            with self.subTest(password=wrong):
                self.assertFalse(auth.verify_password(wrong, salt, stored, rounds))

    def test_hash_is_not_the_password(self):
        _, stored, _ = auth.hash_password("secret123")
        self.assertNotIn("secret123", stored)

    def test_old_rounds_still_verify_after_default_changes(self):
        """调高 PBKDF2 迭代次数后，老账号必须还能登进去。

        迭代次数随记录存，所以这里用一个小得多的次数模拟"老记录"。
        """
        salt, stored, rounds = auth.hash_password("secret123", rounds=1000)
        self.assertEqual(rounds, 1000)
        self.assertTrue(auth.verify_password("secret123", salt, stored, 1000))

    def test_verify_tolerates_broken_record(self):
        """畸形记录只能返回 False，不能把登录页抛崩。"""
        cases = [
            ("", "", 0),
            ("zzzz", "abc", 1000),  # 非十六进制
            ("ab12", "", 1000),
            ("ab12", "cd34", -1),
            ("ab12", "cd34", "many"),
        ]
        for salt, stored, rounds in cases:
            with self.subTest(salt=salt, hash=stored, rounds=rounds):
                self.assertFalse(auth.verify_password("secret123", salt, stored, rounds))


# ---------------------------------------------------------------------------
# 注册与登录
# ---------------------------------------------------------------------------


class TestRegistration(TempBase):
    def test_register_then_authenticate(self):
        ok, _ = auth.register(self.base, "alice", "secret123", "爱丽丝")
        self.assertTrue(ok)
        user = auth.authenticate(self.base, "alice", "secret123")
        self.assertIsNotNone(user)
        self.assertEqual(user["account"], "alice")
        self.assertEqual(user["name"], "爱丽丝")

    def test_account_is_case_insensitive(self):
        auth.register(self.base, "Alice", "secret123")
        self.assertIsNotNone(auth.authenticate(self.base, "alice", "secret123"))
        self.assertIsNotNone(auth.authenticate(self.base, "ALICE", "secret123"))

    def test_duplicate_is_rejected_case_insensitively(self):
        auth.register(self.base, "alice", "secret123")
        ok, message = auth.register(self.base, "ALICE", "another123")
        self.assertFalse(ok)
        self.assertIn("已经被注册", message)

    def test_invalid_accounts_are_rejected(self):
        for bad in ("", "a", "带中文", "has space", "bad-dash", "x" * 21):
            with self.subTest(account=bad):
                ok, _ = auth.register(self.base, bad, "secret123")
                self.assertFalse(ok, f"{bad!r} 不该通过校验")

    def test_short_password_is_rejected(self):
        ok, message = auth.register(self.base, "alice", "12345")
        self.assertFalse(ok)
        self.assertIn("至少", message)

    def test_failed_validation_writes_nothing(self):
        auth.register(self.base, "a", "123")
        self.assertEqual(auth.load_users(self.base), {})

    def test_authenticate_wrong_password_returns_none(self):
        auth.register(self.base, "alice", "secret123")
        self.assertIsNone(auth.authenticate(self.base, "alice", "secret124"))

    def test_authenticate_unknown_account_returns_none(self):
        self.assertIsNone(auth.authenticate(self.base, "nobody", "secret123"))

    def test_password_and_salt_never_appear_in_plaintext(self):
        auth.register(self.base, "alice", "secret123")
        raw = auth.users_path(self.base).read_text(encoding="utf-8")
        self.assertNotIn("secret123", raw, "明文密码绝不能落盘")
        data = json.loads(raw)
        self.assertIn("salt", data["alice"])
        self.assertIn("hash", data["alice"])


class TestPublicUser(TempBase):
    def test_public_user_omits_secrets(self):
        auth.register(self.base, "alice", "secret123", "爱丽丝")
        user = auth.public_user(self.base, "alice")
        self.assertEqual(set(user), {"account", "name", "created_at", "has_api_key"})

    def test_public_user_of_unknown_is_none(self):
        self.assertIsNone(auth.public_user(self.base, "nobody"))


# ---------------------------------------------------------------------------
# 会话 token
# ---------------------------------------------------------------------------


class TestSessionToken(TempBase):
    def setUp(self):
        super().setUp()
        auth.register(self.base, "alice", "secret123")

    def test_issue_and_resolve(self):
        token = auth.issue_token(self.base, "alice")
        self.assertEqual(auth.resolve_token(self.base, token), "alice")

    def test_token_hash_only_on_disk(self):
        """sessions.json 里绝不能出现能直接用的 token。"""
        token = auth.issue_token(self.base, "alice")
        raw = auth.sessions_path(self.base).read_text(encoding="utf-8")
        self.assertNotIn(token, raw)

    def test_unknown_token_resolves_to_none(self):
        auth.issue_token(self.base, "alice")
        for bad in ("", "   ", "deadbeef", "0" * 48):
            with self.subTest(token=bad):
                self.assertIsNone(auth.resolve_token(self.base, bad))

    def test_revoke_immediately_invalidates(self):
        token = auth.issue_token(self.base, "alice")
        auth.revoke_token(self.base, token)
        self.assertIsNone(auth.resolve_token(self.base, token))

    def test_revoke_is_idempotent(self):
        token = auth.issue_token(self.base, "alice")
        auth.revoke_token(self.base, token)
        auth.revoke_token(self.base, token)  # 不该抛
        auth.revoke_token(self.base, "")
        self.assertIsNone(auth.resolve_token(self.base, token))

    def test_expired_token_is_rejected(self):
        token = auth.issue_token(self.base, "alice")
        key = auth._token_key(token)
        sessions = json.loads(auth.sessions_path(self.base).read_text(encoding="utf-8"))
        sessions[key]["exp"] = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        auth.sessions_path(self.base).write_text(json.dumps(sessions), encoding="utf-8")
        self.assertIsNone(auth.resolve_token(self.base, token))

    def test_token_of_deleted_account_is_rejected(self):
        """账号被管理员删掉后，旧 token 不能继续用 —— 否则等于一个幽灵账号。"""
        token = auth.issue_token(self.base, "alice")
        users = auth.load_users(self.base)
        users.pop("alice")
        auth.users_path(self.base).write_text(json.dumps(users), encoding="utf-8")
        self.assertIsNone(auth.resolve_token(self.base, token))

    def test_multiple_tokens_coexist(self):
        """多设备同时登录：A 的 token 不该被 B 的签发挤掉。"""
        first = auth.issue_token(self.base, "alice")
        second = auth.issue_token(self.base, "alice")
        self.assertNotEqual(first, second)
        self.assertEqual(auth.resolve_token(self.base, first), "alice")
        self.assertEqual(auth.resolve_token(self.base, second), "alice")

    def test_revoke_user_tokens_clears_all_devices(self):
        first = auth.issue_token(self.base, "alice")
        second = auth.issue_token(self.base, "alice")
        auth.register(self.base, "bob", "secret123")
        bobs = auth.issue_token(self.base, "bob")

        removed = auth.revoke_user_tokens(self.base, "alice")
        self.assertEqual(removed, 2)
        self.assertIsNone(auth.resolve_token(self.base, first))
        self.assertIsNone(auth.resolve_token(self.base, second))
        self.assertEqual(auth.resolve_token(self.base, bobs), "bob", "别人的会话不能被误伤")

    def test_broken_sessions_file_resolves_to_none(self):
        auth.issue_token(self.base, "alice")
        auth.sessions_path(self.base).write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(auth.resolve_token(self.base, "whatever"))


# ---------------------------------------------------------------------------
# 改密码与演示账号
# ---------------------------------------------------------------------------


class TestChangePassword(TempBase):
    def test_change_password(self):
        auth.register(self.base, "alice", "secret123")
        ok, _ = auth.change_password(self.base, "alice", "secret123", "newsecret123")
        self.assertTrue(ok)
        self.assertIsNone(auth.authenticate(self.base, "alice", "secret123"))
        self.assertIsNotNone(auth.authenticate(self.base, "alice", "newsecret123"))

    def test_wrong_old_password_is_rejected(self):
        auth.register(self.base, "alice", "secret123")
        ok, message = auth.change_password(self.base, "alice", "wrong", "newsecret123")
        self.assertFalse(ok)
        self.assertIn("原密码", message)
        self.assertIsNotNone(auth.authenticate(self.base, "alice", "secret123"))

    def test_change_password_kills_other_sessions(self):
        auth.register(self.base, "alice", "secret123")
        token = auth.issue_token(self.base, "alice")
        auth.change_password(self.base, "alice", "secret123", "newsecret123")
        self.assertIsNone(auth.resolve_token(self.base, token), "其他设备必须重新登录")

    def test_short_new_password_is_rejected(self):
        auth.register(self.base, "alice", "secret123")
        ok, _ = auth.change_password(self.base, "alice", "secret123", "123")
        self.assertFalse(ok)


class TestDemoUser(TempBase):
    def test_demo_user_is_created_once(self):
        self.assertTrue(auth.ensure_demo_user(self.base))
        self.assertIsNotNone(
            auth.authenticate(self.base, auth.DEMO_ACCOUNT, auth.DEMO_PASSWORD)
        )

    def test_demo_user_password_is_not_reset(self):
        """用户改了演示账号密码之后，重启不该把它改回去。"""
        auth.ensure_demo_user(self.base)
        auth.change_password(self.base, auth.DEMO_ACCOUNT, auth.DEMO_PASSWORD, "changed123")
        self.assertFalse(auth.ensure_demo_user(self.base))
        self.assertIsNone(
            auth.authenticate(self.base, auth.DEMO_ACCOUNT, auth.DEMO_PASSWORD)
        )
        self.assertIsNotNone(auth.authenticate(self.base, auth.DEMO_ACCOUNT, "changed123"))

    def test_demo_account_may_not_claim_legacy_data(self):
        """演示号是公开的，不能让它把主人的旧记录认领走。

        现场演示时评委常常第一个登录。如果 demo 参与认领，主人的真实历史
        就躺在一个"密码写在登录页上"的账号里，而主人自己注册后看到空历史。
        """
        self.assertFalse(auth.may_claim_legacy(auth.DEMO_ACCOUNT))
        self.assertFalse(auth.may_claim_legacy(auth.DEMO_ACCOUNT.upper()))
        self.assertTrue(auth.may_claim_legacy("alice"))
        self.assertFalse(auth.may_claim_legacy(""))

    def test_demo_account_does_not_persist_history(self):
        """演示号的记录不落盘：所有人共用一个 owner，落盘就是互相看简历。"""
        self.assertFalse(auth.may_save_history(auth.DEMO_ACCOUNT))
        self.assertFalse(auth.may_save_history(auth.DEMO_ACCOUNT.upper()))
        self.assertTrue(auth.may_save_history("alice"))
        self.assertFalse(auth.may_save_history(""))


class TestAccessCode(TempBase):
    """公网演示的准入口令：配了才拦，没配就完全不影响本机自用。"""

    def setUp(self):
        super().setUp()
        self._saved = os.environ.pop(auth.ACCESS_CODE_ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(auth.ACCESS_CODE_ENV, None)
        else:
            os.environ[auth.ACCESS_CODE_ENV] = self._saved
        super().tearDown()

    def test_no_code_configured_lets_everyone_in(self):
        self.assertEqual(auth.access_code_required(), "")
        self.assertTrue(auth.verify_access_code(""))
        self.assertTrue(auth.verify_access_code("随便输什么都行"))

    def test_configured_code_is_enforced(self):
        os.environ[auth.ACCESS_CODE_ENV] = "zy-2026"
        self.assertEqual(auth.access_code_required(), "zy-2026")
        self.assertTrue(auth.verify_access_code("zy-2026"))
        self.assertFalse(auth.verify_access_code("zy-2025"))
        self.assertFalse(auth.verify_access_code(""))

    def test_blank_code_is_treated_as_absent(self):
        os.environ[auth.ACCESS_CODE_ENV] = "   "
        self.assertEqual(auth.access_code_required(), "")
        self.assertTrue(auth.verify_access_code(""))


class TestSeedUsers(TempBase):
    """云端容器重建后从 Secrets 恢复预置账号。

    云端文件系统是临时的，页面上注册的账号活不过一次重启。这里钉住三件事：
    能补回来、不覆盖已改过的密码、坏配置不把登录页搞崩。
    """

    def test_seeds_missing_accounts(self):
        created = auth.seed_users(
            self.base, {"2024135004": "mypassword", "Alice": "another123"}
        )
        self.assertEqual(created, 2)
        self.assertIsNotNone(auth.authenticate(self.base, "2024135004", "mypassword"))
        # 账号名统一小写，Secrets 里写 Alice 也能用 alice 登
        self.assertIsNotNone(auth.authenticate(self.base, "alice", "another123"))

    def test_does_not_overwrite_existing_password(self):
        auth.register(self.base, "2024135004", "changedbyuser")
        created = auth.seed_users(self.base, {"2024135004": "fromsecrets"})
        self.assertEqual(created, 0)
        self.assertIsNotNone(auth.authenticate(self.base, "2024135004", "changedbyuser"))
        self.assertIsNone(auth.authenticate(self.base, "2024135004", "fromsecrets"))

    def test_second_run_is_noop(self):
        """每次 rerun 都会调一次，必须幂等。"""
        payload = {"2024135004": "mypassword"}
        self.assertEqual(auth.seed_users(self.base, payload), 1)
        self.assertEqual(auth.seed_users(self.base, payload), 0)

    def test_bad_entries_are_skipped(self):
        """密码太短、账号名非法、空值都跳过，剩下的照建。"""
        created = auth.seed_users(
            self.base,
            {
                "bad account": "whatever",
                "short": "123",
                "": "nopass",
                "good": "goodpassword",
            },
        )
        self.assertEqual(created, 1)
        self.assertIsNotNone(auth.authenticate(self.base, "good", "goodpassword"))

    def test_empty_payload_is_noop(self):
        self.assertEqual(auth.seed_users(self.base, {}), 0)
        self.assertEqual(auth.load_users(self.base), {})


# ---------------------------------------------------------------------------
# owner 标记
# ---------------------------------------------------------------------------


class TestOwnerTag(unittest.TestCase):
    def test_tag_and_parse(self):
        self.assertEqual(auth.owner_tag("Alice"), "u:alice")
        self.assertEqual(auth.account_of_owner("u:alice"), "alice")

    def test_ip_owner_is_not_an_account(self):
        """v18 的 IP 归属不能被误认成账号名。"""
        for value in ("127.0.0.1", "192.168.1.7", "", "alice"):
            with self.subTest(owner=value):
                self.assertEqual(auth.account_of_owner(value), "")


# ---------------------------------------------------------------------------
# 本机老数据认领
# ---------------------------------------------------------------------------


class TestClaimLocalData(TempBase):
    def test_claim_records_without_owner(self):
        path = self.base / "history.json"
        history_store.append_record(path, {"time": "t1", "overall_score": 80})
        history_store.append_record(path, {"time": "t2", "overall_score": 70}, owner="127.0.0.1")

        claimed = history_store.claim_local_records(path, "u:alice")
        self.assertEqual(claimed, 2)
        records = history_store.load_records(path, owner="u:alice")
        self.assertEqual(len(records), 2)

    def test_claim_skips_other_peoples_records(self):
        """局域网里别人留下的记录不能被第一个注册的人顺走。"""
        path = self.base / "history.json"
        history_store.append_record(path, {"time": "mine"}, owner="127.0.0.1")
        history_store.append_record(path, {"time": "theirs"}, owner="192.168.1.7")

        claimed = history_store.claim_local_records(path, "u:alice")
        self.assertEqual(claimed, 1)
        self.assertEqual(len(history_store.load_records(path, owner="u:alice")), 1)

    def test_claim_twice_does_not_steal_from_first_account(self):
        path = self.base / "history.json"
        history_store.append_record(path, {"time": "t1"})

        self.assertEqual(history_store.claim_local_records(path, "u:alice"), 1)
        self.assertEqual(history_store.claim_local_records(path, "u:bob"), 0, "已被认领的记录不该再被抢")
        self.assertEqual(len(history_store.load_records(path, owner="u:alice")), 1)

    def test_claim_snapshots(self):
        session_store.write_snapshot(
            self.base, "s1", {"saved_at": "2026-10-11T10:00:00", "profile": {"scene": "校招"}}
        )
        session_store.write_snapshot(
            self.base,
            "s2",
            {"saved_at": "2026-10-11T11:00:00", "profile": {"scene": "校招"}, "owner": "192.168.1.7"},
        )

        claimed = session_store.claim_local_snapshots(self.base, "u:alice")
        self.assertEqual(claimed, 1, "只认领本机那份，别人的不动")
        latest = session_store.latest_snapshot(self.base, owner="u:alice")
        self.assertEqual(latest[session_store.SESSION_KEY], "s1")

    def test_claim_with_empty_owner_is_noop(self):
        path = self.base / "history.json"
        history_store.append_record(path, {"time": "t1"})
        self.assertEqual(history_store.claim_local_records(path, ""), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
