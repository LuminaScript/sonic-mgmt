"""
Test: 40K static routes stress test.

Three steps:
  1. Add 40K routes (method: config_db or ip_batch), measure time for
     'show ip route' to reflect them.
  2. Check CPU and memory with 'top'.
  3. Remove 40K routes (same method), measure time for 'show ip route'
     to reflect removal.

Methods (--route-stress-method):
  - config_db: config load -y STATIC_ROUTE + redis DEL. Persistent; FIB
    convergence can take minutes.
  - ip_batch: ip route add/del via ip -batch. Non-persistent; convergence
    is typically fast.

The loganalyzer fixture validates syslog automatically.
"""

import time
import logging

import pytest
from tests.community.route_helpers import (
    NUM_ROUTES,
    ROUTE_PREFIX,
    apply_routes,
    apply_routes_config_db,
    remove_routes_config_db,
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
]

POLL_INTERVAL = 5


def _count_routes(duthost):
    """Return number of routes matching ROUTE_PREFIX in 'show ip route'."""
    result = duthost.shell(
        "show ip route | grep -c '{}'".format(ROUTE_PREFIX),
        module_ignore_errors=True
    )
    return int(result["stdout"].strip() or "0")


def _wait_for_routes(duthost, target, compare, label):
    """
    Poll 'show ip route' until compare(count, target) is True.

    This measures FIB convergence: time for the switch to program routes
    from config DB into the kernel/ASIC so they appear in 'show ip route'.
    For 40k routes this can take several minutes; it is separate from the
    (fast) config DB write. No timeout -- polls until convergence.

    Returns (elapsed_seconds, final_count).
    """
    start = time.time()
    while True:
        count = _count_routes(duthost)
        elapsed = time.time() - start
        logger.info("[%s] routes matching '%s': %d  (%.1fs elapsed)",
                    label, ROUTE_PREFIX, count, elapsed)
        if compare(count, target):
            return elapsed, count
        time.sleep(POLL_INTERVAL)


def test_add_40k_static_routes(duthost, nexthop_ip, loganalyzer,
                               baseline_route_count, test_summary, route_stress_method):
    """Add 40K static routes and verify how long until 'show ip route' reflects them."""
    logger.info("Baseline routes before add: %d", baseline_route_count)
    logger.info("Route method: %s", route_stress_method)

    start_time = time.time()
    if route_stress_method == "config_db":
        logger.info("Adding %d static routes via config load (STATIC_ROUTE) ...", NUM_ROUTES)
        apply_routes_config_db(duthost, NUM_ROUTES, nexthop_ip)
    else:
        logger.info("Adding %d static routes via ip -batch ...", NUM_ROUTES)
        apply_routes(duthost, "add", NUM_ROUTES, nexthop_ip)
    add_duration = time.time() - start_time
    logger.info("Route addition took %.2f seconds", add_duration)

    target = baseline_route_count + int(NUM_ROUTES * 0.99)
    if route_stress_method == "config_db":
        logger.info(
            "Waiting for FIB convergence (routes to appear in 'show ip route'; target >= %d). "
            "Switch programming 40k routes from config DB to FIB.",
            target,
        )
    else:
        logger.info("Waiting for routes to appear in 'show ip route' (target >= %d) ...", target)
    convergence_time, final_count = _wait_for_routes(
        duthost,
        target,
        lambda count, tgt: count >= tgt,
        "add-convergence",
    )

    start_time = time.time()
    duthost.shell("show ip route > /dev/null 2>&1")
    show_duration = time.time() - start_time
    logger.info("'show ip route' took %.2f seconds with %d routes loaded",
                show_duration, final_count)

    test_summary["add"] = {
        "add_duration": add_duration,
        "convergence_time": convergence_time,
        "show_duration": show_duration,
        "route_count": final_count,
        "baseline_route_count": baseline_route_count,
        "route_method": route_stress_method,
    }


def test_verify_cpu_and_memory(duthost, loganalyzer, test_summary):
    """Check CPU and memory while 40K routes are loaded."""
    logger.info("=== CPU (top) ===")
    top_output = duthost.shell(
        "top -b -n 1 | head -20", module_ignore_errors=True)["stdout"]
    logger.info(top_output)

    logger.info("=== Memory ===")
    mem_output = duthost.shell(
        "show system-memory", module_ignore_errors=True)["stdout"]
    logger.info(mem_output)

    test_summary["cpu_memory"] = {
        "top_output": top_output,
        "mem_output": mem_output,
    }


def test_remove_40k_static_routes(duthost, nexthop_ip, loganalyzer,
                                  baseline_route_count, test_summary, route_stress_method):
    """Remove 40K static routes and verify how long until 'show ip route' reflects removal."""
    logger.info("Baseline routes before test: %d", baseline_route_count)
    logger.info("Route method: %s", route_stress_method)

    start_time = time.time()
    if route_stress_method == "config_db":
        logger.info("Removing %d static routes from config DB ...", NUM_ROUTES)
        remove_routes_config_db(duthost, NUM_ROUTES)
    else:
        logger.info("Removing %d static routes via ip -batch ...", NUM_ROUTES)
        apply_routes(duthost, "del", NUM_ROUTES, nexthop_ip)
    del_duration = time.time() - start_time
    logger.info("Route removal took %.2f seconds", del_duration)

    # Converged when 99% of added routes are gone (allow up to 1% remaining)
    del_target = baseline_route_count + int(NUM_ROUTES * 0.01)
    if route_stress_method == "config_db":
        logger.info(
            "Waiting for FIB convergence (99%% removed: routes <= %d).",
            del_target,
        )
    else:
        logger.info("Waiting for 99%% of routes to disappear (target <= %d) ...", del_target)
    convergence_time, final_count = _wait_for_routes(
        duthost,
        del_target,
        lambda count, tgt: count <= tgt,
        "del-convergence",
    )

    start_time = time.time()
    duthost.shell("show ip route > /dev/null 2>&1")
    show_duration = time.time() - start_time
    logger.info("'show ip route' took %.2f seconds after removal", show_duration)

    test_summary["remove"] = {
        "del_duration": del_duration,
        "convergence_time": convergence_time,
        "show_duration": show_duration,
        "remaining_routes": final_count,
        "baseline_route_count": baseline_route_count,
        "route_method": route_stress_method,
    }
