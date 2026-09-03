from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)


class CacheStoreError(RuntimeError):
    """The validated stream cache could not be persisted."""


def _store_stream_cache(payload: dict[str, object]) -> str:
    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    secret_id = os.getenv("FOURGTV_CACHE_SECRET", "tw-news-fourgtv-streams").strip()
    if not project_id or not secret_id:
        raise CacheStoreError("缺少 GCP_PROJECT_ID 或 FOURGTV_CACHE_SECRET")

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    parent = f"projects/{project_id}/secrets/{secret_id}"
    with httpx.Client(timeout=20, trust_env=False) as client:
        token_response = client.get(
            METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CacheStoreError("無法取得 Cloud Run 服務身分權杖")
        headers = {"Authorization": f"Bearer {access_token}"}
        response = client.post(
            f"https://secretmanager.googleapis.com/v1/{parent}:addVersion",
            headers=headers,
            json={"payload": {"data": base64.b64encode(data).decode("ascii")}},
        )
        if response.status_code >= 400:
            raise CacheStoreError(f"Secret Manager 寫入失敗（{response.status_code}）")
        name = response.json().get("name")
        if not isinstance(name, str):
            raise CacheStoreError("Secret Manager 沒有回傳版本名稱")

        versions_response = client.get(
            f"https://secretmanager.googleapis.com/v1/{parent}/versions",
            headers=headers,
            params={"filter": "state:ENABLED", "pageSize": "100"},
        )
        if versions_response.status_code < 400:
            versions = versions_response.json().get("versions", [])
            enabled = sorted(
                (item for item in versions if isinstance(item, dict)),
                key=lambda item: str(item.get("createTime") or ""),
                reverse=True,
            )
            for old in enabled[2:]:
                old_name = old.get("name")
                if isinstance(old_name, str):
                    client.post(
                        f"https://secretmanager.googleapis.com/v1/{old_name}:destroy",
                        headers=headers,
                        json={},
                    )
    return name.rsplit("/", 1)[-1]


async def store_stream_cache(payload: dict[str, object]) -> str:
    try:
        return await asyncio.to_thread(_store_stream_cache, payload)
    except CacheStoreError:
        raise
    except Exception as exc:
        raise CacheStoreError(f"儲存官方直播快取失敗：{exc}") from exc
