import os
import tempfile
import unittest
from unittest.mock import patch
import subprocess


class _MockResp:
    def __init__(self, status_code=200, data=None, text="", headers=None, url=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text if text else ("x" if data is not None else "")
        self.ok = status_code < 400
        self.headers = headers or {}
        self.url = url

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class SimpleAgentTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["OPENAI_API_KEY"] = "test-key-123456"
        os.environ["SIMPLEAGENT_MODEL"] = "test-model"
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "0"
        os.environ["SIMPLEAGENT_DB_PATH"] = os.path.join(self._tmpdir.name, "simpleagent-test.db")
        os.environ["TELEGRAM_POLL_ENABLED"] = "0"
        os.environ["SIMPLEAGENT_MCP_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_MCP_DISABLED_TOOLS"] = ""
        os.environ["SIMPLEAGENT_WEB_ENABLED"] = "1"
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("SIMPLEAGENT_FORWARD_URL", None)
        os.environ.pop("SIMPLEAGENT_FORWARD_TOKEN", None)

    def tearDown(self):
        self._tmpdir.cleanup()
        for key in [
            "SIMPLEAGENT_FORWARD_ENABLED",
            "SIMPLEAGENT_FORWARD_URL",
            "SIMPLEAGENT_FORWARD_TOKEN",
            "SIMPLEAGENT_MODEL",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "SIMPLEAGENT_DB_PATH",
            "TELEGRAM_POLL_ENABLED",
            "TELEGRAM_BOT_TOKEN",
            "SIMPLEAGENT_MCP_ENABLED",
            "SIMPLEAGENT_MCP_DISABLED_TOOLS",
            "SIMPLEAGENT_WEB_ENABLED",
            "SIMPLEAGENT_PUBLIC_BASE_URL",
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
        self.assertIn("SimpleAgent Chat", body)
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
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
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
        os.environ["SIMPLEAGENT_FORWARD_TOKEN"] = "forward-secret-key"
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

    def test_tools_config_can_toggle_web(self):
        app = self._make_app()
        client = app.test_client()
        cfg = client.post("/api/config/tools", json={"web_enabled": False})
        self.assertEqual(cfg.status_code, 200)
        cfg_data = cfg.get_json()
        self.assertEqual(cfg_data["status"], "ok")
        self.assertFalse(cfg_data["web_enabled"])

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_chat_executes_embedded_web_search_tag(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>latest docker compose docs</tool:web_search>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "I found relevant links."}}]}),
        ]
        mock_get.return_value = _MockResp(
            text=(
                '<html><body>'
                '<a class="result__a" href="https://docs.docker.com/compose/">Docker Compose documentation</a>'
                "</body></html>"
            ),
            headers={"Content-Type": "text/html"},
            url="https://duckduckgo.com/html/",
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web", "message": "find compose docs"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response"], "I found relevant links.")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_get.call_count, 1)

    @patch("app.requests.post")
    def test_chat_web_fetch_blocks_localhost(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "<tool:web_fetch>http://localhost:3000</tool:web_fetch>"}}]},
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-block", "message": "fetch local"})
        self.assertEqual(resp.status_code, 502)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("blocked for local or private hosts", payload["error"])

    @patch("app.requests.post")
    def test_chat_web_fetch_blocks_private_ip(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "<tool:web_fetch>http://10.0.0.8/status</tool:web_fetch>"}}]},
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-private", "message": "fetch private"})
        self.assertEqual(resp.status_code, 502)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("blocked for local or private hosts", payload["error"])

    @patch("app.requests.post")
    def test_chat_web_fetch_rejects_non_http_scheme(self, mock_post):
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "<tool:web_fetch>ftp://example.com/file</tool:web_fetch>"}}]},
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-scheme", "message": "fetch ftp"})
        self.assertEqual(resp.status_code, 502)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("must use http or https", payload["error"])

    @patch("app.requests.post")
    def test_chat_web_tool_respects_disabled_toggle(self, mock_post):
        app = self._make_app()
        client = app.test_client()
        cfg = client.post("/api/config/tools", json={"web_enabled": False})
        self.assertEqual(cfg.status_code, 200)

        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "<tool:web_search>weather</tool:web_search>"}}]},
        )
        resp = client.post("/api/chat", json={"session_id": "s-web-off", "message": "search weather"})
        self.assertEqual(resp.status_code, 502)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("disabled by configuration", payload["error"])

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_web_search_normalizes_duckduckgo_redirect_links(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>docker docs</tool:web_search>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Done"}}]}),
        ]
        mock_get.return_value = _MockResp(
            text=(
                '<html><body>'
                '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fdocs.docker.com%2Fcompose%2F">Docs</a>'
                "</body></html>"
            ),
            headers={"Content-Type": "text/html"},
            url="https://duckduckgo.com/html/",
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-ddg", "message": "docker docs"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_web_fetch_converts_html_to_markdown(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_fetch>https://example.com/doc</tool:web_fetch>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Fetched"}}]}),
        ]
        mock_get.return_value = _MockResp(
            text=(
                "<html><head><title>Doc</title></head>"
                "<body><h1>Guide</h1><p>See <a href=\"https://example.com/x\">details</a>.</p></body></html>"
            ),
            headers={"Content-Type": "text/html"},
            url="https://example.com/doc",
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-fetch", "message": "fetch it"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.assertEqual(mock_post.call_count, 2)
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("# Guide", tool_result_payload)
        self.assertIn("[details](https://example.com/x)", tool_result_payload)

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_web_fetch_keeps_plain_text_content(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_fetch>https://example.com/raw</tool:web_fetch>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Fetched raw"}}]}),
        ]
        mock_get.return_value = _MockResp(
            text="line1\nline2",
            headers={"Content-Type": "text/plain"},
            url="https://example.com/raw",
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-web-raw", "message": "fetch raw"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("line1\\nline2", tool_result_payload)

    @patch("app.requests.post")
    def test_chat_executes_send_telegram_tool_in_telegram_session(self, mock_post):
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABCDEF"
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "send_telegram_messege(\"hello from tool\")"}}]}),
            _MockResp(data={"ok": True, "result": {"message_id": 9}}),
            _MockResp(data={"choices": [{"message": {"content": "Sent to Telegram."}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "telegram:12345", "message": "send now"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response"], "Sent to Telegram.")
        self.assertEqual(mock_post.call_count, 3)

    @patch("app.requests.post")
    def test_chat_send_telegram_tool_rejects_non_telegram_session(self, mock_post):
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABCDEF"
        mock_post.return_value = _MockResp(
            data={"choices": [{"message": {"content": "send_telegram_messege(\"hello\")"}}]},
        )
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "local-dev", "message": "send now"})
        self.assertEqual(resp.status_code, 502)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("requires a Telegram session", payload["error"])

    @patch("app.requests.post")
    def test_chat_executes_get_callback_url_tool_call(self, mock_post):
        os.environ["SIMPLEAGENT_PUBLIC_BASE_URL"] = "https://agent.example.com"
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "get_callback_url()"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Use this callback URL."}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-callback", "message": "what callback should I use?"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response"], "Use this callback URL.")
        self.assertEqual(mock_post.call_count, 2)
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("TOOL_RESULT get_callback_url", tool_result_payload)
        self.assertIn("https://agent.example.com/hooks/outward_inbox", tool_result_payload)

    @patch("app.requests.post")
    def test_system_prompt_includes_ottoauth_callback_instruction(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "ok"}}]})
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s-ottoauth", "message": "help with ottoauth signup"})
        self.assertEqual(resp.status_code, 200)
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        messages = first_payload.get("messages") or []
        self.assertTrue(messages)
        system_prompt = str(messages[0].get("content", ""))
        self.assertIn("OttoAuth signup/account creation flows", system_prompt)
        self.assertIn("always call get_callback_url()", system_prompt)

    @patch("app.requests.post")
    def test_ottoauth_signup_flow_uses_callback_tool_result(self, mock_post):
        os.environ["SIMPLEAGENT_PUBLIC_BASE_URL"] = "https://agent.example.com"
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "get_callback_url()"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Use this callback for OttoAuth signup."}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        resp = client.post(
            "/api/chat",
            json={"session_id": "s-ottoauth-flow", "message": "create an ottoauth account signup webhook"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("OttoAuth signup", payload["response"])
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        tool_result_msg = str((second_payload.get("messages") or [])[-1].get("content", ""))
        self.assertIn("TOOL_RESULT get_callback_url", tool_result_msg)
        self.assertIn("https://agent.example.com/hooks/outward_inbox", tool_result_msg)

    def test_outward_inbox_hook_creates_session_notification(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.post(
            "/hooks/outward_inbox",
            json={"source": "ottoauth", "message": "account created", "event_id": "evt-1"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session_id"], "outward:ottoauth")

        hist = client.get("/api/sessions/outward:ottoauth").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["history"]), 1)
        self.assertEqual(hist["history"][0]["role"], "user")
        self.assertIn("Outward inbox notification from ottoauth", hist["history"][0]["content"])

    @patch("app.subprocess.run")
    @patch("app.requests.post")
    def test_chat_executes_embedded_shell_tag(self, mock_post, mock_subprocess_run):
        os.environ["SIMPLEAGENT_SHELL_ENABLED"] = "1"
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
