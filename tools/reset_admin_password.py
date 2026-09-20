"""重置某个账号的密码（忘了密码时的救援入口）

为什么要单独做个工具，而不是删 `data/auth.db`：
    直接删库会连**用户、权限组、审计日志一起丢掉**，而这些是不可再生的
    （见 README「备份与迁移」）。本工具只改一个密码字段，其余原样保留。

用法
----
    # 给管理员重置成新密码（会把该账号所有已登录设备踢下线）
    python tools\\reset_admin_password.py --user admin --password NewPass123

    # 只看有哪些账号，不改动
    python tools\\reset_admin_password.py --list

    # 随机生成一个密码并打印（不记进日志）
    python tools\\reset_admin_password.py --user admin --random

安全提示
--------
本工具直接读写本地库，等价于「能碰到这台机器的人就能改密码」。
这不是漏洞，而是本地单机工具的前提；一旦部署到服务器，
请确保 `data/` 目录的文件权限只对服务账号开放。
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import auth                      # noqa: E402
from core.db import init_db                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="重置账号密码")
    ap.add_argument("--user", default="", help="账号名")
    ap.add_argument("--password", default="", help="新密码（至少 4 位）")
    ap.add_argument("--random", action="store_true", help="随机生成密码")
    ap.add_argument("--list", action="store_true", help="只列出账号")
    args = ap.parse_args()

    init_db()
    auth.ensure_bootstrap()

    if args.list or not args.user:
        print("账号列表：")
        for u in auth.list_users():
            print(f"  {u['username']:<16} {u['display_name']:<12} "
                  f"{u['group_name']:<6} "
                  f"{'启用' if u['enabled'] else '停用'}  "
                  f"最后登录 {u['last_login_at'] or '从未'}")
        if not args.user:
            print("\n用 --user <账号> --password <新密码> 重置密码。")
        return 0

    user = auth.get_user_by_name(args.user)
    if not user:
        print(f"找不到账号「{args.user}」。用 --list 查看现有账号。")
        return 1

    pwd = args.password
    generated = False
    if args.random or not pwd:
        # 去掉容易看错的字符（0/O、1/l/I），方便口头或截图传递
        alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        pwd = "".join(secrets.choice(alphabet) for _ in range(14))
        generated = True

    if len(pwd) < 4:
        print("密码至少 4 位。")
        return 1

    try:
        auth.update_user(user["id"], password=pwd)
    except ValueError as exc:
        print(f"重置失败：{exc}")
        return 1

    auth.log_action("password.reset", user["username"], "命令行重置",
                    "tools/reset_admin_password")
    print(f"已重置「{user['username']}」的密码"
          f"（权限组 {user['group_name']}，已踢掉该账号所有登录会话）。")
    if generated:
        print(f"\n  新密码：{pwd}\n")
        print("  请立即登录并改成自己的密码 —— 上面这串只在本次输出里出现。")
    else:
        print("  新密码：你通过 --password 指定的那个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
