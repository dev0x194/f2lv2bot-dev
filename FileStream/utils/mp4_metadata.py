"""Compatibility facade for FileStream.core.media.mp4_metadata.

This module re-exports all symbols from FileStream.core.media.mp4_metadata and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.media.mp4_metadata')
from FileStream.core.media.mp4_metadata import *

sys.modules[__name__] = _canonical
