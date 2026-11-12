import asyncio
from collections import deque
import datetime
import html
import threading
import time
from typing import Any
import speedtest
from FileStream.bot.messages import AdminMessages, LANG
from FileStream.core.runtime.human_readable import humanbytes

MSG_SPEEDTEST_START = AdminMessages.SPEEDTEST_START
MSG_SPEEDTEST_ERROR = AdminMessages.SPEEDTEST_ERROR
MSG_SPEEDTEST_TIMEOUT = getattr(AdminMessages, "SPEEDTEST_TIMEOUT", "⚠️ <b>Speed Test Timeout</b>\n\nThe network test did not complete within the allowed time.")

# In-memory storage for latest speed test result across admin panel refreshes
_latest_speedtest_result: dict | None = None
_last_speedtest_at: float = 0.0
SPEEDTEST_COOLDOWN = 60  # seconds between full speed tests

# Bounded in-memory history of recent speed test results (last 10)
_speedtest_history: deque[dict] = deque(maxlen=10)


def add_speedtest_history(result: dict) -> None:
    """Adds a valid speed test result to bounded history."""
    if isinstance(result, dict) and "error" not in result:
        _speedtest_history.append(result)


def get_speedtest_history() -> list[dict]:
    """Returns the list of recent speed tests, newest first."""
    return list(reversed(_speedtest_history))


def clear_speedtest_history() -> None:
    """Clears the in-memory speed test history."""
    _speedtest_history.clear()


def get_latest_speedtest() -> dict | None:
    """Returns the most recent speed test result or None if not run yet."""
    return _latest_speedtest_result


def set_latest_speedtest(result: dict | None) -> None:
    """Updates the cached speed test result, records the timestamp, and appends to history."""
    global _latest_speedtest_result, _last_speedtest_at
    _last_speedtest_at = time.time()
    if result is None:
        _latest_speedtest_result = None
        return
    if isinstance(result, dict) and "error" not in result:
        _latest_speedtest_result = result
        add_speedtest_history(result)


def format_speedtest_empty() -> str:
    """Returns the empty/no-results-yet screen for the speed test view."""
    return (
        "⚡ <b>Speed Test</b>\n\n"
        "📊 <b>No Speed Test Results Yet</b>\n\n"
        "No completed speed test is available.\n\n"
        "Run a speed test to measure:\n"
        "• Download speed\n"
        "• Upload speed\n"
        "• Ping\n"
        "• Speedtest server\n"
        "• Network details"
    )


def format_speedtest_failure(reason: str, has_previous: bool = False) -> str:
    """Formats a user-friendly speed test failure message."""
    history_line = "\n\nThe previous completed result remains available in History." if has_previous else ""
    return (
        "⚡ <b>Speed Test Failed</b>\n\n"
        "Unable to complete the speed test.\n\n"
        f"Reason:\n<code>{html.escape(str(reason))}</code>"
        f"{history_line}"
    )


def get_last_speedtest_at() -> float:
    """Returns the timestamp of the last speed test."""
    return _last_speedtest_at


def evaluate_network_quality(download_mbps: float, upload_mbps: float, ping_ms: float) -> str:
    """
    Evaluates network quality based on standard broadband and latency benchmarks:
    - Excellent: Ping <= 30 ms, Download >= 100 Mbps, Upload >= 50 Mbps
    - Good: Ping <= 80 ms, Download >= 25 Mbps, Upload >= 10 Mbps
    - Fair: Ping <= 150 ms, Download >= 10 Mbps, Upload >= 2 Mbps
    - Poor: Otherwise (high latency or low throughput)
    """
    try:
        ping = float(ping_ms)
        dl = float(download_mbps)
        ul = float(upload_mbps)
    except (ValueError, TypeError):
        return "Unknown"

    if ping <= 30 and dl >= 100 and ul >= 50:
        return "Excellent"
    if ping <= 80 and dl >= 25 and ul >= 10:
        return "Good"
    if ping <= 150 and dl >= 10 and ul >= 2:
        return "Fair"
    return "Poor"


def _run_speedtest(shutdown_event: threading.Event | None = None) -> dict:
    """
    Executes speedtest-cli in a synchronous worker thread.
    Selects the best server automatically by latency, measures download and upload,
    and supports cancellation via shutdown_event.
    """
    st = speedtest.Speedtest(secure=True, timeout=15, shutdown_event=shutdown_event)
    try:
        st.get_servers([])
    except Exception:
        pass

    if shutdown_event and shutdown_event.is_set():
        return {"error": "cancelled", "message": "Speed test cancelled"}

    try:
        st.get_best_server()
    except Exception:
        if getattr(st, "servers", None):
            for servers in st.servers.values():
                if servers:
                    st.best = servers[0]
                    break
        if not getattr(st, "best", None):
            raise

    if shutdown_event and shutdown_event.is_set():
        return {"error": "cancelled", "message": "Speed test cancelled"}

    st.download(threads=4)

    if shutdown_event and shutdown_event.is_set():
        return {"error": "cancelled", "message": "Speed test cancelled"}

    st.upload(pre_allocate=False, threads=4)

    try:
        share_url = st.results.share()
    except Exception:
        share_url = None

    results = st.results.dict()
    if share_url:
        results["share"] = share_url
    return results


async def run_speedtest(
    retries: int = 1,
    delay: int = 2,
    timeout: float = 45.0,
) -> dict:
    """
    Asynchronous runner for speedtest with bounded timeout and shutdown_event protection.
    Prevents blocking the Telegram event loop and signals worker threads to abort on timeout.
    """
    loop = asyncio.get_running_loop()
    last_error = None

    for attempt in range(retries + 1):
        shutdown_event = threading.Event()
        try:
            test_coro = loop.run_in_executor(None, _run_speedtest, shutdown_event)
            result = await asyncio.wait_for(test_coro, timeout=timeout)
            set_latest_speedtest(result)
            return result
        except asyncio.TimeoutError:
            shutdown_event.set()
            last_error = TimeoutError("Speed test timed out")
            break
        except Exception as e:
            shutdown_event.set()
            last_error = e
            if attempt < retries:
                await asyncio.sleep(delay)
            continue

    raise last_error


def _format_time_compact(timestamp_raw: str | None) -> str:
    """
    Formats timestamps explicitly as UTC (e.g. '07:18 AM UTC') matching project conventions.
    """
    if not timestamp_raw:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%I:%M %p UTC")
    try:
        raw_str = str(timestamp_raw).strip()
        if raw_str.endswith("Z"):
            raw_str = raw_str[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(raw_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.strftime("%I:%M %p UTC")
    except Exception:
        clean_fallback = str(timestamp_raw)[:16].replace("T", " ")
        return f"{clean_fallback} UTC"


def format_speedtest(result: dict) -> str:
    """
    Formats the speed test result into a compact, mobile-friendly Telegram HTML message.
    """
    if not isinstance(result, dict):
        return AdminMessages.SPEEDTEST_ERROR

    if "error" in result:
        reason = result.get("message") or result.get("error") or "Unknown error"
        return f"⚡ <b>Speed Test Failed</b>\n\nReason: <code>{html.escape(str(reason))}</code>"

    download_bps = result.get("download", 0) / 8
    upload_bps = result.get("upload", 0) / 8

    server = result.get("server", {}) or {}
    client = result.get("client", {}) or {}

    download_mbps = round(result.get("download", 0) / 1_000_000, 2)
    upload_mbps = round(result.get("upload", 0) / 1_000_000, 2)
    ping = round(result.get("ping", 0), 2)
    raw_timestamp = result.get("timestamp")
    time_display = _format_time_compact(raw_timestamp)
    bytes_sent = humanbytes(result.get("bytes_sent", 0))
    bytes_received = humanbytes(result.get("bytes_received", 0))

    server_name = server.get("name", "Unknown")
    server_country = server.get("country", "")
    server_text = f"{server_name}, {server_country}".strip(", ") if server_country else server_name

    quality = evaluate_network_quality(download_mbps, upload_mbps, ping)

    return (
        "⚡ <b>Speed Test</b>\n\n"
        f"↓ <code>{download_mbps} Mbps</code>\n"
        f"↑ <code>{upload_mbps} Mbps</code>\n"
        f"Ping: <code>{ping} ms</code>\n"
        f"Quality: <code>{quality}</code>\n\n"
        f"Server: <code>{html.escape(server_text)}</code>\n"
        f"ISP: <code>{html.escape(client.get('isp', 'Unknown'))}</code>\n"
        f"Data: <code>{bytes_received} / {bytes_sent}</code>\n\n"
        f"🕐 Last test: <code>{time_display}</code>"
    )


def format_speedtest_detailed(result: dict) -> str:
    """
    Formats the speed test result into a dedicated, rich, mobile-friendly Telegram HTML message
    with separated SPEEDTEST INFO, SERVER INFO, and CLIENT DETAILS.
    """
    if not isinstance(result, dict):
        return AdminMessages.SPEEDTEST_ERROR

    if "error" in result:
        reason = result.get("message") or result.get("error") or "Unknown error"
        has_prev = bool(get_latest_speedtest() or get_speedtest_history())
        return format_speedtest_failure(reason, has_previous=has_prev)

    def _val(v: Any, default: str = "Unavailable") -> str:
        if v is None or v == "" or str(v).strip().lower() in {"none", "null"}:
            return default
        return str(v).strip()

    # 1. SPEEDTEST INFO
    dl_raw = float(result.get("download") or 0.0)
    ul_raw = float(result.get("upload") or 0.0)
    dl_mbps = round(dl_raw / 1_000_000, 2)
    dl_mbs = round((dl_raw / 8) / (1024 * 1024), 2)
    ul_mbps = round(ul_raw / 1_000_000, 2)
    ul_mbs = round((ul_raw / 8) / (1024 * 1024), 2)

    ping_val = result.get("ping")
    ping_disp = f"{round(float(ping_val), 2)} ms" if ping_val is not None else "Unavailable"
    time_disp = _format_time_compact(result.get("timestamp"))

    sent_raw = result.get("bytes_sent")
    recv_raw = result.get("bytes_received")
    sent_disp = humanbytes(sent_raw) if sent_raw is not None else "Unavailable"
    recv_disp = humanbytes(recv_raw) if recv_raw is not None else "Unavailable"

    # 2. SERVER INFO
    server = result.get("server", {}) or {}
    srv_name = html.escape(_val(server.get("name")))
    srv_country = html.escape(_val(server.get("country")))
    srv_sponsor = html.escape(_val(server.get("sponsor")))
    srv_lat_val = server.get("latency")
    srv_latency = f"{round(float(srv_lat_val), 2)} ms" if srv_lat_val is not None else "Unavailable"

    dist_val = server.get("d")
    srv_dist = f"{round(float(dist_val), 1)} km" if dist_val is not None else "Unavailable"

    s_lat = server.get("lat")
    s_lon = server.get("lon")
    srv_coords = f"{s_lat}, {s_lon}" if s_lat is not None and s_lon is not None else "Unavailable"

    # 3. CLIENT DETAILS
    client = result.get("client", {}) or {}
    cli_ip = html.escape(_val(client.get("ip")))
    cli_isp = html.escape(_val(client.get("isp")))
    cli_rating = html.escape(_val(client.get("isprating")))
    cli_country = html.escape(_val(client.get("country")))

    c_lat = client.get("lat")
    c_lon = client.get("lon")
    cli_coords = f"{c_lat}, {c_lon}" if c_lat is not None and c_lon is not None else "Unavailable"

    return (
        "⚡ <b>Speed Test</b>\n\n"
        "📊 <b>Speedtest Info</b>\n"
        f"🔻 Download: <code>{dl_mbps} Mbps ({dl_mbs} MB/s)</code>\n"
        f"🔺 Upload: <code>{ul_mbps} Mbps ({ul_mbs} MB/s)</code>\n"
        f"🏓 Ping: <code>{ping_disp}</code>\n"
        f"🕒 Time: <code>{time_disp}</code>\n"
        f"📦 Data: <code>{sent_disp} sent / {recv_disp} recv</code>\n\n"
        "🖥 <b>Server Info</b>\n"
        f"Name: <code>{srv_name}</code>\n"
        f"Country: <code>{srv_country}</code>\n"
        f"Sponsor: <code>{srv_sponsor}</code>\n"
        f"Latency: <code>{srv_latency}</code>\n"
        f"Distance: <code>{srv_dist}</code>\n"
        f"Coordinates: <code>{srv_coords}</code>\n\n"
        "👤 <b>Client Details</b>\n"
        f"IP: <code>{cli_ip}</code>\n"
        f"Coordinates: <code>{cli_coords}</code>\n"
        f"ISP: <code>{cli_isp}</code>\n"
        f"Rating: <code>{cli_rating}</code>\n"
        f"Country: <code>{cli_country}</code>"
    )


def format_speedtest_history() -> str:
    """
    Formats recent in-memory speedtest history into a readable list.
    """
    history = get_speedtest_history()
    if not history:
        return (
            "📜 <b>Speed Test History</b>\n\n"
            "<i>No previous speed tests recorded yet.</i>"
        )

    lines = ["📜 <b>Speed Test History</b>\n"]
    for idx, item in enumerate(history, 1):
        dl_raw = float(item.get("download") or 0.0)
        ul_raw = float(item.get("upload") or 0.0)
        dl = round(dl_raw / 1_000_000, 2)
        ul = round(ul_raw / 1_000_000, 2)
        ping = round(float(item.get("ping") or 0.0), 2)
        ts = _format_time_compact(item.get("timestamp"))
        srv = item.get("server", {}).get("name", "Unknown")
        lines.append(
            f"<b>#{idx}</b> <code>{ts}</code>\n"
            f"  ↓ <code>{dl} Mbps</code> • ↑ <code>{ul} Mbps</code> • 🏓 <code>{ping} ms</code>\n"
            f"  Server: <code>{html.escape(str(srv))}</code>"
        )
    return "\n\n".join(lines)


def format_system_status(
    vps_info: dict,
    net_info: dict | None = None,
    speed_info: dict | None = None,
) -> str:
    """
    Renders the host/VPS/server status view:
    - Server: Hostname / Container, OS, Kernel, CPU, RAM, Uptime
    - Storage: Total, Used, Free (Percentage)
    - Realtime Network: Upload / Download rates
    - Cumulative Traffic: Total uploaded / downloaded
    """
    is_container = bool(vps_info.get("is_container", False))
    host_label = "Container" if is_container else "Hostname"
    hostname = html.escape(str(vps_info.get("hostname", "localhost")))
    os_name = html.escape(str(vps_info.get("os", "Linux")))
    kernel = html.escape(str(vps_info.get("kernel", "Unknown")))

    cpu = vps_info.get("cpu", {}) or {}
    cpu_model = cpu.get("model")
    cpu_cores = cpu.get("cores", 1)
    cpu_usage = cpu.get("usage_percent", 0.0)
    if cpu_model:
        cpu_text = html.escape(f"{cpu_model} ({cpu_cores} cores) • {cpu_usage}%")
    else:
        cpu_disp = cpu.get("display")
        if cpu_disp:
            cpu_text = html.escape(f"{cpu_disp} • {cpu_usage}%")
        else:
            cpu_text = html.escape(f"{cpu_cores} logical cores • {cpu_usage}%")

    ram = vps_info.get("ram", {}) or {}
    ram_used = ram.get("used", "0 B")
    ram_total = ram.get("total", "0 B")
    ram_pct = ram.get("percent")
    if ram_pct is not None:
        ram_text = f"{ram_used} / {ram_total} ({ram_pct}%)"
    else:
        ram_text = ram.get("display", "N/A")

    uptime_disp = html.escape(str(vps_info.get("uptime", {}).get("display", "0s")))

    disk = vps_info.get("disk", {}) or {}
    disk_total = html.escape(str(disk.get("total", "0 B")))
    disk_used = html.escape(str(disk.get("used", "0 B")))
    disk_free = html.escape(str(disk.get("free", "0 B")))
    disk_pct = disk.get("percent")
    if disk_pct is not None:
        disk_used_disp = f"{disk_used} ({disk_pct}%)"
    else:
        disk_used_disp = disk_used

    traffic = vps_info.get("traffic", {}) or {}
    rt_up = html.escape(str(traffic.get("realtime_upload", "Unavailable")))
    rt_down = html.escape(str(traffic.get("realtime_download", "Unavailable")))
    tot_up = html.escape(str(traffic.get("total_sent", "Unavailable")))
    tot_down = html.escape(str(traffic.get("total_recv", "Unavailable")))

    server_block = (
        "🖥 <b>Server</b>\n"
        f"{host_label}: <code>{hostname}</code>\n"
        f"OS: <code>{os_name}</code>\n"
        f"Kernel: <code>{kernel}</code>\n"
        f"CPU: <code>{cpu_text}</code>\n"
        f"RAM: <code>{ram_text}</code>\n"
        f"Uptime: <code>{uptime_disp}</code>"
    )

    storage_block = (
        "💾 <b>Storage</b>\n"
        f"Total: <code>{disk_total}</code>\n"
        f"Used: <code>{disk_used_disp}</code>\n"
        f"Free: <code>{disk_free}</code>"
    )

    network_block = (
        "🌐 <b>Realtime Network</b>\n"
        f"🔺 Upload: <code>{rt_up}</code>\n"
        f"🔻 Download: <code>{rt_down}</code>"
    )

    traffic_block = (
        "📈 <b>Cumulative Traffic</b>\n"
        f"🔺 Uploaded: <code>{tot_up}</code>\n"
        f"🔻 Downloaded: <code>{tot_down}</code>"
    )

    return (
        "📊 <b>System Status</b>\n\n"
        f"{server_block}\n\n"
        f"{storage_block}\n\n"
        f"{network_block}\n\n"
        f"{traffic_block}"
    )
