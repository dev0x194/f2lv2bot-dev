"""Compatibility facade for FileStream.bot.helpers.translation.

Canonical implementation lives in FileStream.bot.helpers.translation.
"""

import sys
import importlib

_canonical = importlib.import_module("FileStream.bot.helpers.translation")
from FileStream.bot.helpers.translation import *

sys.modules[__name__] = _canonical
