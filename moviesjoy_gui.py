#!/usr/bin/env python3
"""
moviesjoy_gui.py — bulk download queue GUI for moviesjoy.mom (VidUp server),
on top of moviesjoy_dl.py.

Launch: double-click "MoviesJoy Downloader" desktop shortcut, or
    pythonw moviesjoy_gui.py
"""

import json
import os
import queue as pyqueue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moviesjoy_dl as core  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(APP_DIR, "queue.json")

PENDING, PREPARING, DOWNLOADING, DONE, FAILED, CANCELED = (
    "pending", "preparing", "downloading", "done", "failed", "canceled")

ICON = {PENDING: "\u2026", PREPARING: "~", DOWNLOADING: "\u00bb",
        DONE: "\u2713", FAILED: "\u00d7", CANCELED: "-"}

BG = "#14141d"
PANEL = "#1d1d2b"
PANEL2 = "#232335"
FG = "#e9e9f2"
MUTED = "#9a9ab0"
ACCENT = "#ff5fa2"
GOOD = "#6ee7a0"
BAD = "#ff6b6b"


def read_queue():
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            items = json.load(f).get("items", [])
        for it in items:
            if it.get("status") in (PREPARING, DOWNLOADING, PENDING, FAILED, CANCELED):
                it["status"] = PENDING
                it["pct"] = 0
        return items
    except Exception:
        return []


class App:
    def __init__(self, root):
        self.root = root
        root.title("MoviesJoy Downloader")
        root.geometry("940x680")
        root.minsize(820, 600)
        root.configure(bg=BG)

        self.items = read_queue()          # {target,title,status,pct,mb,path}
        self.evq = pyqueue.Queue()
        self.cancel_events = {}            # item index -> threading.Event
        self.stop_all = threading.Event()
        self.workers_active = 0
        self.lock = threading.Lock()
        self._last_results = []

        self._style()
        self._build()
        self._redraw()
        self._log("Welcome~ Add movies via search or paste URLs, then hit Start.")
        if not core.shutil.which("ffmpeg"):
            self._log("WARNING: ffmpeg not found on PATH — downloads will fail at muxing.")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._poll)

    # ---------------------------------------------------------------- style
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                     bordercolor=PANEL2, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                     focusthickness=0)
        st.map("TButton", background=[("active", "#33334a")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#14141d")
        st.map("Accent.TButton", background=[("active", "#ff7cb5")])
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                     insertcolor=FG, borderwidth=0)
        st.configure("TCombobox", fieldbackground=PANEL2, foreground=FG,
                     arrowcolor=FG, borderwidth=0)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)])
        st.configure("Horizontal.TProgressbar", background=ACCENT,
                     troughcolor=PANEL2, borderwidth=0)

    # ---------------------------------------------------------------- build
    def _build(self):
        pad = {"padx": 10, "pady": 6}

        # --- search row
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=34)
        search_entry.pack(side="left", padx=(6, 6))
        search_entry.bind("<Return>", lambda e: self._start_search())
        ttk.Button(top, text="Search", command=self._start_search).pack(side="left")
        ttk.Button(top, text="Add selected", command=self._add_selected_results,
                   style="Accent.TButton").pack(side="left", padx=6)

        self.results = tk.Listbox(self.root, height=5, bg=PANEL, fg=FG,
                                  selectbackground=ACCENT, selectforeground="#14141d",
                                  relief="flat", highlightthickness=0,
                                  activestyle="none", font=("Segoe UI", 9))
        self.results.pack(fill="x", padx=10)
        self.results.bind("<Double-Button-1>", lambda e: self._add_selected_results())
        ttk.Label(self.root, text="(double-click or select several + Add selected)",
                  style="Muted.TLabel").pack(anchor="w", padx=12)

        # --- paste row
        paste_row = ttk.Frame(self.root)
        paste_row.pack(fill="x", **pad)
        ttk.Label(paste_row, text="Or paste movie URLs / TMDB ids (one per line):").pack(side="left")
        self.paste_var = tk.StringVar()
        paste_entry = ttk.Entry(paste_row, textvariable=self.paste_var)
        paste_entry.pack(side="left", fill="x", expand=True, padx=6)
        paste_entry.bind("<Return>", lambda e: self._add_paste())
        ttk.Button(paste_row, text="Add to queue", command=self._add_paste,
                   style="Accent.TButton").pack(side="left")

        # --- settings row
        set_row = ttk.Frame(self.root)
        set_row.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(set_row, text="Quality:").pack(side="left")
        self.quality_var = tk.StringVar(value="best")
        ttk.Combobox(set_row, textvariable=self.quality_var, width=7, state="readonly",
                     values=["best", "1080", "720", "480"]).pack(side="left", padx=(4, 12))
        ttk.Label(set_row, text="Segment workers:").pack(side="left")
        self.workers_var = tk.IntVar(value=8)
        ttk.Spinbox(set_row, from_=2, to=16, textvariable=self.workers_var, width=4).pack(
            side="left", padx=(4, 12))
        ttk.Label(set_row, text="Parallel downloads:").pack(side="left")
        self.parallel_var = tk.IntVar(value=1)
        ttk.Spinbox(set_row, from_=1, to=3, textvariable=self.parallel_var, width=4).pack(
            side="left", padx=(4, 12))
        ttk.Label(set_row, text="Save to:").pack(side="left")
        self.outdir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Videos"))
        ttk.Entry(set_row, textvariable=self.outdir_var, width=24).pack(side="left", padx=4)
        ttk.Button(set_row, text="...", width=3, command=self._pick_dir).pack(side="left")
        ttk.Button(set_row, text="Open folder", command=self._open_folder).pack(
            side="left", padx=6)

        # --- queue
        qhead = ttk.Frame(self.root)
        qhead.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(qhead, text="Queue").pack(side="left")
        ttk.Button(qhead, text="Remove selected", command=self._remove_selected).pack(side="right")
        ttk.Button(qhead, text="Clear finished", command=self._clear_finished).pack(side="right", padx=6)

        self.queue_list = tk.Listbox(self.root, bg=PANEL, fg=FG, relief="flat",
                                     selectbackground="#3a3a55", selectforeground=FG,
                                     highlightthickness=0, activestyle="none",
                                     font=("Segoe UI", 10))
        self.queue_list.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        self.queue_list.bind("<Delete>", lambda e: self._remove_selected())

        # --- progress
        prog = ttk.Frame(self.root)
        prog.pack(fill="x", padx=10)
        self.cur_label = ttk.Label(prog, text="Idle.", style="Muted.TLabel")
        self.cur_label.pack(anchor="w")
        self.cur_bar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.cur_bar.pack(fill="x", pady=(2, 4))
        self.all_label = ttk.Label(prog, text="", style="Muted.TLabel")
        self.all_label.pack(anchor="w")

        # --- buttons + log
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=10, pady=6)
        self.start_btn = ttk.Button(btns, text="Start queue", style="Accent.TButton",
                                    command=self._start_queue)
        self.start_btn.pack(side="left")
        ttk.Button(btns, text="Stop", command=self._stop).pack(side="left", padx=6)
        self.log_text = ScrolledText(self.root, height=8, bg=PANEL, fg=FG, relief="flat",
                                     highlightthickness=0, font=("Consolas", 8),
                                     state="disabled")
        self.log_text.pack(fill="both", padx=10, pady=(0, 10))

    # ---------------------------------------------------------------- helpers
    def _log(self, msg):
        self.evq.put(("log", msg))

    def _setitem(self, idx, status=None, pct=None, title=None, mb=None, path=None):
        with self.lock:
            it = self.items[idx]
            if status is not None:
                it["status"] = status
            if pct is not None:
                it["pct"] = pct
            if title is not None:
                it["title"] = title
            if mb is not None:
                it["mb"] = mb
            if path is not None:
                it["path"] = path
        self.evq.put(("redraw", None))

    def _redraw(self):
        self.queue_list.delete(0, "end")
        for it in self.items:
            icon = ICON.get(it.get("status", PENDING), "\u2026")
            title = it.get("title") or it["target"]
            line = f"[{icon}] {title}"
            if it.get("status") == DOWNLOADING:
                line += f"  \u2014  {it.get('pct', 0)}%  ({it.get('mb', 0)} MB)"
            elif it.get("status") == DONE:
                line += f"  \u2014  saved ({it.get('mb', '?')} MB)"
            elif it.get("status") == FAILED:
                line += "  \u2014  failed (see log)"
            self.queue_list.insert("end", line)
        done_n = sum(1 for i in self.items if i.get("status") == DONE)
        total = len(self.items)
        self.all_label.config(text=f"Finished {done_n}/{total} movies this session."
                              if total else "Queue is empty, cutie~ add some movies.")

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir_var.get() or os.path.expanduser("~"))
        if d:
            self.outdir_var.set(d)

    def _open_folder(self):
        d = self.outdir_var.get()
        if os.path.isdir(d):
            os.startfile(d)

    def _save_queue(self):
        try:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump({"items": self.items}, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _on_close(self):
        self.stop_all.set()
        for evt in self.cancel_events.values():
            evt.set()
        self._save_queue()
        self.root.destroy()

    # ---------------------------------------------------------------- add / remove
    def _add_targets(self, targets):
        added = 0
        with self.lock:
            existing = {it["target"] for it in self.items}
            for t in targets:
                t = t.strip()
                if t and t not in existing:
                    self.items.append({"target": t, "title": None, "status": PENDING,
                                       "pct": 0, "mb": 0, "path": None})
                    existing.add(t)
                    added += 1
        self.evq.put(("redraw", None))
        self._log(f"Added {added} movie(s) to the queue.")
        self._save_queue()

    def _add_paste(self):
        raw = self.paste_var.get().replace(",", "\n").split()
        targets = [x for x in (s.strip() for s in raw) if x]
        if targets:
            self._add_targets(targets)
            self.paste_var.set("")

    def _start_search(self):
        q = self.search_var.get().strip()
        if not q:
            return
        self.results.delete(0, "end")
        self.results.insert("end", "  searching\u2026")
        def work():
            try:
                res = core.search_movies(core.make_session(), q)
            except Exception as e:
                res = []
                self._log(f"Search failed: {e}")
            self.evq.put(("results", res))
        threading.Thread(target=work, daemon=True).start()

    def _add_selected_results(self):
        sel = [self.results.get(i) for i in self.results.curselection()]
        # map displayed lines back to urls via stored results
        picked = []
        for i in self.results.curselection():
            if 0 <= i < len(self._last_results):
                picked.append(self._last_results[i]["url"])
        if picked:
            self._add_targets(picked)

    # ---------------------------------------------------------------- queue ops
    def _remove_selected(self):
        sel = sorted(self.queue_list.curselection(), reverse=True)
        with self.lock:
            for i in sel:
                if self.items[i].get("status") == DOWNLOADING:
                    evt = self.cancel_events.get(i)
                    if evt:
                        evt.set()
                del self.items[i]
        self.evq.put(("redraw", None))
        self._save_queue()

    def _clear_finished(self):
        with self.lock:
            self.items = [it for it in self.items if it.get("status") != DONE]
        self.evq.put(("redraw", None))
        self._save_queue()

    def _start_queue(self):
        if self.workers_active:
            return
        self.stop_all.clear()
        n_par = max(1, min(3, int(self.parallel_var.get())))
        pending = sum(1 for it in self.items if it.get("status") == PENDING)
        if not pending:
            self._log("Nothing pending in the queue.")
            return
        n = min(n_par, pending)
        self.workers_active = n
        self.start_btn.config(state="disabled")
        for _ in range(n):
            threading.Thread(target=self._queue_worker, daemon=True).start()

    def _stop(self):
        self.stop_all.set()
        for evt in self.cancel_events.values():
            evt.set()
        self._log("Stop requested\u2026")

    def _next_pending(self):
        with self.lock:
            for i, it in enumerate(self.items):
                if it.get("status") == PENDING:
                    it["status"] = PREPARING
                    return i
        return None

    def _queue_worker(self):
        while not self.stop_all.is_set():
            idx = self._next_pending()
            if idx is None:
                break
            self._download_item(idx)
        with self.lock:
            self.workers_active = max(0, self.workers_active - 1)
            if self.workers_active == 0:
                self.evq.put(("queue_done", None))

    def _download_item(self, idx):
        with self.lock:
            target = self.items[idx]["target"]
        cancel = threading.Event()
        self.cancel_events[idx] = cancel
        session = core.make_session()
        self._setitem(idx, status=PREPARING)
        try:
            prep = core.prepare_movie(session, target, log=self._log)
            self._setitem(idx, status=DOWNLOADING, title=prep["title"], pct=0)
            outdir = self.outdir_var.get().strip() or os.path.join(os.path.expanduser("~"), "Videos")

            def progress(done, total, nbytes):
                pct = int(done * 100 / max(total, 1))
                self._setitem(idx, pct=pct, mb=int(nbytes / 1e6))
                self.evq.put(("progress", (prep["title"], pct, done, total)))

            res = core.download_movie(
                session, prep, quality=self.quality_var.get(), out=outdir,
                workers=max(2, int(self.workers_var.get())), log=self._log,
                progress=progress, cancel=cancel)
            if res["status"] == "done":
                mb = int(os.path.getsize(res["path"]) / 1e6)
                self._setitem(idx, status=DONE, pct=100, path=res["path"], mb=mb)
                self._log(f"\u2713 {prep['title']} finished -> {res['path']}")
            elif res["status"] == "canceled":
                self._setitem(idx, status=CANCELED)
                self._log(f"- {prep['title']} canceled.")
            else:
                self._setitem(idx, status=FAILED)
                self._log(f"\u00d7 {prep['title']} failed: {res.get('error')}")
        except Exception as e:
            self._setitem(idx, status=FAILED)
            self._log(f"\u00d7 failed: {e}")
        finally:
            self.cancel_events.pop(idx, None)
            self._save_queue()

    # ---------------------------------------------------------------- event poll
    def _poll(self):
        try:
            while True:
                kind, payload = self.evq.get_nowait()
                if kind == "log":
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", str(payload) + "\n")
                    if int(self.log_text.index("end-1c").split(".")[0]) > 500:
                        self.log_text.delete("1.0", "100.0")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
                elif kind == "redraw":
                    self._redraw()
                elif kind == "results":
                    self._last_results = payload
                    self.results.delete(0, "end")
                    if not payload:
                        self.results.insert("end", "  (no results)")
                    for r in payload:
                        self.results.insert("end", f"  {r['title']}   [{r['url'].rsplit('/', 1)[-1]}]")
                elif kind == "progress":
                    title, pct, done, total = payload
                    self.cur_bar.config(value=pct)
                    self.cur_label.config(text=f"{title}  \u2014  {pct}%  ({done}/{total} segments)")
                elif kind == "queue_done":
                    self.start_btn.config(state="normal")
                    self.cur_label.config(text="Idle.")
                    self.cur_bar.config(value=0)
                    self._log("Queue finished. Enjoy the flight~")
        except pyqueue.Empty:
            pass
        self.root.after(150, self._poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
