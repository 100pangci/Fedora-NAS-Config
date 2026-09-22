#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fedora-NAS 月度自动维护（dnf5 + systemd timer）

设计目标（与 AGENTS.md 的存储/运维哲学一致）：
  * 每月最后一个周六 20:00 预检查并邮件通知；紧随其后的周日 04:00 执行完整 dnf5 upgrade，
    必要时才重启；重启后自动做健康检查并发送最终邮件。
  * 「最后一个周六 + 其紧邻的周日」是同一个窗口对象（含跨月，例如 10-31 → 11-01），
    不分别独立计算周六/周日，保证预检查与升级始终属于同一连续周末。
  * 预检查成功才授权升级；预检查失败 / 手动跳过 / 存在中断事务时，周日绝不擅自升级。
  * 更新失败绝不自动重启；新内核安装不完整绝不重启；/boot 空间不足绝不硬更新。
  * 状态持久化在 /var/lib（重启不丢），日志双写 journal + /var/log/fedora-nas-update/。
  * 邮件发送失败只记录，不影响系统更新本身的判定。

子命令：
  precheck           预检查（含 --dry-run / --manual / --no-mail / --now）
  upgrade            正式升级（含 --dry-run / --ignore-window / --retry）
  health             重启后健康检查（含 --dry-run / --force）
  run                手动完整维护流程（预检查 + 升级，可能重启；--dry-run 安全演练）
  mail-test          单独测试发邮件
  status             查看状态、最近日志、下一个维护窗口
  skip / unskip      创建 / 删除「跳过下一次维护」flag
  clear-interrupted  清理被中断的升级标记（人工确认后）
  bootinfo           内核与启动项诊断（重启就绪判定的依据，建议 root 运行）
  init-config        生成 /etc/fedora-nas/update.conf（可选从 Scrutiny YAML 一次性导入 SMTP）
  render-units       按 update.conf 渲染 systemd unit 到目标目录（安装脚本调用）
  selftest           日期窗口逻辑自检（无需 root、无副作用）

只使用 Python 标准库（PyYAML 仅用于可选的 Scrutiny 一次性导入，缺失则跳过）。
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与默认值
# ---------------------------------------------------------------------------

PROG = "fedora_nas_maintenance"
HOSTNAME = socket.gethostname()

DEFAULT_CONF = "/etc/fedora-nas/update.conf"
DEFAULT_SERVICES = "/etc/fedora-nas/services.conf"
DEFAULT_SKIP_FLAG = "/etc/fedora-nas/skip-next-update"
DEFAULT_STATE_DIR = "/var/lib/fedora-nas-update"
DEFAULT_LOG_DIR = "/var/log/fedora-nas-update"
DEFAULT_LOCK_FILE = "/run/fedora-nas-update.lock"
DEFAULT_ROOTLESS_USER = "ywpc"

# timer 候选日（只作为「候选」，真正判定在脚本内完成）：
#   最后一个周六一定落在 22..31；它后面的周日可能落在 23..31 或下月 1..3（跨月）
SAT_CANDIDATE_DAYS = list(range(22, 32))
SUN_CANDIDATE_DAYS = [1, 2, 3] + list(range(23, 32))
PRECHECK_WEEKDAY = 5  # 周一=0 ... 周六=5
UPGRADE_WEEKDAY = 6   # 周日

SCHEMA_VERSION = 1
HISTORY_LIMIT = 24

SEV_ORDER = {"ok": 0, "info": 1, "warning": 2, "critical": 3}
SEV_SYMBOL = {"ok": "✓", "info": "·", "warning": "!", "critical": "✗"}

DEFAULT_IMPORTANT_PACKAGES = [
    "kernel", "kernel-core", "systemd", "glibc", "podman", "selinux-policy",
    "NetworkManager", "samba", "openssl", "dracut", "systemd-udev", "grub2-common",
]

# 需要重启的显式核心组件（dnf5 needs-restarting 之外的兜底判断）
REBOOT_PACKAGES = ["systemd", "glibc", "systemd-udev", "selinux-policy"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def now_local() -> datetime:
    """本地时区（Asia/Shanghai）的当前时间（naive，与 systemd OnCalendar 的本地时间一致）。"""
    return datetime.now()


def parse_dt_override(text: str) -> datetime:
    """解析 --now 覆盖值：YYYY-MM-DD、YYYY-MM-DDTHH:MM[:SS]。"""
    text = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"无法解析时间: {text!r}（示例 2026-10-31T20:00）")


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def tail_text(text: str, lines: int = 20) -> str:
    parts = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(parts[-lines:])


def redact(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "****"
    return secret[:2] + "****" + secret[-2:]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


@dataclass
class Config:
    # 调度
    precheck_time: dtime = dtime(20, 0)
    upgrade_time: dtime = dtime(4, 0)
    window_grace_hours: int = 24
    max_precheck_age_hours: int = 36
    # 阈值 / 超时
    min_free_root_gb: int = 10
    min_free_boot_mb: int = 300
    min_free_efi_mb: int = 50
    health_wait_seconds: int = 300
    precheck_wait_seconds: int = 1200
    lock_wait_seconds: int = 1800
    dnf_timeout_seconds: int = 14400
    rpm_verifydb: str = "auto"          # auto / always / never
    log_retention_days: int = 400
    # 行为
    important_packages: list[str] = field(default_factory=lambda: list(DEFAULT_IMPORTANT_PACKAGES))
    run_container_update: bool = False
    container_update_script: str = "/home/ywpc/Tools/check_container_updates.sh"
    dnf_upgrade_cmd: str = "dnf5 upgrade --refresh -y"
    # 邮件
    mail_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"           # ssl / starttls / none
    smtp_user: str = ""
    smtp_pass: str = ""
    mail_from: str = ""
    mail_to: list[str] = field(default_factory=list)
    subject_prefix: str = "[fedora-nas]"

    @property
    def mail_ready(self) -> bool:
        if not (self.mail_enabled and self.smtp_host and self.mail_to and (self.mail_from or self.smtp_user)):
            return False
        # 占位符视为未配置
        joined = " ".join([self.smtp_host, self.smtp_user, self.smtp_pass,
                           self.mail_from] + list(self.mail_to))
        return "<" not in joined and ">" not in joined

    def smtp_summary(self) -> str:
        return (f"{self.smtp_host}:{self.smtp_port}({self.smtp_security}) "
                f"user={self.smtp_user or '-'} from={self.mail_from or self.smtp_user or '-'} "
                f"pass={redact(self.smtp_pass)} to={','.join(self.mail_to) or '-'}")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "yes", "true", "on", "y")


# 配置键别名（大写键 → 字段名）
CFG_KEY_ALIASES = {
    "MAIL_SUBJECT_PREFIX": "subject_prefix",
}


def read_kv_file(path: str | Path) -> dict[str, str]:
    """读取 KEY=VALUE 配置（# 注释，支持引号，值内允许 =）。"""
    data: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {p}")
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in data:
            eprint(f"[warn] {p}:{lineno}: 重复的配置项 {key}，后者覆盖前者")
        data[key] = value
    return data


def load_config(path: str | Path) -> tuple[Config, list[str]]:
    """加载配置；返回 (Config, warnings)。缺项使用默认值。"""
    kv = read_kv_file(path)
    warnings: list[str] = []
    cfg = Config()
    known = set(Config.__dataclass_fields__.keys())

    for key, value in kv.items():
        field_name = CFG_KEY_ALIASES.get(key.upper(), key.lower())
        if field_name not in known:
            warnings.append(f"未知配置项 {key}（已忽略）")
            continue
        try:
            current = getattr(cfg, field_name)
            if isinstance(current, bool):
                setattr(cfg, field_name, _parse_bool(value))
            elif isinstance(current, int):
                setattr(cfg, field_name, int(value))
            elif isinstance(current, dtime):
                hh, mm = value.split(":")
                setattr(cfg, field_name, dtime(int(hh), int(mm)))
            elif isinstance(current, list):
                setattr(cfg, field_name, [x.strip() for x in value.split(",") if x.strip()])
            else:
                setattr(cfg, field_name, value)
        except Exception as exc:  # noqa: BLE001 - 配置错误要给出可读信息
            raise ConfigError(f"配置项 {key}={value!r} 解析失败: {exc}") from exc

    if cfg.smtp_security not in ("ssl", "starttls", "none"):
        raise ConfigError("SMTP_SECURITY 只允许 ssl / starttls / none")
    if cfg.window_grace_hours < 0 or cfg.window_grace_hours > 168:
        raise ConfigError("WINDOW_GRACE_HOURS 需在 0..168 之间")
    if cfg.max_precheck_age_hours < 1:
        raise ConfigError("MAX_PRECHECK_AGE_HOURS 需 >= 1")
    if not cfg.mail_ready:
        if not cfg.mail_enabled:
            warnings.append("MAIL_ENABLED=no：将不发送任何邮件")
        elif "<" in " ".join([cfg.smtp_host, cfg.smtp_user, cfg.mail_from] + list(cfg.mail_to)):
            warnings.append("SMTP 配置仍是占位符（<...>），邮件不会发送；请编辑配置文件")
        else:
            warnings.append("SMTP 配置不完整，邮件不会发送")
    return cfg, warnings


@dataclass
class Paths:
    conf: str = DEFAULT_CONF
    services: str = DEFAULT_SERVICES
    skip_flag: str = DEFAULT_SKIP_FLAG
    state_dir: str = DEFAULT_STATE_DIR
    log_dir: str = DEFAULT_LOG_DIR
    lock_file: str = DEFAULT_LOCK_FILE

    @property
    def state_file(self) -> str:
        return os.path.join(self.state_dir, "state.json")

    @property
    def pending_health(self) -> str:
        return os.path.join(self.state_dir, "pending-health.json")

    @property
    def in_progress(self) -> str:
        return os.path.join(self.state_dir, "upgrade-in-progress.json")


# ---------------------------------------------------------------------------
# 记录器 / 状态 / 锁
# ---------------------------------------------------------------------------

class Logger:
    """同时输出到 stdout（journal）与 /var/log/fedora-nas-update/<mode>-<ts>.log。"""

    def __init__(self, log_dir: str, mode: str, quiet: bool = False):
        self.lines: list[str] = []
        self.mode = mode
        self.quiet = quiet
        self.path: str | None = None
        self._fh = None
        try:
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
            ts = now_local().strftime("%Y%m%d-%H%M%S")
            self.path = os.path.join(log_dir, f"{mode}-{ts}.log")
            self._fh = open(self.path, "w", encoding="utf-8")
            os.chmod(self.path, 0o640)
            latest = os.path.join(log_dir, f"latest-{mode}.log")
            try:
                if os.path.islink(latest) or os.path.exists(latest):
                    os.unlink(latest)
                os.symlink(os.path.basename(self.path), latest)
            except OSError:
                pass
        except OSError as exc:
            eprint(f"[warn] 无法写独立日志文件（{exc}），仅输出到 journal")

    def log(self, msg: str = "", level: str = "INFO") -> None:
        line = f"[{now_local():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
        self.lines.append(line)
        if not self.quiet:
            print(line, flush=True)
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass

    def section(self, title: str) -> None:
        self.log("")
        self.log(f"===== {title} =====")

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def ensure_runtime_dirs(paths: Paths) -> None:
    for d in (paths.state_dir, paths.log_dir):
        os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(paths.state_dir, 0o700)
        os.chmod(paths.log_dir, 0o700)
    except OSError:
        pass
    lock_dir = os.path.dirname(paths.lock_file)
    if lock_dir:
        try:
            os.makedirs(lock_dir, mode=0o755, exist_ok=True)
        except OSError:
            pass


class FileLock:
    """fcntl 排他锁，避免预检查/升级/健康检查并发。"""

    def __init__(self, path: str, logger: Logger | None = None):
        self.path = path
        self.logger = logger
        self._fh = None

    def acquire(self, wait_seconds: int = 0) -> bool:
        try:
            self._fh = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            # 非 root 测试或路径不可写：记录并继续（正式 unit 以 root 运行，不受影响）
            if self.logger:
                self.logger.log(f"无法使用锁文件 {self.path}（{exc}），继续执行但不加锁", "WARN")
            self._fh = None
            return True
        deadline = time.monotonic() + max(0, wait_seconds)
        warned = False
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(f"{os.getpid()} {fmt_dt(now_local())}\n")
                self._fh.flush()
                return True
            except OSError:
                if not warned and self.logger:
                    self.logger.log(f"已有维护进程持有锁 {self.path}，等待中…", "WARN")
                    warned = True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(5)

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class StateStore:
    def __init__(self, paths: Paths, logger: Logger | None = None):
        self.paths = paths
        self.logger = logger

    def load(self) -> dict:
        p = Path(self.paths.state_file)
        if not p.exists():
            return {"schema": SCHEMA_VERSION, "window": None, "history": [],
                    "last_mail": None, "updated_at": None}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if self.logger:
                self.logger.log(f"状态文件损坏（{exc}），将重建: {p}", "ERROR")
            return {"schema": SCHEMA_VERSION, "window": None, "history": [],
                    "last_mail": None, "updated_at": None, "corrupt": True}
        data.setdefault("schema", SCHEMA_VERSION)
        data.setdefault("history", [])
        data.setdefault("window", None)
        return data

    def save(self, data: dict) -> bool:
        data["schema"] = SCHEMA_VERSION
        data["updated_at"] = fmt_dt(now_local())
        try:
            os.makedirs(self.paths.state_dir, mode=0o700, exist_ok=True)
            tmp = f"{self.paths.state_file}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.paths.state_file)
            return True
        except OSError as exc:
            if self.logger:
                self.logger.log(f"写入状态失败（可能是非 root 运行）: {exc}", "ERROR")
            return False

    # ---- 归档 ----
    def archive_window(self, data: dict, logger: Logger | None = None) -> None:
        window = data.get("window")
        if not window:
            return
        window["archived_at"] = fmt_dt(now_local())
        history = data.get("history", [])
        history.append(window)
        data["history"] = history[-HISTORY_LIMIT:]
        try:
            hist_dir = os.path.join(self.paths.state_dir, "history")
            os.makedirs(hist_dir, mode=0o700, exist_ok=True)
            dst = os.path.join(hist_dir, f"{window.get('id', 'unknown')}.json")
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump(window, fh, ensure_ascii=False, indent=2)
            os.chmod(dst, 0o600)
        except OSError as exc:
            if logger:
                logger.log(f"写入历史归档失败（不影响维护）: {exc}", "WARN")
        data["window"] = None
        if logger:
            logger.log(f"维护窗口 {window.get('id')} 已归档到 history/")

    # ---- pending health ----
    def load_pending_health(self) -> dict | None:
        p = Path(self.paths.pending_health)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_pending_health(self, payload: dict) -> None:
        try:
            os.makedirs(self.paths.state_dir, mode=0o700, exist_ok=True)
            with open(self.paths.pending_health, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.chmod(self.paths.pending_health, 0o600)
        except OSError:
            pass

    def clear_pending_health(self) -> None:
        try:
            os.unlink(self.paths.pending_health)
        except OSError:
            pass

    # ---- 中断标记 ----
    def read_in_progress(self) -> dict | None:
        p = Path(self.paths.in_progress)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pid": None}

    def write_in_progress(self, extra: dict | None = None) -> None:
        payload = {"pid": os.getpid(), "started_at": fmt_dt(now_local())}
        if extra:
            payload.update(extra)
        try:
            with open(self.paths.in_progress, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.chmod(self.paths.in_progress, 0o600)
        except OSError:
            pass

    def clear_in_progress(self) -> None:
        try:
            os.unlink(self.paths.in_progress)
        except OSError:
            pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------

class Mailer:
    def __init__(self, cfg: Config, logger: Logger):
        self.cfg = cfg
        self.logger = logger
        self.last_error: str | None = None

    def send(self, subject: str, body: str) -> bool:
        """发送纯文本邮件。任何失败都只记录日志并返回 False，绝不抛出。"""
        if not self.cfg.mail_ready:
            self.last_error = "SMTP 未配置完整，未发送"
            self.logger.log(f"邮件未发送: {self.last_error}", "WARN")
            return False
        msg = EmailMessage()
        msg["Subject"] = f"{self.cfg.subject_prefix} {subject}"
        sender = self.cfg.mail_from or self.cfg.smtp_user
        msg["From"] = sender
        msg["To"] = ", ".join(self.cfg.mail_to)
        msg["X-Mailer"] = f"{PROG} on {HOSTNAME}"
        msg.set_content(body, charset="utf-8")
        try:
            if self.cfg.smtp_security == "ssl":
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.cfg.smtp_host, self.cfg.smtp_port,
                                      timeout=30, context=context) as smtp:
                    if self.cfg.smtp_user:
                        smtp.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
                    smtp.ehlo()
                    if self.cfg.smtp_security == "starttls":
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                    if self.cfg.smtp_user:
                        smtp.login(self.cfg.smtp_user, self.cfg.smtp_pass)
                    smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - 邮件失败不得影响维护判定
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.logger.log(f"邮件发送失败（不影响本次维护结论）: {self.last_error}", "ERROR")
            return False
        self.logger.log(f"邮件已发送: {subject}")
        self.last_error = None
        return True


# ---------------------------------------------------------------------------
# 日期 / 维护窗口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    saturday: date

    @property
    def id(self) -> str:
        return self.saturday.strftime("%Y-%m-%d")

    @property
    def month(self) -> str:
        return self.saturday.strftime("%Y-%m")

    @property
    def sunday(self) -> date:
        return self.saturday + timedelta(days=1)

    def open_dt(self, cfg: Config) -> datetime:
        return datetime.combine(self.saturday, cfg.precheck_time)

    def upgrade_dt(self, cfg: Config) -> datetime:
        return datetime.combine(self.sunday, cfg.upgrade_time)

    def close_dt(self, cfg: Config) -> datetime:
        return self.upgrade_dt(cfg) + timedelta(hours=cfg.window_grace_hours)

    def describe(self, cfg: Config) -> str:
        return (f"窗口 {self.id}：预检查 {fmt_dt(self.open_dt(cfg))}，"
                f"升级 {fmt_dt(self.upgrade_dt(cfg))}（最迟 {fmt_dt(self.close_dt(cfg))}）")


def last_saturday(year: int, month: int) -> date:
    """当月最后一个周六。"""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    day = nxt - timedelta(days=1)
    while day.weekday() != PRECHECK_WEEKDAY:
        day -= timedelta(days=1)
    return day


def window_for_saturday(sat: date) -> Window:
    return Window(saturday=sat)


def candidate_saturdays(now: datetime) -> list[date]:
    """返回最近的候选周六（本月的 + 上月的），由近及远。"""
    sats = [last_saturday(now.year, now.month)]
    prev_last_day = date(now.year, now.month, 1) - timedelta(days=1)
    sats.append(last_saturday(prev_last_day.year, prev_last_day.month))
    return sorted(set(sats), reverse=True)


def find_window(now: datetime, cfg: Config) -> Window | None:
    """找到当前所处的维护窗口（唯一权威判定，预检查/升级共用）。"""
    for sat in candidate_saturdays(now):
        w = window_for_saturday(sat)
        if w.open_dt(cfg) <= now < w.close_dt(cfg):
            return w
    return None


def is_last_saturday(day: date) -> bool:
    return day.weekday() == PRECHECK_WEEKDAY and day == last_saturday(day.year, day.month)


def is_upgrade_sunday(day: date) -> bool:
    return day.weekday() == UPGRADE_WEEKDAY and day - timedelta(days=1) == last_saturday(
        (day - timedelta(days=1)).year, (day - timedelta(days=1)).month)


# ---------------------------------------------------------------------------
# 子进程封装与系统探测
# ---------------------------------------------------------------------------

@dataclass
class Res:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        return "\n".join(x for x in (self.out, self.err) if x)


def run(cmd: list[str] | str, timeout: int | None = 300, logger: Logger | None = None,
        env_extra: dict[str, str] | None = None) -> Res:
    """执行命令，绝不抛出；设置 LC_ALL=C 便于稳定解析。"""
    args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              env=env, errors="replace")
        return Res(proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError:
        return Res(127, "", f"命令不存在: {args[0]}")
    except subprocess.TimeoutExpired as exc:
        return Res(124, exc.stdout or "", f"命令超时({timeout}s): {' '.join(args)}")
    except OSError as exc:
        return Res(126, "", f"{type(exc).__name__}: {exc}")


def is_root() -> bool:
    return os.geteuid() == 0


def systemd_available() -> bool:
    return Path("/run/systemd/system").exists()


def unit_active(unit: str) -> tuple[str, int]:
    res = run(["systemctl", "is-active", unit], timeout=15)
    return res.out.strip() or "unknown", res.rc


def unit_enabled(unit: str) -> str:
    res = run(["systemctl", "is-enabled", unit], timeout=15)
    return res.out.strip() or res.err.strip() or "unknown"


def failed_units() -> list[str]:
    res = run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=30)
    units = []
    for line in res.out.splitlines():
        line = line.strip()
        if not line or line.startswith("0 loaded"):
            continue
        parts = line.split()
        if parts:
            units.append(parts[0].lstrip("●").strip())
    return units


def running_kernel() -> str:
    return os.uname().release


def installed_kernels() -> list[str]:
    """按安装时间倒序返回已安装内核实名版本（kernel-core）。"""
    res = run(["rpm", "-q", "--last", "--qf", "%{NAME}|%{VERSION}-%{RELEASE}.%{ARCH}\n",
               "kernel-core"], timeout=60)
    if res.rc != 0 or not res.out.strip():
        res = run(["rpm", "-q", "--last", "--qf", "%{NAME}|%{VERSION}-%{RELEASE}.%{ARCH}\n",
                   "kernel"], timeout=60)
    kernels = []
    for line in res.out.splitlines():
        line = line.strip()
        if not line or "not installed" in line or "|" not in line:
            continue
        name, evr = line.split("|", 1)
        if name.strip() in ("kernel-core", "kernel"):
            kernels.append(evr.strip())
    # 去重保序
    seen: set[str] = set()
    return [k for k in kernels if not (k in seen or seen.add(k))]


def newest_installed_kernel() -> str:
    ks = installed_kernels()
    return ks[0] if ks else ""


def rpm_snapshot() -> dict[str, str]:
    res = run(["rpm", "-qa", "--qf", "%{NAME}|%{VERSION}-%{RELEASE}.%{ARCH}\n"], timeout=180)
    snap: dict[str, str] = {}
    if res.rc != 0:
        return snap
    for line in res.out.splitlines():
        if "|" in line:
            name, evr = line.split("|", 1)
            snap[name] = evr
    return snap


def rpm_count() -> int:
    res = run(["rpm", "-qa", "--qf", "%{NAME}\n"], timeout=120)
    if res.rc != 0:
        return -1
    return len([x for x in res.out.splitlines() if x.strip()])


def listening_ports() -> set[int]:
    ports: set[int] = set()
    res = run(["ss", "-H", "-ltn"], timeout=30)
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        port_txt = local.rsplit(":", 1)[-1]
        if port_txt.isdigit():
            ports.add(int(port_txt))
    if not ports:
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(path).read_text().splitlines()[1:]
            except OSError:
                continue
            for ln in lines:
                f = ln.split()
                if len(f) > 3 and f[3] == "0A":
                    ports.add(int(f[1].rsplit(":", 1)[1], 16))
    return ports


def default_gateway() -> str:
    res = run(["ip", "-4", "route", "show", "default"], timeout=15)
    m = re.search(r"\bvia\s+(\S+)", res.out)
    if m:
        return m.group(1)
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                gw = int(fields[2], 16)
                return ".".join(str((gw >> (8 * i)) & 0xFF) for i in range(4))
    except OSError:
        pass
    return ""


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    severity: str = "ok"          # ok / info / warning / critical
    detail: str = ""
    tier: str = "critical"        # critical / optional / expected_stopped（健康检查用）

    def line(self) -> str:
        sym = SEV_SYMBOL.get(self.severity, "?")
        text = f"{sym} [{self.severity.upper()}] {self.name}"
        if self.detail:
            text += f" — {self.detail}"
        return text


def worst_severity(checks: list[Check]) -> str:
    worst = "ok"
    for c in checks:
        if SEV_ORDER.get(c.severity, 0) > SEV_ORDER.get(worst, 0):
            worst = c.severity
    return worst


def check_disk(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    targets = [
        ("/", cfg.min_free_root_gb * 1024**3, f"{cfg.min_free_root_gb} GiB"),
        ("/boot", cfg.min_free_boot_mb * 1024**2, f"{cfg.min_free_boot_mb} MiB"),
        ("/boot/efi", cfg.min_free_efi_mb * 1024**2, f"{cfg.min_free_efi_mb} MiB"),
    ]
    for path, threshold, threshold_human in targets:
        p = Path(path)
        if not p.exists():
            checks.append(Check(f"磁盘 {path}", "info", "路径不存在，跳过"))
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            checks.append(Check(f"磁盘 {path}", "critical", f"无法读取空间: {exc}"))
            continue
        free, total = usage.free, usage.total
        free_human = f"{free / 1024**3:.1f} GiB" if threshold >= 1024**3 else f"{free / 1024**2:.0f} MiB"
        total_human = f"{total / 1024**3:.1f} GiB" if threshold >= 1024**3 else f"{total / 1024**2:.0f} MiB"
        if free < threshold:
            checks.append(Check(f"磁盘 {path}", "critical",
                                f"可用 {free_human} / 总计 {total_human}，低于阈值 {threshold_human}"))
        elif free < threshold * 1.5:
            checks.append(Check(f"磁盘 {path}", "warning",
                                f"可用 {free_human} / 总计 {total_human}，接近阈值 {threshold_human}"))
        else:
            checks.append(Check(f"磁盘 {path}", "ok", f"可用 {free_human} / 总计 {total_human}"))
    return checks


def check_rpm_dnf(cfg: Config, logger: Logger) -> list[Check]:
    checks: list[Check] = []

    busy = run(["pgrep", "-x", "-d,", "dnf5|rpm"], timeout=15)
    if busy.ok and busy.out.strip():
        checks.append(Check("dnf/rpm 进行中", "warning",
                            f"检测到正在运行的 dnf5/rpm 进程: {busy.out.strip()}"))

    version = run(["dnf5", "--version"], timeout=30)
    if version.ok:
        first = version.out.splitlines()[0].strip() if version.out.strip() else "unknown"
        checks.append(Check("dnf5 可用", "ok", first))
    else:
        checks.append(Check("dnf5 可用", "critical", f"dnf5 --version 失败: {tail_text(version.text(), 3)}"))

    do_verify = cfg.rpm_verifydb == "always" or (cfg.rpm_verifydb == "auto")
    if do_verify and cfg.rpm_verifydb != "never":
        if not is_root() and cfg.rpm_verifydb == "auto":
            checks.append(Check("rpm 数据库校验", "info", "非 root 跳过 rpm --verifydb（安装后由 timer 以 root 执行）"))
        else:
            res = run(["rpm", "--verifydb"], timeout=600)
            if res.ok:
                checks.append(Check("rpm 数据库校验", "ok", "rpm --verifydb 通过"))
            else:
                checks.append(Check("rpm 数据库校验", "critical",
                                    f"rpm --verifydb 失败: {tail_text(res.text(), 5)}"))

    count = rpm_count()
    if count > 0:
        checks.append(Check("rpm 包数量", "ok", f"已安装 {count} 个包，rpm -qa 正常"))
    elif count == 0:
        checks.append(Check("rpm 包数量", "critical", "rpm -qa 返回 0 个包，数据库异常"))
    else:
        checks.append(Check("rpm 包数量", "critical", "rpm -qa 执行失败"))

    return checks


def refresh_metadata(cfg: Config, logger: Logger) -> tuple[Check, list[tuple[str, str, str]]]:
    """刷新元数据并返回待更新清单。返回 (Check, upgrades)。"""
    logger.log("刷新 dnf 元数据（dnf5 --refresh makecache）…")
    res = run(["dnf5", "--refresh", "makecache"], timeout=900, logger=logger)
    if not res.ok:
        logger.log(f"makecache 返回 {res.rc}，尝试用 repoquery 复核…", "WARN")
        probe = run(["dnf5", "--refresh", "repoquery", "--upgrades",
                     "--qf", "%{name}|%{evr}|%{arch}"], timeout=900)
        if probe.ok:
            check = Check("dnf 元数据刷新", "warning",
                          "makecache 有仓库报错，但 repoquery 可用（可能是第三方仓库抖动）")
            return check, parse_upgrade_lines(probe.out)
        check = Check("dnf 元数据刷新", "critical",
                      f"元数据刷新失败: {tail_text(res.text(), 8)}")
        return check, []

    upgrades = list_upgrades(cfg, logger)
    if upgrades is None:
        return Check("dnf 元数据刷新", "critical", "元数据已刷新，但无法获取待更新清单"), []
    return Check("dnf 元数据刷新", "ok", "dnf5 元数据刷新成功"), upgrades


def list_upgrades(cfg: Config, logger: Logger) -> list[tuple[str, str, str]] | None:
    res = run(["dnf5", "repoquery", "--upgrades", "--qf", "%{name}|%{evr}|%{arch}"], timeout=600)
    if not res.ok:
        logger.log(f"repoquery --upgrades 失败: {tail_text(res.text(), 5)}", "ERROR")
        return None
    return parse_upgrade_lines(res.out)


def parse_upgrade_lines(text: str) -> list[tuple[str, str, str]]:
    upgrades: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, evr, arch = (p.strip() for p in parts)
        if name in seen:
            continue
        seen.add(name)
        upgrades.append((name, evr, arch))
    return upgrades


def check_network() -> list[Check]:
    checks: list[Check] = []
    gw = default_gateway()
    if not gw:
        checks.append(Check("默认路由", "critical", "没有默认路由"))
    else:
        checks.append(Check("默认路由", "ok", f"网关 {gw}"))
        res = run(["ping", "-c", "2", "-W", "2", gw], timeout=15)
        if res.ok:
            m = re.search(r"(\d+)% packet loss", res.out)
            loss = m.group(1) + "%" if m else "?"
            checks.append(Check("网关连通性", "ok" if (m and m.group(1) == "0") else "warning",
                                f"ping {gw} 丢包 {loss}"))
        else:
            checks.append(Check("网关连通性", "critical", f"ping {gw} 失败"))
    res = run(["getent", "ahosts", "www.163.com"], timeout=15)
    if res.ok and res.out.strip():
        checks.append(Check("DNS 解析", "ok", "www.163.com 可解析"))
    else:
        checks.append(Check("DNS 解析", "warning", "www.163.com 解析失败"))
    return checks


# ---------------------------------------------------------------------------
# 内核 / 启动项（重启就绪判定，按「证据 + 分级」而非固定文件名硬判）
# ---------------------------------------------------------------------------

def scan_boot_entries() -> tuple[dict[str, dict], str]:
    """扫描 BLS 启动项。

    返回 ({version: {kernel, initrd, conf}}, state)，state ∈
      ok          目录可读且至少有一个条目（可作为「完整可见」证据）
      empty       目录可读但没有 *.conf
      unreadable  目录存在但当前用户无法读取
      no-dir      没有 BLS 目录（可能使用其它启动布局）
    """
    dirs = ["/boot/loader/entries", "/boot/efi/loader/entries"]
    existing = [d for d in dirs if Path(d).is_dir()]
    if not existing:
        return {}, "no-dir"
    entries: dict[str, dict] = {}
    readable_any = False
    for d in existing:
        if not os.access(d, os.R_OK | os.X_OK):
            continue
        readable_any = True
        try:
            confs = sorted(glob.glob(os.path.join(d, "*.conf")))
        except OSError:
            continue
        for conf in confs:
            try:
                text = Path(conf).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            info: dict[str, str] = {"conf": conf}
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition(" ")
                value = value.strip().strip('"')
                if key in ("version", "linux", "initrd", "title"):
                    info[key] = value
            ver = info.get("version", "")
            if not ver:
                m = re.search(r"vmlinuz-(.+?)(?:\s|$)", info.get("linux", ""))
                if m:
                    ver = m.group(1)
            if not ver:
                m = re.search(r"(\d+\.\d+\.\d+[^/]*)$", os.path.basename(conf).replace(".conf", ""))
                if m:
                    ver = m.group(1).lstrip("-")
            if ver:
                entries[ver] = info
    if not readable_any:
        return {}, "unreadable"
    return entries, ("ok" if entries else "empty")


def kernel_boot_readiness(version: str) -> tuple[str, list[str]]:
    """判断新内核是否安装完整且存在可靠启动方式。

    返回 (status, details)，status ∈ ok / warning / fail。
    判定原则（在本机实测校准过，避免过度严格而误判成功更新）：
      * vmlinuz / initramfs 缺失或为空 → fail（安装不完整，绝不重启）
      * 有「完整可见」的启动机制（可读的 BLS 条目或有效的 grubby 输出）但确认没有引用该内核
        → fail（没有可靠启动方式）
      * 启动机制不可见（非 root、无 BLS 布局、grubby 无效输出）或仅在 UKI 目录里没找到
        → warning（不阻断重启，仅记录；重启后健康检查会核对运行内核）
      * 启动项存在但默认项不是它 → warning
    """
    details: list[str] = []
    vmlinuz = f"/boot/vmlinuz-{version}"
    initramfs = f"/boot/initramfs-{version}.img"
    try:
        v_ok = Path(vmlinuz).is_file() and Path(vmlinuz).stat().st_size > 0
        i_ok = Path(initramfs).is_file() and Path(initramfs).stat().st_size > 0
    except OSError as exc:
        v_ok = i_ok = False
        details.append(f"检查内核文件出错: {exc}")
    details.append(f"{vmlinuz}: {'存在' if v_ok else '缺失'}")
    details.append(f"{initramfs}: {'存在' if i_ok else '缺失'}")

    found_any: bool | None = None
    full_view = False  # 是否存在「完整可见」的证据来源

    # 证据 1：BLS 启动项
    entries, bls_state = scan_boot_entries()
    if bls_state in ("ok", "empty"):
        full_view = True
        bls_found = any(version in ver or version in info.get("linux", "")
                        or version in info.get("initrd", "")
                        for ver, info in entries.items())
        found_any = bls_found if found_any is None else (found_any or bls_found)
        details.append(f"BLS 启动项: {'已找到' if bls_found else '未找到'}（{len(entries)} 个条目）")
    else:
        details.append(f"BLS 启动项: 无法确认（{bls_state}）")

    # 证据 2：grubby（覆盖非 BLS 的 grub.cfg；输出必须真的含 kernel= 行才算有效）
    grubby_out = run(["grubby", "--info=ALL"], timeout=60)
    valid_grubby = grubby_out.ok and "kernel=" in grubby_out.out
    if valid_grubby:
        full_view = True
        g_found = bool(re.search(rf'kernel\s*=\s*"?[^"\s]*{re.escape(version)}', grubby_out.out))
        found_any = g_found if found_any is None else (found_any or g_found)
        details.append(f"grubby 条目: {'已找到' if g_found else '未找到'}")
    else:
        details.append("grubby 条目: 无法确认（非 root 或输出无效，不作为判定依据）")

    # 证据 3：UKI（unified kernel image）与 kernel-install 目录布局
    uki_matches: list[str] = []
    for ud in ("/boot/EFI/Linux", "/boot/efi/EFI/Linux"):
        if Path(ud).is_dir() and os.access(ud, os.R_OK | os.X_OK):
            try:
                uki_matches += glob.glob(os.path.join(ud, f"*{version}*.efi"))
            except OSError:
                pass
    machine_id = ""
    try:
        machine_id = Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    if machine_id and Path(f"/boot/{machine_id}/{version}").is_dir():
        found_any = True
        details.append(f"kernel-install 目录: 已找到 /boot/{machine_id}/{version}")
    if uki_matches:
        found_any = True
        details.append(f"UKI: 已找到 {', '.join(os.path.basename(p) for p in uki_matches)}")

    # 证据 4：默认启动内核（仅当输出是真实文件路径时才可信）
    default_kernel = ""
    d = run(["grubby", "--default-kernel"], timeout=30)
    if d.ok and d.out.strip() and Path(d.out.strip()).is_file():
        default_kernel = os.path.basename(d.out.strip())
        details.append(f"默认启动内核: {default_kernel}")
    else:
        details.append("默认启动内核: 无法确认")

    # 结论
    if not (v_ok and i_ok):
        status = "fail"
    elif found_any is False and full_view:
        status = "fail"
        details.append("可读的启动机制中没有任何一条引用该内核，判定为不可靠启动")
    elif found_any is True and default_kernel and version not in default_kernel:
        status = "warning"
        details.append("启动项存在，但默认启动项不是新内核（重启后可能仍运行旧内核，健康检查会确认）")
    elif found_any is None:
        status = "warning"
        details.append("无法确认启动项（非 root / 无 BLS 布局），按 warning 处理，不阻断重启")
    else:
        status = "ok"
    return status, details



def bootinfo_report(logger: Logger) -> str:
    lines: list[str] = []
    run_kernel = running_kernel()
    kernels = installed_kernels()
    newest = kernels[0] if kernels else ""
    lines.append(f"运行内核: {run_kernel}")
    lines.append(f"已安装内核(按安装时间倒序): {', '.join(kernels) or '未知'}")
    lines.append(f"最新已安装内核: {newest or '未知'}")
    try:
        boot_mount = run(["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "/boot"], timeout=15)
        lines.append(f"/boot 挂载: {boot_mount.out.strip() or '（未单独挂载）'}")
    except Exception:  # noqa: BLE001
        pass
    entries, state = scan_boot_entries()
    lines.append(f"启动项目录状态: {state}（/boot/loader/entries）")
    for ver, info in sorted(entries.items()):
        lines.append(f"  - {ver}: linux={info.get('linux', '?')} initrd={info.get('initrd', '?')}")
    g = run(["grubby", "--default-kernel"], timeout=30)
    lines.append(f"grubby 默认内核: {g.out.strip() or g.err.strip() or '无法查询'}")
    if newest:
        status, details = kernel_boot_readiness(newest)
        lines.append(f"重启就绪判定（{newest}）: {status}")
        for d in details:
            lines.append(f"  · {d}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 健康检查名单（services.conf）
# ---------------------------------------------------------------------------

@dataclass
class Services:
    systemd: dict[str, list[str]] = field(default_factory=lambda: {
        "critical": [], "optional": [], "expected_stopped": []})
    containers: dict[str, list[str]] = field(default_factory=lambda: {
        "critical": [], "optional": [], "expected_stopped": []})
    mounts: dict[str, list[str]] = field(default_factory=lambda: {
        "critical": [], "optional": []})
    ports: dict[str, list[str]] = field(default_factory=lambda: {
        "critical": [], "optional": []})
    rootless_user: str = DEFAULT_ROOTLESS_USER

    def tier_of_systemd(self, unit: str) -> str:
        for tier in ("critical", "optional", "expected_stopped"):
            if unit in self.systemd[tier]:
                return tier
        return "unknown"

    def tier_of_container(self, name: str) -> str:
        for tier in ("critical", "optional", "expected_stopped"):
            if name in self.containers[tier]:
                return tier
        return "unknown"


def default_services() -> Services:
    svc = Services()
    svc.systemd["critical"] = ["sshd.service", "NetworkManager.service", "firewalld.service",
                               "smb.service", "nmb.service", "chronyd.service",
                               "systemd-resolved.service", "podman.socket"]
    svc.systemd["optional"] = ["cockpit.socket"]
    svc.systemd["expected_stopped"] = []
    svc.containers["critical"] = ["syncthing", "qbittorrent-ee", "openlist", "openlist_mysql",
                                  "openlist_meilisearch", "open-webui-pure", "frpc", "localsend",
                                  "peerbanhelper", "bili-sync-rs", "ubuntu-xfce-webtop"]
    svc.containers["optional"] = ["vnstat-dashboard", "archivebox", "fluxdown-server",
                                  "antigravity-manager", "scrutiny"]
    svc.containers["expected_stopped"] = ["jellyfin", "minecraft", "terraria"]
    svc.mounts["critical"] = ["/mnt/Old-1", "/mnt/Old-2", "/mnt/New-1", "/mnt/New-2", "/mnt/SSD-Cache"]
    svc.ports["critical"] = ["445"]
    svc.ports["optional"] = ["1145", "8384", "7474", "8080"]
    return svc


def load_services(path: str) -> tuple[Services, list[str]]:
    """解析 services.conf：[systemd:critical] / [containers:optional] / [mounts:critical] ...

    文件不存在时返回内置默认值（安装脚本会生成真实文件）。
    """
    svc = default_services()
    warnings: list[str] = []
    p = Path(path)
    if not p.exists():
        warnings.append(f"{path} 不存在，使用内置默认名单")
        return svc, warnings
    current: tuple[str, str] | None = None
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name == "settings":
                current = ("settings", "")
                continue
            if ":" not in name:
                warnings.append(f"{path}:{lineno}: 段名应为 [类别:级别]，忽略: {name}")
                current = None
                continue
            cat, tier = (x.strip() for x in name.split(":", 1))
            if cat not in ("systemd", "containers", "mounts", "ports") or \
               tier not in ("critical", "optional", "expected_stopped"):
                warnings.append(f"{path}:{lineno}: 未知段 [{name}]，忽略")
                current = None
                continue
            current = (cat, tier)
            continue
        if current is None:
            continue
        if current[0] == "settings":
            if "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k == "rootless_user":
                    svc.rootless_user = v
            continue
        cat, tier = current
        item = line.split("#", 1)[0].strip()
        if not item:
            continue
        target = getattr(svc, cat)
        if item not in target[tier]:
            target[tier].append(item)
    return svc, warnings


# ---------------------------------------------------------------------------
# 容器 / 健康检查实现
# ---------------------------------------------------------------------------

def rootless_env(user: str) -> dict[str, str]:
    uid_res = run(["id", "-u", user], timeout=15)
    uid = uid_res.out.strip() if uid_res.ok else "1000"
    return {"XDG_RUNTIME_DIR": f"/run/user/{uid}"}


def podman_ps(scope: str, user: str) -> tuple[dict[str, dict], str]:
    """返回 {name: {state, status, scope}} 与错误说明。scope ∈ root/rootless。"""
    if scope == "root":
        if not is_root():
            return {}, "非 root，无法查询 rootful 容器"
        cmd = ["podman", "ps", "-a", "--format", "json"]
    else:
        if not is_root():
            # 非 root（测试/dry-run）时当前用户即 rootless 视角
            cmd = ["podman", "ps", "-a", "--format", "json"]
        else:
            cmd = ["runuser", "-u", user, "--", "env",
                   f"XDG_RUNTIME_DIR={rootless_env(user)['XDG_RUNTIME_DIR']}",
                   "podman", "ps", "-a", "--format", "json"]
    res = run(cmd, timeout=60)
    if not res.ok:
        return {}, f"podman ps ({scope}) 失败: {tail_text(res.text(), 3)}"
    try:
        data = json.loads(res.out or "[]")
    except json.JSONDecodeError as exc:
        return {}, f"podman ps ({scope}) 输出无法解析: {exc}"
    result: dict[str, dict] = {}
    for item in data:
        names = item.get("Names") or []
        name = names[0] if isinstance(names, list) and names else str(names)
        if not name:
            continue
        result[name] = {
            "state": str(item.get("State", "")).lower(),
            "status": str(item.get("Status", "")),
            "image": str(item.get("Image", "")),
            "scope": scope,
        }
    return result, ""


def containers_snapshot(user: str) -> tuple[dict[str, dict], list[str], list[str]]:
    """返回 (容器字典, 提示信息, 查询失败的范围列表)。"""
    notes: list[str] = []
    failed_scopes: list[str] = []
    allc: dict[str, dict] = {}
    for scope in ("rootless", "root"):
        data, err = podman_ps(scope, user)
        if err:
            notes.append(err)
            failed_scopes.append(scope)
        for k, v in data.items():
            allc[k] = v
    return allc, notes, failed_scopes


def check_services_health(svc: Services, cfg: Config, logger: Logger,
                          pending: dict | None) -> list[Check]:
    checks: list[Check] = []
    user = svc.rootless_user

    # --- systemd 总体状态 ---
    overall = run(["systemctl", "is-system-running"], timeout=30).out.strip() or "unknown"
    if overall in ("running", "degraded"):
        checks.append(Check("systemd 总体状态", "ok" if overall == "running" else "warning",
                            f"is-system-running = {overall}"))
    elif overall in ("starting", "reloading"):
        checks.append(Check("systemd 总体状态", "warning", f"is-system-running = {overall}"))
    else:
        checks.append(Check("systemd 总体状态", "critical", f"is-system-running = {overall}"))

    # --- 失败 unit（按级别归类）---
    known_units = set()
    for tier in svc.systemd.values():
        known_units.update(tier)
    failed = failed_units()
    if failed:
        crit, warn, info = [], [], []
        for u in failed:
            tier = svc.tier_of_systemd(u)
            if tier == "critical":
                crit.append(u)
            elif tier in ("optional",):
                warn.append(u)
            elif tier == "expected_stopped":
                info.append(u)
            else:
                warn.append(u)
        if crit:
            checks.append(Check("失败的 systemd 单元", "critical", "、".join(crit)))
        if warn:
            checks.append(Check("失败的 systemd 单元(非关键)", "warning", "、".join(warn)))
        if info:
            checks.append(Check("失败的 systemd 单元(按需服务)", "info", "、".join(info)))
    else:
        checks.append(Check("失败的 systemd 单元", "ok", "systemctl --failed 为空"))

    # --- 关键 unit 状态 ---
    for tier in ("critical", "optional", "expected_stopped"):
        for unit in svc.systemd[tier]:
            state, rc = unit_active(unit)
            if tier == "critical":
                sev = "ok" if state == "active" else "critical"
            elif tier == "optional":
                sev = "ok" if state == "active" else "warning"
            else:
                sev = "info" if state == "active" else "ok"
            if state == "active":
                detail = "active"
            elif state == "unknown" or rc == 4:
                detail = "单元不存在"
            else:
                detail = state
            checks.append(Check(f"unit {unit}", sev, detail, tier=tier))

    # --- 挂载点 ---
    for tier in ("critical", "optional"):
        for mount in svc.mounts[tier]:
            if os.path.ismount(mount):
                checks.append(Check(f"挂载 {mount}", "ok", "已挂载", tier=tier))
            else:
                sev = "critical" if tier == "critical" else "warning"
                checks.append(Check(f"挂载 {mount}", sev, "未挂载", tier=tier))

    # --- 端口 ---
    ports = listening_ports()
    for tier in ("critical", "optional"):
        for port_txt in svc.ports[tier]:
            if not port_txt.isdigit():
                continue
            port = int(port_txt)
            if port in ports:
                checks.append(Check(f"端口 {port}", "ok", "监听中", tier=tier))
            else:
                sev = "critical" if tier == "critical" else "warning"
                checks.append(Check(f"端口 {port}", sev, "未监听", tier=tier))

    # --- 网络 ---
    checks.extend(check_network())

    # --- podman ---
    for scope in ("rootless", "root"):
        if scope == "rootless":
            if not is_root():
                checks.append(Check("podman (rootless)", "info", "非 root 跳过"))
                continue
            res = run(["runuser", "-u", user, "--", "env",
                       f"XDG_RUNTIME_DIR={rootless_env(user)['XDG_RUNTIME_DIR']}",
                       "podman", "info"], timeout=60)
        else:
            res = run(["podman", "info"], timeout=60)
        checks.append(Check(f"podman ({scope})", "ok" if res.ok else "critical",
                            "podman info 正常" if res.ok else tail_text(res.text(), 3)))

    # --- 容器（等待恢复 + 分级别判定）---
    expected_names = set(svc.containers["critical"]) | set(svc.containers["optional"])
    deadline = time.monotonic() + max(0, cfg.health_wait_seconds)
    before = ((pending or {}).get("containers_before") or {})
    # 只等待「重启前确实在运行的 critical 容器」；非 root 视角不完整，避免无谓等待
    wait_expected = {n for n in svc.containers["critical"]
                     if not before or (before.get(n) or {}).get("state") == "running"}
    if not is_root():
        wait_expected = set()
    snapshot: dict[str, dict] = {}
    notes: list[str] = []
    failed_scopes: list[str] = []
    while True:
        snapshot, notes, failed_scopes = containers_snapshot(user)
        missing = [n for n in sorted(wait_expected) if snapshot.get(n, {}).get("state") != "running"]
        if not missing or time.monotonic() >= deadline:
            break
        logger.log(f"等待容器恢复中（还差: {', '.join(missing)}）…")
        time.sleep(15)

    def unknown_note() -> str:
        return "无法确认（" + "、".join(f"{s} podman 查询失败" for s in failed_scopes) + "）"

    for name in svc.containers["critical"]:
        c = snapshot.get(name)
        if c is None:
            if failed_scopes:
                checks.append(Check(f"容器 {name}", "warning", unknown_note(), tier="critical"))
            else:
                checks.append(Check(f"容器 {name}", "critical", "未找到该容器", tier="critical"))
        elif c["state"] != "running":
            checks.append(Check(f"容器 {name}", "critical", c.get("status", c["state"]), tier="critical"))
        elif "unhealthy" in c.get("status", "").lower():
            checks.append(Check(f"容器 {name}", "critical",
                                f"{c['status']}（healthcheck unhealthy）", tier="critical"))
        else:
            checks.append(Check(f"容器 {name}", "ok", c["status"], tier="critical"))
    for name in svc.containers["optional"]:
        c = snapshot.get(name)
        was_running = before.get(name, {}).get("state") == "running"
        if c is None:
            if was_running:
                checks.append(Check(f"容器 {name}", "warning",
                                    "重启前在运行，现在找不到该容器", tier="optional"))
            elif failed_scopes:
                checks.append(Check(f"容器 {name}", "warning", unknown_note(), tier="optional"))
            else:
                checks.append(Check(f"容器 {name}", "info", "未运行（按需）", tier="optional"))
        elif c["state"] != "running":
            sev = "warning" if was_running else "info"
            checks.append(Check(f"容器 {name}", sev, c.get("status", c["state"]), tier="optional"))
        elif "unhealthy" in c.get("status", "").lower():
            checks.append(Check(f"容器 {name}", "warning",
                                f"{c['status']}（healthcheck unhealthy）", tier="optional"))
        else:
            checks.append(Check(f"容器 {name}", "ok", c["status"], tier="optional"))
    for name in svc.containers["expected_stopped"]:
        c = snapshot.get(name)
        if c is None or c["state"] != "running":
            checks.append(Check(f"容器 {name}", "ok", "未运行（按需服务，符合预期）", tier="expected_stopped"))
        else:
            checks.append(Check(f"容器 {name}", "info", f"正在运行: {c.get('status', '')}", tier="expected_stopped"))
    # 未列入名单的容器
    unlisted = sorted(set(snapshot) - expected_names - set(svc.containers["expected_stopped"]))
    if unlisted:
        checks.append(Check("未列入名单的容器", "info", "、".join(unlisted)))

    for note in notes:
        checks.append(Check("容器查询提示", "info", note))

    # --- 运行内核 vs 最新已装内核（按 warning，不因此判「服务异常」）---
    run_kernel = running_kernel()
    newest = newest_installed_kernel()
    if newest and run_kernel != newest:
        checks.append(Check("运行内核", "warning",
                            f"运行 {run_kernel}，最新已安装 {newest}（内核更新未生效，可能需要再次重启或检查启动项）"))
    else:
        checks.append(Check("运行内核", "ok", run_kernel))

    # --- 重启确认 ---
    if pending:
        same_boot = pending.get("boot_id_before") and pending["boot_id_before"] == boot_id()
        checks.append(Check("重启确认", "info" if not same_boot else "warning",
                            "已进入新 boot" if not same_boot else "仍是同一 boot_id（未真正重启）"))

    checks.append(Check("运行时长", "info", f"{uptime_seconds() / 3600:.1f} 小时"))
    return checks


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_report(title: str, checks: list[Check], intro: list[str] | None = None,
                  extra: list[str] | None = None, log_path: str | None = None) -> str:
    lines = [title, "=" * len(title), ""]
    if intro:
        lines.extend(intro)
        lines.append("")
    groups = [("critical", "严重问题（需要人工介入）"),
              ("warning", "警告（建议关注）"),
              ("info", "信息"),
              ("ok", "正常")]
    for sev, label in groups:
        subset = [c for c in checks if c.severity == sev]
        if not subset:
            continue
        lines.append(f"【{label}】")
        for c in subset:
            lines.append("  " + c.line())
        lines.append("")
    if extra:
        lines.extend(extra)
        lines.append("")
    lines.append(f"主机: {HOSTNAME}    生成时间: {fmt_dt(now_local())}")
    if log_path:
        lines.append(f"完整日志: {log_path}")
    lines.append("")
    lines.append("—— 本邮件由 fedora-nas 月度维护脚本自动发送")
    return "\n".join(lines)


def log_tail(path: str | None, lines: int = 15) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return tail_text(text, lines)


# ---------------------------------------------------------------------------
# 主流程上下文
# ---------------------------------------------------------------------------

@dataclass
class Ctx:
    cfg: Config
    paths: Paths
    logger: Logger
    store: StateStore
    mailer: Mailer
    svc: Services
    args: argparse.Namespace
    now: datetime = field(default_factory=now_local)

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.args, "dry_run", False))


def load_ctx(args: argparse.Namespace, mode: str) -> Ctx:
    paths = Paths(
        conf=getattr(args, "config", DEFAULT_CONF),
        services=getattr(args, "services", DEFAULT_SERVICES),
        skip_flag=getattr(args, "skip_flag", DEFAULT_SKIP_FLAG),
        state_dir=getattr(args, "state_dir", DEFAULT_STATE_DIR),
        log_dir=getattr(args, "log_dir", DEFAULT_LOG_DIR),
        lock_file=getattr(args, "lock_file", DEFAULT_LOCK_FILE),
    )
    cfg, warnings = load_config(paths.conf)
    svc, svc_warnings = load_services(paths.services)
    logger = Logger(paths.log_dir, mode)
    for w in warnings + svc_warnings:
        logger.log(w, "WARN")
    store = StateStore(paths, logger)
    mailer = Mailer(cfg, logger)
    now = getattr(args, "now", None) or now_local()
    os.environ.setdefault("TZ", "Asia/Shanghai")
    return Ctx(cfg=cfg, paths=paths, logger=logger, store=store, mailer=mailer,
               svc=svc, args=args, now=now)


def finish(ctx: Ctx, rc: int) -> int:
    ctx.logger.close()
    return rc


# ---------------------------------------------------------------------------
# 命令：初始配置 / 渲染 unit
# ---------------------------------------------------------------------------

def cmd_init_config(args: argparse.Namespace) -> int:
    template = args.template
    dest = Path(args.dest)
    scrutiny = Path(args.scrutiny)
    dest_exists = dest.exists()
    if dest_exists and not args.force:
        print(f"[info] {dest} 已存在，不做覆盖（--force 可强制重建）")
    else:
        if not Path(template).exists():
            eprint(f"[error] 模板不存在: {template}")
            return 2
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(Path(template).read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(dest, 0o600)
        print(f"[ok] 已生成 {dest}（权限 600）")

    # ---- 可选的 Scrutiny 一次性导入（convenience，不是安装成功的必要条件）----
    imported: dict[str, str] = {}
    note = ""
    if not scrutiny.exists():
        note = f"Scrutiny 配置不存在（{scrutiny}），跳过 SMTP 自动导入"
    else:
        try:
            import yaml  # type: ignore
        except ImportError:
            note = "未安装 PyYAML，跳过 SMTP 自动导入（不会猜测字段，请手动填写）"
        else:
            try:
                raw = yaml.safe_load(scrutiny.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001
                note = f"Scrutiny YAML 解析失败（{type(exc).__name__}: {exc}），跳过自动导入，请手动填写"
            else:
                urls = []
                if isinstance(raw.get("notify"), dict):
                    urls = raw["notify"].get("urls") or []
                if not isinstance(urls, list):
                    note = "Scrutiny YAML 结构与预期不符（notify.urls 不是列表），跳过自动导入"
                else:
                    import urllib.parse
                    for item in urls:
                        if not isinstance(item, str) or not item.startswith("smtp"):
                            continue
                        parsed = urllib.parse.urlparse(item)
                        if parsed.scheme not in ("smtp", "smtps"):
                            continue
                        query = urllib.parse.parse_qs(parsed.query or "")
                        if parsed.username:
                            imported["SMTP_USER"] = urllib.parse.unquote(parsed.username)
                        if parsed.password:
                            imported["SMTP_PASS"] = urllib.parse.unquote(parsed.password)
                        if parsed.hostname:
                            imported["SMTP_HOST"] = parsed.hostname
                        if parsed.port:
                            imported["SMTP_PORT"] = str(parsed.port)
                        imported["SMTP_SECURITY"] = "starttls" if parsed.scheme == "smtp" and parsed.port == 587 else "ssl"
                        if query.get("fromAddress"):
                            imported["MAIL_FROM"] = query["fromAddress"][0]
                        if query.get("toAddresses"):
                            imported["MAIL_TO"] = query["toAddresses"][0]
                        break
                    if not imported:
                        note = "Scrutiny YAML 中没有可用的 smtp:// 通知 URL，跳过自动导入"

    if imported:
        lines = dest.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in imported:
                    line = f"{key}={imported[key]}"
            out.append(line)
        dest.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(dest, 0o600)
        masked = {k: (redact(v) if "PASS" in k else v) for k, v in imported.items()}
        print("[ok] 已从 Scrutiny YAML 一次性导入 SMTP 参数（之后两者互不依赖）:")
        for k, v in masked.items():
            print(f"      {k}={v}")
        note = ""
    if note:
        print(f"[warn] {note}")
    remaining = dest.read_text(encoding="utf-8")
    if "<your-" in remaining:
        print("[next] SMTP 仍是占位符，请补全后再启用维护:")
        print(f"       sudoedit {dest}")
        print("       然后重跑: sudo bash ~/Tools/install_monthly_maintenance.sh")
    else:
        print("[ok] SMTP 配置看起来已完整（可用 mail-test 验证发信）")
    return 0


SERVICE_TEMPLATE = """[Unit]
Description={desc}
Documentation=file:///home/ywpc/Tools/fedora_nas_maintenance.py
After={after}
Wants=network-online.target
{extra_unit}
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/ywpc/Tools/fedora_nas_maintenance.py {mode}
Nice={nice}
IOSchedulingClass=best-effort
IOSchedulingPriority=6
TimeoutStartSec={timeout}
UMask=0027
"""


def render_units(conf_path: str | None, dest_dir: str, force: bool = False) -> tuple[int, list[str]]:
    if conf_path and Path(conf_path).exists():
        cfg, _warnings = load_config(conf_path)
    else:
        cfg = Config()
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    precheck_hhmm = cfg.precheck_time.strftime("%H:%M:%S")
    upgrade_hhmm = cfg.upgrade_time.strftime("%H:%M:%S")
    units: dict[str, str] = {}

    units["fedora-nas-monthly-precheck.service"] = SERVICE_TEMPLATE.format(
        desc="Fedora-NAS monthly maintenance precheck (last Saturday)",
        after="network-online.target",
        mode="precheck", nice=10, timeout=1800, extra_unit="")
    units["fedora-nas-monthly-precheck.timer"] = (
        "[Unit]\n"
        "Description=Fedora-NAS monthly maintenance precheck (candidate last Saturdays)\n\n"
        "[Timer]\n"
        "# 候选：每月 22..31 日的周六；真正的「最后一个周六」由脚本严格判定\n"
        f"OnCalendar=Sat *-*-22..31 {precheck_hhmm}\n"
        "Persistent=true\n"
        "AccuracySec=1min\n"
        "Unit=fedora-nas-monthly-precheck.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    units["fedora-nas-monthly-upgrade.service"] = SERVICE_TEMPLATE.format(
        desc="Fedora-NAS monthly full dnf5 upgrade (Sunday after last Saturday)",
        after="network-online.target fedora-nas-monthly-precheck.service",
        mode="upgrade", nice=5, timeout=cfg.dnf_timeout_seconds + 1800,
        extra_unit="")
    units["fedora-nas-monthly-upgrade.timer"] = (
        "[Unit]\n"
        "Description=Fedora-NAS monthly upgrade (Sunday after last Saturday; may cross month)\n\n"
        "[Timer]\n"
        f"OnCalendar=Sun *-*-23..31 {upgrade_hhmm}\n"
        f"OnCalendar=Sun *-*-01..03 {upgrade_hhmm}\n"
        "Persistent=true\n"
        "AccuracySec=1min\n"
        "Unit=fedora-nas-monthly-upgrade.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    units["fedora-nas-monthly-health.service"] = SERVICE_TEMPLATE.format(
        desc="Fedora-NAS post-reboot health check",
        after="network-online.target local-fs.target",
        mode="health", nice=10, timeout=900,
        extra_unit="ConditionPathExists=/var/lib/fedora-nas-update/pending-health.json\n")
    units["fedora-nas-monthly-health.timer"] = (
        "[Unit]\n"
        "Description=Fedora-NAS post-reboot health check (3 min after boot)\n\n"
        "[Timer]\n"
        "OnBootSec=3min\n"
        "AccuracySec=30s\n"
        "Unit=fedora-nas-monthly-health.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    written: list[str] = []
    for name, content in units.items():
        p = dest / name
        if p.exists() and not force:
            same = p.read_text(encoding="utf-8") == content
            if same:
                written.append(f"{name} (未变化)")
                continue
        p.write_text(content, encoding="utf-8")
        os.chmod(p, 0o644)
        written.append(name)
    return 0, written


def cmd_render_units(args: argparse.Namespace) -> int:
    rc, written = render_units(args.config, args.dest, force=args.force)
    for name in written:
        print(f"[unit] {name}")
    return rc


def cmd_selftest(args: argparse.Namespace) -> int:
    """日期窗口逻辑自检（无副作用）。"""
    if Path(args.config).exists():
        try:
            cfg, _warn = load_config(args.config)
        except ConfigError as exc:
            eprint(f"[error] 配置无法加载: {exc}")
            return 2
    else:
        cfg = Config()
        print(f"[info] {args.config} 不存在，使用内置默认配置（{cfg.precheck_time} / "
              f"{cfg.upgrade_time} / grace {cfg.window_grace_hours}h）")
    failures: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # 1) 每个月的窗口基本性质 + timer 候选覆盖
    for year in range(2020, 2061):
        for month in range(1, 13):
            sat = last_saturday(year, month)
            sun = sat + timedelta(days=1)
            expect(sat.weekday() == PRECHECK_WEEKDAY, f"{year}-{month}: 最后一个周六不是周六")
            expect(22 <= sat.day <= 31, f"{year}-{month}: 最后一个周六日期 {sat.day} 不在 22..31")
            expect(sat.day in SAT_CANDIDATE_DAYS, f"{year}-{month}: 预检查 timer 候选未覆盖 {sat}")
            expect(sun.day in SUN_CANDIDATE_DAYS,
                   f"{year}-{month}: 升级 timer 候选未覆盖 {sun}（跨月场景）")
            expect(is_last_saturday(sat), f"{year}-{month}: is_last_saturday({sat}) 判定失败")
            # 窗口开闭
            open_dt = datetime.combine(sat, cfg.precheck_time)
            close_dt = datetime.combine(sun, cfg.upgrade_time) + timedelta(hours=cfg.window_grace_hours)
            expect(find_window(open_dt, cfg) == Window(sat), f"{year}-{month}: 周六 20:00 未命中窗口")
            expect(find_window(datetime.combine(sun, cfg.upgrade_time), cfg) == Window(sat),
                   f"{year}-{month}: 周日 04:00 未命中同一窗口")
            expect(find_window(open_dt - timedelta(seconds=1), cfg) is None,
                   f"{year}-{month}: 窗口开始前一秒不应命中")
            expect(find_window(close_dt, cfg) is None, f"{year}-{month}: 窗口关闭时刻不应命中")
            expect(find_window(close_dt - timedelta(seconds=1), cfg) == Window(sat),
                   f"{year}-{month}: 宽限期内应命中窗口")
            # 上周六不应被误判为最后周六（除非真的是最后周六）
            prev_sat = sat - timedelta(days=7)
            if prev_sat.month == month:
                expect(not is_last_saturday(prev_sat) or prev_sat == sat,
                       f"{year}-{month}: 非最后一个周六被误判 {prev_sat}")
            # 相邻窗口不重叠
            nxt_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            nxt_sat = last_saturday(nxt_first.year, nxt_first.month)
            expect(close_dt < datetime.combine(nxt_sat, cfg.precheck_time),
                   f"{year}-{month}: 窗口与下月窗口重叠")

    # 2) 显式跨月/边界用例
    cases = [
        ((2026, 9), date(2026, 9, 26), date(2026, 9, 27)),
        ((2026, 10), date(2026, 10, 31), date(2026, 11, 1)),   # 跨月：预检查 10-31，升级 11-01
        ((2026, 11), date(2026, 11, 28), date(2026, 11, 29)),
        ((2027, 1), date(2027, 1, 30), date(2027, 1, 31)),
        ((2027, 5), date(2027, 5, 29), date(2027, 5, 30)),
    ]
    for (year, month), sat, sun in cases:
        expect(last_saturday(year, month) == sat, f"用例 {year}-{month}: 最后周六应为 {sat}")
        expect(sat + timedelta(days=1) == sun, f"用例 {year}-{month}: 升级日应为 {sun}")
        cfg_grace0 = Config(window_grace_hours=0)
        expect(find_window(datetime.combine(sat, cfg.precheck_time), cfg_grace0) == Window(sat),
               f"用例 {year}-{month}: 预检查时刻未命中")
        if sun.month != sat.month:
            expect(find_window(datetime.combine(sun, cfg.upgrade_time), cfg) == Window(sat),
                   f"用例 {year}-{month}: 跨月升级未命中同一窗口")

    # 3) 渲染的 timer 表达式应包含上述候选集合（可选）
    if getattr(args, "dest", None):
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        _rc, written = render_units(args.config, str(dest), force=True)
        timer_path = dest / "fedora-nas-monthly-precheck.timer"
        upgrade_path = dest / "fedora-nas-monthly-upgrade.timer"
        expect(timer_path.exists(), "selftest: 渲染的 precheck timer 不存在")
        expect(upgrade_path.exists(), "selftest: 渲染的 upgrade timer 不存在")
        if timer_path.exists():
            text = timer_path.read_text(encoding="utf-8")
            expect("Sat *-*-22..31" in text, "selftest: precheck timer 未覆盖 22..31 候选")
        if upgrade_path.exists():
            text = upgrade_path.read_text(encoding="utf-8")
            expect("Sun *-*-23..31" in text and "Sun *-*-01..03" in text,
                   "selftest: upgrade timer 未覆盖跨月候选")

    if failures:
        print(f"selftest: FAIL（{len(failures)} 项）")
        for f in failures[:40]:
            print("  ✗ " + f)
        return 1
    print("selftest: PASS（2020–2060 全部月份窗口判定、跨月用例、timer 候选覆盖均通过）")
    return 0


# ---------------------------------------------------------------------------
# 命令：precheck
# ---------------------------------------------------------------------------

def collect_precheck_checks(ctx: Ctx) -> tuple[list[Check], list[tuple[str, str, str]], dict]:
    checks: list[Check] = []
    meta: dict = {}
    cfg = ctx.cfg

    checks.extend(check_disk(cfg))
    checks.extend(check_rpm_dnf(cfg, ctx.logger))

    meta_chk, upgrades = refresh_metadata(cfg, ctx.logger)
    checks.append(meta_chk)
    important = set(cfg.important_packages)
    pending_important = [(n, e) for n, e, _a in upgrades if n in important]
    if upgrades:
        detail = f"共 {len(upgrades)} 个待更新包"
        if pending_important:
            detail += f"，其中重要组件: {', '.join(n for n, _ in pending_important)}"
        checks.append(Check("待更新软件包", "info", detail))
    else:
        checks.append(Check("待更新软件包", "ok", "没有待更新软件包"))

    run_kernel = running_kernel()
    kernels = installed_kernels()
    newest = kernels[0] if kernels else ""
    if newest:
        sev = "ok" if run_kernel == newest else "warning"
        msg = f"运行 {run_kernel}"
        if run_kernel != newest:
            msg += f"，最新已安装 {newest}（等待重启生效）"
        checks.append(Check("当前运行内核", sev, msg))
        kernel_status, kernel_details = kernel_boot_readiness(newest)
        checks.append(Check(
            "最新内核启动就绪", {"ok": "ok", "warning": "warning", "fail": "critical"}[kernel_status],
            "；".join(kernel_details)))
    else:
        checks.append(Check("当前运行内核", "warning", f"无法确定最新已安装内核（运行 {run_kernel}）"))

    nr = run(["dnf5", "needs-restarting", "--json"], timeout=120)
    if nr.ok:
        try:
            data = json.loads(nr.out or "[]")
            reboot = any(isinstance(x, dict) and x.get("reboot_required") for x in data)
            pkgs: list[str] = []
            for x in data:
                if isinstance(x, dict):
                    pkgs.extend(x.get("packages", []) or [])
            detail = "需要重启" if reboot else "当前无需重启"
            if pkgs:
                detail += f"；涉及: {', '.join(pkgs[:6])}"
            checks.append(Check("重启需求(dnf5 needs-restarting)", "info", detail))
        except json.JSONDecodeError:
            checks.append(Check("重启需求", "info", "needs-restarting 输出无法解析（忽略）"))
    else:
        checks.append(Check("重启需求", "info", "dnf5 needs-restarting 不可用（忽略）"))

    if systemd_available():
        overall = run(["systemctl", "is-system-running"], timeout=30).out.strip() or "unknown"
        checks.append(Check("systemd 状态", "ok" if overall == "running" else "warning", overall))
        failed = failed_units()
        if failed:
            crit = [u for u in failed if ctx.svc.tier_of_systemd(u) == "critical"]
            other = [u for u in failed if u not in crit]
            if crit:
                checks.append(Check("失败的 systemd 单元", "warning", "、".join(crit)))
            if other:
                checks.append(Check("失败的 systemd 单元(其他)", "info", "、".join(other)))
        else:
            checks.append(Check("失败的 systemd 单元", "ok", "无"))
    else:
        checks.append(Check("systemd 状态", "info", "未运行 systemd（容器/测试环境）"))

    ports = listening_ports()
    if ports:
        checks.append(Check("监听端口", "info", f"共 {len(ports)} 个：{','.join(str(p) for p in sorted(ports))}"))
    for mount in ctx.svc.mounts["critical"]:
        if os.path.ismount(mount):
            checks.append(Check(f"挂载 {mount}", "ok", "已挂载"))
        else:
            checks.append(Check(f"挂载 {mount}", "critical", "未挂载"))

    meta["run_kernel"] = run_kernel
    meta["newest_kernel"] = newest
    meta["upgrades"] = upgrades
    return checks, upgrades, meta


def mail_last_state(ctx: Ctx, state: dict, subject: str, sent_ok: bool) -> None:
    state["last_mail"] = {
        "at": fmt_dt(now_local()),
        "subject": f"{ctx.cfg.subject_prefix} {subject}",
        "ok": sent_ok,
        "error": ctx.mailer.last_error,
    }
    ctx.store.save(state)


def cmd_precheck(args: argparse.Namespace) -> int:
    ctx = load_ctx(args, "precheck")
    logger, cfg, store = ctx.logger, ctx.cfg, ctx.store
    now = ctx.now
    manual = bool(getattr(args, "manual", False))
    dry_run = ctx.dry_run

    window = find_window(now, cfg)
    if window is None and not manual:
        logger.log(f"当前时间 {fmt_dt(now)} 不属于任何维护窗口（非本月最后一个周六及其周末），本次不执行预检查。")
        return finish(ctx, 0)
    if window is None:
        # 手动模式且不在窗口内：构造一个临时窗口对象，仅用于承载状态（升级时需 --ignore-window）
        window = Window(saturday=now.date())
        logger.log("手动预检查（--manual，窗口校验已跳过；状态窗口 id 使用今天日期）")

    logger.section("月度维护预检查")
    logger.log(f"窗口: {window.describe(cfg) if window else '无（手动）'}")
    logger.log(f"配置: {ctx.paths.conf}；SMTP: {cfg.smtp_summary()}")

    lock = FileLock(ctx.paths.lock_file, logger)
    if not lock.acquire(wait_seconds=cfg.lock_wait_seconds):
        logger.log("未能获得维护锁（另有维护任务在运行），跳过本次预检查。", "WARN")
        return finish(ctx, 0)
    try:
        state = store.load()
        # 归档上个月遗留的未完成窗口（既不阻塞也不丢失，仅整理状态）
        stale = state.get("window")
        if window and stale and stale.get("id") != window.id:
            up_status = (stale.get("upgrade") or {}).get("status")
            if up_status not in ("running", "reboot-requested"):
                logger.log(f"发现上次维护窗口 {stale.get('id')}（升级状态 {up_status}）未归档，"
                           f"先归档再开始本次维护", "WARN")
                store.archive_window(state, logger)
                state = store.load()
        # 中断事务检查
        inprog = store.read_in_progress()
        interrupted = None
        if inprog and not pid_alive(inprog.get("pid")):
            w = state.get("window") or {}
            up = (w.get("upgrade") or {}).get("status")
            if up == "running":
                interrupted = f"上次升级进程（pid {inprog.get('pid')}）异常中断，未完成"
        if interrupted:
            checks = [Check("中断的升级事务", "critical",
                            interrupted + "。已阻止自动升级，请人工确认后执行 clear-interrupted")]
            body = render_report("月度维护预检查：发现中断事务，已阻止自动升级",
                                 checks, log_path=logger.path)
            sent = (not dry_run and not args.no_mail) and ctx.mailer.send(
                "维护预检查异常：上次升级中断，已阻止自动更新（需人工介入）", body)
            if window and not dry_run:
                win = state.get("window") or {}
                win.setdefault("id", window.id)
                win.update({"month": window.month, "saturday": window.id,
                            "sunday": window.sunday.isoformat(),
                            "opened_at": fmt_dt(window.open_dt(cfg)),
                            "closes_at": fmt_dt(window.close_dt(cfg)),
                            "precheck": {"status": "failed", "at": fmt_dt(now),
                                         "reason": interrupted, "log": logger.path}})
                state["window"] = win
                mail_last_state(ctx, state, "维护预检查异常", sent)
            return finish(ctx, 1)

        # 跳过 flag
        if os.path.exists(ctx.paths.skip_flag):
            logger.log(f"检测到跳过标记 {ctx.paths.skip_flag}，本次维护将跳过。", "WARN")
            consumed = False
            try:
                os.unlink(ctx.paths.skip_flag)
                consumed = True
                logger.log("跳过标记已消费并删除（只生效一次）")
            except OSError as exc:
                logger.log(f"删除跳过标记失败: {exc}", "ERROR")
            checks = [Check("手动跳过", "warning",
                            "存在 skip-next-update 标记，本次月度维护整体跳过（标记已删除，只跳过一次）")]
            body = render_report("本次月度维护已跳过", checks, log_path=logger.path)
            sent = False
            if not dry_run and not args.no_mail:
                sent = ctx.mailer.send("本次维护已跳过（手动 skip-next-update）", body)
            if window and not dry_run:
                win = state.get("window") or {}
                win.update({"id": window.id, "month": window.month, "saturday": window.id,
                            "sunday": window.sunday.isoformat(),
                            "opened_at": fmt_dt(window.open_dt(cfg)),
                            "closes_at": fmt_dt(window.close_dt(cfg)),
                            "precheck": {"status": "skipped", "at": fmt_dt(now),
                                         "reason": "skip-next-update", "log": logger.path}})
                state["window"] = win
                mail_last_state(ctx, state, "本次维护已跳过", sent)
            return finish(ctx, 0)

        checks, upgrades, meta = collect_precheck_checks(ctx)
        critical = [c for c in checks if c.severity == "critical"]
        warnings = [c for c in checks if c.severity == "warning"]
        logger.section("预检查结果")
        for c in checks:
            logger.log(f"  {c.line()}")
        if upgrades:
            for name, evr, arch in upgrades[:80]:
                mark = " *" if name in set(cfg.important_packages) else ""
                logger.log(f"    - {name}.{arch} → {evr}{mark}")

        # 主题与状态
        month_label = window.month if window else now.strftime("%Y-%m")
        if critical:
            status = "failed"
            subject = "维护预检查异常，已阻止自动更新（需人工介入）"
            title = "月度维护预检查：发现严重问题，已阻止本月自动更新"
        elif not upgrades:
            status = "no-updates"
            subject = f"本月无需维护：无待更新软件包（{month_label}）"
            title = "月度维护预检查：本月无需维护"
        else:
            status = "ok"
            n_imp = len([1 for n, _e, _a in upgrades if n in set(cfg.important_packages)])
            subject = (f"维护预告：{window.saturday if window else now.date()} 将执行完整更新"
                       f"（{len(upgrades)} 个包，重要组件 {n_imp} 个）")
            title = "月度维护预检查：一切正常，将按计划执行完整更新"

        intro = [
            f"维护窗口: {window.describe(cfg) if window else '手动检查（不授权升级）'}",
            f"待更新软件包: {len(upgrades)} 个" + (
                f"（其中重要组件 {len([1 for n, _e, _a in upgrades if n in set(cfg.important_packages)])} 个）"
                if upgrades else ""),
            f"跳过本次维护: sudo touch {ctx.paths.skip_flag}（生效后自动删除）",
        ]
        important_lines = ["重要组件明细:"]
        important_lines += [f"  * {n}: {e}" for n, e, _a in upgrades if n in set(cfg.important_packages)] or ["  （无）"]
        extra = important_lines
        if upgrades:
            extra += ["", "完整待更新清单请查看日志文件。"]
        body = render_report(title, checks, intro, extra, logger.path)

        sent = False
        if not dry_run and not args.no_mail:
            sent = ctx.mailer.send(subject, body)
        logger.log(f"预检查状态: {status}" + ("（dry-run 未写状态、未发信）" if dry_run else ""))

        if window and not dry_run:
            win = state.get("window") or {}
            win.update({
                "id": window.id, "month": window.month,
                "saturday": window.id, "sunday": window.sunday.isoformat(),
                "opened_at": fmt_dt(window.open_dt(cfg)),
                "closes_at": fmt_dt(window.close_dt(cfg)),
                "precheck": {
                    "status": status, "at": fmt_dt(now),
                    "updates": len(upgrades),
                    "important": [f"{n} {e}" for n, e, _a in upgrades
                                  if n in set(cfg.important_packages)],
                    "warnings": [c.line() for c in warnings],
                    "log": logger.path,
                    "run_kernel": meta.get("run_kernel", ""),
                    "newest_kernel": meta.get("newest_kernel", ""),
                },
            })
            state["window"] = win
            mail_last_state(ctx, state, subject, sent)
            logger.log(f"已写入维护状态: {ctx.paths.state_file}（窗口 {window.id}）")

        return finish(ctx, 1 if critical else 0)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 命令：upgrade
# ---------------------------------------------------------------------------

def precheck_evidence(ctx: Ctx, window: Window) -> bool:
    """判断周六预检查是否可能正在（或刚）执行——用于升级阶段的开机补执行等待。

    只有存在证据时才长时间等待，避免在预检查根本没安排的情况下空等 20 分钟。
    """
    if systemd_available():
        state, _rc = unit_active("fedora-nas-monthly-precheck.service")
        if state in ("activating", "active", "reloading"):
            return True
    # 最近 10 分钟内预检查日志有写入，也算“正在/刚执行”
    try:
        latest = os.path.join(ctx.paths.log_dir, "latest-precheck.log")
        if os.path.exists(latest) and (time.time() - os.path.getmtime(latest)) < 600:
            return True
    except OSError:
        pass
    # 今天就是窗口的预检查日（周六），预检查可能稍后才触发
    return window.saturday == ctx.now.date()


def wait_for_precheck_state(ctx: Ctx, window: Window, timeout: int) -> dict | None:
    deadline = time.monotonic() + timeout
    short_deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = ctx.store.load()
        win = state.get("window") or {}
        if win.get("id") == window.id and (win.get("precheck") or {}).get("status"):
            return win
        if not precheck_evidence(ctx, window) and time.monotonic() >= short_deadline:
            ctx.logger.log("未发现预检查正在执行的证据，提前结束等待", "WARN")
            return None
        time.sleep(15)
    state = ctx.store.load()
    win = state.get("window") or {}
    if win.get("id") == window.id:
        return win
    return None


def dnf_stream(cmd: str, logger: Logger, timeout: int) -> tuple[int, list[str]]:
    """流式执行 dnf，返回 (rc, 关键行缓存)。"""
    args = shlex.split(cmd)
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    logger.log(f"执行: {cmd}（超时 {timeout}s）")
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, errors="replace")
    except OSError as exc:
        logger.log(f"无法启动 dnf: {exc}", "ERROR")
        return 127, [str(exc)]
    captured: list[str] = []
    start = time.monotonic()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        logger.log("  " + line)
        captured.append(line)
        if len(captured) > 4000:
            del captured[:1000]
        if time.monotonic() - start > timeout:
            proc.kill()
            logger.log("dnf 执行超时，已终止", "ERROR")
            return 124, captured
    rc = proc.wait()
    return rc, captured


def error_summary(lines: list[str]) -> str:
    patterns = re.compile(
        r"(error|错误|failed|failure|problem|conflict|冲突|依赖|dependency|no space|空间不足|"
        r"cannot|could not|无法|Failed to|unfinished|transaction)", re.IGNORECASE)
    hits = [ln for ln in lines if patterns.search(ln)]
    if not hits:
        hits = lines[-15:]
    return "\n".join(hits[-25:])


def container_update_hook(ctx: Ctx, logger: Logger) -> list[Check]:
    """可选的容器镜像更新钩子：复用既有 ~/Tools/check_container_updates.sh（默认关闭）。

    只在真实执行时调用（dry-run 不调用）；失败只记为警告，不影响系统更新的结论。
    """
    cfg = ctx.cfg
    script = cfg.container_update_script
    logger.section("容器镜像更新（可选钩子）")
    if not os.path.exists(script):
        return [Check("容器镜像更新", "warning", f"脚本不存在: {script}")]
    res = run(["bash", script], timeout=1800)
    if res.ok:
        return [Check("容器镜像更新", "ok", f"{script} 执行完成")]
    return [Check("容器镜像更新", "warning",
                  f"脚本返回 {res.rc}: {tail_text(res.text(), 5)}")]


def record_hook_result(ctx: Ctx, checks: list[Check]) -> None:
    try:
        state = ctx.store.load()
        state["last_container_update"] = {
            "at": fmt_dt(now_local()),
            "checks": [c.line() for c in checks],
        }
        ctx.store.save(state)
    except Exception as exc:  # noqa: BLE001 - 记录失败绝不影响维护
        ctx.logger.log(f"记录容器更新结果失败（忽略）: {exc}", "WARN")


def cmd_upgrade(args: argparse.Namespace) -> int:
    ctx = load_ctx(args, "upgrade")
    logger, cfg, store = ctx.logger, ctx.cfg, ctx.store
    now = ctx.now
    dry_run = ctx.dry_run

    window = find_window(now, cfg)
    state = store.load()
    win = state.get("window") or {}

    if dry_run:
        logger.section("升级 dry-run（不执行升级、不写状态、不重启）")
        logger.log(f"当前时间: {fmt_dt(now)}；窗口: {window.describe(cfg) if window else '不在维护窗口'}")
        logger.log(f"现有状态窗口: {win.get('id', '无')}，预检查状态: "
                   f"{(win.get('precheck') or {}).get('status', '无')}")
        checks, upgrades, _meta = collect_precheck_checks(ctx)
        for c in checks:
            logger.log("  " + c.line())
        logger.log(f"待更新: {len(upgrades)} 个包")
        res = run(shlex.split(cfg.dnf_upgrade_cmd) + ["--assumeno"], timeout=900)
        logger.log("dnf5 upgrade --assumeno 输出（节选）:")
        for line in tail_text(res.text(), 40).splitlines():
            logger.log("  " + line)
        if window:
            pending = store.load_pending_health()
            logger.log(f"pending-health: {'存在' if pending else '不存在'}；"
                       f"跳过标记: {'存在' if os.path.exists(ctx.paths.skip_flag) else '无'}")
        return finish(ctx, 0)

    # ---- 等待预检查状态（开机补执行场景）----
    if window is not None and (win.get("id") != window.id or not (win.get("precheck") or {}).get("status")):
        logger.log(f"尚未看到窗口 {window.id} 的预检查状态，最多等待 "
                   f"{cfg.precheck_wait_seconds}s（用于开机补执行时与预检查并行触发的情况）…")
        waited = wait_for_precheck_state(ctx, window, cfg.precheck_wait_seconds)
        if waited is not None:
            win = waited
            state = store.load()
            logger.log(f"已等到预检查状态: {(win.get('precheck') or {}).get('status')}")
        else:
            logger.log("等待预检查状态超时", "WARN")

    # ---- 窗口与授权校验 ----
    if window is None:
        if getattr(args, "ignore_window", False) and win.get("id"):
            logger.log("--ignore-window：跳过日期窗口校验（仍要求预检查状态有效）", "WARN")
        else:
            logger.log("当前不属于任何维护窗口，未执行升级（不发送邮件）。")
            return finish(ctx, 0)

    pre = win.get("precheck") or {}
    pre_status = pre.get("status")
    reasons: list[str] = []
    if not win:
        reasons.append("没有找到本次维护窗口的状态记录")
    else:
        if window is not None and win.get("id") != window.id:
            reasons.append(f"状态窗口({win.get('id')})与当前窗口({window.id})不一致")
        if pre_status not in ("ok", "no-updates"):
            reasons.append(f"预检查状态为 {pre_status!r}，未通过")
        else:
            try:
                pre_at = datetime.strptime(pre.get("at", ""), "%Y-%m-%d %H:%M:%S")
                age_h = (now - pre_at).total_seconds() / 3600
                if age_h > cfg.max_precheck_age_hours:
                    reasons.append(f"预检查已过去 {age_h:.1f} 小时（超过 {cfg.max_precheck_age_hours}h）")
            except ValueError:
                reasons.append("预检查时间无法解析")
        if (win.get("upgrade") or {}).get("status") == "success":
            reasons.append("本窗口已成功完成升级")
        if (win.get("upgrade") or {}).get("status") == "failed" and not getattr(args, "retry", False):
            reasons.append("本窗口上次升级失败，需人工确认后使用 --retry")
        if (win.get("upgrade") or {}).get("status") == "running" and not getattr(args, "retry", False):
            reasons.append("本窗口升级被标记为进行中（可能中断），需 clear-interrupted 确认")

    if reasons:
        logger.log("升级被拒绝: " + "；".join(reasons), "ERROR")
        checks = [Check("升级授权校验", "critical", r) for r in reasons]
        body = render_report("本次自动升级被取消", checks,
                             [f"窗口: {window.id if window else win.get('id', '未知')}",
                              f"预检查状态: {pre_status}",
                              "如确认系统健康，可人工运行: sudo python3 ~/Tools/fedora_nas_maintenance.py run --yes"],
                             log_path=logger.path)
        if not args.no_mail:
            ctx.mailer.send("维护已取消：预检查未通过或状态无效，未执行自动更新", body)
        return finish(ctx, 1)

    # ---- 跳过 flag ----
    if os.path.exists(ctx.paths.skip_flag):
        logger.log("检测到 skip-next-update，升级阶段整体跳过。", "WARN")
        try:
            os.unlink(ctx.paths.skip_flag)
            logger.log("跳过标记已消费并删除")
        except OSError as exc:
            logger.log(f"删除跳过标记失败: {exc}", "ERROR")
        win.setdefault("upgrade", {}).update({"status": "skipped", "at": fmt_dt(now),
                                              "reason": "skip-next-update", "log": logger.path})
        state["window"] = win
        body = render_report("本次月度维护已跳过（升级前发现跳过标记）",
                             [Check("手动跳过", "warning", "skip-next-update 已消费")],
                             log_path=logger.path)
        sent = not args.no_mail and ctx.mailer.send("本次维护已跳过（手动 skip-next-update）", body)
        mail_last_state(ctx, state, "本次维护已跳过", sent)
        return finish(ctx, 0)

    # ---- 并发锁 ----
    lock = FileLock(ctx.paths.lock_file, logger)
    if not lock.acquire(wait_seconds=cfg.lock_wait_seconds):
        logger.log("未能获得维护锁，放弃本次升级（不视为失败）。", "WARN")
        return finish(ctx, 0)
    try:
        # 重新读取状态（等待期间可能变化）
        state = store.load()
        win = state.get("window") or win

        logger.section("月度维护正式升级")
        logger.log(f"窗口: {window.describe(cfg) if window else win.get('id')}")
        logger.log(f"预检查: {pre_status} @ {pre.get('at')}")

        # ---- 升级前重新检查（绝不只依赖周六数据）----
        checks: list[Check] = []
        checks.extend(check_disk(cfg))
        checks.extend(check_rpm_dnf(cfg, logger))
        meta_chk, upgrades = refresh_metadata(cfg, logger)
        checks.append(meta_chk)
        blocked = [c for c in checks if c.severity == "critical"]
        if blocked:
            logger.section("重新检查未通过，取消升级")
            for c in checks:
                logger.log("  " + c.line())
            win.setdefault("upgrade", {}).update({
                "status": "failed", "at": fmt_dt(now),
                "reason": "升级前重新检查未通过: " + "；".join(c.detail for c in blocked),
                "log": logger.path})
            state["window"] = win
            body = render_report("月度维护升级前重新检查未通过，已取消升级（未重启）",
                                 checks, log_path=logger.path)
            sent = not args.no_mail and ctx.mailer.send(
                "系统更新失败：升级前检查未通过，已取消（需人工介入）", body)
            mail_last_state(ctx, state, "系统更新失败", sent)
            return finish(ctx, 1)

        if not upgrades:
            logger.log("重新检查后没有待更新软件包。")
            if pre_status == "no-updates":
                win.setdefault("upgrade", {}).update({"status": "noop", "at": fmt_dt(now),
                                                      "reason": "无待更新包", "log": logger.path})
                state["window"] = win
                mail_last_state(ctx, state, "无需更新（未发送重复邮件）", True)
                store.archive_window(state, logger)
                store.save(state)
                return finish(ctx, 0)
            win.setdefault("upgrade", {}).update({"status": "noop", "at": fmt_dt(now),
                                                  "reason": "预检查后无新增更新", "log": logger.path})
            state["window"] = win
            body = render_report("月度维护：重新检查后无待更新包",
                                 checks, log_path=logger.path)
            sent = not args.no_mail and ctx.mailer.send("系统更新成功：实际无需更新", body)
            mail_last_state(ctx, state, "系统更新成功", sent)
            store.archive_window(state, logger)
            store.save(state)
            return finish(ctx, 0)

        # ---- 记录中断标记 + 前置快照 ----
        if not is_root():
            logger.log("正式升级必须以 root 运行（systemd unit 已如此配置）；未执行升级。", "ERROR")
            return finish(ctx, 2)
        store.write_in_progress({"window": win.get("id"), "upgrades": len(upgrades)})
        win.setdefault("upgrade", {}).update({"status": "running", "at": fmt_dt(now),
                                              "updates": len(upgrades), "log": logger.path})
        state["window"] = win
        store.save(state)

        before_snapshot = rpm_snapshot()
        kernel_before = newest_installed_kernel()

        # ---- dnf5 全量升级 ----
        logger.section(f"执行升级: {cfg.dnf_upgrade_cmd}")
        rc, lines = dnf_stream(cfg.dnf_upgrade_cmd, logger, cfg.dnf_timeout_seconds)
        after_snapshot = rpm_snapshot()
        changed = {}
        for name, evr in after_snapshot.items():
            if before_snapshot.get(name) != evr:
                changed[name] = f"{before_snapshot.get(name, '（新增）')} → {evr}"
        removed = [n for n in before_snapshot if n not in after_snapshot]

        if rc != 0:
            summary = error_summary(lines)
            logger.section("升级失败")
            logger.log(f"dnf 返回 {rc}；已取消重启。", "ERROR")
            for ln in summary.splitlines():
                logger.log("  " + ln)
            win.setdefault("upgrade", {}).update({
                "status": "failed", "at": fmt_dt(now), "rc": rc,
                "reason": f"dnf5 upgrade 返回 {rc}", "changed": len(changed),
                "changes": changed, "log": logger.path,
                "error_summary": summary})
            state["window"] = win
            checks.append(Check("dnf5 upgrade", "critical", f"返回码 {rc}"))
            body = render_report(
                "月度维护升级失败：已取消自动重启（需人工介入）", checks,
                intro=[f"窗口: {win.get('id')}",
                       f"待更新: {len(upgrades)} 个包",
                       f"dnf 返回码: {rc}",
                       "",
                       "关键错误摘要:",
                       summary],
                extra=["", "建议: 修复问题后人工执行 sudo dnf5 upgrade --refresh -y；"
                           "如为 rpm/dnf 事务中断，请先检查 rpm 状态。"],
                log_path=logger.path)
            sent = not args.no_mail and ctx.mailer.send(
                "系统更新失败，已取消重启（需人工介入）", body)
            mail_last_state(ctx, state, "系统更新失败", sent)
            return finish(ctx, 1)

        logger.section("升级完成")
        logger.log(f"dnf 返回 0；发生变化的软件包 {len(changed)} 个，移除 {len(removed)} 个")

        # ---- 重启判定 ----
        reboot_reasons: list[str] = []
        nr = run(["dnf5", "needs-restarting", "--json"], timeout=180)
        if nr.ok:
            try:
                data = json.loads(nr.out or "[]")
                if any(isinstance(x, dict) and x.get("reboot_required") for x in data):
                    reboot_reasons.append("dnf5 needs-restarting 判定需要重启")
            except json.JSONDecodeError:
                logger.log("needs-restarting 输出无法解析（忽略）", "WARN")
        else:
            logger.log(f"needs-restarting 不可用（忽略）: {tail_text(nr.text(), 3)}", "WARN")

        kernel_after = newest_installed_kernel()
        kernel_changed = kernel_after and kernel_after != kernel_before
        if kernel_changed:
            reboot_reasons.append(f"新内核已安装: {kernel_after}（当前运行 {running_kernel()}）")
        for pkg in REBOOT_PACKAGES:
            if pkg in changed:
                reboot_reasons.append(f"核心组件已更新: {pkg} {changed[pkg]}")
        if Path("/.autorelabel").exists():
            reboot_reasons.append("检测到 /.autorelabel：重启后将重新标记 SELinux 文件，首次启动会明显变慢")
        # 去重
        seen_r: set[str] = set()
        reboot_reasons = [r for r in reboot_reasons if not (r in seen_r or seen_r.add(r))]
        reboot_required = bool(reboot_reasons)

        # 内核完整性门槛（升级了内核才检查）
        kernel_check = Check("新内核启动就绪", "info", "本次未更新内核")
        kernel_block = False
        if kernel_changed:
            status, details = kernel_boot_readiness(kernel_after)
            if status == "fail":
                kernel_block = True
                kernel_check = Check("新内核启动就绪", "critical", "；".join(details))
            elif status == "warning":
                kernel_check = Check("新内核启动就绪", "warning", "；".join(details))
            else:
                kernel_check = Check("新内核启动就绪", "ok", "；".join(details))
        checks.append(kernel_check)

        important_changed = {k: v for k, v in changed.items() if k in set(cfg.important_packages)}
        logger.log(f"重启判定: {'需要（' + '；'.join(reboot_reasons) + '）' if reboot_required else '不需要'}")

        win.setdefault("upgrade", {}).update({
            "status": "success", "at": fmt_dt(now), "rc": 0,
            "packages": len(changed), "changes": dict(list(changed.items())[:200]),
            "removed": removed, "reboot_required": reboot_required,
            "reboot_reasons": reboot_reasons,
            "kernel_before": kernel_before, "kernel_after": kernel_after,
            "log": logger.path,
        })

        # 记录 dnf 期间的容器状态（用于重启后对比）
        pre_containers, _notes, _failed_scopes = containers_snapshot(ctx.svc.rootless_user)
        win["containers_before"] = {k: {"state": v["state"], "status": v["status"]}
                                    for k, v in pre_containers.items()}

        extra_lines = ["变化的重要组件:"]
        extra_lines += [f"  * {k}: {v}" for k, v in important_changed.items()] or ["  （无）"]
        if cfg.run_container_update:
            extra_lines += ["", "容器镜像更新：已启用（升级后执行）"]

        if not reboot_required:
            win.setdefault("upgrade", {}).update({"status": "success", "reboot_at": None})
            state["window"] = win
            if cfg.run_container_update:
                hook_checks = container_update_hook(ctx, logger)
                checks.extend(hook_checks)
                record_hook_result(ctx, hook_checks)
            extra_lines += ["", "结论: 无需重启，本月维护完成。"]
            body = render_report("月度维护完成：系统更新成功（无需重启）", checks,
                                 intro=[f"窗口: {win.get('id')}",
                                        f"更新软件包: {len(changed)} 个",
                                        f"当前内核: {running_kernel()}"],
                                 extra=extra_lines, log_path=logger.path)
            sent = not args.no_mail and ctx.mailer.send(
                f"系统更新成功（无需重启，共 {len(changed)} 个包）", body)
            mail_last_state(ctx, state, "系统更新成功", sent)
            store.archive_window(state, logger)
            store.save(state)
            store.clear_in_progress()
            return finish(ctx, 0)

        if kernel_block:
            reason = "新内核安装不完整，已取消自动重启"
            logger.log(reason, "ERROR")
            win.setdefault("upgrade", {}).update({"status": "failed", "at": fmt_dt(now),
                                                  "reason": reason, "no_reboot": True})
            state["window"] = win
            body = render_report("月度维护异常：新内核安装不完整，已取消重启（需人工介入）",
                                 checks,
                                 intro=[f"窗口: {win.get('id')}",
                                        f"新内核: {kernel_after}",
                                        "已取消自动重启，系统当前仍运行旧内核，功能不受影响。"],
                                 extra=["", "建议: 检查 /boot 空间与 /boot/loader/entries，"
                                           "完整安装后人工重启验证。"],
                                 log_path=logger.path)
            sent = not args.no_mail and ctx.mailer.send(
                "系统更新异常：新内核安装不完整，已取消重启（需人工介入）", body)
            mail_last_state(ctx, state, "系统更新失败", sent)
            return finish(ctx, 1)

        # ---- 即将重启 ----
        store.write_pending_health({
            "window_id": win.get("id"),
            "reboot_requested_at": fmt_dt(now),
            "boot_id_before": boot_id(),
            "running_kernel_before": running_kernel(),
            "kernel_after": kernel_after,
            "upgrade_log": logger.path,
            "containers_before": win.get("containers_before", {}),
            "packages": len(changed),
        })
        win.setdefault("upgrade", {}).update({"status": "reboot-requested",
                                              "reboot_at": fmt_dt(now)})
        state["window"] = win
        store.save(state)

        extra_lines += ["", "重启原因:", *[f"  - {r}" for r in reboot_reasons],
                        "", "重启后将自动执行健康检查并再次发送结果邮件。"]
        body = render_report("月度维护：更新完成，即将重启", checks,
                             intro=[f"窗口: {win.get('id')}",
                                    f"更新软件包: {len(changed)} 个",
                                    f"当前内核: {running_kernel()}",
                                    f"新内核: {kernel_after or '未变化'}"],
                             extra=extra_lines, log_path=logger.path)
        sent = not args.no_mail and ctx.mailer.send(
            f"系统更新完成，即将重启（新内核 {kernel_after or running_kernel()}）", body)
        mail_last_state(ctx, state, "系统更新完成，即将重启", sent)

        # 记录本次维护结果（在重启前落盘）
        logger.log("准备重启: systemctl reboot")
        res = run(["systemctl", "reboot", "--no-block"], timeout=60)
        if not res.ok:
            logger.log(f"重启命令失败: {tail_text(res.text(), 3)}", "ERROR")
            body = render_report("月度维护：更新成功但重启命令失败（需人工重启）",
                                 checks + [Check("systemctl reboot", "critical",
                                                 tail_text(res.text(), 3))],
                                 log_path=logger.path)
            ctx.mailer.send("系统更新成功，但自动重启失败（请人工重启）", body)
            return finish(ctx, 1)
        return finish(ctx, 0)
    finally:
        lock.release()
        # 未进入重启流程时清理「升级进行中」标记；若状态为 reboot-requested，
        # 说明正在（或即将）重启，标记留给重启后的健康检查清理。
        cur = (store.load().get("window") or {}).get("upgrade") or {}
        if cur.get("status") != "reboot-requested":
            store.clear_in_progress()


# ---------------------------------------------------------------------------
# 命令：health
# ---------------------------------------------------------------------------

def cmd_health(args: argparse.Namespace) -> int:
    ctx = load_ctx(args, "health")
    logger, cfg, store = ctx.logger, ctx.cfg, ctx.store
    now = ctx.now
    dry_run = ctx.dry_run
    force = bool(getattr(args, "force", False))

    pending = store.load_pending_health()
    if pending is None and not force:
        logger.log("没有待执行的重启后健康检查（pending-health.json 不存在），无事可做。")
        return finish(ctx, 0)
    if pending is not None and not force:
        same_boot = pending.get("boot_id_before") == boot_id()
        if same_boot:
            logger.log("pending-health 记录的是同一个 boot_id，说明尚未真正重启；"
                       "保留标记，等待下次开机后执行。", "WARN")
            return finish(ctx, 0)

    logger.section("重启后健康检查")
    if pending:
        logger.log(f"窗口 {pending.get('window_id')}；重启请求时间 {pending.get('reboot_requested_at')}；"
                   f"重启前内核 {pending.get('running_kernel_before')}；预期最新内核 "
                   f"{pending.get('kernel_after') or '未变化'}；当前内核 {running_kernel()}")
    else:
        logger.log("手动健康检查（--force，无 pending-health 记录）")

    lock = FileLock(ctx.paths.lock_file, logger)
    if not lock.acquire(wait_seconds=120):
        logger.log("未能获得维护锁，跳过本次健康检查。", "WARN")
        return finish(ctx, 0)
    try:
        checks = check_services_health(ctx.svc, cfg, logger, pending)
        for c in checks:
            logger.log("  " + c.line())
        critical = [c for c in checks if c.severity == "critical"]
        warnings = [c for c in checks if c.severity == "warning"]

        if critical:
            subject = f"重启后服务异常（{len(critical)} 项严重问题，需检查）"
            title = "重启后健康检查：发现严重问题"
            rc = 1
        else:
            subject = (f"重启成功，健康检查通过（内核 {running_kernel()}"
                       + (f"，{len(warnings)} 项提醒" if warnings else "") + "）")
            title = "重启后健康检查：一切正常"
            rc = 0

        intro = [
            f"运行内核: {running_kernel()}",
            f"运行时长: {uptime_seconds() / 60:.1f} 分钟",
            f"关键问题: {len(critical)} 项；警告: {len(warnings)} 项",
            "（critical = 需要人工介入；optional/按需服务问题只列为警告或信息）",
        ]
        if pending and pending.get("packages"):
            intro.append(f"本次更新涉及 {pending.get('packages')} 个软件包（窗口 {pending.get('window_id')}）")
        body = render_report(title, checks, intro, log_path=logger.path)

        sent = False
        if not dry_run and not args.no_mail:
            sent = ctx.mailer.send(subject, body)

        if not dry_run:
            state = store.load()
            win = state.get("window") or {}
            if pending and win.get("id") == pending.get("window_id"):
                win.setdefault("health", {}).update({
                    "status": "abnormal" if critical else "healthy",
                    "at": fmt_dt(now),
                    "critical": [c.line() for c in critical],
                    "warnings": [c.line() for c in warnings],
                    "log": logger.path,
                })
                state["window"] = win
                mail_last_state(ctx, state, subject, sent)
                if not critical:
                    store.archive_window(state, logger)
                    state["window"] = None
                    store.save(state)
            if pending:
                store.clear_pending_health()
                store.clear_in_progress()
            if cfg.run_container_update:
                hook_checks = container_update_hook(ctx, logger)
                for c in hook_checks:
                    logger.log("  " + c.line())
                record_hook_result(ctx, hook_checks)
        logger.log(f"健康检查结论: {'异常' if critical else '正常'}"
                   + ("（dry-run 未写状态、未发信）" if dry_run else ""))
        return finish(ctx, rc)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 命令：run（手动完整流程）/ status / skip / mail-test / clear-interrupted
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        eprint("手动完整维护可能执行真实升级并重启系统。确认请加 --yes，"
               "或先用 --dry-run 演练。")
        return 2
    ctx = load_ctx(args, "manual-run")
    logger = ctx.logger
    logger.section("手动完整维护流程")
    logger.log(f"dry-run={ctx.dry_run}；--now={ctx.args.now}")
    # 1) 预检查（手动模式：不在窗口也执行；写状态以授权升级）
    rc = cmd_precheck(argparse.Namespace(
        config=args.config, services=args.services, skip_flag=args.skip_flag,
        state_dir=args.state_dir, log_dir=args.log_dir, lock_file=args.lock_file,
        now=args.now, manual=True, dry_run=args.dry_run, no_mail=args.no_mail))
    logger.log(f"预检查阶段返回 {rc}")
    if rc != 0 and not ctx.dry_run:
        logger.log("预检查未通过，手动流程终止（不会升级）。", "ERROR")
        return finish(ctx, rc)
    # 2) 升级（--ignore-window 允许窗口外手动执行；仍要求预检查状态有效）
    rc2 = cmd_upgrade(argparse.Namespace(
        config=args.config, services=args.services, skip_flag=args.skip_flag,
        state_dir=args.state_dir, log_dir=args.log_dir, lock_file=args.lock_file,
        now=args.now, dry_run=args.dry_run, no_mail=args.no_mail,
        ignore_window=True, retry=True))
    return finish(ctx, rc2)


def cmd_status(args: argparse.Namespace) -> int:
    cfg, warnings = load_config(args.config)
    paths = Paths(conf=args.config, services=args.services, skip_flag=args.skip_flag,
                  state_dir=args.state_dir, log_dir=args.log_dir, lock_file=args.lock_file)
    store = StateStore(paths)
    data = store.load()
    now = args.now or now_local()
    print(f"=== {PROG} 状态 ===")
    print(f"主机: {HOSTNAME}    时间: {fmt_dt(now)}")
    print(f"配置: {paths.conf}")
    print(f"SMTP: {cfg.smtp_summary()}")
    if warnings:
        print("配置提示: " + "；".join(warnings))
    print()
    win = data.get("window")
    if win:
        pre = win.get("precheck") or {}
        up = win.get("upgrade") or {}
        he = win.get("health") or {}
        print(f"当前维护窗口: {win.get('id')}（{win.get('month')}）")
        print(f"  预检查: {pre.get('status')} @ {pre.get('at')}  待更新 {pre.get('updates', '-')} 个")
        print(f"  升级:   {up.get('status')} @ {up.get('at')}  重启需要: {up.get('reboot_required', '-')}")
        if up.get("kernel_after"):
            print(f"  内核:   {up.get('kernel_before')} → {up.get('kernel_after')}")
        print(f"  健康:   {he.get('status')} @ {he.get('at')}")
    else:
        print("当前维护窗口: 无（上次维护已归档或尚未开始）")
    hist = data.get("history") or []
    if hist:
        last = hist[-1]
        print(f"上次完成: {last.get('id')}  升级 {((last.get('upgrade') or {}).get('status'))}  "
              f"健康 {((last.get('health') or {}).get('status'))}")
    print()
    print(f"跳过标记: {'存在（下次维护将跳过并自动删除）' if os.path.exists(paths.skip_flag) else '无'}")
    pend = store.load_pending_health()
    print(f"待健康检查: {'有（等待重启后执行）' if pend else '无'}")
    inprog = store.read_in_progress()
    if inprog:
        alive = pid_alive(inprog.get("pid"))
        print(f"升级中标记: pid={inprog.get('pid')} alive={alive} 开始于 {inprog.get('started_at')}")
    print()
    print("接下来 3 个维护窗口:")
    for sat in _next_saturdays(now, 3):
        w = window_for_saturday(sat)
        print(f"  * {w.describe(cfg)}")
    logs = sorted(glob.glob(os.path.join(paths.log_dir, "*.log")))
    if logs:
        print()
        print(f"最近日志（{paths.log_dir}）:")
        for p in logs[-5:]:
            print(f"  - {p}")
    return 0


def _next_saturdays(now: datetime, count: int) -> list[date]:
    result: list[date] = []
    year, month = now.year, now.month
    for _ in range(24):
        sat = last_saturday(year, month)
        if sat >= now.date():
            result.append(sat)
        if len(result) >= count:
            break
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return result


def cmd_skip(args: argparse.Namespace) -> int:
    path = args.skip_flag
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(f"created {fmt_dt(now_local())}\n", encoding="utf-8")
    print(f"[ok] 已创建 {path}；下一次月度维护（预检查或升级阶段）将跳过并自动删除该标记。")
    return 0


def cmd_unskip(args: argparse.Namespace) -> int:
    try:
        os.unlink(args.skip_flag)
        print(f"[ok] 已删除 {args.skip_flag}")
    except FileNotFoundError:
        print(f"[info] {args.skip_flag} 不存在")
    except OSError as exc:
        eprint(f"[error] 删除失败: {exc}")
        return 1
    return 0


def cmd_mail_test(args: argparse.Namespace) -> int:
    cfg, warnings = load_config(args.config)
    for w in warnings:
        eprint(f"[warn] {w}")
    logger = Logger(args.log_dir, "mail-test")
    mailer = Mailer(cfg, logger)
    body = render_report(
        "邮件配置测试",
        [Check("SMTP 配置", "ok" if cfg.mail_ready else "critical", cfg.smtp_summary()),
         Check("测试邮件", "info", "如果你收到这封邮件，说明 SMTP 通道正常")],
        intro=[f"主机: {HOSTNAME}", f"配置: {args.config}"])
    ok = mailer.send(f"邮件测试（{fmt_dt(now_local())}）", body)
    print("发送成功" if ok else f"发送失败: {mailer.last_error}")
    logger.close()
    return 0 if ok else 1


def cmd_clear_interrupted(args: argparse.Namespace) -> int:
    paths = Paths(conf=args.config, services=args.services, skip_flag=args.skip_flag,
                  state_dir=args.state_dir, log_dir=args.log_dir, lock_file=args.lock_file)
    logger = Logger(args.log_dir, "clear-interrupted")
    store = StateStore(paths, logger)
    state = store.load()
    win = state.get("window") or {}
    up = win.get("upgrade") or {}
    if not win:
        print("[info] 没有进行中的维护窗口，无需清理。")
    else:
        up.update({"status": "failed",
                   "reason": f"人工于 {fmt_dt(now_local())} 确认中断并清理",
                   "cleared_at": fmt_dt(now_local())})
        win["upgrade"] = up
        state["window"] = win
        store.save(state)
        print(f"[ok] 窗口 {win.get('id')} 的升级状态已标记为 failed（可用 upgrade --retry 重试）")
    store.clear_in_progress()
    print("[ok] 已清除 upgrade-in-progress 标记")
    logger.close()
    return 0


def cmd_bootinfo(args: argparse.Namespace) -> int:
    logger = Logger(args.log_dir, "bootinfo")
    text = bootinfo_report(logger)
    print(text)
    if logger.path:
        for line in text.splitlines():
            logger.log(line)
    logger.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Fedora-NAS 月度自动维护（预检查 / 完整升级 / 重启后健康检查）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=os.environ.get("FEDORA_NAS_UPDATE_CONF", DEFAULT_CONF),
                        help=f"配置文件（默认 {DEFAULT_CONF}）")
    common.add_argument("--services", default=os.environ.get("FEDORA_NAS_SERVICES_CONF", DEFAULT_SERVICES),
                        help=f"健康检查名单（默认 {DEFAULT_SERVICES}）")
    common.add_argument("--skip-flag", default=DEFAULT_SKIP_FLAG, help="跳过标记路径")
    common.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="状态目录")
    common.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="日志目录")
    common.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="互斥锁文件")
    common.add_argument("--now", type=parse_dt_override, default=None,
                        help="覆盖当前时间（用于测试，如 2026-10-31T20:00）")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("precheck", parents=[common], help="预检查")
    sp.add_argument("--dry-run", action="store_true", help="只检查：不写状态、不发邮件")
    sp.add_argument("--no-mail", action="store_true", help="不发邮件")
    sp.add_argument("--manual", action="store_true", help="手动模式：忽略日期窗口（但仍写状态授权升级）")
    sp.set_defaults(func=cmd_precheck)

    sp = sub.add_parser("upgrade", parents=[common], help="正式升级")
    sp.add_argument("--dry-run", action="store_true", help="只演练：dnf --assumeno，不升级/不写状态/不重启")
    sp.add_argument("--no-mail", action="store_true", help="不发邮件")
    sp.add_argument("--ignore-window", action="store_true", help="跳过日期窗口校验（仍要求有效预检查）")
    sp.add_argument("--retry", action="store_true", help="允许重试本窗口内失败的升级")
    sp.set_defaults(func=cmd_upgrade)

    sp = sub.add_parser("health", parents=[common], help="重启后健康检查")
    sp.add_argument("--dry-run", action="store_true", help="只检查：不写状态、不发邮件")
    sp.add_argument("--no-mail", action="store_true", help="不发邮件")
    sp.add_argument("--force", action="store_true", help="无 pending-health 也执行（测试用）")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("run", parents=[common], help="手动完整维护流程（可能重启，需 --yes）")
    sp.add_argument("--dry-run", action="store_true", help="演练：预检查 + dnf --assumeno，不升级/不重启")
    sp.add_argument("--no-mail", action="store_true", help="不发邮件")
    sp.add_argument("--yes", action="store_true", help="确认执行真实升级（非 dry-run 时必填）")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("mail-test", parents=[common], help="测试发邮件")
    sp.set_defaults(func=cmd_mail_test)

    sp = sub.add_parser("status", parents=[common], help="查看状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("skip", parents=[common], help="跳过下一次维护")
    sp.set_defaults(func=cmd_skip)
    sp = sub.add_parser("unskip", parents=[common], help="取消跳过")
    sp.set_defaults(func=cmd_unskip)

    sp = sub.add_parser("clear-interrupted", parents=[common], help="清理被中断的升级标记")
    sp.set_defaults(func=cmd_clear_interrupted)

    sp = sub.add_parser("bootinfo", parents=[common], help="内核/启动项诊断")
    sp.set_defaults(func=cmd_bootinfo)

    sp = sub.add_parser("init-config", help="生成 /etc/fedora-nas/update.conf（可一次性导入 Scrutiny SMTP）")
    sp.add_argument("--template",
                    default=os.environ.get(
                        "FEDORA_NAS_UPDATE_TEMPLATE",
                        str(Path(__file__).resolve().parent.parent
                            / "Configs/Fedora/config/fedora-nas/update.conf.example")),
                    help="配置模板文件（默认仓库内 update.conf.example）")
    sp.add_argument("--dest", default=DEFAULT_CONF)
    sp.add_argument("--scrutiny", default="/home/ywpc/Podman/Scrutiny/config/scrutiny.yaml")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init_config)

    sp = sub.add_parser("render-units", help="按配置渲染 systemd unit")
    sp.add_argument("--config", default=DEFAULT_CONF)
    sp.add_argument("--dest", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_render_units)

    sp = sub.add_parser("selftest", help="日期窗口逻辑自检")
    sp.add_argument("--config", default=DEFAULT_CONF)
    sp.add_argument("--dest", default=None, help="可选：渲染 unit 到该目录以检查 timer 表达式")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        eprint(f"[error] {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("[warn] 被中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
