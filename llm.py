import httpx
from config import ENV


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
        self.disable_thinking = ENV.get("GLM_DISABLE_THINKING", "1") == "1"
        self.always_think = {"glm-5.3-flash"}  # 始终思考模型，不支持关闭，传了直接 400
        self.client = httpx.Client(timeout=120)

    def _call(self, base, key, model, messages, max_tokens, glm=False):
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
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        if not content:
            content = msg.get("reasoning_content") or ""
        return content

    def chat_messages(self, messages, tier="main", max_tokens=1500, models=None):
        # models: 前置的付费旗舰模型（按优先级），全部失败后回落标准链路
        order = [("glm", m) for m in (models or [])]
        order += [("ds", self.main_model if tier == "main" else self.router_model)]
        order += [("glm", m) for m in self.glm_models]

        last_err = None
        for prov, model in order:
            try:
                if prov == "ds":
                    if not self.ds_key:
                        continue
                    return self._call(self.ds_url, self.ds_key, model, messages, max_tokens)
                else:
                    if not self.glm_key:
                        continue
                    return self._call(self.glm_url, self.glm_key, model, messages, max_tokens, glm=True)
            except Exception as e:
                last_err = e
                continue
        return "[模型调用失败] %s" % last_err

    def chat(self, prompt, system=None, tier="router", max_tokens=800):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_messages(messages, tier=tier, max_tokens=max_tokens)

    def vision(self, image_data_url, prompt, max_tokens=1500):
        """视觉识别（智谱免费视觉模型），image_data_url 为 base64 data URL。"""
        candidates = [m.strip() for m in ENV.get("MODEL_VISION", "glm-4v-flash,glm-4v-plus,glm-4.6v-flash").split(",") if m.strip()]
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
                    return data["choices"][0]["message"].get("content") or ""
                last_err = "HTTP %s %s" % (r.status_code, r.text[:120])
            except Exception as e:
                last_err = e
                continue
        return "[视觉识别失败] %s" % last_err
