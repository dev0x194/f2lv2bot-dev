"""Compatibility facade for FileStream.core.monitoring.speedtest.

This module re-exports all symbols from FileStream.core.monitoring.speedtest and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.speedtest')
from FileStream.core.monitoring.speedtest import *

sys.modules[__name__] = _canonical
