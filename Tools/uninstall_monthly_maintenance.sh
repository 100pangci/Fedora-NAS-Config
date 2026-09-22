#!/bin/bash
# fedora-nas 月度自动维护 —— 卸载脚本
#
#   sudo bash ~/Tools/uninstall_monthly_maintenance.sh            # 停止并卸载 unit，保留配置/状态/日志
#   sudo bash ~/Tools/uninstall_monthly_maintenance.sh --purge    # 连同配置、状态、日志一起删除
#
# 不影响 dnf5-automatic（每日安全更新）与任何其它服务。

set -uo pipefail

CONF_DIR="/etc/fedora-nas"
STATE_DIR="/var/lib/fedora-nas-update"
LOG_DIR="/var/log/fedora-nas-update"
UNIT_DIR="/etc/systemd/system"
TIMERS=(fedora-nas-monthly-precheck.timer fedora-nas-monthly-upgrade.timer fedora-nas-monthly-health.timer)
SERVICES_UNITS=(fedora-nas-monthly-precheck.service fedora-nas-monthly-upgrade.service fedora-nas-monthly-health.service)

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "[错误] 未知参数: $arg" >&2; exit 2 ;;
    esac
done

log() { echo "[卸载] $*"; }
die() { echo "[错误] $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行: sudo bash $0"

for t in "${TIMERS[@]}"; do
    systemctl disable --now "$t" >/dev/null 2>&1 && log "已停止并禁用 $t" || log "$t 未启用或已停止"
done
for u in "${SERVICES_UNITS[@]}"; do
    systemctl stop "$u" >/dev/null 2>&1 || true
    systemctl reset-failed "$u" >/dev/null 2>&1 || true
done
rm -f "$UNIT_DIR"/fedora-nas-monthly-*.service "$UNIT_DIR"/fedora-nas-monthly-*.timer
systemctl daemon-reload
log "已删除 /etc/systemd/system/fedora-nas-monthly-* 并 daemon-reload"

if [[ $PURGE -eq 1 ]]; then
    rm -rf "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"
    log "已删除配置 $CONF_DIR、状态 $STATE_DIR、日志 $LOG_DIR"
    log "主脚本未删除（如需删除: rm -f /home/ywpc/Scripts/fedora_nas_maintenance.py）"
else
    log "保留配置 $CONF_DIR、状态 $STATE_DIR、日志 $LOG_DIR（彻底删除请加 --purge）"
fi

echo
echo "--- 残留检查（应无输出）---"
systemctl list-timers 'fedora-nas-monthly*' --no-pager 2>/dev/null | grep -v '^$\|NEXT\|timers listed' || true
echo "--- 每日安全更新应仍然启用 ---"
systemctl is-enabled dnf5-automatic.timer || true
exit 0
