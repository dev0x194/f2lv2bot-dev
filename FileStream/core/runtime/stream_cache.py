from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable

from FileStream.config import Server


class LocalStreamCache:
    def __init__(self) -> None:
        self.enabled = bool(Server.STREAM_LOCAL_CACHE_ENABLED)
        self.root = Path(Server.STREAM_CACHE_DIR or "/tmp/filestream_stream_cache").expanduser()
        self.max_bytes = max(int(float(Server.STREAM_CACHE_MAX_GB) * 1024 * 1024 * 1024), 0)
        self.ttl_seconds = max(int(float(Server.STREAM_CACHE_TTL_HOURS) * 3600), 0)
        self._mem_cache: OrderedDict[str, bytes] = OrderedDict()
        self._max_mem_chunks = max(int(getattr(Server, "STREAM_MAX_MEM_CHUNKS", 64)), 16)
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        self._inflight_lock = asyncio.Lock()
        self._prune_lock = asyncio.Lock()
        self._last_prune = 0.0
        self._prune_interval = 300
        self._writes_since_prune = 0

        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_file_key(file_key: str) -> str:
        raw = str(file_key or "").strip()
        safe = "".join(ch if ch.isalnum() else "_" for ch in raw)
        return safe or "file"

    def _chunk_dir(self, file_key: str) -> Path:
        safe_key = self._safe_file_key(file_key)
        return self.root / safe_key[:2] / safe_key

    def _chunk_path(self, file_key: str, offset: int) -> Path:
        return self._chunk_dir(file_key) / f"{int(offset):020d}.chunk"

    async def warm(self) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        await self.prune(force=True)

    async def get_chunk(self, file_key: str, offset: int, expected_size: int = 0) -> bytes | None:
        if not self.enabled:
            return None
        mem_key = f"{self._safe_file_key(file_key)}:{int(offset)}"
        if mem_key in self._mem_cache:
            data = self._mem_cache[mem_key]
            if expected_size <= 0 or len(data) == expected_size:
                self._mem_cache.move_to_end(mem_key)
                return data

        cached = await self._read_chunk(file_key, offset, expected_size)
        if cached is not None:
            self._mem_cache[mem_key] = cached
            self._mem_cache.move_to_end(mem_key)
            while len(self._mem_cache) > self._max_mem_chunks:
                self._mem_cache.popitem(last=False)
            return cached
        return None

    async def put_chunk(self, file_key: str, offset: int, data: bytes) -> None:
        if not self.enabled or not data:
            return
        mem_key = f"{self._safe_file_key(file_key)}:{int(offset)}"
        self._mem_cache[mem_key] = data
        self._mem_cache.move_to_end(mem_key)
        while len(self._mem_cache) > self._max_mem_chunks:
            self._mem_cache.popitem(last=False)
        await self._write_chunk(file_key, offset, data)

    async def get_or_fetch_chunk(
        self,
        *,
        file_key: str,
        offset: int,
        expected_size: int,
        fetcher: Callable[[], Awaitable[bytes]],
    ) -> tuple[bytes, bool]:
        if not self.enabled or expected_size <= 0:
            return await fetcher(), False

        mem_key = f"{self._safe_file_key(file_key)}:{int(offset)}"
        if mem_key in self._mem_cache:
            data = self._mem_cache[mem_key]
            if expected_size <= 0 or len(data) == expected_size:
                self._mem_cache.move_to_end(mem_key)
                return data, True

        cached = await self._read_chunk(file_key, offset, expected_size)
        if cached is not None:
            self._mem_cache[mem_key] = cached
            self._mem_cache.move_to_end(mem_key)
            while len(self._mem_cache) > self._max_mem_chunks:
                self._mem_cache.popitem(last=False)
            return cached, True

        cache_key = f"{self._safe_file_key(file_key)}:{int(offset)}:{int(expected_size)}"
        loop = asyncio.get_running_loop()
        owner = False

        async with self._inflight_lock:
            future = self._inflight.get(cache_key)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(lambda fut: fut.exception() if not fut.cancelled() else None)
                self._inflight[cache_key] = future
                owner = True

        if not owner:
            return await future, True

        try:
            cached = await self._read_chunk(file_key, offset, expected_size)
            if cached is not None:
                self._mem_cache[mem_key] = cached
                self._mem_cache.move_to_end(mem_key)
                while len(self._mem_cache) > self._max_mem_chunks:
                    self._mem_cache.popitem(last=False)
                future.set_result(cached)
                return cached, True

            data = await fetcher()
            if data:
                self._mem_cache[mem_key] = data
                self._mem_cache.move_to_end(mem_key)
                while len(self._mem_cache) > self._max_mem_chunks:
                    self._mem_cache.popitem(last=False)
                await self._write_chunk(file_key, offset, data)
            future.set_result(data)
            return data, False
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            async with self._inflight_lock:
                self._inflight.pop(cache_key, None)

    async def prune(self, *, force: bool = False) -> None:
        if not self.enabled:
            return

        now = time.time()
        if not force:
            if (now - self._last_prune) < self._prune_interval and self._writes_since_prune < 32:
                return

        async with self._prune_lock:
            now = time.time()
            if not force:
                if (now - self._last_prune) < self._prune_interval and self._writes_since_prune < 32:
                    return
            self._last_prune = now
            self._writes_since_prune = 0
            await asyncio.to_thread(self._prune_sync)

    def invalidate_file(self, file_key: str) -> None:
        if not self.enabled:
            return

        safe_key = self._safe_file_key(file_key)
        prefix = f"{safe_key}:"
        keys_to_del = [k for k in self._mem_cache if k.startswith(prefix)]
        for k in keys_to_del:
            self._mem_cache.pop(k, None)

        chunk_dir = self._chunk_dir(file_key)
        if not chunk_dir.exists():
            return

        with contextlib.suppress(Exception):
            shutil.rmtree(chunk_dir)

        parent = chunk_dir.parent
        with contextlib.suppress(OSError):
            parent.rmdir()

    async def _read_chunk(self, file_key: str, offset: int, expected_size: int) -> bytes | None:
        if not self.enabled:
            return None

        path = self._chunk_path(file_key, offset)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None

        if self.ttl_seconds and stat.st_mtime < (time.time() - self.ttl_seconds):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return None

        if expected_size > 0 and stat.st_size != expected_size:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return None

        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None
        except Exception:
            logging.debug("Stream cache read failed for %s", path, exc_info=True)
            return None

        if expected_size > 0 and len(data) != expected_size:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return None

        with contextlib.suppress(OSError):
            os.utime(path, None)

        return data

    async def _write_chunk(self, file_key: str, offset: int, data: bytes) -> None:
        if not self.enabled or not data:
            return

        await asyncio.to_thread(self._write_chunk_sync, file_key, offset, data)
        self._writes_since_prune += 1
        await self.prune()

    def _write_chunk_sync(self, file_key: str, offset: int, data: bytes) -> None:
        try:
            usage = shutil.disk_usage(self.root)
            free_gb = usage.free / (1024 * 1024 * 1024)
            min_free_gb = getattr(Server, "STREAM_MIN_DISK_FREE_GB", 1.0)
            if free_gb < min_free_gb:
                self._prune_sync()
                usage = shutil.disk_usage(self.root)
                free_gb = usage.free / (1024 * 1024 * 1024)
                if free_gb < min_free_gb:
                    logging.warning(
                        "[STREAM_CACHE] Low disk space (%.2f GB free < %.2f GB threshold). Dropping chunk write.",
                        free_gb,
                        min_free_gb,
                    )
                    return
        except Exception:
            pass

        chunk_dir = self._chunk_dir(file_key)
        chunk_dir.mkdir(parents=True, exist_ok=True)
        final_path = self._chunk_path(file_key, offset)
        temp_path = final_path.with_suffix(f".{time.time_ns()}.tmp")
        try:
            temp_path.write_bytes(data)
            os.replace(temp_path, final_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()

    def _prune_sync(self) -> None:
        if not self.root.exists():
            return

        now = time.time()
        files: list[tuple[float, int, Path]] = []
        total_size = 0

        for path in self.root.rglob("*.chunk"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue

            expired = self.ttl_seconds and stat.st_mtime < (now - self.ttl_seconds)
            if expired:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue

            atime = stat.st_atime or stat.st_mtime
            total_size += stat.st_size
            files.append((atime, stat.st_size, path))

        if self.max_bytes > 0 and total_size > self.max_bytes:
            for _, size, path in sorted(files, key=lambda item: item[0]):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                total_size -= size
                if total_size <= self.max_bytes:
                    break

        for directory, _, _ in os.walk(self.root, topdown=False):
            with contextlib.suppress(OSError):
                Path(directory).rmdir()


stream_cache = LocalStreamCache()


async def warm_stream_cache() -> None:
    await stream_cache.warm()


def invalidate_file_stream_cache(file_key: str) -> None:
    stream_cache.invalidate_file(file_key)
