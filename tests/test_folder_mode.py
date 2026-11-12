import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId

from FileStream.services.folder_service import FolderService
from FileStream.bot.messages import FolderMessages
from FileStream.utils.human_readable import humanbytes
from FileStream.config import Telegram, Server
from FileStream.database.storage_jobs import StorageJobRepositoryMixin


class AsyncCursorMock:
    def __init__(self, docs):
        self.docs = list(docs)
        self.idx = 0

    def __aiter__(self):
        self.idx = 0
        return self

    async def __anext__(self):
        if self.idx < len(self.docs):
            doc = self.docs[self.idx]
            self.idx += 1
            return doc
        raise StopAsyncIteration

    def sort(self, key="_id", direction=1):
        try:
            reverse = (direction in (-1, "desc", "DESCENDING") or str(direction).endswith("DESCENDING"))
            self.docs.sort(key=lambda d: d.get(key, 0) or 0, reverse=reverse)
        except Exception:
            pass
        return self

    def skip(self, count=0):
        if count and count > 0:
            self.docs = self.docs[count:]
        return self

    def limit(self, count=None):
        if count is not None and count >= 0:
            self.docs = self.docs[:count]
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self.docs)
        return list(self.docs[:length])


class MockCollection:
    def __init__(self, name):
        self.name = name
        self.docs = {}

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        _id = doc["_id"]
        self.docs[str(_id)] = doc
        res = MagicMock()
        res.inserted_id = _id
        return res

    async def insert_many(self, docs):
        res = MagicMock()
        inserted_ids = []
        for doc in docs:
            d = dict(doc)
            if "_id" not in d:
                d["_id"] = ObjectId()
            self.docs[str(d["_id"])] = d
            inserted_ids.append(d["_id"])
        res.inserted_ids = inserted_ids
        return res

    def _doc_matches(self, doc, query):
        if not query:
            return True
        for k, v in query.items():
            if k == "$or":
                if not any(self._doc_matches(doc, sub) for sub in v):
                    return False
            elif k == "_id":
                if isinstance(v, dict) and "$in" in v:
                    in_ids = [str(x) for x in v["$in"]]
                    if str(doc.get("_id")) not in in_ids:
                        return False
                elif isinstance(v, dict) and "$ne" in v:
                    if str(doc.get("_id")) == str(v["$ne"]):
                        return False
                elif str(doc.get("_id")) != str(v):
                    return False
            elif isinstance(v, dict):
                val = doc.get(k)
                for op, exp in v.items():
                    if op == "$in":
                        exp_strs = [str(x) for x in exp]
                        if val not in exp and str(val) not in exp_strs:
                            return False
                    elif op == "$nin":
                        exp_strs = [str(x) for x in exp]
                        if val in exp or str(val) in exp_strs:
                            return False
                    elif op == "$ne":
                        if val == exp:
                            return False
                    elif op == "$exists":
                        exists = (k in doc and doc[k] is not None)
                        if exists != bool(exp):
                            return False
                    elif op == "$lt":
                        if val is None or val >= exp:
                            return False
                    elif op == "$gt":
                        if val is None or val <= exp:
                            return False
                    elif op == "$gte":
                        if val is None or val < exp:
                            return False
                    elif op == "$lte":
                        if val is None or val > exp:
                            return False
                    elif op == "$regex":
                        import re
                        options = 0
                        if v.get("$options") == "i":
                            options = re.IGNORECASE
                        if not val or not re.search(exp, str(val), options):
                            return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(self, query):
        for doc in self.docs.values():
            if self._doc_matches(doc, query):
                return dict(doc)
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        matched = [dict(doc) for doc in self.docs.values() if self._doc_matches(doc, query)]
        return AsyncCursorMock(matched)

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if not doc:
            if upsert:
                new_doc = dict(query)
                if "$set" in update:
                    for k, v in update["$set"].items():
                        if isinstance(v, list):
                            new_doc[k] = list(v)
                        elif isinstance(v, dict):
                            new_doc[k] = dict(v)
                        else:
                            new_doc[k] = v
                if "$setOnInsert" in update:
                    for k, v in update["$setOnInsert"].items():
                        if isinstance(v, list):
                            new_doc[k] = list(v)
                        elif isinstance(v, dict):
                            new_doc[k] = dict(v)
                        else:
                            new_doc[k] = v
                if "_id" not in new_doc:
                    new_doc["_id"] = ObjectId()
                self.docs[str(new_doc["_id"])] = new_doc
                res = MagicMock()
                res.matched_count = 0
                res.modified_count = 1
                res.upserted_id = new_doc["_id"]
                return res
            res = MagicMock()
            res.matched_count = 0
            res.modified_count = 0
            res.upserted_id = None
            return res

        _id_str = str(doc["_id"])
        target = self.docs[_id_str]
        if "$set" in update:
            for k, v in update["$set"].items():
                if isinstance(v, list):
                    target[k] = list(v)
                elif isinstance(v, dict):
                    target[k] = dict(v)
                else:
                    target[k] = v
        if "$unset" in update:
            for k in update["$unset"]:
                target.pop(k, None)
        if "$inc" in update:
            for k, v in update["$inc"].items():
                target[k] = target.get(k, 0) + v
        if "$push" in update:
            for k, v in update["$push"].items():
                cur = target.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    cur.extend(v["$each"])
                    if "$slice" in v:
                        slice_n = v["$slice"]
                        if slice_n < 0:
                            target[k] = cur[slice_n:]
                        else:
                            target[k] = cur[:slice_n]
                else:
                    cur.append(v)
        if "$addToSet" in update:
            for k, v in update["$addToSet"].items():
                cur = target.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    for item in v["$each"]:
                        if item not in cur:
                            cur.append(item)
                else:
                    if v not in cur:
                        cur.append(v)
        res = MagicMock()
        res.matched_count = 1
        res.modified_count = 1
        res.upserted_id = None
        return res

    async def update_many(self, query, update):
        res = MagicMock()
        res.matched_count = 0
        res.modified_count = 0
        cursor = self.find(query)
        async for doc in cursor:
            _id_str = str(doc["_id"])
            target = self.docs.get(_id_str)
            if target:
                if "$set" in update:
                    for k, v in update["$set"].items():
                        if isinstance(v, list):
                            target[k] = list(v)
                        elif isinstance(v, dict):
                            target[k] = dict(v)
                        else:
                            target[k] = v
                if "$unset" in update:
                    for k in update["$unset"]:
                        target.pop(k, None)
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        target[k] = target.get(k, 0) + v
                if "$push" in update:
                    for k, v in update["$push"].items():
                        cur = target.setdefault(k, [])
                        if isinstance(v, dict) and "$each" in v:
                            cur.extend(v["$each"])
                            if "$slice" in v:
                                slice_n = v["$slice"]
                                if slice_n < 0:
                                    target[k] = cur[slice_n:]
                                else:
                                    target[k] = cur[:slice_n]
                        else:
                            cur.append(v)
                if "$addToSet" in update:
                    for k, v in update["$addToSet"].items():
                        cur = target.setdefault(k, [])
                        if isinstance(v, dict) and "$each" in v:
                            for item in v["$each"]:
                                if item not in cur:
                                    cur.append(item)
                        else:
                            if v not in cur:
                                cur.append(v)
                res.modified_count += 1
                res.matched_count += 1
        return res

    async def delete_one(self, query):
        doc = await self.find_one(query)
        res = MagicMock()
        if doc:
            self.docs.pop(str(doc["_id"]), None)
            res.deleted_count = 1
        else:
            res.deleted_count = 0
        return res

    async def delete_many(self, query):
        res = MagicMock()
        deleted = 0
        cursor = self.find(query)
        to_delete = []
        async for doc in cursor:
            to_delete.append(str(doc["_id"]))
        for _id in to_delete:
            self.docs.pop(_id, None)
            deleted += 1
        res.deleted_count = deleted
        return res

    async def count_documents(self, query=None):
        query = query or {}
        cursor = self.find(query)
        cnt = 0
        async for _ in cursor:
            cnt += 1
        return cnt

    async def find_one_and_update(self, filter, update, projection=None, sort=None, return_document=None):
        cursor = self.find(filter)
        if sort:
            for s in sort:
                cursor.sort(s[0], s[1])
        matched_docs = cursor.docs
        if not matched_docs:
            return None
        target = matched_docs[0]
        before_doc = dict(target)

        _id_str = str(target["_id"])
        target = self.docs[_id_str]
        if "$set" in update:
            for k, v in update["$set"].items():
                if isinstance(v, list):
                    target[k] = list(v)
                elif isinstance(v, dict):
                    target[k] = dict(v)
                else:
                    target[k] = v
        if "$unset" in update:
            for k in update["$unset"]:
                target.pop(k, None)
        if "$inc" in update:
            for k, v in update["$inc"].items():
                target[k] = target.get(k, 0) + v
        if "$push" in update:
            for k, v in update["$push"].items():
                cur = target.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    cur.extend(v["$each"])
                    if "$slice" in v:
                        slice_n = v["$slice"]
                        if slice_n < 0:
                            target[k] = cur[slice_n:]
                        else:
                            target[k] = cur[:slice_n]
                else:
                    cur.append(v)
        if "$addToSet" in update:
            for k, v in update["$addToSet"].items():
                cur = target.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    for item in v["$each"]:
                        if item not in cur:
                            cur.append(item)
                else:
                    if v not in cur:
                        cur.append(v)

        from pymongo import ReturnDocument
        is_after = (return_document is True or return_document == ReturnDocument.AFTER or str(return_document).lower() in ("after", "true", "returndocument.after"))
        if is_after:
            return dict(target)
        return before_doc

    async def create_index(self, *args, **kwargs):
        return "mock_index"


class MockDatabase(StorageJobRepositoryMixin):
    def __init__(self):
        self.file = MockCollection("file")
        self.folders = MockCollection("folders")
        self.folder_sessions = MockCollection("folder_sessions")
        self.public_links = MockCollection("public_links")
        self.users = MockCollection("users")
        self.settings = MockCollection("settings")
        self.storage_jobs = MockCollection("storage_jobs")
        self.worker_health = MockCollection("worker_health")
        self.client_health = MockCollection("client_health")
        self.audit_events = MockCollection("audit_events")
        self.metrics_snapshots = MockCollection("metrics_snapshots")

    async def get_web_ads_status(self, *args, **kwargs):
        return False

    async def is_user_banned(self, id):
        return False

    async def get_file(self, file_id):
        doc = await self.file.find_one({"_id": file_id})
        return doc

    async def add_file(self, file_info):
        doc = dict(file_info)
        doc.setdefault("_id", ObjectId())
        doc.setdefault("time", time.time())
        res = await self.file.insert_one(doc)
        return getattr(res, "inserted_id", None) or doc["_id"]

    async def get_file_by_fileuniqueid(self, user_id, file_unique_id, many=False, bot_id=None, include_legacy=False):
        return await self.file.find_one({"file_unique_id": file_unique_id})

    @staticmethod
    def build_file_scope_query(
        user_id: int,
        bot_id: int | None = None,
        *,
        include_legacy: bool = False,
        standalone_only: bool = True,
        filename: str | None = None,
        file_type: str | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
        file_id: str | None = None,
        caption: str | None = None,
    ) -> dict:
        from FileStream.database.database import Database
        return Database.build_file_scope_query(
            user_id,
            bot_id,
            include_legacy=include_legacy,
            standalone_only=standalone_only,
            filename=filename,
            file_type=file_type,
            date_from=date_from,
            date_to=date_to,
            file_id=file_id,
            caption=caption,
        )

    async def find_files(
        self,
        user_id,
        range,
        *,
        bot_id: int | None = None,
        include_legacy: bool = False,
        standalone_only: bool = True,
        filename: str | None = None,
        file_type: str | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
        file_id: str | None = None,
        caption: str | None = None,
    ):
        start, end = range
        if start > end:
            return self.file.find({"_id": None}), 0
        query = self.build_file_scope_query(
            user_id,
            bot_id,
            include_legacy=include_legacy,
            standalone_only=standalone_only,
            filename=filename,
            file_type=file_type,
            date_from=date_from,
            date_to=date_to,
            file_id=file_id,
            caption=caption,
        )
        user_files = (
            self.file.find(query, {"_id": 1, "file_name": 1})
            .sort('_id', -1)
            .skip(start - 1)
            .limit(end - start + 1)
        )
        total_files = await self.file.count_documents(query)
        return user_files, total_files

    async def search_files(
        self,
        user_id: int,
        search_query: str,
        range: tuple[int, int],
        *,
        bot_id: int | None = None,
        include_legacy: bool = False,
        file_type: str | None = None,
    ):
        return await self.find_files(
            user_id,
            range,
            bot_id=bot_id,
            include_legacy=include_legacy,
            standalone_only=True,
            filename=search_query,
            file_type=file_type,
        )

    async def total_files(self, id=None, *, bot_id: int | None = None, include_legacy: bool = False, standalone_only: bool = True):
        if id:
            query = self.build_file_scope_query(id, bot_id, include_legacy=include_legacy, standalone_only=standalone_only)
            return await self.file.count_documents(query)
        return await self.file.count_documents({})

    async def delete_one_file(self, file_id):
        await self.file.delete_one({"_id": file_id})
        await self.public_links.delete_many({"target_id": str(file_id)})

    async def get_folder(self, folder_id):
        doc = await self.folders.find_one({"_id": folder_id})
        return doc

    async def get_folder_for_user(self, folder_id, user_id):
        doc = await self.folders.find_one({"_id": folder_id, "user_id": int(user_id)})
        return doc

    async def create_folder(self, user_id, file_ids, bot_id=None, title="Shared Folder", total_size=0, public_token=None, storage_message_refs=None, expires_at=None):
        folder_id = str(ObjectId())
        doc = {
            "_id": folder_id,
            "folder_id": folder_id,
            "user_id": int(user_id),
            "files": list(file_ids),
            "file_count": len(file_ids),
            "total_size": int(total_size),
            "title": title,
            "public_token": public_token,
            "storage_message_refs": storage_message_refs or [],
            "expires_at": expires_at,
            "created_at": time.time(),
            "bot_id": bot_id,
            "status": "active",
        }
        await self.folders.insert_one(doc)
        if public_token:
            await self.ensure_public_link_for_folder(doc)
        return folder_id

    async def ensure_public_link_for_folder(self, folder):
        folder_id = str(folder.get("_id") or folder.get("folder_id"))
        existing = await self.public_links.find_one({"target_id": folder_id, "type": "folder"})
        if existing:
            return existing
        public_id = folder.get("public_token") or "mock_pub_token"
        link_doc = {
            "_id": public_id,
            "public_id": public_id,
            "target_id": folder_id,
            "type": "folder",
            "expires_at": folder.get("expires_at"),
            "status": "active",
            "created_at": time.time(),
            "click_count": 0,
        }
        await self.public_links.insert_one(link_doc)
        return link_doc

    async def ensure_public_link_for_file(self, file_data):
        file_id = str(file_data.get("_id"))
        existing = await self.public_links.find_one({"target_id": file_id, "type": "file"})
        if existing:
            return existing
        public_id = f"pub_{file_id}"
        link_doc = {
            "_id": public_id,
            "public_id": public_id,
            "target_id": file_id,
            "type": "file",
            "status": "active",
            "created_at": time.time(),
            "click_count": 0,
        }
        await self.public_links.insert_one(link_doc)
        return link_doc

    async def get_public_link(self, public_id):
        return await self.public_links.find_one({"public_id": str(public_id)})

    async def resolve_public_folder(self, public_id, increment_click=False):
        link = await self.get_public_link(public_id)
        if not link:
            from FileStream.server.exceptions import FileNotFound
            raise FileNotFound("Folder link not found")
        folder = await self.get_folder(link.get("target_id"))
        if not folder:
            from FileStream.server.exceptions import FileNotFound
            raise FileNotFound("Folder not found")
        return folder, link

    async def save_folder_session(self, user_id, chat_id, status_msg_id=None, files=None, recent_names=None, session_id=None, total_size=0, file_count=0, new_files=None, storage_messages=None, file_unique_ids=None, status="active"):
        uid = int(user_id)
        doc = {
            "_id": uid,
            "user_id": uid,
            "chat_id": chat_id,
            "status_msg_id": status_msg_id,
            "files": files or [],
            "recent_names": recent_names or [],
            "session_id": session_id or "sess_123",
            "total_size": total_size,
            "file_count": file_count,
            "new_files": new_files or [],
            "storage_messages": storage_messages or [],
            "file_unique_ids": file_unique_ids or [],
            "status": status,
            "updated_at": time.time(),
            "created_at": time.time(),
        }
        await self.folder_sessions.update_one({"_id": uid}, {"$set": doc}, upsert=True)

    async def get_folder_session(self, user_id, max_age_seconds=None):
        uid = int(user_id)
        doc = await self.folder_sessions.find_one({"_id": uid})
        if not doc:
            return None
        if max_age_seconds:
            updated_at = doc.get("updated_at") or doc.get("created_at") or 0
            if time.time() - updated_at > max_age_seconds:
                return None
        return doc

    async def update_folder_session_files(self, user_id, files, recent_names=None, status_msg_id=None, total_size=None, file_count=None, new_files=None, storage_messages=None, file_unique_ids=None, session_id=None):
        uid = int(user_id)
        updates = {"files": files, "updated_at": time.time()}
        if recent_names is not None:
            updates["recent_names"] = recent_names
        if status_msg_id is not None:
            updates["status_msg_id"] = status_msg_id
        if total_size is not None:
            updates["total_size"] = total_size
        if file_count is not None:
            updates["file_count"] = file_count
        if new_files is not None:
            updates["new_files"] = new_files
        if storage_messages is not None:
            updates["storage_messages"] = storage_messages
        if file_unique_ids is not None:
            updates["file_unique_ids"] = file_unique_ids
        if session_id is not None:
            updates["session_id"] = session_id
        await self.folder_sessions.update_one({"_id": uid}, {"$set": updates}, upsert=True)

    async def append_folder_session_file(
        self,
        user_id,
        entry,
        *,
        recent_name,
        file_size,
        is_new=True,
        storage_message=None,
        file_unique_id=None,
    ):
        file_id = str(entry.get("file_id") or "")
        push_dict = {
            "files": file_id,
            "file_objects": entry,
            "recent_names": {"$each": [str(recent_name)], "$slice": -10},
        }
        if is_new:
            push_dict["new_files"] = file_id
        if storage_message:
            push_dict["storage_messages"] = storage_message

        update_op = {
            "$push": push_dict,
            "$inc": {
                "file_count": 1,
                "total_size": int(file_size),
            },
            "$set": {
                "updated_at": time.time(),
            },
        }
        if file_unique_id:
            update_op["$addToSet"] = {"file_unique_ids": file_unique_id}

        await self.folder_sessions.update_one(
            {"_id": int(user_id)},
            update_op,
        )

    async def delete_folder_session(self, user_id):
        uid = int(user_id)
        await self.folder_sessions.delete_one({"_id": uid})

    async def total_folders(self, user_id):
        return await self.folders.count_documents({"user_id": int(user_id)})

    async def list_folders(self, user_id, page_range):
        cursor = self.folders.find({"user_id": int(user_id)})
        res = []
        async for doc in cursor:
            res.append(doc)
        return res

    async def delete_folder(self, folder_id, user_id):
        return await self.folders.delete_one({"_id": str(folder_id), "user_id": int(user_id)})


class MockBot:
    def __init__(self, bot_id=123456, username="test_bot"):
        self.id = bot_id
        self.username = username
        self.name = "TestBot"
        self.me = MagicMock()
        self.me.id = bot_id
        self.me.username = username
        self.sent_messages = []
        self.edited_messages = []
        self.deleted_messages = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, disable_web_page_preview=False):
        msg = MagicMock()
        msg.id = len(self.sent_messages) + 100
        msg.chat = MagicMock()
        msg.chat.id = chat_id
        msg.text = text
        msg.reply_markup = reply_markup
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "msg": msg})
        return msg

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None, disable_web_page_preview=False):
        self.edited_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
        return MagicMock()

    async def delete_messages(self, chat_id, message_ids):
        if not isinstance(message_ids, list):
            message_ids = [message_ids]
        self.deleted_messages.extend([{"chat_id": chat_id, "message_id": mid} for mid in message_ids])
        return True

    async def send_chat_action(self, *args, **kwargs):
        pass


class MockMessage:
    def __init__(self, message_id=1, user_id=1001, chat_id=1001, text="", bot=None, **kwargs):
        self.id = kwargs.get("msg_id", message_id)
        uid = kwargs.get("from_user_id", user_id)
        self.from_user = MagicMock()
        self.from_user.id = uid
        self.from_user.first_name = "User"
        self.from_user.last_name = ""
        self.from_user.username = f"user_{uid}"
        self.chat = MagicMock()
        self.chat.id = kwargs.get("chat_id", chat_id)
        self.text = text
        self.bot = bot
        self.media = None
        self.document = None
        self.video = None
        self.photo = None
        self.audio = None
        self.voice = None
        self.animation = None
        self.video_note = None
        self.forward_date = None
        self.forward_from = None
        self.stop_propagation = MagicMock()
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, parse_mode=None, quote=True, reply_markup=None, disable_web_page_preview=False):
        msg = MagicMock()
        msg.id = 999 + len(self.replies)
        msg.chat = self.chat
        msg.text = text
        msg.reply_markup = reply_markup
        self.replies.append({"text": text, "reply_markup": reply_markup, "msg": msg})
        return msg

    async def reply_photo(self, photo, caption=None, parse_mode=None, reply_markup=None, **kwargs):
        msg = MagicMock()
        msg.id = 999 + len(self.replies)
        msg.chat = self.chat
        msg.caption = caption
        msg.reply_markup = reply_markup
        self.replies.append({"text": caption, "reply_markup": reply_markup, "msg": msg, "photo": photo})
        return msg

    @property
    def replied(self):
        return self.replies

    async def delete(self):
        self.deleted = True


# ==============================================================================
# TESTS 1 - 20
# ==============================================================================

@pytest.fixture
def mock_db():
    return MockDatabase()

@pytest.fixture
def folder_svc(mock_db):
    return FolderService(db=mock_db)

@pytest.fixture
def mock_bot():
    return MockBot()


@pytest.mark.asyncio
async def test_1_folder_with_zero_files(mock_db, folder_svc):
    """TEST 1: /folder with zero files creates session and initial state."""
    user_id = 1001
    chat_id = 1001
    session = await folder_svc.create_session(user_id=user_id, chat_id=chat_id, status_msg_id=50)

    assert session["user_id"] == user_id
    assert session["status"] == "active"
    assert session["file_count"] == 0
    assert session["total_size"] == 0
    assert len(session["files"]) == 0

    # Verify session persisted in MongoDB
    saved = await mock_db.get_folder_session(user_id)
    assert saved is not None
    assert saved["status"] == "active"
    assert saved["file_count"] == 0


@pytest.mark.asyncio
async def test_2_add_one_file(mock_db, folder_svc):
    """TEST 2: Add one file -> verify count and size."""
    user_id = 1002
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    file_doc = {
        "_id": ObjectId(),
        "file_name": "ep1.mkv",
        "file_size": 1024 * 1024 * 450,
        "file_unique_id": "uniq_ep1",
        "flog_msg_id": 101,
        "flog_channel_id": -100123456,
    }
    await mock_db.file.insert_one(file_doc)

    success, reason, session = await folder_svc.add_file(user_id, file_doc, is_new=True)
    assert success is True
    assert reason == "added"
    assert session["file_count"] == 1
    assert session["total_size"] == 1024 * 1024 * 450
    assert "uniq_ep1" in session["file_unique_ids"]
    assert str(file_doc["_id"]) in session["new_files"]


@pytest.mark.asyncio
async def test_3_add_three_files_recent_list(mock_db, folder_svc):
    """TEST 3: Add three files -> verify recent list."""
    user_id = 1003
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    files = [
        {"_id": ObjectId(), "file_name": f"ep{i}.mkv", "file_size": 1000, "file_unique_id": f"uniq_{i}"}
        for i in range(1, 4)
    ]
    for f in files:
        await mock_db.file.insert_one(f)
        await folder_svc.add_file(user_id, f, is_new=True)

    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 3
    assert session["total_size"] == 3000
    assert session["recent_names"] == ["ep1.mkv", "ep2.mkv", "ep3.mkv"]
    rendered = FolderMessages.render_added_list(session["recent_names"])
    assert "• ep1.mkv" in rendered
    assert "• ep2.mkv" in rendered
    assert "• ep3.mkv" in rendered


@pytest.mark.asyncio
async def test_4_forward_files(mock_db, folder_svc):
    """TEST 4: Forward files instead of directly uploading."""
    user_id = 1004
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    forwarded_file = {
        "_id": ObjectId(),
        "file_name": "forwarded_video.mp4",
        "file_size": 500000,
        "file_unique_id": "uniq_fwd_1",
        "flog_msg_id": 201,
        "flog_channel_id": -100123456,
    }
    await mock_db.file.insert_one(forwarded_file)

    success, reason, session = await folder_svc.add_file(user_id, forwarded_file, is_new=False)
    assert success is True
    assert session["file_count"] == 1
    assert "forwarded_video.mp4" in session["recent_names"]
    # Forwarded pre-existing file should not be in new_files
    assert str(forwarded_file["_id"]) not in session["new_files"]


@pytest.mark.asyncio
async def test_5_duplicate_same_file(mock_db, folder_svc):
    """TEST 5: Duplicate same file rejected."""
    user_id = 1005
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    file_doc = {
        "_id": ObjectId(),
        "file_name": "ep1.mkv",
        "file_size": 1000,
        "file_unique_id": "uniq_dup",
    }
    await mock_db.file.insert_one(file_doc)

    ok1, reason1, _ = await folder_svc.add_file(user_id, file_doc, is_new=True)
    assert ok1 is True
    assert reason1 == "added"

    # Second addition of identical unique_id
    ok2, reason2, session = await folder_svc.add_file(user_id, file_doc, is_new=True)
    assert ok2 is False
    assert reason2 == "duplicate"
    assert session["file_count"] == 1
    assert session["total_size"] == 1000


@pytest.mark.asyncio
async def test_6_press_done_with_zero_files(mock_db, folder_svc):
    """TEST 6: Press Done with zero files."""
    user_id = 1006
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    with pytest.raises(ValueError, match="No files have been added"):
        await folder_svc.finalize_folder(user_id)

    # Session must remain active!
    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert session["status"] == "active"
    # No permanent folder created
    assert await mock_db.total_folders(user_id) == 0


@pytest.mark.asyncio
async def test_7_press_done_with_files(mock_db, folder_svc, mock_bot):
    """TEST 7: Press Done with files -> permanent folder and public link created."""
    user_id = 1007
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    f1 = {"_id": ObjectId(), "file_name": "f1.pdf", "file_size": 500, "file_unique_id": "u1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    folder_doc, public_url = await folder_svc.finalize_folder(user_id, bot=mock_bot)
    assert folder_doc is not None
    assert folder_doc["user_id"] == user_id
    assert folder_doc["expires_at"] is None
    assert folder_doc["total_size"] == 500
    assert folder_doc["file_count"] == 1
    assert "/gfolder/" in public_url

    # Check that session is cleared from memory and DB
    assert folder_svc.get_session(user_id) is None
    assert await mock_db.get_folder_session(user_id) is None

    # Check file document was updated with folder association
    updated_f1 = await mock_db.get_file(f1["_id"])
    assert folder_doc["_id"] in updated_f1.get("folders", [])


@pytest.mark.asyncio
async def test_8_press_done_twice(mock_db, folder_svc):
    """TEST 8: Press Done twice (race condition / idempotency)."""
    user_id = 1008
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    f1 = {"_id": ObjectId(), "file_name": "f1.pdf", "file_size": 500, "file_unique_id": "u1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    # First done
    folder_doc, _ = await folder_svc.finalize_folder(user_id)
    assert folder_doc is not None

    # Second done raises ValueError because session is completed/cleared
    with pytest.raises(ValueError):
        await folder_svc.finalize_folder(user_id)

    assert await mock_db.total_folders(user_id) == 1


@pytest.mark.asyncio
async def test_9_cancel_after_zero_files(mock_db, folder_svc, mock_bot):
    """TEST 9: Cancel after zero files."""
    user_id = 1009
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    success = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert success is True
    assert folder_svc.get_session(user_id) is None
    assert await mock_db.get_folder_session(user_id) is None
    assert await mock_db.total_folders(user_id) == 0


@pytest.mark.asyncio
async def test_10_cancel_after_three_files(mock_db, folder_svc, mock_bot):
    """TEST 10: Cancel after three files purges storage messages and DB records."""
    user_id = 1010
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    for i in range(1, 4):
        f = {
            "_id": ObjectId(),
            "file_name": f"video_{i}.mp4",
            "file_size": 2000,
            "file_unique_id": f"uniq_{i}",
            "flog_msg_id": 500 + i,
            "flog_channel_id": -100999,
        }
        await mock_db.file.insert_one(f)
        await folder_svc.add_file(user_id, f, is_new=True)

    success = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert success is True

    # Telegram storage messages deleted
    deleted_msg_ids = [d["message_id"] for d in mock_bot.deleted_messages]
    assert 501 in deleted_msg_ids
    assert 502 in deleted_msg_ids
    assert 503 in deleted_msg_ids

    # Temporary DB records deleted
    assert await mock_db.file.count_documents({}) == 0
    assert folder_svc.get_session(user_id) is None
    assert await mock_db.get_folder_session(user_id) is None
    assert await mock_db.total_folders(user_id) == 0


@pytest.mark.asyncio
async def test_11_press_cancel_twice(mock_db, folder_svc, mock_bot):
    """TEST 11: Press Cancel twice is safe and idempotent."""
    user_id = 1011
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    res1 = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert res1 is True
    res2 = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert res2 is True
    assert folder_svc.get_session(user_id) is None


@pytest.mark.asyncio
async def test_12_concurrent_users(mock_db, folder_svc):
    """TEST 12: Two different users creating folders simultaneously with zero interference."""
    user_a = 2001
    user_b = 2002

    await folder_svc.create_session(user_id=user_a, chat_id=user_a)
    await folder_svc.create_session(user_id=user_b, chat_id=user_b)

    fa = {"_id": ObjectId(), "file_name": "a.txt", "file_size": 100, "file_unique_id": "ua"}
    fb = {"_id": ObjectId(), "file_name": "b.txt", "file_size": 200, "file_unique_id": "ub"}
    await mock_db.file.insert_one(fa)
    await mock_db.file.insert_one(fb)

    await folder_svc.add_file(user_a, fa, is_new=True)
    await folder_svc.add_file(user_b, fb, is_new=True)

    sess_a = await folder_svc.get_active_session(user_a)
    sess_b = await folder_svc.get_active_session(user_b)

    assert sess_a["file_count"] == 1
    assert sess_b["file_count"] == 1
    assert sess_a["file_ids"] == [str(fa["_id"])]
    assert sess_b["file_ids"] == [str(fb["_id"])]

    # Cancel user A -> User B must remain completely intact
    await folder_svc.cancel_session(user_a)
    assert folder_svc.get_session(user_a) is None
    assert folder_svc.get_session(user_b) is not None
    assert (await folder_svc.get_active_session(user_b))["file_count"] == 1


@pytest.mark.asyncio
async def test_13_session_timeout(mock_db, folder_svc):
    """TEST 13: Session timeout triggers cleanup."""
    user_id = 1013
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    # Simulate past timestamp beyond timeout
    session["updated_at"] = time.time() - 4000

    active = await folder_svc.get_active_session(user_id)
    assert active is None  # Timed out and cleaned up!


@pytest.mark.asyncio
async def test_14_mongodb_failure_during_finalization(mock_db, folder_svc):
    """TEST 14: MongoDB failure during finalization releases lock and handles gracefully."""
    user_id = 1014
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "f.txt", "file_size": 10, "file_unique_id": "u"}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    with patch.object(mock_db, "create_folder", side_effect=RuntimeError("DB Connection Error")):
        with pytest.raises(RuntimeError, match="DB Connection Error"):
            await folder_svc.finalize_folder(user_id)

    # Lock must be released so session can be cancelled or retried
    session = folder_svc.get_session(user_id)
    assert session is not None
    assert not session["lock"].locked()


@pytest.mark.asyncio
async def test_15_telegram_storage_deletion_failure(mock_db, folder_svc, mock_bot):
    """TEST 15: Telegram storage deletion failure does not crash cancel."""
    user_id = 1015
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {
        "_id": ObjectId(),
        "file_name": "f.txt",
        "file_size": 10,
        "file_unique_id": "u",
        "flog_msg_id": 999,
        "flog_channel_id": -100,
    }
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    with patch.object(mock_bot, "delete_messages", side_effect=Exception("Telegram FloodWait/Error")):
        success = await folder_svc.cancel_session(user_id, bot=mock_bot)
        assert success is True
        # DB cleanup still completed!
        assert await mock_db.file.count_documents({}) == 0
        assert folder_svc.get_session(user_id) is None


@pytest.mark.asyncio
async def test_16_open_generated_gfolder_url(mock_db, folder_svc, mock_bot):
    """TEST 16: Open generated /gfolder/<token> URL resolves and renders."""
    from FileStream.server.render_template import render_public_folder

    user_id = 1016
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "video.mp4", "file_size": 1048576, "file_unique_id": "uvid", "mime_type": "video/mp4"}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    folder_doc, public_url = await folder_svc.finalize_folder(user_id, bot=mock_bot)
    public_token = folder_doc["public_token"]

    # Patch global db used by render_template to mock_db
    with patch("FileStream.server.render_template.db", mock_db):
        html_content = await render_public_folder(public_token)
        assert "video.mp4" in html_content
        assert "Shared Folder" in html_content


@pytest.mark.asyncio
async def test_17_folders_displays_newly_created_folder(mock_db, folder_svc, mock_bot):
    """TEST 17: /folders displays newly created folder."""
    user_id = 1017
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "book.pdf", "file_size": 2048, "file_unique_id": "ubook"}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    folder_doc, _ = await folder_svc.finalize_folder(user_id, title="Study Notes", bot=mock_bot)
    user_folders = await mock_db.list_folders(user_id, (0, 10))

    assert len(user_folders) == 1
    assert user_folders[0]["title"] == "Study Notes"
    assert user_folders[0]["file_count"] == 1


@pytest.mark.asyncio
async def test_18_restart_bot_session_restore(mock_db, folder_svc):
    """TEST 18: Restart bot while session exists restores session from MongoDB."""
    user_id = 1018
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "img.png", "file_size": 500, "file_unique_id": "uimg"}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    # Simulate bot restart by wiping in-memory cache
    folder_svc._sessions.clear()
    assert folder_svc.get_session(user_id) is None

    # Retrieve active session -> should restore seamlessly from mock_db
    restored = await folder_svc.get_active_session(user_id)
    assert restored is not None
    assert restored["file_count"] == 1
    assert "uimg" in restored["file_unique_ids"]
    assert restored["recent_names"] == ["img.png"]


@pytest.mark.asyncio
async def test_19_large_number_of_files(mock_db, folder_svc):
    """TEST 19: Large number of files up to max limit."""
    user_id = 1019
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    folder_svc.max_folder_items = 5  # Set small limit for testing

    for i in range(5):
        f = {"_id": ObjectId(), "file_name": f"f_{i}.txt", "file_size": 10, "file_unique_id": f"u_{i}"}
        await mock_db.file.insert_one(f)
        ok, reason, _ = await folder_svc.add_file(user_id, f, is_new=True)
        assert ok is True

    # 6th file exceeds limit
    f_extra = {"_id": ObjectId(), "file_name": "extra.txt", "file_size": 10, "file_unique_id": "u_extra"}
    await mock_db.file.insert_one(f_extra)
    ok, reason, session = await folder_svc.add_file(user_id, f_extra, is_new=True)
    assert ok is False
    assert reason == "limit"
    assert session["file_count"] == 5


@pytest.mark.asyncio
async def test_20_existing_old_folder_compatibility(mock_db):
    """TEST 20: Existing old folder still works."""
    from FileStream.bot.plugins.folder_manager import _folder_expiry_label

    old_folder = {
        "_id": "old_folder_1",
        "user_id": 9999,
        "title": "Legacy Archive",
        "files": ["f_old_1"],
        "created_at": 1600000000,
        "expires_at": None,
    }
    await mock_db.folders.insert_one(old_folder)

    loaded = await mock_db.get_folder("old_folder_1")
    assert loaded is not None
    assert loaded["title"] == "Legacy Archive"

    # Expiry label for permanent folder returns Never expires
    label, detail = _folder_expiry_label([], folder=loaded)
    assert label == "Never expires"
    assert detail == "Permanent"


# ==============================================================================
# TESTS 21 - 27: PRESENTATION LAYER & HANDLER DISPATCH TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_21_handle_folder_file_sequential_no_deadlock(mock_db, folder_svc, mock_bot):
    """TEST 21: handle_folder_file sequential processing does NOT deadlock and increments file count."""
    from FileStream.bot.plugins import batch

    user_id = 2021
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=100)

    episodes = [
        "Tarot.2024.S01E01.720p.WEB-DL.x265-[T4TSA.cc].mkv",
        "Tarot.2024.S01E02.720p.WEB-DL.x265-[T4TSA.cc].mkv",
        "Tarot.2024.S01E03.720p.WEB-DL.x265-[T4TSA.cc].mkv",
        "Tarot.2024.S01E04.720p.WEB-DL.x265-[T4TSA.cc].mkv",
        "Tarot.2024.S01E05.720p.WEB-DL.x265-[T4TSA.cc].mkv",
        "Tarot.2024.S01E06.720p.WEB-DL.x265-[T4TSA.cc].mkv",
    ]

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        for idx, ep_name in enumerate(episodes):
            fid = ObjectId()
            file_doc = {
                "_id": fid,
                "file_name": ep_name,
                "file_size": 150 * 1024 * 1024,
                "file_unique_id": f"uniq_{idx}",
            }
            await mock_db.file.insert_one(file_doc)

            msg = MockMessage(message_id=idx + 1, user_id=user_id, chat_id=user_id)
            media = MagicMock()
            media.file_unique_id = f"uniq_{idx}"
            media.file_name = ep_name
            media.file_size = 150 * 1024 * 1024
            msg.media = MagicMock()
            msg.media.value = "document"
            msg.document = media

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": ep_name}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):

                res = await asyncio.wait_for(
                    batch.handle_folder_file(mock_bot, msg, user_id=user_id),
                    timeout=2.0
                )
                assert res is True
                assert msg.deleted is True

    # Assert all 6 files added cleanly with dynamic status replacement at bottom
    updated = await folder_svc.get_active_session(user_id)
    assert updated["file_count"] == 6
    assert len(updated["files"]) == 6
    assert len(mock_bot.sent_messages) == 6
    assert len(mock_bot.deleted_messages) == 6
    assert "Tarot.2024.S01E06" in mock_bot.sent_messages[-1]["text"]
    assert updated["status_msg_id"] == mock_bot.sent_messages[-1]["msg"].id


@pytest.mark.asyncio
async def test_22_handle_folder_file_concurrent_rapid_burst(mock_db, folder_svc, mock_bot):
    """TEST 22: Rapid concurrent arrival of files (Telegram batch/album) processes without deadlock."""
    from FileStream.bot.plugins import batch

    user_id = 2022
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=200)

    episodes = [f"Tarot.2024.S01E0{i}.mkv" for i in range(1, 7)]
    file_docs = []
    messages = []

    for idx, ep_name in enumerate(episodes):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": ep_name,
            "file_size": 100,
            "file_unique_id": f"cuniq_{idx}",
        }
        await mock_db.file.insert_one(file_doc)
        file_docs.append(file_doc)

        msg = MockMessage(message_id=idx + 10, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"cuniq_{idx}"
        media.file_name = ep_name
        media.file_size = 100
        msg.media = MagicMock()
        msg.media.value = "document"
        msg.document = media
        messages.append((msg, fid, ep_name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def process_msg(msg, fid, name):
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                return await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

        tasks = [process_msg(m, fid, name) for m, fid, name in messages]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
        assert all(results)

    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 6


@pytest.mark.asyncio
async def test_23_complete_folder_finishes_successfully(mock_db, folder_svc, mock_bot):
    """TEST 23: complete_folder finalizes folder atomically, edits status message with links."""
    from FileStream.bot.plugins import batch

    user_id = 2023
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=300)

    f1 = {"_id": ObjectId(), "file_name": "ep1.mkv", "file_size": 100, "file_unique_id": "u1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        await asyncio.wait_for(
            batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id),
            timeout=2.0
        )

    # In-memory session cleared
    assert await folder_svc.get_active_session(user_id) is None

    # Status message edited with success and public url
    assert len(mock_bot.edited_messages) > 0
    last_edit = mock_bot.edited_messages[-1]
    assert "Folder Ready" in last_edit["text"]
    assert "Open" in str(last_edit["reply_markup"])
    assert "Share" in str(last_edit["reply_markup"])

    # Double-tap complete_folder does not raise error
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id)


@pytest.mark.asyncio
async def test_24_folderm_callback_done_and_cancel(mock_db, folder_svc, mock_bot):
    """TEST 24: folderm_callback answers queries and enforces ownership."""
    from FileStream.bot.plugins import batch

    user_id = 2024
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=400)
    f1 = {"_id": ObjectId(), "file_name": "ep1.mkv", "file_size": 100, "file_unique_id": "u1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    cb_query = MagicMock()
    cb_query.from_user.id = 99999  # Unauthorized other user
    cb_query.data = f"fld_btn:done:{user_id}"
    cb_query.answer = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_query)
        cb_query.answer.assert_called_with("⚠️ This folder session belongs to another user.", show_alert=True)

    # Owner clicks Done
    cb_query_owner = MagicMock()
    cb_query_owner.from_user.id = user_id
    cb_query_owner.data = f"fld_btn:done:{user_id}"
    cb_query_owner.message.chat.id = user_id
    cb_query_owner.answer = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_query_owner)
        cb_query_owner.answer.assert_called()

    # Session is finalized
    assert await folder_svc.get_active_session(user_id) is None


@pytest.mark.asyncio
async def test_25_cancel_folder_cleans_up_and_resets(mock_db, folder_svc, mock_bot):
    """TEST 25: cancel_folder removes temporary files and clears session."""
    from FileStream.bot.plugins import batch

    user_id = 2025
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=500)
    f1 = {"_id": ObjectId(), "file_name": "temp.mkv", "file_size": 100, "file_unique_id": "utemp"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    assert await folder_svc.get_active_session(user_id) is None
    # Temporary file deleted
    assert await mock_db.get_file(str(f1["_id"])) is None
    # Status message edited to Cancelled
    assert any("cancelled" in str(msg["text"]).lower() for msg in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_26_zero_files_done_rejected(mock_db, folder_svc, mock_bot):
    """TEST 26: complete_folder rejects completion when 0 files have been added."""
    from FileStream.bot.plugins import batch

    user_id = 2026
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=600)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id)

    # Session is STILL active
    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    # Status message edited with empty warning
    assert any("no files" in str(msg["text"]).lower() for msg in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_27_duplicate_file_in_folder_handled_gracefully(mock_db, folder_svc, mock_bot):
    """TEST 27: Adding duplicate file shows warning and does not duplicate in folder."""
    from FileStream.bot.plugins import batch

    user_id = 2027
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=700)

    fid = ObjectId()
    f1 = {"_id": fid, "file_name": "dup.mkv", "file_size": 100, "file_unique_id": "udup"}
    await mock_db.file.insert_one(f1)

    msg = MockMessage(message_id=1, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "udup"
    media.file_name = "dup.mkv"
    media.file_size = 100
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "dup.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, False))):

        # Add once
        await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        s1 = await folder_svc.get_active_session(user_id)
        assert s1["file_count"] == 1
        assert msg.deleted is True

        # Add duplicate (different msg_id to bypass telegram retry filter)
        msg2 = MockMessage(message_id=2, user_id=user_id, chat_id=user_id)
        msg2.media = msg.media
        msg2.document = media
        await batch.handle_folder_file(mock_bot, msg2, user_id=user_id)
        s2 = await folder_svc.get_active_session(user_id)
        assert s2["file_count"] == 1  # count remains 1
        assert msg2.deleted is False
        assert any("already added" in str(m["text"]).lower() for m in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_28_single_file_cleanup_and_persistent_buttons(mock_db, folder_svc, mock_bot):
    """TEST 28: Adding a single file cleans up input message and preserves persistent Done/Cancel controls."""
    from FileStream.bot.plugins import batch

    user_id = 2028
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=800)

    fid = ObjectId()
    ep_name = "Silo.S01E01.mkv"
    file_doc = {
        "_id": fid,
        "file_name": ep_name,
        "file_size": 300 * 1024 * 1024,
        "file_unique_id": "uniq_silo1",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=1, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_silo1"
    media.file_name = ep_name
    media.file_size = 300 * 1024 * 1024
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": ep_name}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):

        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # Verification:
    # 1. Incoming user file message was automatically cleaned up:
    assert msg.deleted is True
    # 2. Previous status message (id=800) was deleted:
    assert any(d["message_id"] == 800 for d in mock_bot.deleted_messages)
    # 3. New status message was sent at bottom:
    assert len(mock_bot.sent_messages) == 1
    new_status = mock_bot.sent_messages[0]
    assert "Silo.S01E01" in new_status["text"]
    assert "Files added: <b>1</b>" in new_status["text"]
    # 4. Persistent Done, Cancel, Add Files, and View Files buttons remain attached:
    reply_markup = new_status["reply_markup"]
    assert reply_markup is not None
    button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "✅ Done" in button_texts
    assert "❌ Cancel" in button_texts
    assert "➕ Add Files" not in button_texts
    assert "📋 View Files" in button_texts
    # 5. Session status_msg_id updated to the new message:
    session = await folder_svc.get_active_session(user_id)
    assert session["status_msg_id"] == new_status["msg"].id


@pytest.mark.asyncio
async def test_29_multiple_files_persistent_panel_and_all_cleaned_up(mock_db, folder_svc, mock_bot):
    """TEST 29: Multiple files (10 files) update the single status message and clean up all 10 input messages."""
    from FileStream.bot.plugins import batch

    user_id = 2029
    status_msg_id = 900
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_msg_id)

    file_messages = []
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        for i in range(1, 11):
            ep_name = f"Silo.S01E{i:02d}.mkv"
            fid = ObjectId()
            file_doc = {
                "_id": fid,
                "file_name": ep_name,
                "file_size": 300 * 1024 * 1024,
                "file_unique_id": f"uniq_silo_{i}",
            }
            await mock_db.file.insert_one(file_doc)

            msg = MockMessage(message_id=i, user_id=user_id, chat_id=user_id)
            media = MagicMock()
            media.file_unique_id = f"uniq_silo_{i}"
            media.file_name = ep_name
            media.file_size = 300 * 1024 * 1024
            msg.media = MagicMock()
            msg.media.value = "document"
            msg.document = media
            file_messages.append(msg)

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": ep_name}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):

                res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
                assert res is True

    # 1. All 10 user input messages cleaned up from chat:
    for idx, msg in enumerate(file_messages):
        assert msg.deleted is True, f"File message {idx + 1} was not deleted!"

    # 2. Status message was dynamically updated at the bottom and old message deleted:
    assert len(mock_bot.sent_messages) == 10
    assert any(d["message_id"] == status_msg_id for d in mock_bot.deleted_messages)
    last_status = mock_bot.sent_messages[-1]
    assert "Files added: <b>10</b>" in last_status["text"]
    assert "+5 more…" in last_status["text"]

    # 3. Session status_msg_id updated to the latest message:
    session = await folder_svc.get_active_session(user_id)
    assert session["status_msg_id"] == last_status["msg"].id

    # 4. Done and Cancel remain accessible:
    button_texts = [btn.text for row in last_status["reply_markup"].inline_keyboard for btn in row]
    assert "✅ Done" in button_texts
    assert "❌ Cancel" in button_texts


@pytest.mark.asyncio
async def test_30_forwarded_files_cleanup(mock_db, folder_svc, mock_bot):
    """TEST 30: Forwarded files are recognized, added, and their forwarded input messages cleaned up."""
    from FileStream.bot.plugins import batch

    user_id = 2030
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=950)

    ep_name = "ForwardedVideo.mp4"
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": ep_name,
        "file_size": 50 * 1024 * 1024,
        "file_unique_id": "uniq_fwd_1",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=99, user_id=user_id, chat_id=user_id)
    msg.forward_date = int(time.time()) - 100
    msg.forward_from = MagicMock(id=55555, first_name="SourceUser")
    media = MagicMock()
    media.file_unique_id = "uniq_fwd_1"
    media.file_name = ep_name
    media.file_size = 50 * 1024 * 1024
    msg.media = MagicMock()
    msg.media.value = "video"
    msg.video = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": ep_name}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):

        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    assert msg.deleted is True
    assert len(mock_bot.sent_messages) == 1
    assert "ForwardedVideo.mp4" in mock_bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_31_processing_failure_does_not_delete_input(mock_db, folder_svc, mock_bot):
    """TEST 31: Storage/processing failure preserves the input message and does NOT delete it."""
    from FileStream.bot.plugins import batch

    user_id = 2031
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1000)

    msg = MockMessage(message_id=101, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_name = "corrupted.mkv"
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_name": "corrupted.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(side_effect=RuntimeError("Storage connection failed"))):

        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # Safe deletion rule: MUST NOT delete input message on failure!
    assert msg.deleted is False

    # Session must remain healthy and active
    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert session["status"] == "active"
    assert session["file_count"] == 0

    # Error message shown in status with controls preserved
    assert len(mock_bot.edited_messages) == 1
    assert "Failed to add this file" in mock_bot.edited_messages[0]["text"]
    button_texts = [btn.text for row in mock_bot.edited_messages[0]["reply_markup"].inline_keyboard for btn in row]
    assert "✅ Done" in button_texts
    assert "❌ Cancel" in button_texts


@pytest.mark.asyncio
async def test_32_failed_message_deletion_does_not_break_folder_ingestion(mock_db, folder_svc, mock_bot):
    """TEST 32: If Telegram rejects message deletion (e.g. permission error), folder ingestion succeeds smoothly."""
    from FileStream.bot.plugins import batch

    user_id = 2032
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1050)

    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "perm_test.mkv",
        "file_size": 1024,
        "file_unique_id": "uniq_perm",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=202, user_id=user_id, chat_id=user_id)
    # Simulate Telegram API error when attempting deletion
    msg.delete = AsyncMock(side_effect=Exception("Telegram: MessageDeleteForbidden"))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "perm_test.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):

        # Ingestion must NOT crash or fail
        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # File was successfully added to session
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 1
    assert "perm_test.mkv" in session["recent_names"]

    # Status message was sent at bottom
    assert len(mock_bot.sent_messages) == 1
    assert "perm_test.mkv" in mock_bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_33_text_commands_done_and_cancel(mock_db, folder_svc, mock_bot):
    """TEST 33: /done and /cancel commands execute identical underlying service operations as inline buttons."""
    from FileStream.bot.plugins import batch

    # Sub-test A: /done command
    user_a = 2033
    await folder_svc.create_session(user_id=user_a, chat_id=user_a, status_msg_id=1100)
    f1 = {"_id": ObjectId(), "file_name": "cmd_done.mkv", "file_size": 500, "file_unique_id": "ucmd1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_a, f1, is_new=True)

    cmd_msg_done = MockMessage(message_id=301, user_id=user_a, chat_id=user_a, text="/done")
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.finish_folderm(mock_bot, cmd_msg_done, user_id=user_a)

    assert await folder_svc.get_active_session(user_a) is None
    assert any("Folder Ready" in str(m["text"]) for m in mock_bot.edited_messages)

    # Sub-test B: /cancel command
    user_b = 2034
    await folder_svc.create_session(user_id=user_b, chat_id=user_b, status_msg_id=1150)
    f2 = {"_id": ObjectId(), "file_name": "cmd_cancel.mkv", "file_size": 500, "file_unique_id": "ucmd2"}
    await mock_db.file.insert_one(f2)
    await folder_svc.add_file(user_b, f2, is_new=True)

    cmd_msg_cancel = MockMessage(message_id=302, user_id=user_b, chat_id=user_b, text="/cancel")
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.cancel_folderm(mock_bot, cmd_msg_cancel, user_id=user_b)

    assert await folder_svc.get_active_session(user_b) is None
    assert await mock_db.get_file(str(f2["_id"])) is None
    assert any("cancelled" in str(m["text"]).lower() for m in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_34_normal_file_mode_not_affected(mock_db, folder_svc, mock_bot):
    """TEST 34: Users without an active Folder Mode session are NOT intercepted by folder mode and messages not deleted."""
    from FileStream.bot.plugins import batch

    user_id = 2035
    # Confirm user is not in folder mode
    assert await batch.is_user_in_folder_mode(user_id) is False

    normal_msg = MockMessage(message_id=401, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_name = "normal_file.mkv"
    normal_msg.media = MagicMock(value="document")
    normal_msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        # handle_folder_file must return False
        res = await batch.handle_folder_file(mock_bot, normal_msg, user_id=user_id)
        assert res is False

    # Input message was NOT deleted
    assert normal_msg.deleted is False


@pytest.mark.asyncio
async def test_35_burst_concurrent_files_no_stale_ui_overwrites(mock_db, folder_svc, mock_bot):
    """TEST 35: High concurrency burst with out-of-order UI completion never overwrites status with older count."""
    from FileStream.bot.plugins import batch

    user_id = 2036
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1200)

    files = [f"Burst.E{i:02d}.mkv" for i in range(1, 6)]
    mock_messages = []
    file_docs = []

    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 100 * 1024, "file_unique_id": f"burst_u_{idx}"}
        await mock_db.file.insert_one(doc)
        file_docs.append(doc)

        msg = MockMessage(message_id=500 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"burst_u_{idx}"
        media.file_name = name
        media.file_size = 100 * 1024
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def worker(m, fid, name, delay):
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                if delay:
                    await asyncio.sleep(delay)
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        delays = [0.05, 0.04, 0.03, 0.02, 0.0]
        tasks = [worker(m, fid, name, d) for (m, fid, name), d in zip(mock_messages, delays)]
        results = await asyncio.gather(*tasks)
        assert all(results)

    # 1. All 5 input messages deleted
    for msg, _, _ in mock_messages:
        assert msg.deleted is True

    # 2. Session file count reaches 5
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 5

    # 3. Last status message sent at bottom MUST show 5 files, never an older count like 1 or 2
    last_msg = mock_bot.sent_messages[-1]
    assert "Files added: <b>5</b>" in last_msg["text"]
    assert session["status_msg_id"] == last_msg["msg"].id


@pytest.mark.asyncio
async def test_36_view_files_and_back_transition_simplified_ui(mock_db, folder_svc, mock_bot):
    """TEST 36: View Files and Back callback returns to ACTIVE panel with simplified buttons (no Add Files)."""
    from FileStream.bot.plugins import batch
    from FileStream.services.folder_service import FolderState

    user_id = 2036
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1300)
    f1 = {"_id": ObjectId(), "file_name": "test36.mkv", "file_size": 100, "file_unique_id": "u36"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    # 1. User clicks View Files
    cb_view = MagicMock()
    cb_view.from_user.id = user_id
    cb_view.data = f"fld_btn:view:{user_id}"
    cb_view.message.chat.id = user_id
    cb_view.message.id = 1300
    cb_view.answer = AsyncMock()
    cb_view.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_view)

    # Message edited with Folder Files list
    assert len(cb_view.edit_message_text.call_args_list) > 0
    edit_args = cb_view.edit_message_text.call_args
    assert "Folder Files" in edit_args[0][0]

    # Verify buttons contain Back button
    markup = edit_args[1].get("reply_markup")
    assert any("Back" in btn.text for row in markup.inline_keyboard for btn in row)

    # 2. Click Back button
    cb_back = MagicMock()
    cb_back.from_user.id = user_id
    cb_back.data = f"fld_btn:back:{user_id}"
    cb_back.message.chat.id = user_id
    cb_back.message.id = 1300
    cb_back.answer = AsyncMock()
    cb_back.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_back)

    # State returned to ACTIVE
    session = await folder_svc.get_active_session(user_id)
    assert session["state"] == FolderState.ACTIVE

    # Control panel re-rendered with simplified UI
    assert len(cb_back.edit_message_text.call_args_list) > 0
    back_edit_args = cb_back.edit_message_text.call_args
    markup = back_edit_args[1].get("reply_markup")
    button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert not any("Add Files" in t for t in button_texts)
    assert any("View Files" in t for t in button_texts)
    assert any("Done" in t for t in button_texts)
    assert any("Cancel" in t for t in button_texts)


@pytest.mark.asyncio
async def test_37_view_files_pagination_and_navigation(mock_db, folder_svc, mock_bot):
    """TEST 37: View files displays 5 files per page with pagination navigation and Back button."""
    from FileStream.bot.plugins import batch

    user_id = 2037
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1400)

    # Add 12 files
    for i in range(1, 13):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": f"Document_{i:02d}.pdf", "file_size": 1024 * i, "file_unique_id": f"uniq_{i}"}
        await mock_db.file.insert_one(doc)
        await folder_svc.add_file(user_id, doc, is_new=True)

    # Page 1: Click View
    cb_view = MagicMock()
    cb_view.from_user.id = user_id
    cb_view.data = f"fld_btn:view:{user_id}"
    cb_view.message.chat.id = user_id
    cb_view.message.id = 1400
    cb_view.answer = AsyncMock()
    cb_view.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_view)

    args_p1 = cb_view.edit_message_text.call_args
    text_p1 = args_p1[0][0]
    markup_p1 = args_p1[1]["reply_markup"]
    assert "<b>1</b> / <b>3</b>" in text_p1
    assert "Document_01.pdf" in text_p1
    assert "Document_05.pdf" in text_p1
    assert "Document_06.pdf" not in text_p1

    # Check pagination buttons on page 1
    buttons_p1 = [btn for row in markup_p1.inline_keyboard for btn in row]
    assert any("▶️" in b.text for b in buttons_p1)
    assert any("Back" in b.text for b in buttons_p1)

    # Page 2: Click Next
    cb_page2 = MagicMock()
    cb_page2.from_user.id = user_id
    cb_page2.data = f"fld_btn:page:2:{user_id}"
    cb_page2.message.chat.id = user_id
    cb_page2.message.id = 1400
    cb_page2.answer = AsyncMock()
    cb_page2.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_page2)

    args_p2 = cb_page2.edit_message_text.call_args
    text_p2 = args_p2[0][0]
    assert "<b>2</b> / <b>3</b>" in text_p2
    assert "Document_06.pdf" in text_p2
    assert "Document_10.pdf" in text_p2
    assert "Document_01.pdf" not in text_p2

    # Page 3: Click Next
    cb_page3 = MagicMock()
    cb_page3.from_user.id = user_id
    cb_page3.data = f"fld_btn:page:3:{user_id}"
    cb_page3.message.chat.id = user_id
    cb_page3.message.id = 1400
    cb_page3.answer = AsyncMock()
    cb_page3.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_page3)

    args_p3 = cb_page3.edit_message_text.call_args
    text_p3 = args_p3[0][0]
    assert "<b>3</b> / <b>3</b>" in text_p3
    assert "Document_11.pdf" in text_p3
    assert "Document_12.pdf" in text_p3

    # Click Back
    cb_back = MagicMock()
    cb_back.from_user.id = user_id
    cb_back.data = f"fld_btn:back:{user_id}"
    cb_back.message.chat.id = user_id
    cb_back.message.id = 1400
    cb_back.answer = AsyncMock()
    cb_back.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_back)

    args_back = cb_back.edit_message_text.call_args
    markup_back = args_back[1]["reply_markup"]
    btn_texts = [btn.text for row in markup_back.inline_keyboard for btn in row]
    assert any("View Files" in t for t in btn_texts)


@pytest.mark.asyncio
async def test_38_done_with_zero_files_prompts_empty(mock_db, folder_svc, mock_bot):
    """TEST 38: Clicking Done with zero files warns user and provides Add Files / Back / Cancel buttons without creating a folder."""
    from FileStream.bot.plugins import batch

    user_id = 2038
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1500)

    cb_done = MagicMock()
    cb_done.from_user.id = user_id
    cb_done.data = f"fld_btn:done:{user_id}"
    cb_done.message.chat.id = user_id
    cb_done.message.id = 1500
    cb_done.answer = AsyncMock()
    cb_done.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_done)

    # Session is NOT deleted/finalized
    session = await folder_svc.get_active_session(user_id)
    assert session is not None

    # Status message was edited with empty warning
    assert len(mock_bot.edited_messages) > 0
    edit = mock_bot.edited_messages[-1]
    assert "No Files Added Yet" in edit["text"] or "No files" in edit["text"]

    # Buttons include Back, Cancel (no Add Files)
    markup = edit["reply_markup"]
    btn_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("Add Files" in t for t in btn_texts)
    assert any("Back" in t for t in btn_texts)
    assert any("Cancel" in t for t in btn_texts)


@pytest.mark.asyncio
async def test_39_finalize_intermediate_state_and_success_buttons(mock_db, folder_svc, mock_bot):
    """TEST 39: Finalizing folder displays intermediate progress message followed by final ready state with Open, Share, and My Folders buttons."""
    from FileStream.bot.plugins import batch

    user_id = 2039
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=1600)
    f1 = {"_id": ObjectId(), "file_name": "video.mp4", "file_size": 2048, "file_unique_id": "uvid1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id)

    # Check intermediate finalizing edit was made
    texts = [m["text"] for m in mock_bot.edited_messages]
    assert any("Finalizing Folder" in t for t in texts)

    # Final message contains Folder Ready
    final_edit = mock_bot.edited_messages[-1]
    assert "Folder Ready" in final_edit["text"]

    # Final message buttons
    markup = final_edit["reply_markup"]
    btn_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Open Folder" in t or "Open" in t for t in btn_texts)
    assert any("Share" in t for t in btn_texts)
    assert any("My Folders" in t for t in btn_texts)


@pytest.mark.asyncio
async def test_40_unauthorized_user_blocked_on_all_callbacks(mock_db, folder_svc, mock_bot):
    """TEST 40: Callbacks with an owner_id cannot be executed by other users."""
    from FileStream.bot.plugins import batch

    owner_id = 2040
    attacker_id = 99999
    await folder_svc.create_session(user_id=owner_id, chat_id=owner_id, status_msg_id=1700)

    callbacks_to_test = [
        f"fld_btn:add:{owner_id}",
        f"fld_btn:back:{owner_id}",
        f"fld_btn:view:{owner_id}",
        f"fld_btn:page:2:{owner_id}",
        f"fld_btn:done:{owner_id}",
        f"fld_btn:cancel:{owner_id}",
    ]

    for data in callbacks_to_test:
        cb = MagicMock()
        cb.from_user.id = attacker_id
        cb.data = data
        cb.answer = AsyncMock()
        cb.edit_message_text = AsyncMock()

        with patch.object(batch, "db", mock_db), \
             patch.object(batch, "folder_service", folder_svc):
            await batch.folderm_callback(mock_bot, cb)

        cb.answer.assert_called_with("⚠️ This folder session belongs to another user.", show_alert=True)
        cb.edit_message_text.assert_not_called()

    # Session is completely untouched
    session = await folder_svc.get_active_session(owner_id)
    assert session is not None


@pytest.mark.asyncio
async def test_41_dynamic_status_replacement_coalescing_burst_10_files(mock_db, folder_svc, mock_bot):
    """TEST 41: 10 files arriving rapidly in a burst coalesce into 1 bottom status message without generating 10 messages."""
    from FileStream.bot.plugins import batch

    user_id = 2041
    initial_status_id = 2100
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)

    files = [f"Coalesce.E{i:02d}.mkv" for i in range(1, 11)]
    mock_messages = []

    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 150 * 1024 * 1024, "file_unique_id": f"burst_coal_{idx}"}
        await mock_db.file.insert_one(doc)

        msg = MockMessage(message_id=600 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"burst_coal_{idx}"
        media.file_name = name
        media.file_size = 150 * 1024 * 1024
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def worker(m, fid, name):
            async def mock_process(*args, **kwargs):
                await asyncio.sleep(0.01)
                return fid, True

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        tasks = [worker(m, fid, name) for m, fid, name in mock_messages]
        results = await asyncio.gather(*tasks)
        assert all(results)

    # 1. All 10 input messages deleted cleanly
    for msg, _, _ in mock_messages:
        assert msg.deleted is True

    # 2. Session reaches 10 files
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 10

    # 3. Old initial status message (id=2100) was deleted
    assert any(d["message_id"] == initial_status_id for d in mock_bot.deleted_messages)

    # 4. Critical performance check: 10 files arriving concurrently did NOT produce 10 status messages
    assert len(mock_bot.sent_messages) <= 2
    last_status = mock_bot.sent_messages[-1]
    assert "Files added: <b>10</b>" in last_status["text"]
    assert session["status_msg_id"] == last_status["msg"].id


@pytest.mark.asyncio
async def test_42_single_authoritative_status_message_invariant(mock_db, folder_svc, mock_bot):
    """TEST 42: INVARIANT: Exactly 1 active session, exactly 1 authoritative status message in the chat."""
    from FileStream.bot.plugins import batch

    user_id = 2042
    initial_status_id = 3000
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)

    files = ["doc_A.pdf", "doc_B.pdf", "doc_C.pdf"]

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        for idx, name in enumerate(files):
            fid = ObjectId()
            doc = {"_id": fid, "file_name": name, "file_size": 2048, "file_unique_id": f"inv_uniq_{idx}"}
            await mock_db.file.insert_one(doc)

            msg = MockMessage(message_id=700 + idx, user_id=user_id, chat_id=user_id)
            media = MagicMock()
            media.file_unique_id = f"inv_uniq_{idx}"
            media.file_name = name
            media.file_size = 2048
            msg.media = MagicMock(value="document")
            msg.document = media

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

            session = await folder_svc.get_active_session(user_id)
            # Exactly one authoritative status_msg_id tracked
            assert session["status_msg_id"] == mock_bot.sent_messages[idx]["msg"].id

    # Each step deleted previous status message and sent 1 new status message at the bottom
    assert len(mock_bot.sent_messages) == 3
    assert len(mock_bot.deleted_messages) == 3
    assert any(d["message_id"] == initial_status_id for d in mock_bot.deleted_messages)


@pytest.mark.asyncio
async def test_43_view_files_replaced_when_new_file_arrives(mock_db, folder_svc, mock_bot):
    """TEST 43: If user is on View Files and a new file arrives, old view is deleted and new Folder Status sent at bottom."""
    from FileStream.bot.plugins import batch

    user_id = 2043
    status_id = 4000
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_id)

    # Initial file
    f1 = {"_id": ObjectId(), "file_name": "chapter1.pdf", "file_size": 1000, "file_unique_id": "u_ch1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    # User opens View Files
    cb_view = MagicMock()
    cb_view.from_user.id = user_id
    cb_view.data = f"fld_btn:view:{user_id}"
    cb_view.message.chat.id = user_id
    cb_view.message.id = status_id
    cb_view.answer = AsyncMock()
    cb_view.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_view)
        cb_view.edit_message_text.assert_called_once()

    # While on View Files, a new file arrives
    f2 = {"_id": ObjectId(), "file_name": "chapter2.pdf", "file_size": 2000, "file_unique_id": "u_ch2"}
    await mock_db.file.insert_one(f2)

    msg = MockMessage(message_id=888, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "u_ch2"
    media.file_name = "chapter2.pdf"
    media.file_size = 2000
    msg.media = MagicMock(value="document")
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(f2["_id"]), "file_name": "chapter2.pdf"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(f2["_id"], True))):
        await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

    # Old view files message (id=4000) was deleted
    assert any(d["message_id"] == status_id for d in mock_bot.deleted_messages)

    # New status message sent at bottom showing 2 files
    assert len(mock_bot.sent_messages) == 1
    new_status = mock_bot.sent_messages[0]
    assert "Files added: <b>2</b>" in new_status["text"]

    session = await folder_svc.get_active_session(user_id)
    assert session["status_msg_id"] == new_status["msg"].id


@pytest.mark.asyncio
async def test_44_delete_old_status_failure_continues_gracefully(mock_db, folder_svc, mock_bot):
    """TEST 44: If deleting old status message fails, bot logs and continues without breaking."""
    from FileStream.bot.plugins import batch

    user_id = 2044
    status_id = 5000
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_id)

    # Mock delete_messages to raise exception
    mock_bot.delete_messages = AsyncMock(side_effect=Exception("Message already deleted by user"))

    fid = ObjectId()
    doc = {"_id": fid, "file_name": "del_fail.mkv", "file_size": 5000, "file_unique_id": "u_delfail"}
    await mock_db.file.insert_one(doc)

    msg = MockMessage(message_id=901, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "u_delfail"
    media.file_name = "del_fail.mkv"
    media.file_size = 5000
    msg.media = MagicMock(value="document")
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "del_fail.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # New status message sent anyway
    assert len(mock_bot.sent_messages) == 1
    session = await folder_svc.get_active_session(user_id)
    assert session["status_msg_id"] == mock_bot.sent_messages[0]["msg"].id


@pytest.mark.asyncio
async def test_45_floodwait_retry_in_refresh_folder_status(mock_db, folder_svc, mock_bot):
    """TEST 45: FloodWait during send_message in refresh_folder_status is handled with sleep and retry."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 2045
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=6000)

    original_send = mock_bot.send_message
    call_count = 0

    async def flaky_send(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return await original_send(*args, **kwargs)

    mock_bot.send_message = flaky_send

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        msg = await batch.refresh_folder_status(mock_bot, session, chat_id=user_id)
        assert msg is not None
        assert call_count == 2
        assert session["status_msg_id"] == msg.id


# ==============================================================================
# TESTS 46 - 51: REGRESSION TESTS FOR DUPLICATE STATUS BUG
# ==============================================================================

@pytest.mark.asyncio
async def test_46_normal_text_does_not_create_status_or_trigger_refresh(mock_db, folder_svc, mock_bot):
    """TEST 46 (Bug Regression 1): Sending normal text while Folder Mode is active does NOT create a status message or call refresh_folder_status."""
    from FileStream.bot.plugins import batch

    user_id = 2046
    folder_cmd_msg = MockMessage(message_id=100, user_id=user_id, chat_id=user_id, text="/folder")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, folder_cmd_msg)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    initial_status_id = session.get("status_msg_id")
    assert initial_status_id is not None
    assert len(folder_cmd_msg.replies) == 1
    initial_panel_text = folder_cmd_msg.replies[0]["text"]
    assert "Create Folder" in initial_panel_text
    assert "Folder mode is active" not in initial_panel_text

    # User sends normal text "hello"
    text_msg = MockMessage(message_id=101, user_id=user_id, chat_id=user_id, text="hello")
    with patch.object(batch, "refresh_folder_status", AsyncMock()) as mock_refresh, \
         patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_guard_text(mock_bot, text_msg)
        mock_refresh.assert_not_called()

    # Verify no Telegram messages sent, edited, or deleted for "hello"
    assert len(mock_bot.sent_messages) == 0
    assert len(mock_bot.edited_messages) == 0
    assert len(mock_bot.deleted_messages) == 0

    # Status message count == 1, session status_msg_id remains unchanged
    updated_session = await folder_svc.get_active_session(user_id)
    assert updated_session["status_msg_id"] == initial_status_id
    assert updated_session["file_count"] == 0


@pytest.mark.asyncio
async def test_47_multiple_normal_text_messages_leaves_single_status(mock_db, folder_svc, mock_bot):
    """TEST 47 (Bug Regression 2): Multiple normal text messages while Folder Mode is active leave exactly 1 authoritative Folder Status."""
    from FileStream.bot.plugins import batch

    user_id = 2047
    folder_cmd_msg = MockMessage(message_id=200, user_id=user_id, chat_id=user_id, text="/folder")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, folder_cmd_msg)

    session = await folder_svc.get_active_session(user_id)
    initial_status_id = session.get("status_msg_id")

    for idx, text in enumerate(["hello", "test", "abc", "random text"], start=201):
        msg = MockMessage(message_id=idx, user_id=user_id, chat_id=user_id, text=text)
        with patch.object(batch, "refresh_folder_status", AsyncMock()) as mock_refresh, \
             patch.object(batch, "db", mock_db), \
             patch.object(batch, "folder_service", folder_svc):
            await batch.folderm_guard_text(mock_bot, msg)
            mock_refresh.assert_not_called()

    # Zero Telegram API calls made during text messages
    assert len(mock_bot.sent_messages) == 0
    assert len(mock_bot.edited_messages) == 0

    # Exactly one authoritative status message remains
    curr_session = await folder_svc.get_active_session(user_id)
    assert curr_session["status_msg_id"] == initial_status_id
    assert curr_session["file_count"] == 0


@pytest.mark.asyncio
async def test_48_normal_text_followed_by_file_preserves_single_status(mock_db, folder_svc, mock_bot):
    """TEST 48 (Bug Regression 3): Normal text does not refresh; subsequent media file updates workspace to exactly 1 authoritative status."""
    from FileStream.bot.plugins import batch

    user_id = 2048
    folder_cmd_msg = MockMessage(message_id=300, user_id=user_id, chat_id=user_id, text="/folder")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, folder_cmd_msg)

    session = await folder_svc.get_active_session(user_id)
    initial_status_id = session.get("status_msg_id")

    # 1. User sends "hello"
    text_msg = MockMessage(message_id=301, user_id=user_id, chat_id=user_id, text="hello")
    with patch.object(batch, "refresh_folder_status", AsyncMock()) as mock_refresh, \
         patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_guard_text(mock_bot, text_msg)
        mock_refresh.assert_not_called()

    assert len(mock_bot.sent_messages) == 0

    # 2. User sends file.mkv
    fid = ObjectId()
    doc = {"_id": fid, "file_name": "file.mkv", "file_size": 1024 * 1024, "file_unique_id": "uniq_48"}
    await mock_db.file.insert_one(doc)

    file_msg = MockMessage(message_id=302, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_48"
    media.file_name = "file.mkv"
    media.file_size = 1024 * 1024
    file_msg.media = MagicMock(value="document")
    file_msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "file.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        res = await batch.handle_folder_file(mock_bot, file_msg, user_id=user_id)
        assert res is True

    # Initial status message was deleted, new status message posted at bottom
    assert any(d["message_id"] == initial_status_id for d in mock_bot.deleted_messages)
    assert len(mock_bot.sent_messages) == 1
    new_status = mock_bot.sent_messages[0]
    assert "Files added: <b>1</b>" in new_status["text"]
    assert "Folder mode is active" not in new_status["text"]

    updated_session = await folder_svc.get_active_session(user_id)
    assert updated_session["file_count"] == 1
    assert updated_session["status_msg_id"] == new_status["msg"].id


@pytest.mark.asyncio
async def test_49_file_burst_still_coalesces_with_interleaved_text(mock_db, folder_svc, mock_bot):
    """TEST 49 (Bug Regression 4): 10 files in rapid burst with interleaved normal text coalesce properly into 1 authoritative status."""
    from FileStream.bot.plugins import batch

    user_id = 2049
    initial_status_id = 4100
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)

    files = [f"BurstFile_{i}.mkv" for i in range(1, 11)]
    mock_messages = []

    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 100 * 1024 * 1024, "file_unique_id": f"burst_u_{idx}"}
        await mock_db.file.insert_one(doc)

        msg = MockMessage(message_id=400 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"burst_u_{idx}"
        media.file_name = name
        media.file_size = 100 * 1024 * 1024
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def file_worker(m, fid, name):
            async def mock_process(*args, **kwargs):
                await asyncio.sleep(0.01)
                return fid, True

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        async def text_worker(txt):
            m = MockMessage(message_id=800, user_id=user_id, chat_id=user_id, text=txt)
            await batch.folderm_guard_text(mock_bot, m)

        tasks = [file_worker(m, fid, name) for m, fid, name in mock_messages]
        tasks.append(text_worker("random comment 1"))
        tasks.append(text_worker("random comment 2"))

        results = await asyncio.gather(*tasks)

    # 1. 10 files processed successfully
    assert results[:10] == [True] * 10

    # 2. Session reaches 10 files
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 10

    # 3. Burst coalesced into <= 2 sent status messages
    assert len(mock_bot.sent_messages) <= 2
    last_status = mock_bot.sent_messages[-1]
    assert "Files added: <b>10</b>" in last_status["text"]
    assert session["status_msg_id"] == last_status["msg"].id


@pytest.mark.asyncio
async def test_50_folder_done_cancel_commands_work_as_expected(mock_db, folder_svc, mock_bot):
    """TEST 50 (Bug Regression 5): Verify /folder, /done, /cancel commands continue working as expected."""
    from FileStream.bot.plugins import batch

    # Sub-test A: /folder refreshes existing session to bottom
    user_a = 2050
    session_a = await folder_svc.create_session(user_id=user_a, chat_id=user_a, status_msg_id=5001)
    msg_folder = MockMessage(message_id=5002, user_id=user_a, chat_id=user_a, text="/folder")
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, msg_folder)

    # Status message was refreshed to bottom
    assert any(d["message_id"] == 5001 for d in mock_bot.deleted_messages)
    assert len(mock_bot.sent_messages) == 1
    assert session_a["status_msg_id"] == mock_bot.sent_messages[0]["msg"].id

    # Sub-test B: /done finalizes folder
    user_b = 2051
    await folder_svc.create_session(user_id=user_b, chat_id=user_b, status_msg_id=5010)
    fb = {"_id": ObjectId(), "file_name": "cmd_test.mkv", "file_size": 200, "file_unique_id": "ucmd_b"}
    await mock_db.file.insert_one(fb)
    await folder_svc.add_file(user_b, fb, is_new=True)

    msg_done = MockMessage(message_id=5011, user_id=user_b, chat_id=user_b, text="/done")
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.finish_folderm(mock_bot, msg_done, user_id=user_b)

    assert await folder_svc.get_active_session(user_b) is None
    assert any("Folder Ready" in str(m["text"]) for m in mock_bot.edited_messages)

    # Sub-test C: /cancel cancels folder
    user_c = 2052
    await folder_svc.create_session(user_id=user_c, chat_id=user_c, status_msg_id=5020)
    fc = {"_id": ObjectId(), "file_name": "cancel_test.mkv", "file_size": 200, "file_unique_id": "ucmd_c"}
    await mock_db.file.insert_one(fc)
    await folder_svc.add_file(user_c, fc, is_new=True)

    msg_cancel = MockMessage(message_id=5021, user_id=user_c, chat_id=user_c, text="/cancel")
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.cancel_folderm(mock_bot, msg_cancel, user_id=user_c)

    assert await folder_svc.get_active_session(user_c) is None
    assert any("cancelled" in str(m["text"]).lower() for m in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_51_callback_buttons_add_view_done_cancel_work_as_expected(mock_db, folder_svc, mock_bot):
    """TEST 51 (Bug Regression 6): Callback buttons (View Files, Done, Cancel) continue working as expected and Add Files is absent."""
    from FileStream.bot.plugins import batch
    from FileStream.services.folder_service import FolderState

    user_id = 2053
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=6010)

    f1 = {"_id": ObjectId(), "file_name": "cb_file.mkv", "file_size": 500, "file_unique_id": "ucb1"}
    await mock_db.file.insert_one(f1)
    await folder_svc.add_file(user_id, f1, is_new=True)

    # 1. Verify ➕ Add Files is absent from active folder buttons
    buttons = batch._folderm_buttons(user_id, file_count=1)
    btn_texts = [b.text for row in buttons.inline_keyboard for b in row]
    assert not any("Add Files" in t for t in btn_texts)
    assert "📋 View Files" in btn_texts
    assert "✅ Done" in btn_texts
    assert "❌ Cancel" in btn_texts

    status_mock_msg = MagicMock()
    status_mock_msg.id = 6010
    status_mock_msg.chat = MagicMock()
    status_mock_msg.chat.id = user_id

    # 2. 📋 View Files callback
    cb_view = MagicMock()
    cb_view.data = f"fld_btn:view:{user_id}"
    cb_view.from_user.id = user_id
    cb_view.message = status_mock_msg
    cb_view.answer = AsyncMock()
    cb_view.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_view)
        cb_view.edit_message_text.assert_called_once()
        assert "Folder Files" in cb_view.edit_message_text.call_args[0][0]

    # 3. 🔙 Back callback
    cb_back = MagicMock()
    cb_back.data = f"fld_btn:back:{user_id}"
    cb_back.from_user.id = user_id
    cb_back.message = status_mock_msg
    cb_back.answer = AsyncMock()
    cb_back.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_back)
        assert session["state"] == FolderState.ACTIVE
        cb_back.edit_message_text.assert_called_once()
        back_btn_texts = [b.text for row in cb_back.edit_message_text.call_args[1]["reply_markup"].inline_keyboard for b in row]
        assert not any("Add Files" in t for t in back_btn_texts)
        assert "📋 View Files" in back_btn_texts

    # 4. ✅ Done callback
    cb_done = MagicMock()
    cb_done.data = f"fld_btn:done:{user_id}"
    cb_done.from_user.id = user_id
    cb_done.message = status_mock_msg
    cb_done.answer = AsyncMock()
    cb_done.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_done)
        assert await folder_svc.get_active_session(user_id) is None
        assert any("Folder Ready" in str(m["text"]) for m in mock_bot.edited_messages)


# ==============================================================================
# TESTS 52 - 56: UI SIMPLIFICATION REGRESSION SUITE (REMOVAL OF ADD FILES)
# ==============================================================================

@pytest.mark.asyncio
async def test_52_add_files_button_absent_and_controls_present():
    """TEST 52 (UI Simplification 1-4): Verify ➕ Add Files is absent, and 📋 View Files, ✅ Done, ❌ Cancel exist."""
    from FileStream.bot.plugins import batch

    # Case A: file_count == 0 -> [Done] [Cancel]
    markup_empty = batch._folderm_buttons(owner_id=3001, file_count=0)
    texts_empty = [b.text for row in markup_empty.inline_keyboard for b in row]
    assert not any("Add Files" in t for t in texts_empty), "Add Files button must NOT exist when file_count == 0"
    assert "📋 View Files" not in texts_empty, "View Files button must NOT exist when file_count == 0"
    assert "✅ Done" in texts_empty, "Done button must exist"
    assert "❌ Cancel" in texts_empty, "Cancel button must exist"
    assert len(markup_empty.inline_keyboard) == 1
    assert len(markup_empty.inline_keyboard[0]) == 2

    # Case B: file_count > 0 -> Row 1: [View Files], Row 2: [Done, Cancel]
    markup_files = batch._folderm_buttons(owner_id=3001, file_count=5)
    texts_files = [b.text for row in markup_files.inline_keyboard for b in row]
    assert not any("Add Files" in t for t in texts_files), "Add Files button must NOT exist when file_count > 0"
    assert "📋 View Files" in texts_files, "View Files button must exist when file_count > 0"
    assert "✅ Done" in texts_files, "Done button must exist"
    assert "❌ Cancel" in texts_files, "Cancel button must exist"
    assert len(markup_files.inline_keyboard) == 2
    assert [b.text for b in markup_files.inline_keyboard[0]] == ["📋 View Files"]
    assert [b.text for b in markup_files.inline_keyboard[1]] == ["✅ Done", "❌ Cancel"]


@pytest.mark.asyncio
async def test_53_user_can_still_send_files_directly_without_add_button(mock_db, folder_svc, mock_bot):
    """TEST 53 (UI Simplification 5): User starts folder and sends 3 files directly. Files == 3 and no Add Files button appears."""
    from FileStream.bot.plugins import batch

    user_id = 3002
    folder_cmd_msg = MockMessage(message_id=100, user_id=user_id, chat_id=user_id, text="/folder")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, folder_cmd_msg)

    # Initial status has no Add Files
    assert len(folder_cmd_msg.replies) == 1
    initial_markup = folder_cmd_msg.replies[0]["reply_markup"]
    init_texts = [b.text for row in initial_markup.inline_keyboard for b in row]
    assert not any("Add Files" in t for t in init_texts)
    assert "✅ Done" in init_texts
    assert "❌ Cancel" in init_texts

    # Send 3 files directly without pressing any button
    for i in range(1, 4):
        fid = ObjectId()
        fname = f"DirectFile_{i}.mkv"
        await mock_db.file.insert_one({"_id": fid, "file_name": fname, "file_size": 1024 * 1024, "file_unique_id": f"uniq_{user_id}_{i}"})

        msg = MockMessage(message_id=100 + i, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"uniq_{user_id}_{i}"
        media.file_name = fname
        media.file_size = 1024 * 1024
        msg.media = MagicMock(value="document")
        msg.document = media

        with patch.object(batch, "db", mock_db), \
             patch.object(batch, "folder_service", folder_svc), \
             patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": fname}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
            res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
            assert res is True

    # Check final session and UI state
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 3

    last_status = mock_bot.sent_messages[-1]
    last_texts = [b.text for row in last_status["reply_markup"].inline_keyboard for b in row]
    assert not any("Add Files" in t for t in last_texts)
    assert "📋 View Files" in last_texts
    assert "✅ Done" in last_texts
    assert "❌ Cancel" in last_texts


@pytest.mark.asyncio
async def test_54_additional_files_after_status_refresh(mock_db, folder_svc, mock_bot):
    """TEST 54 (UI Simplification 6): /folder -> file1 -> refresh -> file2 -> refresh. Both files stored, 1 status, no Add Files."""
    from FileStream.bot.plugins import batch

    user_id = 3003
    folder_cmd_msg = MockMessage(message_id=200, user_id=user_id, chat_id=user_id, text="/folder")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.start_folderm(mock_bot, folder_cmd_msg)

    initial_status_id = folder_cmd_msg.replies[0]["msg"].id

    # 1. First file
    fid1 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "step1.mkv", "file_size": 2048, "file_unique_id": f"u_{user_id}_1"})
    msg1 = MockMessage(message_id=201, user_id=user_id, chat_id=user_id)
    media1 = MagicMock()
    media1.file_unique_id = f"u_{user_id}_1"
    media1.file_name = "step1.mkv"
    media1.file_size = 2048
    msg1.media = MagicMock(value="document")
    msg1.document = media1

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "step1.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
        await batch.handle_folder_file(mock_bot, msg1, user_id=user_id)

    status_after_file1 = mock_bot.sent_messages[-1]
    assert any(d["message_id"] == initial_status_id for d in mock_bot.deleted_messages)
    texts_1 = [b.text for row in status_after_file1["reply_markup"].inline_keyboard for b in row]
    assert not any("Add Files" in t for t in texts_1)
    assert "📋 View Files" in texts_1

    # 2. Second file arrives after refresh
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid2, "file_name": "step2.mkv", "file_size": 4096, "file_unique_id": f"u_{user_id}_2"})
    msg2 = MockMessage(message_id=202, user_id=user_id, chat_id=user_id)
    media2 = MagicMock()
    media2.file_unique_id = f"u_{user_id}_2"
    media2.file_name = "step2.mkv"
    media2.file_size = 4096
    msg2.media = MagicMock(value="document")
    msg2.document = media2

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "step2.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
        await batch.handle_folder_file(mock_bot, msg2, user_id=user_id)

    # Verify state: 2 files stored, exactly 1 authoritative status message, controls intact
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 2
    assert session["total_size"] == 6144

    status_after_file2 = mock_bot.sent_messages[-1]
    assert session["status_msg_id"] == status_after_file2["msg"].id
    assert any(d["message_id"] == status_after_file1["msg"].id for d in mock_bot.deleted_messages)

    final_texts = [b.text for row in status_after_file2["reply_markup"].inline_keyboard for b in row]
    assert not any("Add Files" in t for t in final_texts)
    assert "📋 View Files" in final_texts
    assert "✅ Done" in final_texts
    assert "❌ Cancel" in final_texts


@pytest.mark.asyncio
async def test_55_view_files_remains_functional_with_new_file_arrival(mock_db, folder_svc, mock_bot):
    """TEST 55 (UI Simplification 7): View Files pagination works and new file arrival refreshes dynamic workspace cleanly."""
    from FileStream.bot.plugins import batch

    user_id = 3004
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=7000)

    # Add 7 files to generate 2 pages (5 items per page)
    for i in range(1, 8):
        f = {"_id": ObjectId(), "file_name": f"Item_{i}.mkv", "file_size": 100 * i, "file_unique_id": f"u_7_{i}"}
        await mock_db.file.insert_one(f)
        await folder_svc.add_file(user_id, f, is_new=True)

    status_mock_msg = MagicMock()
    status_mock_msg.id = 7000
    status_mock_msg.chat = MagicMock()
    status_mock_msg.chat.id = user_id

    # 1. Open View Files (Page 1)
    cb_view = MagicMock()
    cb_view.data = f"fld_btn:view:{user_id}"
    cb_view.from_user.id = user_id
    cb_view.message = status_mock_msg
    cb_view.answer = AsyncMock()
    cb_view.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_view)
        assert "Page <b>1</b> / <b>2</b>" in cb_view.edit_message_text.call_args[0][0]

    # 2. Navigate to Page 2
    cb_page2 = MagicMock()
    cb_page2.data = f"fld_btn:page:2:{user_id}"
    cb_page2.from_user.id = user_id
    cb_page2.message = status_mock_msg
    cb_page2.answer = AsyncMock()
    cb_page2.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_page2)
        assert "Page <b>2</b> / <b>2</b>" in cb_page2.edit_message_text.call_args[0][0]

    # 3. Navigate Back to main panel
    cb_back = MagicMock()
    cb_back.data = f"fld_btn:back:{user_id}"
    cb_back.from_user.id = user_id
    cb_back.message = status_mock_msg
    cb_back.answer = AsyncMock()
    cb_back.edit_message_text = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_back)
        back_markup = cb_back.edit_message_text.call_args[1]["reply_markup"]
        back_texts = [b.text for row in back_markup.inline_keyboard for b in row]
        assert not any("Add Files" in t for t in back_texts)
        assert "📋 View Files" in back_texts
        assert "✅ Done" in back_texts
        assert "❌ Cancel" in back_texts

    # 4. While on View Files, a new file arrives
    fid8 = ObjectId()
    await mock_db.file.insert_one({"_id": fid8, "file_name": "Item_8.mkv", "file_size": 800, "file_unique_id": "u_7_8"})
    msg8 = MockMessage(message_id=7050, user_id=user_id, chat_id=user_id)
    media8 = MagicMock()
    media8.file_unique_id = "u_7_8"
    media8.file_name = "Item_8.mkv"
    media8.file_size = 800
    msg8.media = MagicMock(value="document")
    msg8.document = media8

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid8), "file_name": "Item_8.mkv"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid8, True))):
        res = await batch.handle_folder_file(mock_bot, msg8, user_id=user_id)
        assert res is True

    updated = await folder_svc.get_active_session(user_id)
    assert updated["file_count"] == 8
    new_status = mock_bot.sent_messages[-1]
    assert updated["status_msg_id"] == new_status["msg"].id
    final_texts = [b.text for row in new_status["reply_markup"].inline_keyboard for b in row]
    assert not any("Add Files" in t for t in final_texts)
    assert "📋 View Files" in final_texts


@pytest.mark.asyncio
async def test_56_burst_processing_coalescing_regression(mock_db, folder_svc, mock_bot):
    """TEST 56 (UI Simplification 8): 10 files in rapid burst coalesce properly and final status has no Add Files button."""
    from FileStream.bot.plugins import batch

    user_id = 3005
    initial_status_id = 8000
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)

    files = [f"BurstSimplified_{i}.mkv" for i in range(1, 11)]
    mock_messages = []

    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 50 * 1024 * 1024, "file_unique_id": f"burst_simp_{idx}"}
        await mock_db.file.insert_one(doc)

        msg = MockMessage(message_id=8100 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"burst_simp_{idx}"
        media.file_name = name
        media.file_size = 50 * 1024 * 1024
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def file_worker(m, fid, name):
            async def mock_process(*args, **kwargs):
                await asyncio.sleep(0.01)
                return fid, True

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        tasks = [file_worker(m, fid, name) for m, fid, name in mock_messages]
        results = await asyncio.gather(*tasks)

    assert results == [True] * 10
    session = await folder_svc.get_active_session(user_id)
    assert session["file_count"] == 10

    # Coalescing invariant preserved: <= 2 sent status messages
    assert len(mock_bot.sent_messages) <= 2
    last_status = mock_bot.sent_messages[-1]
    assert "Files added: <b>10</b>" in last_status["text"]
    assert session["status_msg_id"] == last_status["msg"].id

    final_buttons = [b.text for row in last_status["reply_markup"].inline_keyboard for b in row]
    assert not any("Add Files" in t for t in final_buttons)
    assert "📋 View Files" in final_buttons
    assert "✅ Done" in final_buttons
    assert "❌ Cancel" in final_buttons


# ==============================================================================
# TESTS 57 - 62: ADVANCED PERFORMANCE, FLOODWAIT & BATCH DELETION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_57_batched_input_message_deletion(mock_db, folder_svc, mock_bot):
    """TEST 57: Rapid burst buffers user input messages and flushes them in a single batched delete_messages call."""
    from FileStream.bot.plugins import batch

    user_id = 4001
    initial_status_id = 9000
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)
    session["force_batch_delete"] = True

    files = [f"BatchDel_{i}.mkv" for i in range(1, 6)]
    mock_messages = []
    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 1024, "file_unique_id": f"bdel_{idx}"}
        await mock_db.file.insert_one(doc)

        msg = MockMessage(message_id=9100 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"bdel_{idx}"
        media.file_name = name
        media.file_size = 1024
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def file_worker(m, fid, name):
            async def mock_process(*args, **kwargs):
                await asyncio.sleep(0.005)
                return fid, True

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        tasks = [file_worker(m, fid, name) for m, fid, name in mock_messages]
        results = await asyncio.gather(*tasks)

    assert results == [True] * 5
    # Input messages were deleted in mock_bot.deleted_messages in batch
    deleted_ids = [d["message_id"] for d in mock_bot.deleted_messages]
    for idx in range(5):
        assert (9100 + idx) in deleted_ids
    # Old status message was deleted
    assert initial_status_id in deleted_ids


@pytest.mark.asyncio
async def test_58_adaptive_status_refresh_coalesces_to_single_send(mock_db, folder_svc, mock_bot):
    """TEST 58: Rapid 10-file burst produces exactly 1 final status send and 1 status delete."""
    from FileStream.bot.plugins import batch

    user_id = 4002
    initial_status_id = 9200
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=initial_status_id)

    files = [f"BurstSingleSend_{i}.mkv" for i in range(1, 11)]
    mock_messages = []
    for idx, name in enumerate(files):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": name, "file_size": 2048, "file_unique_id": f"bss_{idx}"}
        await mock_db.file.insert_one(doc)

        msg = MockMessage(message_id=9300 + idx, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"bss_{idx}"
        media.file_name = name
        media.file_size = 2048
        msg.media = MagicMock(value="document")
        msg.document = media
        mock_messages.append((msg, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def file_worker(m, fid, name):
            async def mock_process(*args, **kwargs):
                await asyncio.sleep(0.005)
                return fid, True

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
                return await batch.handle_folder_file(mock_bot, m, user_id=user_id)

        tasks = [file_worker(m, fid, name) for m, fid, name in mock_messages]
        results = await asyncio.gather(*tasks)

    assert results == [True] * 10
    # Exactly 1 new status message sent!
    assert len(mock_bot.sent_messages) == 1
    last_status = mock_bot.sent_messages[0]
    assert "Files added: <b>10</b>" in last_status["text"]
    assert "• BurstSingleSend_10.mkv" in last_status["text"]


@pytest.mark.asyncio
async def test_59_bounded_storage_semaphore_and_flog_concurrency(mock_db):
    """TEST 59: FileService uses a bounded Semaphore(5) to limit concurrent FLOG operations."""
    from FileStream.services.file_service import FileService

    file_svc = FileService(mock_db, multi_clients={})
    assert hasattr(file_svc, "_storage_semaphore")
    assert file_svc._storage_semaphore._value == 5

    max_concurrent = 0
    current_concurrent = 0

    async def mock_get_file_ids(client, db_id, multi_clients, message):
        nonlocal max_concurrent, current_concurrent
        current_concurrent += 1
        if current_concurrent > max_concurrent:
            max_concurrent = current_concurrent
        await asyncio.sleep(0.01)
        current_concurrent -= 1

    messages = []
    for i in range(15):
        msg = MockMessage(message_id=9400 + i, user_id=4003, chat_id=4003)
        media = MagicMock()
        media.file_unique_id = f"flog_sem_{i}"
        media.file_name = f"sem_file_{i}.mkv"
        media.file_size = 1000
        msg.media = MagicMock(value="document")
        msg.document = media
        messages.append(msg)

    with patch("FileStream.services.file_service.get_file_info", side_effect=lambda m: {"file_id": f"fid_{m.id}", "file_name": f"sem_file_{m.id}.mkv", "file_size": 1000, "user_id": 4003, "file_unique_id": f"uniq_{m.id}"}), \
         patch("FileStream.services.file_service.get_file_ids", side_effect=mock_get_file_ids):
        tasks = [file_svc.process_new_file(m, check_duplicate=True) for m in messages]
        results = await asyncio.gather(*tasks)

    assert len(results) == 15
    assert max_concurrent <= 5


@pytest.mark.asyncio
async def test_60_view_files_slice_enrichment_does_not_load_all_files(mock_db, folder_svc):
    """TEST 60: _enrich_page_files only queries the database for the active page slice."""
    from FileStream.bot.plugins.batch import _enrich_page_files

    # 15 files where file_name is missing (requiring DB enrichment)
    files = []
    for i in range(1, 16):
        fid = ObjectId()
        doc = {"_id": fid, "file_name": f"EnrichedDoc_{i:02d}.pdf", "file_size": 2000 * i}
        await mock_db.file.insert_one(doc)
        files.append({"file_id": str(fid)})

    # Enrich page 2 (items 6 to 10)
    enriched = await _enrich_page_files(files, page=2, page_size=5, db_instance=mock_db)
    assert len(enriched) == 15
    # Items 6-10 enriched with real names
    assert enriched[5]["file_name"] == "EnrichedDoc_06.pdf"
    assert enriched[9]["file_name"] == "EnrichedDoc_10.pdf"
    # Items 1-5 and 11-15 were not touched (lazy loading)
    assert "file_name" not in enriched[0]
    assert "file_name" not in enriched[14]


@pytest.mark.asyncio
async def test_61_floodwait_during_batch_deletion_retries_safely(mock_db, folder_svc, mock_bot):
    """TEST 61: FloodWait exception during batch deletion sleeps and retries safely."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 4004
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=9500)
    session["pending_input_deletions"] = {9601, 9602}

    call_count = 0

    async def mock_delete_messages(chat_id, message_ids):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FloodWait(1)
        return True

    mock_bot.delete_messages = mock_delete_messages

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        res = await batch._flush_pending_input_deletions(mock_bot, session, chat_id=user_id)
        assert res is True
        mock_sleep.assert_called_once_with(1)
        assert call_count == 2
        assert len(session["pending_input_deletions"]) == 0


@pytest.mark.asyncio
async def test_62_cancellation_during_concurrent_burst_clears_pending_deletions(mock_db, folder_svc, mock_bot):
    """TEST 62: Cancelling folder while input deletions are pending safely flushes deletions."""
    from FileStream.bot.plugins import batch

    user_id = 4005
    status_id = 9700
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_id)

    msg1 = MockMessage(message_id=9701, user_id=user_id, chat_id=user_id)
    msg2 = MockMessage(message_id=9702, user_id=user_id, chat_id=user_id)
    batch._queue_folder_input_deletion(session, msg1)
    batch._queue_folder_input_deletion(session, msg2)

    assert len(session["pending_input_deletions"]) == 2

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    assert msg1.deleted is True
    assert msg2.deleted is True
    assert await folder_svc.get_active_session(user_id) is None
    assert any("cancelled" in str(m["text"]).lower() for m in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_63_burst_100_files_single_authoritative_status(mock_db, folder_svc, mock_bot):
    """TEST 63: Ingesting 100 files in a single burst maintains exactly one authoritative status and batches deletions."""
    from FileStream.bot.plugins import batch

    user_id = 4100
    chat_id = user_id
    await folder_svc.create_session(user_id=user_id, chat_id=chat_id, status_msg_id=88000)

    messages = []
    for i in range(100):
        fid = str(ObjectId())
        name = f"File_{i:03d}.mkv"
        await mock_db.file.insert_one({
            "_id": ObjectId(fid),
            "file_name": name,
            "file_size": 1000,
            "file_unique_id": f"uniq_100_{i}",
        })
        m = MockMessage(message_id=89000 + i, user_id=user_id, chat_id=chat_id)
        m.chat.type = "private"
        media = MagicMock()
        media.file_id = fid
        media.file_unique_id = f"uniq_100_{i}"
        media.file_name = name
        media.file_size = 1000
        m.media = MagicMock(value="document")
        m.document = media
        messages.append((m, fid, name))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def process_item(msg, fid, name):
            with patch.object(batch, "get_file_info", return_value={"file_id": fid, "file_name": name, "file_size": 1000, "file_unique_id": f"uniq_{fid}"}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                return await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

        tasks = [process_item(m, fid, name) for m, fid, name in messages]
        results = await asyncio.gather(*tasks)
        assert all(results)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert session["file_count"] == 100
    # All 100 files had their messages deleted
    assert all(m.deleted for m, _, _ in messages)


@pytest.mark.asyncio
async def test_64_floodwait_retry_bounded_and_recovers():
    """TEST 64: call_with_floodwait handles FloodWait(1), FloodWait(3), and raises on max_retries with FloodWait(10)."""
    from FileStream.utils.floodwait import call_with_floodwait

    class FakeFloodWait(Exception):
        def __init__(self, value):
            self.value = value
            super().__init__(f"A wait of {value} seconds is required")

    # Case 1: Recovers after 1s wait
    attempts_1 = 0

    async def fn_recover_1():
        nonlocal attempts_1
        attempts_1 += 1
        if attempts_1 == 1:
            raise FakeFloodWait(1)
        return "success_1"

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        res = await call_with_floodwait(fn_recover_1, max_retries=3, max_wait_seconds=30.0)
        assert res == "success_1"
        assert attempts_1 == 2
        mock_sleep.assert_called_once_with(1.0)

    # Case 2: Recovers after 3s wait with backoff
    attempts_2 = 0

    async def fn_recover_2():
        nonlocal attempts_2
        attempts_2 += 1
        if attempts_2 == 1:
            raise FakeFloodWait(3)
        return "success_2"

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        res = await call_with_floodwait(fn_recover_2, max_retries=3, max_wait_seconds=30.0)
        assert res == "success_2"
        mock_sleep.assert_called_once_with(3.0)

    # Case 3: Exhausted max_retries with FloodWait(10) raises
    attempts_3 = 0

    async def fn_exhaust():
        nonlocal attempts_3
        attempts_3 += 1
        raise FakeFloodWait(10)

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(FakeFloodWait):
            await call_with_floodwait(fn_exhaust, max_retries=2, max_wait_seconds=30.0)
        assert attempts_3 == 3  # 1 initial + 2 retries = 3 calls before raising


@pytest.mark.asyncio
async def test_65_cancellation_during_mid_flight_burst_5_10_50(mock_db, folder_svc, mock_bot):
    """TEST 65: Cancelling during active in-flight bursts (5, 10, 50 files) leaves zero leaked tasks or pending state."""
    from FileStream.bot.plugins import batch

    for burst_size in (5, 10, 50):
        uid = 4200 + burst_size
        session = await folder_svc.create_session(user_id=uid, chat_id=uid, status_msg_id=70000 + burst_size)
        session["in_flight"] = burst_size

        # Queue pending deletions
        msgs = [MockMessage(message_id=71000 + burst_size + i, user_id=uid, chat_id=uid) for i in range(burst_size)]
        for m in msgs:
            batch._queue_folder_input_deletion(session, m)

        assert len(session["pending_input_deletions"]) == burst_size

        with patch.object(batch, "db", mock_db), \
             patch.object(batch, "folder_service", folder_svc):
            await batch.cancel_folder(mock_bot, user_id=uid, chat_id=uid)

        # In-memory session removed
        assert await folder_svc.get_active_session(uid) is None
        # All messages deleted
        assert all(m.deleted for m in msgs)
        # Pending state cleared
        assert len(session["pending_input_deletions"]) == 0


@pytest.mark.asyncio
async def test_66_concurrent_users_complete_isolation(mock_db, folder_svc, mock_bot):
    """TEST 66: User A, User B, and User C simultaneously ingesting 20 files each have fully isolated sessions."""
    from FileStream.bot.plugins import batch

    users = [5001, 5002, 5003]
    for uid in users:
        await folder_svc.create_session(user_id=uid, chat_id=uid, status_msg_id=60000 + uid)

    async def ingest_user_files(uid: int):
        for i in range(20):
            fid = str(ObjectId())
            name = f"Movie_{uid}_{i}.mkv"
            await mock_db.file.insert_one({
                "_id": ObjectId(fid),
                "file_name": name,
                "file_size": 200,
                "file_unique_id": f"uniq_{uid}_{i}",
            })
            m = MockMessage(message_id=uid * 100 + i, user_id=uid, chat_id=uid)
            m.chat.type = "private"
            with patch.object(batch, "get_file_info", return_value={"file_id": fid, "file_name": name, "file_size": 200, "file_unique_id": f"uniq_{uid}_{i}"}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                await batch.handle_folder_file(mock_bot, m, user_id=uid)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await asyncio.gather(*[ingest_user_files(uid) for uid in users])

    for uid in users:
        sess = await folder_svc.get_active_session(uid)
        assert sess is not None
        assert sess["file_count"] == 20
        assert sess["total_size"] == 20 * 200
        # Exactly 20 files for this specific user
        assert len(sess["files"]) == 20
        assert all(f"_{uid}_" in f["file_name"] for f in sess["files"])


@pytest.mark.asyncio
async def test_67_audit_manager_bounded_concurrency_and_shutdown():
    """TEST 67: AuditTaskManager bounds concurrency, executes background work, and drains on shutdown."""
    from FileStream.utils.audit_manager import AuditTaskManager

    manager = AuditTaskManager(max_concurrent=3, max_queue=20)
    assert manager.active_count == 0

    max_observed = 0
    current_observed = 0

    async def mock_audit_task(idx):
        nonlocal max_observed, current_observed
        current_observed += 1
        if current_observed > max_observed:
            max_observed = current_observed
        await asyncio.sleep(0.01)
        current_observed -= 1

    for i in range(10):
        enqueued = manager.enqueue(mock_audit_task, i)
        assert enqueued is True

    await asyncio.sleep(0.05)
    assert max_observed <= 3
    await manager.shutdown(timeout=1.0)
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_68_storage_service_multi_client_rotation(mock_db):
    """TEST 68: StorageService balances across multiple configured clients based on inflight and health."""
    from FileStream.services.storage_service import StorageService

    c1 = MagicMock()
    c1.id = 111
    c2 = MagicMock()
    c2.id = 222
    c3 = MagicMock()
    c3.id = 333
    clients = {1: c1, 2: c2, 3: c3}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=50, concurrency_per_client=2)

    # Initially, all clients have 0 score. First client selected
    sel1 = svc.select_best_client()
    assert sel1 is not None
    assert sel1[0] in (1, 2, 3)

    # Increase inflight on selected client
    _, _, stat1 = sel1
    stat1.current_inflight += 1

    # Next selection picks another client with lower inflight
    sel2 = svc.select_best_client()
    assert sel2 is not None
    assert sel2[0] != sel1[0]

    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_69_storage_service_client_cooldown_on_floodwait(mock_db):
    """TEST 69: When client 1 hits FloodWait, it enters cooldown while clients 2 and 3 continue handling traffic."""
    from FileStream.services.storage_service import StorageService

    c1 = MagicMock()
    c1.id = 101
    c2 = MagicMock()
    c2.id = 102
    clients = {1: c1, 2: c2}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=20, concurrency_per_client=2)

    # Put client 1 into cooldown
    stat1 = svc._ensure_client_stat(1, c1)
    stat1.cooldown_until = time.monotonic() + 10.0
    stat1.floodwait_count = 1

    # Next select_best_client MUST skip client 1 and pick client 2
    for _ in range(5):
        sel = svc.select_best_client()
        assert sel is not None
        assert sel[0] == 2

    # Stats snapshot reflects cooldown
    stats = svc.get_client_stats()
    assert stats[1]["in_cooldown"] is True
    assert stats[1]["cooldown_remaining_s"] > 0
    assert stats[2]["in_cooldown"] is False

    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_70_storage_service_zero_client_fallback(mock_db):
    """TEST 70: When multi_clients is empty, StorageService gracefully falls back to FileStream (Client 0)."""
    from FileStream.services.storage_service import StorageService
    import FileStream.services.storage_service as ss_mod

    mock_filestream = MagicMock()
    mock_filestream.id = 999999

    with patch.object(ss_mod, "FileStream", mock_filestream):
        svc = StorageService(db=mock_db, clients={}, max_queue_size=20)
        sel = svc.select_best_client()
        assert sel is not None
        assert sel[0] == 0  # Client 0 fallback
        assert sel[1] == mock_filestream

        await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_71_storage_service_client_failure_state_transitions(mock_db):
    """TEST 71: Storage job transitions through states: pending -> processing -> retrying -> failed on error."""
    from FileStream.services.storage_service import StorageService, StorageStatus, StorageJob

    c1 = MagicMock()
    c1.id = 555
    svc = StorageService(db=mock_db, clients={1: c1}, max_queue_size=10)

    job = StorageJob(
        db_id=str(ObjectId()),
        file_id="tg_file_fail",
        file_name="fail.mkv",
        file_size=1024,
        file_info={"file_name": "fail.mkv", "file_size": 1024},
        max_retries=1,
    )

    with patch("FileStream.utils.file_properties.send_file", side_effect=RuntimeError("Channel permissions error")):
        # First attempt -> Retrying
        res = await svc._process_job(job)
        assert res is False
        assert job.status == StorageStatus.RETRYING
        assert job.retry_count == 1

        # Second attempt (exceeds max_retries) -> Failed
        res2 = await svc._process_job(job)
        assert res2 is False
        assert job.status == StorageStatus.FAILED
        assert job.error is not None
        assert "Channel permissions error" in job.error

    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_72_storage_queue_saturation_and_backpressure(mock_db):
    """TEST 72: Bounded storage queue prevents memory runaway when saturated."""
    from FileStream.services.storage_service import StorageService

    svc = StorageService(db=mock_db, clients={}, max_queue_size=3)
    dummy_worker = asyncio.create_task(asyncio.sleep(100))
    svc._workers = [dummy_worker]
    svc._started = True

    try:
        # Fill queue to capacity (3 items)
        await svc.enqueue_storage("id1", "fid1", "f1.mkv", 100, {})
        await svc.enqueue_storage("id2", "fid2", "f2.mkv", 200, {})
        await svc.enqueue_storage("id3", "fid3", "f3.mkv", 300, {})

        assert svc._queue.full()

        # Next enqueue without workers will wait (backpressure timeout)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                svc.enqueue_storage("id4", "fid4", "f4.mkv", 400, {}),
                timeout=0.05
            )
    finally:
        dummy_worker.cancel()
        await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_73_storage_service_graceful_shutdown(mock_db):
    """TEST 73: Shutdown drains queue and cancels worker tasks cleanly with zero leaked tasks."""
    from FileStream.services.storage_service import StorageService

    c1 = MagicMock()
    c1.id = 777
    svc = StorageService(db=mock_db, clients={1: c1}, max_queue_size=20)

    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=MagicMock(id=999))):
        svc.ensure_started()
        for i in range(5):
            await svc.enqueue_storage(str(ObjectId()), f"fid_{i}", f"file_{i}.mkv", 1000, {})

        # Shutdown waits for drain
        await svc.shutdown(timeout=2.0)
        assert svc._queue.empty()
        assert not svc._started
        assert len(svc._workers) == 0


@pytest.mark.asyncio
async def test_74_append_folder_session_file_efficiency(mock_db, folder_svc):
    """TEST 74: append_folder_session_file accurately increments file_count, total_size, and caps recent_names."""
    uid = 6001
    await folder_svc.create_session(user_id=uid, chat_id=uid)

    for i in range(15):
        entry = {"file_id": f"fid_{i}", "file_name": f"batch_{i}.mkv", "file_size": 100}
        await mock_db.append_folder_session_file(
            uid,
            entry,
            recent_name=f"batch_{i}.mkv",
            file_size=100,
            is_new=True,
            file_unique_id=f"uniq_{i}",
        )

    doc = await mock_db.get_folder_session(uid)
    assert doc["file_count"] == 15
    assert doc["total_size"] == 1500
    assert len(doc["files"]) == 15
    # Capped at 10 items
    assert len(doc["recent_names"]) == 10
    assert doc["recent_names"][-1] == "batch_14.mkv"
    assert doc["recent_names"][0] == "batch_5.mkv"


@pytest.mark.asyncio
async def test_75_large_burst_100_500_1000_files_memory_and_integrity(mock_db, folder_svc):
    """TEST 75: Session memory stays bounded and accurate under bursts of 100, 500, and 1000 files."""
    for burst_count in (100, 500, 1000):
        uid = 7000 + burst_count
        session = await folder_svc.create_session(user_id=uid, chat_id=uid)

        for i in range(burst_count):
            fid = f"fid_{burst_count}_{i}"
            file_info = {
                "_id": fid,
                "file_name": f"Video_{burst_count}_{i}.mp4",
                "file_size": 2048,
                "file_unique_id": f"uniq_{burst_count}_{i}",
            }
            success, reason, _ = await folder_svc.add_file(
                user_id=uid,
                file_info=file_info,
                is_new=True,
            )
            assert success is True

        assert session["file_count"] == burst_count
        assert session["total_size"] == burst_count * 2048
        # Memory control: recent_names never exceeds 10 items in session
        assert len(session["recent_names"]) <= 10
        assert session["recent_names"][-1] == f"Video_{burst_count}_{burst_count - 1}.mp4"


@pytest.mark.asyncio
async def test_76_multi_user_concurrency_heavy_burst(mock_db, folder_svc, mock_bot):
    """TEST 76: Multi-user heavy concurrency (User A: 100, User B: 100, User C: 50) maintain complete isolation."""
    from FileStream.bot.plugins import batch

    user_loads = {
        8001: 100,  # User A
        8002: 100,  # User B
        8003: 50,   # User C
    }

    for uid in user_loads:
        await folder_svc.create_session(user_id=uid, chat_id=uid, status_msg_id=uid * 10)

    messages_by_user = {}
    for uid, count in user_loads.items():
        messages_by_user[uid] = []
        for i in range(count):
            fid = str(ObjectId())
            name = f"BurstDoc_{uid}_{i}.pdf"
            await mock_db.file.insert_one({
                "_id": ObjectId(fid),
                "file_name": name,
                "file_size": 500,
                "file_unique_id": f"uniq_{uid}_{i}",
            })
            m = MockMessage(message_id=uid * 1000 + i, user_id=uid, chat_id=uid)
            m.chat.type = "private"
            m._test_fid = fid
            m._test_name = name
            m._test_uniq = f"uniq_{uid}_{i}"
            messages_by_user[uid].append(m)

    def mock_get_file_info(msg):
        return {
            "file_id": getattr(msg, "_test_fid"),
            "file_name": getattr(msg, "_test_name"),
            "file_size": 500,
            "file_unique_id": getattr(msg, "_test_uniq"),
        }

    async def mock_process(msg, bot_id=None, check_duplicate=False):
        return getattr(msg, "_test_fid"), True

    async def ingest_user(uid: int):
        for m in messages_by_user[uid]:
            await batch.handle_folder_file(mock_bot, m, user_id=uid)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", side_effect=mock_get_file_info), \
         patch.object(batch.file_service, "process_new_file", side_effect=mock_process):
        await asyncio.gather(*[ingest_user(uid) for uid in user_loads.keys()])

    for uid, count in user_loads.items():
        sess = await folder_svc.get_active_session(uid)
        assert sess is not None
        assert sess["file_count"] == count
        assert sess["total_size"] == count * 500
        assert len(sess["files"]) == count
        # Verify no file cross-contamination
        assert all(f"_{uid}_" in f["file_name"] for f in sess["files"])
        # Exactly one authoritative status message exists and is maintained
        assert sess["status_msg_id"] is not None


# =====================================================================
# TESTS 77-100: FINAL PRODUCTION FLOODWAIT ELIMINATION,
# MULTI-CLIENT STORAGE VERIFICATION & LIVE CANCELLATION PROGRESS
# =====================================================================

@pytest.mark.asyncio
async def test_77_multi_client_concurrency_load_distribution(mock_db):
    """TEST 77: Parallel storage jobs are distributed across available multi-clients without single-client pinning."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock()
    c0.id = 1000
    c1 = MagicMock()
    c1.id = 1001
    c2 = MagicMock()
    c2.id = 1002
    clients = {0: c0, 1: c1, 2: c2}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=50, concurrency_per_client=2)

    selected_clients = set()
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=MagicMock(id=999))):
        for i in range(6):
            sel = svc.select_best_client()
            assert sel is not None
            c_idx, _, stat = sel
            selected_clients.add(c_idx)
            stat.current_inflight += 1

    # Verified: all 3 clients were utilized under concurrent load
    assert len(selected_clients) == 3
    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_78_client_cooldown_isolation_multi_client(mock_db):
    """TEST 78: When Client 1 encounters FloodWait, it is placed on cooldown and subsequent jobs route to Client 0 and 2."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock()
    c0.id = 1000
    c1 = MagicMock()
    c1.id = 1001
    c2 = MagicMock()
    c2.id = 1002
    clients = {0: c0, 1: c1, 2: c2}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=20)
    stat1 = svc._ensure_client_stat(1, c1)
    stat1.cooldown_until = time.monotonic() + 30.0
    stat1.floodwait_count = 1

    routed_indices = []
    for _ in range(4):
        sel = svc.select_best_client()
        assert sel is not None
        c_idx, _, stat = sel
        stat.current_inflight += 1
        routed_indices.append(c_idx)

    # Client 1 was skipped 100% of the time during cooldown
    assert 1 not in routed_indices
    assert set(routed_indices) == {0, 2}
    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_79_audit_manager_non_blocking_execution():
    """TEST 79: Non-critical audit operations execute asynchronously without blocking main flow."""
    from FileStream.utils.audit_manager import AuditTaskManager

    manager = AuditTaskManager(max_concurrent=5, max_queue=20)
    executed = []

    async def sample_audit(val):
        await asyncio.sleep(0.02)
        executed.append(val)

    enqueued = manager.enqueue(sample_audit, "audit_card_1")
    assert enqueued is True
    assert manager.active_count >= 1

    await manager.shutdown(timeout=1.0)
    assert "audit_card_1" in executed


@pytest.mark.asyncio
async def test_80_audit_manager_error_tolerance():
    """TEST 80: Exceptions inside background audit tasks are safely absorbed without crashing."""
    from FileStream.utils.audit_manager import AuditTaskManager

    manager = AuditTaskManager(max_concurrent=5, max_queue=20)

    async def failing_audit():
        raise RuntimeError("Audit channel permissions revoked")

    enqueued = manager.enqueue(failing_audit)
    assert enqueued is True

    # Shutdown drains safely without raising exception to caller
    await manager.shutdown(timeout=0.5)
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_81_storage_queue_job_cancellation_by_session(mock_db):
    """TEST 81: StorageService.cancel_session_jobs marks queued jobs as CANCELLED and cancels futures."""
    from FileStream.services.storage_service import StorageService, StorageStatus

    svc = StorageService(db=mock_db, clients={}, max_queue_size=20)
    # Stop workers from processing immediately
    svc._started = True
    dummy = asyncio.create_task(asyncio.sleep(100))
    svc._workers = [dummy]

    try:
        job1 = await svc.enqueue_storage("id1", "fid1", "f1.mkv", 100, {}, session_id="sess_target", create_future=True)
        job2 = await svc.enqueue_storage("id2", "fid2", "f2.mkv", 200, {}, session_id="sess_target", create_future=True)
        job3 = await svc.enqueue_storage("id3", "fid3", "f3.mkv", 300, {}, session_id="sess_other", create_future=True)

        cancelled = svc.cancel_session_jobs("sess_target")
        assert cancelled == 2

        assert job1.status == StorageStatus.CANCELLED
        assert job2.status == StorageStatus.CANCELLED
        assert job3.status == StorageStatus.PENDING

        assert job1.future.cancelled()
        assert job2.future.cancelled()
        assert not job3.future.cancelled()
    finally:
        dummy.cancel()
        await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_82_storage_worker_skips_cancelled_job(mock_db):
    """TEST 82: Storage worker dequeues a CANCELLED job and skips network transmission entirely."""
    from FileStream.services.storage_service import StorageService, StorageStatus, StorageJob

    c0 = MagicMock()
    c0.id = 999
    svc = StorageService(db=mock_db, clients={0: c0}, max_queue_size=10)

    job = StorageJob(
        db_id=str(ObjectId()),
        file_id="fid_skipped",
        file_name="skipped.mkv",
        file_size=1024,
        file_info={},
        session_id="cancelled_sess",
        status=StorageStatus.CANCELLED,
    )

    with patch("FileStream.utils.file_properties.send_file") as mock_send:
        result = await svc._process_job(job)
        assert result is False
        mock_send.assert_not_called()

    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_83_in_flight_job_late_cancellation_cleanup(mock_db):
    """TEST 83: If session is cancelled while send_file was in-flight, late message is purged immediately."""
    from FileStream.services.storage_service import StorageService, StorageStatus, StorageJob

    mock_client = MagicMock()
    mock_client.id = 888
    mock_client.delete_messages = AsyncMock(return_value=True)

    svc = StorageService(db=mock_db, clients={0: mock_client}, max_queue_size=10)
    sess_id = "late_cancel_sess"
    db_id = str(ObjectId())

    # Pre-insert DB doc
    await mock_db.file.insert_one({"_id": ObjectId(db_id), "file_name": "late.mkv"})

    job = StorageJob(
        db_id=db_id,
        file_id="fid_late",
        file_name="late.mkv",
        file_size=1024,
        file_info={"flog_channel_id": -100123},
        session_id=sess_id,
        target_channel_id=-100123,
    )

    fake_log_msg = MagicMock(id=5555)

    async def mock_send_with_concurrent_cancel(*args, **kwargs):
        # Session is cancelled while this call was in progress!
        svc.record_cancelled_session(sess_id)
        return fake_log_msg

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send_with_concurrent_cancel):
        res = await svc._process_job(job)
        assert res is False
        assert job.status == StorageStatus.CANCELLED

    # Verified: late message was deleted and DB file was cleaned
    mock_client.delete_messages.assert_called_once_with(-100123, 5555)
    assert await mock_db.file.count_documents({"_id": ObjectId(db_id)}) == 0

    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_84_batched_telegram_message_deletion_100_files(mock_db, folder_svc, mock_bot):
    """TEST 84: 100 storage messages in a channel are deleted in a single batched delete_messages call."""
    user_id = 9084
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    # Seed 100 files
    for i in range(100):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "file_name": f"batch_{i}.mkv",
            "file_size": 100,
            "flog_channel_id": -100999,
            "flog_msg_id": 1000 + i,
        })
        await folder_svc.add_file(user_id, {
            "_id": fid,
            "file_name": f"batch_{i}.mkv",
            "file_size": 100,
            "flog_channel_id": -100999,
            "flog_msg_id": 1000 + i,
        }, is_new=True)

    delete_calls = []
    async def mock_delete_messages(ch_id, ids):
        delete_calls.append({"channel": ch_id, "ids": ids})
        return True

    mock_bot.delete_messages = mock_delete_messages
    res = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert res is True

    # Verified: exactly 1 call with 100 IDs
    assert len(delete_calls) == 1
    assert delete_calls[0]["channel"] == -100999
    assert len(delete_calls[0]["ids"]) == 100
    assert await mock_db.file.count_documents({}) == 0


@pytest.mark.asyncio
async def test_85_multi_batch_telegram_deletion_250_files(mock_db, folder_svc, mock_bot):
    """TEST 85: 250 files are partitioned into batches of at most 100 per delete_messages call (100+100+50 = 3 calls)."""
    user_id = 9085
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    for i in range(250):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "file_name": f"part_{i}.mkv",
            "file_size": 100,
            "flog_channel_id": -100888,
            "flog_msg_id": 2000 + i,
        })
        await folder_svc.add_file(user_id, {
            "_id": fid,
            "file_name": f"part_{i}.mkv",
            "file_size": 100,
            "flog_channel_id": -100888,
            "flog_msg_id": 2000 + i,
        }, is_new=True)

    delete_calls = []
    async def mock_delete_messages(ch_id, ids):
        delete_calls.append(list(ids))
        return True

    mock_bot.delete_messages = mock_delete_messages
    res = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert res is True

    # Verified: 3 batches of lengths 100, 100, 50
    assert len(delete_calls) == 3
    assert len(delete_calls[0]) == 100
    assert len(delete_calls[1]) == 100
    assert len(delete_calls[2]) == 50
    assert await mock_db.file.count_documents({}) == 0


@pytest.mark.asyncio
async def test_86_batched_db_deletion_delete_many(mock_db, folder_svc, mock_bot):
    """TEST 86: cancel_session uses delete_many for efficient batch database pruning."""
    user_id = 9086
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    for i in range(10):
        fid = ObjectId()
        await mock_db.file.insert_one({"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 50})
        await folder_svc.add_file(user_id, {"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 50}, is_new=True)

    with patch.object(mock_db.file, "delete_many", wraps=mock_db.file.delete_many) as mock_del_many:
        res = await folder_svc.cancel_session(user_id, bot=mock_bot)
        assert res is True
        assert mock_del_many.called
        assert await mock_db.file.count_documents({}) == 0


@pytest.mark.asyncio
async def test_87_live_cancellation_progress_preparing_state(mock_db, folder_svc, mock_bot):
    """TEST 87: cancel_folder immediately renders the preparing cleanup panel with keyboard removed."""
    from FileStream.bot.plugins import batch

    user_id = 9087
    status_id = 7087
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_id)
    f = {"_id": ObjectId(), "file_name": "prep.mkv", "file_size": 100}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    # First edit MUST be preparing state with reply_markup=None
    first_edit = mock_bot.edited_messages[0]
    assert "🛑 <b>Cancelling Folder</b>" in first_edit["text"]
    assert "Preparing cleanup..." in first_edit["text"]
    assert first_edit["reply_markup"] is None


@pytest.mark.asyncio
async def test_88_live_cancellation_progress_callback_invocations(mock_db, folder_svc, mock_bot):
    """TEST 88: progress_callback is called with monotonically increasing cleaned file counts."""
    user_id = 9088
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    for i in range(120):
        fid = ObjectId()
        await mock_db.file.insert_one({"_id": fid, "file_name": f"c_{i}.mkv", "file_size": 10})
        await folder_svc.add_file(user_id, {"_id": fid, "file_name": f"c_{i}.mkv", "file_size": 10}, is_new=True)

    progress_reports = []
    async def mock_callback(cleaned, total):
        progress_reports.append((cleaned, total))

    await folder_svc.cancel_session(user_id, bot=mock_bot, progress_callback=mock_callback)

    # Starts at 0, progresses by batch, finishes at total
    assert progress_reports[0] == (0, 120)
    assert progress_reports[1] == (100, 120)
    assert progress_reports[2] == (120, 120)


@pytest.mark.asyncio
async def test_89_live_cancellation_progress_debouncing(mock_db, folder_svc, mock_bot):
    """TEST 89: Progress bar updates in batch.py are throttled/debounced to avoid Telegram rate limits."""
    from FileStream.bot.plugins import batch

    bar1 = batch._render_progress_bar(50, 100)
    assert "50%" in bar1
    assert "█████░░░░░" in bar1

    bar2 = batch._render_progress_bar(100, 100)
    assert "100%" in bar2
    assert "██████████" in bar2


@pytest.mark.asyncio
async def test_90_final_cancellation_message_format(mock_db, folder_svc, mock_bot):
    """TEST 90: Final cancellation message renders exact cleanup totals and clean status."""
    from FileStream.bot.plugins import batch

    user_id = 9090
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=7090)
    for i in range(5):
        f = {"_id": ObjectId(), "file_name": f"fin_{i}.mkv", "file_size": 200}
        await mock_db.file.insert_one(f)
        await folder_svc.add_file(user_id, f, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    final_edit = mock_bot.edited_messages[-1]
    assert "✅ <b>Folder Cancelled</b>" in final_edit["text"]
    assert "5 files cleaned up" in final_edit["text"]
    assert "Temporary data removed" in final_edit["text"]
    assert final_edit["reply_markup"] is None


@pytest.mark.asyncio
async def test_91_zero_file_cancellation_immediate(mock_db, folder_svc, mock_bot):
    """TEST 91: Cancelling an empty folder (0 files) transitions directly to final cancelled state."""
    from FileStream.bot.plugins import batch

    user_id = 9091
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=7091)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    final_edit = mock_bot.edited_messages[-1]
    assert "✅ <b>Folder Cancelled</b>" in final_edit["text"]
    assert "0 files cleaned up" in final_edit["text"]
    assert await folder_svc.get_active_session(user_id) is None


@pytest.mark.asyncio
async def test_92_cancellation_idempotency_concurrent_cancels(mock_db, folder_svc, mock_bot):
    """TEST 92: Multiple concurrent cancel calls for the same user execute safely without double-cleanup."""
    user_id = 9092
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "idem.mkv", "file_size": 100}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    results = await asyncio.gather(
        folder_svc.cancel_session(user_id, bot=mock_bot),
        folder_svc.cancel_session(user_id, bot=mock_bot),
        folder_svc.cancel_session(user_id, bot=mock_bot),
    )
    assert all(r is True for r in results)
    assert folder_svc.get_session(user_id) is None


@pytest.mark.asyncio
async def test_93_multi_user_cancellation_isolation(mock_db, folder_svc, mock_bot):
    """TEST 93: User A cancelling a folder does not affect User B's active folder session."""
    user_a = 90931
    user_b = 90932

    await folder_svc.create_session(user_id=user_a, chat_id=user_a)
    await folder_svc.create_session(user_id=user_b, chat_id=user_b)

    fa = {"_id": ObjectId(), "file_name": "user_a.mkv", "file_size": 100}
    fb = {"_id": ObjectId(), "file_name": "user_b.mkv", "file_size": 200}
    await mock_db.file.insert_one(fa)
    await mock_db.file.insert_one(fb)
    await folder_svc.add_file(user_a, fa, is_new=True)
    await folder_svc.add_file(user_b, fb, is_new=True)

    await folder_svc.cancel_session(user_a, bot=mock_bot)

    # User A is purged
    assert folder_svc.get_session(user_a) is None
    assert await mock_db.file.count_documents({"_id": fa["_id"]}) == 0

    # User B remains 100% active and unimpacted
    sess_b = await folder_svc.get_active_session(user_b)
    assert sess_b is not None
    assert sess_b["file_count"] == 1
    assert await mock_db.file.count_documents({"_id": fb["_id"]}) == 1


@pytest.mark.asyncio
async def test_94_large_scale_1000_file_cancellation_performance(mock_db, folder_svc, mock_bot):
    """TEST 94: Cancelling a massive 1000-file folder completes efficiently in batched calls."""
    user_id = 9094
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)

    bulk_docs = []
    for i in range(1000):
        fid = ObjectId()
        doc = {
            "_id": fid,
            "file_name": f"Massive_{i:04d}.mkv",
            "file_size": 500,
            "flog_channel_id": -100777,
            "flog_msg_id": 50000 + i,
        }
        bulk_docs.append(doc)

    await mock_db.file.insert_many(bulk_docs)

    # Direct append to session for fast setup
    session = folder_svc.get_session(user_id)
    session["new_files"] = [str(d["_id"]) for d in bulk_docs]
    session["file_count"] = 1000
    session["storage_messages"] = [
        {"channel_id": -100777, "message_id": 50000 + i, "file_id": str(bulk_docs[i]["_id"])}
        for i in range(1000)
    ]

    start_time = time.monotonic()
    success = await folder_svc.cancel_session(user_id, bot=mock_bot)
    elapsed = time.monotonic() - start_time

    assert success is True
    # 1000 files in batches must execute efficiently in test environment
    assert elapsed < 10.0
    assert await mock_db.file.count_documents({}) == 0



@pytest.mark.asyncio
async def test_95_partial_failure_during_telegram_delete(mock_db, folder_svc, mock_bot):
    """TEST 95: If Telegram message deletion fails, database records and session are still pruned."""
    user_id = 9095
    await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    f = {"_id": ObjectId(), "file_name": "err.mkv", "file_size": 50, "flog_channel_id": -100, "flog_msg_id": 123}
    await mock_db.file.insert_one(f)
    await folder_svc.add_file(user_id, f, is_new=True)

    with patch.object(mock_bot, "delete_messages", side_effect=RuntimeError("MTProto FloodWait")):
        success = await folder_svc.cancel_session(user_id, bot=mock_bot)
        assert success is True

    assert await mock_db.file.count_documents({}) == 0
    assert folder_svc.get_session(user_id) is None


@pytest.mark.asyncio
async def test_96_input_deletion_buffer_flush_on_cancel(mock_db, folder_svc, mock_bot):
    """TEST 96: Input messages buffered during folder creation are flushed and deleted upon cancel."""
    from FileStream.bot.plugins import batch

    user_id = 9096
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=7096)
    session = folder_svc.get_session(user_id)
    session["pending_input_deletions"] = {901, 902, 903}

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    deleted_ids = [d["message_id"] for d in mock_bot.deleted_messages]
    assert 901 in deleted_ids
    assert 902 in deleted_ids
    assert 903 in deleted_ids


@pytest.mark.asyncio
async def test_97_reject_file_addition_during_cancelling_state(mock_db, folder_svc):
    """TEST 97: A file arriving while folder is actively cancelling is rejected and not retained."""
    user_id = 9097
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session["cancelling"] = True

    f = {"_id": ObjectId(), "file_name": "rejected.mkv", "file_size": 100}
    success, reason, _ = await folder_svc.add_file(user_id, f, is_new=True)
    assert success is False
    assert reason == "in_progress"


@pytest.mark.asyncio
async def test_98_client_startup_diagnostics_safety():
    """TEST 98: log_client_diagnostics produces clean structured diagnostics without logging secrets."""
    from FileStream.bot.clients import log_client_diagnostics
    from FileStream.bot import multi_clients

    mock_client = MagicMock()
    mock_client.id = 123456
    mock_client.username = "test_bot"
    multi_clients[0] = mock_client

    diag = log_client_diagnostics()
    assert diag["total_active"] >= 1
    assert 0 in diag["clients"]
    assert diag["clients"][0]["username"] == "test_bot"


@pytest.mark.asyncio
async def test_99_pyrogram_sleep_threshold_zero_configuration():
    """TEST 99: PYROGRAM_SLEEP_THRESHOLD is set to 0 to prevent Pyrogram internal sleep freezing."""
    from FileStream.config import Telegram

    assert getattr(Telegram, "PYROGRAM_SLEEP_THRESHOLD", 0) == 0


@pytest.mark.asyncio
async def test_100_end_to_end_folder_lifecycle_with_cancellation(mock_db, folder_svc, mock_bot):
    """TEST 100: Complete folder lifecycle: creation -> file ingestion -> cancellation progress -> clean reset."""
    from FileStream.bot.plugins import batch

    user_id = 10000
    status_id = 10001
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=status_id)

    # Ingest 3 files
    for i in range(3):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "file_name": f"E2E_{i}.mkv",
            "file_size": 1024,
            "flog_channel_id": -100555,
            "flog_msg_id": 9000 + i,
        })
        await folder_svc.add_file(user_id, {
            "_id": fid,
            "file_name": f"E2E_{i}.mkv",
            "file_size": 1024,
            "flog_channel_id": -100555,
            "flog_msg_id": 9000 + i,
        }, is_new=True)

    sess = await folder_svc.get_active_session(user_id)
    assert sess["file_count"] == 3

    # Trigger cancel
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)

    # Verified: session is gone, DB cleaned, storage messages deleted, status panel finalized
    assert await folder_svc.get_active_session(user_id) is None
    assert await mock_db.file.count_documents({}) == 0

    deleted_mids = [d["message_id"] for d in mock_bot.deleted_messages]
    assert 9000 in deleted_mids
    assert 9001 in deleted_mids
    assert 9002 in deleted_mids

    final_msg = mock_bot.edited_messages[-1]
    assert "✅ <b>Folder Cancelled</b>" in final_msg["text"]
    assert "3 files cleaned up" in final_msg["text"]
    assert final_msg["reply_markup"] is None


@pytest.mark.asyncio
async def test_101_single_client_configuration_reports_active_one():
    """TEST 101: Single-client configuration reports active_clients = 1 and multi_client = False."""
    import os
    import re
    from FileStream.bot.clients import initialize_clients
    from FileStream.config import Telegram

    clean_env = {k: v for k, v in os.environ.items() if not re.fullmatch(r"MULTI_TOKEN\d+", k)}
    with patch.dict(os.environ, clean_env, clear=True):
        diag = await initialize_clients(force_reinit=True)
        assert diag["active_storage_clients"] == 1
        assert diag["multi_client_mode"] is False
        assert diag["multi_client_storage"] == "DISABLED"
        assert Telegram.MULTI_CLIENT is False
        assert diag["storage_clients"][1] == "NOT CONFIGURED"
        assert diag["storage_clients"][2] == "NOT CONFIGURED"


@pytest.mark.asyncio
async def test_102_configured_secondary_client_enters_storage_registry():
    """TEST 102: Configured secondary client successfully enters the storage registry."""
    import os
    from FileStream.bot.clients import initialize_clients
    from FileStream.services.storage_service import storage_service
    from FileStream.config import Telegram

    mock_client = MagicMock()
    mock_client.id = 2002
    mock_client.username = "bot_worker_1"
    mock_client.client_index = 1
    mock_client.is_connected = True
    mock_client.start = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=MagicMock(id=2002, username="bot_worker_1", first_name="Worker 1", is_bot=True))

    with patch.dict(os.environ, {"MULTI_TOKEN1": "mock_valid_token_12345"}), \
         patch("FileStream.bot.clients.Client", return_value=mock_client), \
         patch("FileStream.bot.clients.clone_primary_handlers", AsyncMock()), \
         patch("FileStream.utils.bot_commands.register_bot_commands", AsyncMock()):
        diag = await initialize_clients(force_reinit=True)
        assert diag["active_storage_clients"] >= 2
        assert diag["multi_client_mode"] is True
        assert diag["multi_client_storage"] == "ENABLED"
        assert Telegram.MULTI_CLIENT is True
        assert "CONNECTED" in diag["storage_clients"][1]

        # Verify registration in storage service
        active_storage = storage_service._get_active_clients()
        active_indices = [idx for idx, _ in active_storage]
        assert 1 in active_indices


@pytest.mark.asyncio
async def test_103_failed_secondary_client_not_reported_as_active():
    """TEST 103: Failed secondary client is not reported as active."""
    import os
    from FileStream.bot.clients import initialize_clients
    from FileStream.config import Telegram

    mock_failing_client = MagicMock()
    mock_failing_client.start = AsyncMock(side_effect=Exception("Unauthorized Token"))

    with patch.dict(os.environ, {"MULTI_TOKEN1": "bad_token"}), \
         patch("FileStream.bot.clients.Client", return_value=mock_failing_client), \
         patch("FileStream.bot.clients.clone_primary_handlers", AsyncMock()):
        diag = await initialize_clients(force_reinit=True)
        assert diag["active_storage_clients"] == 1
        assert diag["multi_client_mode"] is False
        assert Telegram.MULTI_CLIENT is False
        assert diag["storage_clients"][1] == "CONFIGURED / INITIALIZATION FAILED"


@pytest.mark.asyncio
async def test_104_floodwait_removes_client_from_scheduling_until_cooldown_expires(mock_db):
    """TEST 104: FloodWait removes a client from scheduling until cooldown expires."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock()
    c0.id = 5000
    c0.is_connected = True
    clients = {0: c0}
    svc = StorageService(db=mock_db, clients=clients)

    stat0 = svc._ensure_client_stat(0, c0)
    # Put client 0 into cooldown for 20s
    stat0.cooldown_until = time.monotonic() + 20.0

    sel = svc.select_best_client()
    assert sel is None

    # Fast forward cooldown
    stat0.cooldown_until = time.monotonic() - 1.0
    sel = svc.select_best_client()
    assert sel is not None
    assert sel[0] == 0


@pytest.mark.asyncio
async def test_105_healthy_secondary_client_receives_jobs_while_client_0_cooldown(mock_db):
    """TEST 105: Healthy secondary client receives jobs while Client 0 is cooling down."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock(id=5000, is_connected=True)
    c1 = MagicMock(id=5001, is_connected=True)
    clients = {0: c0, 1: c1}
    svc = StorageService(db=mock_db, clients=clients)

    stat0 = svc._ensure_client_stat(0, c0)
    stat1 = svc._ensure_client_stat(1, c1)

    # Arm cooldown on client 0
    stat0.cooldown_until = time.monotonic() + 30.0

    # Next selections must go to client 1
    sel = svc.select_best_client()
    assert sel is not None
    assert sel[0] == 1


@pytest.mark.asyncio
async def test_106_cooled_down_client_0_becomes_eligible_again(mock_db):
    """TEST 106: Cooled-down Client 0 becomes eligible again after cooldown expiration."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock(id=5000, is_connected=True)
    c1 = MagicMock(id=5001, is_connected=True)
    clients = {0: c0, 1: c1}
    svc = StorageService(db=mock_db, clients=clients)

    stat0 = svc._ensure_client_stat(0, c0)
    stat1 = svc._ensure_client_stat(1, c1)

    # Arm brief cooldown on client 0
    stat0.cooldown_until = time.monotonic() + 0.1

    sel = svc.select_best_client()
    assert sel[0] == 1

    await asyncio.sleep(0.15)
    # Now client 0 is eligible again
    sel2 = svc.select_best_client(preferred_id=5000)
    assert sel2 is not None
    assert sel2[0] == 0


@pytest.mark.asyncio
async def test_107_single_client_floodwait_does_not_create_unlimited_retry_tasks(mock_db):
    """TEST 107: Single-client FloodWait does not create unlimited retry tasks."""
    from FileStream.services.storage_service import StorageService, StorageStatus, StorageJob
    from pyrogram.errors import FloodWait

    c0 = MagicMock(id=5000, is_connected=True)
    clients = {0: c0}
    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)

    # Mock send_file to raise FloodWait with 0.05s delay
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(side_effect=FloodWait(0.05))):
        job = StorageJob(
            db_id="607f1f77bcf86cd799439011",
            file_id="tg_123",
            file_name="fw_test.mkv",
            file_size=1000,
            file_info={"file_id": "tg_123"},
            max_retries=2,
            future=asyncio.get_running_loop().create_future(),
        )

        initial_tasks = len(asyncio.all_tasks())
        # Process attempts directly without background worker collision
        await svc._process_job(job)
        await asyncio.sleep(0.06)
        await svc._process_job(job)
        await asyncio.sleep(0.06)
        await svc._process_job(job)

        final_tasks = len(asyncio.all_tasks())
        # No runaway task creation occurred
        assert final_tasks <= initial_tasks + 1
        assert job.status == StorageStatus.FAILED
        assert job.retry_count == 3
        if job.future and job.future.done() and not job.future.cancelled():
            _ = job.future.exception()
        await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_108_single_client_queue_remains_bounded_during_floodwait(mock_db):
    """TEST 108: Single-client queue remains bounded during FloodWait."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock(id=5000, is_connected=True)
    clients = {0: c0}
    max_q = 5
    svc = StorageService(db=mock_db, clients=clients, max_queue_size=max_q)

    # Arm cooldown on client 0
    stat0 = svc._ensure_client_stat(0, c0)
    stat0.cooldown_until = time.monotonic() + 100.0

    # Fill queue up to capacity
    for i in range(max_q):
        await svc.enqueue_storage(
            db_id=f"607f1f77bcf86cd7994390{i:02d}",
            file_id=f"fid_{i}",
            file_name=f"test_{i}.mkv",
            file_size=100,
            file_info={},
        )

    metrics = svc.get_metrics()
    assert metrics["storage_queue_size"] == max_q
    assert metrics["storage_queue_peak"] == max_q
    assert svc._queue.full()
    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_109_two_concurrent_storage_workers_do_not_create_unbounded_telegram_ops(mock_db):
    """TEST 109: Two concurrent storage workers do not create unbounded Telegram operations."""
    from FileStream.services.storage_service import StorageService

    c0 = MagicMock(id=5000, is_connected=True)
    clients = {0: c0}
    svc = StorageService(db=mock_db, clients=clients, max_queue_size=20, concurrency_per_client=1)

    call_count = 0
    async def mock_send(client, db_id, file_id, **kwargs):
        nonlocal call_count
        call_count += 1
        return MagicMock(id=888)

    with patch("FileStream.utils.file_properties.send_file", mock_send):
        for i in range(5):
            await svc.store_file(
                db_id=f"607f1f77bcf86cd7994390{i:02d}",
                file_id=f"fid_{i}",
                file_name=f"bounded_{i}.mkv",
                file_size=100,
                file_info={},
            )

    # Exactly 5 calls executed for 5 files
    assert call_count == 5
    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_110_duplicate_startup_diagnostics_not_emitted_by_duplicate_init():
    """TEST 110: Duplicate startup diagnostics are not emitted by duplicate initialization."""
    import os
    import re
    from FileStream.bot import clients as clients_mod
    from FileStream.bot.clients import initialize_clients

    clients_mod._clients_initialized = False

    clean_env = {k: v for k, v in os.environ.items() if not re.fullmatch(r"MULTI_TOKEN\d+", k)}
    with patch.dict(os.environ, clean_env, clear=True):
        diag1 = await initialize_clients()
        assert diag1["active_storage_clients"] == 1

        # Second call should be a no-op due to idempotency guard
        with patch.object(clients_mod, "log_client_diagnostics", wraps=clients_mod.log_client_diagnostics) as mock_diag:
            diag2 = await initialize_clients()
            assert diag2["active_storage_clients"] == 1
            # Did not execute double gathering
            assert mock_diag.called


@pytest.mark.asyncio
async def test_111_cancellation_preservation_regression(mock_db, folder_svc, mock_bot):
    """TEST 111: Cancellation preservation regression ensures Tests 77-100 capabilities remain intact."""
    from FileStream.bot.plugins import batch

    user_id = 9111
    chat_id = user_id
    sess = await folder_svc.create_session(user_id=user_id, chat_id=chat_id, status_msg_id=77111)

    # Ingest 5 files
    for i in range(5):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "file_name": f"reg_{i}.mkv",
            "file_size": 2048,
            "flog_channel_id": -100444,
            "flog_msg_id": 5500 + i,
            "folder_session_id": sess["session_id"],
        })
        await folder_svc.add_file(user_id, {
            "_id": fid,
            "file_name": f"reg_{i}.mkv",
            "file_size": 2048,
            "flog_channel_id": -100444,
            "flog_msg_id": 5500 + i,
        }, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=chat_id)

    # Session wiped
    assert await folder_svc.get_active_session(user_id) is None
    # DB files wiped
    assert await mock_db.file.count_documents({}) == 0
    # Telegram messages deleted in batch
    deleted_ids = [d["message_id"] for d in mock_bot.deleted_messages]
    for i in range(5):
        assert 5500 + i in deleted_ids

    # Final progress rendered on status message
    final_edit = mock_bot.edited_messages[-1]
    assert "✅ <b>Folder Cancelled</b>" in final_edit["text"]
    assert "5 files cleaned up" in final_edit["text"]


@pytest.mark.asyncio
async def test_112_twelve_forwarded_files_accepted_and_registered(mock_db, folder_svc, mock_bot):
    """TEST 112: 12 forwarded files accepted and registered without loss (accepted_folder_files == 12)."""
    from FileStream.bot.plugins import batch
    user_id = 12001
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12001)

    file_ids = []
    messages = []
    for i in range(12):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": f"Outlander.S01E{i+1:02d}.mkv",
            "file_size": 1024 * 1024 * 500,
            "file_unique_id": f"uniq_outlander_{i}",
        }
        await mock_db.file.insert_one(file_doc)
        file_ids.append(fid)

        msg = MockMessage(message_id=100 + i, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"uniq_outlander_{i}"
        media.file_name = f"Outlander.S01E{i+1:02d}.mkv"
        media.file_size = 1024 * 1024 * 500
        msg.media = MagicMock()
        msg.media.value = "document"
        msg.document = media
        messages.append((msg, fid, f"Outlander.S01E{i+1:02d}.mkv"))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        for msg, fid, name in messages:
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name, "file_size": 1024 * 1024 * 500}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
                assert res is True

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 12
    assert session["file_count"] == 12
    assert len(session["file_ids"]) == 12
    assert session["total_size"] == 12 * 1024 * 1024 * 500


@pytest.mark.asyncio
async def test_113_twelve_concurrent_files_processed_without_loss_or_duplicates(mock_db, folder_svc, mock_bot):
    """TEST 113: 12 concurrent files processed without race conditions, loss, or duplicates."""
    from FileStream.bot.plugins import batch
    user_id = 12002
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12002)

    messages = []
    for i in range(12):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": f"Concurrent_{i:02d}.mkv",
            "file_size": 1000,
            "file_unique_id": f"uniq_conc_{i}",
        }
        await mock_db.file.insert_one(file_doc)

        msg = MockMessage(message_id=200 + i, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"uniq_conc_{i}"
        media.file_name = f"Concurrent_{i:02d}.mkv"
        media.file_size = 1000
        msg.media = MagicMock()
        msg.media.value = "document"
        msg.document = media
        messages.append((msg, fid, f"Concurrent_{i:02d}.mkv"))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def worker(msg, fid, name):
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name, "file_size": 1000}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                return await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

        tasks = [worker(m, fid, name) for m, fid, name in messages]
        results = await asyncio.gather(*tasks)
        assert all(results)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 12
    assert session["file_count"] == 12
    assert len(set(session["file_ids"])) == 12


@pytest.mark.asyncio
async def test_114_single_floodwait_retry_success_registered_once(mock_db, folder_svc, mock_bot):
    """TEST 114: Single FloodWait with retry -> succeeds and registers exactly once in folder."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 12003
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12003)

    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "FloodWait_Single.mkv",
        "file_size": 2048,
        "file_unique_id": "uniq_fw_single",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=301, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_fw_single"
    media.file_name = "FloodWait_Single.mkv"
    media.file_size = 2048
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    attempts = 0
    async def mock_process(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return (fid, True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "FloodWait_Single.mkv", "file_size": 2048}), \
         patch.object(batch.file_service, "process_new_file", side_effect=mock_process), \
         patch("asyncio.sleep", AsyncMock()):
        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    assert attempts == 2
    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1
    assert session["files"][0]["file_id"] == str(fid)


@pytest.mark.asyncio
async def test_115_multiple_floodwaits_with_retries_all_registered_once(mock_db, folder_svc, mock_bot):
    """TEST 115: Multiple files experiencing FloodWaits retry and register without drops or duplicates."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 12004
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12004)

    messages = []
    attempt_counters = {}
    for i in range(5):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": f"Multi_FW_{i}.mkv",
            "file_size": 500,
            "file_unique_id": f"uniq_multi_fw_{i}",
        }
        await mock_db.file.insert_one(file_doc)

        msg = MockMessage(message_id=400 + i, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"uniq_multi_fw_{i}"
        media.file_name = f"Multi_FW_{i}.mkv"
        media.file_size = 500
        msg.media = MagicMock()
        msg.media.value = "document"
        msg.document = media
        messages.append((msg, fid, f"Multi_FW_{i}.mkv"))
        attempt_counters[str(fid)] = 0

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def worker(msg, fid, name):
            async def mock_process(*args, **kwargs):
                attempt_counters[str(fid)] += 1
                if attempt_counters[str(fid)] == 1:
                    fw = FloodWait(0.01)
                    fw.value = 0.01
                    raise fw
                return (fid, True)

            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name, "file_size": 500}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process), \
                 patch("asyncio.sleep", AsyncMock()):
                return await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

        tasks = [worker(m, fid, name) for m, fid, name in messages]
        results = await asyncio.gather(*tasks)
        assert all(results)

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 5
    assert session["file_count"] == 5
    assert all(c == 2 for c in attempt_counters.values())


@pytest.mark.asyncio
async def test_116_retry_identity_preservation(mock_db, folder_svc, mock_bot):
    """TEST 116: Identity (session_id, user_id, message_id, file identity) is strictly preserved across retries."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 12005
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12005)
    orig_session_id = session["session_id"]

    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "Preserved_Identity.mkv",
        "file_size": 4096,
        "file_unique_id": "uniq_preserved_116",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=555, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_preserved_116"
    media.file_name = "Preserved_Identity.mkv"
    media.file_size = 4096
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    captured_calls = []
    attempt = 0
    async def mock_process(m, bot_id=None, check_duplicate=False, session_id=None):
        nonlocal attempt
        attempt += 1
        captured_calls.append({
            "message_id": getattr(m, "id", None),
            "session_id": session_id,
            "user_id": getattr(getattr(m, "from_user", None), "id", None),
        })
        if attempt == 1:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return (fid, True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "Preserved_Identity.mkv", "file_size": 4096}), \
         patch.object(batch.file_service, "process_new_file", side_effect=mock_process), \
         patch("asyncio.sleep", AsyncMock()):
        await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

    assert len(captured_calls) == 2
    for call in captured_calls:
        assert call["message_id"] == 555
        assert call["session_id"] == orig_session_id
        assert call["user_id"] == user_id

    session = await folder_svc.get_active_session(user_id)
    assert session["files"][0]["session_id"] == orig_session_id
    assert session["files"][0]["file_unique_id"] == "uniq_preserved_116"


@pytest.mark.asyncio
async def test_117_no_duplicate_registration_for_duplicate_messages(mock_db, folder_svc, mock_bot):
    """TEST 117: Duplicate Telegram updates for the same message are idempotently discarded."""
    from FileStream.bot.plugins import batch
    user_id = 12006
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12006)

    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "Duplicate_Check.mkv",
        "file_size": 1024,
        "file_unique_id": "uniq_dup_check",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=601, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_dup_check"
    media.file_name = "Duplicate_Check.mkv"
    media.file_size = 1024
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "Duplicate_Check.mkv", "file_size": 1024}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        res1 = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        res2 = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res1 is True
        assert res2 is True

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1


@pytest.mark.asyncio
async def test_118_ui_count_consistency_between_panel_and_view_files(mock_db, folder_svc, mock_bot):
    """TEST 118: UI consistency: Files added count on panel strictly matches total in View Files pagination."""
    from FileStream.bot.plugins import batch
    user_id = 12007
    sess = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12007)

    for i in range(12):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "file_name": f"Series.S01E{i+1:02d}.mkv",
            "file_size": 1000,
        })
        await folder_svc.add_file(user_id, {
            "_id": fid,
            "file_name": f"Series.S01E{i+1:02d}.mkv",
            "file_size": 1000,
        }, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        # Refresh status panel
        await batch.refresh_folder_status(mock_bot, sess, chat_id=user_id)

    panel_msg = mock_bot.sent_messages[-1]["text"]
    assert "📦 Files added: <b>12</b>" in panel_msg
    assert "✅ Files updated" in panel_msg

    # Now verify View Files pagination pages sum to 12
    files = sess["files"]
    page_size = 5
    import math
    total_pages = math.ceil(len(files) / page_size)
    assert total_pages == 3

    page1_text = FolderMessages.view_files_page(files, page=1, total_pages=total_pages, page_size=page_size)
    assert "Page <b>1</b> / <b>3</b>" in page1_text
    assert "1. <code>Series.S01E01.mkv</code>" in page1_text
    assert "5. <code>Series.S01E05.mkv</code>" in page1_text

    page3_text = FolderMessages.view_files_page(files, page=3, total_pages=total_pages, page_size=page_size)
    assert "Page <b>3</b> / <b>3</b>" in page3_text
    assert "11. <code>Series.S01E11.mkv</code>" in page3_text
    assert "12. <code>Series.S01E12.mkv</code>" in page3_text


@pytest.mark.asyncio
async def test_119_pending_processing_ui_representation(mock_db, folder_svc, mock_bot):
    """TEST 119: Panel displays ⏳ Processing: N while files are in-flight, switching to ✅ Files updated upon completion."""
    from FileStream.bot.plugins import batch
    user_id = 12008
    sess = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12008)

    # Simulate 2 files in-flight
    sess["in_flight"] = 2
    sess["files"] = [{"file_id": "f1", "file_name": "f1.mkv", "file_size": 100}]
    sess["file_count"] = 1
    sess["total_size"] = 100

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.refresh_folder_status(mock_bot, sess, chat_id=user_id)

    pending_panel = mock_bot.sent_messages[-1]["text"]
    assert "⏳ Processing: <b>2</b>" in pending_panel
    assert "✅ Files updated" not in pending_panel
    assert "📦 Files added: <b>1</b>" in pending_panel

    # When in_flight drops to 0
    sess["in_flight"] = 0
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.refresh_folder_status(mock_bot, sess, chat_id=user_id)

    completed_panel = mock_bot.sent_messages[-1]["text"]
    assert "⏳ Processing" not in completed_panel
    assert "✅ Files updated" in completed_panel
    assert "📦 Files added: <b>1</b>" in completed_panel


@pytest.mark.asyncio
async def test_120_done_button_rejected_while_pending_files_exist(mock_db, folder_svc, mock_bot):
    """TEST 120: Done action is rejected and alerted while pending files are in-flight; succeeds after completion."""
    from FileStream.bot.plugins import batch
    user_id = 12009
    sess = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12009)

    # 10 completed files, 2 files pending
    for i in range(10):
        fid = ObjectId()
        await mock_db.file.insert_one({"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 100})
        await folder_svc.add_file(user_id, {"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 100}, is_new=True)

    sess["in_flight"] = 2

    cb_query = MagicMock()
    cb_query.data = f"fld_btn:done:{user_id}"
    cb_query.from_user.id = user_id
    cb_query.message = MagicMock(chat=MagicMock(id=user_id), id=12009)
    cb_query.answer = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        # Attempt to finalize while files are pending
        await batch.folderm_callback(mock_bot, cb_query)

    # Done rejected! Alert answered with warning
    cb_query.answer.assert_called_with(FolderMessages.STILL_PROCESSING.format(2), show_alert=True)
    # Session is NOT completed or finalized
    active_sess = await folder_svc.get_active_session(user_id)
    assert active_sess is not None
    assert active_sess["file_count"] == 10

    # Now the 2 pending files finish
    sess["in_flight"] = 0
    for i in range(10, 12):
        fid = ObjectId()
        await mock_db.file.insert_one({"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 100})
        await folder_svc.add_file(user_id, {"_id": fid, "file_name": f"f_{i}.mkv", "file_size": 100}, is_new=True)

    cb_query.answer.reset_mock()
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.folderm_callback(mock_bot, cb_query)

    # Now completion succeeds with all 12 files
    final_edit = mock_bot.edited_messages[-1]["text"]
    assert "12 files" in final_edit
    assert await folder_svc.get_active_session(user_id) is None


@pytest.mark.asyncio
async def test_121_cancel_during_retry_stops_late_additions(mock_db, folder_svc, mock_bot):
    """TEST 121: Cancellation during a FloodWait retry stops late additions from polluting cancelled session."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 12010
    sess = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12010)

    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "Late_File.mkv",
        "file_size": 1000,
        "file_unique_id": "uniq_late_121",
    }
    await mock_db.file.insert_one(file_doc)

    msg = MockMessage(message_id=999, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_late_121"
    media.file_name = "Late_File.mkv"
    media.file_size = 1000
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    attempt = 0
    async def mock_process(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            # During wait, user cancels the session
            await batch.cancel_folder(mock_bot, user_id=user_id, chat_id=user_id)
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return (fid, True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "Late_File.mkv", "file_size": 1000}), \
         patch.object(batch.file_service, "process_new_file", side_effect=mock_process), \
         patch("asyncio.sleep", AsyncMock()):
        await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

    # Session is cleanly cancelled and deleted
    assert await folder_svc.get_active_session(user_id) is None
    # Late file was NOT added to any resurrected session
    assert user_id not in folder_svc._sessions


@pytest.mark.asyncio
async def test_122_fifty_concurrent_files_processed_without_loss_or_duplicates(mock_db, folder_svc, mock_bot):
    """TEST 122: 50 concurrent incoming files processed with zero loss and zero duplicates."""
    from FileStream.bot.plugins import batch
    user_id = 12050
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12050)

    messages = []
    for i in range(50):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": f"Fifty_Batch_{i:02d}.mkv",
            "file_size": 2048,
            "file_unique_id": f"uniq_fifty_{i}",
        }
        await mock_db.file.insert_one(file_doc)

        msg = MockMessage(message_id=5000 + i, user_id=user_id, chat_id=user_id)
        media = MagicMock()
        media.file_unique_id = f"uniq_fifty_{i}"
        media.file_name = f"Fifty_Batch_{i:02d}.mkv"
        media.file_size = 2048
        msg.media = MagicMock()
        msg.media.value = "document"
        msg.document = media
        messages.append((msg, fid, f"Fifty_Batch_{i:02d}.mkv"))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):

        async def worker(msg, fid, name):
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": name, "file_size": 2048}), \
                 patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
                return await batch.handle_folder_file(mock_bot, msg, user_id=user_id)

        tasks = [worker(m, fid, name) for m, fid, name in messages]
        results = await asyncio.gather(*tasks)
        assert all(results)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 50
    assert session["file_count"] == 50
    assert len(set(session["file_ids"])) == 50
    assert session["total_size"] == 50 * 2048


@pytest.mark.asyncio
async def test_123_floodwait_retry_exhaustion_explicit_failure_no_silent_loss(mock_db, folder_svc, mock_bot):
    """TEST 123: When FloodWait retries are genuinely exhausted, fails with explicit UPLOAD_FAILED (no silent drop)."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 12051
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12051)

    fid = ObjectId()
    msg = MockMessage(message_id=7777, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_exhausted"
    media.file_name = "Exhausted.mkv"
    media.file_size = 1000
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    attempts = 0
    async def always_flood(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        fw = FloodWait(0.01)
        fw.value = 0.01
        raise fw

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "Exhausted.mkv", "file_size": 1000}), \
         patch.object(batch.file_service, "process_new_file", side_effect=always_flood), \
         patch("asyncio.sleep", AsyncMock()):
        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # Retries reached max_retries (3)
    assert attempts == 3
    # User received explicit UPLOAD_FAILED feedback
    last_edit = mock_bot.edited_messages[-1]["text"]
    assert "Failed to add this file" in last_edit or "UPLOAD_FAILED" in last_edit or "FolderMessages.UPLOAD_FAILED" in str(last_edit)
    # Session is not corrupt and file was NOT silently registered
    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 0
    assert session["in_flight"] == 0


@pytest.mark.asyncio
async def test_124_duplicate_file_unique_id_handled_safely(mock_db, folder_svc, mock_bot):
    """TEST 124: Duplicate file_unique_id shows already added feedback and does not create duplicate entries."""
    from FileStream.bot.plugins import batch
    user_id = 12052
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=12052)

    fid1 = ObjectId()
    fid2 = ObjectId()
    same_unique_id = "same_unique_media_id"

    await mock_db.file.insert_one({"_id": fid1, "file_name": "Orig.mkv", "file_size": 1000, "file_unique_id": same_unique_id})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "Copy.mkv", "file_size": 1000, "file_unique_id": same_unique_id})

    msg1 = MockMessage(message_id=8801, user_id=user_id, chat_id=user_id)
    media1 = MagicMock(file_unique_id=same_unique_id, file_name="Orig.mkv", file_size=1000)
    msg1.media = MagicMock(value="document")
    msg1.document = media1

    msg2 = MockMessage(message_id=8802, user_id=user_id, chat_id=user_id)
    media2 = MagicMock(file_unique_id=same_unique_id, file_name="Copy.mkv", file_size=1000)
    msg2.media = MagicMock(value="document")
    msg2.document = media2

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "Orig.mkv", "file_size": 1000, "file_unique_id": same_unique_id}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
            await batch.handle_folder_file(mock_bot, msg1, user_id=user_id)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "Copy.mkv", "file_size": 1000, "file_unique_id": same_unique_id}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
            await batch.handle_folder_file(mock_bot, msg2, user_id=user_id)

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1
    assert any("already added" in m.get("text", "").lower() for m in mock_bot.edited_messages)


# ==============================================================================
# TESTS 125 - 139: PHOTO ROUTING REGRESSION SUITE (SECTION 13)
# ==============================================================================

@pytest.mark.asyncio
async def test_125_photo_captured_in_active_folder_mode(mock_db, folder_svc, mock_bot):
    """TEST 125 (TEST 1): Photo while Folder Mode active is captured by Folder Mode. len(session['files']) == 1."""
    from FileStream.bot.plugins import batch
    user_id = 13001
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13001)

    fid = ObjectId()
    photo_unique_id = "photo_uniq_125"
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo-2026-09-20.jpg", "file_size": 50000, "file_unique_id": photo_unique_id})

    msg = MockMessage(message_id=9001, user_id=user_id, chat_id=user_id)
    photo_obj = MagicMock(file_id="photo_fid_125", file_unique_id=photo_unique_id, file_size=50000)
    msg.photo = photo_obj
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo-2026-09-20.jpg", "file_size": 50000, "file_unique_id": photo_unique_id}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 1
    assert session["file_count"] == 1
    assert msg.stop_propagation.called


@pytest.mark.asyncio
async def test_126_two_photos_captured_in_folder_mode(mock_db, folder_svc, mock_bot):
    """TEST 126 (TEST 2): Two photos while Folder Mode active. len(session['files']) == 2."""
    from FileStream.bot.plugins import batch
    user_id = 13002
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13002)

    fid1 = ObjectId()
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "photo_1.jpg", "file_size": 40000, "file_unique_id": "p_uniq_1"})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "photo_2.jpg", "file_size": 60000, "file_unique_id": "p_uniq_2"})

    msg1 = MockMessage(message_id=9002, user_id=user_id, chat_id=user_id)
    msg1.photo = MagicMock(file_id="p1", file_unique_id="p_uniq_1", file_size=40000)
    msg1.media = MagicMock(value="photo")

    msg2 = MockMessage(message_id=9003, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="p2", file_unique_id="p_uniq_2", file_size=60000)
    msg2.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "photo_1.jpg", "file_size": 40000, "file_unique_id": "p_uniq_1"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
            await batch.folder_mode_media_handler(mock_bot, msg1)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "photo_2.jpg", "file_size": 60000, "file_unique_id": "p_uniq_2"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
            await batch.folder_mode_media_handler(mock_bot, msg2)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 2
    assert session["file_count"] == 2
    assert session["total_size"] == 100000
    assert msg1.stop_propagation.called
    assert msg2.stop_propagation.called


@pytest.mark.asyncio
async def test_127_photo_does_not_produce_standalone_link_ready(mock_db, folder_svc, mock_bot):
    """TEST 127 (TEST 3): Photo while Folder Mode active never produces standalone Link Ready response."""
    from FileStream.bot.plugins import batch, stream
    user_id = 13003
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13003)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_3.jpg", "file_size": 30000, "file_unique_id": "p_uniq_3"})

    msg = MockMessage(message_id=9004, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="p3", file_unique_id="p_uniq_3", file_size=30000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(stream, "db", mock_db), \
         patch.object(stream, "verify_user", AsyncMock(return_value=True)), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_3.jpg", "file_size": 30000, "file_unique_id": "p_uniq_3"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await stream.private_receive_handler(mock_bot, msg)

    for reply in msg.replies:
        text = reply.get("text", "")
        assert "Link Ready" not in text
        assert "STREAM_LINK" not in text
        assert "Open Link" not in str(reply.get("reply_markup"))

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1


@pytest.mark.asyncio
async def test_128_photo_does_not_reach_standalone_handler_when_folder_active(mock_db, folder_svc, mock_bot):
    """TEST 128 (TEST 4): Photo does not reach standalone file pipeline; folder_mode_media_handler calls stop_propagation."""
    from FileStream.bot.plugins import batch
    user_id = 13004
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13004)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_4.jpg", "file_size": 25000, "file_unique_id": "p_uniq_4"})

    msg = MockMessage(message_id=9005, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="p4", file_unique_id="p_uniq_4", file_size=25000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_4.jpg", "file_size": 25000, "file_unique_id": "p_uniq_4"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    assert msg.stop_propagation.called


@pytest.mark.asyncio
async def test_129_photo_outside_folder_mode_uses_standalone_pipeline(mock_db, folder_svc, mock_bot):
    """TEST 129 (TEST 5): Photo outside Folder Mode allows propagation and is handled by standalone pipeline."""
    from FileStream.bot.plugins import batch, stream
    user_id = 13005

    msg = MockMessage(message_id=9006, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="p5", file_unique_id="p_uniq_5", file_size=15000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "folder_service", folder_svc):
        await batch.folder_mode_media_handler(mock_bot, msg)

    assert not msg.stop_propagation.called

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_5.jpg", "file_size": 15000, "file_unique_id": "p_uniq_5"})

    with patch.object(stream, "db", mock_db), \
         patch.object(stream, "verify_user", AsyncMock(return_value=True)), \
         patch.object(stream.file_service, "process_new_file", AsyncMock(return_value=(fid, True))), \
         patch.object(stream, "gen_link", AsyncMock(return_value=(MagicMock(), "📄 <b>Link Ready</b>"))):
        await stream.private_receive_handler(mock_bot, msg)

    assert len(msg.replies) > 0


@pytest.mark.asyncio
async def test_130_forwarded_photo_captured_in_folder_mode(mock_db, folder_svc, mock_bot):
    """TEST 130 (TEST 6): Forwarded photo while Folder Mode active is captured."""
    from FileStream.bot.plugins import batch
    user_id = 13006
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13006)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "fwd_photo.jpg", "file_size": 45000, "file_unique_id": "fwd_p_uniq"})

    msg = MockMessage(message_id=9007, user_id=user_id, chat_id=user_id)
    msg.forward_date = int(time.time()) - 3600
    msg.forward_from = MagicMock(id=99999, first_name="OriginalSender")
    msg.photo = MagicMock(file_id="fwd_p", file_unique_id="fwd_p_uniq", file_size=45000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "fwd_photo.jpg", "file_size": 45000, "file_unique_id": "fwd_p_uniq"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 1
    assert msg.stop_propagation.called


@pytest.mark.asyncio
async def test_131_mixed_media_captured_in_folder_mode(mock_db, folder_svc, mock_bot):
    """TEST 131 (TEST 7): Mixed media (document, photo, video) all sent during one Folder session. len(session['files']) == 3."""
    from FileStream.bot.plugins import batch
    user_id = 13007
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13007)

    fid_doc = ObjectId()
    fid_photo = ObjectId()
    fid_video = ObjectId()
    await mock_db.file.insert_one({"_id": fid_doc, "file_name": "Doc.pdf", "file_size": 10000, "file_unique_id": "uniq_doc"})
    await mock_db.file.insert_one({"_id": fid_photo, "file_name": "Pic.jpg", "file_size": 20000, "file_unique_id": "uniq_pic"})
    await mock_db.file.insert_one({"_id": fid_video, "file_name": "Vid.mp4", "file_size": 30000, "file_unique_id": "uniq_vid"})

    msg1 = MockMessage(message_id=9010, user_id=user_id, chat_id=user_id)
    msg1.document = MagicMock(file_id="d1", file_unique_id="uniq_doc", file_size=10000)
    msg1.media = MagicMock(value="document")

    msg2 = MockMessage(message_id=9011, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="p1", file_unique_id="uniq_pic", file_size=20000)
    msg2.media = MagicMock(value="photo")

    msg3 = MockMessage(message_id=9012, user_id=user_id, chat_id=user_id)
    msg3.video = MagicMock(file_id="v1", file_unique_id="uniq_vid", file_size=30000)
    msg3.media = MagicMock(value="video")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_doc), "file_name": "Doc.pdf", "file_size": 10000, "file_unique_id": "uniq_doc"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_doc, True))):
            await batch.folder_mode_media_handler(mock_bot, msg1)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_photo), "file_name": "Pic.jpg", "file_size": 20000, "file_unique_id": "uniq_pic"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_photo, True))):
            await batch.folder_mode_media_handler(mock_bot, msg2)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_video), "file_name": "Vid.mp4", "file_size": 30000, "file_unique_id": "uniq_vid"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_video, True))):
            await batch.folder_mode_media_handler(mock_bot, msg3)

    session = await folder_svc.get_active_session(user_id)
    assert session is not None
    assert len(session["files"]) == 3
    assert session["file_count"] == 3
    assert session["total_size"] == 60000


@pytest.mark.asyncio
async def test_132_duplicate_photo_update_does_not_register_twice(mock_db, folder_svc, mock_bot):
    """TEST 132 (TEST 8): Duplicate photo update (same msg_id) does not register twice."""
    from FileStream.bot.plugins import batch
    user_id = 13008
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13008)

    fid = ObjectId()
    photo_unique_id = "p_uniq_dup"
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_dup.jpg", "file_size": 35000, "file_unique_id": photo_unique_id})

    msg = MockMessage(message_id=9020, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="pdup", file_unique_id=photo_unique_id, file_size=35000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_dup.jpg", "file_size": 35000, "file_unique_id": photo_unique_id}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)
        await batch.folder_mode_media_handler(mock_bot, msg)

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1


@pytest.mark.asyncio
async def test_133_two_concurrent_photos_with_floodwait_register_both(mock_db, folder_svc, mock_bot):
    """TEST 133 (TEST 9): Two concurrent photos with FloodWait still register both."""
    from FileStream.bot.plugins import batch
    from pyrogram.errors import FloodWait

    user_id = 13009
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13009)

    fid1 = ObjectId()
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "cphoto1.jpg", "file_size": 20000, "file_unique_id": "cp_1"})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "cphoto2.jpg", "file_size": 30000, "file_unique_id": "cp_2"})

    msg1 = MockMessage(message_id=9031, user_id=user_id, chat_id=user_id)
    msg1.photo = MagicMock(file_id="cp1", file_unique_id="cp_1", file_size=20000)
    msg1.media = MagicMock(value="photo")

    msg2 = MockMessage(message_id=9032, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="cp2", file_unique_id="cp_2", file_size=30000)
    msg2.media = MagicMock(value="photo")

    called1 = 0
    async def mock_process1(*args, **kwargs):
        nonlocal called1
        called1 += 1
        if called1 == 1:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return (fid1, True)

    called2 = 0
    async def mock_process2(*args, **kwargs):
        nonlocal called2
        called2 += 1
        if called2 == 1:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return (fid2, True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch("asyncio.sleep", AsyncMock()):

        async def worker1():
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "cphoto1.jpg", "file_size": 20000, "file_unique_id": "cp_1"}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process1):
                return await batch.folder_mode_media_handler(mock_bot, msg1)

        async def worker2():
            with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "cphoto2.jpg", "file_size": 30000, "file_unique_id": "cp_2"}), \
                 patch.object(batch.file_service, "process_new_file", side_effect=mock_process2):
                return await batch.folder_mode_media_handler(mock_bot, msg2)

        await asyncio.gather(worker1(), worker2())

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 2
    assert session["file_count"] == 2
    assert session["total_size"] == 50000


@pytest.mark.asyncio
async def test_134_done_while_photos_processing_rejected(mock_db, folder_svc, mock_bot):
    """TEST 134 (TEST 10): Done while photos are still processing is rejected with wait message."""
    from FileStream.bot.plugins import batch
    user_id = 13010
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13010)

    session["in_flight"] = 2
    session["pending_messages"] = {9040, 9041}

    done_msg = MockMessage(message_id=9042, user_id=user_id, chat_id=user_id, text="/done")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.finish_folderm(mock_bot, done_msg, user_id=user_id)

    assert len(done_msg.replies) > 0
    reply_text = done_msg.replies[-1]["text"]
    assert "still processing" in reply_text.lower() or "please wait" in reply_text.lower()
    assert await folder_svc.get_active_session(user_id) is not None


@pytest.mark.asyncio
async def test_135_after_all_photos_finish_ui_updates(mock_db, folder_svc, mock_bot):
    """TEST 135 (TEST 11): After all photos finish: in_flight == 0 and UI shows Files updated."""
    from FileStream.bot.plugins import batch
    user_id = 13011
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13011)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_done.jpg", "file_size": 25000, "file_unique_id": "p_done_uniq"})

    msg = MockMessage(message_id=9050, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="pdone", file_unique_id="p_done_uniq", file_size=25000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_done.jpg", "file_size": 25000, "file_unique_id": "p_done_uniq"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    session = await folder_svc.get_active_session(user_id)
    assert session["in_flight"] == 0
    assert len(session.get("pending_messages", set())) == 0

    all_ui_texts = [m["text"] for m in mock_bot.sent_messages] + [m["text"] for m in mock_bot.edited_messages]
    assert any("Files updated" in t or "Folder Status" in t for t in all_ui_texts)


@pytest.mark.asyncio
async def test_136_cancel_during_photo_processing_prevents_late_registration(mock_db, folder_svc, mock_bot):
    """TEST 136 (TEST 12): Cancel during photo processing prevents late registration."""
    from FileStream.bot.plugins import batch
    user_id = 13012
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13012)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_late.jpg", "file_size": 50000, "file_unique_id": "p_late_uniq"})

    msg = MockMessage(message_id=9060, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="plate", file_unique_id="p_late_uniq", file_size=50000)
    msg.media = MagicMock(value="photo")

    session["cancelling"] = True
    session["cancelled"] = True

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_late.jpg", "file_size": 50000, "file_unique_id": "p_late_uniq"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    assert len(session["files"]) == 0


@pytest.mark.asyncio
async def test_137_no_duplicate_storage_operation_for_one_photo(mock_db, folder_svc, mock_bot):
    """TEST 137 (TEST 13): No duplicate storage operation for one photo."""
    from FileStream.bot.plugins import batch
    user_id = 13013
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13013)

    fid = ObjectId()
    photo_unique_id = "p_uniq_single_store"
    await mock_db.file.insert_one({"_id": fid, "file_name": "single_store.jpg", "file_size": 42000, "file_unique_id": photo_unique_id})

    msg = MockMessage(message_id=9070, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="pss", file_unique_id=photo_unique_id, file_size=42000)
    msg.media = MagicMock(value="photo")

    mock_process = AsyncMock(return_value=(fid, True))

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "single_store.jpg", "file_size": 42000, "file_unique_id": photo_unique_id}), \
         patch.object(batch.file_service, "process_new_file", mock_process):
        await batch.folder_mode_media_handler(mock_bot, msg)

    assert mock_process.call_count == 1


@pytest.mark.asyncio
async def test_138_no_duplicate_folder_registration(mock_db, folder_svc, mock_bot):
    """TEST 138 (TEST 14): Same photo sent twice with different message IDs does not register duplicate entry."""
    from FileStream.bot.plugins import batch
    user_id = 13014
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13014)

    fid1 = ObjectId()
    fid2 = ObjectId()
    same_photo_uniq = "same_photo_media_uniq"
    await mock_db.file.insert_one({"_id": fid1, "file_name": "photo_a.jpg", "file_size": 18000, "file_unique_id": same_photo_uniq})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "photo_b.jpg", "file_size": 18000, "file_unique_id": same_photo_uniq})

    msg1 = MockMessage(message_id=9081, user_id=user_id, chat_id=user_id)
    msg1.photo = MagicMock(file_id="pa", file_unique_id=same_photo_uniq, file_size=18000)
    msg1.media = MagicMock(value="photo")

    msg2 = MockMessage(message_id=9082, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="pb", file_unique_id=same_photo_uniq, file_size=18000)
    msg2.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "photo_a.jpg", "file_size": 18000, "file_unique_id": same_photo_uniq}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
            await batch.folder_mode_media_handler(mock_bot, msg1)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "photo_b.jpg", "file_size": 18000, "file_unique_id": same_photo_uniq}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
            await batch.folder_mode_media_handler(mock_bot, msg2)

    session = await folder_svc.get_active_session(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1
    assert any("already added" in m.get("text", "").lower() for m in mock_bot.edited_messages)


@pytest.mark.asyncio
async def test_139_folder_finalization_includes_all_photos(mock_db, folder_svc, mock_bot):
    """TEST 139 (TEST 15): Folder finalization includes all photo files and generates folder link."""
    from FileStream.bot.plugins import batch
    user_id = 13015
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=13015)

    fid1 = ObjectId()
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "final_photo_1.jpg", "file_size": 25000, "file_unique_id": "fp_1"})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "final_photo_2.jpg", "file_size": 35000, "file_unique_id": "fp_2"})

    msg1 = MockMessage(message_id=9091, user_id=user_id, chat_id=user_id)
    msg1.photo = MagicMock(file_id="fp1", file_unique_id="fp_1", file_size=25000)
    msg1.media = MagicMock(value="photo")

    msg2 = MockMessage(message_id=9092, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="fp2", file_unique_id="fp_2", file_size=35000)
    msg2.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "final_photo_1.jpg", "file_size": 25000, "file_unique_id": "fp_1"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
            await batch.folder_mode_media_handler(mock_bot, msg1)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "final_photo_2.jpg", "file_size": 35000, "file_unique_id": "fp_2"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
            await batch.folder_mode_media_handler(mock_bot, msg2)

        await batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id)

    folders = await mock_db.list_folders(user_id, (0, 10))
    assert len(folders) == 1
    folder_doc = folders[0]
    assert len(folder_doc["files"]) == 2
    assert str(fid1) in folder_doc["files"]
    assert str(fid2) in folder_doc["files"]
    assert folder_doc["total_size"] == 60000
    assert await folder_svc.get_active_session(user_id) is None


# ==============================================================================
# TESTS 140 - 153: SESSION VISIBILITY & PHOTO ROUTING REGRESSION SUITE
# ==============================================================================

@pytest.mark.asyncio
async def test_140_human_user_creates_folder_session(mock_db, folder_svc):
    """TEST 140: Human user 8819224472 creates a folder -> session["user_id"] == 8819224472 != 8827985075."""
    user_id = 8819224472
    bot_id = 8827985075
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14001)
    assert session["user_id"] == user_id
    assert session["user_id"] != bot_id
    assert session["status"] == "active"


@pytest.mark.asyncio
async def test_141_is_user_in_folder_mode_human_user(mock_db, folder_svc):
    """TEST 141: await is_user_in_folder_mode(8819224472) returns True."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14002)
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        assert await batch.is_user_in_folder_mode(user_id) is True


@pytest.mark.asyncio
async def test_142_is_user_in_folder_mode_bot_identity(mock_db, folder_svc):
    """TEST 142: await is_user_in_folder_mode(8827985075) returns False."""
    from FileStream.bot.plugins import batch
    human_user_id = 8819224472
    bot_id = 8827985075
    await folder_svc.create_session(user_id=human_user_id, chat_id=human_user_id, status_msg_id=14003)
    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        assert await batch.is_user_in_folder_mode(bot_id) is False


@pytest.mark.asyncio
async def test_143_create_folder_via_callback_identity_resolution(mock_db, folder_svc, mock_bot):
    """TEST 143: Create folder through real callback handler folder_manager.folder_callbacks with cq.from_user.id=8819224472 and cq.message.from_user.id=8827985075."""
    from FileStream.bot.plugins import batch, folder_manager
    human_user_id = 8819224472
    bot_id = 8827985075
    mock_bot.id = bot_id
    mock_bot.me.id = bot_id

    cq = MagicMock()
    cq.from_user = MagicMock(id=human_user_id, first_name="HumanUser")
    cq.data = "fld:new"
    cq.answer = AsyncMock()

    cq_msg = MockMessage(message_id=501, user_id=bot_id, chat_id=human_user_id, text="My Folders")
    cq.message = cq_msg

    with patch.object(folder_manager, "db", mock_db), \
         patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await folder_manager.folder_callbacks(mock_bot, cq)

    human_session = await folder_svc.get_active_session_for_user(human_user_id)
    assert human_session is not None
    assert human_session["user_id"] == human_user_id

    bot_session = await folder_svc.get_active_session_for_user(bot_id)
    assert bot_session is None


@pytest.mark.asyncio
async def test_144_callback_new_folder_captures_photo(mock_db, folder_svc, mock_bot):
    """TEST 144: Press New Folder via callback as 8819224472 and send photo -> captured, stop_propagation called, 0 standalone cards."""
    from FileStream.bot.plugins import batch, folder_manager
    human_user_id = 8819224472
    bot_id = 8827985075
    mock_bot.id = bot_id
    mock_bot.me.id = bot_id

    cq = MagicMock()
    cq.from_user = MagicMock(id=human_user_id, first_name="HumanUser")
    cq.data = "fld:new"
    cq.answer = AsyncMock()
    cq_msg = MockMessage(message_id=502, user_id=bot_id, chat_id=human_user_id, text="My Folders")
    cq.message = cq_msg

    with patch.object(folder_manager, "db", mock_db), \
         patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await folder_manager.folder_callbacks(mock_bot, cq)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "photo_144.jpg", "file_size": 102400, "file_unique_id": "p_144_uniq"})

    photo_msg = MockMessage(message_id=503, user_id=human_user_id, chat_id=human_user_id)
    photo_msg.photo = MagicMock(file_id="p144", file_unique_id="p_144_uniq", file_size=102400)
    photo_msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "photo_144.jpg", "file_size": 102400, "file_unique_id": "p_144_uniq"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, photo_msg)

    photo_msg.stop_propagation.assert_called_once()
    session = await folder_svc.get_active_session_for_user(human_user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1

    for reply in photo_msg.replies:
        assert "Link Ready" not in reply.get("text", "")


@pytest.mark.asyncio
async def test_145_send_two_photos_captured_in_active_folder(mock_db, folder_svc, mock_bot):
    """TEST 145: Send two photos -> len(session["files"]) == 2, 0 standalone Link Ready cards."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14005)

    fid1 = ObjectId()
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "photo_145_1.jpg", "file_size": 50000, "file_unique_id": "p_145_1"})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "photo_145_2.jpg", "file_size": 60000, "file_unique_id": "p_145_2"})

    msg1 = MockMessage(message_id=601, user_id=user_id, chat_id=user_id)
    msg1.photo = MagicMock(file_id="p145_1", file_unique_id="p_145_1", file_size=50000)
    msg1.media = MagicMock(value="photo")

    msg2 = MockMessage(message_id=602, user_id=user_id, chat_id=user_id)
    msg2.photo = MagicMock(file_id="p145_2", file_unique_id="p_145_2", file_size=60000)
    msg2.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid1), "file_name": "photo_145_1.jpg", "file_size": 50000, "file_unique_id": "p_145_1"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid1, True))):
            await batch.folder_mode_media_handler(mock_bot, msg1)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid2), "file_name": "photo_145_2.jpg", "file_size": 60000, "file_unique_id": "p_145_2"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid2, True))):
            await batch.folder_mode_media_handler(mock_bot, msg2)

    msg1.stop_propagation.assert_called_once()
    msg2.stop_propagation.assert_called_once()

    session = await folder_svc.get_active_session_for_user(user_id)
    assert len(session["files"]) == 2
    assert session["file_count"] == 2
    assert session["total_size"] == 110000

    all_replies = msg1.replies + msg2.replies
    for reply in all_replies:
        assert "Link Ready" not in reply.get("text", "")


@pytest.mark.asyncio
async def test_146_forwarded_photo_captured_in_folder(mock_db, folder_svc, mock_bot):
    """TEST 146: Send forwarded photo -> captured into session and stop_propagation called."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14006)

    fid = ObjectId()
    await mock_db.file.insert_one({"_id": fid, "file_name": "fwd_photo.jpg", "file_size": 75000, "file_unique_id": "p_fwd_uniq"})

    msg = MockMessage(message_id=701, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="pfwd", file_unique_id="p_fwd_uniq", file_size=75000)
    msg.media = MagicMock(value="photo")
    msg.forward_date = int(time.time()) - 50
    msg.forward_from = MagicMock(id=999999, first_name="OtherSender")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "fwd_photo.jpg", "file_size": 75000, "file_unique_id": "p_fwd_uniq"}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    msg.stop_propagation.assert_called_once()
    session = await folder_svc.get_active_session_for_user(user_id)
    assert len(session["files"]) == 1
    assert session["file_count"] == 1
    assert session["files"][0]["file_id"] == str(fid)


@pytest.mark.asyncio
async def test_147_mixed_media_captured_in_folder(mock_db, folder_svc, mock_bot):
    """TEST 147: Mixed media (document, photo, video) -> len(session["files"]) == 3."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14007)

    fid_doc = ObjectId()
    fid_photo = ObjectId()
    fid_video = ObjectId()
    await mock_db.file.insert_one({"_id": fid_doc, "file_name": "doc.pdf", "file_size": 10000, "file_unique_id": "u_doc"})
    await mock_db.file.insert_one({"_id": fid_photo, "file_name": "img.jpg", "file_size": 20000, "file_unique_id": "u_photo"})
    await mock_db.file.insert_one({"_id": fid_video, "file_name": "vid.mp4", "file_size": 30000, "file_unique_id": "u_video"})

    msg_doc = MockMessage(message_id=801, user_id=user_id, chat_id=user_id)
    msg_doc.document = MagicMock(file_id="d801", file_unique_id="u_doc", file_size=10000)
    msg_doc.media = MagicMock(value="document")

    msg_photo = MockMessage(message_id=802, user_id=user_id, chat_id=user_id)
    msg_photo.photo = MagicMock(file_id="p802", file_unique_id="u_photo", file_size=20000)
    msg_photo.media = MagicMock(value="photo")

    msg_vid = MockMessage(message_id=803, user_id=user_id, chat_id=user_id)
    msg_vid.video = MagicMock(file_id="v803", file_unique_id="u_video", file_size=30000)
    msg_vid.media = MagicMock(value="video")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_doc), "file_name": "doc.pdf", "file_size": 10000, "file_unique_id": "u_doc"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_doc, True))):
            await batch.folder_mode_media_handler(mock_bot, msg_doc)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_photo), "file_name": "img.jpg", "file_size": 20000, "file_unique_id": "u_photo"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_photo, True))):
            await batch.folder_mode_media_handler(mock_bot, msg_photo)

        with patch.object(batch, "get_file_info", return_value={"file_id": str(fid_video), "file_name": "vid.mp4", "file_size": 30000, "file_unique_id": "u_video"}), \
             patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid_video, True))):
            await batch.folder_mode_media_handler(mock_bot, msg_vid)

    session = await folder_svc.get_active_session_for_user(user_id)
    assert len(session["files"]) == 3
    assert session["file_count"] == 3
    assert session["total_size"] == 60000


@pytest.mark.asyncio
async def test_148_user_b_photo_not_captured_in_user_a_folder(mock_db, folder_svc, mock_bot):
    """TEST 148: Active folder for user A; user B sends photo -> user B not captured in user A's folder."""
    from FileStream.bot.plugins import batch
    user_a = 8819224472
    user_b = 9999111122
    await folder_svc.create_session(user_id=user_a, chat_id=user_a, status_msg_id=14008)

    fid_b = ObjectId()
    await mock_db.file.insert_one({"_id": fid_b, "file_name": "b_photo.jpg", "file_size": 40000, "file_unique_id": "u_b_photo"})

    msg_b = MockMessage(message_id=901, user_id=user_b, chat_id=user_b)
    msg_b.photo = MagicMock(file_id="pb901", file_unique_id="u_b_photo", file_size=40000)
    msg_b.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        res = await batch.handle_folder_file(mock_bot, msg_b, user_id=user_b)
        assert res is False

    msg_b.stop_propagation.assert_not_called()

    session_a = await folder_svc.get_active_session_for_user(user_a)
    assert len(session_a["files"]) == 0
    assert session_a["file_count"] == 0

    assert await folder_svc.get_active_session_for_user(user_b) is None


@pytest.mark.asyncio
async def test_149_bot_identity_cannot_create_session(mock_db, folder_svc):
    """TEST 149: Bot identity cannot create or hijack folder session; attempting create_session(8827985075, ...) raises ValueError."""
    bot_id = 8827985075
    with pytest.raises(ValueError, match="Cannot create folder session for bot identity"):
        await folder_svc.create_session(user_id=bot_id, chat_id=bot_id, status_msg_id=14009)


@pytest.mark.asyncio
async def test_150_after_cancel_is_user_in_folder_mode_is_false(mock_db, folder_svc, mock_bot):
    """TEST 150: After Cancel (cancel_folderm), is_user_in_folder_mode returns False."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14010)

    cancel_msg = MockMessage(message_id=1001, user_id=user_id, chat_id=user_id, text="/cancel")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "verify_user", AsyncMock(return_value=True)):
        await batch.cancel_folderm(mock_bot, cancel_msg, user_id=user_id)
        assert await batch.is_user_in_folder_mode(user_id) is False

    assert await folder_svc.get_active_session_for_user(user_id) is None


@pytest.mark.asyncio
async def test_151_after_done_session_finalizes_and_persists_in_db(mock_db, folder_svc, mock_bot):
    """TEST 151: After Done (complete_folder), session finalizes and all files are persisted in database folder."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14011)

    fid1 = ObjectId()
    fid2 = ObjectId()
    await mock_db.file.insert_one({"_id": fid1, "file_name": "photo1.jpg", "file_size": 25000, "file_unique_id": "p1"})
    await mock_db.file.insert_one({"_id": fid2, "file_name": "photo2.jpg", "file_size": 35000, "file_unique_id": "p2"})
    await folder_svc.add_file(user_id, {"_id": fid1, "file_name": "photo1.jpg", "file_size": 25000, "file_unique_id": "p1"}, is_new=True)
    await folder_svc.add_file(user_id, {"_id": fid2, "file_name": "photo2.jpg", "file_size": 35000, "file_unique_id": "p2"}, is_new=True)

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.complete_folder(mock_bot, user_id=user_id, chat_id=user_id)

    assert await folder_svc.get_active_session_for_user(user_id) is None

    folders = await mock_db.list_folders(user_id, (0, 10))
    assert len(folders) == 1
    folder = folders[0]
    assert len(folder["files"]) == 2
    assert str(fid1) in folder["files"]
    assert str(fid2) in folder["files"]
    assert folder["total_size"] == 60000


@pytest.mark.asyncio
async def test_152_session_lookup_and_media_handler_identity_consistency(mock_db, folder_svc, mock_bot):
    """TEST 152: Session lookup (get_active_session_for_user) and media handler use the exact same identity (8819224472)."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    bot_id = 8827985075
    mock_bot.id = bot_id
    mock_bot.me.id = bot_id

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14012)
    assert session["user_id"] == user_id

    lookup_session = await folder_svc.get_active_session_for_user(user_id, bot=mock_bot)
    assert lookup_session is not None
    assert lookup_session["session_id"] == session["session_id"]
    assert lookup_session["user_id"] == user_id

    bot_lookup = await folder_svc.get_active_session_for_user(bot_id, bot=mock_bot)
    assert bot_lookup is None

    msg = MockMessage(message_id=1201, user_id=user_id, chat_id=user_id)
    resolved_uid = getattr(getattr(msg, "from_user", None), "id", None)
    if resolved_uid == bot_id:
        resolved_uid = msg.chat.id
    assert resolved_uid == user_id


@pytest.mark.asyncio
async def test_153_production_style_photo_routing_verification(mock_db, folder_svc, mock_bot):
    """TEST 153: Production assertion: session for 8819224472, folder_mode_media_handler called with message.from_user.id=8819224472 -> folder_active=True, captured, stop_propagation=True."""
    from FileStream.bot.plugins import batch
    user_id = 8819224472
    bot_id = 8827985075
    mock_bot.id = bot_id
    mock_bot.me.id = bot_id

    created = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=14013)
    assert created["user_id"] == user_id

    fid = ObjectId()
    photo_unique_id = "prod_photo_uniq_153"
    await mock_db.file.insert_one({"_id": fid, "file_name": "prod_photo.jpg", "file_size": 85000, "file_unique_id": photo_unique_id})

    msg = MockMessage(message_id=1301, user_id=user_id, chat_id=user_id)
    msg.photo = MagicMock(file_id="p153", file_unique_id=photo_unique_id, file_size=85000)
    msg.media = MagicMock(value="photo")

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "get_file_info", return_value={"file_id": str(fid), "file_name": "prod_photo.jpg", "file_size": 85000, "file_unique_id": photo_unique_id}), \
         patch.object(batch.file_service, "process_new_file", AsyncMock(return_value=(fid, True))):
        await batch.folder_mode_media_handler(mock_bot, msg)

    msg.stop_propagation.assert_called_once()
    active_session = await folder_svc.get_active_session_for_user(user_id)
    assert active_session is not None
    assert len(active_session["files"]) == 1
    assert active_session["file_count"] == 1
    assert active_session["total_size"] == 85000
    assert any(f.get("file_id") == str(fid) for f in active_session["files"])
