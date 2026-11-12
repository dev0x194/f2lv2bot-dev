"""
Lightweight MP4 Box Parser and Moov-Atom Virtualizer.

Optimizes seeking and start-times for MP4 files stored on Telegram MTProto
by locating, caching, and virtualizing 'moov' atom metadata boxes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import struct
from pathlib import Path
from typing import AsyncGenerator, Dict, Optional, Tuple

from FileStream.config import Server

logger = logging.getLogger(__name__)


def find_boxes(data: bytes, base_offset: int = 0) -> list[dict]:
    """Parse top-level boxes from binary data slice."""
    boxes = []
    pos = 0
    data_len = len(data)

    while pos + 8 <= data_len:
        box_size = struct.unpack(">I", data[pos : pos + 4])[0]
        box_type = data[pos + 4 : pos + 8]
        header_size = 8

        if box_size == 1:
            if pos + 16 > data_len:
                break
            box_size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header_size = 16
        elif box_size == 0:
            box_size = data_len - pos

        if box_size < header_size:
            break

        boxes.append({
            "type": box_type,
            "type_str": box_type.decode("latin1", errors="replace"),
            "offset": base_offset + pos,
            "size": box_size,
            "header_size": header_size,
            "rel_offset": pos,
        })
        pos += box_size

    return boxes


def find_moov_atom(data: bytes, file_size: int, base_offset: int = 0) -> Optional[dict]:
    """
    Search binary data for a valid 'moov' atom box.
    Handles standard 32-bit and 64-bit box sizes.
    """
    data_len = len(data)
    idx = 0

    while True:
        idx = data.find(b"moov", idx)
        if idx == -1:
            break

        # Check for 4 bytes size before 'moov'
        if idx >= 4:
            box_start = idx - 4
            box_size = struct.unpack(">I", data[box_start : box_start + 4])[0]
            header_size = 8

            if box_size == 1:
                # 64-bit size
                if idx + 12 <= data_len:
                    box_size = struct.unpack(">Q", data[idx + 4 : idx + 12])[0]
                    header_size = 16
                else:
                    idx += 4
                    continue
            elif box_size == 0:
                box_size = file_size - (base_offset + box_start)

            # Sanity checks for valid moov box:
            # 1. Box size must be at least header size and not larger than the file.
            # 2. Total end offset must not exceed file size + 64 bytes tolerance.
            abs_start = base_offset + box_start
            abs_end = abs_start + box_size

            if header_size <= box_size <= file_size and abs_end <= file_size + 64:
                # Verify that it contains valid child boxes like 'mvhd' or 'trak'
                content_start = box_start + header_size
                content_slice = data[content_start : min(content_start + 32, data_len)]
                if b"mvhd" in content_slice or b"trak" in content_slice or b"iods" in content_slice:
                    box_data = data[box_start : min(box_start + box_size, data_len)]
                    return {
                        "offset": abs_start,
                        "size": box_size,
                        "data": box_data,
                        "is_tail": abs_start > (file_size // 2),
                        "header_size": header_size,
                    }

        idx += 4

    return None


def patch_moov_chunk_offsets(moov_data: bytes, shift_bytes: int) -> bytes:
    """
    Update chunk offset tables ('stco' and 'co64') inside 'moov' atom
    by shifting all offsets forward by `shift_bytes` (for faststart remuxing).
    """
    if shift_bytes <= 0:
        return moov_data

    buf = bytearray(moov_data)
    buf_len = len(buf)
    idx = 0

    # Search for 'stco' (32-bit chunk offsets)
    while True:
        idx = buf.find(b"stco", idx)
        if idx == -1:
            break

        if idx >= 4:
            box_start = idx - 4
            box_size = struct.unpack(">I", buf[box_start : box_start + 4])[0]
            if 16 <= box_size <= (buf_len - box_start):
                # Header: size (4), 'stco' (4), version (1), flags (3), entry_count (4)
                entry_count = struct.unpack(">I", buf[box_start + 12 : box_start + 16])[0]
                expected_size = 16 + entry_count * 4
                if expected_size == box_size:
                    for i in range(entry_count):
                        entry_pos = box_start + 16 + (i * 4)
                        old_offset = struct.unpack(">I", buf[entry_pos : entry_pos + 4])[0]
                        new_offset = min(old_offset + shift_bytes, 0xFFFFFFFF)
                        buf[entry_pos : entry_pos + 4] = struct.pack(">I", new_offset)
        idx += 4

    # Search for 'co64' (64-bit chunk offsets)
    idx = 0
    while True:
        idx = buf.find(b"co64", idx)
        if idx == -1:
            break

        if idx >= 4:
            box_start = idx - 4
            box_size = struct.unpack(">I", buf[box_start : box_start + 4])[0]
            if 16 <= box_size <= (buf_len - box_start):
                entry_count = struct.unpack(">I", buf[box_start + 12 : box_start + 16])[0]
                expected_size = 16 + entry_count * 8
                if expected_size == box_size:
                    for i in range(entry_count):
                        entry_pos = box_start + 16 + (i * 8)
                        old_offset = struct.unpack(">Q", buf[entry_pos : entry_pos + 8])[0]
                        new_offset = old_offset + shift_bytes
                        buf[entry_pos : entry_pos + 8] = struct.pack(">Q", new_offset)
        idx += 4

    return bytes(buf)


class MP4MetadataManager:
    """Manages parsing, caching, and serving of MP4 metadata and virtual faststart."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir or Server.STREAM_CACHE_DIR or "/tmp/filestream_stream_cache") / "moov"
        self._memory_cache: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        with contextlib.suppress(Exception):
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_key(key: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in str(key or "").strip()) or "file"

    def get_cached_moov(self, file_key: str) -> Optional[dict]:
        safe = self._safe_key(file_key)
        if safe in self._memory_cache:
            return self._memory_cache[safe]

        meta_path = self.cache_dir / f"{safe}.meta.json"
        data_path = self.cache_dir / f"{safe}.moov"
        head_path = self.cache_dir / f"{safe}.head"
        pmoov_path = self.cache_dir / f"{safe}.pmoov"

        if meta_path.exists() and data_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["data"] = data_path.read_bytes()
                if head_path.exists():
                    meta["header_data"] = head_path.read_bytes()
                else:
                    meta["header_data"] = b""
                if pmoov_path.exists():
                    meta["patched_moov"] = pmoov_path.read_bytes()
                elif meta.get("is_tail"):
                    meta["patched_moov"] = patch_moov_chunk_offsets(meta["data"], shift_bytes=meta["size"])
                else:
                    meta["patched_moov"] = b""
                self._memory_cache[safe] = meta
                return meta
            except Exception as e:
                logger.debug("Failed reading cached moov for %s: %s", safe, e)

        return None

    def cache_moov(
        self,
        file_key: str,
        moov_info: dict,
        moov_data: bytes,
        header_data: Optional[bytes] = None,
        patched_moov: Optional[bytes] = None,
    ) -> None:
        safe = self._safe_key(file_key)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        mdat_offset = int(moov_info.get("mdat_offset") or 0)
        moov_size = int(moov_info["size"])
        is_tail = bool(moov_info.get("is_tail", True))

        if is_tail and (patched_moov is None or len(patched_moov) == 0):
            patched_moov = patch_moov_chunk_offsets(moov_data, shift_bytes=moov_size)

        meta = {
            "offset": int(moov_info["offset"]),
            "size": moov_size,
            "is_tail": is_tail,
            "mdat_offset": mdat_offset,
        }
        self._memory_cache[safe] = {
            **meta,
            "data": moov_data,
            "header_data": header_data or b"",
            "patched_moov": patched_moov or b"",
        }

        try:
            (self.cache_dir / f"{safe}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (self.cache_dir / f"{safe}.moov").write_bytes(moov_data)
            if header_data:
                (self.cache_dir / f"{safe}.head").write_bytes(header_data)
            if patched_moov:
                (self.cache_dir / f"{safe}.pmoov").write_bytes(patched_moov)
        except Exception as e:
            logger.warning("Failed writing moov cache for %s: %s", safe, e)

    def get_cached_range(self, file_key: str, from_bytes: int, until_bytes: int) -> Optional[bytes]:
        """
        Check if the requested range is satisfied entirely by cached moov data.
        Returns the byte slice if satisfied, else None.
        """
        cached = self.get_cached_moov(file_key)
        if not cached:
            return None

        moov_start = cached["offset"]
        moov_size = cached["size"]
        moov_end = moov_start + moov_size - 1
        moov_data = cached.get("data") or b""

        if len(moov_data) < moov_size:
            return None

        # If requested range falls completely within the cached moov atom
        if moov_start <= from_bytes <= until_bytes <= moov_end:
            rel_start = from_bytes - moov_start
            rel_end = until_bytes - moov_start + 1
            return moov_data[rel_start:rel_end]

        return None

    async def fetch_and_index_moov(
        self,
        byte_streamer,
        file_id,
        file_key: str,
        file_size: int,
        client_index: int = 0,
    ) -> Optional[dict]:
        """
        Fetch the tail 1–2MB of the MP4 file using ByteStreamer,
        parse the 'moov' atom box, probe the head for mdat offset,
        pre-patch moov chunk offsets, and cache everything.
        """
        if not file_size or file_size <= 0:
            return None

        cached = self.get_cached_moov(file_key)
        if cached:
            return cached

        async with self._lock:
            # Double-check inside lock
            cached = self.get_cached_moov(file_key)
            if cached:
                return cached

            scan_mb = getattr(Server, "MP4_MOOV_TAIL_SCAN_MB", 2)
            tail_size = min(scan_mb * 1024 * 1024, file_size)

            tail_data = b""
            base_offset = 0
            try:
                tail_data, base_offset = await fetch_tail_bytes(
                    byte_streamer=byte_streamer,
                    file_id=file_id,
                    file_key=file_key,
                    file_size=file_size,
                    tail_size=tail_size,
                    client_index=client_index,
                )
            except Exception as e:
                logger.warning("Error fetching tail bytes for %s: %s", file_key, e)
                return None

            if not tail_data:
                return None

            moov_info = find_moov_atom(tail_data, file_size=file_size, base_offset=base_offset)
            if moov_info:
                header_data = b""
                mdat_offset = 0

                if moov_info["is_tail"]:
                    try:
                        # Probe head to discover ftyp / pre-mdat headers and mdat start
                        head_buf = bytearray()
                        probe_size = min(64 * 1024, file_size)
                        async for chunk in byte_streamer.yield_file(
                            file_key=file_key,
                            file_id=file_id,
                            index=client_index,
                            offset=0,
                            first_part_cut=0,
                            last_part_cut=probe_size,
                            part_count=1,
                            chunk_size=probe_size,
                        ):
                            head_buf.extend(chunk)
                            if len(head_buf) >= 4096:
                                break

                        head_bytes = bytes(head_buf)
                        head_boxes = find_boxes(head_bytes, base_offset=0)
                        for b in head_boxes:
                            if b["type"] == b"mdat":
                                mdat_offset = b["offset"]
                                break

                        if mdat_offset > 0:
                            header_data = head_bytes[:mdat_offset]
                        elif head_boxes:
                            # If mdat wasn't encountered, take up to the end of ftyp
                            for b in head_boxes:
                                if b["type"] == b"ftyp":
                                    mdat_offset = b["offset"] + b["size"]
                                    header_data = head_bytes[:mdat_offset]
                                    break
                    except Exception as ex:
                        logger.debug("Failed probing head boxes for faststart %s: %s", file_key, ex)

                moov_info["mdat_offset"] = mdat_offset
                patched_moov = b""
                if moov_info["is_tail"]:
                    patched_moov = patch_moov_chunk_offsets(moov_info["data"], shift_bytes=moov_info["size"])

                self.cache_moov(
                    file_key=file_key,
                    moov_info=moov_info,
                    moov_data=moov_info["data"],
                    header_data=header_data,
                    patched_moov=patched_moov,
                )
                logger.info(
                    "Indexed moov atom for %s: offset=%s, size=%s, is_tail=%s, mdat_offset=%s",
                    file_key,
                    moov_info["offset"],
                    moov_info["size"],
                    moov_info["is_tail"],
                    mdat_offset,
                )
                return self.get_cached_moov(file_key)

        return None

    async def virtual_faststart_stream(
        self,
        byte_streamer,
        file_key: str,
        file_id,
        client_index: int,
        file_size: int,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncGenerator[bytes, None]:
        """
        Yields an on-the-fly faststart MP4 stream:
        [ftyp and headers] -> [patched moov box] -> [mdat data]
        This allows browsers and FFmpeg pipe:0 to immediately begin playback
        without round-tripping to Telegram DCs for tail metadata.
        """
        cached_moov = self.get_cached_moov(file_key)
        if not cached_moov or not cached_moov.get("is_tail"):
            # If not a tail moov, stream directly
            part_count = (file_size + chunk_size - 1) // chunk_size
            async for chunk in byte_streamer.yield_file(
                file_key=file_key,
                file_id=file_id,
                index=client_index,
                offset=0,
                first_part_cut=0,
                last_part_cut=chunk_size,
                part_count=part_count,
                chunk_size=chunk_size,
            ):
                yield chunk
            return

        moov_offset = cached_moov["offset"]
        moov_data = cached_moov["data"]
        moov_size = cached_moov["size"]

        # Fetch header (first chunk, up to 1MB or start of mdat)
        header_buf = bytearray()
        first_chunk_gen = byte_streamer.yield_file(
            file_key=file_key,
            file_id=file_id,
            index=client_index,
            offset=0,
            first_part_cut=0,
            last_part_cut=chunk_size,
            part_count=1,
            chunk_size=chunk_size,
        )
        async for chunk in first_chunk_gen:
            header_buf.extend(chunk)

        # Parse boxes in the header
        boxes = find_boxes(bytes(header_buf), base_offset=0)
        mdat_offset = None
        for b in boxes:
            if b["type"] == b"mdat":
                mdat_offset = b["offset"]
                break

        if mdat_offset is None:
            # Fallback to direct stream
            yield bytes(header_buf)
            part_count = (file_size + chunk_size - 1) // chunk_size
            for p in range(1, part_count):
                async for chunk in byte_streamer.yield_file(
                    file_key=file_key,
                    file_id=file_id,
                    index=client_index,
                    offset=p * chunk_size,
                    first_part_cut=0,
                    last_part_cut=chunk_size,
                    part_count=1,
                    chunk_size=chunk_size,
                ):
                    yield chunk
            return

        # Header boxes before mdat (e.g. ftyp, free)
        pre_mdat_header = header_buf[:mdat_offset]
        yield bytes(pre_mdat_header)

        # Patch moov offsets shifted by the length of the new moov box
        patched_moov = patch_moov_chunk_offsets(moov_data, shift_bytes=moov_size)
        yield patched_moov

        # Now stream from mdat_offset up to moov_offset (excluding tail moov)
        mdat_end = moov_offset
        current_offset = mdat_offset

        # Check if first chunk contains part of mdat
        if len(header_buf) > mdat_offset:
            initial_mdat = header_buf[mdat_offset:min(len(header_buf), mdat_end)]
            yield bytes(initial_mdat)
            current_offset += len(initial_mdat)

        if current_offset < mdat_end:
            from_bytes = current_offset
            until_bytes = mdat_end - 1
            stream_chunk_size = min(chunk_size, 1024 * 1024)
            # Ensure stream_chunk_size is aligned to 1024 bytes
            stream_chunk_size = max(1024, (stream_chunk_size // 1024) * 1024)

            offset = from_bytes - (from_bytes % stream_chunk_size)
            first_part_cut = from_bytes - offset
            last_part_cut = (until_bytes % stream_chunk_size) + 1
            part_count = math.ceil((until_bytes + 1) / stream_chunk_size) - math.floor(offset / stream_chunk_size)

            async for chunk in byte_streamer.yield_file(
                file_key=file_key,
                file_id=file_id,
                index=client_index,
                offset=offset,
                first_part_cut=first_part_cut,
                last_part_cut=last_part_cut,
                part_count=part_count,
                chunk_size=stream_chunk_size,
            ):
                yield chunk

    async def fetch_tail_bytes(
        self,
        byte_streamer,
        file_id,
        file_key: str,
        file_size: int,
        scan_bytes: int = 1048576,
        tail_size: Optional[int] = None,
        client_index: int = 0,
    ) -> tuple[bytes, int]:
        effective_tail = tail_size if tail_size is not None else scan_bytes
        return await fetch_tail_bytes(
            byte_streamer=byte_streamer,
            file_id=file_id,
            file_key=file_key,
            file_size=file_size,
            tail_size=effective_tail,
            client_index=client_index,
        )

    async def virtual_faststart_range_stream(
        self,
        byte_streamer,
        file_id,
        file_key: str,
        client_index: int,
        from_bytes: int,
        until_bytes: int,
        file_size: int,
        chunk_size: int,
        cached_moov: dict,
    ) -> AsyncGenerator[bytes, None]:
        """
        Yields the requested byte range [from_bytes, until_bytes] from a virtual faststart stream.
        - Virtual Range [0 .. prefix_len-1]: Header (ftyp) + Patched Moov atom.
        - Virtual Range [prefix_len .. file_size-1]: Real mdat starting at mdat_offset.
        """
        header_data = cached_moov.get("header_data") or b""
        patched_moov = cached_moov.get("patched_moov") or b""
        moov_size = cached_moov.get("size") or cached_moov.get("moov_size") or 0
        mdat_offset = cached_moov.get("mdat_offset") or len(header_data) or 32
        prefix = header_data + patched_moov
        prefix_len = len(prefix)

        # 1. Entire range inside prefix (ftyp + moov)
        if until_bytes < prefix_len:
            yield prefix[from_bytes : until_bytes + 1]
            return

        # 2. Range starts in prefix and extends into mdat
        if from_bytes < prefix_len:
            yield prefix[from_bytes:]
            real_from = mdat_offset
        else:
            # 3. Range is completely inside mdat
            real_from = from_bytes - moov_size

        real_until = min(until_bytes - moov_size, file_size - 1)
        if real_from <= real_until:
            stream_chunk_size = min(chunk_size, 1024 * 1024)
            stream_chunk_size = max(1024, (stream_chunk_size // 1024) * 1024)

            offset = real_from - (real_from % stream_chunk_size)
            first_part_cut = real_from - offset
            last_part_cut = (real_until % stream_chunk_size) + 1
            part_count = math.ceil((real_until + 1) / stream_chunk_size) - math.floor(offset / stream_chunk_size)

            async for chunk in byte_streamer.yield_file(
                file_key,
                file_id,
                client_index,
                offset,
                first_part_cut,
                last_part_cut,
                part_count,
                stream_chunk_size,
            ):
                yield chunk


async def fetch_tail_bytes(
    byte_streamer,
    file_id,
    file_key: str,
    file_size: int,
    tail_size: int = 1048576,
    client_index: int = 0,
) -> tuple[bytes, int]:
    """
    Fetch the last tail_size bytes of a file from Telegram MTProto,
    strictly aligning offsets to 1024-byte boundaries.
    Returns (data_bytes, base_offset) where base_offset is the aligned starting byte offset.
    """
    if not file_size or file_size <= 0 or tail_size <= 0:
        return b"", 0

    desired_tail = min(tail_size, file_size)
    raw_offset = max(0, file_size - desired_tail)
    aligned_offset = (raw_offset // 1024) * 1024

    chunk_size = 1024 * 1024  # 1MB, aligned to 1024 bytes and <= TELEGRAM_GETFILE_LIMIT
    from_bytes = aligned_offset
    until_bytes = file_size - 1

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = (until_bytes % chunk_size) + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - math.floor(offset / chunk_size)

    tail_buf = bytearray()
    async for chunk in byte_streamer.yield_file(
        file_key,
        file_id,
        client_index,
        offset,
        first_part_cut,
        last_part_cut,
        part_count,
        chunk_size,
    ):
        tail_buf.extend(chunk)

    return bytes(tail_buf), aligned_offset

    return bytes(tail_buf), aligned_offset


mp4_metadata_manager = MP4MetadataManager()
