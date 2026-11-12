"""Resilience, circuit breaking, flood control, and disaster recovery modules."""

__all__ = [
    "CircuitBreakerState",
    "ClientCircuitBreaker",
    "CircuitBreakerRegistry",
    "get_circuit_breaker_registry",
    "is_floodwait",
    "get_floodwait_delay",
    "call_with_floodwait",
    "WorkloadTracker",
    "workload_tracker",
    "verify_database_schema",
    "emergency_recover_all_inflight_jobs",
    "verify_backup_directory",
    "generate_dr_status_report",
    "FailureType",
    "FailureConfig",
    "InjectedFloodWait",
    "FailureInjector",
    "get_failure_injector",
    "create_faulty_client",
    "SoakTestResult",
    "SoakTestRunner",
    "create_security_middleware",
    "get_load_shedder",
    "get_ip_rate_limiter",
    "get_security_hardener",
    "is_suspicious_path",
    "sanitize_download_path",
    "LoadShedder",
    "IPRateLimiter",
    "SecurityHardener",
]


def __getattr__(name: str):
    if name in ("CircuitBreakerState", "ClientCircuitBreaker", "CircuitBreakerRegistry", "get_circuit_breaker_registry"):
        from . import circuit_breaker
        return getattr(circuit_breaker, name)
    if name in ("is_floodwait", "get_floodwait_delay", "call_with_floodwait"):
        from . import floodwait
        return getattr(floodwait, name)
    if name in ("WorkloadTracker", "workload_tracker"):
        from . import workload
        return getattr(workload, name)
    if name in ("create_security_middleware", "get_load_shedder", "get_ip_rate_limiter", "get_security_hardener", "is_suspicious_path", "sanitize_download_path", "LoadShedder", "IPRateLimiter", "SecurityHardener"):
        from . import load_shedding
        return getattr(load_shedding, name)
    if name in ("verify_database_schema", "emergency_recover_all_inflight_jobs", "verify_backup_directory", "generate_dr_status_report"):
        from . import disaster_recovery
        return getattr(disaster_recovery, name)
    if name in ("FailureType", "FailureConfig", "InjectedFloodWait", "FailureInjector", "get_failure_injector", "create_faulty_client", "SoakTestResult", "SoakTestRunner"):
        from . import testing_harness
        return getattr(testing_harness, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
