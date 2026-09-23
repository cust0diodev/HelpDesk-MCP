import json
import unittest
from datetime import datetime, timezone

import httpx

from tactical import TacticalClient, IntegrationError, normalize_alert


def alert(id=1, **extra):
    return {"id": id, "severity": "error", "resolved": False, "snoozed": False,
            "alert_time": "2026-09-20T12:00:00Z", "message": "CPU elevada", **extra}


class TacticalTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return TacticalClient("https://rmm.example", "test-secret",
                              transport=httpx.MockTransport(handler))

    async def test_contract_sort_counts_and_minimization(self):
        def respond(req):
            self.assertEqual(req.method, "PATCH")
            self.assertEqual(req.url.path, "/alerts/")
            self.assertEqual(req.headers["X-API-KEY"], "test-secret")
            self.assertEqual(json.loads(req.content), {"resolvedFilter": False, "snoozedFilter": False})
            return httpx.Response(200, json=[alert(1, severity="info"), alert(2, secret="omit"),
                                            alert(3, resolved=True), alert(4, snoozed=True)])
        async with self.client(respond) as client:
            data = await client.alerts(limit=1)
        self.assertEqual(data["total_matching"], 2)
        self.assertTrue(data["partial"])
        self.assertEqual(data["next_offset"], 1)
        self.assertEqual(data["items"][0]["id"], 2)
        self.assertNotIn("secret", data["items"][0])
        self.assertEqual(data["counts_by_severity"], {"error": 1, "info": 1})

    async def test_include_snoozed_and_filters(self):
        def respond(req):
            self.assertEqual(json.loads(req.content), {"resolvedFilter": False,
                             "severityFilter": ["warning"], "clientFilter": [5]})
            return httpx.Response(200, json=[alert(severity="warning", snoozed=True)])
        async with self.client(respond) as client:
            data = await client.alerts(include_snoozed=True, severity="warning", client_ids=[5])
        self.assertEqual(data["total_matching"], 1)

    async def test_http_errors_do_not_leak_body_or_secret(self):
        for status in (301, 401, 403, 404, 405, 429, 500):
            calls = []
            def respond(req):
                calls.append(req)
                return httpx.Response(status, text="test-secret", headers={"Location": "https://other.example"})
            async with self.client(respond) as client:
                with self.assertRaises(IntegrationError) as exc:
                    await client.alerts()
            self.assertNotIn("test-secret", str(exc.exception))
            self.assertEqual(len(calls), 1)

    async def test_unknown_schema_is_error_not_zero(self):
        for payload in ({"results": [], "next": "https://other.example"}, [{"id": 1}], ["bad"]):
            async with self.client(lambda r: httpx.Response(200, json=payload)) as client:
                with self.assertRaises(IntegrationError):
                    await client.alerts()

    async def test_empty_is_valid(self):
        async with self.client(lambda r: httpx.Response(200, json=[])) as client:
            data = await client.alerts()
        self.assertEqual(data["total_matching"], 0)
        self.assertFalse(data["partial"])

    async def test_timeout(self):
        def respond(req):
            raise httpx.ReadTimeout("internal-sensitive", request=req)
        async with self.client(respond) as client:
            with self.assertRaises(IntegrationError) as exc:
                await client.alerts()
        self.assertNotIn("internal-sensitive", str(exc.exception))

    async def test_agents_allowlist(self):
        def respond(req):
            self.assertEqual(req.method, "GET")
            self.assertEqual(req.url.path, "/agents/")
            self.assertEqual(req.url.params["detail"], "true")
            return httpx.Response(200, json=[{"agent_id": "abc", "status": "online", "password": "omit"}])
        async with self.client(respond) as client:
            data = await client.agents()
        self.assertEqual(data["counts_by_status"], {"online": 1})
        self.assertNotIn("password", data["items"][0])

    async def test_pagination_and_bad_input(self):
        async with self.client(lambda r: httpx.Response(200, json=[alert(1), alert(2)])) as client:
            data = await client.alerts(limit=1, offset=1)
            self.assertEqual(data["items"][0]["id"], 2)
            self.assertIsNone(data["next_offset"])
            for kwargs in ({"limit": 0}, {"offset": -1}, {"severity": "critical"}, {"client_ids": []}):
                with self.assertRaises(IntegrationError):
                    await client.alerts(**kwargs)

    def test_dates_and_unknown_severity(self):
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        for value in (None, "invalid", "2026-09-20T12:00:00", "2099-01-01T00:00:00Z"):
            data = normalize_alert(alert(alert_time=value, severity="new"), now)
            self.assertTrue(data["date_needs_review"])
            self.assertIsNone(data["age_hours"])
            self.assertEqual(data["priority"], "revisar")


if __name__ == "__main__":
    unittest.main()
