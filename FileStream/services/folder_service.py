from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING, Callable, Awaitable
from bson.objectid import ObjectId

from FileStream.config import Telegram, Server
from FileStream.core.runtime.file_cleanup import invalidate_runtime_access
from FileStream.core.runtime.public_links import build_public_folder_url

if TYPE_CHECKING:
    from pyrogram import Client
    from FileStream.database.database import Database

logger = logging.getLogger(__name__)


class FolderState:
    IDLE = "idle"
    ACTIVE = "active"
    ADDING = "adding"
    PROCESSING = "processing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELING = "canceling"
    CANCELLED = "cancelled"


class FolderService:
    def __init__(self, db: Database | None = None, storage_service=None):
        self._db = db
        self._storage_service = storage_service
        self._sessions: dict[int, dict] = {}
        self.max_folder_items = getattr(Telegram, "MAX_FOLDER_ITEMS", 1000)

    @property
    def db(self) -> Database:
        if self._db is None:
            from FileStream.config import Telegram
            from FileStream.database.database import Database
            self._db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
        return self._db

    # --- Session Management ---

    def get_session(self, user_id: int) -> dict | None:
        return self._sessions.get(int(user_id))

    def clear_session(self, user_id: int) -> dict | None:
        return self._sessions.pop(int(user_id), None)

    async def get_active_session_for_user(self, user_id: int, bot: Client | None = None) -> dict | None:
        """Authoritative atomic lookup for a user's active folder session."""
        return await self.get_active_session(user_id, bot=bot)

    async def get_active_session(self, user_id: int, bot: Client | None = None) -> dict | None:
        uid = int(user_id)
        session = self._sessions.get(uid)
        timeout = getattr(Telegram, "FOLDER_SESSION_TIMEOUT", 3600)

        if session:
            # Check in-memory timeout
            if time.time() - session.get("updated_at", 0) > timeout:
                logger.info("Folder session for user %s expired in-memory, cleaning up", uid)
                await self.cancel_session(uid, bot=bot)
                logger.info(
                    "[FOLDER] FOLDER_SESSION_LOOKUP: user_id=%s session_id=None active=False available_session_users=%s",
                    uid,
                    list(self._sessions.keys()),
                )
                return None
            logger.info(
                "[FOLDER] FOLDER_SESSION_LOOKUP: user_id=%s session_id=%s active=True",
                uid,
                session.get("session_id"),
            )
            return session

        # Check database for active persistent session
        try:
            saved = await self.db.get_folder_session(uid, max_age_seconds=timeout)
            if saved:
                session = self._restore_session_from_db(uid, saved)
                self._sessions[uid] = session
                logger.info("Restored folder session for user %s from database", uid)
                logger.info(
                    "[FOLDER] FOLDER_SESSION_LOOKUP: user_id=%s session_id=%s active=True",
                    uid,
                    session.get("session_id"),
                )
                return session
        except Exception as exc:
            logger.error("Error retrieving folder session for user %s: %s", uid, exc)

        logger.info(
            "[FOLDER] FOLDER_SESSION_LOOKUP: user_id=%s session_id=None active=False available_session_users=%s",
            uid,
            list(self._sessions.keys()),
        )
        return None

    def _restore_session_from_db(self, user_id: int, saved: dict) -> dict:
        files = list(saved.get("files") or [])
        file_objects = list(saved.get("file_objects") or [])
        file_ids = [str(fid) for fid in files]
        file_unique_ids = set(saved.get("file_unique_ids") or [])
        recent_names = list(saved.get("recent_names") or [])
        session_id = str(saved.get("session_id") or secrets.token_hex(16))
        total_size = int(saved.get("total_size") or 0)
        file_count = int(saved.get("file_count") or len(file_ids))
        status_msg_id = saved.get("status_msg_id")
        chat_id = saved.get("chat_id") or user_id
        new_files = list(saved.get("new_files") or [])
        storage_messages = list(saved.get("storage_messages") or [])

        # Reconstruct file_unique_ids from file_objects if present
        for obj in file_objects:
            if isinstance(obj, dict) and obj.get("file_unique_id"):
                file_unique_ids.add(obj["file_unique_id"])

        return {
            "session_id": session_id,
            "user_id": int(user_id),
            "chat_id": int(chat_id),
            "status_msg_id": int(status_msg_id) if status_msg_id else None,
            "status": saved.get("status") or "active",
            "state": saved.get("state") or saved.get("status") or FolderState.ACTIVE,
            "files": file_objects if file_objects else [{"file_id": fid} for fid in file_ids],
            "file_ids": file_ids,
            "file_unique_ids": file_unique_ids,
            "new_files": new_files,
            "storage_messages": storage_messages,
            "recent_names": recent_names,
            "total_size": total_size,
            "file_count": file_count,
            "created_at": float(saved.get("created_at") or time.time()),
            "updated_at": float(saved.get("updated_at") or time.time()),
            "lock": asyncio.Lock(),
            "ui_lock": asyncio.Lock(),
            "refresh_lock": asyncio.Lock(),
            "refresh_pending": False,
            "refresh_task": None,
            "last_refresh_time": float(saved.get("updated_at") or time.time()),
            "last_ui_seq": 0,
            "last_ui_time": 0.0,
            "in_flight": 0,
            "completing": False,
            "cancelling": False,
            "completed": False,
            "cancelled": False,
            "processed_messages": set(),
            "pending_messages": set(),
            "pending_input_deletions": set(),
            "pending_input_messages": [],
        }

    async def create_session(
        self,
        user_id: int,
        chat_id: int,
        status_msg_id: int | None = None,
        bot: Client | None = None,
    ) -> dict:
        uid = int(user_id)
        bot_id = getattr(bot, "id", None) or getattr(getattr(bot, "me", None), "id", None)
        if not bot_id:
            try:
                from FileStream.config import Telegram
                if getattr(Telegram, "BOT_ID", None):
                    bot_id = int(Telegram.BOT_ID)
                elif getattr(Telegram, "BOT_TOKEN", None) and ":" in str(Telegram.BOT_TOKEN):
                    bot_id = int(str(Telegram.BOT_TOKEN).split(":")[0])
            except Exception:
                pass

        known_bot_ids = {8827985075}
        if bot_id:
            known_bot_ids.add(bot_id)

        if uid in known_bot_ids:
            logger.error("[FOLDER] FOLDER_USER_ID_MISMATCH: Refusing to create folder session for bot ID %s", uid)
            raise ValueError(f"Cannot create folder session for bot identity {uid}")

        logger.info(
            "[FOLDER] FOLDER_SESSION_CREATE_REQUEST: user_id=%s chat_id=%s status_msg_id=%s",
            uid,
            chat_id,
            status_msg_id,
        )

        now = time.time()
        session_id = secrets.token_hex(16)

        session = {
            "session_id": session_id,
            "user_id": uid,
            "chat_id": int(chat_id),
            "status_msg_id": int(status_msg_id) if status_msg_id else None,
            "status": "active",
            "state": FolderState.ACTIVE,
            "files": [],
            "file_ids": [],
            "file_unique_ids": set(),
            "new_files": [],
            "storage_messages": [],
            "recent_names": [],
            "total_size": 0,
            "file_count": 0,
            "created_at": now,
            "updated_at": now,
            "lock": asyncio.Lock(),
            "ui_lock": asyncio.Lock(),
            "refresh_lock": asyncio.Lock(),
            "refresh_pending": False,
            "refresh_task": None,
            "last_refresh_time": now,
            "last_ui_seq": 0,
            "last_ui_time": 0.0,
            "in_flight": 0,
            "completing": False,
            "cancelling": False,
            "completed": False,
            "cancelled": False,
            "processed_messages": set(),
            "pending_messages": set(),
            "pending_input_deletions": set(),
            "pending_input_messages": [],
        }
        self._sessions[uid] = session

        try:
            await self.db.save_folder_session(
                user_id=uid,
                chat_id=chat_id,
                status_msg_id=status_msg_id,
                files=[],
                recent_names=[],
                session_id=session_id,
                total_size=0,
                file_count=0,
                new_files=[],
                storage_messages=[],
                file_unique_ids=[],
                status="active",
            )
            logger.info("Folder session %s created for user %s", session_id, uid)
            logger.info(
                "[FOLDER] FOLDER_SESSION_CREATED: user=%s session=%s chat_id=%s",
                uid,
                session_id,
                chat_id,
            )
        except Exception as exc:
            logger.error("Failed to persist folder session for user %s: %s", uid, exc)

        return session

    def start_session(self, user_id: int, chat_id: int | None = None, status_msg_id: int | None = None) -> dict:
        """Backwards compatibility helper."""
        uid = int(user_id)
        now = time.time()
        session_id = secrets.token_hex(16)
        session = {
            "session_id": session_id,
            "user_id": uid,
            "chat_id": int(chat_id or uid),
            "status_msg_id": int(status_msg_id) if status_msg_id else None,
            "status": "active",
            "state": FolderState.ACTIVE,
            "files": [],
            "file_ids": [],
            "file_unique_ids": set(),
            "new_files": [],
            "storage_messages": [],
            "recent_names": [],
            "total_size": 0,
            "file_count": 0,
            "created_at": now,
            "updated_at": now,
            "lock": asyncio.Lock(),
            "ui_lock": asyncio.Lock(),
            "refresh_lock": asyncio.Lock(),
            "refresh_pending": False,
            "refresh_task": None,
            "last_refresh_time": now,
            "last_ui_seq": 0,
            "last_ui_time": 0.0,
            "in_flight": 0,
            "completing": False,
            "cancelling": False,
            "completed": False,
            "cancelled": False,
            "processed_messages": set(),
            "pending_messages": set(),
            "pending_input_deletions": set(),
            "pending_input_messages": [],
        }
        self._sessions[uid] = session
        return session

    async def add_file(
        self,
        user_id: int,
        file_info: dict,
        is_new: bool = False,
    ) -> tuple[bool, str, dict]:
        uid = int(user_id)
        session = await self.get_active_session(uid)
        if not session:
            return False, "no_session", {}

        async with session["lock"]:
            if session.get("completing") or session.get("completed") or session.get("cancelling") or session.get("cancelled"):
                logger.info("[FOLDER] FOLDER_FILE_CANCELLED: user=%s session=%s - session is no longer active", uid, session.get("session_id"))
                return False, "in_progress", session

            if session.get("file_count", 0) >= self.max_folder_items:
                return False, "limit", session

            file_id = str(file_info.get("_id") or "")
            file_unique_id = file_info.get("file_unique_id")

            # Validate that file has not failed storage
            if file_info.get("storage_status") in ("failed", "cancelled"):
                logger.error("[FOLDER] FOLDER_FILE_FAILED: user=%s session=%s Refusing to add file with failed storage status: %s", uid, session.get("session_id"), file_id)
                return False, "storage_failed", session

            # Duplicate protection: check both file_unique_id and file_id
            if file_unique_id and file_unique_id in session["file_unique_ids"]:
                logger.info("[FOLDER] FOLDER_FILE_DUPLICATE: user=%s session=%s unique_id=%s file_id=%s", uid, session.get("session_id"), file_unique_id, file_id)
                return False, "duplicate", session

            if file_id and file_id in session["file_ids"]:
                logger.info("[FOLDER] FOLDER_FILE_DUPLICATE: user=%s session=%s file_id=%s", uid, session.get("session_id"), file_id)
                return False, "duplicate", session

            file_size = int(file_info.get("file_size") or 0)
            file_name = file_info.get("file_name") or "file"
            flog_msg_id = file_info.get("flog_msg_id")
            flog_channel_id = file_info.get("flog_channel_id")

            entry = {
                "file_id": file_id,
                "file_name": file_name,
                "file_size": file_size,
                "file_unique_id": file_unique_id,
                "is_new": bool(is_new),
                "session_id": session["session_id"],
                "flog_msg_id": flog_msg_id,
                "flog_channel_id": flog_channel_id,
            }

            session["files"].append(entry)
            session["file_ids"].append(file_id)
            if file_unique_id:
                session["file_unique_ids"].add(file_unique_id)

            if is_new:
                session["new_files"].append(file_id)
                # Tag folder_session_id in database
                try:
                    if ObjectId.is_valid(file_id):
                        await self.db.file.update_one(
                            {"_id": ObjectId(file_id)},
                            {"$set": {"folder_session_id": session["session_id"], "is_folder_file": True}},
                        )
                except Exception as exc:
                    logger.warning("Failed to tag folder_session_id on file %s: %s", file_id, exc)

            if flog_msg_id and flog_channel_id:
                session["storage_messages"].append({
                    "channel_id": flog_channel_id,
                    "message_id": flog_msg_id,
                    "file_id": file_id,
                })

            session["recent_names"].append(file_name)
            if len(session["recent_names"]) > 10:
                session["recent_names"] = session["recent_names"][-10:]
            session["file_count"] = len(session["files"])
            session["total_size"] = sum(int(f.get("file_size") or 0) for f in session["files"])
            session["updated_at"] = time.time()

            # Update persistent session
            try:
                if hasattr(self.db, "append_folder_session_file"):
                    storage_ref = {
                        "channel_id": flog_channel_id,
                        "message_id": flog_msg_id,
                        "file_id": file_id,
                    } if (flog_msg_id and flog_channel_id) else None
                    await self.db.append_folder_session_file(
                        uid,
                        entry,
                        recent_name=file_name,
                        file_size=file_size,
                        is_new=is_new,
                        storage_message=storage_ref,
                        file_unique_id=file_unique_id,
                    )
                else:
                    await self.db.update_folder_session_files(
                        uid,
                        files=session["files"],
                        recent_names=session["recent_names"],
                        status_msg_id=session.get("status_msg_id"),
                        total_size=session["total_size"],
                        file_count=session["file_count"],
                        new_files=session["new_files"],
                        storage_messages=session["storage_messages"],
                        file_unique_ids=list(session["file_unique_ids"]),
                        session_id=session["session_id"],
                    )
                logger.info(
                    "[FOLDER] FOLDER_FILE_REGISTERED: user=%s session=%s file_id=%s name='%s' count=%s total_size=%s",
                    uid,
                    session["session_id"],
                    file_id,
                    file_name,
                    session["file_count"],
                    session["total_size"],
                )
            except Exception as exc:
                logger.error("Failed to update folder session in database for user %s: %s", uid, exc)

            return True, "added", session

    async def finalize_folder(
        self,
        user_id: int,
        bot_id: int | None = None,
        bot: Client | None = None,
        title: str = "Shared Folder",
    ) -> tuple[dict, str]:
        uid = int(user_id)
        session = await self.get_active_session(uid)
        if not session or not session.get("file_ids"):
            raise ValueError("No files have been added")

        if session.get("in_flight", 0) > 0 or len(session.get("pending_messages", set())) > 0:
            raise ValueError("Files are still processing in this folder")

        session_id = session.get("session_id")
        if session_id and hasattr(self.db, "count_active_storage_jobs_for_session"):
            try:
                active_storage_jobs = await self.db.count_active_storage_jobs_for_session(session_id)
                if active_storage_jobs > 0:
                    raise ValueError("Files are still processing in this folder")
            except ValueError:
                raise
            except Exception as e:
                logger.warning("Error checking active storage jobs for session %s: %s", session_id, e)

        async with session["lock"]:
            if (session.get("completing") and session.get("state") != FolderState.FINALIZING) or session.get("completed"):
                raise ValueError("Folder is already being finalized")
            session["completing"] = True
            session["state"] = FolderState.FINALIZING

            file_ids = list(session["file_ids"])
            if not file_ids:
                raise ValueError("No files have been added")

            total_size = sum(int(f.get("file_size") or 0) for f in session.get("files") or []) or int(session.get("total_size") or 0)
            session["total_size"] = total_size
            session["file_count"] = len(file_ids)
            public_token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
            storage_refs = list(session.get("storage_messages") or [])

            folder_id = await self.db.create_folder(
                uid,
                file_ids,
                bot_id=bot_id,
                title=title,
                total_size=total_size,
                public_token=public_token,
                storage_message_refs=storage_refs,
                expires_at=None,
            )

            # Associate all uploaded files with the newly created folder and clear session tag
            try:
                valid_ids = [ObjectId(fid) for fid in file_ids if ObjectId.is_valid(fid)]
                if valid_ids:
                    await self.db.file.update_many(
                        {"_id": {"$in": valid_ids}},
                        {
                            "$addToSet": {"folders": folder_id},
                            "$set": {"folder_id": folder_id, "is_folder_file": True},
                            "$unset": {"folder_session_id": ""},
                        },
                    )
            except Exception as exc:
                logger.warning("Failed to update folder references in files: %s", exc)

            folder_doc = await self.db.get_folder(folder_id)
            session["completed"] = True
            session["state"] = FolderState.COMPLETED
            self.clear_session(uid)

            try:
                await self.db.delete_folder_session(uid)
            except Exception:
                pass

            public_url = build_public_folder_url(public_token, bot=bot, folder=folder_doc)
            logger.info("[FOLDER] FOLDER_BATCH_COMPLETE: user=%s session=%s total_files=%s total_size=%s folder_id=%s public_token=%s", uid, session.get("session_id"), len(file_ids), total_size, folder_id, public_token)
            return folder_doc, public_url

    async def cancel_session(
        self,
        user_id: int,
        bot: Client | None = None,
        progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> bool:
        uid = int(user_id)
        session = self._sessions.get(uid)
        if not session:
            try:
                saved = await self.db.get_folder_session(uid)
                if saved:
                    session = self._restore_session_from_db(uid, saved)
            except Exception:
                pass

        if not session:
            try:
                await self.db.delete_folder_session(uid)
            except Exception:
                pass
            if progress_callback:
                try:
                    await progress_callback(0, 0)
                except Exception:
                    pass
            return True

        async with session["lock"]:
            if session.get("cancelling") or session.get("cancelled"):
                return True
            session["cancelling"] = True
            session["state"] = FolderState.CANCELING

            session_id = session.get("session_id")

            # Prune pending / queued jobs from StorageService and MongoDB
            try:
                from FileStream.services.storage_service import storage_service as default_storage_service
                svc = self._storage_service or default_storage_service
                if session_id:
                    svc.cancel_session_jobs(session_id)
            except Exception as exc:
                logger.debug("Failed to cancel storage service jobs for session %s: %s", session_id, exc)

            if session_id and hasattr(self.db, "cancel_storage_jobs_for_session"):
                try:
                    await self.db.cancel_storage_jobs_for_session(session_id)
                except Exception as exc:
                    logger.debug("Failed to cancel database storage jobs for session %s: %s", session_id, exc)

            new_file_ids = list(session.get("new_files") or [])

            # Also query DB for any temporary files tagged with this folder_session_id
            db_tagged_ids = []
            if session_id:
                try:
                    cursor = self.db.file.find({"folder_session_id": session_id, "user_id": uid}, {"_id": 1})
                    async for doc in cursor:
                        db_tagged_ids.append(str(doc["_id"]))
                except Exception:
                    pass

            all_cleanup_ids = list(dict.fromkeys(new_file_ids + db_tagged_ids))
            total_files = len(all_cleanup_ids)

            if progress_callback:
                try:
                    await progress_callback(0, total_files)
                except Exception:
                    pass

            if total_files == 0:
                session["cancelled"] = True
                session["state"] = FolderState.CANCELLED
                session.get("pending_input_deletions", set()).clear()
                session.get("pending_input_messages", []).clear()
                self.clear_session(uid)
                try:
                    await self.db.delete_folder_session(uid)
                except Exception:
                    pass
                if progress_callback:
                    try:
                        await progress_callback(0, 0)
                    except Exception:
                        pass
                logger.info("Folder session %s cancelled (0 files) for user %s", session_id, uid)
                return True

            cleaned_count = 0
            batch_size = 100

            for i in range(0, total_files, batch_size):
                batch_fids = all_cleanup_ids[i: i + batch_size]
                valid_obj_ids = [ObjectId(fid) for fid in batch_fids if ObjectId.is_valid(fid)]

                # Fetch metadata for files in this batch
                channel_msg_ids: dict[int, list[int]] = {}
                try:
                    if valid_obj_ids:
                        cursor = self.db.file.find({"_id": {"$in": valid_obj_ids}})
                        async for f_doc in cursor:
                            ch_id = f_doc.get("flog_channel_id")
                            m_id = f_doc.get("flog_msg_id")
                            if ch_id and m_id:
                                channel_msg_ids.setdefault(int(ch_id), []).append(int(m_id))
                except Exception as fetch_exc:
                    logger.warning("Error fetching file docs for cleanup batch: %s", fetch_exc)

                # Also cross-check with session storage_messages for any items in this batch
                for s_ref in (session.get("storage_messages") or []):
                    if s_ref.get("file_id") in batch_fids:
                        ch_id = s_ref.get("channel_id")
                        m_id = s_ref.get("message_id")
                        if ch_id and m_id:
                            channel_msg_ids.setdefault(int(ch_id), []).append(int(m_id))

                # Batched deletion of Telegram storage messages
                if bot and channel_msg_ids:
                    for ch_id, msg_list in channel_msg_ids.items():
                        unique_mids = list(dict.fromkeys(msg_list))
                        for sub_i in range(0, len(unique_mids), 100):
                            sub_chunk = unique_mids[sub_i: sub_i + 100]
                            try:
                                await bot.delete_messages(ch_id, sub_chunk)
                                logger.info("Deleted %d storage messages in channel %s for session %s", len(sub_chunk), ch_id, session_id)
                            except Exception as del_exc:
                                logger.warning("Bulk delete failed in channel %s (%s); attempting fallback", ch_id, del_exc)
                                for single_mid in sub_chunk:
                                    try:
                                        await bot.delete_messages(ch_id, single_mid)
                                    except Exception:
                                        pass

                # Bulk database removal
                try:
                    if valid_obj_ids:
                        if hasattr(self.db.file, "delete_many"):
                            await self.db.file.delete_many({"_id": {"$in": valid_obj_ids}})
                        else:
                            for oid in valid_obj_ids:
                                await self.db.delete_one_file(oid)
                    if hasattr(self.db, "public_links") and hasattr(self.db.public_links, "delete_many"):
                        await self.db.public_links.delete_many({"target_id": {"$in": batch_fids}})
                except Exception as db_del_exc:
                    logger.warning("Database bulk deletion failed in cancelled session: %s", db_del_exc)

                # Invalidate runtime access caches
                for fid in batch_fids:
                    invalidate_runtime_access(fid)

                cleaned_count += len(batch_fids)
                if progress_callback:
                    try:
                        await progress_callback(cleaned_count, total_files)
                    except Exception as p_err:
                        logger.debug("Cancel progress callback error: %s", p_err)

            session["cancelled"] = True
            session["state"] = FolderState.CANCELLED
            session.get("pending_input_deletions", set()).clear()
            session.get("pending_input_messages", []).clear()
            self.clear_session(uid)

            try:
                await self.db.delete_folder_session(uid)
            except Exception as exc:
                logger.warning("Failed to delete folder session from DB for user %s: %s", uid, exc)

            logger.info("Folder session %s cancelled and cleaned up (%d files) for user %s", session_id, cleaned_count, uid)
            return True

    async def cleanup_expired_sessions(self, bot: Client | None = None) -> int:
        timeout = getattr(Telegram, "FOLDER_SESSION_TIMEOUT", 3600)
        cutoff = time.time() - timeout
        cleaned = 0
        try:
            cursor = self.db.folder_sessions.find({"updated_at": {"$lt": cutoff}})
            async for doc in cursor:
                uid = doc.get("user_id") or doc.get("_id")
                if uid:
                    try:
                        await self.cancel_session(int(uid), bot=bot)
                        cleaned += 1
                        logger.info("Cleaned up expired session for user %s", uid)
                    except Exception as exc:
                        logger.warning("Error cleaning up expired session for user %s: %s", uid, exc)
        except Exception as exc:
            logger.error("Error running cleanup_expired_sessions: %s", exc)
        return cleaned

    # --- Delegated DB methods for backwards compatibility ---
    async def create_folder(self, user_id: int, file_ids: list[str], bot_id: int | None = None) -> str:
        return await self.db.create_folder(int(user_id), file_ids, bot_id=bot_id)

    async def get_folder(self, folder_id: str) -> dict | None:
        return await self.db.get_folder(str(folder_id))

    async def get_folder_for_user(self, folder_id: str, user_id: int) -> dict | None:
        return await self.db.get_folder_for_user(str(folder_id), int(user_id))

    async def list_folders(self, user_id: int, page_range: tuple[int, int]):
        return await self.db.list_folders(int(user_id), page_range)

    async def total_folders(self, user_id: int) -> int:
        return await self.db.total_folders(int(user_id))

    async def update_folder_title(self, folder_id: str, user_id: int, title: str) -> bool:
        res = await self.db.update_folder_title(str(folder_id), int(user_id), title)
        return bool(res and getattr(res, "modified_count", 0) > 0)

    async def delete_folder(self, folder_id: str, user_id: int) -> bool:
        res = await self.db.delete_folder(str(folder_id), int(user_id))
        return bool(res and getattr(res, "deleted_count", 0) > 0)

    async def get_public_folder_context(self, folder_ref, bot=None) -> tuple[dict, dict, str]:
        folder = folder_ref if isinstance(folder_ref, dict) else await self.get_folder(folder_ref)
        link_doc = await self.db.ensure_public_link_for_folder(folder)
        public_url = build_public_folder_url(link_doc["public_id"], bot=bot, folder=folder)
        return folder, link_doc, public_url


folder_service = FolderService()
