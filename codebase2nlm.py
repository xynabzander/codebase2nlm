#!/usr/bin/env python3
"""
codebase2nlm
------------
Crawl a codebase, respecting .gitignore and .crawlignore, and emit markdown
document(s) suitable for uploading to NotebookLM as sources.

Usage:
    codebase2nlm [PATH] [-o OUTPUT_DIR] [--max-words N] [--max-lines N] [--max-mb N]

If PATH is omitted, the current directory is used.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import pathspec
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'pathspec'. Reinstall with:  pipx install .")


# ----------------------------- configuration --------------------------------

DEFAULT_MAX_WORDS = 450_000          # safely below NotebookLM's ~500k limit
DEFAULT_MAX_LINES = 95_000           # safety target below observed ~100k NotebookLM line cap
DEFAULT_MAX_BYTES = 190 * 1024 * 1024  # safely below NotebookLM's 200MB upload limit
NOTEBOOK_SOURCE_LIMIT = 50
DEFAULT_OUTPUT_NAME = "notebooklm_output"

ALWAYS_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode",
    "__pycache__", ".venv", "venv", "env",
    "node_modules", ".next", ".nuxt", "dist", "build",
    ".DS_Store", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".cache", "target",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".tif", ".psd", ".ai", ".heic",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".pyc", ".pyo", ".class", ".o", ".a", ".so", ".dll", ".dylib", ".exe",
    ".wasm",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    ".db", ".sqlite", ".sqlite3", ".mdb",
    ".bin", ".dat", ".dmg", ".iso", ".pkl", ".npy", ".npz", ".parquet",
}

LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Cargo.lock", "Gemfile.lock",
    "composer.lock", "mix.lock",
}

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java",
    ".kt": "kotlin", ".swift": "swift", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".php": "php", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".fish": "fish", ".ps1": "powershell", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sass": "sass", ".less": "less",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".env": "dotenv",
    ".md": "markdown", ".markdown": "markdown", ".rst": "rst",
    ".tex": "latex", ".r": "r", ".lua": "lua",
    ".dart": "dart", ".scala": "scala", ".clj": "clojure",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".vim": "vim", ".tf": "hcl", ".hcl": "hcl",
    ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf", ".vue": "vue", ".svelte": "svelte",
}

SPECIAL_FILENAMES = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "rakefile": "ruby",
    "gemfile": "ruby",
}


# --------------------------- ignore-spec loading ----------------------------

def load_ignore_spec(root: Path,
                     extra_skip_dirs: List[str],
                     use_gitignore: bool = True) -> pathspec.PathSpec:
    """Merge .gitignore + .crawlignore + ALWAYS_IGNORE_DIRS into one spec."""
    patterns: List[str] = []
    files_to_read = [".crawlignore"]
    if use_gitignore:
        files_to_read.insert(0, ".gitignore")
    for name in files_to_read:
        f = root / name
        if f.is_file():
            patterns.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
    for d in ALWAYS_IGNORE_DIRS:
        patterns.append(f"{d}/")
    for d in extra_skip_dirs:
        patterns.append(f"{d}/")
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


# ----------------------------- file walking ---------------------------------

def walk_codebase(root: Path, spec: pathspec.PathSpec) -> List[Path]:
    """Return a sorted list of files under `root` not matched by `spec`."""
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)

        # Prune ignored directories in place.
        pruned = []
        for d in dirnames:
            if d in ALWAYS_IGNORE_DIRS:
                continue
            rel = (rel_dir / d).as_posix() + "/"
            if spec.match_file(rel):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for fn in filenames:
            rel = (rel_dir / fn).as_posix()
            if spec.match_file(rel):
                continue
            files.append((root / rel).resolve())

    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


# --------------------------- binary detection -------------------------------

def is_binary_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    if path.name in LOCKFILES:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        try:
            chunk.decode("latin-1")
            printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
            return printable / max(len(chunk), 1) < 0.85
        except Exception:
            return True
    return False


# ----------------------------- tree rendering -------------------------------

def build_tree(root: Path, files: List[Path], binary_set: set) -> str:
    """Build an ASCII tree; binary files are marked."""
    tree: dict = {}
    for f in files:
        node = tree
        for p in f.relative_to(root).parts:
            node = node.setdefault(p, {})

    lines = [(root.name or "/") + "/"]

    def render(node: dict, path_parts: tuple, prefix: str):
        keys = list(node.keys())
        dirs = sorted(k for k in keys if node[k])
        fils = sorted(k for k in keys if not node[k])
        items = [(d, True) for d in dirs] + [(f, False) for f in fils]
        for i, (name, is_dir) in enumerate(items):
            last = i == len(items) - 1
            connector = "└── " if last else "├── "
            label = name + ("/" if is_dir else "")
            if not is_dir:
                rel = "/".join(path_parts + (name,))
                if rel in binary_set:
                    label += "   (binary — contents omitted)"
            lines.append(prefix + connector + label)
            if is_dir:
                ext = "    " if last else "│   "
                render(node[name], path_parts + (name,), prefix + ext)

    render(tree, tuple(), "")
    return "\n".join(lines)


# ----------------------------- rendering files ------------------------------

def code_fence_for(content: str) -> str:
    """Choose a backtick fence long enough to wrap `content` safely."""
    runs = re.findall(r"`+", content)
    longest = max((len(r) for r in runs), default=0)
    return "`" * max(3, longest + 1)


def lang_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in LANG_BY_EXT:
        return LANG_BY_EXT[ext]
    return SPECIAL_FILENAMES.get(path.name.lower(), "")


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ------------------------------- budgeting ----------------------------------

def line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def byte_count(text: str) -> int:
    return len(text.encode("utf-8"))


def build_section(rel: str,
                  content: str,
                  lang: str,
                  chunk_index: Optional[int] = None,
                  chunk_total: Optional[int] = None) -> str:
    chunk_label = ""
    if chunk_index is not None and chunk_total is not None:
        chunk_label = f" (chunk {chunk_index} of {chunk_total})"
    fence = code_fence_for(content)
    return f"\n### `{rel}`{chunk_label}\n\n{fence}{lang}\n{content}\n{fence}\n"


def split_oversized_content(rel: str,
                            content: str,
                            lang: str,
                            base_header: str,
                            max_words: int,
                            max_lines: int,
                            max_bytes: int) -> List[str]:
    """Split an oversized file content into chunked sections that fit limits."""
    whole = build_section(rel, content, lang)
    if (
        len((base_header + whole).split()) <= max_words
        and line_count(base_header + whole) <= max_lines
        and byte_count(base_header + whole) <= max_bytes
    ):
        return [whole]

    lines = content.splitlines(keepends=True)
    if not lines:
        return [whole]

    chunks_raw: List[str] = []
    i = 0
    while i < len(lines):
        lo, hi = i + 1, len(lines)
        best = i
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = "".join(lines[i:mid])
            probe = build_section(rel, candidate, lang, chunk_index=1, chunk_total=9999)
            text = base_header + probe
            fits = (
                len(text.split()) <= max_words
                and line_count(text) <= max_lines
                and byte_count(text) <= max_bytes
            )
            if fits:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best == i:
            # Last-resort fallback: force progress even if one very long line breaches budget.
            best = i + 1
        chunks_raw.append("".join(lines[i:best]))
        i = best

    total = len(chunks_raw)
    return [build_section(rel, c, lang, idx, total) for idx, c in enumerate(chunks_raw, 1)]


# ------------------------------ main driver ---------------------------------

def write_output(root: Path,
                 output_dir: Path,
                 files: List[Path],
                 max_words: int,
                 max_lines: int,
                 max_bytes: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    project = root.name or "codebase"

    rel_paths = [f.relative_to(root).as_posix() for f in files]
    binary_set = set()
    text_files: List[Tuple[Path, str, str]] = []  # (path, rel, content)
    for f, rel in zip(files, rel_paths):
        if is_binary_file(f):
            binary_set.add(rel)
            continue
        content = read_text(f)
        if content is None:
            binary_set.add(rel)
            continue
        text_files.append((f, rel, content))

    tree_str = build_tree(root, files, binary_set)
    chunked_files: set[str] = set()

    def make_header(part: Optional[Tuple[int, int]] = None,
                    files_in_part: Optional[List[str]] = None) -> str:
        if part:
            i, n = part
            title = f"# Codebase: {project} — Part {i} of {n}\n"
            note = (f"\n> This is part **{i} of {n}**. The full file tree and "
                    f"binary-skipped list are repeated in every part so each "
                    f"source stands alone in NotebookLM.\n")
        else:
            title = f"# Codebase: {project}\n"
            note = ""
        h = [title, note, "\n## File Tree\n", "```\n" + tree_str + "\n```\n"]
        if binary_set:
            h.append("\n## Binary / skipped files (listed in tree, contents omitted)\n\n")
            h.append("\n".join(f"- `{p}`" for p in sorted(binary_set)) + "\n")
        if chunked_files:
            h.append("\n## Chunked files (split across multiple sections)\n\n")
            h.append("\n".join(f"- `{p}`" for p in sorted(chunked_files)) + "\n")
        if files_in_part is not None:
            h.append("\n## Files included in this part\n\n")
            h.append("\n".join(f"- `{p}`" for p in files_in_part) + "\n")
        h.append("\n## Contents\n")
        return "".join(h)

    base_header = make_header(part=(1, 1), files_in_part=[])

    sections: List[Tuple[str, str, int, int, int]] = []  # (rel, block, words, lines, bytes)
    for f, rel, content in text_files:
        lang = lang_for(f)
        file_sections = split_oversized_content(rel, content, lang, base_header,
                                                max_words, max_lines, max_bytes)
        if len(file_sections) > 1:
            chunked_files.add(rel)
        for block in file_sections:
            sections.append((rel, block, len(block.split()), line_count(block), byte_count(block)))

    single_header = make_header()
    single_body = "".join(b for _, b, _, _, _ in sections)
    single_text = single_header + single_body
    total_words = len(single_text.split())
    total_lines = line_count(single_text)
    total_bytes = byte_count(single_text)

    def within_limits(text: str) -> bool:
        return (
            len(text.split()) <= max_words
            and line_count(text) <= max_lines
            and byte_count(text) <= max_bytes
        )

    if within_limits(single_text) or not sections:
        out = output_dir / "codebase.md"
        out.write_text(single_text, encoding="utf-8")
        print(f"  wrote {out}  ({total_words:,} words, {total_lines:,} lines, "
              f"{total_bytes:,} bytes, "
              f"{len(sections)} text files, {len(binary_set)} binary skipped)")
        return

    # Split by all per-source limits (words, lines, and bytes).
    parts: List[List[Tuple[str, str, int, int, int]]] = []
    current: List[Tuple[str, str, int, int, int]] = []
    for sec in sections:
        candidate = current + [sec]
        probe_header = make_header(part=(1, 1),
                                   files_in_part=[rel for rel, _, _, _, _ in candidate])
        probe_text = probe_header + "".join(b for _, b, _, _, _ in candidate)
        if current and not within_limits(probe_text):
            parts.append(current)
            current = [sec]
        else:
            current = candidate
    if current:
        parts.append(current)

    n = len(parts)
    pad = max(2, len(str(n)))
    for i, part_sections in enumerate(parts, 1):
        files_in_part = [rel for rel, _, _, _, _ in part_sections]
        header = make_header(part=(i, n), files_in_part=files_in_part)
        body = "".join(b for _, b, _, _, _ in part_sections)
        out = output_dir / f"codebase_part{i:0{pad}d}.md"
        text = header + body
        out.write_text(text, encoding="utf-8")
        print(f"  wrote {out}  ({len(text.split()):,} words, {line_count(text):,} lines, "
              f"{byte_count(text):,} bytes, "
              f"{len(part_sections)} files)")
        if not within_limits(text):
            print(f"  warning: {out.name} still exceeds configured limits (likely due to header overhead)")
    print(f"\nSplit into {n} parts. Upload each .md as a separate NotebookLM source.")
    if n > NOTEBOOK_SOURCE_LIMIT:
        print(f"warning: produced {n} parts; NotebookLM notebooks accept up to "
              f"{NOTEBOOK_SOURCE_LIMIT} sources per notebook")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codebase2nlm",
        description="Crawl a codebase and emit markdown document(s) for NotebookLM.",
    )
    p.add_argument(
        "path", nargs="?", default=".",
        help="Path to the codebase root (default: current directory).",
    )
    p.add_argument(
        "-o", "--output", default=None,
        help=f"Output directory (default: <PATH>/{DEFAULT_OUTPUT_NAME}).",
    )
    p.add_argument(
        "--max-words", type=int, default=DEFAULT_MAX_WORDS,
        help=f"Max words per output file before splitting "
             f"(default: {DEFAULT_MAX_WORDS:,}).",
    )
    p.add_argument(
        "--max-lines", type=int, default=DEFAULT_MAX_LINES,
        help=f"Max lines per output file before splitting "
             f"(default: {DEFAULT_MAX_LINES:,}).",
    )
    p.add_argument(
        "--max-mb", type=float, default=DEFAULT_MAX_BYTES / (1024 * 1024),
        help="Max MB per output file before splitting (default: 190).",
    )
    p.add_argument(
        "--no-gitignore", action="store_true",
        help="Ignore .gitignore rules (still honors .crawlignore and built-in skips).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = build_arg_parser().parse_args(argv)

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"Error: {root} is not a directory.")

    if args.output:
        output_dir = Path(args.output).expanduser().resolve()
    else:
        output_dir = root / DEFAULT_OUTPUT_NAME

    # If the output dir sits inside root, make sure we don't crawl it.
    extra_skip_dirs: List[str] = []
    try:
        rel = output_dir.relative_to(root)
        # Only skip the top-level directory name; pathspec handles the rest.
        first = rel.parts[0] if rel.parts else None
        if first:
            extra_skip_dirs.append(first)
    except ValueError:
        pass  # output_dir is outside root – nothing to skip

    print(f"Crawling:  {root}")
    print(f"Output:    {output_dir}")

    spec = load_ignore_spec(root, extra_skip_dirs,
                            use_gitignore=not args.no_gitignore)
    files = walk_codebase(root, spec)
    print(f"  found {len(files)} candidate files after applying ignores")

    if not files:
        print("Nothing to write.")
        return

    max_bytes = int(args.max_mb * 1024 * 1024)
    write_output(root, output_dir, files, args.max_words, args.max_lines, max_bytes)
    print("\nDone. Upload the .md file(s) as sources in NotebookLM.")


if __name__ == "__main__":
    main()
