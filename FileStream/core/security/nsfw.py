import os
import logging
import asyncio
import tempfile
import threading
from pathlib import Path

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    from nudenet import NudeDetector
except Exception:  # pragma: no cover
    NudeDetector = None

from FileStream.config import NSFW, Telegram
from FileStream.core.media.file_properties import get_file_info


NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}

_detector = None
_detector_lock = threading.Lock()


def _get_detector():
    global _detector
    if NudeDetector is None:
        logging.warning("nudenet not available; skipping NSFW scan")
        return None
    with _detector_lock:
        if _detector is None:
            _detector = NudeDetector()
        return _detector


def _has_nsfw(result) -> bool:
    if not result:
        return False
    for item in result:
        label = item.get("class")
        score = float(item.get("score") or 0)
        if label in NSFW_LABELS and score >= NSFW.THRESHOLD:
            return True
    return False


def _detect_image_sync(path: str) -> bool:
    detector = _get_detector()
    if detector is None:
        return False
    return _has_nsfw(detector.detect(path))


def _detect_video_sync(path: str) -> bool:
    if cv2 is None:
        logging.warning("opencv not available; skipping video NSFW scan")
        return False
    detector = _get_detector()
    if detector is None:
        return False
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return False

    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    step = max(int(fps * NSFW.FRAME_INTERVAL), 1)
    max_frames = max(int(NSFW.MAX_VIDEO_FRAMES), 1)

    scanned = 0
    frame_index = 0
    while scanned < max_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, frame = cap.read()
        if not ret:
            break
        if _has_nsfw(detector.detect(frame)):
            cap.release()
            return True
        scanned += 1
        frame_index += step

    cap.release()
    return False


def _media_kind(info: dict) -> str | None:
    mime = (info.get("mime_type") or "").lower()
    ext = (info.get("file_ext") or "").lower()
    if mime.startswith("image") or ext in IMAGE_EXT:
        return "image"
    if mime.startswith("video") or ext in VIDEO_EXT:
        return "video"
    return None


async def scan_message(message) -> tuple[bool, str]:
    if not NSFW.ENABLE:
        return False, "disabled"

    info = get_file_info(message)
    if not info:
        return False, "no_media"

    kind = _media_kind(info)
    if kind == "image" and not NSFW.SCAN_IMAGES:
        return False, "image_scan_disabled"
    if kind == "video" and not NSFW.SCAN_VIDEOS:
        return False, "video_scan_disabled"
    if not kind:
        return False, "unsupported"

    temp_dir = Path(NSFW.TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = info.get("file_ext") or ".bin"

    fd, temp_path = tempfile.mkstemp(prefix="nsfw_", suffix=suffix, dir=str(temp_dir))
    os.close(fd)

    try:
        downloaded = await message.download(file_name=temp_path)
        if not downloaded:
            raise RuntimeError("Download failed")

        if kind == "image":
            is_nsfw = await asyncio.to_thread(_detect_image_sync, temp_path)
        else:
            is_nsfw = await asyncio.to_thread(_detect_video_sync, temp_path)

        return is_nsfw, kind
    except Exception as exc:
        logging.warning(f"NSFW scan failed: {exc}")
        if NSFW.BLOCK_ON_ERROR:
            return True, "error"
        return False, "error"
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass
