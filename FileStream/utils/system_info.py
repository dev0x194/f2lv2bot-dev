"""Compatibility facade for FileStream.core.monitoring.system_info.

This module re-exports all symbols from FileStream.core.monitoring.system_info and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.system_info')
from FileStream.core.monitoring.system_info import *

sys.modules[__name__] = _canonical
