# Snowgrid

AI-powered task coordination system with automated agents.

## Overview

Snowgrid is a distributed task management system that combines a Kanban-style board for human oversight with autonomous AI agents that execute tasks. The system uses opencode to run AI coding tasks and includes an automated judge to determine task completion.

## Components

### Board (Task Coordinator)

A Flask-based web interface for managing tasks via a horizontal Kanban board.

**Features:**
- Task creation and management
- Status tracking: new → wip → review → complete/failed
- Real-time search and filtering
- Auto-refresh every 8 seconds
- Work log tracking with timestamps
- Automatic database migration support

**Tech Stack:**
- Flask
- SQLite (database.db)

**Run the board:**
```bash
cd board/code
pip install -r requirements.txt
python app.py
```

The board runs on `http://127.0.0.1:5000` by default.

### Shard (AI Agent Worker)

An autonomous agent that monitors for new tasks and executes them using opencode.

**Features:**
- Health checks for Task API and opencode
- Configurable model selection
- Automatic retry logic (up to 3 attempts)
- AI-powered task completion judgment
- Plain-text judge output (no JSON parsing required)
- Real-time status and log monitoring

**Tech Stack:**
- Flask
- requests
- opencode for AI execution

**Run the shard:**
```bash
cd agents/shard
pip install -r requirements.txt
python agent.py
```

The shard runs on `http://127.0.0.1:5001` by default.

## Configuration

Configure the shard with:
- **TASK_API_URL**: URL of the board (default: http://127.0.0.1:5000)
- **OPEN_CODE_MODEL**: Model identifier for opencode (default: llama.cpp/ggml-org/GLM-4.7-Flash-GGUF:Q8_0)

Configuration is stored in `agents/shard/config.json`.

## Architecture

```
┌─────────────┐     ┌──────────────┐
│   Human     │────▶│    Board     │
│  Creates    │     │ (Task API)   │
│   Tasks     │◀────│ Port: 5000   │
└─────────────┘     └──────────────┘
                         │
                         ▼ HTTP
                    ┌──────────────┐
                    │    Shard     │
                    │  (Agent)     │
                    │ Port: 5001   │
                    └──────────────┘
                         │
                         ▼ subprocess
                    ┌──────────────┐
                    │   opencode   │
                    │   AI Model   │
                    └──────────────┘
```

## Screenshots

![Board Interface](/screenshots/snowgrid_board.png "Snowgrid Board")
![Task View](/screenshots/snowgrid_task.png "Snowgrid Task")

## Workflow

1. Human creates a task on the Board (e.g., "git clone repo && review code")
2. Shard detects new task and marks it as "wip"
3. Shard runs opencode with the task description
4. AI agent executes the task in an isolated temp directory
5. Judge evaluates if the task is complete
6. If complete → "review" status, otherwise retry (up to 3 attempts)
7. Final status: "complete" or "failed"

## License

This is free and unencumbered software released into the public domain. See LICENSE for details.
