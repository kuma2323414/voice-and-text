# Edge 失效了怎么办 · 免费文字转语音渠道名单

## 先说清楚：Edge 为什么会失效

本程序默认用**微软 Edge 的在线语音**（通过 `edge-tts`）。要明白一件事：

> 它**不是**微软给开发者的正式 API，是社区逆向出来的 WebSocket 接口。

所以它有三个绕不开的问题：

1. **音色会被微软随时增删。** 2026-10 那一轮，微软把在线中文音色砍到只剩 8 个，晓辰 / 晓涵 / 晓墨 / 晓秋 / 晓睿 / 晓萱 / 晓颜 / 晓悠 / 云野 / 云泽 全部下线，程序里早就清掉了。
2. **握手链路不稳定。** 国内网络下建连顺利时 0.7 秒，倒霉时 20~40 秒才超时。程序为此专门做了常驻连接池（`edge_pool.py`），但底层链路终究不是我们能控制的。
3. **它可能彻底停掉。** 微软哪天关掉或加固这个接口，程序直接不能出声。

**看到这些现象，基本就是 Edge 那条路出问题了：**

| 现象 | 含义 |
| --- | --- |
| 状态栏「连不上微软语音服务器」 | 网络或接口挂了 |
| 「合成结果为空」 | 接口返回空音频，通常音色被下线 |
| `NoAudioReceived` | 音色代码已失效 |
| 一直「合成中…」不动 | 握手卡住 |

**出现这些，请按下面的名单自己换渠道。** 本程序不会自动帮你换，需要你手动操作。

---

## 三种换法，按「要不要改代码」分

| 档次 | 渠道 | 要不要改代码 | 适合谁 |
| --- | --- | --- | --- |
| **A. 已经内置，改配置就行** | Fish Audio、RVC | ❌ 不用 | 大多数人 |
| **B. 加几行代码** | Azure、讯飞、Google、腾讯、阿里、百度 | ✅ 要 | 会一点 Python 的 |
| **C. 彻底离线** | Piper、Kokoro、Windows SAPI | ✅ 要 | 想彻底断网用 |

---

## A. 已经内置，改配置就行

这两个引擎程序里早就写好了，**在「设置」里点几下就能切过去，一行代码都不用改。**

### A1. Fish Audio（最快能恢复说话的方案）

1. 去 <https://fish.audio> 注册
2. 建一个 API Key
3. 音色广场挑一个音色，复制它的 `reference_id`
4. 程序「设置」里填 **Fish Key** 和 **音色 ID**
5. 「发声 → 引擎」切到 **Fish Audio API**

免费额度有限但要不了多少钱，按量付费。不想花钱就先试 Free 档。

> 注意：Fish Audio 的服务器也在境外，国内有时要挂代理。如果一直超时，试试开关系统代理。

### A2. RVC 角色音色（完全本地，最抗风险）

**这是唯一一种「境外服务全挂了也照样能用」的方案** —— 因为它的第一步虽然也走 Edge，但模型加载和变声全在本地。

但要注意：现在的 RVC 流程是 **Edge 合成 → RVC 变声**。Edge 彻底挂了，RVC 也会跟着挂。

真要在 Edge 全挂时还能用，得配合下面 C 档的离线合成器（用 Piper 代替 Edge 那一步）。

不过只要 Edge 还没彻底死、只是音色变少或者慢，RVC 就照常能用。

---

## B. 加几行代码，接官方云厂商

这一档都是**正规 API**，有合同有 SLA，不会像 Edge 那样说没就没。代价是每个都要注册、实名、拿 Key，而且得改代码。

### B1. Azure 语音（**最推荐**）

| 项目 | 内容 |
| --- | --- |
| 免费额度 | **每月 50 万字符**（神经音色），限 20 次 / 60 秒 |
| 中文质量 | 全量音色，含方言、情感、多角色 |
| 国内直连 | 国际版偏慢；中国区（21Vianet）要企业认证 |
| 改动量 | **最小**，SSML 结构和 Edge 几乎一样 |

额度来自[微软官方配额文档](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits)（2026-10-02 更新）。50 万字符 / 月，按每句 20 字算能用 2.5 万句，打游戏绝对够。

**怎么做：**

1. 去 <https://portal.azure.com> 建一个「语音服务」资源，定价层选 **Free F0**
2. 拿到 **密钥** 和 **区域**（例如 `eastasia`）
3. 在 `app.py` 里加一个合成函数（照抄 Edge 那套 SSML 结构）：

```python
def _synth_azure(self, text):
    key = self.cfg.get("azure_key", "").strip()
    region = self.cfg.get("azure_region", "").strip() or "eastasia"
    if not key:
        raise RuntimeError("还没填 Azure 密钥，去「设置」里填")
    voice = self.cfg.get("voice", "zh-CN-XiaoxiaoNeural")
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="zh-CN"><voice name="%s">%s</voice></speak>'
        % (voice, text)
    )
    req = urllib.request.Request(
        "https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % region,
        data=ssml.encode("utf-8"),
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()
```

4. 在 `ENGINES` 列表（`app.py` 约第 99 行）里加一项：

```python
("Azure 官方 · 免费额度", "azure"),
```

5. 在 `_synth`（约第 1556 行）里加个分支：

```python
if key == "azure":
    return self._synth_azure(text)
```

6. 记得在设置窗口里加两个输入框填 `azure_key` / `azure_region`，或者直接改 `config.json`：

```json
{ "azure_key": "你的密钥", "azure_region": "eastasia" }
```

改完双击 `build.bat` 重新打包。

### B2. 讯飞在线语音合成

| 项目 | 内容 |
| --- | --- |
| 免费额度 | **每天 500 次**（SDK 和 Web API 合计），基础发音人免费 |
| 中文质量 | 国内第一梯队 |
| 国内直连 | ✅ 完全没问题 |
| 改动量 | 中，要算 HMAC 签名 |

额度来自[讯飞官方说明](https://www.xfyun.cn/doc/tts/online_tts/tts_description.html)（2026-10-20 核实）。每日 500 次对个人够用，**国内直连这一点是它最大的优势**。

注意：只有**基础发音人**（小燕、许久、小萍、小婧、许小宝）免费，特色发音人要买，部分可试用 15 天。

接入要点：需要 `APPID` / `APIKey` / `APISecret`，用 HMAC-SHA256 生成签名拼进 WebSocket URL。官方文档有各语言示例，照抄即可。

### B3. 腾讯云 / 阿里云 / 百度智能云

| 渠道 | 免费额度 | 国内直连 | 改动量 |
| --- | --- | --- | --- |
| 腾讯云 TTS | 有免费试用额度 | ✅ | 中，TC3 签名 |
| 阿里云智能语音 | 有免费试用（通常 3 个月） | ✅ | 中，Token 鉴权 |
| 百度智能云 | 有免费额度 | ✅ | 中，鉴权方式较老 |

> ⚠️ **这三家的具体免费额度我没能核实到可靠的一手数字**（都要登录控制台才看得到）。所以这里只写「有免费额度」，不写具体数。真要用请自己开控制台确认，别信任何二手转述。

三家都要实名认证。接入方式差不多：拿 AccessKey → 签名 → POST 文本 → 拿回音频字节。以你的 `_synth` 签名（`text -> bytes`）为准，包一层就行。

### B4. Google Cloud Text-to-Speech

| 项目 | 内容 |
| --- | --- |
| 免费额度 | Standard/WaveNet **400 万字符/月**，Neural2/Chirp3 HD **100 万/月** |
| 中文质量 | 好 |
| 国内直连 | ❌ 需要代理 |
| 改动量 | 中，要 OAuth 或 API Key |

**必须绑信用卡**才能开通，超额自动扣费 —— 用之前一定先设预算告警。

额度数字来自 [2026-10 的第三方盘点](https://dev.to/unicodefawn/free-speech-to-text-and-text-to-speech-api-quotas-oct-2026-with-the-catches-17hg)，Google 官方定价页建议自己再看一眼。

**国内直连不通这一点和 Edge 一样是硬伤**，如果你想摆脱网络问题，它帮不了你。

### B5. ElevenLabs

| 项目 | 内容 |
| --- | --- |
| 免费额度 | 1 万 credits / 月 ≈ **10 分钟语音** |
| 中文质量 | 一般 |
| 国内直连 | ⚠️ 不稳 |
| 商用 | **免费档不允许商用** |

音色最像真人，但 1 万 credits 只够 10 分钟，打游戏一晚上就用光了。适合试听，不适合日常。

---

## C. 彻底离线（Edge 死了也能用）

这是**最抗风险**的一档：不联网、没有额度、没有 Key、没人能给你下线。

| 方案 | 说明 | 中文 | 代价 |
| --- | --- | --- | --- |
| **Piper** | 本地神经网络 TTS，CPU 就能跑，延迟几十毫秒 | 有中文模型，质量中等 | 要引入依赖 + 下载模型（几十 MB） |
| **Kokoro** | 更新的本地模型，质量比 Piper 好 | 有中文 | 要 PyTorch / ONNX，体积大 |
| **Windows SAPI** | 系统自带，`pyttsx3` 一行调用 | 有（系统语音） | **音质差**，但零安装、绝对可用 |

如果你想要「Edge 挂了我也不受影响」，**正解是把合成层换成 Piper 或 SAPI**，而不是再找一个在线渠道。在线渠道只是把「依赖微软」换成「依赖别家」，本质问题没解决。

改造思路：在 `_synth` 里加一个 `local` 分支，用 `pyttsx3` 或 `piper` 生成 wav，其余流程（缓存、播放、热键）完全不用动。

---

## 怎么判断该选哪个

```
Edge 还能用，只是偶尔慢？
└─ 什么都不用换，点「预热」把常用语本地化即可

Edge 报错 / 音色没了，想立刻恢复说话
└─ A1 Fish Audio（改配置，5 分钟搞定）

想要稳定、长期、中文好，愿意注册
└─ B1 Azure F0（改动最小）或 B2 讯飞（国内直连最稳）

不想再受任何在线服务影响
└─ C 档，换成本地合成器
```

---

## 改完记得

1. **重新打包**：双击 `build.bat`
2. **清缓存**：换引擎后缓存键不同，不会串音，但旧的 `cache\` 可以清掉省空间
3. **自己测一遍**：点「试听」确认出声，再按 `Enter` 看队友能不能听到

---

## 说明

本文档里的免费额度都是**写这份文档时**查到的情况，各家随时会改。数字后面标了来源和核实日期，用之前建议自己再确认一遍。

**只有 Edge 是「不需要注册、不需要 Key、无限制」的** —— 这也是它一直被用作默认值的原因。天下没有既能白用、又稳定、又有 SLA 的接口；你只能在「免费但脆弱」和「要注册但可靠」之间选一个。
