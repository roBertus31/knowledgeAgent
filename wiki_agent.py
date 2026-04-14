"""
wiki_agent.py
Local LLM knowledge wiki agent — Karpathy pattern, Ollama backend.

Usage:
    python wiki_agent.py                                    # interactive session (default)
    python wiki_agent.py --mode interactive                 # same
    python wiki_agent.py --mode lint                        # run lint pass (non-interactive)
    python wiki_agent.py --mode ingest FILE                 # ingest a specific file interactively
    python wiki_agent.py --mode query --question "..."      # one-shot query, stdout output
    python wiki_agent.py --model qwen2.5:7b                 # override model
    python wiki_agent.py --wiki /path/to/wiki               # override wiki directory
    python wiki_agent.py --raw  /path/to/raw                # override raw directory

Query mode is designed for scripting and agent integration:
    python wiki_agent.py --mode query --question "What is X?" --output json
    python wiki_agent.py --mode query --question "What is X?" --output plain
    python wiki_agent.py --mode query --question "What is X?" --pages concepts/x.md reference/y.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import requests
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from file_parser import parse_file, supported_extensions
from wiki_tools import (
    QUEUE_FILE,
    append_log,
    init_wiki,
    list_all_pages,
    now_for_log,
    read_glossary,
    read_index,
    read_log,
    read_overview,
    read_queue,
    read_wiki_page,
    scan_raw,
    slugify,
    touch_updated,
    update_index_entry,
    update_queue,
    write_wiki_page,
)

# ── configuration ─────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent.resolve()
DEFAULT_WIKI = SCRIPT_DIR / "wiki"
DEFAULT_RAW  = SCRIPT_DIR / "raw"
DEFAULT_MODEL = "qwen2.5:14b"
OLLAMA_URL    = "http://localhost:11434/api/chat"

# Context budget: how much wiki content to inject at session start.
# Ollama with qwen2.5:14b supports 128k context, but we stay conservative
# to leave room for source documents and conversation.
MAX_CONTEXT_WIKI_CHARS = 40_000

console = Console()


# ── ollama client ─────────────────────────────────────────────────────────────

def check_ollama(model: str) -> bool:
    """Verify Ollama is running and the model is available."""
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        tags = r.json().get("models", [])
        available = [m["name"] for m in tags]
        # Check exact match or prefix match (e.g. "qwen2.5:14b" vs "qwen2.5:14b-instruct-q4_K_M")
        if any(m == model or m.startswith(model.split(":")[0]) for m in available):
            return True
        console.print(f"[yellow]Warning:[/yellow] Model [bold]{model}[/bold] not found in Ollama.")
        console.print(f"Available models: {', '.join(available) or '(none)'}")
        console.print(f"Pull it with: [bold]ollama pull {model}[/bold]")
        return False
    except requests.ConnectionError:
        console.print("[red]Error:[/red] Cannot connect to Ollama at localhost:11434")
        console.print("Make sure Ollama is running: [bold]ollama serve[/bold]")
        return False


def chat(
    messages: list[dict],
    model: str,
    stream: bool = True,
) -> str:
    """
    Send a chat request to Ollama. Returns the full assistant response string.
    Streams output to console if stream=True.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": {
            "temperature": 0.1,   # low temperature = more faithful to source text
            "num_ctx": 65536,     # context window — tuned for 64GB RAM / 12GB VRAM
        },
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, stream=stream, timeout=300)
        response.raise_for_status()
    except requests.ConnectionError:
        console.print("[red]Error:[/red] Lost connection to Ollama.")
        return ""
    except requests.Timeout:
        console.print("[red]Error:[/red] Ollama request timed out.")
        return ""

    full_response = []

    if stream:
        console.print()  # blank line before response
        current_line = []
        for line in response.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = chunk.get("message", {}).get("content", "")
            if token:
                print(token, end="", flush=True)
                full_response.append(token)
            if chunk.get("done"):
                break
        print()  # newline after stream ends
    else:
        data = response.json()
        full_response.append(data.get("message", {}).get("content", ""))

    return "".join(full_response)


# ── context builders ──────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    system_path = SCRIPT_DIR / "SYSTEM.md"
    if system_path.exists():
        return system_path.read_text(encoding="utf-8")
    return "You are a personal knowledge base curator. Be precise and cite sources."


def build_session_context(wiki_dir: Path) -> str:
    """
    Build the wiki context injected at session start.
    Includes index, recent log, queue, overview, and glossary — within budget.
    """
    parts = []
    budget = MAX_CONTEXT_WIKI_CHARS

    def add(label: str, content: str) -> None:
        nonlocal budget
        if not content.strip():
            return
        block = f"\n\n=== {label} ===\n{content}"
        if len(block) <= budget:
            parts.append(block)
            budget -= len(block)

    add("wiki/index.md", read_index(wiki_dir))
    add("wiki/log.md (recent entries)", read_log(wiki_dir, last_n_entries=10))
    add("wiki/queue.md", read_queue(wiki_dir))
    add("wiki/overview.md", read_overview(wiki_dir))
    add("wiki/glossary.md", read_glossary(wiki_dir))

    if not parts:
        return "(Wiki is empty — no pages yet.)"
    return "".join(parts)


def build_ingest_context(source_path: Path) -> str:
    """Extract text from a source file and wrap it for the prompt."""
    text, meta = parse_file(source_path)
    if "error" in meta:
        return f"ERROR reading {source_path.name}: {meta['error']}"
    header = (
        f"=== SOURCE FILE: {source_path.name} ===\n"
        f"Path: {source_path}\n"
        f"Size: {source_path.stat().st_size:,} bytes\n"
    )
    if meta:
        for k, v in meta.items():
            if v and k != "error":
                header += f"{k}: {v}\n"
    header += f"Extracted text ({len(text):,} chars):\n\n"
    return header + text


# ── page write helper (interactive) ──────────────────────────────────────────

def prompt_and_write(
    wiki_dir: Path,
    suggested_path: str,
    content: str,
    label: str = "page",
) -> bool:
    """
    Show the user what the model wants to write, ask for approval.
    Returns True if written, False if skipped.
    """
    console.print(f"\n[cyan]── Proposed wiki {label}:[/cyan] [bold]{suggested_path}[/bold]")
    console.print(Panel(
        content[:2000] + ("\n\n[...truncated for preview...]" if len(content) > 2000 else ""),
        title=suggested_path,
        border_style="dim",
    ))
    choice = Prompt.ask(
        "[green]Write this page?[/green]",
        choices=["y", "n", "edit"],
        default="y",
    )
    if choice == "n":
        console.print("[dim]Skipped.[/dim]")
        return False
    if choice == "edit":
        console.print("[yellow]Paste your edited content (end with a line containing only '---END---'):[/yellow]")
        lines = []
        while True:
            line = input()
            if line.strip() == "---END---":
                break
            lines.append(line)
        content = "\n".join(lines)

    write_wiki_page(wiki_dir, suggested_path, content)
    console.print(f"[green]✓ Written:[/green] wiki/{suggested_path}")
    return True


# ── session history management ────────────────────────────────────────────────

class WikiSession:
    """Manages a single interactive or scripted session."""

    def __init__(self, wiki_dir: Path, raw_dir: Path, model: str):
        self.wiki_dir = wiki_dir
        self.raw_dir  = raw_dir
        self.model    = model
        self.system   = load_system_prompt()
        self.history: list[dict] = []  # conversation messages (user + assistant)

    def _messages(self, extra_user: str | None = None) -> list[dict]:
        """Build the full messages array for Ollama."""
        msgs = [{"role": "system", "content": self.system}]
        msgs.extend(self.history)
        if extra_user:
            msgs.append({"role": "user", "content": extra_user})
        return msgs

    def say(self, user_message: str, silent: bool = False) -> str:
        """Send a message, get a response, update history."""
        self.history.append({"role": "user", "content": user_message})
        response = chat(self._messages(), self.model, stream=not silent)
        self.history.append({"role": "assistant", "content": response})
        return response

    def reset_history(self) -> None:
        self.history = []


# ── modes ─────────────────────────────────────────────────────────────────────

def mode_query(
    wiki_dir: Path,
    raw_dir: Path,
    model: str,
    question: str,
    output_format: str = "plain",
    specific_pages: list[str] | None = None,
) -> None:
    """
    One-shot query mode: answer a question from wiki content and exit.
    Designed for scripting and agent-to-agent integration.

    output_format:
        "plain"  — human-readable text to stdout
        "json"   — JSON object: {"question": ..., "answer": ..., "pages_consulted": [...]}
    specific_pages:
        Optional list of wiki-relative paths to load directly instead of
        auto-selecting from the index. Useful when the calling agent already
        knows which pages are relevant.
    """
    session = WikiSession(wiki_dir, raw_dir, model)

    # Build context: index for auto-selection, or load specific pages directly
    if specific_pages:
        page_blocks = []
        pages_consulted = []
        for rel_path in specific_pages:
            content = read_wiki_page(wiki_dir, rel_path)
            if content:
                page_blocks.append(f"=== wiki/{rel_path} ===\n{content}")
                pages_consulted.append(rel_path)
            else:
                console.print(f"[yellow]Warning:[/yellow] Page not found: wiki/{rel_path}",
                               file=sys.stderr)
        wiki_context = "\n\n".join(page_blocks) if page_blocks else "(No pages loaded.)"
        context_note = f"Loaded {len(pages_consulted)} specific page(s)."
    else:
        # Give the model the index and let it identify relevant pages.
        # Then load those pages for a second grounded pass.
        idx = read_index(wiki_dir)
        overview = read_overview(wiki_dir)
        wiki_context = f"=== wiki/index.md ===\n{idx}\n\n=== wiki/overview.md ===\n{overview}"
        pages_consulted = []
        context_note = "Used index + overview for page selection."

    query_prompt = (
        f"Question: {question}\n\n"
        f"Wiki content available:\n{wiki_context}\n\n"
        "Instructions:\n"
        "- Answer based strictly on the wiki content provided above.\n"
        "- Cite specific pages using [[page-name]] notation for every factual claim.\n"
        "- If the answer is not present in the wiki, state that explicitly — "
        "do not supplement from general knowledge.\n"
        "- If additional pages would improve your answer, list them at the end "
        "under 'Suggested pages to load:' so the caller can re-run with --pages.\n"
        "- Keep your answer focused and direct."
    )

    # Use silent mode so raw answer goes to stdout cleanly
    if output_format == "json":
        answer = session.say(query_prompt, silent=True)
        import json as _json
        result = {
            "question": question,
            "answer": answer,
            "pages_consulted": pages_consulted,
            "model": model,
            "wiki_dir": str(wiki_dir),
        }
        print(_json.dumps(result, indent=2, ensure_ascii=False))
    else:
        # plain: stream to stdout naturally
        answer = session.say(query_prompt, silent=False)

    # Log the query
    log_entry = (
        f"\n## [{now_for_log()}] query\n"
        f"Question: {question}\n"
        f"Mode: {output_format} | {context_note}\n"
        f"Pages consulted: {', '.join(pages_consulted) or 'auto-selected from index'}\n"
        f"Output filed: no\n"
    )
    append_log(wiki_dir, log_entry)


def mode_lint(wiki_dir: Path, raw_dir: Path, model: str) -> None:
    """
    Weekly lint job: run a lint pass against the full wiki.
    Non-interactive — writes a lint report to wiki/analyses/.
    """
    console.print(Panel(
        f"[bold]Weekly Lint Pass[/bold]  {now_for_log()}",
        border_style="magenta",
    ))

    pages = list_all_pages(wiki_dir)
    if not pages:
        console.print("[yellow]Wiki is empty — nothing to lint.[/yellow]")
        return

    console.print(f"Reading {len(pages)} wiki page(s)...")

    # Build a condensed representation of all pages for the lint prompt
    page_summaries = []
    total_chars = 0
    LINT_BUDGET = 50_000

    for page in pages:
        rel = page.relative_to(wiki_dir)
        content = page.read_text(encoding="utf-8", errors="replace")
        block = f"\n--- FILE: wiki/{rel} ---\n{content[:1500]}"
        if total_chars + len(block) > LINT_BUDGET:
            page_summaries.append(f"\n--- [Remaining pages truncated — budget reached] ---")
            break
        page_summaries.append(block)
        total_chars += len(block)

    wiki_dump = "".join(page_summaries)

    lint_prompt = (
        "Please perform a full lint pass on this wiki. Review all pages provided and report:\n\n"
        "1. Pages missing `sources:` frontmatter (unsourced content)\n"
        "2. Factual contradictions between pages\n"
        "3. Orphan pages (not linked from any other page)\n"
        "4. Terms used inconsistently with the glossary\n"
        "5. Missing cross-references that should logically exist\n"
        "6. Stale or superseded claims (if dates are available)\n\n"
        "For each issue, specify: the affected file, the problem, and a proposed fix.\n"
        "Format your report as markdown with ## sections per issue type.\n"
        "At the end, provide a summary count of issues by type.\n\n"
        "=== WIKI CONTENT ===\n"
        f"{wiki_dump}\n"
        "=== END WIKI CONTENT ==="
    )

    session = WikiSession(wiki_dir, raw_dir, model)
    console.print("\n[magenta]Running lint analysis (this may take a while)...[/magenta]\n")
    report = session.say(lint_prompt, silent=False)

    if report:
        today = datetime.now().strftime("%Y-%m-%d")
        report_path = f"analyses/lint-{today}.md"
        frontmatter = (
            f"---\ntitle: Lint Report {today}\ntype: analysis\n"
            f"created: {today}\nupdated: {today}\n"
            f"sources: []\ntags: [lint, maintenance]\n---\n\n"
        )
        write_wiki_page(wiki_dir, report_path, frontmatter + report)
        console.print(f"\n[green]✓ Lint report saved:[/green] wiki/{report_path}")

        log_entry = (
            f"\n## [{now_for_log()}] lint\n"
            f"Pages reviewed: {len(pages)}\n"
            f"Report saved: wiki/{report_path}\n"
        )
        append_log(wiki_dir, log_entry)


def mode_interactive(
    wiki_dir: Path,
    raw_dir: Path,
    model: str,
    initial_ingest: Path | None = None,
) -> None:
    """
    Full interactive REPL session.
    Optionally starts by ingesting a specific file.
    """
    console.print(Panel(
        Text.assemble(
            ("LLM Wiki Agent\n", "bold cyan"),
            (f"Model: {model}  |  ", "dim"),
            (f"wiki/ → {wiki_dir}  |  ", "dim"),
            (f"raw/  → {raw_dir}", "dim"),
        ),
        border_style="cyan",
    ))

    session = WikiSession(wiki_dir, raw_dir, model)

    # ── scan raw/ for new files before doing anything else ────────────────────
    new_files = scan_raw(raw_dir, wiki_dir)
    update_queue(wiki_dir, new_files)
    if new_files:
        console.print(
            f"\n[yellow]Scan:[/yellow] {len(new_files)} new file(s) added to queue — "
            f"{', '.join(f.name for f in new_files)}"
        )
        scan_log = (
            f"\n## [{now_for_log()}] scan\n"
            f"New files detected: {', '.join(f.name for f in new_files)}\n"
            f"Action: added to queue.md.\n"
        )
        append_log(wiki_dir, scan_log)
    else:
        console.print("\n[dim]Scan: no new files in raw/.[/dim]")

    # ── session start: orient the model ──────────────────────────────────────
    wiki_context = build_session_context(wiki_dir)
    queue_text   = read_queue(wiki_dir)

    orientation = (
        "You are starting a new wiki curation session. Here is the current state of the wiki:\n\n"
        f"{wiki_context}\n\n"
        "Please:\n"
        "1. Confirm you've oriented yourself with the current wiki state.\n"
        "2. Report any files currently in the queue.\n"
        "3. Ask what the user would like to do.\n"
        "Keep your response brief."
    )

    console.print("\n[dim]Orienting agent with current wiki state...[/dim]\n")
    session.say(orientation)

    # ── optional: jump straight into an ingest ────────────────────────────────
    if initial_ingest:
        _interactive_ingest(session, wiki_dir, initial_ingest)

    # ── main REPL ─────────────────────────────────────────────────────────────
    console.print(
        "\n[dim]Commands: 'ingest <file>' | 'query <question>' | 'lint' | "
        "'read <wiki/path>' | 'queue' | 'quit'[/dim]\n"
    )

    while True:
        try:
            user_input = Prompt.ask("[bold green]you[/bold green]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting.[/dim]")
            break

        if not user_input:
            continue

        lower = user_input.lower()

        # ── built-in commands ─────────────────────────────────────────────────

        if lower in ("quit", "exit", "q"):
            console.print("[dim]Session ended.[/dim]")
            break

        elif lower == "queue":
            q = read_queue(wiki_dir)
            console.print(Markdown(q))
            continue

        elif lower.startswith("read "):
            rel_path = user_input[5:].strip()
            content = read_wiki_page(wiki_dir, rel_path)
            if content:
                console.print(Markdown(content))
            else:
                console.print(f"[red]Not found:[/red] wiki/{rel_path}")
            continue

        elif lower.startswith("ingest "):
            filename = user_input[7:].strip()
            # Accept bare filename or path
            candidate = raw_dir / filename if not Path(filename).is_absolute() else Path(filename)
            if not candidate.exists():
                # Try without raw/ prefix
                candidate2 = Path(filename)
                if candidate2.exists():
                    candidate = candidate2
                else:
                    console.print(f"[red]File not found:[/red] {candidate}")
                    continue
            _interactive_ingest(session, wiki_dir, candidate)
            continue

        elif lower == "lint":
            console.print("[magenta]Running lint analysis...[/magenta]")
            mode_lint(wiki_dir, raw_dir, model)
            continue

        elif lower.startswith("query "):
            # Load relevant wiki pages into context before answering
            question = user_input[6:].strip()
            idx = read_index(wiki_dir)
            augmented = (
                f"Question: {question}\n\n"
                f"Current wiki index:\n{idx}\n\n"
                "Please answer based strictly on wiki content. "
                "If the answer isn't in the wiki, say so explicitly. "
                "After answering, ask if I should file this as an analysis page."
            )
            session.say(augmented)
            continue

        # ── free-form conversation ────────────────────────────────────────────
        else:
            session.say(user_input)


def _interactive_ingest(
    session: WikiSession,
    wiki_dir: Path,
    source_path: Path,
) -> None:
    """Run the ingest workflow for a single source file."""
    console.print(f"\n[cyan]── Ingesting:[/cyan] [bold]{source_path.name}[/bold]")

    # Parse the file
    source_context = build_ingest_context(source_path)
    if source_context.startswith("ERROR"):
        console.print(f"[red]{source_context}[/red]")
        return

    char_count = len(source_context)
    console.print(f"[dim]Extracted {char_count:,} chars from {source_path.name}[/dim]\n")

    # Reset history for clean ingest context
    session.reset_history()
    wiki_ctx = build_session_context(session.wiki_dir)

    ingest_prompt = (
        f"Please ingest the following source document.\n\n"
        f"Current wiki state:\n{wiki_ctx}\n\n"
        f"SOURCE DOCUMENT:\n{source_context}\n\n"
        "Follow the ingest workflow from your instructions:\n"
        "1. Summarize key takeaways (ask me 1-3 clarifying questions if needed)\n"
        "2. Tell me what wiki pages you plan to create or update\n"
        "3. Wait for my go-ahead before writing anything\n\n"
        "Important: Only state facts that are present in the source document above."
    )

    session.say(ingest_prompt)

    # ── collaborative ingest loop ─────────────────────────────────────────────
    console.print(
        "\n[dim]Continue the ingest conversation. "
        "Type 'write <path>' to approve a page, "
        "'done' when finished ingesting this file.[/dim]\n"
    )

    while True:
        try:
            user_input = Prompt.ask("[bold green]you[/bold green]").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue

        lower = user_input.lower()

        if lower == "done":
            # Log the ingest
            log_entry = (
                f"\n## [{now_for_log()}] ingest | {source_path.name}\n"
                f"Interactive ingest session completed.\n"
                f"See conversation above for pages created/updated.\n"
            )
            append_log(session.wiki_dir, log_entry)
            # Remove from queue
            remaining = scan_raw(session.raw_dir, session.wiki_dir)
            update_queue(session.wiki_dir, remaining)
            console.print(f"[green]✓ Ingest of {source_path.name} marked complete.[/green]\n")
            break

        elif lower.startswith("write "):
            # User explicitly approves writing a page the model described
            rel_path = user_input[6:].strip()
            write_prompt = (
                f"Please write the complete wiki page for `wiki/{rel_path}` now. "
                "Output ONLY the full markdown content for this page, including "
                "YAML frontmatter. Nothing else before or after — just the page content."
            )
            page_content = session.say(write_prompt, silent=False)
            if page_content:
                # Strip any accidental code fences the model might add
                page_content = _strip_code_fences(page_content)
                written = prompt_and_write(
                    session.wiki_dir, rel_path, page_content, label="page"
                )
                if written:
                    # Extract one-line summary for index update
                    summary = _extract_summary(page_content)
                    update_index_entry(session.wiki_dir, rel_path, summary)

        else:
            session.say(user_input)


# ── utilities ─────────────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """Remove ```markdown or ``` fences that models sometimes add."""
    import re
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _extract_summary(page_content: str) -> str:
    """Pull the first non-frontmatter, non-header sentence as the one-line summary."""
    import re
    # Strip frontmatter
    if page_content.startswith("---"):
        end = page_content.find("---", 3)
        if end != -1:
            page_content = page_content[end + 3:].strip()
    # Skip headers
    lines = page_content.splitlines()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    return "(no summary)"


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM Wiki Agent — local Ollama-backed knowledge base curator"
    )
    parser.add_argument(
        "--mode",
        choices=["interactive", "lint", "ingest", "query"],
        default="interactive",
        help="Run mode (default: interactive)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--wiki",
        default=str(DEFAULT_WIKI),
        help=f"Path to wiki directory (default: {DEFAULT_WIKI})",
    )
    parser.add_argument(
        "--raw",
        default=str(DEFAULT_RAW),
        help=f"Path to raw/ directory (default: {DEFAULT_RAW})",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="File to ingest (required for --mode ingest)",
    )
    # ── query mode args ───────────────────────────────────────────────────────
    parser.add_argument(
        "--question", "-q",
        default=None,
        help="Question to answer (required for --mode query)",
    )
    parser.add_argument(
        "--output",
        choices=["plain", "json"],
        default="plain",
        help="Output format for --mode query (default: plain)",
    )
    parser.add_argument(
        "--pages",
        nargs="*",
        default=None,
        metavar="WIKI_REL_PATH",
        help=(
            "Specific wiki pages to load for --mode query "
            "(e.g. --pages concepts/x.md reference/y.md). "
            "If omitted, the agent selects pages from the index automatically."
        ),
    )
    args = parser.parse_args()

    wiki_dir = Path(args.wiki).resolve()
    raw_dir  = Path(args.raw).resolve()

    # Ensure directory structure exists
    init_wiki(wiki_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # All modes require Ollama
    if not check_ollama(args.model):
        sys.exit(1)

    if args.mode == "lint":
        mode_lint(wiki_dir, raw_dir, args.model)

    elif args.mode == "query":
        if not args.question:
            console.print("[red]Error:[/red] --mode query requires --question \"...\"")
            sys.exit(1)
        mode_query(
            wiki_dir,
            raw_dir,
            args.model,
            question=args.question,
            output_format=args.output,
            specific_pages=args.pages,
        )

    elif args.mode == "ingest":
        if not args.file:
            console.print("[red]Error:[/red] --mode ingest requires a file argument.")
            sys.exit(1)
        target = Path(args.file)
        if not target.is_absolute():
            target = raw_dir / target
        if not target.exists():
            console.print(f"[red]Error:[/red] File not found: {target}")
            sys.exit(1)
        mode_interactive(wiki_dir, raw_dir, args.model, initial_ingest=target)

    else:  # interactive
        mode_interactive(wiki_dir, raw_dir, args.model)


if __name__ == "__main__":
    main()
