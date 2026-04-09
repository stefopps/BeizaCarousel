#!/usr/bin/env python3
"""
Tkinter front-end for make_videos: pick a session folder, stream FFmpeg stderr to the window.
"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from make_videos import OUT_SUBDIR, SCRIPT_DIR, caption_summary_for_folder, process_session_folder


def main() -> None:
    root = tk.Tk()
    root.title("Carousel → MP4 (make_videos)")
    root.minsize(560, 420)

    work_var = tk.StringVar(value=str(SCRIPT_DIR))

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill=tk.BOTH, expand=True)

    row1 = ttk.Frame(frm)
    row1.pack(fill=tk.X)
    ttk.Label(row1, text="Session folder (Cover.jpg + *.json):").pack(side=tk.LEFT)
    ent = ttk.Entry(row1, textvariable=work_var, width=50)
    ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4))

    def browse() -> None:
        p = filedialog.askdirectory(initialdir=work_var.get() or str(SCRIPT_DIR))
        if p:
            work_var.set(p)

    ttk.Button(row1, text="Browse…", command=browse).pack(side=tk.RIGHT)

    cap_frame = ttk.LabelFrame(frm, text="Caption style (from your saved JSON — same fields as the Carousel editor)")
    cap_frame.pack(fill=tk.X, pady=(8, 0))
    cap_info = tk.Text(cap_frame, height=5, wrap=tk.WORD, font=("Segoe UI", 9), state=tk.DISABLED)
    cap_info.pack(fill=tk.X, padx=6, pady=6)

    def refresh_caption_info() -> None:
        d = Path(work_var.get().strip())
        txt = caption_summary_for_folder(d) if d.is_dir() else "Invalid folder."
        cap_info.configure(state=tk.NORMAL)
        cap_info.delete("1.0", tk.END)
        cap_info.insert(tk.END, txt)
        cap_info.configure(state=tk.DISABLED)

    work_var.trace_add("write", lambda *_: refresh_caption_info())

    row2 = ttk.Frame(frm)
    row2.pack(fill=tk.X, pady=(8, 0))
    prog = ttk.Progressbar(row2, mode="indeterminate", length=200)
    prog.pack(side=tk.LEFT)

    log_box = scrolledtext.ScrolledText(frm, height=18, wrap=tk.WORD, font=("Consolas", 9))
    log_box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

    def append_line(s: str) -> None:
        log_box.insert(tk.END, s + "\n")
        log_box.see(tk.END)

    run_btn = ttk.Button(row2, text="Run")
    run_btn.pack(side=tk.RIGHT, padx=(8, 0))

    def open_out() -> None:
        d = Path(work_var.get().strip())
        out = d / OUT_SUBDIR
        if not out.is_dir():
            messagebox.showinfo("mp4-out", f"Folder does not exist yet:\n{out}")
            return
        if os.name == "nt":
            os.startfile(out)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(out)])

    ttk.Button(row2, text="Open mp4-out folder", command=open_out).pack(side=tk.RIGHT)

    worker_running = threading.Event()

    def log_cb(msg: str) -> None:
        root.after(0, lambda m=msg: append_line(m))

    def run_job() -> None:
        if worker_running.is_set():
            return
        wd = Path(work_var.get().strip())
        if not wd.is_dir():
            messagebox.showerror("Invalid folder", str(wd))
            return

        worker_running.set()
        run_btn.configure(state=tk.DISABLED)
        prog.start(8)
        log_box.delete("1.0", tk.END)
        append_line(f"Working folder: {wd}")
        append_line("—")

        def work() -> None:
            try:
                process_session_folder(wd, log=log_cb)
            finally:
                def done() -> None:
                    prog.stop()
                    run_btn.configure(state=tk.NORMAL)
                    worker_running.clear()
                    append_line("—")
                    append_line("Finished.")

                root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    run_btn.configure(command=run_job)

    ttk.Label(
        frm,
        text=(
            "Workflow: In Carousel, save the session (stores caption sliders + export size in the JSON) "
            "and put Cover.jpg here. Run — MP4s go to mp4-out\\ . You do not re-enter caption settings; "
            "they are read from each .json like the FFmpeg ZIP in the app."
        ),
        wraplength=520,
    ).pack(anchor=tk.W, pady=(8, 0))

    refresh_caption_info()
    root.mainloop()


if __name__ == "__main__":
    main()
