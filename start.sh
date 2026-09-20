#!/usr/bin/env bash
# 售后返件登记系统 —— Linux / macOS 启动脚本
#
# 用法：
#   ./start.sh                  # 用内置默认值（127.0.0.1:8000）
#   ./start.sh --lan            # 开放局域网访问（0.0.0.0:8000）
#   ./start.sh --public         # 公网部署预设（含数据接口对外 + HTTPS 相关开关）
#   ./start.sh --local          # 强制只听本机（压过环境变量与上面两个预设）
#   ARS_PORT=9000 ./start.sh    # 环境变量覆盖任意配置项
#
# 三档与 Windows 的 start.bat 完全一致（改动时两边要一起改）。
#
# Windows 上用 start.bat；本脚本只是同一件事的 Unix 版本。
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for cand in .venv/bin/python venv/bin/python python3 python; do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "找不到 Python。请先创建虚拟环境："
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

for arg in "$@"; do
  case "$arg" in
    --local)
      # 强制本机。注意这里**不用** ${ARS_HOST:-...} —— --local 的语义是
      # 「断言只监听本机」，要压过环境变量与 --lan / --public，
      # 与 app.py 里那个 --local 开关一致（那边也是无条件置 127.0.0.1）。
      export ARS_HOST=127.0.0.1
      ;;
    --lan)
      export ARS_HOST="${ARS_HOST:-0.0.0.0}"
      ;;
    --public)
      # 公网预设：主界面与数据接口都对外；Cookie 安全项留给反代场景
      export ARS_HOST="${ARS_HOST:-0.0.0.0}"
      export ARS_OPEN_API_HOST="${ARS_OPEN_API_HOST:-0.0.0.0}"
      echo "[提示] 公网部署请务必："
      echo "       1) 先改掉初始管理员密码"
      echo "       2) 经反向代理启用 HTTPS 后设 ARS_COOKIE_SECURE=1"
      echo "       3) 在「数据接口」页重置访问令牌并设置来源 IP 白名单"
      echo
      ;;
  esac
done

# 只透传非选项参数（host / port）。其实 app.py 现在会忽略所有 "-" 开头的
# 参数，透传 --lan 也不会被当成 host —— 但这里仍然过滤，避免以后 app.py
# 改变参数处理方式时出现「某天开始把开关当监听地址」的哑巴故障。
# Windows 的 start.bat 行为与此一致（--lan / --public / --local 三档）。
PASS=()
for arg in "$@"; do
  case "$arg" in
    --*) ;;
    *) PASS+=("$arg") ;;
  esac
done

echo "Python: $PY"
echo "主界面: ${ARS_HOST:-127.0.0.1}:${ARS_PORT:-8000}"
if [ "${ARS_OPEN_API:-1}" != "0" ]; then
  echo "数据接口: ${ARS_OPEN_API_HOST:-127.0.0.1}:${ARS_OPEN_API_PORT:-8100}"
fi
echo

exec "$PY" app.py ${PASS[@]+"${PASS[@]}"}
