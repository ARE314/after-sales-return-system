# 售后返件登记系统 —— 容器镜像
#
# 与「手工装 Python + 建 .venv」相比，这条路径把环境也一起固化了：
#     docker compose up -d
# 就得到两个端口（8000 主界面 / 8100 数据接口）+ 每天自动备份。
#
# 构建：docker build -t ars:local .
# 运行：docker compose up -d      （推荐，见 docker-compose.yml）
#
# ⚠️ 本文件在本机**未经 docker build 实测**（这台机器没有装 Docker）。
#    结构与指令已按自检核对（tools/check_deploy.py 的 [6] 段），
#    首次在目标机 build 后请跑一次 docker compose exec ars \
#        python tools/check_deploy.py 与 tests/smoke_test.py 确认。

FROM python:3.12-slim

# Pillow 在 slim 镜像里通常有 manylinux wheel，但一旦没有 wheel 就会退回源码编译，
# 那时缺 libjpeg/zlib 会以一堆链接错误失败、报错完全不指向根因。
# 这两个是运行时/构建期都可能用到的，装上很便宜。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖清单再装 —— 代码改动不会让这一层缓存失效，重建快得多。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 非 root 运行：容器内被攻破也不该是 root。
# data/ 需要可写（五个库 + 照片 + 备份都在这里），所以先建好并授权。
RUN useradd --create-home --shell /usr/sbin/nologin ars \
 && mkdir -p /app/data \
 && chown -R ars:ars /app

USER ars

# 容器内必须监听 0.0.0.0，否则端口映射进不来（宿主侧再用 -p 127.0.0.1:8000:8000
# 收窄暴露范围）。open_api 同理。
ENV ARS_HOST=0.0.0.0 \
    ARS_PORT=8000 \
    ARS_OPEN_API=1 \
    ARS_OPEN_API_HOST=0.0.0.0 \
    ARS_OPEN_API_PORT=8100

EXPOSE 8000 8100

# 健康检查用 Python 而不是 curl：slim 镜像里没有 curl，装它只为健康检查不值。
# 8080 之外还探一下 8100，两个服务都活着才算健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import json,sys,urllib.request as u; \
d=[json.load(u.urlopen('http://127.0.0.1:%d/api/%s' % (p, q), timeout=3)) \
for p, q in ((8000, 'health'), (8100, 'open/health'))]; \
sys.exit(0 if all(x.get('ok') for x in d) else 1)"

CMD ["python", "app.py"]
