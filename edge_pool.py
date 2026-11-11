"""Edge TTS 常驻连接池。

为什么要这个东西：
    edge-tts 每合成一句话都会新建一条 WebSocket 连接到微软服务器。
    在国内网络下，这条链路「建立连接」极不稳定：实测成功时 0.7 秒，
    倒霉时要 20~40 秒才超时失败，而 edge-tts 内部还会重试 + 退避。
    结果是说一句「你好」要等十几秒，用户体感就是「又慢又卡」。

    但一旦连接建立成功，合成本身只要 0.25 秒（实测同一连接连续 3 次：
    0.24s / 0.25s / 0.26s）。也就是说 99% 的时间都浪费在反复握手。

本模块的做法：
    后台线程常驻一个 asyncio 事件循环，维护一条可复用的 WebSocket 连接。
    每次合成直接在这条连接上发 SSML，不再握手。
    连接断了 / 空闲太久被服务端踢了，会自动重连（并带并发竞速，抢最快的）。

对外只暴露一个同步方法：EdgePool.synth(text, voice, rate, pitch) -> bytes
"""

import asyncio
import ssl
import threading
import time
import uuid

import aiohttp
import certifi

from edge_tts.communicate import (
    connect_id,
    date_to_string,
    mkssml,
    split_text_by_byte_length,
    ssml_headers_plus_data,
)
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.data_classes import TTSConfig
from edge_tts.drm import DRM

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# 单段文本上限（字节）。超过就切块，逐块合成后把 mp3 字节拼起来。
_MAX_CHUNK = 4000

# 建立连接的单次超时。故意设短：与其等 30 秒，不如 6 秒失败立刻重来。
_CONNECT_TIMEOUT = 6.0

# 空闲多久主动断开重连（服务端约 2 分钟无数据会踢人）
_IDLE_RECYCLE = 100.0

# 合成单段的总超时
_TURN_TIMEOUT = 40.0


class EdgePoolError(RuntimeError):
    pass


class EdgePool:
    def __init__(self, log=None):
        self._log = log or (lambda msg, *a: None)
        self._loop = None
        self._thread = None
        self._started = threading.Event()

        self._session = None
        self._ws = None
        self._lock = None          # asyncio.Lock，串行化同一连接上的请求
        self._last_used = 0.0
        self._config_sent = False
        self._conn_count = 0
        self._keeper_task = None

    # ---------------------------------------------------------- 生命周期
    def start(self):
        """启动后台线程（幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="edge-pool", daemon=True
        )
        self._thread.start()
        self._started.wait(3.0)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._lock = asyncio.Lock()
        self._keeper_task = loop.create_task(self._keeper())
        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                self._keeper_task.cancel()
                loop.run_until_complete(self._close_all())
            except Exception:
                pass
            loop.close()

    async def _keeper(self):
        """保活：连接掉了就悄悄重连，保证用户随时说话都是热的。"""
        await asyncio.sleep(2)
        while True:
            try:
                await asyncio.sleep(25)
                need = self._ws is None or self._ws.closed
                if not need and time.time() - self._last_used > _IDLE_RECYCLE:
                    need = True
                if need:
                    async with self._lock:
                        if self._ws is not None and (
                            self._ws.closed
                            or time.time() - self._last_used > _IDLE_RECYCLE
                        ):
                            await self._drop_ws()
                        await self._ensure_conn()
            except asyncio.CancelledError:
                return
            except Exception:
                # 连不上就等下一轮，不要刷屏
                await self._drop_ws()
                await asyncio.sleep(5)

    def close(self):
        if not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(3)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # ---------------------------------------------------------- 对外接口
    def prewarm(self):
        """后台先把连接建好，这样第一次说话不用等握手。"""
        self.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(self._prewarm_locked(), self._loop)

            def _done(f):
                try:
                    f.result()
                except Exception as e:
                    self._log("Edge 预连接失败：%s" % type(e).__name__)

            fut.add_done_callback(_done)
        except Exception:
            pass

    async def _prewarm_locked(self):
        async with self._lock:
            await self._ensure_conn()

    def synth(self, text, voice, rate="+0%", pitch="+0Hz", timeout=_TURN_TIMEOUT):
        """同步合成，返回 mp3 字节。线程安全。"""
        self.start()
        fut = asyncio.run_coroutine_threadsafe(
            self._synth(text, voice, rate, pitch), self._loop
        )
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise EdgePoolError("合成超时，网络到微软服务器不稳定")

    # ---------------------------------------------------------- 内部实现
    async def _close_all(self):
        ws, self._ws = self._ws, None
        self._config_sent = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        sess, self._session = self._session, None
        if sess is not None:
            try:
                await sess.close()
            except Exception:
                pass

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                trust_env=True,
            )
        return self._session

    async def _open_once(self):
        sess = await self._get_session()
        url = (
            f"{WSS_URL}&ConnectionId={connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
        )
        return await asyncio.wait_for(
            sess.ws_connect(
                url,
                compress=15,
                headers=DRM.headers_with_muid(WSS_HEADERS),
                ssl=_SSL_CTX,
                heartbeat=20,
            ),
            _CONNECT_TIMEOUT,
        )

    async def _ensure_conn(self):
        """拿到一条可用连接。必要时新建。"""
        if self._ws is not None and not self._ws.closed:
            if time.time() - self._last_used < _IDLE_RECYCLE:
                return self._ws
            await self._drop_ws()

        # 先单发一次（成功率高、开销小）
        try:
            self._ws = await self._open_once()
        except Exception as first_err:
            # 失败就两路并发抢，谁先连上算谁的
            self._log("Edge 连接失败，改为双路竞速：%s" % type(first_err).__name__)
            tasks = [asyncio.ensure_future(self._open_once()) for _ in range(2)]
            winner = None
            last_err = first_err
            try:
                for _ in range(2):
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in done:
                        if winner is None and t.exception() is None:
                            winner = t.result()
                        elif t.exception() is not None:
                            last_err = t.exception()
                    if winner is not None:
                        break
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    if t.done() and t is not None:
                        try:
                            r = t.result()
                        except Exception:
                            continue
                        if r is not winner:
                            try:
                                await r.close()
                            except Exception:
                                pass
            if winner is None:
                raise EdgePoolError(
                    "连不上微软语音服务器（%s），检查网络或代理" % type(last_err).__name__
                )
            self._ws = winner

        self._config_sent = False
        self._conn_count += 1
        self._last_used = time.time()
        await self._send_config()
        return self._ws

    async def _drop_ws(self):
        ws, self._ws = self._ws, None
        self._config_sent = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _send_config(self):
        if self._config_sent:
            return
        payload = (
            f"X-Timestamp:{date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"'
            '},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        await self._ws.send_str(payload)
        self._config_sent = True

    async def _one_turn(self, chunk: bytes, cfg: TTSConfig) -> bytes:
        ws = self._ws
        await ws.send_str(
            ssml_headers_plus_data(
                uuid.uuid4().hex, date_to_string(), mkssml(cfg, chunk)
            )
        )
        buf = bytearray()
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                # 前 2 字节是本条消息的 header 长度，audio 数据在 header + 空行之后
                if len(msg.data) >= 2:
                    hlen = int.from_bytes(msg.data[:2], "big")
                    if 0 < hlen <= len(msg.data):
                        buf.extend(msg.data[hlen + 2:])
                    else:
                        buf.extend(msg.data[2:])
            elif msg.type == aiohttp.WSMsgType.TEXT:
                if "turn.end" in msg.data[:400]:
                    break
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                raise EdgePoolError("连接被服务端关闭")
        return bytes(buf)

    async def _synth(self, text, voice, rate, pitch):
        async with self._lock:
            cfg = TTSConfig(
                voice=voice,
                rate=rate or "+0%",
                volume="+0%",
                pitch=pitch or "+0Hz",
                boundary="SentenceBoundary",
            )

            chunks = list(split_text_by_byte_length(text, _MAX_CHUNK)) or [
                text.encode("utf-8")
            ]

            out = bytearray()
            for chunk in chunks:
                for attempt in (1, 2):
                    try:
                        await self._ensure_conn()
                        data = await asyncio.wait_for(
                            self._one_turn(chunk, cfg), _TURN_TIMEOUT
                        )
                        if not data:
                            raise EdgePoolError("服务端返回空音频")
                        out.extend(data)
                        self._last_used = time.time()
                        break
                    except Exception as e:
                        # 连接坏了就丢掉重来一次
                        await self._drop_ws()
                        if attempt == 2:
                            raise EdgePoolError(self._explain(e))
            return bytes(out)

    @staticmethod
    def _explain(e):
        name = type(e).__name__
        if isinstance(e, EdgePoolError):
            return str(e)
        if "Timeout" in name:
            return "连微软语音服务器超时，网络不稳（可试试开/关代理）"
        if "ClientConnector" in name or "Cannot connect" in str(e):
            return "连不上微软语音服务器，检查网络或代理"
        return "%s: %s" % (name, str(e)[:120])
