import json
import time

import httpx

from config import ENV, DATA_DIR

# 模型开关设置（设置页修改，落盘立即生效，无需重启服务）
SETTINGS_FILE = DATA_DIR / "model_settings.json"
# 模型调用日志（每次成功调用记一行：时间/厂商/模型/档位），供回溯"这次用的哪个模型"
USAGE_LOG = DATA_DIR / "model_usage.log"
# 失败日志（每次降级记一行：时间/厂商/模型/档位/原因），用于回答"为什么落到某个模型"
FAIL_LOG = DATA_DIR / "model_fail.log"


def log_usage(prov, model, tier=""):
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), prov, model, tier))
    except Exception:
        pass


def log_fail(prov, model, tier, err):
    """记一次失败，便于回溯降级原因（限流 / 无正文 / HTTP 4xx 等）。"""
    try:
        with open(FAIL_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), prov, model, tier, str(err)[:160]))
    except Exception:
        pass


def load_model_settings():
    """读模型开关；premium 里没写的模型默认开。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {
        "deepseek": bool(data.get("deepseek", True)),
        "premium": dict(data.get("premium") or {}),
        # 定时归纳的两个勾选模型（默认都开）；glm-4.7-flash 始终兜底，不落配置
        "digest_ds": bool(data.get("digest_ds", True)),
        "digest_glm53": bool(data.get("digest_glm53", True)),
    }


def save_model_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def digest_model_order(router):
    """定时归纳的调用顺序：勾选的模型按固定顺序（deepseek-v4-pro → glm-5.3-flash），
    最后始终以 glm-4.7-flash 兜底（MODEL_GLM_FINAL，默认值），不随勾选、
    也不随 MODEL_FALLBACK 顺序变化。"""
    s = load_model_settings()
    order = []
    if s["digest_ds"]:
        order.append(("ds", router.main_model))            # 默认 deepseek-v4-pro
    if s["digest_glm53"]:
        order.append(("glm", "glm-5.3-flash"))
    # 兜底固定 4.7：以前取 glm_models[0]，把 5.3 提到最前后会变成重复且失去 4.7 兜底
    if router.glm_final and all(m != router.glm_final for _, m in order):
        order.append(("glm", router.glm_final))
    return order


class LLMRouter:
    """DeepSeek 优先，失败自动降级到 GLM（智谱免费模型）。"""

    def __init__(self):
        self.ds_key = ENV.get("DEEPSEEK_API_KEY", "")
        self.ds_url = ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.glm_key = ENV.get("GLM_API_KEY", "")
        self.glm_url = ENV.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        self.router_model = ENV.get("MODEL_ROUTER", "deepseek-flash")
        self.main_model = ENV.get("MODEL_MAIN", "deepseek-v4-pro")
        self.glm_models = [m.strip() for m in ENV.get("MODEL_FALLBACK", "glm-4.7-flash").split(",") if m.strip()]
        # 定时归纳的最终兜底，独立于 MODEL_FALLBACK 顺序：
        # 即使以后把 glm-5.3-flash 排到最前面，「4.7 兜底」这条规则也不受影响。
        self.glm_final = ENV.get("MODEL_GLM_FINAL", "glm-4.7-flash")
        self.disable_thinking = ENV.get("GLM_DISABLE_THINKING", "1") == "1"
        self.always_think = {"glm-5.3-flash"}  # 始终思考模型，不支持关闭，传了直接 400
        # 思考模型的最小 token 保底：max_tokens 太小时，思考会把额度吃光导致正文为空
        # （实测 glm-5.3-flash 给 30 时 content=""、finish_reason=length），
        # 会被误判成失败并降级。所以对思考模型强制拉到这个下限。
        self.think_min_tokens = int(ENV.get("MODEL_THINK_MIN_TOKENS", "300"))
        # 截断重试：思考模型偶尔会把预算耗在思考上，正文写到一半就 finish_reason=length。
        # 这种内容非空但残缺，原来会静默返回半句话（例如笔记的「一句话总结」被截断）。
        # 对 max_tokens ≥ 该阈值的调用，翻倍重试一次；短调用（如 30 token 分类）不重试。
        self.length_retry_min = int(ENV.get("MODEL_LENGTH_RETRY_MIN", "400"))
        self.length_retry_cap = int(ENV.get("MODEL_LENGTH_RETRY_CAP", "8000"))
        self.client = httpx.Client(timeout=120)

    def _call(self, base, key, model, messages, max_tokens, glm=False):
        # 思考模型（glm-5.3-flash）不支持关思考，且思考内容同样计入 max_tokens。
        # 上限给小了会出现「HTTP 200 但正文为空」，被上层当成失败降级，
        # 结果是配置里排第一的 5.3 永远轮不到。这里统一保底。
        if glm and model in self.always_think and max_tokens < self.think_min_tokens:
            max_tokens = self.think_min_tokens
        retried = False
        while True:
            body = {"model": model, "messages": messages, "max_tokens": max_tokens}
            if glm and self.disable_thinking and model not in self.always_think:
                body["thinking"] = {"type": "disabled"}
            r = self.client.post(
                base + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            )
            if r.status_code != 200:
                raise RuntimeError("HTTP %s %s" % (r.status_code, r.text[:100]))
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            finish = choice.get("finish_reason")
            if not content:
                # 绝不能把 reasoning_content 当正文返回：那是模型的思考过程，
                # 混进结果会污染笔记/技能卡。宁可抛错让降级链换下一个模型。
                raise RuntimeError("模型无正文输出（finish_reason=%s，通常是想太多把 token 用光了）"
                                   % finish)
            # 正文非空但被截断：多半是思考吃掉了预算，翻倍再来一次。
            # 短调用（归类只给 30 token）不在此列——那里的截断属于正常行为。
            if (finish == "length" and not retried
                    and max_tokens >= self.length_retry_min
                    and max_tokens < self.length_retry_cap):
                retried = True
                new_max = min(max_tokens * 2, self.length_retry_cap)
                log_fail("glm" if glm else "ds", model, "truncated",
                         "输出被截断（正文 %d 字），max_tokens %d → %d 重试一次"
                         % (len(content), max_tokens, new_max))
                max_tokens = new_max
                continue
            return content

    def chat_messages(self, messages, tier="main", max_tokens=1500, models=None):
        # models: 前置的付费旗舰模型（按优先级），全部失败后回落标准链路
        s = load_model_settings()
        order = [("glm", m) for m in (models or [])]
        if s["deepseek"]:  # 设置页关掉 DeepSeek 后，付费通道直接跳过
            order += [("ds", self.main_model if tier == "main" else self.router_model)]
        order += [("glm", m) for m in self.glm_models]

        last_err = None
        for prov, model in order:
            try:
                if prov == "ds":
                    if not self.ds_key:
                        continue
                    out = self._call(self.ds_url, self.ds_key, model, messages, max_tokens)
                else:
                    if not self.glm_key:
                        continue
                    out = self._call(self.glm_url, self.glm_key, model, messages, max_tokens, glm=True)
                log_usage(prov, model, tier)  # 记录本次实际用的模型，便于回溯
                return out
            except Exception as e:
                last_err = e
                log_fail(prov, model, tier, e)  # 记下为什么降级
                continue
        return "[模型调用失败] %s" % last_err

    def chat(self, prompt, system=None, tier="router", max_tokens=800):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_messages(messages, tier=tier, max_tokens=max_tokens)

    def chat_ordered(self, prompt, order, max_tokens=4000, system=None):
        """按明确顺序尝试 order=[(provider, model), ...]，provider ∈ {ds, glm}。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        last_err = None
        for prov, model in order:
            try:
                if prov == "ds":
                    if not self.ds_key:
                        continue
                    out = self._call(self.ds_url, self.ds_key, model, messages, max_tokens)
                else:
                    if not self.glm_key:
                        continue
                    out = self._call(self.glm_url, self.glm_key, model, messages, max_tokens, glm=True)
                log_usage(prov, model, "digest")
                return out
            except Exception as e:
                last_err = e
                log_fail(prov, model, "digest", e)
                continue
        return "[模型调用失败] %s" % last_err

    def vision(self, image_data_url, prompt, max_tokens=1500):
        """视觉识别（智谱免费视觉模型），image_data_url 为 base64 data URL。"""
        # 默认顺序：plus 识别更干净（flash 会把状态栏/按钮字一起认进来）；
        # flash 留作兜底。顺序可用 MODEL_VISION 覆盖。
        candidates = [m.strip() for m in ENV.get(
            "MODEL_VISION", "glm-4v-plus,glm-4v-flash,glm-4.6v-flash").split(",") if m.strip()]
        # glm-4v 系列 max_tokens 上限 1024，给 1500 会直接 HTTP 400 (code 1210)，
        # 导致首选模型每次被跳过、白等一轮。这里统一夹住。
        max_tokens = min(max_tokens, 1024)
        if not self.glm_key:
            return "[视觉模型未配置] 缺少 GLM_API_KEY"
        last_err = None
        for model in candidates:
            try:
                r = self.client.post(
                    self.glm_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.glm_key},
                    json={
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_data_url}},
                                {"type": "text", "text": prompt},
                            ],
                        }],
                        "max_tokens": max_tokens,
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    log_usage("glm", model, "vision")  # 视觉链也记一笔，回答“OCR 用了哪个模型”
                    return data["choices"][0]["message"].get("content") or ""
                last_err = "HTTP %s %s" % (r.status_code, r.text[:120])
                log_fail("glm", model, "vision", last_err)
            except Exception as e:
                last_err = e
                log_fail("glm", model, "vision", e)
                continue
        return "[视觉识别失败] %s" % last_err
