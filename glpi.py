"""Leitor somente consulta da REST API V1 do GLPI/TurboDesk."""
from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

import httpx

from tactical import IntegrationError, validate_page


_SESSION_ERROR_HELP = {
    "ERROR_APP_TOKEN_PARAMETERS_MISSING": "Esta instalacao exige App-Token; solicite ao administrador um cliente de API de leitura.",
    "ERROR_WRONG_APP_TOKEN_PARAMETER": "O App-Token configurado nao e aceito.",
    "ERROR_NOT_ALLOWED_IP": "O IP desta maquina nao esta autorizado no cliente de API do GLPI.",
    "ERROR_LOGIN_PARAMETERS_MISSING": "O servidor nao recebeu o user_token; verifique a configuracao e o proxy HTTP.",
    "ERROR_GLPI_LOGIN_USER_TOKEN": "O user_token nao foi aceito; confira a chave do usuario.",
    "ERROR_GLPI_LOGIN": "O GLPI nao conseguiu iniciar a sessao desse usuario.",
    "ERROR_LOGIN_WITH_CREDENTIALS_DISABLED": "Esta instalacao exige login por user_token.",
}


def _session_error(response):
    """Extrai somente um codigo conhecido, sem repassar corpo ou credenciais."""
    try:
        payload = response.json()
    except ValueError:
        return None
    code = payload[0] if isinstance(payload, list) and payload else None
    if isinstance(payload, dict):
        code = payload.get("error") or payload.get("code")
    if isinstance(code, str) and code in _SESSION_ERROR_HELP:
        return f"{code}: {_SESSION_ERROR_HELP[code]}"
    return None


class GLPIClient:
    def __init__(self, base_url, user_token, app_token, *, ca_file=None, transport=None,
                 open_statuses=None, allow_insecure_http=False):
        url = urlsplit(base_url)
        if (url.scheme not in ("https", "http") or (url.scheme == "http" and not allow_insecure_http)
                or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path in ("", "/")):
            raise IntegrationError("GLPI_BASE_URL deve ser HTTPS; HTTP exige GLPI_ALLOW_INSECURE_HTTP=true.")
        if not user_token or user_token.startswith("SUA "):
            raise IntegrationError("Configure GLPI_USER_TOKEN localmente no .env.")
        self.open_statuses = open_statuses or (1, 2, 3, 4)
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Authorization": f"user_token {user_token}"}
        if app_token and not app_token.startswith("SUA "):
            headers["App-Token"] = app_token
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            verify=ca_file or True, transport=transport,
        )
        self._session_token = None

    @classmethod
    def from_env(cls):
        statuses = tuple(parse_statuses(os.getenv("GLPI_OPEN_STATUSES", "1,2,3,4")))
        return cls(os.getenv("GLPI_BASE_URL", ""), os.getenv("GLPI_USER_TOKEN", ""),
                   os.getenv("GLPI_APP_TOKEN", ""), ca_file=os.getenv("GLPI_CA_FILE") or None,
                   open_statuses=statuses,
                   allow_insecure_http=os.getenv("GLPI_ALLOW_INSECURE_HTTP", "false").lower() == "true")

    async def __aenter__(self):
        await self.start_session()
        return self

    async def __aexit__(self, *args):
        try:
            if self._session_token:
                await self.http.get("/killSession/")
        finally:
            await self.http.aclose()

    async def start_session(self):
        try:
            response = await self.http.get("/initSession/")
        except httpx.RequestError:
            raise IntegrationError("Falha de rede, timeout ou TLS ao iniciar sessao GLPI.") from None
        if response.status_code in (401, 403):
            detail = _session_error(response)
            raise IntegrationError(detail or "GLPI recusou user_token/App-Token ou perfil.")
        if not 200 <= response.status_code < 300:
            detail = _session_error(response)
            raise IntegrationError(detail or f"GLPI retornou HTTP {response.status_code} ao iniciar sessao.")
        try:
            payload = response.json()
            token = payload.get("session_token") if isinstance(payload, dict) else None
        except ValueError:
            token = None
        if not isinstance(token, str) or not token:
            raise IntegrationError("Resposta de initSession sem session_token valido.")
        self._session_token = token
        self.http.headers["Session-Token"] = token

    async def tickets(self, *, limit=50, offset=0, idle_hours=None, statuses=None):
        validate_page(limit, offset)
        if idle_hours is not None and (type(idle_hours) not in (int, float) or idle_hours < 0 or idle_hours > 24 * 365):
            raise IntegrationError("idle_hours deve estar entre 0 e 8760.")
        wanted = tuple(statuses) if statuses is not None else self.open_statuses
        if any(type(item) is not int or item < 1 for item in wanted):
            raise IntegrationError("statuses deve conter IDs inteiros positivos.")
        try:
            response = await self.http.get("/Ticket/", params={"range": "0-999", "is_deleted": "0"})
        except httpx.RequestError:
            raise IntegrationError("Falha de rede, timeout ou TLS ao consultar tickets GLPI.") from None
        if response.status_code in (401, 403):
            raise IntegrationError("GLPI recusou a sessao ou o acesso a tickets.")
        if not 200 <= response.status_code < 300:
            raise IntegrationError(f"GLPI retornou HTTP {response.status_code} ao consultar tickets.")
        try:
            rows = response.json()
        except ValueError:
            raise IntegrationError("Resposta de tickets GLPI nao e JSON valido.") from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise IntegrationError("Formato inesperado de Ticket; validar versao/endpoint do GLPI.")
        now = datetime.now(timezone.utc)
        items = [normalize_ticket(row, now) for row in rows]
        items = [item for item in items if item["status_id"] in wanted]
        if idle_hours is not None:
            items = [item for item in items if item["idle_hours"] is not None and item["idle_hours"] >= idle_hours]
        items.sort(key=lambda item: (-item["priority_rank"], -(item["idle_hours"] or 0), str(item["id"])))
        page = items[offset:offset + limit]
        return {"source": "turbodesk_glpi", "fetched_at": now.isoformat(),
                "total_matching": len(items), "returned": len(page), "offset": offset,
                "partial": len(page) < len(items),
                "next_offset": offset + limit if offset + limit < len(items) else None,
                "items": page, "open_statuses": list(wanted),
                "idle_definition": "horas desde date_mod; aproximacao, nao equivale necessariamente a ultima interacao humana."}


def parse_statuses(value):
    try:
        result = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError:
        raise IntegrationError("GLPI_OPEN_STATUSES deve ser uma lista como 1,2,3,4.") from None
    if not result:
        raise IntegrationError("GLPI_OPEN_STATUSES nao pode ser vazio.")
    return result


def normalize_ticket(row, now):
    updated = parse_date(row.get("date_mod"))
    idle = round((now - updated).total_seconds() / 3600, 2) if updated else None
    # GLPI: priority 1..6 (low..very high); custom installations may add values.
    priority = row.get("priority") if type(row.get("priority")) is int else 0
    return {"source": "turbodesk_glpi", "id": row.get("id"),
            "title": str(row.get("name") or "")[:500], "status_id": row.get("status"),
            "priority": priority, "priority_rank": max(0, min(priority, 6)),
            "entity_id": row.get("entities_id"), "requester_id": row.get("users_id_recipient"),
            "created_at": row.get("date"), "updated_at": updated.isoformat() if updated else None,
            "idle_hours": idle, "date_needs_review": updated is None,
            "url": None}


def parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None
