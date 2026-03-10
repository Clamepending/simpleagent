import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


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
        os.environ["SIMPLEAGENT_BOT_CONTEXT_DIR"] = os.path.join(self._tmpdir.name, "bot-context")
        os.environ["SIMPLEAGENT_MCP_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_MCP_DISABLED_TOOLS"] = ""
        os.environ["SIMPLEAGENT_WEB_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "all"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_TELEGRAM_POLLER_ENABLED"] = "0"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "5"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S"] = "120"
        os.environ["SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER"] = "20"
        os.environ["SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER"] = "40"
        os.environ["SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS"] = "0"
        os.environ["SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS"] = "100"
        os.environ["SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS"] = "0"
        os.environ["SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS"] = "100"
        os.environ["SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP"] = "10"
        os.environ["SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN"] = "10"
        os.environ.pop("SIMPLEAGENT_FORWARD_URL", None)
        os.environ.pop("SIMPLEAGENT_FORWARD_TOKEN", None)
        os.environ.pop("SIMPLEAGENT_GATEWAY_TOOL_URL", None)
        os.environ.pop("SIMPLEAGENT_FIXED_BOT_ID", None)
        os.environ.pop("SIMPLEAGENT_FIXED_BOT_NAME", None)
        os.environ.pop("SIMPLEAGENT_FIXED_BOT_MODEL", None)
        os.environ.pop("SIMPLEAGENT_FIXED_BOT_HEARTBEAT_S", None)
        os.environ.pop("GATEWAY_TOKEN", None)
        os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)

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
            "SIMPLEAGENT_BOT_CONTEXT_DIR",
            "SIMPLEAGENT_MCP_ENABLED",
            "SIMPLEAGENT_MCP_DISABLED_TOOLS",
            "SIMPLEAGENT_WEB_ENABLED",
            "SIMPLEAGENT_SHELL_ENABLED",
            "SIMPLEAGENT_PUBLIC_BASE_URL",
            "SIMPLEAGENT_GATEWAY_TOOL_URL",
            "SIMPLEAGENT_FIXED_BOT_ID",
            "SIMPLEAGENT_FIXED_BOT_NAME",
            "SIMPLEAGENT_FIXED_BOT_MODEL",
            "SIMPLEAGENT_FIXED_BOT_HEARTBEAT_S",
            "SIMPLEAGENT_SERVICE_MODE",
            "SIMPLEAGENT_EXECUTOR_AUTO_RUN",
            "SIMPLEAGENT_DELIVERY_AUTO_RUN",
            "SIMPLEAGENT_TELEGRAM_POLLER_ENABLED",
            "SIMPLEAGENT_MAX_TOOL_PASSES",
            "SIMPLEAGENT_MAX_TURN_SECONDS",
            "SIMPLEAGENT_MAX_TOOL_CALL_SECONDS",
            "SIMPLEAGENT_MAX_OUTPUT_CHARS",
            "SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S",
            "SIMPLEAGENT_QUEUE_MAX_ATTEMPTS",
            "SIMPLEAGENT_QUEUE_RETRY_BASE_S",
            "SIMPLEAGENT_QUEUE_RETRY_CAP_S",
            "SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S",
            "SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER",
            "SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER",
            "SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS",
            "SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS",
            "SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS",
            "SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS",
            "SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP",
            "SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN",
            "GATEWAY_TOKEN",
            "OPENCLAW_GATEWAY_TOKEN",
        ]:
            os.environ.pop(key, None)

    def _make_app(self):
        from app import create_app

        app = create_app()
        app.testing = True
        return app

    def _create_bot(self, client, **overrides):
        payload = {"name": "bot-a", "model": "test-model"}
        payload.update(overrides)
        gateway_token = os.environ.get("GATEWAY_TOKEN", "").strip() or os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {gateway_token}"} if gateway_token else None
        resp = client.post("/api/bots", json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(data["status"], "ok")
            return data["bot"]

        data = resp.get_json() or {}
        if resp.status_code != 403 or "disabled" not in str(data.get("error", "")).lower():
            self.fail(f"Unexpected bot create response: {resp.status_code} {resp.get_data(as_text=True)}")

        listed = client.get("/api/bots", headers=headers)
        self.assertEqual(listed.status_code, 200, msg=listed.get_data(as_text=True))
        bots = (listed.get_json() or {}).get("bots") or []
        self.assertTrue(bots, "single-bot mode should always expose one bot")
        bot = bots[0]
        bot_id = str(bot.get("bot_id", "")).strip()
        self.assertTrue(bot_id)

        config_payload = {}
        if "name" in payload:
            config_payload["name"] = payload["name"]
        if "model" in payload:
            config_payload["model"] = payload["model"]
        if "telegram_bot_token" in payload:
            config_payload["telegram_bot_token"] = payload["telegram_bot_token"]
        if "heartbeat_interval_s" in payload:
            config_payload["heartbeat_interval_s"] = payload["heartbeat_interval_s"]

        if config_payload:
            upd = client.post(f"/api/bots/{bot_id}/config", json=config_payload, headers=headers)
            self.assertEqual(upd.status_code, 200, msg=upd.get_data(as_text=True))

        resolved = client.get(f"/api/bots/{bot_id}", headers=headers)
        self.assertEqual(resolved.status_code, 200, msg=resolved.get_data(as_text=True))
        return resolved.get_json()["bot"]

    def test_root_serves_chat_ui(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("SimpleAgent Chat", body)
        self.assertIn("/api/chat", body)
        self.assertIn("contextMeterBar", body)
        self.assertNotIn("Ready. Create a bot if none exist, then chat.", body)

    def test_events_stream_bootstrap_for_bot(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.get(f"/api/events/stream?bot_id={bot['bot_id']}&session_id=s1", buffered=False)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(str(resp.content_type).startswith("text/event-stream"))
        first_chunk = next(resp.response).decode("utf-8")
        self.assertIn("retry:", first_chunk)
        resp.close()

    def test_ops_dashboard_ui_serves(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/ops")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("SimpleAgent Ops Dashboard", body)
        self.assertIn("/api/autoscale/signals", body)
        self.assertIn("/api/queue/dead-letter/purge", body)
        self.assertIn("/api/engagement", body)

    def test_engagement_dashboard_ui_serves(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/engagement")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Engagement Graphs", body)
        self.assertIn("botChart", body)
        self.assertIn("/api/engagement", body)

    def test_health_reports_single_default_bot_and_masks_keys(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["bot_count"], 1)
        self.assertEqual(len(data["bots"]), 1)
        self.assertEqual(data["openai_api_key"], "test...3456")
        self.assertEqual(data["service_mode"], "all")

    def test_v1_health_exposes_contract_metadata(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/v1/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["runtime_contract_version"], "v1")
        self.assertIn("runtime_kind", data)
        self.assertIn("runtime_version", data)

    def test_v1_capabilities_exposes_required_fields(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/v1/capabilities")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["runtime_contract_version"], "v1")
        self.assertEqual(data["tools_protocol"], "tool-tag-v1")
        self.assertIn("features", data)
        self.assertIn("max_tool_passes", data)
        self.assertIn("runtime_kind", data)
        self.assertIn("runtime_version", data)

    @patch("app.subprocess.run")
    @patch("app.requests.post")
    def test_v1_turn_returns_tool_trace_for_shell_tool(self, mock_post, mock_subprocess_run):
        os.environ["SIMPLEAGENT_SHELL_ENABLED"] = "1"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        mock_post.side_effect = [
            _MockResp(
                data={
                    "choices": [
                        {"message": {"content": "<tool:shell>echo hello</tool:shell>"}}
                    ]
                }
            ),
            _MockResp(
                data={
                    "choices": [
                        {"message": {"content": "Done. Shell command completed."}}
                    ]
                }
            ),
        ]
        mock_subprocess_run.return_value = subprocess.CompletedProcess(
            args="echo hello",
            returncode=0,
            stdout="hello\n",
            stderr="",
        )

        resp = client.post(
            "/v1/turn",
            json={
                "request_id": "req_shell_1",
                "deployment_id": "dep_shell_1",
                "tenant": {"bot_id": bot["bot_id"], "session_id": "sess-v1-shell"},
                "user_message": "run shell and confirm",
                "model": {"provider": "openai", "model": "test-model"},
                "runtime_context": {"tool_policy": {"enabled_tools": ["shell"], "disabled_tools": []}},
                "runtime_contract_version": "v1",
            },
        )
        self.assertEqual(resp.status_code, 200, msg=resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["runtime_contract_version"], "v1")
        self.assertEqual(payload["assistant_message"], "Done. Shell command completed.")
        self.assertEqual(len(payload["tool_trace"]), 1)
        self.assertEqual(payload["tool_trace"][0]["tool"], "shell")
        self.assertTrue(payload["tool_trace"][0]["ok"])
        self.assertTrue(str(payload["tool_trace"][0]["call_id"]).startswith("tc_"))

    def test_v1_turn_rejects_incompatible_contract_version(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post(
            "/v1/turn",
            json={
                "request_id": "req_bad_contract",
                "deployment_id": "dep_bad_contract",
                "tenant": {"bot_id": bot["bot_id"], "session_id": "sess-bad-contract"},
                "user_message": "hello",
                "runtime_contract_version": "v2",
            },
        )
        self.assertEqual(resp.status_code, 400, msg=resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertIn("Unsupported runtime_contract_version", payload["error"])

    def test_livez_and_readyz_endpoints(self):
        app = self._make_app()
        client = app.test_client()

        for path in ["/livez", "/api/livez"]:
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["live"])

        for path in ["/readyz", "/api/readyz"]:
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["ready"])

    def test_metrics_endpoint_exposes_queue_metrics_shape(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/api/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("queue_metrics", data)
        qm = data["queue_metrics"]
        self.assertIn("inbound", qm)
        self.assertIn("outbound", qm)
        self.assertIn("counts", qm["inbound"])
        self.assertIn("queued_ready", qm["inbound"])
        self.assertIn("autoscale", data)

    def test_engagement_endpoint_exposes_kpis_shape(self):
        app = self._make_app()
        client = app.test_client()
        self._create_bot(client)

        resp = client.get("/api/engagement?window_days=7&engaged_event_threshold=1&limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("engagement", data)
        engagement = data["engagement"]
        self.assertIn("kpis", engagement)
        self.assertIn("bots", engagement)
        self.assertIn("active_agents_24h", engagement["kpis"])
        self.assertIn("retention_7d_pct", engagement["kpis"])
        self.assertIn("success_rate_window_pct", engagement["kpis"])
        self.assertIn("engagement_rate_window_pct", engagement["kpis"])

    def test_autoscale_signals_reports_queued_ready_and_suggestion(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER"] = "1"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        queued = client.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "autoscale-q",
                "message": "queue for autoscale signal",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        resp = client.get("/api/autoscale/signals")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        inbound = data["autoscale"]["inbound"]
        self.assertGreaterEqual(int(inbound["queued_ready"]), 1)
        self.assertGreaterEqual(int(inbound["suggested_workers"]), 1)
        hot_bots = inbound.get("hot_bots") or []
        self.assertTrue(any(str(item.get("bot_id", "")) == bot["bot_id"] for item in hot_bots))

    def test_autoscale_recommendation_observe_only_without_current_workers(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/api/autoscale/recommendation")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["recommendation"]["executor"]["action"], "observe_only")
        self.assertEqual(data["recommendation"]["delivery"]["action"], "observe_only")

    def test_autoscale_recommendation_scales_up_from_queue_pressure(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER"] = "1"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        for i in range(3):
            queued = client.post(
                "/api/chat",
                json={
                    "bot_id": bot["bot_id"],
                    "session_id": f"autoscale-r-{i}",
                    "message": "queue for recommendation",
                    "wait_for_response": False,
                },
            )
            self.assertEqual(queued.status_code, 202)

        rec = client.get("/api/autoscale/recommendation?current_executor_workers=0&current_delivery_workers=0")
        self.assertEqual(rec.status_code, 200)
        data = rec.get_json()
        executor = data["recommendation"]["executor"]
        self.assertEqual(executor["action"], "scale_up")
        self.assertGreaterEqual(int(executor["suggested_workers"]), 2)
        self.assertGreaterEqual(int(executor["recommended_workers"]), 1)

    def test_autoscale_recommendation_respects_step_down_limit(self):
        os.environ["SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN"] = "2"
        app = self._make_app()
        client = app.test_client()
        rec = client.post(
            "/api/autoscale/recommendation",
            json={"current_executor_workers": 7, "current_delivery_workers": 0},
        )
        self.assertEqual(rec.status_code, 200)
        data = rec.get_json()
        executor = data["recommendation"]["executor"]
        self.assertEqual(executor["action"], "scale_down")
        self.assertEqual(int(executor["recommended_workers"]), 5)

    def test_autoscale_recommendation_rejects_invalid_current_workers(self):
        app = self._make_app()
        client = app.test_client()
        bad = client.get("/api/autoscale/recommendation?current_executor_workers=-1")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("current_executor_workers", bad.get_json()["error"])

    def test_create_bot_returns_bot_id_and_default_context_files(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        self.assertTrue(bool(bot["bot_id"]))
        self.assertEqual(bot["name"], "bot-a")
        self.assertEqual(bot["model"], "test-model")

        ctx = client.get(f"/api/bots/{bot['bot_id']}/context")
        self.assertEqual(ctx.status_code, 200)
        files = ctx.get_json()["files"]
        self.assertIn("SOUL.md", files)
        self.assertIn("USER.md", files)
        self.assertIn("HEARTBEAT.md", files)

    def test_create_bot_rejects_duplicate_bot_id(self):
        app = self._make_app()
        client = app.test_client()
        first = client.post("/api/bots", json={"bot_id": "bot-fixed", "name": "A", "model": "test-model"})
        self.assertEqual(first.status_code, 403)
        self.assertIn("disabled", first.get_json()["error"].lower())

        second = client.post("/api/bots", json={"bot_id": "bot-fixed", "name": "B", "model": "test-model"})
        self.assertEqual(second.status_code, 403)
        self.assertIn("disabled", second.get_json()["error"].lower())

    def test_single_bot_mode_returns_same_bot_on_recreate_attempt(self):
        app = self._make_app()
        client = app.test_client()
        bot1 = self._create_bot(client, name="one")
        bot2 = self._create_bot(client, name="two")

        upd1 = client.post(f"/api/bots/{bot1['bot_id']}/config", json={"telegram_bot_token": "123456:ABCDEF"})
        self.assertEqual(upd1.status_code, 200)

        upd2 = client.post(f"/api/bots/{bot2['bot_id']}/config", json={"telegram_bot_token": "123456:ABCDEF"})
        self.assertEqual(upd2.status_code, 200)
        self.assertEqual(bot1["bot_id"], bot2["bot_id"])

    @patch("app.requests.post")
    def test_chat_requires_bot_id(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "irrelevant"}}]})
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/chat", json={"session_id": "s1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("bot_id", resp.get_json())

    @patch("app.requests.post")
    def test_fixed_bot_mode_allows_chat_without_bot_id_and_blocks_cross_bot(self, mock_post):
        os.environ["SIMPLEAGENT_FIXED_BOT_ID"] = "fixed-main"
        os.environ["SIMPLEAGENT_FIXED_BOT_NAME"] = "Fixed Main"
        os.environ["SIMPLEAGENT_FIXED_BOT_MODEL"] = "test-model"
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "fixed reply"}}]})

        app = self._make_app()
        client = app.test_client()

        ui_cfg = client.get("/api/ui/config")
        self.assertEqual(ui_cfg.status_code, 200)
        self.assertTrue(ui_cfg.get_json()["fixed_bot_mode"]["enabled"])
        self.assertEqual(ui_cfg.get_json()["fixed_bot_mode"]["bot_id"], "fixed-main")

        list_resp = client.get("/api/bots")
        self.assertEqual(list_resp.status_code, 200)
        bots = list_resp.get_json()["bots"]
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["bot_id"], "fixed-main")

        blocked_create = client.post("/api/bots", json={"bot_id": "another", "name": "Another", "model": "test-model"})
        self.assertEqual(blocked_create.status_code, 403)

        blocked_cross = client.post("/api/chat", json={"bot_id": "another", "session_id": "s-fixed", "message": "hello"})
        self.assertEqual(blocked_cross.status_code, 400)
        self.assertIn("locked", blocked_cross.get_json()["error"].lower())

        ok_chat = client.post("/api/chat", json={"session_id": "s-fixed", "message": "hello"})
        self.assertEqual(ok_chat.status_code, 200, msg=ok_chat.get_data(as_text=True))
        self.assertEqual(ok_chat.get_json()["bot_id"], "fixed-main")

    @patch("app.requests.post")
    def test_fixed_bot_mode_sessions_endpoints_do_not_require_bot_query(self, mock_post):
        os.environ["SIMPLEAGENT_FIXED_BOT_ID"] = "fixed-main"
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "fixed reply"}}]})

        app = self._make_app()
        client = app.test_client()

        chat = client.post("/api/chat", json={"session_id": "sess-fixed", "message": "hello"})
        self.assertEqual(chat.status_code, 200, msg=chat.get_data(as_text=True))

        sessions = client.get("/api/sessions")
        self.assertEqual(sessions.status_code, 200, msg=sessions.get_data(as_text=True))
        self.assertEqual(sessions.get_json()["bot_id"], "fixed-main")

        history = client.get("/api/sessions/sess-fixed")
        self.assertEqual(history.status_code, 200, msg=history.get_data(as_text=True))
        self.assertEqual(history.get_json()["bot_id"], "fixed-main")
        self.assertEqual(len(history.get_json()["history"]), 2)

    def test_chat_requires_message(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s1", "message": ""})
        self.assertEqual(resp.status_code, 400)

    @patch("app.requests.post")
    def test_chat_generates_response_and_stores_session(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "Hello from model"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["bot_id"], bot["bot_id"])
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["response"], "Hello from model")

        hist = client.get(f"/api/sessions/s1?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["history"]), 2)
        self.assertEqual(hist["history"][0]["role"], "user")
        self.assertEqual(hist["history"][1]["role"], "assistant")

        sessions = client.get(f"/api/sessions?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(sessions["status"], "ok")
        self.assertEqual(len(sessions["sessions"]), 1)
        self.assertEqual(sessions["sessions"][0]["session_id"], "s1")

    def test_chat_context_usage_endpoint_returns_estimate(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post(
            "/api/context/usage",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "ctx-meter",
                "model": "test-model",
                "draft_message": "hello context meter",
            },
        )
        self.assertEqual(resp.status_code, 200, msg=resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["estimated"])
        self.assertEqual(payload["bot_id"], bot["bot_id"])
        self.assertEqual(payload["session_id"], "ctx-meter")
        self.assertEqual(payload["model"], "test-model")
        self.assertGreater(payload["current_tokens"], 0)
        self.assertEqual(payload["max_tokens"], 128000)
        self.assertGreaterEqual(payload["remaining_tokens"], 0)
        self.assertGreaterEqual(payload["usage_ratio"], 0)
        self.assertLessEqual(payload["usage_ratio"], 1)

    @patch("app.requests.post")
    def test_chat_same_session_id_keeps_messages_in_single_bot_mode(self, mock_post):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "A-reply"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "B-reply"}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, name="A")

        r1 = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "shared", "message": "hello A"})
        r2 = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "shared", "message": "hello B"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

        history = client.get(f"/api/sessions/shared?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[1]["content"], "A-reply")
        self.assertEqual(history[3]["content"], "B-reply")

    @patch("app.requests.post")
    def test_session_history_persists_across_app_instances(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "Persistent reply"}}]})
        app1 = self._make_app()
        client1 = app1.test_client()
        bot = self._create_bot(client1)

        resp = client1.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "persist", "message": "hello"})
        self.assertEqual(resp.status_code, 200)

        app2 = self._make_app()
        client2 = app2.test_client()
        hist = client2.get(f"/api/sessions/persist?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual([m["role"] for m in hist["history"]], ["user", "assistant"])
        self.assertEqual(hist["history"][1]["content"], "Persistent reply")

    @patch("app.requests.post")
    def test_can_delete_session(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "Delete me"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-delete", "message": "hello"})
        self.assertEqual(resp.status_code, 200)

        before = client.get(f"/api/sessions/s-delete?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(len(before["history"]), 2)

        deleted = client.delete(f"/api/sessions/s-delete?bot_id={bot['bot_id']}")
        self.assertEqual(deleted.status_code, 200)
        deleted_payload = deleted.get_json()
        self.assertEqual(deleted_payload["status"], "ok")
        self.assertGreaterEqual(deleted_payload["deleted_messages"], 1)

        after = client.get(f"/api/sessions/s-delete?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(after["history"], [])

    def test_sessions_endpoints_require_bot_id(self):
        app = self._make_app()
        client = app.test_client()
        self.assertEqual(client.get("/api/sessions").status_code, 200)
        self.assertEqual(client.get("/api/sessions/x").status_code, 200)
        self.assertEqual(client.delete("/api/sessions/x").status_code, 200)

    @patch("app.requests.post")
    def test_forwards_when_enabled_and_includes_bot_id(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "Hello from model"}}]}),
            _MockResp(data={"ok": True}),
        ]

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s1", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["forwarded"])
        self.assertEqual(mock_post.call_count, 2)

        forward_json = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(forward_json["bot_id"], bot["bot_id"])
        self.assertEqual(forward_json["event"]["bot_id"], bot["bot_id"])

    @patch("app.requests.post")
    def test_chat_idempotency_key_deduplicates_execution(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "idempotent reply"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        idem = "idem-chat-1"
        r1 = client.post(
            "/api/chat",
            json={"bot_id": bot["bot_id"], "session_id": "idem-session", "message": "hello", "idempotency_key": idem},
        )
        r2 = client.post(
            "/api/chat",
            json={"bot_id": bot["bot_id"], "session_id": "idem-session", "message": "hello", "idempotency_key": idem},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.get_json()["response"], "idempotent reply")
        self.assertEqual(r2.get_json()["response"], "idempotent reply")
        self.assertEqual(mock_post.call_count, 1)

        hist = client.get(f"/api/sessions/idem-session?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["role"], "user")
        self.assertEqual(hist[1]["role"], "assistant")

    def test_mcp_tools_lists_demo_server_tools(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/api/mcp/tools")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        tool_names = [t["name"] for t in data["tools"]]
        self.assertIn("demo.echo", tool_names)
        self.assertIn("demo.time_now", tool_names)

    @patch("app.DemoMcpServer.call_tool", autospec=True)
    def test_mcp_call_attaches_bot_id_context(self, mock_call_tool):
        mock_call_tool.return_value = {"ok": True, "text": "hi"}
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/mcp/call", json={"bot_id": bot["bot_id"], "tool": "demo.echo", "arguments": {"text": "hi"}})
        self.assertEqual(resp.status_code, 200)
        call_args = mock_call_tool.call_args[0]
        forwarded_args = call_args[2]
        self.assertEqual(forwarded_args["_bot_id"], bot["bot_id"])
        self.assertEqual(forwarded_args["bot_id"], bot["bot_id"])

    def test_gateway_mode_blocks_direct_mcp_call_execution(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/mcp/call", json={"bot_id": bot["bot_id"], "tool": "demo.time_now", "arguments": {}})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("executor plane is disabled", resp.get_json()["error"])

    def test_tools_config_can_disable_mcp_tool(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        cfg = client.post(
            "/api/config/tools",
            json={
                "shell_enabled": False,
                "mcp_enabled": True,
                "mcp_tools": {"demo.echo": False, "demo.time_now": True},
            },
        )
        self.assertEqual(cfg.status_code, 200)

        blocked = client.post("/api/mcp/call", json={"bot_id": bot["bot_id"], "tool": "demo.echo", "arguments": {"text": "x"}})
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("disabled", blocked.get_json()["error"])

        allowed = client.post("/api/mcp/call", json={"bot_id": bot["bot_id"], "tool": "demo.time_now", "arguments": {}})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["status"], "ok")

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
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-web", "message": "find docs"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["response"], "I found relevant links.")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_get.call_count, 1)

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_chat_records_tool_progress_events(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>Berkeley bagels</tool:web_search>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Done."}}]}),
        ]
        mock_get.return_value = _MockResp(
            text='<html><body><a class="result__a" href="https://example.com/bagels">Bagels</a></body></html>',
            headers={"Content-Type": "text/html"},
            url="https://duckduckgo.com/html/",
        )

        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post(
            "/api/chat",
            json={"bot_id": bot["bot_id"], "session_id": "s-progress", "message": "search now"},
        )
        self.assertEqual(resp.status_code, 200)

        events_resp = client.get(f"/api/events?bot_id={bot['bot_id']}")
        self.assertEqual(events_resp.status_code, 200)
        events = events_resp.get_json().get("events") or []
        progress_events = [
            evt for evt in events
            if str(evt.get("event_type", "")).strip() == "tool_call_progress"
            and str(evt.get("session_id", "")).strip() == "s-progress"
        ]
        self.assertTrue(progress_events)
        traces = []
        for evt in progress_events:
            traces.extend(evt.get("tool_trace") or [])
        self.assertTrue(traces)
        self.assertTrue(any(str(t.get("tool", "")).strip() == "web_search" for t in traces))
        statuses = {str(t.get("status", "")).strip() for t in traces}
        self.assertIn("running", statuses)
        self.assertIn("ok", statuses)

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_web_search_decodes_duckduckgo_redirect_url(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>bagel</tool:web_search>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Done"}}]}),
        ]
        mock_get.return_value = _MockResp(
            text=(
                '<html><body>'
                '<a class="result__a" '
                'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fbagels&amp;rut=abc">Example</a>'
                "</body></html>"
            ),
            headers={"Content-Type": "text/html"},
            url="https://duckduckgo.com/html/",
        )

        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-web2", "message": "search"})
        self.assertEqual(resp.status_code, 200)
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("https://example.com/bagels", tool_result_payload)

    @patch("app.requests.get")
    @patch("app.requests.post")
    def test_web_search_falls_back_when_duckduckgo_challenge_page_is_returned(self, mock_post, mock_get):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>best food places in Berkeley</tool:web_search>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Done"}}]}),
        ]
        ddg_challenge_html = (
            "<html><body>"
            "<div class='anomaly-modal__title'>Unfortunately, bots use DuckDuckGo too.</div>"
            "<form id='challenge-form' action='//duckduckgo.com/anomaly.js'></form>"
            "</body></html>"
        )
        ddg_via_jina_markdown = (
            "1.[Best Restaurants](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fberkeley-food&rut=abc)\n"
            "2.[Another Result](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fplaces&rut=def)\n"
        )
        mock_get.side_effect = [
            _MockResp(text=ddg_challenge_html, headers={"Content-Type": "text/html"}, url="https://duckduckgo.com/html/"),
            _MockResp(text=ddg_challenge_html, headers={"Content-Type": "text/html"}, url="https://lite.duckduckgo.com/lite/"),
            _MockResp(text=ddg_via_jina_markdown, headers={"Content-Type": "text/plain"}, url="https://r.jina.ai/http://lite.duckduckgo.com/lite/"),
        ]

        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-ddg-fallback", "message": "search"})
        self.assertEqual(resp.status_code, 200)
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("duckduckgo_lite_via_jina", tool_result_payload)
        self.assertIn("https://example.com/berkeley-food", tool_result_payload)
        self.assertEqual(mock_get.call_count, 3)

    @patch("app.requests.post")
    def test_chat_web_fetch_blocks_localhost(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "<tool:web_fetch>http://localhost:3000</tool:web_fetch>"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s1", "message": "fetch"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("blocked for local or private hosts", resp.get_json()["error"])

    @patch("app.requests.post")
    def test_chat_web_fetch_blocks_private_ip(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "<tool:web_fetch>http://10.0.0.8/status</tool:web_fetch>"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s2", "message": "fetch"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("blocked for local or private hosts", resp.get_json()["error"])

    @patch("app.requests.post")
    def test_chat_web_fetch_rejects_non_http_scheme(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "<tool:web_fetch>ftp://example.com/file</tool:web_fetch>"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s3", "message": "fetch"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("must use http or https", resp.get_json()["error"])

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
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s4", "message": "fetch"})
        self.assertEqual(resp.status_code, 200)
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
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s5", "message": "fetch"})
        self.assertEqual(resp.status_code, 200)
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("line1\\nline2", tool_result_payload)

    @patch("app.requests.post")
    def test_chat_web_tool_respects_disabled_toggle(self, mock_post):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        cfg = client.post("/api/config/tools", json={"web_enabled": False})
        self.assertEqual(cfg.status_code, 200)

        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "<tool:web_search>weather</tool:web_search>"}}]})
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s6", "message": "search weather"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("disabled by configuration", resp.get_json()["error"])

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
        bot = self._create_bot(client)
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-shell", "message": "did it work"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["response"], "It worked. Shell returned ok.")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_subprocess_run.call_count, 1)

    @patch("app.requests.post")
    def test_chat_executes_send_telegram_tool_in_telegram_session(self, mock_post):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "send_telegram_messege(\"hello from tool\")"}}]}),
            _MockResp(data={"ok": True, "result": {"message_id": 9}}),
            _MockResp(data={"choices": [{"message": {"content": "Sent to Telegram."}}]}),
        ]
        resp = client.post(
            "/api/chat",
            json={"bot_id": bot["bot_id"], "session_id": "telegram:12345", "message": "send now"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["response"], "Sent to Telegram.")
        self.assertEqual(mock_post.call_count, 3)

    @patch("app.requests.post")
    def test_chat_send_telegram_tool_rejects_non_telegram_session(self, mock_post):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "send_telegram_messege(\"hello\")"}}]})
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "local-dev", "message": "send now"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("requires a Telegram session", resp.get_json()["error"])

    @patch("app.requests.post")
    def test_chat_executes_get_callback_url_tool_call(self, mock_post):
        os.environ["SIMPLEAGENT_PUBLIC_BASE_URL"] = "https://agent.example.com"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "get_callback_url()"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Use this callback URL."}}]}),
        ]
        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s-callback", "message": "what callback?"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["response"], "Use this callback URL.")
        tool_result_payload = mock_post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
        self.assertIn("TOOL_RESULT get_callback_url", tool_result_payload)
        self.assertIn("https://agent.example.com/hooks/outward_inbox", tool_result_payload)
        self.assertIn(bot["bot_id"], tool_result_payload)

    @patch("app.requests.post")
    def test_system_prompt_includes_bot_id_and_context_files(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "ok"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        client.post(f"/api/bots/{bot['bot_id']}/context/SOUL.md", json={"content": "soul text"})
        client.post(f"/api/bots/{bot['bot_id']}/context/USER.md", json={"content": "user text"})
        client.post(f"/api/bots/{bot['bot_id']}/context/HEARTBEAT.md", json={"content": "heartbeat text"})

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "ctx", "message": "hello"})
        self.assertEqual(resp.status_code, 200)
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        messages = first_payload.get("messages") or []
        self.assertTrue(messages)
        system_prompt = str(messages[0].get("content", ""))
        self.assertIn(f"BOT_ID: {bot['bot_id']}", system_prompt)
        self.assertIn("soul text", system_prompt)
        self.assertIn("user text", system_prompt)
        self.assertIn("heartbeat text", system_prompt)

    @patch("app.requests.post")
    def test_gateway_heartbeat_set_tool_updates_file_and_calls_gateway(self, mock_post):
        gateway_url = "https://gateway.example.com/tool"
        os.environ["SIMPLEAGENT_GATEWAY_TOOL_URL"] = gateway_url
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:gateway>{\"action\":\"heartbeat_set\",\"content\":\"new heartbeat\",\"interval_s\":120}</tool:gateway>"}}]}),
            _MockResp(data={"ok": True}),
            _MockResp(data={"choices": [{"message": {"content": "Heartbeat updated."}}]}),
        ]

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "hb1", "message": "update heartbeat"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["response"], "Heartbeat updated.")

        gateway_call = mock_post.call_args_list[1]
        self.assertEqual(gateway_call.args[0], gateway_url)
        self.assertEqual(gateway_call.kwargs["json"]["bot_id"], bot["bot_id"])
        self.assertEqual(gateway_call.kwargs["json"]["action"], "heartbeat_set")

        hb = client.get(f"/api/bots/{bot['bot_id']}/heartbeat").get_json()
        self.assertEqual(hb["status"], "ok")
        self.assertEqual(hb["heartbeat_interval_s"], 120)
        self.assertIn("new heartbeat", hb["heartbeat"])

    @patch("app.requests.post")
    def test_gateway_heartbeat_get_tool_returns_file(self, mock_post):
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:gateway>{\"action\":\"heartbeat_get\"}</tool:gateway>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Read heartbeat."}}]}),
        ]

        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)
        client.post(f"/api/bots/{bot['bot_id']}/context/HEARTBEAT.md", json={"content": "heartbeat-file-content"})

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "hb2", "message": "show heartbeat"})
        self.assertEqual(resp.status_code, 200)
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        tool_result = str((second_payload.get("messages") or [])[-1].get("content", ""))
        self.assertIn("TOOL_RESULT gateway", tool_result)
        self.assertIn("heartbeat-file-content", tool_result)

    def test_heartbeat_endpoint_can_update_file_and_interval(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        upd = client.post(f"/api/bots/{bot['bot_id']}/heartbeat", json={"content": "hb-new", "interval_s": 222})
        self.assertEqual(upd.status_code, 200)
        data = upd.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["heartbeat_interval_s"], 222)
        self.assertIn("hb-new", data["heartbeat"])

    def test_context_file_endpoint_rejects_invalid_file_name(self):
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        bad = client.post(f"/api/bots/{bot['bot_id']}/context/NOTES.md", json={"content": "x"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("context file", bad.get_json()["error"])

    @patch("app.DemoMcpServer.call_tool", autospec=True)
    @patch("app.requests.post")
    def test_mcp_tool_from_llm_attaches_bot_id(self, mock_post, mock_call_tool):
        mock_call_tool.return_value = {"ok": True, "text": "done"}
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:mcp name=\"demo.echo\">{\"text\":\"hi\"}</tool:mcp>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "done"}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "mcp-llm", "message": "echo hi"})
        self.assertEqual(resp.status_code, 200)
        args = mock_call_tool.call_args[0][2]
        self.assertEqual(args["_bot_id"], bot["bot_id"])
        self.assertEqual(args["bot_id"], bot["bot_id"])

    @patch("app.DemoMcpServer.call_tool", autospec=True)
    @patch("app.requests.post")
    def test_mcp_tool_from_llm_direct_tag_executes(self, mock_post, mock_call_tool):
        mock_call_tool.return_value = {"ok": True, "devices": [{"id": "cam-1"}]}
        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "<tool:demo.time_now>{}</tool:demo.time_now>"}}]}),
            _MockResp(data={"choices": [{"message": {"content": "Found one device."}}]}),
        ]
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "mcp-direct-tag", "message": "what devices do you see"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["response"], "Found one device.")
        self.assertEqual(len(payload["tool_trace"]), 1)
        self.assertEqual(payload["tool_trace"][0]["tool"], "demo.time_now")
        self.assertEqual(payload["tool_trace"][0]["arguments"], {})
        self.assertTrue(payload["tool_trace"][0]["ok"])
        self.assertIsNone(payload["tool_trace"][0]["error"])
        self.assertEqual(mock_call_tool.call_count, 1)
        self.assertEqual(mock_post.call_count, 2)

        args = mock_call_tool.call_args[0][2]
        self.assertEqual(args["_bot_id"], bot["bot_id"])
        self.assertEqual(args["bot_id"], bot["bot_id"])

    @patch("app.requests.post")
    def test_outward_inbox_hook_requires_bot_id_and_records_session(self, mock_post):
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "hook handled"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        missing = client.post("/hooks/outward_inbox", json={"source": "x", "message": "y"})
        self.assertEqual(missing.status_code, 200)

        resp = client.post(
            "/hooks/outward_inbox",
            json={"bot_id": bot["bot_id"], "source": "ottoauth", "message": "account created", "event_id": "evt-1"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session_id"], "hook:outward:ottoauth:evt-1")
        self.assertEqual(payload["response"], "hook handled")

        hist = client.get(f"/api/sessions/hook:outward:ottoauth:evt-1?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["history"]), 2)
        self.assertIn("Outward inbox notification from ottoauth", hist["history"][0]["content"])
        self.assertEqual(hist["history"][0]["role"], "user")
        self.assertEqual(hist["history"][1]["role"], "assistant")

        sessions = client.get(f"/api/sessions?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(sessions["status"], "ok")
        self.assertIn("hook:outward:ottoauth:evt-1", [s["session_id"] for s in sessions["sessions"]])

    @patch("app.requests.post")
    def test_videomemory_hook_auth_and_bot_id_requirement(self, mock_post):
        os.environ["GATEWAY_TOKEN"] = "gw-secret"
        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "video handled"}}]})
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        unauthorized = client.post("/hooks/videomemory-alert", json={"bot_id": bot["bot_id"], "io_id": "cam-1"})
        self.assertEqual(unauthorized.status_code, 401)

        missing_bot = client.post(
            "/hooks/videomemory-alert",
            json={"io_id": "cam-1"},
            headers={"Authorization": "Bearer gw-secret"},
        )
        self.assertEqual(missing_bot.status_code, 200)

        ok = client.post(
            "/hooks/videomemory-alert",
            json={"bot_id": bot["bot_id"], "io_id": "cam-1", "task_id": "t-1", "note": "motion", "event_id": "vm-1"},
            headers={"Authorization": "Bearer gw-secret"},
        )
        self.assertEqual(ok.status_code, 200)
        payload = ok.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session_id"], "hook:videomemory:cam-1:t-1:vm-1")
        self.assertEqual(payload["response"], "video handled")

        hist = client.get(
            f"/api/sessions/hook:videomemory:cam-1:t-1:vm-1?bot_id={bot['bot_id']}",
            headers={"Authorization": "Bearer gw-secret"},
        ).get_json()
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["history"]), 2)
        self.assertIn("VideoMemory alert on cam-1 task t-1: motion", hist["history"][0]["content"])

    @patch("app.requests.post")
    def test_telegram_inbound_claims_owner_then_blocks_other_user(self, mock_post):
        def post_side_effect(url, **kwargs):
            if "api.telegram.org" in url:
                return _MockResp(data={"ok": True, "result": {"message_id": 7}})
            return _MockResp(data={"choices": [{"message": {"content": "Owner reply"}}]})

        mock_post.side_effect = post_side_effect
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        first = client.post(
            "/api/telegram/inbound",
            json={
                "bot_id": bot["bot_id"],
                "message": {
                    "chat": {"id": 1111},
                    "from": {"id": 9001},
                    "message_id": 10,
                    "text": "hello",
                },
                "update_id": 55,
            },
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["status"], "ok")

        bot_after_first = client.get(f"/api/bots/{bot['bot_id']}").get_json()["bot"]
        self.assertEqual(bot_after_first["telegram_owner_user_id"], 9001)

        second = client.post(
            "/api/telegram/inbound",
            json={
                "bot_id": bot["bot_id"],
                "message": {
                    "chat": {"id": 1111},
                    "from": {"id": 42},
                    "message_id": 11,
                    "text": "intrude",
                },
                "update_id": 56,
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "forbidden")

        hist = client.get(f"/api/sessions/telegram:1111?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(len(hist["history"]), 2)
        self.assertEqual(hist["history"][0]["role"], "user")
        self.assertEqual(hist["history"][1]["role"], "assistant")

    @patch("app.requests.post")
    def test_telegram_inbound_deduplicates_same_update_id(self, mock_post):
        call_counts = {"llm": 0, "telegram": 0}

        def post_side_effect(url, **kwargs):
            if "api.telegram.org" in url:
                call_counts["telegram"] += 1
                return _MockResp(data={"ok": True, "result": {"message_id": 17}})
            call_counts["llm"] += 1
            return _MockResp(data={"choices": [{"message": {"content": "reply once"}}]})

        mock_post.side_effect = post_side_effect
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        payload = {
            "bot_id": bot["bot_id"],
            "update_id": 900,
            "message": {
                "chat": {"id": 777},
                "from": {"id": 888},
                "message_id": 4,
                "text": "hello",
            },
        }
        first = client.post("/api/telegram/inbound", json=payload)
        second = client.post("/api/telegram/inbound", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["status"], "ok")
        self.assertEqual(second.get_json()["status"], "ok")
        self.assertEqual(call_counts["llm"], 1)
        self.assertEqual(call_counts["telegram"], 1)

        hist = client.get(f"/api/sessions/telegram:777?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual(len(hist), 2)

    @patch("app.requests.post")
    def test_telegram_webhook_processes_message(self, mock_post):
        def post_side_effect(url, **kwargs):
            if "api.telegram.org" in url:
                return _MockResp(data={"ok": True, "result": {"message_id": 99}})
            return _MockResp(data={"choices": [{"message": {"content": "Webhook reply"}}]})

        mock_post.side_effect = post_side_effect
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        resp = client.post(
            f"/api/telegram/webhook/{bot['bot_id']}",
            json={
                "update_id": 88,
                "message": {
                    "chat": {"id": 5000},
                    "from": {"id": 7000},
                    "message_id": 21,
                    "text": "hello webhook",
                },
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    @patch("app.requests.post")
    def test_telegram_inbound_requires_gateway_auth_when_configured(self, mock_post):
        os.environ["GATEWAY_TOKEN"] = "super-secret"

        def post_side_effect(url, **kwargs):
            if "api.telegram.org" in url:
                return _MockResp(data={"ok": True, "result": {"message_id": 2}})
            return _MockResp(data={"choices": [{"message": {"content": "ok"}}]})

        mock_post.side_effect = post_side_effect
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client, telegram_bot_token="123456:ABCDEF")

        payload = {
            "bot_id": bot["bot_id"],
            "message": {
                "chat": {"id": 123},
                "from": {"id": 1000},
                "message_id": 1,
                "text": "ping",
            },
        }

        unauthorized = client.post("/api/telegram/inbound", json=payload)
        self.assertEqual(unauthorized.status_code, 401)

        authorized = client.post("/api/telegram/inbound", json=payload, headers={"Authorization": "Bearer super-secret"})
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.get_json()["status"], "ok")

    @patch("app.requests.post")
    def test_gateway_mode_chat_queues_event_without_execution(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        resp = client.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "gw-session",
                "message": "hello from gateway-only",
                "wait_for_response": False,
            },
        )
        self.assertEqual(resp.status_code, 202)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["bot_id"], bot["bot_id"])
        self.assertTrue(payload["event_id"])
        self.assertEqual(mock_post.call_count, 0)

        sessions = client.get(f"/api/sessions?bot_id={bot['bot_id']}").get_json()
        self.assertEqual(sessions["sessions"], [])

        q = client.get("/api/queue/stats").get_json()
        self.assertGreaterEqual(int(q["queue"]["inbound"].get("queued", 0)), 1)

    @patch("app.requests.post")
    def test_executor_mode_process_once_consumes_gateway_queued_chat(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "queued-1",
                "message": "run through executor",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "executor response"}}]})

        processed = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(processed.status_code, 200)
        body = processed.get_json()
        self.assertGreaterEqual(int(body["processed_inbound"]), 1)

        hist = client_gw.get(f"/api/sessions/queued-1?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[1]["content"], "executor response")

    def test_executor_mode_disables_gateway_ingress_endpoints(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        chat = client.post("/api/chat", json={"bot_id": bot["bot_id"], "session_id": "s", "message": "hi"})
        self.assertEqual(chat.status_code, 503)
        self.assertIn("gateway plane is disabled", chat.get_json()["error"])

        inbound = client.post(
            "/api/telegram/inbound",
            json={
                "bot_id": bot["bot_id"],
                "message": {"chat": {"id": 1}, "from": {"id": 2}, "message_id": 3, "text": "x"},
            },
        )
        self.assertEqual(inbound.status_code, 503)

    def test_gateway_mode_rejects_manual_queue_process_once(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/queue/process-once", json={"kind": "both", "max_events": 1})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("executor plane is disabled", resp.get_json()["error"])

    def test_gateway_mode_rejects_dead_letter_replay(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/queue/dead-letter/replay", json={"queue": "inbound", "bot_id": "bot-x"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("executor plane is disabled", resp.get_json()["error"])

    def test_gateway_mode_rejects_dead_letter_purge(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/queue/dead-letter/purge", json={"queue": "inbound", "bot_id": "bot-x"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("executor plane is disabled", resp.get_json()["error"])

    def test_executor_dead_letter_purge_requires_scope_or_allow_all(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app = self._make_app()
        client = app.test_client()
        resp = client.post("/api/queue/dead-letter/purge", json={"queue": "both"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_gateway_mode_metrics_show_queued_inbound_age(self):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        app = self._make_app()
        client = app.test_client()
        bot = self._create_bot(client)

        queued = client.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "metrics-queued",
                "message": "queue me",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        metrics = client.get("/api/metrics")
        self.assertEqual(metrics.status_code, 200)
        qm = metrics.get_json()["queue_metrics"]["inbound"]
        self.assertGreaterEqual(int(qm["counts"].get("queued", 0)), 1)
        self.assertIsNotNone(qm["oldest_queued_age_s"])

    @patch("app.requests.post")
    def test_executor_mode_processes_outbound_forward_queue(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "queued-forward",
                "message": "needs forward",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = [
            _MockResp(data={"choices": [{"message": {"content": "executor forward response"}}]}),
            _MockResp(data={"ok": True}),
        ]

        in_res = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(in_res.status_code, 200)
        out_res = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 5})
        self.assertEqual(out_res.status_code, 200)

        self.assertGreaterEqual(mock_post.call_count, 2)
        forward_call = mock_post.call_args_list[-1]
        self.assertEqual(forward_call.args[0], "http://example.test/hook")
        self.assertEqual(forward_call.kwargs["json"]["bot_id"], bot["bot_id"])

    @patch("app.requests.post")
    def test_executor_retries_inbound_then_marks_dead_letter(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "dead-letter-inbound",
                "message": "fail this inbound job",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = RuntimeError("llm unavailable")

        first = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(first.status_code, 502)
        q1 = client_ex.get("/api/queue/stats").get_json()["queue"]["inbound"]
        self.assertGreaterEqual(int(q1.get("queued", 0)), 1)
        self.assertEqual(int(q1.get("dead_letter", 0)), 0)

        time.sleep(1.05)
        second = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(second.status_code, 502)

        dead = client_ex.get(f"/api/queue/dead-letter?queue=inbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["inbound"]
        self.assertTrue(dead)
        self.assertEqual(dead[0]["status"], "dead_letter")
        self.assertEqual(dead[0]["attempts"], 2)
        self.assertIn("llm unavailable", dead[0]["error"])

        hist = client_gw.get(f"/api/sessions/dead-letter-inbound?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual([m["role"] for m in hist], ["user"])

    @patch("app.requests.post")
    def test_inbound_retry_then_success_preserves_single_user_message(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "3"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "retry-success",
                "message": "recover after one retry",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = [
            RuntimeError("transient llm failure"),
            _MockResp(data={"choices": [{"message": {"content": "recovered reply"}}]}),
        ]

        first = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(first.status_code, 502)

        time.sleep(1.05)
        second = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(second.status_code, 200)
        self.assertGreaterEqual(int(second.get_json()["processed_inbound"]), 1)

        hist = client_gw.get(f"/api/sessions/retry-success?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[1]["content"], "recovered reply")

        dead = client_ex.get(f"/api/queue/dead-letter?queue=inbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["inbound"]
        self.assertEqual(dead, [])

    @patch("app.requests.post")
    def test_executor_retries_outbound_then_marks_dead_letter(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "dead-letter-outbound",
                "message": "trigger outbound dead letter",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        def post_side_effect(url, **kwargs):
            if url == "http://example.test/hook":
                raise RuntimeError("forward unavailable")
            return _MockResp(data={"choices": [{"message": {"content": "assistant reply"}}]})

        mock_post.side_effect = post_side_effect

        in_res = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(in_res.status_code, 200)
        self.assertGreaterEqual(int(in_res.get_json()["processed_inbound"]), 1)

        out_first = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_first.status_code, 502)
        out_q1 = client_ex.get("/api/queue/stats").get_json()["queue"]["outbound"]
        self.assertGreaterEqual(int(out_q1.get("queued", 0)), 1)
        self.assertEqual(int(out_q1.get("dead_letter", 0)), 0)

        time.sleep(1.05)
        out_second = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_second.status_code, 502)

        dead = client_ex.get(f"/api/queue/dead-letter?queue=outbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["outbound"]
        self.assertTrue(dead)
        self.assertEqual(dead[0]["status"], "dead_letter")
        self.assertEqual(dead[0]["attempts"], 2)
        self.assertEqual(dead[0]["channel"], "forward")
        self.assertIn("forward unavailable", dead[0]["error"])

    @patch("app.requests.post")
    def test_executor_can_replay_inbound_dead_letter_event(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "replay-dead-letter",
                "message": "needs replay",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = RuntimeError("llm down")
        first = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(first.status_code, 502)
        time.sleep(1.05)
        second = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(second.status_code, 502)

        dead = client_ex.get(f"/api/queue/dead-letter?queue=inbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["inbound"]
        self.assertTrue(dead)
        dead_event_id = dead[0]["event_id"]

        replay = client_ex.post(
            "/api/queue/dead-letter/replay",
            json={"queue": "inbound", "event_ids": [dead_event_id], "bot_id": bot["bot_id"], "reset_attempts": False},
        )
        self.assertEqual(replay.status_code, 200)
        replay_payload = replay.get_json()["replay"]
        self.assertEqual(int(replay_payload["replayed_inbound"]), 1)

        mock_post.side_effect = [_MockResp(data={"choices": [{"message": {"content": "replayed ok"}}]})]
        processed = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(processed.status_code, 200)
        self.assertGreaterEqual(int(processed.get_json()["processed_inbound"]), 1)

        hist = client_gw.get(f"/api/sessions/replay-dead-letter?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[1]["content"], "replayed ok")

    @patch("app.requests.post")
    def test_executor_reclaims_stale_processing_inbound_lock(self, mock_post):
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S"] = "5"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "stale-lock",
                "message": "recover stale lock",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)
        event_id = queued.get_json()["event_id"]

        db_path = os.environ["SIMPLEAGENT_DB_PATH"]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE inbound_queue
                SET status = 'processing',
                    locked_at = ?,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (time.time() - 30.0, time.time() - 30.0, event_id),
            )
            conn.commit()

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.return_value = _MockResp(data={"choices": [{"message": {"content": "stale lock recovered"}}]})
        processed = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 1})
        self.assertEqual(processed.status_code, 200)
        self.assertGreaterEqual(int(processed.get_json()["processed_inbound"]), 1)

        hist = client_gw.get(f"/api/sessions/stale-lock?bot_id={bot['bot_id']}").get_json()["history"]
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[1]["content"], "stale lock recovered")

    @patch("app.requests.post")
    def test_executor_can_replay_outbound_dead_letter_event(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "replay-outbound-dead-letter",
                "message": "needs outbound replay",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = [_MockResp(data={"choices": [{"message": {"content": "reply queued"}}]})]
        in_res = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(in_res.status_code, 200)
        self.assertGreaterEqual(int(in_res.get_json()["processed_inbound"]), 1)

        mock_post.side_effect = RuntimeError("forward still down")
        out_first = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_first.status_code, 502)
        time.sleep(1.05)
        out_second = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_second.status_code, 502)

        dead = client_ex.get(f"/api/queue/dead-letter?queue=outbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["outbound"]
        self.assertTrue(dead)
        dead_event_id = dead[0]["event_id"]

        replay = client_ex.post(
            "/api/queue/dead-letter/replay",
            json={"queue": "outbound", "event_id": dead_event_id, "bot_id": bot["bot_id"], "reset_attempts": False},
        )
        self.assertEqual(replay.status_code, 200)
        replay_payload = replay.get_json()["replay"]
        self.assertEqual(int(replay_payload["replayed_outbound"]), 1)

        mock_post.side_effect = [_MockResp(data={"ok": True})]
        out_processed = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_processed.status_code, 200)
        self.assertGreaterEqual(int(out_processed.get_json()["processed_outbound"]), 1)

        dead_after = client_ex.get(f"/api/queue/dead-letter?queue=outbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["outbound"]
        self.assertEqual(dead_after, [])

    @patch("app.requests.post")
    def test_executor_can_purge_outbound_dead_letter_events(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_MAX_ATTEMPTS"] = "2"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_BASE_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_RETRY_CAP_S"] = "1"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "purge-outbound-dead-letter",
                "message": "needs outbound purge",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = [_MockResp(data={"choices": [{"message": {"content": "reply before purge"}}]})]
        in_res = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(in_res.status_code, 200)

        mock_post.side_effect = RuntimeError("forward dead-letter purge test")
        out_first = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_first.status_code, 502)
        time.sleep(1.05)
        out_second = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_second.status_code, 502)

        dead_before = client_ex.get(f"/api/queue/dead-letter?queue=outbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["outbound"]
        self.assertTrue(dead_before)

        purged = client_ex.post(
            "/api/queue/dead-letter/purge",
            json={"queue": "outbound", "bot_id": bot["bot_id"], "limit": 100},
        )
        self.assertEqual(purged.status_code, 200)
        purge_payload = purged.get_json()["purge"]
        self.assertGreaterEqual(int(purge_payload["deleted_outbound"]), 1)

        dead_after = client_ex.get(f"/api/queue/dead-letter?queue=outbound&bot_id={bot['bot_id']}").get_json()["dead_letter"]["outbound"]
        self.assertEqual(dead_after, [])

    @patch("app.requests.post")
    def test_executor_reclaims_stale_processing_outbound_lock(self, mock_post):
        os.environ["SIMPLEAGENT_FORWARD_ENABLED"] = "1"
        os.environ["SIMPLEAGENT_FORWARD_URL"] = "http://example.test/hook"
        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "gateway"
        os.environ["SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S"] = "1"
        os.environ["SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S"] = "5"
        app_gw = self._make_app()
        client_gw = app_gw.test_client()
        bot = self._create_bot(client_gw)

        queued = client_gw.post(
            "/api/chat",
            json={
                "bot_id": bot["bot_id"],
                "session_id": "stale-outbound-lock",
                "message": "create outbound and stale lock",
                "wait_for_response": False,
            },
        )
        self.assertEqual(queued.status_code, 202)

        os.environ["SIMPLEAGENT_SERVICE_MODE"] = "executor"
        os.environ["SIMPLEAGENT_EXECUTOR_AUTO_RUN"] = "0"
        os.environ["SIMPLEAGENT_DELIVERY_AUTO_RUN"] = "0"
        app_ex = self._make_app()
        client_ex = app_ex.test_client()

        mock_post.side_effect = [_MockResp(data={"choices": [{"message": {"content": "queued for outbound"}}]})]
        in_res = client_ex.post("/api/queue/process-once", json={"kind": "inbound", "max_events": 5})
        self.assertEqual(in_res.status_code, 200)
        self.assertGreaterEqual(int(in_res.get_json()["processed_inbound"]), 1)

        db_path = os.environ["SIMPLEAGENT_DB_PATH"]
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT event_id
                FROM outbound_queue
                WHERE bot_id = ? AND channel = 'forward'
                ORDER BY id DESC
                LIMIT 1
                """,
                (bot["bot_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            event_id = str(row[0])
            conn.execute(
                """
                UPDATE outbound_queue
                SET status = 'processing',
                    locked_at = ?,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (time.time() - 30.0, time.time() - 30.0, event_id),
            )
            conn.commit()

        mock_post.side_effect = [_MockResp(data={"ok": True})]
        out_res = client_ex.post("/api/queue/process-once", json={"kind": "outbound", "max_events": 1})
        self.assertEqual(out_res.status_code, 200)
        self.assertGreaterEqual(int(out_res.get_json()["processed_outbound"]), 1)


if __name__ == "__main__":
    unittest.main()
