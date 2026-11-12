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


def web_server():
    from .stream_routes import routes
    from FileStream.core.resilience.load_shedding import create_security_middleware
    # 100MB max upload/download by default (override as needed)
    web_app = web.Application(client_max_size=100 * 1024 * 1024, middlewares=[workload_middleware, create_security_middleware()])
    web_app.add_routes(routes)

    # -----------------[ STATIC FILES SETUP ]----------------- #
    # Resolve static folder path dynamically (FileStream/static)
    # This ensures it works regardless of CWD (Docker, local, etc.)
    current_dir = os.path.dirname(os.path.abspath(__file__))  # FileStream/server
    package_dir = os.path.dirname(current_dir)                # FileStream
    static_path = os.path.join(package_dir, 'web', 'static')
    if not os.path.isdir(static_path):
        static_path = os.path.join(package_dir, 'static')
    
    if os.path.isdir(static_path):
        web_app.router.add_static('/static/', path=static_path, name='static', follow_symlinks=True)
    else:
        logger.warning("Static folder not found at: %s. Local CSS/JS will not work.", static_path)
        print(f"[WARNING] Static folder not found at: {static_path}. Local CSS/JS will not work.", flush=True)
        
    return web_app