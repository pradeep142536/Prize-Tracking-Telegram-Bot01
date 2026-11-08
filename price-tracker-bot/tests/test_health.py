import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.health import create_health_app


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/health", "/healthz"])
async def test_health_endpoints_return_ok(path):
    client = TestClient(TestServer(create_health_app()))
    await client.start_server()
    try:
        response = await client.get(path)
        assert response.status == 200
        assert await response.json() == {"status": "ok"}
    finally:
        await client.close()
