#!/usr/bin/env bash
# ==========================================
# 无头服务器 VeraCrypt 交互式挂载管理脚本
# ==========================================

# 自动探测当前用户 UID/GID（兼容直接 sudo 运行整个脚本的情况）
MY_UID="${SUDO_UID:-$(id -u)}"
MY_GID="${SUDO_GID:-$(id -g)}"

# SELinux 标签：与 fstab 中 NTFS 盘一致，让 Podman 容器能访问挂载点
SE_CONTEXT="system_u:object_r:container_file_t:s0"

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 检查 VeraCrypt 是否安装
if ! command -v veracrypt &> /dev/null; then
    echo -e "${RED}错误: 未检测到 VeraCrypt CLI，请先安装！${NC}"
    exit 1
fi

# 校验是否为纯数字
is_number() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

# 根据挂载点路径，反查指向它的 Samba share 名
# 用 testparm 解析配置，匹配 path= 等于目标路径的 share
get_shares_for_path() {
    local target="$1"
    command -v testparm &> /dev/null || return 0
    testparm -s 2>/dev/null | awk -v target="$target" '
        /^\[/      { name=$0; gsub(/[][]/,"",name) }
        /^[[:space:]]*[Pp]ath[[:space:]]*=/ {
            line=$0
            sub(/^[^=]*=[[:space:]]*/,"",line)
            sub(/[[:space:]]+$/,"",line)
            if (line == target) print name
        }'
}

# 卸载前释放 Samba 对该路径的占用（关闭对应 share 上打开的文件，不重启服务）
release_smb_for_path() {
    local target="$1"
    command -v smbcontrol &> /dev/null || return 0

    local shares
    shares=$(get_shares_for_path "$target")
    if [ -z "$shares" ]; then
        return 0
    fi

    echo -e "${YELLOW}检测到以下 Samba 共享指向 $target，卸载前先释放占用：${NC}"
    while IFS= read -r share; do
        [ -z "$share" ] && continue
        echo -e "  关闭共享 ${YELLOW}[$share]${NC} 上打开的文件 ..."
        sudo smbcontrol smbd close-share "$share" 2>/dev/null
    done <<< "$shares"
    # 给 smbd 一点时间释放句柄
    sleep 1
}

# 因容器内 umount 失败而被临时停止的容器,待卷卸载完成后恢复
STOPPED_CONTAINERS=()

# 查询容器中 source 等于 target 的挂载,输出容器内的目标路径
get_container_mount_dest() {
    local container="$1" target="$2"
    podman inspect --format '{{range .Mounts}}{{.Source}} {{.Destination}}{{"\n"}}{{end}}' "$container" 2>/dev/null \
        | awk -v t="$target" '$1 == t { print $2; exit }'
}

# 卸载前释放 bind mount 了该路径的容器对该路径的占用
# 机制(2026-09-10 查内核 fs/pnode.c 后定论):
#   宿主 umount 的传播是沿"被卸载挂载的父挂载"的传播树去找对应子挂载,不看被卸载挂载自己的 slave。
#   - bind 先于卷创建(容器先启动、卷后挂载): 卷的克隆是 bind 的子挂载(topper),传播能配对,自动摘掉
#     → 这就是以前不碰容器也能正常卸载的原因。
#   - 卷挂着时创建 bind(卷挂着时重启容器): bind 直接成为卷的 slave,不在父挂载的传播树里,
#     宿主 umount 看不到它 → 卷在容器里原地不动, dm/loop 一直被引用 → 正常卸载失败、需再停一次容器。
# 因此策略固定为: 停止容器(销毁命名空间,引用必然释放) → 卷卸载 → 卸载完成后重新启动
#   - start 发生在卸载之后,容器重新 bind 空目录,恢复"bind 先于卷"的正确传播关系(下次挂卷容器立即可见)
#   - 绝不能 restart / 不能先 start: 卷还挂着时启动会立刻重新 bind 卷,又把设备抱住
#   - 也不做容器内 umount: 那会拆掉 rslave 的 bind,之后新挂的卷再也传播不进容器
release_container_for_path() {
    local target="$1"
    command -v podman &> /dev/null || return 0

    local found=0
    while IFS= read -r container; do
        [ -z "$container" ] && continue
        local dest
        dest=$(get_container_mount_dest "$container" "$target")
        [ -z "$dest" ] && continue
        if [ "$found" -eq 0 ]; then
            echo -e "${YELLOW}以下容器 bind 了 $target,先停止以免阻塞设备释放:${NC}"
            found=1
        fi
        echo -e "  停止容器 ${YELLOW}$container${NC} (卷卸载完成后自动启动) ..."
        if podman stop "$container" >/dev/null 2>&1; then
            STOPPED_CONTAINERS+=("$container")
        else
            echo -e "    ${RED}容器停止失败,引用可能仍在${NC}"
        fi
    done < <(podman ps --format '{{.Names}}')
    sleep 1
}

# 卷卸载完成后,恢复被兜底停止的容器
# (此时挂载点已还原为空目录,容器重新 bind 的是空目录,不会再持有 dm/loop 引用)
resume_stopped_containers() {
    [ ${#STOPPED_CONTAINERS[@]} -eq 0 ] && return 0
    for container in "${STOPPED_CONTAINERS[@]}"; do
        echo -e "  重新启动容器 ${YELLOW}$container${NC} ..."
        podman start "$container" >/dev/null 2>&1
    done
    STOPPED_CONTAINERS=()
}

# 获取所有 .hc 文件列表
get_hc_files() {
    hc_files=()
    shopt -s nullglob
    if [ -f "/mnt/New-2/Weii.hc" ]; then
        hc_files+=("/mnt/New-2/Weii.hc")
    fi
    for file in /mnt/Old-1/VM_Disk/*.hc; do
        hc_files+=("$file")
    done
    for file in "/mnt/Old-1/VM_Disk/冗余60%+multipar备份包"/*.hc; do
        hc_files+=("$file")
    done
    shopt -u nullglob
}

# 挂载逻辑
mount_volume() {
    get_hc_files

    if [ ${#hc_files[@]} -eq 0 ]; then
        echo -e "${YELLOW}未找到任何加密卷，请检查硬盘是否挂载或路径是否正确。${NC}"
        return
    fi

    echo -e "\n${BLUE}===== 发现以下加密卷，请输入数字选择 =====${NC}"
    for i in "${!hc_files[@]}"; do
        filename=$(basename "${hc_files[$i]}")
        parent_dir=$(basename "$(dirname "${hc_files[$i]}")")
        echo -e " [${GREEN}$((i+1))${NC}] $filename (${YELLOW}位于: $parent_dir${NC})"
    done
    echo -e " [${RED}0${NC}] 返回主菜单"

    read -p "选择要挂载的文件编号: " choice
    if ! is_number "$choice"; then
        echo -e "${RED}无效的选择！${NC}"
        return
    fi
    if [ "$choice" -eq 0 ]; then
        return
    fi

    idx=$((choice-1))
    if [ "$idx" -lt 0 ] || [ "$idx" -ge ${#hc_files[@]} ]; then
        echo -e "${RED}无效的选择！${NC}"
        return
    fi

    selected_file="${hc_files[$idx]}"
    file_basename=$(basename "$selected_file" .hc)

    echo -e "\n你选择的文件是: ${GREEN}$(basename "$selected_file")${NC}"

    # 智能路径分配逻辑
    if [[ "$selected_file" == *"冗余60%+multipar备份包"* ]]; then
        default_mount="/mnt/Temp"
    elif [[ "$file_basename" == "Weii" ]]; then
        default_mount="/mnt/Weii"
    else
        default_mount="/mnt/H"
    fi
    echo -e "默认挂载路径: ${YELLOW}$default_mount${NC}"
    read -p "请输入挂载路径 (直接回车使用默认值): " custom_mount

    mount_point=${custom_mount:-$default_mount}

    # 预先获取 sudo 权限，避免和 VeraCrypt 密码混淆
    echo -e "\n${YELLOW}[系统提示] 挂载操作需要管理员权限。${NC}"
    echo -e "${YELLOW}如收到 Linux [sudo] 密码提示，请输入您的 Linux 账户密码：${NC}"
    if ! sudo -v; then
        echo -e "${RED}管理员权限认证失败，退出挂载。${NC}"
        return
    fi

    # 创建挂载点
    if [ ! -d "$mount_point" ]; then
        echo "创建挂载目录: $mount_point"
        sudo mkdir -p "$mount_point"
    fi

    # 说明：
    #  --pim=0 -k ""  跳过 PIM 和 keyfile 追问
    #  保留隐藏卷追问（不加 --protect-hidden）
    #  context=...    SELinux 标签，让 Podman 容器能读取挂载点
    #  --filesystem=ntfs3  内核只剩 ntfs3 驱动（老 ntfs 类型已移除），
    #  不指定时 VeraCrypt 默认用 "ntfs" 类型挂载导致 unknown filesystem type
    #  注意：本脚本所有 .hc 卷均为 NTFS 格式，若将来有 FAT/ext4 卷需改此值
    echo -e "\n${GREEN}[VeraCrypt 提示] 接下来请按提示输入密码；${NC}"
    echo -e "${GREEN}如需保护隐藏卷，在 'Protect hidden volume?' 时输入 y 并提供隐藏卷密码。${NC}"
    if sudo veracrypt -t \
        --pim=0 -k "" \
        --filesystem=ntfs3 \
        --fs-options="uid=$MY_UID,gid=$MY_GID,umask=000,context=$SE_CONTEXT" \
        "$selected_file" "$mount_point"; then
        echo -e "${GREEN}挂载成功！已挂载到 $mount_point${NC}"
    else
        echo -e "${RED}挂载失败，请检查密码或文件状态。${NC}"
    fi
}

# 卸载逻辑
unmount_volume() {
    mapfile -t mounted_list < <(sudo veracrypt -t -l 2>/dev/null)

    if [ ${#mounted_list[@]} -eq 0 ]; then
        echo -e "${YELLOW}当前没有已挂载的 VeraCrypt 卷。${NC}"
        return
    fi

    echo -e "\n${BLUE}===== 当前已挂载的卷 =====${NC}"
    mount_paths=()
    for i in "${!mounted_list[@]}"; do
        line="${mounted_list[$i]}"
        m_path=$(echo "$line" | awk '{print $NF}')
        container_path=$(echo "$line" | awk '{print $2}')
        mount_paths+=("$m_path")
        echo -e " [${GREEN}$((i+1))${NC}] 挂载点: ${YELLOW}$m_path${NC} (源文件: $(basename "$container_path"))"
    done
    echo -e " [${RED}0${NC}] 返回主菜单"

    read -p "选择要卸载(锁定)的编号: " choice
    if ! is_number "$choice"; then
        echo -e "${RED}无效的选择！${NC}"
        return
    fi
    if [ "$choice" -eq 0 ]; then
        return
    fi

    idx=$((choice-1))
    if [ "$idx" -lt 0 ] || [ "$idx" -ge ${#mount_paths[@]} ]; then
        echo -e "${RED}无效的选择！${NC}"
        return
    fi

    target_unmount="${mount_paths[$idx]}"

    # 卸载前先释放 Samba 对该路径的占用
    release_smb_for_path "$target_unmount"
    # 再释放容器 bind 引用(停止容器,卷卸载完成后再启动)
    release_container_for_path "$target_unmount"

    echo "正在卸载 $target_unmount ..."
    if sudo veracrypt -d "$target_unmount"; then
        echo -e "${GREEN}卸载成功！文件已安全加密锁定。${NC}"
    else
        echo -e "${RED}卸载失败，该目录可能正被其他程序（如 qBittorrent / 容器）读写！${NC}"
    fi

    # 卷已卸载(或卸载失败),恢复被兜底停止的容器
    resume_stopped_containers
}

# 一键卸载所有
unmount_all() {
    echo -e "${YELLOW}正在安全卸载所有加密卷...${NC}"

    # 先对所有已挂载卷的路径释放 Samba 占用,再释放容器 bind 引用
    mapfile -t all_mounted < <(sudo veracrypt -t -l 2>/dev/null)
    for line in "${all_mounted[@]}"; do
        m_path=$(echo "$line" | awk '{print $NF}')
        [ -n "$m_path" ] && release_smb_for_path "$m_path"
    done
    for line in "${all_mounted[@]}"; do
        m_path=$(echo "$line" | awk '{print $NF}')
        [ -n "$m_path" ] && release_container_for_path "$m_path"
    done

    if sudo veracrypt -d; then
        echo -e "${GREEN}操作完成。${NC}"
    else
        echo -e "${RED}部分卷卸载失败，可能正被占用。${NC}"
    fi

    # 卷已卸载(或卸载失败),恢复被兜底停止的容器
    resume_stopped_containers
}

# 主菜单循环
while true; do
    echo -e "\n${BLUE}=======================================${NC}"
    echo -e "       VeraCrypt 交互式挂载助手"
    echo -e "${BLUE}=======================================${NC}"
    echo -e " [${GREEN}1${NC}] 查看并挂载加密卷 (.hc)"
    echo -e " [${GREEN}2${NC}] 卸载(锁定)已挂载的卷"
    echo -e " [${GREEN}3${NC}] 一键卸载所有加密卷"
    echo -e " [${RED}4${NC}] 退出脚本"
    echo -e "${BLUE}---------------------------------------${NC}"
    read -p "请输入选项 [1-4]: " menu_choice
    case "$menu_choice" in
        1) mount_volume ;;
        2) unmount_volume ;;
        3) unmount_all ;;
        4)
            echo "再见！"
            exit 0
            ;;
        *)
            echo -e "${RED}请输入正确的数字！${NC}"
            ;;
    esac
done
