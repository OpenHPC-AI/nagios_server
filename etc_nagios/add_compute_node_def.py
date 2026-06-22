import os
import sys
import subprocess
import readline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_command(command):
    try:
        result = subprocess.run(
            command, shell=True, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return result.stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e}")
        sys.exit(1)


def get_user_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        print("Error: End of input encountered!")
        sys.exit(1)


def define_prefix(node_number, prefix, max_digit_count):
    """Return a zero-padded node name, e.g. rbcn00042."""
    return f"{prefix}{str(node_number).zfill(max_digit_count)}"


def get_max_digit_count(start_node_no, last_node_no):
    """At least 3 digits; enough to hold the largest node number."""
    return max(len(str(last_node_no)), 3)


# ---------------------------------------------------------------------------
# IP management
# ---------------------------------------------------------------------------

class IPManager:
    """
    Manages sequential IP allocation across an arbitrary number of /N subnets.

    Rules enforced:
      * .0   is the network address  → skipped
      * .255 is the broadcast address → skipped
      * The number of usable host bits is determined by the subnet prefix.

    For a /prefix the block size is 2^(32-prefix).
    The third-octet portion advances automatically when the 4th octet wraps,
    and the second-octet portion advances when the third wraps, etc.
    """

    def __init__(self, start_ip: str, subnet_prefix: int):
        parts = start_ip.split('.')
        if len(parts) != 4:
            raise ValueError(f"Invalid IP address: {start_ip}")
        self.octets = [int(p) for p in parts]
        for i, o in enumerate(self.octets):
            if not (0 <= o <= 255):
                raise ValueError(f"Octet {i+1} out of range: {o}")

        self.prefix = subnet_prefix
        # How many host bits are there?
        self.host_bits = 32 - subnet_prefix
        # The block size for one subnet
        self.block_size = 2 ** self.host_bits   # e.g. /24 → 256

        # Validate: start IP must not be a network (.0) or broadcast (.255) address
        if self._is_reserved():
            raise ValueError(
                f"Start IP {start_ip} is a reserved (network/broadcast) address."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _to_int(self):
        o = self.octets
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _from_int(self, n):
        self.octets = [(n >> 24) & 0xFF,
                       (n >> 16) & 0xFF,
                       (n >>  8) & 0xFF,
                        n        & 0xFF]

    def _is_reserved(self):
        """
        True if the current IP is the network address (host_part == 0)
        or the broadcast address (host_part == block_size - 1) of its subnet.

        For prefixes < 24 (e.g. /23, /22 …) the block spans multiple
        third-octet values, so addresses like x.x.0.255 or x.x.1.0 that
        fall in the *middle* of the block are perfectly valid host addresses
        and must NOT be skipped.
        """
        ip_int    = self._to_int()
        host_mask = self.block_size - 1          # bits below the prefix boundary
        host_part = ip_int & host_mask
        return host_part == 0 or host_part == host_mask

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def current(self) -> str:
        return ".".join(str(o) for o in self.octets)

    def advance(self):
        """
        Move to the next usable host address.
        Automatically skips network (.0) and broadcast (.255) addresses,
        and handles third- and second-octet rollovers transparently.
        Raises OverflowError if the entire IPv4 space is exhausted.
        """
        ip_int = self._to_int()
        ip_int += 1

        # Keep skipping reserved addresses
        while True:
            if ip_int > 0xFFFFFFFF:
                raise OverflowError("IPv4 address space exhausted.")
            host_mask = self.block_size - 1
            host_part = ip_int & host_mask
            if host_part == 0 or host_part == host_mask:
                ip_int += 1
            else:
                break

        self._from_int(ip_int)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 add_compute_node_def.py <hosts_cfg_path> <template_name>")
        sys.exit(1)

    hosts_cfg_path = sys.argv[1]
    template_name  = sys.argv[2]

    # ---- Collect user inputs ------------------------------------------------
    raw_prefix = get_user_input("Enter the Subnet Prefix of Network (Valid range: 18-24): ")
    try:
        subnet_prefix = int(raw_prefix)
    except ValueError:
        print("Invalid input: subnet prefix must be an integer.")
        sys.exit(1)
    if not (18 <= subnet_prefix <= 24):
        print("Invalid subnet prefix. Please enter a value between 18 and 24.")
        sys.exit(1)

    node_type    = get_user_input(
        "Enter the Node Type to Add in Host Groups (e.g., compute, hm, gpu): "
    )
    pv_net_address = get_user_input(
        f"Enter Private Network Address (Starting Pvt_IP Address of {node_type} node): "
    )
    prefix       = get_user_input(
        f"Enter the Prefix Value for {node_type} node (e.g., rbcn, rpcn, cn): "
    )

    try:
        start_node_no = int(get_user_input(f"Enter the Start {node_type} node number: "))
        last_node_no  = int(get_user_input(f"Enter the Last  {node_type} node number: "))
    except ValueError:
        print("Invalid input: node numbers must be integers.")
        sys.exit(1)

    if start_node_no > last_node_no:
        print("Start node number must be ≤ last node number.")
        sys.exit(1)

    total_nodes = last_node_no - start_node_no + 1

    # ---- Capacity check -----------------------------------------------------
    # Usable hosts per subnet = block_size - 2  (network + broadcast reserved)
    block_size    = 2 ** (32 - subnet_prefix)
    usable_per_subnet = block_size - 2
    subnets_needed    = -(-total_nodes // usable_per_subnet)  # ceiling division

    print(f"\n  Total nodes      : {total_nodes:,}")
    print(f"  Subnet /{subnet_prefix}         : {usable_per_subnet:,} usable hosts per subnet")
    print(f"  Subnets required : {subnets_needed:,}\n")

    # ---- Initialise IP manager ----------------------------------------------
    try:
        ip_mgr = IPManager(pv_net_address, subnet_prefix)
    except ValueError as exc:
        print(f"IP address error: {exc}")
        sys.exit(1)

    # ---- Zero-padding width -------------------------------------------------
    max_digit_count = get_max_digit_count(start_node_no, last_node_no)

    # ---- Write configuration ------------------------------------------------
    try:
        with open(hosts_cfg_path, "a") as hosts_cfg:

            # --- hostgroup block ---
            hosts_cfg.write(f"\ndefine hostgroup {{\n")
            hosts_cfg.write(f"    hostgroup_name {node_type}\n")
            hosts_cfg.write(f"    alias {node_type} nodes\n")

            members = [
                define_prefix(n, prefix, max_digit_count)
                for n in range(start_node_no, last_node_no + 1)
            ]
            hosts_cfg.write(f"    members {','.join(members)}\n")
            hosts_cfg.write(f"}}\n")

            # --- host blocks ---
            for node_number in range(start_node_no, last_node_no + 1):
                node_name = define_prefix(node_number, prefix, max_digit_count)
                node_ip   = ip_mgr.current()

                hosts_cfg.write(f"\ndefine host{{\n")
                hosts_cfg.write(f"    use          {template_name}\n")
                hosts_cfg.write(f"    host_name    {node_name}\n")
                hosts_cfg.write(f"    alias        {node_name}\n")
                hosts_cfg.write(f"    address      {node_ip}\n")
                hosts_cfg.write(f"}}\n")

                # Advance IP only if there are more nodes to process
                if node_number < last_node_no:
                    try:
                        ip_mgr.advance()
                    except OverflowError as exc:
                        print(f"Fatal: {exc}")
                        sys.exit(1)

        print(f"Configuration successfully appended to {hosts_cfg_path}")
        print(f"Last IP used: {ip_mgr.current()}")

    except FileNotFoundError:
        print(f"Error: The file {hosts_cfg_path} was not found.")
        sys.exit(1)
    except Exception as exc:
        print(f"An unexpected error occurred: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

