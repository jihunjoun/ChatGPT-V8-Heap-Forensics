# Heap Snapshot Forensics — GUI

A graphical interface for analyzing V8 heap snapshots. Provides the same extraction capabilities as the command-line tool with a point-and-click workflow.

## Requirements

```bash
pip install -r requirements-gui.txt
```

The GUI uses [customtkinter](https://github.com/TomSchimansky/CustomTkinter) for a modern dark-themed interface. If `customtkinter` is not installed, the tool falls back to the built-in `tkinter` interface automatically.

## Launch

```bash
# Launch GUI directly
python heap_forensics_app.py

# Or explicitly
python heap_forensics_app.py --gui
```

To run in CLI mode instead, pass a `.heapsnapshot` file path:

```bash
python heap_forensics_app.py path/to/snapshot.heapsnapshot [output_directory]
```

## Workflow

1. Click **Browse...** to select a `.heapsnapshot` file
2. Optionally change the **Output folder** (defaults to a `result_<filename>` folder in the tool directory)
3. Click **Analyze** to run the extraction
4. Once complete, use the result buttons:
   - **Conversation threads** — Opens the reconstructed conversation HTML report
   - **Structure report** — Generates and opens the full object hierarchy report (on-demand)
   - **Open folder** — Opens the output directory containing all generated files

## Output Files

| File | Description |
| --- | --- |
| `conversation_threads.html` | Messenger-style conversation display |
| `conversation_threads.json` | Machine-readable conversation export |
| `structure_report.html` | Full object hierarchy (generated on-demand via button) |
| `forensic_run_summary.txt` | Hashes, timestamps, and extraction statistics |
