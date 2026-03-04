"""Helpers for generating and applying bulk static routes on a DUT.

Supports two methods:
  - ip_batch: 'ip route add/del' via 'ip -batch' (fast convergence, non-persistent).
  - config_db: config load STATIC_ROUTE + redis DEL (persistent, FIB convergence can be slow).
"""

import ipaddress
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

NUM_ROUTES = 40000
# Base network for generated /32 routes (we use .1–.254 per /24 to avoid .0 and .255)
ROUTE_NETWORK = ipaddress.IPv4Network("40.0.0.0/16")
ROUTE_PREFIX = "40.0"  # grep pattern for counting these routes in 'show ip route'

# VRF name used in STATIC_ROUTE table keys
STATIC_ROUTE_VRF = "default"


# ---------- ip_batch method (Linux ip -batch) ----------


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
    hosts_per_net = 254
    for i in range(num_routes):
        block = i // hosts_per_net
        host = (i % hosts_per_net) + 1
        addr = ipaddress.IPv4Address(base + block * 256 + host)
        yield f"ip route {action} {addr}/32 via {nexthop}"


def apply_routes(duthost, action, num_routes, nexthop):
    """
    Generate route commands and execute them on the DUT in one shot using 'ip -batch'.

    Routes are not persistent (lost on reboot). Convergence in 'show ip route' is fast.

    Returns the batch text that was applied (useful for logging/debug).
    """
    batch_text = "\n".join(
        cmd.replace("ip ", "", 1) for cmd in generate_ip_route_commands(action, num_routes, nexthop)
    ) + "\n"
    fd, batch_file = tempfile.mkstemp(suffix=".txt", prefix="routes_{}_".format(action))
    os.close(fd)
    os.unlink(batch_file)
    duthost.copy(content=batch_text, dest=batch_file)
    duthost.shell("ip -batch {}".format(batch_file), module_ignore_errors=True)
    return batch_text


# ---------- config_db method ----------


def _iter_route_prefixes(num_routes):
    """
    Yield IPv4 /32 prefix strings (e.g. '40.0.0.1/32') for the stress test range.

    Uses the same logic as before: 40.0.0.0/16, skipping .0 and .255 per /24.
    """
    base = int(ROUTE_NETWORK.network_address)
    hosts_per_net = 254
    for i in range(num_routes):
        block = i // hosts_per_net
        host = (i % hosts_per_net) + 1
        addr = ipaddress.IPv4Address(base + block * 256 + host)
        yield f"{addr}/32"


def get_static_route_keys(num_routes):
    """
    Yield full CONFIG_DB Redis key names for the test routes.

    Format: "STATIC_ROUTE|default|40.0.0.1/32", etc., for use with redis-cli DEL.
    """
    for prefix in _iter_route_prefixes(num_routes):
        yield "STATIC_ROUTE|{}|{}".format(STATIC_ROUTE_VRF, prefix)


def build_static_route_config_json(num_routes, nexthop):
    """
    Build a config-db fragment (dict) with STATIC_ROUTE entries for merge via config load.

    Args:
        num_routes: number of /32 routes to generate (in 40.0.0.0/16)
        nexthop: gateway IP address for all routes

    Returns:
        dict suitable for JSON: {"STATIC_ROUTE": {"default|40.0.0.1/32": {"nexthop": "..."}, ...}}
    """
    static_routes = {}
    for prefix in _iter_route_prefixes(num_routes):
        key = f"{STATIC_ROUTE_VRF}|{prefix}"
        static_routes[key] = {"nexthop": nexthop}
    return {"STATIC_ROUTE": static_routes}


def apply_routes_config_db(duthost, num_routes, nexthop, json_path="/tmp/static_routes_40k.json"):
    """
    Add static routes by merging a STATIC_ROUTE JSON into config DB via 'config load -y'.

    Routes are persistent across reboot. Uses the same address range as the stress test
    (40.0.0.0/16, /32 prefixes).

    Args:
        duthost: DUT host object
        num_routes: number of routes to add
        nexthop: gateway IP for all routes
        json_path: path on the DUT to write the JSON file

    Returns:
        Path on DUT where the JSON was written.
    """
    config = build_static_route_config_json(num_routes, nexthop)
    payload = json.dumps(config, indent=2)
    duthost.copy(content=payload, dest=json_path)
    duthost.shell("config load -y {}".format(json_path), module_ignore_errors=True)
    return json_path


def remove_routes_config_db(duthost, num_routes, keys_path="/tmp/static_route_keys_40k.txt"):
    """
    Remove the stress-test static routes from CONFIG_DB using sonic-db-cli.

    Runs 'sonic-db-cli CONFIG_DB KEYS "STATIC_ROUTE*"' on the switch to get all
    STATIC_ROUTE keys, filters to the keys we added (40.0.0.0/16 range), then
    deletes each with 'sonic-db-cli CONFIG_DB DEL "key"'. Persists with 'config save -y'.

    Args:
        duthost: DUT host object
        num_routes: number of routes to remove (used to build the exact key set we added)
        keys_path: path on the DUT for the key list file used in the DEL loop
    """
    # Get the exact set of keys we added (STATIC_ROUTE|default|40.0.x.x/32)
    our_keys = set(get_static_route_keys(num_routes))
    if not our_keys:
        return

    # Get all STATIC_ROUTE keys from the switch
    result = duthost.shell(
        'sonic-db-cli CONFIG_DB KEYS "STATIC_ROUTE*"',
        module_ignore_errors=True
    )
    stdout = (result.get("stdout") or "").strip()
    # KEYS output can be newline-separated or space-separated
    all_keys = [k.strip() for k in stdout.replace("\n", " ").split() if k.strip()]

    # Delete only keys that belong to our test routes
    to_delete = [k for k in all_keys if k in our_keys]
    if not to_delete:
        logger.info("No test static route keys found in CONFIG_DB to remove")
        return

    keys_content = "\n".join(to_delete)
    duthost.copy(content=keys_content, dest=keys_path)
    # Delete each key with sonic-db-cli CONFIG_DB DEL "key"
    duthost.shell(
        'while read -r key; do [ -n "$key" ] && sonic-db-cli CONFIG_DB DEL "$key"; done < {}'.format(
            keys_path
        ),
        module_ignore_errors=True
    )
    duthost.shell("config save -y", module_ignore_errors=True)
    duthost.shell("rm -f {}".format(keys_path), module_ignore_errors=True)
