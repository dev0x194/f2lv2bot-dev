"""Compatibility facade for FileStream.core.monitoring.admin_audit.

This module re-exports all symbols from FileStream.core.monitoring.admin_audit and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.admin_audit')
from FileStream.core.monitoring.admin_audit import *

sys.modules[__name__] = _canonical
