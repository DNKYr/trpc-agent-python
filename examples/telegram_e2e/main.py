# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Telegram end-to-end harness.

Wires the multi-tenant SDK (SP4 ChannelRouter + SP3 RunnerPool) to a real
Telegram bot via long polling. Long polling needs no public domain / HTTPS
and no webhook; it only requires outbound internet access.

Environment variables:
    TELEGRAM_BOT_TOKEN      (required) bot token from @BotFather
    TELEGRAM_BOT_USERNAME   (optional) bot username, used as the channel binding
                            external_account_id; default "e2e_bot"
    MODEL_PROVIDER          (optional) informational; default "openai"
    MODEL_NAME              (optional) model name resolved via ModelRegistry;
                            default "gpt-4o-mini"
    MODEL_API_KEY           (required) model provider API key
    TENANT_ID               (optional) default "e2e"
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv

from trpc_agent_sdk.channels import ChannelRouter
from trpc_agent_sdk.channels import InMemoryDedupStore
from trpc_agent_sdk.channels import TelegramAdapter
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
logger = logging.getLogger("telegram_e2e")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "e2e_bot")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "deepseek")
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-flash")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "https://api.deepseek.com")
MODEL_API_KEY = os.environ["MODEL_API_KEY"]
TENANT_ID = os.environ.get("TENANT_ID", "e2e")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Telegram sendMessage caps text at 4096 characters.
TELEGRAM_MAX_TEXT_LENGTH = 4096


def event_reply_text(event) -> str:
    """Extract the visible reply text, excluding the model's reasoning (thought) parts.

    DeepSeek v4 (and other reasoning models) return ``reasoning_content``
    alongside ``content``; the SDK marks the former as a ``thought=True`` part.
    ``event.get_text()`` concatenates both, which leaks the chain-of-thought.
    """
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text for part in event.content.parts if part.text and not part.thought)


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Split text into chunks under ``limit`` chars, preferring newline boundaries."""
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


def build_tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT_ID,
        model_settings=ModelConfig(provider=MODEL_PROVIDER,
                                   model_name=MODEL_NAME,
                                   endpoint=MODEL_ENDPOINT,
                                   api_key_ref="model_key"),
        data_backends=DataBackendConfig(
            session=BackendSpec(type="in_memory"),
            memory=BackendSpec(type="in_memory"),
            artifact=BackendSpec(type="in_memory"),
        ),
        im_channels=[
            ChannelBinding(
                binding_id="telegram",
                channel_type="telegram",
                webhook_url="https://unused.invalid",
                external_account_id=BOT_USERNAME,
                verify_enabled=False,
            )
        ],
    )


async def main() -> None:
    source = InMemoryTenantSource()
    await source.put(build_tenant())

    registry = TenantRegistry(source)
    secret_store = InMemorySecretStore({"model_key": MODEL_API_KEY})
    storage = StorageAdapter(registry, BackendFactory())
    agent_factory = TenantAgentFactory(registry, storage, secret_store, {})
    pool = RunnerPool(registry, agent_factory)

    adapter = TelegramAdapter()
    router = ChannelRouter(registry, secret_store, {"telegram": adapter}, InMemoryDedupStore())

    async with httpx.AsyncClient(timeout=60.0) as client:
        offset = 0
        while True:
            try:
                resp = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 30
                    },
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except httpx.HTTPError as ex:
                logger.warning("getUpdates failed: %s", ex)
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                if "message" not in update or "text" not in update["message"]:
                    continue

                try:
                    routed = await router.route(
                        channel_type="telegram",
                        platform_id=BOT_USERNAME,
                        raw=update,
                        signature="",
                        timestamp="",
                        nonce="",
                    )
                except Exception as ex:  # duplicate / parse / binding errors
                    logger.info("route skipped (%s): %s", type(ex).__name__, ex)
                    continue

                chat_id = routed.inbound.chat_id
                logger.info("message user=%s session=%s chat=%s: %r", routed.user_id, routed.session_id, chat_id,
                            routed.inbound.text)
                try:
                    await run_and_reply(pool, client, routed, chat_id)
                except Exception as ex:
                    logger.error("run failed: %s", ex, exc_info=True)
                    await client.post(
                        f"{TELEGRAM_API}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": f"error: {type(ex).__name__}"
                        },
                    )


async def run_and_reply(pool: RunnerPool, client: httpx.AsyncClient, routed, chat_id: str) -> None:
    """Run the agent and send the final agent text back to the chat."""
    runner = await pool.get_runner(routed.tenant_id)
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
        logger.info("no reply text for chat=%s", chat_id)
        return

    for chunk in split_text(final_text, limit=TELEGRAM_MAX_TEXT_LENGTH - 96):
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": chunk})
        resp.raise_for_status()
        logger.info("replied chat=%s (%d chars)", chat_id, len(chunk))


if __name__ == "__main__":
    asyncio.run(main())
