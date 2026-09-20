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
        check("定义了两个服务：ars + backup", set(svcs) == {"ars", "backup"},
              ", ".join(svcs))
        ports = [str(p) for p in (svcs.get("ars", {}).get("ports") or [])]
        check("端口只绑 127.0.0.1（对外交给反代）",
              bool(ports) and all(p.startswith("127.0.0.1:") for p in ports),
              str(ports))
        vols = [str(v) for v in (svcs.get("ars", {}).get("volumes") or [])]
        check("挂载 data 卷（否则容器一删数据全没）",
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
    else:
        for needle, name in (
                ("services:", "有 services 段"),
                ("ars:", "有 ars 服务"),
                ("backup:", "有 backup 服务"),
                ("127.0.0.1:8000:8000", "主界面绑定宿主本机"),
                ("./data:/app/data", "挂载 data 卷"),
                ("ARS_BOOTSTRAP_PASSWORD", "注入管理员密码"),
                ("healthcheck", "有健康检查")):
            check(name, needle in raw)

    if os.path.exists(".env.example"):
        envx = open(".env.example", encoding="utf-8").read()
        check(".env.example 提醒改初始密码", "ARS_BOOTSTRAP_PASSWORD" in envx)
        check(".env.example 说明 Cookie Secure 与 HTTPS 的配套",
              "ARS_COOKIE_SECURE" in envx and "ARS_HTTPS_ONLY" in envx)
    else:
        check("存在 .env.example（compose 需要它生成 .env）", False)

# ---------------------------------------------------------------------------
# 7. 备份：工具 + 定时器
# ---------------------------------------------------------------------------
print("\n== [7] 数据备份 ==")
if os.path.exists("tools/backup.py"):
    bk = open("tools/backup.py", encoding="utf-8").read()
    check("用 VACUUM INTO 做一致性快照（不是直接拷文件）",
          "VACUUM INTO" in bk,
          "WAL 模式下直接拷 .db 会丢未 checkpoint 的事务")
    check("备份五个库 + 照片目录", "PHOTO_DIR" in bk and "DATABASES" in bk)
    check("写完才落 DONE 标记（残缺备份不冒充可用）", '"DONE"' in bk or "'DONE'" in bk)
    check("有轮转且只轮转自己产出的目录", "STAMP_RE" in bk and "keep" in bk)
    check("失败时写失败状态并返回非 0", "_write_status(False" in bk)
    check("支持 --dry-run / --list", "--dry-run" in bk and "--list" in bk)
else:
    check("存在 tools/backup.py", False)

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
