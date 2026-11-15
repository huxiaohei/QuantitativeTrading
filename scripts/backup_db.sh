#!/usr/bin/env bash
# 备份 compose 里的数据库：在 db 容器内执行 pg_dump，压缩后写到 backups/qt-YYYYMMDD.sql.gz，
# 并删除 14 天前的备份。备份含全部使用者的数据，文件权限 0600。
# 用法（在项目根目录）：./scripts/backup_db.sh
# worker 每日运行完成后会自动做同样的备份；这个脚本用于手动备份，例如迁移服务器之前。
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP_DAYS=14
DIR="backups"
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"

STAMP="$(date +%Y%m%d)"
FINAL="$DIR/qt-$STAMP.sql.gz"
TMP="$DIR/.qt-$STAMP.sql.gz.tmp"
trap 'rm -f "$TMP"' EXIT

# 容器内的 pg_dump 通过本地套接字连接，不需要把密码放到命令行
docker compose exec -T db pg_dump -U qt -d qt --no-owner --no-privileges | gzip -c > "$TMP"
mv "$TMP" "$FINAL"
chmod 600 "$FINAL"
echo "已备份到 $FINAL"

# 清理超过保留天数的备份（按文件名里的日期判断，不依赖文件修改时间）
CUTOFF="$(date -v-"${KEEP_DAYS}"d +%Y%m%d 2>/dev/null || date -d "-${KEEP_DAYS} days" +%Y%m%d)"
for f in "$DIR"/qt-????????.sql.gz; do
    [ -e "$f" ] || continue
    name="$(basename "$f")"
    day="${name#qt-}"
    day="${day%.sql.gz}"
    if [ "$day" -lt "$CUTOFF" ]; then
        rm -f -- "$f"
        echo "已删除旧备份 $f"
    fi
done
