# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

```bash
# Run the development server (auto-reloads on file changes)
uvicorn main:app --reload

# Interactive API docs (while server is running)
open http://localhost:8000/docs
```

## Architecture

Single-file FastAPI app (`main.py`). All routes are defined there. The app is intentionally minimal — a learning project to get familiar with Claude Code.
