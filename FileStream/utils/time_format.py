"""Compatibility facade for FileStream.core.runtime.time_format.

Canonical implementation lives in FileStream.core.runtime.time_format.
"""

import sys
import importlib

_canonical = importlib.import_module("FileStream.core.runtime.time_format")
from FileStream.core.runtime.time_format import *

sys.modules[__name__] = _canonical
