import unittest

import httpx

from tactical import IntegrationError
from zabbix import ZabbixClient


class ZabbixTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return ZabbixClient("https://zabbix.example/zabbix/api_jsonrpc.php", "test-secret",
                            transport=httpx.MockTransport(handler))

    async def test_problem_get_contract_and_counts(self):
        def respond(request):
            payload = request.read().decode()
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/zabbix/api_jsonrpc.php")
            self.assertNotIn("Authorization", request.headers)
            self.assertIn('"auth":"test-secret"', payload)
            self.assertIn('"recent":false', payload)
            self.assertIn('"groupids":["5"]', payload)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [
                {"eventid": "100", "objectid": "10", "clock": "1789950000",
                 "name": "Interface down", "severity": "5", "acknowledged": "1",
                 "suppressed": "0", "tags": [{"tag": "service", "value": "internet"}]},
                {"eventid": "101", "objectid": "11", "clock": "1789950000",
                 "name": "CPU high", "severity": "3", "acknowledged": "0",
                 "suppressed": "1", "suppression_data": [{"maintenanceid": "20"}]},
            ]})
        async with self.client(respond) as client:
            data = await client.problems(min_severity=3, group_ids=[5], limit=1)
        self.assertEqual(data["total_matching"], 2)
        self.assertEqual(data["returned"], 1)
        self.assertEqual(data["items"][0]["severity"], "disaster")
        self.assertTrue(data["items"][0]["acknowledged"])
        self.assertEqual(data["counts_by_severity"], {"disaster": 1, "average": 1})

    async def test_rpc_error_sanitized(self):
        async with self.client(lambda r: httpx.Response(200, json={"error": {
            "code": -32602, "data": "test-secret"}})) as client:
            with self.assertRaises(IntegrationError) as exc:
                await client.problems()
        self.assertIn("-32602", str(exc.exception))
        self.assertNotIn("test-secret", str(exc.exception))

    async def test_bad_schema_and_auth(self):
        for response in (httpx.Response(200, json={"result": {}}),
                         httpx.Response(403, text="test-secret")):
            async with self.client(lambda r: response) as client:
                with self.assertRaises(IntegrationError):
                    await client.problems()

    async def test_bad_input(self):
        async with self.client(lambda r: httpx.Response(200, json={"result": []})) as client:
            for kwargs in ({"min_severity": 6}, {"group_ids": []}, {"limit": 0}):
                with self.assertRaises(IntegrationError):
                    await client.problems(**kwargs)

    async def test_http_requires_opt_in(self):
        with self.assertRaises(IntegrationError):
            ZabbixClient("http://zabbix.example/api_jsonrpc.php", "token")
        async with ZabbixClient("http://zabbix.example/api_jsonrpc.php", "token",
                                allow_insecure_http=True) as client:
            self.assertEqual(client.url.split(":", 1)[0], "http")


if __name__ == "__main__":
    unittest.main()
