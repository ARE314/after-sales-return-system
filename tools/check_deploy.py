"""核对 deploy/ 下的文件是否**真的能拿去用**（不是又一次「文档写了、脚本没实现」）。

守三件事：
  1. requirements.txt 必须覆盖代码里全部第三方 import（漏一个 = 新环境启动即崩）；
  2. deploy/ars.service 的注释字符必须是 `#`（`;` 会被 systemd 当成指令行）；
  3. deploy/nginx.conf.sample 必须是真 nginx 配置（含 Markdown 的 ``` / `>` 直接不可用）。
"""
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

FAIL = []
OPTIONAL = {}


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


# ---------------------------------------------------------------------------
# 1. 依赖完整性
# ---------------------------------------------------------------------------
print("== [1] requirements.txt 覆盖全部第三方 import ==")
stdlib = set(sys.stdlib_module_names)
# 本地模块 = 仓库内的顶层 .py + 含 __init__.py 的包目录（core / tests / tools …）
from pathlib import Path                                          # noqa: E402
local_mods = {p.stem for p in Path(".").rglob("*.py")}
for d in Path(".").rglob("__init__.py"):
    if d.parent != Path("."):
        local_mods.add(d.parent.name)
found = {}
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs
               if d not in ("data", "__pycache__", ".workbuddy", ".git", "node_modules")]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(root, f)
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        # **可选依赖**：写在 try/except ImportError 里的 import 不算硬依赖
        # （缺了会走降级分支）。把它们单独收起来，避免把「故意可选」误报成
        # 「requirements 漏包」—— 那种假报警会让人开始不信任这个检查。
        optional = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Try):
                continue
            handled = {getattr(h.type, "id", getattr(h.type, "attr", ""))
                       for h in n.handlers if h.type is not None}
            if not (handled & {"ImportError", "ModuleNotFoundError"}):
                continue
            for sub in ast.walk(n):
                if isinstance(sub, ast.Import):
                    optional.update(a.name.split(".")[0] for a in sub.names)
                elif isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                    optional.add(sub.module.split(".")[0])

        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in stdlib or m in local_mods:
                    continue
                if m in optional:
                    OPTIONAL.setdefault(m, set()).add(p)
                    continue
                found.setdefault(m, set()).add(p)

# import 名 → 发行包名（两者不总相同）
DIST_NAME = {"PIL": "Pillow", "uvicorn": "uvicorn", "fastapi": "fastapi",
             "openpyxl": "openpyxl"}
reqs = []
for line in open("requirements.txt", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#"):
        reqs.append(re.split(r"[<>=!\[;]", line)[0].strip().lower())

for mod, users in sorted(found.items()):
    dist = DIST_NAME.get(mod, mod)
    check(f"requirements.txt 含 {dist}（{mod}，被 {len(users)} 个文件引用）",
          dist.lower() in reqs, f"已声明: {reqs}")

for mod, users in sorted(OPTIONAL.items()):
    # 只提示、不断言：可选依赖本来就允许缺席
    print(f"  [NOTE] {mod} 是可选依赖（{sorted(users)} 在 try/except 里导入），"
          f"不进 requirements.txt")

# ---------------------------------------------------------------------------
# 2. systemd 单元
# ---------------------------------------------------------------------------
print("\n== [2] deploy/ars.service ==")
svc = open("deploy/ars.service", encoding="utf-8").read().splitlines()
semi = [i for i, l in enumerate(svc, 1) if l.lstrip().startswith(";")]
check("没有以 `;` 开头的行（systemd 只认 `#`）", not semi,
      f"发现 {len(semi)} 处：行 {semi[:6]}" if semi else "全部为 # 注释")
check("含 [Unit] / [Service] / [Install] 三节",
      all(f"[{s}]" in svc for s in ("Unit", "Service", "Install")))
check("ExecStart 指向 .venv 里的 python",
      any(l.startswith("ExecStart=") and ".venv/bin/python" in l for l in svc),
      next((l for l in svc if l.startswith("ExecStart=")), "(缺失)"))

# ---------------------------------------------------------------------------
# 3. nginx 配置
# ---------------------------------------------------------------------------
print("\n== [3] deploy/nginx.conf.sample ==")
ng = open("deploy/nginx.conf.sample", encoding="utf-8").read()
lines = ng.splitlines()
check("不含 Markdown 代码围栏 ```", "```" not in ng,
      f"{ng.count('```')} 处" if "```" in ng else "无")
md = [i for i, l in enumerate(lines, 1)
      if l.lstrip().startswith((">", "*", "|", "- "))]
check("不含 Markdown 引用/列表标记", not md,
      f"行 {md[:6]}" if md else "无")
check("含 server 块与 proxy_pass",
      "server {" in ng and "proxy_pass" in ng)
# 关键指令必须在：数据接口限速、静态证书
for directive in ("limit_req_zone", "ssl_certificate", "client_max_body_size"):
    check(f"含 {directive}", directive in ng)
# {} 配平（Markdown 版在这里就会挂）
check("花括号配平", ng.count("{") == ng.count("}"),
      f"{{ = {ng.count('{')} , }} = {ng.count('}')}")

# ---------------------------------------------------------------------------
# 4. 平台初始化脚本对齐
# ---------------------------------------------------------------------------
print("\n== [4] 首次初始化脚本（两平台对齐） ==")
check("存在 Windows 的 setup_env.bat", os.path.exists("setup_env.bat"))
check("存在 Unix 的 setup_env.sh", os.path.exists("setup_env.sh"))
if os.path.exists("setup_env.sh"):
    sh = open("setup_env.sh", encoding="utf-8").read()
    check("setup_env.sh 从 requirements.txt 装依赖", "requirements.txt" in sh)
    check("setup_env.sh 装了依赖后做导入自检",
          "importlib" in sh and "PIL" in sh,
          "缺了它会出现「pip 说装成功、启动仍崩」")
    check("setup_env.sh 带 shebang 且用 set -euo pipefail",
          sh.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in sh)
    # 可执行位在 Windows 上读不到，这里只做「提醒」，不做断言 ——
    # 写一条恒真的断言等于摆设（本项目踩过这种假绿）。
    print("  [NOTE] setup_env.sh / start.sh 需要可执行位："
          "部署后执行 chmod +x start.sh setup_env.sh")

# ---------------------------------------------------------------------------
# 5. 文档与实现一致（「文档写了、脚本没实现」是本项目踩过的坑）
# ---------------------------------------------------------------------------
print("\n== [5] 文档与实现一致 ==")
main = open("README.md", encoding="utf-8").read()
dep = open("deploy/README.md", encoding="utf-8").read()

check("README 的快速开始提到 setup_env.sh（两平台都给入口）",
      "setup_env.sh" in main)
check("README 的服务器部署提到 tools/check_deploy.py",
      "check_deploy.py" in main)
check("deploy/README 提到 check_deploy.py", "check_deploy.py" in dep)
check("deploy/README 讲清了 nginx 要先替换的占位",
      "server_name" in dep and "deny  all" in dep.replace("deny all", "deny  all"))
check("deploy/README 提醒了 systemd 的注释字符",
      "`#`" in dep and "systemd 只认" in dep)
# 文档里承诺的每个脚本都得真的存在
import re as _re                                                # noqa: E402
promised = set(_re.findall(r"(setup_env\.(?:sh|bat)|start\.(?:sh|bat))",
                           main + dep))
for name in sorted(promised):
    check(f"文档提到的 {name} 存在", os.path.exists(name))

print()

# ---------------------------------------------------------------------------
# 6. 容器部署（Dockerfile / compose）
# ---------------------------------------------------------------------------
print("== [6] 容器部署 ==")
if not os.path.exists("Dockerfile"):
    print("  [SKIP] 没有 Dockerfile")
else:
    df = open("Dockerfile", encoding="utf-8").read()
    df_lines = [l.strip() for l in df.splitlines()
                if l.strip() and not l.strip().startswith("#")]
    check("FROM python:3.12-slim", any(l.startswith("FROM python:3.12") for l in df_lines),
          next((l for l in df_lines if l.startswith("FROM")), "(无 FROM)"))
    check("先拷 requirements.txt 再装（利用层缓存）",
          df.find("COPY requirements.txt") < df.find("COPY . ."),
          "顺序反了每次改代码都要重装依赖")
    check("以非 root 用户运行", any(l.startswith("USER ") and l.split()[1] != "root"
                                    for l in df_lines),
          next((l for l in df_lines if l.startswith("USER")), "(没有 USER)"))
    check("暴露 8000 与 8100", "EXPOSE 8000" in df and "8100" in df)
    check("有 HEALTHCHECK", any(l.startswith("HEALTHCHECK") for l in df_lines))
    check("容器内监听 0.0.0.0（否则端口映射进不来）",
          "ARS_HOST=0.0.0.0" in df.replace(" ", ""))
    check("有启动命令", any(l.startswith(("CMD", "ENTRYPOINT")) for l in df_lines))
    # 备份容器要用 mysqldump 出库、mysql 导入影子库（tools/backup.py / check_backup.py）。
    # 少了它主服务照常跑，备份要到运行时才报「找不到 mysqldump」。
    check("镜像里有 mysql 客户端（备份要用 mysqldump）",
          "default-mysql-client" in df or "mysql-client" in df,
          "缺它备份会在运行时才失败")

    # .dockerignore：**data/ 绝不能进镜像**
    if not os.path.exists(".dockerignore"):
        check("存在 .dockerignore", False, "缺它会把 data/ 打进镜像")
    else:
        di = [l.strip().rstrip("/") for l in
              open(".dockerignore", encoding="utf-8").read().splitlines()
              if l.strip() and not l.strip().startswith("#")]
        check("data/ 被排除（里面有库文件、密码哈希、审计日志）",
              "data" in di, f"已排除: {di}")
        check("tests/_smoke.env 被排除（本机凭据）",
              "tests/_smoke.env" in di)
        check(".venv / __pycache__ 被排除", ".venv" in di and "__pycache__" in di)

# compose：优先按 YAML 解析，装不上 PyYAML 就退化成结构核对
if os.path.exists("docker-compose.yml"):
    raw = open("docker-compose.yml", encoding="utf-8").read()
    parsed = None
    try:
        import yaml                                              # noqa: PLC0415
        parsed = yaml.safe_load(raw)
    except ImportError:
        print("  [NOTE] 没装 PyYAML，compose 只做文本级核对")
    if parsed:
        svcs = parsed.get("services", {})
        check("定义了三个服务：mysql + ars + backup",
              set(svcs) == {"ars", "backup", "mysql"}, ", ".join(svcs))
        ports = [str(p) for p in (svcs.get("ars", {}).get("ports") or [])]
        check("端口只绑 127.0.0.1（对外交给反代）",
              bool(ports) and all(p.startswith("127.0.0.1:") for p in ports),
              str(ports))
        vols = [str(v) for v in (svcs.get("ars", {}).get("volumes") or [])]
        check("挂载 data 卷（否则容器一删照片与备份全没）",
              any("/app/data" in v for v in vols), str(vols))
        check("backup 服务也共享同一 data 卷",
              any("/app/data" in str(v) for v in
                  (svcs.get("backup", {}).get("volumes") or [])))
        env = svcs.get("ars", {}).get("environment") or {}
        check("初始管理员密码必须由外部注入（缺失则拒绝启动）",
              ":?" in str(env.get("ARS_BOOTSTRAP_PASSWORD", "")),
              str(env.get("ARS_BOOTSTRAP_PASSWORD")))
        check("主服务有健康检查", "healthcheck" in svcs.get("ars", {}))
        check("日志有大小上限（防 json 日志写满磁盘）",
              "logging" in svcs.get("ars", {}))

        # ---- 数据库：库是外部依赖，缺了这几项应用起不来 ----
        my = svcs.get("mysql", {})
        check("mysql 服务用 8.0 镜像", "mysql:8.0" in str(my.get("image", "")),
              str(my.get("image")))
        check("库文件挂在命名卷上（否则容器一删业务数据全没）",
              any("mysql-data" in str(v) and "/var/lib/mysql" in str(v)
                  for v in (my.get("volumes") or [])), str(my.get("volumes")))
        check("mysql 服务有健康检查（ars 靠它决定何时启动）",
              "healthcheck" in my)
        check("建库脚本挂在 initdb.d 上（否则六个 schema 不存在）",
              any("docker-entrypoint-initdb.d" in str(v)
                  for v in (my.get("volumes") or [])), str(my.get("volumes")))
        dep = svcs.get("ars", {}).get("depends_on") or {}
        check("ars 等库健康后再启动（连不上库时早报错）",
              "mysql" in dep and (dep.get("mysql") or {}).get("condition")
              == "service_healthy", str(dep))
        for svc in ("ars", "backup"):
            senv = svcs.get(svc, {}).get("environment") or {}
            check(f"{svc} 服务拿到了数据库地址与账号",
                  "ARS_MYSQL_HOST" in senv and "ARS_MYSQL_USER" in senv, ", ".join(senv))
            check(f"{svc} 服务的库口令由外部注入（缺失则拒绝启动）",
                  ":?" in str(senv.get("ARS_MYSQL_PASSWORD", "")),
                  str(senv.get("ARS_MYSQL_PASSWORD")))

        # ---- 建库脚本：六个 schema + 账号 + 影子库授权 ----
        init_sh = Path("deploy") / "mysql-init.sh"
        if not init_sh.exists():
            check("存在 deploy/mysql-init.sh（建六个 schema 与 ars 账号）", False,
                  "compose 挂了它，文件不在容器就起不来")
        else:
            ish = init_sh.read_text(encoding="utf-8")
            _all_schemas = all(s in ish for s in
                               ("returns_db", "inspect_db", "handle_db",
                                "items_db", "auth_db", "delivery_db"))
            check("建库脚本覆盖六个 schema", _all_schemas)
            check("建库脚本强制 mysql_native_password（PyMySQL 做不了 caching_sha2 的密钥交换）",
                  "mysql_native_password" in ish)
            check("建库脚本授了 verify\\_% 影子库（备份的还原演练要建临时库）",
                  "verify\\_%" in ish)
            check("建库脚本**不给**全局权限（演练写错也炸不到真库）",
                  "ON *.*" not in ish and "ALL PRIVILEGES ON *" not in ish,
                  "只授权到具体 schema")
    else:
        for needle, name in (
                ("services:", "有 services 段"),
                ("ars:", "有 ars 服务"),
                ("backup:", "有 backup 服务"),
                ("mysql:", "有 mysql 服务"),
                ("mysql-data", "库文件有命名卷"),
                ("127.0.0.1:8000:8000", "主界面绑定宿主本机"),
                ("./data:/app/data", "挂载 data 卷"),
                ("ARS_BOOTSTRAP_PASSWORD", "注入管理员密码"),
                ("ARS_MYSQL_PASSWORD", "注入数据库口令"),
                ("docker-entrypoint-initdb.d", "挂了建库脚本"),
                ("healthcheck", "有健康检查")):
            check(name, needle in raw)

    if os.path.exists(".env.example"):
        envx = open(".env.example", encoding="utf-8").read()
        check(".env.example 提醒改初始密码", "ARS_BOOTSTRAP_PASSWORD" in envx)
        check(".env.example 说明 Cookie Secure 与 HTTPS 的配套",
              "ARS_COOKIE_SECURE" in envx and "ARS_HTTPS_ONLY" in envx)
        check(".env.example 里有数据库连接项",
              "ARS_MYSQL_HOST" in envx and "ARS_MYSQL_PASSWORD" in envx,
              "compose 的 mysql 服务靠它建账号")
    else:
        check("存在 .env.example（compose 需要它生成 .env）", False)

# ---------------------------------------------------------------------------
# 6b. 局域网开放：启动脚本的三档预设 + 防火墙放行工具
# ---------------------------------------------------------------------------
print("\n== [6b] 局域网开放 ==")
if os.path.exists("start.bat"):
    sb = open("start.bat", encoding="utf-8").read()
    check("start.bat 的 --lan 把主界面绑到 0.0.0.0",
          "--lan" in sb and "ARS_HOST=0.0.0.0" in sb.replace(" ", ""),
          "绑在 127.0.0.1 上时同事连不上")
    check("start.bat 的 --public 才把数据接口一起对外",
          "ARS_OPEN_API_HOST=0.0.0.0" in sb.replace(" ", ""),
          "8100 跟着 8000 一起对外是额外暴露面")
else:
    check("存在 start.bat（Windows 启动脚本）", False)

# 放行防火墙这一步**刻意不放进应用**：服务不该自己去改防火墙。
# 于是它成了一个容易被漏掉的运维动作 —— 守在这里，别只在文档里写着。
fw = Path("tools") / "open_lan_firewall.bat"
if not fw.exists():
    check("存在 tools/open_lan_firewall.bat（放行 Windows 防火墙入站 8000）", False,
          "缺它同事打不开，而脚本又是唯一的放行入口")
else:
    fws = fw.read_text(encoding="utf-8", errors="replace")
    check("放行脚本针对 8000 端口", "8000" in fws)
    check("规则名与文档一致（收回时要能对上）",
          "ARS main UI TCP" in fws)
    check("能收回放行（/remove 分支）", "/remove" in fws)
    check("非管理员时自己提权（Start-Process -Verb RunAs）",
          "-Verb RunAs" in fws and "Start-Process" in fws)
    check("**不碰** 8100（数据接口不跟着对外）", "8100" not in fws,
          "8100 白名单为空等于不限制，绝不能顺手放开")
    check("全 ASCII（.bat 按 ANSI 码页读，中文在别的区域设置下会乱码）",
          all(ord(ch) < 128 for ch in fws))

# ---------------------------------------------------------------------------
# 7. 备份：工具 + 定时器
# ---------------------------------------------------------------------------
print("\n== [7] 数据备份 ==")
if os.path.exists("tools/backup.py"):
    bk = open("tools/backup.py", encoding="utf-8").read()
    check("用 mysqldump --single-transaction 做一致性快照（不是直接拷库文件）",
          "--single-transaction" in bk and "--databases" in bk,
          "InnoDB 边写边导会拿到半截事务；拷 datadir 更是直接拿到坏快照")
    check("口令走 --defaults-extra-file 的临时 cnf（不进命令行/不留痕）",
          "--defaults-extra-file" in bk and "_write_cnf" in bk,
          "口令写进 argv 会出现在进程列表里")
    check("dump 成功也要逐表核对（防「成功但只导了结构」）",
          "_dump_tables" in bk and "-- Dump completed" in bk)
    # 真正的检查：backup.py 的 DATABASES 必须**逐个覆盖** config.SCHEMAS。
    # ⚠️ 别只数「有几个库」—— 那跟 backup.py 声明了什么毫无关系：
    # 把 delivery 从 DATABASES 里删掉，库数一个不变，断言照样 PASS
    # （这个假绿是负向测试当场抓出来的，已改成实际比对 import 进来的清单）。
    from pathlib import Path as _P                                 # noqa: PLC0415
    import importlib.util as _ilu                                  # noqa: PLC0415
    _root = str(_P(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from config import SCHEMAS as _SCHEMAS                         # noqa: PLC0415

    _spec = _ilu.spec_from_file_location("_bk", "tools/backup.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    declared = {s for s, _label in _mod.DATABASES}
    missing = sorted(set(_SCHEMAS) - declared)
    check(f"备份清单覆盖全部 {len(_SCHEMAS)} 个库",
          not missing,
          f"DATABASES 声明 {len(declared)} 个：{sorted(declared)}"
          + (f"  ❌ 漏了 {missing}" if missing else ""))
    check("备份含照片目录（照片是磁盘文件，不在库里）", "PHOTO_DIR" in bk)
    check("写完才落 DONE 标记（残缺备份不冒充可用）", '"DONE"' in bk or "'DONE'" in bk)
    check("有轮转且只轮转自己产出的目录", "STAMP_RE" in bk and "keep" in bk)
    check("失败时写失败状态并返回非 0", "_write_status(False" in bk)
    check("支持 --dry-run / --list", "--dry-run" in bk and "--list" in bk)
else:
    check("存在 tools/backup.py", False)

if os.path.exists("tools/check_backup.py"):
    cb = open("tools/check_backup.py", encoding="utf-8").read()
    # 只比 sha256 与退出码是不够的：前者只证明文件没被改动过，
    # 后者只证明 mysqldump 自己觉得跑完了。恢复演练才是「能用」。
    check("备份自检真的做还原演练（导进影子库再逐表比行数）",
          "verify_" in cb and "_restore_drill" in cb and "mysql" in cb,
          "只比 sha256 = 只证明文件没变，不证明能恢复")
    check("还原演练用影子库、跑完必删（不会碰真库）",
          "DROP DATABASE IF EXISTS" in cb and "VERIFY_PREFIX" in cb)
    check("还原演练缺权限时给出补授权的具体命令", "GRANT ALL PRIVILEGES" in cb)
else:
    check("存在 tools/check_backup.py", False)

for unit, name in (("deploy/ars-backup.service", "备份 service"),
                   ("deploy/ars-backup.timer", "备份 timer")):
    if not os.path.exists(unit):
        check(f"存在 {name}", False)
        continue
    body = open(unit, encoding="utf-8").read()
    lines = body.splitlines()
    check(f"{name} 用 `#` 注释（无 `;` 开头的行）",
          not [l for l in lines if l.lstrip().startswith(";")])
    # ExecStart 不做变量展开：写了 ${...} 就是把字面量传给程序
    exe = [l for l in lines if l.startswith("ExecStart=")]
    check(f"{name} 的 ExecStart 不含 ${{...}}（systemd 不展开变量）",
          all("${" not in l for l in exe),
          str(exe) if any("${" in l for l in exe) else "无变量展开")

check("config 里有备份超期阈值", "BACKUP_STALE_HOURS" in
      open("config.py", encoding="utf-8").read())
check("界面能读到备份状态（/api/auth/settings 带 backup）",
      "backup_status" in open("app.py", encoding="utf-8").read())
check("存在 tools/check_backup.py（备份真实性守卫）",
      os.path.exists("tools/check_backup.py"))

# 真的核一遍最新备份能不能用（没有备份就 SKIP，不算失败）
print("\n  -- 调用 tools/check_backup.py --")
_r = subprocess.run([sys.executable, "-X", "utf8", "tools/check_backup.py"],
                    capture_output=True, text=True, encoding="utf-8")
for _line in (_r.stdout or "").splitlines():
    print("  " + _line)
if _r.returncode != 0:
    check("最新备份通过真实性核对", False,
          f"退出码 {_r.returncode}：{(_r.stdout or '')[-200:]}")

print()
if FAIL:
    print(f"未通过 {len(FAIL)} 项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过。")
