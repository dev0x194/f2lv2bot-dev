"""Monitoring, metrics, alerting, and system telemetry modules."""

__all__ = [
    "MetricsCollector",
    "get_metrics",
    "calculate_percentiles",
    "AlertSeverity",
    "Alert",
    "AlertManager",
    "get_alert_manager",
    "AuditTaskManager",
    "audit_manager",
    "AdminActionEvent",
    "log_admin_action",
    "get_recent_admin_actions",
    "clear_admin_audit_log_for_testing",
    "redact_secrets",
    "ParsedLogRecord",
    "tail_log_file",
    "format_records_for_telegram",
    "get_logs_for_category",
    "format_uptime",
    "get_os_info",
    "get_kernel_info",
    "get_arch_info",
    "is_container_environment",
    "get_hostname",
    "get_cpu_info",
    "get_ram_info",
    "get_disk_info",
    "get_uptime_info",
    "get_local_network_info",
    "get_network_traffic",
    "get_bot_process_resources",
    "get_system_info",
    "get_network_info",
    "get_public_ip",
    "clear_network_cache",
    "run_speedtest",
    "add_speedtest_history",
    "get_speedtest_history",
    "clear_speedtest_history",
    "get_latest_speedtest",
    "set_latest_speedtest",
    "format_speedtest",
    "format_speedtest_detailed",
    "format_speedtest_history",
    "format_system_status",
]


def __getattr__(name: str):
    if name in ("MetricsCollector", "get_metrics", "calculate_percentiles"):
        from . import metrics
        return getattr(metrics, name)
    if name in ("AlertSeverity", "Alert", "AlertManager", "get_alert_manager"):
        from . import alert_manager
        return getattr(alert_manager, name)
    if name in ("AuditTaskManager", "audit_manager"):
        from . import audit_manager
        return getattr(audit_manager, name)
    if name in ("AdminActionEvent", "log_admin_action", "get_recent_admin_actions", "clear_admin_audit_log_for_testing"):
        from . import admin_audit
        return getattr(admin_audit, name)
    if name in ("redact_secrets", "ParsedLogRecord", "tail_log_file", "format_records_for_telegram", "get_logs_for_category"):
        from . import log_reader
        return getattr(log_reader, name)
    if name in (
        "format_uptime", "get_os_info", "get_kernel_info", "get_arch_info", "is_container_environment",
        "get_hostname", "get_cpu_info", "get_ram_info", "get_disk_info", "get_uptime_info",
        "get_local_network_info", "get_network_traffic", "get_bot_process_resources", "get_system_info",
        "get_network_info", "get_public_ip", "clear_network_cache"
    ):
        from . import system_info
        return getattr(system_info, name)
    if name in (
        "run_speedtest", "add_speedtest_history", "get_speedtest_history", "clear_speedtest_history",
        "get_latest_speedtest", "set_latest_speedtest", "format_speedtest", "format_speedtest_detailed",
        "format_speedtest_history", "format_system_status"
    ):
        from . import speedtest
        return getattr(speedtest, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
