"""Compatibility facade for FileStream.core.monitoring.audit_manager.

This module re-exports all symbols from FileStream.core.monitoring.audit_manager and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.audit_manager')
from FileStream.core.monitoring.audit_manager import *

sys.modules[__name__] = _canonical
