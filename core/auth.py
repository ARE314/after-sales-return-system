"""鉴权与权限组 —— 登录验证 / 权限点 / 会话

设计要点
--------
* **独立成库**（`data/auth.db`）：与业务库同样遵循「一个模块一个库」，
  鉴权是横切关注点，将来换 SSO 或对接企业账号只动这一库。
* **权限模型是「权限组 + 权限点」两级**，不是把角色写死在代码里：
  预置 `管理员 / 登记员 / 只读` 三个内置组（`builtin=1`，不可删除），
  其余组可在界面上自由增删、逐项勾选权限点。
* **密码用标准库的 PBKDF2-HMAC-SHA256**，不引入第三方依赖；
  存储格式 `pbkdf2_sha256$迭代次数$盐$摘要`，迭代次数随记录走，
  将来提高强度时旧密码仍可校验（校验后按需重算升级）。
* **会话是服务端记录 + Cookie 里只放随机 ID**（HttpOnly）：
  退出、停用账号、改密都能立即失效，而不是等 Cookie 过期。
* **总开关可运行时切换**：`auth_enabled` 存 setting 表，默认取
  `config.AUTH_ENABLED_DEFAULT`。关掉后一切请求放行（本地开发方便），
  这是刻意的——不做「关掉了但接口还偷偷拦」这种半吊子状态。
"""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta

from config import (AUTH_BOOTSTRAP_PASSWORD, AUTH_BOOTSTRAP_USER,
                    SESSION_HOURS)
from core.db import get_conn, tx

PBKDF2_ITER = 120_000
SALT_BYTES = 16

# ---------------------------------------------------------------------------
# 权限点注册表
#
# 分两类：
#   page.*  界面访问 —— 控制导航入口与页面可达性
#   act.*   数据/系统操作 —— 控制接口层面的写操作与管理动作
#
# 只读类接口（查询、看板、导出明细）不设权限点：只要登录且看得见页面即可读。
# 这是刻意的取舍 —— 细粒度到「哪个字段能看」属于数据权限，本模块不做，
# 需要时在 repository 层按组过滤（那里能看到具体字段）。
# ---------------------------------------------------------------------------
PERMISSION_GROUPS = [
    {"title": "界面访问", "perms": [
        {"key": "page.index", "label": "工作台"},
        {"key": "page.scan", "label": "退回登记"},
        {"key": "page.inspect", "label": "检测登记"},
        {"key": "page.handle", "label": "处理登记"},
        {"key": "page.query", "label": "明细查询"},
        {"key": "page.dashboard", "label": "数据看板"},
        {"key": "page.items", "label": "匹配数据库"},
        {"key": "page.api", "label": "数据接口"},
        {"key": "page.auth", "label": "权限设置"},
    ]},
    {"title": "数据操作", "perms": [
        {"key": "act.create", "label": "新增登记"},
        {"key": "act.edit", "label": "修改明细与照片"},
        {"key": "act.delete", "label": "删除明细"},
        {"key": "act.export", "label": "导出数据"},
        {"key": "act.items", "label": "物料维护（增删 / 导入）"},
    ]},
    {"title": "系统管理", "perms": [
        {"key": "act.openapi", "label": "数据接口配置（令牌 / 白名单 / 范围）"},
        {"key": "act.user", "label": "用户与权限组管理"},
        {"key": "act.settings", "label": "系统开关（登录验证开关）"},
    ]},
]

ALL_PERMS = [p["key"] for g in PERMISSION_GROUPS for p in g["perms"]]

PERM_LABELS = {p["key"]: p["label"]
               for g in PERMISSION_GROUPS for p in g["perms"]}

# 页面文件 → 权限点（服务端据此拒绝直接敲 URL 的越权访问）
PAGE_PERMS = {
    "index.html": "page.index",
    "scan.html": "page.scan",
    "inspect.html": "page.inspect",
    "handle.html": "page.handle",
    "query.html": "page.query",
    "dashboard.html": "page.dashboard",
    "items.html": "page.items",
    "api.html": "page.api",
    "auth.html": "page.auth",
}
# 登录页等「未登录也要能打开」的页面
PUBLIC_PAGES = {"login.html"}

# 预置权限组：内置组不可删除，但可以改权限（除「管理员」外）
PRESET_GROUPS = [
    {
        "name": "管理员",
        "description": "全部权限，含用户与接口配置",
        "perms": ALL_PERMS,
        "builtin": 1,
        # 管理员组的权限**不允许被削减** —— 否则一次误操作可能把所有人
        # 锁在系统外，且没有恢复入口。要限制某人就用别的组。
        "locked": True,
    },
    {
        "name": "登记员",
        "description": "日常登记与查询，不能删数据、不能改用户与接口",
        "perms": [
            "page.index", "page.scan", "page.inspect", "page.handle",
            "page.query", "page.dashboard",
            "act.create", "act.edit", "act.export",
        ],
        "builtin": 1,
    },
    {
        "name": "只读",
        "description": "只能查看与导出，不能改动任何数据",
        "perms": ["page.index", "page.query", "page.dashboard", "act.export"],
        "builtin": 1,
    },
]
# 被锁定的组（权限不可改）
LOCKED_GROUPS = [g["name"] for g in PRESET_GROUPS if g.get("locked")]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 → `pbkdf2_sha256$iter$salt$hash`。"""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ITER)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITER,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码。格式异常一律判失败（不抛异常，避免登录接口 500）。"""
    try:
        algo, iter_s, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 int(iter_s))
        return hmac.compare_digest(dk, expect)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# setting（运行时可切换项）
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = get_conn().execute(
        "SELECT value FROM auth_db.setting WHERE key = ?;", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth_db.setting(key, value, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at;",
            (key, "" if value is None else str(value), _now()),
        )


def auth_enabled() -> bool:
    """登录验证总开关。setting 表没值时回落到 config 的默认值。"""
    from config import AUTH_ENABLED_DEFAULT
    raw = get_setting("auth_enabled", "")
    if raw == "":
        return AUTH_ENABLED_DEFAULT
    return raw == "1"


def set_auth_enabled(on: bool) -> None:
    set_setting("auth_enabled", "1" if on else "0")


# ---------------------------------------------------------------------------
# 权限组
# ---------------------------------------------------------------------------

def _row_to_group(row) -> dict:
    try:
        perms = json.loads(row["perms"] or "[]")
    except Exception:  # noqa: BLE001
        perms = []
    perms = [p for p in perms if p in ALL_PERMS]     # 丢弃已下线的权限点
    return {
        "id": row["id"], "name": row["name"],
        "description": row["description"] or "",
        "perms": perms, "count": len(perms),
        "builtin": bool(row["builtin"]),
        "locked": row["name"] in LOCKED_GROUPS,
        "updated_at": row["updated_at"],
    }


def list_groups() -> list:
    rows = get_conn().execute(
        "SELECT * FROM auth_db.permission_group ORDER BY builtin DESC, id;"
    ).fetchall()
    groups = [_row_to_group(r) for r in rows]
    # 附带每组的人数，界面上删组前一眼能看到影响面
    counts = {r["group_id"]: r["c"] for r in get_conn().execute(
        "SELECT group_id, COUNT(*) c FROM auth_db.users GROUP BY group_id;")}
    for g in groups:
        g["user_count"] = counts.get(g["id"], 0)
    return groups


def get_group(group_id: int):
    row = get_conn().execute(
        "SELECT * FROM auth_db.permission_group WHERE id = ?;",
        (group_id,)).fetchone()
    return _row_to_group(row) if row else None


def group_by_name(name: str):
    row = get_conn().execute(
        "SELECT * FROM auth_db.permission_group WHERE name = ?;",
        (name,)).fetchone()
    return _row_to_group(row) if row else None


def perms_of(user: dict) -> list:
    """取用户的权限点列表（从所属组继承）。"""
    if user.get("perms") is not None:
        return list(user["perms"])
    g = get_group(user.get("group_id") or 0)
    return g["perms"] if g else []


def has_perm(user, perm: str) -> bool:
    return bool(user) and perm in perms_of(user)


def create_group(name: str, description: str = "", perms=None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("权限组名不能为空")
    if group_by_name(name):
        raise ValueError(f"权限组「{name}」已存在")
    picked = [p for p in (perms or []) if p in ALL_PERMS]
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO auth_db.permission_group(name, description, perms, "
            "builtin, created_at, updated_at) VALUES (?,?,?,0,?,?);",
            (name, description or "", json.dumps(picked, ensure_ascii=False),
             _now(), _now()))
    return cur.lastrowid


def update_group(group_id: int, name=None, description=None, perms=None) -> int:
    g = get_group(group_id)
    if not g:
        raise ValueError("权限组不存在")
    new_name = (name or g["name"]).strip() or g["name"]
    if new_name != g["name"] and group_by_name(new_name):
        raise ValueError(f"权限组「{new_name}」已存在")
    if g["locked"] and perms is not None:
        raise ValueError(f"「{g['name']}」是内置的管理员组，权限不可修改")
    if g["builtin"] and new_name != g["name"]:
        raise ValueError("内置权限组不可改名")

    fields, params = ["name = ?", "description = ?", "updated_at = ?"], \
        [new_name, (description if description is not None
                    else g["description"]), _now()]
    if perms is not None:
        fields.insert(2, "perms = ?")
        params.insert(2, json.dumps([p for p in perms if p in ALL_PERMS],
                                    ensure_ascii=False))
    with tx() as conn:
        conn.execute(
            f"UPDATE auth_db.permission_group SET {', '.join(fields)} "
            f"WHERE id = ?;", params + [group_id])
    return 1


def delete_group(group_id: int) -> int:
    g = get_group(group_id)
    if not g:
        raise ValueError("权限组不存在")
    if g["builtin"]:
        raise ValueError(f"「{g['name']}」是内置组，不可删除")
    n = get_conn().execute(
        "SELECT COUNT(*) c FROM auth_db.users WHERE group_id = ?;",
        (group_id,)).fetchone()["c"]
    if n:
        raise ValueError(f"该组下还有 {n} 个用户，请先改派或删除这些用户")
    with tx() as conn:
        conn.execute("DELETE FROM auth_db.permission_group WHERE id = ?;",
                     (group_id,))
    return 1


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------

def _row_to_user(row, with_perms: bool = False) -> dict:
    u = {
        "id": row["id"], "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "group_id": row["group_id"],
        "enabled": bool(row["enabled"]),
        "must_change_pwd": bool(row["must_change_pwd"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
    }
    g = get_group(row["group_id"] or 0)
    u["group_name"] = g["name"] if g else "（未分组）"
    if with_perms:
        u["perms"] = g["perms"] if g else []
    return u


def list_users() -> list:
    rows = get_conn().execute(
        "SELECT * FROM auth_db.users ORDER BY id;").fetchall()
    return [_row_to_user(r) for r in rows]


def get_user(user_id: int, with_perms: bool = False):
    row = get_conn().execute(
        "SELECT * FROM auth_db.users WHERE id = ?;", (user_id,)).fetchone()
    return _row_to_user(row, with_perms) if row else None


def get_user_by_name(username: str, with_perms: bool = False):
    row = get_conn().execute(
        "SELECT * FROM auth_db.users WHERE username = ?;",
        ((username or "").strip(),)).fetchone()
    return _row_to_user(row, with_perms) if row else None


def create_user(username: str, password: str, group_id: int,
                display_name: str = "", must_change_pwd: bool = False) -> int:
    username = (username or "").strip()
    if not username:
        raise ValueError("账号不能为空")
    if len(password or "") < 4:
        raise ValueError("密码至少 4 位")
    if get_user_by_name(username):
        raise ValueError(f"账号「{username}」已存在")
    if not get_group(group_id):
        raise ValueError("所选权限组不存在")
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO auth_db.users(username, display_name, password_hash, "
            "group_id, enabled, must_change_pwd, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?,?);",
            (username, display_name or username, hash_password(password),
             group_id, 1 if must_change_pwd else 0, _now(), _now()))
    return cur.lastrowid


def update_user(user_id: int, display_name=None, group_id=None, enabled=None,
                password=None) -> int:
    u = get_user(user_id)
    if not u:
        raise ValueError("用户不存在")
    if enabled is False and _admin_count() <= 1 and u["group_name"] == "管理员":
        raise ValueError("这是最后一个启用的管理员账号，不能停用")
    fields, params = [], []
    if display_name is not None:
        fields.append("display_name = ?"); params.append(display_name)
    if group_id is not None:
        if not get_group(group_id):
            raise ValueError("所选权限组不存在")
        # 把最后一个管理员移出管理员组 = 自锁，同样拦掉
        if u["group_name"] == "管理员" and _admin_count() <= 1:
            tgt = get_group(group_id)
            if tgt and tgt["name"] != "管理员":
                raise ValueError("这是最后一个管理员账号，不能改到其它权限组")
        fields.append("group_id = ?"); params.append(group_id)
    if enabled is not None:
        fields.append("enabled = ?"); params.append(1 if enabled else 0)
    if password:
        if len(password) < 4:
            raise ValueError("密码至少 4 位")
        fields.append("password_hash = ?"); params.append(hash_password(password))
        fields.append("must_change_pwd = ?"); params.append(0)
    if not fields:
        return 0
    fields.append("updated_at = ?"); params.append(_now())
    with tx() as conn:
        conn.execute(f"UPDATE auth_db.users SET {', '.join(fields)} "
                     f"WHERE id = ?;", params + [user_id])
        if enabled is False or password:
            # 停用 / 改密后让既有会话立即失效，不然旧 Cookie 还能用
            conn.execute("UPDATE auth_db.session SET revoked = 1 "
                         "WHERE user_id = ?;", (user_id,))
    return 1


def delete_user(user_id: int) -> int:
    u = get_user(user_id)
    if not u:
        raise ValueError("用户不存在")
    if u["group_name"] == "管理员" and _admin_count() <= 1:
        raise ValueError("这是最后一个管理员账号，不能删除")
    with tx() as conn:
        conn.execute("DELETE FROM auth_db.users WHERE id = ?;", (user_id,))
        conn.execute("DELETE FROM auth_db.session WHERE user_id = ?;",
                     (user_id,))
    return 1


def _admin_count() -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) c FROM auth_db.users u "
        "JOIN auth_db.permission_group g ON g.id = u.group_id "
        "WHERE u.enabled = 1 AND g.name = '管理员';").fetchone()
    return row["c"] if row else 0


def change_password(user_id: int, old_pwd: str, new_pwd: str,
                    keep_sid: str = "") -> int:
    """用户自助改密。校验旧密码，成功后吊销**其它**会话（保留当前设备）。"""
    row = get_conn().execute(
        "SELECT password_hash FROM auth_db.users WHERE id = ?;",
        (user_id,)).fetchone()
    if not row or not verify_password(old_pwd, row["password_hash"]):
        raise ValueError("原密码不正确")
    if len(new_pwd or "") < 4:
        raise ValueError("新密码至少 4 位")
    if new_pwd == old_pwd:
        raise ValueError("新密码不能与原密码相同")
    with tx() as conn:
        conn.execute("UPDATE auth_db.users SET password_hash = ?, "
                     "must_change_pwd = 0, updated_at = ? WHERE id = ?;",
                     (hash_password(new_pwd), _now(), user_id))
        conn.execute("UPDATE auth_db.session SET revoked = 1 "
                     "WHERE user_id = ? AND sid <> ?;", (user_id, keep_sid))
    return 1


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str):
    """校验账号密码，成功返回用户 dict（含 perms），失败返回 None。"""
    row = get_conn().execute(
        "SELECT * FROM auth_db.users WHERE username = ?;",
        ((username or "").strip(),)).fetchone()
    if not row:
        return None
    if not verify_password(password or "", row["password_hash"]):
        return None
    if not row["enabled"]:
        return None
    return _row_to_user(row, with_perms=True)


def create_session(user: dict, ip: str = "", user_agent: str = "") -> dict:
    sid = secrets.token_urlsafe(32)
    created = datetime.now()
    expires = created + timedelta(hours=SESSION_HOURS)
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth_db.session(sid, user_id, username, ip, "
            "user_agent, created_at, expires_at) VALUES (?,?,?,?,?,?,?);",
            (sid, user["id"], user["username"], ip or "",
             (user_agent or "")[:300],
             created.strftime("%Y-%m-%d %H:%M:%S"),
             expires.strftime("%Y-%m-%d %H:%M:%S")))
        conn.execute("UPDATE auth_db.users SET last_login_at = ? WHERE id = ?;",
                     (created.strftime("%Y-%m-%d %H:%M:%S"), user["id"]))
    return {"sid": sid,
            "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S")}


def get_session(sid: str):
    """按会话 ID 取当前用户。过期 / 已吊销 / 账号停用一律视为无效。"""
    if not sid:
        return None
    row = get_conn().execute(
        "SELECT * FROM auth_db.session WHERE sid = ?;", (sid,)).fetchone()
    if not row or row["revoked"]:
        return None
    if (row["expires_at"] or "") <= _now():
        return None
    u = get_user(row["user_id"], with_perms=True)
    if not u or not u["enabled"]:
        return None
    u["sid"] = sid
    return u


def revoke_session(sid: str) -> None:
    if sid:
        with tx() as conn:
            conn.execute("UPDATE auth_db.session SET revoked = 1 "
                         "WHERE sid = ?;", (sid,))


def purge_expired_sessions() -> int:
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM auth_db.session WHERE expires_at < ?;", (_now(),))
        return cur.rowcount


def active_sessions(limit: int = 50) -> list:
    rows = get_conn().execute(
        "SELECT * FROM auth_db.session WHERE revoked = 0 AND expires_at >= ? "
        "ORDER BY created_at DESC LIMIT ?;", (_now(), limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

def log_action(action: str, target: str = "", payload: str = "",
               operator: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth_db.op_log(action, detail_key, payload, operator, "
            "created_at) VALUES (?,?,?,?,?);",
            (action, target, payload, operator, _now()))


def recent_logs(limit: int = 100) -> list:
    rows = get_conn().execute(
        "SELECT * FROM auth_db.op_log ORDER BY id DESC LIMIT ?;",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def ensure_bootstrap() -> dict:
    """幂等初始化：预置权限组 + 首个管理员。

    只在**缺失时**创建，不会覆盖已有配置 —— 每次启动都会调用。
    """
    created = {"groups": [], "admin": None}
    for g in PRESET_GROUPS:
        if not group_by_name(g["name"]):
            with tx() as conn:
                conn.execute(
                    "INSERT INTO auth_db.permission_group(name, description, "
                    "perms, builtin, created_at, updated_at) "
                    "VALUES (?,?,?,1,?,?);",
                    (g["name"], g["description"],
                     json.dumps(g["perms"], ensure_ascii=False), _now(), _now()))
            created["groups"].append(g["name"])

    if get_conn().execute(
            "SELECT COUNT(*) c FROM auth_db.users;").fetchone()["c"] == 0:
        admin_group = group_by_name("管理员")
        uid = create_user(AUTH_BOOTSTRAP_USER, AUTH_BOOTSTRAP_PASSWORD,
                          admin_group["id"], "系统管理员",
                          must_change_pwd=True)
        created["admin"] = {"id": uid, "username": AUTH_BOOTSTRAP_USER}
    return created


def backup_status() -> dict:
    """最近一次数据备份的状态（供界面提示「备份是否超期」）。

    为什么放在服务里：定时备份失败是**静默**的。没人天天看 journal，
    等到真要恢复时才发现最近一份是三个月前的 —— 那时已经来不及了。
    所以把状态暴露到界面上，让「该备份了」这件事自己浮出来。

    数据来源 `data/backup_status.json`（由 tools/backup.py 每次运行后写入）。
    读文件而不是读库：备份可能在**另一个容器 / 另一台机器**上跑，
    只有共享的卷能同时被两边看到；库里那份（setting.last_backup_at）留作 SQL 核对。
    """
    from config import BACKUP_STALE_HOURS, BACKUP_STATUS_FILE
    out = {
        "checked": True, "ok": False, "at": "", "age_hours": None,
        "stale": True, "stale_hours": BACKUP_STALE_HOURS,
        "path": "", "bytes": 0, "error": "", "note": "",
    }
    if not BACKUP_STATUS_FILE.exists():
        out["note"] = "还没有备份记录。请运行 tools/backup.py，或启用定时备份"
        return out
    try:
        data = json.loads(BACKUP_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:                                    # noqa: BLE001
        out["note"] = f"备份状态文件无法解析：{exc}"
        return out

    out["ok"] = bool(data.get("ok"))
    out["at"] = str(data.get("at") or "")
    out["path"] = str(data.get("path") or "")
    out["bytes"] = int(data.get("bytes") or 0)
    out["error"] = str(data.get("error") or "")
    try:
        when = datetime.strptime(out["at"], "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - when).total_seconds() / 3600.0
        out["age_hours"] = round(age, 1)
        out["stale"] = age > BACKUP_STALE_HOURS
    except ValueError:
        out["note"] = f"备份时间无法解析：{out['at']!r}"
        return out

    if not out["ok"]:
        out["note"] = f"最近一次备份失败：{out['error'] or '未知原因'}"
    elif out["stale"]:
        out["note"] = (f"最近一次成功备份在 {out['age_hours']} 小时前，"
                       f"已超过 {BACKUP_STALE_HOURS} 小时的阈值")
    else:
        out["note"] = f"最近一次备份 {out['age_hours']} 小时前"
    return out


def summary() -> dict:
    """供接口返回的鉴权概览。"""
    conn = get_conn()
    return {
        "enabled": auth_enabled(),
        "users": conn.execute(
            "SELECT COUNT(*) c FROM auth_db.users;").fetchone()["c"],
        "enabled_users": conn.execute(
            "SELECT COUNT(*) c FROM auth_db.users WHERE enabled = 1;"
        ).fetchone()["c"],
        "groups": conn.execute(
            "SELECT COUNT(*) c FROM auth_db.permission_group;").fetchone()["c"],
        "sessions": conn.execute(
            "SELECT COUNT(*) c FROM auth_db.session WHERE revoked = 0 "
            "AND expires_at >= ?;", (_now(),)).fetchone()["c"],
        "session_hours": SESSION_HOURS,
        "permission_groups": PERMISSION_GROUPS,
        "preset_groups": [g["name"] for g in PRESET_GROUPS],
    }


__all__ = [
    "PERMISSION_GROUPS", "ALL_PERMS", "PERM_LABELS", "PAGE_PERMS",
    "PUBLIC_PAGES", "PRESET_GROUPS", "LOCKED_GROUPS",
    "hash_password", "verify_password",
    "get_setting", "set_setting", "auth_enabled", "set_auth_enabled",
    "list_groups", "get_group", "group_by_name", "perms_of", "has_perm",
    "create_group", "update_group", "delete_group",
    "list_users", "get_user", "get_user_by_name", "create_user",
    "update_user", "delete_user", "change_password",
    "authenticate", "create_session", "get_session", "revoke_session",
    "purge_expired_sessions", "active_sessions",
    "log_action", "recent_logs", "ensure_bootstrap", "summary",
    "backup_status",
]
