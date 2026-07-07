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
from astrbot.api.event import filter
from astrbot.api import logger

class _MessageWrapper:
    def __init__(self, chain):
        self.chain = chain

@register("astrbot_plugin_timer_image", "YHJM", "定时发送图片插件（渲染+API双模式）", "1.0.0")
class TimerImagePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.tasks = self.config.get("tasks", [])
        self.debug = self.config.get("debug_mode", False)

        self._init_paths()
        self._my_tasks = []
        for i, task in enumerate(self.tasks):
            t = asyncio.create_task(self._run_task(i, task))
            self._my_tasks.append(t)

        logger.info(f"[TimerImage] 已加载 {len(self.tasks)} 个任务")

    def _init_paths(self):
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        path = get_astrbot_data_path()
        self.data_dir = (Path(path) if isinstance(path, str) else path) / "plugin_data" / self.name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backgrounds_dir = self.data_dir / "backgrounds"
        self.backgrounds_dir.mkdir(exist_ok=True)

    def _log(self, msg: str, level: str = "info"):
        if self.debug or level != "debug":
            getattr(logger, level)(f"[TimerImage] {msg}")

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

    async def _generate_text(self, prompt: str) -> str:
        if not self.config.get("use_llm", False):
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

    async def _render_image_from_template(self, task_cfg: Dict[str, Any]) -> Optional[str]:
        template = task_cfg.get("template", "")
        if not template:
            self._log("渲染模式缺少 template", "error")
            return None
        content = ""
        if task_cfg.get("use_llm", False) or self.config.get("use_llm", False):
            prompt = task_cfg.get("prompt", "生成一段温馨的每日问候语")
            generated = await self._generate_text(prompt)
            if generated:
                content = generated
        html = template.replace("{{content}}", content)
        bg_input = task_cfg.get("background", "")
        bg_data = self.resolve_background(bg_input) if bg_input else ""
        if bg_data:
            html = html.replace("{{background}}", bg_data)
        try:
            image_url = await self.html_render(html, {"full_page": True})
            return image_url
        except Exception as e:
            self._log(f"渲染失败: {e}", "error")
            return None

    async def _fetch_image_from_api(self, task_cfg: Dict[str, Any]) -> Optional[bytes]:
        api_url = task_cfg.get("api_url")
        if not api_url:
            self._log("API 模式缺少 api_url", "error")
            return None
        method = task_cfg.get("api_method", "GET")
        headers = task_cfg.get("api_headers", {})
        params = task_cfg.get("api_params", {})
        response_type = task_cfg.get("api_response_type", "json")
        image_key = task_cfg.get("api_image_key", "")
        try:
            async with aiohttp.ClientSession() as sess:
                if method.upper() == "GET":
                    async with sess.get(api_url, headers=headers, params=params, timeout=30) as resp:
                        if resp.status != 200:
                            self._log(f"API 请求失败 {resp.status}", "error")
                            return None
                        if response_type == "json":
                            data = await resp.json()
                            if image_key:
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
                                    async with sess.get(val) as img_resp:
                                        if img_resp.status == 200:
                                            return await img_resp.read()
                            else:
                                if isinstance(data, str) and data.startswith("http"):
                                    async with sess.get(data) as img_resp:
                                        if img_resp.status == 200:
                                            return await img_resp.read()
                                else:
                                    self._log("无法从 JSON 提取图片 URL", "error")
                        else:
                            return await resp.read()
        except Exception as e:
            self._log(f"API 请求异常: {e}", "error")
        return None

    async def _execute_task(self, task: Dict[str, Any]):
        umo = task.get("umo", "")
        if not umo:
            self._log("任务缺少 umo", "error")
            return

        mode = task.get("mode")
        if not mode:
            mode = "render" if task.get("template") else "api"
        self._log(f"任务模式: {mode}")

        if mode == "render":
            image_url = await self._render_image_from_template(task)
            if not image_url:
                return
            msg_chain = [Image.fromURL(image_url)]
        else:
            img_bytes = await self._fetch_image_from_api(task)
            if not img_bytes:
                self._log("API 获取图片失败", "error")
                return
            msg_chain = [Image.fromBytes(img_bytes)]

        if task.get("at_all", False):
            msg_chain.insert(0, At(qq="all"))

        try:
            wrapper = _MessageWrapper(msg_chain)
            await self.context.send_message(umo, wrapper)
            self._log(f"图片已发送至 {umo}")
        except Exception as e:
            self._log(f"发送失败: {e}", "error")

    async def _run_task(self, idx: int, task: Dict[str, Any]):
        time_str = task.get("time", "")
        if not time_str:
            self._log(f"任务 {idx} 缺少 'time' 字段，跳过", "error")
            return
        try:
            h, m = map(int, time_str.split(":"))
        except ValueError:
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
                self._log(f"任务 {idx} 被取消")
                break
            self._log(f"执行任务 {idx} at {time_str}")
            await self._execute_task(task)

    async def terminate(self):
        self._log("正在终止所有定时任务...")
        for t in getattr(self, '_my_tasks', []):
            t.cancel()
        if self._my_tasks:
            await asyncio.gather(*self._my_tasks, return_exceptions=True)
        self._log("所有任务已终止")

    @filter.command("timerimage_reload")
    async def reload_cmd(self, event):
        admins = self.context.get_config().get("admins_id", [])
        if str(event.get_sender_id()) not in admins:
            yield event.plain_result("权限不足")
            return
        await self.terminate()
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        cf = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "config.json"
        if cf.exists():
            with open(cf, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = {}
        self.tasks = self.config.get("tasks", [])
        self.debug = self.config.get("debug_mode", False)
        self._my_tasks = []
        for i, task in enumerate(self.tasks):
            t = asyncio.create_task(self._run_task(i, task))
            self._my_tasks.append(t)
        yield event.plain_result(f"已重载，当前 {len(self.tasks)} 个任务")
