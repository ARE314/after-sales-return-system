"""数据开放接口 —— 独立端口的对外服务

为什么单独一个应用、单独一个端口
--------------------------------
1. 主界面要面对人、这个服务要面对机器（金山文档定时任务）。两者的
   安全边界不同：主界面靠会话登录，这里靠令牌 + 来源 IP 白名单。
   混在同一个端口上，防火墙只能整体放行，粒度太粗。
2. 独立端口可以在反向代理 / 安全组上单独放行，主界面端口继续留在内网。
3. 独立应用便于将来自立门户（单独进程、单独容器），路由前缀也已隔离
   （`/api/open/*`），迁移时不用改调用方。

启动方式
--------
默认由 app.py 在后台线程里拉起（`config.OPEN_API_ENABLED = True`），
也可以只跑这一个服务：

    python open_api.py            # 默认 127.0.0.1:8100
    ARS_OPEN_API_HOST=0.0.0.0 python open_api.py

鉴权（三重，任一不过即拒绝）
----------------------------
    Authorization: Bearer <token>     ← 或 ?token= / X-API-Token
    来源 IP 在白名单内（白名单为空 = 不限制）
    请求的数据集在允许范围内
"""
import csv
import io
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import (OPEN_API_ENABLED, OPEN_API_HOST, OPEN_API_PORT,
                    PORT, PUBLIC_BASE_URL)
from core import openapi as oa
from core.db import init_db

SERVICE = "售后返件登记系统 · 数据开放接口"
VERSION = "1.0.0"
# 单次返回上限：金山侧写入是按批的，一次几万行没有意义还容易超时
MAX_LIMIT = 20000

# 启动状态（幂等保护，见 start_in_thread）
_start_lock = threading.Lock()
_state: dict = {}


def state() -> dict:
    """当前开放接口的运行状态（供主服务展示端口与存活情况）。"""
    return dict(_state)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


open_app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan,
                   docs_url="/api/open/docs", redoc_url=None)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """取调用方 IP。

    注意：**不信任** X-Forwarded-For，除非将来的部署里明确了受信任代理
    （主服务的 config.TRUSTED_PROXIES 是给界面用的）。开放接口的来源
    白名单是安全边界，能被请求头伪造就等于白名单失效，所以这里只取
    传输层对端地址。
    """
    return request.client.host if request.client else ""


def _token_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-token", "")
            or request.query_params.get("token", "")).strip()


def require_access(request: Request, dataset: str = ""):
    """校验三重条件，返回调用方 IP。不通过直接抛 HTTPException。"""
    ip = _client_ip(request)
    if not oa.client_allowed(ip):
        oa.log_pull(ip, dataset, request.url.path, 0, False,
                    "来源 IP 不在白名单")
        raise HTTPException(403, {"code": "ip_denied",
                                  "message": "来源地址不在白名单内"})
    token = _token_of(request)
    if not token or token != oa.get_token():
        oa.log_pull(ip, dataset, request.url.path, 0, False, "令牌无效")
        raise HTTPException(401, {"code": "bad_token",
                                  "message": "访问令牌无效或缺失"})
    # 先判「有没有这个数据集」，再判「有没有授权」——
    # 顺序反过来的话，拼错数据集名会得到「未被授权」，配置的人会去翻授权设置，
    # 而真正的问题是名字写错了。
    if dataset and dataset not in oa.DATASETS:
        oa.log_pull(ip, dataset, request.url.path, 0, False, "数据集不存在")
        raise HTTPException(404, {"code": "unknown_dataset",
                                  "message": f"没有名为「{dataset}」的数据集"})
    if dataset and dataset not in oa.scopes():
        oa.log_pull(ip, dataset, request.url.path, 0, False, "数据集未授权")
        raise HTTPException(403, {"code": "scope_denied",
                                  "message": f"数据集「{dataset}」未被授权"})
    return ip


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@open_app.get("/api/open/health")
def open_health():
    """连通性探测：**不需要令牌**，只回最小信息。

    金山侧配好定时任务后第一件事就是测通不通，这时还不方便带令牌；
    返回内容里没有任何业务数据，也不含令牌本身，可以安全暴露。
    """
    return {"ok": True, "service": SERVICE, "version": VERSION,
            "time": datetime.now().isoformat(timespec="seconds")}


@open_app.get("/api/open/ping")
def open_ping(request: Request):
    ip = require_access(request)
    oa.log_pull(ip, "", request.url.path, 0, True, "连通性测试")
    return {"ok": True, "message": "令牌有效", "scopes": oa.scopes(),
            "time": datetime.now().isoformat(timespec="seconds")}


@open_app.get("/api/open/datasets")
def open_datasets(request: Request):
    """可用数据集与字段清单 —— 金山侧据此建表头。"""
    ip = require_access(request)
    allowed = set(oa.scopes())
    items = [{k: v for k, v in d.items() if k != "columns"}
             for d in oa.dataset_list() if d["key"] in allowed]
    oa.log_pull(ip, "", request.url.path, len(items), True, "数据集清单")
    return {"ok": True, "datasets": items, "scopes": oa.scopes()}


@open_app.get("/api/open/data/{dataset}")
def open_data(dataset: str, request: Request,
              since: str = Query("", description="增量起点（updated_at >）"),
              limit: int = Query(2000, ge=1, le=MAX_LIMIT),
              offset: int = Query(0, ge=0)):
    """按数据集取数（JSON）。`next_offset` 非 0 时带上一页继续翻。"""
    ip = require_access(request, dataset)
    try:
        data = oa.fetch(dataset, since=since, limit=limit, offset=offset)
    # BadSince 必须排在 ValueError 之前 —— 它不继承 ValueError，
    # 但如果哪天改成继承了，顺序错了就会把它报成「数据集不存在」。
    except oa.BadSince as exc:
        oa.log_pull(ip, dataset, request.url.path, 0, False, str(exc))
        raise HTTPException(400, {"code": "bad_since",
                                  "message": str(exc)}) from exc
    except ValueError as exc:
        oa.log_pull(ip, dataset, request.url.path, 0, False, str(exc))
        raise HTTPException(404, {"code": "unknown_dataset",
                                  "message": str(exc)}) from exc
    oa.log_pull(ip, dataset, request.url.path, data["count"], True,
                f"since={since or '-'} offset={offset}")
    return {"ok": True, **data}


@open_app.get("/api/open/export/{dataset}.{fmt}")
def open_export(dataset: str, fmt: str, request: Request,
                since: str = Query("", description="增量起点"),
                limit: int = Query(MAX_LIMIT, ge=1, le=MAX_LIMIT)):
    """导出文件（csv / xlsx）—— 定时任务想要「下载一个文件」时用这个。"""
    fmt = fmt.lower()
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(400, {"code": "bad_format",
                                  "message": "仅支持 csv / xlsx"})
    ip = require_access(request, dataset)
    try:
        data = oa.fetch(dataset, since=since, limit=limit)
    except oa.BadSince as exc:            # 见 open_data 里的顺序说明
        oa.log_pull(ip, dataset, request.url.path, 0, False, str(exc))
        raise HTTPException(400, {"code": "bad_since",
                                  "message": str(exc)}) from exc
    except ValueError as exc:
        oa.log_pull(ip, dataset, request.url.path, 0, False, str(exc))
        raise HTTPException(404, {"code": "unknown_dataset",
                                  "message": str(exc)}) from exc

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{dataset}-{stamp}.{fmt}"
    oa.log_pull(ip, dataset, request.url.path, data["count"], True,
                f"导出 {name}")

    if fmt == "csv":
        buf = io.StringIO()
        buf.write("\ufeff")            # BOM：让 Excel 认出 UTF-8，不然中文乱码
        w = csv.writer(buf)
        w.writerow(data["columns"])
        for row in data["rows"]:
            w.writerow(["" if row.get(c) is None else row.get(c)
                        for c in data["columns"]])
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = data["label"]
    ws.append(data["columns"])
    for row in data["rows"]:
        ws.append([row.get(c) for c in data["columns"]])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@open_app.exception_handler(HTTPException)
async def _http_error(_request: Request, exc: HTTPException):
    """统一错误体：金山侧的脚本只要看 `ok` 就能判断成败。"""
    detail = exc.detail
    if isinstance(detail, dict):
        body = {"ok": False, **detail}
    else:
        body = {"ok": False, "message": str(detail)}
    return JSONResponse(body, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def serve(host: str = "", port: int = 0, log_level: str = "warning") -> None:
    """阻塞式启动（`python open_api.py` 时用）。

    在 app.py 里是以守护线程启动的，那时不会走 log_level="info"，
    避免把主服务的启动日志淹没。
    """
    import uvicorn
    init_db()
    uvicorn.run(open_app, host=host or OPEN_API_HOST,
                port=port or OPEN_API_PORT, log_level=log_level)


def start_in_thread(host: str = "", port: int = 0) -> dict:
    """给主服务用：后台线程拉起开放接口。

    **幂等**：主服务的 `main()` 与 FastAPI 的 `lifespan` 都会调这里
    （前者为了在启动横幅里打印端口，后者保证被别的 ASGI 容器托管时也能起），
    重复调用直接返回首次的状态 —— 否则第二次会在同一个端口上重复绑定，
    报 `winerror 10048`（端口已被占用），只在日志里留一行 ERROR，很容易漏看。

    关闭信号交给主服务处理，子服务不注册信号处理器
    （否则 Ctrl+C 会被两个服务抢着处理，日志里两遍关闭流程）。
    """
    import uvicorn
    from uvicorn import Config, Server

    with _start_lock:
        if _state.get("server"):
            return _state
        cfg = Config(open_app, host=host or OPEN_API_HOST,
                     port=port or OPEN_API_PORT, log_level="warning")
        server = Server(cfg)
        t = threading.Thread(target=server.run, name="open-api", daemon=True)
        t.start()
        _state.update({"thread": t, "server": server,
                       "host": cfg.host, "port": cfg.port})
        return _state


def main() -> None:
    if not OPEN_API_ENABLED:
        print("数据开放接口已在配置中关闭（config.OPEN_API_ENABLED = False）")
        return
    host = sys.argv[1] if len(sys.argv) > 1 else OPEN_API_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else OPEN_API_PORT
    base = PUBLIC_BASE_URL or f"http://{host}:{port}"
    print("=" * 60)
    print(f"  {SERVICE}")
    print("-" * 60)
    print(f"  监听        {host}:{port}")
    print(f"  连通性测试  {base}/api/open/health   （无需令牌）")
    print(f"  数据集清单  {base}/api/open/datasets （需令牌）")
    print(f"  接口文档    {base}/api/open/docs")
    if host in ("127.0.0.1", "localhost"):
        print("  提示：当前只监听本机。要让金山文档定时任务访问，"
              "需改为 0.0.0.0 并放行该端口。")
    webbase = PUBLIC_BASE_URL or f"http://127.0.0.1:{PORT}"
    print(f"  配置界面    {webbase}/api.html （主服务，需登录）")
    print("  停止服务    按 Ctrl+C")
    print("=" * 60)
    serve(host, port, log_level="info")


if __name__ == "__main__":
    main()
