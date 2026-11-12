import os
import re
import time
import socket
import shutil
import platform
import asyncio
import aiohttp
import psutil

from FileStream.core.runtime.human_readable import humanbytes

# Network cache settings (cache IP/ISP/ASN/Location for 10 minutes)
_cached_network_info: dict | None = None
_cached_network_time: float = 0.0
NETWORK_CACHE_TTL = 600  # 10 minutes


def format_uptime(seconds: int | float) -> str:
    """
    Formats uptime compactly for mobile screens (e.g. '3d 7h', '5h 12m', '42m 10s', '15s').
    """
    total = max(int(seconds), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def get_os_info() -> str:
    """
    Detects OS / Linux distribution dynamically without external dependencies.
    """
    try:
        info = platform.freedesktop_os_release()
        pretty = info.get("PRETTY_NAME")
        if pretty:
            return pretty.strip("\"'")
        name = info.get("NAME")
        ver = info.get("VERSION_ID") or info.get("VERSION")
        if name and ver:
            return f"{name} {ver}".strip("\"'")
        if name:
            return name.strip("\"'")
    except Exception:
        pass

    try:
        if os.path.isfile("/etc/os-release"):
            with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as f:
                data = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        data[k.strip()] = v.strip("\"'")
                if "PRETTY_NAME" in data and data["PRETTY_NAME"]:
                    return data["PRETTY_NAME"]
                if "NAME" in data and data["NAME"]:
                    ver = data.get("VERSION_ID", data.get("VERSION", ""))
                    return f"{data['NAME']} {ver}".strip()
    except Exception:
        pass

    sys_name = platform.system() or "Linux"
    rel = platform.release() or ""
    return f"{sys_name} {rel}".strip()


def get_kernel_info() -> str:
    """
    Returns the kernel release string.
    """
    try:
        return platform.release() or "Unknown"
    except Exception:
        return "Unknown"


def get_arch_info() -> str:
    """
    Returns the system architecture (e.g. x86_64, aarch64).
    """
    try:
        return platform.machine() or "Unknown"
    except Exception:
        return "Unknown"


def is_container_environment() -> bool:
    """
    Safely checks if the current process is running inside a container (Docker/containerd/Podman/LXC)
    without using privileged tricks or modifying system state.
    """
    try:
        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            return True
        if os.path.isfile("/proc/1/cgroup"):
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                if any(k in content for k in ("docker", "kubepods", "containerd", "lxc")):
                    return True
    except Exception:
        pass
    return False


def get_hostname() -> str:
    """
    Returns the machine hostname.
    """
    try:
        return socket.gethostname() or "localhost"
    except Exception:
        return "localhost"


def get_cpu_info() -> dict:
    """
    Collects CPU model, core count, and usage percent safely.
    Accurately reports logical CPU cores.
    """
    cores = 1
    try:
        cores = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    except Exception:
        pass

    model = None
    try:
        if os.path.isfile("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if any(line.startswith(k) for k in ("model name", "Hardware", "Processor")):
                        parts = line.split(":", 1)
                        if len(parts) == 2 and parts[1].strip():
                            model = re.sub(r"\s+", " ", parts[1].strip())
                            break
    except Exception:
        pass

    usage = 0.0
    try:
        usage = psutil.cpu_percent(interval=0.05)
    except Exception:
        pass

    display = f"{cores} logical cores"
    return {
        "cores": cores,
        "model": model,
        "usage_percent": usage,
        "display": display,
    }


def get_ram_info() -> dict:
    """
    Collects RAM usage and total capacity.
    Respects container cgroup memory limits (cgroups v1 and v2) when constrained.
    """
    used_bytes = None
    total_bytes = None
    available_bytes = None

    try:
        # Cgroups v2
        if os.path.isfile("/sys/fs/cgroup/memory.max"):
            with open("/sys/fs/cgroup/memory.max", "r", encoding="utf-8", errors="ignore") as f:
                val = f.read().strip()
                if val and val != "max" and val.isdigit():
                    cgroup_limit = int(val)
                    if 0 < cgroup_limit < 1_000_000_000_000_000:
                        total_bytes = cgroup_limit
                        if os.path.isfile("/sys/fs/cgroup/memory.current"):
                            with open("/sys/fs/cgroup/memory.current", "r", encoding="utf-8", errors="ignore") as cur_f:
                                cur_val = cur_f.read().strip()
                                if cur_val and cur_val.isdigit():
                                    used_bytes = int(cur_val)
                                    available_bytes = max(total_bytes - used_bytes, 0)
        # Cgroups v1
        elif os.path.isfile("/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r", encoding="utf-8", errors="ignore") as f:
                val = f.read().strip()
                if val and val.isdigit():
                    cgroup_limit = int(val)
                    if 0 < cgroup_limit < 1_000_000_000_000_000:
                        total_bytes = cgroup_limit
                        if os.path.isfile("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
                            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r", encoding="utf-8", errors="ignore") as cur_f:
                                cur_val = cur_f.read().strip()
                                if cur_val and cur_val.isdigit():
                                    used_bytes = int(cur_val)
                                    available_bytes = max(total_bytes - used_bytes, 0)
    except Exception:
        pass

    try:
        mem = psutil.virtual_memory()
        if total_bytes is None:
            total_bytes = mem.total
            used_bytes = mem.used
            available_bytes = mem.available
    except Exception:
        if total_bytes is None:
            total_bytes = 0
            used_bytes = 0
            available_bytes = 0

    used_str = humanbytes(used_bytes or 0)
    total_str = humanbytes(total_bytes or 0)
    avail_str = humanbytes(available_bytes or 0)
    percent = round(((used_bytes or 0) / total_bytes) * 100, 1) if total_bytes else 0.0

    return {
        "used": used_str,
        "total": total_str,
        "available": avail_str,
        "percent": percent,
        "display": f"{used_str} / {total_str}",
    }


def get_disk_info(path: str = "/") -> dict:
    """
    Collects disk usage and total space safely.
    """
    try:
        disk = shutil.disk_usage(path)
        used = humanbytes(disk.used)
        total = humanbytes(disk.total)
        free = humanbytes(disk.free)
        percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    except Exception:
        used, total, free, percent = "0 B", "0 B", "0 B", 0.0

    return {
        "used": used,
        "total": total,
        "free": free,
        "percent": percent,
        "display": f"{used} / {total}",
    }


def get_uptime_info() -> dict:
    """
    Collects system boot time and uptime string.
    """
    try:
        boot_time = psutil.boot_time()
        uptime_seconds = max(int(time.time() - boot_time), 0)
    except Exception:
        boot_time = time.time()
        uptime_seconds = 0

    return {
        "boot_time": boot_time,
        "uptime_seconds": uptime_seconds,
        "display": format_uptime(uptime_seconds),
    }


def get_local_network_info() -> dict:
    """
    Collects local network interface details safely without failing on restricted /proc/net/dev.
    """
    interfaces = {}
    primary_interface = "N/A"
    primary_status = "Active"

    try:
        addrs = psutil.net_if_addrs()
        for iface_name, addr_list in addrs.items():
            if iface_name == "lo":
                continue
            ipv4 = None
            ipv6 = None
            for addr in addr_list:
                family = getattr(addr.family, "name", str(addr.family))
                if family == "AF_INET" or addr.family == 2:
                    ipv4 = addr.address
                elif family == "AF_INET6" or addr.family == 10:
                    addr_str = str(addr.address)
                    if not addr_str.startswith("fe80:"):
                        ipv6 = addr_str
            interfaces[iface_name] = {"ipv4": ipv4, "ipv6": ipv6}
            if primary_interface == "N/A" and (ipv4 or ipv6):
                primary_interface = iface_name
    except Exception:
        pass

    try:
        stats = psutil.net_if_stats()
        if primary_interface in stats:
            primary_status = "UP" if stats[primary_interface].isup else "DOWN"
    except Exception:
        pass

    return {
        "interfaces": interfaces,
        "primary_interface": primary_interface,
        "primary_status": primary_status,
    }


# Previous I/O state for calculating realtime network rate
_last_net_io: tuple[float, int, int] | None = None


def get_network_traffic() -> dict:
    """
    Collects realtime upload/download rates and cumulative traffic.
    Gracefully falls back to 'Unavailable' if /proc/net/dev is restricted (PRoot/Docker).
    """
    global _last_net_io
    now = time.time()
    try:
        counters = psutil.net_io_counters(pernic=False)
        sent = counters.bytes_sent
        recv = counters.bytes_recv

        realtime_up_mbps = 0.0
        realtime_down_mbps = 0.0

        if _last_net_io is not None:
            last_time, last_sent, last_recv = _last_net_io
            dt = max(now - last_time, 0.001)
            delta_sent = max(sent - last_sent, 0)
            delta_recv = max(recv - last_recv, 0)
            realtime_up_mbps = round((delta_sent * 8) / (dt * 1_000_000), 2)
            realtime_down_mbps = round((delta_recv * 8) / (dt * 1_000_000), 2)

        _last_net_io = (now, sent, recv)

        return {
            "realtime_upload": f"{realtime_up_mbps} Mbps",
            "realtime_download": f"{realtime_down_mbps} Mbps",
            "total_sent": humanbytes(sent),
            "total_recv": humanbytes(recv),
            "available": True,
        }
    except Exception:
        return {
            "realtime_upload": "Unavailable",
            "realtime_download": "Unavailable",
            "total_sent": "Unavailable",
            "total_recv": "Unavailable",
            "available": False,
        }


def get_bot_process_resources() -> dict:
    """
    Collects process-level CPU % and RAM usage (MB and %) of the current Python bot process.
    """
    try:
        proc = psutil.Process()
        cpu_pct = proc.cpu_percent(interval=0.05)
        mem = proc.memory_info()
        ram_mb = round(mem.rss / (1024 * 1024), 1)
        ram_pct = round(proc.memory_percent(), 1)
        return {
            "cpu": f"{cpu_pct}%",
            "ram": f"{ram_mb} MB ({ram_pct}%)",
            "ram_mb": ram_mb,
            "ram_pct": ram_pct,
            "cpu_pct": cpu_pct,
            "available": True,
        }
    except Exception:
        return {
            "cpu": "Unavailable",
            "ram": "Unavailable",
            "ram_mb": 0.0,
            "ram_pct": 0.0,
            "cpu_pct": 0.0,
            "available": False,
        }


def get_system_info() -> dict:
    """
    Aggregates all synchronous VPS information into a single dictionary.
    """
    os_name = get_os_info()
    kernel = get_kernel_info()
    arch = get_arch_info()
    hostname = get_hostname()
    cpu = get_cpu_info()
    ram = get_ram_info()
    disk = get_disk_info("/")
    uptime = get_uptime_info()
    local_net = get_local_network_info()
    traffic = get_network_traffic()

    return {
        "hostname": hostname,
        "is_container": is_container_environment(),
        "os": os_name,
        "kernel": kernel,
        "arch": arch,
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "uptime": uptime,
        "local_net": local_net,
        "traffic": traffic,
    }


async def get_network_info(
    session: aiohttp.ClientSession | None = None,
    force_refresh: bool = False,
    timeout_seconds: float = 3.5,
) -> dict:
    """
    Dynamically detects public IPv4, public IPv6, ISP, ASN, and VPS location with
    bounded timeouts and safe fallbacks. Never blocks event loop and caches results.
    """
    global _cached_network_info, _cached_network_time
    now = time.time()
    if not force_refresh and _cached_network_info and (now - _cached_network_time < NETWORK_CACHE_TTL):
        return _cached_network_info

    res = {
        "ipv4": "Unavailable",
        "ipv6": "Unavailable",
        "isp": "Unknown",
        "asn": "Unknown",
        "location": "Unknown",
        "interface": "N/A",
        "interface_status": "Active",
    }

    local_net = get_local_network_info()
    res["interface"] = local_net.get("primary_interface", "N/A")
    res["interface_status"] = local_net.get("primary_status", "Active")

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    close_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=timeout)
        close_session = True

    try:
        # 1. Primary: ip-api.com (provides IPv4, ISP, ASN, and Location in one fast call)
        try:
            async with session.get("http://ip-api.com/json/?fields=status,country,city,isp,org,as,query") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, dict) and data.get("status") == "success":
                        query_ip = str(data.get("query") or "").strip()
                        if ":" in query_ip:
                            res["ipv6"] = query_ip
                        elif query_ip:
                            res["ipv4"] = query_ip

                        isp_val = str(data.get("isp") or data.get("org") or "").strip()
                        if isp_val:
                            res["isp"] = isp_val

                        raw_as = str(data.get("as") or "").strip()
                        as_match = re.search(r"(AS\d+)", raw_as)
                        if as_match:
                            res["asn"] = as_match.group(1)
                        elif raw_as:
                            res["asn"] = raw_as.split()[0]

                        city = str(data.get("city") or "").strip()
                        country = str(data.get("country") or "").strip()
                        if city and country:
                            res["location"] = f"{city}, {country}"
                        elif country:
                            res["location"] = country
        except Exception:
            pass

        # 2. Fallback for IPv4 if ip-api.com was unavailable or returned empty
        if res["ipv4"] == "Unavailable":
            fallback_ipv4_urls = [
                "https://api.ipify.org?format=json",
                "https://ifconfig.me/ip",
                "https://icanhazip.com",
            ]
            for url in fallback_ipv4_urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            if "json" in url:
                                d = await resp.json()
                                ip_candidate = str(d.get("ip") or "").strip()
                            else:
                                ip_candidate = (await resp.text()).strip()
                            if ip_candidate and ":" not in ip_candidate:
                                res["ipv4"] = ip_candidate
                                break
                except Exception:
                    continue

        # 3. Fallback for IPv6 check if still unavailable
        if res["ipv6"] == "Unavailable":
            fallback_ipv6_urls = [
                "https://api64.ipify.org?format=json",
                "https://v6.ident.me",
            ]
            for url in fallback_ipv6_urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            if "json" in url:
                                d = await resp.json()
                                ip_candidate = str(d.get("ip") or "").strip()
                            else:
                                ip_candidate = (await resp.text()).strip()
                            if ip_candidate and ":" in ip_candidate:
                                res["ipv6"] = ip_candidate
                                break
                except Exception:
                    continue

    finally:
        if close_session:
            await session.close()

    _cached_network_info = res
    _cached_network_time = time.time()
    return res


async def get_public_ip(force_refresh: bool = False) -> str:
    """
    Returns public IPv4 address or 'Unavailable'.
    """
    net_info = await get_network_info(force_refresh=force_refresh)
    return net_info.get("ipv4", "Unavailable")


def clear_network_cache() -> None:
    """
    Clears the cached network information.
    """
    global _cached_network_info, _cached_network_time
    _cached_network_info = None
    _cached_network_time = 0.0
