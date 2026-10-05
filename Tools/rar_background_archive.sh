#!/bin/zsh
# ================= 配置区 =================
# 1. 永远的最终输出目录（机械硬盘存储区）
DEST_DIR="/mnt/New-2/Temp/待上传"
# 2. 缓存目录：在高速 SSD 上进行压缩，压缩完成后再移动到目标目录
CACHE_BASE_DIR="/mnt/SSD-Cache/U-A-Temp"
# 3. 固定密码
PASSWORD="<REDACTED-填实际压缩密码>"
# 4. 分卷大小：2g
VOLUME_SIZE="2g"
# 5. 恢复记录比例：10% (p表示百分比)
RECOVERY_RECORD="10p"
# 6. 压缩日志存放目录（统一放 SSD 缓存区，避免机械盘长期写入）
LOG_DIR="/mnt/SSD-Cache/Temp"
# 7. 用于主动刷新 SMB 缓存的本地账号和密码
SMB_USER="ywpc"
SMB_PASS="<REDACTED-填实际账户密码>"
# ==========================================

if [ -z "$ZSH_VERSION" ]; then
    exec zsh "$0" "$@"
fi
setopt localoptions no_bang_hist

# 确保必要的目录存在
mkdir -p "$DEST_DIR"
mkdir -p "$LOG_DIR"
if ! mkdir -p "$CACHE_BASE_DIR" 2>/dev/null; then
    echo "错误: 无法创建或访问缓存目录 '$CACHE_BASE_DIR'，请检查权限。"
    exit 1
fi

# 1. 获取输入路径
SRC_PATH="$1"
if [[ -z "$SRC_PATH" ]]; then
    echo "=================================================="
    echo " 💡 提示：强烈推荐在路径开头加单引号 ' 再按 TAB 补全，如："
    echo "        ./rar_background_archive.sh '/mnt/Old-1/Video/[TAB...'"
    echo "=================================================="
    autoload -Uz compinit && compinit -u >/dev/null 2>&1
    zstyle ':completion::vared::*' completer _files
    bindkey '^I' expand-or-complete
    
    vared -p "请输入要归档的源路径: " SRC_PATH || exit 1
    echo ""
fi

# 【完美路径解析器】
SRC_PATH="${SRC_PATH#\"}"
SRC_PATH="${SRC_PATH%\"}"
SRC_PATH="${SRC_PATH#\'}"
SRC_PATH="${SRC_PATH%\'}"
if [[ "$SRC_PATH" =~ '^[a-zA-Z]:' || ( "$SRC_PATH" == *'\'* && "$SRC_PATH" != */* ) ]]; then
    SRC_PATH="${SRC_PATH//\\//}"
else
    SRC_PATH="${(Q)SRC_PATH}"
fi

# 转换源路径为绝对路径
if [[ -e "$SRC_PATH" ]]; then
    SRC_PATH=$(realpath "$SRC_PATH")
else
    echo "错误: 路径 '$SRC_PATH' 不存在，请重新运行脚本。"
    exit 1
fi

# 2. 提取目标文件名
BASENAME=$(basename "$SRC_PATH")

# 生成带时间戳的日志文件名
TIMESTAMP=$(date "+%Y%m%d_%H%M%S")
LOG_FILE="$LOG_DIR/${BASENAME}_${TIMESTAMP}.log"

# 3. 解析 Samba 共享名与内部相对路径
local SMB_SHARE=""
local SMB_SUBDIR=""
if [[ "$DEST_DIR" =~ ^/mnt/([^/]+)(.*)$ ]]; then
    SMB_SHARE="${match[1]}"
    SMB_SUBDIR="${match[2]}"
    SMB_SUBDIR="${SMB_SUBDIR#/}"
fi

# 4. 启动原生后台子进程
(
    echo "=================================================="
    echo " 🚀 实时流式归档任务启动时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo " 源路径: $SRC_PATH"
    echo " 缓存区: $CACHE_BASE_DIR"
    echo " 目的地: $DEST_DIR"
    echo "=================================================="
    echo ""

    # 创建本次任务在高速 SSD 上的专属临时目录
    local temp_archive_dir="$CACHE_BASE_DIR/${BASENAME}_tmp_$$"
    mkdir -p "$temp_archive_dir"

    # 临时输出文件基础路径
    local temp_out_file="$temp_archive_dir/$BASENAME"

    # 1. 异步启动后台压缩进程
    echo "⚡ 正在 SSD 高速缓存区启动压缩 (带有 10% 恢复记录，去除绝对路径)..."
    # 这里加了 -ep1 参数，防止将上级绝对目录打包进去
    rar a -ep1 -hp"$PASSWORD" -v"$VOLUME_SIZE" -rr"$RECOVERY_RECORD" -r -ma5 -m3 "$temp_out_file" "$SRC_PATH" &
    local RAR_PID=$!

    # 记录已经成功同步的文件以及在目标盘生成的路径（用于异常时的事务回滚）
    typeset -A synced_files
    local -a transferred_dest_paths

    # 2. 流式同步守护线程：在 RAR 压缩时，实时检测并迁移已完成的分卷
    while kill -0 $RAR_PID 2>/dev/null; do
        # 扫描 SSD 缓存目录下的分卷
        local files=( "$temp_archive_dir"/*.(rar|RAR)(N) )
        for f in $files; do
            # 如果该分卷已经同步过，跳过
            if [[ -n "$synced_files[$f]" ]]; then
                continue
            fi

            # 判断 RAR 是否仍在占用写入此文件
            local is_open=0
            if [[ -d "/proc/$RAR_PID/fd" ]]; then
                for fd in /proc/$RAR_PID/fd/*(N); do
                    if [[ "$(readlink "$fd")" == "$f" ]]; then
                        is_open=1
                        break
                    fi
                done
            else
                # 备用兼容方案（非 Linux 系统）
                if command -v fuser >/dev/null 2>&1; then
                    fuser -s "$f" 2>/dev/null && is_open=1
                fi
            fi

            # 如果文件仍被占用，说明此分卷还在写入中，跳过
            if (( is_open )); then
                continue
            fi

            # 双重保险：校验文件大小是否已经稳定不再增长
            local sz1=$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f" 2>/dev/null)
            sleep 1
            local sz2=$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f" 2>/dev/null)
            if (( sz1 != sz2 || sz1 == 0 )); then
                continue
            fi

            # 确定此分卷已写完并关闭，开始用 rsync 流式搬运
            local fname=$(basename "$f")
            echo "🚚 [分卷就绪] 检测到 $fname 已生成完毕。开始搬运至目标盘..."
            
            # 使用 rsync 搬运，同步完成后自动删除 SSD 上的源文件
            if rsync --remove-source-files -t -v --inplace "$f" "$DEST_DIR/"; then
                echo "✅ [迁移成功] $fname 已安全写入 HDD，SSD 缓存已释放。"
                synced_files[$f]=1
                transferred_dest_paths+=("$DEST_DIR/$fname")
                
                # 触发实时 SMB 缓存刷新，让客户端（如 Kodi/Infuse）能逐个看到新分卷
                touch "$DEST_DIR/.smb_refresh" && rm "$DEST_DIR/.smb_refresh"
            else
                echo "⚠️ [迁移失败] $fname 同步出错，将在下个周期重试..."
            fi
        done
        sleep 5
    done

    # 3. 压缩进程结束后的收尾工作
    wait $RAR_PID
    local rar_exit_status=$?

    if (( rar_exit_status == 0 )); then
        echo ""
        echo "⚡ RAR 压缩主进程顺利结束。开始处理最终残留的分卷（通常是最后一个分卷）..."
        
        # 迁移最后一卷
        local files=( "$temp_archive_dir"/*.(rar|RAR)(N) )
        for f in $files; do
            if [[ -n "$synced_files[$f]" ]]; then
                continue
            fi
            local fname=$(basename "$f")
            echo "🚚 [最终分卷] 正在搬运最后一卷: $fname..."
            if rsync --remove-source-files -t -v --inplace "$f" "$DEST_DIR/"; then
                echo "✅ [最终归档] $fname 搬运成功。"
                synced_files[$f]=1
                transferred_dest_paths+=("$DEST_DIR/$fname")
            else
                echo "❌ [严重错误] 最终分卷 $fname 迁移失败！"
                rar_exit_status=99
            fi
        done
    fi

    # 4. 判定整套归档任务的最终命运
    if (( rar_exit_status == 0 )); then
        echo "🎉 恭喜！所有分卷已完美、安全地搬运至最终目的地！"
        # 彻底移除 SSD 上的临时目录（此时里边应该只有空目录）
        rm -rf "$temp_archive_dir"

        # 【最终双通道实时音速刷新】
        touch "$DEST_DIR/.smb_refresh" && rm "$DEST_DIR/.smb_refresh"
        
        if [[ -n "$SMB_SHARE" ]] && command -v smbclient >/dev/null 2>&1; then
            local -a smb_cmds
            if [[ -n "$SMB_SUBDIR" ]]; then
                smb_cmds+=("cd \"$SMB_SUBDIR\"")
            fi
            smb_cmds+=(
                "mkdir .smb_refresh_dir"
                "rmdir .smb_refresh_dir"
            )
            print -l "${smb_cmds[@]}" | smbclient "//127.0.0.1/$SMB_SHARE" -U "$SMB_USER%$SMB_PASS" >/dev/null 2>&1
        fi
    else
        echo ""
        echo "❌ [任务失败] 压缩或同步过程遭遇致命错误（代码: $rar_exit_status）！"
        echo "🧹 [启动回滚机制] 正在从目标机械硬盘删除已迁移的部分分卷，防止损坏数据污染目录..."
        for tf in $transferred_dest_paths; do
            if [[ -f "$tf" ]]; then
                echo "  -> 正在删除目标盘上的破损分卷: $(basename "$tf")"
                rm -f "$tf"
            fi
        done
        echo "💡 建议排查原因。SSD 上的临时工作目录已保留在: $temp_archive_dir"
    fi

    echo ""
    echo "=================================================="
    echo " 🎉 归档任务处理完毕！"
    echo " 结束时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=================================================="
) > "$LOG_FILE" 2>&1 &

# 获取后台进程 PID
PID=$!
disown

# 5. 提示用户
echo "=================================================="
echo " 🚀 实时流水线归档任务已成功提交到后台运行！"
echo "=================================================="
echo " 源 路径 : $SRC_PATH"
echo " SSD 缓存: $CACHE_BASE_DIR"
echo " 最终输出: $DEST_DIR"
echo " 进程 ID : $PID"
echo " 日志文件: $LOG_FILE"
echo "--------------------------------------------------"
echo " 此时您可以安全地关闭 SSH 窗口，任务不会中断。"
echo ""
echo " 想要【实时查看进度】，请运行以下命令："
echo " tail -f \"$LOG_FILE\""
echo "=================================================="
