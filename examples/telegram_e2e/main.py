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

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "e2e_bot")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "deepseek")
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-v4-flash")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "https://api.deepseek.com")
MODEL_API_KEY = os.environ["MODEL_API_KEY"]
TENANT_ID = os.environ.get("TENANT_ID", "e2e")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def build_tenant() -> Tenant:
    return Tenant(
        tenant_id=TENANT_ID,
        model_settings=ModelConfig(provider=MODEL_PROVIDER, model_name=MODEL_NAME,
                                   endpoint=MODEL_ENDPOINT, api_key_ref="model_key"),
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
                    params={"offset": offset, "timeout": 30},
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except httpx.HTTPError as ex:
                print(f"getUpdates failed: {ex}")
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
                    print(f"route skipped: {type(ex).__name__}: {ex}")
                    continue

                chat_id = routed.inbound.chat_id
                try:
                    await run_and_reply(pool, client, routed, chat_id)
                except Exception as ex:
                    print(f"run failed: {type(ex).__name__}: {ex}")
                    await client.post(
                        f"{TELEGRAM_API}/sendMessage",
                        json={"chat_id": chat_id, "text": f"error: {type(ex).__name__}"},
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
        text = event.get_text()
        if text:
            final_text = text

    if final_text:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": final_text})


if __name__ == "__main__":
    asyncio.run(main())
