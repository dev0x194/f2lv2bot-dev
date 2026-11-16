import os
import logging
from aiohttp import web
from FileStream.core.resilience.workload import workload_tracker

logger = logging.getLogger(__name__)


@web.middleware
async def workload_middleware(request: web.Request, handler):
    workload_tracker.record_request()
    workload_tracker.enter_connection()
    try:
        return await handler(request)
    finally:
        workload_tracker.exit_connection()


@web.middleware
async def web_privacy_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    render_error = request.app.get("filestream_render_error")
    media_paths = (
        "/static/",
        "/stream/",
        "/file/",
        "/thumb/",
        "/thumbnails/",
        "/get-",
        "/dl/",
    )
    if (
        response.status == 404
        and response.content_type != "text/html"
        and "text/html" in request.headers.get("Accept", "")
        and not request.path.startswith(media_paths)
        and render_error
    ):
        response = web.Response(
            text=await render_error(request), status=404, content_type="text/html"
        )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    existing_csp = response.headers.get(
        "Content-Security-Policy", "base-uri 'self'; object-src 'none';"
    )
    response.headers["Content-Security-Policy"] = (
        existing_csp + " frame-ancestors 'self'; form-action 'self';"
    )
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


def web_server():
    from .stream_routes import routes
    from FileStream.core.resilience.load_shedding import create_security_middleware

    # 100MB max upload/download by default (override as needed)
    web_app = web.Application(
        client_max_size=100 * 1024 * 1024,
        middlewares=[
            web_privacy_middleware,
            workload_middleware,
            create_security_middleware(),
        ],
    )
    from .render_template import render_public_error_page, resolve_request_bot

    async def render_missing_page(request):
        return await render_public_error_page(
            "Page not found",
            "This address does not point to an available page.",
            bot=resolve_request_bot(request.host),
        )

    web_app["filestream_render_error"] = render_missing_page
    web_app.add_routes(routes)

    # -----------------[ STATIC FILES SETUP ]----------------- #
    # Resolve static folder path dynamically (FileStream/static)
    # This ensures it works regardless of CWD (Docker, local, etc.)
    current_dir = os.path.dirname(os.path.abspath(__file__))  # FileStream/server
    package_dir = os.path.dirname(current_dir)  # FileStream
    static_path = os.path.join(package_dir, "web", "static")
    if not os.path.isdir(static_path):
        static_path = os.path.join(package_dir, "static")

    if os.path.isdir(static_path):
        web_app.router.add_static(
            "/static/", path=static_path, name="static", follow_symlinks=False
        )
    else:
        logger.warning(
            "Static folder not found at: %s. Local CSS/JS will not work.", static_path
        )
        print(
            f"[WARNING] Static folder not found at: {static_path}. Local CSS/JS will not work.",
            flush=True,
        )

    return web_app
