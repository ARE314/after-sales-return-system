#!/usr/bin/env python
"""本机开发用 MySQL（官方免安装 ZIP 版）的下载 / 解压 / 初始化 / 启停 / 建库

为什么用免安装 ZIP 版
---------------------
这台开发机**没有管理员权限**（`IsInRole(Administrator)` 实测为 False），装不了
Windows 服务（`mysqld --install` 需要提权）。所以走官方 winx64 ZIP：解压即用，
不注册服务、不写注册表、不动 Program Files。生产环境请按常规方式装 MySQL。

它装在哪
--------
默认 `C:\\Users\\Administrator\\.workbuddy\\binaries\\mysql`（**不在项目目录里**，
免得二进制被 git / 备份卷进去）。可用 `ARS_MYSQL_HOME` 覆盖。

```text
<HOME>\\mysql-8.0.29-winx64\\   mysqld / mysql 可执行文件
<HOME>\\data\\                  数据目录
<HOME>\\my.ini                  配置（utf8mb4 / 127.0.0.1:3306 / 原生密码认证）
<HOME>\\mysqld.err              错误日志
<HOME>\\credentials.json        root 与应用账号口令（仅本机开发用）
```

口令为什么用 mysql_native_password
-----------------------------------
MySQL 8 默认 `caching_sha2_password`，PyMySQL 在**非 TLS** 连接下要额外依赖
`cryptography` 才能完成首次认证。本机是 127.0.0.1 开发库，直接用原生密码认证
省掉一个二进制依赖；生产上请按需改回 `caching_sha2_password` 并开 TLS。

用法
----
    python tools/dev_mysql.py status
    python tools/dev_mysql.py setup     # 下载 + 解压 + 初始化 + 启动 + 建库 + 建账号 + 装 pymysql
    python tools/dev_mysql.py start | stop
    python tools/dev_mysql.py grant     # 幂等：给应用账号补 `verify_*` 影子库权限
    python tools/dev_mysql.py sql "SHOW DATABASES"

注意：脚本调用外部命令时把输出**重定向到文件**而不是管道 —— 受限模式下
子进程的管道 stdio 会 EPERM。日志落在项目 `data/_mysql_logs/`。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VERSION = os.environ.get("ARS_MYSQL_VERSION", "8.0.29")
HOME = Path(os.environ.get("ARS_MYSQL_HOME",
                           r"C:\Users\Administrator\.workbuddy\binaries\mysql"))
PORT = int(os.environ.get("ARS_MYSQL_PORT", "3306"))
HOST = "127.0.0.1"

ZIP_NAME = f"mysql-{VERSION}-winx64.zip"
MIRRORS = [
    "https://mirrors.huaweicloud.com/mysql/Downloads/MySQL-8.0/",
    "https://mirrors.aliyun.com/mysql/MySQL-8.0/",
]

BASE = HOME / f"mysql-{VERSION}-winx64"
DATADIR = HOME / "data"
INI = HOME / "my.ini"
ERRLOG = HOME / "mysqld.err"
CRED = HOME / "credentials.json"
LOGDIR = ROOT / "data" / "_mysql_logs"

SCHEMAS = ["returns_db", "inspect_db", "handle_db",
           "items_db", "auth_db", "delivery_db"]
APP_USER = "ars"

MYSQLD = BASE / "bin" / "mysqld.exe"
MYSQL = BASE / "bin" / "mysql.exe"


def log(msg: str) -> None:
    print(msg, flush=True)


def new_password(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def port_open(host: str = HOST, port: int = PORT, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def run(cmd: list, step: str, timeout: int = 600) -> int:
    """跑一条外部命令，stdout/stderr 一律落文件（受限模式下不要用管道）。"""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    out = LOGDIR / f"{step}.log"
    log(f"    $ {' '.join(str(c) for c in cmd)}")
    log(f"      -> {out.relative_to(ROOT)}")
    with open(out, "wb") as fh:
        proc = subprocess.run([str(c) for c in cmd], stdout=fh,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    return proc.returncode


def tail(step: str, lines: int = 25) -> str:
    p = LOGDIR / f"{step}.log"
    if not p.exists():
        return ""
    body = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(body[-lines:])


# --------------------------------------------------------------------------
# 下载 / 解压
# --------------------------------------------------------------------------

def download() -> bool:
    zip_path = HOME / ZIP_NAME
    if zip_path.exists() and zip_path.stat().st_size > 100 * 1024 * 1024:
        log(f"[download] 已有 {zip_path.name} "
            f"({zip_path.stat().st_size / 1048576:.1f} MB)，跳过")
        return True

    HOME.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for base in MIRRORS:
        url = base + ZIP_NAME
        log(f"[download] {url}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                mark = 0
                with open(zip_path, "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if got - mark >= 20 * 1048576:
                            mark = got
                            pct = f"{got * 100 // total}%" if total else "?"
                            log(f"           {got / 1048576:6.1f} MB / "
                                f"{total / 1048576:.1f} MB  ({pct})")
            size = zip_path.stat().st_size
            if total and size != total:
                last_err = f"大小不符：期望 {total} 实得 {size}"
                log(f"           ✗ {last_err}")
                continue
            log(f"           ✓ {size / 1048576:.1f} MB")
            return True
        except Exception as exc:                      # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"           ✗ {last_err}")
    log(f"[download] 全部镜像失败：{last_err}")
    return False


def extract() -> bool:
    if MYSQLD.exists():
        log(f"[extract] {MYSQLD} 已存在，跳过")
        return True
    zip_path = HOME / ZIP_NAME
    if not zip_path.exists():
        log("[extract] 压缩包不存在")
        return False
    log(f"[extract] 解压 {ZIP_NAME} -> {HOME}")
    # Windows 自带 bsdtar 能解 zip，比 Expand-Archive 快很多
    tar = shutil.which("tar")
    if tar:
        rc = run([tar, "-xf", str(zip_path), "-C", str(HOME)], "extract", timeout=1800)
    else:
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(HOME)
        rc = 0
    if rc != 0 or not MYSQLD.exists():
        log(f"[extract] 失败（rc={rc}）\n{tail('extract')}")
        return False
    log(f"[extract] ✓ {MYSQLD}")
    return True


# --------------------------------------------------------------------------
# 配置 / 初始化
# --------------------------------------------------------------------------

def write_ini() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    ini = f"""[mysqld]
# 本机开发实例：只监听回环，免安装 ZIP，不注册服务
basedir={BASE.as_posix()}
datadir={DATADIR.as_posix()}
port={PORT}
bind-address={HOST}
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci

# PyMySQL 在非 TLS 下走 caching_sha2_password 需要额外的 cryptography；
# 本机开发库用原生密码认证，省掉这个二进制依赖。
default_authentication_plugin=mysql_native_password

# 开发机不需要 binlog；关掉能省一半空间和不少 IO
skip-log-bin
max_connections=200
log-error={ERRLOG.as_posix()}

[client]
port={PORT}
default-character-set=utf8mb4
"""
    INI.write_text(ini, encoding="utf-8")
    log(f"[ini] 写入 {INI}")


def initialize() -> bool:
    if (DATADIR / "mysql").exists():
        log("[init] 数据目录已初始化，跳过")
        return True
    write_ini()
    DATADIR.parent.mkdir(parents=True, exist_ok=True)
    log("[init] mysqld --initialize-insecure（root 初始为空口令）")
    rc = run([MYSQLD, f"--defaults-file={INI}", "--initialize-insecure"],
             "initialize", timeout=900)
    if rc != 0 or not (DATADIR / "mysql").exists():
        log(f"[init] 失败（rc={rc}）\n{tail('initialize', 40)}")
        return False
    log("[init] ✓")
    return True


def start() -> bool:
    if port_open():
        log(f"[start] {HOST}:{PORT} 已在监听，跳过")
        return True
    if not MYSQLD.exists():
        log("[start] mysqld 不存在，先跑 setup")
        return False
    # 日志写进项目 data/_mysql_logs/，不写 HOME —— HOME 在项目目录之外，
    # 受限沙箱下 open() 会被拒（实测 PermissionError: mysqld.out）。
    logs = LOGDIR / "mysqld.out"
    LOGDIR.mkdir(parents=True, exist_ok=True)
    fh = open(logs, "ab")
    DETACHED = 0x00000008
    NEWGROUP = 0x00000200
    log(f"[start] 启动 mysqld（独立进程，控制台关闭也不退）")
    subprocess.Popen([str(MYSQLD), f"--defaults-file={INI}"], stdout=fh,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=DETACHED | NEWGROUP, close_fds=True)
    for i in range(60):
        time.sleep(1)
        if port_open():
            log(f"[start] ✓ {HOST}:{PORT} 就绪（{i + 1}s）")
            return True
    log(f"[start] ✗ 60 秒内没起来，见 {ERRLOG}")
    if ERRLOG.exists():
        log(tail_err())
    return False


def tail_err(lines: int = 30) -> str:
    if not ERRLOG.exists():
        return ""
    body = ERRLOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(body[-lines:])


def stop() -> None:
    if not port_open():
        log("[stop] 本来就没在跑")
        return
    if MYSQL.exists():
        cred = read_cred()
        args = [str(MYSQL), "-h", HOST, "-P", str(PORT), "-u", "root"]
        if cred.get("root_password"):
            args.append(f"-p{cred['root_password']}")
        run(args + ["-e", "SHUTDOWN"], "shutdown", timeout=120)
    for _ in range(30):
        if not port_open():
            break
        time.sleep(1)
    log("[stop] ✓" if not port_open() else "[stop] ✗ 仍在监听")


# --------------------------------------------------------------------------
# 口令与建库
# --------------------------------------------------------------------------

def read_cred() -> dict:
    if CRED.exists():
        return json.loads(CRED.read_text(encoding="utf-8"))
    return {}


def mysql_root(sql: str, step: str = "sql", password: str = "") -> int:
    args = [str(MYSQL), "-h", HOST, "-P", str(PORT), "-u", "root",
            "--default-character-set=utf8mb4"]
    if password:
        args.append(f"-p{password}")
    return run(args + ["-e", sql], step, timeout=180)


def bootstrap_accounts() -> bool:
    """建 6 个库 + 应用账号。已建过则直接复用既有口令。"""
    cred = read_cred()
    if cred.get("ready"):
        log("[db] credentials.json 标记 ready，跳过")
        return True
    cred = {"version": VERSION, "host": HOST, "port": PORT,
            "schemas": SCHEMAS, "app_user": APP_USER}
    root_pwd = new_password()
    app_pwd = new_password()
    cred["root_password"] = root_pwd
    cred["app_password"] = app_pwd

    log("[db] 设置 root 口令并建库建账号")
    if mysql_root(f"ALTER USER 'root'@'localhost' "
                  f"IDENTIFIED WITH mysql_native_password BY '{root_pwd}';",
                  "set_root", password="") != 0:
        log(f"[db] ✗ 设置 root 口令失败\n{tail('set_root')}")
        return False

    stmts = [f"CREATE DATABASE IF NOT EXISTS {s} "
             f"CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
             for s in SCHEMAS]
    stmts += [
        f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{HOST}' "
        f"IDENTIFIED WITH mysql_native_password BY '{app_pwd}';",
        f"CREATE USER IF NOT EXISTS '{APP_USER}'@'localhost' "
        f"IDENTIFIED WITH mysql_native_password BY '{app_pwd}';",
    ]
    for s in SCHEMAS:
        stmts.append(f"GRANT ALL PRIVILEGES ON {s}.* TO '{APP_USER}'@'{HOST}';")
        stmts.append(f"GRANT ALL PRIVILEGES ON {s}.* TO '{APP_USER}'@'localhost';")
    stmts.append("FLUSH PRIVILEGES;")
    if mysql_root(" ".join(stmts), "create_db", password=root_pwd) != 0:
        log(f"[db] ✗ 建库失败\n{tail('create_db')}")
        return False

    cred["ready"] = True
    CRED.write_text(json.dumps(cred, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    log(f"[db] ✓ 6 个库 + 账号 {APP_USER} 就绪，口令写入 {CRED}")
    return True


def grant_verify() -> bool:
    """给应用账号补上影子库权限（备份「还原演练」要用）。

    tools/check_backup.py 会把 dump 导进 `verify_<schema>` 再逐表比行数 ——
    那是「备份能用」唯一有说服力的证据。但 bootstrap_accounts 只把权限授到
    六个真库上，应用账号建不了影子库，演练必然失败。

    这里按**库名模式**授权（`verify\\_%`，`\\_` 让下划线当字面量），不放开
    全局 CREATE/DROP DATABASE —— 演练只需要动这几个影子库。
    GRANT 可重复执行，所以不走 credentials.json 的 ready 短路。
    """
    cred = read_cred()
    if not cred.get("app_password"):
        log(f"[db] ✗ 还没有 {CRED}，先跑 setup")
        return False
    stmts = [
        f"GRANT ALL PRIVILEGES ON `verify\\_%`.* TO '{APP_USER}'@'{HOST}';",
        f"GRANT ALL PRIVILEGES ON `verify\\_%`.* TO '{APP_USER}'@'localhost';",
        "FLUSH PRIVILEGES;",
    ]
    if mysql_root(" ".join(stmts), "grant_verify",
                  password=cred.get("root_password", "")) != 0:
        log(f"[db] ✗ 影子库授权失败\n{tail('grant_verify')}")
        return False
    log("[db] ✓ 已授权影子库 `verify_*`（备份还原演练用）")
    return True


def pip_install() -> bool:
    log("[pip] 安装 PyMySQL 到 venv")
    rc = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
              "PyMySQL"], "pip", timeout=600)
    if rc != 0:
        log(f"[pip] ✗ rc={rc}\n{tail('pip')}")
        return False
    log("[pip] ✓")
    return True


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def setup() -> int:
    log(f"目标：MySQL {VERSION} @ {HOME}  端口 {PORT}")
    for step, fn in (("下载", download), ("解压", extract), ("初始化", initialize),
                     ("启动", start), ("建库", bootstrap_accounts),
                     ("影子库授权", grant_verify), ("PyMySQL", pip_install)):
        log(f"\n=== {step} ===")
        if not fn():
            log(f"\n✗ 卡在「{step}」")
            return 1
    log("\n=== 完成 ===")
    return status()


def status() -> int:
    cred = read_cred()
    log(f"安装目录 : {HOME}")
    log(f"mysqld   : {'✓ ' + str(MYSQLD) if MYSQLD.exists() else '✗ 未解压'}")
    log(f"数据目录 : {'✓ 已初始化' if (DATADIR / 'mysql').exists() else '✗ 未初始化'}")
    log(f"监听     : {'✓ ' + HOST + ':' + str(PORT) if port_open() else '✗ 未监听'}")
    log(f"credentials.json : {'✓' if cred.get('ready') else '✗'}")
    if cred.get("ready"):
        log(f"  应用账号 : {cred['app_user']} / {cred['app_password']}")
        log(f"  root     : root / {cred['root_password']}")
        log(f"  库       : {', '.join(cred['schemas'])}")
    try:
        import pymysql
        log(f"PyMySQL  : ✓ {pymysql.__version__}")
    except Exception as exc:                          # noqa: BLE001
        log(f"PyMySQL  : ✗ {exc}")
    return 0 if (port_open() and cred.get("ready")) else 1


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "setup":
        return setup()
    if cmd == "status":
        return status()
    if cmd == "grant":
        return 0 if grant_verify() else 1
    if cmd == "download":
        return 0 if download() else 1
    if cmd == "start":
        return 0 if start() else 1
    if cmd == "stop":
        stop()
        return 0
    if cmd == "sql":
        cred = read_cred()
        sql = argv[2] if len(argv) > 2 else "SELECT VERSION()"
        return mysql_root(sql, "sql_manual", password=cred.get("root_password", ""))
    log(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
