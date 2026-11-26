"""视频/音频 → 字幕 → 中文速览。**只给人看，不进 RAG。**

流程
----
    B 站链接 ──→ 官方 API 拿标题/时长/CID ──┬─ 有字幕 → 直接拉（0 成本）
                                             └─ 没字幕 → 下音频流 → 切片 → GLM-ASR
    其它链接 ──→ yt-dlp ──→ 同上
    本地文件 ──→ 直接切片 → GLM-ASR

    字幕 + 速览 → 03-Resources/视频字幕/<标题>.md

四条踩过的坑（别再改回去）
--------------------------
1. **不要用 yt-dlp 抓 B 站网页**：机房 IP 会被风控返回 412 Precondition Failed，
   连浏览器 UA + Referer 都救不了。走 api.bilibili.com 的 view / player / playurl
   接口（带一次随机 buvid3 cookie 就行），CDN 音频流能正常下。
2. **每段 28 秒**：GLM-ASR 单次上限 30 秒 / 25MB。上一段结果当 prompt 回传，
   人名术语不会每段重猜。
3. **字幕不进向量库**：embedding 又贵又把检索结果冲淡。视频目录在 rag.skip_file
   里整目录排除，只有你主动打开才看得到。
4. **先找字幕再花钱转写**：平台字幕免费，ASR 约 0.015 元/分钟。

环境变量
--------
MODEL_ASR        语音识别模型（默认 glm-asr-2512）
VIDEO_MAX_MIN    单个视频最长几分钟（默认 180，超了直接拒，避免烧钱）
VIDEO_DIR        视频笔记目录（默认 03-Resources/视频字幕）
BILI_COOKIE      可选，B 站登录 Cookie（如 `SESSDATA=xxx`），能解锁字幕/更高音质
YTDLP_COOKIES    可选，cookies.txt 路径，给抖音等需要登录的平台用
"""
import gc
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from config import ENV, DATA_DIR, user_dir
from llm import LLMRouter

llm = LLMRouter()

VIDEO_DIR = ENV.get("VIDEO_DIR", "03-Resources/视频字幕").strip("/")
WORK_ROOT = DATA_DIR / "video_work"
MAX_MINUTES = int(ENV.get("VIDEO_MAX_MIN", "180") or 180)
ASR_MODEL = ENV.get("MODEL_ASR", "glm-asr-2512").strip() or "glm-asr-2512"
# 语音识别供应商：
#   sensevoice（默认）—— 本地 sherpa-onnx 跑 SenseVoice-Small int8，0 元/RTF≈0.06/峰值约 421MB
#   siliconflow —— 硅基流动云端 SenseVoiceSmall，0 元（免费档），不占本地内存
#   deepseek —— 云端按小时计费（约 0.41 元/小时）
#   glm —— 智谱，支持把上一段当 prompt（需有资源包）
#
# 兜底顺序（ASR_PROVIDER=sensevoice 时）：本地 → 硅基流动（免费）→ DeepSeek（付费）。
# 为什么中间插一层：2026-11-26 批量导入 181 篇时内存只有 493MB < 阈值 520MB，
# 每一个都回落到了付费的 DeepSeek。硅基流动上 FunAudioLLM/SenseVoiceSmall 标价免费，
# 而且和本地是同一个模型，没理由继续付这笔钱。
ASR_PROVIDER = (ENV.get("ASR_PROVIDER", "sensevoice") or "sensevoice").strip().lower()
# 硅基流动 ASR（免费档）。key 默认复用 embedding 那个（同一个账号），也可单独配
ASR_SF_API_KEY = (ENV.get("ASR_SF_API_KEY") or ENV.get("EMBED_API_KEY") or "").strip()
ASR_SF_BASE = (ENV.get("ASR_SF_BASE") or "https://api.siliconflow.cn").rstrip("/")
ASR_SF_MODEL = (ENV.get("ASR_SF_MODEL") or "FunAudioLLM/SenseVoiceSmall").strip()
# SenseVoice 模型目录（需 model.int8.onnx + tokens.txt，共约 230MB）
SENSEVOICE_DIR = Path(ENV.get("SENSEVOICE_DIR") or (DATA_DIR / "models" / "sensevoice"))
SENSEVOICE_THREADS = max(1, int(ENV.get("SENSEVOICE_THREADS", "2") or 2))
# 加载本地模型前要求的最低可用内存（MB）。实测单进程峰值约 421MB，留出余量给 web 本身
SENSEVOICE_MIN_FREE_MB = int(ENV.get("SENSEVOICE_MIN_FREE_MB", "520") or 520)
# 本地识别同时只允许 1 段（421MB × N 会把 1.9G 机器拖进 OOM）
ASR_CONCURRENCY = max(1, int(ENV.get("ASR_CONCURRENCY", "1") or 1))
CHUNK_SEC = 28


class AsrQuotaError(RuntimeError):
    """ASR 额度/鉴权类致命错误：重试和继续下一段都没意义，必须立即终止整个任务。"""
VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "flv", "webm", "m4v", "ts", "wmv", "mpg", "mpeg"}
AUDIO_EXTS = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "amr"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# —— 运行时 Cookie（网页「设置」页粘贴，落盘即生效，不用重启）——
COOKIE_FILE = DATA_DIR / "ytdlp_cookies.txt"        # 通用平台 cookies.txt（YouTube 等）
BILI_COOKIE_FILE = DATA_DIR / "bili_cookie.txt"     # B 站 SESSDATA 等（解锁字幕/更高音质）
# 抖音走 fork 版 yt-dlp（内置 secsdk 签名，免 cookie），见 douyin_*()
DOUYIN_YDLP_DIR = DATA_DIR / "douyin-ytdlp"

URL_RE = re.compile(r"^https?://\S+$")
VIDEO_HOSTS = ("bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "kuaishou.com",
               "xiaohongshu.com", "xhslink.com", "ixigua.com", "youtube.com", "youtu.be",
               "v.qq.com", "youku.com", "iqiyi.com", "weibo.com", "ixigua.com")
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")


def is_douyin(url):
    return any(h in (url or "").lower() for h in DOUYIN_HOSTS)



# ---------------- 基础工具 ----------------

def is_url(text):
    return bool(URL_RE.match((text or "").strip()))


# 从一段话里抠 URL：中文和中文标点一律当结束符。
# App 复制出来的分享文案常是「链接+口令」粘成一句（抖音尤其），整段拿去请求必然失败。
_URL_IN_TEXT = re.compile(
    r"https?://[^\s\u4e00-\u9fff，。！？、；：\u201c\u201d\u2018\u2019（）【】《》…]+")


def extract_url(text):
    """抽出句子里的第一个 URL，返回干净链接（没有就返回空串）。"""
    m = _URL_IN_TEXT.search(text or "")
    return m.group(0).rstrip(".,;:!?)]}\"'") if m else ""


def is_video_url(text):
    """看域名判「这大概率是个视频」——粘链接时用来区分「存个链接」和「帮我看这个视频」。"""
    t = (text or "").lower()
    return any(h in t for h in VIDEO_HOSTS)


def media_ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def is_media(name):
    return media_ext(name) in (VIDEO_EXTS | AUDIO_EXTS)


def _ts(sec):
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def ffmpeg_exe():
    p = ENV.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _safe_name(text, maxlen=60):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", text or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen] or "未命名视频").strip(" .")


def _http():
    return httpx.Client(timeout=60, follow_redirects=True,
                        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})


# ---------------- B 站：官方 API 路线（机房 IP 唯一能走通的路） ----------------

def bili_bvid(url):
    """从各种 B 站链接里揪出 BV 号；短链先跳转。"""
    u = (url or "").strip()
    m = re.search(r"(BV[0-9A-Za-z]{10})", u)
    if m:
        return m.group(1), _bili_page(u)
    if "b23.tv" in u:
        try:
            with _http() as c:
                r = c.get(u)
            m = re.search(r"(BV[0-9A-Za-z]{10})", str(r.url))
            if m:
                return m.group(1), _bili_page(str(r.url))
        except Exception:
            pass
    m = re.search(r"av(\d+)", u, re.I)
    if m:
        return "av" + m.group(1), _bili_page(u)
    return None, 1


def _bili_page(url):
    m = re.search(r"[?&]p=(\d+)", url or "")
    return int(m.group(1)) if m else 1


def _bili_client():
    c = _http()
    try:
        spi = c.get("https://api.bilibili.com/x/frontend/finger/spi").json()
        d = spi.get("data") or {}
        if d.get("b_3"):
            c.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
        if d.get("b_4"):
            c.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
    except Exception:
        pass
    raw = ""                                        # 登录 Cookie（网页设置页可填）
    try:
        raw = BILI_COOKIE_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        raw = ENV.get("BILI_COOKIE", "").strip()
    if raw:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                c.cookies.set(k.strip(), v.strip(), domain=".bilibili.com")
    return c


def bili_info(ids):
    """view 接口 → 标题 / 时长 / CID。"""
    bvid, page = ids
    c = _bili_client()            # 注意：这个 client 已经发过请求，不能再 with 进去
    try:
        params = {"bvid": bvid} if bvid.startswith("BV") else {"aid": bvid[2:]}
        v = c.get("https://api.bilibili.com/x/web-interface/view", params=params).json()
        if v.get("code") != 0:
            raise RuntimeError("B 站接口返回 %s：%s" % (v.get("code"), v.get("message")))
        d = v["data"]
        pages = d.get("pages") or []
        idx = min(max(page - 1, 0), len(pages) - 1) if pages else 0
        cid = pages[idx]["cid"] if pages else d.get("cid")
        dur = pages[idx].get("duration") if pages else d.get("duration")
    finally:
        c.close()
    return {
        "title": d.get("title") or "未命名视频",
        "duration": float(dur or 0),
        "platform": "bilibili",
        "uploader": (d.get("owner") or {}).get("name", ""),
        "source": "https://www.bilibili.com/video/%s" % d.get("bvid", bvid)
                  + (("?p=%d" % page) if page > 1 else ""),
        "_bvid": d.get("bvid", bvid),
        "_cid": cid,
    }


def bili_subs(meta):
    """player/v2 的字幕列表（多数视频匿名看不到，登录 Cookie 才行）。"""
    bvid, cid = meta["_bvid"], meta["_cid"]
    c = _bili_client()
    try:
        j = c.get("https://api.bilibili.com/x/player/v2",
                  params={"bvid": bvid, "cid": cid}).json()
        subs = ((j.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return []
        subs.sort(key=lambda s: 0 if "zh" in (s.get("lan") or "") else 1)   # 优先中文
        url = subs[0].get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return []
        body = c.get(url).text
    finally:
        c.close()
    return _merge_lines(_parse_bili_json(body))


def bili_audio(meta, workdir):
    """playurl 拿 DASH 音频流，挑码率最低的那条（语音识别够用，省流量省时间）。"""
    bvid, cid = meta["_bvid"], meta["_cid"]
    workdir.mkdir(parents=True, exist_ok=True)
    c = _bili_client()
    try:
        j = c.get("https://api.bilibili.com/x/player/playurl",
                  params={"bvid": bvid, "cid": cid, "fnval": 16,
                          "fnver": 0, "fourk": 1}).json()
        data = j.get("data") or {}
        dash = data.get("dash") or {}
        audio = sorted(dash.get("audio") or [], key=lambda x: x.get("bandwidth", 0))
        url = audio[0]["baseUrl"] if audio else (
            (data.get("durl") or [{}])[0].get("url") if data.get("durl") else "")
        if not url:
            raise RuntimeError("拿不到音频流（可能该视频是付费/番剧/需要登录）")
        ext = "m4s" if audio else "flv"
        dst = workdir / ("audio." + ext)
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                raise RuntimeError("音频下载失败 HTTP %s" % r.status_code)
            with open(dst, "wb") as f:
                for chunk in r.iter_bytes(256 * 1024):
                    f.write(chunk)
    finally:
        c.close()
    if dst.stat().st_size < 10240:
        raise RuntimeError("音频文件异常（太小）")
    return dst


# ---------------- 通用路线：yt-dlp（抖音等） ----------------

def _ydl(opts):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "retries": 2, "socket_timeout": 20, "http_headers": {"User-Agent": UA}}
    cookie = ENV.get("YTDLP_COOKIES", "").strip()
    if cookie:
        base["cookiefile"] = cookie
    elif COOKIE_FILE.exists():                    # 网页「设置」页保存的 cookies.txt
        base["cookiefile"] = str(COOKIE_FILE)
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


# ---------------- 抖音：fork 版 yt-dlp（免 cookie，内置 secsdk 签名） ----------------
#
# 官方 yt-dlp 的 DouyinIE 走 aweme/v1/web/aweme/detail 但没签 x-secsdk-web-signature，
# 加任何 cookie 都报 "Fresh cookies needed"。所以抖音不走 cookie，走 douyin-fix fork
# （见 data/douyin-ytdlp/COMMIT）。用 subprocess 隔离，避免污染主 venv 的 yt-dlp。

def _douyin_ydl(args, timeout=300):
    if not (DOUYIN_YDLP_DIR / "yt_dlp").is_dir():
        raise RuntimeError("抖音补丁版 yt-dlp 未安装（data/douyin-ytdlp 缺 yt_dlp）")
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings"] + args,
                       cwd=str(DOUYIN_YDLP_DIR), capture_output=True, timeout=timeout)
    return r


def _douyin_expand(url):
    """v.douyin.com / iesdouyin.com 短链展开成 www.douyin.com/video/<id>。"""
    if "v.douyin.com" in url or "iesdouyin.com" in url:
        try:
            with httpx.Client(timeout=30, follow_redirects=True,
                              headers={"User-Agent": UA}) as c:
                final = str(c.get(url).url)
            if "douyin.com/video/" in final:
                return final
        except Exception:
            pass
    return url


def douyin_info_and_audio(url, workdir):
    """抖音：fork 版下音频，返回 (meta, 音频路径)。"""
    url = _douyin_expand(url)
    workdir.mkdir(parents=True, exist_ok=True)

    r = _douyin_ydl(["--skip-download", "--dump-single-json", url])
    if r.returncode != 0 or not r.stdout.strip():
        err = (r.stderr or b"").decode("utf-8", "ignore").strip()
        raise RuntimeError("抖音解析失败：%s" % (err[-160:] or "无返回"))
    info = json.loads(r.stdout)
    raw_title = (info.get("title") or info.get("description") or "未命名视频")
    # 抖音标题常带 #话题，取第一个非空片段当标题，其余丢掉
    title = next((s.strip() for s in re.split(r"#", raw_title) if s.strip()), "")
    meta = {
        "title": title or "抖音视频",
        "duration": float(info.get("duration") or 0),
        "platform": "douyin",
        "uploader": (info.get("uploader") or info.get("channel") or ""),
        "source": info.get("webpage_url") or url,
    }

    out_tpl = str(workdir / "audio.%(ext)s")
    r = _douyin_ydl(["-f", "bestaudio/best", "-o", out_tpl, url], timeout=900)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "ignore").strip()
        raise RuntimeError("抖音音频下载失败：%s" % err[-160:])
    files = [p for p in workdir.glob("audio.*") if p.suffix.lower() != ".part"]
    if not files:
        raise RuntimeError("抖音音频下载失败（没有产出文件）")
    return meta, max(files, key=lambda p: p.stat().st_size)


def ytdlp_info(url):
    with _ydl({"skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    return {
        "title": info.get("title") or "未命名视频",
        "duration": float(info.get("duration") or 0),
        "platform": info.get("extractor_key") or info.get("extractor") or "",
        "uploader": info.get("uploader") or "",
        "source": info.get("webpage_url") or url,
    }


def ytdlp_subs(url, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        with _ydl({
            "skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": ["zh-Hans", "zh-CN", "zh", "zh-Hant", "en"],
            "subtitlesformat": "srt/vtt/best",
            "outtmpl": str(workdir / "sub.%(ext)s"),
        }) as ydl:
            ydl.download([url])
    except Exception:
        return []
    pairs = []
    for p in sorted(workdir.glob("sub*")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        ext = media_ext(p.name)
        if ext == "json":
            pairs += _parse_bili_json(text)
        elif ext in ("srt", "vtt"):
            pairs += _parse_srt(text)
    return _merge_lines(pairs)


def ytdlp_audio(url, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    with _ydl({"format": "bestaudio/best",
               "outtmpl": str(workdir / "audio.%(ext)s")}) as ydl:
        ydl.download([url])
    files = [p for p in workdir.glob("audio.*") if p.suffix.lower() != ".part"]
    if not files:
        raise RuntimeError("音频下载失败（该平台可能需要登录 Cookie，见 YTDLP_COOKIES）")
    return max(files, key=lambda p: p.stat().st_size)


# ---------------- 字幕解析 ----------------

def _srt_seconds(line):
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", line.strip())
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _parse_srt(text):
    out, cur, buf = [], None, []
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            if cur is not None and buf:
                out.append((cur, " ".join(buf).strip()))
            cur, buf = None, []
            continue
        t = _srt_seconds(line)
        if t is not None:
            cur = t
            continue
        if line.isdigit() or line.upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        buf.append(re.sub(r"<[^>]+>", "", line))
    if cur is not None and buf:
        out.append((cur, " ".join(buf).strip()))
    return [(t, s) for t, s in out if s]


def _parse_bili_json(text):
    """B 站字幕 json：{"body":[{"from":12.3,"content":"…"}]}"""
    import json
    try:
        data = json.loads(text)
    except Exception:
        return []
    body = data.get("body") if isinstance(data, dict) else data
    out = []
    for it in body or []:
        if isinstance(it, dict) and it.get("content"):
            out.append((int(float(it.get("from", 0))), str(it["content"]).strip()))
    return out


def _merge_lines(pairs):
    """去重 + 合并：相邻同文只留一条，短句累积成一行（字幕一条条读没意义）。"""
    out, last = [], None
    for t, s in pairs:
        s = re.sub(r"\s+", " ", s).strip()
        if not s or s == last:
            continue
        last = s
        if out and t - out[-1][0] < CHUNK_SEC * 2 and len(out[-1][1]) < 120:
            out[-1] = (out[-1][0], (out[-1][1] + " " + s).strip())
        else:
            out.append((t, s))
    return out


# ---------------- 切片 + 语音识别 ----------------

def _duration_of(path):
    exe = ffmpeg_exe()
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "-i", str(path)], capture_output=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)",
                      (r.stderr or b"").decode("utf-8", "ignore"))
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    except Exception:
        pass
    return 0.0


def slice_audio(src, outdir, seconds=CHUNK_SEC):
    """切成 16k 单声道 wav 小段（用 wav 是不挑 mp3 编码器）。"""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("服务器没有 ffmpeg（pip install imageio-ffmpeg 或系统装 ffmpeg）")
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(src), "-vn",
           "-ac", "1", "-ar", "16000", "-f", "segment",
           "-segment_time", str(seconds), "-reset_timestamps", "1",
           str(outdir / "c%04d.wav")]
    r = subprocess.run(cmd, capture_output=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError("切片失败：%s" % (r.stderr or b"")[:200].decode("utf-8", "ignore"))
    return sorted(outdir.glob("c*.wav"))


# 只由标点 / emoji / 符号组成的"文本"，是模型对音乐段、静音段的常见输出。
# 硅基流动实测：纯 440Hz 音返回 "🎼."。现有过滤是 len(text)<=1，拦不住这种两字符的。
_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)


def _noise_only(text):
    """去掉标点/符号/emoji 后什么都不剩 → 判定为噪音。

    汉字和字母数字属于 \\w，会被保留；"啦。" 这种仍算有内容（由上层的最短长度兜）。
    """
    return not _NOISE_RE.sub("", text or "")


def _asr_siliconflow(path):
    """硅基流动 ASR：POST /v1/audio/transcriptions，免费档 FunAudioLLM/SenseVoiceSmall。

    和本地跑的是同一个模型（云端浮点，本地 int8），但**不占本机内存**，所以专门
    用来替代「内存一紧就回落付费 DeepSeek」这条路径。

    实测（2026-11-26）：
      - 我们的 28 秒 16k 单声道 WAV 可直接上传（mp3 也行），10 秒音频约 0.9MB
      - 20 次连发零限流；延迟 0.5~20.7s、均值 3.6s（本地是稳定 1.7s/片），
        所以它只做兜底，不当主力
      - 限制：单文件 ≤1 小时 / ≤50MB，28 秒切片远低于此
      - 无 prompt / language / response_format 参数，返回 {"text": "..."}
    """
    if not ASR_SF_API_KEY:
        raise AsrQuotaError("缺少 ASR_SF_API_KEY / EMBED_API_KEY，无法走硅基流动转写")
    name = getattr(path, "name", None) or str(path).rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        r = httpx.post(
            ASR_SF_BASE + "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + ASR_SF_API_KEY},
            files={"file": (name, f, "audio/wav")},
            data={"model": ASR_SF_MODEL},
            timeout=180,          # 实测最坏 20.7s，留足余量
        )
    if r.status_code != 200:
        body = r.text[:200]
        # 401/403 鉴权或实名、402 余额 → 换谁都没用，别再往下拖
        if r.status_code in (401, 402, 403):
            raise AsrQuotaError("硅基流动 ASR 不可用（HTTP %s，key 失效/未实名/余额不足）：%s"
                                % (r.status_code, body))
        # 429 免费档限流、503 繁忙 → 临时状态，交给上层退避重试
        if r.status_code in (429, 503):
            raise RuntimeError("硅基流动 ASR 限流/繁忙（HTTP %s）" % r.status_code)
        raise RuntimeError("硅基流动 ASR HTTP %s %s" % (r.status_code, body))
    text = re.sub(r"\s+", " ", (r.json().get("text") or "").strip())
    return "" if _noise_only(text) else text


def asr_deepseek(path):
    """DeepSeek ASR：POST /v1/tools/asr。

    限制：单次 ≤120 秒 / ≤8 MiB，16-bit PCM WAV 或 Ogg Opus；无 prompt 参数，
    语言自动检测（支持中文/粤语等 21 种）。我们的切片是 28 秒单声道 16k WAV，正好符合。
    计费按音频时长（比智谱便宜，约 $0.116/小时，限时五折）。
    """
    key = ENV.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise AsrQuotaError("缺少 DEEPSEEK_API_KEY，无法用 DeepSeek 转写")
    base = ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    name = getattr(path, "name", None) or str(path).rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        r = httpx.post(base + "/v1/tools/asr",
                       headers={"Authorization": "Bearer " + key},
                       files={"file": (name, f, "audio/wav")}, timeout=180)
    if r.status_code != 200:
        body = r.text[:200]
        # 401 鉴权失效、402 余额不足 → 致命，重试没意义
        if r.status_code in (401, 402):
            raise AsrQuotaError("DeepSeek ASR 不可用（HTTP %s，余额不足或 key 失效）：%s"
                                % (r.status_code, body))
        # 429 是账号级并发超限（上限 50 个进行中），属于临时状态，交给上层退避重试
        if r.status_code == 429:
            raise RuntimeError("DeepSeek ASR 并发超限（HTTP 429），稍后重试")
        if r.status_code == 503:
            raise RuntimeError("DeepSeek ASR 服务繁忙，稍后重试")
        raise RuntimeError("DeepSeek ASR HTTP %s %s" % (r.status_code, body))
    d = r.json()
    return re.sub(r"\s+", " ", (d.get("text") or "").strip())


def _asr_glm(path, prev_text=""):
    """智谱 ASR：单次 ≤30 秒 / 25MB，支持 prompt 传上一段做上下文。"""
    key = ENV.get("GLM_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 GLM_API_KEY，无法转写")
    url = ENV.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/") \
        + "/audio/transcriptions"
    data = {"model": ASR_MODEL, "stream": "false"}
    if prev_text:
        data["prompt"] = prev_text[-600:]      # 上一段当上下文，术语/人名更连贯
    name = getattr(path, "name", None) or str(path).rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        r = httpx.post(url, headers={"Authorization": "Bearer " + key},
                       data=data, files={"file": (name, f, "audio/wav")},
                       timeout=120)
    if r.status_code != 200:
        body = r.text[:200]
        # 智谱 ASR 单独计费：资源包/余额用完返 429 code 1113。
        # 这种错必须往上抛成「额度不足」，否则每段都被吞掉，最后只报「转写结果为空」，
        # 排查时会误以为是音频没声音（2026-11-20 踩过）。
        if r.status_code == 429 or "1113" in body or "余额不足" in body:
            raise AsrQuotaError("ASR 额度不足（智谱语音识别资源包/余额用完），"
                                "请到智谱开放平台充值后重试")
        if r.status_code in (401, 403):
            raise AsrQuotaError("ASR 鉴权失败（GLM_API_KEY 失效或没有语音识别权限）：%s" % body)
        raise RuntimeError("ASR HTTP %s %s" % (r.status_code, body))
    d = r.json()
    text = (d.get("text") or "").strip()
    if not text:
        try:
            text = (d["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            text = ""
    return re.sub(r"\s+", " ", text)


# ---- 本地语音识别：SenseVoice-Small（sherpa-onnx / int8，无需 torch、无需联网）----
_sv_lock = threading.Lock()
_sv_rec = None
_sv_load_err = ""
_ASR_SLOT = threading.Semaphore(ASR_CONCURRENCY)   # 本地识别限流，防内存叠加
# 当前有几段正在用本地模型：只有归零时才能安全释放（否则会把别人正在用的模型拆掉）
_sv_state_lock = threading.Lock()
_sv_inflight = 0
FALLBACK_LOG = DATA_DIR / "asr_fallback.log"


def _sensevoice_rec():
    """懒加载识别器：首次约 1.6 秒、常驻约 330MB，之后复用。"""
    global _sv_rec, _sv_load_err
    if _sv_rec is not None:
        return _sv_rec
    with _sv_lock:
        if _sv_rec is not None:
            return _sv_rec
        if _sv_load_err:
            raise RuntimeError(_sv_load_err)
        try:
            import sherpa_onnx
        except Exception as e:
            _sv_load_err = "未安装 sherpa-onnx（%s）" % e
            raise RuntimeError(_sv_load_err)
        model = SENSEVOICE_DIR / "model.int8.onnx"
        tokens = SENSEVOICE_DIR / "tokens.txt"
        if not model.is_file() or not tokens.is_file():
            _sv_load_err = "SenseVoice 模型缺失：%s（需 model.int8.onnx + tokens.txt）" % SENSEVOICE_DIR
            raise RuntimeError(_sv_load_err)
        try:
            _sv_rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model), tokens=str(tokens), num_threads=SENSEVOICE_THREADS,
                use_itn=True, language="zh", debug=False)
        except Exception as e:
            _sv_load_err = "SenseVoice 初始化失败：%s" % e
            raise RuntimeError(_sv_load_err)
        return _sv_rec


def _wav_samples(path):
    """wav → (float 采样点, 采样率)。切片由 ffmpeg 保证 16k 单声道 s16le。"""
    import array
    import wave
    with wave.open(str(path), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    a = array.array("h")
    a.frombytes(raw)
    if ch > 1:
        a = a[::ch]
    return [x / 32768.0 for x in a], sr


def _asr_note_fallback(err, target="DeepSeek"):
    """前一层失败、回落云端时留痕，便于回溯「这一段为什么走了云、走了哪一层」。"""
    try:
        with open(FALLBACK_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t识别失败，回落 %s\t%s\n"
                    % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), target,
                       str(err)[:200]))
    except Exception:
        pass


def _asr_sensevoice(path):
    """本地 SenseVoice-Small（int8）：0 元、RTF≈0.06、峰值约 421MB。"""
    global _sv_inflight
    with _ASR_SLOT:
        with _sv_state_lock:
            _sv_inflight += 1
        try:
            if _sv_rec is None:
                avail = min(_mem_available_mb(), _cgroup_headroom_mb())
                if avail < SENSEVOICE_MIN_FREE_MB:
                    raise RuntimeError("可用内存 %.0fMB < %dMB，本地识别暂不启动"
                                       % (avail, SENSEVOICE_MIN_FREE_MB))
            rec = _sensevoice_rec()
            samples, sr = _wav_samples(path)
            st = rec.create_stream()
            st.accept_waveform(sr, samples)
            rec.decode_stream(st)
            return re.sub(r"\s+", " ", (st.result.text or "").strip())
        finally:
            with _sv_state_lock:
                _sv_inflight -= 1


def _cgroup_headroom_mb():
    """本服务 cgroup 还能用多少 MB（systemd MemoryMax - 当前用量）。

    系统 MemAvailable 够、但 cgroup 顶到 MemoryMax 时，内核照样按 memcg 杀进程
    （2026-11-25 就是这样 OOM 的），所以本地识别前必须同时看这条线。
    """
    try:
        for line in open("/proc/self/cgroup", encoding="ascii"):
            parts = line.strip().split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                base = "/sys/fs/cgroup" + parts[2]
                with open(base + "/memory.max", encoding="ascii") as f:
                    mx = f.read().strip()
                if mx == "max":
                    return 999999.0
                with open(base + "/memory.current", encoding="ascii") as f:
                    cur = int(f.read().strip())
                return (int(mx) - cur) / 1048576.0
    except Exception:
        pass
    return 999999.0


def release_sensevoice():
    """释放本地模型，把约 330MB 常驻内存还给系统；下次用到会自动重新加载（约 1.6 秒）。

    任务结束调用一次即可：2 核 1.9G 的机器上，模型一直挂着会让 cgroup 贴着上限跑，
    一旦叠加 yt-dlp / ffmpeg 就触发 OOM（2026-11-25 就是这样把排队任务全丢了）。
    """
    global _sv_rec
    with _sv_state_lock:
        if _sv_inflight > 0:      # 还有段在识别，别拆
            return False
        rec, _sv_rec = _sv_rec, None
    if rec is None:
        return False
    del rec
    gc.collect()
    # glibc 不会自动把空闲堆还给系统：onnxruntime 释放后 RSS 仍停在 320MB 左右，
    # 必须显式 malloc_trim 才真正回收（实测 320MB → 61MB）。非 glibc 平台忽略即可。
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
    return True


# 硅基流动连续失败熔断：同一任务内连挂 N 次就跳过它，直接走 DeepSeek。
# 不加这个，一次网络故障会让每片都等满 180s 超时——11 片就是半小时。
_sf_state = threading.local()
_SF_MAX_FAILS = 2


def _sf_reset():
    _sf_state.n = 0


def _sf_tripped():
    return getattr(_sf_state, "n", 0) >= _SF_MAX_FAILS


def _sf_mark_fail():
    _sf_state.n = getattr(_sf_state, "n", 0) + 1


# 记录本次任务最后一次实际成功的通道，用于把「到底花没花钱」写进任务结果。
# 用 threading.local：每个任务一个线程，互不串。
_chan_state = threading.local()


def _mark_channel(name):
    _chan_state.last = name


CHANNEL_LABEL = {
    "sensevoice": "SenseVoice 本地",
    "siliconflow": "硅基流动 SenseVoice",
    "deepseek": "DeepSeek",
    "glm": "GLM",
}


def last_channel_label():
    """本次任务最终用的通道名（没走过识别时回落到配置值）。"""
    return CHANNEL_LABEL.get(getattr(_chan_state, "last", "") or ASR_PROVIDER,
                             "云端")


def asr_file(path, prev_text=""):
    """单段音频 → 文字。

    默认链（ASR_PROVIDER=sensevoice）：
        本地 SenseVoice → 硅基流动（免费）→ DeepSeek（付费，最后保险）

    - sensevoice（默认）：本地跑，0 成本、约 1.7s/片，但峰值 421MB
    - siliconflow：云端免费、不占内存，但慢且不稳（实测 0.5~20s）
    - deepseek：付费兜底（约 0.41 元/小时）
    - glm：支持把上一段当 prompt，术语/人名更连贯（需有额度）
    """
    if ASR_PROVIDER == "glm":
        return _asr_glm(path, prev_text)
    if ASR_PROVIDER == "siliconflow":
        t = _asr_siliconflow(path)
        _mark_channel("siliconflow")
        return t
    if ASR_PROVIDER == "deepseek":
        t = asr_deepseek(path)
        _mark_channel("deepseek")
        return t
    if ASR_PROVIDER == "sensevoice":
        try:
            t = _asr_sensevoice(path)
            _mark_channel("sensevoice")
            return t
        except AsrQuotaError:
            raise
        except Exception as e:
            # 本地跑不起来（最常见：内存不足）→ 先试免费的硅基流动，别急着付钱
            if _sf_tripped():
                _asr_note_fallback(e, "DeepSeek（硅基流动已熔断）")
                t = asr_deepseek(path)
                _mark_channel("deepseek")
                return t
            _asr_note_fallback(e, "硅基流动")
            try:
                t = _asr_siliconflow(path)
                _sf_state.n = 0            # 成功就复位，下次继续先试它
                _mark_channel("siliconflow")
                return t
            except Exception as e2:
                _sf_mark_fail()
                _asr_note_fallback(e2, "DeepSeek")
                t = asr_deepseek(path)
                _mark_channel("deepseek")
                return t
    t = asr_deepseek(path)
    _mark_channel("deepseek")
    return t


def transcribe_media(path, workdir, on_progress=None):
    """本地媒体文件（或已下好的音频）→ [(秒, 文本)]。"""
    d = _duration_of(path)
    if d and d > MAX_MINUTES * 60:
        raise RuntimeError("视频 %.0f 分钟，超过上限 %d 分钟（放开就改 VIDEO_MAX_MIN）"
                           % (d / 60, MAX_MINUTES))
    chunks = slice_audio(path, workdir / "chunks")
    if not chunks:
        raise RuntimeError("没切出音频片段（文件可能只有画面/已损坏）")
    _sf_reset()          # 每个任务独立计熔断，别把上个任务的失败带过来
    pairs, prev, fails = [], "", 0
    repeat = {"text": "", "n": 0}
    for i, p in enumerate(chunks):
        if on_progress:
            on_progress(i + 1, len(chunks))
        text = ""
        for attempt in range(2):
            try:
                text = asr_file(p, prev)
                break
            except AsrQuotaError:
                raise                      # 额度/鉴权问题，继续跑只会每段都白白失败
            except Exception as e:
                if attempt == 1:
                    fails += 1
                    if fails >= 5 and i < 3:
                        raise RuntimeError("ASR 连续失败：%s" % e)
                else:
                    time.sleep(2)          # 429 并发超限 / 503 繁忙，等一下再试一次
        if not text or len(text) <= 1:          # 纯音乐/静音常返回空或单个符号
            continue
        # 连续 3 段输出同一句话 = 模型在复读（常见于长静音），丢掉
        if text == repeat["text"]:
            repeat["n"] += 1
            if repeat["n"] >= 3:
                continue
        else:
            repeat = {"text": text, "n": 1}
        pairs.append((i * CHUNK_SEC, text))
        prev = (prev + " " + text)[-600:]
    if not pairs:
        raise RuntimeError("转写结果为空（整段没人声，或音频有问题）")
    return pairs


# ---------------- 总结 ----------------

SUMMARY_PROMPT = """你在帮用户做视频速览笔记。下面是视频字幕（每条前面有时间点）。
请输出**中文**，严格按下面格式，不要前言、不要解释、不要代码围栏：

## 一句话总结
（一句话说清这个视频讲了什么，40 字以内）

## 章节
- [mm:ss] 小标题：这一节讲了什么（按内容自然分段，别每一小段都列）

## 要点
- （真正有信息量的点：方法、参数、结论、数字；不要写"介绍了…"这类空话）

规则：
1. 只用字幕里出现过的事实，没提到的别编。
2. 时间点用字幕里给的，格式 mm:ss（超过一小时用 h:mm:ss）。
3. 全文 600 字以内，宁可少而准。"""


def _fmt_lines(pairs):
    return "\n".join("[%s] %s" % (_ts(t), s) for t, s in pairs)


def _blockify(text, size=8000):
    blocks, buf = [], ""
    for line in text.splitlines():
        if len(buf) + len(line) + 1 > size:
            blocks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        blocks.append(buf)
    return blocks


_TITLE_PREFIX = re.compile(r"(?m)^(\s*[-*]\s*\[?[\d:]+\]?\s*)小标题\s*[:：]\s*")


def _clean_summary(text):
    """模型爱在章节行加「小标题：」前缀，读起来啰嗦，统一去掉。"""
    return _TITLE_PREFIX.sub(r"\1", (text or "").strip()).strip()


def summarize(pairs):
    """字幕 → 速览。超长先分块提炼再合并，避免一次塞爆上下文。"""
    full = _fmt_lines(pairs)
    if len(full) <= 9000:
        return _clean_summary(llm.chat("%s\n\n字幕：\n%s" % (SUMMARY_PROMPT, full),
                                       tier="main", max_tokens=2000))

    blocks = _blockify(full)
    notes = []
    for i, b in enumerate(blocks):
        part = llm.chat(
            "这是视频字幕的第 %d/%d 段。只提炼这一段的事实要点与时间点，"
            "输出 5~8 条 Markdown 列表项，格式：- [mm:ss] 内容。不要总结全文。\n\n%s"
            % (i + 1, len(blocks), b), tier="main", max_tokens=900)
        notes.append(part.strip())
    merged = "\n".join(notes)[:12000]
    return _clean_summary(llm.chat(
        "%s\n\n下面是分段提炼的要点（带时间点），请合并成一份完整速览："
        "去重、按时间顺序组织。\n%s" % (SUMMARY_PROMPT, merged),
        tier="main", max_tokens=2000))


# ---------------- 落盘 / 读回 ----------------

def save_note(user, meta, pairs, summary, method, series=""):
    d = user_dir(user) / VIDEO_DIR
    sname = _safe_name(series, maxlen=40) if (series or "").strip() else ""
    if sname:
        d = d / sname
    d.mkdir(parents=True, exist_ok=True)
    title = _safe_name(meta.get("title"))
    path = d / ("%s.md" % title)
    i = 2
    while path.exists():
        path = d / ("%s-%d.md" % (title, i))
        i += 1
    dur = meta.get("duration") or (pairs[-1][0] + CHUNK_SEC if pairs else 0)
    src = meta.get("source") or ""
    head = [
        "---",
        "title: %s" % title,
        "type: video",
        "platform: %s" % (meta.get("platform") or "-"),
        "source: %s" % (src or "-"),
        "source_raw: %s" % (meta.get("source_raw") or "-"),
        "duration: %s" % _ts(dur),
        "method: %s" % method,
        "created: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "---",
        "",
        "# %s" % title,
        "",
        "> %s时长 %s ｜ %s ｜ %s" % (
            ("%s ｜ " % src) if src.startswith("http") else "",
            _ts(dur), method, datetime.now().strftime("%Y-%m-%d")),
        "",
        summary.strip(),
        "",
        "## 完整字幕",
        "",
    ]
    path.write_text("\n".join(head) + _fmt_lines(pairs) + "\n", encoding="utf-8")
    return path


SECTION_RE = re.compile(r"(?m)^##\s*(.+?)\s*$")


def _section(body, name):
    m = SECTION_RE.search(body)
    while m:
        if name in m.group(1):
            start = m.end()
            nxt = SECTION_RE.search(body, start)
            return body[start:(nxt.start() if nxt else len(body))].strip()
        m = SECTION_RE.search(body, m.end())
    return ""


def parse_note(path, rel=None):
    """视频笔记 → 结构化数据，供网页列表渲染。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    raw, body = "", text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if m:
        raw, body = m.group(1), text[m.end():]
    fm = {}
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    secs = []
    for line in _section(body, "章节").splitlines():
        mm = re.match(r"^\s*[-*]\s*\[?([\d:]+)\]?\s*(.+)$", line)
        if mm:
            secs.append({"t": mm.group(1), "text": mm.group(2).strip()})
    points = []
    for line in _section(body, "要点").splitlines():
        mm = re.match(r"^\s*[-*]\s*(.+)$", line)
        if mm:
            points.append(mm.group(1).strip())
    return {
        "title": fm.get("title") or path.stem,
        "source": fm.get("source", ""),
        "source_raw": fm.get("source_raw", ""),
        "platform": fm.get("platform", ""),
        "duration": fm.get("duration", ""),
        "method": fm.get("method", ""),
        "created": fm.get("created", ""),
        "gist": _section(body, "一句话总结").replace("\n", " ").strip(),
        "sections": secs,
        "points": points,
        "rel": rel or path.name,
        "mtime": path.stat().st_mtime,
    }


def list_notes(user, limit=50):
    d = user_dir(user) / VIDEO_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        rel_dir = p.parent.relative_to(d)
        series = "" if str(rel_dir) == "." else rel_dir.as_posix()
        rel = "%s/%s" % (VIDEO_DIR, p.relative_to(d).as_posix())
        item = parse_note(p, rel)
        if item:
            item["series"] = series
            out.append(item)
    return out


def list_series(user):
    """视频字幕目录下已有的系列（一级子目录），给前端下拉选择用。

    只列一级目录：save_note 的系列名会过 _safe_name（斜杠被剔掉），
    系列本来就只能是 `视频字幕/<系列>/` 这一层。
    """
    d = user_dir(user) / VIDEO_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


# ---------------- 索引页生成（给人看的 Obsidian 导航层，不参与检索） ----------------

_LINK_UNSAFE = set("[]#|^")


def _is_video_note(path):
    """只看前 400 字节判断 type，避免为过滤索引文件去读整篇。

    persona.py 里有一份同样的实现：那边是为了不引入 video 的重依赖才独立的，
    改判断逻辑时两处要一起改。
    """
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(400)
    except Exception:
        return False
    return bool(re.search(r"(?m)^type:\s*video\s*$", head))


def _note_link(stem):
    """文件名 → Obsidian 可点的链接。

    按**真实文件名**建链（不是 frontmatter 的 title）：save_note 遇到重名会存成
    `X-2.md`，而 frontmatter 里 title 仍是 X，用 title 建链会指向不存在的文件。
    文件名含 [] # | ^ 时双链会断，退回 markdown 链接，显示文本里的方括号要转义。
    """
    if any(c in _LINK_UNSAFE for c in stem):
        text = stem.replace("[", "\\[").replace("]", "\\]")
        return "[%s](<%s.md>)" % (text, stem)
    return "[[%s]]" % stem


def _series_entries(sdir):
    """某系列下的视频笔记 → [{stem, duration, created}]。"""
    out = []
    for p in sdir.glob("*.md"):
        if not _is_video_note(p):
            continue
        note = parse_note(p)
        if not note:
            continue
        created = (note.get("created") or "")[:10] or "未知日期"
        out.append({"stem": p.stem, "duration": note.get("duration") or "",
                    "created": created})
    return out


def render_series_index(user, series):
    """重建 `<系列>/<系列>索引.md`。返回条数（目录不存在返回 -1）。"""
    base = user_dir(user) / VIDEO_DIR
    sdir = base / series
    if not sdir.is_dir():
        return -1
    entries = _series_entries(sdir)
    groups = {}
    for e in entries:
        groups.setdefault(e["created"], []).append(e)
    lines = [
        "---",
        "title: %s索引" % series,
        "type: index",
        "series: %s" % series,
        "updated: %s" % datetime.now().strftime("%Y-%m-%d"),
        "---",
        "",
        "# %s · 视频速览索引" % series,
        "",
        "> 共 **%d** 条 ｜ 返回 [[视频字幕总览]]" % len(entries),
        "",
    ]
    for date in sorted(groups, reverse=True):
        lines.append("## %s" % date)
        for e in sorted(groups[date], key=lambda x: x["stem"]):
            dur = (" ｜ `%s`" % e["duration"]) if e["duration"] else ""
            lines.append("- %s%s" % (_note_link(e["stem"]), dur))
        lines.append("")
    (sdir / ("%s索引.md" % series)).write_text("\n".join(lines), encoding="utf-8")
    return len(entries)


def render_overview(user):
    """重建 `视频字幕总览.md`（系列汇总表）。返回 (系列数, 总条数)。"""
    base = user_dir(user) / VIDEO_DIR
    if not base.is_dir():
        return 0, 0
    rows = []
    for sdir in sorted(p for p in base.iterdir()
                       if p.is_dir() and not p.name.startswith(".")):
        n = render_series_index(user, sdir.name)
        if n >= 0:
            rows.append((sdir.name, n))
    rows.sort(key=lambda x: (-x[1], x[0]))
    total = sum(n for _, n in rows)
    lines = [
        "---",
        "title: 视频字幕总览",
        "type: index",
        "updated: %s" % datetime.now().strftime("%Y-%m-%d"),
        "---",
        "",
        "# 视频字幕总览",
        "",
        "> 共 **%d** 条速览，%d 个系列。本页只做导航，不存内容。" % (total, len(rows)),
        "",
        "| 系列 | 条数 | 索引 |",
        "|---|---|---|",
    ]
    for name, n in rows:
        lines.append("| %s | %d | [[%s索引]] |" % (name, n, name))
    (base / "视频字幕总览.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows), total


def find_by_source(user, source):
    """同一个链接不重复花钱。

    同时比 `source`（展开后的正式地址）和 `source_raw`（用户当时粘的原始地址）：
    抖音短链会被展开成 www.douyin.com/video/<id> 再存，只比 source 的话
    第二次粘同一个短链就认不出来，会重复下载转写。
    """
    if not source:
        return None
    s = source.rstrip("/")
    for item in list_notes(user, 200):
        for key in ("source", "source_raw"):
            if (item.get(key) or "").rstrip("/") == s:
                return item
    return None


# ---------------- 任务队列（网页轮询进度） ----------------

JOBS = {}
_JOBS_LOCK = threading.Lock()
# 同时处理几个视频任务。默认 1：本机 2 核 1.9G，本地 ASR 单进程峰值约 421MB，
# 再加上 ffmpeg 切片是 CPU 密集型，并发跑只会互相拖慢并逼近 OOM。
# 想放开用 VIDEO_CONCURRENCY 覆盖，但必须先确认内存余量。
MAX_CONCURRENT = max(1, int(ENV.get("VIDEO_CONCURRENCY", "1") or 1))
_QUEUE = threading.Semaphore(MAX_CONCURRENT)
# 当前真正在跑的任务 id（区别于"在排队等信号量"的）
RUNNING = set()
# 可用内存低于这个值就先等，别让几个任务把机器撑爆（2G 内存机器默认 220MB）
MIN_FREE_MB = int(ENV.get("VIDEO_MIN_FREE_MB", "220") or 220)


# ---------------- 任务持久化（重启不丢队列） ----------------
# 为什么要有：JOBS 只在内存里，重启即蒸发。11-25 那次重启把一个跑到一半的任务
# 静默吃掉了，用户只看到队列空了、视频没进来，既没报错也没痕迹；video_work 里
# 还留下一个空目录当唯一证据。
#
# 设计取舍：
#   - 内存 JOBS 仍是唯一读写源，DB 只是镜像。读路径零开销。
#   - 写路径走脏标记 + 后台线程，因为 _set 被 ASR 每段进度回调高频调用，
#     不能每次都落盘（转写一个视频约 200 段）。
#   - 终态（done=True）立即落盘，结论不会因为进程被 kill 而丢。
#   - **不做自动续跑**：上传的源文件在 work 目录里，任务结束就被删了；重跑
#     等于重新下载 + 重新花 ASR 的钱。所以中断任务一律标失败并说明中断在哪一步。
JOBS_DB = DATA_DIR / "video_jobs.db"
_DB_LOCK = threading.Lock()
_DIRTY: set = set()
_FLUSH_EVERY = 2.0          # 秒：脏任务最多延迟这么久落盘
_KEEP_ROWS = 200            # DB 里最多保留多少条历史任务
_FLUSH_THREAD = None
_FIELDS = ("id", "user", "title", "series", "stage", "step", "total", "done",
           "err", "note", "method", "duration", "elapsed", "t", "t_end",
           "url", "file_path")


def _db():
    con = sqlite3.connect(str(JOBS_DB), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _db_init():
    with _DB_LOCK:
        con = _db()
        try:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, user TEXT, title TEXT, series TEXT,
                  stage TEXT, step INTEGER, total INTEGER, done INTEGER,
                  err TEXT, note TEXT, method TEXT, duration TEXT,
                  elapsed REAL, t REAL, t_end REAL, url TEXT, file_path TEXT,
                  updated REAL
                );
                CREATE INDEX IF NOT EXISTS jobs_by_time ON jobs(t_end);
            """)
            con.commit()
        finally:
            con.close()


def _flush(only=None):
    """把脏任务（或 only 指定的任务）从内存镜像到 DB。失败不抛，只记一行。"""
    ids = set(only or ())
    with _JOBS_LOCK:
        ids |= _DIRTY
        _DIRTY.clear()
        rows = []
        for jid in ids:
            j = JOBS.get(jid)
            if j:
                rows.append([j.get(k) for k in _FIELDS])
    if not rows:
        return
    cols = ", ".join(_FIELDS)
    marks = ", ".join("?" * len(_FIELDS))
    upd = ", ".join("%s=excluded.%s" % (f, f) for f in _FIELDS if f != "id")
    sql = ("INSERT INTO jobs (%s, updated) VALUES (%s, ?) "
           "ON CONFLICT(id) DO UPDATE SET %s, updated=excluded.updated"
           % (cols, marks, upd))
    now = time.time()
    try:
        with _DB_LOCK:
            con = _db()
            try:
                con.executemany(sql, [r + [now] for r in rows])
                # 只留最近 N 条，DB 不会无限长
                con.execute("DELETE FROM jobs WHERE id IN "
                            "(SELECT id FROM jobs ORDER BY COALESCE(t_end, t) DESC "
                            " LIMIT -1 OFFSET ?)", (_KEEP_ROWS,))
                con.commit()
            finally:
                con.close()
    except Exception as e:
        print("[video] 任务落盘失败: %s: %s" % (type(e).__name__, str(e)[:150]),
              file=sys.stderr, flush=True)


def _flusher():
    while True:
        time.sleep(_FLUSH_EVERY)
        _flush()


def _ensure_flusher():
    global _FLUSH_THREAD
    if _FLUSH_THREAD is not None:
        return
    _db_init()
    _FLUSH_THREAD = threading.Thread(target=_flusher, name="job-flusher", daemon=True)
    _FLUSH_THREAD.start()


def _mem_available_mb():
    """读 /proc/meminfo 的 MemAvailable，非 Linux 或读不到时返回大数（不拦截）。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 999999.0


def _set(jid, **kw):
    with _JOBS_LOCK:
        if jid in JOBS:
            # done 是终态，顺手补一个结束时间：前端据此判断「最近失败」，
            # 恢复出来的中断任务也靠它排到列表末尾（否则 t 是旧的，会被截掉）
            if kw.get("done") and "t_end" not in kw:
                kw["t_end"] = time.time()
            JOBS[jid].update(kw)
            _DIRTY.add(jid)
    if kw.get("done"):
        _flush({jid})      # 终态立即落盘，进程被 kill 也不会丢结论


def get_job(jid):
    with _JOBS_LOCK:
        j = JOBS.get(jid)
        return dict(j) if j else None


def list_jobs(user, done_limit=3):
    """某用户的任务快照，给前端「处理中」列表用。

    并发跑 N 个之后，光看索引会算错，所以用 RUNNING 集合标记真正在跑的：
      running=True      → 正在跑（显示具体阶段和进度条）
      running=False, queued=0 → 有槽位就会立刻开跑
      running=False, queued=N → 前面还有 N 个同样在等的任务
    末尾附带最近几个已完成的任务（含失败的），前端用来提示错误和刷新列表。
    """
    with _JOBS_LOCK:
        items = [dict(j) for j in JOBS.values() if j.get("user") == user]
        running = set(RUNNING)
    items.sort(key=lambda j: j.get("t_end") or j.get("t", 0))
    pending = [j for j in items if not j.get("done")]
    ahead = 0
    for j in pending:
        if j["id"] in running:
            j["queued"] = 0
            j["running"] = True
        else:
            j["queued"] = ahead          # 只统计"还在等"的任务，不把在跑的算进去
            j["running"] = False
            ahead += 1
    finished = [j for j in items if j.get("done")][-done_limit:]
    for j in finished:
        j["queued"] = 0
    return pending + finished


def create_job(user, title_hint="", series=""):
    """只占位不启动：上传大文件时先把 job 建好，落盘完再 launch。"""
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        if len(JOBS) > 50:
            for k in sorted(JOBS, key=lambda k: JOBS[k].get("t", 0))[:20]:
                JOBS.pop(k, None)
        JOBS[jid] = {"id": jid, "user": user, "stage": "准备中", "step": 0, "total": 0,
                     "done": False, "err": None, "note": None, "title": title_hint or "",
                     "series": series or "", "method": "", "duration": "", "elapsed": 0,
                     "t": datetime.now().timestamp(), "t_end": 0.0,
                     "url": "", "file_path": ""}
    _ensure_flusher()
    _flush({jid})     # 创建即落盘，重启后至少能知道「有过这么个任务」
    return jid


def launch(jid, url=None, file_path=None):
    # 记下来源。以前 JOBS 里只有标题，重启后根本说不清是哪个视频丢的；
    # 也只有存了 url 以后才可能做「重试」。
    _set(jid, url=url or "", file_path=str(file_path) if file_path else "")
    threading.Thread(target=_run, args=(jid, url, file_path), daemon=True).start()
    return jid


def start_job(user, url=None, file_path=None, title_hint=None, series=""):
    jid = create_job(user, title_hint or "", series or "")
    return launch(jid, url, file_path)


def job_workdir(jid):
    return WORK_ROOT / jid


def _row_to_job(r):
    """DB 行（dict）→ 内存 job，字段与 create_job 保持一致。"""
    return {
        "id": r["id"], "user": r.get("user"), "title": r.get("title") or "",
        "series": r.get("series") or "", "stage": r.get("stage") or "",
        "step": r.get("step") or 0, "total": r.get("total") or 0,
        "done": bool(r.get("done")), "err": r.get("err"), "note": r.get("note"),
        "method": r.get("method") or "", "duration": r.get("duration") or "",
        "elapsed": r.get("elapsed") or 0, "t": r.get("t") or 0,
        "t_end": r.get("t_end") or 0, "url": r.get("url") or "",
        "file_path": r.get("file_path") or "",
    }


def recover_jobs():
    """服务启动时调一次：把上次没跑完的任务标失败，清掉残留工作目录。

    返回被标记的任务数。

    **只能在进程启动阶段调用**（web.py 的 lifespan）。运行期间调会误标正在跑
    的任务、误删它们的工作目录。

    为什么标失败而不是自动续跑：
      - 上传的源文件在 work 目录里，_run 的 finally 会整个删掉，重启后文件没了
      - 即使 URL 任务能重下，ASR 那笔钱也已经花过，重跑等于再花一次
      - 一个「明确失败 + 中断在哪一步」的记录，比一个悄悄重跑、进度倒退的任务有用
    """
    _db_init()
    now = time.time()
    try:
        with _DB_LOCK:
            con = _db()
            try:
                rows = [dict(r) for r in
                        con.execute("SELECT * FROM jobs WHERE done=0 ORDER BY t").fetchall()]
            finally:
                con.close()
    except Exception as e:
        print("[video] 读取历史任务失败，跳过恢复: %s: %s"
              % (type(e).__name__, str(e)[:150]), file=sys.stderr, flush=True)
        rows = []

    revived = []
    for r in rows:
        old_stage = (r.get("stage") or "").strip() or "准备中"
        j = _row_to_job(r)
        j.update({
            "stage": "失败",
            "done": True,
            "err": "服务重启中断（中断时：%s）" % old_stage,
            "elapsed": round(now - (r.get("t") or now), 1),
            "t_end": now,      # 排到列表末尾，重启后第一眼就能看到这条
        })
        revived.append(j)
    if revived:
        with _JOBS_LOCK:
            for j in revived:
                JOBS[j["id"]] = j
                _DIRTY.add(j["id"])
        _flush()               # 失败态立刻写回，下回启动不会重复标

    # 把最近 10 分钟内的失败记录也装回内存。否则会出现这种死角：
    # 「中断 → 重启（内存里有）→ 用户还没看 → 又重启一次」，
    # 第二次启动时 DB 里已经没有 done=0 了，提示就永远消失了。
    # 只捞失败的、且限制时间窗，不会把历史成功任务灌进来刷屏。
    restored = 0
    try:
        with _DB_LOCK:
            con = _db()
            try:
                recent = [dict(r) for r in con.execute(
                    "SELECT * FROM jobs WHERE err IS NOT NULL AND t_end > ? "
                    "ORDER BY t_end DESC LIMIT 5", (now - 600,)).fetchall()]
            finally:
                con.close()
    except Exception:
        recent = []
    with _JOBS_LOCK:
        for r in recent:
            if r["id"] in JOBS:
                continue
            JOBS[r["id"]] = _row_to_job(r)
            restored += 1

    # 清孤儿工作目录：11-25 那次重启后 video_work 里一直挂着个空目录，就是它。
    # RUNNING 为空说明是启动阶段，可以安全全清；非空（误调）则跳过活跃任务。
    removed = 0
    with _JOBS_LOCK:
        alive = set(RUNNING)
    try:
        if WORK_ROOT.exists():
            for d in WORK_ROOT.iterdir():
                if d.is_dir() and d.name not in alive:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
    except Exception:
        pass

    if revived or restored or removed:
        print("[video] 启动恢复：%d 个中断任务标记失败，%d 条近期失败记录已载入，"
              "清理 %d 个残留工作目录" % (len(revived), restored, removed),
              file=sys.stderr, flush=True)
    return len(revived)


def _run(jid, url, file_path):
    t0 = datetime.now()
    work = WORK_ROOT / jid
    try:
        with _QUEUE:
            with _JOBS_LOCK:
                RUNNING.add(jid)
            _set(jid, stage="准备中")
            # 内存闸门：2G 小机器同时跑 3 个任务时，先把可用内存等出来再开工，
            # 宁可慢一点也不要被 OOM 杀掉整个服务（杀掉会连排队任务一起没）。
            while _mem_available_mb() < MIN_FREE_MB:
                _set(jid, stage="等待内存（当前可用 %.0f MB）…" % _mem_available_mb())
                time.sleep(5)
            work.mkdir(parents=True, exist_ok=True)
            job = get_job(jid) or {}
            user = job.get("user")

            pairs, method, meta = [], "", {}
            if url:
                ids = None
                if "bilibili.com" in url or "b23.tv" in url:
                    _set(jid, stage="解析 B 站视频")
                    bid, page = bili_bvid(url)
                    if bid:
                        ids = (bid, page)
                if ids:
                    meta = bili_info(ids)
                    _set(jid, title=meta["title"])
                    if meta["duration"] > MAX_MINUTES * 60:
                        raise RuntimeError("视频 %.0f 分钟，超过上限 %d 分钟"
                                           % (meta["duration"] / 60, MAX_MINUTES))
                    _set(jid, stage="找平台字幕")
                    pairs = bili_subs(meta)
                    if pairs:
                        method = "平台字幕"
                    else:
                        _set(jid, stage="下载音频")
                        src = bili_audio(meta, work)
                elif is_douyin(url):
                    _set(jid, stage="下载抖音视频")
                    meta, src = douyin_info_and_audio(url, work)
                    _set(jid, title=meta["title"])
                    if meta["duration"] > MAX_MINUTES * 60:
                        raise RuntimeError("视频 %.0f 分钟，超过上限 %d 分钟"
                                           % (meta["duration"] / 60, MAX_MINUTES))
                else:
                    _set(jid, stage="解析链接")
                    meta = ytdlp_info(url)
                    _set(jid, title=meta["title"])
                    if meta["duration"] > MAX_MINUTES * 60:
                        raise RuntimeError("视频 %.0f 分钟，超过上限 %d 分钟"
                                           % (meta["duration"] / 60, MAX_MINUTES))
                    _set(jid, stage="找平台字幕")
                    pairs = ytdlp_subs(url, work / "subs")
                    if pairs:
                        method = "平台字幕"
                    else:
                        _set(jid, stage="下载音频")
                        src = ytdlp_audio(url, work)
            else:
                src = Path(file_path)
                meta = {"title": src.stem, "duration": _duration_of(src),
                        "platform": "本地文件", "source": ""}
                _set(jid, title=meta["title"])

            # 记下用户当时粘的原始地址：抖音短链会被展开，只存展开后的地址会导致二次去重失效
            if url:
                meta["source_raw"] = url

            if not pairs:
                _set(jid, stage="转写音频", step=0, total=0)
                pairs = transcribe_media(
                    src, work,
                    on_progress=lambda i, n: _set(jid, step=i, total=n,
                                                  stage="转写中 %d/%d 段" % (i, n)))
                # 标签取「实际用过的通道」，而不是配置值：回落时说清楚到底走的哪一层，
                # 这样看笔记就能知道这次有没有花钱
                method = "%s ASR 转写" % last_channel_label()

            _set(jid, stage="生成速览")
            summary = summarize(pairs)
            _set(jid, stage="写入笔记")
            path = save_note(user, meta, pairs, summary, method, job.get("series") or "")
            rel = str(path.relative_to(user_dir(user))).replace("\\", "/")
            _set(jid, stage="完成", done=True, note=rel, method=method,
                 title=meta["title"], duration=_ts(meta.get("duration") or 0),
                 elapsed=round((datetime.now() - t0).total_seconds(), 1))
            # 索引页跟着刷新。以前这两份索引靠手动重建，11-25 批量导入后姜胡说索引
            # 就一直停在 205/466。刷新失败不能让任务判失败，所以单独吞掉异常。
            try:
                render_overview(user)
            except Exception:
                pass
    except Exception as e:
        _set(jid, stage="失败", done=True, err=str(e)[:300],
             elapsed=round((datetime.now() - t0).total_seconds(), 1))
    finally:
        with _JOBS_LOCK:
            RUNNING.discard(jid)      # 不管成功失败，都要从"正在跑"里摘掉
        release_sensevoice()          # 任务结束释放本地模型，别让它常驻压着 cgroup 上限
        shutil.rmtree(work, ignore_errors=True)
