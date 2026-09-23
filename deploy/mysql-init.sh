#!/bin/bash
# 售后返件登记系统 —— MySQL 首次启动时建库、建账号、授权
#
# 由 docker-compose.yml 里的 mysql 服务挂到 /docker-entrypoint-initdb.d/，
# **官方镜像只在数据卷为空（首次启动）时执行一次**。改了口令要重跑，
# 就得先删数据卷（会丢数据），或进容器手工 ALTER USER。
#
# 它干三件事：
#   1. 建六个 schema。**只建库不建表** —— 表结构由应用启动时的 core/db.py 建，
#      这里再写一份 DDL 必然与应用漂移。
#   2. 建应用账号，并**强制 mysql_native_password**：PyMySQL 在没有 cryptography
#      包的 TLS 之外无法完成 caching_sha2_password 的 RSA 密钥交换，用 MySQL 8
#      的默认插件会直接认证失败（报 "Authentication plugin 'caching_sha2_password'
#      cannot be loaded"）。dev_mysql.py 的 my.ini 里也是这么做的。
#   3. 授权：六个库给全部权限 + `verify\_%` 影子库（备份的还原演练要建临时库）。
#      **不给全局 CREATE/DROP DATABASE** —— 演练脚本万一写错也炸不到真库。

set -euo pipefail

SCHEMAS="returns_db inspect_db handle_db items_db auth_db delivery_db"

: "${ARS_MYSQL_USER:=ars}"
: "${ARS_MYSQL_PASSWORD:?必须在 .env 里设置数据库口令（ARS_MYSQL_PASSWORD）}"
: "${MYSQL_ROOT_PASSWORD:?容器缺少 MYSQL_ROOT_PASSWORD}"

echo "[mysql-init] 建库：${SCHEMAS}"
for s in ${SCHEMAS}; do
  mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" \
    -e "CREATE DATABASE IF NOT EXISTS \`${s}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
done

echo "[mysql-init] 建账号 ${ARS_MYSQL_USER}@% 并授权（六个库 + verify\\_% 影子库）"
mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS '${ARS_MYSQL_USER}'@'%'
  IDENTIFIED WITH mysql_native_password BY '${ARS_MYSQL_PASSWORD}';
ALTER USER '${ARS_MYSQL_USER}'@'%'
  IDENTIFIED WITH mysql_native_password BY '${ARS_MYSQL_PASSWORD}';

GRANT ALL PRIVILEGES ON \`returns_db\`.*  TO '${ARS_MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`inspect_db\`.*  TO '${ARS_MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`handle_db\`.*   TO '${ARS_MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`items_db\`.*    TO '${ARS_MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`auth_db\`.*     TO '${ARS_MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`delivery_db\`.* TO '${ARS_MYSQL_USER}'@'%';
-- 影子库：备份的还原演练用。\_ 让下划线当字面量，模式只匹配 verify_*
GRANT ALL PRIVILEGES ON \`verify\_%\`.*   TO '${ARS_MYSQL_USER}'@'%';

FLUSH PRIVILEGES;
SQL

echo "[mysql-init] 完成"
