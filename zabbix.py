"""Consulta sob demanda aos problemas atuais do Zabbix via JSON-RPC."""
from collections import Counter
from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

import httpx

from tactical import IntegrationError, validate_page


SEVERITIES = ("not_classified", "information", "warning", "average", "high", "disaster")


class ZabbixClient:
    def __init__(self, api_url, api_token, *, ca_file=None, transport=None,
                 allow_insecure_http=False):
        parts = urlsplit(api_url)
        if (parts.scheme not in ("https", "http")
                or (parts.scheme == "http" and not allow_insecure_http)
                or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or not parts.path.endswith("/api_jsonrpc.php")):
            raise IntegrationError("ZABBIX_API_URL deve terminar em /api_jsonrpc.php; HTTP exige ZABBIX_ALLOW_INSECURE_HTTP=true.")
        if not api_token or api_token.startswith("SUA_"):
            raise IntegrationError("Configure ZABBIX_API_TOKEN localmente no .env.")
        self.http = httpx.AsyncClient(
            headers={"Content-Type": "application/json-rpc", "Accept": "application/json"},
            timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            verify=ca_file or True, transport=transport,
        )
        self.url = api_url
        self.api_token = api_token

    @classmethod
    def from_env(cls):
        return cls(os.getenv("ZABBIX_API_URL", ""), os.getenv("ZABBIX_API_TOKEN", ""),
                   ca_file=os.getenv("ZABBIX_CA_FILE") or None,
                   allow_insecure_http=os.getenv("ZABBIX_ALLOW_INSECURE_HTTP", "false").lower() == "true")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def _rpc(self, method, params):
        if method != "problem.get":
            raise IntegrationError("Metodo Zabbix nao permitido.")
        try:
            response = await self.http.post(self.url, json={"jsonrpc": "2.0",
                                                 "method": method, "params": params,
                                                 "auth": self.api_token, "id": 1})
        except httpx.RequestError:
            raise IntegrationError("Falha de rede, timeout ou TLS ao consultar Zabbix.") from None
        if response.status_code in (401, 403):
            raise IntegrationError("Zabbix recusou o token ou as permissoes.")
        if not 200 <= response.status_code < 300:
            raise IntegrationError(f"Zabbix retornou HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            raise IntegrationError("Resposta Zabbix nao e JSON valido.") from None
        if not isinstance(data, dict):
            raise IntegrationError("Formato inesperado na resposta do Zabbix.")
        if "error" in data:
            # A descricao arbitraria pode conter dados sensiveis; omitimos o corpo.
            code = data["error"].get("code") if isinstance(data["error"], dict) else None
            raise IntegrationError(f"Zabbix retornou erro JSON-RPC {code if type(code) is int else 'desconhecido'}. Confira token, papel e versao.")
        if "result" not in data:
            raise IntegrationError("Resposta Zabbix sem result.")
        return data["result"]

    async def problems(self, *, min_severity=0, group_ids=None, host_ids=None,
                       limit=50, offset=0):
        validate_page(limit, offset)
        if type(min_severity) is not int or not 0 <= min_severity <= 5:
            raise IntegrationError("min_severity deve estar entre 0 e 5.")
        for ids in (group_ids, host_ids):
            if ids is not None and (not ids or any(type(value) is not int or value <= 0 for value in ids)):
                raise IntegrationError("IDs de grupo/host devem ser numeros positivos.")
        params = {"output": ["eventid", "objectid", "clock", "name", "severity",
                              "acknowledged", "suppressed", "r_eventid", "opdata"],
                  "recent": False, "severities": list(range(min_severity, 6)),
                  "selectTags": ["tag", "value"],
                  "selectSuppressionData": ["maintenanceid", "userid", "suppress_until"],
                  "sortfield": "eventid", "sortorder": "DESC"}
        if group_ids:
            params["groupids"] = [str(value) for value in group_ids]
        if host_ids:
            params["hostids"] = [str(value) for value in host_ids]
        rows = await self._rpc("problem.get", params)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise IntegrationError("Formato inesperado de problem.get.")
        now = datetime.now(timezone.utc)
        items = [normalize_problem(row, now) for row in rows]
        items.sort(key=lambda item: (-item["severity_id"], item["created_at"] or "", item["id"]))
        page = items[offset:offset + limit]
        return {"source": "zabbix", "fetched_at": now.isoformat(),
                "total_matching": len(items), "returned": len(page),
                "offset": offset, "partial": len(page) < len(items),
                "next_offset": offset + limit if offset + limit < len(items) else None,
                "counts_by_severity": dict(Counter(item["severity"] for item in items)),
                "scope": {"min_severity": min_severity, "group_ids": group_ids,
                          "host_ids": host_ids, "recent": False,
                          "visibility": "API token role"},
                "pagination_note": "Recorte local; nova chamada consulta novamente a API.",
                "items": page}


def normalize_problem(row, now):
    try:
        severity_id = int(row.get("severity"))
        if not 0 <= severity_id <= 5:
            raise ValueError
    except (ValueError, TypeError):
        raise IntegrationError("Problema Zabbix com severidade inesperada.") from None
    try:
        created = datetime.fromtimestamp(int(row["clock"]), timezone.utc)
        age_hours = round((now - created).total_seconds() / 3600, 2)
        if age_hours < 0:
            age_hours = None
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        created, age_hours = None, None
    tags = row.get("tags")
    tags = [{"tag": str(item.get("tag", ""))[:80],
             "value": str(item.get("value", ""))[:120]}
            for item in tags[:20] if isinstance(item, dict)] if isinstance(tags, list) else []
    suppressed = row.get("suppressed") in ("1", 1, True)
    return {"source": "zabbix", "id": str(row.get("eventid", "")),
            "trigger_id": str(row.get("objectid", "")),
            "name": str(row.get("name", ""))[:500],
            "severity_id": severity_id, "severity": SEVERITIES[severity_id],
            "acknowledged": row.get("acknowledged") in ("1", 1, True),
            "suppressed": suppressed,
            "suppression_data": row.get("suppression_data") if suppressed else [],
            "tags": tags, "opdata": str(row.get("opdata") or "")[:500],
            "created_at": created.isoformat() if created else None,
            "age_hours": age_hours, "date_needs_review": created is None or age_hours is None}
