import os
import tempfile
import unittest
from unittest.mock import patch
import subprocess


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
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["OPENAI_API_KEY"] = "test-key-123456"
        os.environ["ADMINAGENT_MODEL"] = "test-model"
        os.environ["ADMINAGENT_FORWARD_ENABLED"] = "0"
        os.environ["ADMINAGENT_DB_PATH"] = os.path.join(self._tmpdir.name, "adminagent-test.db")
        os.environ["TELEGRAM_POLL_ENABLED"] = "0"
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("ADMINAGENT_FORWARD_URL", None)
        os.environ.pop("ADMINAGENT_FORWARD_TOKEN", None)

    def tearDown(self):
        self._tmpdir.cleanup()
        for key in [
            "ADMINAGENT_FORWARD_ENABLED",
            "ADMINAGENT_FORWARD_URL",
            "ADMINAGENT_FORWARD_TOKEN",
            "ADMINAGENT_MODEL",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "ADMINAGENT_DB_PATH",
            "TELEGRAM_POLL_ENABLED",
            "TELEGRAM_BOT_TOKEN",
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

        sessions = client.get("/api/sessions").get_json()
        self.assertEqual(sessions["status"], "ok")
        self.assertEqual(len(sessions["sessions"]), 1)
        self.assertEqual(sessions["sessions"][0]["session_id"], "s1")

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
        self.assertEqual(data["openai_api_key"], "test...3456")
        self.assertEqual(data["forward_api_key"], "forw...-key")
        self.assertTrue(data["mcp"]["enabled"])

    def test_mcp_tools_lists_demo_server_tools(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/api/mcp/tools")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        tool_names = [t["name"] for t in data["tools"]]
        self.assertIn("demo.echo", tool_names)
        self.assertIn("demo.time_now", tool_names)

    def test_mcp_call_demo_echo(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/mcp/call", json={"tool": "demo.echo", "arguments": {"text": "hi"}})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["result"]["text"], "hi")

    def test_tools_config_can_disable_mcp_tool(self):
        app = self._make_app()
        client = app.test_client()

        cfg = client.post(
            "/api/config/tools",
            json={
                "shell_enabled": False,
                "mcp_enabled": True,
                "mcp_tools": {"demo.echo": False, "demo.time_now": True},
            },
        )
        self.assertEqual(cfg.status_code, 200)
        cfg_data = cfg.get_json()
        self.assertEqual(cfg_data["status"], "ok")

        blocked = client.post("/api/mcp/call", json={"tool": "demo.echo", "arguments": {"text": "x"}})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("disabled", blocked.get_json()["error"])

        allowed = client.post("/api/mcp/call", json={"tool": "demo.time_now", "arguments": {}})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["status"], "ok")

    @patch("app.subprocess.run")
    @patch("app.requests.post")
    def test_chat_executes_embedded_shell_tag(self, mock_post, mock_subprocess_run):
        os.environ["ADMINAGENT_SHELL_ENABLED"] = "1"
        mock_subprocess_run.return_value = subprocess.CompletedProcess(
            args=["echo", "ok"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "Let me check.\n<tool:shell>echo ok</tool:shell>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "It worked. Shell returned ok."}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-shell", "message": "did it work"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response"], "It worked. Shell returned ok.")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_subprocess_run.call_count, 1)

    @patch("app.requests.post")
    def test_session_history_persists_across_app_instances(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "Persistent reply"}}]},
        )

        app1 = self._make_app()
        client1 = app1.test_client()
        resp = client1.post("/api/chat", json={"session_id": "persist-1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)

        app2 = self._make_app()
        client2 = app2.test_client()
        hist = client2.get("/api/sessions/persist-1").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual([m["role"] for m in hist["history"]], ["user", "assistant"])
        self.assertEqual(hist["history"][1]["content"], "Persistent reply")

    @patch("app.requests.post")
    def test_can_delete_session(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "Delete me"}}]},
        )
        app = self._make_app()
        client = app.test_client()

        resp = client.post("/api/chat", json={"session_id": "s-delete", "message": "hello"})
        self.assertEqual(resp.status_code, 200)

        before = client.get("/api/sessions/s-delete").get_json()
        self.assertEqual(len(before["history"]), 2)

        deleted = client.delete("/api/sessions/s-delete")
        self.assertEqual(deleted.status_code, 200)
        deleted_payload = deleted.get_json()
        self.assertEqual(deleted_payload["status"], "ok")
        self.assertGreaterEqual(deleted_payload["deleted_messages"], 1)

        after = client.get("/api/sessions/s-delete").get_json()
        self.assertEqual(after["status"], "ok")
        self.assertEqual(after["history"], [])


if __name__ == "__main__":
    unittest.main()
