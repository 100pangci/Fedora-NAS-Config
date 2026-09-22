#!/bin/bash
# fedora-nas 月度自动维护 —— 幂等安装脚本
#
#   sudo bash ~/Tools/install_monthly_maintenance.sh [--no-smoke-test]
#
# 行为：
#   1. 检查/安装主脚本（~/Tools/fedora_nas_maintenance.py）
#   2. 生成独立配置（/etc/fedora-nas/update.conf，权限 600；仅首次，之后不覆盖）
#      首次可选的从 Scrutiny YAML 一次性导入 SMTP —— 解析失败只提示、绝不猜测、绝不改原文件
#   3. 生成健康检查名单（/etc/fedora-nas/services.conf，仅首次）
#   4. 按配置渲染 systemd unit 到 /etc/systemd/system（时间改动只需重跑本脚本）
#   5. systemd-analyze verify 校验、daemon-reload、启用三个 timer
#   6. 用 systemd-run 做一次冒烟测试（验证 systemd 能执行 /home 下的脚本）
#
# 幂等：重复运行不产生重复 unit/timer，不覆盖已有配置；不改动 dnf5-automatic。
# 不做的事：不清理 dnf 缓存 / journal / 旧内核，不动 /etc/dnf/automatic.conf。

set -uo pipefail

MAINT_PY="${MAINT_PY:-/home/ywpc/Tools/fedora_nas_maintenance.py}"
REPO_DIR="${REPO_DIR:-/home/ywpc/Configs/Fedora}"
CONF_DIR="/etc/fedora-nas"
CONF="$CONF_DIR/update.conf"
SERVICES="$CONF_DIR/services.conf"
SKIP_FLAG="$CONF_DIR/skip-next-update"
EXAMPLE="$REPO_DIR/config/fedora-nas/update.conf.example"
SERVICES_EXAMPLE="$REPO_DIR/config/fedora-nas/services.conf.example"
SCRUTINY="${SCRUTINY:-/home/ywpc/Podman/Scrutiny/config/scrutiny.yaml}"
UNIT_DIR="/etc/systemd/system"
STATE_DIR="/var/lib/fedora-nas-update"
LOG_DIR="/var/log/fedora-nas-update"
TIMERS=(fedora-nas-monthly-precheck.timer fedora-nas-monthly-upgrade.timer fedora-nas-monthly-health.timer)

SMOKE_TEST=1
for arg in "$@"; do
    case "$arg" in
        --no-smoke-test) SMOKE_TEST=0 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "[错误] 未知参数: $arg" >&2; exit 2 ;;
    esac
done

log()  { echo "[安装] $*"; }
warn() { echo "[警告] $*" >&2; }
die()  { echo "[错误] $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行: sudo bash $0"

# ---------- 1. 主脚本 ----------
if [[ ! -f "$MAINT_PY" && -f "$REPO_DIR/Tools/fedora_nas_maintenance.py" ]]; then
    log "主脚本不存在，从仓库复制: $REPO_DIR/Tools/fedora_nas_maintenance.py"
    install -m 755 "$REPO_DIR/Tools/fedora_nas_maintenance.py" "$MAINT_PY"
fi
[[ -f "$MAINT_PY" ]] || die "主脚本缺失: $MAINT_PY（请从仓库恢复 Tools/fedora_nas_maintenance.py）"
python3 -m py_compile "$MAINT_PY" || die "主脚本语法检查失败: $MAINT_PY"
log "主脚本语法检查通过: $MAINT_PY"

# ---------- 2. 目录 ----------
mkdir -p "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"
chmod 700 "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"
chown root:root "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"

# ---------- 3. 配置（不覆盖已有）----------
if [[ ! -f "$CONF" ]]; then
    log "生成独立配置 $CONF（首次；SMTP 可选从 Scrutiny YAML 一次性导入）"
    python3 "$MAINT_PY" init-config --template "$EXAMPLE" --dest "$CONF" --scrutiny "$SCRUTINY" \
        || die "生成配置失败"
else
    log "$CONF 已存在，保留现有配置（不覆盖）"
fi
chmod 600 "$CONF"; chown root:root "$CONF"
if grep -q '<your-' "$CONF" 2>/dev/null; then
    warn "$CONF 中的 SMTP 仍是占位符：安装继续，但邮件不会发送。"
    warn "  请编辑: sudoedit $CONF   然后重跑本脚本以重渲染 timer。"
fi

if [[ ! -f "$SERVICES" ]]; then
    [[ -f "$SERVICES_EXAMPLE" ]] || die "缺少名单模板: $SERVICES_EXAMPLE"
    install -m 644 "$SERVICES_EXAMPLE" "$SERVICES"
    log "生成健康检查名单 $SERVICES（critical / optional / expected_stopped 分级）"
else
    log "$SERVICES 已存在，保留（可直接编辑调整分级）"
fi
chmod 644 "$SERVICES"

# ---------- 4. 渲染并安装 unit ----------
log "按 $CONF 渲染 systemd unit 到 $UNIT_DIR"
python3 "$MAINT_PY" render-units --config "$CONF" --dest "$UNIT_DIR" --force \
    || die "渲染 unit 失败"
chmod 644 "$UNIT_DIR"/fedora-nas-monthly-*.service "$UNIT_DIR"/fedora-nas-monthly-*.timer

verify_failed=0
for u in "$UNIT_DIR"/fedora-nas-monthly-*.service "$UNIT_DIR"/fedora-nas-monthly-*.timer; do
    if ! out=$(systemd-analyze verify "$u" 2>&1); then
        warn "systemd-analyze verify 失败: $u"
        echo "$out" | sed 's/^/    /' >&2
        verify_failed=1
    elif [[ -n "$out" ]]; then
        echo "    [verify] $(basename "$u"): $out" | sed 's/^/    /'
    fi
done
[[ $verify_failed -eq 0 ]] || die "unit 校验未通过，已中止（未启用 timer）"

systemctl daemon-reload || die "systemctl daemon-reload 失败"

# ---------- 5. 启用 timer ----------
for t in "${TIMERS[@]}"; do
    systemctl enable --now "$t" >/dev/null 2>&1 || die "启用 $t 失败"
    log "已启用 $t"
done

# ---------- 6. 冒烟测试（验证 SELinux/权限下 systemd 能执行 /home 脚本）----------
smoke_ok=1
if [[ $SMOKE_TEST -eq 1 ]]; then
    log "冒烟测试: 通过 systemd 运行一次 selftest（不升级、不发邮件、无副作用）"
    if out=$(systemd-run --wait --collect --pipe --property=Type=oneshot \
             /usr/bin/python3 "$MAINT_PY" selftest 2>&1); then
        echo "$out" | grep -E 'selftest:' | sed 's/^/    /' || true
    else
        smoke_ok=0
        warn "冒烟测试失败；systemd 可能无法执行 /home 下的脚本（SELinux）。"
        warn "  排查: sudo ausearch -m avc -ts recent | tail"
        warn "  兜底: 把主脚本复制到 /usr/local/libexec/fedora-nas/，并把 unit 的 ExecStart 改为该路径后 daemon-reload"
        echo "$out" | tail -5 | sed 's/^/    /' >&2
    fi
fi

# ---------- 7. 汇总 ----------
echo
echo "================ 安装完成 ================"
echo "配置文件   : $CONF（600）"
echo "检查名单   : $SERVICES（644）"
echo "跳过标记   : $SKIP_FLAG（需要跳过时: sudo python3 $MAINT_PY skip）"
echo "状态目录   : $STATE_DIR"
echo "日志目录   : $LOG_DIR（journal 中也有）"
echo
echo "--- 已启用的维护 timer ---"
systemctl list-timers 'fedora-nas-monthly*' --no-pager || true
echo
echo "--- 下一个维护窗口（按当前配置计算）---"
python3 "$MAINT_PY" status 2>/dev/null | sed -n '/接下来 3 个维护窗口/,$p' | head -5 || true
echo
echo "--- 每日安全更新（应保持不动）---"
systemctl is-enabled dnf5-automatic.timer 2>/dev/null || true
systemctl is-active dnf5-automatic.timer 2>/dev/null || true
echo
echo "建议的验证命令（都需要 root）:"
echo "  sudo python3 $MAINT_PY mail-test"
echo "  sudo python3 $MAINT_PY precheck --dry-run"
echo "  sudo python3 $MAINT_PY bootinfo"
echo "  sudo journalctl -u fedora-nas-monthly-precheck -b"
echo

[[ $smoke_ok -eq 1 ]] || { warn "安装已完成，但冒烟测试未通过，请按上面提示处理。"; exit 1; }
exit 0
