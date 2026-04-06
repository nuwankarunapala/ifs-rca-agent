# IFS Kube Medic

An AI-powered Root Cause Analysis and Kubernetes health check tool that reads log files, detects error patterns, asks you a few questions about the incident, sends everything to Claude Opus 4.6 for analysis, and produces a formatted Word document report — all from a single command.

---

## Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-org/ifs_kube_medic.git
cd ifs_kube_medic
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

### 3. Activate the virtual environment

**macOS / Linux:**
```bash
source venv/bin/activate
```

**Windows (Command Prompt):**
```cmd
venv\Scripts\activate.bat
```

**Windows (PowerShell):**
```powershell
venv\Scripts\Activate.ps1
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure your API key

```bash
cp .env.example .env
```

Open `.env` and replace `your_api_key_here` with your real Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

### 1. Add log files

Copy your Kubernetes log files (`.log`, `.txt`, or `.gz`) into the `logs/` directory.
The agent reads all files recursively, so sub-folders are fine.

Sample logs for testing are provided in `tests/sample_logs/` — copy them into `logs/`:

```bash
cp tests/sample_logs/*.log logs/
```

### 2. Run the agent

```bash
python -m src.main
```

The agent will:
1. Read and parse all log files in `logs/`
2. Detect known Kubernetes/IFS error patterns
3. Ask you 4–5 questions about the incident
4. Send everything to Claude Opus 4.6 for RCA
5. Generate a Word document in `output/`

### 3. Find the report

Reports are saved to the `output/` folder with a timestamped filename:

```
output/RCA_20260320_140522.docx
```

---

## Project Structure

```
ifs_kube_medic/
├── logs/                    ← Place your log files here
├── output/                  ← Generated RCA reports appear here
├── src/
│   ├── __init__.py
│   ├── main.py              ← Entry point / orchestrator
│   ├── log_reader.py        ← Recursively reads .log/.txt/.gz files
│   ├── log_parser.py        ← Detects error patterns, returns LogError list
│   ├── user_interaction.py  ← CLI prompts for incident context
│   ├── claude_analyst.py    ← Calls Claude Opus 4.6 via Anthropic SDK
│   └── rca_generator.py     ← Produces the Word document report
├── tests/
│   └── sample_logs/         ← Sample Kubernetes logs for testing
├── .env.example             ← Template for API key configuration
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Cross-Platform Notes

- All file paths use `pathlib.Path`, so the project works identically on macOS, Linux, and Windows.
- Log files are read with `encoding="utf-8", errors="ignore"` to handle any byte sequences safely.
- The `output/` directory is created automatically if it does not exist.
- On Windows, activate the virtual environment with `venv\Scripts\activate.bat` (CMD) or `venv\Scripts\Activate.ps1` (PowerShell). If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` first.
