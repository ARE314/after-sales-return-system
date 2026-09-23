"""ERP 连接配置的通用读写（两套 profile 共用同一份实现）。

2026-09-22 起两个任务的**连接不再共用**：

    匹配数据库   key 前缀 `erp_`        环境变量 `ARS_ERP_*`
    发货明细     key 前缀 `ship_erp_`   环境变量 `ARS_SHIP_ERP_*`

键名、默认值、环境变量前缀不同，但取值顺序（环境变量 > `auth_db.setting` > 代码默认值）、
「密码只写不读」、「空值不留库」这几条规则完全一样，所以实现只写一遍。

为什么拆开：共用时「改一处、忘了另一处」是最难发现的一类故障 ——
两个任务的界面都显示正常，只有一个同步在后台连不上。两个数据源本来也可能
换成不同账号 / 不同库。

升级路径：拆开之前两份共用 `erp_*`。`core/erp_ship.ensure_conn_seeded()`
会在首次读取时把老那份**搬一次**到 `ship_erp_*`（幂等、有标记），
所以升级上来不会掉回「没有密码」的默认值。
"""

from core import auth

# 空串怎么处理：**不写库**，把该键删掉 —— 取值随即回落到默认值。
# 反过来（写一个空串进表）会留下「显式保存过的空值」：读出来是 ''，
# 而取值逻辑把 '' 当 falsy 又回落默认，于是「看着像没配、实际在生效」，
# 直接看库的人一定会读错。空了就是没配，行本身也不该留着。
PLACEHOLDER = ""


def read(prefix: str, env_keys: dict, defaults: dict, redact: bool = True) -> dict:
    """读一套连接配置。

    `env_keys` 只覆盖与「连接」有关的键；`defaults` 可以更大
    （匹配库那份里还带着 `interval_hours` / `enabled`），多出来的键
    只是「没有环境变量入口」而已，读写规则一致。
    """
    import os

    out = {}
    for key, default in defaults.items():
        env = env_keys.get(key)
        val = (os.getenv(env, "") or "").strip() if env else ""
        if not val:
            val = auth.get_setting(prefix + key, "") or default
        out[key] = val
    # `source` 给界面用：这一项的值到底来自环境变量、数据库还是代码默认值。
    # 少了它，「为什么改了没生效」只能靠猜（环境变量优先级最高）。
    out["source"] = {
        k: ("env" if (env_keys.get(k) and (os.getenv(env_keys[k], "") or "").strip())
            else ("setting" if auth.get_setting(prefix + k, "") else "default"))
        for k in defaults
    }
    out["password_set"] = bool(out.get("password"))
    if redact:
        out.pop("password", None)
    return out


def write(prefix: str, env_keys: dict, defaults: dict, data: dict) -> dict:
    """写一套连接配置，返回写入后的结果（已脱敏）。

    **密码只写不读**：传空串 = 不改（页面上的空密码框不该把已保存的密码抹掉），
    传 `None` = 清除。其余字段传空 = 删掉该键（回落默认值）。
    """
    for k, v in (data or {}).items():
        if k not in defaults:
            # 认不出来的键直接丢掉 —— 一个 PUT 承担多件事时靠这张白名单隔离，
            # 否则「顺手改了别的配置」不会报错。
            continue
        if k == "password":
            if v is None:
                auth.del_setting(prefix + k)
            elif str(v).strip():
                auth.set_setting(prefix + k, str(v).strip())
            continue
        val = str(v).strip() if v is not None else PLACEHOLDER
        if val:
            auth.set_setting(prefix + k, val)
        else:
            auth.del_setting(prefix + k)
    return read(prefix, env_keys, defaults, redact=True)


__all__ = ["read", "write", "PLACEHOLDER"]
