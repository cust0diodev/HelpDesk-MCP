import json
import unittest
import httpx

from glpi import GLPIClient, parse_statuses
from tactical import IntegrationError


class GLPITests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return GLPIClient("https://glpi.example/apirest.php", "user-secret", "app-secret",
                          transport=httpx.MockTransport(handler))

    async def test_http_requires_explicit_opt_in(self):
        with self.assertRaises(IntegrationError):
            GLPIClient("http://glpi.example/apirest.php", "u", "a")
        client = GLPIClient("http://glpi.example/apirest.php", "u", "a", allow_insecure_http=True)
        self.assertEqual(client.http.base_url.scheme, "http")
        await client.http.aclose()

    async def test_app_token_is_optional(self):
        client = GLPIClient("https://glpi.example/apirest.php", "user-secret", "")
        self.assertNotIn("App-Token", client.http.headers)
        await client.http.aclose()

    async def test_session_tickets_filter_and_cleanup(self):
        calls = []
        def respond(req):
            calls.append(req)
            if req.url.path.endswith("/initSession/"):
                self.assertEqual(req.headers["App-Token"], "app-secret")
                self.assertEqual(req.headers["Authorization"], "user_token user-secret")
                return httpx.Response(200, json={"session_token": "session-secret"})
            if req.url.path.endswith("/Ticket/"):
                self.assertEqual(req.headers["Session-Token"], "session-secret")
                self.assertEqual(req.url.params["is_deleted"], "0")
                return httpx.Response(200, json=[
                    {"id": 2, "name": "Atrasado", "status": 2, "priority": 5, "date_mod": "2026-09-20T12:00:00+00:00"},
                    {"id": 3, "name": "Fechado", "status": 6, "priority": 6, "date_mod": "2026-09-20T12:00:00+00:00"},
                ])
            if req.url.path.endswith("/killSession/"):
                self.assertEqual(req.method, "GET")
                return httpx.Response(200, json=True)
            raise AssertionError(req.url)
        async with self.client(respond) as client:
            data = await client.tickets(idle_hours=1)
        self.assertEqual(data["total_matching"], 1)
        self.assertEqual(data["items"][0]["id"], 2)
        self.assertEqual([r.url.path for r in calls], ["/apirest.php/initSession/", "/apirest.php/Ticket/", "/apirest.php/killSession/"])

    async def test_auth_error_does_not_leak_token(self):
        def respond(req):
            return httpx.Response(401, text="user-secret app-secret")
        client = self.client(respond)
        try:
            with self.assertRaises(IntegrationError) as exc:
                await client.start_session()
        finally:
            await client.http.aclose()
        self.assertNotIn("secret", str(exc.exception))

    async def test_glpi_400_exposes_only_known_code(self):
        async def failure(body):
            client = self.client(lambda req: httpx.Response(400, json=body))
            try:
                with self.assertRaises(IntegrationError) as exc:
                    await client.start_session()
                return str(exc.exception)
            finally:
                await client.http.aclose()
        message = await failure(["ERROR_APP_TOKEN_PARAMETERS_MISSING", "app-secret"])
        self.assertIn("ERROR_APP_TOKEN_PARAMETERS_MISSING", message)
        self.assertNotIn("app-secret", message)
        message = await failure(["UNKNOWN_TOKEN", "user-secret"])
        self.assertEqual(message, "GLPI retornou HTTP 400 ao iniciar sessao.")

    async def test_bad_schema_is_error(self):
        def respond(req):
            if req.url.path.endswith("/initSession/"):
                return httpx.Response(200, json={"session_token": "s"})
            return httpx.Response(200, json={"data": []})
        async with self.client(respond) as client:
            await client.start_session()
            with self.assertRaises(IntegrationError):
                await client.tickets()

    def test_status_validation(self):
        self.assertEqual(parse_statuses("1, 2,4"), [1, 2, 4])
        with self.assertRaises(IntegrationError):
            parse_statuses("1,closed")


if __name__ == "__main__":
    unittest.main()
