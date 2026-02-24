import os
import unittest
from unittest.mock import patch


class _MockResp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text if text else ("x" if data is not None else "")
        self.ok = status_code < 400

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class AdminAgentTests(unittest.TestCase):
    def setUp(self):
        os.environ["ADMINAGENT_LLM_URL"] = "http://llm.test/v1/chat/completions"
        os.environ["ADMINAGENT_LLM_API_KEY"] = "test-key-123456"
        os.environ["ADMINAGENT_MODEL"] = "test-model"
        os.environ["ADMINAGENT_FORWARD_ENABLED"] = "0"
        os.environ.pop("ADMINAGENT_FORWARD_URL", None)
        os.environ.pop("ADMINAGENT_FORWARD_TOKEN", None)

    def tearDown(self):
        for key in [
            "ADMINAGENT_FORWARD_ENABLED",
            "ADMINAGENT_FORWARD_URL",
            "ADMINAGENT_FORWARD_TOKEN",
            "ADMINAGENT_LLM_URL",
            "ADMINAGENT_LLM_API_KEY",
            "ADMINAGENT_MODEL",
        ]:
            os.environ.pop(key, None)

    def _make_app(self):
        from app import create_app

        app = create_app()
        app.testing = True
        return app

    def test_root_serves_chat_ui(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("AdminAgent Chat", body)
        self.assertIn("/api/chat", body)

    def test_chat_requires_message(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s1", "message": ""})
        self.assertEqual(resp.status_code, 400)

    @patch("app.requests.post")
    def test_chat_generates_response_and_stores_session(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "Hello from model"}}]},
        )
        app = self._make_app()
        client = app.test_client()

        resp = client.post("/api/chat", json={"session_id": "s1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["response"], "Hello from model")
        self.assertFalse(payload["forwarded"])

        hist = client.get("/api/sessions/s1").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["history"]), 2)
        self.assertEqual(hist["history"][0]["role"], "user")
        self.assertEqual(hist["history"][1]["role"], "assistant")

    @patch("app.requests.post")
    def test_forwards_when_enabled(self, mock_post):
        os.environ["ADMINAGENT_FORWARD_ENABLED"] = "1"
        os.environ["ADMINAGENT_FORWARD_URL"] = "http://example.test/hook"
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "Hello from model"}}]}),
            _MockResp(data={"ok": True}),
        ]
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["forwarded"])
        self.assertEqual(mock_post.call_count, 2)

    def test_health_masks_api_keys(self):
        os.environ["ADMINAGENT_FORWARD_TOKEN"] = "forward-secret-key"
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["llm_api_key"], "test...3456")
        self.assertEqual(data["forward_api_key"], "forw...-key")


if __name__ == "__main__":
    unittest.main()
