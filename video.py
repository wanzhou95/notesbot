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
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
CHUNK_SEC = 28
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


def asr_file(path, prev_text=""):
    """单段音频 → 文字（GLM-ASR，单次 ≤30 秒 / 25MB）。"""
    key = ENV.get("GLM_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 GLM_API_KEY，无法转写")
    url = ENV.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/") \
        + "/audio/transcriptions"
    data = {"model": ASR_MODEL, "stream": "false"}
    if prev_text:
        data["prompt"] = prev_text[-600:]      # 上一段当上下文，术语/人名更连贯
    with open(path, "rb") as f:
        r = httpx.post(url, headers={"Authorization": "Bearer " + key},
                       data=data, files={"file": (path.name, f, "audio/wav")},
                       timeout=120)
    if r.status_code != 200:
        raise RuntimeError("ASR HTTP %s %s" % (r.status_code, r.text[:160]))
    d = r.json()
    text = (d.get("text") or "").strip()
    if not text:
        try:
            text = (d["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            text = ""
    return re.sub(r"\s+", " ", text)


def transcribe_media(path, workdir, on_progress=None):
    """本地媒体文件（或已下好的音频）→ [(秒, 文本)]。"""
    d = _duration_of(path)
    if d and d > MAX_MINUTES * 60:
        raise RuntimeError("视频 %.0f 分钟，超过上限 %d 分钟（放开就改 VIDEO_MAX_MIN）"
                           % (d / 60, MAX_MINUTES))
    chunks = slice_audio(path, workdir / "chunks")
    if not chunks:
        raise RuntimeError("没切出音频片段（文件可能只有画面/已损坏）")
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
            except Exception as e:
                if attempt == 1:
                    fails += 1
                    if fails >= 5 and i < 3:
                        raise RuntimeError("ASR 连续失败：%s" % e)
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
_QUEUE = threading.Semaphore(1)          # 一次只跑一个，避免并发烧钱


def _set(jid, **kw):
    with _JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)


def get_job(jid):
    with _JOBS_LOCK:
        j = JOBS.get(jid)
        return dict(j) if j else None


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
                     "t": datetime.now().timestamp()}
    return jid


def launch(jid, url=None, file_path=None):
    threading.Thread(target=_run, args=(jid, url, file_path), daemon=True).start()
    return jid


def start_job(user, url=None, file_path=None, title_hint=None, series=""):
    jid = create_job(user, title_hint or "", series or "")
    return launch(jid, url, file_path)


def job_workdir(jid):
    return WORK_ROOT / jid


def _run(jid, url, file_path):
    t0 = datetime.now()
    work = WORK_ROOT / jid
    try:
        with _QUEUE:
            _set(jid, stage="准备中")
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
                method = "GLM-ASR 转写"

            _set(jid, stage="生成速览")
            summary = summarize(pairs)
            _set(jid, stage="写入笔记")
            path = save_note(user, meta, pairs, summary, method, job.get("series") or "")
            rel = str(path.relative_to(user_dir(user))).replace("\\", "/")
            _set(jid, stage="完成", done=True, note=rel, method=method,
                 title=meta["title"], duration=_ts(meta.get("duration") or 0),
                 elapsed=round((datetime.now() - t0).total_seconds(), 1))
    except Exception as e:
        _set(jid, stage="失败", done=True, err=str(e)[:300],
             elapsed=round((datetime.now() - t0).total_seconds(), 1))
    finally:
        shutil.rmtree(work, ignore_errors=True)
