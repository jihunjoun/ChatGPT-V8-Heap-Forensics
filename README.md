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
- The adaptive extraction approach is resilient to application updates (e.g., ChatGPT's transition to private class fields in v5.x)

## Repository Structure

```
tool/
├── heap_forensics.py          # Core extraction engine (CLI)
├── heap_forensics_app.py      # Unified entry point (CLI + GUI)
├── heap_forensics_gui.py      # GUI version (tkinter / customtkinter)
└── requirements-gui.txt       # Dependencies for GUI version
```

## Dataset

The benchmark dataset (heap snapshot files) is available for download:

**[Download Dataset](https://drive.google.com/drive/folders/1kcEYWK02I8dhIcrtkD0qhEm7DMOqzhiT?usp=sharing)**

The dataset contains `.heapsnapshot` files collected at different conversation stages (350, 500, and 800 messages) for validating the forensic extraction methodology.

## Requirements

- Python 3.x
- No external dependencies required for the core tool (`heap_forensics.py`)
- See `requirements-gui.txt` for optional GUI dependencies

## Usage

### Command Line

```bash
python heap_forensics.py <path_to_heapsnapshot_file> [output_directory]
```

### GUI

```bash
pip install -r requirements-gui.txt
python heap_forensics_app.py
```

Or run `heap_forensics_app.py` without arguments to launch the GUI directly. Pass a `.heapsnapshot` path as the first argument to run in CLI mode.

For detailed GUI instructions, see [README-GUI.md](README-GUI.md).

## Output

The tool produces four output files:

1. **Structure Report** (`structure_report.html`) — Full object hierarchy of each extracted entry with conversation content, organized by conversation thread
2. **Conversation Threads Report** (`conversation_threads.html`) — Reconstructed conversations in messenger-style format organized by thread UUID
3. **Conversation Threads JSON** (`conversation_threads.json`) — Machine-readable export of all extracted conversations with metadata
4. **Forensic Run Summary** (`forensic_run_summary.txt`) — SHA-256/MD5 hashes of the input snapshot, system information, analysis timestamp, tool version, and extraction statistics

## How It Works

1. **Graph Reconstruction**: Parses the `.heapsnapshot` file and reconstructs the directed object graph by interpreting the metadata schema and decoding node/edge arrays
2. **Adaptive Signature Scan**: Scans all object nodes in the heap for the structural signature of conversation data — specifically, objects possessing the four required properties: `id`, `parentId`, `children`, and `message`. This approach does not rely on any fixed container path (e.g., WeakMap), making it resilient to application refactors
3. **Data Extraction**: For each matched object, extracts message content (via `message → content → parts`), author role, creation timestamp, and metadata including search queries, content references, and image generation parameters
4. **Thread Reconstruction**: Traces `parentId` and `children` fields to reconstruct the original conversation flow and branching structure, including regenerated responses

## Extracted Artifacts

| Artifact | Description |
| --- | --- |
| Message Content | User prompts and AI responses (up to ~1,024 characters per message) |
| Message Metadata | Author role, creation timestamp, message ID, model name |
| Content References | External source URLs, attributions, thumbnails |
| File Upload Metadata | Filename, MIME type, file size, token count |
| Image Generation Metadata | Generation prompt, image dimensions, DALL-E generation ID |
| Search Queries | Web search queries issued by the AI model |
| Search Result Groups | Grouped search results with domain, title, URL, and snippet |
| Tool Invocations | Tool message details including author source and real_author metadata |

## Citation

If you use this tool or dataset in your research, please cite:

```bibtex
@article{chatgpt_v8_heap_forensics,
  title={Forensic Analysis from Generative AI Web Application Memory: A ChatGPT Case Study},
  author={Joun, Jihun},
  journal={Preprint submitted to Elsevier},
  year={2026}
}
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
