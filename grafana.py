"""Catalogo de dashboards/fontes Grafana 12.1 sem consultas SQL ou series brutas."""
import os
import re
from urllib.parse import urlsplit

import httpx

from tactical import IntegrationError


_UID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class GrafanaClient:
    def __init__(self, base_url, service_token, *, ca_file=None, transport=None,
                 allow_insecure_http=False):
        url = urlsplit(base_url)
        if (url.scheme not in ("https", "http")
                or (url.scheme == "http" and not allow_insecure_http)
                or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise IntegrationError("GRAFANA_BASE_URL deve ser a origem; HTTP exige GRAFANA_ALLOW_INSECURE_HTTP=true.")
        if not service_token or service_token.startswith("SUA_"):
            raise IntegrationError("Configure GRAFANA_SERVICE_TOKEN localmente no .env.")
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            verify=ca_file or True, transport=transport,
        )
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls, instance="monitor"):
        if instance not in ("monitor", "helpdesk"):
            raise IntegrationError("Instancia Grafana invalida.")
        prefix = "GRAFANA_MONITOR" if instance == "monitor" else "GRAFANA_HELPDESK"
        def setting(name, default=""):
            value = os.getenv(f"{prefix}_{name}")
            if value is None and instance == "monitor":
                value = os.getenv(f"GRAFANA_{name}")
            return value if value is not None else default
        return cls(setting("BASE_URL"), setting("SERVICE_TOKEN"),
                   ca_file=setting("CA_FILE") or None,
                   allow_insecure_http=setting("ALLOW_INSECURE_HTTP", "false").lower() == "true")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def _get(self, path, params=None):
        try:
            response = await self.http.get(path, params=params)
        except httpx.RequestError:
            raise IntegrationError("Falha de rede, timeout ou TLS ao consultar Grafana.") from None
        if response.status_code == 401:
            raise IntegrationError("Grafana retornou HTTP 401: token ausente, invalido ou expirado.")
        if response.status_code == 403:
            raise IntegrationError("Grafana retornou HTTP 403: a conta de servico nao tem permissao para este recurso.")
        if not 200 <= response.status_code < 300:
            raise IntegrationError(f"Grafana retornou HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise IntegrationError("Resposta Grafana nao e JSON valido.") from None

    async def catalog(self, *, query="", limit=50):
        if type(limit) is not int or not 1 <= limit <= 100 or len(query) > 100:
            raise IntegrationError("Use limit entre 1 e 100 e query com ate 100 caracteres.")
        dashboards = await self._get("/api/search", {"type": "dash-db", "query": query,
                                                      "limit": limit})
        datasources = await self._get("/api/datasources")
        if (not isinstance(dashboards, list) or not isinstance(datasources, list)
                or any(not isinstance(row, dict) for row in dashboards + datasources)):
            raise IntegrationError("Formato inesperado do catalogo Grafana.")
        return {"source": "grafana", "kind": "catalog",
                "dashboards": [{"uid": item.get("uid"), "title": item.get("title"),
                                "folder_title": item.get("folderTitle"),
                                "url": self.base_url + item["url"] if isinstance(item.get("url"), str)
                                       and item["url"].startswith("/d/") else None}
                               for item in dashboards],
                "datasources": [{"uid": item.get("uid"), "name": item.get("name"),
                                 "type": item.get("type"), "is_default": item.get("isDefault")}
                                for item in datasources],
                "note": "Catalogo e configuracao; nao representa valores atuais dos paineis."}

    async def dashboard(self, uid):
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise IntegrationError("UID de dashboard invalido.")
        payload = await self._get(f"/api/dashboards/uid/{uid}")
        dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
        if not isinstance(dashboard, dict) or not isinstance(dashboard.get("panels"), list):
            raise IntegrationError("Formato inesperado do dashboard Grafana.")
        panels = []
        pending = list(dashboard["panels"])
        while pending:
            panel = pending.pop(0)
            if not isinstance(panel, dict):
                continue
            if isinstance(panel.get("panels"), list):
                pending[0:0] = panel["panels"]
            targets = panel.get("targets") if isinstance(panel.get("targets"), list) else []
            datasources = set()
            for source in [panel.get("datasource")] + [t.get("datasource") for t in targets if isinstance(t, dict)]:
                if isinstance(source, dict) and isinstance(source.get("uid"), str):
                    datasources.add(source["uid"])
                elif isinstance(source, str):
                    datasources.add(source)
            panels.append({"id": panel.get("id"), "title": panel.get("title"),
                           "type": panel.get("type"), "datasource_uids": sorted(datasources)})
        return {"source": "grafana", "kind": "dashboard_metadata", "uid": uid,
                "title": dashboard.get("title"), "panels": panels,
                "note": "Nao inclui consultas, SQL, credenciais ou valores de series."}
