# Providers

`LLMProvider` defines asynchronous `complete`, `stream`, `structured_complete`, feature discovery,
health checking, and cleanup. `OpenAICompatibleProvider` supplies the common implementation.
OpenRouter only adds `X-Title` and `HTTP-Referer` when configured. Ollama and vLLM run fully
offline and permit an empty API key.

```bash
vasuki providers add private \
  --type openai-compatible \
  --base-url https://llm.internal.example/v1 \
  --model company-coder \
  --api-key-ref env://PRIVATE_LLM_KEY
vasuki providers test private
```

## Offline providers

Vasuki runs entirely on locally hosted models when every role routes to an Ollama or vLLM
provider. Tool-capable instruct models (for example `qwen2.5-coder`, `llama3.1`) give the agent
loop native tool calling plus grammar-constrained structured output, so no cloud provider is
required for planning, building, repairing, or review.

```bash
vasuki providers add local-ollama \
  --type ollama \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen2.5-coder:7b
vasuki providers test local-ollama

vasuki providers add local-vllm \
  --type vllm \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --local
```

Both types are marked local automatically, so data-sensitivity routing keeps restricted work on
the machine. Ollama needs no API key; vLLM accepts an empty one.

## Structured output

Structured responses use JSON Schema, validate with Pydantic, attempt bounded repair, and fail
closed. Each backend constrains decoding with the mechanism its server implements:

- OpenAI-compatible and OpenRouter: `response_format: json_schema`
- Ollama: top-level `format` with the full JSON Schema
- vLLM: `guided_json`

If a server rejects the constraint parameter, the request is retried once without it, so both
newer and older server versions keep working. The deterministic test server under `tests/` uses
the same protocol and requires no paid credentials.

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
