#!/usr/bin/env bash
# 售后返件登记系统 —— Linux / macOS 首次环境初始化
#
# 与 setup_env.bat 一一对应（Windows 用那个，Unix 用这个）：
#   创建 .venv + 按 requirements.txt 安装依赖。
# 装完就能 ./start.sh 起来。
#
# 用法：
#   ./setup_env.sh                 # 默认在项目目录建 .venv
#   PYTHON=python3.12 ./setup_env.sh   # 指定解释器
#   ./setup_env.sh --no-apt        # 不去装系统包（无 sudo 或已自备 Pillow 依赖）
#
# 为什么需要它：原先只有 Windows 的 setup_env.bat，Linux 上没有任何等价的
# "首次初始化" 入口 —— 文档让用户自己敲 `python -m venv .venv && pip install -r ...`，
# 一旦漏掉（或 requirements.txt 本身缺包）就要靠读堆栈排查。
set -euo pipefail
cd "$(dirname "$0")"

WANT_APT=1
for arg in "$@"; do
  case "$arg" in
    --no-apt) WANT_APT=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（可用：--no-apt）"; exit 2 ;;
  esac
done

# ---------- 1. 找一个可用的 Python ----------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "[ERROR] 找不到 Python。请先装 Python 3.11+："
  echo "        Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip"
  echo "        RHEL/CentOS  : sudo dnf install python3 python3-pip"
  exit 1
fi

VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "使用解释器: $PY (Python $VER)"
"$PY" - <<'EOF' || { echo "[ERROR] 需要 Python 3.11 及以上"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

# ---------- 2. Pillow 的系统依赖（Linux 常见缺失）----------
# Pillow 从源码构建时才需要这些；有 wheel 就用不上。缺 libjpeg 时
# `pip install Pillow` 会尝试编译并在链接阶段失败，报错信息很长且不指向根因，
# 所以这里先把常见系统包装上（失败也不致命，继续走 pip 看 wheel 能否命中）。
if [ "$WANT_APT" = "1" ] && command -v apt-get >/dev/null 2>&1; then
  MISSING=""
  for pkg in python3-venv python3-pip; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
  done
  if [ -n "$MISSING" ]; then
    echo "补装系统包:$MISSING"
    if command -v sudo >/dev/null 2>&1; then
      sudo apt-get update -qq && sudo apt-get install -y $MISSING || \
        echo "[WARN] 系统包安装失败，继续尝试 pip（venv 缺失会导致下一步失败）"
    else
      echo "[WARN] 没有 sudo，跳过系统包安装（若下一步失败请手工装$MISSING）"
    fi
  fi
fi

# ---------- 3. 建虚拟环境 ----------
if [ -x ".venv/bin/python" ]; then
  echo "已存在 .venv，复用（要重建就先删掉它）"
else
  echo "创建虚拟环境 .venv ..."
  "$PY" -m venv .venv || {
    echo "[ERROR] venv 创建失败。Debian/Ubuntu 上通常是缺 python3-venv："
    echo "        sudo apt install python3-venv"
    exit 1
  }
fi

# ---------- 4. 装依赖 ----------
echo "安装依赖（requirements.txt）..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# ---------- 5. 自检：关键包真的能导入 ----------
# 只报「安装成功」不够 —— 装上但导入失败（系统库缺失）同样起不来。
.venv/bin/python - <<'EOF' || { echo "[ERROR] 依赖导入失败，见上面的报错"; exit 1; }
import importlib, sys
need = {"fastapi": "fastapi", "uvicorn": "uvicorn",
        "openpyxl": "openpyxl", "PIL": "Pillow"}
bad = []
for mod, pkg in need.items():
    try:
        importlib.import_module(mod)
    except Exception as e:                                   # noqa: BLE001
        bad.append(f"{pkg} ({mod}): {e}")
if bad:
    print("以下依赖无法导入：")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("依赖自检通过：fastapi / uvicorn / openpyxl / Pillow")
EOF

echo
echo "完成。启动："
echo "    ./start.sh              # 仅本机 127.0.0.1:8000"
echo "    ./start.sh --lan        # 开放局域网"
echo "    ./start.sh --public     # 公网预设（另见 deploy/README.md 的上线清单）"
