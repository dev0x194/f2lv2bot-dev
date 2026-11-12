"""Compatibility facade for FileStream.core.runtime.flog_channels.

This module re-exports all symbols from FileStream.core.runtime.flog_channels and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.flog_channels')
from FileStream.core.runtime.flog_channels import *

sys.modules[__name__] = _canonical
