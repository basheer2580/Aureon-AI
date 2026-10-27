"""Aureon backend API.

Required environment:
  SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
  AUREON_MODEL_ROUTES: JSON map keyed by model IDs in the selector. Each value:
    {"url": "https://provider.example/v1/chat/completions",
     "model": "provider-model-id", "api_key_env": "PROVIDER_API_KEY"}

Optional media environment:
  AUREON_IMAGE_ENDPOINT, AUREON_IMAGE_MODEL, AUREON_IMAGE_API_KEY_ENV
  AUREON_VIDEO_ENDPOINT, AUREON_VIDEO_MODEL, AUREON_VIDEO_API_KEY_ENV
  AUREON_SITES_DIR, AUREON_PUBLIC_BASE_URL, AUREON_CORS_ORIGINS

Authentication/admin secrets:
  AUREON_COUPON_PEPPER (32+ chars)
  AUREON_ADMIN_USERNAME, AUREON_ADMIN_PASSWORD_SALT (16+ chars),
  AUREON_ADMIN_PASSWORD_HASH (hex scrypt digest),
  AUREON_ADMIN_TOKEN_SECRET (32+ chars)
  AUREON_WEBHOOK_SECRET (32+ chars)
Optional private reporting directory:
  AUREON_COMPLIANCE_REPORT_DIR (must be outside public asset folders)

Provider image/video responses must return inline base64 in `b64_json` or
`data[0].b64_json`. Generated media is stored privately and returned with a
five-minute signed URL. The mock domain API never registers a domain or changes DNS.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logger = logging.getLogger("aureon")

APP_NAME = "Aureon — Intelligence, Beyond Limits"
SUPABASE_TIMEOUT_SECONDS = 20.0
PROVIDER_TIMEOUT_SECONDS = 180.0
MAX_PROMPT_LENGTH = 30_000
MAX_PROJECT_BYTES = 5 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_SESSION_ID_LENGTH = 120
MAX_HISTORY_MESSAGES = 500
ADMIN_TOKEN_TTL_SECONDS = 30 * 60
DEFAULT_STANDARD_PLAN = {
    "registration_credits": 5000,
    "daily_renewal_credits": 1000,
    "max_file_uploads": 15,
    "max_active_domains": 1,
    "task_credit_cost": 50,
}

MODEL_TARGETS = {
    "opus_5_5": "Opus 5.5",
    "glm_5_3_flash": "GLM-5.3-Flash",
    "nemotron_3_ultra": "Nemotron 3-Ultra",
    "gemini_3_1_pro": "Gemini 3.1 Pro",
    "fable_5_1": "Fable 5.1",
    "sonnet_5_5": "Sonnet 5.5",
    "haiku_5_5": "Haiku 5.5",
    "ltx_video": "LTX-Video",
}

# Browser adapters must pass this bounded configuration at launch.
BROWSER_ENGINE_LAUNCH_VARIABLES = {
    "infinite_loop": False,
    "max_steps": 15,
    "max_execution_steps": 15,
}

DEFAULT_SITES_DIR = Path(__file__).resolve().parent / "static" / "sites"
SITES_DIR = Path(os.getenv("AUREON_SITES_DIR", str(DEFAULT_SITES_DIR))).expanduser().resolve()
PUBLIC_BASE_URL = os.getenv("AUREON_PUBLIC_BASE_URL", "").rstrip("/")
PROJECT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)

app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    description="Streaming chat, publishing, media, and explicitly simulated domain claims.",
)

UI_DIR = Path(__file__).resolve().parent / "static" / "ui"
DOWNLOADS_DIR = Path(__file__).resolve().parent / "static" / "downloads"
DEFAULT_COMPLIANCE_REPORT_DIR = Path(__file__).resolve().parent / "private_compliance_reports"
PRIVACY_POLICY_VERSION = "v1"
ACTIVE_WEBSOCKETS: dict[str, set[WebSocket]] = {}
app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
app.mount("/downloads", StaticFiles(directory=DOWNLOADS_DIR), name="downloads")


def _cors_origins() -> list[str]:
    configured = os.getenv("AUREON_CORS_ORIGINS", "http://localhost:3000")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    origins.extend(["https://localhost", "capacitor://localhost"])
    return list(dict.fromkeys(origins))


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


class ChatMessageInput(BaseModel):
    type: str = "message"
    content: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    model: str


class RenameThreadInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class PublishedFile(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    content: str


class PublishWebsiteInput(BaseModel):
    project_name: str = Field(min_length=1, max_length=63)
    files: list[PublishedFile] = Field(min_length=1, max_length=300)


class MediaGenerationInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)


class DomainClaimInput(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    project_name: str = Field(min_length=1, max_length=63)


class AccountRegistrationInput(BaseModel):
    account_identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    coupon_code: str | None = Field(default=None, max_length=128)
    privacy_policy_accepted: bool = False


class AccountLoginInput(BaseModel):
    account_identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class AccountVerificationInput(BaseModel):
    account_identifier: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=20)


class PasswordInitializationInput(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class AdminLoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class PlanConfigInput(BaseModel):
    registration_credits: int = Field(ge=0, le=10_000_000)
    daily_renewal_credits: int = Field(ge=0, le=10_000_000)
    max_file_uploads: int = Field(ge=0, le=10_000)
    max_active_domains: int = Field(ge=0, le=10_000)
    task_credit_cost: int = Field(ge=0, le=10_000_000)


class ModelCatalogEntry(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str = Field(min_length=1, max_length=120)


class ModelCatalogChange(BaseModel):
    action: str
    model: ModelCatalogEntry | None = None
    model_id: str | None = Field(default=None, max_length=80)


class PremiumWhitelistInput(BaseModel):
    enabled: bool


class LandingTextInput(BaseModel):
    text: dict[str, str]


class CouponCreateInput(BaseModel):
    code: str = Field(min_length=20, max_length=128)


class PaymentWebhookInput(BaseModel):
    """Future provider-neutral webhook envelope; provider signatures are mandatory."""

    provider: str = Field(min_length=1, max_length=40)
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class DownloadTarget(BaseModel):
    platform: str = Field(min_length=2, max_length=40)
    label: str = Field(min_length=2, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=2048)
    detail: str = Field(default="", max_length=300)


class DownloadCatalogInput(BaseModel):
    downloads: list[DownloadTarget] = Field(max_length=12)


class BreachAlertInput(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    detected_at: datetime | None = None
    data_categories: list[
        Literal[
            "account_identifiers",
            "session_timestamps",
            "project_code_assets",
            "media_render_logs",
            "chat_history",
            "credentials",
            "other",
        ]
    ] = Field(min_length=1, max_length=7)
    affected_user_count: int | None = Field(default=None, ge=0, le=1_000_000_000)
    containment_actions: str = Field(min_length=1, max_length=4000)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Server integration is not configured: {name}",
        )
    return value


def _supabase_url() -> str:
    url = _required_env("SUPABASE_URL").rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[:-len("/rest/v1")]
    return url


def _supabase_anon_key() -> str:
    return _required_env("SUPABASE_ANON_KEY")


def _supabase_service_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


def _supabase_service_headers(content_type: str = "application/json") -> dict[str, str]:
    key = _supabase_service_key()
    headers = {"apikey": key, "Content-Type": content_type}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def _authenticated_user(access_token: str) -> dict[str, Any]:
    headers = {
        "apikey": _supabase_anon_key(),
        "Authorization": f"Bearer {access_token}",
    }
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{_supabase_url()}/auth/v1/user", headers=headers)
    except httpx.HTTPError as exc:
        logger.exception("Supabase Auth request failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        )
    try:
        user = response.json()
        if not isinstance(user, dict) or not user.get("id"):
            raise ValueError("Missing Supabase user ID")
        uuid.UUID(user["id"])
        return user
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication service returned an invalid user",
        ) from exc


async def _request_supabase_auth(
    method: str,
    path: str,
    *,
    payload: dict[str, Any],
    access_token: str | None = None,
) -> dict[str, Any]:
    headers = {"apikey": _supabase_anon_key(), "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                f"{_supabase_url()}/auth/v1/{path}",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.exception("Supabase Auth request failed")
        raise HTTPException(status_code=503, detail="Authentication service is unavailable") from exc
    if response.status_code >= 400:
        detail = "Authentication request failed"
        try:
            error = response.json()
            message = error.get("msg") or error.get("message")
            if isinstance(message, str) and message:
                detail = message[:240]
        except (ValueError, AttributeError):
            pass
        raise HTTPException(status_code=400, detail=detail)
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Authentication service returned invalid data") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Authentication service returned invalid data")
    return result


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer access token required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer access token required")
    return token


async def _get_account_profile(user_id: str) -> dict[str, Any] | None:
    rows = await _supabase_request(
        "GET",
        "account_profiles",
        params={
            "select": "user_id,password_initialized,is_premium_whitelisted,credit_balance",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned an invalid account profile")
    return rows[0] if rows else None


def _user_has_verified_identifier(user: dict[str, Any]) -> bool:
    return bool(user.get("email_confirmed_at") or user.get("phone_confirmed_at"))


async def _require_active_account(access_token: str) -> tuple[str, dict[str, Any]]:
    user = await _authenticated_user(access_token)
    user_id = str(uuid.UUID(user["id"]))
    if not _user_has_verified_identifier(user):
        raise HTTPException(status_code=403, detail="Verify your email or mobile number before continuing")
    profile = await _get_account_profile(user_id)
    if not profile or not profile.get("password_initialized"):
        raise HTTPException(
            status_code=403,
            detail="Initialize a private account password before accessing the app",
        )
    return user_id, profile


async def current_user_id(authorization: str | None = Header(default=None)) -> str:
    user_id, _ = await _require_active_account(_bearer_token(authorization))
    return user_id


async def _supabase_request(
    method: str,
    resource: str,
    *,
    params: dict[str, str] | None = None,
    payload: Any = None,
    prefer: str | None = None,
) -> Any:
    headers = _supabase_service_headers()
    if prefer:
        headers["Prefer"] = prefer

    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                f"{_supabase_url()}/rest/v1/{resource}",
                params=params,
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.exception("Supabase database request failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistent storage is unavailable",
        ) from exc

    if response.status_code >= 400:
        logger.error("Supabase REST returned HTTP %s for %s", response.status_code, resource)
        if response.status_code == 409:
            raise HTTPException(status_code=409, detail="A conflicting record already exists")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistent storage request failed",
        )
    if not response.content:
        return None
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        logger.error("Supabase REST returned non-JSON for %s", resource)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Persistent storage returned an invalid response",
        ) from exc


async def _supabase_storage_request(method: str, resource: str, payload: Any) -> Any:
    headers = _supabase_service_headers()
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                f"{_supabase_url()}/storage/v1/{resource}",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.exception("Supabase Storage request failed")
        raise HTTPException(status_code=503, detail="Media storage is unavailable") from exc
    if response.status_code >= 400:
        logger.error("Supabase Storage returned HTTP %s for %s", response.status_code, resource)
        raise HTTPException(status_code=503, detail="Media storage request failed")
    if not response.content:
        return None
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Media storage returned an invalid response") from exc


async def _list_user_media_paths(user_id: str) -> list[str]:
    root = f"{user_id}/"
    prefixes = [root]
    paths: list[str] = []
    while prefixes:
        prefix = prefixes.pop()
        offset = 0
        while True:
            rows = await _supabase_storage_request(
                "POST",
                "object/list/aureon-media",
                {"prefix": prefix, "limit": 1000, "offset": offset},
            )
            if not isinstance(rows, list):
                raise HTTPException(status_code=502, detail="Media storage returned an invalid listing")
            for item in rows:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise HTTPException(status_code=502, detail="Media storage returned an invalid listing")
                name = item["name"]
                path = f"{prefix}{name}"
                relative = PurePosixPath(path)
                if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != user_id:
                    raise HTTPException(status_code=502, detail="Media storage returned an unsafe path")
                if item.get("id") is None:
                    prefixes.append(f"{path.rstrip('/')}/")
                else:
                    paths.append(path)
            if len(rows) < 1000:
                break
            offset += len(rows)
    return paths


async def _delete_user_media(user_id: str) -> None:
    paths = await _list_user_media_paths(user_id)
    for start in range(0, len(paths), 1000):
        await _supabase_storage_request(
            "DELETE",
            "object/aureon-media",
            {"prefixes": paths[start : start + 1000]},
        )


async def _delete_supabase_auth_user(user_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT_SECONDS) as client:
            response = await client.delete(
                f"{_supabase_url()}/auth/v1/admin/users/{user_id}",
                headers=_supabase_service_headers(),
            )
    except httpx.HTTPError as exc:
        logger.exception("Supabase Auth account deletion failed")
        raise HTTPException(status_code=503, detail="Account deletion service is unavailable") from exc
    if response.status_code not in (200, 204):
        logger.error("Supabase Auth account deletion returned HTTP %s", response.status_code)
        raise HTTPException(status_code=503, detail="Account deletion could not be completed")


def _delete_user_site_files(user_id: str) -> None:
    sites_root = SITES_DIR.resolve()
    user_root = SITES_DIR / user_id
    if user_root.is_symlink() or user_root.parent.resolve() != sites_root:
        raise HTTPException(status_code=500, detail="User project directory is not safely contained")
    if user_root.exists():
        try:
            shutil.rmtree(user_root)
        except OSError as exc:
            logger.exception("Could not remove published sites for account %s", user_id)
            raise HTTPException(status_code=500, detail="Published project files could not be removed") from exc


def _model_routes() -> dict[str, dict[str, str]]:
    raw = os.getenv("AUREON_MODEL_ROUTES", "{}")
    try:
        routes = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("AUREON_MODEL_ROUTES is not valid JSON")
        raise HTTPException(status_code=503, detail="Model routing configuration is invalid") from exc
    if not isinstance(routes, dict):
        raise HTTPException(status_code=503, detail="Model routing configuration must be an object")
    return routes


def _valid_http_endpoint(value: str) -> bool:
    try:
        parsed = httpx.URL(value)
    except httpx.InvalidURL:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.host)


async def _read_system_config(key: str, default: Any = None) -> Any:
    rows = await _supabase_request(
        "GET",
        "system_config",
        params={"select": "config_value", "config_key": f"eq.{key}", "limit": "1"},
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned invalid configuration")
    return rows[0].get("config_value", default) if rows else default


async def _write_system_config(key: str, value: Any) -> None:
    await _supabase_request(
        "POST",
        "system_config",
        params={"on_conflict": "config_key"},
        payload={"config_key": key, "config_value": value, "updated_at": _now_iso()},
        prefer="resolution=merge-duplicates,return=minimal",
    )


async def _active_model_catalog() -> dict[str, dict[str, str]]:
    default_catalog = {model_id: {"label": label} for model_id, label in MODEL_TARGETS.items()}
    configured = await _read_system_config("model_catalog", default_catalog)
    if not isinstance(configured, dict):
        raise HTTPException(status_code=503, detail="Model catalog configuration is invalid")
    catalog: dict[str, dict[str, str]] = {}
    for model_id, entry in configured.items():
        if (
            isinstance(model_id, str)
            and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", model_id)
            and isinstance(entry, dict)
            and isinstance(entry.get("label"), str)
        ):
            catalog[model_id] = entry
    return catalog


def _model_route_config(target: str, spec: dict[str, str]) -> dict[str, str] | None:
    catalog_route = {
        key: spec[key]
        for key in ("url", "model", "api_key_env")
        if isinstance(spec.get(key), str)
    }
    return catalog_route if len(catalog_route) == 3 else _model_routes().get(target)


def _model_is_configured(target: str, spec: dict[str, str]) -> bool:
    route = _model_route_config(target, spec)
    if not isinstance(route, dict):
        return False
    endpoint, model, key_env = route.get("url"), route.get("model"), route.get("api_key_env")
    return (
        isinstance(endpoint, str)
        and _valid_http_endpoint(endpoint)
        and isinstance(model, str)
        and bool(model.strip())
        and isinstance(key_env, str)
        and bool(key_env.strip())
        and bool(os.getenv(key_env))
    )


async def _model_route(target: str) -> tuple[str, str, str]:
    catalog = await _active_model_catalog()
    spec = catalog.get(target)
    if not spec:
        raise HTTPException(status_code=400, detail="Unsupported model target")
    route = _model_route_config(target, spec)
    if not isinstance(route, dict):
        raise HTTPException(status_code=503, detail=f"Model target is not configured: {spec['label']}")
    endpoint = route.get("url")
    model = route.get("model")
    key_env = route.get("api_key_env")
    if not all(isinstance(value, str) and value.strip() for value in (endpoint, model, key_env)):
        raise HTTPException(status_code=503, detail=f"Model target configuration is incomplete: {target}")
    if not _valid_http_endpoint(endpoint):
        raise HTTPException(status_code=503, detail=f"Model target endpoint is invalid: {target}")
    api_key = os.getenv(key_env)
    if not api_key:
        raise HTTPException(status_code=503, detail=f"Provider credential is not configured: {key_env}")
    return endpoint, model, api_key


def _validate_session_id(session_id: str) -> str:
    if not session_id.strip() or len(session_id) > MAX_SESSION_ID_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    return session_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _touch_thread(user_id: str, session_id: str, first_prompt: str) -> None:
    await _supabase_request(
        "POST",
        "chat_threads",
        params={"on_conflict": "user_id,session_id"},
        payload={
            "user_id": user_id,
            "session_id": session_id,
            "title": first_prompt.strip().replace("\n", " ")[:80] or "New chat",
            "last_message_at": _now_iso(),
        },
        prefer="resolution=ignore-duplicates,return=minimal",
    )
    await _supabase_request(
        "PATCH",
        "chat_threads",
        params={"user_id": f"eq.{user_id}", "session_id": f"eq.{session_id}"},
        payload={"last_message_at": _now_iso(), "updated_at": _now_iso()},
        prefer="return=minimal",
    )


async def _save_message(user_id: str, session_id: str, role: str, content: str) -> None:
    await _supabase_request(
        "POST",
        "chat_messages",
        payload={
            "user_id": user_id,
            "session_id": session_id,
            "role": role,
            "content": content,
        },
        prefer="return=minimal",
    )


async def _create_assistant_message(user_id: str, session_id: str) -> str:
    rows = await _supabase_request(
        "POST",
        "chat_messages",
        payload={
            "user_id": user_id,
            "session_id": session_id,
            "role": "assistant",
            "content": "",
            "status": "streaming",
        },
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows or "id" not in rows[0]:
        raise HTTPException(status_code=502, detail="Could not initialize the response history record")
    return str(rows[0]["id"])


async def _update_assistant_message(
    user_id: str,
    session_id: str,
    message_id: str,
    content: str,
    message_status: str,
) -> None:
    await _supabase_request(
        "PATCH",
        "chat_messages",
        params={
            "id": f"eq.{message_id}",
            "user_id": f"eq.{user_id}",
            "session_id": f"eq.{session_id}",
            "role": "eq.assistant",
        },
        payload={"content": content, "status": message_status},
        prefer="return=minimal",
    )


def _sse_delta(data: str) -> str | None:
    if data.strip() == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Model provider sent invalid stream data") from exc
    if isinstance(event, dict) and event.get("error"):
        raise HTTPException(status_code=502, detail="Model provider returned a streaming error")
    choices = event.get("choices", []) if isinstance(event, dict) else []
    if not choices:
        return ""
    if not isinstance(choices[0], dict):
        raise HTTPException(status_code=502, detail="Model provider sent invalid stream data")
    delta = choices[0].get("delta", {})
    if not isinstance(delta, dict):
        raise HTTPException(status_code=502, detail="Model provider sent invalid stream data")
    content = delta.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


async def _stream_model(
    endpoint: str,
    provider_model: str,
    api_key: str,
    prompt: str,
):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": provider_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_SECONDS) as client:
        async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                logger.error("Model provider returned HTTP %s", response.status_code)
                raise HTTPException(status_code=502, detail="Model provider request failed")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                delta = _sse_delta(data)
                if delta:
                    yield delta


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
@app.get("/landing", include_in_schema=False)
@app.get("/app", include_in_schema=False)
async def frontend_entry() -> FileResponse:
    return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def pwa_manifest() -> FileResponse:
    return FileResponse(
        UI_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker() -> FileResponse:
    return FileResponse(
        UI_DIR / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/api/downloads")
async def download_catalog() -> dict[str, Any]:
    configured = await _read_system_config("app_downloads", [])
    if not isinstance(configured, list):
        raise HTTPException(status_code=503, detail="Download catalog configuration is invalid")
    return {"downloads": configured}


@app.get("/api/public/landing")
async def public_landing_content() -> dict[str, Any]:
    landing = await _read_system_config("landing_text", {})
    if not isinstance(landing, dict):
        raise HTTPException(status_code=503, detail="Landing content configuration is invalid")
    return {"landing_text": landing}


@app.get("/api/domains")
async def list_user_domains(user_id: str = Depends(current_user_id)) -> list[dict[str, Any]]:
    rows = await _supabase_request(
        "GET",
        "domain_claims",
        params={
            "select": "domain,project_name,site_path,active,created_at",
            "user_id": f"eq.{user_id}",
            "active": "eq.true",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned an invalid domain list")
    return rows


def _normalize_account_identifier(identifier: str) -> tuple[str, str]:
    value = identifier.strip()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "email", value.lower()
    if re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        return "phone", value
    raise HTTPException(
        status_code=422,
        detail="Use a valid email address or mobile number in E.164 format",
    )


def _validate_password_strength(password: str) -> None:
    if len(password) < 12:
        raise HTTPException(
            status_code=422,
            detail="Password must be at least 12 characters",
        )


async def _standard_plan() -> dict[str, int]:
    value = await _read_system_config("standard_plan", DEFAULT_STANDARD_PLAN)
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Standard plan configuration is invalid")
    result: dict[str, int] = {}
    for key, default in DEFAULT_STANDARD_PLAN.items():
        item = value.get(key, default)
        if not isinstance(item, int) or item < 0:
            raise HTTPException(status_code=503, detail="Standard plan configuration is invalid")
        result[key] = item
    return result


async def _ensure_account_profile(
    user_id: str,
    *,
    password_initialized: bool,
    registration_credits: int,
    privacy_policy_accepted_at: str | None = None,
) -> None:
    profile = {
        "user_id": user_id,
        "password_initialized": password_initialized,
        "is_premium_whitelisted": False,
        "credit_balance": registration_credits,
    }
    if privacy_policy_accepted_at:
        profile.update(
            {
                "privacy_policy_accepted_at": privacy_policy_accepted_at,
                "privacy_policy_version": PRIVACY_POLICY_VERSION,
            }
        )
    await _supabase_request(
        "POST",
        "account_profiles",
        params={"on_conflict": "user_id"},
        payload=profile,
        prefer="resolution=ignore-duplicates,return=minimal",
    )
    if password_initialized:
        await _supabase_request(
            "PATCH",
            "account_profiles",
            params={"user_id": f"eq.{user_id}"},
            payload={"password_initialized": True},
            prefer="return=minimal",
        )


def _coupon_digest(code: str) -> str:
    pepper = _required_env("AUREON_COUPON_PEPPER")
    if len(pepper) < 32:
        raise HTTPException(status_code=503, detail="Coupon security is not configured securely")
    return hmac.new(pepper.encode("utf-8"), code.strip().encode("utf-8"), hashlib.sha256).hexdigest()


async def _assert_coupon_available(code: str) -> str:
    digest = _coupon_digest(code)
    rows = await _supabase_request(
        "GET",
        "premium_coupons",
        params={
            "select": "coupon_hash",
            "coupon_hash": f"eq.{digest}",
            "redeemed_at": "is.null",
            "limit": "1",
        },
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="Coupon code is invalid or already redeemed")
    return digest


async def _redeem_pending_coupon(user_id: str) -> bool:
    rows = await _supabase_request(
        "GET",
        "pending_coupon_redemptions",
        params={
            "select": "coupon_hash",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned invalid coupon data")
    if not rows:
        return False
    redeemed = await _supabase_request(
        "POST",
        "rpc/redeem_premium_coupon",
        payload={"p_user_id": user_id, "p_coupon_hash": rows[0]["coupon_hash"]},
    )
    await _supabase_request(
        "DELETE",
        "pending_coupon_redemptions",
        params={"user_id": f"eq.{user_id}"},
        prefer="return=minimal",
    )
    return redeemed is True


@app.post("/api/auth/register", status_code=201)
async def register_account(body: AccountRegistrationInput) -> dict[str, Any]:
    if not body.privacy_policy_accepted:
        raise HTTPException(
            status_code=400,
            detail="You must accept the privacy framework to register an account.",
        )
    kind, identifier = _normalize_account_identifier(body.account_identifier)
    _validate_password_strength(body.password)
    coupon_digest = None
    if body.coupon_code and body.coupon_code.strip():
        coupon_digest = await _assert_coupon_available(body.coupon_code)

    auth_result = await _request_supabase_auth(
        "POST",
        "signup",
        payload={kind: identifier, "password": body.password},
    )
    user = auth_result.get("user")
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=502, detail="Authentication service did not create an account")
    try:
        user_id = str(uuid.UUID(user["id"]))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Authentication service returned an invalid account") from exc

    plan = await _standard_plan()
    await _ensure_account_profile(
        user_id,
        password_initialized=True,
        registration_credits=plan["registration_credits"],
        privacy_policy_accepted_at=_now_iso(),
    )
    if coupon_digest:
        await _supabase_request(
            "POST",
            "pending_coupon_redemptions",
            payload={"user_id": user_id, "coupon_hash": coupon_digest},
            prefer="return=minimal",
        )

    verified = bool(user.get("email_confirmed_at") or user.get("phone_confirmed_at"))
    access_token = auth_result.get("access_token")
    if access_token:
        verified = _user_has_verified_identifier(await _authenticated_user(access_token))
    is_premium = await _redeem_pending_coupon(user_id) if verified and coupon_digest else False
    return {
        "user_id": user_id,
        "verification_required": not verified,
        "is_premium_whitelisted": is_premium,
        "message": "Verify your email or mobile number before signing in" if not verified else "Account created",
    }


@app.get("/api/auth/github")
async def github_oauth(
    privacy_policy_accepted: bool = Query(default=False),
) -> RedirectResponse:
    if not privacy_policy_accepted:
        raise HTTPException(
            status_code=400,
            detail="You must accept the privacy framework to register an account.",
        )
    redirect_uri = f"{PUBLIC_BASE_URL}/app" if PUBLIC_BASE_URL else ""
    if not redirect_uri:
        raise HTTPException(status_code=503, detail="Set AUREON_PUBLIC_BASE_URL before enabling GitHub sign-in")
    query = urlencode(
        {
            "provider": "github",
            "redirect_to": redirect_uri,
            "apikey": _supabase_anon_key(),
        }
    )
    return RedirectResponse(
        f"{_supabase_url()}/auth/v1/authorize?{query}",
        status_code=302,
    )


@app.post("/api/auth/verify")
async def verify_account(body: AccountVerificationInput) -> dict[str, Any]:
    kind, identifier = _normalize_account_identifier(body.account_identifier)
    verify_type = "email" if kind == "email" else "sms"
    result = await _request_supabase_auth(
        "POST",
        "verify",
        payload={verify_type: identifier, "token": body.code, "type": "signup"},
    )
    access_token = result.get("access_token")
    user = result.get("user")
    if access_token:
        user = await _authenticated_user(access_token)
    if not isinstance(user, dict) or not _user_has_verified_identifier(user):
        raise HTTPException(status_code=403, detail="Account verification was not confirmed")
    user_id = str(uuid.UUID(user["id"]))
    plan = await _standard_plan()
    await _ensure_account_profile(
        user_id,
        password_initialized=True,
        registration_credits=plan["registration_credits"],
    )
    premium = await _redeem_pending_coupon(user_id)
    return {
        "user_id": user_id,
        "verified": True,
        "is_premium_whitelisted": premium,
        "access_token": access_token,
        "token_type": result.get("token_type", "bearer"),
    }


@app.post("/api/auth/login")
async def login_account(body: AccountLoginInput) -> dict[str, Any]:
    kind, identifier = _normalize_account_identifier(body.account_identifier)
    result = await _request_supabase_auth(
        "POST",
        "token?grant_type=password",
        payload={kind: identifier, "password": body.password},
    )
    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=401, detail="Invalid account identifier or password")
    user_id, profile = await _require_active_account(access_token)
    return {
        "access_token": access_token,
        "refresh_token": result.get("refresh_token"),
        "token_type": result.get("token_type", "bearer"),
        "expires_in": result.get("expires_in"),
        "user_id": user_id,
        "is_premium_whitelisted": bool(profile.get("is_premium_whitelisted")),
    }


@app.post("/api/auth/password/initialize")
async def initialize_oauth_password(
    body: PasswordInitializationInput,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _bearer_token(authorization)
    user = await _authenticated_user(token)
    if not _user_has_verified_identifier(user):
        raise HTTPException(status_code=403, detail="Verify your email or mobile number before setting a password")
    _validate_password_strength(body.password)
    await _request_supabase_auth(
        "PUT",
        "user",
        payload={"password": body.password},
        access_token=token,
    )
    user_id = str(uuid.UUID(user["id"]))
    plan = await _standard_plan()
    await _ensure_account_profile(
        user_id,
        password_initialized=True,
        registration_credits=plan["registration_credits"],
    )
    return {"user_id": user_id, "password_initialized": True}


@app.delete("/api/user/delete-account")
async def delete_account(user_id: str = Depends(current_user_id)) -> dict[str, str]:
    _delete_user_site_files(user_id)
    await _delete_user_media(user_id)
    await _delete_supabase_auth_user(user_id)
    connections = tuple(ACTIVE_WEBSOCKETS.pop(user_id, set()))
    for connection in connections:
        try:
            await connection.close(code=4403, reason="Account deleted")
        except (RuntimeError, WebSocketDisconnect):
            logger.info("A websocket for deleted account %s was already closed", user_id)
    return {
        "status": "deleted",
        "message": "Account and associated profile, history, domain, and usage records were deleted.",
        "cache_cleanup": "No Redis or external session cache is configured.",
    }


@app.get("/api/account/metrics")
async def account_metrics(
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    plan = await _standard_plan()
    await _supabase_request(
        "POST",
        "rpc/renew_daily_credits",
        payload={"p_user_id": user_id},
    )
    profile = await _get_account_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Account profile not found")
    return {"plan": plan, "account": profile}


@app.post("/api/account/privacy-consent", status_code=204)
async def record_privacy_consent(
    user_id: str = Depends(current_user_id),
) -> None:
    rows = await _supabase_request(
        "PATCH",
        "account_profiles",
        params={"user_id": f"eq.{user_id}"},
        payload={
            "privacy_policy_accepted_at": _now_iso(),
            "privacy_policy_version": PRIVACY_POLICY_VERSION,
        },
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Account profile not found")


@app.post("/api/credits/deduct-task")
async def deduct_agent_task_credits(
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    result = await _supabase_request(
        "POST",
        "rpc/deduct_task_credits",
        payload={"p_user_id": user_id},
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Credit service returned invalid data")
    if result.get("error") == "insufficient_credits":
        raise HTTPException(status_code=402, detail="Insufficient credits to run this task")
    if result.get("error") == "account_not_found":
        raise HTTPException(status_code=404, detail="Account profile not found")
    return result


def _admin_token_secret() -> bytes:
    secret = _required_env("AUREON_ADMIN_TOKEN_SECRET").encode("utf-8")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Admin token signing secret must be at least 32 bytes")
    return secret


def _make_admin_token(username: str) -> str:
    now = int(time.time())
    payload = json.dumps(
        {"sub": username, "iat": now, "exp": now + ADMIN_TOKEN_TTL_SECONDS, "nonce": secrets.token_hex(12)},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(_admin_token_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify_admin_token(token: str) -> bool:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_admin_token_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(payload_bytes)
        configured_user = _required_env("AUREON_ADMIN_USERNAME")
        return (
            isinstance(payload, dict)
            and payload.get("sub") == configured_user
            and isinstance(payload.get("exp"), int)
            and payload["exp"] > int(time.time())
        )
    except (ValueError, TypeError, json.JSONDecodeError, HTTPException):
        return False


async def require_admin(authorization: str | None = Header(default=None)) -> str:
    token = _bearer_token(authorization)
    if not _verify_admin_token(token):
        raise HTTPException(status_code=401, detail="Valid administrator token required")
    return _required_env("AUREON_ADMIN_USERNAME")


@app.post("/api/admin/login")
async def admin_login(body: AdminLoginInput) -> dict[str, Any]:
    username = _required_env("AUREON_ADMIN_USERNAME")
    password_hash = _required_env("AUREON_ADMIN_PASSWORD_HASH").lower()
    salt = _required_env("AUREON_ADMIN_PASSWORD_SALT").encode("utf-8")
    if len(salt) < 16 or not re.fullmatch(r"[0-9a-f]{64}", password_hash):
        raise HTTPException(status_code=503, detail="Admin password hash configuration is invalid")
    allowed = await _supabase_request(
        "POST",
        "rpc/admin_login_allowed",
        payload={"p_username": username},
    )
    if allowed is not True:
        raise HTTPException(status_code=429, detail="Administrator login temporarily locked")
    submitted_hash = hashlib.scrypt(
        body.password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()
    if not hmac.compare_digest(body.username, username) or not hmac.compare_digest(
        submitted_hash, password_hash
    ):
        await _supabase_request(
            "POST",
            "rpc/record_admin_login",
            payload={"p_username": username, "p_success": False},
        )
        raise HTTPException(status_code=401, detail="Invalid administrator credentials")
    await _supabase_request(
        "POST",
        "rpc/record_admin_login",
        payload={"p_username": username, "p_success": True},
    )
    return {"access_token": _make_admin_token(username), "token_type": "bearer", "expires_in": ADMIN_TOKEN_TTL_SECONDS}


@app.get("/api/models")
async def models(_: str = Depends(current_user_id)) -> dict[str, Any]:
    catalog = await _active_model_catalog()
    return {
        "models": [
            {
                "id": model_id,
                "label": spec["label"],
                "configured": _model_is_configured(model_id, spec),
            }
            for model_id, spec in catalog.items()
        ],
        "browser_use": BROWSER_ENGINE_LAUNCH_VARIABLES,
    }


@app.get("/api/config")
async def public_configuration(
    _: str = Depends(current_user_id),
) -> dict[str, Any]:
    plan = await _standard_plan()
    landing = await _read_system_config("landing_text", {})
    if not isinstance(landing, dict):
        raise HTTPException(status_code=503, detail="Landing text configuration is invalid")
    return {"standard_plan": plan, "landing_text": landing}


@app.get("/api/admin/config")
async def get_admin_configuration(
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "standard_plan": await _standard_plan(),
        "landing_text": await _read_system_config("landing_text", {}),
        "model_catalog": await _active_model_catalog(),
    }


@app.patch("/api/admin/config")
async def update_admin_configuration(
    body: PlanConfigInput,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    plan = body.model_dump()
    await _write_system_config("standard_plan", plan)
    return {"standard_plan": plan}


@app.post("/api/admin/models")
async def change_model_catalog(
    body: ModelCatalogChange,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    catalog = await _active_model_catalog()
    if body.action == "upsert":
        if body.model is None:
            raise HTTPException(status_code=422, detail="model is required for upsert")
        entry = body.model
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", entry.id):
            raise HTTPException(status_code=422, detail="Model ID may contain only letters, digits, _ and -")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,119}", entry.api_key_env):
            raise HTTPException(status_code=422, detail="api_key_env must be an environment variable name")
        if not _valid_http_endpoint(entry.url):
            raise HTTPException(status_code=422, detail="Model URL must be an absolute HTTP(S) endpoint")
        catalog[entry.id] = {
            "label": entry.label.strip(),
            "url": entry.url,
            "model": entry.model.strip(),
            "api_key_env": entry.api_key_env,
        }
    elif body.action == "remove":
        if not body.model_id or body.model_id not in catalog:
            raise HTTPException(status_code=404, detail="Model target not found")
        catalog.pop(body.model_id)
    else:
        raise HTTPException(status_code=422, detail="action must be upsert or remove")
    await _write_system_config("model_catalog", catalog)
    return {"models": catalog}


@app.patch("/api/admin/users/{user_id}/premium")
async def set_premium_whitelist(
    user_id: str,
    body: PremiumWhitelistInput,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        normalized_user_id = str(uuid.UUID(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid user ID") from exc
    rows = await _supabase_request(
        "PATCH",
        "account_profiles",
        params={"user_id": f"eq.{normalized_user_id}"},
        payload={"is_premium_whitelisted": body.enabled},
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Account profile not found")
    return {
        "user_id": normalized_user_id,
        "is_premium_whitelisted": body.enabled,
    }


@app.patch("/api/admin/landing-text")
async def update_landing_text(
    body: LandingTextInput,
    _: str = Depends(require_admin),
) -> dict[str, dict[str, str]]:
    if len(body.text) > 30:
        raise HTTPException(status_code=422, detail="At most 30 landing text fields are allowed")
    cleaned: dict[str, str] = {}
    for key, value in body.text.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", key) or len(value) > 2000:
            raise HTTPException(status_code=422, detail=f"Invalid landing text field: {key}")
        cleaned[key] = value
    await _write_system_config("landing_text", cleaned)
    return {"landing_text": cleaned}


@app.post("/api/admin/downloads")
async def update_download_catalog(
    body: DownloadCatalogInput,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    platforms: set[str] = set()
    downloads: list[dict[str, str]] = []
    for target in body.downloads:
        platform = target.platform.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{2,40}", platform) or platform in platforms:
            raise HTTPException(status_code=422, detail="Download platforms must be unique URL-safe identifiers")
        local_path = (
            target.url.startswith("/")
            and not target.url.startswith("//")
            and "\\" not in target.url
            and not any(part == ".." for part in target.url.split("/"))
        )
        remote_https = _valid_http_endpoint(target.url) and httpx.URL(target.url).scheme == "https"
        if not (local_path or remote_https):
            raise HTTPException(status_code=422, detail="Download URLs must be HTTPS or same-origin paths")
        platforms.add(platform)
        downloads.append(
            {
                "platform": platform,
                "label": target.label.strip(),
                "version": target.version.strip(),
                "url": target.url,
                "detail": target.detail.strip(),
            }
        )
    await _write_system_config("app_downloads", downloads)
    return {"downloads": downloads}


@app.post("/api/admin/coupons", status_code=201)
async def create_premium_coupon(
    body: CouponCreateInput,
    _: str = Depends(require_admin),
) -> dict[str, str]:
    digest = _coupon_digest(body.code)
    await _supabase_request(
        "POST",
        "premium_coupons",
        payload={"coupon_hash": digest},
        prefer="return=minimal",
    )
    return {"status": "created", "redemption": "single-use"}


@app.post("/api/payments/webhook", status_code=202)
async def payments_webhook(
    request: Request,
    x_aureon_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    secret = _required_env("AUREON_WEBHOOK_SECRET").encode("utf-8")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Payment webhook secret must be at least 32 bytes")
    if not x_aureon_signature or not re.fullmatch(r"[0-9a-fA-F]{64}", x_aureon_signature):
        raise HTTPException(status_code=401, detail="Valid webhook signature required")
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payment webhook payload is too large")
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_aureon_signature.lower()):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        event = PaymentWebhookInput.model_validate_json(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid payment webhook payload") from exc
    return {
        "accepted": True,
        "processed": False,
        "provider": event.provider,
        "event_id": event.event_id,
        "notice": "Signature-verified webhook scaffold; no subscription or credit changes are applied.",
    }


@app.post("/api/admin/breach-alert", status_code=201)
async def create_breach_alert(
    body: BreachAlertInput,
    admin_username: str = Depends(require_admin),
) -> dict[str, Any]:
    detected_at = body.detected_at or datetime.now(timezone.utc)
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    incident_id = str(uuid.uuid4())
    report = "\n".join(
        [
            "AUREON SECURITY INCIDENT REPORT — DRAFT",
            f"Incident ID: {incident_id}",
            f"Prepared at (UTC): {_now_iso()}",
            f"Detected at: {detected_at.astimezone(timezone.utc).isoformat()}",
            f"Reported by: {admin_username}",
            f"Affected user count: {body.affected_user_count if body.affected_user_count is not None else 'Not yet determined'}",
            f"Data categories: {', '.join(body.data_categories)}",
            "",
            "Incident summary:",
            body.summary.strip(),
            "",
            "Containment actions:",
            body.containment_actions.strip(),
            "",
            "Distribution: Internal draft only. Confirm applicable jurisdiction, recipient, facts, and legal reporting deadlines before external submission.",
        ]
    )
    report_dir = Path(
        os.getenv("AUREON_COMPLIANCE_REPORT_DIR", str(DEFAULT_COMPLIANCE_REPORT_DIR))
    ).expanduser().resolve()
    public_roots = (UI_DIR.resolve(), DOWNLOADS_DIR.resolve(), SITES_DIR.resolve())
    if any(report_dir == root or root in report_dir.parents for root in public_roots):
        raise HTTPException(status_code=503, detail="Compliance report directory must be outside public asset folders")
    report_path = report_dir / f"breach-{incident_id}.txt"
    temporary_path: Path | None = None
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report_dir,
            prefix=f".breach-{incident_id}-",
            suffix=".tmp",
            delete=False,
        ) as report_file:
            temporary_path = Path(report_file.name)
            report_file.write(report)
            report_file.flush()
            os.fsync(report_file.fileno())
        os.replace(temporary_path, report_path)
    except OSError as exc:
        logger.exception("Could not write private breach report")
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove temporary breach report")
        raise HTTPException(status_code=500, detail="Could not create the private breach report") from exc

    try:
        await _supabase_request(
            "POST",
            "security_incidents",
            payload={
                "id": incident_id,
                "created_by": admin_username,
                "detected_at": detected_at.astimezone(timezone.utc).isoformat(),
                "summary": body.summary.strip(),
                "data_categories": body.data_categories,
                "affected_user_count": body.affected_user_count,
                "containment_actions": body.containment_actions.strip(),
                "report_path": str(report_path),
            },
            prefer="return=minimal",
        )
    except HTTPException:
        try:
            report_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove unrecorded private breach report %s", report_path.name)
        raise
    return {
        "incident_id": incident_id,
        "status": "recorded",
        "report": report,
        "report_file": report_path.name,
        "external_notifications_sent": False,
        "notice": (
            "This endpoint records an internal draft only. It does not mass-email users or submit "
            "a notice to a regulator; configure an approved notification provider and verify the "
            "applicable jurisdiction and recipient before external dispatch."
        ),
    }


@app.websocket("/ws/chat/{session_id}")
async def chat_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    token = websocket.query_params.get("access_token")
    if not token:
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        await websocket.close(code=4401, reason="Bearer access token required")
        return
    try:
        user_id, _ = await _require_active_account(token)
        _validate_session_id(session_id)
    except HTTPException as exc:
        await websocket.close(
            code=4403 if exc.status_code == 403 else 4401,
            reason=str(exc.detail),
        )
        return

    user_connections = ACTIVE_WEBSOCKETS.setdefault(user_id, set())
    user_connections.add(websocket)
    try:
        while True:
            try:
                raw_message = await websocket.receive_json()
                message = ChatMessageInput.model_validate(raw_message)
            except (ValueError, TypeError):
                await websocket.send_json({"type": "error", "detail": "Expected JSON with content and model"})
                continue

            assistant_message_id: str | None = None
            output_parts: list[str] = []
            try:
                endpoint, provider_model, api_key = await _model_route(message.model)
                await _touch_thread(user_id, session_id, message.content)
                await _save_message(user_id, session_id, "user", message.content)
                assistant_message_id = await _create_assistant_message(user_id, session_id)
                await websocket.send_json({"type": "start", "model": message.model})
                last_persisted_at = time.monotonic()
                async for token_text in _stream_model(
                    endpoint, provider_model, api_key, message.content
                ):
                    output_parts.append(token_text)
                    await websocket.send_json({"type": "token", "content": token_text})
                    now = time.monotonic()
                    if now - last_persisted_at >= 0.5:
                        await _update_assistant_message(
                            user_id,
                            session_id,
                            assistant_message_id,
                            "".join(output_parts),
                            "streaming",
                        )
                        last_persisted_at = now
                await _update_assistant_message(
                    user_id,
                    session_id,
                    assistant_message_id,
                    "".join(output_parts),
                    "complete",
                )
                await websocket.send_json({"type": "done"})
            except HTTPException as exc:
                if assistant_message_id is not None:
                    try:
                        await _update_assistant_message(
                            user_id,
                            session_id,
                            assistant_message_id,
                            "".join(output_parts),
                            "failed",
                        )
                    except HTTPException:
                        logger.exception("Could not persist the failed assistant response")
                await websocket.send_json({"type": "error", "detail": exc.detail})
            except httpx.HTTPError:
                logger.exception("Model provider connection failed")
                if assistant_message_id is not None:
                    try:
                        await _update_assistant_message(
                            user_id,
                            session_id,
                            assistant_message_id,
                            "".join(output_parts),
                            "failed",
                        )
                    except HTTPException:
                        logger.exception("Could not persist the failed assistant response")
                await websocket.send_json(
                    {"type": "error", "detail": "Model provider is unavailable"}
                )
            except WebSocketDisconnect:
                if assistant_message_id is not None:
                    try:
                        await _update_assistant_message(
                            user_id,
                            session_id,
                            assistant_message_id,
                            "".join(output_parts),
                            "failed",
                        )
                    except HTTPException:
                        logger.exception("Could not persist the disconnected assistant response")
                raise
    except WebSocketDisconnect:
        return
    finally:
        user_connections.discard(websocket)
        if not user_connections:
            ACTIVE_WEBSOCKETS.pop(user_id, None)


@app.get("/api/history")
async def list_history(
    user_id: str = Depends(current_user_id),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    rows = await _supabase_request(
        "GET",
        "chat_threads",
        params={
            "select": "session_id,title,created_at,updated_at,last_message_at",
            "user_id": f"eq.{user_id}",
            "order": "last_message_at.desc.nullslast,updated_at.desc",
            "limit": str(limit),
        },
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned an invalid thread list")
    return rows


@app.get("/api/history/{session_id}")
async def get_history(
    session_id: str,
    user_id: str = Depends(current_user_id),
    limit: int = Query(default=MAX_HISTORY_MESSAGES, ge=1, le=MAX_HISTORY_MESSAGES),
) -> list[dict[str, Any]]:
    _validate_session_id(session_id)
    rows = await _supabase_request(
        "GET",
        "chat_messages",
        params={
            "select": "id,role,content,status,created_at",
            "user_id": f"eq.{user_id}",
            "session_id": f"eq.{session_id}",
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned an invalid message list")
    return rows


@app.patch("/api/history/{session_id}")
async def rename_history(
    session_id: str,
    body: RenameThreadInput,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    _validate_session_id(session_id)
    rows = await _supabase_request(
        "PATCH",
        "chat_threads",
        params={"user_id": f"eq.{user_id}", "session_id": f"eq.{session_id}"},
        payload={"title": body.title.strip(), "updated_at": _now_iso()},
        prefer="return=representation",
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    return {"session_id": session_id, "title": body.title.strip()}


def _safe_project_name(project_name: str) -> str:
    normalized = project_name.strip().lower()
    if not PROJECT_NAME_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="project_name must be a valid URL slug")
    return normalized


def _safe_relative_file_path(raw_path: str) -> PurePosixPath:
    if "\\" in raw_path or "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="File paths must use safe relative POSIX paths")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise HTTPException(status_code=400, detail="File paths must stay inside the project")
    if any(":" in part for part in path.parts):
        raise HTTPException(status_code=400, detail="Invalid file path")
    return path


def _write_published_files(user_id: str, project_name: str, files: list[PublishedFile]) -> None:
    project_root = SITES_DIR / user_id
    project_root.mkdir(parents=True, exist_ok=True)
    if project_root.is_symlink() or not project_root.resolve().is_relative_to(SITES_DIR):
        raise HTTPException(status_code=400, detail="Invalid publishing directory")

    destination = project_root / project_name
    if destination.is_symlink():
        raise HTTPException(status_code=400, detail="Invalid publishing destination")
    staging = project_root / f".{project_name}.{uuid.uuid4().hex}.staging"
    backup = project_root / f".{project_name}.{uuid.uuid4().hex}.backup"
    total_bytes = 0
    seen_paths: set[str] = set()

    try:
        staging.mkdir()
        for item in files:
            relative_path = _safe_relative_file_path(item.path)
            path_key = relative_path.as_posix()
            if path_key in seen_paths:
                raise HTTPException(status_code=400, detail=f"Duplicate file path: {path_key}")
            seen_paths.add(path_key)
            encoded = item.content.encode("utf-8")
            total_bytes += len(encoded)
            if total_bytes > MAX_PROJECT_BYTES:
                raise HTTPException(status_code=413, detail="Project exceeds the 5 MiB size limit")
            target = staging.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        if not (staging / "index.html").is_file():
            raise HTTPException(status_code=400, detail="Published projects must include index.html")

        had_destination = destination.exists()
        if had_destination:
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except OSError:
            if had_destination and backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                logger.exception("Could not remove the previous published site backup")
    except HTTPException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    except OSError as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        logger.exception("Website publication failed")
        raise HTTPException(status_code=500, detail="Website publication failed") from exc


@app.post("/api/publish-website", status_code=201)
async def publish_website(
    body: PublishWebsiteInput,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    project_name = _safe_project_name(body.project_name)
    profile = await _get_account_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Account profile not found")
    if not profile.get("is_premium_whitelisted"):
        plan = await _standard_plan()
        if len(body.files) > plan["max_file_uploads"]:
            raise HTTPException(
                status_code=403,
                detail=f"Standard plan allows at most {plan['max_file_uploads']} files per upload",
            )
    _write_published_files(user_id, project_name, body.files)
    path = f"/sites/{user_id}/{project_name}"
    return {
        "project_name": project_name,
        "path": path,
        "url": f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path,
    }


@app.get("/sites/{user_id}/{project_name}", include_in_schema=False)
@app.get("/sites/{user_id}/{project_name}/{file_path:path}", include_in_schema=False)
async def serve_published_site(
    user_id: str,
    project_name: str,
    file_path: str = "",
) -> FileResponse:
    try:
        safe_user_id = str(uuid.UUID(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Site not found") from exc
    safe_project = _safe_project_name(project_name)
    user_root = SITES_DIR / safe_user_id
    unresolved_project_root = user_root / safe_project
    if user_root.is_symlink() or unresolved_project_root.is_symlink():
        raise HTTPException(status_code=404, detail="Site not found")
    project_root = unresolved_project_root.resolve()
    if not project_root.is_relative_to(SITES_DIR):
        raise HTTPException(status_code=404, detail="Site not found")
    if not file_path:
        relative = PurePosixPath("index.html")
    else:
        relative = _safe_relative_file_path(file_path)
    target = project_root.joinpath(*relative.parts)
    resolved_target = target.resolve()
    if (
        not target.is_relative_to(project_root)
        or not resolved_target.is_relative_to(project_root)
        or target.is_symlink()
    ):
        raise HTTPException(status_code=404, detail="File not found")
    if target.is_dir() and "." not in PurePosixPath(file_path).name:
        target = target / "index.html"
    if not target.is_file():
        if not PurePosixPath(file_path).suffix:
            target = project_root / "index.html"
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        target,
        headers={
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def _generate_and_upload_media(
    user_id: str,
    prompt: str,
    *,
    media_kind: str,
) -> dict[str, str]:
    prefix = f"AUREON_{media_kind.upper()}"
    endpoint = _required_env(f"{prefix}_ENDPOINT")
    model = _required_env(f"{prefix}_MODEL")
    key_env = _required_env(f"{prefix}_API_KEY_ENV")
    api_key = _required_env(key_env)
    if not _valid_http_endpoint(endpoint):
        raise HTTPException(status_code=503, detail=f"{media_kind.title()} provider endpoint is invalid")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, str] = {"model": model, "prompt": prompt}
    if media_kind == "image":
        payload["response_format"] = "b64_json"

    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_SECONDS) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.exception("%s provider request failed", media_kind)
        raise HTTPException(status_code=502, detail=f"{media_kind.title()} provider is unavailable") from exc
    if response.status_code >= 400:
        logger.error("%s provider returned HTTP %s", media_kind, response.status_code)
        raise HTTPException(status_code=502, detail=f"{media_kind.title()} generation failed")
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid media response")
        encoded = result.get("b64_json")
        if (
            not encoded
            and isinstance(result.get("data"), list)
            and result["data"]
            and isinstance(result["data"][0], dict)
        ):
            encoded = result["data"][0].get("b64_json")
        if not isinstance(encoded, str):
            raise ValueError("No inline base64 media payload")
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{media_kind.title()} provider returned an unsupported response",
        ) from exc
    if not content or len(content) > MAX_MEDIA_BYTES:
        raise HTTPException(status_code=502, detail="Generated media is empty or exceeds 50 MiB")

    content_type = "image/png" if media_kind == "image" else "video/mp4"
    extension = "png" if media_kind == "image" else "mp4"
    object_path = f"{user_id}/{uuid.uuid4().hex}.{extension}"
    storage_headers = _supabase_service_headers(content_type)
    storage_headers["x-upsert"] = "false"
    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_SECONDS) as client:
            upload = await client.post(
                f"{_supabase_url()}/storage/v1/object/aureon-media/{object_path}",
                content=content,
                headers=storage_headers,
            )
    except httpx.HTTPError as exc:
        logger.exception("Supabase media upload failed")
        raise HTTPException(status_code=503, detail="Media storage is unavailable") from exc
    if upload.status_code >= 400:
        logger.error("Supabase Storage returned HTTP %s", upload.status_code)
        raise HTTPException(status_code=503, detail="Media upload failed")
    signed_result = await _supabase_storage_request(
        "POST",
        f"object/sign/aureon-media/{object_path}",
        {"expiresIn": 300},
    )
    signed_path = signed_result.get("signedURL") if isinstance(signed_result, dict) else None
    if not isinstance(signed_path, str) or not signed_path.startswith("/"):
        raise HTTPException(status_code=502, detail="Media storage returned an invalid signed URL")
    signed_url = (
        f"{_supabase_url()}{signed_path}"
        if signed_path.startswith("/storage/v1/")
        else f"{_supabase_url()}/storage/v1{signed_path}"
    )
    return {"url": signed_url, "content_type": content_type}


@app.post("/api/generate-image")
async def generate_image(
    body: MediaGenerationInput,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    return await _generate_and_upload_media(user_id, body.prompt, media_kind="image")


@app.post("/api/generate-video")
async def generate_video(
    body: MediaGenerationInput,
    user_id: str = Depends(current_user_id),
) -> dict[str, str]:
    return await _generate_and_upload_media(user_id, body.prompt, media_kind="video")


def _normalize_domain(domain: str) -> str:
    normalized = domain.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Provide a valid fully qualified domain name")
    return normalized


@app.get("/api/domains/search")
async def search_domain(
    domain: str = Query(min_length=3, max_length=253),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    del user_id
    normalized = _normalize_domain(domain)
    rows = await _supabase_request(
        "GET",
        "domain_claims",
        params={"select": "domain", "domain": f"eq.{normalized}", "limit": "1"},
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Persistent storage returned an invalid domain result")
    claimed = bool(rows)
    return {
        "domain": normalized,
        "available": not claimed,
        "mode": "mock",
        "simulated": True,
        "notice": "Availability is only simulated; no registrar lookup or DNS check was performed.",
    }


@app.post("/api/domains/buy", status_code=201)
async def mock_claim_domain(
    body: DomainClaimInput,
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    domain = _normalize_domain(body.domain)
    project_name = _safe_project_name(body.project_name)
    published_project = SITES_DIR / user_id / project_name / "index.html"
    if not published_project.is_file():
        raise HTTPException(status_code=404, detail="Publish this project before creating a mock claim")
    profile = await _get_account_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Account profile not found")
    plan = await _standard_plan()
    result = await _supabase_request(
        "POST",
        "rpc/claim_mock_domain",
        payload={
            "p_domain": domain,
            "p_user_id": user_id,
            "p_project_name": project_name,
            "p_site_path": f"/sites/{user_id}/{project_name}",
            "p_standard_limit": plan["max_active_domains"],
        },
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Domain service returned invalid data")
    if result.get("error") == "active_domain_limit":
        raise HTTPException(status_code=403, detail="Standard plan active-domain limit reached")
    if result.get("error") == "domain_unavailable":
        raise HTTPException(status_code=409, detail="This domain already has a mock claim")
    if result.get("error"):
        raise HTTPException(status_code=400, detail="Mock domain claim could not be completed")
    return {
        "domain": domain,
        "mode": "mock",
        "simulated": True,
        "site_path": f"/sites/{user_id}/{project_name}",
        "notice": (
            "This is only a local Aureon claim record. It does not register the domain, "
            "verify ownership, configure DNS, or bind the domain through Vercel/Cloudflare."
        ),
    }