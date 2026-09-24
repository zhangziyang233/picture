# -*- coding: utf-8 -*-
"""Agnes desktop UI. Reuses agnes_image; no independent/mock generation backend."""
import importlib.util
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agnes_image as ai
try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None

APP_TITLE = "Agnes 生图工具"
FONT = ("Microsoft YaHei UI", 9)
STATUS_NAMES = {"success": "已完成", "partial": "部分完成", "failed": "失败", "running": "执行中 / 上次中断",
                "stopped": "已停止后续", "pending": "待提交"}


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.last_path = None
        self.current_record = None
        self.active_id = None
        self.records = {}
        self.stop_event = threading.Event()
        self.started = 0
        self.phase = "就绪"
        self.styles, self.defaults = ai.load_styles()
        self.style_names = [s["name"] for s in self.styles]
        self.style_ids = [s["id"] for s in self.styles]
        self.mode = tk.StringVar(value="text")
        self.reference = tk.StringVar()
        self.count = tk.StringVar(value="1")
        self.timeout = tk.StringVar(value="300")
        self.auto_open = tk.BooleanVar(value=False)
        self.photos = {}
        self.controls = []
        root.title(APP_TITLE)
        root.geometry("1100x820")
        root.minsize(1020, 780)
        root.option_add("*Font", FONT)
        self._build()
        self._refresh_preview()
        self._mode_changed()
        self.refresh_history()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self._drain)

    def _button(self, parent, text, command, **pack):
        b = ttk.Button(parent, text=text, command=command)
        b.pack(**pack)
        return b

    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self._build_api_bar(root)
        main = ttk.Frame(root, padding=14)
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1, uniform="half")
        main.columnconfigure(1, weight=1, uniform="half")
        main.rowconfigure(0, weight=1)
        left = ttk.Frame(main, padding=(0, 0, 14, 0))
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        modebar = ttk.Frame(left)
        modebar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for text, val in (("文字生图", "text"), ("图片生图", "image")):
            rb = ttk.Radiobutton(modebar, text=text, variable=self.mode, value=val, command=self._mode_changed)
            rb.pack(side="left", padx=(0, 24))
            self.controls.append((rb, "normal"))
        ttk.Label(left, text="提示词 · 描述主体、风格，以及要保留或改变的内容").grid(row=1, column=0, sticky="w")
        wrap = ttk.Frame(left)
        wrap.grid(row=2, column=0, sticky="nsew", pady=6)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.text = tk.Text(wrap, wrap="word", height=6, undo=True, font=FONT)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.bind("<<Modified>>", self._text_changed)
        self.char_label = ttk.Label(left, text="0 字")
        self.char_label.grid(row=3, column=0, sticky="e")
        self.ref_frame = ttk.LabelFrame(left, text="参考图片 · 仅在点击生成时上传", padding=8)
        self.ref_frame.grid(row=4, column=0, sticky="ew", pady=5)
        self.ref_frame.columnconfigure(1, weight=1)
        self.ref_preview = tk.Label(self.ref_frame, text="未选择图片", width=16, height=5, bg="#f3f3f3")
        self.ref_preview.grid(row=0, column=0, rowspan=3, padx=(0, 8))
        refbuttons = ttk.Frame(self.ref_frame)
        refbuttons.grid(row=0, column=1, sticky="w")
        b = self._button(refbuttons, "选择图片…", self.choose_reference, side="left")
        c = self._button(refbuttons, "移除", self.clear_reference, side="left", padx=5)
        self.controls += [(b, "normal"), (c, "normal")]
        self.ref_info = ttk.Label(self.ref_frame, text="PNG / JPEG / WebP\n≤10 MB，≤4000 万像素", wraplength=270)
        self.ref_info.grid(row=1, column=1, sticky="w")
        ttk.Label(self.ref_frame, text="图片与提示词将发送给你所配置 Key 对应的 Agnes 云服务；请确认拥有使用权限。", wraplength=270).grid(row=2, column=1, sticky="w")
        options = ttk.LabelFrame(left, text="生成参数", padding=8)
        options.grid(row=5, column=0, sticky="ew", pady=6)
        self.style_cb = self._combo(options, "风格", self.style_names, 0, 0, 19)
        self.size_cb = self._combo(options, "尺寸", ai.SIZES, 1, 0, 8)
        self.ratio_cb = self._combo(options, "比例", ai.RATIOS, 1, 2, 8)
        self.count_cb = self._combo(options, "数量", ["1", "2", "3", "4"], 2, 0, 8)
        self.timeout_cb = self._combo(options, "超时/秒", ["60", "120", "300", "600"], 2, 2, 8)
        self.style_cb.set(self.style_names[self.style_ids.index(self.defaults["style"])])
        self.size_cb.set(self.defaults["size"])
        self.ratio_cb.set(self.defaults["ratio"])
        self.count_cb.set("1")
        self.timeout_cb.set("300")
        self.style_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_preview())
        ttk.Label(options, text="数量为逐张提交；2K–4K 可能更慢。费用以账户为准。", wraplength=430).grid(row=3, column=0, columnspan=4, sticky="w", pady=5)
        ttk.Label(left, text="实际发送的提示词（实时预览）").grid(row=6, column=0, sticky="w")
        self.preview = tk.Text(left, height=3, wrap="word", state="disabled", bg="#f5f5f5", font=FONT)
        self.preview.grid(row=7, column=0, sticky="ew", pady=5)
        actions = ttk.Frame(left)
        actions.grid(row=8, column=0, sticky="ew", pady=8)
        self.btn_gen = self._button(actions, "生成图片", self.on_generate, side="left")
        self.btn_stop = self._button(actions, "停止后续", self.on_stop, side="left", padx=6)
        self.btn_stop.configure(state="disabled")
        self.reload_btn = self._button(actions, "刷新风格", self.reload_styles, side="left")
        self.controls.append((self.reload_btn, "normal"))
        ttk.Checkbutton(left, text="完成后用系统查看器打开首张图片", variable=self.auto_open).grid(row=9, column=0, sticky="w")
        ttk.Label(left, text="记录保存在 outputs/records（含提示词及参考图路径，不含 Key）。", wraplength=470).grid(row=10, column=0, sticky="w", pady=8)
        outbtn = ttk.Button(left, text="打开输出目录", command=self.open_out_dir)
        outbtn.grid(row=11, column=0, sticky="w")

        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.tabs = ttk.Notebook(right)
        self.tabs.grid(row=0, column=0, sticky="ew")
        resulttab = ttk.Frame(self.tabs, padding=4)
        historytab = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(resulttab, text="任务结果")
        self.tabs.add(historytab, text="生成记录")
        self.result_tree = ttk.Treeview(resulttab, columns=("state", "file"), show="headings", height=5)
        self.result_tree.heading("state", text="状态")
        self.result_tree.heading("file", text="文件 / 错误摘要")
        self.result_tree.column("state", width=75, stretch=False)
        self.result_tree.column("file", width=355)
        self.result_tree.pack(fill="both", expand=True)
        self.result_tree.bind("<<TreeviewSelect>>", self.select_result)
        self.history = ttk.Treeview(historytab, columns=("time", "state", "prompt"), show="headings", height=5)
        for key, text, width in (("time", "时间", 150), ("state", "状态", 95), ("prompt", "描述", 185)):
            self.history.heading(key, text=text)
            self.history.column(key, width=width)
        self.history.pack(side="left", fill="both", expand=True)
        hs = ttk.Scrollbar(historytab, command=self.history.yview)
        hs.pack(side="right", fill="y")
        self.history.configure(yscrollcommand=hs.set)
        self.history.bind("<<TreeviewSelect>>", self.select_history)
        self.result_preview = tk.Label(right, text="生成后在此预览\n也可从生成记录选择已有结果", bg="#f4f4f4", fg="#555555")
        self.result_preview.grid(row=1, column=0, sticky="nsew", pady=8)
        self.result_preview.bind("<Configure>", self._resize_preview)
        ra = ttk.Frame(right)
        ra.grid(row=2, column=0, sticky="ew", pady=5)
        self.open_btn = self._button(ra, "查看原图", self.open_result, side="left")
        self.save_btn = self._button(ra, "另存为…", self.save_as, side="left", padx=6)
        self.reuse_btn = self._button(ra, "复用参数", self.reuse_params, side="left")
        self.retry_btn = self._button(ra, "重试未完成", self.retry_job, side="left", padx=6)
        self.details = tk.Text(right, height=7, wrap="word", state="disabled", font=FONT, bg="#f5f5f5")
        self.details.grid(row=3, column=0, sticky="ew", pady=6)
        ttk.Button(right, text="刷新记录", command=self.refresh_history).grid(row=4, column=0, sticky="e")
        foot = ttk.Frame(root, padding=(14, 0, 14, 12))
        foot.grid(row=2, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(foot, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew", pady=5)
        self.status = ttk.Label(foot, text="就绪", wraplength=1000)
        self.status.grid(row=1, column=0, sticky="w")
        self._buttons()

    def _build_api_bar(self, root):
        """Own-key entry: saved once, reused forever until changed again."""
        bar = ttk.LabelFrame(root, text="Agnes API 设置 · 保存后持续生效，直到再次修改", padding=(10, 8))
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="API Key").grid(row=0, column=0, sticky="w")
        self.api_key = tk.StringVar()
        self.show_key = tk.BooleanVar(value=False)
        self.api_entry = ttk.Entry(bar, textvariable=self.api_key, show="*", width=58)
        self.api_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.controls.append((self.api_entry, "normal"))
        btns = ttk.Frame(bar)
        btns.grid(row=0, column=2, sticky="w")
        for text, cmd in (("保存", self.save_api_key), ("测试连接", self.test_api_key), ("清除", self.clear_api_key)):
            b = ttk.Button(btns, text=text, command=cmd, width=8)
            b.pack(side="left", padx=(0, 4))
            self.controls.append((b, "normal"))
        eye = ttk.Checkbutton(bar, text="显示", variable=self.show_key, command=self._toggle_key_visibility)
        eye.grid(row=0, column=3, sticky="w")
        self.controls.append((eye, "normal"))
        self.api_status = ttk.Label(bar, text="", wraplength=1000, justify="left")
        self.api_status.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.api_key.set(ai.get_saved_api_key() or "")
        self.refresh_api_status()

    def _toggle_key_visibility(self):
        self.api_entry.configure(show="" if self.show_key.get() else "*")

    def refresh_api_status(self):
        try:
            cred = ai.active_credential()
        except ai.GenError as e:
            self.api_status.configure(text="配置读取失败：%s" % e)
            return
        names = {"tool": "来源：本工具保存的个人 Key（一直生效，直到再次修改）",
                 "env": "来源：环境变量 AGNES_API_KEY",
                 "models": "来源：models.json 中 Agnes 对话模型的 Key（未设置个人 Key 时的回退）",
                 "none": "未找到可用 Key，请在上方填写并保存，或设置环境变量 AGNES_API_KEY"}
        shown = ai.mask_key(cred["apiKey"])
        head = ("Key %s · " % shown) if shown else ""
        self.api_status.configure(text="%s%s\n接口地址：%s" % (head, names[cred["source"]], cred["url"]))

    def save_api_key(self):
        value = self.api_key.get().strip()
        if not value:
            messagebox.showwarning(APP_TITLE, "请先填写 API Key，再点击保存。")
            return
        try:
            ai.save_api_config(value)
        except ai.GenError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.api_key.set(ai.get_saved_api_key())
        self.refresh_api_status()
        self.phase = "API Key 已保存，后续生成都将使用它。"
        messagebox.showinfo(APP_TITLE, "API Key 已保存到本目录 api_config.json，后续生成一直使用，直到再次修改。")

    def clear_api_key(self):
        if not ai.get_saved_api_key():
            messagebox.showinfo(APP_TITLE, "当前没有保存的 Key。")
            return
        if not messagebox.askyesno(APP_TITLE, "删除保存的 Key 后，将回退到环境变量或 models.json 中的 Key。确认删除？"):
            return
        try:
            ai.clear_api_config()
        except ai.GenError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.api_key.set("")
        self.refresh_api_status()
        self.phase = "已删除保存的 Key。"

    def test_api_key(self):
        value = self.api_key.get().strip() or ai.get_saved_api_key()
        if not value:
            messagebox.showwarning(APP_TITLE, "请先填写或保存一个 API Key。")
            return
        self.phase = "正在校验 API Key…"
        self.status.configure(text=self.phase)
        self.root.update_idletasks()

        def run():
            try:
                message = ai.test_api_key(value)
                self.q.put(("api_test", (True, message)))
            except ai.GenError as e:
                self.q.put(("api_test", (False, str(e))))

        threading.Thread(target=run, daemon=True).start()

    def _combo(self, parent, text, values, row, col, width):
        ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", pady=4)
        box = ttk.Combobox(parent, values=values, state="readonly", width=width)
        box.grid(row=row, column=col+1, sticky="w", padx=(8, 15))
        self.controls.append((box, "readonly"))
        return box

    def _mode_changed(self):
        if self.mode.get() == "image":
            self.ref_frame.grid()
        else:
            self.ref_frame.grid_remove()
        self._refresh_preview()

    def _text_changed(self, event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._refresh_preview()

    def _current_style_id(self):
        index = self.style_cb.current()
        return self.style_ids[index] if index >= 0 else self.defaults["style"]

    def _set_text(self, widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _refresh_preview(self):
        raw = self.text.get("1.0", "end-1c")
        try:
            final = ai.build_prompt(raw, self._current_style_id(), self.styles)
        except ai.GenError as e:
            final = str(e)
        self._set_text(self.preview, final)
        self.char_label.configure(text="原文 %d 字 · 最终 %d / %d 字" % (len(raw), len(final), ai.MAX_PROMPT))

    def reload_styles(self):
        if self.busy:
            return
        try:
            old = self._current_style_id()
            self.styles, self.defaults = ai.load_styles()
            self.style_names = [s["name"] for s in self.styles]
            self.style_ids = [s["id"] for s in self.styles]
            self.style_cb.configure(values=self.style_names)
            sid = old if old in self.style_ids else self.defaults["style"]
            self.style_cb.current(self.style_ids.index(sid))
            self._refresh_preview()
        except ai.GenError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def choose_reference(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="选择参考图片", filetypes=[("静态图片", "*.png *.jpg *.jpeg *.webp")])
        if path:
            self.set_reference(path, match_ratio=True)

    def set_reference(self, path, match_ratio=False):
        try:
            _, info = ai.read_reference(path)
            self.reference.set(info["path"])
            self.ref_info.configure(text="%s\n%d × %d · %.1f MB" % (Path(path).name, info["width"], info["height"], info["bytes"] / 1024**2))
            self._thumbnail(path, self.ref_preview, "reference", (130, 94))
            if match_ratio:
                ratio = info["width"] / info["height"]
                nearest = min(ai.RATIOS, key=lambda v: abs(int(v.split(":")[0]) / int(v.split(":")[1]) - ratio))
                self.ratio_cb.set(nearest)
        except ai.GenError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def clear_reference(self):
        if self.busy:
            return
        self.reference.set("")
        self.photos.pop("reference", None)
        self.ref_preview.configure(image="", text="未选择图片", width=16, height=5)
        self.ref_info.configure(text="PNG / JPEG / WebP\n≤10 MB，≤4000 万像素")

    def _thumbnail(self, path, target, key, size):
        if not Image:
            target.configure(image="", text="缺少 Pillow，无法内嵌预览；可查看原图。")
            return
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail(size)
                photo = ImageTk.PhotoImage(im.copy(), master=self.root)
            self.photos[key] = photo
            target.configure(image=photo, text="", width=0, height=0)
        except Exception:
            target.configure(image="", text="图片已移动或无法预览，请查看文件状态。")

    def _resize_preview(self, event=None):
        if self.last_path and self.result_preview.winfo_width() > 10:
            self._thumbnail(self.last_path, self.result_preview, "result", (max(100, self.result_preview.winfo_width()-16), max(100, self.result_preview.winfo_height()-16)))

    def _snapshot(self):
        raw = self.text.get("1.0", "end-1c").strip()
        try:
            count, timeout = int(self.count_cb.get()), int(self.timeout_cb.get())
        except ValueError:
            raise ai.GenError("数量和超时必须为整数。") from None
        image = self.reference.get() if self.mode.get() == "image" else None
        if self.mode.get() == "image" and not image:
            raise ai.GenError("图片生图模式需要选择参考图片。")
        ai.validate_request(raw, self.size_cb.get(), self.ratio_cb.get(), count, timeout, image)
        final = ai.build_prompt(raw, self._current_style_id(), self.styles)
        ai.validate_request(final, self.size_cb.get(), self.ratio_cb.get(), count, timeout)
        ai.load_model()
        return dict(prompt=raw, style_id=self._current_style_id(), size=self.size_cb.get(),
                    ratio=self.ratio_cb.get(), count=count, timeout=timeout, image=image)

    def on_generate(self):
        if self.busy:
            return
        try:
            args = self._snapshot()
        except ai.GenError as e:
            messagebox.showwarning(APP_TITLE, str(e))
            return
        if args["image"] and not messagebox.askyesno(APP_TITLE, "参考图片与提示词将发送给 Agnes 云服务。确认上传并生成？"):
            return
        if args["count"] > 1 and not messagebox.askyesno(APP_TITLE, "将逐张提交 %d 次请求，可能按张计费。确认继续？" % args["count"]):
            return
        self.current_record = None
        self.result_tree.delete(*self.result_tree.get_children())
        self.tabs.select(0)
        self._start(args)

    def _start(self, args):
        if self.busy:
            return
        self.busy = True
        self.stop_event.clear()
        self.active_id = None
        self.started = time.monotonic()
        self.phase = "正在校验配置…"
        self.text.configure(state="disabled")
        for widget, _ in self.controls:
            widget.configure(state="disabled")
        self.btn_gen.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.start(15)
        self._buttons()
        threading.Thread(target=self._worker, args=(dict(args),), daemon=True).start()

    def _worker(self, args):
        try:
            record = ai.run_job(**args, stop_event=self.stop_event, on_event=lambda k, v: self.q.put((k, v)))
            self.q.put(("finished", record))
        except ai.GenError as e:
            self.q.put(("error", str(e)))
        except Exception:
            self.q.put(("error", "任务异常终止，请检查本地磁盘与配置；已有结果会保留在输出目录。"))

    def on_stop(self):
        if self.busy:
            self.stop_event.set()
            self.phase = "将在当前请求完成后停止后续图片；不代表取消云端当前任务。"
            self.btn_stop.configure(state="disabled")

    def _finish_controls(self):
        self.busy = False
        self.progress.stop()
        self.text.configure(state="normal")
        for widget, state in self.controls:
            widget.configure(state=state)
        self.btn_gen.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._buttons()

    def _drain(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "record":
                    self.active_id = value
                elif kind == "status":
                    self.phase = value
                elif kind == "result":
                    self.last_path = value
                    self._resize_preview()
                elif kind == "api_test":
                    ok, message = value
                    self.phase = message
                    self.refresh_api_status()
                    (messagebox.showinfo if ok else messagebox.showerror)(APP_TITLE, message)
                elif kind == "finished":
                    self.current_record = value
                    self._finish_controls()
                    self.display_record(value)
                    self.refresh_history()
                    success = sum(i["status"] == "success" for i in value["items"])
                    self.phase = "%s · 已保存 %d/%d 张 · %.1f 秒" % (STATUS_NAMES[value["status"]], success, value["params"]["count"], time.monotonic()-self.started)
                    if success and self.auto_open.get():
                        self._open(next(i["path"] for i in value["items"] if i["status"] == "success"))
                elif kind == "error":
                    self._finish_controls()
                    self.phase = value
                    if self.active_id:
                        try:
                            self.display_record(ai.load_record(self.active_id))
                        except ai.GenError:
                            pass
                    self.refresh_history()
                    messagebox.showerror(APP_TITLE, value)
        except queue.Empty:
            pass
        suffix = " · 已等待 %d 秒" % (time.monotonic()-self.started) if self.busy else ""
        self.status.configure(text=self.phase + suffix)
        self.root.after(120, self._drain)

    def refresh_history(self):
        if self.busy:
            return
        records, warnings = ai.list_records()
        self.records = {r["id"]: r for r in records}
        self.history.delete(*self.history.get_children())
        for r in records:
            self.history.insert("", "end", iid=r["id"], values=(r["created"][:19].replace("T", " "), STATUS_NAMES.get(r["status"], r["status"]), r["params"].get("prompt", "")[:35]))
        if warnings:
            self.phase = "已跳过 %d 个损坏记录；原文件保留。" % len(warnings)

    def display_record(self, record):
        self.current_record = record
        self.last_path = None
        self.photos.pop("result", None)
        self.result_preview.configure(image="", text="本任务尚无可用图片")
        self.result_tree.delete(*self.result_tree.get_children())
        for i in record["items"]:
            info = Path(i["path"]).name if i.get("path") else i.get("error") or "尚未提交"
            self.result_tree.insert("", "end", iid=str(i["index"]), values=(STATUS_NAMES.get(i["status"], i["status"]), info))
        p = record["params"]
        lines = ["任务：" + record["id"], "%s · %s / %s · %d 张" % ("图片生图" if p.get("mode") == "image" else "文字生图", p["size"], p["ratio"], p["count"]), "提示词：" + p["prompt"], "实际发送：" + p["final_prompt"]]
        lines += ["第 %d 张：%s" % (i["index"], i["error"]) for i in record["items"] if i.get("error")]
        self._set_text(self.details, "\n".join(lines))
        valid = next((i for i in record["items"] if i["status"] == "success"), None)
        if valid:
            self.result_tree.selection_set(str(valid["index"]))
            self.select_result()
        self._buttons()

    def select_history(self, event=None):
        if self.busy:
            return
        selection = self.history.selection()
        if selection and selection[0] in self.records:
            self.display_record(self.records[selection[0]])

    def select_result(self, event=None):
        selected = self.result_tree.selection()
        if not selected or not self.current_record:
            return
        item = next((i for i in self.current_record["items"] if str(i["index"]) == selected[0]), None)
        self.last_path = item.get("path") if item else None
        if self.last_path and Path(self.last_path).is_file():
            self._resize_preview()
        else:
            self.result_preview.configure(image="", text="没有本地结果 / 文件已移动" if not item or not item.get("error") else item["error"], wraplength=420)
        self._buttons()

    def _buttons(self):
        has_file = bool(self.last_path and Path(self.last_path).is_file())
        self.open_btn.configure(state="normal" if has_file else "disabled")
        self.save_btn.configure(state="normal" if has_file else "disabled")
        self.reuse_btn.configure(state="normal" if self.current_record and not self.busy else "disabled")
        incomplete = self.current_record and any(i["status"] != "success" for i in self.current_record["items"])
        self.retry_btn.configure(state="normal" if incomplete and not self.busy else "disabled")

    def reuse_params(self):
        if self.busy or not self.current_record:
            return
        p = self.current_record["params"]
        self.text.delete("1.0", "end")
        self.text.insert("1.0", p["prompt"])
        sid = p.get("style")
        if sid in self.style_ids:
            self.style_cb.current(self.style_ids.index(sid))
        else:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", p["final_prompt"])
            self.style_cb.current(self.style_ids.index("none") if "none" in self.style_ids else 0)
        self.size_cb.set(p["size"])
        self.ratio_cb.set(p["ratio"])
        self.count_cb.set(str(p["count"]))
        self.timeout_cb.set(str(p["timeout"]))
        self.mode.set(p.get("mode", "text"))
        self.clear_reference()
        if p.get("image"):
            if Path(p["image"]).is_file():
                self.set_reference(p["image"])
            else:
                messagebox.showwarning(APP_TITLE, "原参考图已移动，请重新选择。")
        self._mode_changed()
        self.phase = "参数已填回，可编辑后生成；尚未提交请求。"

    def retry_job(self):
        if self.busy or not self.current_record:
            return
        if messagebox.askyesno(APP_TITLE, "只处理未完成项。已拿到结果地址的图片只重试下载；其他失败项将重新生成。超时请求可能已在云端执行，重试可能额外计费。确认继续？"):
            self.tabs.select(0)
            self._start({"retry_id": self.current_record["id"]})

    def _open(self, path):
        try:
            os.startfile(str(path))
        except OSError:
            messagebox.showerror(APP_TITLE, "系统无法打开该文件或目录。")

    def open_result(self):
        if self.last_path:
            self._open(self.last_path)

    def save_as(self):
        if not self.last_path or not Path(self.last_path).is_file():
            return
        source = Path(self.last_path)
        dest = filedialog.asksaveasfilename(title="另存结果（不覆盖已有文件）", initialfile=source.name,
                                           defaultextension=source.suffix, filetypes=[("原始格式", "*"+source.suffix)])
        if not dest:
            return
        target = Path(dest)
        if target.suffix.lower() != source.suffix.lower():
            messagebox.showwarning(APP_TITLE, "请保留原图片扩展名 %s；此处保存原图，不做格式转换。" % source.suffix)
            return
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            self.phase = "已另存到：" + str(target)
        except FileExistsError:
            messagebox.showwarning(APP_TITLE, "目标文件已存在，为避免覆盖，请换一个文件名。")
        except OSError:
            messagebox.showerror(APP_TITLE, "保存失败，请检查目标位置权限与磁盘空间。")

    def open_out_dir(self):
        try:
            Path(ai.DEFAULT_OUT_DIR).mkdir(parents=True, exist_ok=True)
            self._open(ai.DEFAULT_OUT_DIR)
        except OSError:
            messagebox.showerror(APP_TITLE, "无法访问输出目录。")

    def on_close(self):
        if self.busy:
            messagebox.showinfo(APP_TITLE, "当前请求仍在执行。可点击停止后续，待当前请求返回后关闭；不会声称取消云端任务。")
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    if Image is None:
        messagebox.showerror(APP_TITLE, "缺少 Pillow 图片库，请使用带 tkinter 和 Pillow 的 Python 启动。")
        root.destroy()
        return 1
    try:
        App(root)
    except ai.GenError as e:
        messagebox.showerror(APP_TITLE, str(e))
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
