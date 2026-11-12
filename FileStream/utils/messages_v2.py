"""Compatibility shim for FileStream.utils.messages_v2.

Canonical Telegram bot and web messages live in FileStream.bot.messages.
"""

from FileStream.bot.messages import (
    AdminMessages,
    DonationMessages,
    ErrorMessages,
    FileMessages,
    FolderMessages,
    PublicMessages,
    SafetyMessages,
    ForceSubMessages,
    ChannelLogMessages,
    WebMessages,
    LANG,
)

__all__ = [
    "AdminMessages",
    "DonationMessages",
    "ErrorMessages",
    "FileMessages",
    "FolderMessages",
    "PublicMessages",
    "SafetyMessages",
    "ForceSubMessages",
    "ChannelLogMessages",
    "WebMessages",
    "LANG",
]
