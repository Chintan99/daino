# Evals

D[Ai]NO's unit tests measure mechanism: that the loop applies actions, that the
compactor sheds the right things, that the command gate refuses the right
commands. None of them measure whether the agent *finishes the task*, and none
of them measure whether the retrieval ranking picks the right files.

Those are the two numbers that actually move when someone touches the context
work, and they were the two nobody could see. The eval harness is how they get
seen.

```bash
daino eval list          # every case, and which need a model
daino eval run           # the free ones
daino eval run --tasks --model my-profile   # the expensive ones
```

`daino eval run` exits non-zero when anything failed, so it works as a CI gate.

## Three kinds, deliberately different in cost

**`retrieval`** — no model, no network, milliseconds. A synthetic repository, a
task, and an assertion about which files the ranking chose. This is what turns
the hand-tuned constants in `daino/context/retrieval.py` into numbers with a
regression test.

**`sizing`** — also free. A model profile in, and assertions about the envelope
derived from it: the compaction headroom, the files-per-task ceiling, the source
budget. These are the thresholds the task splitter acts on, and they are
arithmetic, and arithmetic can be checked.

**`task`** — the expensive one. A scratch Git repository, one real instruction,
one real model, and assertions about the working tree afterwards. This is what
"does this model actually work with D[Ai]NO" means.

The split matters because the cheap two will actually be run on every change, and
they cover exactly the code with no other test. They run as part of the ordinary
unit suite too, so a regression fails there whether or not anyone remembered to
invoke the CLI.

## What a case looks like

Suites are YAML. Built-in ones ship with D[Ai]NO; a project adds its own under
`.daino/evals/`, and a project suite with the same name replaces the built-in
one. Your real tasks are better evidence about a model than anything shipped.

A retrieval case:

```yaml
- id: lexical-matches-are-weaker-than-graph-edges
  kind: retrieval
  instruction: Fix the webhook signature verification.
  required: [webhooks/handler.py]
  files:
    webhooks/handler.py: |
      from webhooks.signature import verify
    webhooks/signature.py: |
      def verify(request): return True
    docs/webhook_signature_guide.md: |
      How webhook signature verification works.
  retrieval:
    includes: [webhooks/signature.py]
    top: [webhooks/signature.py]
    top_n: 1
```

Most assertions are about *order* rather than membership, because that is what
the constants control:

```yaml
  retrieval:
    order: ["tests/test_discount.py > tests/test_shipping.py"]
```

A task case grades the tree, never the agent's account of it:

```yaml
- id: fix-a-failing-test
  kind: task
  instruction: >
    The test suite is failing. Find the bug in the source (not the test), fix it,
    and confirm the tests pass.
  files:
    calculator.py: |
      def add(a, b):
          return a - b
    test_calculator.py: |
      from calculator import add
      def test_add():
          assert add(2, 3) == 5
  expect:
    changed: [calculator.py]
    unchanged: [test_calculator.py]     # editing the test is the cheap cheat
    commands: ["python -m pytest -q"]
```

`unchanged` is the assertion that earns its place: the easy way to make a failing
test pass is to change the test, and a run that does it looks identical in the
summary to one that fixed the bug.

## Reading the result

```
retrieval: 5/5 passed (100%)
sizing: 5/5 passed (100%)
tasks [gpt-5.6]: 3/4 passed (75%)
  ✗ a-multi-file-rename
      service.py does not match 'fetch_user'
      `python -m pytest -q` exited 1:
      ImportError: cannot import name 'fetch' from 'client'

Total: 13/14 passed (93%)
Spent: 184,203 tokens, $0.4120
```

Three outcomes, not two. A case that **failed** is a capability measurement; a
case that **could not run** — a provider outage, a missing executable — is not,
and is excluded from the denominator. Folding the second into the first is how a
benchmark starts producing numbers that look like model quality and are actually
network weather.

## Assertion reference

**retrieval** — `includes`, `excludes`, `top` + `top_n`, `order`
(`"a.py > b.py"`), `max_selected`.

**sizing** — `compact`, `one_action_per_turn`, and min/max bounds on
`working_headroom_tokens`, `max_files_per_task`, `task_source_budget_tokens`.

**task** — `changed`, `unchanged`, `contains` (path → regex), `absent`,
`commands` (must exit zero), `answer_matches`, `max_steps`.

## Isolation

A task case gets its own temporary directory, its own Git repository, its own
database, and its own `.daino` state, and it runs under the `sandbox` runtime
with review and memory extraction off. It cannot see your global configuration
and leaves nothing behind — or the second run of a suite would be measuring
something different from the first.
