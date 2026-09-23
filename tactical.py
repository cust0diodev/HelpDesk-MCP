"""Adaptador de consulta para o contrato da API Tactical RMM v1.5.2."""
from collections import Counter
from datetime import datetime, timezone
import os
import ssl
from urllib.parse import urlsplit

import httpx


class IntegrationError(Exception):
    """Erro seguro: sem corpo da resposta, URL interna ou credenciais."""


class TacticalClient:
    def __init__(self, base_url, api_key, *, ca_file=None, transport=None):
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise IntegrationError("TACTICAL_BASE_URL deve ser uma origem HTTPS, sem caminho ou credenciais.")
        if not api_key or api_key == "SUBSTITUA_LOCALMENTE":
            raise IntegrationError("Configure TACTICAL_API_KEY localmente no .env.")
        try:
            context = ssl.create_default_context(cafile=ca_file or None)
        except (OSError, ssl.SSLError):
            raise IntegrationError("Nao foi possivel carregar a CA configurada.") from None
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            verify=context, transport=transport,
        )

    @classmethod
    def from_env(cls):
        return cls(os.getenv("TACTICAL_BASE_URL", ""), os.getenv("TACTICAL_API_KEY", ""),
                   ca_file=os.getenv("TACTICAL_CA_FILE"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def _read(self, path, payload=None):
        # Allowlist fixa: nenhum endpoint ou metodo arbitrario e exposto ao modelo.
        if path not in ("/alerts/", "/agents/"):
            raise IntegrationError("Endpoint nao permitido.")
        try:
            # PATCH nesta rota de listagem e leitura na v1.5.2; nao confundir
            # com PUT /alerts/{id}/, que altera um alerta.
            if path == "/alerts/":
                response = await self.http.patch(path, json=payload)
            else:
                # detail=false retorna apenas identificacao na v1.5.2, sem status.
                response = await self.http.get(path, params={"detail": "true"})
            if response.status_code in (401, 403):
                raise IntegrationError("Autenticacao ou permissao negada. Confira chave, papel e escopo de clientes.")
            if response.status_code == 429:
                raise IntegrationError("Limite de requisicoes atingido. Tente novamente mais tarde.")
            if not 200 <= response.status_code < 300:
                raise IntegrationError(f"Tactical retornou HTTP {response.status_code}. Confira versao, rota e proxy.")
            data = response.json()
        except httpx.RequestError:
            raise IntegrationError("Falha de rede, timeout ou TLS ao consultar Tactical.") from None
        except ValueError:
            raise IntegrationError("Resposta nao e JSON valido. Confira a URL da API.") from None
        # Este endpoint legado nao e paginado. Nao interpretar envelopes
        # desconhecidos como lista vazia, nem seguir URLs fornecidas pela API.
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise IntegrationError("Formato inesperado: esperado array da API v1.5.2. Validar contrato antes de continuar.")
        return data

    async def alerts(self, *, include_snoozed=False, severity=None, client_ids=None,
                     limit=50, offset=0):
        validate_page(limit, offset)
        if severity is not None and severity not in ("error", "warning", "info"):
            raise IntegrationError("Severidade deve ser error, warning ou info.")
        if client_ids is not None and (not client_ids or any(type(i) is not int or i <= 0 for i in client_ids)):
            raise IntegrationError("client_ids deve conter IDs inteiros positivos.")
        filters = {"resolvedFilter": False}
        if not include_snoozed:
            filters["snoozedFilter"] = False
        if severity:
            filters["severityFilter"] = [severity]
        if client_ids:
            filters["clientFilter"] = client_ids
        rows = await self._read("/alerts/", filters)
        if any(type(r.get("resolved")) is not bool or type(r.get("snoozed")) is not bool for r in rows):
            raise IntegrationError("Alerta sem flags resolved/snoozed validas; conferir contrato.")
        rows = [r for r in rows if not r["resolved"] and not r.get("hidden", False)
                and (include_snoozed or not r["snoozed"])
                and (severity is None or r.get("severity") == severity)]
        now = datetime.now(timezone.utc)
        items = [normalize_alert(row, now) for row in rows]
        # Severidade primeiro, mais antigos primeiro. Datas desconhecidas ficam
        # no inicio da mesma severidade para revisao, em vez de desaparecerem.
        items.sort(key=lambda r: (r["priority_rank"], r["created_at"] or "", str(r["id"])))
        result = envelope(items, limit, offset, now)
        result["counts_by_severity"] = dict(Counter(i["severity"] for i in items))
        result["scope"] = {"resolved": False, "hidden": False,
                           "include_snoozed": include_snoozed, "severity": severity,
                           "client_ids": client_ids, "visibility": "API key role"}
        return result

    async def agents(self, *, limit=50, offset=0):
        validate_page(limit, offset)
        rows = await self._read("/agents/")
        fields = ("agent_id", "hostname", "client_name", "site_name", "status", "last_seen")
        items = [{key: row.get(key) for key in fields} for row in rows]
        items.sort(key=lambda r: (str(r["client_name"]), str(r["hostname"]), str(r["agent_id"])))
        result = envelope(items, limit, offset, datetime.now(timezone.utc))
        result["counts_by_status"] = dict(Counter(str(i["status"] or "unknown") for i in items))
        result["scope"] = "Dispositivos visiveis ao papel da chave; nao representa todo o ambiente necessariamente."
        return result


def validate_page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise IntegrationError("Use limit entre 1 e 200 e offset >= 0.")


def normalize_alert(row, now):
    raw_date = row.get("alert_time")
    created, age = None, None
    if isinstance(raw_date, str):
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
                created = parsed.isoformat()
                age = round((now - parsed).total_seconds() / 3600, 2)
                if age < 0:
                    age = None
        except ValueError:
            pass
    severity = row.get("severity") or "unknown"
    rank, label = {"error": (1, "alta"), "warning": (2, "media"), "info": (3, "rotina")}.get(severity, (0, "revisar"))
    message = str(row.get("message") or "")
    return {
        "source": "tactical", "id": row.get("id"), "kind": row.get("alert_type"),
        "client": row.get("client"), "site": row.get("site"),
        "asset_id": row.get("agent_id"), "hostname": row.get("hostname"),
        "severity": severity, "priority_rank": rank, "priority": label,
        "priority_reason": f"Regra inicial por severidade: {severity}; impacto ainda nao avaliado.",
        "message": message[:800], "message_truncated": len(message) > 800,
        "created_at": created, "age_hours": age,
        "date_needs_review": created is None or age is None,
        "snoozed": row["snoozed"], "resolved": row["resolved"],
    }


def envelope(items, limit, offset, now):
    page = items[offset:offset + limit]
    return {"source": "tactical", "fetched_at": now.isoformat(),
            "total_matching": len(items), "returned": len(page),
            "offset": offset, "partial": len(page) < len(items),
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "pagination_note": "Recorte local de consulta completa; nova chamada consulta novamente a API.",
            "items": page}
