#!/usr/bin/env python3
"""
add_service_node_def.py  —  Nagios service-node host definition generator
==========================================================================

Generates hostgroup + host definition blocks for service-class nodes:
  master  |  management (mgmt)  |  login

Key differences from add_compute_node_def.py
─────────────────────────────────────────────
• Minimum zero-pad width = 2  (service nodes are few; mn01, login03 are normal)
• Node-count guard: warns when a service group exceeds SERVICE_NODE_WARN_LIMIT
  (these nodes are not expected to number in the thousands)
• Same IPManager with full octet-rollover for correctness, even though service
  nodes rarely cross a /24 boundary
• All other enterprise features identical to the compute variant:
    - IPManager: subnet-aware, no raw 4th-octet arithmetic
    - Atomic file writes (temp-file + rename, never a partial write)
    - Duplicate host / hostgroup detection — skips silently with a warning
    - Dynamic zero-pad that stays consistent across append runs
    - Structured argument parsing (positional + optional --node_type override)
    - Capacity summary printed before writing
    - Last IP used printed after writing
    - PermissionError surfaced with a remediation hint

Usage
─────
  Standalone:
      python3 add_service_node_def.py <hosts_cfg_path> <template_name>

  Via hosts_cfg_add.py orchestrator (pre-fills node type):
      python3 add_service_node_def.py <hosts_cfg> <template> --node_type login
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import shutil
import subprocess
import readline          # noqa: F401  — enables arrow-key editing in input()
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Emit a warning when a *service* node group exceeds this count.
# Compute / GPU / HM nodes are expected to be large; service nodes are not.
SERVICE_NODE_WARN_LIMIT = 64

# Recognised node types for this script
VALID_SERVICE_NODE_TYPES = {"master", "mgmt", "management", "login"}

# Minimum zero-pad digits for service nodes  (2 → mn01, login03)
MIN_PAD_DIGITS = 2


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def run_command(command: str) -> str:
    """Run a shell command and return stdout.  Exits on failure."""
    try:
        result = subprocess.run(
            command, shell=True, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Command failed: {exc}")
        sys.exit(1)


def get_user_input(prompt: str) -> str:
    """Safe input() wrapper; handles EOF gracefully."""
    try:
        return input(prompt)
    except EOFError:
        print("\n[ERROR] End of input encountered.")
        sys.exit(1)


def node_name(number: int, prefix: str, base_pad: int) -> str:
    """
    Return a zero-padded hostname.

    The pad width is the *larger* of:
      • base_pad  (inferred from existing members or MIN_PAD_DIGITS)
      • the natural width of `number` itself

    This ensures names stay consistent across append runs while never
    truncating a number that has grown beyond the original pad width.

    Examples with base_pad=2:
        1  → prefix01      9  → prefix09
        10 → prefix10      99 → prefix99
        100 → prefix100   (grows naturally — no zero-prefix needed)
    """
    pad = max(base_pad, len(str(number)))
    return f"{prefix}{str(number).zfill(pad)}"


def get_base_pad(last_no: int) -> int:
    """Default base pad for a fresh run: at least MIN_PAD_DIGITS, enough for last_no."""
    return max(len(str(last_no)), MIN_PAD_DIGITS)


# ──────────────────────────────────────────────────────────────────────────────
# IP Manager
# ──────────────────────────────────────────────────────────────────────────────

class IPManager:
    """
    Sequential IPv4 allocator with correct subnet-boundary handling.

    The only "reserved" addresses are the very first (network address) and
    very last (broadcast address) of each /prefix block.  For prefixes < /24
    (e.g. /23) addresses whose 4th octet happens to be 0 or 255 but which
    fall *inside* the block are perfectly valid host addresses and must NOT
    be skipped.
    """

    def __init__(self, start_ip: str, subnet_prefix: int) -> None:
        parts = start_ip.split(".")
        if len(parts) != 4:
            raise ValueError(f"Invalid IP address: '{start_ip}'")
        try:
            self.octets = [int(p) for p in parts]
        except ValueError:
            raise ValueError(f"Non-numeric octet in IP address: '{start_ip}'")
        for i, o in enumerate(self.octets):
            if not 0 <= o <= 255:
                raise ValueError(f"Octet {i + 1} out of range (0-255): {o}")
        self.prefix     = subnet_prefix
        self.block_size = 2 ** (32 - subnet_prefix)
        if self._reserved():
            raise ValueError(
                f"{start_ip} is the network or broadcast address of its "
                f"/{subnet_prefix} block — choose the first usable host address."
            )

    # ── internals ──────────────────────────────────────────────────────────

    def _as_int(self) -> int:
        o = self.octets
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _load(self, n: int) -> None:
        self.octets = [(n >> s) & 0xFF for s in (24, 16, 8, 0)]

    def _reserved(self) -> bool:
        """True only for the very first or very last address of a subnet block."""
        hp = self._as_int() & (self.block_size - 1)
        return hp == 0 or hp == self.block_size - 1

    # ── public ─────────────────────────────────────────────────────────────

    def current(self) -> str:
        return ".".join(str(o) for o in self.octets)

    def advance(self) -> None:
        """
        Step to the next usable host address.
        Automatically skips subnet network/broadcast boundaries and handles
        3rd- and 2nd-octet rollovers transparently.
        """
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


# ──────────────────────────────────────────────────────────────────────────────
# Nagios config parser
# ──────────────────────────────────────────────────────────────────────────────

class NagiosConfig:
    """
    Parse an existing hosts.cfg and answer three queries used for safe appending:
      • host_exists(name)        → bool
      • hostgroup_exists(name)   → bool
      • hostgroup_members(name)  → list[str]
    """

    _BLOCK_RE = re.compile(r"define\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
    _FIELD_RE = re.compile(r"^\s*(\S+)\s+(.+)$",            re.MULTILINE)

    def __init__(self, path: str) -> None:
        self.path       = path
        self.hosts:      dict[str, dict] = {}
        self.hostgroups: dict[str, dict] = {}
        self._parse()

    def _parse(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            text = fh.read()
        for m in self._BLOCK_RE.finditer(text):
            kind   = m.group(1).lower()
            fields = {k: v.strip()
                      for k, v in self._FIELD_RE.findall(m.group(2))}
            if kind == "host" and "host_name" in fields:
                if fields.get("register", "1") == "0":
                    continue          # template block — not a real host
                self.hosts[fields["host_name"]] = fields
            elif kind == "hostgroup" and "hostgroup_name" in fields:
                fields["members"] = (
                    [x.strip() for x in fields["members"].split(",") if x.strip()]
                    if "members" in fields else []
                )
                self.hostgroups[fields["hostgroup_name"]] = fields

    def host_exists(self, name: str) -> bool:
        return name in self.hosts

    def hostgroup_exists(self, name: str) -> bool:
        return name in self.hostgroups

    def hostgroup_members(self, name: str) -> list[str]:
        return self.hostgroups.get(name, {}).get("members", [])


# ──────────────────────────────────────────────────────────────────────────────
# Atomic writer
# ──────────────────────────────────────────────────────────────────────────────

class AtomicWriter:
    """
    Buffer writes in memory; commit() writes to a sibling temp file then
    atomically renames it over the target.

    Append mode (default): pre-loads existing content so old + new are merged.
    Overwrite mode ('w'):  starts with an empty buffer.
    """

    def __init__(self, path: str, mode: str = "a") -> None:
        self.path   = path
        self._lines: list[str] = []
        if mode == "a" and os.path.exists(path):
            with open(path) as fh:
                self._lines.append(fh.read())

    def write(self, text: str) -> None:
        self._lines.append(text)

    def commit(self) -> None:
        content   = "".join(self._lines)
        directory = os.path.dirname(self.path) or "."
        fd, tmp   = tempfile.mkstemp(dir=directory, prefix=".svc_node_tmp_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            shutil.move(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ──────────────────────────────────────────────────────────────────────────────
# Hostgroup in-place patch
# ──────────────────────────────────────────────────────────────────────────────

def patch_hostgroup_members(
    path: str,
    group: str,
    new_members: list[str],
) -> int:
    """
    Locate the hostgroup block for `group` in `path` and extend its members
    list with `new_members` (deduped, order-preserving).

    Returns the count of members actually added (0 = nothing to do).
    Uses AtomicWriter — the file is never left in a partial state.
    """
    if not os.path.exists(path):
        return 0

    with open(path) as fh:
        original = fh.read()

    pattern = re.compile(
        r"(define\s+hostgroup\s*\{[^}]*hostgroup_name\s+"
        + re.escape(group)
        + r"[^}]*\})",
        re.DOTALL,
    )
    match = pattern.search(original)
    if not match:
        return 0

    block     = match.group(1)
    m_members = re.search(r"(members\s+)(\S+)", block)
    if not m_members:
        return 0

    existing = [x.strip() for x in m_members.group(2).split(",") if x.strip()]
    merged, added = existing[:], 0
    for nm in new_members:
        if nm not in merged:
            merged.append(nm)
            added += 1

    if added == 0:
        return 0

    new_line  = m_members.group(1) + ",".join(merged)
    new_block = block.replace(m_members.group(0), new_line)
    updated   = original.replace(block, new_block)

    aw = AtomicWriter(path, mode="w")
    aw._lines = [updated]
    aw.commit()
    return added


# ──────────────────────────────────────────────────────────────────────────────
# Validated input helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_int(prompt: str, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    while True:
        raw = get_user_input(prompt)
        try:
            val = int(raw)
        except ValueError:
            print(f"  [ERROR] Expected an integer, got '{raw}'.")
            continue
        if lo is not None and val < lo:
            print(f"  [ERROR] Value must be >= {lo}.")
            continue
        if hi is not None and val > hi:
            print(f"  [ERROR] Value must be <= {hi}.")
            continue
        return val


def _read_ip(prompt: str) -> str:
    while True:
        raw   = get_user_input(prompt).strip()
        parts = raw.split(".")
        if (len(parts) == 4
                and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)):
            return raw
        print(f"  [ERROR] '{raw}' is not a valid IPv4 address (e.g. 10.0.1.1).")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:

    # ── Argument parsing ──────────────────────────────────────────────────────
    if len(sys.argv) < 3:
        print("Usage: python3 add_service_node_def.py "
              "<hosts_cfg_path> <template_name> [--node_type <type>]")
        sys.exit(1)

    hosts_cfg_path = sys.argv[1]
    template_name  = sys.argv[2]

    # Optional --node_type flag (used by the orchestrator to pre-fill the type)
    forced_node_type: Optional[str] = None
    extra = sys.argv[3:]
    i = 0
    while i < len(extra):
        if extra[i] == "--node_type" and i + 1 < len(extra):
            forced_node_type = extra[i + 1].strip().lower()
            i += 2
        else:
            i += 1

    # ── Parse existing config ─────────────────────────────────────────────────
    cfg = NagiosConfig(hosts_cfg_path)
    print(f"\n  [INFO] Parsed '{hosts_cfg_path}': "
          f"{len(cfg.hosts)} host(s), {len(cfg.hostgroups)} hostgroup(s).")

    # ── Collect user inputs ───────────────────────────────────────────────────
    subnet_prefix = _read_int(
        "Enter the Subnet Prefix of Network (valid range 18-24): ",
        lo=18, hi=24,
    )

    if forced_node_type:
        node_type = forced_node_type
        print(f"  [INFO] Node type pre-set to: {node_type}")
    else:
        node_type = get_user_input(
            "Enter the Node Type (e.g., master, mgmt, login): "
        ).strip().lower()
        if not node_type:
            print("  [ERROR] Node type cannot be empty.")
            sys.exit(1)

    if node_type not in VALID_SERVICE_NODE_TYPES:
        print(f"  [WARN] '{node_type}' is not a standard service node type "
              f"({', '.join(sorted(VALID_SERVICE_NODE_TYPES))}).")
        ans = get_user_input("  Continue anyway? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    start_ip = _read_ip(
        f"Enter starting private IP for '{node_type}' nodes: "
    )

    prefix = get_user_input(
        f"Enter hostname prefix for '{node_type}' (e.g., master, mn, login): "
    ).strip()
    if not prefix:
        print("  [ERROR] Prefix cannot be empty.")
        sys.exit(1)

    start_no = _read_int(f"Enter start node number for '{node_type}': ", lo=1)
    last_no  = _read_int(f"Enter last  node number for '{node_type}': ", lo=start_no)

    total_nodes = last_no - start_no + 1

    # ── Service-node count advisory ───────────────────────────────────────────
    if total_nodes > SERVICE_NODE_WARN_LIMIT:
        print(f"\n  [WARN] {total_nodes:,} '{node_type}' nodes requested.")
        print(f"         Service nodes are typically fewer than "
              f"{SERVICE_NODE_WARN_LIMIT}.")
        print("         For large counts consider add_compute_node_def.py.")
        ans = get_user_input("  Proceed? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    # ── Capacity summary ──────────────────────────────────────────────────────
    block_size        = 2 ** (32 - subnet_prefix)
    usable_per_subnet = block_size - 2
    subnets_needed    = -(-total_nodes // usable_per_subnet)

    print(f"\n  Total nodes      : {total_nodes:,}")
    print(f"  Subnet /{subnet_prefix:<2}        : {usable_per_subnet:,} usable hosts")
    print(f"  Subnets required : {subnets_needed:,}")

    # ── IP Manager ────────────────────────────────────────────────────────────
    try:
        ip_mgr = IPManager(start_ip, subnet_prefix)
    except ValueError as exc:
        print(f"\n  [ERROR] {exc}")
        sys.exit(1)

    # ── Zero-pad width ────────────────────────────────────────────────────────
    # Inherit pad width from existing members so names stay consistent across
    # append runs (e.g. login01 stays 2-digit even when appending login10–login20).
    existing_members = cfg.hostgroup_members(node_type)
    if existing_members:
        pfx_len  = len(prefix)
        suffix   = existing_members[0][pfx_len:]
        base_pad = len(suffix) if suffix.isdigit() else get_base_pad(last_no)
        print(f"\n  [INFO] Hostgroup '{node_type}' already has "
              f"{len(existing_members)} member(s). "
              f"Inheriting {base_pad}-digit pad from '{existing_members[0]}'.")
    else:
        base_pad = get_base_pad(last_no)

    # ── Build host blocks ─────────────────────────────────────────────────────
    new_members: list[str] = []
    host_blocks: list[str] = []
    skipped = 0

    for n in range(start_no, last_no + 1):
        name = node_name(n, prefix, base_pad)
        ip   = ip_mgr.current()

        if cfg.host_exists(name):
            print(f"  [WARN] Host '{name}' ({ip}) already defined — skipping.")
            skipped += 1
        else:
            host_blocks.append(
                f"\ndefine host{{\n"
                f"    use          {template_name}\n"
                f"    host_name    {name}\n"
                f"    alias        {name}\n"
                f"    address      {ip}\n"
                f"}}\n"
            )
            new_members.append(name)

        # Always advance IP — even for skipped hosts — so the IP sequence
        # stays aligned with the original allocation plan.
        if n < last_no:
            try:
                ip_mgr.advance()
            except OverflowError as exc:
                print(f"\n  [FATAL] {exc}")
                sys.exit(1)

    if skipped:
        print(f"  [WARN] {skipped} host(s) skipped (already defined).")

    if not new_members:
        print("\n  [INFO] No new hosts to write — "
              "all requested nodes already exist in the config.")
        sys.exit(0)

    # ── Write to file (atomic) ────────────────────────────────────────────────
    try:
        if cfg.hostgroup_exists(node_type) and existing_members:
            # ── Append mode: patch existing hostgroup, then append host blocks ─
            added = patch_hostgroup_members(hosts_cfg_path, node_type, new_members)
            if added:
                print(f"  [INFO] Hostgroup '{node_type}': "
                      f"{added} new member(s) merged in-place.")
            else:
                print(f"  [INFO] Hostgroup '{node_type}': "
                      "members already up-to-date.")

            aw = AtomicWriter(hosts_cfg_path, mode="a")
            for blk in host_blocks:
                aw.write(blk)
            aw.commit()

        else:
            # ── Fresh mode: write hostgroup block + all host blocks together ───
            all_members = existing_members[:]
            for m in new_members:
                if m not in all_members:
                    all_members.append(m)

            hostgroup_block = (
                f"\ndefine hostgroup {{\n"
                f"    hostgroup_name  {node_type}\n"
                f"    alias           {node_type} nodes\n"
                f"    members         {','.join(all_members)}\n"
                f"}}\n"
            )

            aw = AtomicWriter(hosts_cfg_path, mode="a")
            aw.write(hostgroup_block)
            for blk in host_blocks:
                aw.write(blk)
            aw.commit()

    except PermissionError:
        print(f"\n  [ERROR] Permission denied writing to '{hosts_cfg_path}'.")
        print("         Remediation:")
        print("           sudo chown nagios:nagios "
              f"'{hosts_cfg_path}'")
        print("           sudo chmod 640 "
              f"'{hosts_cfg_path}'")
        sys.exit(1)
    except FileNotFoundError:
        print(f"\n  [ERROR] File not found: '{hosts_cfg_path}'")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  [ERROR] Unexpected error: {exc}")
        sys.exit(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  [INFO] Configuration written successfully.")
    print(f"  [INFO] File         : {hosts_cfg_path}")
    print(f"  [INFO] Hosts added  : {len(new_members):,}")
    print(f"  [INFO] Last IP used : {ip_mgr.current()}")


if __name__ == "__main__":
    main()
