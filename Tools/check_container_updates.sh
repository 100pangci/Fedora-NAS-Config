#!/bin/bash
# 一键检查并自动重建所有 rootless 容器镜像更新(带代理)
# 含停止容器: 同样检查/拉取镜像; 停止态的过时容器用 --no-start 原地重建(不启动, 下次启动即用新镜像)
# 排除: mc-fabric-server(EXCLUDE_NAMES); 版本锁定镜像见 FIXED_IMAGES
# 流程: 代理预检 -> 遍历容器(含停止) -> pull 新镜像 -> 对比 Image ID -> 运行态重建 / 停止态 --no-start 重建
# 用法: check_container_updates.sh [--dry-run]   --dry-run 仅展示将要拉取/重建的动作, 不实际执行

set -o pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "[错误] 未知参数: $arg" >&2; exit 2 ;;
    esac
done

# 1. 代理配置
PROXY_URL="http://127.0.0.1:1145"
export HTTP_PROXY="$PROXY_URL"
export HTTPS_PROXY="$PROXY_URL"
export http_proxy="$PROXY_URL"
export https_proxy="$PROXY_URL"
export NO_PROXY="localhost,127.0.0.1"
export no_proxy="localhost,127.0.0.1"

# 排除配置
EXCLUDE_NAMES=("mc-fabric-server")
# 固定版本镜像: 精确匹配或前缀匹配(如 "docker.io/library/mysql:8.0" 匹配 mysql:8.0.x)
# 说明: mysql 8.0 为固定主版本(openlist 依赖),meilisearch 1.47.0 为数据兼容锁定
FIXED_IMAGES=(
    "docker.io/getmeili/meilisearch:v1.47.0"
    "docker.io/library/mysql:8.0"
)

# 辅助函数: 判断元素是否在数组中
in_array() {
    local target="$1"; shift
    for item in "$@"; do
        [[ "$item" == "$target" ]] && return 0
    done
    return 1
}

# 判断镜像是否属于固定版本(精确匹配,或镜像以固定前缀开头)
is_fixed_image() {
    local img="$1"
    in_array "$img" "${FIXED_IMAGES[@]}" && return 0
    for prefix in "${FIXED_IMAGES[@]}"; do
        [[ "$img" == "$prefix"* ]] && return 0
    done
    return 1
}

# ================= 0. 代理连通性预检 =================
echo "==== 0. 检查代理 ===="
# 快速探测代理端口是否在监听（只测本地 TCP 连接，不卡外网）
if ! timeout 2 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/1145" 2>/dev/null; then
    echo "[错误] 代理端口 127.0.0.1:1145 无法连通，请先启动代理客户端！"
    exit 1
fi
echo "[OK] 本地代理端口正常"
echo ""

# ================= 1. 获取容器列表(含停止) =================
mapfile -t lines < <(podman ps -a --format '{{.Names}}\t{{.Image}}\t{{.State}}')

if [ ${#lines[@]} -eq 0 ]; then
    echo "未发现任何 Podman 容器。"
    exit 0
fi

echo "==== 1. 检查并拉取最新镜像(含停止容器) ===="

declare -A seen_images managed
declare -A c_ref c_id c_state c_project c_dir

for line in "${lines[@]}"; do
    [ -z "$line" ] && continue
    name="${line%%$'\t'*}"; rest="${line#*$'\t'}"
    img="${rest%%$'\t'*}"; state="${rest#*$'\t'}"

    # 记录容器元数据(全部容器,供第 2/3 步判断停止态同项目容器)
    meta=$(podman inspect "$name" --format '{{.Image}}|{{.State.Status}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null)
    IFS='|' read -r cimg cstat cproj cwd <<< "$meta"
    c_ref[$name]="$img"; c_id[$name]="$cimg"; c_state[$name]="${cstat:-$state}"
    c_project[$name]="$cproj"; c_dir[$name]="$cwd"

    # 以下仅对"参与更新"的容器执行
    if in_array "$name" "${EXCLUDE_NAMES[@]}"; then
        echo "[跳过] 容器: $name (已排除)"
        continue
    fi
    if is_fixed_image "$img"; then
        echo "[固定] 镜像: $img (版本锁定，跳过)"
        continue
    fi
    if [[ "$img" == localhost/* ]]; then
        echo "[跳过] 镜像: $img (本地构建)"
        continue
    fi
    managed[$name]=1

    # 镜像去重处理
    if [ -z "${seen_images[$img]}" ]; then
        seen_images[$img]=1

        if [ "$DRY_RUN" = 1 ]; then
            echo "[预演] podman pull $img"
            continue
        fi

        echo -n "[检查] 正在拉取 $img ... "

        old_id=$(podman image inspect "$img" --format '{{.Id}}' 2>/dev/null)

        # 设置 120 秒拉取超时，并记录错误输出
        pull_output=$(timeout 120 podman pull "$img" 2>&1)
        pull_exit_code=$?

        if [ $pull_exit_code -eq 0 ]; then
            new_id=$(podman image inspect "$img" --format '{{.Id}}' 2>/dev/null)
            if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
                echo -e "\r\033[K[更新] 镜像: $img (已拉取新版本)"
            else
                echo -e "\r\033[K[最新] 镜像: $img"
            fi
        else
            echo -e "\r\033[K[失败] 镜像: $img (拉取失败/超时)"
            err_msg=$(echo "$pull_output" | tail -n 1)
            [ -n "$err_msg" ] && echo "       └─ $err_msg"
        fi
    fi
done

echo ""
echo "==== 2. 检查容器是否运行旧镜像(含停止容器) ===="

# 每个 compose 项目是否有运行中的容器(停止态重建前的安全判断:避免停掉同项目运行中的兄弟容器)
declare -A proj_has_running
for name in "${!c_state[@]}"; do
    [ "${c_state[$name]}" = "running" ] || continue
    proj="${c_project[$name]}"
    [ -n "$proj" ] && [ "$proj" != "<no value>" ] && proj_has_running[$proj]=1
done

declare -A container_old
for name in "${!managed[@]}"; do
    img="${c_ref[$name]}"
    container_img_id="${c_id[$name]}"
    latest_img_id=$(podman image inspect "$img" --format '{{.Id}}' 2>/dev/null)

    if [ -n "$container_img_id" ] && [ -n "$latest_img_id" ] && [ "$container_img_id" != "$latest_img_id" ]; then
        echo "[落后] 容器: $name ($img, 状态: ${c_state[$name]})"
        container_old[$name]=1
    fi
done

if [ ${#container_old[@]} -eq 0 ]; then
    echo "所有容器均运行最新镜像，无需重建。"
    exit 0
fi

echo ""
echo "==== 3. 重建过时容器(停止态保持停止) ===="

declare -A dirs_run dirs_stop
for name in "${!container_old[@]}"; do
    st="${c_state[$name]}"; proj="${c_project[$name]}"; dir="${c_dir[$name]}"

    if [ -z "$dir" ] || [ "$dir" = "<no value>" ]; then
        echo "[警告] $name 缺少 compose working_dir 标签，跳过自动重建"
        continue
    fi

    if [ "$st" = "running" ]; then
        dirs_run[$dir]=1
    elif [ -n "$proj" ] && [ "$proj" != "<no value>" ] && [ "${proj_has_running[$proj]:-0}" = 1 ]; then
        echo "[保持] $name 未运行且镜像已更新; 其项目 $proj 中还有运行中的容器，未自动重建"
        echo "       需要时手动: cd $dir && podman compose up -d --force-recreate --no-start"
    else
        dirs_stop[$dir]=1
    fi
done

# 同一目录两种模式冲突时，运行态优先
for dir in "${!dirs_run[@]}"; do
    unset "dirs_stop[$dir]"
done

for dir in "${!dirs_run[@]}"; do
    echo "[重建] 目录: $dir"
    if [ "$DRY_RUN" = 1 ]; then
        echo "       [预演] (cd $dir && podman compose up -d --force-recreate)"
    else
        (cd "$dir" && podman compose up -d --force-recreate 2>&1 | tail -5)
    fi
done

for dir in "${!dirs_stop[@]}"; do
    echo "[重建·保持停止] 目录: $dir"
    if [ "$DRY_RUN" = 1 ]; then
        echo "       [预演] (cd $dir && podman compose up -d --force-recreate --no-start)"
    else
        (cd "$dir" && podman compose up -d --force-recreate --no-start 2>&1 | tail -5)
    fi
done

echo ""
echo "==== 重建后容器状态 ===="
podman ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | sort
echo "注: 停止态重建的容器保持停止，下次 podman start 即使用新镜像。"

echo ""
echo "==== 清理提示 ===="
echo "如业务正常，可执行清理悬空旧镜像: podman image prune -f"
