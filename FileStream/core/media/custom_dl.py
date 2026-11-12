import asyncio
import contextlib
import logging
from typing import Dict, Union
from FileStream.bot import work_loads
from FileStream.config import Server
from pyrogram import Client, utils, raw
from .file_properties import get_file_ids
from FileStream.core.runtime.client_balance import ensure_client_stat, record_stream_completed, record_stream_failed
from FileStream.core.runtime.stream_cache import stream_cache
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.types import Message

from FileStream.core.resilience.workload import workload_tracker
from FileStream.core.monitoring.metrics import get_metrics

TELEGRAM_GETFILE_LIMIT = 1024 * 1024

# Persistent media session cache keyed by (client_key, dc_id)
_media_sessions: dict[tuple[int, int], Session] = {}
_session_locks: dict[tuple[int, int], asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


def _get_client_key(client: Client) -> int:
    idx = getattr(client, "client_index", None)
    if idx is not None:
        return int(idx)
    me = getattr(client, "me", None)
    if me and getattr(me, "id", None):
        return int(me.id)
    return id(client)


async def _get_session_lock(key: tuple[int, int]) -> asyncio.Lock:
    async with _session_locks_guard:
        if key not in _session_locks:
            _session_locks[key] = asyncio.Lock()
        return _session_locks[key]


# Global sliding-window background prefetch task registry
_stream_prefetches: dict[str, dict[int, asyncio.Task]] = {}
_stream_prefetch_lock = asyncio.Lock()


async def schedule_prefetch_window(
    file_key: str,
    file_id: FileId,
    current_offset: int,
    chunk_size: int,
    base_index: int,
    prefetch_count: int = 4,
) -> None:
    """Launch concurrent background sliding-window prefetch tasks across multi_clients."""
    if not bool(getattr(Server, "STREAM_LOCAL_CACHE_ENABLED", True)):
        return

    from FileStream.bot import multi_clients
    file_size = int(getattr(file_id, "file_size", 0) or 0)
    if file_size <= 0:
        return

    async with _stream_prefetch_lock:
        file_tasks = _stream_prefetches.setdefault(file_key, {})
        # Prune distant stale prefetch tasks (> 16MB away from current offset)
        prune_threshold = 16 * chunk_size
        stale_offsets = [
            off for off in list(file_tasks.keys())
            if abs(off - current_offset) > prune_threshold
        ]
        for off in stale_offsets:
            task = file_tasks.pop(off, None)
            if task and not task.done():
                task.cancel()

    clients_list = [c for c in multi_clients.values() if c is not None] if multi_clients else []
    num_clients = len(clients_list)

    for step in range(1, prefetch_count + 1):
        target_offset = current_offset + (step * chunk_size)
        if target_offset >= file_size:
            break

        # Check if already cached in RAM or on disk
        if await stream_cache.get_chunk(file_key, target_offset) is not None:
            continue

        async with _stream_prefetch_lock:
            existing = file_tasks.get(target_offset)
            if existing and not existing.done():
                continue

            # Distribute across multi_clients round-robin
            if num_clients > 1:
                client_idx = (base_index + step) % num_clients
                fetch_client = clients_list[client_idx]
            else:
                client_idx = base_index
                fetch_client = clients_list[0] if clients_list else None

            rem_bytes = max(file_size - target_offset, 0)
            expected_size = min(chunk_size, rem_bytes) if rem_bytes else chunk_size

            async def _fetch_task(p_client: Client, p_off: int, p_size: int, p_idx: int):
                try:
                    if p_client is None:
                        return
                    streamer = ByteStreamer(p_client)
                    media_session = await streamer.generate_media_session(p_client, file_id)
                    location = await streamer.get_location(file_id)

                    async def _call_tg():
                        r = await streamer._fetch_chunk(media_session, location, p_off, chunk_size)
                        if isinstance(r, raw.types.upload.File):
                            return bytes(r.bytes or b"")
                        return b""

                    await stream_cache.get_or_fetch_chunk(
                        file_key=file_key,
                        offset=p_off,
                        expected_size=p_size,
                        fetcher=_call_tg,
                    )
                except Exception as exc:
                    logging.debug("Prefetch failed for %s offset %s: %s", file_key, p_off, exc)
                finally:
                    async with _stream_prefetch_lock:
                        if file_key in _stream_prefetches:
                            _stream_prefetches[file_key].pop(p_off, None)

            if fetch_client is not None:
                task = asyncio.create_task(_fetch_task(fetch_client, target_offset, expected_size, client_idx))
                file_tasks[target_offset] = task


class ByteStreamer:
    def __init__(self, client: Client):
        self.clean_timer = 30 * 60
        self.client: Client = client
        self.cached_file_ids: Dict[str, FileId] = {}
        self._clean_task: asyncio.Task | None = None
        try:
            self._clean_task = asyncio.create_task(self.clean_cache())
        except RuntimeError:
            pass

    async def close(self) -> None:
        """Cancel background cache cleanup task safely."""
        if self._clean_task and not self._clean_task.done():
            self._clean_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._clean_task

    async def get_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Returns the properties of a media of a specific message in a FIleId class.
        if the properties are cached, then it'll return the cached results.
        or it'll generate the properties from the Message ID and cache them.
        """
        if not db_id in self.cached_file_ids:
            logging.debug("Before Calling generate_file_properties")
            await self.generate_file_properties(db_id, multi_clients)
            logging.debug(f"Cached file properties for file with ID {db_id}")
        return self.cached_file_ids[db_id]

    async def generate_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Generates the properties of a media file on a specific message.
        returns ths properties in a FIleId class.
        """
        logging.debug("Before calling get_file_ids")
        file_id = await get_file_ids(self.client, db_id, multi_clients, None)
        logging.debug(f"Generated file ID and Unique ID for file with ID {db_id}")
        self.cached_file_ids[db_id] = file_id
        logging.debug(f"Cached media file with ID {db_id}")
        return self.cached_file_ids[db_id]

    async def reset_media_session(self, client: Client, dc_id: int) -> None:
        client_key = _get_client_key(client)
        session_key = (client_key, dc_id)
        media_session = _media_sessions.pop(session_key, None)
        if hasattr(client, "media_sessions"):
            client.media_sessions.pop(dc_id, None)
        if media_session is None:
            return
        with contextlib.suppress(Exception):
            await media_session.stop()

    async def generate_media_session(self, client: Client, file_id: FileId, *, refresh: bool = False) -> Session:
        """
        Generates or reuses the media session for the DC that contains the media file.
        This is required for getting the bytes from Telegram servers.
        """
        client_key = _get_client_key(client)
        session_key = (client_key, file_id.dc_id)

        if refresh:
            await self.reset_media_session(client, file_id.dc_id)

        lock = await _get_session_lock(session_key)
        async with lock:
            if not refresh:
                media_session = _media_sessions.get(session_key)
                if media_session is not None:
                    if hasattr(client, "media_sessions"):
                        client.media_sessions[file_id.dc_id] = media_session
                    logging.debug("Reusing persistent media session for DC %s (client %s)", file_id.dc_id, client_key)
                    return media_session

                if hasattr(client, "media_sessions") and file_id.dc_id in client.media_sessions:
                    media_session = client.media_sessions[file_id.dc_id]
                    _media_sessions[session_key] = media_session
                    return media_session

            # Create new Session
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(
                        client, file_id.dc_id, await client.storage.test_mode()
                    ).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

                for attempt in range(6):
                    try:
                        exported_auth = await client.invoke(
                            raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                        )
                    except FloodWait as fw:
                        wait_sec = int(fw.value or 1) + 1
                        logging.warning(
                            "FloodWait of %ss on ExportAuthorization for DC %s (client %s). Waiting...",
                            wait_sec,
                            file_id.dc_id,
                            client_key,
                        )
                        await asyncio.sleep(wait_sec)
                        continue
                    except Exception as exc:
                        logging.warning(
                            "ExportAuthorization error for DC %s (attempt %s/6): %s",
                            file_id.dc_id,
                            attempt + 1,
                            exc,
                        )
                        await asyncio.sleep(1.0)
                        continue

                    try:
                        await media_session.invoke(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logging.debug(
                            f"Invalid authorization bytes for DC {file_id.dc_id}"
                        )
                        continue
                else:
                    with contextlib.suppress(Exception):
                        await media_session.stop()
                    await self.reset_media_session(client, file_id.dc_id)
                    raise AuthBytesInvalid
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()

            logging.debug("Created and cached persistent media session for DC %s (client %s)", file_id.dc_id, client_key)
            _media_sessions[session_key] = media_session
            if hasattr(client, "media_sessions"):
                client.media_sessions[file_id.dc_id] = media_session
            return media_session


    @staticmethod
    async def get_location(file_id: FileId) -> Union[raw.types.InputPhotoFileLocation,
                                                     raw.types.InputDocumentFileLocation,
                                                     raw.types.InputPeerPhotoFileLocation,]:
        """
        Returns the file location for the media file.
        """
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return location

    async def yield_file(
        self,
        file_key: str,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
    ) -> Union[str, None]:
        """
        Custom generator that yields the bytes of the media file.
        Modded from <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py#L20>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        client = self.client
        logging.debug(f"Starting to yielding file with client {index}.")

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        bytes_sent = 0
        telegram_bytes = 0
        telegram_started_at = None
        telegram_first_byte_delay = None
        client_load_acquired = False
        pending_fetch = None
        stream_failed = False
        stream_acquired = False

        try:
            workload_tracker.enter_stream()
            stream_acquired = True
            current_part = 1
            media_session = None
            location = None
            media_session_lock = asyncio.Lock()
            prefetch_depth = max(1, min(int(Server.STREAM_PREFETCH_CHUNKS), int(part_count), 8))
            pending_fetches: dict[int, asyncio.Task] = {}
            next_part_to_schedule = 1

            async def ensure_media_ready():
                nonlocal media_session, location

                if media_session is not None and location is not None:
                    return media_session, location

                async with media_session_lock:
                    if media_session is not None and location is not None:
                        return media_session, location

                    last_exc = None
                    for attempt in range(2):
                        try:
                            media_session = await self.generate_media_session(
                                client,
                                file_id,
                                refresh=(attempt > 0),
                            )
                            location = await self.get_location(file_id)
                            return media_session, location
                        except AuthBytesInvalid as exc:
                            last_exc = exc
                            logging.warning(
                                "Media session auth failed for DC %s on client %s (attempt %s/2)",
                                file_id.dc_id,
                                index,
                                attempt + 1,
                            )
                            await self.reset_media_session(client, file_id.dc_id)

                    raise last_exc or AuthBytesInvalid

            async def fetch_chunk_from_telegram(part_offset: int) -> bytes:
                nonlocal media_session, location, telegram_started_at, telegram_first_byte_delay, client_load_acquired

                if not client_load_acquired:
                    ensure_client_stat(index)
                    work_loads[index] = work_loads.get(index, 0) + 1
                    client_load_acquired = True

                if telegram_started_at is None:
                    telegram_started_at = loop.time()

                media_session, location = await ensure_media_ready()

                r = await self._fetch_chunk(media_session, location, part_offset, chunk_size)
                if not isinstance(r, raw.types.upload.File):
                    return b""

                chunk = bytes(r.bytes or b"")
                if chunk and telegram_first_byte_delay is None and telegram_started_at is not None:
                    telegram_first_byte_delay = loop.time() - telegram_started_at
                return chunk

            async def get_part(part_number: int):
                part_offset = offset + ((part_number - 1) * chunk_size)
                remaining_bytes = max(int(file_id.file_size or 0) - part_offset, 0)
                expected_size = min(chunk_size, remaining_bytes) if remaining_bytes else chunk_size
                return await stream_cache.get_or_fetch_chunk(
                    file_key=file_key,
                    offset=part_offset,
                    expected_size=expected_size,
                    fetcher=lambda: fetch_chunk_from_telegram(part_offset),
                )

            # Trigger background sliding-window prefetch across multi_clients
            prefetch_window_size = max(int(getattr(Server, "STREAM_PREFETCH_CHUNKS", 4)), 4)
            asyncio.create_task(
                schedule_prefetch_window(
                    file_key=file_key,
                    file_id=file_id,
                    current_offset=offset,
                    chunk_size=chunk_size,
                    base_index=index,
                    prefetch_count=prefetch_window_size,
                )
            )

            while next_part_to_schedule <= prefetch_depth:
                pending_fetches[next_part_to_schedule] = asyncio.create_task(
                    get_part(next_part_to_schedule)
                )
                next_part_to_schedule += 1

            try:
                while current_part <= part_count:
                    pending_fetch = pending_fetches.pop(current_part, None)
                    if pending_fetch is None:
                        break
                    chunk, cache_hit = await pending_fetch

                    if next_part_to_schedule <= part_count:
                        pending_fetches[next_part_to_schedule] = asyncio.create_task(
                            get_part(next_part_to_schedule)
                        )
                        next_part_to_schedule += 1

                    if not chunk:
                        break
                    if not cache_hit:
                        telegram_bytes += len(chunk)

                    if part_count == 1:
                        payload = chunk[first_part_cut:last_part_cut]
                    elif current_part == 1:
                        payload = chunk[first_part_cut:]
                    elif current_part == part_count:
                        payload = chunk[:last_part_cut]
                    else:
                        payload = chunk

                    if payload:
                        bytes_sent += len(payload)
                        get_metrics().record_bytes_streamed(len(payload))
                        yield payload

                    current_part += 1
                    current_part_offset = offset + ((current_part - 1) * chunk_size)
                    if current_part_offset < int(getattr(file_id, "file_size", 0) or 0):
                        asyncio.create_task(
                            schedule_prefetch_window(
                                file_key=file_key,
                                file_id=file_id,
                                current_offset=current_part_offset,
                                chunk_size=chunk_size,
                                base_index=index,
                                prefetch_count=prefetch_window_size,
                            )
                        )
            except (TimeoutError, AttributeError):
                stream_failed = True
                if client_load_acquired:
                    record_stream_failed(index)
            except Exception:
                stream_failed = True
                if client_load_acquired:
                    record_stream_failed(index)
                raise
        finally:
            pending_tasks = []
            if pending_fetch is not None and not pending_fetch.done():
                pending_tasks.append(pending_fetch)
            pending_tasks.extend(task for task in locals().get("pending_fetches", {}).values() if not task.done())
            for task in pending_tasks:
                task.cancel()
            for task in pending_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            duration = max(loop.time() - start_time, 0.001)
            if client_load_acquired:
                work_loads[index] = max(work_loads.get(index, 1) - 1, 0)

            if telegram_bytes > 0 and not stream_failed:
                telegram_duration = max(loop.time() - (telegram_started_at or start_time), 0.001)
                record_stream_completed(
                    index,
                    bytes_sent=telegram_bytes,
                    duration_s=telegram_duration,
                    first_byte_s=telegram_first_byte_delay,
                )

            logging.debug(f"Finished yielding file with {locals().get('current_part', 0)} parts.")
            logging.debug(
                "Stream duration %.3fs, yielded %s bytes, telegram fetched %s bytes",
                duration,
                bytes_sent,
                telegram_bytes,
            )
            if stream_acquired:
                workload_tracker.exit_stream()
                stream_acquired = False

    @staticmethod
    async def _fetch_chunk(
        media_session: Session,
        location: Union[
            raw.types.InputPhotoFileLocation,
            raw.types.InputDocumentFileLocation,
            raw.types.InputPeerPhotoFileLocation,
        ],
        offset: int,
        chunk_size: int,
    ):
        limit = min(int(chunk_size), TELEGRAM_GETFILE_LIMIT)
        aligned_offset = (int(offset) // 1024) * 1024
        aligned_limit = min(max(1024, (int(limit) // 1024) * 1024), TELEGRAM_GETFILE_LIMIT)
        return await media_session.invoke(
            raw.functions.upload.GetFile(
                location=location,
                offset=aligned_offset,
                limit=aligned_limit,
            ),
        )


    async def clean_cache(self) -> None:
        """
        function to clean the cache to reduce memory usage
        """
        while True:
            await asyncio.sleep(self.clean_timer)
            self.cached_file_ids.clear()
            with contextlib.suppress(Exception):
                async with _stream_prefetch_lock:
                    for tasks in _stream_prefetches.values():
                        for t in list(tasks.values()):
                            if not t.done():
                                t.cancel()
                    _stream_prefetches.clear()
            logging.debug("Cleaned the cache")
