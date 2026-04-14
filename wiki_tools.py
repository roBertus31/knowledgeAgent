"""
wiki_tools.py
All file system operations against the wiki/ directory.
The agent calls these; nothing writes to wiki/ outside this module.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


# ── constants ─────────────────────────────────────────────────────────────────

WIKI_SUBDIRS = [
    "sources", "concepts", "people", "projects", "reference", "analyses"
]

INDEX_FILE    = "index.md"
LOG_FILE      = "log.md"
OVERVIEW_FILE = "overview.md"
GLOSSARY_FILE = "glossary.md"
QUEUE_FILE    = "queue.md"


# ── init ──────────────────────────────────────────────────────────────────────

def init_wiki(wiki_dir: Path) -> None:
    """Create wiki directory structure and stub files if they don't exist."""
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for sub in WIKI_SUBDIRS:
        (wiki_dir / sub).mkdir(exist_ok=True)

    _init_file(wiki_dir / INDEX_FILE,
        "# Wiki Index\n\nMaster catalog of all wiki pages. Updated on every ingest.\n\n"
        "## Pages\n\n*(No pages yet — start by ingesting a source document.)*\n"
    )
    _init_file(wiki_dir / LOG_FILE,
        "# Activity Log\n\nAppend-only record of all wiki operations.\n\n"
    )
    _init_file(wiki_dir / OVERVIEW_FILE,
        "# Knowledge Base Overview\n\nHigh-level synthesis of the full knowledge base.\n\n"
        "*(Not yet written — will be created after first ingest.)*\n"
    )
    _init_file(wiki_dir / GLOSSARY_FILE,
        "# Glossary\n\nLiving terminology, definitions, and style conventions.\n\n"
        "*(Empty — terms will be added during ingest.)*\n"
    )
    _init_file(wiki_dir / QUEUE_FILE,
        "# Ingest Queue\n\nFiles detected in raw/ but not yet ingested.\n\n"
        "*(Empty)*\n"
    )


def _init_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


# ── raw/ scanning ─────────────────────────────────────────────────────────────

PARSEABLE_EXTENSIONS = {
    ".txt", ".md", ".docx", ".pdf", ".csv", ".log", ".rst"
}


def scan_raw(raw_dir: Path, wiki_dir: Path) -> list[Path]:
    """
    Return list of files in raw/ that have not yet been ingested.
    A file is considered ingested if wiki/sources/<stem>.md exists.
    """
    if not raw_dir.exists():
        return []
    sources_dir = wiki_dir / "sources"
    new_files = []
    for f in sorted(raw_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in PARSEABLE_EXTENSIONS:
            ingested_marker = sources_dir / f"{f.stem}.md"
            if not ingested_marker.exists():
                new_files.append(f)
    return new_files


def update_queue(wiki_dir: Path, new_files: list[Path]) -> None:
    """Rewrite queue.md with the current list of un-ingested files."""
    queue_path = wiki_dir / QUEUE_FILE
    if not new_files:
        queue_path.write_text(
            "# Ingest Queue\n\nFiles detected in raw/ but not yet ingested.\n\n*(Empty)*\n",
            encoding="utf-8"
        )
        return

    lines = [
        "# Ingest Queue\n",
        "Files detected in `raw/` but not yet ingested.\n",
        f"Last scanned: {_now()}\n\n",
        "## Pending\n",
    ]
    for f in new_files:
        lines.append(f"- `{f.name}`")
    queue_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── reading wiki files ────────────────────────────────────────────────────────

def read_file(path: Path) -> str:
    """Read any file; return empty string if missing."""
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def read_index(wiki_dir: Path) -> str:
    return read_file(wiki_dir / INDEX_FILE)


def read_log(wiki_dir: Path, last_n_entries: int = 10) -> str:
    """Return the last N log entries (by ## header blocks)."""
    log_text = read_file(wiki_dir / LOG_FILE)
    if not log_text:
        return ""
    # Split on ## headers
    entries = re.split(r"(?=^## )", log_text, flags=re.MULTILINE)
    # entries[0] is the preamble (before first ##)
    preamble = entries[0] if not entries[0].startswith("## ") else ""
    entry_blocks = [e for e in entries if e.startswith("## ")]
    recent = entry_blocks[-last_n_entries:] if len(entry_blocks) > last_n_entries else entry_blocks
    return preamble + "".join(recent)


def read_queue(wiki_dir: Path) -> str:
    return read_file(wiki_dir / QUEUE_FILE)


def read_overview(wiki_dir: Path) -> str:
    return read_file(wiki_dir / OVERVIEW_FILE)


def read_glossary(wiki_dir: Path) -> str:
    return read_file(wiki_dir / GLOSSARY_FILE)


def read_wiki_page(wiki_dir: Path, relative_path: str) -> str:
    """Read a page by its relative path within wiki/."""
    return read_file(wiki_dir / relative_path)


def list_all_pages(wiki_dir: Path) -> list[Path]:
    """Return all .md files under wiki/, excluding the top-level management files."""
    top_level = {INDEX_FILE, LOG_FILE, OVERVIEW_FILE, GLOSSARY_FILE, QUEUE_FILE}
    pages = []
    for p in sorted(wiki_dir.rglob("*.md")):
        if p.parent == wiki_dir and p.name in top_level:
            continue
        pages.append(p)
    return pages


# ── writing wiki files ────────────────────────────────────────────────────────

def write_wiki_page(wiki_dir: Path, relative_path: str, content: str) -> Path:
    """
    Write (or overwrite) a wiki page.
    relative_path is relative to wiki_dir, e.g. 'sources/my-doc.md'
    Creates parent directories as needed.
    Returns the absolute path written.
    """
    target = wiki_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def append_log(wiki_dir: Path, entry: str) -> None:
    """Append a log entry to wiki/log.md."""
    log_path = wiki_dir / LOG_FILE
    current = read_file(log_path)
    if not current:
        current = "# Activity Log\n\nAppend-only record of all wiki operations.\n\n"
    log_path.write_text(current + entry + "\n", encoding="utf-8")


def update_index_entry(wiki_dir: Path, page_rel_path: str, one_line_summary: str) -> None:
    """
    Add or update a single entry in wiki/index.md.
    Entries are stored as: `- [[path/stem]]: one-line summary`
    """
    index_path = wiki_dir / INDEX_FILE
    content = read_file(index_path)
    stem = Path(page_rel_path).stem
    link = f"[[{page_rel_path.replace('.md', '')}]]"
    new_entry = f"- {link}: {one_line_summary}"

    # If entry already exists, replace it
    pattern = re.compile(
        r"^- \[\[" + re.escape(page_rel_path.replace(".md", "")) + r"\]\].*$",
        re.MULTILINE
    )
    if pattern.search(content):
        content = pattern.sub(new_entry, content)
    else:
        # Append to the ## Pages section, or end of file
        if "## Pages" in content:
            content = content.rstrip() + "\n" + new_entry + "\n"
        else:
            content = content.rstrip() + "\n\n## Pages\n\n" + new_entry + "\n"

    index_path.write_text(content, encoding="utf-8")


# ── frontmatter helpers ───────────────────────────────────────────────────────

def make_frontmatter(
    title: str,
    page_type: str,
    sources: list[str],
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    sources_yaml = "[" + ", ".join(sources) + "]"
    tags_yaml = "[" + ", ".join(tags or []) + "]"
    lines = [
        "---",
        f"title: {title}",
        f"type: {page_type}",
        f"created: {today}",
        f"updated: {today}",
        f"sources: {sources_yaml}",
        f"tags: {tags_yaml}",
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def touch_updated(wiki_dir: Path, relative_path: str) -> None:
    """Update the `updated:` date in a page's frontmatter to today."""
    today = datetime.now().strftime("%Y-%m-%d")
    target = wiki_dir / relative_path
    if not target.exists():
        return
    content = target.read_text(encoding="utf-8")
    content = re.sub(r"^updated: \d{4}-\d{2}-\d{2}", f"updated: {today}",
                     content, flags=re.MULTILINE)
    target.write_text(content, encoding="utf-8")


# ── utilities ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def now_for_log() -> str:
    return _now()


def slugify(name: str) -> str:
    """Convert a string to a kebab-case filename stem."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")
