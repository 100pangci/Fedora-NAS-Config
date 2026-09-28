#!/bin/bash
# webtop 启动时确保百度网盘 / 115 浏览器已安装
# 离线优先:/config/debs 中的离线包(含依赖);缺依赖时 apt 兜底,并把依赖包回填 /config/debs
# 每次容器重建(镜像更新)后自动重装 —— 这是"原版镜像 + 非 commit"方案保留 App 的关键
set -u
export DEBIAN_FRONTEND=noninteractive

log() { echo "**** [install-apps] $*"; }
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

if installed baidunetdisk && installed 115browser; then
    log "baidunetdisk & 115browser 已安装,跳过"
    exit 0
fi

log "开始安装百度网盘 / 115 浏览器(来源:/config/debs)"
shopt -s nullglob
debs=(/config/debs/*.deb)
if [ "${#debs[@]}" -gt 0 ]; then
    log "dpkg -i ${#debs[@]} 个离线包"
    dpkg -i "${debs[@]}" || true
else
    log "警告:/config/debs 下没有安装包"
fi

if ! installed baidunetdisk || ! installed 115browser; then
    log "依赖不完整,apt 兜底补齐(需要网络)"
    apt-get update -qq || true
    # 先记录将要补装的依赖,再实际安装
    missing_pkgs=$(apt-get -s -f install 2>/dev/null | sed -n 's/^Inst \([^ ]*\) .*/\1/p' | tr '\n' ' ')
    apt-get -f install -y --no-install-recommends || true

    # docker-clean 会清空 apt 缓存,所以用 apt-get download 显式抓取依赖包回填
    if [ -n "${missing_pkgs:-}" ]; then
        log "回填依赖离线包: $missing_pkgs"
        ( cd /tmp && apt-get download $missing_pkgs >/dev/null 2>&1 || true )
        for f in /tmp/*.deb; do
            [ -f "$f" ] || continue
            if [ ! -f "/config/debs/$(basename "$f")" ]; then
                cp "$f" /config/debs/ && chown 1000:1000 "/config/debs/$(basename "$f")" || true
            fi
        done
    fi
fi

if installed baidunetdisk && installed 115browser; then
    log "安装完成"
else
    log "警告:仍有未安装项,请检查上面的输出"
fi
exit 0
