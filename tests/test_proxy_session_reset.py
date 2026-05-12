import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gemini_translator.api import base as base_module
from gemini_translator.api.base import BaseApiHandler, get_worker_loop


class _WorkerStub:
    def __init__(self):
        self.provider_config = {"is_async": True, "base_timeout": 600}


class _FakeClientSession:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self.close_calls = 0
        self.kwargs = kwargs

    async def close(self):
        self.closed = True
        self.close_calls += 1


class _FakeTCPConnector:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class ProxySessionResetTests(unittest.TestCase):
    def setUp(self):
        self.handler = BaseApiHandler(_WorkerStub())

    def tearDown(self):
        loop = get_worker_loop()
        if not loop.is_closed():
            loop.run_until_complete(self.handler._close_thread_session_internal())
            loop.close()

        for attr in ("loop", "session", "session_proxy_signature", "session_timeout"):
            if hasattr(base_module._thread_local, attr):
                delattr(base_module._thread_local, attr)

    def test_session_uses_explicit_ssl_context_without_proxy(self):
        loop = get_worker_loop()
        ssl_context = object()

        with patch("gemini_translator.api.base.aiohttp.ClientSession", _FakeClientSession), \
             patch("gemini_translator.api.base.aiohttp.TCPConnector", _FakeTCPConnector), \
             patch("gemini_translator.api.base._create_ssl_context", return_value=ssl_context, create=True):
            self.handler.setup_client(proxy_settings={"enabled": False})
            session = loop.run_until_complete(self.handler._get_or_create_session_internal(600))

        connector = session.kwargs["connector"]
        self.assertIsInstance(connector, _FakeTCPConnector)
        self.assertIs(connector.kwargs["ssl"], ssl_context)

    def test_proxy_change_recreates_cached_session(self):
        loop = get_worker_loop()
        ssl_context = object()

        with patch("gemini_translator.api.base.aiohttp.ClientSession", _FakeClientSession), \
             patch("gemini_translator.api.base.aiohttp.TCPConnector", _FakeTCPConnector), \
             patch("gemini_translator.api.base._create_ssl_context", return_value=ssl_context, create=True), \
             patch("gemini_translator.api.base.ProxyConnector") as proxy_connector:
            proxy_connector.from_url.side_effect = lambda url, rdns=True, **kwargs: {
                "url": url,
                "rdns": rdns,
                **kwargs,
            }

            self.handler.setup_client(
                proxy_settings={
                    "enabled": True,
                    "type": "SOCKS5",
                    "host": "proxy.example",
                    "port": 1080,
                    "user": "",
                    "pass": "",
                }
            )
            first_session = loop.run_until_complete(self.handler._get_or_create_session_internal(600))

            self.handler.setup_client(proxy_settings={"enabled": False})
            second_session = loop.run_until_complete(self.handler._get_or_create_session_internal(600))

        self.assertIsNot(first_session, second_session)
        self.assertTrue(first_session.closed)
        self.assertEqual(first_session.close_calls, 1)
        self.assertIsInstance(second_session.kwargs["connector"], _FakeTCPConnector)
        self.assertIs(second_session.kwargs["connector"].kwargs["ssl"], ssl_context)
        proxy_connector.from_url.assert_called_once_with(
            "socks5://proxy.example:1080",
            rdns=True,
            ssl=ssl_context,
        )


if __name__ == "__main__":
    unittest.main()
