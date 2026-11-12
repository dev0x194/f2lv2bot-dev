from __future__ import annotations

import os
import re

CATEGORIES = (
    "Movies",
    "TV-Series",
    "Music",
    "Games",
    "Software",
    "Courses",
    "Books",
    "Anime",
    "Sports",
    "Other",
)

VIDEO_EXT = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ts",
}

AUDIO_EXT = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".wav",
    ".opus",
    ".oga",
}

BOOK_EXT = {
    ".pdf",
    ".epub",
    ".mobi",
    ".azw",
    ".azw3",
    ".djvu",
    ".cbz",
    ".cbr",
}

GAME_EXT = {
    ".iso",
    ".rom",
    ".xiso",
    ".cia",
    ".nsp",
    ".xci",
}

SOFTWARE_EXT = {
    ".exe",
    ".msi",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
    ".apk",
    ".ipa",
    ".appimage",
    ".bat",
    ".sh",
    ".jar",
    ".whl",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
}

RE_TV_SERIES = re.compile(
    r"(?:"
    r"s\d{1,2}\s*e\d{1,3}|"
    r"season\s*(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)|"
    r"episode\s*(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)|"
    r"\bep\s*\d{1,3}\b|"
    r"\be\d{1,3}\b"
    r")",
    re.IGNORECASE,
)


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _has_any(text: str, words: set[str]) -> bool:
    return any(w in text for w in words)


def detect_category(file_name: str | None, mime_type: str | None = None, file_ext: str | None = None) -> str:
    """Best-effort category detection for uploaded files."""
    name = _norm(file_name)
    mime = _norm(mime_type)
    ext = _norm(file_ext)

    if not ext and name:
        ext = os.path.splitext(name)[1].lower()

    anime_words = {
        "anime",
        "ani",
        "crunchyroll",
        "subsplease",
        "horriblesubs",
        "dual audio",
        "dub",
        "sub",
    }
    anime_theme_words = {
        "opening theme",
        "ending theme",
        "opening song",
        "ending song",
        "anime op",
        "anime ed",
        "op theme",
        "ed theme",
        "anisong",
    }
    sports_words = {
        "sports",
        "sport",
        "football",
        "soccer",
        "cricket",
        "nba",
        "nfl",
        "ipl",
        "uefa",
        "champions league",
        "f1",
        "motogp",
        "wwe",
        "ufc",
        "highlights",
        "match",
    }
    course_words = {
        "course",
        "tutorial",
        "lesson",
        "lecture",
        "module",
        "bootcamp",
        "udemy",
        "coursera",
        "class",
        "masterclass",
    }
    game_words = {
        "game",
        "fitgirl",
        "repack",
        "steamrip",
        "codex",
        "skidrow",
        "gog",
        "pc game",
    }
    software_words = {
        "software",
        "setup",
        "installer",
        "portable",
        "driver",
        "plugin",
        "patch",
        "crack",
        "vst",
    }
    music_words = {
        "music",
        "song",
        "album",
        "single",
        "ost",
        "soundtrack",
        "flac",
        "mp3",
    }
    book_words = {
        "book",
        "ebook",
        "novel",
        "manga",
        "comic",
        "pdf",
        "epub",
    }
    movie_words = {
        "movie",
        "bluray",
        "blu-ray",
        "webrip",
        "web-dl",
        "hdrip",
        "camrip",
        "dvdrip",
        "x264",
        "x265",
        "1080p",
        "720p",
        "2160p",
    }

    if _has_any(name, anime_words):
        return "Anime"

    # Handles names like: "Third Season Opening Theme - ..."
    if _has_any(name, anime_theme_words):
        return "Anime"

    if RE_TV_SERIES.search(name):
        return "TV-Series"

    if _has_any(name, sports_words):
        return "Sports"

    if _has_any(name, course_words):
        return "Courses"

    if ext in GAME_EXT or _has_any(name, game_words):
        return "Games"

    if ext in SOFTWARE_EXT or _has_any(name, software_words):
        return "Software"

    if mime.startswith("audio/") or ext in AUDIO_EXT or _has_any(name, music_words):
        return "Music"

    if ext in BOOK_EXT or _has_any(name, book_words):
        return "Books"

    if _has_any(name, movie_words):
        return "Movies"

    if mime.startswith("video/") or ext in VIDEO_EXT:
        # If it's video but no clear signal for TV series/sports/anime/courses,
        # treat it as Movies by default.
        return "Movies"

    return "Other"
