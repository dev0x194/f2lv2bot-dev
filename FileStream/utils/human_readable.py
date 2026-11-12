"""Compatibility facade for FileStream.core.runtime.human_readable.

Canonical implementation lives in FileStream.core.runtime.human_readable.
"""

import sys
import importlib

_canonical = importlib.import_module("FileStream.core.runtime.human_readable")
from FileStream.core.runtime.human_readable import *

sys.modules[__name__] = _canonical
