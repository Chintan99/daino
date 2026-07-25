# Providers

`LLMProvider` defines asynchronous `complete`, `stream`, `structured_complete`, feature discovery,
health checking, and cleanup. `OpenAICompatibleProvider` supplies the common implementation.
OpenRouter only adds `X-Title` and `HTTP-Referer` when configured. vLLM permits an empty API key.

```bash
vasuki providers add private \
  --type openai-compatible \
  --base-url https://llm.internal.example/v1 \
  --model company-coder \
  --api-key-ref env://PRIVATE_LLM_KEY
vasuki providers test private
```

Structured responses use JSON Schema where advertised, validate with Pydantic, attempt bounded
repair, and fail closed. The deterministic test server under `tests/` uses the same protocol and
requires no paid credentials.
