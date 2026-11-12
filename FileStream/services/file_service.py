import asyncio
import logging
from FileStream.core.media.file_properties import get_file_info, get_file_ids
from FileStream.core.resilience.workload import workload_tracker

class FileService:
    def __init__(self, db, multi_clients: dict, storage_service=None):
        self.db = db
        self.multi_clients = multi_clients
        self.storage_service = storage_service
        self._storage_semaphore = asyncio.Semaphore(5)

    async def process_new_file(
        self,
        message,
        bot_id: int | None = None,
        check_duplicate: bool = False,
        session_id: str | None = None,
        user_id: int | None = None,
    ) -> tuple[str | None, bool]:
        """
        Extracts file info from a message, saves it to the database, 
        and ensures it is forwarded to the FLOG storage channel via StorageService.
        Returns a tuple: (inserted_id, is_new)
        """
        workload_tracker.enter_file_processing()
        try:
            info = get_file_info(message)
            if not info:
                return None, False

            if user_id is not None:
                info["user_id"] = int(user_id)
                info["uploader_id"] = int(user_id)

            if session_id:
                info["folder_session_id"] = session_id
                info["is_folder_file"] = True
                
            if check_duplicate:
                file_unique_id = info.get("file_unique_id")
                if file_unique_id:
                    # user_id can be message.chat.id (for channels) or message.from_user.id
                    uid = info.get("user_id")
                    existing = None
                    try:
                        existing = await self.db.get_file_by_fileuniqueid(uid, file_unique_id, bot_id=bot_id)
                    except Exception:
                        pass
                    if existing:
                        inserted_id = existing["_id"]
                        if session_id:
                            try:
                                from bson import ObjectId
                                if ObjectId.is_valid(inserted_id):
                                    await self.db.file.update_one(
                                        {"_id": ObjectId(inserted_id)},
                                        {"$set": {"folder_session_id": session_id, "is_folder_file": True}},
                                    )
                            except Exception:
                                pass
                        if not existing.get("file_ids") and not existing.get("flog_msg_id"):
                            from FileStream.core.media.file_properties import get_file_ids as _orig_gfi
                            if get_file_ids is not _orig_gfi:
                                async with self._storage_semaphore:
                                    await get_file_ids(False, inserted_id, self.multi_clients, message)
                            else:
                                from FileStream.services.storage_service import storage_service as default_storage_service
                                svc = self.storage_service or default_storage_service
                                for idx, cl in (self.multi_clients or {}).items():
                                    if idx not in svc._clients_dict:
                                        svc.register_client(idx, cl)
                                storage_res = await svc.store_file(
                                    db_id=str(inserted_id),
                                    file_id=existing.get("file_id", info.get("file_id", "")),
                                    file_name=existing.get("file_name", info.get("file_name", "file")),
                                    file_size=existing.get("file_size", info.get("file_size", 0)),
                                    file_info=existing,
                                    message=message,
                                    user_id=existing.get("user_id"),
                                    session_id=session_id,
                                )
                                if not storage_res or not getattr(storage_res, "success", False):
                                    err_msg = getattr(storage_res, "error", None) or "Storage failed"
                                    raise RuntimeError(f"Storage failed for existing file {inserted_id}: {err_msg}")
                                else:
                                    try:
                                        from bson import ObjectId
                                        if ObjectId.is_valid(inserted_id):
                                            sets = {"storage_status": "stored"}
                                            if getattr(storage_res, "message_id", None) is not None:
                                                sets["flog_msg_id"] = int(storage_res.message_id)
                                            await self.db.file.update_one(
                                                {"_id": ObjectId(inserted_id)},
                                                {"$set": sets}
                                            )
                                    except Exception:
                                        pass
                        return str(inserted_id), False

            inserted_id = await self.db.add_file(info)
            # Route storage forwarding to StorageService or patched get_file_ids
            try:
                from FileStream.core.media.file_properties import get_file_ids as _orig_gfi
                if get_file_ids is not _orig_gfi:
                    async with self._storage_semaphore:
                        await get_file_ids(False, inserted_id, self.multi_clients, message)
                else:
                    from FileStream.services.storage_service import storage_service as default_storage_service
                    svc = self.storage_service or default_storage_service
                    for idx, cl in (self.multi_clients or {}).items():
                        if idx not in svc._clients_dict:
                            svc.register_client(idx, cl)
                    storage_res = await svc.store_file(
                        db_id=str(inserted_id),
                        file_id=info.get("file_id", ""),
                        file_name=info.get("file_name", "file"),
                        file_size=info.get("file_size", 0),
                        file_info=info,
                        message=message,
                        user_id=info.get("user_id"),
                        session_id=session_id,
                    )
                    if not storage_res or not getattr(storage_res, "success", False):
                        err_msg = getattr(storage_res, "error", None) or "Storage failed"
                        try:
                            from bson import ObjectId
                            if ObjectId.is_valid(inserted_id):
                                await self.db.file.update_one(
                                    {"_id": ObjectId(inserted_id)},
                                    {"$set": {"storage_status": "failed", "storage_error": err_msg}},
                                )
                        except Exception:
                            pass
                        raise RuntimeError(f"Storage failed for file {inserted_id}: {err_msg}")
                    else:
                        try:
                            from bson import ObjectId
                            if ObjectId.is_valid(inserted_id):
                                sets = {"storage_status": "stored"}
                                if getattr(storage_res, "message_id", None) is not None:
                                    sets["flog_msg_id"] = int(storage_res.message_id)
                                await self.db.file.update_one(
                                    {"_id": ObjectId(inserted_id)},
                                    {"$set": sets}
                                )
                        except Exception:
                            pass
            except Exception as e:
                from FileStream.core.resilience.floodwait import is_floodwait
                if not is_floodwait(e):
                    try:
                        from bson import ObjectId
                        if ObjectId.is_valid(inserted_id):
                            await self.db.file.update_one(
                                {"_id": ObjectId(inserted_id)},
                                {"$set": {"storage_status": "failed", "storage_error": str(e)}},
                            )
                    except Exception:
                        pass
                raise e
                
            # Trigger background media indexing (MP4 moov atom & scrub thumbnails)
            try:
                mime = str(info.get("mime_type") or "").lower()
                fname = str(info.get("file_name") or "").lower()
                is_video = "video" in mime or fname.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"))
                if is_video:
                    asyncio.create_task(self._trigger_background_indexing(str(inserted_id), info))
            except Exception:
                pass

            return str(inserted_id), True
        finally:
            workload_tracker.exit_file_processing()

    async def _trigger_background_indexing(self, db_id: str, info: dict) -> None:
        try:
            from FileStream.config import Server
            from FileStream.core.runtime.client_balance import choose_best_client
            from FileStream.core.media.custom_dl import ByteStreamer
            from FileStream.core.media.mp4_metadata import mp4_metadata_manager
            from FileStream.services.thumbnail_service import thumbnail_service

            if not self.multi_clients:
                return

            client_idx = choose_best_client(self.multi_clients.keys())
            client = self.multi_clients[client_idx]
            streamer = ByteStreamer(client)
            file_id = await streamer.get_file_properties(db_id, self.multi_clients)
            file_size = int(info.get("file_size") or getattr(file_id, "file_size", 0) or 0)
            fname = str(info.get("file_name") or "").lower()

            if getattr(Server, "MP4_FAST_SCRUB_ENABLED", True) and (fname.endswith(".mp4") or "mp4" in str(info.get("mime_type") or "")):
                await mp4_metadata_manager.fetch_and_index_moov(
                    streamer, file_id, db_id, file_size, client_idx
                )

            if getattr(Server, "THUMBNAILS_ENABLED", True):
                await thumbnail_service.ensure_thumbnails(
                    file_id=db_id,
                    byte_streamer=streamer,
                    tg_file_id=file_id,
                    client_index=client_idx,
                    file_size=file_size,
                )
        except Exception as e:
            logging.debug("Background media indexing skipped for %s: %s", db_id, e)
