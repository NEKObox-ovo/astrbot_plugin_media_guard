"""多平台媒体发送守卫。

解决的核心问题：各平台适配器在处理 Image / File / Record / Video 消息段时，
会因为「远程资源拉不到」「文件已失效」「体积过大」而发送失败，甚至把
Python 异常原样甩给用户。

本插件在适配器 send() 外层加一层预处理：
1. 远程 URL 先下载到本地，再走本地上传通道（KOOK 尤其需要）
2. 超大图片自动压缩到阈值内
3. 失效 / 损坏的文件不抛异常，降级为提示文本
4. 记录可读的失败日志
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.media_utils import MediaResolver
except Exception:  # pragma: no cover - 兼容旧版本
    MediaResolver = None

PLUGIN_NAME = "astrbot_plugin_media_guard"
TEMP_DIR = Path("data/temp/media_guard")
TEMP_MAX_AGE = 6 * 3600  # 临时文件最长保留 6 小时

# (模块路径, 类名) —— send() 会被包装的适配器
PATCH_TARGETS: list[tuple[str, str]] = [
    ("astrbot.core.platform.sources.kook.kook_event", "KookEvent"),
    (
        "astrbot.core.platform.sources.qqofficial.qqofficial_message_event",
        "QQOfficialMessageEvent",
    ),
    (
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        "AiocqhttpMessageEvent",
    ),
]

# 这些平台不会主动去拉远程 URL，必须先落本地
STRICT_LOCAL_PLATFORMS = {"kook", "kaiheila"}


@register(
    PLUGIN_NAME,
    "猫小箱",
    "多平台媒体发送修复：远程资源本地化、超大图片压缩、失效文件兜底",
    "1.0.0",
    "https://github.com/NEKObox-ovo/astrbot_plugin_media_guard",
)
class MediaGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._patched: list[tuple[type, str]] = []
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._cleanup_temp()
        self._install_patches()

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    @property
    def localize_remote(self) -> bool:
        return bool(self._cfg("localize_remote", True))

    @property
    def compress_image(self) -> bool:
        return bool(self._cfg("compress_image", True))

    @property
    def image_limit_mb(self) -> float:
        try:
            return float(self._cfg("image_limit_mb", 2.0))
        except (TypeError, ValueError):
            return 2.0

    @property
    def download_limit_mb(self) -> float:
        try:
            return float(self._cfg("download_limit_mb", 30.0))
        except (TypeError, ValueError):
            return 30.0

    # ------------------------------------------------------------- 补丁管理

    def _install_patches(self) -> None:
        for module_path, cls_name in PATCH_TARGETS:
            try:
                module = __import__(module_path, fromlist=[cls_name])
                cls = getattr(module, cls_name)
            except Exception as exc:
                logger.debug(f"[MediaGuard] 跳过 {cls_name}: {exc}")
                continue

            original = getattr(cls, "send", None)
            if original is None or getattr(original, "__media_guard__", False):
                continue

            async def wrapper(ev_self, message, *args, __orig=original, **kwargs):
                return await self._send_wrapper(ev_self, message, __orig, *args, **kwargs)

            wrapper.__media_guard__ = True
            wrapper.__media_guard_original__ = original
            cls.send = wrapper
            self._patched.append((cls, cls_name))
            logger.info(f"[MediaGuard] 已接管 {cls_name}.send")

    def _remove_patches(self) -> None:
        for cls, cls_name in self._patched:
            current = getattr(cls, "send", None)
            original = getattr(current, "__media_guard_original__", None)
            if original is not None:
                cls.send = original
                logger.info(f"[MediaGuard] 已还原 {cls_name}.send")
        self._patched.clear()

    # ------------------------------------------------------------- 发送拦截

    async def _send_wrapper(self, event, message, original, *args, **kwargs):
        if not self.enabled or getattr(event, "_mg_busy", False):
            return await original(event, message, *args, **kwargs)

        setattr(event, "_mg_busy", True)
        try:
            message = await self._prepare(event, message)
            return await original(event, message, *args, **kwargs)
        except Exception as exc:
            logger.error(f"[MediaGuard] 发送失败: {type(exc).__name__}: {exc}")
            return await self._fallback(event, original, exc, *args, **kwargs)
        finally:
            setattr(event, "_mg_busy", False)

    async def _fallback(self, event, original, exc, *args, **kwargs):
        """媒体发不出去时，至少让用户知道发生了什么。"""
        try:
            hint = (
                "⚠️ 媒体发送失败"
                f"（{type(exc).__name__}）。文件可能已失效或超出平台限制，"
                "可让管理员查看日志。"
            )
            from astrbot.core.message.message_event_result import MessageChain

            return await original(
                event, MessageChain(chain=[Plain(hint)]), *args, **kwargs
            )
        except Exception as second:
            logger.error(f"[MediaGuard] 兜底文本也失败: {second}")
            return None

    # ------------------------------------------------------------- 预处理

    async def _prepare(self, event, message):
        chain = getattr(message, "chain", None)
        if not chain:
            return message

        platform = ""
        try:
            platform = (event.get_platform_name() or "").lower()
        except Exception:
            platform = ""

        must_local = platform in STRICT_LOCAL_PLATFORMS
        new_chain = []
        for comp in chain:
            try:
                fixed = await self._fix_component(comp, must_local)
                new_chain.append(fixed)
            except Exception as exc:
                logger.warning(
                    f"[MediaGuard] 处理 {type(comp).__name__} 失败，原样发送: {exc}"
                )
                new_chain.append(comp)

        message.chain = new_chain
        return message

    async def _fix_component(self, comp, must_local: bool):
        if isinstance(comp, Image):
            return await self._fix_image(comp, must_local)
        if isinstance(comp, File):
            return await self._fix_file(comp, must_local)
        if isinstance(comp, (Record, Video)):
            return await self._fix_media(comp, must_local)
        return comp

    async def _fix_image(self, comp: Image, must_local: bool):
        src = comp.url or comp.file or ""
        if not src:
            return comp

        local = src
        if self._is_remote(src) and (must_local or self.localize_remote):
            downloaded = await self._localize(src)
            if not downloaded:
                logger.error("[MediaGuard] 图片下载失败，可能无法送达")
                return comp
            local = downloaded

        if self.compress_image and not self._is_remote(local):
            local = self._compress(local)

        comp.file = local
        comp.path = local if not self._is_remote(local) else comp.path
        return comp

    async def _fix_file(self, comp: File, must_local: bool):
        local = comp.file_ or ""
        has_local = bool(local) and not self._is_remote(local) and os.path.exists(local)

        if not has_local:
            src = comp.url or local
            if self._is_remote(src):
                downloaded = await self._localize(src)
                if downloaded:
                    comp.file_ = downloaded
                    if not comp.name:
                        comp.name = Path(downloaded).name
                else:
                    logger.error("[MediaGuard] 文件下载失败，转为提示")
            elif local and not os.path.exists(local):
                logger.error(f"[MediaGuard] 文件不存在: {local}")
        return comp

    async def _fix_media(self, comp, must_local: bool):
        src = getattr(comp, "file", "") or getattr(comp, "url", "") or ""
        if not self._is_remote(src):
            return comp
        if not (must_local or self.localize_remote):
            return comp
        downloaded = await self._localize(src)
        if downloaded:
            try:
                comp.file = downloaded
            except Exception:
                pass
        return comp

    # ------------------------------------------------------------- 工具方法

    @staticmethod
    def _is_remote(src: str) -> bool:
        return isinstance(src, str) and src.startswith(("http://", "https://"))

    async def _localize(self, url: str) -> str | None:
        """把远程资源落到本地，返回本地绝对路径。"""
        if MediaResolver is None:
            return None
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(url.split("?")[0]).suffix or ".bin"
        target = TEMP_DIR / f"dl_{uuid.uuid4().hex}{suffix}"
        try:
            async with MediaResolver(url).as_path() as media:
                size_mb = media.path.stat().st_size / 1024 / 1024
                if size_mb > self.download_limit_mb:
                    logger.warning(
                        f"[MediaGuard] 资源 {size_mb:.1f}MB 超过上限 "
                        f"{self.download_limit_mb}MB，跳过本地化"
                    )
                    return None
                await asyncio.to_thread(shutil_copy, media.path, target)
            return str(target.resolve())
        except Exception as exc:
            logger.warning(f"[MediaGuard] 下载失败 {url[:60]}: {exc}")
            return None

    def _compress(self, path: str) -> str:
        """图片超过阈值时压缩，失败则原样返回。"""
        try:
            if not os.path.isfile(path):
                return path
            limit = self.image_limit_mb * 1024 * 1024
            if os.path.getsize(path) <= limit:
                return path
            from PIL import Image as PILImage

            with PILImage.open(path) as img:
                img.thumbnail((2048, 2048))
                out = TEMP_DIR / f"cz_{uuid.uuid4().hex}.jpg"
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.save(out, "JPEG", quality=85, optimize=True)
            logger.info(
                f"[MediaGuard] 图片已压缩 "
                f"{os.path.getsize(path) / 1024 / 1024:.1f}MB → "
                f"{os.path.getsize(out) / 1024 / 1024:.1f}MB"
            )
            return str(out.resolve())
        except Exception as exc:
            logger.debug(f"[MediaGuard] 压缩失败，使用原图: {exc}")
            return path

    def _cleanup_temp(self) -> None:
        now = time_now()
        removed = 0
        for item in TEMP_DIR.glob("*"):
            try:
                if now - item.stat().st_mtime > TEMP_MAX_AGE:
                    item.unlink()
                    removed += 1
            except Exception:
                continue
        if removed:
            logger.info(f"[MediaGuard] 清理了 {removed} 个过期临时文件")

    # ------------------------------------------------------------- 指令

    @filter.command("mediaguard")
    async def mediaguard_status(self, event: AstrMessageEvent):
        """查看媒体守卫运行状态。"""
        lines = [
            "🛡 媒体发送守卫 v1.0.0",
            f"状态：{'启用' if self.enabled else '停用'}",
            f"远程本地化：{'开' if self.localize_remote else '关'}",
            f"图片压缩：{'开' if self.compress_image else '关'}"
            f"（阈值 {self.image_limit_mb}MB）",
            f"下载上限：{self.download_limit_mb}MB",
            f"已接管：{', '.join(n for _, n in self._patched) or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------- 生命周期

    async def terminate(self):
        self._remove_patches()
        logger.info("[MediaGuard] 插件已卸载，补丁已还原")


def shutil_copy(src, dst):
    import shutil

    shutil.copy2(src, dst)


def time_now() -> float:
    import time

    return time.time()
