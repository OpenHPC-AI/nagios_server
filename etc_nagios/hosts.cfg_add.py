#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           NAGIOS HOST CONFIGURATION UTILITY  —  CDAC HPC Edition           ║
║                        Enterprise Orchestrator v2.0                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

Supports:
  • Fresh configuration and incremental append of any node type
  • Inline IP management with full octet-rollover (10 000+ nodes)
  • Duplicate host / hostgroup / template detection and safe merge
  • Atomic writes (temp-file + rename) — no half-written configs
  • Timestamped backups before every mutation
  • Dry-run mode — preview changes without touching disk
  • Nagios config validation (nagios -v) with clear error surfacing
  • Structured audit log  (/var/log/nagios_cfg_utility.log)
  • Colour terminal UI with progress indicators

Usage:
    python3 hosts_cfg_add.py [--dry-run] [--conf-dir /etc/nagios/conf.d]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# ANSI colours
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# Logging — file + console
# ──────────────────────────────────────────────────────────────────────────────

LOG_FILE = "/var/log/nagios_cfg_utility.log"

def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("nagios_cfg")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # File handler — fall back to /tmp if /var/log is not writable
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
    logger.info("Session started — PID %s", os.getpid())
    return logger, log_path


logger, _log_path = _setup_logging()


# ──────────────────────────────────────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def info(msg: str)  -> None:
    print(C.wrap(f"  [INFO]  ", C.GREEN, C.BOLD) + msg)
    logger.info(msg)

def warn(msg: str)  -> None:
    print(C.wrap(f"  [WARN]  ", C.YELLOW, C.BOLD) + msg)
    logger.warning(msg)

def error(msg: str, fatal: bool = True) -> None:
    print(C.wrap(f"  [ERROR] ", C.RED, C.BOLD) + msg)
    logger.error(msg)
    if fatal:
        raise SystemExit(1)

def step(title: str) -> None:
    print()
    print(C.wrap(f"  ▶  {title}", C.CYAN, C.BOLD))
    print(C.wrap("  " + "─" * 56, C.DIM))
    logger.info("STEP: %s", title)

def prompt(msg: str, default: str = "") -> str:
    """Safe input() with optional default value shown in brackets."""
    display = f"{msg} [{default}]: " if default else f"{msg}: "
    try:
        val = input(C.wrap(f"  → ", C.BLUE) + display).strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        error("Input interrupted.")
        raise  # unreachable — error() exits

def confirm(msg: str) -> bool:
    """Yes/No prompt — returns True for 'y'."""
    ans = prompt(f"{msg} [y/N]", "n").lower()
    return ans in ("y", "yes")


def banner() -> None:
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║       NAGIOS HOST CONFIGURATION UTILITY  —  v2.0            ║",
        "║                    CDAC HPC Edition                         ║",
        "╚══════════════════════════════════════════════════════════════╝",
    ]
    print()
    for line in lines:
        print(C.wrap(f"  {line}", C.CYAN, C.BOLD))
    print(C.wrap(f"  Audit log → {_log_path}", C.DIM))
    print()


# ──────────────────────────────────────────────────────────────────────────────
# IP Manager  (inline — no subprocess dependency on add_compute_node_def.py)
# ──────────────────────────────────────────────────────────────────────────────

class IPManager:
    """
    Sequential IPv4 allocator that respects subnet boundaries.

    The only "reserved" addresses are the very first (network) and very last
    (broadcast) of each /prefix block.  For prefixes < /24 this means that
    addresses whose 4th octet is 0 or 255 may still be valid host addresses
    (e.g. in a /23, .0.255 and .1.0 are interior to the block).
    """

    def __init__(self, start_ip: str, subnet_prefix: int) -> None:
        parts = start_ip.split(".")
        if len(parts) != 4:
            raise ValueError(f"Invalid IP: {start_ip}")
        self.octets = [int(p) for p in parts]
        for i, o in enumerate(self.octets):
            if not 0 <= o <= 255:
                raise ValueError(f"Octet {i+1} out of range: {o}")
        self.prefix     = subnet_prefix
        self.block_size = 2 ** (32 - subnet_prefix)
        if self._reserved():
            raise ValueError(f"{start_ip} is a subnet network/broadcast address.")

    # ── internals ──────────────────────────────────────────────────────────

    def _as_int(self) -> int:
        o = self.octets
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _load(self, n: int) -> None:
        self.octets = [(n >> s) & 0xFF for s in (24, 16, 8, 0)]

    def _reserved(self) -> bool:
        hp = self._as_int() & (self.block_size - 1)
        return hp == 0 or hp == self.block_size - 1

    # ── public ─────────────────────────────────────────────────────────────

    def current(self) -> str:
        return ".".join(str(o) for o in self.octets)

    def advance(self) -> None:
        n = self._as_int() + 1
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


# ──────────────────────────────────────────────────────────────────────────────
# Nagios config parser  (lightweight — regex-based)
# ──────────────────────────────────────────────────────────────────────────────

class NagiosConfig:
    """
    Read an existing hosts.cfg and answer:
      • Does template X already exist?
      • Does hostgroup Y already exist?  If so, what are its current members?
      • Does host Z already exist?
    """

    _BLOCK_RE   = re.compile(r"define\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
    _FIELD_RE   = re.compile(r"^\s*(\S+)\s+(.+)$", re.MULTILINE)

    def __init__(self, path: str) -> None:
        self.path = path
        self.templates:   dict[str, dict] = {}   # name  → fields
        self.hostgroups:  dict[str, dict] = {}   # name  → fields  (members is a list)
        self.hosts:       dict[str, dict] = {}   # host_name → fields
        self._parse()

    def _parse(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            text = fh.read()
        for m in self._BLOCK_RE.finditer(text):
            kind   = m.group(1).lower()
            body   = m.group(2)
            fields = {k: v.strip() for k, v in self._FIELD_RE.findall(body)}
            if kind == "host":
                name = fields.get("name") or fields.get("host_name", "")
                if "register" in fields and fields["register"] == "0":
                    self.templates[name] = fields
                elif "host_name" in fields:
                    self.hosts[fields["host_name"]] = fields
            elif kind == "hostgroup":
                hg = fields.get("hostgroup_name", "")
                if "members" in fields:
                    fields["members"] = [
                        m.strip() for m in fields["members"].split(",")
                    ]
                else:
                    fields["members"] = []
                self.hostgroups[hg] = fields

    def template_exists(self, name: str) -> bool:
        return name in self.templates

    def hostgroup_exists(self, name: str) -> bool:
        return name in self.hostgroups

    def host_exists(self, name: str) -> bool:
        return name in self.hosts

    def hostgroup_members(self, name: str) -> list[str]:
        return self.hostgroups.get(name, {}).get("members", [])


# ──────────────────────────────────────────────────────────────────────────────
# Atomic file writer
# ──────────────────────────────────────────────────────────────────────────────

class AtomicWriter:
    """
    Buffers all writes in memory; on commit() writes to a temp file
    then atomically renames it over the target.  On rollback() does nothing.
    """

    def __init__(self, path: str, mode: str = "a") -> None:
        self.path    = path
        self.mode    = mode          # 'w' = overwrite, 'a' = append
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
            # Show first 60 lines
            lines = content.splitlines()
            for line in lines[:60]:
                print(C.wrap("  │ ", C.DIM) + line)
            if len(lines) > 60:
                print(C.wrap(f"  │  … ({len(lines) - 60} more lines)", C.DIM))
            return
        dir_  = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".nagios_tmp_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            shutil.move(tmp, self.path)
            logger.info("Committed %d bytes → %s", len(content), self.path)
        except Exception:
            os.unlink(tmp)
            raise


# ──────────────────────────────────────────────────────────────────────────────
# Backup
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

NODE_TYPE_MAP = {
    "master":  "master",
    "mgmt":    "management",
    "login":   "login",
    "compute": "compute",
    "hm":      "high memory",
    "gpu":     "gpu",
}

GROUPS = [
    ("master",  "MASTER"),
    ("mgmt",    "MANAGEMENT"),
    ("login",   "LOGIN"),
    ("compute", "COMPUTE"),
    ("hm",      "HIGH MEMORY"),
    ("gpu",     "GPU"),
]

SERVICE_CHECKS = [
    ("Current Users",  "check_nrpe!check_users"),
    ("Node Load",      "check_nrpe!check_load"),
    ("Check IB",       "check_nrpe!check_ib"),
    ("Check NTP",      "check_nrpe!check_ntp"),
]


# ──────────────────────────────────────────────────────────────────────────────
# UI flows
# ──────────────────────────────────────────────────────────────────────────────

def select_mode() -> str:
    step("Configuration Mode")
    print("    1)  Fresh — wipe existing config and start clean")
    print("    2)  Append — add new nodes to an existing config")
    print()
    while True:
        choice = prompt("Select [1/2]", "2")
        if choice == "1":
            if confirm("⚠  This will ERASE existing config files. Continue?"):
                return "fresh"
            else:
                info("Aborted — returning to mode selection.")
        elif choice == "2":
            return "append"
        else:
            warn("Enter 1 or 2.")


def get_template(cfg: NagiosConfig) -> str:
    step("Host Template")
    existing = list(cfg.templates.keys())
    if existing:
        info(f"Existing templates detected: {', '.join(existing)}")
    while True:
        t = prompt("Enter template name")
        if t:
            if cfg.template_exists(t):
                warn(f"Template '{t}' already exists — will reuse it (no duplicate written).")
            return t
        warn("Template name cannot be empty.")


def select_groups() -> list[str]:
    step("Select Host Groups")
    for idx, (_, display) in enumerate(GROUPS, 1):
        print(f"    {idx})  {display}")
    print()
    raw = prompt("Enter group numbers (comma-separated, e.g. 3,4,5)")
    selected = []
    try:
        for item in raw.split(","):
            idx = int(item.strip())
            if not 1 <= idx <= len(GROUPS):
                raise ValueError(f"Index {idx} out of range")
            key = GROUPS[idx - 1][0]
            if key not in selected:
                selected.append(key)
    except (ValueError, IndexError) as exc:
        error(f"Invalid selection: {exc}")
    if not selected:
        error("No groups selected.")
    info(f"Selected groups: {', '.join(selected)}")
    return selected


def collect_node_params(group: str, cfg: NagiosConfig) -> dict:
    """
    Interactively collect parameters for one node group.
    Returns a dict with all required fields.
    Detects existing hostgroup members so we can extend them in append mode.
    """
    step(f"Node Parameters  →  {group.upper()}")

    existing_members = cfg.hostgroup_members(group)
    if existing_members:
        info(f"Hostgroup '{group}' already has {len(existing_members)} member(s).")
        info(f"  First: {existing_members[0]}  …  Last: {existing_members[-1]}")

    # Subnet prefix
    while True:
        raw = prompt("Subnet prefix (18–24)", "24")
        try:
            subnet_prefix = int(raw)
            if 18 <= subnet_prefix <= 24:
                break
            warn("Value must be between 18 and 24.")
        except ValueError:
            warn("Enter an integer.")

    # Starting IP
    while True:
        start_ip = prompt(f"Starting private IP for '{group}' nodes")
        parts = start_ip.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            break
        warn("Enter a valid IPv4 address (e.g. 10.0.1.1).")

    # Hostname prefix
    while True:
        prefix = prompt(f"Hostname prefix for '{group}' (e.g. rbcn, cn, gpu)")
        if prefix:
            break
        warn("Prefix cannot be empty.")

    # Node range
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
        "group":         group,
        "node_type":     NODE_TYPE_MAP[group],
        "subnet_prefix": subnet_prefix,
        "start_ip":      start_ip,
        "prefix":        prefix,
        "start_no":      start_no,
        "last_no":       last_no,
        "existing_members": existing_members,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Config generators
# ──────────────────────────────────────────────────────────────────────────────

def _zpad(n: int, width: int) -> str:
    return str(n).zfill(width)

def _pad_width(last_no: int) -> int:
    return max(len(str(last_no)), 3)


def render_template(name: str) -> str:
    return f"""\
define host{{
        name                    {name}
        use                     generic-host
        check_period            24x7
        check_interval          5
        retry_interval          1
        max_check_attempts      10
        check_command           check-host-alive
        notification_period     24x7
        notification_interval   30
        notification_options    d,r
        contact_groups          admins
        register                0
}}
"""


def render_hostgroup(
    group: str,
    node_type: str,
    new_members: list[str],
    existing_members: list[str],
    cfg: NagiosConfig,
) -> str:
    """
    Return a complete hostgroup block.
    In append mode the existing members are merged with new ones (deduped).
    """
    merged = existing_members[:]
    for m in new_members:
        if m not in merged:
            merged.append(m)
    members_str = ",".join(merged)
    return (
        f"\ndefine hostgroup {{\n"
        f"    hostgroup_name  {group}\n"
        f"    alias           {node_type} nodes\n"
        f"    members         {members_str}\n"
        f"}}\n"
    )


def render_hosts(
    params: dict,
    template: str,
    dry_run: bool,
) -> tuple[list[str], str, list[str]]:
    """
    Build host definition strings.
    Returns (blocks, last_ip_used, new_member_names).
    Skips hosts that already exist in cfg (append-safe).
    """
    group        = params["group"]
    start_no     = params["start_no"]
    last_no      = params["last_no"]
    prefix       = params["prefix"]
    subnet_pfx   = params["subnet_prefix"]
    start_ip     = params["start_ip"]
    cfg: NagiosConfig = params["cfg"]

    # Determine zero-padding width.
    # In append mode existing members may have been written with a narrower pad
    # (e.g. 3 digits when the cluster had <1000 nodes).  Infer from their suffix
    # so that duplicate-detection ("cn001" vs "cn00001") works correctly.
    existing_members_list: list[str] = params.get("existing_members", [])
    # Determine zero-pad width.
    # Priority: (1) infer from existing member suffix so names stay consistent
    # across append runs, (2) ensure the new last_no is representable.
    # Example: cluster started with cn001…cn254 (3-digit).  Appending nodes
    # 255–10240 must still generate cn255…cn999 and cn1000…cn10240, i.e. the
    # pad only *grows* when the number itself overflows the inferred width.
    # Determine the *base* zero-pad width from existing members so that names
    # match across append runs (e.g. cn001 stays 3-digit).  For node numbers
    # that overflow the base width the pad grows naturally per-node so we never
    # truncate or mismatch names (cn001…cn999 = 3-digit, cn1000… = 4-digit).
    if existing_members_list:
        pfx_len = len(prefix)
        suffix  = existing_members_list[0][pfx_len:]
        base_pad = len(suffix) if suffix.isdigit() else _pad_width(last_no)
    else:
        base_pad = _pad_width(last_no)

    ip_mgr = IPManager(start_ip, subnet_pfx)

    total        = last_no - start_no + 1
    usable       = ip_mgr.usable_per_block()
    subnets_need = -(-total // usable)

    info(f"  Nodes: {total:,}  |  /{subnet_pfx} usable: {usable:,}  |  Subnets needed: {subnets_need:,}")

    blocks:      list[str] = []
    new_members: list[str] = []
    skipped = 0

    for n in range(start_no, last_no + 1):
        name = f"{prefix}{_zpad(n, max(base_pad, len(str(n))))}"

        ip   = ip_mgr.current()

        if cfg.host_exists(name):
            warn(f"  Host '{name}' already exists — skipping (no duplicate).")
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

    last_ip = ip_mgr.current()
    return blocks, last_ip, new_members


def render_services(groups: list[str]) -> str:
    hostgroups = ",".join(groups)
    lines = []
    for desc, cmd in SERVICE_CHECKS:
        lines.append(
            f"\ndefine service{{\n"
            f"    use                 generic-service\n"
            f"    hostgroup_name      {hostgroups}\n"
            f"    service_description {desc}\n"
            f"    check_command       {cmd}\n"
            f"}}\n"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Hostgroup patch  (append mode: update existing members list in place)
# ──────────────────────────────────────────────────────────────────────────────

def patch_hostgroup_in_file(
    path: str,
    group: str,
    new_members: list[str],
    dry_run: bool,
) -> None:
    """
    If a hostgroup block already exists in path, extend its members line.
    Uses AtomicWriter so the file is never left in a partial state.
    """
    if not os.path.exists(path):
        return
    with open(path) as fh:
        original = fh.read()

    # Locate the hostgroup block
    pattern = re.compile(
        r"(define\s+hostgroup\s*\{[^}]*hostgroup_name\s+" + re.escape(group) + r"[^}]*\})",
        re.DOTALL,
    )
    match = pattern.search(original)
    if not match:
        return  # not present — caller will append a fresh block

    block = match.group(1)
    # Extract current members
    m_members = re.search(r"(members\s+)(\S+)", block)
    if not m_members:
        return

    existing = [x.strip() for x in m_members.group(2).split(",") if x.strip()]
    merged   = existing[:]
    added    = 0
    for nm in new_members:
        if nm not in merged:
            merged.append(nm)
            added += 1

    if added == 0:
        info(f"  Hostgroup '{group}': no new members to add.")
        return

    new_line  = m_members.group(1) + ",".join(merged)
    new_block = block.replace(m_members.group(0), new_line)
    updated   = original.replace(block, new_block)

    if dry_run:
        info(f"  [DRY-RUN] Would add {added} member(s) to hostgroup '{group}'.")
        return

    aw = AtomicWriter(path, mode="w")
    aw._buf = [updated]
    aw.commit(dry_run=False)
    info(f"  Hostgroup '{group}': added {added} new member(s).")


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────

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
        else:
            error("Nagios validation FAILED.  Errors from nagios -v:", fatal=False)
            # Extract just the error lines for clarity
            for line in output.splitlines():
                if any(kw in line.lower() for kw in ("error", "warning", "critical")):
                    print(C.wrap(f"  │  {line}", C.RED))
            print()
            print(C.wrap(f"  Full output logged to {_log_path}", C.DIM))
            return False
    except subprocess.TimeoutExpired:
        error("nagios -v timed out after 60 s.", fatal=False)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(
    mode: str,
    template: str,
    groups: list[str],
    params_list: list[dict],
    dry_run: bool,
    hosts_cfg: str,
    services_cfg: str,
) -> None:
    width = 64
    print()
    print(C.wrap("  " + "═" * width, C.CYAN))
    print(C.wrap(f"  {'CONFIGURATION SUMMARY':^{width}}", C.CYAN, C.BOLD))
    print(C.wrap("  " + "═" * width, C.CYAN))

    rows = [
        ("Mode",          mode + (" [DRY-RUN]" if dry_run else "")),
        ("Template",       template),
        ("Host groups",    ", ".join(groups)),
        ("hosts.cfg",      hosts_cfg),
        ("services.cfg",   services_cfg),
        ("Audit log",      _log_path),
    ]
    for label, val in rows:
        print(f"  {C.wrap(label + ':', C.BOLD):<28} {val}")

    print(C.wrap("  " + "─" * width, C.DIM))
    for p in params_list:
        total = p["last_no"] - p["start_no"] + 1
        pad   = _pad_width(p["last_no"])
        first = f"{p['prefix']}{_zpad(p['start_no'], pad)}"
        last  = f"{p['prefix']}{_zpad(p['last_no'],  pad)}"
        print(
            f"  {C.wrap(p['group'].upper() + ':', C.BOLD):<28} "
            f"{total:,} nodes  ({first} … {last})  "
            f"last IP: {p.get('last_ip', 'n/a')}"
        )
    print(C.wrap("  " + "═" * width, C.CYAN))
    print()


# ──────────────────────────────────────────────────────────────────────────────
# Main orchestration
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Nagios Host Configuration Utility — CDAC HPC Edition v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing any file.",
    )
    ap.add_argument(
        "--conf-dir", default="/etc/nagios/conf.d",
        metavar="DIR",
        help="Nagios conf.d directory (default: /etc/nagios/conf.d).",
    )
    ap.add_argument(
        "--nagios-cfg", default="/etc/nagios/nagios.cfg",
        metavar="FILE",
        help="Main nagios.cfg used for validation.",
    )
    ap.add_argument(
        "--skip-validation", action="store_true",
        help="Skip 'nagios -v' validation step.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    HOSTS_CFG    = os.path.join(args.conf_dir, "hosts.cfg")
    SERVICES_CFG = os.path.join(args.conf_dir, "services.cfg")

    banner()

    if args.dry_run:
        print(C.wrap("  ⚠  DRY-RUN MODE — no files will be modified.\n", C.YELLOW, C.BOLD))

    # ── 1.  Mode ──────────────────────────────────────────────────────────────
    mode = select_mode()

    # ── 2.  Ensure conf.d exists ──────────────────────────────────────────────
    if not args.dry_run:
        os.makedirs(args.conf_dir, exist_ok=True)

    # ── 3.  Backup ────────────────────────────────────────────────────────────
    step("Backup Existing Files")
    for f in [HOSTS_CFG, SERVICES_CFG]:
        backup(f, dry_run=args.dry_run)

    # ── 4.  Fresh mode: wipe ──────────────────────────────────────────────────
    if mode == "fresh" and not args.dry_run:
        open(HOSTS_CFG, "w").close()
        open(SERVICES_CFG, "w").close()
        info("Existing config files cleared.")

    # ── 5.  Parse existing config ─────────────────────────────────────────────
    step("Parsing Existing Configuration")
    cfg = NagiosConfig(HOSTS_CFG)
    info(
        f"Found: {len(cfg.templates)} template(s), "
        f"{len(cfg.hostgroups)} hostgroup(s), "
        f"{len(cfg.hosts)} host(s)."
    )

    # ── 6.  Template ──────────────────────────────────────────────────────────
    template = get_template(cfg)

    # ── 7.  Group selection ───────────────────────────────────────────────────
    groups = select_groups()

    # ── 8.  Per-group parameters ──────────────────────────────────────────────
    step("Collect Node Parameters")
    params_list: list[dict] = []
    for g in groups:
        p = collect_node_params(g, cfg)
        p["cfg"] = cfg          # pass parser reference for duplicate checks
        params_list.append(p)

    # ── 9.  Confirm ───────────────────────────────────────────────────────────
    print()
    if not confirm("Proceed with configuration generation?"):
        info("Aborted by user.")
        sys.exit(0)

    # ── 10. Write hosts.cfg ───────────────────────────────────────────────────
    step("Generating hosts.cfg")
    hosts_writer = AtomicWriter(HOSTS_CFG, mode="a")

    # Template block
    if not cfg.template_exists(template):
        hosts_writer.write("\n" + render_template(template))
        info(f"Template '{template}' added.")
    else:
        info(f"Template '{template}' already present — skipping.")

    for p in params_list:
        group  = p["group"]
        blocks, last_ip, new_members = render_hosts(p, template, args.dry_run)
        p["last_ip"]      = last_ip
        p["new_members"]  = new_members

        if mode == "append" and cfg.hostgroup_exists(group):
            # Patch the existing hostgroup members line in-place (atomic)
            if not args.dry_run:
                patch_hostgroup_in_file(HOSTS_CFG, group, new_members, dry_run=False)
            else:
                patch_hostgroup_in_file(HOSTS_CFG, group, new_members, dry_run=True)
        else:
            # Write a fresh hostgroup block
            hg_block = render_hostgroup(
                group,
                p["node_type"],
                new_members,
                p["existing_members"],
                cfg,
            )
            hosts_writer.write(hg_block)
            info(f"Hostgroup '{group}' block queued ({len(new_members)} member(s)).")

        for blk in blocks:
            hosts_writer.write(blk)

        info(
            f"  {group.upper()}: {len(new_members)} host definition(s) queued.  "
            f"Last IP: {last_ip}"
        )

    hosts_writer.commit(dry_run=args.dry_run)
    if not args.dry_run:
        info(f"hosts.cfg written → {HOSTS_CFG}")

    # ── 11. Write services.cfg ────────────────────────────────────────────────
    step("Generating services.cfg")
    if mode == "append":
        warn("Append mode — existing service definitions preserved.")
        info("To add new service checks edit services.cfg manually or re-run in fresh mode.")
    else:
        svc_writer = AtomicWriter(SERVICES_CFG, mode="w")
        svc_writer.write(render_services(groups))
        svc_writer.commit(dry_run=args.dry_run)
        if not args.dry_run:
            info(f"services.cfg written → {SERVICES_CFG}")

    # ── 12. Validate ──────────────────────────────────────────────────────────
    if not args.skip_validation and not args.dry_run:
        validate_nagios(args.nagios_cfg)
    elif args.dry_run:
        info("[DRY-RUN] Skipping nagios -v validation.")

    # ── 13. Summary ───────────────────────────────────────────────────────────
    print_summary(mode, template, groups, params_list, args.dry_run, HOSTS_CFG, SERVICES_CFG)
    info("Nagios configuration completed successfully ✓")
    logger.info("Session completed successfully.")


if __name__ == "__main__":
    main()
