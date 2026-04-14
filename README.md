# LLM Wiki Agent

A local LLM-powered personal knowledge base, based on Andrej Karpathy's wiki pattern.
Runs entirely on your machine via Ollama — no external APIs, no cloud services.

## How It Works

You drop source documents into `raw/`. The agent reads them, discusses the content
with you, and builds a structured wiki in `wiki/` — summaries, concept pages, people,
projects, reference material, cross-linked and sourced. The more you feed it, the
richer and more connected it gets.

**No hallucination by design:** the agent is instructed to only state facts present
in the source document currently in its context window. Every wiki page includes a
`sources:` field pointing back to the raw file it came from. You approve every write.

---

## Quickstart

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Pull the model

```
ollama pull qwen2.5:14b
```

If 14b is too slow on your hardware, use 7b:
```
ollama pull qwen2.5:7b
python wiki_agent.py --model qwen2.5:7b
```

### 3. Drop a file into raw/

Copy any `.docx`, `.pdf`, `.txt`, or `.md` file into the `raw/` directory.

### 4. Start an interactive session

```
python wiki_agent.py
```

The agent will orient itself, report what's in the queue, and ask what you want to do.

### 5. Ingest the file

Type:
```
ingest my-document.pdf
```

The agent will:
1. Extract and read the full document
2. Summarize key takeaways and ask clarifying questions
3. Propose which wiki pages to create
4. Wait for you to type `write <path>` to approve each page
5. Update the index, glossary, and log

Type `done` when you're finished with that document.

---

## Commands (Interactive Mode)

| Command | What it does |
|---|---|
| `ingest <filename>` | Start ingesting a file from raw/ |
| `query <question>` | Ask a question answered from wiki content |
| `lint` | Run a wiki health check |
| `queue` | Show files waiting to be ingested |
| `read <wiki/path>` | Display a wiki page |
| `write <path>` | (During ingest) Approve writing a page the model described |
| `done` | (During ingest) Mark the current file as ingested |
| `quit` | Exit |

---

## CLI Modes

```
python wiki_agent.py                     # interactive session
python wiki_agent.py --mode daily        # scan for new files (no Ollama needed)
python wiki_agent.py --mode lint         # weekly lint pass
python wiki_agent.py --mode ingest FILE  # jump straight to ingesting a file
python wiki_agent.py --model qwen2.5:7b  # use a different model
```

---

## Scheduling

See `SCHEDULING.md` for Windows Task Scheduler setup:
- **Daily** (no Ollama needed): scans `raw/` for new files and updates `wiki/queue.md`
- **Weekly** (requires Ollama): runs a lint pass and saves a report to `wiki/analyses/`

---

## File Support

| Extension | Parser |
|---|---|
| `.pdf` | pymupdf (preferred), pdfplumber (fallback) |
| `.docx` | python-docx — preserves headings and tables |
| `.txt` `.md` `.log` `.rst` | plain text |
| `.csv` | converted to markdown table |

---

## Customizing for Your Domain

Edit `SYSTEM.md` to fit your work context. It's the agent's operating manual —
controls page types, ingest workflow, terminology discipline, and output format.
Changes take effect on the next session start.

---

## Model Recommendations

| Model | Notes |
|---|---|
| `qwen2.5:14b` | **Recommended.** Best instruction-following, 128k context. |
| `qwen2.5:7b` | Good fallback if 14b is too slow on CPU. |
| `mistral-nemo:12b` | Strong alternative; slightly weaker structured output. |

All models run at `temperature: 0.1` to maximize faithfulness to source text.
