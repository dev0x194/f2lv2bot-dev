"""Compatibility facade for FileStream.core.resilience.load_shedding.

Load shedding has been relocated to FileStream.core.resilience.load_shedding.
This facade re-exports all symbols to maintain full backwards compatibility.
"""
from __future__ import annotations

import sys
from FileStream.core.resilience.load_shedding import *  # noqa: F401, F403
import FileStream.core.resilience.load_shedding as _canonical

__all__ = getattr(_canonical, "__all__", [k for k in dir(_canonical) if not k.startswith("_")])
