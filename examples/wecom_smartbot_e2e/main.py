# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""WeCom 智能机器人 (Smart Bot) end-to-end harness, long-connection mode.

Uses the official ``wecom-aibot-python-sdk`` to establish a WebSocket long
connection to WeCom (no domain / HTTPS / callback URL required). Receives
``message.text`` frames, routes through the multi-tenant SDK, runs the model,
and replies via ``reply_stream``.

Environment variables (.env):
    WECOM_BOT_ID       (required) 机器人 BotID
    WECOM_BOT_SECRET   (required) 机器人 Secret
    MODEL_NAME         (optional) default deepseek-v4-flash
    MODEL_ENDPOINT     (optional) default https://api.deepseek.com
    MODEL_API_KEY      (required) model provider API key
    TENANT_ID          (optional) default wecom_smartbot_e2e
"""

from __future__ import annotations

import asyncio
import logging
import os

from aibot import WSClient
from aibot import WSClientOptions
from aibot import generate_req_id
from dotenv import load_dotenv

from trpc_agent_sdk.channels import InMemoryDedupStore
from trpc_agent_sdk.channels import content_from_text
from trpc_agent_sdk.tenant import BackendFactory
from trpc_agent_sdk.tenant import BackendSpec
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
logger = logging.getLogger("wecom_smartbot_e2e")

BOT_ID = os.environ["WECOM_BOT_ID"]
BOT_SECRET = os.environ["WECOM_BOT_SECRET"]
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-flash")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "https://api.deepseek.com")
MODEL_API_KEY = os.environ["MODEL_API_KEY"]
TENANT_ID = os.environ.get("TENANT_ID", "wecom_smartbot_e2e")

WECOM_TEXT_LIMIT = 2000  # stream content 保守分片长度


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
    )


def event_reply_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text for part in event.content.parts if part.text and not part.thought)


def split_text(text: str, limit: int = WECOM_TEXT_LIMIT) -> list[str]:
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


async def main() -> None:
    source = InMemoryTenantSource()
    await source.put(build_tenant())

    registry = TenantRegistry(source)
    secret_store = InMemorySecretStore({"model_key": MODEL_API_KEY})
    storage = StorageAdapter(registry, BackendFactory())
    pool = RunnerPool(registry, TenantAgentFactory(registry, storage, secret_store, {}))
    dedup = InMemoryDedupStore()

    client = WSClient(WSClientOptions(bot_id=BOT_ID, secret=BOT_SECRET))

    @client.on("authenticated")
    async def on_authenticated():
        logger.info("机器人订阅成功")

    @client.on("message.text")
    async def on_text(frame):
        body = frame.get("body", {}) or {}
        content = (body.get("text") or {}).get("content", "") or ""
        msgid = str(body.get("msgid") or "")
        userid = (body.get("from") or {}).get("userid", "") or ""
        chattype = body.get("chattype", "single") or "single"
        chatid = body.get("chatid") or userid

        if msgid and dedup.seen("wecom_smartbot", msgid):
            return

        logger.info("message user=%s chattype=%s chatid=%s msgid=%s: %r",
                    userid, chattype, chatid, msgid, content)

        session_id = f"{'group' if chattype == 'group' else 'chat'}_wecom_{chatid}"
        try:
            runner = await pool.get_runner(TENANT_ID)
            final_text = ""
            async for event in runner.run_async(
                user_id=userid,
                session_id=session_id,
                new_message=content_from_text(content),
            ):
                if event.partial or event.author == "user":
                    continue
                text = event_reply_text(event)
                if text:
                    final_text = text

            if final_text:
                for chunk in split_text(final_text):
                    await client.reply_stream(frame, generate_req_id("stream"), chunk, True)
                    logger.info("replied chatid=%s (%d chars)", chatid, len(chunk))
        except Exception as ex:
            logger.error("run failed: %s", ex, exc_info=True)
            try:
                await client.reply_stream(frame, generate_req_id("stream"),
                                          f"error: {type(ex).__name__}", True)
            except Exception:
                pass

    @client.on("error")
    async def on_error(error):
        logger.error("连接异常: %s", error)

    await client.connect()
    logger.info("connected; waiting for messages...")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
