# -*- coding: utf-8 -*-
"""
文字转语音v1.0 —— 打字转语音，直接输出到虚拟麦克风（VB-CABLE 等）。

流程：输入文字 → TTS 合成 → 播放到指定的输出设备（CABLE Input）
      语音软件（QQ / 微信 / Discord）的麦克风选 CABLE Output 即可。

作者：WorkBuddy
"""

import os
import re
import sys
import json
import time
import queue
import ctypes
import asyncio
import hashlib
import threading
import subprocess
import urllib.request
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, messagebox

if getattr(sys, "frozen", False):
    # 打包成 exe 后，__file__ 在临时解压目录里，配置要跟着 exe 走
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
PHRASES_PATH = os.path.join(APP_DIR, "phrases.json")
CACHE_DIR = os.path.join(APP_DIR, "cache")

from edge_pool import EdgePool  # noqa: E402  (依赖 APP_DIR 的路径约定)

APP_NAME = "文字转语音v1.0"

# ---------------------------------------------------------------- 配色
BG         = "#171920"
CARD       = "#20232b"
FIELD      = "#1b1e25"
BORDER     = "#2c313c"
TEXT       = "#e8eaf0"
MUTED      = "#8f95a6"
FAINT      = "#5c6170"
ACCENT     = "#6c8cff"
ACCENT_H   = "#7f9bff"
ACCENT_SOFT = "#2a3147"
OK         = "#45c08a"
DANGER     = "#e2685f"
FONT       = "Microsoft YaHei UI"

# ---------------------------------------------------------------- 音色
# 2026-10 实测：微软在线只保留这 8 个 zh-CN 音色，
# 晓辰/晓涵/晓墨/晓秋/晓睿/晓萱/晓颜/晓悠/云野/云泽 已下线（调用会报 NoAudioReceived）
EDGE_VOICES = [
    ("晓晓 · 女声 · 温柔自然", "zh-CN-XiaoxiaoNeural"),
    ("晓伊 · 女声 · 少女", "zh-CN-XiaoyiNeural"),
    ("云希 · 男声 · 少年", "zh-CN-YunxiNeural"),
    ("云扬 · 男声 · 播音", "zh-CN-YunyangNeural"),
    ("云健 · 男声 · 浑厚", "zh-CN-YunjianNeural"),
    ("云夏 · 男声 · 少年", "zh-CN-YunxiaNeural"),
    ("小北 · 女声 · 东北话", "zh-CN-liaoning-XiaobeiNeural"),
    ("小妮 · 女声 · 陕西话", "zh-CN-shaanxi-XiaoniNeural"),
]
EDGE_VOICE_CODES = {c for _, c in EDGE_VOICES}

# 留空表示还没配置，由使用者在「设置 → 高级 · RVC 变声」里指定自己的 RVC 目录
RVC_DEFAULT_DIR = ""

DEFAULT_PHRASES = [
    "我马上到", "打野在右边", "别冲，等我", "稍等一下",
    "nice！", "有人吗", "撤了撤了", "谢谢",
]

DEFAULT_CONFIG = {
    "engine": "edge",
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": 0,
    "volume": 100,
    "device_name": "CABLE Input",
    "always_on_top": True,
    "hide_after_send": True,
    "hotkey_summon": "Ctrl+Alt+Space",
    "geometry": "470x320+120+80",
    "fish_key": "",
    "fish_ref": "",
    "rvc_dir": RVC_DEFAULT_DIR,
    "rvc_model": "",
    "pitch": 0,
    "rvc_pitch": 0,
    "rvc_device": "auto",
    "rvc_threads": 16,
    "rvc_f0": "pm",
}

ENGINES = [
    ("Edge 在线 · 免费", "edge"),
    ("RVC 角色音色 · 本地", "rvc"),
    ("Fish Audio API", "fish"),
]


# ================================================================ 工具函数
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            merged = dict(default)
            merged.update(data if isinstance(data, dict) else {})
            return merged
        return data
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def decode_audio(raw: bytes, sample_rate: int = 44100):
    """把 mp3/wav 字节流解码成指定采样率的立体声 float32。"""
    import numpy as np
    import miniaudio

    dec = miniaudio.decode(
        raw,
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=2,
        sample_rate=sample_rate,
    )
    arr = np.array(dec.samples, dtype=np.float32).reshape(-1, dec.nchannels)
    return arr, dec.sample_rate


VIRTUAL_KEYWORDS = ("cable", "voicemeeter", "virtual", "vb-audio", "blackhole")


def is_virtual_device(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in VIRTUAL_KEYWORDS)


# ================================================================ 播放器
class Player:
    """可暂停 / 继续 / 停止的播放器。

    原来一句 sd.play(blocking=True) 要播完才返回：播报三分钟的长文本既停不下来
    也暂停不了。

    这里沿用 sd.play 的底层做法（回调式 OutputStream，不能改成阻塞 write：
    WDM-KS 设备上 PortAudio 直接报 Blocking API not supported），回调里按
    当前位置填数据，暂停时填等长静音、位置不动，恢复后是样本级接上的，
    不会吞字也不会重复。
    """

    def __init__(self):
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._busy = False
        self.wrote = False          # 这次播放有没有真的出过声（用于失败重试判断）
        self._on_state = None

    def set_state_callback(self, cb):
        self._on_state = cb

    def _notify(self, state):
        if self._on_state is None:
            return
        try:
            self._on_state(state)
        except Exception:
            pass

    @property
    def busy(self):
        return self._busy

    def toggle_pause(self):
        """返回 'paused' / 'playing'，当前没在播就返回 None。"""
        if not self._busy:
            return None
        if self._pause.is_set():
            self._pause.clear()
            self._notify("playing")
            return "playing"
        self._pause.set()
        self._notify("paused")
        return "paused"

    def stop(self):
        if not self._busy:
            return False
        self._pause.clear()
        self._stop.set()
        return True

    def play(self, arr, samplerate, device=None):
        """阻塞播完（或被暂停 / 停止）。返回 'done' 或 'stopped'。"""
        import numpy as np
        import sounddevice as sd

        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        arr = np.ascontiguousarray(arr, dtype=np.float32)
        total = len(arr)
        cols = arr.shape[1]

        self._stop.clear()
        self._pause.clear()
        self.wrote = False
        self._busy = True

        pos = [0]
        done = threading.Event()

        def callback(outdata, frames, _time_info, _status):
            if self._stop.is_set():
                outdata.fill(0)
                done.set()
                raise sd.CallbackStop()
            if self._pause.is_set():
                outdata.fill(0)
                return
            start = pos[0]
            n = min(frames, total - start)
            if n > 0:
                outdata[:n] = arr[start:start + n]
                pos[0] = start + n
                self.wrote = True
            if n < frames:
                outdata[n:].fill(0)
            if pos[0] >= total:
                done.set()
                raise sd.CallbackStop()

        stream = None
        try:
            self._notify("playing")
            stream = sd.OutputStream(
                samplerate=samplerate, device=device,
                channels=cols, dtype="float32", callback=callback,
            )
            stream.start()
            while not done.wait(0.1):
                pass
        finally:
            self._busy = False
            stopped = self._stop.is_set()
            self._pause.clear()
            self._stop.clear()
            if stream is not None:
                for close_it in (stream.stop, stream.close):
                    try:
                        close_it()
                    except Exception:
                        pass
            self._notify("stopped" if stopped else "idle")
        return "stopped" if stopped else "done"


# ================================================================ 全局热键
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

HK_SUMMON = 1
HK_PHRASE_BASE = 100


def parse_hotkey(text: str):
    """把 'Ctrl+Alt+Space' 解析成 (modifiers, vk)。解析失败返回 (None, None)。"""
    mods = 0
    vk = None
    for part in str(text or "").replace("+", " ").split():
        p = part.strip().upper()
        if not p:
            continue
        if p in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif p == "ALT":
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        elif p in ("WIN", "META", "SUPER"):
            mods |= MOD_WIN
        elif p == "SPACE":
            vk = 0x20
        elif p in ("ENTER", "RETURN"):
            vk = 0x0D
        elif len(p) == 1:
            vk = ord(p)
        elif p.startswith("F") and p[1:].isdigit():
            n = int(p[1:])
            if 1 <= n <= 24:
                vk = 0x70 + n - 1
    if vk is None or mods == 0:
        return None, None
    return mods, vk


class HotkeyThread(threading.Thread):
    """用 Win32 RegisterHotKey 注册全局热键，独立消息循环。"""

    def __init__(self, bindings, callback):
        super().__init__(daemon=True)
        self.bindings = bindings          # [(id, mods, vk), ...]
        self.callback = callback
        self.tid = None
        self.registered = {}
        self.ready = threading.Event()

    def run(self):
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        for hid, mods, vk in self.bindings:
            if vk is None:
                self.registered[hid] = False
                continue
            ok = bool(user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk))
            self.registered[hid] = ok
        self.ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.callback(int(msg.wParam))
                except Exception:
                    pass

    def stop(self):
        if self.tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        # 等线程真的退出：线程结束时系统才会释放它注册的热键，
        # 不等的话马上重新注册会全部失败（改热键后「点了没反应」就是这个原因）
        try:
            self.join(1.5)
        except Exception:
            pass


# ================================================================ RVC 客户端
class RvcClient:
    """管理 RVC 常驻工作进程（模型只加载一次）。"""

    def __init__(self, cfg_getter):
        self.cfg_get = cfg_getter
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.logf = None
        self.device = ""
        self.seq = 0

    # ---------- 进程管理 ----------
    def _start(self):
        rvc_dir = self.cfg_get("rvc_dir") or RVC_DEFAULT_DIR
        if not rvc_dir:
            raise RuntimeError(
                "还没设置 RVC 目录，去「设置 → 高级 · RVC 变声」填你自己解压出来的 RVC-GPU 路径")
        py = ""
        for cand in (
            os.path.join(rvc_dir, "runtime-gpu", "Scripts", "python.exe"),
            os.path.join(rvc_dir, "runtime", "python.exe"),
        ):
            if os.path.exists(cand):
                py = cand
                break
        if not py:
            raise RuntimeError("找不到 RVC 运行环境，检查 RVC 目录：%s" % rvc_dir)
        worker = os.path.join(APP_DIR, "rvc", "rvc_worker.py")
        if not os.path.exists(worker):
            # 打包后资源在 _internal 里
            base = getattr(sys, "_MEIPASS", "")
            if base:
                worker = os.path.join(base, "rvc", "rvc_worker.py")
        if not os.path.exists(worker):
            raise RuntimeError("找不到 rvc_worker.py")

        env = os.environ.copy()
        env["RVC_DIR"] = rvc_dir
        env["RVC_DEVICE"] = self.cfg_get("rvc_device") or "auto"
        env["RVC_THREADS"] = str(self.cfg_get("rvc_threads") or 16)

        log_dir = os.path.join(APP_DIR, "rvc")
        os.makedirs(log_dir, exist_ok=True)
        self.logf = open(os.path.join(log_dir, "worker.log"), "w", encoding="utf-8")

        flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [py, worker],
            cwd=rvc_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.logf,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=flags,
        )
        # 每次起新进程都换一个队列：旧 reader 线程退出时丢的 EOF 标记
        # 留在旧队列里，不会污染新进程的第一条回复。
        self.q = queue.Queue()
        threading.Thread(
            target=self._reader, args=(self.q, self.proc.stdout), daemon=True
        ).start()

        try:
            r = self._recv(None, timeout=120)
        except Exception:
            self._kill()
            raise
        if not r.get("ok"):
            self._kill()
            raise RuntimeError(r.get("err", "RVC 启动失败"))
        self.device = str(r.get("device") or "")

    def _reader(self, q, stdout):
        try:
            for line in stdout:
                line = line.strip()
                if line:
                    q.put(line)
        except Exception:
            pass
        q.put(None)

    def _recv(self, iid, timeout=180):
        """读一条回复。给了 iid 就丢掉 id 对不上的（超时后才迟到的旧回复）。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self._kill()
                raise RuntimeError("RVC 处理超时（%ds），看 rvc/worker.log" % timeout)
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                self._kill()
                raise RuntimeError("RVC 处理超时（%ds），看 rvc/worker.log" % timeout)
            if line is None:
                raise RuntimeError("RVC 进程意外退出，看 rvc/worker.log")
            try:
                r = json.loads(line)
            except Exception:
                continue
            if iid is not None and r.get("id") != iid:
                continue
            return r

    def _request(self, payload, timeout=180):
        if self.proc is None or self.proc.poll() is not None:
            self.close()
            self._start()
        self.seq += 1
        iid = self.seq
        payload["id"] = iid
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            self._kill()
            raise RuntimeError("RVC 管道断开：%s" % e)
        return self._recv(iid, timeout)

    def _drain(self):
        while True:
            try:
                self.q.get_nowait()
            except Exception:
                return

    def _kill(self):
        """超时或管道坏了：直接杀进程。留着它迟到的回复只会错位到下一条请求上。"""
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                pass
        self._drain()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.write('{"cmd":"exit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self._drain()

    # ---------- 对外接口 ----------
    def ensure_ready(self):
        with self.lock:
            r = self._request({"cmd": "ping"}, timeout=120)
        if not r.get("ok"):
            raise RuntimeError(r.get("err", "RVC 启动失败"))
        if self.device:
            r["device"] = self.device
        return r

    def convert(self, in_path, out_path, model, pitch, f0="pm"):
        with self.lock:
            r = self._request({
                "cmd": "convert",
                "name": model,
                "in": in_path,
                "out": out_path,
                "pitch": int(pitch),
                "f0": f0,
            }, timeout=300)
        if not r.get("ok"):
            raise RuntimeError(r.get("err", "RVC 转换失败"))
        return r


# ================================================================ 自绘滑杆
class Slider(tk.Canvas):
    def __init__(self, parent, from_=0, to=100, value=0, command=None,
                 width=110, bg=CARD, step=None):
        super().__init__(parent, width=width, height=20, bg=bg,
                         bd=0, highlightthickness=0, cursor="hand2")
        self.from_, self.to_, self.w = from_, to, width
        self.value = value
        self.command = command
        self.step = step
        self.pad = 7
        self.enabled = True
        self.create_line(self.pad, 10, width - self.pad, 10, width=4,
                         fill="#3a3f4b", capstyle="round")
        self.fill_item = self.create_line(self.pad, 10, self.pad, 10, width=4,
                                          fill=ACCENT, capstyle="round")
        self.thumb = self.create_oval(0, 0, 0, 0, fill="#e9ebf1", outline="")
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<MouseWheel>", self._on_wheel)
        self._redraw()

    def _on_wheel(self, e):
        if not self.enabled:
            return
        step = self.step or 1
        d = step if e.delta > 0 else -step
        self.value = max(self.from_, min(self.to_, self.value + d))
        self._redraw()
        if self.command:
            self.command(self.value)

    def _ratio(self):
        span = self.to_ - self.from_
        return 0.0 if not span else (self.value - self.from_) / span

    def _redraw(self):
        x = self.pad + self._ratio() * (self.w - 2 * self.pad)
        self.coords(self.fill_item, self.pad, 10, x, 10)
        self.coords(self.thumb, x - 6, 4, x + 6, 16)

    def _snap(self, v):
        if self.step:
            v = round(v / self.step) * self.step
        return v

    def _apply_x(self, x):
        if not self.enabled:
            return
        r = (x - self.pad) / (self.w - 2 * self.pad)
        r = max(0.0, min(1.0, r))
        self.value = self._snap(self.from_ + r * (self.to_ - self.from_))
        self._redraw()
        if self.command:
            self.command(self.value)

    def _on_press(self, e):
        self._apply_x(e.x)

    def _on_drag(self, e):
        self._apply_x(e.x)

    def get(self):
        return self.value

    def set(self, v):
        self.value = self._snap(max(self.from_, min(self.to_, float(v))))
        self._redraw()

    def set_enabled(self, flag):
        self.enabled = bool(flag)
        self.itemconfigure(self.fill_item, fill=ACCENT if flag else "#4a4f5c")
        self.itemconfigure(self.thumb, fill="#e9ebf1" if flag else "#565b69")


# ================================================================ 通用控件
def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class PillButton(tk.Canvas):
    """圆角按钮：主按钮 / 次按钮 / 幽灵按钮。"""

    KINDS = {
        "primary": (ACCENT, ACCENT_H, "#ffffff"),
        "secondary": (CARD, "#2a2e38", TEXT),
        "ghost": (None, "#232730", MUTED),
    }

    def __init__(self, parent, text, command=None, kind="primary",
                 height=38, width=None, radius=10, font=None, bg=None):
        self._parent_bg = bg or parent["bg"]
        super().__init__(parent, height=height, bg=self._parent_bg,
                         bd=0, highlightthickness=0, cursor="hand2")
        if width:
            self.configure(width=width)
        else:
            self.configure(width=1)
        self._text = text
        self._command = command
        self._kind = kind
        self._radius = radius
        self._font = font or (FONT, 10, "bold")
        self._fg = self.KINDS[kind][2]
        self._hovering = False
        self._pressed = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            try:
                w = int(float(self.cget("width")))
            except Exception:
                w = 120
        if h <= 1:
            try:
                h = int(float(self.cget("height")))
            except Exception:
                h = 28
        if w <= 1 or h <= 1:
            return
        base, hover, _ = self.KINDS.get(self._kind, self.KINDS["primary"])
        fill = base or self._parent_bg
        if self._hovering or self._pressed:
            fill = hover
        _round_rect(self, 0, 0, w, h, self._radius, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=self._font)

    def _on_enter(self, _=None):
        self._hovering = True
        self._draw()

    def _on_leave(self, _=None):
        self._hovering = False
        self._pressed = False
        self._draw()

    def _on_press(self, _=None):
        self._pressed = True
        self._draw()

    def _on_release(self, e):
        was = self._pressed
        self._pressed = False
        self._draw()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if was and inside and self._command:
            self._command()

    def set_text(self, text):
        self._text = text
        self._draw()

    def set_text_color(self, color):
        self._fg = color
        self._draw()


class Toggle(tk.Frame):
    """自绘开关，比系统 Checkbutton 更贴合深色界面。"""

    def __init__(self, parent, text, variable, bg=CARD):
        super().__init__(parent, bg=bg, cursor="hand2")
        self.var = variable
        self._bg = bg
        self.cv = tk.Canvas(self, width=38, height=22, bg=bg,
                            bd=0, highlightthickness=0, cursor="hand2")
        self.cv.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=MUTED,
                            font=(FONT, 9), anchor="w", cursor="hand2")
        self.lbl.pack(side="left", padx=(10, 0))
        for w in (self, self.cv, self.lbl):
            w.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _toggle(self, _=None):
        self.var.set(not self.var.get())

    def _draw(self):
        self.cv.delete("all")
        on = bool(self.var.get())
        _round_rect(self.cv, 3, 3, 35, 19, 8,
                    fill=ACCENT if on else "#3a3f4b", outline="")
        x = 26 if on else 12
        color = "#ffffff" if on else "#8a90a0"
        self.cv.create_oval(x - 7, 4, x + 7, 18, fill=color, outline="")


# ================================================================ 热键录制框
_HK_KEYS = {"space": "Space", "return": "Enter"}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _HK_KEYS[_c] = _c.upper()
for _n in range(10):
    _HK_KEYS[str(_n)] = str(_n)
for _n in range(1, 25):
    _HK_KEYS["F%d" % _n] = "F%d" % _n

_MOD_KEYSYMS = {
    "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
    "Super_L", "Super_R", "Win_L", "Win_R", "Meta_L", "Meta_R",
    "Caps_Lock", "Num_Lock", "ISO_Level3_Shift",
}


def _mods_pressed(state=0):
    """当前按住的修饰键。

    优先问 Windows（GetAsyncKeyState）—— tkinter 的 event.state 在部分
    输入法/游戏环境下会漏报 Alt，只靠它会让用户「按了没反应」。
    """
    names = []
    try:
        u = ctypes.windll.user32
        if u.GetAsyncKeyState(0x11) & 0x8000:      # VK_CONTROL
            names.append("Ctrl")
        if u.GetAsyncKeyState(0x12) & 0x8000:      # VK_MENU
            names.append("Alt")
        if u.GetAsyncKeyState(0x10) & 0x8000:      # VK_SHIFT
            names.append("Shift")
        if (u.GetAsyncKeyState(0x5B) | u.GetAsyncKeyState(0x5C)) & 0x8000:
            names.append("Win")
    except Exception:
        names = []

    if names:
        return names

    # 兜底：用 tk 自己报告的 state
    st = int(state or 0)
    if st & 0x0004:
        names.append("Ctrl")
    if st & 0x0008:
        names.append("Alt")
    if st & 0x0001:
        names.append("Shift")
    if st & 0x0040:
        names.append("Win")
    return names


class HotkeyBox(tk.Frame):
    """点一下，再按下想要的组合键，自动记成 'Ctrl+Alt+Space'。"""

    def __init__(self, parent, value="", bg=BG):
        super().__init__(parent, bg=bg)
        self.value = value or ""
        self.capturing = False
        self._bg = bg

        self.entry = tk.Entry(
            self, bg=FIELD, fg=TEXT, readonlybackground=FIELD,
            insertbackground=TEXT, bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            font=(FONT, 10), justify="center",
        )
        self.entry.pack(fill="x", ipady=5)
        self._show(self.value or "点这里设置")

        self.entry.bind("<Button-1>", self._start)
        self.entry.bind("<KeyPress>", self._on_key)
        self.entry.bind("<FocusOut>", self._cancel)

    def _show(self, text, color=TEXT):
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.entry.configure(fg=color, state="readonly")

    def _start(self, _=None):
        if self.capturing:
            return
        self.capturing = True
        self._show("按下组合键…", ACCENT)
        self.entry.configure(state="normal")
        self.entry.focus_set()

    def _cancel(self, _=None):
        if not self.capturing:
            return
        self.capturing = False
        self._show(self.value or "点这里设置", TEXT if self.value else MUTED)

    def _on_key(self, e):
        if not self.capturing:
            return None
        if e.keysym in _MOD_KEYSYMS:
            return "break"
        if e.keysym == "Escape":
            self._cancel()
            return "break"

        key = _HK_KEYS.get(e.keysym) or _HK_KEYS.get(e.keysym.lower())
        if key is None:
            self._show("不支持这个键，换一个", DANGER)
            self.after(900, lambda: self._show("按下组合键…", ACCENT))
            return "break"

        st = int(e.state)
        mods = _mods_pressed(st)
        if not mods:
            self._show("至少要带 Ctrl 或 Alt", DANGER)
            self.after(900, lambda: self._show("按下组合键…", ACCENT))
            return "break"

        self.value = "+".join(mods + [key])
        self.capturing = False
        self._show(self.value, TEXT)
        return "break"

    def get(self):
        return self.value


# ================================================================ 主程序
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        self.phrases = load_json(PHRASES_PATH, list(DEFAULT_PHRASES))
        if not isinstance(self.phrases, list):
            self.phrases = list(DEFAULT_PHRASES)

        self.jobs = queue.Queue()
        self.device_map = []
        self._placeholder_on = False
        self._busy = 0
        self._working = False
        self._stop_token = 0
        self._cfg_warned = False
        self.rvc = RvcClient(lambda k: self.cfg.get(k))
        self.edge_pool = EdgePool()
        self.player = Player()
        self.player.set_state_callback(
            lambda st: self.root.after(0, self._on_player_state, st))

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._refresh_devices(silent=True)

        threading.Thread(target=self._worker, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.hk = None
        # 缓存目录可能有上千个文件，全部 stat 一遍交给后台线程，别卡住启动
        threading.Thread(target=self._sweep_cache, daemon=True).start()
        self.root.after(300, self._start_hotkeys)
        # 启动就把 Edge 的连接建好，第一次说话不用等握手
        self.root.after(600, self._prewarm_edge)

    def _sweep_cache(self):
        """清理临时文件，并把语音缓存控制在合理体积内。

        mp3（Edge）和 wav（RVC 转换结果）一起算。以前只清 mp3，RVC 的 wav
        只增不减，用久了 cache 目录会一直涨。
        """
        if not os.path.isdir(CACHE_DIR):
            return
        now = time.time()
        items = []
        for fn in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, fn)
            low = fn.lower()
            try:
                st = os.stat(p)
            except Exception:
                continue
            if fn.startswith("_") and low.endswith(".mp3"):
                # RVC 转换用的中间文件，进程挂了会残留
                if now - st.st_mtime > 3600:
                    self._remove_quietly(p)
                continue
            if low.endswith(".mp3") or low.endswith(".wav"):
                items.append((st.st_mtime, st.st_size, p))

        # 超过 3000 条或 300MB，就按最后使用时间从旧到新删到 1500 条 / 150MB
        total = sum(s for _, s, _ in items)
        if len(items) <= 3000 and total <= 300 * 1024 * 1024:
            return
        items.sort()
        remain = len(items)
        for _, size, p in items:
            if remain <= 1500 and total <= 150 * 1024 * 1024:
                break
            if self._remove_quietly(p):
                remain -= 1
                total -= size

    @staticmethod
    def _remove_quietly(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False

    def _prewarm_edge(self):
        """后台把到微软服务器的连接先建好（只建一次，之后一直复用）。"""
        def run():
            try:
                self.edge_pool.start()
                self.edge_pool.prewarm()
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ 窗口
    def _setup_window(self):
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        default = "470x320+120+80"
        geo = str(self.cfg.get("geometry", default))
        # 老版本存的是 560x730 那种大窗口，或者 420x250 的旧默认尺寸，统一校正
        if "-32000" in geo or "+-" in geo:
            geo = default
        m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geo)
        if not m:
            geo = default
        else:
            w, h, x, y = (int(v) for v in m.groups())
            if w > 560:
                geo = default
            elif w < 440 or h < 280:
                geo = f"{max(w, 470)}x320{x:+d}{y:+d}"
        try:
            self.root.geometry(geo)
        except Exception:
            self.root.geometry(default)
        self.root.minsize(420, 260)
        if self.cfg.get("always_on_top", True):
            self.root.attributes("-topmost", True)
        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Map>", self._on_window_map, add="+")
        self.root.after(600, self._dark_titlebar)

    def _on_window_map(self, event):
        if event.widget is self.root:
            self.root.after(60, self._dark_titlebar)
            self.root.after(900, self._dark_titlebar)

    def _dark_titlebar(self, win=None):
        """让系统标题栏也变深色，和界面统一。系统不支持就算了。"""
        win = win or self.root
        try:
            user32 = ctypes.windll.user32
            dwm = ctypes.windll.dwmapi
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            dwm.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD,
                ctypes.POINTER(ctypes.c_int), wintypes.DWORD,
            ]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long

            hwnd = user32.GetParent(wintypes.HWND(win.winfo_id()))
            if not hwnd:
                hwnd = wintypes.HWND(win.winfo_id())

            def set_attr(attr, value):
                v = ctypes.c_int(value)
                return dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)
                )

            set_attr(20, 1)             # Win10 1809+：沉浸式深色
            set_attr(35, 0x00231D1B)    # Win11：标题栏底色（BGR）
            set_attr(36, 0x00FFFFFF)    # Win11：标题文字白色
            try:
                dwm.DwmFlush()
            except Exception:
                pass
        except Exception:
            pass

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Dark.TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            selectbackground=FIELD,
            selectforeground=TEXT,
            padding=7,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", FIELD)],
            foreground=[("readonly", TEXT)],
            bordercolor=[("focus", ACCENT)],
            arrowcolor=[("active", ACCENT)],
        )
        try:
            style.layout("Dark.Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {
                    "sticky": "ns",
                    "children": [
                        ("Vertical.Scrollbar.thumb",
                         {"expand": "1", "sticky": "nswe"}),
                    ],
                }),
            ])
        except Exception:
            pass
        style.configure(
            "Dark.Vertical.TScrollbar",
            troughcolor=BG, background="#3a3f4b", bordercolor=BG,
            lightcolor="#3a3f4b", darkcolor="#3a3f4b",
            arrowcolor=BG, relief="flat", width=8,
        )
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", "#4a5060")])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    # ------------------------------------------------------------ 组件
    def _label(self, parent, text, size=10, color=MUTED, bold=False):
        return tk.Label(
            parent, text=text, bg=parent["bg"], fg=color,
            font=(FONT, size, "bold" if bold else "normal"),
            anchor="w",
        )

    def _combo(self, parent, **kw):
        return ttk.Combobox(
            parent, style="Dark.TCombobox", state="readonly",
            font=(FONT, 10), **kw
        )

    def _build_ui(self):
        root = self.root

        # ---------- 底部状态行（先 pack，占据最底部）
        bar = tk.Frame(root, bg=BG, height=20)
        bar.pack(fill="x", side="bottom", padx=16, pady=(0, 10))
        bar.pack_propagate(False)

        self.status_dot = tk.Canvas(bar, width=8, height=8, bg=BG,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_dot_item = self.status_dot.create_oval(
            1, 1, 7, 7, fill=OK, outline="")

        self.status = tk.Label(
            bar, text="就绪", bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

        # ---------- 按钮行
        btns = tk.Frame(root, bg=BG, height=40)
        btns.pack(fill="x", side="bottom", padx=16, pady=(10, 0))
        btns.pack_propagate(False)

        self.send_btn = PillButton(
            btns, "发送", self._send, kind="primary", height=40,
        )
        self.send_btn.pack(side="left", fill="x", expand=True)

        PillButton(
            btns, "试听", lambda: self._send(preview=True),
            kind="secondary", height=40, width=72,
        ).pack(side="right", padx=(10, 0))

        self.stop_btn = PillButton(
            btns, "停止", self._stop_play, kind="secondary", height=40, width=62,
        )
        self.stop_btn.pack(side="right", padx=(10, 0))

        self.pause_btn = PillButton(
            btns, "暂停", self._toggle_pause, kind="secondary",
            height=40, width=62,
        )
        self.pause_btn.pack(side="right", padx=(10, 0))
        self.pause_btn.set_text_color(FAINT)

        # ---------- 输入提示 + 字数
        meta = tk.Frame(root, bg=BG, height=18)
        meta.pack(fill="x", side="bottom", padx=17, pady=(8, 0))
        meta.pack_propagate(False)

        tk.Label(
            meta, text="Enter 发送 · Shift+Enter 换行", bg=BG, fg=FAINT,
            font=(FONT, 8), anchor="w",
        ).pack(side="left")

        self.count_lbl = tk.Label(
            meta, text="0 / 800", bg=BG, fg=FAINT, font=(FONT, 8),
        )
        self.count_lbl.pack(side="right")

        # ---------- 顶部标题栏
        head = tk.Frame(root, bg=BG, height=34)
        head.pack(fill="x", side="top", padx=16, pady=(12, 0))
        head.pack_propagate(False)

        tk.Label(
            head, text=APP_NAME, bg=BG, fg=TEXT,
            font=(FONT, 12, "bold"),
        ).pack(side="left")

        self.engine_badge = tk.Label(
            head, text="", bg=BG, fg="#7f93d9", font=(FONT, 8),
        )
        self.engine_badge.pack(side="left", padx=(9, 0), pady=(2, 0))

        self.top_btn = PillButton(
            head, "置顶", self._toggle_top, kind="ghost",
            height=26, width=52, font=(FONT, 8), radius=8,
        )
        self.top_btn.pack(side="right", padx=(0, 6))

        PillButton(
            head, "设置", self._open_settings, kind="ghost",
            height=26, width=52, font=(FONT, 8), radius=8,
        ).pack(side="right")

        # ---------- 输入框，占满剩下的空间
        wrap = tk.Frame(root, bg=BORDER, bd=0)
        inner = tk.Frame(wrap, bg=FIELD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.text = tk.Text(
            inner, bg=FIELD, fg=TEXT, insertbackground=TEXT,
            bd=0, highlightthickness=0, wrap="word",
            font=(FONT, 12), padx=12, pady=10, undo=True,
            width=1, height=1,
            selectbackground=ACCENT, selectforeground="#ffffff",
            spacing1=3, spacing3=3,
        )
        self.text.pack(fill="both", expand=True)
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<Control-Return>", lambda e: (self._send(), "break")[1])
        self.text.bind("<Control-Shift-Return>", lambda e: (self._send(preview=True), "break")[1])
        self.text.bind("<Escape>", lambda e: (self._collapse(), "break")[1])
        self.text.bind("<FocusIn>", self._clear_placeholder)
        self.text.bind("<FocusOut>", self._set_placeholder)
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<KeyRelease>", lambda e: self._update_count())
        self._set_placeholder()

        wrap.pack(fill="both", expand=True, padx=16, pady=(12, 0))

    def _on_text_modified(self, _=None):
        try:
            modified = self.text.edit_modified()
            if modified:
                self.text.edit_modified(False)
        except Exception:
            return
        if modified:
            self.root.after_idle(self._update_count)

    def _update_count(self):
        lbl = getattr(self, "count_lbl", None)
        if lbl is None:
            return
        n = 0 if self._placeholder_on else len(self.text.get("1.0", "end-1c"))
        lbl.configure(text=f"{n} / 800", fg=DANGER if n > 800 else FAINT)

    # ------------------------------------------------------------ 占位符
    def _set_placeholder(self, _=None):
        if not self.text.get("1.0", "end").strip():
            self._placeholder_on = True
            self.text.delete("1.0", "end")
            self.text.insert("1.0", "输入要说的话…")
            self.text.configure(fg="#5f6472")
        self._update_count()
        self._sync_engine_ui()

    def _clear_placeholder(self, _=None):
        if self._placeholder_on:
            self._placeholder_on = False
            self.text.delete("1.0", "end")
            self.text.configure(fg=TEXT)
        self._update_count()

    def _get_text(self):
        if self._placeholder_on:
            return ""
        return self.text.get("1.0", "end").strip()

    # ------------------------------------------------------------ 设备
    def _scan_devices(self):
        """枚举所有能出声的设备，返回 [(设备号, 名字, 驱动), ...]。"""
        import sounddevice as sd

        out = []
        try:
            devs = sd.query_devices()
            apis = sd.query_hostapis()
            for i, d in enumerate(devs):
                if d.get("max_output_channels", 0) <= 0:
                    continue
                out.append((i, d["name"], apis[d["hostapi"]]["name"]))
        except Exception as e:
            self._set_status(f"枚举音频设备失败：{e}", DANGER)
        return out

    def _refresh_devices(self, silent=False):
        self.device_map = self._scan_devices()
        if not silent:
            self._set_status(f"设备已刷新（{len(self.device_map)} 个输出）")
        return self.device_map

    def _device_label(self, item):
        _, name, api = item
        return f"[虚拟] {name}" if is_virtual_device(name) else f"{name}  ·  {api}"

    def _on_rate_move(self, v):
        self.rate_val.configure(text=f"{int(float(v)):+d}%")

    def _on_vol_move(self, v):
        self.vol_val.configure(text=f"{int(float(v))}%")

    def _on_pitch_move(self, v):
        p = int(round(float(v)))
        self.pitch_val.configure(text=f"{p:+d}")
        self.cfg["pitch"] = p

    def _set_pitch(self, v):
        self.pitch_scale.set(v)
        self._on_pitch_move(v)
        self._set_status(f"声调 {int(v):+d} 半音")

    def _on_device_change(self, _=None):
        combo = getattr(self, "dev_combo", None)
        try:
            if combo is not None and combo.winfo_exists():
                k = combo.current()
                if 0 <= k < len(self.device_map):
                    self.cfg["device_name"] = self.device_map[k][1]
        except Exception:
            pass
        self._save_cfg()

    def _find_device(self, keyword):
        keyword = (keyword or "").lower()
        if not keyword:
            return None
        best = None
        for k, (_, name, api) in enumerate(self.device_map):
            if keyword in name.lower():
                if "wasapi" in api.lower():
                    return k
                if best is None:
                    best = k
        return best

    def _current_device(self):
        """按配置里的设备名找当前输出设备号。"""
        if not self.device_map:
            self.device_map = self._scan_devices()
        k = self._find_device(self.cfg.get("device_name") or "")
        if k is None:
            k = self._find_device("CABLE")
        if k is not None:
            return self.device_map[k][0]
        return None

    # ------------------------------------------------------------ 引擎
    def _engine_key(self):
        key = self.cfg.get("engine", "edge")
        for _, k in ENGINES:
            if k == key:
                return k
        return "edge"

    def _on_engine_change(self, _=None):
        combo = getattr(self, "engine_combo", None)
        try:
            if combo is not None and combo.winfo_exists():
                k = combo.current()
                if 0 <= k < len(ENGINES):
                    self.cfg["engine"] = ENGINES[k][1]
        except Exception:
            pass
        self._refresh_voice_list()
        self._sync_engine_ui()
        self._update_engine_badge()
        self._save_cfg()
        if self._engine_key() == "rvc":
            self._preload_rvc()

    def _preload_rvc(self):
        def run():
            try:
                self._set_status("RVC 引擎加载中…")
                r = self.rvc.ensure_ready()
                dev = str(r.get("device") or "cpu")
                label = "GPU" if dev.startswith("cuda") else "CPU"
                self._set_status("RVC 就绪（%s），可以说话了" % label)
            except Exception as e:
                self._set_status("RVC 加载失败：" + str(e), DANGER)

        threading.Thread(target=run, daemon=True).start()

    def _sync_engine_ui(self):
        """设置窗口打开时，按引擎同步控件状态。窗口没开就什么也不做。"""
        combo = getattr(self, "voice_combo", None)
        try:
            if combo is None or not combo.winfo_exists():
                return
        except Exception:
            return

        key = self._engine_key()
        if key in ("edge", "rvc"):
            combo.configure(state="readonly")
            if getattr(self, "rate_scale", None):
                self.rate_scale.set_enabled(True)
            if key == "rvc":
                n = len(self._rvc_models())
                self.engine_hint.configure(text=f"RVC 模型 {n} 个，在「音色」里选")
            else:
                self.engine_hint.configure(text="微软在线音色，免费、自然")
        else:
            combo.configure(state="disabled")
            if getattr(self, "rate_scale", None):
                self.rate_scale.set_enabled(False)
            self.engine_hint.configure(text="用 Fish Audio 的角色音色，参数在下面「高级」里")

    # ------------------------------------------------------------ RVC 模型
    def _rvc_models(self):
        rvc_dir = self.cfg.get("rvc_dir") or RVC_DEFAULT_DIR
        if not rvc_dir:
            return []
        wdir = os.path.join(rvc_dir, "weights")
        names = []
        if os.path.isdir(wdir):
            for root, _, files in os.walk(wdir):
                for fn in files:
                    if fn.lower().endswith(".pth"):
                        names.append(os.path.splitext(fn)[0])
        return sorted(set(names))

    def _refresh_voice_list(self):
        """把音色 / 模型列表填进设置窗口的下拉框（窗口没开就跳过）。"""
        combo = getattr(self, "voice_combo", None)
        try:
            if combo is None or not combo.winfo_exists():
                return
        except Exception:
            return

        key = self._engine_key()
        if key == "rvc":
            models = self._rvc_models()
            combo["values"] = models
            cur = self.cfg.get("rvc_model", "")
            if cur in models:
                combo.current(models.index(cur))
            elif models:
                combo.current(0)
                self.cfg["rvc_model"] = models[0]
            else:
                combo.set("")
        else:
            combo["values"] = [v[0] for v in EDGE_VOICES]
            voice = self.cfg.get("voice", "zh-CN-XiaoxiaoNeural")
            for i, (_, v) in enumerate(EDGE_VOICES):
                if v == voice:
                    combo.current(i)
                    break
            else:
                combo.current(0)
                self.cfg["voice"] = EDGE_VOICES[0][1]

    def _synth_rvc(self, text):
        model = self.cfg.get("rvc_model", "")
        if not model:
            models = self._rvc_models()
            if not models:
                raise RuntimeError("RVC 目录下没找到模型（weights/**/*.pth）")
            model = models[0]
            self.cfg["rvc_model"] = model

        pitch = int(self.cfg.get("pitch", 0) or 0)
        key = hashlib.md5(
            ("%s|%s|%s" % (model, pitch, text)).encode("utf-8")
        ).hexdigest()[:16]

        cache_dir = os.path.join(APP_DIR, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        out_path = os.path.join(cache_dir, key + ".wav")

        if os.path.exists(out_path):
            try:
                os.utime(out_path, None)
            except Exception:
                pass
            with open(out_path, "rb") as f:
                return f.read()

        tmp_mp3 = os.path.join(cache_dir, "_" + key + ".mp3")
        with open(tmp_mp3, "wb") as f:
            f.write(self._synth_edge(text))
        try:
            self._set_status("RVC 转换中…（首次要加载模型）")
            self.rvc.convert(tmp_mp3, out_path, model, pitch,
                             self.cfg.get("rvc_f0", "pm"))
        finally:
            try:
                os.remove(tmp_mp3)
            except Exception:
                pass

        with open(out_path, "rb") as f:
            return f.read()

    def _warmup_phrases(self):
        phrases = list(self.phrases)
        if not phrases:
            self._set_status("常用语是空的，去「设置」里加几句")
            return

        def run():
            for i, p in enumerate(phrases, 1):
                self._set_status(f"预热中 {i}/{len(phrases)}：{p}")
                try:
                    self._synth(p)
                except Exception as e:
                    self._set_status("预热失败：" + str(e), DANGER)
                    return
            self._set_status("常用语已缓存，热键可秒播")

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ 合成
    def _edge_params(self):
        """当前 Edge 参数（音色 / 语速 / 音高），用于合成和缓存键。"""
        voice = self.cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        if voice not in EDGE_VOICE_CODES:
            voice = "zh-CN-XiaoxiaoNeural"
        rate = f"{int(self.cfg.get('rate', 0) or 0):+d}%"

        # 半音 → Edge 的音高偏移（以 180Hz 为基准换算）
        semis = int(self.cfg.get("pitch", 0) or 0)
        pitch = "+0Hz"
        if semis:
            hz = int(round(180 * (2 ** (semis / 12.0) - 1)))
            pitch = f"{hz:+d}Hz"
        return voice, rate, pitch

    def _synth_edge(self, text):
        voice, rate, pitch = self._edge_params()

        # ---- 1. 本地缓存：说过的话第二次直接读盘，0 秒，断网也能播
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        key = hashlib.md5(
            ("%s|%s|%s|%s" % (voice, rate, pitch, text)).encode("utf-8")
        ).hexdigest()[:20]
        path = os.path.join(CACHE_DIR, "edge_" + key + ".mp3")
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if data:
                    try:
                        # 刷新时间：天天用的句子不该按「最旧」被清掉
                        os.utime(path, None)
                    except Exception:
                        pass
                    return data
            except Exception:
                pass

        # ---- 2. 常驻连接池：不再每次重新握手（这一步是「卡」的元凶）
        data = self.edge_pool.synth(text, voice, rate, pitch)

        # ---- 3. 写回缓存
        if data:
            try:
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                pass
        return data

    def _synth_fish(self, text):
        key = self.cfg.get("fish_key", "").strip()
        ref = self.cfg.get("fish_ref", "").strip()
        if not key:
            raise RuntimeError("还没填 Fish Audio API Key，去「设置」里填")
        payload = {"text": text, "format": "mp3"}
        if ref:
            payload["reference_id"] = ref
        req = urllib.request.Request(
            "https://api.fish.audio/v1/tts",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "model": "speech-1.5",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def _synth(self, text):
        key = self._engine_key()
        if key == "rvc":
            return self._synth_rvc(text)
        if key == "fish":
            return self._synth_fish(text)
        return self._synth_edge(text)

    # ------------------------------------------------------------ 播放
    def _worker(self):
        import sounddevice as sd

        while True:
            job = self.jobs.get()
            if job is None:
                return
            text, preview = job
            token = self._stop_token
            self._working = True
            try:
                self._set_status("合成中…")
                raw = self._synth(text)
                if token != self._stop_token:
                    # 合成期间按了「停止」：这句也不播了
                    self._set_status("已停止")
                    continue
                if not raw:
                    raise RuntimeError("合成结果为空，检查网络或引擎配置")

                vol = float(self.cfg.get("volume", 100) or 100)
                gain = max(0.0, min(1.5, vol / 100.0))

                dev = self._current_device()
                if preview:
                    try:
                        dev = sd.default.device[1]
                    except Exception:
                        dev = None
                elif dev is None:
                    # 以前这里会退到系统默认设备：声音从音箱放出来，
                    # 你以为在对麦克风说话，其实全屋都听见了。
                    raise RuntimeError(
                        "找不到输出设备「%s」，去设置 → 输出设备 里重选一次"
                        % (self.cfg.get("device_name") or "CABLE")
                    )

                target_sr = 44100
                if dev is not None:
                    try:
                        target_sr = int(sd.query_devices(dev)["default_samplerate"]) or 44100
                    except Exception:
                        target_sr = 44100

                self._set_status("试听中…" if preview else "正在播报…")
                played = False
                result = "done"
                last_err = None
                for sr_try in (target_sr, 44100, 48000):
                    try:
                        arr, sr = decode_audio(raw, sr_try)
                        if gain != 1.0:
                            arr = arr * gain
                        result = self.player.play(arr, sr, dev)
                        played = True
                        break
                    except Exception as e:
                        last_err = e
                        if self.player.wrote:
                            # 已经出过声了，换采样率重来会重复播一遍
                            break
                if not played:
                    raise RuntimeError("这个设备播不出来：%s" % last_err)
                self._set_status("已停止" if result == "stopped" else "就绪")
            except Exception as e:
                msg = str(e)
                if "401" in msg or "403" in msg:
                    msg = "鉴权失败，检查 API Key"
                elif self._engine_key() == "edge":
                    # Edge 是逆向接口，微软随时可能调整或关停。出错时明确指路，
                    # 否则用户只会以为是自己的网络问题。
                    msg += "（Edge 音色可能已失效，见 docs/FREE-TTS-CHANNELS.md）"
                self._set_status("出错：" + msg, DANGER)
            finally:
                self._working = False
                self.jobs.task_done()

    def _send(self, preview=False):
        text = self._get_text()
        if not text:
            self._set_status("先输入点文字", DANGER)
            return
        note = ""
        if len(text) > 800:
            text = text[:800]
            note = "（超过 800 字，只播前 800 字）"
        self.jobs.put((text, preview))
        self._set_status("已加入队列…" + note, DANGER if note else None)
        if not preview and self.cfg.get("hide_after_send", True):
            self.root.after(150, self._collapse)

    def _on_return(self, event):
        if event.state & 0x0001:      # Shift+Enter 换行
            return None
        self._send()
        return "break"

    def _collapse(self):
        try:
            self.root.iconify()
        except Exception:
            pass

    # ------------------------------------------------------------ 播放控制
    def _on_player_state(self, state):
        btn = getattr(self, "pause_btn", None)
        if btn is None:
            return
        if state == "paused":
            btn.set_text("继续")
            btn.set_text_color(ACCENT)
        elif state == "playing":
            btn.set_text("暂停")
            btn.set_text_color(TEXT)
        else:
            btn.set_text("暂停")
            btn.set_text_color(FAINT)

    def _toggle_pause(self):
        state = self.player.toggle_pause()
        if state is None:
            self._set_status("现在没有在播报", DANGER)
        elif state == "paused":
            self._set_status("已暂停，再点一次继续")
        else:
            self._set_status("继续播报…")

    def _clear_jobs(self):
        """丢掉还没开始处理的播报任务，返回丢掉的条数。

        按热键连点几下会排一串，以前「停止」只管当前这句，后面排队的照旧
        一句句念出来。停止就该是「从现在起别出声了」。
        """
        dropped = 0
        sentinel = False
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            if job is None:
                sentinel = True        # 关闭时的哨兵，放回去让 worker 正常退出
                continue
            dropped += 1
            self.jobs.task_done()
        if sentinel:
            self.jobs.put(None)
        return dropped

    def _stop_play(self):
        self._stop_token += 1          # 正在合成的那句也别播了
        was_playing = self.player.stop()
        was_working = self._working
        dropped = self._clear_jobs()
        if not (was_playing or was_working or dropped):
            self._set_status("现在没有在播报", DANGER)
            return
        msg = "已停止"
        if dropped:
            msg += "，队列里 %d 条也清掉了" % dropped
        self._set_status(msg)

    # ------------------------------------------------------------ 热键
    def _start_hotkeys(self):
        # 先收掉旧的：热键是线程级资源，旧线程不退出新线程注册会全部失败
        if self.hk:
            try:
                self.hk.stop()
            except Exception:
                pass
            self.hk = None

        summon = self.cfg.get("hotkey_summon", "Ctrl+Alt+Space")
        mods, vk = parse_hotkey(summon)
        bindings = [(HK_SUMMON, mods, vk)]
        for i in range(min(8, len(self.phrases))):
            bindings.append((HK_PHRASE_BASE + i, MOD_CONTROL | MOD_ALT, 0x31 + i))

        self.hk = HotkeyThread(bindings, self._on_hotkey)
        self.hk.start()
        self.hk.ready.wait(1.5)

        if vk is None:
            self._set_status(f"热键「{summon}」看不懂，去设置里重录一次", DANGER)
        elif not self.hk.registered.get(HK_SUMMON, False):
            self._set_status(f"热键 {summon} 注册失败，可能被别的软件占用", DANGER)
        else:
            self._set_status(f"就绪 · {summon}")

    def _restart_hotkeys(self):
        if self.hk:
            self.hk.stop()
            self.hk = None
        self.root.after(200, self._start_hotkeys)

    def _on_hotkey(self, hid):
        if hid == HK_SUMMON:
            self.root.after(0, self._summon)
            return
        idx = hid - HK_PHRASE_BASE
        if 0 <= idx < 8 and idx < len(self.phrases):
            self.jobs.put((self.phrases[idx], False))
            self._set_status(f"热键播报：{self.phrases[idx]}")

    def _summon(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.text.focus_set()
            if not self._placeholder_on:
                self.text.tag_add("sel", "1.0", "end-1c")
                self.text.mark_set("insert", "end-1c")
            self.text.see("end")
        except Exception:
            pass

    def _set_status(self, msg, color=None):
        def apply():
            self.status.configure(text=msg, fg=color or MUTED)
            dot = getattr(self, "status_dot", None)
            if dot is not None:
                if color == DANGER:
                    fill = DANGER
                elif "…" in msg:
                    fill = ACCENT
                else:
                    fill = OK
                dot.itemconfigure(self.status_dot_item, fill=fill)
        try:
            self.root.after(0, apply)
        except Exception:
            pass

    # ------------------------------------------------------------ 常用语
    def _play_phrase(self, text):
        self.jobs.put((text, False))
        self._set_status(f"播报：{text}")

    def _save_phrases(self, lines):
        """写入常用语并重挂热键（前 8 条对应 Ctrl+Alt+1~8）。

        返回需要提醒用户的文字，正常保存返回空串。
        """
        clean = [ln.strip() for ln in lines if ln.strip()]
        dropped = max(0, len(clean) - 8)
        self.phrases = clean[:8]
        try:
            save_json(PHRASES_PATH, self.phrases)
        except Exception as e:
            return "常用语保存失败：%s" % e
        self._restart_hotkeys()
        if dropped:
            return "常用语只保留了前 8 条，多出的 %d 条没保存" % dropped
        return ""

    # ------------------------------------------------------------ 设置
    def _open_settings(self):
        old = getattr(self, "settings_win", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.deiconify()
                    old.lift()
                    old.focus_force()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("设置")
        win.configure(bg=BG)
        win.minsize(520, 560)
        win.transient(self.root)
        win.attributes("-topmost", True)
        win.bind(
            "<Map>",
            lambda e: win.after(60, lambda: self._dark_titlebar(win))
            if e.widget is win else None,
            add="+",
        )
        win.after(250, lambda: self._dark_titlebar(win))
        win.after(900, lambda: self._dark_titlebar(win))

        w, h = 580, 720
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = min(w, max(520, sw - 40))
        h = min(h, max(560, sh - 80))
        if self.root.state() == "normal":
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        else:
            x = (sw - w) // 2
            y = (sh - h) // 2
        x = max(10, min(sw - w - 10, x))
        y = max(10, min(sh - h - 40, y))
        win.geometry(f"{w}x{h}+{x}+{y}")

        bottom = tk.Frame(win, bg=BG)
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x")

        area = tk.Frame(win, bg=BG)
        area.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(area, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(area, orient="vertical", command=canvas.yview,
                           style="Dark.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win_id, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def on_wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")

        def on_destroy(e):
            if e.widget is win:
                canvas.unbind_all("<MouseWheel>")

        canvas.bind_all("<MouseWheel>", on_wheel)
        win.bind("<Destroy>", on_destroy)

        first_card = [True]

        def card(title, subtitle=None, collapsible=False):
            outer = tk.Frame(inner, bg=CARD, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=BORDER)
            outer.pack(fill="x", padx=18, pady=(14 if first_card[0] else 0, 10))
            first_card[0] = False

            head = tk.Frame(outer, bg=CARD)
            head.pack(fill="x", padx=14, pady=(11, 0))
            tk.Frame(head, bg=ACCENT, width=3, height=12).pack(
                side="left", padx=(0, 8))
            tk.Label(head, text=title, bg=CARD, fg="#b8becc",
                     font=(FONT, 9, "bold")).pack(side="left")

            body = tk.Frame(outer, bg=CARD)

            if collapsible:
                arrow = tk.Label(head, text="展开", bg=CARD, fg=FAINT,
                                 font=(FONT, 8), cursor="hand2")
                arrow.pack(side="right")

                def toggle(_=None):
                    if body.winfo_ismapped():
                        body.pack_forget()
                        arrow.configure(text="展开")
                    else:
                        body.pack(fill="x", padx=14, pady=(8, 12))
                        arrow.configure(text="收起")
                    inner.update_idletasks()
                    canvas.configure(scrollregion=canvas.bbox("all"))

                for widget in (head, arrow):
                    widget.bind("<Button-1>", toggle)

                if subtitle:
                    tk.Label(outer, text=subtitle, bg=CARD, fg=FAINT,
                             font=(FONT, 8), anchor="w", justify="left",
                             wraplength=480).pack(fill="x", padx=17,
                                                  pady=(4, 10))
            else:
                body.pack(fill="x", padx=14, pady=(8, 12))
                if subtitle:
                    tk.Label(body, text=subtitle, bg=CARD, fg=FAINT,
                             font=(FONT, 8), anchor="w", justify="left",
                             wraplength=480).pack(fill="x", pady=(0, 8))
            return body

        def row(parent, pady=(0, 8)):
            f = tk.Frame(parent, bg=CARD)
            f.pack(fill="x", pady=pady)
            return f

        def tip(parent, text):
            tk.Label(
                parent, text=text, bg=CARD, fg=FAINT, font=(FONT, 8),
                anchor="w", justify="left", wraplength=480,
            ).pack(fill="x", pady=(4, 0))

        def entry_row(parent, label, key, hint=""):
            f = row(parent)
            tk.Label(f, text=label, bg=CARD, fg=MUTED, font=(FONT, 9),
                     width=13, anchor="w").pack(side="left")
            e = tk.Entry(
                f, bg=FIELD, fg=TEXT, insertbackground=TEXT, bd=0,
                highlightthickness=1, highlightbackground=BORDER,
                highlightcolor=ACCENT, font=(FONT, 9),
            )
            e.pack(side="left", fill="x", expand=True, ipady=6)
            e.insert(0, str(self.cfg.get(key, "")))
            if hint:
                tk.Label(f, text=hint, bg=CARD, fg=FAINT,
                         font=(FONT, 8)).pack(side="left", padx=(8, 0))
            return e

        # ================= 发声
        body = card("发声")

        f = row(body)
        tk.Label(f, text="引擎", bg=CARD, fg=MUTED, font=(FONT, 9),
                 width=13, anchor="w").pack(side="left")
        self.engine_combo = self._combo(f, width=22, values=[e[0] for e in ENGINES])
        self.engine_combo.pack(side="left", fill="x", expand=True)
        for i, (_, k) in enumerate(ENGINES):
            if k == self.cfg.get("engine", "edge"):
                self.engine_combo.current(i)
                break
        else:
            self.engine_combo.current(0)

        f = row(body)
        tk.Label(f, text="音色 / 模型", bg=CARD, fg=MUTED, font=(FONT, 9),
                 width=13, anchor="w").pack(side="left")
        self.voice_combo = self._combo(f, width=22, values=[])
        self.voice_combo.pack(side="left", fill="x", expand=True)

        self.engine_hint = tk.Label(
            body, text="", bg=CARD, fg=FAINT, font=(FONT, 8), anchor="w",
        )
        self.engine_hint.pack(fill="x", pady=(0, 8))

        def slider_row(parent, label, from_, to, value, fmt, key, step=None):
            f = row(parent)
            tk.Label(f, text=label, bg=CARD, fg=MUTED, font=(FONT, 9),
                     width=13, anchor="w").pack(side="left")
            s = Slider(f, from_=from_, to=to, value=value,
                       command=lambda v: on_move(v), width=200, step=step,
                       bg=CARD)
            s.pack(side="left")
            val = tk.Label(f, text=fmt(value), bg=CARD, fg=TEXT,
                           font=(FONT, 9), width=6, anchor="w")
            val.pack(side="left", padx=(10, 0))

            def on_move(v):
                self.cfg[key] = int(round(float(v)))
                val.configure(text=fmt(float(v)))

            return s, val

        self.rate_scale, self.rate_val = slider_row(
            body, "语速", -50, 80, self.cfg.get("rate", 0),
            lambda v: f"{int(v):+d}%", "rate")
        self.vol_scale, self.vol_val = slider_row(
            body, "音量", 0, 150, self.cfg.get("volume", 100),
            lambda v: f"{int(v)}%", "volume")

        # 声调单独一行，带预设
        f = row(body, pady=(0, 0))
        tk.Label(f, text="声调", bg=CARD, fg=MUTED, font=(FONT, 9),
                 width=13, anchor="w").pack(side="left")
        self.pitch_scale = Slider(
            f, from_=-24, to=24, value=self.cfg.get("pitch", 0),
            command=self._on_pitch_move, width=160, step=1, bg=CARD,
        )
        self.pitch_scale.pack(side="left")
        self.pitch_val = tk.Label(
            f, text=f"{int(self.cfg.get('pitch', 0)):+d}", bg=CARD, fg=TEXT,
            font=(FONT, 9), width=4, anchor="w",
        )
        self.pitch_val.pack(side="left", padx=(10, 4))
        tk.Label(f, text="半音", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        for txt, pv in (("-12", -12), ("0", 0), ("+12", 12)):
            PillButton(
                f, txt, lambda x=pv: self._set_pitch(x), kind="secondary",
                height=24, width=42, radius=7, font=(FONT, 8), bg=CARD,
            ).pack(side="left", padx=(4, 0))

        # ================= 输出
        body = card("输出设备")

        self.device_map = self._refresh_devices(silent=True)

        f = row(body)
        tk.Label(f, text="播放到", bg=CARD, fg=MUTED, font=(FONT, 9),
                 width=13, anchor="w").pack(side="left")
        self.dev_combo = self._combo(
            f, width=30, values=[self._device_label(d) for d in self.device_map]
        )
        self.dev_combo.pack(side="left", fill="x", expand=True)

        idx = self._find_device(self.cfg.get("device_name", ""))
        if idx is None:
            idx = self._find_device("CABLE")
        if idx is None and self.device_map:
            idx = 0
        if idx is not None:
            self.dev_combo.current(idx)

        def refresh():
            self.device_map = self._refresh_devices(silent=True)
            self.dev_combo["values"] = [self._device_label(d) for d in self.device_map]
            k = self._find_device(self.cfg.get("device_name", ""))
            if k is not None:
                self.dev_combo.current(k)

        PillButton(
            f, "刷新", refresh, kind="ghost", height=30, width=54,
            radius=8, font=(FONT, 8), bg=CARD,
        ).pack(side="left", padx=(8, 0))

        tip(body, "播放端选 [虚拟] CABLE Input / CABLE-A；语音软件里的麦克风选 "
                  "CABLE Output / CABLE-A Output。选普通扬声器的话只有你自己听得到。")

        # ================= 快捷键
        body = card("快捷键")

        f = row(body)
        tk.Label(f, text="呼出窗口", bg=CARD, fg=MUTED, font=(FONT, 9),
                 width=13, anchor="w").pack(side="left")
        self.hk_box = HotkeyBox(
            f, self.cfg.get("hotkey_summon", "Ctrl+Alt+Space"), bg=CARD)
        self.hk_box.pack(side="left", fill="x", expand=True)

        tip(body, "点一下上面的框，直接按下想要的组合键（必须带 Ctrl 或 Alt）。"
                  "常用语固定用 Ctrl+Alt+1~8，条数跟着下面写了几句走。")

        # ================= 常用语
        body = card("常用语")

        f = tk.Frame(body, bg=CARD)
        f.pack(fill="x", pady=(0, 6))
        tk.Label(f, text="第 1~8 条对应 Ctrl+Alt+1~8", bg=CARD, fg=FAINT,
                 font=(FONT, 8)).pack(side="left")
        PillButton(
            f, "预热", self._warmup_phrases, kind="ghost", height=26,
            width=54, radius=8, font=(FONT, 8), bg=CARD,
        ).pack(side="right")

        ptext = tk.Text(
            body, bg=FIELD, fg=TEXT, insertbackground=TEXT, bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, font=(FONT, 10), height=6,
            padx=8, pady=6,
        )
        ptext.pack(fill="x")
        ptext.insert("1.0", "\n".join(self.phrases))
        tip(body, "一行一句，点「保存并关闭」后立即生效；超过 8 条只保留前 8 条。")

        # ================= 行为
        body = card("行为")

        self.top_var = tk.BooleanVar(value=self.cfg.get("always_on_top", True))
        Toggle(body, "窗口置顶（游戏里不会被盖住）", self.top_var,
               bg=CARD).pack(fill="x", pady=(2, 10))

        self.hide_var = tk.BooleanVar(value=self.cfg.get("hide_after_send", True))
        Toggle(body, "发送后自动收起窗口", self.hide_var,
               bg=CARD).pack(fill="x", pady=(0, 2))

        # ================= 高级
        body = card(
            "高级 · RVC 变声",
            subtitle="只有使用 RVC 角色音色时才需要改这里",
            collapsible=True,
        )

        e_rvc_dir = entry_row(body, "RVC 目录", "rvc_dir", "含 runtime 和 weights")
        e_rvc_dev = entry_row(body, "推理设备", "rvc_device", "auto / cpu / cuda:0")
        e_rvc_thr = entry_row(body, "线程数", "rvc_threads", "CPU 建议 8~16")

        body = card(
            "高级 · Fish Audio",
            subtitle="填写 API Key 和音色后才会生效",
            collapsible=True,
        )

        e_fs_key = entry_row(body, "Fish Key", "fish_key", "")
        e_fs_ref = entry_row(body, "Fish 音色", "fish_ref", "")

        tk.Frame(inner, bg=BG, height=8).pack()

        # ================= 保存
        def save():
            k = self.engine_combo.current()
            if 0 <= k < len(ENGINES):
                self.cfg["engine"] = ENGINES[k][1]

            k = self.voice_combo.current()
            if self._engine_key() == "rvc":
                models = self._rvc_models()
                if 0 <= k < len(models):
                    self.cfg["rvc_model"] = models[k]
            elif 0 <= k < len(EDGE_VOICES):
                self.cfg["voice"] = EDGE_VOICES[k][1]

            k = self.dev_combo.current()
            if 0 <= k < len(self.device_map):
                self.cfg["device_name"] = self.device_map[k][1]

            hk = self.hk_box.get().strip()
            if hk and parse_hotkey(hk)[1] is not None:
                self.cfg["hotkey_summon"] = hk

            self.cfg["always_on_top"] = self.top_var.get()
            self.cfg["hide_after_send"] = self.hide_var.get()

            old = (self.cfg.get("rvc_dir"), self.cfg.get("rvc_device"),
                   self.cfg.get("rvc_threads"))
            for key, ent in (
                ("rvc_dir", e_rvc_dir), ("rvc_device", e_rvc_dev),
                ("rvc_threads", e_rvc_thr),
                ("fish_key", e_fs_key), ("fish_ref", e_fs_ref),
            ):
                v = ent.get().strip()
                if key == "rvc_threads":
                    try:
                        v = int(float(v))
                    except Exception:
                        v = 16
                self.cfg[key] = v

            self._save_cfg()
            self._update_engine_badge()
            self.root.attributes("-topmost", self.cfg["always_on_top"])

            if old != (self.cfg.get("rvc_dir"), self.cfg.get("rvc_device"),
                       self.cfg.get("rvc_threads")):
                self.rvc.close()

            note = self._save_phrases(ptext.get("1.0", "end").splitlines())
            win.destroy()
            self._restart_hotkeys()
            self._set_status(note or "设置已保存", DANGER if note else None)

        PillButton(
            bottom, "保存并关闭", save, kind="primary", height=40,
        ).pack(fill="x", padx=18, pady=12)

        # 控件建好后再绑事件，避免初始化时就触发
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_change)
        self.dev_combo.bind("<<ComboboxSelected>>", self._on_device_change)
        self._refresh_voice_list()
        self._sync_engine_ui()
        self._update_engine_badge()

    # ------------------------------------------------------------ 杂项
    def _toggle_top(self):
        v = not bool(self.cfg.get("always_on_top", True))
        self.cfg["always_on_top"] = v
        self.root.attributes("-topmost", v)
        self._update_top_btn()
        self._save_cfg()

    def _update_top_btn(self):
        btn = getattr(self, "top_btn", None)
        if btn is None:
            return
        on = bool(self.cfg.get("always_on_top", True))
        btn.set_text_color(ACCENT if on else MUTED)

    def _update_engine_badge(self):
        badge = getattr(self, "engine_badge", None)
        if badge is None:
            return
        key = self._engine_key()
        names = {"edge": "Edge", "rvc": "RVC", "fish": "Fish Audio"}
        detail = ""
        if key == "rvc":
            detail = self.cfg.get("rvc_model", "") or "未选模型"
        elif key == "edge":
            code = self.cfg.get("voice", "")
            for label, c in EDGE_VOICES:
                if c == code:
                    detail = label.split(" · ")[0]
                    break
        else:
            detail = "API"
        text = names.get(key, key)
        badge.configure(text=f"{text} · {detail}" if detail else text)

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        try:
            # 最小化时 geometry 会变成 -32000，不能存，否则下次启动跑到屏幕外
            if self.root.state() != "normal":
                return
            geo = self.root.geometry()
        except Exception:
            return
        if "-32000" in geo or "+-" in geo:
            return
        self.cfg["geometry"] = geo

    def _save_cfg(self):
        try:
            save_json(CONFIG_PATH, self.cfg)
        except Exception as e:
            # 以前这里是静默 pass：程序放在没有写权限的目录时，设置看着保存了，
            # 重启全丢，用户完全不知道为什么。现在明确报一次。
            if self._cfg_warned:
                return
            self._cfg_warned = True
            self._set_status("配置写不进去：%s" % e, DANGER)
            try:
                messagebox.showwarning(
                    "配置没保存",
                    "写不进这个文件：\n%s\n\n%s\n\n"
                    "把整个程序文件夹放到有写权限的位置（比如 D 盘）再试。"
                    % (CONFIG_PATH, e),
                )
            except Exception:
                pass

    def _restore_selection(self):
        # 旧版本把变调存在 rvc_pitch，迁移一次
        pitch = int(self.cfg.get("pitch", 0) or 0)
        if not pitch:
            pitch = int(self.cfg.get("rvc_pitch", 0) or 0)
        self.cfg["pitch"] = pitch
        self._update_top_btn()
        self._update_engine_badge()
        if self._engine_key() == "rvc":
            self._preload_rvc()

    def _on_close(self):
        self._save_cfg()
        try:
            self.player.stop()
        except Exception:
            pass
        if self.hk:
            self.hk.stop()
        try:
            self.rvc.close()
        except Exception:
            pass
        try:
            self.edge_pool.close()
        except Exception:
            pass
        self.jobs.put(None)
        self.root.destroy()


def main():
    root = tk.Tk()
    app = App(root)
    app._restore_selection()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("启动失败", str(exc))
        except Exception:
            pass
        sys.exit(1)
