# Providers

`LLMProvider` defines asynchronous `complete`, `stream`, `structured_complete`, feature discovery,
health checking, and cleanup. `OpenAICompatibleProvider` supplies the common implementation.
OpenRouter only adds `X-Title` and `HTTP-Referer` when configured. Ollama and vLLM run fully
offline and permit an empty API key.

For OpenRouter, token counts and the charged `usage.cost` returned by the API are persisted for
complete, structured, and streaming calls. The TUI header sums those durable usage records rather
than estimating spend from a static price table.

```bash
daino providers add private \
  --type openai-compatible \
  --base-url https://llm.internal.example/v1 \
  --model company-coder \
  --api-key-ref env://PRIVATE_LLM_KEY
daino providers test private
```

## Offline providers

D[Ai]NO runs entirely on locally hosted models when every role routes to an Ollama or vLLM
provider. Tool-capable instruct models (for example `qwen2.5-coder`, `llama3.1`) give the agent
loop native tool calling plus grammar-constrained structured output, so no cloud provider is
required for planning, building, repairing, or review.

```bash
daino providers add local-ollama \
  --type ollama \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen2.5-coder:7b
daino providers test local-ollama

daino providers add local-vllm \
  --type vllm \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --local
```

Both types are marked local automatically, so data-sensitivity routing keeps restricted work on
the machine. Ollama needs no API key; vLLM accepts an empty one.

## Concurrent requests to one model server

D[Ai]NO fans out model calls in two places: a QA scan runs its specialists in
parallel, and `/team` runs sub-agents in parallel. Against a hosted API that is
free speed. Against a local runtime it is not: Ollama and vLLM hold one copy of
one model, so the requests queue inside the server — and the client's timeout is
running the whole time each one waits, which is how a fan-out that would have
finished serially instead times out.

`max_concurrent_requests` bounds the in-flight generation requests per **model
server** (keyed by base URL, so two profiles on the same Ollama share its queue
while two Ollamas on different hosts do not block each other):

```yaml
providers:
  local-ollama:
    type: ollama
    base_url: http://127.0.0.1:11434/v1
    model: qwen3.5:9b
    max_concurrent_requests: 1   # the default for ollama and vllm
  openrouter:
    type: openrouter
    base_url: https://openrouter.ai/api/v1
    model: anthropic/claude-sonnet-4
    max_concurrent_requests: 0   # the default for hosted APIs: no limit
```

**What this does and does not buy you.** Measured on an M-series Mac against a
warm `qwen3.8:27b-mlx` — three concurrent 220-token generations with a ~2 KB
prompt — serialising made no difference to throughput at all:

| | wall clock | slowest request |
| --- | --- | --- |
| unlimited | 15.5s, 15.9s | 15.5s, 15.9s |
| serialised | 15.8s, 15.4s | 15.8s, 15.4s |

Ollama was already serialising internally, so the gate only moves the queue from
its side to D[Ai]NO's. That is still worth doing — a request waiting for a slot
has not started its timeout, and nothing thrashes the model server's memory when
`OLLAMA_NUM_PARALLEL` is raised — but it is a robustness measure, **not** a
speed-up. If a local server is genuinely faster with overlap on your hardware,
set `max_concurrent_requests: 0` and it will be left alone.

Metadata calls — listing models, health checks, key validation — are never
gated, so the provider form keeps answering while a generation is in flight.

## Structured output

Structured responses use JSON Schema, validate with Pydantic, attempt bounded repair, and fail
closed. Each backend constrains decoding with the mechanism its server implements:

- OpenAI-compatible and OpenRouter: `response_format: json_schema`
- Ollama: top-level `format` with the full JSON Schema
- vLLM: `guided_json`

If a server rejects the constraint parameter, the request is retried once without it, so both
newer and older server versions keep working. The deterministic test server under `tests/` uses
the same protocol and requires no paid credentials.

Only an HTTP request-shape rejection (for example 400 or 422) disables a schema constraint or
native tools. Authentication, quota, transport, and server failures remain provider failures and
move to a configured model fallback instead of repeating the same doomed request in a weaker
format.

## Native tool calling

Providers advertise capabilities through `features` in the provider configuration:
`chat`, `structured`, and `tools`. The builder/debugger loop sends the action space
(`read_file`, `search_text`, `list_directory`, `replace`, `write`, `delete`, `finish`) as
OpenAI-format tools when the routed provider advertises `tools`, executes every returned tool
call through the same validated, scope-checked executor, and threads observations back as `tool`
messages. A turn may carry several tool calls. When the provider has no tool support, or rejects
the tools request once, the loop falls back to the identical action expressed as
schema-constrained JSON.

- Ollama and OpenRouter advertise `tools` by default.
- vLLM advertises `tools` only when you opt in, because the server must be started with
  `--enable-auto-tool-choice --tool-call-parser <parser>` (for example `hermes` or
  `qwen25` for Qwen models):

```yaml
providers:
  local-vllm:
    type: vllm
    base_url: http://127.0.0.1:8000/v1
    model: Qwen/Qwen2.5-Coder-7B-Instruct
    features: [chat, structured, tools]
```

Mission builders receive the same policy-gated command runner as the interactive coding agent.
Tests, linters, builds, and read-only inspection can therefore run inside the implementation loop;
installs and network commands remain unavailable in a headless mission unless an approval-capable
interface is attached, and destructive commands remain refused.
