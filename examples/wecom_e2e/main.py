# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""WeCom (企业微信) end-to-end harness.

Wires the multi-tenant SDK to a real WeCom self-built app via its message
callback (requires HTTPS + a verified callback URL).

Environment variables (.env):
    WECOM_CORP_ID            (required) 企业ID
    WECOM_AGENT_ID           (required) 自建应用 AgentId
    WECOM_CORP_SECRET        (required) 自建应用 Secret
    WECOM_CALLBACK_TOKEN     (required) 回调 Token
    WECOM_ENCODING_AES_KEY   (required) 回调 EncodingAESKey (43 位)
    MODEL_NAME               (optional) default deepseek-v4-flash
    MODEL_ENDPOINT           (optional) default https://api.deepseek.com
    MODEL_API_KEY            (required) model provider API key
    TENANT_ID                (optional) default wecom_e2e
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Query
from fastapi import Request

from trpc_agent_sdk.channels import ChannelRouter
from trpc_agent_sdk.channels import InMemoryDedupStore
from trpc_agent_sdk.channels import WecomAdapter
from trpc_agent_sdk.channels._wecom_crypto import WeComCrypto
from trpc_agent_sdk.tenant import BackendFactory
from trpc_agent_sdk.tenant import BackendSpec
from trpc_agent_sdk.tenant import ChannelBinding
from trpc_agent_sdk.tenant import DataBackendConfig
from trpc_agent_sdk.tenant import InMemorySecretStore
from trpc_agent_sdk.tenant import InMemoryTenantSource
from trpc_agent_sdk.tenant import ModelConfig
from trpc_agent_sdk.tenant import RunnerPool
from trpc_agent_sdk.tenant import StorageAdapter
from trpc_agent_sdk.tenant import Tenant
from trpc_agent_sdk.tenant import TenantAgentFactory
from trpc_agent_sdk.tenant import TenantRegistry

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wecom_e2e")

CORP_ID = os.environ["WECOM_CORP_ID"]
AGENT_ID = os.environ["WECOM_AGENT_ID"]
CORP_SECRET = os.environ["WECOM_CORP_SECRET"]
CALLBACK_TOKEN = os.environ["WECOM_CALLBACK_TOKEN"]
ENCODING_AES_KEY = os.environ["WECOM_ENCODING_AES_KEY"]
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-flash")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "https://api.deepseek.com")
MODEL_API_KEY = os.environ["MODEL_API_KEY"]
TENANT_ID = os.environ.get("TENANT_ID", "wecom_e2e")

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_TEXT_LIMIT = 2048  # bytes; split conservatively at 1800 chars

crypto = WeComCrypto(CALLBACK_TOKEN, ENCODING_AES_KEY, CORP_ID)

app = FastAPI()

# Access token cache (expires in 7200s).
_token_cache: dict = {"token": "", "expires_at": 0.0}


def build_tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT_ID,
        model_settings=ModelConfig(provider="deepseek", model_name=MODEL_NAME,
                                   endpoint=MODEL_ENDPOINT, api_key_ref="model_key"),
        data_backends=DataBackendConfig(
            session=BackendSpec(type="in_memory"),
            memory=BackendSpec(type="in_memory"),
            artifact=BackendSpec(type="in_memory"),
        ),
        im_channels=[
            ChannelBinding(
                binding_id="wecom",
                channel_type="wecom",
                webhook_url="https://unused.invalid",
                token_ref="wecom_token",
                external_account_id=AGENT_ID,
                verify_enabled=True,
            )
        ],
    )


# Build the SDK stack once at import time.
_source = InMemoryTenantSource()
_registry = TenantRegistry(_source)
_secret_store = InMemorySecretStore({"model_key": MODEL_API_KEY, "wecom_token": CALLBACK_TOKEN})
_storage = StorageAdapter(_registry, BackendFactory())
_pool = RunnerPool(_registry, TenantAgentFactory(_registry, _storage, _secret_store, {}))
_router = ChannelRouter(_registry, _secret_store, {"wecom": WecomAdapter()}, InMemoryDedupStore())


async def get_access_token(client: httpx.AsyncClient) -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]
    resp = await client.get(f"{WECOM_API}/gettoken",
                            params={"corpid": CORP_ID, "corpsecret": CORP_SECRET})
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + 7000
    return data["access_token"]


async def send_text(client: httpx.AsyncClient, to_user: str, text: str) -> None:
    token = await get_access_token(client)
    resp = await client.post(
        f"{WECOM_API}/message/send",
        params={"access_token": token},
        json={"touser": to_user, "msgtype": "text", "agentid": AGENT_ID,
              "text": {"content": text}},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"send failed: {data}")


def split_text(text: str, limit: int = 1800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def event_reply_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text for part in event.content.parts if part.text and not part.thought)


@app.get("/wecom/callback")
async def verify_callback(msg_signature: str = Query(...), timestamp: str = Query(...),
                          nonce: str = Query(...), echostr: str = Query(...)):
    if not crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
        return "invalid signature"
    return crypto.decrypt(echostr)


@app.post("/wecom/callback")
async def receive_callback(request: Request, msg_signature: str = Query(...),
                           timestamp: str = Query(...), nonce: str = Query(...)):
    body = await request.body()
    encrypt = ET.fromstring(body).findtext("Encrypt") or ""
    if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypt):
        return "invalid signature"

    plain = crypto.decrypt(encrypt)
    root = ET.fromstring(plain)
    if root.findtext("MsgType") != "text":
        return "success"

    inbound = {
        "msg_id": root.findtext("MsgId"),
        "from_user": root.findtext("FromUserName"),
        "text": root.findtext("Content") or "",
        "timestamp": float(root.findtext("CreateTime") or 0),
    }

    try:
        routed = await _router.route(
            channel_type="wecom", platform_id=AGENT_ID, raw=inbound,
            signature=msg_signature, timestamp=timestamp, nonce=nonce, payload=encrypt,
        )
    except Exception as ex:
        logger.info("route skipped (%s): %s", type(ex).__name__, ex)
        return "success"

    logger.info("message user=%s session=%s: %r",
                routed.user_id, routed.session_id, routed.inbound.text)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            await run_and_reply(client, routed)
        except Exception as ex:
            logger.error("run failed: %s", ex, exc_info=True)
    return "success"


async def run_and_reply(client: httpx.AsyncClient, routed) -> None:
    runner = await _pool.get_runner(routed.tenant_id)
    final_text = ""
    async for event in runner.run_async(
        user_id=routed.user_id,
        session_id=routed.session_id,
        new_message=routed.content,
    ):
        if event.partial or event.author == "user":
            continue
        text = event_reply_text(event)
        if text:
            final_text = text

    if not final_text:
        return
    to_user = routed.inbound.sender_id
    for chunk in split_text(final_text):
        await send_text(client, to_user, chunk)
        logger.info("replied user=%s (%d chars)", to_user, len(chunk))
