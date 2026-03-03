"""Helpers for generating and applying bulk static routes on a DUT."""

import ipaddress
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

NUM_ROUTES = 40000
# Base network for generated /32 routes (we use .1–.254 per /24 to avoid .0 and .255)
ROUTE_NETWORK = ipaddress.IPv4Network("40.0.0.0/16")
ROUTE_PREFIX = "40.0"  # grep pattern for counting these routes in 'show ip route'


def generate_ip_route_commands(action, num_routes, nexthop):
    """
    Yield 'ip route' commands for the given action (generator; no full list in memory).

    Uses ipaddress for correctness. Skips .0 and .255 in each /24 to avoid
    network/broadcast addresses.

    Args:
        action: "add" or "del"
        num_routes: how many /32 routes to generate (in 40.0.0.0/16)
        nexthop: gateway IP address

    Yields:
        command strings, e.g. "ip route add 40.0.0.1/32 via 10.0.0.1", ...
    """
    base = int(ROUTE_NETWORK.network_address)
    # 254 usable hosts per /24 (.1–.254; skip .0 and .255)
    hosts_per_net = 254
    for i in range(num_routes):
        block = i // hosts_per_net
        host = (i % hosts_per_net) + 1  # 1..254
        addr = ipaddress.IPv4Address(base + block * 256 + host)
        yield f"ip route {action} {addr}/32 via {nexthop}"


def apply_routes(duthost, action, num_routes, nexthop):
    """
    Generate route commands and execute them on the DUT in one shot
    using 'ip -batch'.

    Returns the batch text that was applied (useful for logging/debug).
    """
    batch_text = "\n".join(
        cmd.replace("ip ", "", 1) for cmd in generate_ip_route_commands(action, num_routes, nexthop)
    ) + "\n"
    fd, batch_file = tempfile.mkstemp(suffix=".txt", prefix=f"routes_{action}_")
    os.close(fd)
    os.unlink(batch_file)

    duthost.copy(content=batch_text, dest=batch_file)
    duthost.shell(f"ip -batch {batch_file}", module_ignore_errors=True)
    return batch_text
