"""
Automated WebP Storyboard & Scrub Thumbnail Generator.

Extracts periodic frames from video, generates a 10x10 consolidated WebP
sprite sheet (160x90 per tile), and produces standard WebVTT preview files
for Plyr.js previewThumbnails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from FileStream.config import Server
from FileStream.core.media.mp4_metadata import mp4_metadata_manager

logger = logging.getLogger(__name__)


def format_vtt_timestamp(seconds: float) -> str:
    """Formats seconds into HH:MM:SS.mmm format for WebVTT."""
    s = max(0.0, float(seconds))
    hrs = int(s // 3600)
    mins = int((s % 3600) // 60)
    secs = int(s % 60)
    millis = int(round((s - int(s)) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{hrs:02d}:{mins:02d}:{secs:02d}.{millis:03d}"


class ThumbnailService:
    """Manages generation, caching, and serving of WebP sprite sheets and WebVTT."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir or Server.STREAM_CACHE_DIR or "/tmp/filestream_stream_cache") / "thumbnails"
        self._lock = asyncio.Lock()
        self._in_progress: dict[str, asyncio.Future] = {}
        with contextlib.suppress(Exception):
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _file_dir(self, file_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() else "_" for c in str(file_id or "").strip()) or "file"
        return self.cache_dir / safe_id

    def has_thumbnails(self, file_id: str) -> bool:
        dir_path = self._file_dir(file_id)
        sprite_path = dir_path / "sprites.webp"
        vtt_path = dir_path / "thumbnails.vtt"
        return sprite_path.exists() and vtt_path.exists() and sprite_path.stat().st_size > 0

    def is_generating(self, file_id: str) -> bool:
        return file_id in self._in_progress

    def get_vtt_content(self, file_id: str, token: str) -> Optional[str]:
        dir_path = self._file_dir(file_id)
        vtt_path = dir_path / "thumbnails.vtt"
        if not vtt_path.exists():
            return None

        try:
            content = vtt_path.read_text(encoding="utf-8")
            # Replace placeholder or ensure correct token in thumbnail URI
            return content.replace("{token}", token)
        except Exception as e:
            logger.warning("Error reading thumbnails.vtt for %s: %s", file_id, e)
            return None

    def get_sprite_path(self, file_id: str) -> Optional[Path]:
        dir_path = self._file_dir(file_id)
        sprite_path = dir_path / "sprites.webp"
        if sprite_path.exists() and sprite_path.stat().st_size > 0:
            return sprite_path
        return None

    def get_poster_path(self, file_id: str) -> Optional[Path]:
        dir_path = self._file_dir(file_id)
        poster_path = dir_path / "poster.jpg"
        if poster_path.exists() and poster_path.stat().st_size > 0:
            return poster_path
        return None

    def has_poster(self, file_id: str) -> bool:
        return self.get_poster_path(file_id) is not None

    async def generate_poster(
        self,
        file_id: str,
        byte_streamer,
        tg_file_id,
        client_index: int,
        file_size: int,
        duration: Optional[float] = None,
    ) -> Optional[Path]:
        """Extract a canonical, high-quality, non-black video poster frame."""
        existing = self.get_poster_path(file_id)
        if existing:
            return existing

        dir_path = self._file_dir(file_id)
        dir_path.mkdir(parents=True, exist_ok=True)
        poster_path = dir_path / "poster.jpg"

        dur = float(duration or 0)
        # Smart seek: seek 5-8% into video to avoid black opening slate/logo
        if dur > 20.0:
            seek_pos = min(max(5.0, dur * 0.08), 30.0)
        elif dur > 5.0:
            seek_pos = 2.0
        else:
            seek_pos = 0.5

        cmd = [
            "ffmpeg",
            "-v", "error",
            "-ss", f"{seek_pos:.2f}",
            "-i", "pipe:0",
            "-frames:v", "1",
            "-vf", "scale=min(1280\\,iw):-2",
            "-q:v", "3",
            "-y",
            str(poster_path),
        ]

        proc = None
        feeder_task = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def feed_pipe():
                try:
                    stream_gen = mp4_metadata_manager.virtual_faststart_stream(
                        byte_streamer=byte_streamer,
                        file_key=file_id,
                        file_id=tg_file_id,
                        client_index=client_index,
                        file_size=file_size,
                    )
                    async for chunk in stream_gen:
                        if proc.returncode is not None:
                            break
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as ex:
                    logger.debug("Poster feeder error for %s: %s", file_id, ex)
                finally:
                    with contextlib.suppress(Exception):
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.close()
                            await proc.stdin.wait_closed()

            feeder_task = asyncio.create_task(feed_pipe())
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=25.0)

            if proc.returncode == 0 and poster_path.exists() and poster_path.stat().st_size > 0:
                logger.info("Canonical poster extracted for %s at seek %.1fs", file_id, seek_pos)
                return poster_path
        except Exception as e:
            logger.debug("Poster extraction failed for %s: %s", file_id, e)
            if proc and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        finally:
            if feeder_task and not feeder_task.done():
                feeder_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feeder_task

        return None


    async def ensure_thumbnails(
        self,
        file_id: str,
        byte_streamer,
        tg_file_id,
        client_index: int,
        file_size: int,
        duration: Optional[int] = None,
    ) -> bool:
        """
        Ensure thumbnail sprite and VTT exist, spawning a background FFmpeg job if needed.
        """
        if not getattr(Server, "THUMBNAILS_ENABLED", True):
            return False

        if self.has_thumbnails(file_id):
            return True

        loop = asyncio.get_running_loop()
        owner = False

        async with self._lock:
            fut = self._in_progress.get(file_id)
            if fut is None:
                fut = loop.create_future()
                self._in_progress[file_id] = fut
                owner = True

        if not owner:
            try:
                return await fut
            except Exception:
                return False

        try:
            success = await self._generate(
                file_id=file_id,
                byte_streamer=byte_streamer,
                tg_file_id=tg_file_id,
                client_index=client_index,
                file_size=file_size,
                duration=duration,
            )
            fut.set_result(success)
            return success
        except Exception as e:
            if not fut.done():
                fut.set_result(False)
            logger.warning("Thumbnail generation error for %s: %s", file_id, e)
            return False
        finally:
            async with self._lock:
                self._in_progress.pop(file_id, None)

    async def _generate(
        self,
        file_id: str,
        byte_streamer,
        tg_file_id,
        client_index: int,
        file_size: int,
        duration: Optional[int] = None,
    ) -> bool:
        dir_path = self._file_dir(file_id)
        dir_path.mkdir(parents=True, exist_ok=True)
        sprite_path = dir_path / "sprites.webp"
        vtt_path = dir_path / "thumbnails.vtt"

        # Determine video duration
        dur = float(duration or 0)
        if dur <= 0:
            # Fallback duration if unknown: assume 600s
            dur = 600.0

        # Calculate grid and interval (bounded to max 30 tiles to prevent timeout on large videos)
        max_tiles = 30
        columns = 6
        tile_width = 160
        tile_height = 90

        interval = max(5.0, dur / max_tiles)
        num_tiles = max(1, min(max_tiles, int(math.ceil(dur / interval))))
        rows = max(1, int(math.ceil(num_tiles / columns)))

        logger.info(
            "Generating %dx%d thumbnail sprite sheet for %s (dur=%.1fs, interval=%.1fs, tiles=%d)",
            columns,
            rows,
            file_id,
            dur,
            interval,
            num_tiles,
        )

        if not self.has_poster(file_id):
            asyncio.create_task(
                self.generate_poster(
                    file_id=file_id,
                    byte_streamer=byte_streamer,
                    tg_file_id=tg_file_id,
                    client_index=client_index,
                    file_size=file_size,
                    duration=dur,
                )
            )

        filter_chain = f"fps=1/{interval:.4f},scale={tile_width}:{tile_height},tile={columns}x{rows}"
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-an", "-sn", "-dn",
            "-i", "pipe:0",
            "-vf", filter_chain,
            "-frames:v", "1",
            "-c:v", "libwebp",
            "-lossless", "0",
            "-qscale", "70",
            "-y",
            str(sprite_path),
        ]

        proc = None
        feeder_task = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def feed_pipe():
                try:
                    stream_gen = mp4_metadata_manager.virtual_faststart_stream(
                        byte_streamer=byte_streamer,
                        file_key=file_id,
                        file_id=tg_file_id,
                        client_index=client_index,
                        file_size=file_size,
                    )
                    async for chunk in stream_gen:
                        if proc.returncode is not None:
                            break
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as ex:
                    logger.debug("Thumbnail feeder error for %s: %s", file_id, ex)
                finally:
                    with contextlib.suppress(Exception):
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.close()
                            await proc.stdin.wait_closed()

            feeder_task = asyncio.create_task(feed_pipe())
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace") if stderr else "Unknown error"
                logger.warning("FFmpeg thumbnail generation failed for %s: %s", file_id, err_msg)
                return False

            if not sprite_path.exists() or sprite_path.stat().st_size == 0:
                logger.warning("Generated sprite file is missing or empty for %s", file_id)
                return False

            # Generate WebVTT content
            vtt_lines = [
                "WEBVTT",
                "",
            ]
            for i in range(num_tiles):
                col = i % columns
                row = i // columns
                x = col * tile_width
                y = row * tile_height
                start_s = i * interval
                end_s = min((i + 1) * interval, dur)
                start_str = format_vtt_timestamp(start_s)
                end_str = format_vtt_timestamp(end_s)

                vtt_lines.append(f"{start_str} --> {end_str}")
                vtt_lines.append(f"/thumbnails/{{token}}/sprites.webp#xywh={x},{y},{tile_width},{tile_height}")
                vtt_lines.append("")

            vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
            logger.info("Thumbnails and VTT created successfully for %s", file_id)
            return True

        except asyncio.TimeoutError:
            logger.warning("Thumbnail generation timed out for %s", file_id)
            if proc and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
            return False
        finally:
            if feeder_task and not feeder_task.done():
                feeder_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feeder_task


thumbnail_service = ThumbnailService()
