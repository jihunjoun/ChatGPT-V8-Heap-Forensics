# ChatGPT-V8-Heap-Forensics

Automated forensic extraction tool for recovering conversation artifacts from ChatGPT V8 heap snapshots, including deleted conversations.

This repository contains the tool and dataset associated with the paper:

> **Forensic Analysis from Generative AI Web Application Memory: A ChatGPT Case Study**

## Overview

This project proposes a methodology for systematically extracting conversation data from generative AI web applications by analyzing the heap memory of the JavaScript V8 engine. The tool takes a `.heapsnapshot` file as input and extracts both active and deleted conversation artifacts by traversing the memory object graph.

### Key Findings

- Conversation artifacts, even after being deleted through the UI, remain referenced and recoverable from browser memory
- The tool achieves a **100% message identification rate** across all tested scenarios
- Data persists as long as the browser tab remains active, regardless of elapsed time, memory pressure, or incognito mode

## Repository Structure

```
├── heap_forensics.py          # Core extraction tool (CLI)
├── heap_forensics_app.py      # Application entry point
├── heap_forensics_gui.py      # GUI version
├── requirements-gui.txt       # Dependencies for GUI version
├── README-GUI.md              # GUI usage instructions
└── README.md
```

## Dataset

The benchmark dataset (heap snapshot files) is available for download:

**[Download Dataset](https://drive.google.com/drive/folders/1kcEYWK02I8dhIcrtkD0qhEm7DMOqzhiT?usp=sharing)**

The dataset contains `.heapsnapshot` files collected at different conversation stages (350, 500, and 800 messages) for validating the forensic extraction methodology.

## Requirements

- Python 3.x
- No external dependencies required for the core tool (`heap_forensics.py`)
- See `requirements-gui.txt` for GUI dependencies

## Usage

### Command Line

```bash
python heap_forensics.py <path_to_heapsnapshot_file>
```

### GUI

```bash
pip install -r requirements-gui.txt
python heap_forensics_app.py
```

For detailed GUI instructions, see [README-GUI.md](README-GUI.md).

## Output

The tool produces three output files:

1. **Structure Report** — Full object hierarchy of each extracted entry with conversation content
2. **Conversation Threads Report** — Reconstructed conversations in messenger-style format organized by UUID
3. **Integrity Log** — SHA-256 hash of the input snapshot, analysis timestamp, tool version, and summary statistics

## How It Works

1. Parses the `.heapsnapshot` file and reconstructs the directed object graph
2. Identifies all `WeakMap` nodes and locates conversation data objects through structural signature matching
3. Extracts message content, author role, timestamps, and metadata
4. Reconstructs conversation thread structure by tracing parent-child relationships

## Extracted Artifacts

| Artifact | Description |
|----------|-------------|
| Message Content | User prompts and AI responses (up to ~1,024 characters per message) |
| Message Metadata | Author role, creation timestamp, message ID, model name |
| Content References | External source URLs, attributions, thumbnails |
| File Upload Metadata | Filename, MIME type, file size, token count |
| Image Generation Metadata | Generation prompt, image dimensions, DALL-E generation ID |

## Citation

If you use this tool or dataset in your research, please cite:

```bibtex
@article{chatgpt_v8_heap_forensics,
  title={Forensic Analysis from Generative AI Web Application Memory: A ChatGPT Case Study},
  journal={Preprint submitted to Elsevier},
  year={2026}
}
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
