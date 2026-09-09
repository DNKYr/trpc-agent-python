# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Tests for the Masker and SensitiveDataFilter."""

import logging

from trpc_agent_sdk.tenant._masker import Masker
from trpc_agent_sdk.tenant._masker import SensitiveDataFilter


def test_mask_api_key_assignment():
    masker = Masker()
    masked = masker.mask("api_key=sk-abc123xyz")
    assert "sk-abc123xyz" not in masked
    assert "[REDACTED]" in masked


def test_mask_phone_number():
    masker = Masker()
    masked = masker.mask("call 13812345678 now")
    assert "13812345678" not in masked


def test_register_custom_pattern():
    masker = Masker()
    masker.register("custom", r"\bCUSTOM_\d+\b", replacement="<hidden>")
    assert masker.mask("value CUSTOM_123 end") == "value <hidden> end"


def test_sensitive_data_filter_masks_formatted_message():
    masker = Masker()
    flt = SensitiveDataFilter(masker)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="api_key=%s",
        args=("sk-leak-here", ),
        exc_info=None,
    )
    assert flt.filter(record) is True
    assert "sk-leak-here" not in record.getMessage()
