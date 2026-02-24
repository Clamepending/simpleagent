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
        os.environ["GATEWAY_HOOK_PATH"] = "inbox"
        os.environ["GATEWAY_TOKEN"] = "secret"
        os.environ["ADMINAGENT_FORWARD_ENABLED"] = "0"
        os.environ.pop("ADMINAGENT_FORWARD_URL", None)
        os.environ.pop("ADMINAGENT_FORWARD_TOKEN", None)

    def tearDown(self):
        for key in [
            "GATEWAY_HOOK_PATH",
            "GATEWAY_TOKEN",
            "ADMINAGENT_FORWARD_ENABLED",
            "ADMINAGENT_FORWARD_URL",
            "ADMINAGENT_FORWARD_TOKEN",
            "DISCORD_WEBHOOK_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        ]:
            os.environ.pop(key, None)

    def _make_app(self):
        from app import create_app

        app = create_app()
        app.testing = True
        return app

    def test_rejects_missing_auth(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/hooks/inbox", json={"task_id": "1"})
        self.assertEqual(resp.status_code, 401)

    def test_legacy_path_does_not_exist(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/hooks/videomemory-alert", json={"task_id": "1"})
        self.assertEqual(resp.status_code, 404)

    def test_custom_hook_path(self):
        os.environ["GATEWAY_HOOK_PATH"] = "videomemory-alert"
        app = self._make_app()
        client = app.test_client()
        resp = client.post(
            "/hooks/videomemory-alert",
            headers={"Authorization": "Bearer secret"},
            json={"task_id": "1"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_root_serves_chat_ui(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("AdminAgent Chat Tester", body)
        self.assertIn("/api/trigger", body)

    def test_rejects_non_object_json(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post(
            "/hooks/inbox",
            headers={"Authorization": "Bearer secret"},
            json=["not", "an", "object"],
        )
        self.assertEqual(resp.status_code, 400)

    @patch("app.requests.post")
    def test_forwards_when_enabled(self, mock_post):
        os.environ["ADMINAGENT_FORWARD_ENABLED"] = "1"
        os.environ["ADMINAGENT_FORWARD_URL"] = "http://example.test/hook"
        mock_post.return_value = _MockResp(data={"ok": True})

        app = self._make_app()
        client = app.test_client()
        resp = client.post(
            "/hooks/inbox",
            headers={"Authorization": "Bearer secret"},
            json={"io_id": "net0", "task_id": "1", "note": "Package detected", "task_description": "Watch"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(resp.get_json()["forwarded"])

    @patch("app.requests.post")
    def test_discord_action_endpoint(self, mock_post):
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/webhook"
        mock_post.return_value = _MockResp(status_code=204, data={})
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/actions/discord", json={"message": "hello", "username": "AdminAgent"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.assertEqual(mock_post.call_count, 1)

    @patch("app.requests.post")
    def test_telegram_action_endpoint(self, mock_post):
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "1234"
        mock_post.return_value = _MockResp(status_code=200, data={"ok": True})
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/actions/telegram", json={"message": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
