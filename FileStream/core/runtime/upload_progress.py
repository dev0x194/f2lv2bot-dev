import asyncio
import logging
import time
from typing import Any
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from FileStream.config import Telegram

logger = logging.getLogger(__name__)


def format_progress_bar(percent: float, length: int = 10) -> str:
    """Generate a visual ASCII progress bar."""
    pct = max(0.0, min(100.0, percent))
    filled = int(round(length * pct / 100.0))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct:.1f}%"


def format_human_bytes(size: float) -> str:
    """Format bytes to human readable format."""
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    return f"{size:.2f} {units[unit_idx]}"


class UploadProgressTracker:
    """
    Failure-isolated multi-file upload progress tracker.
    Edits a Telegram status message with throttled progress updates.
    Never lets Telegram messaging errors fail the background storage job.
    """

    def __init__(
        self,
        bot: Any,
        status_message: Any,
        total_files: int = 1,
        total_bytes: int = 0,
        title: str = "Uploading Files",
        min_interval: float | None = None,
        min_percent_change: float | None = None,
    ):
        self.bot = bot
        self.status_message = status_message
        self.total_files = max(1, int(total_files))
        self.total_bytes = max(0, int(total_bytes))
        self.title = title

        self.min_interval = (
            min_interval
            if min_interval is not None
            else getattr(Telegram, "FLOG_PROGRESS_UPDATE_INTERVAL", 3.0)
        )
        self.min_percent_change = (
            min_percent_change
            if min_percent_change is not None
            else getattr(Telegram, "FLOG_PROGRESS_MIN_PERCENT_CHANGE", 5.0)
        )

        self.completed_files: int = 0
        self.failed_files: int = 0
        self.completed_bytes: int = 0
        self.current_file_bytes: int = 0
        self.last_update_time: float = 0.0
        self.last_reported_percent: float = -1.0
        self._lock = asyncio.Lock()
        self.is_completed: bool = False

    async def update_file_progress(
        self,
        current_bytes: int,
        file_total_bytes: int,
        file_name: str = "",
    ) -> None:
        """Called periodically during chunk transfer of an individual file."""
        if self.is_completed or not self.status_message:
            return

        now = time.time()
        if (now - self.last_update_time) < self.min_interval:
            return

        async with self._lock:
            if (now - self.last_update_time) < self.min_interval:
                return

            self.current_file_bytes = current_bytes
            total_transferred = self.completed_bytes + current_bytes
            estimated_total = max(self.total_bytes, total_transferred)

            if estimated_total > 0:
                percent = (total_transferred / estimated_total) * 100.0
            else:
                percent = (self.completed_files / self.total_files) * 100.0

            if (percent - self.last_reported_percent) < self.min_percent_change and (now - self.last_update_time) < 15.0:
                return

            self.last_update_time = now
            self.last_reported_percent = percent

            bar = format_progress_bar(percent)
            text = (
                f"**{self.title}**\n\n"
                f"{bar}\n"
                f"• Files: `{self.completed_files + 1}/{self.total_files}`\n"
                f"• Transferred: `{format_human_bytes(total_transferred)} / {format_human_bytes(estimated_total)}`\n"
            )
            if file_name:
                short_name = file_name[:30] + ("..." if len(file_name) > 30 else "")
                text += f"• Current: `{short_name}`\n"

            await self._safe_edit(text)

    async def mark_file_complete(self, file_bytes: int = 0, file_name: str = "") -> None:
        """Called when an individual file upload completes successfully."""
        async with self._lock:
            self.completed_files += 1
            self.completed_bytes += max(0, file_bytes)
            self.current_file_bytes = 0
            now = time.time()
            self.last_update_time = now

            if self.completed_files + self.failed_files >= self.total_files:
                self.is_completed = True
                text = (
                    f"**{self.title} Complete**\n\n"
                    f"{format_progress_bar(100.0)}\n"
                    f"• Total Processed: `{self.completed_files}/{self.total_files}`\n"
                    f"• Uploaded: `{format_human_bytes(self.completed_bytes)}`\n"
                )
                if self.failed_files > 0:
                    text += f"• Failed: `{self.failed_files}`\n"
                await self._safe_edit(text)
            else:
                pct = (self.completed_files / self.total_files) * 100.0
                self.last_reported_percent = pct
                text = (
                    f"**{self.title}**\n\n"
                    f"{format_progress_bar(pct)}\n"
                    f"• Completed: `{self.completed_files}/{self.total_files}`\n"
                    f"• Size: `{format_human_bytes(self.completed_bytes)}`\n"
                )
                await self._safe_edit(text)

    async def mark_file_failed(self, file_name: str = "", error: str = "") -> None:
        """Called when an individual file upload fails permanently."""
        async with self._lock:
            self.failed_files += 1
            self.current_file_bytes = 0
            now = time.time()
            self.last_update_time = now

            if self.completed_files + self.failed_files >= self.total_files:
                self.is_completed = True
                text = (
                    f"**{self.title} Finished**\n\n"
                    f"• Completed: `{self.completed_files}/{self.total_files}`\n"
                    f"• Failed: `{self.failed_files}`\n"
                    f"• Uploaded: `{format_human_bytes(self.completed_bytes)}`\n"
                )
                await self._safe_edit(text)

    async def _safe_edit(self, text: str) -> None:
        """Safely edit the status message, completely isolating failures."""
        if not self.status_message:
            return
        try:
            if hasattr(self.status_message, "edit_text"):
                await self.status_message.edit_text(text)
            elif hasattr(self.bot, "edit_message_text"):
                chat_id = getattr(self.status_message, "chat", None)
                cid = chat_id.id if chat_id else getattr(self.status_message, "chat_id", None)
                mid = getattr(self.status_message, "id", None) or getattr(self.status_message, "message_id", None)
                if cid and mid:
                    await self.bot.edit_message_text(chat_id=cid, message_id=mid, text=text)
        except FloodWait as fw:
            logger.debug("[PROGRESS] FloodWait on message edit: %s (ignoring)", fw)
        except MessageNotModified:
            pass
        except RPCError as rpc:
            logger.debug("[PROGRESS] RPC error on message edit: %s (ignoring)", rpc)
        except Exception as ex:
            logger.debug("[PROGRESS] Unexpected error editing status message: %s (ignoring)", ex)
