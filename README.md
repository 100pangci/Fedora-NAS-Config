# Fedora-NAS Config

Fedora Server 45 NAS（`fedora-nas`，内网 IP 已模糊为 `192.168.x.x`）的系统与服务配置快照，脱敏后供灾难恢复与日常维护参考。实机核对日期：**2026-11-08**。仓库保存部署与系统配置，不包含应用数据、账号态、模型和加密卷内容。

配套维护手册（含恢复流程、故障排查、硬件记录）保存在个人笔记库：《Fedora Server 系统安装与维护手册》。

## 目录结构

```
config/
├── dnf/                          # DNF 配置和 Fedora 45 软件源模板
├── fstab/                        # XFS 文件系统挂载表
├── samba/smb.conf                # Samba 共享；加密卷挂载点以 Vault-1/Vault-2 指代
├── containers/containers.conf    # Podman 用户配置
├── firewalld/
│   ├── zones/                    # FedoraServer.xml 等 zone 快照
│   └── services/                 # 自定义端口服务定义，含 Magpie/MaiBot/SnowLuma
├── crontab/                      # root/user 定时任务快照
├── fedora-nas/                   # 月度维护配置模板（真实 SMTP 配置不入库）
└── systemd/                      # 月度维护、代理和容器启动 unit/timer
Podman/
├── Bili-Sync/ FluxDown/ Frpc/ Jellyfin/ Openlist/
├── LocalSend/ OpenWebUI-Pure/ PeerBanHelper/ qBittorrent-Enhanced-Edition/
├── Scrutiny/ Syncthing/ Vnstat-ui/ Ubuntu-Xfce/
├── Magpie/                       # Claude Code/OpenCode 网关 + Web UI；密钥脱敏
├── MaiBot/                       # MaiBot 部署参数；config/data 不入库
├── Ollama/                       # 本地 embedding 服务；模型数据不入库
├── SnowLuma/                     # QQ 桌面 + OneBot；QQ 登录态不入库
├── Netfrp/                       # SakuraFrp launcher；token/远程地址脱敏
├── Minecraft/                    # MC 服务端 + Mefrp-MC 三方 frp（按需启动）
└── Terraria/                     # Terraria 服务端 + Mefrp-TR 三方 frp（按需启动）
Tools/*.sh                        # 运维脚本快照
Scripts/*.py                      # 月度维护和每日索引生成脚本
```

## 实机服务概况

2026-11-08 核对时，rootless Podman 有 17 个运行中容器、6 个 Exited/Created 容器。新加入的服务包括 Magpie（3425/3430）、MaiBot（8001）、SnowLuma（6081/5099）和 Ollama（仅宿主回环 11434）；natfrp-service 当前运行并监听 7102。Minecraft、Terraria、Jellyfin 和 webtop 按需启动，核对时均未运行。Scrutiny API 8088 返回 HTTP 200；当前无 sudo 权限，rootful 容器列表未能复核，仓库保留最近已核实的 Scrutiny 配置。

Magpie 使用 host 网络，并挂载选定的 Claude Code/OpenCode 配置、会话数据和宿主 CLI。SnowLuma 使用 rootless Podman、`SYS_PTRACE` 和 `seccomp=unconfined` 支持 QQ hook；其 OneBot 3000/3001 只绑定宿主回环。MaiBot 通过 127.0.0.1:3001 接入 SnowLuma，Ollama 为其提供本地 embedding。

## firewalld

`config/firewalld/services/` 保留现役和按需服务定义，不要清理。仓库中的 `FedoraServer.xml` 最近一次完整核对日期为 2026-10-29；2026-11-08 非特权会话只能确认 `FedoraServer` 绑定 `bridge0`/`enp2s0`，读取 zone 服务列表时授权失败，因此 XML 仍是上次核对的快照。新增的 Magpie、MaiBot、SnowLuma 服务定义按实机监听端口整理；是否已加入当前 zone，需在有权限时另行核对。

ArchiveBox、Antigravity-Manager 等服务虽已删除，相关 firewalld 定义仍按用户要求保留备用。

## 月度自动维护（fedora-nas-monthly-*）

每月**最后一个周六 20:00** 预检查并发邮件；紧随其后的**周日 04:00** 执行 `dnf5 upgrade --refresh -y`，仅在需要时重启；重启后进行健康检查并发送最终邮件（跨月按同一窗口处理）。

- 主程序：`~/Scripts/fedora_nas_maintenance.py`（`precheck/upgrade/health/run/mail-test/status/skip/unskip/selftest/bootinfo/clear-interrupted`）
- 配置：`/etc/fedora-nas/update.conf`（600；真实 SMTP 凭据不入库）
- 名单：`/etc/fedora-nas/services.conf`（critical / optional / expected_stopped 分级）
- 状态：`/var/lib/fedora-nas-update/`；日志：`/var/log/fedora-nas-update/`
- 2026-10-31 预检查、11-01 升级和重启后健康检查已完成；下次窗口为 **2026-11-28 20:00 → 11-29 04:00**。
- 与每日 `dnf5-automatic`（仅安全更新）互补。

## 脱敏与未入库数据

配置快照中的敏感值以 `<REDACTED-...>` 替换；恢复时需在本机安全填回。局域网 IP 已模糊，VeraCrypt 挂载点使用 `Vault-1`/`Vault-2` 名称。

| 文件 | 脱敏或排除内容 |
|------|----------------|
| `Podman/Frpc/config/frpc.toml` | frp 服务端 token、服务器地址 |
| `Podman/Netfrp/docker-compose.yml` | SakuraFrp token、隧道远程地址 |
| `Podman/Magpie/docker-compose.yml` | `MAGPIE_WEB_KEY`、局域网地址 |
| `Podman/OpenWebUI-Pure/docker-compose.yml` | `WEBUI_SECRET_KEY` |
| `Podman/Openlist/docker-compose.yml` | MySQL root 密码；VeraCrypt 挂载点名称 |
| `Podman/Ubuntu-Xfce/docker-compose.yml` | webtop 密码 |
| `Podman/Minecraft/Mefrp-MC/docker-compose.yml` | 三方 frp 令牌 |
| `Podman/Terraria/Mefrp-TR/docker-compose.yml` | 三方 frp 令牌 |
| `Podman/Terraria/Trlatest/docker-compose.yml` | 世界文件名 |
| `Podman/Terraria/Trlatest/config/serverconfig.txt` | 服务器密码 |
| `Tools/rar_background_archive.sh`、`_vn.sh`、`rar_background_unpacker.sh` | RAR 密码、SMB 账户密码 |
| `Tools/wakeup_pc.sh` | WoL 目标 MAC |

以下本机数据不进入仓库：

- `/etc/fedora-nas/update.conf`（真实 SMTP 配置）与 `~/.ssh/authorized_keys`
- `~/Podman/Magpie/config/`（服务商密钥、登录态、插件与用量数据）
- `~/Podman/MaiBot/docker-config/` 与 `data/`（Bot、注册表、WebUI 凭据和插件数据）
- `~/Podman/SnowLuma/data/`、`qq-config/`、`qq-data/`（QQ 登录态及应用数据）
- `~/Podman/Ollama/data/`（模型文件）
- `~/Podman/Ubuntu-Xfce/config/Software/v2rayN/`（订阅与代理配置）及 `config/debs/`（百度网盘/115 离线安装包）
- `~/Podman/Terraria/Trlatest/config/` 中的 `*.wld` 与 `banlist.txt`
- VeraCrypt 卷密码和卷内数据；`/etc/ssh/sshd_config`、`/etc/selinux/`（未修改）
- 已弃用的 podman 代理 override 备份；已移除的 `localsend-update.{service,timer}`

## 恢复用法

```bash
git clone https://github.com/100pangci/Fedora-NAS-Config.git
# 对照《Fedora Server 系统安装与维护手册》Phase 2~7 逐步还原
# 按设备、磁盘和网络实际情况调整模板，并填回所有 <REDACTED-...> 占位符
# 月度维护（可选）：
#   sudo bash ~/Tools/install_monthly_maintenance.sh
#   sudo python3 ~/Scripts/fedora_nas_maintenance.py selftest
```

## 关联仓库

- **localsend-cli-container**：<https://github.com/100pangci/localsend-cli-container>（LocalSend CLI 自建镜像与构建发布流水线）
