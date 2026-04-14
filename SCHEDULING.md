# Scheduling: Daily Scan + Weekly Lint

This document covers how to set up automated runs on Windows using Task Scheduler,
plus the batch scripts you'll need.

---

## Batch Scripts

Create these two `.bat` files in the same directory as `wiki_agent.py`.

### `run_daily.bat`

```bat
@echo off
REM Daily scan — detects new files in raw/ and updates the queue.
REM No Ollama required; runs silently.

set SCRIPT_DIR=%~dp0
set PYTHON=python

REM If using a venv, point to its python instead:
REM set PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe

cd /d "%SCRIPT_DIR%"
"%PYTHON%" wiki_agent.py --mode daily >> "%SCRIPT_DIR%logs\daily.log" 2>&1
```

### `run_lint.bat`

```bat
@echo off
REM Weekly lint — requires Ollama to be running.
REM Produces a lint report in wiki/analyses/.

set SCRIPT_DIR=%~dp0
set PYTHON=python
set MODEL=qwen2.5:14b

REM If using a venv:
REM set PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe

cd /d "%SCRIPT_DIR%"
"%PYTHON%" wiki_agent.py --mode lint --model %MODEL% >> "%SCRIPT_DIR%logs\lint.log" 2>&1
```

Create the `logs/` directory so the log redirects work:

```
mkdir logs
```

---

## Task Scheduler Setup

### Daily Scan Task

1. Open **Task Scheduler** (search for it in Start)
2. Click **Create Basic Task** in the right panel
3. Name it: `Wiki Daily Scan`
4. Trigger: **Daily** — set the time (e.g. 8:00 AM)
5. Action: **Start a program**
   - Program/script: Browse to your `run_daily.bat`
   - Start in: the directory containing `wiki_agent.py`
6. Finish, then open the task properties:
   - **General** tab → check "Run whether user is logged on or not" (optional)
   - **General** tab → check "Run with highest privileges" (may be needed for file access)
   - **Settings** tab → uncheck "Stop the task if it runs longer than 3 days"

### Weekly Lint Task

1. Click **Create Basic Task**
2. Name it: `Wiki Weekly Lint`
3. Trigger: **Weekly** — pick a day (e.g. Sunday at 9:00 AM)
4. Action: **Start a program**
   - Program/script: Browse to your `run_lint.bat`
   - Start in: the directory containing `wiki_agent.py`
5. Important: The lint task requires Ollama to be running. If Ollama isn't running
   when the task fires, it will fail gracefully and log the error.

**Note on Ollama for the lint task:** If Ollama doesn't auto-start on your work
machine, you have two options:
- Also schedule `ollama serve` to run before the lint task (add a dependency)
- Or simply run the lint manually from the terminal when you want it:
  `python wiki_agent.py --mode lint`

---

## Verifying the Tasks Work

After setting up, right-click each task and select **Run** to test it manually.
Check the `logs/` directory for output.

For the daily task, you can also check `wiki/queue.md` — it will list any new
files found in `raw/`.

---

## Directory Structure After Setup

```
llm_wiki/
├── wiki_agent.py         ← main agent
├── file_parser.py        ← document extraction
├── wiki_tools.py         ← wiki file operations
├── SYSTEM.md             ← agent system prompt (customize for your domain)
├── requirements.txt
├── run_daily.bat         ← create this
├── run_lint.bat          ← create this
├── logs/
│   ├── daily.log         ← auto-created
│   └── lint.log          ← auto-created
├── raw/                  ← drop your source documents here
└── wiki/                 ← AI-maintained knowledge base
    ├── index.md
    ├── log.md
    ├── overview.md
    ├── glossary.md
    ├── queue.md
    ├── sources/
    ├── concepts/
    ├── people/
    ├── projects/
    ├── reference/
    └── analyses/
```

---

## Customizing SYSTEM.md for Your Domain

The `SYSTEM.md` file is the agent's operating manual — the equivalent of `CLAUDE.md`
in the original pattern. You should edit it to fit your specific work context:

- **Add domain-specific page types** in the Directory Structure section
  (e.g. `metrics/`, `stakeholders/`, `data-sources/` if you're in data/analytics)
- **Add terminology rules** specific to your organization
- **Adjust the ingest workflow** — e.g. if you always want source summaries to
  follow a specific template
- **Set the tone** — technical reference vs. narrative prose, depending on your use case

The model reads `SYSTEM.md` at the start of every session, so changes take effect
immediately on the next run.
