"""Compatibility facade for FileStream.core.runtime.client_balance.

This module re-exports all symbols from FileStream.core.runtime.client_balance and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.client_balance')
from FileStream.core.runtime.client_balance import *

sys.modules[__name__] = _canonical
