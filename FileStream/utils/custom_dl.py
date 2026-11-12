"""Compatibility facade for FileStream.core.media.custom_dl.

This module re-exports all symbols from FileStream.core.media.custom_dl and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.media.custom_dl')
from FileStream.core.media.custom_dl import *

sys.modules[__name__] = _canonical
