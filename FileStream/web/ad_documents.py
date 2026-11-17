"""Public banner documents only. No application/database/session dependencies."""

import hashlib
import html
import json
import re
from urllib.parse import urlsplit

# This inventory supplements the reserved fs_/filestream_ families. Values are
# never inspected, interpolated into HTML, logged, or used as authentication.
SENSITIVE_COOKIE_NAMES = (
    "fs_page_session",
    "__Host-fs_page_session",
    "page_token",
    "pageToken",
    "stream_token",
    "download_token",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "auth_token",
    "session",
    "sessionid",
    "session_id",
    "csrf_token",
    "csrftoken",
    "CF_Authorization",
    "cf_clearance",
    "__cf_bm",
)


def sensitive_cookie_pattern(extra_names=()):
    names = sorted(set(SENSITIVE_COOKIE_NAMES) | set(extra_names))
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n) for n in names):
        raise ValueError("Invalid protected cookie name")
    reserved = r"(?:__Host-(?:Http-)?|__Secure-)?(?:fs_|filestream_)[^=;\s]*"
    return (
        r"(^|;\s*)(?:"
        + reserved
        + "|"
        + "|".join(re.escape(n) for n in names)
        + r")\s*="
    )


def https_origin(value):
    value = str(value or "")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value)
        ):
            return ""
        port = parsed.port
        if port == 0:
            return ""
        host = parsed.hostname.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            return ""
        return (
            "https://"
            + host.lower()
            + (":" + str(port) if port and port != 443 else "")
        )
    except (ValueError, UnicodeError):
        return ""


def banner_options(slot):
    key = str(slot.get("key", ""))
    width, height = slot.get("width"), slot.get("height")
    url = str(slot.get("invoke_url", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise ValueError("Invalid banner key")
    if any(type(n) is not int or not 0 < n <= 4096 for n in (width, height)):
        raise ValueError("Invalid banner dimensions")
    parsed = urlsplit(url)
    if (
        not https_origin("https://" + parsed.netloc)
        or parsed.scheme != "https"
        or parsed.fragment
        or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url)
    ):
        raise ValueError("Invalid banner invocation URL")
    return dict(key=key, width=width, height=height, invoke_url=url)


def banner_id(slot):
    options = banner_options(slot)
    return hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def document_url(origin, slot):
    origin = https_origin(origin)
    if not origin:
        return ""
    return origin + "/banner/" + banner_id(slot) + ".html"


def render_document(slot, parent_origins):
    options = banner_options(slot)
    parents = sorted({https_origin(p) for p in parent_origins})
    if not parents or "" in parents:
        raise ValueError("Explicit HTTPS parent origins required")
    setup = json.dumps(
        dict(
            key=options["key"],
            format="iframe",
            width=options["width"],
            height=options["height"],
            params={},
        )
    ).replace("<", "\\u003c")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="strict-origin-when-cross-origin">'
        "<style>html,body{margin:0;padding:0}iframe{display:block}</style>"
        "<title>Advertisement</title></head><body><script>var atOptions="
        + setup
        + ";(()=>{"
        "const allowedParents=" + json.dumps(parents) + ";"
        "let target;try{target=new URL(document.referrer).origin}catch(e){};"
        "const report=state=>{if(allowedParents.includes(target))parent.postMessage({type:'filestream-ad',state},target)};"
        "const hasContent=()=>{if(document.querySelector('iframe,img,video,object,canvas'))return true;"
        "const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let t;"
        "while(t=walker.nextNode()){if(t.textContent.trim()&&!t.parentElement.closest('script,style,template,noscript'))return true}return false};"
        "new MutationObserver(()=>{if(hasContent())report('content')}).observe(document.documentElement,{childList:true,subtree:true});"
        "window.__filestreamAdMonitor={loaded:()=>setTimeout(()=>report(hasContent()?'content':'empty'),60000),error:()=>report('error')};"
        '})();</script><script src="'
        + html.escape(options["invoke_url"], quote=True)
        + '" '
        'onload="window.__filestreamAdMonitor.loaded()" '
        'onerror="window.__filestreamAdMonitor.error()"></script></body></html>'
    )


def error_document(parent_origins):
    return (
        "<!doctype html><title>Advertisement unavailable</title><script>"
        "const allowed=" + json.dumps(sorted(parent_origins)) + ";"
        "let target;try{target=new URL(document.referrer).origin}catch(e){};"
        "if(allowed.includes(target))parent.postMessage({type:'filestream-ad',state:'error'},target);"
        "</script>"
    )
