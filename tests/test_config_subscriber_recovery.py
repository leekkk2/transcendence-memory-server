import asyncio
import json
from unittest.mock import AsyncMock


def test_subscriber_polling_keeps_idle_connection_alive(monkeypatch):
    from scripts import config_store as store
    received = asyncio.Event()

    class Pubsub:
        count = 0
        closed = False
        async def get_message(self, **kwargs):
            assert kwargs["timeout"] == 0.5
            self.count += 1
            await asyncio.sleep(0)
            if self.count < 3:
                return None
            return {"type": "message", "data": json.dumps({"changed_keys": ["config:test"]})}
        async def aclose(self): self.closed = True

    pub = Pubsub()
    monkeypatch.setattr(store.redis_client, "make_pubsub", AsyncMock(return_value=pub))
    async def refresh(keys):
        assert keys == ["config:test"]
        received.set()
    monkeypatch.setattr(store, "refresh", refresh)

    async def run():
        task = await store.start_config_subscriber()
        try:
            await asyncio.wait_for(received.wait(), timeout=1)
            assert not task.done()
        finally:
            await store.stop_config_subscriber()
        assert pub.closed
    asyncio.run(run())
