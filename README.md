# Roundtable: synchronous multi-agent research environment

Roundtable is a local web app where any number of independently instructed LLM agents work on the same
research problem in **synchronized, discrete rounds**. There is a common channel, a private channel per agent, a
persistent structured research workspace, a shared file tree, tools (web, arXiv, Python, Bash), and a
convergence policy you can inspect. You can watch each round, step in between rounds, and control execution.

The app ships no built-in roles, supervisor or methodology. Every agent is a peer, and its behaviour comes only from
the instructions you write for it.

## Quick start

You need **Python 3.10 or newer**. Node.js is not needed (the web page comes pre-built).

### Step 1: Get an API key

Roundtable talks to an AI model through an API key. Pick **one** provider:

| Provider | Where to get a key | Cost |
|---|---|---|
| Google AI Studio (Gemini) | https://aistudio.google.com/apikey | Free tier available (small daily limits) |
| Anthropic (Claude) | https://console.anthropic.com/settings/keys | Paid |
| OpenAI | https://platform.openai.com/api-keys | Paid |
| DeepSeek | https://platform.deepseek.com/api_keys | Paid (low prices) |

One key is enough for all agents.

### Step 2: Give the key to Roundtable

Choose **one** of these three ways. They all work the same.

**Way A: paste it in the app (easiest).** Skip this step for now. When you open the app and no key is found, the setup
page asks for one. Paste it there and press **Save key**.

**Way B: an environment variable.** Run this in the terminal before starting the app (use the line for your provider):

```bash
export GEMINI_API_KEY="your-key-here"       # Google AI Studio
export ANTHROPIC_API_KEY="your-key-here"    # Anthropic
export OPENAI_API_KEY="your-key-here"       # OpenAI
export DEEPSEEK_API_KEY="your-key-here"     # DeepSeek
```

This lasts only for that terminal window.

**Way C: a key file (stays after restarts).**

```bash
cp roundtable/local_keys.example.py roundtable/local_keys.py
```

Open `roundtable/local_keys.py` in a text editor and fill in your key, for example:

```python
BUILTIN_KEYS = {
    "google": "your-key-here",
}
```

If you use more than one way, the app uses the key saved in the app first, then the environment variable, then the
key file.

### Step 3: Start the app

```bash
cd roundtable          # the folder you cloned
./run.sh
```

The first start takes a minute: it creates a Python environment in `.venv` and installs everything. When you see
`Roundtable running at http://127.0.0.1:8765`, open that address in your browser.

To stop the app, press **Ctrl+C** in the terminal. To start it again later, run `./run.sh` again.

### Step 4: Use it

1. **Setup page:** write the goal, choose how many agents, write a role for each one, and set the maximum number of
   rounds. Optionally press **+ Add a neutral judge**. Press **Start**.
2. **Activity page:** watch the rounds. The **Common ground** tab shows everything all agents see, including the full
   content of every file they write. Each agent also has its own tab. Anything you type or attach is delivered at the
   start of the next round.
3. **Options** (top right) has everything else: models, settings, files, and the event log.
4. **New run** (top right of Activity) deletes the current run and starts fresh.

### If something goes wrong

| Problem | What to do |
|---|---|
| "No API key found" on the setup page | Paste a key there (Way A), or use Way B or C and restart the app. |
| "daily free-tier quota exhausted" | That model's free daily limit is used up. In **Options → Agents & models**, switch the agent to another model (each model has its own limit), then press **Retry failed agents**. Or wait until the next day. |
| "Model ... is no longer available" | Pick another model in **Options → Agents & models**. |
| `address already in use` when starting | The app is already running somewhere. Close it, or start on another port: `./run.sh --port 8766`. |
| `./run.sh: Permission denied` | Run `chmod +x run.sh` once. |

### Keep your key safe

- **Never put your key in any other file in this folder, and never commit it.** `roundtable/local_keys.py` and the
  `data/` folder (where keys typed into the app are saved) are already excluded from git.
- Before you push, run `git status`. It must **not** list `roundtable/local_keys.py`, anything in `data/`, or a `.env` file.
- If your key ever ends up on GitHub or in a chat, **delete it** at the provider (link in Step 1) and make a new one.
  Removing it from git afterwards is not enough, because old commits and copies keep it.
- The app never shows your key in the browser, never writes it to logs, and never gives it to the agents' code.

### Extra options

- `./run.sh --port 8766` starts on another port. The app only listens on your own computer (127.0.0.1).
- All runs are saved in `data/`. Set `ROUNDTABLE_DATA=/some/folder` to keep them somewhere else.
- `OPENAI_BASE_URL`, `GEMINI_BASE_URL` and `DEEPSEEK_BASE_URL` point the app at a different compatible server (for example a local model).
- Without `run.sh`:

  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m roundtable --port 8765
  ```

## The round protocol

For round *t* with members *A₁…Aₙ*, the engine (`roundtable/engine.py`) runs these steps:

1. **Freeze** (one DB transaction, nothing awaited in between):
   * pending user messages, uploads and workspace edits become visible from round *t*;
   * membership is pinned: enabled agents in position order, each with its current instruction version and config;
   * every member's full context is built from that snapshot and stored. Contexts are built before any agent starts.
2. **Execute**: all member runs execute concurrently (`max_concurrency`). Nothing they produce is visible to anyone else:
   * outputs are stored with `visible_from_round = NULL`;
   * file writes go to a private per-run copy of the file tree;
   * workspace edits are *staged proposals*.
3. **Barrier**: the engine waits for every run. A failed run never silently drops out. Depending on the failure policy it is:
   * retried with the *same frozen context*;
   * left for you to resolve (**Retry failed agents**, **Continue without them**, **Discard round**);
   * or, only if you chose `skip_failed`, recorded as skipped.
4. **Commit** (one transaction):
   * outputs become visible from *t+1*;
   * staged workspace proposals, memory and files are committed, with conflicts preserved;
   * messages delivered in round *t* are marked *processed*, and the round is marked completed.
5. **Post-round checks**, in this order: stop, convergence, max rounds, budget, breakpoints, pause request, step mode.

Only after that can round *t+1* freeze. Guarantees that follow from this design (each one is tested):

* No agent sees same-round output from another agent, whether through context, workspace, files, transcript or tool records.
* There is exactly one engine loop per project, so rounds never overlap. A partial unique index on `(project, round)`
  makes a duplicate round impossible at the database level.
* Interventions submitted mid-round are queued as **pending** and delivered at the next freeze, then shown as
  **incorporated** and finally **processed**. Instruction and config edits mid-round are versioned and take effect at the next boundary.
* After a crash or restart, a running round becomes **interrupted**. Nothing re-runs automatically; you retry
  explicitly, and only the unfinished agents run again.
* **Stop** cancels in-flight calls. Completed rounds are kept, and the partial round is marked *aborted* (retry or discard it).
  **Pause** always lets the current round finish first.

## Using the GUI

There are two pages, plus Options:

* **Setup** (shown when no run exists) asks for:
  * the **goal**;
  * the **API key**: it uses the one you have, asks which one if there are several, and lets you add one if there are none;
  * **how many agents**, with one role prompt each (optionally **+ Add a neutral judge**);
  * the **maximum number of rounds**.

  Then **Start**.
* **Activity** has two kinds of tab:
  * **Common ground**: everything every agent sees. That is the goal, each round's outputs, and the **full content of every
    file the agents wrote**, inline and as it was at that round (Markdown and LaTeX rendered, code and data as text,
    images inline). Anything you add here (text and/or files) is seen by all agents from the next round.
  * **One tab per agent**: that agent's own turns, plus a private ground. Anything you add there is seen only by that agent, from
    the next round.

  The header has Start / Continue, Pause after this round, Stop, recovery actions if an agent fails, and **New run**.
  Each run is fresh: New run deletes the previous one.
* **Options** holds everything else: agents and models, settings and keys, workspace, files and sources, convergence, a
  detailed timeline (side-by-side grid), and the event log. Each agent turn also has a **details** link to the exact
  context, tool calls and transcript.

## Context management (`roundtable/context.py`)

Each run receives:

* a neutral protocol preamble (how rounds work; standards on evidence, citations and disagreement), followed by **your instructions**;
* the task;
* the last `recent_rounds_full` rounds verbatim, with each agent's tool-call list;
* common-channel and private-channel interventions (marked NEW when they arrived this round);
* the agent's private memory (if enabled);
* the workspace snapshot, the file list, and the list of retrieved sources;
* older rounds as summaries (optional `summarize_older`) or as pointers.

Sections are filled in priority order under `max_context_tokens`, and anything cut is marked. Agents can always read the
exact original text with `read_round`, `read_tool_result` and `workspace_read`. Summaries are stored separately in
`round_summaries`; the raw transcript is never modified.

## Convergence (`roundtable/convergence.py`)

When convergence is enabled, each agent ends its output with a fenced `convergence` JSON block:
`support_termination`, `open_objections`, `unresolved_steps`, `unsupported_claims`, `rationale`.
It is parsed and stored separately. After each round, the policy requires *all* of:

* the minimum number of rounds has passed;
* every member gave a parsable assessment;
* the support fraction is reached;
* objections, unresolved steps and unsupported claims are within their limits;
* optionally, output stability (word-trigram overlap with the previous round);
* no unresolved workspace conflicts;
* all of the above for N consecutive rounds.

Agreement alone never ends a run, and every decision is recorded with its evidence. `max_rounds` always applies as a hard stop.

## Tools (`roundtable/tools/`)

| group | tools | notes |
|---|---|---|
| workspace | `workspace_read`, `workspace_propose` | proposals are committed at the boundary |
| files | `list_files`, `read_file` (text/PDF), `write_file` | writes are staged in a per-run copy |
| transcript | `read_round`, `list_tool_calls`, `read_tool_result`, `list_sources` | earlier rounds only; private items stay private |
| memory | `memory_write` | private to the agent, committed at the boundary |
| web_search / fetch_url | DuckDuckGo HTML search, page/PDF fetch | no key needed; retrieved pages are recorded as sources |
| arxiv | `arxiv_search`, `arxiv_read` (full text via pypdf) | requests are spaced ≥ 3 s across all agents, with retries |
| python | `run_python` | numpy/sympy available; torch too if installed in the chosen interpreter |
| bash | `run_bash` | **off by default** (Settings → Tools) |

Permissions combine per-agent tool groups with project-wide vetoes (network / Python / Bash).

**Sandbox.** Python and Bash run in the agent's staged copy of the project files, so they cannot change shared state
mid-round. Their outputs become shared only at the boundary. When two agents change the same file in one round, both
versions are kept as `<file>.conflict-r<t>-<agent>` and the conflict is logged.

* With working **bubblewrap**, processes see a read-only host, a hidden `$HOME` and app data, a private `/tmp`, and no network unless allowed.
* Where bubblewrap cannot create namespaces (as on this machine, where AppArmor blocks unprivileged user
  namespaces), the app falls back to **guarded** mode:
  * a scrubbed environment without API keys, CPU/memory/file-size limits, and a process-group kill on timeout;
  * a Python audit hook that blocks writes and deletes outside the working copy, process spawning, reading
    app data, and (optionally) network access;
  * a Bash command filter: no sudo, no absolute paths outside the workspace, no `..`, no destructive system commands.

  Guarded mode is defence in depth, **not a security boundary**. Determined code (for example via ctypes) could
  escape it. Use it with models you trust, or enable bubblewrap or run the app inside a container or VM.
  The active mode is shown in the sidebar and in Settings.

## Budgets, failures, providers

* **Budget**: total tokens and USD cost. It is checked before each round and before *every* model call, so a call that
  would exceed it mid-round fails visibly instead of spending more. Prices live in `pricing.json`; overrides go
  in Settings. The shipped prices are best-effort and may be out of date, and unknown models show cost "n/a" while tokens still count.
* **Retries**: rate limits and transient errors (408/409/429/5xx/529, network errors) back off exponentially and
  honour `retry-after`. Each retry is logged in Activity.
* **Providers** (`roundtable/providers/`):
  * Anthropic Messages API, OpenAI Chat Completions and the Google AI Studio Gemini API (`generateContent`) via
    `httpx`, behind one interface. The Gemini key is sent in the `x-goog-api-key` header, never in the URL; Gemini
    thought signatures are echoed back during tool loops, and thinking tokens are counted as output tokens.
  * Each agent picks its own provider and model.
  * Temperature is only sent when set (some reasoning models reject it).
  * Tool-use loops are capped by `max_tool_iterations`; the final call disables tools.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/test_sync.py` covers the protocol with a deterministic mock provider:

* one-agent and three-agent runs;
* no same-round visibility (context, workspace, files and transcript tools);
* the barrier (round *t+1* never starts before round *t* ends; there is real concurrency within a round);
* mid-round messages delivered at the boundary, and private-channel privacy;
* breakpoints, pause and resume, persistence across restarts, and crash recovery with explicit, targeted retry;
* API failures leaving shared state untouched, auto-retry and skip policies, rate-limit retries;
* consistent snapshots across agents, and instruction changes applied at the boundary;
* disabling an agent mid-round, stop and discard, and duplicate-round rejection.

`tests/test_features.py` covers:

* workspace and file conflicts;
* Python artifacts and the guard, the Bash policy;
* private memory;
* the convergence policy, both unit-level and stopping a run;
* the token budget at the boundary and mid-round;
* context truncation, with originals kept intact;
* summaries, and upload delivery.

`tests/test_api.py` drives the HTTP API end to end and checks that keys never leak.
`tests/test_deepseek_provider.py` does the same for DeepSeek.
`tests/test_google_provider.py` runs the Gemini provider against a fake endpoint (request format, tool loop,
thought signatures, token accounting, retries, blocked prompts, key sources).

Real-provider smoke test (two agents, two rounds, a few cents):

```bash
RT_LIVE_TEST=1 RT_LIVE_PROVIDER=anthropic RT_LIVE_MODEL=claude-haiku-5-5 .venv/bin/python -m pytest tests/test_live_provider.py
RT_LIVE_TEST=1 RT_LIVE_PROVIDER=google GEMINI_API_KEY=... .venv/bin/python -m pytest tests/test_live_provider.py
RT_LIVE_TEST=1 RT_LIVE_PROVIDER=deepseek DEEPSEEK_API_KEY=... .venv/bin/python -m pytest tests/test_live_provider.py
```

## Layout

```
roundtable/
  engine.py        round protocol: freeze / execute / barrier / commit, failure policies, recovery
  context.py       per-agent context assembly from the frozen snapshot
  workspace.py     structured entries (versioned, conflict-preserving) + staged file tree
  convergence.py   transparent convergence policy
  service.py       project/agent/message operations
  api.py           FastAPI routes (+ serves frontend/dist)
  db.py            SQLite schema
  config.py        settings defaults, secrets, pricing
  providers/       anthropic.py, openai.py, mock.py, base.py (retries)
  tools/           registry.py (tool definitions + recording), sandbox.py, web.py
frontend/src/      React UI (Vite)
tests/             pytest suite
```

## Known limitations

* The guarded sandbox is not a hard isolation boundary (see above).
* Web search scrapes DuckDuckGo's HTML endpoint, which can rate-limit or change format. The arXiv API also
  rate-limits aggressively per IP; on 429s the tool tells the agent to fall back to `web_search` + `fetch_url`.
* The UI polls once a second (no websockets). Large projects re-fetch all messages when anything changes.
* Token counts come from provider usage fields. The context budget is estimated at 4 characters per token.
* GPU access is whatever the chosen Python interpreter can see (`CUDA_VISIBLE_DEVICES` is passed through). There is
  no GitHub or semantic-search tool yet.
