"""Small HTTP health endpoint required when hosting the polling bot as a Render Web Service."""
from aiohttp import web


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    return app
