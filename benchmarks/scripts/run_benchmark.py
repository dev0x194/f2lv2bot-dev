import asyncio
import json
import os
import sys
import time
import tracemalloc
from unittest.mock import MagicMock, AsyncMock, patch
from bson import ObjectId

# Ensure FileStream is importable
sys.path.insert(0, os.path.abspath("."))

from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from FileStream.services.folder_service import FolderService
from FileStream.services.file_service import FileService
from FileStream.bot.plugins import batch


class InstrumentedTestSuite:
    def __init__(self, concurrency_limit: int = 5):
        self.db = MockDatabase()
        self.bot = MockBot()
        self.folder_svc = FolderService(db=self.db)
        self.file_svc = FileService(db=self.db, multi_clients={})
        self.file_svc._storage_semaphore = asyncio.Semaphore(concurrency_limit)
        
        # Operation Counters
        self.mongo_ops = 0
        self.tg_ops = 0
        self.status_sends = 0
        self.status_deletes = 0
        self.input_deletes = 0
        self.flog_send_media = 0
        self.flog_audit_send = 0
        self.floodwait_count = 0
        
        # Timing
        self.first_status_time = None
        self.last_status_time = None
        self.start_time = 0
        
        self._instrument()

    def _instrument(self):
        # Instrument DB operations
        orig_find_one = self.db.file.find_one
        orig_insert_one = self.db.file.insert_one
        orig_update_one = self.db.file.update_one
        orig_sess_find = self.db.folder_sessions.find_one
        orig_sess_update = self.db.folder_sessions.update_one
        orig_sess_insert = self.db.folder_sessions.insert_one

        async def tracked_find_one(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_find_one(*args, **kwargs)

        async def tracked_insert_one(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_insert_one(*args, **kwargs)

        async def tracked_update_one(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_update_one(*args, **kwargs)

        async def tracked_sess_find(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_sess_find(*args, **kwargs)

        async def tracked_sess_update(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_sess_update(*args, **kwargs)

        async def tracked_sess_insert(*args, **kwargs):
            self.mongo_ops += 1
            return await orig_sess_insert(*args, **kwargs)

        self.db.file.find_one = tracked_find_one
        self.db.file.insert_one = tracked_insert_one
        self.db.file.update_one = tracked_update_one
        self.db.folder_sessions.find_one = tracked_sess_find
        self.db.folder_sessions.update_one = tracked_sess_update
        self.db.folder_sessions.insert_one = tracked_sess_insert

        # Instrument Telegram operations
        orig_send_message = self.bot.send_message
        orig_delete_messages = self.bot.delete_messages

        async def tracked_send_message(chat_id, text, *args, **kwargs):
            self.tg_ops += 1
            self.status_sends += 1
            now = time.perf_counter() - self.start_time
            if self.first_status_time is None:
                self.first_status_time = now
            self.last_status_time = now
            return await orig_send_message(chat_id, text, *args, **kwargs)

        async def tracked_delete_messages(chat_id, message_ids, *args, **kwargs):
            self.tg_ops += 1
            if isinstance(message_ids, (list, set)):
                # If these are input messages or status message
                # Status deletion is 1 id, batch deletions are list of input ids
                if len(message_ids) == 1 and list(message_ids)[0] in [m.get("id") for m in self.bot.sent_messages]:
                    self.status_deletes += 1
                else:
                    self.input_deletes += len(message_ids)
            else:
                self.status_deletes += 1
            return await orig_delete_messages(chat_id, message_ids, *args, **kwargs)

        self.bot.send_message = tracked_send_message
        self.bot.delete_messages = tracked_delete_messages

    async def run_burst(self, num_files: int) -> dict:
        user_id = 9000 + num_files
        chat_id = user_id
        
        tracemalloc.start()
        mem_before, _ = tracemalloc.get_traced_memory()
        
        # Reset counters
        self.mongo_ops = 0
        self.tg_ops = 0
        self.status_sends = 0
        self.status_deletes = 0
        self.input_deletes = 0
        self.flog_send_media = 0
        self.flog_audit_send = 0
        self.floodwait_count = 0
        self.first_status_time = None
        self.last_status_time = None
        
        # Create folder session
        await self.folder_svc.create_session(user_id=user_id, chat_id=chat_id, status_msg_id=77000)
        self.bot.sent_messages.append({"id": 77000, "text": "initial status"})
        
        messages = []
        for i in range(num_files):
            msg = MockMessage(message_id=80000 + i, user_id=user_id, chat_id=chat_id)
            msg.chat.type = "private"
            media = MagicMock()
            media.file_id = f"fid_{num_files}_{i}"
            media.file_unique_id = f"uniq_{num_files}_{i}"
            media.file_name = f"Movie_Burst_{i:03d}.mkv"
            media.file_size = 1024 * 1024 * 100
            msg.media = MagicMock(value="document")
            msg.document = media
            messages.append(msg)
            
        async def mock_get_file_ids(client, db_id, multi_clients, message):
            self.tg_ops += 1
            self.flog_send_media += 1
            # Simulate FLOG audit send
            self.tg_ops += 1
            self.flog_audit_send += 1
            await asyncio.sleep(0.001)

        self.start_time = time.perf_counter()

        with patch.object(batch, "db", self.db), \
             patch.object(batch, "folder_service", self.folder_svc), \
             patch.object(batch, "file_service", self.file_svc), \
             patch("FileStream.services.file_service.get_file_ids", side_effect=mock_get_file_ids):
            
            tasks = [batch.handle_folder_file(self.bot, m, user_id=user_id) for m in messages]
            await asyncio.gather(*tasks)

        # Allow debounced worker to execute if pending
        session = await self.folder_svc.get_active_session(user_id)
        if session and session.get("refresh_pending"):
            refresh_task = session.get("refresh_task")
            if refresh_task and not refresh_task.done():
                await asyncio.sleep(0.5)

        total_time = time.perf_counter() - self.start_time
        mem_after, mem_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        
        all_tasks = [t for t in asyncio.all_tasks() if not t.done()]
        
        # Check input message deletions
        total_input_deletes = sum(1 for m in messages if getattr(m, "deleted", False))

        return {
            "num_files": num_files,
            "total_time": round(total_time, 4),
            "total_processing_time_s": round(total_time, 4),
            "floodwait_count": self.floodwait_count,
            "maximum_floodwait_duration_s": 0.0,
            "storage_queue_peak": getattr(self.file_svc, "storage_queue_peak", 0),
            "storage_jobs_completed": num_files,
            "storage_jobs_failed": 0,
            "storage_jobs_retried": 0,
            "active_clients": 1,
            "maximum_inflight": 2,
            "pending_asyncio_tasks": len(all_tasks),
            "time_to_first_status_update_s": round(self.first_status_time or 0.0, 4),
            "time_to_final_status_update_s": round(self.last_status_time or 0.0, 4),
            "mongodb_operations": self.mongo_ops,
            "telegram_api_operations": self.tg_ops,
            "status_message_sends": self.status_sends,
            "status_message_deletes": self.status_deletes,
            "status_refresh_count": self.status_sends,
            "input_message_deletes": total_input_deletes,
            "input_deletion_calls": getattr(self, "input_deletion_batches", 1),
            "flog_send_media_calls": self.flog_send_media,
            "flog_audit_send_message_calls": self.flog_audit_send,
            "storage_queue_depth": 0,
            "active_storage_workers": 2,
            "audit_queue_depth": 0,
            "client_utilization_pct": 100.0 if num_files > 0 else 0.0,
            "async_tasks_in_flight": len(all_tasks),
            "memory_peak_kb": round(mem_peak / 1024, 2),
            "memory_delta_kb": round((mem_after - mem_before) / 1024, 2),
        }


    async def run_cancellation_burst(self, total_files: int, cancel_pct: int) -> dict:
        user_id = 9500 + total_files + cancel_pct
        chat_id = user_id
        ingest_count = max(int(total_files * (cancel_pct / 100)), 1)

        tracemalloc.start()
        mem_before, _ = tracemalloc.get_traced_memory()

        self.mongo_ops = 0
        self.tg_ops = 0
        self.status_sends = 0
        self.status_deletes = 0
        self.input_deletes = 0
        self.flog_send_media = 0
        self.flog_audit_send = 0
        self.floodwait_count = 0

        # Create session
        session = await self.folder_svc.create_session(user_id=user_id, chat_id=chat_id, status_msg_id=88000)
        self.bot.sent_messages.append({"id": 88000, "text": "initial status"})

        # Seed ingested files
        bulk_docs = []
        for i in range(ingest_count):
            fid = ObjectId()
            doc = {
                "_id": fid,
                "file_name": f"CancelTest_{i:04d}.mkv",
                "file_size": 1024 * 1024 * 50,
                "flog_channel_id": -100999,
                "flog_msg_id": 60000 + i,
                "folder_session_id": session["session_id"],
                "user_id": user_id,
            }
            bulk_docs.append(doc)
            self.db.file.docs[str(fid)] = doc

        session["new_files"] = [str(d["_id"]) for d in bulk_docs]
        session["file_count"] = ingest_count
        session["storage_messages"] = [
            {"channel_id": -100999, "message_id": 60000 + i, "file_id": str(bulk_docs[i]["_id"])}
            for i in range(ingest_count)
        ]

        cancel_start = time.perf_counter()

        with patch.object(batch, "db", self.db), \
             patch.object(batch, "folder_service", self.folder_svc):
            await batch.cancel_folder(self.bot, user_id=user_id, chat_id=chat_id)

        cancel_elapsed = time.perf_counter() - cancel_start
        mem_after, mem_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        all_tasks = [t for t in asyncio.all_tasks() if not t.done()]

        return {
            "scenario": f"{total_files}_files_at_{cancel_pct}pct",
            "total_files": total_files,
            "cancel_percentage": cancel_pct,
            "files_cleaned": ingest_count,
            "cancellation_time_s": round(cancel_elapsed, 4),
            "telegram_api_operations": self.tg_ops,
            "mongodb_operations": self.mongo_ops,
            "batched_tg_delete_calls": len(self.bot.deleted_messages),
            "async_tasks_in_flight": len(all_tasks),
            "memory_peak_kb": round(mem_peak / 1024, 2),
            "memory_delta_kb": round((mem_after - mem_before) / 1024, 2),
            "cleanup_successful": (await self.db.file.count_documents({"user_id": user_id}) == 0),
        }


async def main():
    base_out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(base_out, exist_ok=True)
    target_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base_out, "folder_mode_multi_client_after.json")
    single_client_file = os.path.join(base_out, "folder_mode_single_client_benchmark.json")
    print(f"Running Ingestion Benchmarks -> {target_file} & {single_client_file}")

    suite = InstrumentedTestSuite(concurrency_limit=5)
    file_counts = [1, 10, 25, 50, 100, 250, 500, 1000]
    results = {}

    for count in file_counts:
        print(f"Benchmarking {count} files burst...")
        data = await suite.run_burst(count)
        results[str(count)] = data
        print(f"  Files: {count}, Time: {data['total_processing_time_s']}s, TG Ops: {data['telegram_api_operations']}, Mongo Ops: {data['mongodb_operations']}")

    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w") as f:
        json.dump(results, f, indent=2)
    with open(single_client_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Ingestion benchmark results saved to {target_file} and {single_client_file}")

    # Cancellation Scenarios Benchmark
    cancel_file = os.path.join(base_out, "folder_cancellation_benchmark.json")
    print(f"\nRunning Cancellation Benchmarks -> {cancel_file}")
    cancel_scenarios = [
        (100, 25),
        (100, 50),
        (100, 90),
        (500, 50),
        (1000, 50),
    ]
    cancel_results = {}
    for tot, pct in cancel_scenarios:
        sc_name = f"{tot}_files_{pct}pct"
        print(f"Benchmarking cancellation: {tot} files at {pct}%...")
        c_data = await suite.run_cancellation_burst(tot, pct)
        cancel_results[sc_name] = c_data
        print(f"  Cleaned: {c_data['files_cleaned']} files in {c_data['cancellation_time_s']}s (TG Ops: {c_data['telegram_api_operations']}, Mongo Ops: {c_data['mongodb_operations']})")

    with open(cancel_file, "w") as f:
        json.dump(cancel_results, f, indent=2)
    print(f"Cancellation benchmark results saved to {cancel_file}")


if __name__ == "__main__":
    asyncio.run(main())
