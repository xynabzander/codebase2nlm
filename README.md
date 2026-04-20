# codebase2nlm

Crawl a codebase and emit markdown document(s) for uploading to NotebookLM as sources.

## Install (macOS)

One-time setup if you don't have pipx yet:

```bash
brew install pipx
pipx ensurepath
# open a new terminal after this
```

### Option A — install from GitHub (recommended)

Public repo:

```bash
pipx install git+https://github.com/bsreeram08/codebase2nlm.git
```

Specific branch, tag, or commit:

```bash
pipx install git+https://github.com/bsreeram08/codebase2nlm.git@main
pipx install git+https://github.com/bsreeram08/codebase2nlm.git@v0.1.0
pipx install git+https://github.com/bsreeram08/codebase2nlm.git@<commit-sha>
```

Private repo (uses your SSH key):

```bash
pipx install git+ssh://git@github.com/bsreeram08/codebase2nlm.git
```

Update to the latest version on the default branch:

```bash
pipx upgrade codebase2nlm
# or force a clean reinstall
pipx install --force git+https://github.com/bsreeram08/codebase2nlm.git
```

### Option B — install from a local clone

```bash
git clone https://github.com/bsreeram08/codebase2nlm.git
cd codebase2nlm
pipx install .
```

### Uninstall

```bash
pipx uninstall codebase2nlm
```

## Usage

```bash
# crawl the current directory
codebase2nlm

# crawl a specific project
codebase2nlm ~/projects/myapp

# custom output location
codebase2nlm ~/projects/myapp -o ~/Desktop/myapp-notebooklm

# tweak the per-file word limit
codebase2nlm ~/projects/myapp --max-words 400000

# tweak line and byte limits too (NotebookLM-friendly defaults are used automatically)
codebase2nlm ~/projects/myapp --max-lines 100000 --max-bytes 199000000

# ignore .gitignore (still respects .crawlignore and built-in skips)
codebase2nlm ~/projects/myapp --no-gitignore
```

Output lands in `<PATH>/notebooklm_output/` by default. Upload the resulting
`codebase.md` (or each `codebase_partN.md` if the codebase was too large for a
single source) to NotebookLM.

## What it does

- Honors `.gitignore` and `.crawlignore` at the repo root (gitignore syntax).
- Skips common noise: `.git`, `node_modules`, `__pycache__`, `.venv`, lockfiles, etc.
- Lists binary files in the tree tagged `(binary — contents omitted)`, skips their contents.
- Produces a full ASCII file tree at the top, followed by every text file's contents in labeled code fences.
- Auto-splits into `codebase_part1.md`, `codebase_part2.md`, ... when any NotebookLM per-source limit would be exceeded:
  - word count (default target: 450k to stay below 500k),
  - line count (default: 100k lines),
  - upload size (default target: 190MB to stay below 200MB).
- Warns when output creates more than 50 parts (NotebookLM notebook source count limit).
