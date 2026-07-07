import asyncio
import json
import gc
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, At
from astrbot.api.event import filter
from astrbot.api import logger

class _MessageWrapper:
    def __init__(self, chain):
        self.chain = chain

@register("astrbot_plugin_lolicon_timer", "YHJM", "定时发送Lolicon二次元图片（支持渲染）", "2.0.0")
class LoliconTimerPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.tasks = self.config.get("tasks", [])
        self.debug = self.config.get("debug_mode", False)
        self.image_size = self.config.get("image_size", "regular")
        self.render_template = self.config.get("render_template", "")

        self._session = None
        self._my_tasks = []
        for i, task in enumerate(self.tasks):
            t = asyncio.create_task(self._run_task(i, task))
            self._my_tasks.append(t)

        logger.info(f"[LoliconTimer] 已加载 {len(self.tasks)} 个任务，尺寸={self.image_size}")

    def _log(self, msg: str, level: str = "info"):
        if self.debug or level != "debug":
            getattr(logger, level)(f"[LoliconTimer] {msg}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _fetch_image_and_meta(self) -> Optional[tuple]:
        """获取图片二进制和元数据 (img_bytes, meta_dict)"""
        api_url = "https://api.lolicon.app/setu/v2?r18=0&num=1"
        session = await self._get_session()
        try:
            async with session.get(api_url, timeout=30) as resp:
                if resp.status != 200:
                    self._log(f"API 请求失败 {resp.status}", "error")
                    return None
                data = await resp.json()
                if self.debug:
                    self._log(f"API 返回数据: {json.dumps(data, ensure_ascii=False)[:300]}", "debug")
                
                if not isinstance(data, dict) or 'data' not in data or not data['data']:
                    self._log("API 返回数据为空", "error")
                    return None
                first = data['data'][0]
                urls = first.get('urls', {})
                size = self.image_size
                img_url = urls.get(size) or urls.get('regular') or urls.get('original')
                if not img_url:
                    self._log("无法提取图片 URL", "error")
                    return None
                
                # 下载图片
                async with session.get(img_url) as img_resp:
                    if img_resp.status != 200:
                        self._log(f"图片下载失败 {img_resp.status}", "error")
                        return None
                    img_bytes = await img_resp.read()
                    # 提取元数据
                    meta = {
                        'title': first.get('title', ''),
                        'author': first.get('author', ''),
                        'pid': first.get('pid', ''),
                        'tags': ', '.join(first.get('tags', [])),
                        'r18': first.get('r18', False),
                    }
                    return img_bytes, meta
        except Exception as e:
            self._log(f"请求异常: {e}", "error")
            return None

    async def _render_image(self, img_bytes: bytes, meta: dict) -> Optional[str]:
        """使用 HTML 模板渲染图片（返回图片 URL）"""
        template = self.render_template
        if not template:
            self._log("渲染模板为空，使用纯图模式", "warning")
            return None

        # 将图片转为 base64 内联
        import base64
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        bg_data = f"data:image/png;base64,{b64}"  # 假设 png，实际可能 jpg，但浏览器可识别

        # 替换占位符
        html = template.replace("{{background}}", bg_data)
        html = html.replace("{{title}}", meta.get('title', ''))
        html = html.replace("{{author}}", meta.get('author', ''))
        html = html.replace("{{pid}}", str(meta.get('pid', '')))
        html = html.replace("{{tags}}", meta.get('tags', ''))
        # 可自定义更多占位符

        try:
            image_url = await self.html_render(html, {"full_page": True})
            return image_url
        except Exception as e:
            self._log(f"HTML 渲染失败: {e}", "error")
            return None

    async def _execute_task(self, task: Dict[str, Any]):
        umo = task.get("umo", "")
        if not umo:
            self._log("任务缺少 umo", "error")
            return
    
        result = await self._fetch_image_and_meta()
        if not result:
            self._log("获取图片失败", "error")
            return
        img_bytes, meta = result
    
        enable_render = task.get("enable_render", False)
        msg_chain = []
    
        if enable_render and self.render_template:
            rendered_url = await self._render_image(img_bytes, meta)
            if rendered_url:
                msg_chain = [Image.fromURL(rendered_url)]
            else:
                self._log("渲染失败，回退到纯图", "warning")
                msg_chain = [Image.fromBytes(img_bytes)]
        else:
            msg_chain = [Image.fromBytes(img_bytes)]
    
        if task.get("at_all", False):
            msg_chain.insert(0, At(qq="all"))
    
        # ---------- 发送重试机制 ----------
        max_retries = 2  # 最多尝试3次（包括首次）
        for attempt in range(max_retries + 1):
            try:
                wrapper = _MessageWrapper(msg_chain)
                await self.context.send_message(umo, wrapper)
                self._log(f"图片已发送至 {umo}")
                break  # 成功则跳出循环
            except Exception as e:
                if attempt < max_retries:
                    self._log(f"发送失败 (尝试 {attempt+1}/{max_retries+1}): {e}，2秒后重试...", "warning")
                    await asyncio.sleep(2)
                else:
                    self._log(f"发送失败: {e}", "error")
    
        # 清理内存
        del img_bytes
        gc.collect()

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
        if self._session and not self._session.closed:
            await self._session.close()
        self._log("所有任务已终止")

    @filter.command("lolicon_send")
    async def send_cmd(self, event, task_index: str = None):
        admins = self.context.get_config().get("admins_id", [])
        if str(event.get_sender_id()) not in admins:
            yield event.plain_result("权限不足")
            return
        if not self.tasks:
            yield event.plain_result("没有配置任何任务")
            return
        try:
            idx = int(task_index) - 1 if task_index else 0
        except ValueError:
            yield event.plain_result("请输入有效数字序号")
            return
        if idx < 0 or idx >= len(self.tasks):
            yield event.plain_result(f"序号超出范围，共 {len(self.tasks)} 个任务")
            return
        task = self.tasks[idx]
        self._log(f"手动触发任务 {idx+1}: {task.get('time')} -> {task.get('umo')}")
        await self._execute_task(task)
        yield event.plain_result(f"任务 {idx+1} 已触发，请等待发送结果")

    @filter.command("lolicon_reload")
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
        self.image_size = self.config.get("image_size", "regular")
        self.render_template = self.config.get("render_template", "")
        self._my_tasks = []
        for i, task in enumerate(self.tasks):
            t = asyncio.create_task(self._run_task(i, task))
            self._my_tasks.append(t)
        yield event.plain_result(f"已重载，当前 {len(self.tasks)} 个任务")
