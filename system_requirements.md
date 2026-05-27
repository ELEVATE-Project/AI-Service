# System Requirements

## spaCy Language Model

After running `uv sync`, the `presidio-analyzer` (PII detection) guardrail requires a spaCy English model. This is a data download, not a Python package, so `uv sync` does not fetch it automatically:

**macOS / Linux / Windows**
```bash
uv run python -m spacy download en_core_web_lg
```

Run this once after first `uv sync`. The model is stored inside the virtual environment.
