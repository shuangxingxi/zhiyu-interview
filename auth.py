"""账号与会话 —— 与 Streamlit 解耦的认证层。

为什么单独一个模块：

1. **可测**。登录、注册、改密码这类逻辑一旦写错，后果是"别人的面试记录能被
   看见"或"自己的账号登不进去"，属于必须离线验证的东西。`app.py` 在 import
   阶段就调 `st.set_page_config`，没法进 unittest；认证本身不需要 Streamlit。

2. **不引入依赖**。密码哈希用标准库 `hashlib.pbkdf2_hmac`，会话 token 用
   `secrets.token_hex`。不装 passlib / bcrypt / pyjwt，符合整个项目"零新增
   依赖、离线可跑"的底线。

安全口径（这是本地演示工具，不是公网服务，但该做的不能省）：

- 密码只存 `pbkdf2-hmac-sha256` 的哈希 + 每条独立的随机盐，迭代次数随记录一起
  存，将来调参不会让老账号登不进去。
- 会话 token 只把**哈希**写进 `sessions.json`。文件被拖走也拿不到能用的 token。
- 所有比较走 `hmac.compare_digest`，不做普通的 `==`。
- 明文密码与明文 token 只存在于内存和 URL query 里，不落盘。

已知限制（写在这里而不是藏起来）：

- 会话靠 URL 上的 `?u=<token>` 维持。刷新页面 Streamlit 会丢掉 `session_state`，
  只有 query 参数能跨刷新活下来；代价是 token 会出现在浏览器历史与地址栏里。
  对局域网演示够用，真上公网应当换成 HttpOnly Cookie。
- 没有密码找回，忘记密码只能由管理员删 `users.json` 里那条记录。
- 没有登录失败次数限制。局域网内不存在暴力破解的现实威胁，但别把端口映射到公网。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from session_store import atomic_write_text

AUTH_DIR_NAME = ".auth"
USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"

# 账号名：字母数字下划线，2~20 字符。不允许中文与空格，避免和"显示名"混在一起，
# 也避免把奇怪的字符写进 history.json 的 owner 字段后无法反查。
_ACCOUNT_RE = re.compile(r"^[a-zA-Z0-9_]{2,20}$")

MIN_PASSWORD = 6
MAX_PASSWORD = 128
MIN_NAME, MAX_NAME = 0, 20

PBKDF2_ROUNDS = 240_000
SESSION_DAYS = 7

# 预置演示账号：参赛现场评委不该先注册才能看效果。
# 密码写在代码里等于公开，所以它的定位是"演示用，别往里放真实数据"。
DEMO_ACCOUNT = "demo"
DEMO_PASSWORD = "ZhiyuDemo2026"
DEMO_NAME = "演示账号"

_EMPTY_USERS: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 路径与小工具
# ---------------------------------------------------------------------------


def auth_dir(base: Path) -> Path:
    return base / AUTH_DIR_NAME


def users_path(base: Path) -> Path:
    return auth_dir(base) / USERS_FILE


def sessions_path(base: Path) -> Path:
    return auth_dir(base) / SESSIONS_FILE


def normalize_account(account: str) -> str:
    """账号名统一小写去空白 —— `Alice` 与 `alice` 必须是同一个账号。"""
    return str(account or "").strip().lower()


def owner_tag(account: str) -> str:
    """写进 history / 快照 `owner` 字段的标记。

    带 `u:` 前缀是为了和 v18 的"IP 归属"区分：那批数据的 owner 是
    `127.0.0.1` 这类裸 IP，不能和账号名混成一个命名空间。
    """
    return f"u:{normalize_account(account)}"


def account_of_owner(owner: str) -> str:
    """`owner` 字段反查账号名；不是账号归属（比如裸 IP、空）时返回空串。"""
    text = str(owner or "").strip()
    if text.startswith("u:"):
        return text[2:]
    return ""


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def hash_password(
    password: str, salt: bytes | None = None, rounds: int = PBKDF2_ROUNDS
) -> tuple[str, str, int]:
    """返回 (盐 hex, 哈希 hex, 迭代次数)。

    迭代次数随记录一起存：将来把 PBKDF2_ROUNDS 调高，老账号仍按自己的次数
    校验，不会集体登不进去。
    """
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt, int(rounds)
    )
    return salt.hex(), digest.hex(), int(rounds)


def verify_password(password: str, salt_hex: str, hash_hex: str, rounds: int) -> bool:
    """校验密码。参数缺失或格式不对一律返回 False，绝不抛异常。

    登录失败的原因不该通过异常类型泄漏给调用方（也避免一次畸形记录把登录页搞崩）。
    """
    try:
        salt = bytes.fromhex(str(salt_hex))
        expected = str(hash_hex)
        rounds = int(rounds)
    except (ValueError, TypeError):
        return False
    if not expected or rounds <= 0:
        return False
    _, actual, _ = hash_password(password, salt=salt, rounds=rounds)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# 用户表
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_users(base: Path) -> dict[str, dict[str, Any]]:
    """全部用户；文件缺失或损坏返回空表。"""
    data = _read_json(users_path(base), _EMPTY_USERS)
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def public_user(base: Path, account: str) -> dict[str, Any] | None:
    """给界面用的用户信息：**不含盐与哈希**。

    界面只需要显示名和创建时间。把盐和哈希往外传是没必要的风险——
    一旦哪天有人 `st.write(user)` 调试，就等于把口令散列摊在页面上。
    """
    record = load_users(base).get(normalize_account(account))
    if not record:
        return None
    return {
        "account": record.get("account", ""),
        "name": record.get("name", ""),
        "created_at": record.get("created_at", ""),
        "has_api_key": bool(record.get("api_key_enc")),
    }


def validate_registration(
    account: str, password: str, name: str = "", *, existing: set[str] | None = None
) -> str:
    """注册参数校验。返回空串表示通过，否则返回给用户看的错误话术。"""
    account = normalize_account(account)
    if not account:
        return "账号不能为空"
    if not _ACCOUNT_RE.match(account):
        return "账号只能由字母、数字、下划线组成，长度 2~20"
    if existing and account in existing:
        return "这个账号已经被注册了"
    if len(str(password or "")) < MIN_PASSWORD:
        return f"密码至少 {MIN_PASSWORD} 位"
    if len(str(password or "")) > MAX_PASSWORD:
        return f"密码最多 {MAX_PASSWORD} 位"
    if len(str(name or "")) > MAX_NAME:
        return f"显示名最多 {MAX_NAME} 个字"
    return ""


def register(
    base: Path, account: str, password: str, name: str = ""
) -> tuple[bool, str]:
    """注册账号，返回 (是否成功, 提示话术)。"""
    account = normalize_account(account)
    error = validate_registration(account, password, name, existing=set(load_users(base)))
    if error:
        return False, error

    salt_hex, hash_hex, rounds = hash_password(password)
    users = load_users(base)
    users[account] = {
        "account": account,
        "name": str(name or "").strip(),
        "salt": salt_hex,
        "hash": hash_hex,
        "rounds": rounds,
        "created_at": _iso(_now()),
        # 预留给"每个账号用自己的 API Key"。当前版本不启用（共用环境变量那把），
        # 但结构先留着：将来只需在设置页填值 + 改 key 解析顺序，不用改数据格式。
        "api_key_enc": "",
    }
    _write_json(users_path(base), users)
    return True, "注册成功"


def authenticate(base: Path, account: str, password: str) -> dict[str, Any] | None:
    """账号密码换取公开用户信息；失败返回 None。

    失败原因不区分"账号不存在"与"密码错误"：对外一律同一句话，
    避免被用来枚举哪些账号存在。
    """
    account = normalize_account(account)
    record = load_users(base).get(account)
    if not record:
        return None
    if not verify_password(
        password,
        record.get("salt", ""),
        record.get("hash", ""),
        record.get("rounds", 0),
    ):
        return None
    return public_user(base, account)


def change_password(base: Path, account: str, old_password: str, new_password: str) -> tuple[bool, str]:
    """改密码。成功后吊销该账号已签发的所有会话，其他设备需要重新登录。"""
    account = normalize_account(account)
    users = load_users(base)
    record = users.get(account)
    if not record:
        return False, "账号不存在"
    if not verify_password(
        old_password, record.get("salt", ""), record.get("hash", ""), record.get("rounds", 0)
    ):
        return False, "原密码不正确"
    if len(str(new_password or "")) < MIN_PASSWORD:
        return False, f"新密码至少 {MIN_PASSWORD} 位"

    salt_hex, hash_hex, rounds = hash_password(new_password)
    record.update({"salt": salt_hex, "hash": hash_hex, "rounds": rounds})
    _write_json(users_path(base), users)
    revoke_user_tokens(base, account)
    return True, "密码已修改，其他设备需要重新登录"


def ensure_demo_user(base: Path) -> bool:
    """首次运行时建好演示账号。已存在则不动（不会覆盖改过的密码）。"""
    if DEMO_ACCOUNT in load_users(base):
        return False
    ok, _ = register(base, DEMO_ACCOUNT, DEMO_PASSWORD, DEMO_NAME)
    return ok


def seed_users(base: Path, accounts: dict[str, str]) -> int:
    """把预置账号灌进用户表，返回实际新建的个数。

    存在的理由：Streamlit Community Cloud 的容器文件系统是临时的，重启、
    休眠唤醒、每次重新部署都会回到 git 仓库的状态，而 `.auth/` 不在仓库里。
    页面上注册的账号活不过一次重启——下次登录只会看到"账号或密码不正确"，
    因为那条记录根本不存在了。账号写进 Secrets 才能跨容器存活，启动时由
    `app.py` 调这里补回来。

    只补缺失的，不覆盖已有记录：用户在页面上改过密码，重启后不该被冲掉。
    账号名非法或密码太短的条目直接跳过（静默），坏配置不该把登录页搞崩。
    """
    created = 0
    for raw_account, raw_password in dict(accounts or {}).items():
        account = normalize_account(raw_account)
        password = str(raw_password or "")
        if not account or not password:
            continue
        # existing 里有的会返回"已被注册"，正好用来做"只补不覆盖"。
        if validate_registration(account, password, existing=set(load_users(base))):
            continue
        ok, _ = register(base, account, password)
        if ok:
            created += 1
    return created


def may_claim_legacy(account: str) -> bool:
    """这个账号有没有资格认领"加登录之前"的本机旧数据。

    演示账号没有。它的密码就印在登录页上，谁都进得去 —— 让它认领等于把
    主人的真实面试记录挪进一个人人可读的账号里。现场演示时评委往往第一个
    登录，这条规则挡的就是这个。
    """
    account = normalize_account(account)
    return bool(account) and account != DEMO_ACCOUNT


# ---------------------------------------------------------------------------
# 公网部署：准入口令与演示账号的记录策略
# ---------------------------------------------------------------------------

ACCESS_CODE_ENV = "ZY_ACCESS_CODE"


def access_code_required() -> str:
    """当前生效的准入口令；空串表示不设防（本机自用就是这种）。

    口令放环境变量而不是配置文件：它不该跟着仓库走。部署平台配一个变量
    就生效，删掉就恢复原样，误提交的可能性为零。
    """
    return str(os.getenv(ACCESS_CODE_ENV, "") or "").strip()


def verify_access_code(code: str) -> bool:
    """校验准入口令。未配置口令时一律通过，本机自用不受影响。"""
    expected = access_code_required()
    if not expected:
        return True
    return hmac.compare_digest(str(code or "").strip(), expected)


def may_save_history(account: str) -> bool:
    """这个账号的面试记录要不要落到 history.json。

    演示账号不落：密码印在登录页上，所有评委共用同一个 owner ——
    落盘等于 A 评委的简历与逐字稿出现在 B 评委的历史页里。
    规则在 app.py 执行（改成留在本次会话内存里）。
    """
    account = normalize_account(account)
    return bool(account) and account != DEMO_ACCOUNT


# ---------------------------------------------------------------------------
# 会话 token
# ---------------------------------------------------------------------------


def _token_key(token: str) -> str:
    """token 在存放表里的键：只存哈希，不存原文。"""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:32]


def _load_sessions(base: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(sessions_path(base), {})
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def _save_sessions(base: Path, sessions: dict[str, dict[str, Any]]) -> None:
    _write_json(sessions_path(base), sessions)


def _alive(record: dict[str, Any], now: datetime | None = None) -> bool:
    try:
        expires = datetime.fromisoformat(str(record.get("exp") or ""))
    except (ValueError, TypeError):
        return False
    return expires > (now or _now())


def issue_token(base: Path, account: str, days: int = SESSION_DAYS) -> str:
    """签发会话 token（明文只这一刻返回，落盘的只有它的哈希）。"""
    account = normalize_account(account)
    token = secrets.token_hex(24)
    sessions = _load_sessions(base)
    # 顺手清掉过期的，避免文件只增不减
    now = _now()
    sessions = {key: rec for key, rec in sessions.items() if _alive(rec, now)}
    sessions[_token_key(token)] = {
        "account": account,
        "exp": _iso(now + timedelta(days=max(1, int(days)))),
        "issued": _iso(now),
    }
    _save_sessions(base, sessions)
    return token


def resolve_token(base: Path, token: str) -> str | None:
    """token 换成账号名；无效 / 过期 / 账号已被删掉都返回 None。"""
    text = str(token or "").strip()
    if not text:
        return None
    sessions = _load_sessions(base)
    record = sessions.get(_token_key(text))
    if not record or not _alive(record):
        return None
    account = normalize_account(record.get("account", ""))
    if account not in load_users(base):
        return None  # 账号被删了，token 不该还能用
    return account


def revoke_token(base: Path, token: str) -> None:
    text = str(token or "").strip()
    if not text:
        return
    sessions = _load_sessions(base)
    if sessions.pop(_token_key(text), None) is not None:
        _save_sessions(base, sessions)


def revoke_user_tokens(base: Path, account: str) -> int:
    """吊销某账号的全部会话，返回清掉几条。"""
    account = normalize_account(account)
    sessions = _load_sessions(base)
    kept = {
        key: rec
        for key, rec in sessions.items()
        if normalize_account(rec.get("account", "")) != account
    }
    removed = len(sessions) - len(kept)
    if removed:
        _save_sessions(base, kept)
    return removed
