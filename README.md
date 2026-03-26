# ChatGPT-V8-Heap-Forensics

Automated forensic extraction tool for recovering conversation artifacts from ChatGPT V8 heap snapshots, including deleted conversations.

This repository contains the tool and dataset associated with the paper:

> **Forensic Analysis from Generative AI Web Application Memory: A ChatGPT Case Study**
>
> Jihun Joun, School of Interdisciplinary Forensics, Arizona State University

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

The benchmark dataset (heap snapshot files and ground-truth logs) is available for download:

**[Download Dataset](https://drive.google.com/drive/folders/1kcEYWK02I8dhIcrtkD0qhEm7DMOqzhiT?usp=sharing)**

### Heap Snapshot Files

| File | Total Messages | Active | Deleted | Description |
| --- | --- | --- | --- | --- |
| `350msg_active.heapsnapshot` | 350 pairs | 350 | 0 | Intermediate snapshot during dataset construction |
| `500msg_active.heapsnapshot` | 500 pairs | 500 | 0 | Intermediate snapshot during dataset construction |
| `800msg_active.heapsnapshot` | 800 pairs | 800 | 0 | Full dataset, pre-deletion snapshot |
| `800msg_400active_400deleted.heapsnapshot` | 800 pairs | 400 | 400 | Post-deletion snapshot (8 conversations deleted via UI) |
| `800msg_400active_400deleted_latest.heapsnapshot` | 800 pairs | 400 | 400 | Post-deletion snapshot collected from the latest ChatGPT client |
| `metadata_scenarios.heapsnapshot` | — | — | — | Supplementary snapshot containing image generation, file upload, and web search conversations for metadata artifact validation |

### Ground-Truth Files

| File | Corresponds To | Description |
| --- | --- | --- |
| `ground_truth_800msg.txt` | `800msg_400active_400deleted.heapsnapshot` | Independent log of all user prompts and AI responses recorded at generation time |
| `ground_truth_800msg_latest.txt` | `800msg_400active_400deleted_latest.heapsnapshot` | Ground-truth log for the latest ChatGPT client dataset |
| `ground_truth_metadata_scenarios.txt` | `metadata_scenarios.heapsnapshot` | Ground-truth log for metadata artifact scenarios including image generation parameters, uploaded file details, and web search results |

### Dataset Description

The primary dataset (text-based conversations) validates extraction accuracy for message content, author roles, timestamps, and thread reconstruction. The supplementary metadata scenarios dataset validates the extraction of additional artifact types:

| Scenario | Example Prompt | Target Artifacts |
| --- | --- | --- |
| Image Generation | "Could you draw a dog?" | Image dimensions, DALL-E generation ID, image title, content moderation status |
| File/Image Upload | "What kind of fruit is this?" (with photo) | Attachment metadata (filename, size, dimensions, source), asset pointers |
| Web Search | "Who is Jihun Joun?" | Search result groups (domain, title, URL, snippet), content references, safe URLs |

All snapshots were collected in a controlled virtual machine environment via the Chrome DevTools Protocol (`HeapProfiler` domain). Each snapshot was captured from a clean VM state to prevent cross-contamination between sessions. The ground-truth files contain independently logged records of all user inputs and AI responses at the time of generation, serving as the reference baseline for verifying extraction accuracy.

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

Run `heap_forensics_app.py` without arguments to launch the GUI. Pass a `.heapsnapshot` path as the first argument to run in CLI mode instead.

**GUI Workflow:**
1. Click **Browse...** to select a `.heapsnapshot` file
2. Optionally change the output folder
3. Click **Analyze** to run the extraction
4. Use the result buttons to open conversation threads, structure report, or the output folder

## Output

The tool produces four output files:

1. **Structure Report** (`structure_report.html`) — Full object hierarchy of each extracted entry with conversation content, organized by conversation thread
2. **Conversation Threads Report** (`conversation_threads.html`) — Reconstructed conversations in messenger-style format organized by thread UUID
3. **Conversation Threads JSON** (`conversation_threads.json`) — Machine-readable export of all extracted conversations with metadata
4. **Forensic Run Summary** (`forensic_run_summary.txt`) — SHA-256/MD5 hashes of the input snapshot, system information, analysis timestamp, tool version, and extraction statistics

## How It Works

1. **Graph Reconstruction** — The `.heapsnapshot` file is parsed by dynamically interpreting the metadata schema (`snapshot.meta.node_fields`, `snapshot.meta.edge_fields`). The flattened node and edge arrays are decoded into an indexed object graph with precomputed edge offsets for efficient traversal.

2. **Adaptive Signature Scan** — All nodes in the graph are scanned to identify conversation data candidates. For each node of type `object`, the tool checks whether it possesses the four required properties (`id`, `parentId`, `children`, `message`) by inspecting its outgoing edges. It then verifies the `message → content → parts → elements` path exists to confirm the object contains extractable conversation data. This approach does not depend on any fixed container path (e.g., WeakMap → table), making it resilient to application refactors.

3. **Data Extraction** — For each matched candidate object, the tool traverses multiple paths to extract conversation artifacts:
   - **Text content**: `message → content → parts → (n) → text → (n)` with fallback to `parts → (n) → elements` and direct string values
   - **AI reasoning**: When `message → content → content_type` is `code`, extracts `message → content → text` as AI internal reasoning output
   - **Author info**: `message → author → role`, `name`, and `author → metadata → real_author`, `source`
   - **Timestamp**: `message → create_time → value`
   - **File uploads**: `message → metadata → attachments → (n) → {id, name, height, width, size, source}`
   - **Image generation**: `message → content → parts → (n) → {height, width, size_bytes}` and `parts → (n) → metadata → dalle → gen_id`, `generation → gen_id`, `sanitized`; plus `message → metadata → image_gen_title`
   - **Web search results**: `message → metadata → search_result_groups → (n) → {domain, entries → (n) → {title, url, snippet, attribution}}`
   - **Content references**: `message → metadata → content_references → (n) → {title, url, snippet, attribution}` and `content_references → (n) → items → (n) → {title, url, snippet, attribution}`
   - **Other metadata**: `message → metadata → model_slug`, `safe_urls`

4. **Thread Reconstruction** — Extracted messages are grouped into conversation threads by tracing `parentId` and `children` fields. Messages sharing a common root (`client-created-root`) are clustered into the same thread. Within each thread, messages are ordered chronologically by `create_time`, and branching from regenerated responses is preserved.

## Extracted Artifacts

| Artifact | Description |
| --- | --- |
| Message Content | User prompts and AI responses (up to ~1,024 characters per message) |
| Message Metadata | Author role, creation timestamp, message ID, model name |
| AI Reasoning | Internal reasoning text when content_type is `code` |
| Content References | External source URLs, attributions, thumbnails |
| File Upload Metadata | Filename, MIME type, file size, dimensions, upload source |
| Image Generation Metadata | Image dimensions, DALL-E generation ID, generation request ID, content moderation status, image title |
| Search Queries | Web search queries issued by the AI model |
| Search Result Groups | Grouped search results with domain, title, URL, snippet, and attribution |
| Safe URLs | External URLs verified as safe by the AI model |
| Tool Invocations | Tool message details including author name, source, and real_author metadata |

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
