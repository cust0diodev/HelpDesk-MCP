import os
import unittest
from unittest.mock import patch

import httpx

from grafana import GrafanaClient
from tactical import IntegrationError


class GrafanaTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return GrafanaClient("https://grafana.example", "test-secret",
                             transport=httpx.MockTransport(handler))

    async def test_catalog_minimizes_metadata(self):
        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            if request.url.path == "/api/search":
                self.assertEqual(request.url.params["type"], "dash-db")
                return httpx.Response(200, json=[{"uid": "abc", "title": "Monitor",
                    "url": "/d/abc/monitor", "folderTitle": "NOC", "secret": "hidden"}])
            if request.url.path == "/api/datasources":
                return httpx.Response(200, json=[{"uid": "ds1", "name": "Zabbix",
                    "type": "alexanderzobnin-zabbix-datasource", "url": "http://internal",
                    "secureJsonData": {"password": "hidden"}, "isDefault": True}])
            self.fail(f"Unexpected path: {request.url.path}")
        async with self.client(respond) as client:
            result = await client.catalog(query="Monitor", limit=1)
        self.assertEqual(result["dashboards"][0]["url"], "https://grafana.example/d/abc/monitor")
        self.assertEqual(result["datasources"][0]["uid"], "ds1")
        self.assertNotIn("hidden", str(result))
        self.assertNotIn("http://internal", str(result))

    async def test_dashboard_omits_queries(self):
        def respond(request):
            self.assertEqual(request.url.path, "/api/dashboards/uid/abc")
            return httpx.Response(200, json={"dashboard": {"title": "Monitor", "panels": [
                {"id": 1, "title": "Graficos", "type": "row", "panels": [
                    {"id": 7, "title": "Backbone", "type": "timeseries",
                     "datasource": {"uid": "ds1"}, "targets": [{"rawSql": "SECRET SQL"}]}
                ]}
            ]}})
        async with self.client(respond) as client:
            result = await client.dashboard("abc")
        self.assertEqual(result["panels"][1]["datasource_uids"], ["ds1"])
        self.assertNotIn("SECRET SQL", str(result))

    async def test_rejects_invalid_uid_and_http_without_opt_in(self):
        async with self.client(lambda _: httpx.Response(200, json={})) as client:
            with self.assertRaises(IntegrationError):
                await client.dashboard("../secret")
        with self.assertRaises(IntegrationError):
            GrafanaClient("http://grafana.example", "test-secret")

    async def test_distinguishes_bad_token_from_denied_dashboard(self):
        for status, expected in ((401, "401"), (403, "403")):
            async with self.client(lambda _: httpx.Response(status)) as client:
                with self.assertRaises(IntegrationError) as exc:
                    await client.dashboard("abc")
            self.assertIn(expected, str(exc.exception))

    async def test_profiles_are_separate(self):
        settings = {"GRAFANA_MONITOR_BASE_URL": "http://monitor.example:3099",
                    "GRAFANA_MONITOR_SERVICE_TOKEN": "monitor-token",
                    "GRAFANA_MONITOR_ALLOW_INSECURE_HTTP": "true",
                    "GRAFANA_HELPDESK_BASE_URL": "http://helpdesk.example:3089",
                    "GRAFANA_HELPDESK_SERVICE_TOKEN": "helpdesk-token",
                    "GRAFANA_HELPDESK_ALLOW_INSECURE_HTTP": "true"}
        with patch.dict(os.environ, settings, clear=True):
            async with GrafanaClient.from_env("monitor") as monitor:
                self.assertEqual(monitor.base_url, "http://monitor.example:3099")
            async with GrafanaClient.from_env("helpdesk") as helpdesk:
                self.assertEqual(helpdesk.base_url, "http://helpdesk.example:3089")
            with self.assertRaises(IntegrationError):
                GrafanaClient.from_env("unknown")


if __name__ == "__main__":
    unittest.main()
