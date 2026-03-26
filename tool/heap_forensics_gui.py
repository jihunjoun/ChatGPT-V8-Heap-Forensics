# -*- coding: utf-8 -*-
"""
Heap Snapshot Forensics — GUI application for research use.
Select a .heapsnapshot file, run analysis, and open HTML reports (default output: this tool folder).
"""

import os
import sys
import threading
import webbrowser
from tkinter import Tk, filedialog, messagebox, ttk

# Optional: use customtkinter for modern UI if available
try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    HAS_CTK = False

# Import analysis engine
from heap_forensics import TOOL_VERSION, generate_structure_report, run_analysis

_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
APP_TITLE = "Heap Snapshot Forensics"
APP_VERSION = TOOL_VERSION
OUTPUT_STRUCTURE = "structure_report.html"
OUTPUT_THREADS = "conversation_threads.html"


def open_file(path: str) -> None:
    if not path or not os.path.isfile(path):
        return
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        webbrowser.open(f"file://{path}")


def open_folder(folder: str) -> None:
    if not folder or not os.path.isdir(folder):
        return
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        os.system(f'open "{folder}"')
    else:
        os.system(f'xdg-open "{folder}"')


def run_gui_plain_tk():
    root = Tk()
    root.title(f"{APP_TITLE}  v{APP_VERSION}")
    root.minsize(520, 420)
    root.geometry("600x500")

    # Style
    style = ttk.Style()
    style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"), padding=(0, 8))
    style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"), padding=(0, 4))
    style.configure("TButton", padding=(12, 6))
    style.configure("TFrame", padding=8)

    main = ttk.Frame(root, padding=16)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
    ttk.Label(main, text="Analyze V8 heap snapshots: object structure and conversation threads.").pack(anchor="w")

    # Input file
    f_input = ttk.Frame(main)
    f_input.pack(fill="x", pady=(16, 0))
    ttk.Label(f_input, text="Heap snapshot file", style="Header.TLabel").pack(anchor="w")
    row1 = ttk.Frame(f_input)
    row1.pack(fill="x")
    var_input = __import__("tkinter").StringVar()
    entry_input = ttk.Entry(row1, textvariable=var_input, state="readonly")
    entry_input.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_input():
        path = filedialog.askopenfilename(
            title="Select heap snapshot",
            filetypes=[("Heap snapshot", "*.heapsnapshot"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if path:
            var_input.set(path)
            name = os.path.splitext(os.path.basename(path))[0]
            var_output.set(os.path.join(_TOOL_DIR, f"result_{name}"))

    ttk.Button(row1, text="Browse...", command=browse_input).pack(side="right")

    # Output folder
    f_output = ttk.Frame(main)
    f_output.pack(fill="x", pady=(12, 0))
    ttk.Label(f_output, text="Output folder", style="Header.TLabel").pack(anchor="w")
    row2 = ttk.Frame(f_output)
    row2.pack(fill="x")
    var_output = __import__("tkinter").StringVar()
    entry_output = ttk.Entry(row2, textvariable=var_output)
    entry_output.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_output():
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            var_output.set(path)

    ttk.Button(row2, text="Browse...", command=browse_output).pack(side="right")

    # Analyze + progress
    progress_frame = ttk.Frame(main)
    progress_frame.pack(fill="x", pady=(16, 0))
    var_status = __import__("tkinter").StringVar(value="Ready.")
    progress = ttk.Progressbar(progress_frame, mode="indeterminate")
    btn_analyze = ttk.Button(progress_frame, text="Analyze", command=lambda: None)

    def do_analyze():
        inp = var_input.get().strip()
        out = var_output.get().strip()
        if not inp:
            messagebox.showwarning("No file", "Please select a heap snapshot file.")
            return
        if not os.path.isfile(inp):
            messagebox.showerror("File not found", f"File does not exist:\n{inp}")
            return
        if not out:
            out = os.path.join(_TOOL_DIR, "result_output")
        os.makedirs(out, exist_ok=True)
        btn_analyze.state(["disabled"])
        var_status.set("Loading snapshot...")
        progress.start(8)

        def work():
            res = run_analysis(inp, out, generate_structure_report=False)
            root.after(0, lambda: on_done(res))

        def on_done(res):
            progress.stop()
            btn_analyze.state(["!disabled"])
            if res.get("error"):
                var_status.set("Error.")
                messagebox.showerror("Analysis failed", res["error"])
                return
            var_status.set(
                f"Done. {res.get('uuid_entries_count', 0)} objects, {res.get('message_path_count', 0)} with message content."
            )
            threads_path = res.get("conversation_path")
            if threads_path and os.path.isfile(threads_path):
                btn_threads.config(state="normal", command=lambda: open_file(threads_path))
            btn_folder.config(state="normal", command=lambda: open_folder(out))
            # Structure report: generate on button click
            def do_structure():
                inp_path = var_input.get().strip()
                out_path = var_output.get().strip()
                if not inp_path or not os.path.isfile(inp_path) or not out_path:
                    messagebox.showwarning("Cannot generate", "Need a valid snapshot file and output folder.")
                    return
                btn_struct.state(["disabled"])
                var_status.set("Generating structure report...")
                progress.start(8)
                def gen_work():
                    r = generate_structure_report(inp_path, out_path)
                    root.after(0, lambda: on_struct_done(r))
                def on_struct_done(r):
                    progress.stop()
                    btn_struct.state(["!disabled"])
                    var_status.set("Ready.")
                    if r.get("error"):
                        messagebox.showerror("Structure report failed", r["error"])
                        return
                    path = r.get("uuid_only_path")
                    if path and os.path.isfile(path):
                        open_file(path)
                threading.Thread(target=gen_work, daemon=True).start()
            btn_struct.config(state="normal", command=do_structure)

        threading.Thread(target=work, daemon=True).start()

    btn_analyze.config(command=do_analyze)
    btn_analyze.pack(side="left", padx=(0, 8))
    progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Label(progress_frame, textvariable=var_status).pack(side="left")

    # Results
    ttk.Label(main, text="Results", style="Header.TLabel").pack(anchor="w", pady=(20, 4))
    result_btns = ttk.Frame(main)
    result_btns.pack(fill="x")
    btn_threads = ttk.Button(result_btns, text="Conversation threads", state="disabled")
    btn_threads.pack(side="left", padx=(0, 8))
    btn_struct = ttk.Button(result_btns, text="Structure report", state="disabled")
    btn_struct.pack(side="left", padx=(0, 8))
    btn_folder = ttk.Button(result_btns, text="Open folder", state="disabled")
    btn_folder.pack(side="left", padx=(0, 8))

    ttk.Label(main, text=f"v{APP_VERSION}").pack(anchor="w", pady=(24, 0))

    root.mainloop()


def run_gui_customtkinter():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.title(f"{APP_TITLE}  v{APP_VERSION}")
    root.minsize(560, 480)
    root.geometry("640x520")

    main = ctk.CTkFrame(root, fg_color="transparent")
    main.pack(fill="both", expand=True, padx=24, pady=24)

    ctk.CTkLabel(main, text=APP_TITLE, font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w")
    ctk.CTkLabel(
        main,
        text="Analyze V8 heap snapshots: object structure and conversation threads.",
        text_color="gray",
    ).pack(anchor="w")

    # Input
    ctk.CTkLabel(main, text="Heap snapshot file", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(20, 4))
    row1 = ctk.CTkFrame(main, fg_color="transparent")
    row1.pack(fill="x")
    var_input = ctk.StringVar()
    entry_input = ctk.CTkEntry(row1, textvariable=var_input, state="readonly")
    entry_input.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_input():
        path = filedialog.askopenfilename(
            title="Select heap snapshot",
            filetypes=[("Heap snapshot", "*.heapsnapshot"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if path:
            var_input.set(path)
            name = os.path.splitext(os.path.basename(path))[0]
            var_output.set(os.path.join(_TOOL_DIR, f"result_{name}"))

    ctk.CTkButton(row1, text="Browse...", width=100, command=browse_input).pack(side="right")

    # Output
    ctk.CTkLabel(main, text="Output folder", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(16, 4))
    row2 = ctk.CTkFrame(main, fg_color="transparent")
    row2.pack(fill="x")
    var_output = ctk.StringVar()
    entry_output = ctk.CTkEntry(row2, textvariable=var_output)
    entry_output.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_output():
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            var_output.set(path)

    ctk.CTkButton(row2, text="Browse...", width=100, command=browse_output).pack(side="right")

    # Analyze
    progress_frame = ctk.CTkFrame(main, fg_color="transparent")
    progress_frame.pack(fill="x", pady=(24, 0))
    var_status = ctk.StringVar(value="Ready.")
    progress = ctk.CTkProgressBar(progress_frame)
    progress.set(0)
    progress.pack(side="left", fill="x", expand=True, padx=(0, 12))

    btn_analyze = ctk.CTkButton(progress_frame, text="Analyze", width=120, command=lambda: None)
    btn_analyze.pack(side="left", padx=(0, 12))

    def do_analyze():
        inp = var_input.get().strip()
        out = var_output.get().strip()
        if not inp:
            messagebox.showwarning("No file", "Please select a heap snapshot file.")
            return
        if not os.path.isfile(inp):
            messagebox.showerror("File not found", f"File does not exist:\n{inp}")
            return
        if not out:
            out = os.path.join(_TOOL_DIR, "result_output")
        os.makedirs(out, exist_ok=True)
        btn_analyze.configure(state="disabled")
        var_status.set("Loading snapshot...")
        progress.set(0)

        def work():
            res = run_analysis(inp, out, generate_structure_report=False)
            root.after(0, lambda: on_done(res))

        def on_done(res):
            progress.set(1)
            btn_analyze.configure(state="normal")
            if res.get("error"):
                var_status.set("Error.")
                messagebox.showerror("Analysis failed", res["error"])
                return
            var_status.set(
                f"Done. {res.get('uuid_entries_count', 0)} objects, {res.get('message_path_count', 0)} with message content."
            )
            threads_path = res.get("conversation_path")
            if threads_path and os.path.isfile(threads_path):
                btn_threads.configure(state="normal", command=lambda: open_file(threads_path))
            btn_folder.configure(state="normal", command=lambda: open_folder(out))
            # Structure report: generate on button click
            def do_structure():
                inp_path = var_input.get().strip()
                out_path = var_output.get().strip()
                if not inp_path or not os.path.isfile(inp_path) or not out_path:
                    messagebox.showwarning("Cannot generate", "Need a valid snapshot file and output folder.")
                    return
                btn_struct.configure(state="disabled")
                var_status.set("Generating structure report...")
                progress.set(0)
                def gen_work():
                    r = generate_structure_report(inp_path, out_path)
                    root.after(0, lambda: on_struct_done(r))
                def on_struct_done(r):
                    progress.set(1)
                    btn_struct.configure(state="normal")
                    var_status.set("Ready.")
                    if r.get("error"):
                        messagebox.showerror("Structure report failed", r["error"])
                        return
                    path = r.get("uuid_only_path")
                    if path and os.path.isfile(path):
                        open_file(path)
                threading.Thread(target=gen_work, daemon=True).start()
            btn_struct.configure(state="normal", command=do_structure)

        threading.Thread(target=work, daemon=True).start()

    btn_analyze.configure(command=do_analyze)

    ctk.CTkLabel(progress_frame, textvariable=var_status, text_color="gray").pack(side="left")

    # Results
    ctk.CTkLabel(main, text="Results", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(28, 8))
    result_btns = ctk.CTkFrame(main, fg_color="transparent")
    result_btns.pack(fill="x")
    btn_threads = ctk.CTkButton(result_btns, text="Conversation threads", state="disabled", width=160)
    btn_threads.pack(side="left", padx=(0, 8))
    btn_struct = ctk.CTkButton(result_btns, text="Structure report", state="disabled", width=160)
    btn_struct.pack(side="left", padx=(0, 8))
    btn_folder = ctk.CTkButton(result_btns, text="Open folder", state="disabled", width=120)
    btn_folder.pack(side="left", padx=(0, 8))

    ctk.CTkLabel(main, text=f"v{APP_VERSION}", text_color="gray").pack(anchor="w", pady=(24, 0))

    root.mainloop()


def main():
    if HAS_CTK:
        run_gui_customtkinter()
    else:
        run_gui_plain_tk()


if __name__ == "__main__":
    main()
