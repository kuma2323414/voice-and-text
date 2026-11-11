# -*- coding: utf-8 -*-
r"""
RVC 常驻工作进程。

由文字转语音v1.0 用 RVC 自带的 runtime\python.exe 启动，模型只加载一次，
之后通过 stdin/stdout 收发 JSON 行，避免每句话都重新初始化（省 2~3 秒）。

协议（每行一个 JSON）：
  请求  {"id":1,"cmd":"ping"}
        {"id":2,"cmd":"load","name":"Sagiri"}
        {"id":3,"cmd":"convert","name":"Sagiri","in":"a.wav","out":"b.wav",
         "pitch":0,"f0":"pm","index_rate":0.75,"protect":0.33}
        {"cmd":"exit"}
  响应  {"ok":true,"id":1,...} / {"ok":false,"id":1,"err":"..."}

  每个回复都会带回请求里的 id。父进程靠它把「超时之后才姗姗来迟」的
  旧回复丢掉，不然那条回复会被当成下一条请求的结果。

注意：stdout 只输出 JSON，RVC 自己的日志全部重定向到 stderr。
"""

import os
import sys
import json
import time
import glob
import traceback

# ---------- 先把 stdout 挪走，避免 RVC 的 print 污染协议 ----------
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr
try:
    _REAL_STDOUT.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

RVC_DIR = os.environ.get("RVC_DIR", r"D:\rvc\RVC-GPU")
THREADS = os.environ.get("RVC_THREADS", "12")
DEVICE = os.environ.get("RVC_DEVICE", "cpu")

os.environ["OMP_NUM_THREADS"] = THREADS
os.environ["MKL_NUM_THREADS"] = THREADS
os.environ["OPENBLAS_NUM_THREADS"] = "1"

os.chdir(RVC_DIR)
sys.path.insert(0, RVC_DIR)


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# 当前请求的 id，回复时原样带回。父进程超时后迟到的回复就靠它认出来丢掉。
_REQ_ID = None


def reply(**kw):
    if _REQ_ID is not None:
        kw["id"] = _REQ_ID
    _REAL_STDOUT.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()


def _pid_alive(pid: int) -> bool:
    """Windows 下判断某个 PID 是否还活着。"""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _watch_parent():
    """父进程（文字转语音v1.0）被强杀 / 崩溃时自己也退出，避免残留进程白吃 CPU。

    正常情况下父进程关闭窗口会先发 {"cmd":"exit"}，走不到这里。
    这里兜的是「任务管理器结束进程」这种硬杀。
    """
    import threading

    def run():
        ppid = os.getppid()
        while True:
            time.sleep(4)
            try:
                if not _pid_alive(ppid):
                    log("[watchdog] 父进程已退出，跟着退出")
                    os._exit(0)
            except Exception:
                return

    threading.Thread(target=run, daemon=True).start()


def main():
    _watch_parent()

    import numpy as np
    import soundfile as sf
    import torch

    torch.set_num_threads(int(THREADS))

    device = DEVICE.strip()
    if device.lower() in ("", "auto"):
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    elif device.lower().startswith("cuda") and not torch.cuda.is_available():
        log("[rvc] CUDA 不可用，回退 CPU")
        device = "cpu"

    from config import Config
    from fairseq import checkpoint_utils
    from lib.infer_pack.models import (
        SynthesizerTrnMs256NSFsid,
        SynthesizerTrnMs256NSFsid_nono,
        SynthesizerTrnMs768NSFsid,
        SynthesizerTrnMs768NSFsid_nono,
    )
    from my_utils import load_audio
    from vc_infer_pipeline import VC

    _argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    config = Config()
    sys.argv = _argv

    config.device = device
    config.is_half = device.startswith("cuda")

    t0 = time.time()
    models, _, _ = checkpoint_utils.load_model_ensemble_and_task(
        ["hubert_base.pt"], suffix=""
    )
    hubert = models[0].to(config.device)
    hubert = hubert.half() if config.is_half else hubert.float()
    hubert.eval()
    log("[rvc] hubert 就绪 %.1fs device=%s threads=%s" % (time.time() - t0, device, THREADS))

    state = {"name": None, "net_g": None, "sr": None, "if_f0": 1,
             "version": "v1", "index": ""}

    def find_model(name):
        stem = os.path.splitext(os.path.basename(name))[0]
        cand = glob.glob(os.path.join("weights", "**", stem + ".pth"), recursive=True)
        if not cand:
            raise RuntimeError("找不到模型：%s" % stem)
        return cand[0]

    def find_index(model_path, stem):
        mdir = os.path.basename(os.path.dirname(model_path))
        idxs = glob.glob(os.path.join("logs", mdir, "*.index"))
        if not idxs:
            idxs = [p for p in glob.glob(os.path.join("logs", "**", "*.index"), recursive=True)
                    if stem.lower() in p.lower()]
        return idxs[0] if idxs else ""

    def load_model(name):
        stem = os.path.splitext(os.path.basename(name))[0]
        if state["name"] == stem and state["net_g"] is not None:
            return
        mp = find_model(stem)
        t = time.time()
        cpt = torch.load(mp, map_location="cpu", weights_only=False)
        tgt_sr = cpt["config"][-1]
        cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]
        if_f0 = cpt.get("f0", 1)
        version = cpt.get("version", "v1")

        if version == "v1":
            net_g = (SynthesizerTrnMs256NSFsid(*cpt["config"], is_half=config.is_half)
                     if if_f0 == 1 else SynthesizerTrnMs256NSFsid_nono(*cpt["config"]))
        else:
            net_g = (SynthesizerTrnMs768NSFsid(*cpt["config"], is_half=config.is_half)
                     if if_f0 == 1 else SynthesizerTrnMs768NSFsid_nono(*cpt["config"]))

        del net_g.enc_q
        net_g.load_state_dict(cpt["weight"], strict=False)
        net_g = net_g.float().to(config.device).eval()
        if config.is_half:
            net_g = net_g.half()

        state.update({
            "name": stem, "net_g": net_g, "sr": tgt_sr, "if_f0": if_f0,
            "version": version, "index": find_index(mp, stem),
        })
        log("[rvc] 模型 %s 就绪 %.1fs (%s, %dHz)" % (stem, time.time() - t, version, tgt_sr))

    def convert(inp, out, pitch, f0_method, index_rate, protect):
        if not os.path.exists(inp):
            raise RuntimeError("输入文件不存在：%s" % inp)
        vc = VC(state["sr"], config)
        audio = load_audio(inp, 16000)
        audio_opt = vc.pipeline(
            hubert, state["net_g"], 0, audio, inp, [0, 0, 0],
            pitch, f0_method, state["index"], index_rate, state["if_f0"],
            3, state["sr"], 0, 0.25, state["version"], protect, f0_file=None,
        )
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        sf.write(out, audio_opt, state["sr"])
        return len(audio_opt) / float(state["sr"])

    reply(ok=True, ready=True, device=device)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            reply(ok=False, err="bad json")
            continue

        cmd = req.get("cmd")
        global _REQ_ID
        _REQ_ID = req.get("id")
        try:
            if cmd == "exit":
                break
            elif cmd == "ping":
                reply(ok=True, pong=True, model=state["name"])
            elif cmd == "load":
                load_model(req.get("name", ""))
                reply(ok=True, model=state["name"], sr=state["sr"])
            elif cmd == "convert":
                name = req.get("name") or state["name"]
                if name:
                    load_model(name)
                t = time.time()
                dur = convert(
                    req["in"], req["out"],
                    int(req.get("pitch", 0)),
                    req.get("f0", "pm"),
                    float(req.get("index_rate", 0.75)),
                    float(req.get("protect", 0.33)),
                )
                reply(ok=True, out=req["out"], ms=int((time.time() - t) * 1000),
                      dur=round(dur, 2))
            else:
                reply(ok=False, err="unknown cmd: %s" % cmd)
        except Exception as e:
            log(traceback.format_exc())
            reply(ok=False, err=str(e))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(traceback.format_exc())
        reply(ok=False, err="startup failed: %s" % e)
        sys.exit(1)
