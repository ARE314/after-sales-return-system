# 服务器部署说明

本目录存放**服务器部署**要用的文件与约定。程序当前以本地单机方式运行
（`HOST = 127.0.0.1`），要上线按下面的顺序走即可，**不需要改代码**。

| 文件 | 是什么 | 能不能直接用 |
|---|---|---|
| `ars.service` | systemd 单元 | ✅ 改掉 `User` / 路径 / `ARS_*` 即可 |
| `nginx.conf.sample` | Nginx 反向代理配置 | ✅ 改掉域名 / 证书 / 网段 / 端口即可 |
| `README.md` | 本说明 | — |
| `../setup_env.sh` `../setup_env.bat` | 首次初始化（建 .venv + 装依赖 + 导入自检）| ✅ 直接跑（在项目根目录）|
| `../start.sh` `../start.bat` | 启动（`--lan` / `--public` / `--local` 三档）| ✅ 直接跑（在项目根目录）|

改完部署文件后跑一次守卫自检，确认没写成「文档能用、文件不能跑」：

```bash
python tools/check_deploy.py
```

它守四件事：`requirements.txt` 覆盖全部第三方 import、`ars.service` 的注释字符
合法、`nginx.conf.sample` 是**真配置**（不含 Markdown 标记）、两平台的初始化脚本
都在。最后一条的由来：这个文件里曾经只有 Markdown 正文（含 ```` ``` ```` 围栏），
`cp` 到 `sites-enabled` 后 `nginx -t` 必然失败。

## 一、两个服务，两个端口

| 服务 | 端口 | 面向谁 | 鉴权方式 | 启动方式 |
|---|---|---|---|---|
| 主界面 + 业务接口 | `8000` | 人（浏览器） | 会话 Cookie + 登录 | `python app.py` |
| 数据开放接口 | `8100` | 机器（金山文档定时任务） | 令牌 + 来源 IP 白名单 | 主服务自动带起（守护线程） |

分开的理由：两者的安全边界不同。主界面要给人用，凭的是"登录会话"；开放接口
给机器用，凭的是"令牌"。混在一个端口上，防火墙只能整体放行，粒度太粗。

也可以只跑开放接口（例如把它单独放到一台机器）：

```bash
python open_api.py                       # 默认 127.0.0.1:8100
ARS_OPEN_API_HOST=0.0.0.0 python open_api.py
```

## 二、环境变量一览

程序读取的所有部署相关配置都在这里。**优先级：命令行参数 > 环境变量 > 内置默认值**。

### 服务监听

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARS_HOST` | `127.0.0.1` | 主界面监听地址。`0.0.0.0` 表示局域网/公网可访问 |
| `ARS_PORT` | `8000` | 主界面端口 |

### 公开访问

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARS_PUBLIC_BASE_URL` | 空 | 对外地址，如 `https://returns.example.com` |
| `ARS_CORS_ORIGINS` | 空 | 允许跨域的来源（逗号分隔）。同源部署留空 |
| `ARS_TRUSTED_PROXIES` | 空 | 受信任代理地址。**只有配了才信任 `X-Forwarded-For`** |
| `ARS_COOKIE_SECURE` | `0` | HTTPS 部署**必须**置 `1`，否则会话 Cookie 明文传输 |
| `ARS_HTTPS_ONLY` | `0` | 置 `1` 后拒绝明文 HTTP 登录 |
| `ARS_SESSION_HOURS` | `12` | 会话有效期（小时） |

### 登录与权限

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARS_AUTH_ENABLED` | `1` | 登录验证开关的**初始值**；运行时可界面上切换 |
| `ARS_BOOTSTRAP_USER` | `admin` | 首个管理员账号（仅用户表为空时创建）|
| `ARS_BOOTSTRAP_PASSWORD` | `admin123` | 首个管理员的初始密码（首次登录强制改密）|

### 数据开放接口

| 变量 | 默认 | 说明 |
|---|---|---|
| `ARS_OPEN_API` | `1` | 是否启用开放接口 |
| `ARS_OPEN_API_HOST` | `127.0.0.1` | `0.0.0.0` 才允许外部访问 |
| `ARS_OPEN_API_PORT` | `8100` | 开放接口端口 |
| `ARS_OPEN_API_TOKEN` | 空 | 留空则首次启动随机生成（界面可查看掩码 / 重置）|
| `ARS_OPEN_API_IPS` | 空 | 来源白名单，支持单 IP 与 CIDR。**留空 = 不限制** |
| `ARS_OPEN_API_SCOPES` | `detail` | 默认授权数据集 |

## 三、首次安装（Linux）

两平台都有「首次初始化」入口，装完即可启动：

```bash
# Linux / macOS
./setup_env.sh              # 建 .venv + 按 requirements.txt 装依赖 + 导入自检
./start.sh --lan            # 启动（--lan / --public / --local 三档）

# Windows
setup_env.bat               # 同上
start.bat --lan
```

`setup_env.sh` 会顺带补 `python3-venv` 等系统包（Debian/Ubuntu，需 sudo；
不想动系统就用 `--no-apt`），并在最后**真的 import 一遍**四个依赖 ——
只报「pip 安装成功」不够，装了但导入失败（缺系统库）同样起不来。

> **为什么必须核对依赖**：`Pillow` 曾经漏在 `requirements.txt` 里。
> `core/photos.py` 的 `from PIL import ...` 在模块顶层，缺它会让 `app.py`
> **连启动都启动不了**，而按文档三步部署的人只会看到一个 ModuleNotFoundError。
> 现在有守卫：`python tools/check_deploy.py` 扫描全仓第三方 import 与
> `requirements.txt` 比对，少任何一个直接报错。

## 四、反向代理（Nginx）

`deploy/nginx.conf.sample` 是**可直接 include 的 nginx 配置**（不是文档）。
两个 `server` 块：数据接口对外（只放行金山侧网段）、主界面只监听内网。

```bash
sudo cp deploy/nginx.conf.sample /etc/nginx/sites-available/ars.conf
sudo ln -s /etc/nginx/sites-available/ars.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**必须先替换四处**，否则 `nginx -t` 直接失败：

| 占位 | 说明 |
|---|---|
| `server_name returns-api.example.com` | 你的域名 |
| `ssl_certificate` / `ssl_certificate_key` | 证书路径 |
| `allow 203.0.113.0/24` | 金山侧真实出口网段（其余 `deny all`）|
| `proxy_pass` 里的 `8000` / `8100` | 改过 `ARS_PORT` / `ARS_OPEN_API_PORT` 时同步 |

程序侧对应设置：

```ini
ARS_HOST=127.0.0.1
ARS_PORT=8000
ARS_PUBLIC_BASE_URL=https://returns-api.example.com
ARS_COOKIE_SECURE=1                 # HTTPS 下必须开，否则 Cookie 明文传
ARS_TRUSTED_PROXIES=127.0.0.1       # 只有配了它才信任 X-Forwarded-For
ARS_OPEN_API_HOST=127.0.0.1         # 由 Nginx 转发，程序本身不必监听 0.0.0.0
ARS_OPEN_API_PORT=8100
```

> 代理侧的 `allow/deny` 与程序内的 `ARS_OPEN_API_IPS` 是**两层独立的白名单**，
> 互为双保险，两边都要配。

## 五、systemd 托管

```bash
sudo useradd -r -s /usr/sbin/nologin ars
sudo cp deploy/ars.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ars
journalctl -u ars -f
```

| 要改的 | 默认 | 说明 |
|---|---|---|
| `User` / `Group` | `ars` | 先建好这个系统账号 |
| `WorkingDirectory` / `ExecStart` | `/opt/ars` | 项目实际路径 |
| `Environment=ARS_*` | 见文件内注释 | 按实际域名 / 端口调整 |

> `ars.service` 的注释字符是 `#`（systemd 只认它）。用 `;` 会被当成指令行，
> `daemon-reload` 报 `Unknown key name '; ...'` 且单元不生效 ——
> 那是 `.ini` 的风格，别混用。`tools/check_deploy.py` 会守这一条。

## 六、容器部署（Docker）

不想在服务器上装 Python / 建 venv 的话，用容器把环境一起固化：

```bash
cp .env.example .env      # 必须改掉 ARS_BOOTSTRAP_PASSWORD
docker compose up -d
docker compose logs -f ars
```

`docker-compose.yml` 起两个服务：

| 服务 | 作用 | 说明 |
|---|---|---|
| `ars` | 主服务 | 端口**只绑宿主 127.0.0.1**，对外交给 Nginx 反代 |
| `backup` | 定时备份 | 与主服务共享 `./data` 卷，启动先备份一次，之后每 24h 一次 |

几个刻意的设计：

- **端口不直接暴露到公网**：`127.0.0.1:8000:8000` 形式，容器内监听 `0.0.0.0`
  是为了让映射进得来，宿主侧收窄才是边界。直接 `-p 8000:8000` 等于绕过反代的
  HTTPS 与访问控制。
- **`data/` 被 `.dockerignore` 排除**：那里有库文件、密码哈希、审计日志。
  打进镜像既会把生产数据固化进可分发产物，又会在别的环境用旧数据启动。
  运行期靠卷挂载提供。
- **`ARS_BOOTSTRAP_PASSWORD` 用 `:?` 语法**：没在 `.env` 里设置就拒绝启动，
  而不是悄悄用默认密码起来。
- **非 root 运行**，健康检查同时探 8000 与 8100。

> ⚠️ 容器相关的三个文件（`Dockerfile` / `docker-compose.yml` / `.dockerignore`）
> 在开发机上**没有 docker 可用，未实测 build**。结构与关键项由
> `tools/check_deploy.py` 的 [6] 段核对（含「data/ 必须被排除」这类安全项）。
> 首次在目标机 `docker compose up -d` 之后，请进容器跑一遍
> `python tools/check_deploy.py && python tests/smoke_test.py` 确认。

## 七、定时备份

备份工具：`python tools/backup.py`（五库 `VACUUM INTO` 一致性快照 + 照片 +
轮转）。**服务在跑也能做** —— `VACUUM INTO` 会在一个读事务里重建完整库文件，
包含 WAL 里尚未 checkpoint 的已提交事务；直接 `cp`/`tar` 则可能拿到缺最近写入、
甚至拷进半个事务的副本。

三种定时方式，按部署形态选一种即可：

| 部署方式 | 怎么定时 | 配置位置 |
|---|---|---|
| systemd 托管 | `ars-backup.timer`（每天 02:30，`Persistent=true` 关机补跑）| `deploy/ars-backup.timer` |
| Docker compose | compose 里的 `backup` 服务（启动 + 每 24h）| `docker-compose.yml` |
| 宿主 cron | `30 2 * * * cd /opt/ars && .venv/bin/python tools/backup.py` | 你的 crontab |

```bash
sudo cp deploy/ars-backup.service deploy/ars-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ars-backup.timer
systemctl list-timers ars-backup.timer      # 下次触发时间
journalctl -u ars-backup -n 50              # 最近几次结果
python tools/backup.py --list               # 现有备份（含「完整/不完整」）
python tools/backup.py --dry-run            # 只显示会做什么
python tools/backup.py --out /mnt/nas/ars   # 备份到异地
```

产物结构（`data/backups/<时间戳>/`）：

```
returns.db inspect.db handle.db items.db auth.db   ← 一致性快照
photos/                                             ← 原图 + 缩略图
MANIFEST.json                                       ← 大小 + sha256 + 行数
DONE                                                ← **只在全部成功后写**
```

> 为什么要有 `DONE`：定时任务失败是静默的。只靠「目录存在」判断成功的话，
> 一次中途失败（磁盘满、库被锁）会留下一个看起来正常的残缺备份，
> **恢复时才发现少了一个库**。见到 `DONE` 才算可用。
>
> 轮转只删本工具产出的时间戳目录，人工建的 `backup_*` 快照不会被碰。

**备份是否超期会显示在界面上**：「权限设置 → 系统开关 → 数据备份」卡片给出
最近一次时间、距今小时数、大小与状态标签（正常 / 已超期 / 失败 / 从未备份）。
超过 `ARS_BACKUP_STALE_HOURS`（默认 30 小时）即标「已超期」——
把「备份悄悄停了」这件事变成看得见的。

### 恢复步骤

```bash
systemctl stop ars                                   # 或 docker compose down
cd /opt/ars/data
cp -a returns.db inspect.db handle.db items.db auth.db \
      data/_before-restore-$(date +%F)/               # 先把现状留一份
cp -a backups/<要恢复的时间戳>/*.db .
cp -a backups/<要恢复的时间戳>/photos ./             # 照片别忘
systemctl start ars
```

> 恢复前先看该目录里有没有 `DONE`；没有就别用（不完整）。
> `auth.db` 是唯一不可再生的库，恢复错了会影响登录，务必先留底。

## 八、上线检查清单

- [ ] 已跑 `./setup_env.sh`（或 `setup_env.bat`），依赖导入自检通过
- [ ] `python tools/check_deploy.py` 全绿
- [ ] `ARS_AUTH_ENABLED` 保持开启，且**已用新密码替换初始管理员密码**
      （未改密前服务端只放行「查身份 / 改密 / 退出」三个接口，改不动数据）
- [ ] 启用 HTTPS，并同时设 `ARS_COOKIE_SECURE=1`（+ 可选 `ARS_HTTPS_ONLY=1`）
- [ ] `ARS_TRUSTED_PROXIES` 只填真实代理地址（填错会让 IP 白名单失效）
- [ ] 数据接口的访问令牌已重置为随机值（不要沿用首次生成的默认值太久）
- [ ] 若金山侧出口 IP 固定，`ARS_OPEN_API_IPS` 填上网段
- [ ] 数据范围只勾选必需的数据集（默认只给 `detail`）
- [ ] **已启用定时备份**，且界面「数据备份」卡片显示为「正常」
- [ ] 防火墙只放行必要端口（通常只需 443，由反代转发）
- [ ] 用「权限设置 → 审计日志」确认登录记录正常落库

## 九、备份与迁移

整目录迁移时，**停服务再拷**最稳妥：

```bash
# 停服务后整目录拷贝（WAL 模式下直接拷 .db 可能拷到未合并的事务）
systemctl stop ars
tar czf ars-backup-$(date +%F).tgz /opt/ars/data
systemctl start ars
```

不停服务也可以用 `tools/backup.py`（一致性快照 + DONE 标记），它比手工 `tar` 可靠。

`data/` 下共五个库，迁移时一并带走：

```
returns.db   退回登记库   100 条明细 + 物料型号字典
inspect.db   检测登记库   检测结论
handle.db    处理登记库   ERP 处理
items.db     匹配数据库   物料主档
auth.db      接入库       用户 / 权限组 / 会话 / 接口日志
```

> `auth.db` 是唯一**不可再生**的库 —— 业务数据都能从金山或单据重建，
> 但用户、密码、审计日志丢了就找不回来。备份时优先保它。
>
> `data/photos/` 是磁盘文件（照片原图 + 缩略图），**不在 `.db` 里**，
> 必须与五个库一起备份，否则记录还在、图全丢。
