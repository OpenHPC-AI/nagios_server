#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         NAGIOS HOST CONFIGURATION UTILITY  —  CDAC HPC Edition  v3.0       ║
║                    Production-Grade Orchestrator                            ║
╚══════════════════════════════════════════════════════════════════════════════╝

What's new in v3.0 (over v2.0)
  ① File ownership + permission enforcement  (nagios:nagios, 0744/0755)
  ② Dynamic service profiles loaded from external .txt files
  ③ Per-hostgroup service profile mapping  (many-to-many)
  ④ ServiceConfig parser  — duplicate detection, safe merge for append mode
  ⑤ Service profile validation  — syntax, delimiter, duplicates, bad chars
  ⑥ Service repository directory  (--service-dir, auto-create, auto-scan)
  ⑦ Append-mode service merge  — never clobbers manually created services
  ⑧ Automatic rollback to timestamped backup on any fatal failure
  ⑨ New CLI flags: --service-dir  --profile  --skip-permissions
                   --skip-service-validation
  ⑩ O(1) duplicate lookups via sets/dicts throughout (10 000+ node safe)
  ⑪ Structured audit log with per-section counters
  ⑫ Full dry-run support for every new code path

Inherited from v2.0
  • Fresh / Append modes       • IPManager (octet-rollover, /16–/32)
  • NagiosConfig parser        • AtomicWriter  (temp + rename)
  • Timestamped backups        • Nagios -v validation
  • Duplicate host detection   • Per-node dynamic zero-padding
  • Colour terminal UI         • Progress indicators

Usage
  python3 hosts_cfg_add.py [options]

  --dry-run                    Preview only — no files written
  --conf-dir DIR               Nagios conf.d  (default /etc/nagios/conf.d)
  --nagios-cfg FILE            Main nagios.cfg for -v validation
  --service-dir DIR            Service profile repo (default /etc/nagios/service_profiles)
  --profile FILE               Pre-select a profile file (repeatable)
  --skip-validation            Skip nagios -v
  --skip-permissions           Skip chown/chmod enforcement
  --skip-service-validation    Skip profile syntax checks
"""

from __future__ import annotations

import argparse
import grp
import logging
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import readline  # noqa: F401 — activates line-editing (backspace, arrows)
import tempfile
from datetime import datetime
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# §1  ANSI COLOUR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class C:
    HEADER = "\033[95m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    END    = "\033[0m"

    @staticmethod
    def supports_color() -> bool:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    @classmethod
    def wrap(cls, text: str, *codes: str) -> str:
        if not cls.supports_color():
            return text
        return "".join(codes) + text + cls.END

    @classmethod
    def prompt_wrap(cls, text: str, *codes: str) -> str:
        """
        readline-safe colouring for prompt strings.
        Wraps every ANSI escape in \\001...\\002 so readline counts only
        the *visible* characters when computing cursor position.
        Without this, backspace / Home / End / arrow keys mis-position
        the cursor because readline thinks the prompt is wider than it is.
        """
        if not cls.supports_color():
            return text
        coded = "\001" + "".join(codes) + "\002"
        reset = "\001" + cls.END + "\002"
        return coded + text + reset


# ══════════════════════════════════════════════════════════════════════════════
# §2  STRUCTURED AUDIT LOGGING
# ══════════════════════════════════════════════════════════════════════════════

LOG_FILE = "/var/log/nagios_cfg_utility.log"

def _setup_logging() -> tuple[logging.Logger, str]:
    logger = logging.getLogger("nagios_cfg")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log_path = LOG_FILE
    try:
        fh = logging.FileHandler(log_path)
    except PermissionError:
        log_path = "/tmp/nagios_cfg_utility.log"
        fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.info("─" * 72)
    logger.info("Session started — PID %s  v3.0", os.getpid())
    return logger, log_path


logger, _log_path = _setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
# §3  UI / TERMINAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def info(msg: str) -> None:
    print(C.wrap("  [INFO]  ", C.GREEN, C.BOLD) + msg)
    logger.info(msg)

def warn(msg: str) -> None:
    print(C.wrap("  [WARN]  ", C.YELLOW, C.BOLD) + msg)
    logger.warning(msg)

def error(msg: str, fatal: bool = True) -> None:
    print(C.wrap("  [ERROR] ", C.RED, C.BOLD) + msg)
    logger.error(msg)
    if fatal:
        raise SystemExit(1)

def step(title: str) -> None:
    print()
    print(C.wrap(f"  ▶  {title}", C.CYAN, C.BOLD))
    print(C.wrap("  " + "─" * 60, C.DIM))
    logger.info("STEP: %s", title)

def prompt(msg: str, default: str = "") -> str:
    display = f"{msg} [{default}]: " if default else f"{msg}: "
    try:
        # Use prompt_wrap so readline measures only visible characters;
        # this is what makes backspace, Home/End, and arrows work correctly.
        val = input(C.prompt_wrap("  → ", C.BLUE) + display).strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        error("Input interrupted.")
        raise  # unreachable

def confirm(msg: str) -> bool:
    ans = prompt(f"{msg} [y/N]", "n").lower()
    return ans in ("y", "yes")

def banner() -> None:
    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        "║     NAGIOS HOST CONFIGURATION UTILITY  —  v3.0                  ║",
        "║     CDAC HPC Edition  |  Production-Grade Orchestrator           ║",
        "╚══════════════════════════════════════════════════════════════════╝",
    ]
    print()
    for line in lines:
        print(C.wrap(f"  {line}", C.CYAN, C.BOLD))
    print(C.wrap(f"  Audit log → {_log_path}", C.DIM))
    print()


# ══════════════════════════════════════════════════════════════════════════════
# §4  IP MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class IPManager:
    """
    Sequential IPv4 allocator that respects /prefix subnet boundaries.
    Only the very first (network) and very last (broadcast) address of each
    block are reserved. For prefixes < /24, interior addresses with a 4th
    octet of 0 or 255 are valid and are NOT skipped.
    """

    def __init__(self, start_ip: str, subnet_prefix: int) -> None:
        parts = start_ip.split(".")
        if len(parts) != 4:
            raise ValueError(f"Invalid IP: {start_ip}")
        self.octets = [int(p) for p in parts]
        for i, o in enumerate(self.octets):
            if not 0 <= o <= 255:
                raise ValueError(f"Octet {i + 1} out of range: {o}")
        self.prefix     = subnet_prefix
        self.block_size = 2 ** (32 - subnet_prefix)
        if self._reserved():
            raise ValueError(f"{start_ip} is a subnet network/broadcast address.")

    def _as_int(self) -> int:
        o = self.octets
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _load(self, n: int) -> None:
        self.octets = [(n >> s) & 0xFF for s in (24, 16, 8, 0)]

    def _reserved(self) -> bool:
        hp = self._as_int() & (self.block_size - 1)
        return hp == 0 or hp == self.block_size - 1

    def current(self) -> str:
        return ".".join(str(o) for o in self.octets)

    def advance(self) -> None:
        n    = self._as_int() + 1
        mask = self.block_size - 1
        while True:
            if n > 0xFFFFFFFF:
                raise OverflowError("IPv4 address space exhausted.")
            if (n & mask) not in (0, mask):
                break
            n += 1
        self._load(n)

    def usable_per_block(self) -> int:
        return self.block_size - 2


# ══════════════════════════════════════════════════════════════════════════════
# §5  NAGIOS HOST CONFIG PARSER
# ══════════════════════════════════════════════════════════════════════════════

class NagiosConfig:
    """
    Parse hosts.cfg for templates, hostgroups, and hosts.
    All existence checks are O(1) via internal sets/dicts.
    """

    _BLOCK_RE = re.compile(r"define\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
    _FIELD_RE = re.compile(r"^\s*(\S+)\s+(.+)$", re.MULTILINE)

    def __init__(self, path: str) -> None:
        self.path        = path
        self.templates:  dict[str, dict] = {}
        self.hostgroups: dict[str, dict] = {}
        self.hosts:      dict[str, dict] = {}
        self._host_set:  set[str]        = set()
        self._parse()

    def _parse(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            text = fh.read()
        for m in self._BLOCK_RE.finditer(text):
            kind   = m.group(1).lower()
            fields = {k: v.strip() for k, v in self._FIELD_RE.findall(m.group(2))}
            if kind == "host":
                name = fields.get("name") or fields.get("host_name", "")
                if fields.get("register") == "0":
                    self.templates[name] = fields
                elif "host_name" in fields:
                    hn = fields["host_name"]
                    self.hosts[hn] = fields
                    self._host_set.add(hn)
            elif kind == "hostgroup":
                hg  = fields.get("hostgroup_name", "")
                raw = fields.get("members", "")
                fields["members"] = [x.strip() for x in raw.split(",") if x.strip()]
                self.hostgroups[hg] = fields

    def template_exists(self, name: str) -> bool:
        return name in self.templates

    def hostgroup_exists(self, name: str) -> bool:
        return name in self.hostgroups

    def host_exists(self, name: str) -> bool:
        return name in self._host_set   # O(1)

    def hostgroup_members(self, name: str) -> list[str]:
        return self.hostgroups.get(name, {}).get("members", [])


# ══════════════════════════════════════════════════════════════════════════════
# §6  SERVICE PROFILE  (one parsed .txt file)
# ══════════════════════════════════════════════════════════════════════════════

class ServiceProfile:
    """
    Represents one service-check profile file.

    File format — one entry per line (blank lines and # comments ignored):
        Service Description|check_command
    Example:
        GPU Utilization|check_nrpe!check_gpu_util
    """

    _BAD_CHARS_RE = re.compile(r"[{};#\\]")
    _CMD_RE       = re.compile(r"^[\w!_\-\.]+$")

    def __init__(self, path: str) -> None:
        self.path   = path
        self.name   = os.path.basename(path)
        self.checks: list[tuple[str, str]] = []   # [(description, command)]
        self.errors: list[str]             = []

    def validate(self, skip_validation: bool = False) -> bool:
        """
        Parse and validate the file. Returns True on success.
        Populates self.checks. On failure populates self.errors.
        """
        if not os.path.exists(self.path):
            self.errors.append(f"File not found: {self.path}")
            return False
        seen_descs: set[str] = set()
        try:
            with open(self.path) as fh:
                raw_lines = fh.readlines()
        except OSError as exc:
            self.errors.append(f"Cannot read {self.path}: {exc}")
            return False

        for line_no, raw in enumerate(raw_lines, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            if "|" not in line:
                self.errors.append(
                    f"Line {line_no}: missing '|' delimiter: {line!r}"
                )
                if not skip_validation:
                    continue
            desc, _, cmd = line.partition("|")
            desc = desc.strip()
            cmd  = cmd.strip()

            if not skip_validation:
                if not desc:
                    self.errors.append(f"Line {line_no}: empty description.")
                    continue
                if not cmd:
                    self.errors.append(f"Line {line_no}: empty check command.")
                    continue
                if self._BAD_CHARS_RE.search(desc):
                    self.errors.append(
                        f"Line {line_no}: invalid chars in description {desc!r}."
                    )
                    continue
                if not self._CMD_RE.match(cmd):
                    self.errors.append(
                        f"Line {line_no}: invalid check command {cmd!r}."
                    )
                    continue
                if desc in seen_descs:
                    self.errors.append(
                        f"Line {line_no}: duplicate description {desc!r}."
                    )
                    continue
            seen_descs.add(desc)
            self.checks.append((desc, cmd))

        return len(self.errors) == 0 or skip_validation


# ══════════════════════════════════════════════════════════════════════════════
# §7  SERVICE CONFIG PARSER  (services.cfg)
# ══════════════════════════════════════════════════════════════════════════════

class ServiceConfig:
    """
    Parse services.cfg to detect duplicate service definitions.
    Key: (hostgroup_name, service_description) — O(1) lookup.
    """

    _BLOCK_RE = re.compile(r"define\s+service\s*\{([^}]*)\}", re.DOTALL)
    _FIELD_RE = re.compile(r"^\s*(\S+)\s+(.+)$", re.MULTILINE)

    def __init__(self, path: str) -> None:
        self.path = path
        self._services:  dict[tuple[str, str], str] = {}
        self._hostgroups: set[str]                  = set()
        self._parse()

    def _parse(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            text = fh.read()
        for m in self._BLOCK_RE.finditer(text):
            fields = {k: v.strip() for k, v in self._FIELD_RE.findall(m.group(1))}
            hg     = fields.get("hostgroup_name", "")
            desc   = fields.get("service_description", "")
            if hg and desc:
                for hg_s in (h.strip() for h in hg.split(",")):
                    self._services[(hg_s, desc)] = m.group(0)
                    self._hostgroups.add(hg_s)

    def service_exists(self, hostgroup: str, description: str) -> bool:
        return (hostgroup, description) in self._services

    def hostgroup_has_services(self, hostgroup: str) -> bool:
        return hostgroup in self._hostgroups

    @property
    def total_defined(self) -> int:
        return len(self._services)


# ══════════════════════════════════════════════════════════════════════════════
# §8  SERVICE REPOSITORY
# ══════════════════════════════════════════════════════════════════════════════

class ServiceRepository:
    """
    Manages the service profile .txt directory.
    Auto-creates the directory and seeds it with starter profiles.
    """

    DEFAULT_PROFILES: dict[str, list[str]] = {
        "compute_service_check.txt": [
            "# Compute node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check IB|check_nrpe!check_ib",
            "Check NTP|check_nrpe!check_ntp",
            "Disk Usage|check_nrpe!check_disk",
            "Memory Usage|check_nrpe!check_mem",
        ],
        "gpu_service_check.txt": [
            "# GPU node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check IB|check_nrpe!check_ib",
            "Check NTP|check_nrpe!check_ntp",
            "GPU Utilization|check_nrpe!check_gpu_util",
            "GPU Memory|check_nrpe!check_gpu_mem",
            "GPU Temperature|check_nrpe!check_gpu_temp",
        ],
        "master_service_check.txt": [
            "# Master node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check NTP|check_nrpe!check_ntp",
            "Scheduler Status|check_nrpe!check_scheduler",
            "Disk Usage|check_nrpe!check_disk",
        ],
        "login_service_check.txt": [
            "# Login node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check NTP|check_nrpe!check_ntp",
            "SSH Service|check_nrpe!check_ssh",
            "Disk Usage|check_nrpe!check_disk",
        ],
        "hm_service_check.txt": [
            "# High Memory node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check IB|check_nrpe!check_ib",
            "Check NTP|check_nrpe!check_ntp",
            "Memory Usage|check_nrpe!check_mem",
            "Huge Pages|check_nrpe!check_hugepages",
        ],
        "management_service_check.txt": [
            "# Management node service checks",
            "Current Users|check_nrpe!check_users",
            "Node Load|check_nrpe!check_load",
            "Check NTP|check_nrpe!check_ntp",
            "DHCP Service|check_nrpe!check_dhcp",
            "DNS Service|check_nrpe!check_dns",
            "Disk Usage|check_nrpe!check_disk",
        ],
    }

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        if not os.path.exists(self.repo_dir):
            try:
                os.makedirs(self.repo_dir, exist_ok=True)
                info(f"Created service profile directory: {self.repo_dir}")
                logger.info("Created service repo: %s", self.repo_dir)
                self._create_starter_profiles()
            except OSError as exc:
                warn(f"Cannot create service directory {self.repo_dir}: {exc}")

    def _create_starter_profiles(self) -> None:
        for fname, lines in self.DEFAULT_PROFILES.items():
            path = os.path.join(self.repo_dir, fname)
            try:
                with open(path, "w") as fh:
                    fh.write("\n".join(lines) + "\n")
                info(f"Created starter profile: {fname}")
            except OSError as exc:
                warn(f"Could not create starter profile {fname}: {exc}")

    def available_profiles(self) -> list[str]:
        if not os.path.isdir(self.repo_dir):
            return []
        return sorted(
            f for f in os.listdir(self.repo_dir)
            if f.endswith(".txt")
            and os.path.isfile(os.path.join(self.repo_dir, f))
        )

    def profile_path(self, name: str) -> str:
        return os.path.join(self.repo_dir, name)

    def load_profile(
        self, name: str, skip_validation: bool = False
    ) -> Optional[ServiceProfile]:
        path = self.profile_path(name)
        if not os.path.exists(path):
            error(f"Profile not found: {path}", fatal=False)
            return None
        profile = ServiceProfile(path)
        ok = profile.validate(skip_validation=skip_validation)
        if not ok:
            for e in profile.errors:
                error(f"  Profile {name}: {e}", fatal=False)
            return None
        logger.info("Loaded profile: %s  (%d checks)", name, len(profile.checks))
        return profile


# ══════════════════════════════════════════════════════════════════════════════
# §9  FILE PERMISSION ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════════════

class PermissionManager:
    """
    Enforces nagios:nagios ownership and correct modes:
        conf.d directory  → 0755
        hosts.cfg         → 0744
        services.cfg      → 0744
    Verifies after applying. Logs every action. Safe no-op in dry-run.
    """

    NAGIOS_USER  = "nagios"
    NAGIOS_GROUP = "nagios"
    DIR_MODE     = 0o755
    FILE_MODE    = 0o744

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._uid: Optional[int] = None
        self._gid: Optional[int] = None
        self._resolve_ids()

    def _resolve_ids(self) -> None:
        try:
            self._uid = pwd.getpwnam(self.NAGIOS_USER).pw_uid
            self._gid = grp.getgrnam(self.NAGIOS_GROUP).gr_gid
        except KeyError:
            warn(
                f"System user/group '{self.NAGIOS_USER}' not found. "
                "Permission enforcement will be skipped."
            )

    def _available(self) -> bool:
        return self._uid is not None and self._gid is not None

    def enforce(self, conf_dir: str, *file_paths: str) -> bool:
        if self.dry_run:
            info("[DRY-RUN] Would enforce nagios:nagios ownership on config files.")
            for p in (conf_dir, *file_paths):
                info(f"  [DRY-RUN] chown nagios:nagios + chmod → {p}")
            return True
        if not self._available():
            warn("Skipping permission enforcement (nagios user not found).")
            return False
        all_ok = True
        all_ok &= self._apply(conf_dir, self.DIR_MODE, is_dir=True)
        for path in file_paths:
            if os.path.exists(path):
                all_ok &= self._apply(path, self.FILE_MODE, is_dir=False)
        return all_ok

    def _apply(self, path: str, mode: int, is_dir: bool) -> bool:
        label = "dir" if is_dir else "file"
        try:
            os.chown(path, self._uid, self._gid)
            os.chmod(path, mode)
            st       = os.stat(path)
            got_mode = stat.S_IMODE(st.st_mode)
            if st.st_uid != self._uid or st.st_gid != self._gid or got_mode != mode:
                warn(f"Permission verify failed: {path}")
                logger.warning("Permission verify failed: %s", path)
                return False
            info(
                f"Permissions OK [{label}]: {path}  "
                f"({self.NAGIOS_USER}:{self.NAGIOS_GROUP}, {oct(mode)})"
            )
            logger.info("Permissions updated: %s  mode=%s", path, oct(mode))
            return True
        except (PermissionError, OSError) as exc:
            warn(f"Cannot set permissions on {path}: {exc}")
            logger.error("Permission error: %s  %s", path, exc)
            return False


# ══════════════════════════════════════════════════════════════════════════════
# §10  ATOMIC WRITER
# ══════════════════════════════════════════════════════════════════════════════

class AtomicWriter:
    """Buffer writes; commit via temp-file + atomic rename."""

    def __init__(self, path: str, mode: str = "a") -> None:
        self.path  = path
        self.mode  = mode
        self._buf: list[str] = []
        if mode == "a" and os.path.exists(path):
            with open(path) as fh:
                self._buf.append(fh.read())

    def write(self, text: str) -> None:
        self._buf.append(text)

    def commit(self, dry_run: bool = False) -> None:
        content = "".join(self._buf)
        if dry_run:
            print()
            print(C.wrap("  [DRY-RUN] Would write to: " + self.path, C.YELLOW))
            print(C.wrap("  " + "─" * 56, C.DIM))
            lines = content.splitlines()
            for line in lines[:60]:
                print(C.wrap("  │ ", C.DIM) + line)
            if len(lines) > 60:
                print(C.wrap(f"  │  … ({len(lines) - 60} more lines)", C.DIM))
            return
        directory = os.path.dirname(self.path) or "."
        fd, tmp   = tempfile.mkstemp(dir=directory, prefix=".nagios_tmp_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            shutil.move(tmp, self.path)
            logger.info("Committed %d bytes → %s", len(content), self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ══════════════════════════════════════════════════════════════════════════════
# §11  BACKUP  +  ROLLBACK
# ══════════════════════════════════════════════════════════════════════════════

def backup(path: str, dry_run: bool = False) -> Optional[str]:
    if not os.path.exists(path):
        return None
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = f"{path}.bak.{ts}"
    if dry_run:
        info(f"[DRY-RUN] Would backup: {path} → {dst}")
        return dst
    shutil.copy2(path, dst)
    info(f"Backup created: {dst}")
    logger.info("Backup: %s → %s", path, dst)
    return dst


def rollback(backups: dict[str, Optional[str]]) -> None:
    step("ROLLBACK — Restoring from backups")
    for original, bak in backups.items():
        if bak and os.path.exists(bak):
            try:
                shutil.copy2(bak, original)
                info(f"Restored: {original}  ←  {bak}")
                logger.info("Rollback: %s ← %s", original, bak)
            except OSError as exc:
                error(f"Rollback failed for {original}: {exc}", fatal=False)
        else:
            warn(f"No backup available for {original} — cannot rollback.")


# ══════════════════════════════════════════════════════════════════════════════
# §12  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

NODE_TYPE_MAP: dict[str, str] = {
    "master":  "master",
    "mgmt":    "management",
    "login":   "login",
    "compute": "compute",
    "hm":      "high memory",
    "gpu":     "gpu",
}

GROUPS: list[tuple[str, str]] = [
    ("master",  "MASTER"),
    ("mgmt",    "MANAGEMENT"),
    ("login",   "LOGIN"),
    ("compute", "COMPUTE"),
    ("hm",      "HIGH MEMORY"),
    ("gpu",     "GPU"),
]

DEFAULT_PROFILE_MAP: dict[str, str] = {
    "master":  "master_service_check.txt",
    "mgmt":    "management_service_check.txt",
    "login":   "login_service_check.txt",
    "compute": "compute_service_check.txt",
    "hm":      "hm_service_check.txt",
    "gpu":     "gpu_service_check.txt",
}


# ══════════════════════════════════════════════════════════════════════════════
# §13  HOST CONFIG GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

def _zpad(n: int, width: int) -> str:
    return str(n).zfill(width)

def _pad_width(last_no: int) -> int:
    return max(len(str(last_no)), 3)


def render_template(name: str) -> str:
    return (
        f"define host{{\n"
        f"        name                    {name}\n"
        f"        use                     generic-host\n"
        f"        check_period            24x7\n"
        f"        check_interval          5\n"
        f"        retry_interval          1\n"
        f"        max_check_attempts      10\n"
        f"        check_command           check-host-alive\n"
        f"        notification_period     24x7\n"
        f"        notification_interval   30\n"
        f"        notification_options    d,r\n"
        f"        contact_groups          admins\n"
        f"        register                0\n"
        f"}}\n"
    )


def render_hostgroup(
    group: str,
    node_type: str,
    new_members: list[str],
    existing_members: list[str],
) -> str:
    seen: set[str] = set(existing_members)
    merged = existing_members[:]
    for m in new_members:
        if m not in seen:
            merged.append(m)
            seen.add(m)
    return (
        f"\ndefine hostgroup {{\n"
        f"    hostgroup_name  {group}\n"
        f"    alias           {node_type} nodes\n"
        f"    members         {','.join(merged)}\n"
        f"}}\n"
    )


def render_hosts(
    params: dict,
    template: str,
    dry_run: bool,
) -> tuple[list[str], str, list[str]]:
    """
    Build host definition blocks.
    Returns (blocks, last_ip_used, new_member_names).
    O(1) duplicate check per host via NagiosConfig._host_set.
    Dynamic per-node zero-padding preserves naming across append runs.
    """
    group      = params["group"]
    start_no   = params["start_no"]
    last_no    = params["last_no"]
    prefix     = params["prefix"]
    subnet_pfx = params["subnet_prefix"]
    start_ip   = params["start_ip"]
    cfg: NagiosConfig = params["cfg"]

    existing_members_list: list[str] = params.get("existing_members", [])
    if existing_members_list:
        pfx_len  = len(prefix)
        suffix   = existing_members_list[0][pfx_len:]
        base_pad = len(suffix) if suffix.isdigit() else _pad_width(last_no)
    else:
        base_pad = _pad_width(last_no)

    ip_mgr       = IPManager(start_ip, subnet_pfx)
    total        = last_no - start_no + 1
    usable       = ip_mgr.usable_per_block()
    subnets_need = -(-total // usable)

    info(
        f"  Nodes: {total:,}  |  /{subnet_pfx} usable: {usable:,}  "
        f"|  Subnets needed: {subnets_need:,}"
    )

    blocks:      list[str] = []
    new_members: list[str] = []
    skipped = 0

    for n in range(start_no, last_no + 1):
        name = f"{prefix}{_zpad(n, max(base_pad, len(str(n))))}"
        ip   = ip_mgr.current()

        if cfg.host_exists(name):
            skipped += 1
        else:
            blocks.append(
                f"\ndefine host{{\n"
                f"    use          {template}\n"
                f"    host_name    {name}\n"
                f"    alias        {name}\n"
                f"    address      {ip}\n"
                f"}}\n"
            )
            new_members.append(name)

        if n < last_no:
            ip_mgr.advance()

    if skipped:
        warn(f"  {skipped} host(s) skipped (already defined).")

    return blocks, ip_mgr.current(), new_members


# ══════════════════════════════════════════════════════════════════════════════
# §14  SERVICE CONFIG GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def render_service_block(hostgroup: str, description: str, command: str) -> str:
    return (
        f"\ndefine service{{\n"
        f"    use                 generic-service\n"
        f"    hostgroup_name      {hostgroup}\n"
        f"    service_description {description}\n"
        f"    check_command       {command}\n"
        f"}}\n"
    )


def generate_services(
    hostgroup_profile_map: dict[str, list[ServiceProfile]],
    svc_cfg: ServiceConfig,
    mode: str,
    dry_run: bool,
) -> tuple[str, int, int]:
    """
    Build service definition content. Returns (content, generated, skipped).
    Append mode: only writes new (non-duplicate) blocks.
    Fresh mode:  writes all blocks (svc_cfg is empty).
    Cross-profile duplicate descriptions per hostgroup are also de-duped.
    """
    blocks:    list[str] = []
    generated = 0
    skipped   = 0

    for group, profiles in hostgroup_profile_map.items():
        seen_descs: set[str] = set()
        for profile in profiles:
            for desc, cmd in profile.checks:
                if desc in seen_descs:
                    warn(
                        f"  Duplicate '{desc}' across profiles for '{group}' — skipping."
                    )
                    skipped += 1
                    continue
                if svc_cfg.service_exists(group, desc):
                    logger.info(
                        "Skipped existing service: hostgroup=%s desc=%s", group, desc
                    )
                    skipped += 1
                    seen_descs.add(desc)
                    continue
                blocks.append(render_service_block(group, desc, cmd))
                seen_descs.add(desc)
                generated += 1

        logger.info(
            "Mapped profile(s) → %s: generated=%d skipped=%d", group, generated, skipped
        )

    return "".join(blocks), generated, skipped


# ══════════════════════════════════════════════════════════════════════════════
# §15  HOSTGROUP PATCH  (extend existing members line in-place)
# ══════════════════════════════════════════════════════════════════════════════

def patch_hostgroup_in_file(
    path: str,
    group: str,
    new_members: list[str],
    dry_run: bool,
) -> None:
    if not os.path.exists(path):
        return
    with open(path) as fh:
        original = fh.read()
    pattern = re.compile(
        r"(define\s+hostgroup\s*\{[^}]*hostgroup_name\s+"
        + re.escape(group) + r"[^}]*\})",
        re.DOTALL,
    )
    match = pattern.search(original)
    if not match:
        return
    block     = match.group(1)
    m_members = re.search(r"(members\s+)(\S+)", block)
    if not m_members:
        return
    existing_set = {x.strip() for x in m_members.group(2).split(",") if x.strip()}
    existing_ord = [x.strip() for x in m_members.group(2).split(",") if x.strip()]
    added = 0
    for nm in new_members:
        if nm not in existing_set:
            existing_ord.append(nm)
            existing_set.add(nm)
            added += 1
    if added == 0:
        info(f"  Hostgroup '{group}': no new members to add.")
        return
    if dry_run:
        info(f"  [DRY-RUN] Would add {added} member(s) to hostgroup '{group}'.")
        return
    new_line  = m_members.group(1) + ",".join(existing_ord)
    new_block = block.replace(m_members.group(0), new_line)
    updated   = original.replace(block, new_block)
    aw        = AtomicWriter(path, mode="w")
    aw._buf   = [updated]
    aw.commit(dry_run=False)
    info(f"  Hostgroup '{group}': added {added} new member(s).")


# ══════════════════════════════════════════════════════════════════════════════
# §16  NAGIOS VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_nagios(nagios_cfg: str = "/etc/nagios/nagios.cfg") -> bool:
    step("Nagios Configuration Validation")
    binary = shutil.which("nagios") or shutil.which("nagios4")
    if not binary:
        warn("'nagios' binary not found — skipping validation.")
        warn("Run manually:  nagios -v /etc/nagios/nagios.cfg")
        return False
    try:
        result = subprocess.run(
            [binary, "-v", nagios_cfg],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        output = result.stdout.decode(errors="replace")
        logger.debug("nagios -v output:\n%s", output)
        if result.returncode == 0:
            info("Validation passed ✓")
            return True
        error("Nagios validation FAILED.  Errors from nagios -v:", fatal=False)
        for line in output.splitlines():
            if any(kw in line.lower() for kw in ("error", "warning", "critical")):
                print(C.wrap(f"  │  {line}", C.RED))
        print(C.wrap(f"  Full output logged to {_log_path}", C.DIM))
        return False
    except subprocess.TimeoutExpired:
        error("nagios -v timed out after 60 s.", fatal=False)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# §17  INTERACTIVE UI FLOWS
# ══════════════════════════════════════════════════════════════════════════════

def select_mode() -> str:
    step("Configuration Mode")
    print("    1)  Fresh  — wipe existing config and start clean")
    print("    2)  Append — add new nodes to an existing config")
    print()
    while True:
        choice = prompt("Select [1/2]", "2")
        if choice == "1":
            if confirm("⚠  This will ERASE existing config files. Continue?"):
                return "fresh"
            info("Aborted — returning to mode selection.")
        elif choice == "2":
            return "append"
        else:
            warn("Enter 1 or 2.")


def get_template(cfg: NagiosConfig) -> str:
    step("Host Template")
    existing = list(cfg.templates.keys())
    if existing:
        info(f"Existing templates: {', '.join(existing)}")
    while True:
        t = prompt("Enter template name")
        if t:
            if cfg.template_exists(t):
                warn(f"Template '{t}' already exists — will reuse (no duplicate written).")
            return t
        warn("Template name cannot be empty.")


def select_groups() -> list[str]:
    step("Select Host Groups")
    for idx, (_, display) in enumerate(GROUPS, 1):
        print(f"    {idx})  {display}")
    print()
    raw = prompt("Enter group numbers (comma-separated, e.g. 3,4,5)")
    selected: list[str] = []
    seen:     set[str]  = set()
    try:
        for item in raw.split(","):
            idx = int(item.strip())
            if not 1 <= idx <= len(GROUPS):
                raise ValueError(f"Index {idx} out of range")
            key = GROUPS[idx - 1][0]
            if key not in seen:
                selected.append(key)
                seen.add(key)
    except (ValueError, IndexError) as exc:
        error(f"Invalid selection: {exc}")
    if not selected:
        error("No groups selected.")
    info(f"Selected groups: {', '.join(selected)}")
    return selected


def select_profiles_for_group(
    group: str,
    repo: ServiceRepository,
    preselected: list[str],
    skip_validation: bool,
) -> list[ServiceProfile]:
    """
    Interactively select service profiles for *group*.
    CLI --profile values are shown pre-ticked.
    Returns a list of validated ServiceProfile objects.
    """
    available = repo.available_profiles()
    if not available:
        warn(f"No service profiles found in {repo.repo_dir}.")
        return []

    step(f"Service Profiles  →  {group.upper()}")
    print(f"    Profiles available in: {repo.repo_dir}\n")
    for idx, fname in enumerate(available, 1):
        marker = "✓" if fname in preselected else " "
        print(f"    {idx})  [{marker}]  {fname}")
    print()

    # Build default hint from CLI --profile selections
    default_hint = ""
    if preselected:
        nums = [
            str(available.index(ps) + 1)
            for ps in preselected
            if ps in available
        ]
        default_hint = ",".join(nums)

    # Fall back to canonical default for this group type
    if not default_hint:
        default_profile = DEFAULT_PROFILE_MAP.get(group, "")
        if default_profile in available:
            default_hint = str(available.index(default_profile) + 1)

    raw = prompt(
        f"Select profile numbers for '{group}' (comma-separated)",
        default_hint,
    )
    if not raw.strip():
        warn(f"No profiles selected for '{group}'.")
        return []

    loaded:     list[ServiceProfile] = []
    seen_names: set[str]             = set()

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            idx = int(item)
            if not 1 <= idx <= len(available):
                warn(f"  Index {idx} out of range — skipping.")
                continue
            fname = available[idx - 1]
        except ValueError:
            warn(f"  '{item}' is not a valid number — skipping.")
            continue

        if fname in seen_names:
            warn(f"  Profile '{fname}' already selected — skipping duplicate.")
            continue
        seen_names.add(fname)

        profile = repo.load_profile(fname, skip_validation=skip_validation)
        if profile:
            loaded.append(profile)
            info(f"  Loaded: {fname}  ({len(profile.checks)} checks)")
            logger.info("Loaded profile: %s → group=%s", fname, group)

    return loaded


def collect_node_params(group: str, cfg: NagiosConfig) -> dict:
    step(f"Node Parameters  →  {group.upper()}")
    existing_members = cfg.hostgroup_members(group)
    if existing_members:
        info(f"Hostgroup '{group}' already has {len(existing_members)} member(s).")
        info(f"  First: {existing_members[0]}  …  Last: {existing_members[-1]}")

    while True:
        raw = prompt("Subnet prefix (16–32)", "24")
        try:
            subnet_prefix = int(raw)
            if 16 <= subnet_prefix <= 32:
                break
            warn("Value must be between 16 and 32.")
        except ValueError:
            warn("Enter an integer.")

    while True:
        start_ip = prompt(f"Starting private IP for '{group}' nodes")
        parts = start_ip.split(".")
        if len(parts) == 4 and all(
            p.isdigit() and 0 <= int(p) <= 255 for p in parts
        ):
            break
        warn("Enter a valid IPv4 address (e.g. 10.0.1.1).")

    while True:
        prefix = prompt(f"Hostname prefix for '{group}' (e.g. rbcn, cn, gpu)")
        if prefix:
            break
        warn("Prefix cannot be empty.")

    while True:
        try:
            start_no = int(prompt(f"Start node number for '{group}'", "1"))
            last_no  = int(prompt(f"Last  node number for '{group}'"))
            if start_no <= last_no:
                break
            warn("Start must be ≤ last.")
        except ValueError:
            warn("Enter integers.")

    return {
        "group":            group,
        "node_type":        NODE_TYPE_MAP[group],
        "subnet_prefix":    subnet_prefix,
        "start_ip":         start_ip,
        "prefix":           prefix,
        "start_no":         start_no,
        "last_no":          last_no,
        "existing_members": existing_members,
    }


# ══════════════════════════════════════════════════════════════════════════════
# §18  SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(
    mode: str,
    template: str,
    groups: list[str],
    params_list: list[dict],
    hg_profile_map: dict[str, list[ServiceProfile]],
    svc_stats: dict[str, tuple[int, int]],
    dry_run: bool,
    hosts_cfg: str,
    services_cfg: str,
) -> None:
    W = 70
    print()
    print(C.wrap("  " + "═" * W, C.CYAN))
    print(C.wrap(f"  {'CONFIGURATION SUMMARY':^{W}}", C.CYAN, C.BOLD))
    print(C.wrap("  " + "═" * W, C.CYAN))

    for label, val in [
        ("Mode",         mode + (" [DRY-RUN]" if dry_run else "")),
        ("Template",     template),
        ("Host groups",  ", ".join(groups)),
        ("hosts.cfg",    hosts_cfg),
        ("services.cfg", services_cfg),
        ("Audit log",    _log_path),
    ]:
        print(f"  {C.wrap(label + ':', C.BOLD):<28} {val}")

    print(C.wrap("  " + "─" * W, C.DIM))
    print(C.wrap("  HOST DEFINITIONS", C.BOLD))
    for p in params_list:
        total = p["last_no"] - p["start_no"] + 1
        bp    = _pad_width(p["last_no"])
        first = f"{p['prefix']}{_zpad(p['start_no'], bp)}"
        last_ = f"{p['prefix']}{_zpad(p['last_no'],  bp)}"
        print(
            f"  {C.wrap(p['group'].upper() + ':', C.BOLD):<28}"
            f"{total:>7,} nodes  ({first} … {last_})  "
            f"last IP: {p.get('last_ip', 'n/a')}"
        )

    print(C.wrap("  " + "─" * W, C.DIM))
    print(C.wrap("  SERVICE DEFINITIONS", C.BOLD))
    for group in groups:
        profiles      = hg_profile_map.get(group, [])
        gen, skip     = svc_stats.get(group, (0, 0))
        pnames        = ", ".join(p.name for p in profiles) or "—"
        print(
            f"  {C.wrap(group.upper() + ':', C.BOLD):<28}"
            f"{gen:>4} generated  {skip:>3} skipped  "
            f"profiles: {pnames}"
        )

    print(C.wrap("  " + "═" * W, C.CYAN))
    print()


# ══════════════════════════════════════════════════════════════════════════════
# §19  CLI ARG PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Nagios Host Configuration Utility — CDAC HPC Edition v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without writing any file.")
    ap.add_argument("--conf-dir", default="/etc/nagios/conf.d", metavar="DIR",
                    help="Nagios conf.d directory.")
    ap.add_argument("--nagios-cfg", default="/etc/nagios/nagios.cfg", metavar="FILE",
                    help="Main nagios.cfg for -v validation.")
    ap.add_argument("--service-dir", default="/etc/nagios/service_profiles", metavar="DIR",
                    help="Service profile repository directory.")
    ap.add_argument("--profile", action="append", default=[], metavar="FILE",
                    help="Pre-select a service profile (repeatable). "
                         "Example: --profile gpu_service_check.txt")
    ap.add_argument("--skip-validation", action="store_true",
                    help="Skip nagios -v.")
    ap.add_argument("--skip-permissions", action="store_true",
                    help="Skip chown/chmod enforcement.")
    ap.add_argument("--skip-service-validation", action="store_true",
                    help="Skip strict profile syntax checks.")
    return ap.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# §20  MAIN ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = parse_args()

    HOSTS_CFG    = os.path.join(args.conf_dir, "hosts.cfg")
    SERVICES_CFG = os.path.join(args.conf_dir, "services.cfg")

    banner()
    if args.dry_run:
        print(C.wrap(
            "  ⚠  DRY-RUN MODE — no files will be modified.\n",
            C.YELLOW, C.BOLD,
        ))

    # §20.1  Mode
    mode = select_mode()

    # §20.2  Ensure conf.d exists
    if not args.dry_run:
        os.makedirs(args.conf_dir, exist_ok=True)

    # §20.3  Backup (paths kept for rollback)
    step("Backup Existing Files")
    backups: dict[str, Optional[str]] = {}
    for f in [HOSTS_CFG, SERVICES_CFG]:
        backups[f] = backup(f, dry_run=args.dry_run)

    # §20.4  Fresh mode: wipe
    if mode == "fresh" and not args.dry_run:
        open(HOSTS_CFG,    "w").close()
        open(SERVICES_CFG, "w").close()
        info("Existing config files cleared.")

    # §20.5  Parse existing host config
    step("Parsing Existing Host Configuration")
    cfg = NagiosConfig(HOSTS_CFG)
    info(
        f"Found: {len(cfg.templates)} template(s), "
        f"{len(cfg.hostgroups)} hostgroup(s), "
        f"{len(cfg.hosts):,} host(s)."
    )

    # §20.6  Parse existing service config
    step("Parsing Existing Service Configuration")
    svc_cfg = ServiceConfig(SERVICES_CFG)
    info(f"Existing service definitions: {svc_cfg.total_defined}")

    # §20.7  Template
    template = get_template(cfg)

    # §20.8  Group selection
    groups = select_groups()

    # §20.9  Service repository
    step("Service Profile Repository")
    repo      = ServiceRepository(args.service_dir)
    available = repo.available_profiles()
    if available:
        info(f"Available profiles ({len(available)}):")
        for pf in available:
            print(f"      • {pf}")
    else:
        warn("No profile files found — services.cfg will be empty.")

    # §20.10  Per-group: node params + profile selection
    step("Collect Node Parameters and Service Profiles")
    params_list:     list[dict]                         = []
    hg_profile_map:  dict[str, list[ServiceProfile]]    = {}

    for g in groups:
        p = collect_node_params(g, cfg)
        p["cfg"] = cfg
        params_list.append(p)

        profiles = select_profiles_for_group(
            g, repo,
            preselected=args.profile,
            skip_validation=args.skip_service_validation,
        )
        hg_profile_map[g] = profiles

        if profiles:
            info(
                f"  '{g}' → "
                + ", ".join(
                    f"{pf.name} ({len(pf.checks)} checks)" for pf in profiles
                )
            )
        else:
            warn(f"  No profiles assigned to '{g}' — no service checks generated.")

    # §20.11  Confirm
    print()
    if not confirm("Proceed with configuration generation?"):
        info("Aborted by user.")
        sys.exit(0)

    # §20.12  Write hosts.cfg
    step("Generating hosts.cfg")
    try:
        hosts_writer = AtomicWriter(HOSTS_CFG, mode="a")

        if not cfg.template_exists(template):
            hosts_writer.write("\n" + render_template(template))
            info(f"Template '{template}' added.")
        else:
            info(f"Template '{template}' already present — skipping.")

        for p in params_list:
            group  = p["group"]
            blocks, last_ip, new_members = render_hosts(p, template, args.dry_run)
            p["last_ip"]     = last_ip
            p["new_members"] = new_members

            if mode == "append" and cfg.hostgroup_exists(group):
                patch_hostgroup_in_file(
                    HOSTS_CFG, group, new_members, dry_run=args.dry_run
                )
            else:
                hg_block = render_hostgroup(
                    group, p["node_type"], new_members, p["existing_members"],
                )
                hosts_writer.write(hg_block)
                info(f"Hostgroup '{group}' queued ({len(new_members)} member(s)).")

            for blk in blocks:
                hosts_writer.write(blk)

            info(
                f"  {group.upper()}: {len(new_members)} definition(s) queued.  "
                f"Last IP: {last_ip}"
            )

        hosts_writer.commit(dry_run=args.dry_run)
        if not args.dry_run:
            info(f"hosts.cfg written → {HOSTS_CFG}")

    except Exception as exc:
        error(f"hosts.cfg generation failed: {exc}", fatal=False)
        logger.exception("hosts.cfg generation failed")
        rollback(backups)
        sys.exit(1)

    # §20.13  Write services.cfg
    step("Generating services.cfg")
    svc_stats: dict[str, tuple[int, int]] = {}
    try:
        svc_content, total_gen, total_skip = generate_services(
            hg_profile_map, svc_cfg, mode, args.dry_run,
        )

        # Per-group stats for summary (compute before any file write)
        for g in groups:
            group_gen  = 0
            group_skip = 0
            for pf in hg_profile_map.get(g, []):
                for desc, _ in pf.checks:
                    if svc_cfg.service_exists(g, desc):
                        group_skip += 1
                    else:
                        group_gen += 1
            svc_stats[g] = (group_gen, group_skip)

        if svc_content.strip():
            svc_writer = AtomicWriter(
                SERVICES_CFG,
                mode="a" if mode == "append" else "w",
            )
            svc_writer.write(svc_content)
            svc_writer.commit(dry_run=args.dry_run)
            action = "appended" if mode == "append" else "written"
            info(
                f"services.cfg {action}: {total_gen} new definition(s), "
                f"{total_skip} duplicate(s) skipped."
            )
            logger.info(
                "Services: generated=%d skipped=%d", total_gen, total_skip
            )
        else:
            warn("No new service definitions — all duplicates or no profiles assigned.")

    except Exception as exc:
        error(f"services.cfg generation failed: {exc}", fatal=False)
        logger.exception("services.cfg generation failed")
        rollback(backups)
        sys.exit(1)

    # §20.14  Permission enforcement
    if not args.skip_permissions:
        step("File Ownership and Permission Enforcement")
        perm_mgr = PermissionManager(dry_run=args.dry_run)
        ok = perm_mgr.enforce(args.conf_dir, HOSTS_CFG, SERVICES_CFG)
        if not ok and not args.dry_run:
            warn(
                "Permission enforcement had errors. "
                "Config may not be readable by the nagios process."
            )

    # §20.15  Nagios validation  (triggers rollback on failure)
    if not args.skip_validation and not args.dry_run:
        ok = validate_nagios(args.nagios_cfg)
        if not ok:
            warn("Validation failed — rolling back to backups.")
            rollback(backups)
            sys.exit(1)
    elif args.dry_run:
        info("[DRY-RUN] Skipping nagios -v validation.")

    # §20.16  Summary
    print_summary(
        mode, template, groups, params_list,
        hg_profile_map, svc_stats,
        args.dry_run, HOSTS_CFG, SERVICES_CFG,
    )
    info("Nagios configuration completed successfully ✓")
    logger.info("Session completed successfully.")


if __name__ == "__main__":
    main()
