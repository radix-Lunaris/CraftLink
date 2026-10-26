"""Read-only diagnostics; never changes DNS, routes, probe order or proxies."""
import asyncio
import ipaddress
import socket
import time

from . import stun


def resolve(host):
    try:
        address = socket.gethostbyname(host)
        synthetic = ipaddress.ip_address(address) in ipaddress.ip_network("198.18.0.0/15")
        return {"host": host, "address": address, "synthetic_range": synthetic}
    except OSError as exc:
        return {"host": host, "error": str(exc)}


async def log_failure(host, log):
    record = await asyncio.to_thread(resolve, host)
    if "error" in record:
        log(f"[nat] relay STUN DNS failed: {host}: {record['error']}")
    else:
        log(f"[nat] relay STUN DNS: {host} -> {record['address']}; "
            f"ports {stun.DEFAULT_STUN_PORT}/{stun.DEFAULT_STUN_PORT2}; "
            f"endpoint discovery budget {stun.STUN_BUDGET_S:.1f}s")
        if record["synthetic_range"]:
            log("[nat] DNS returned a 198.18.0.0/15 synthetic address. TCP reachability "
                "does not prove that STUN UDP reaches this host; check the proxy/TUN "
                "DNS route. No DNS/proxy setting has been changed by CraftLink.")
    log("[nat] no endpoint was measured; this is NOT proof that every public STUN "
        "server was tried. Use --network-report to inspect each destination separately.")


def probe(host, port):
    record = resolve(host)
    record["port"] = port
    record["mapped"] = None
    if "address" not in record:
        return record
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", 0))
        started = time.monotonic()
        record["mapped"] = stun._stun_once(sock, record["address"], port, .8)
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    finally:
        sock.close()
    return record


async def collect(host):
    # Each destination gets its own diagnostic budget. This does not alter the
    # released operational NAT classifier or steal an active punch socket.
    samples = await asyncio.gather(*(asyncio.to_thread(probe, h, p)
                                    for h, p in stun.default_servers(host)))
    profile = await stun.detect_nat_type(servers=stun.default_servers(host))
    return {"relay_host": host, "operational_default": {
                "public": profile.public, "nat": profile.nat_type.value,
                "filtering": profile.filtering.value, "errors": profile.errors},
            "individual_udp_probes": samples,
            "note": "Independent probe results diagnose reachability; they do not replace the operational NAT result."}
