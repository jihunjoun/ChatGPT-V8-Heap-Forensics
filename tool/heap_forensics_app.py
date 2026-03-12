# -*- coding: utf-8 -*-
"""
Heap Snapshot Forensics — 단일 실행 진입점 (CLI + GUI)
- 인자 없이 실행 또는 --gui : GUI 실행
- .heapsnapshot 경로 지정 : CLI 분석 실행 (결과는 스냅샷과 같은 폴더 또는 두 번째 인자로 출력 폴더 지정)
"""

import os
import sys

# 같은 폴더의 heap_forensics, heap_forensics_gui 를 import 할 수 있도록
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)


def _is_gui_request() -> bool:
    if len(sys.argv) <= 1:
        return True
    a = sys.argv[1].strip().lower()
    return a in ("--gui", "-g", "/gui", "-gui", "--ui")


def _run_cli():
    from heap_forensics import run_analysis

    snapshot_path = os.path.abspath(sys.argv[1])
    output_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else None

    if not os.path.isfile(snapshot_path):
        print("File not found:", snapshot_path)
        sys.exit(2)

    result = run_analysis(snapshot_path, output_dir)

    if result.get("error"):
        print("Error:", result["error"])
        sys.exit(1)

    print("Done:", result["uuid_only_path"])
    print("Conversation HTML:", result["conversation_path"])
    print("Conversation JSON:", result["conversation_json_path"])
    print("(See HTML header for message->content->parts->elements count; 0 = path not in heap or different names)")


def _run_gui():
    from heap_forensics_gui import main as gui_main
    gui_main()


def main():
    if _is_gui_request():
        _run_gui()
    else:
        _run_cli()


if __name__ == "__main__":
    main()
