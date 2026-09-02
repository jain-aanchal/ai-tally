# SPDX-License-Identifier: Apache-2.0
"""ai-tally Python SDK.

Cost-and-value observability for AI products. OpenTelemetry ``gen_ai.*`` native.

One-line connect (CTO-260)::

    import tally

    tally.init("tally_sk_live_...")   # reads TALLY_KEY / TALLY_ENDPOINT env if omitted
    # openai / anthropic calls are now auto-instrumented; nothing else to wire up.

The module-level ``record_*`` helpers, ``flush``, ``hash_account``, and the context primitives
(``with_account`` / ``start_trace``) are re-exported here so the common paths are true one-liners.
"""

__version__ = "0.0.1"

from tally.context import start_trace, with_account, with_trace_context
from tally.hash_account import hash_account
from tally.init import (
    flush,
    get_client,
    init,
    record_embedding_call,
    record_llm_call,
    record_tool_call,
    record_vector_call,
    uninstrument,
)

__all__ = [
    "__version__",
    "init",
    "flush",
    "uninstrument",
    "get_client",
    "hash_account",
    "record_llm_call",
    "record_tool_call",
    "record_vector_call",
    "record_embedding_call",
    "with_account",
    "start_trace",
    "with_trace_context",
]
