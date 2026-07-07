import asyncio
import json
import base64
import mimetypes
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, At
from astrbot.api import logger

# ---------- 辅助包装类（兼容 context.send_message） ----------
class _MessageWrapper:
    def __init__(self, chain):
        self.chain = chain


# ---------- 插件主类 ----------
@register("astrbot_plugin_timer_image", "YHJM", "定时发送图片插件（渲染+API双模式）", "2.0.0")
class TimerImagePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.tasks = self.config.get("tasks", [])
        self.debug = self.config.get("debug_mode", False)

        # 初始化数据目录（仅用于渲染模式的背景图）
        self._init_paths()

        # 记录所有定时协程
        self._task_coros = []

        # 启动所有定时任务
        for idx, task_cfg in enumerate(self.tasks):
            t = asyncio.create_task(self._run_scheduler(idx, task_cfg))
            self._task_coros.append(t)

        logger.info(f"[TimerImage] 已加载 {len(self.tasks)} 个任务")

    # ---------- 初始化目录 ----------
    def _init_paths(self):
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        path = get_astrbot_data_path()
        self.data_dir = (Path(path) if isinstance(path, str) else path) / "plugin_data" / self.name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backgrounds_dir = self.data_dir / "backgrounds"
        self.backgrounds_dir.mkdir(exist_ok=True)
        self._log(f"数据目录: {self.data_dir}")

    # ---------- 日志辅助 ----------
    def _log(self, msg: str, level: str = "info"):
        if self.debug or level != "debug":
            getattr(logger, level)(f"[TimerImage] {msg}")

    # ---------- 背景图解析（仅渲染模式使用） ----------
    def resolve_background(self, user_input: str) -> str:
        if not user_input:
            return ""
        if user_input.startswith(("http://", "https://")):
            return user_input
        local_path = self.backgrounds_dir / user_input
        if not local_path.exists():
            self._log(f"背景图不存在: {user_input}", "warning")
            return ""
        try:
            mime_type, _ = mimetypes.guess_type(str(local_path))
            mime_type = mime_type or "image/png"
            with open(local_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            return f"data:{mime_type};base64,{data}"
        except Exception as e:
            self._log(f"读取背景图失败: {e}", "error")
            return ""

    # ---------- LLM 生成（仅渲染模式使用） ----------
    async def _generate_text(self, prompt: str) -> str:
        use_llm = self.config.get("use_llm", False)
        if not use_llm:
            return ""

        api_base = self.config.get("api_base", "https://api.deepseek.com/v1")
        api_key = self.config.get("api_key", "")
        model = self.config.get("model", "deepseek-v4-flash")
        system_prompt = self.config.get("system_prompt", "请用中文回复。")

        if not api_key:
            self._log("LLM 未配置 api_key", "error")
            return ""

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 1024,
            "temperature": 0.8
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{api_base}/chat/completions", json=payload, headers=headers, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            self._log(f"LLM 调用失败: {e}", "error")
        return ""

    # ---------- 渲染模式核心：生成图片 URL ----------
    async def _render_image_from_template(self, task_cfg: Dict[str, Any]) -> Optional[str]:
        template = task_cfg.get("template", "")
        if not template:
            self._log("渲染模式缺少 template", "error")
            return None

        # 1. LLM 生成内容（若任务或全局启用）
        content = ""
        if task_cfg.get("use_llm", False) or self.config.get("use_llm", False):
            prompt = task_cfg.get("prompt", "生成一段温馨的每日问候语")
            generated = await self._generate_text(prompt)
            if generated:
                content = generated
            else:
                self._log("LLM 生成失败，使用空内容", "warning")
        # 替换 {{content}}
        html = template.replace("{{content}}", content)

        # 2. 背景图
        bg_input = task_cfg.get("background", "")
        bg_data = self.resolve_background(bg_input) if bg_input else ""
        if bg_data:
            html = html.replace("{{background}}", bg_data)

        # 3. 渲染
        try:
            image_url = await self.html_render(html, {"full_page": True})
            self._log("渲染成功", "debug")
            return image_url
        except Exception as e:
            self._log(f"渲染失败: {e}", "error")
            return None

    # ---------- API 模式核心：从 API 获取图片 URL 或二进制 ----------
    async def _fetch_image_from_api(self, task_cfg: Dict[str, Any]) -> Optional[bytes]:
        """返回图片二进制数据（供 fromBytes）或 None；若想用 URL 也可自行修改"""
        api_url = task_cfg.get("api_url")
        if not api_url:
            self._log("API 模式缺少 api_url", "error")
            return None

        method = task_cfg.get("api_method", "GET")
        headers = task_cfg.get("api_headers", {})
        params = task_cfg.get("api_params", {})
        response_type = task_cfg.get("api_response_type", "json")  # "json" 或 "binary"
        image_key = task_cfg.get("api_image_key", "")  # 用于 json 提取，如 "data[0].urls.original"

        try:
            async with aiohttp.ClientSession() as sess:
                if method.upper() == "GET":
                    async with sess.get(api_url, headers=headers, params=params, timeout=30) as resp:
                        if resp.status != 200:
                            self._log(f"API 请求失败 {resp.status}", "error")
                            return None
                        if response_type == "json":
                            data = await resp.json()
                            # 解析图片 URL
                            if image_key:
                                # 简单支持点号路径（如 "data.0.urls.original"），更复杂的可用 jsonpath，这里简化为逐级访问
                                keys = image_key.split('.')
                                val = data
                                for k in keys:
                                    if isinstance(val, list):
                                        try:
                                            idx = int(k)
                                            val = val[idx] if idx < len(val) else None
                                        except:
                                            val = None
                                            break
                                    elif isinstance(val, dict):
                                        val = val.get(k)
                                    else:
                                        val = None
                                        break
                                if val and isinstance(val, str) and val.startswith("http"):
                                    # 得到图片 URL，需下载成二进制
                                    async with sess.get(val) as img_resp:
                                        if img_resp.status == 200:
                                            return await img_resp.read()
                            else:
                                # 如果没指定 key，假设 data 本身就是 URL 字符串
                                if isinstance(data, str) and data.startswith("http"):
                                    async with sess.get(data) as img_resp:
                                        if img_resp.status == 200:
                                            return await img_resp.read()
                                else:
                                    self._log("无法从 JSON 提取图片 URL", "error")
                        else:  # binary
                            return await resp.read()
        except Exception as e:
            self._log(f"API 请求异常: {e}", "error")
        return None

    # ---------- 执行单个任务（根据 mode 分流） ----------
    async def _execute_task(self, task_cfg: Dict[str, Any]):
        group_id = task_cfg.get("group_id")
        if not group_id:
            self._log("任务缺少 group_id，跳过", "error")
            return

        # 确定模式：显式指定 mode，或根据 template 自动推断
        mode = task_cfg.get("mode")
        if not mode:
            mode = "render" if task_cfg.get("template") else "api"
        self._log(f"任务模式: {mode}")

        image_data_or_url = None  # 统一处理，可能是 bytes 或 str URL
        if mode == "render":
            image_url = await self._render_image_from_template(task_cfg)
            if not image_url:
                return
            # 渲染返回的是 URL（可能是 base64 或临时文件），可直接用 fromURL
            # 为统一，我们直接用 fromURL 发送
            msg_chain = [Image.fromURL(image_url)]
        else:  # api 模式
            img_bytes = await self._fetch_image_from_api(task_cfg)
            if not img_bytes:
                self._log("API 获取图片失败", "error")
                return
            msg_chain = [Image.fromBytes(img_bytes)]

        # 附加 @ 全员
        if task_cfg.get("at_all", False):
            msg_chain.insert(0, At(qq="all"))

        # 发送
        try:
            wrapper = _MessageWrapper(msg_chain)
            await self.context.send_message(group_id, wrapper)
            self._log(f"图片已发送至 {group_id}")
        except Exception as e:
            self._log(f"发送失败: {e}", "error")

    # ---------- 单个任务的调度循环（不变） ----------
    async def _run_scheduler(self, idx: int, task_cfg: Dict[str, Any]):
        time_str = task_cfg.get("time", "")
        if not time_str:
            self._log(f"任务 {idx} 缺少 'time' 字段，跳过", "error")
            return

        try:
            h, m = map(int, time_str.split(":"))
        except:
            self._log(f"任务 {idx} 时间格式错误: {time_str}", "error")
            return

        while True:
            now = datetime.now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            self._log(f"任务 {idx} 下次触发: {target.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds:.0f}s)")

            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                self._log(f"任务 {idx} 已取消")
                break

            self._log(f"执行任务 {idx} at {time_str}")
            await self._execute_task(task_cfg)

    # ---------- 终止清理 ----------
    async def terminate(self):
        self._log("正在终止所有定时任务...")
        for t in self._task_coros:
            t.cancel()
        if self._task_coros:
            await asyncio.gather(*self._task_coros, return_exceptions=True)
        self._log("所有任务已终止")

    # ---------- 热重载命令 ----------
    @filter.command("timerimage_reload")
    async def reload_cmd(self, event):
        admins = self.context.get_config().get("admins_id", [])
        if str(event.get_sender_id()) not in admins:
            yield event.plain_result("权限不足")
            return

        await self.terminate()
        # 重新加载配置
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        cf = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "config.json"
        if cf.exists():
            self.config = json.loads(cf.read_text(encoding="utf-8"))
        else:
            self.config = {}
        self.tasks = self.config.get("tasks", [])
        self.debug = self.config.get("debug_mode", False)
        # 重启任务
        self._task_coros = []
        for idx, task_cfg in enumerate(self.tasks):
            t = asyncio.create_task(self._run_scheduler(idx, task_cfg))
            self._task_coros.append(t)
        yield event.plain_result(f"已重载，当前 {len(self.tasks)} 个任务")
