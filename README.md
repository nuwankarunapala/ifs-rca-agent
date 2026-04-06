# IFS Kube Medic

An AI-powered Root Cause Analysis and Kubernetes health check tool that reads log files, detects error patterns, sends everything to Claude Opus 4.6 for analysis, and produces a formatted Word document report — all from a single command.

---

## Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)

---

## Installation

### 1. Clone the repository

**Bash (macOS / Linux):**
```bash
git clone https://github.com/your-org/ifs_kube_medic.git
cd ifs_kube_medic
```

**PowerShell (Windows):**
```powershell
git clone https://github.com/your-org/ifs_kube_medic.git
Set-Location ifs_kube_medic
```

### 2. Create a virtual environment

**Bash:**
```bash
python -m venv venv
```

**PowerShell:**
```powershell
python -m venv venv
```

### 3. Activate the virtual environment

**Bash (macOS / Linux):**
```bash
source venv/bin/activate
```

**PowerShell (Windows):**
```powershell
venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script, run this once first:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

**Windows Command Prompt:**
```cmd
venv\Scripts\activate.bat
```

### 4. Install dependencies

**Bash:**
```bash
pip install -r requirements.txt
```

**PowerShell:**
```powershell
pip install -r requirements.txt
```

### 5. Configure your API key

**Bash:**
```bash
cp .env.example .env
```

**PowerShell:**
```powershell
Copy-Item .env.example .env
```

Open `.env` and replace `your_api_key_here` with your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

### Step 1 — Add log files

Copy your Kubernetes log files (`.log`, `.txt`, or `.gz`) into the `logs/` directory.
The agent reads all files recursively, so sub-folders are fine.

**Bash:**
```bash
cp tests/sample_logs/*.log logs/
```

**PowerShell:**
```powershell
Copy-Item tests\sample_logs\*.log logs\
```

Sub-folder layout is also supported:

```
logs/
├── pods/
│   ├── logs/
│   └── descriptions/
├── deployments/
│   └── descriptions/
└── ingress/
    └── logs/
```

### Step 2 — Run the agent

There are two modes: **incident RCA** and **health check**.

---

## Commands

### Incident RCA mode

Investigate a specific outage. Requires `--incident-time`.

**Bash:**
```bash
python -m src.main --mode incident --incident-time "2026-03-20 14:00 UTC"
```

**PowerShell:**
```powershell
python -m src.main --mode incident --incident-time "2026-03-20 14:00 UTC"
```

With a custom logs directory:

**Bash:**
```bash
python -m src.main --mode incident --incident-time "2026-03-20 14:00 UTC" --logs-dir ./my-logs
```

**PowerShell:**
```powershell
python -m src.main --mode incident --incident-time "2026-03-20 14:00 UTC" --logs-dir .\my-logs
```

The agent analyses logs within a **48-hour window** around the incident time, then generates a Word document RCA report.

### Health check mode

Proactive cluster health assessment — no incident required. Analyses all events in the logs.

**Bash:**
```bash
python -m src.main --mode health-check
```

**PowerShell:**
```powershell
python -m src.main --mode health-check
```

With a custom logs directory:

**Bash:**
```bash
python -m src.main --mode health-check --logs-dir ./my-logs
```

**PowerShell:**
```powershell
python -m src.main --mode health-check --logs-dir .\my-logs
```

### All flags

| Flag | Description | Default |
|------|-------------|---------|
| `--mode` | `incident` or `health-check` | `incident` |
| `--incident-time` | Incident start time (required for incident mode) | — |
| `--logs-dir` | Path to directory containing log files | `./logs` |

### Help

**Bash:**
```bash
python -m src.main --help
```

**PowerShell:**
```powershell
python -m src.main --help
```

---

## What the agent does

### Incident RCA pipeline

1. Reads and parses all log files in `logs/`
2. Detects known Kubernetes/IFS error patterns within the 48-hour incident window
3. Extracts additional context (ticket numbers, kubectl commands)
4. Sends everything to Claude Opus 4.6 for RCA
5. Generates a Word document in `output/`
6. Saves the incident to the local knowledge base for future correlation

### Health check pipeline

1. Run `scripts/collect_health_data.sh` (or `.ps1`) to pull all 10 data areas from your cluster
2. The agent reads all files from the output directory (no time filtering — full picture)
3. Detects known error/event patterns across all log types
4. Sends structured data (top, get, describe, events, logs) to Claude Opus 4.6
5. Generates a 7-section Word health report in `output/` with:
   - Status per area (Healthy / Warning / Critical)
   - Every problem found, with evidence
   - Copy-paste-ready kubectl fix commands for each problem

---

## Interactive prompts

### Incident mode

When running in incident mode, the agent asks:

| # | Question | Example answer |
|---|----------|----------------|
| 1 | When did the incident start? | `2026-03-20 14:00 UTC` |
| 2 | Which services were affected? | `ifs-app, ifs-db` |
| 3 | Any recent deployments or changes? | `Deployed v2.4.1 at 13:30 UTC` |
| 4 | Environment | `production` / `staging` / `dev` |
| 5 | Additional notes (optional) | free text |

### Health check mode

Health check mode requires **no interactive prompts**. Run the data collection script first, then run the agent against the collected output.

#### Step 1 — Collect data from the cluster

**Bash (macOS / Linux):**
```bash
chmod +x scripts/collect_health_data.sh
./scripts/collect_health_data.sh ifs-production ./logs/health_check
```

**PowerShell (Windows):**
```powershell
.\scripts\collect_health_data.ps1 -Namespace ifs-production -LogsDir .\logs\health_check
```

The script collects all 10 health areas and saves them into `logs/health_check/` using the file naming convention the agent expects:

| # | Area | Files collected |
|---|------|----------------|
| 1 | Node utilisation | `kubectl-top-nodes.txt`, `kubectl-describe-node-*.txt` |
| 2 | Pod utilisation | `kubectl-top-pods-memory.txt`, `kubectl-top-pods-cpu.txt` |
| 3 | Requests & limits | `kubectl-get-resource-requests.txt`, `kubectl-get-resourcequota.txt`, `kubectl-get-limitrange.txt` |
| 4 | Events | `kubectl-events-all.txt`, `kubectl-events-warnings.txt` |
| 5 | Linkerd health | `linkerd_logs/kubectl-linkerd-*.log`, `kubectl-linkerd-check.txt` |
| 6 | Redis | `redis-<pod>.log` |
| 7 | PVC / Storage | `kubectl-get-pvc.txt`, `kubectl-get-pv.txt`, `kubectl-get-storageclass.txt` |
| 8 | HPA | `kubectl-get-hpa.txt`, `kubectl-describe-hpa-*.txt` |
| 9 | Scheduling constraints | `kubectl-get-scheduling-constraints.txt`, `kubectl-get-node-taints.txt` |
| 10 | Pod summary | `kubectl-get-pods-all.txt`, `kubectl-get-pods-nonrunning.txt`, `kubectl-get-pods-restarts.txt` |

#### Step 2 — Run the health check

**Bash:**
```bash
python -m src.main --mode health-check --logs-dir ./logs/health_check
```

**PowerShell:**
```powershell
python -m src.main --mode health-check --logs-dir .\logs\health_check
```

The agent reads every file, sends the data to Claude Opus 4.6, and generates a 7-section Word report showing:
- A **status table** (Healthy / Warning / Critical) for all 10 areas
- A **Problems Found** section listing every issue with evidence
- A **How to Fix** section with copy-paste-ready kubectl commands for each problem

---

## Output

Reports are saved to the `output/` folder with a timestamped filename:

```
output/RCA_20260320_140522.docx
output/HEALTH_20260406_093011.docx
```

---

## Knowledge base

Every completed RCA run is saved to `knowledge/incidents.json`. On the next run, the agent automatically finds similar past incidents (using Jaccard similarity on error type + pod name pairs) and injects them into Claude's prompt for pattern correlation.

The knowledge base grows over time and requires no manual maintenance.

---

## Project structure

```
ifs_kube_medic/
├── logs/                    ← Place your log files here
│   └── health_check/        ← Output of collect_health_data script
├── output/                  ← Generated RCA and health reports
├── knowledge/
│   └── incidents.json       ← Local incident knowledge base
├── scripts/
│   ├── collect_health_data.sh   ← Collects all 10 health areas (Bash)
│   └── collect_health_data.ps1  ← Collects all 10 health areas (PowerShell)
├── src/
│   ├── __init__.py
│   ├── main.py              ← Entry point / orchestrator
│   ├── log_reader.py        ← Recursively reads .log/.txt/.gz files
│   ├── log_parser.py        ← Detects error patterns, returns LogError list
│   ├── user_interaction.py  ← CLI prompts for incident context
│   ├── claude_analyst.py    ← Calls Claude Opus 4.6 via Anthropic SDK
│   ├── knowledge_base.py    ← Local incident memory and similarity search
│   └── rca_generator.py     ← Produces Word document reports
├── tests/
│   └── sample_logs/         ← Sample Kubernetes logs for testing
├── .env.example             ← Template for API key configuration
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Cross-platform notes

- All file paths use `pathlib.Path` — works identically on macOS, Linux, and Windows.
- Log files are read with `encoding="utf-8", errors="ignore"` to handle any byte sequences safely.
- The `output/` and `knowledge/` directories are created automatically if they do not exist.
- On Windows, use `.\` instead of `./` for relative paths in PowerShell.
