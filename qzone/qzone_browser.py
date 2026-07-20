"""
QZone 浏览器自动化模块
使用 Playwright 模拟浏览器操作 QQ 空间：发动态、查看动态
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Set, Callable, Awaitable

from core.utils import strip_html, format_time, now_iso

logger = logging.getLogger("qzone_browser")

FEEDS_CACHE_FILE = Path(__file__).parent / "qzone_feeds.json"

# 真实动态 appid 与需要排除的广告/系统 appid
AD_APPIDS = {"6600", "7098", "5"}
SYSTEM_APPIDS = {"6405"}  # 系统通知/黄钻/会员等

# QZone 翻页 / 列表接口关键词 (任一命中即拦截)
FEEDS_URL_KEYWORDS = (
    "feeds3_html_more",
    "cgi-bin/feeds",
    "cgi-bin/feeds3",
    "emotion_cgi_get_feeds",
    "icenter_getfeeds",
    "emotion_cgi_",
    "cgi-bin/emotion",
)


def _compute_g_tk(skey: str) -> int:
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7fffffff


def _extract_f_info_text(html: str) -> str:
    """从一条 feed 的 html 字段中抠出 f-info div 的纯文本

    优先取 f-info 内部内容；如果没有就退而求其次取 f-single-content。
    返回已经过 HTML 解析的纯文本。
    """
    if not html:
        return ""
    # 先把 \x3C 等转义字符还原
    raw = html.replace("\\x3C", "<").replace("\\x3E", ">").replace("\\x22", '"')

    # 1. 优先抠 f-info
    m = re.search(r'<div[^>]*class="f-info"[^>]*>(.*?)</div>', raw, re.DOTALL | re.IGNORECASE)
    candidate = m.group(1) if m else ""

    # 2. 抠不到 → 抠 f-single-content
    if not candidate.strip():
        m2 = re.search(r'<div[^>]*class="f-single-content[^"]*"[^>]*>(.*?)</div>\s*</li>',
                       raw, re.DOTALL | re.IGNORECASE)
        if m2:
            candidate = m2.group(1)

    # 3. 还是抠不到 → 全文 strip
    if not candidate.strip():
        candidate = raw

    return strip_html(candidate)


class QZoneBrowser:
    """QQ 空间浏览器操作器"""

    # ===== 初始化 / 关闭 =====

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._uin: str = ""
        self._initialized = False
        self._feed_url = "https://user.qzone.qq.com/{uin}/infocenter"

    async def init(self, raw_cookies: str, uin: str) -> bool:
        """用 NapCat 提供的 cookies + uin 初始化浏览器会话"""
        from playwright.async_api import async_playwright

        self._uin = uin

        # 解析 cookies
        cookie_list = []
        for item in raw_cookies.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookie_list.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".qzone.qq.com",
                    "path": "/",
                })
        logger.info(f"[QZoneBrowser] 解析到 {len(cookie_list)} 个 cookies, uin={uin}")

        # Playwright 路径: 优先尝试本地 chromium, 失败则尝试下载/系统 chrome
        try:
            self._playwright = await async_playwright().start()
        except Exception as e:
            logger.error(f"[QZoneBrowser] playwright 启动失败: {e}")
            await self._cleanup()
            return False

        # 关键: playwright 启动后必须先 .chromium.launch(), 不是 .launch()
        browser = None
        try:
            browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
        except Exception as e:
            logger.error(f"[QZoneBrowser] chromium 启动失败: {e}")
            # 尝试 channel=chrome (用系统Chrome)
            try:
                browser = await self._playwright.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                logger.info("[QZoneBrowser] 使用系统 Chrome 启动成功")
            except Exception as e2:
                logger.error(f"[QZoneBrowser] 系统 Chrome 也启动失败: {e2}")
                await self._cleanup()
                return False

        self._browser = browser
        try:
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
            )
            await self._context.add_cookies(cookie_list)
            self._page = await self._context.new_page()

            # 访问 QZone 主页验证登录 (带重试)
            ok = False
            for attempt in range(3):
                try:
                    await self._page.goto(
                        "https://user.qzone.qq.com/", timeout=20000, wait_until="domcontentloaded"
                    )
                    await asyncio.sleep(3)
                    page_url = self._page.url
                    page_title = await self._page.title()
                    logger.info(
                        f"[QZoneBrowser] 初始化页面 (尝试{attempt+1}): "
                        f"title={page_title} url={page_url[:80]}"
                    )

                    if "login" in page_url.lower() or "登录" in page_title:
                        logger.warning(
                            f"[QZoneBrowser] 尝试{attempt+1}: 页面跳转到登录页"
                        )
                        continue
                    ok = True
                    break
                except Exception as e:
                    logger.warning(f"[QZoneBrowser] 尝试{attempt+1} 失败: {e}")
                    await asyncio.sleep(2)

            if not ok:
                logger.error("[QZoneBrowser] 所有尝试都失败 (登录态无效或网络问题)")
                await self._cleanup()
                return False

            self._initialized = True
            logger.info("[QZoneBrowser] 初始化完成")
            return True
        except Exception as e:
            logger.error(f"[QZoneBrowser] context/page 创建失败: {e}")
            await self._cleanup()
            return False

    async def close(self):
        await self._cleanup()
        self._initialized = False
        logger.info("[QZoneBrowser] 已关闭")

    async def _cleanup(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if hasattr(self, "_playwright") and self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    # ===== 发动态 =====

    async def publish_text(self, content: str,
                           on_progress: Optional[Callable[[str], Awaitable[None]]] = None) -> Optional[str]:
        """发纯文字动态，返回 feed_id 或 None

        on_progress: 可选, 用于在发布各阶段发送进度消息
        """
        async def _progress(msg: str):
            if on_progress:
                try:
                    await on_progress(msg)
                except Exception:
                    pass

        if not self._initialized or not self._page:
            logger.error("[QZoneBrowser] 未初始化")
            return None

        try:
            await _progress("正在打开 QQ 空间页面...")
            await self._page.goto("https://qzone.qq.com/", timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            page_url = self._page.url
            page_title = await self._page.title()
            logger.info(f"[QZoneBrowser] 当前页面: title={page_title} url={page_url[:80]}")

            if "login" in page_url.lower() or "登录" in page_title:
                logger.error("[QZoneBrowser] 页面跳转到登录页，cookie 可能已过期")
                return None

            await _progress("页面加载完成，正在定位输入框...")
            result = await self._publish_via_dom(content, on_progress)
            if result:
                return result

            qzonetoken = await self._extract_qzonetoken()
            if qzonetoken:
                p_skey = await self._get_cookie_value("p_skey")
                if p_skey:
                    result = await self._publish_via_api(content, p_skey, qzonetoken)
                    if result:
                        return result

            logger.error("[QZoneBrowser] 所有发布方式均失败")
            return None
        except Exception as e:
            logger.error(f"[QZoneBrowser] publish 异常: {e}")
            return None

    async def _extract_qzonetoken(self) -> str:
        """从 QZone 页面提取 qzonetoken"""
        try:
            token = await self._page.evaluate("""
                () => {
                    try {
                        if (window.g_qzonetoken) return window.g_qzonetoken;
                        if (window.QZONE && window.QZONE.qzonetoken) return window.QZONE.qzonetoken;
                    } catch(e) {}
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const m = s.textContent?.match(/qzonetoken["'\\s]*[:=]["'\\s]*([a-zA-Z0-9_]+)/);
                        if (m) return m[1];
                    }
                    return '';
                }
            """)
            return token or ""
        except Exception:
            return ""

    async def _publish_via_api(self, content: str, p_skey: str, qzonetoken: str) -> Optional[str]:
        """通过 QZone API 发布（备用）"""
        try:
            g_tk = _compute_g_tk(p_skey)
            result = await self._page.evaluate("""
                async (params) => {
                    const [g_tk, uin, content, qzonetoken] = params;
                    try {
                        const resp = await fetch(
                            `https://h5.qzone.qq.com/webapp/json/publish/publish?g_tk=${g_tk}`,
                            {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    qzonetoken: qzonetoken,
                                    content: content,
                                    syncFlag: 1,
                                    source: 1,
                                    format: 'json',
                                    ugc_right: 1,
                                }),
                            }
                        );
                        const text = await resp.text();
                        try {
                            return JSON.stringify(JSON.parse(text));
                        } catch(e) {
                            return JSON.stringify({error: 'not_json', raw: text.substring(0, 200)});
                        }
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }
            """, [g_tk, self._uin, content, qzonetoken])

            logger.info(f"[QZoneBrowser] API 返回: {result[:300]}")
            data = json.loads(result)
            if data.get("code") == 0 or data.get("ret") == 0:
                feed_id = data.get("data", {}).get("tid", "") or data.get("tid", "")
                if feed_id:
                    self._save_feed(str(feed_id), content)
                    return str(feed_id)
            return None
        except Exception as e:
            logger.error(f"[QZoneBrowser] API 异常: {e}")
            return None

    async def _publish_via_dom(self, content: str,
                               on_progress: Optional[Callable[[str], Awaitable[None]]] = None) -> Optional[str]:
        """通过页面 DOM 操作发动态"""
        async def _progress(msg: str):
            if on_progress:
                try:
                    await on_progress(msg)
                except Exception:
                    pass

        try:
            try:
                await self._page.wait_for_function("""
                    () => {
                        const el = document.querySelector('.qz-poster-editor-cont, .qz-inputer');
                        if (!el) return true;
                        const text = el.textContent || '';
                        return !text.includes('正在加载');
                    }
                """, timeout=15000)
                logger.info("[QZoneBrowser DOM] 编辑器加载完成")
            except Exception:
                logger.warning("[QZoneBrowser DOM] 等待编辑器超时，继续尝试")

            # 先点击编辑器容器激活（用 force 绕过可见性检查）
            container = await self._page.query_selector(".qz-poster-editor-cont, .qz-inputer")
            if container:
                try:
                    await container.click(force=True, timeout=5000)
                    logger.info("[QZoneBrowser DOM] 已点击编辑器容器")
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"[QZoneBrowser DOM] 点击容器失败: {e}")

            await _progress("已找到输入框，正在输入内容...")

            editor_selectors = [
                ".qz-poster-editor-cont div[contenteditable]",
                ".qz-inputer div[contenteditable]",
                "div.qz-poster-editor-cont",
                "div.qz-inputer",
                "div.textinput",
            ]
            editor = None
            for sel in editor_selectors:
                editor = await self._page.query_selector(sel)
                if editor:
                    logger.info(f"[QZoneBrowser DOM] 找到输入框: {sel}")
                    break

            if not editor:
                result = await self._page.evaluate("""
                    (content) => {
                        const editor = document.querySelector('.qz-poster-editor-cont [contenteditable], .qz-inputer [contenteditable], .textinput');
                        if (!editor) return 'no_editor';
                        editor.focus();
                        editor.textContent = content;
                        editor.dispatchEvent(new Event('input', {bubbles: true}));
                        editor.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'ok';
                    }
                """, content)
                if result == "no_editor":
                    logger.error("[QZoneBrowser DOM] JS 也未找到输入框")
                    return None
                logger.info(f"[QZoneBrowser DOM] JS 输入结果: {result}")
            else:
                try:
                    await editor.click(force=True, timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                await self._page.keyboard.type(content, delay=30)
                await asyncio.sleep(1)

            btn_selectors = [
                "a:has-text('发表'), button:has-text('发表')",
                "a[class*=publish], button[class*=publish], a[class*=submit], button[class*=submit]",
                "a[class*=send], button[class*=send]",
                "a:has-text('发布'), button:has-text('发布')",
            ]
            publish_btn = None
            for sel in btn_selectors:
                publish_btn = await self._page.query_selector(sel)
                if publish_btn:
                    logger.info(f"[QZoneBrowser DOM] 找到发布按钮: {sel}")
                    break

            if not publish_btn:
                logger.error("[QZoneBrowser DOM] 未找到发布按钮")
                return None

            await _progress("正在点击发表按钮...")

            captured_response = {"body": None}

            async def handle_route(route):
                try:
                    response = await route.fetch()
                    body = await response.text()
                    captured_response["body"] = body
                    await route.fulfill(response=response)
                except Exception:
                    await route.continue_()

            await self._page.route("**/emotion_cgi_publish*", handle_route)

            try:
                await publish_btn.click(force=True, timeout=5000)
            except Exception:
                await publish_btn.click(timeout=5000)

            await _progress("正在等待服务器响应...")
            await asyncio.sleep(6)
            await self._page.unroute("**/emotion_cgi_publish*")

            feed_id = None
            if captured_response["body"]:
                logger.info(f"[QZoneBrowser DOM] 拦截到API响应: {captured_response['body'][:300]}")
                try:
                    resp_data = json.loads(captured_response["body"])
                    if resp_data.get("code") == 0:
                        feed_id = (resp_data.get("tid") or
                                   resp_data.get("data", {}).get("tid") or
                                   resp_data.get("result", {}).get("tid") or
                                   resp_data.get("feedid") or
                                   resp_data.get("feed_id"))
                except (json.JSONDecodeError, AttributeError):
                    pass

            if feed_id:
                self._save_feed(feed_id, content)
                logger.info(f"[QZoneBrowser DOM] 获取到真实 feed_id: {feed_id}")
                return str(feed_id)

            logger.info("[QZoneBrowser DOM] 未拦截到feed_id，DOM扫描")
            await asyncio.sleep(3)
            feed_id = await self._page.evaluate("""
                () => {
                    const isHexId = (val) => {
                        if (!val || typeof val !== 'string') return false;
                        return /^[a-f0-9]{16,}$/i.test(val.trim());
                    };
                    const allEls = document.querySelectorAll('*');
                    const found = [];
                    for (const el of allEls) {
                        for (const attr of el.attributes) {
                            if (attr.name.startsWith('data-') && isHexId(attr.value)) {
                                found.push(attr.value.trim());
                            }
                        }
                    }
                    if (found.length > 0) {
                        found.sort((a, b) => b.length - a.length);
                        return found[0];
                    }
                    const hash = window.location.hash || '';
                    let m = hash.match(/([a-f0-9]{20,})/i);
                    if (m) return m[1];
                    try {
                        for (const key of Object.keys(window)) {
                            const val = window[key];
                            if (typeof val === 'string' && isHexId(val) && val.length >= 20) return val;
                        }
                    } catch(e) {}
                    const pageText = document.body ? document.body.innerHTML : '';
                    m = pageText.match(/(?:tid|feed_id|feedid|appid)["'\\s:=]+([a-f0-9]{16,})/i);
                    if (m) {
                        const hexM = m[0].match(/[a-f0-9]{16,}/i);
                        if (hexM) return hexM[0];
                    }
                    return '';
                }
            """)
            if feed_id:
                self._save_feed(feed_id, content)
                logger.info(f"[QZoneBrowser DOM] DOM扫描到 feed_id: {feed_id}")
                return str(feed_id)

            fallback_id = "c_" + hashlib.md5((content + str(time.time())).encode()).hexdigest()[:12]
            self._save_feed(fallback_id, content)
            logger.info(f"[QZoneBrowser DOM] 发布完成，生成 fallback_id: {fallback_id}")
            return fallback_id
        except Exception as e:
            logger.error(f"[QZoneBrowser DOM] 异常: {e}")
            return None

    # ===== 看动态 =====

    # 三种模式 URL 模板
    SELF_FEED_URL_TPL = "https://user.qzone.qq.com/{uin}/infocenter"
    # target 模式也用 infocenter: 目标页 API 格式不稳定 (HTML而非JSONP)
    # infocenter 的 DOM 抓取和 API 拦截更可靠，只需按 uin 过滤
    TARGET_FEED_URL_TPL = "https://user.qzone.qq.com/{uin}/infocenter"
    # 好友圈入口
    FRIEND_CIRCLE_URL = "https://user.qzone.qq.com/main"
    # 移动端 QZone 目标用户动态列表 (fallback: 直接访问目标用户空间获取更久远动态)
    MOBILE_TARGET_FEED_URL = "https://mobile.qzone.qq.com/list?uin={uin}&g_f=2000000103"

    async def get_feeds(self, count: int = 20, max_scroll: int = 10,
                        target_uin: Optional[str] = None,
                        mode: str = "self",
                        on_progress: Optional[Callable[[List[Dict]], Awaitable[None]]] = None) -> List[Dict]:
        """获取动态列表

        mode:
            - "self"          看自己 QQ 空间 (默认), 过滤 e.uin == 自己
            - "target"        看某个 QQ 的空间, 过滤 e.uin == target_uin
            - "friend_circle" 好友圈 (公开/可看的好友动态), 不按 uin 过滤

        on_progress: 增量回调, 每解析完一页新数据后调用, 传入当前所有已解析的条目列表
                     用于向用户发送"已经翻到了xxx的内容"等进度提示

        改进点:
        1. 同时拦截多种 URL 模式 (feeds3_html_more / cgi-bin/feeds / emotion_cgi_get_feeds / icenter_getfeeds)
        2. 多次滚动 + 等待 + 检查新增条数, 直到拿够或滚到底
        3. 解析失败时回退到 DOM 抓取 (DOM 抓取独立作为兜底, 即使拦截成功也合并)
        4. 提取内容时优先用 _extract_f_info_text (正确解码 \x3C 等)
        5. 正确的 fct_/feed_ id 正则: 6 个数字段 (uin, appid, ?, abstime, ?, ?)
        6. 过滤空内容 / 无效 uin 的条目 (这些是 BANNER / 直播 / 推荐位)
        """
        if not self._initialized or not self._page:
            return []

        # 根据 mode 决定 URL 和过滤目标
        # 所有模式都走 bot 自己的 infocenter（bot 的聚合流）
        # self 模式按 bot uin 过滤, target 模式按 target_uin 过滤, friend_circle 不过滤
        uin = self._uin
        if mode == "friend_circle":
            feed_url = self.SELF_FEED_URL_TPL.format(uin=uin)
            target_uin_str = ""  # 不过滤 uin
        elif mode == "target" and target_uin:
            # target 模式: 走 bot 的 infocenter 聚合流，按 target_uin 过滤
            # （目标用户空间主页只返回个人资料，不返回动态列表；infocenter 通过大量滚动可加载历史动态）
            feed_url = self.SELF_FEED_URL_TPL.format(uin=uin)
            target_uin_str = str(target_uin)
        else:
            # self 或 target 但没传 target_uin
            feed_url = self.SELF_FEED_URL_TPL.format(uin=uin)
            target_uin_str = str(uin)

        try:
            intercepted: List[Dict] = []
            seen_urls: Set[str] = set()

            def _on_response(response):
                url = response.url
                if not any(kw in url for kw in FEEDS_URL_KEYWORDS):
                    return
                if url in seen_urls:
                    return
                seen_urls.add(url)
                asyncio.create_task(self._capture_feed_response(response, intercepted, url))

            self._page.on("response", _on_response)

            try:
                # 始终显式 goto 目标页, 不用 reload —
                # publish 之后页面可能跳到详情页, reload() 刷新错页面拿不到 feeds
                logger.info(
                    f"[QZone列表] mode={mode} goto {feed_url} "
                    f"(target_uin={target_uin or 'self'})"
                )
                await self._page.goto(
                    feed_url, wait_until="domcontentloaded", timeout=20000
                )
                await asyncio.sleep(4)

                # 多次滚动拉取翻页, 直到拿够或滚到底
                prev_count = -1
                no_change_rounds = 0
                last_progress_count = 0  # 用于增量进度: 上次通知时的条目数
                for i in range(max_scroll):
                    # 同时按 End 和 PageDown, 兼容不同前端
                    await self._page.keyboard.press("End")
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.press("PageDown")
                    await asyncio.sleep(2.0)
                    cur_count = len(intercepted)
                    logger.info(f"[QZone列表] 第 {i + 1} 次滚动, 已拦截 {cur_count} 个响应")

                    # 增量进度回调: 每 2 轮滚动主动从 DOM 抓取
                    # 不再依赖 JSONP 解析 (QZone JSONP 格式不稳定, 经常解析失败)
                    if on_progress and i % 2 == 0:
                        current = await self._scrape_feeds_from_dom()
                        # 简单过滤目标 uin
                        if target_uin_str:
                            current = [
                                e for e in current
                                if str(e.get("uin", "")).strip() == target_uin_str
                            ]
                        current = self._dedupe_entries(current)
                        if len(current) > last_progress_count:
                            await on_progress(current)
                            last_progress_count = len(current)

                    if cur_count == prev_count:
                        no_change_rounds += 1
                        if no_change_rounds >= 2:
                            break
                    else:
                        no_change_rounds = 0
                    prev_count = cur_count
            finally:
                try:
                    self._page.remove_listener("response", _on_response)
                except Exception:
                    pass
            logger.info(f"[QZone列表] 滚动结束, 共拦截 {len(intercepted)} 个响应")

            entries = self._collect_entries(intercepted)

            # 兜底: DOM 抓取, 即使网络拦截成功也合并 (能拿更多)
            try:
                dom_entries = await self._scrape_feeds_from_dom()
                if dom_entries:
                    entries.extend(dom_entries)
            except Exception as e:
                logger.warning(f"[QZone列表] DOM 抓取异常: {e}")

            # 去重 (uin + key)
            entries = self._dedupe_entries(entries)

            # 关键过滤: 只保留目标用户的动态 (好友圈模式不按 uin 过滤).
            # 之前没过滤时, 拦截到的 feeds 包含好友圈所有用户的动态,
            # 显示成"自己的空间里有 14 条"是错的.
            before_filter = len(entries)
            if target_uin_str:
                # 调试：打印前 5 条条目的 uin/nickname, 帮助诊断过滤失败问题
                sample_uins = []
                for e in entries[:5]:
                    euin = str(e.get("uin", "")).strip()
                    enick = e.get("nickname", "")
                    ekey = e.get("key", "")[:16]
                    sample_uins.append(f"  uin={euin!r} nick={enick!r} key={ekey}")
                logger.info(
                    f"[QZone列表] 过滤前 uin 采样 (共{len(entries)}条):\n"
                    + "\n".join(sample_uins)
                )
                entries = [
                    e for e in entries
                    if str(e.get("uin", "")).strip() == target_uin_str
                ]
                logger.info(
                    f"[QZone列表] uin 过滤: {before_filter} → {len(entries)} 条 "
                    f"(目标 uin={target_uin_str})"
                )
            else:
                logger.info(
                    f"[QZone列表] 好友圈模式: 不过滤 uin, 共 {len(entries)} 条"
                )

            # 过滤广告/系统通知
            filtered = [e for e in entries
                        if str(e.get("appid", "")) not in AD_APPIDS
                        and str(e.get("appid", "")) not in SYSTEM_APPIDS]

            # 过滤无效条目: 无 uin 或内容全空
            valid = []
            skipped_no_uin = 0
            skipped_no_content = 0
            for e in filtered:
                if not str(e.get("uin", "")).strip():
                    skipped_no_uin += 1
                    continue
                summary = (e.get("summary") or "").strip()
                if not summary:
                    summary = _extract_f_info_text(e.get("html", ""))
                if not summary:
                    skipped_no_content += 1
                    continue
                e["_display_text"] = summary
                valid.append(e)
            if skipped_no_uin or skipped_no_content:
                logger.info(
                    f"[QZone列表] valid 过滤: 跳过 {skipped_no_uin} 条(无uin) "
                    f"+ {skipped_no_content} 条(无内容)"
                )

            # 按 abstime 倒序 (最新在前), 避免老动态被推到前面
            valid.sort(key=lambda e: int(str(e.get("abstime", "0")) or 0), reverse=True)

            logger.info(
                f"[QZone列表] 共解析 {len(entries)} 条, 过滤后 {len(filtered)} 条, "
                f"有效 {len(valid)} 条, 返回前 {count} 条"
            )

            # Fallback: target 模式下 infocenter 无目标用户条目时,
            # 尝试移动端 QZone 页面直接访问目标用户动态列表
            # (infocenter 聚合流可能不包含过于久远的动态)
            if (
                mode == "target"
                and target_uin
                and not valid
                and intercepted
            ):
                logger.info(
                    f"[QZone列表] infocenter 无目标用户条目, "
                    f"尝试移动端 fallback (uin={target_uin})"
                )
                mobile_valid = await self._get_feeds_via_mobile(
                    str(target_uin)
                )
                if mobile_valid:
                    logger.info(
                        f"[QZone列表] mobile fallback 成功: "
                        f"获取到 {len(mobile_valid)} 条有效动态"
                    )
                    return mobile_valid[:count]
                else:
                    logger.info("[QZone列表] mobile fallback 也无结果")

            return valid[:count]
        except Exception as e:
            logger.error(f"[QZone列表] 异常: {e}")
            return []

    async def _get_feeds_via_mobile(
        self, target_uin: str, max_scroll: int = 8
    ) -> List[Dict]:
        """Fallback: 通过移动端 QZone 页面直接访问目标用户动态列表

        当 infocenter 因动态过于久远无法返回目标用户条目时，
        尝试直接访问 mobile.qzone.qq.com/list?uin={target_uin}
        绕过 infocenter 聚合流的时间限制。
        """
        if not self._page:
            return []

        feed_url = self.MOBILE_TARGET_FEED_URL.format(uin=target_uin)
        logger.info(
            f"[QZone列表] [mobile fallback] 尝试移动端: {feed_url}"
        )

        try:
            intercepted: List[Dict] = []
            seen_urls: Set[str] = set()

            def _on_response(response):
                url = response.url
                if not any(kw in url for kw in FEEDS_URL_KEYWORDS):
                    return
                if url in seen_urls:
                    return
                seen_urls.add(url)
                asyncio.create_task(
                    self._capture_feed_response(response, intercepted, url)
                )

            self._page.on("response", _on_response)

            try:
                await self._page.goto(
                    feed_url, wait_until="domcontentloaded", timeout=20000
                )
                await asyncio.sleep(4)

                # 移动端页面滚动
                prev_count = -1
                no_change_rounds = 0
                for i in range(max_scroll):
                    await self._page.keyboard.press("End")
                    await asyncio.sleep(0.5)
                    await self._page.keyboard.press("PageDown")
                    await asyncio.sleep(2.0)
                    cur_count = len(intercepted)
                    logger.info(
                        f"[QZone列表] [mobile fallback] 第{i+1}次滚动, "
                        f"已拦截 {cur_count} 个响应"
                    )
                    if cur_count == prev_count:
                        no_change_rounds += 1
                        if no_change_rounds >= 2:
                            break
                    else:
                        no_change_rounds = 0
                    prev_count = cur_count
            finally:
                try:
                    self._page.remove_listener("response", _on_response)
                except Exception:
                    pass

            logger.info(
                f"[QZone列表] [mobile fallback] 滚动结束, "
                f"共拦截 {len(intercepted)} 个响应"
            )

            # 从拦截响应中提取条目
            entries = self._collect_entries(intercepted)

            # DOM 兜底
            try:
                dom_entries = await self._scrape_feeds_from_dom()
                if dom_entries:
                    entries.extend(dom_entries)
            except Exception as e:
                logger.warning(f"[QZone列表] [mobile fallback] DOM 抓取异常: {e}")

            # 去重
            entries = self._dedupe_entries(entries)

            # 按 target_uin 过滤
            target_uin_str = str(target_uin)
            before = len(entries)
            entries = [
                e for e in entries
                if str(e.get("uin", "")).strip() == target_uin_str
            ]
            logger.info(
                f"[QZone列表] [mobile fallback] uin过滤: "
                f"{before} → {len(entries)} 条 (目标 uin={target_uin_str})"
            )

            # 过滤广告/系统通知
            entries = [
                e for e in entries
                if str(e.get("appid", "")) not in AD_APPIDS
                and str(e.get("appid", "")) not in SYSTEM_APPIDS
            ]

            # 过滤无效条目
            valid = []
            for e in entries:
                if not str(e.get("uin", "")).strip():
                    continue
                summary = (e.get("summary") or "").strip()
                if not summary:
                    summary = _extract_f_info_text(e.get("html", ""))
                if not summary:
                    continue
                e["_display_text"] = summary
                valid.append(e)

            valid.sort(
                key=lambda e: int(str(e.get("abstime", "0")) or 0), reverse=True
            )
            logger.info(
                f"[QZone列表] [mobile fallback] 最终有效: {len(valid)} 条"
            )
            return valid
        except Exception as e:
            logger.error(f"[QZone列表] [mobile fallback] 异常: {e}")
            return []

    @staticmethod
    def _dedupe_entries(entries: List[Dict]) -> List[Dict]:
        """用 key 为主去重, uin 缺失时也能保留 (推荐位/广告可能没 uin)

        之前用 uin+key 双键去重, 导致 uin 为空的 entry 全部被过滤 (friend_circle
        模式 16 条全部无 uin → 0 条). 改为只用 key 去重, uin 仅作为可选项.
        """
        seen: Set[str] = set()
        out: List[Dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            key = str(e.get("key", ""))
            uin = str(e.get("uin", "")).strip()
            if not key:
                continue
            # 用 key 唯一去重, 缺 uin 的也保留
            sig = key
            if sig in seen:
                continue
            seen.add(sig)
            # 如果 entry 缺 uin, 尝试从 userinfo / feedinfo 提取
            if not uin:
                ui = e.get("userinfo")
                if isinstance(ui, dict):
                    uin = str(ui.get("uin", "")).strip()
                if not uin:
                    fi = e.get("feedinfo")
                    if isinstance(fi, dict):
                        uin = str(fi.get("uin", "")).strip()
                if uin:
                    e["uin"] = uin
            out.append(e)
        return out

    def _collect_entries(self, intercepted: List[Dict]) -> List[Dict]:
        """合并多个 JSONP 响应, 抽取 entry 列表

        数据结构 (QZone):
        _Callback({ code, data: { main: {...}, data: [entries...] } })
        """
        all_entries: List[Dict] = []
        # 调试: 记录前 3 个非空响应的顶层结构, 用于诊断 friend_circle/target 模式 0 条问题
        debug_count = 0

        for resp in intercepted:
            text = resp.get("text", "")
            # 调试先跑一次, 无论 entries 多少, 都能看到真实结构
            if debug_count < 3 and text and len(text) > 100:
                data_dbg = self._parse_jsonp(text)
                if data_dbg:
                    inner = data_dbg.get("data", {})
                    inner_type = (
                        type(inner).__name__
                        if not isinstance(inner, dict)
                        else f"dict(keys={list(inner.keys())[:8]})"
                    )
                    if isinstance(inner, dict):
                        # 兼容多种嵌套结构:
                        # - inner.data / inner.feedlist (旧格式)
                        # - inner.main → main.feeds / main.data (新格式)
                        raw_entries = (
                            inner.get("data")
                            or inner.get("feedlist")
                            or inner.get("list")
                            or []
                        )
                        # QZone 新格式: data.main 下可能有 feeds/data/list
                        main = inner.get("main")
                        if isinstance(main, dict) and not raw_entries:
                            raw_entries = (
                                main.get("feeds")
                                or main.get("data")
                                or main.get("list")
                                or []
                            )
                    elif isinstance(inner, list):
                        raw_entries = inner
                    else:
                        raw_entries = []
                    first_uin = ""
                    first_key = ""
                    if (
                        isinstance(raw_entries, list)
                        and raw_entries
                        and isinstance(raw_entries[0], dict)
                    ):
                        first = raw_entries[0]
                        first_uin = (
                            first.get("uin", "")
                            or (
                                first.get("userinfo", {}).get("uin", "")
                                if isinstance(first.get("userinfo"), dict)
                                else ""
                            )
                            or (
                                first.get("feedinfo", {}).get("uin", "")
                                if isinstance(first.get("feedinfo"), dict)
                                else ""
                            )
                        )
                        first_key = first.get("key", "")
                    logger.info(
                        f"[QZone列表] [DEBUG] 响应 #{debug_count} "
                        f"(len={len(text)}): 顶层 keys={list(data_dbg.keys())[:5]}, "
                        f"inner={inner_type}, raw_entries="
                        f"{len(raw_entries) if isinstance(raw_entries, list) else type(raw_entries).__name__}, "
                        f"首条 uin={first_uin!r} key={first_key!r}"
                    )
                else:
                    logger.info(
                        f"[QZone列表] [DEBUG] 响应 #{debug_count} "
                        f"(len={len(text)}): JSONP 解析失败, "
                        f"前100字符={text[:100]!r}"
                    )
                debug_count += 1

            data = self._parse_jsonp(text)
            if not data:
                continue

            inner = data.get("data", {})
            if isinstance(inner, dict):
                # QZone infocenter 结构: data.main.data / data.main.vFeeds
                # 先试 data.data, 没有就挖 data.main
                entries = inner.get("data") or inner.get("feedlist") or []
                if not entries and "main" in inner:
                    main = inner["main"]
                    if isinstance(main, dict):
                        entries = (
                            main.get("data")
                            or main.get("vFeeds")
                            or main.get("feedlist")
                            or []
                        )
                # data.data 也可能是 { data: [...], main: {...} }
                if isinstance(entries, dict):
                    entries = (entries.get("data")
                               or entries.get("feedlist")
                               or entries.get("vFeeds")
                               or [])
            elif isinstance(inner, list):
                entries = inner
            else:
                entries = []

            if not isinstance(entries, list):
                continue

            for e in entries:
                if isinstance(e, dict):
                    all_entries.append(e)
        return all_entries

    async def _scrape_feeds_from_dom(self) -> List[Dict]:
        """兜底: 直接从 DOM 抓取动态条目

        覆盖多种 QZone DOM 结构:
        - .f-single.f-s-s  (经典 infocenter 好友动态列表)
        - .feed-item / [data-feed-id]  (新版本)
        - .f-single (不带 f-s-s 类名)
        - li[id^="fct_"], li[id^="feed_"]  (按 id 前缀匹配)
        """
        try:
            result = await self._page.evaluate(r"""
                () => {
                    // 多选择器兜底，按优先级尝试
                    const SELECTORS = [
                        '.f-single.f-s-s',
                        '[data-feed-id]',
                        '.feed-item',
                        'li[id^="fct_"]',
                        'li[id^="feed_"]',
                        '.f-single',
                        '.f-single-content',
                    ];
                    let nodes = [];
                    let usedSelector = '';
                    for (const sel of SELECTORS) {
                        nodes = document.querySelectorAll(sel);
                        if (nodes.length > 0) {
                            usedSelector = sel;
                            break;
                        }
                    }
                    if (!nodes || nodes.length === 0) {
                        // 终极兜底：扫描 body 中所有带 data-key 的元素
                        nodes = document.querySelectorAll('[data-key]');
                        if (nodes.length > 0) usedSelector = '[data-key]';
                    }

                    // 诊断信息：统计页面上所有可能的列表容器
                    const domStats = {};
                    for (const sel of SELECTORS) {
                        domStats[sel] = document.querySelectorAll(sel).length;
                    }
                    domStats['[data-key]'] = document.querySelectorAll('[data-key]').length;
                    domStats['_total_lis'] = document.querySelectorAll('li').length;
                    domStats['_usedSelector'] = usedSelector;

                    const out = [];
                    const seenKeys = new Set();
                    for (const node of nodes) {
                        // id 格式: fct_{uin}_{appid}_{?}_{abstime}_{?}_{?}
                        // 或 feed_{uin}_{appid}_{?}_{abstime}_{?}_{?}
                        const elemId = node.id || '';
                        let m = elemId.match(/(?:fct|feed)_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)/);
                        let uin, appid, abstime;
                        if (m) {
                            uin = m[1];
                            appid = m[2];
                            abstime = m[4];
                        } else {
                            // 尝试从 data 属性获取
                            uin = node.getAttribute('data-uin') || '';
                            appid = node.getAttribute('data-appid') || '';
                            abstime = node.getAttribute('data-abstime') || '';
                        }

                        // 提取 key (多种方式)
                        let key = node.getAttribute('data-key') || node.getAttribute('data-feed-id') || '';
                        if (!key) {
                            const keyEl = node.querySelector('[data-key]');
                            if (keyEl) key = keyEl.getAttribute('data-key') || '';
                        }
                        if (!key) key = elemId || '';

                        // 去重
                        if (key && seenKeys.has(key)) continue;
                        if (key) seenKeys.add(key);

                        // 提取内容文本 (多种选择器)
                        let summary = '';
                        const fInfo = node.querySelector('.f-info')
                            || node.querySelector('.f-single-content')
                            || node.querySelector('.feed-content')
                            || node.querySelector('[class*="content"]');
                        if (fInfo) summary = (fInfo.textContent || '').trim();

                        // 昵称
                        let nickname = '';
                        const nick = node.querySelector('.f-nick .f-name')
                            || node.querySelector('.nickname')
                            || node.querySelector('[class*="nick"]')
                            || node.querySelector('[class*="name"]');
                        if (nick) nickname = nick.textContent.trim();

                        // 是否有图
                        const hasPic = !!(
                            node.querySelector('.f-img, .f-pic, img[src*="qpic"]')
                            || node.querySelector('img')
                        );

                        // 如果没有提取到 uin，尝试从昵称链接推断
                        if (!uin) {
                            const uinLink = node.querySelector('a[href*="uin="], a[href*="/u/"]');
                            if (uinLink) {
                                const href = uinLink.getAttribute('href') || '';
                                const uinM = href.match(/(?:uin=|\/u\/)(\d+)/);
                                if (uinM) uin = uinM[1];
                            }
                        }

                        // 只要 extract 到内容则保留；即使 uin 为空也先保留，后续由 JSONP 合并时补充
                        if (summary || key) {
                            out.push({
                                uin, appid, key, abstime, nickname, summary,
                                hasPic,
                                html: fInfo ? (fInfo.outerHTML || '') : '',
                                _source: 'dom',
                            });
                        }
                    }
                    // 把统计信息放第一条（不是真正的 entry，只是调试）
                    if (out.length === 0) {
                        // 返回一个占位，让 Python 侧能读到诊断信息
                        out.push({
                            uin: '', appid: '', key: '__DOM_STATS__', abstime: '',
                            nickname: '', summary: JSON.stringify(domStats),
                            hasPic: false, html: '', _source: 'dom_stats',
                        });
                    } else {
                        // 在最后追加诊断
                        out[out.length - 1]['_domStats'] = JSON.stringify(domStats);
                    }
                    return out;
                }
            """)
            if result:
                # 过滤掉诊断占位
                entries = [e for e in result if e.get('key') != '__DOM_STATS__' and e.get('_source') != 'dom_stats']
                # 打印诊断信息
                stats_str = ""
                for e in result:
                    if '_domStats' in e:
                        stats_str = e.pop('_domStats', '')
                        break
                if stats_str:
                    logger.info(f"[QZone列表] DOM 诊断: {stats_str}")
                if entries:
                    logger.info(
                        f"[QZone列表] DOM 抓取 {len(entries)} 条, "
                        f"前3条 uin 采样: {[e.get('uin','') for e in entries[:3]]}"
                    )
                return entries
            return []
        except Exception as e:
            logger.warning(f"[QZone列表] DOM 抓取失败: {e}")
            return []

    async def _capture_feed_response(self, response, store: List[Dict], url: str):
        try:
            text = await response.text()
            store.append({"url": url, "text": text})
            logger.info(f"[QZone列表] 拦截到 feeds API! len={len(text)}")
        except Exception as e:
            logger.warning(f"[QZone列表] 拦截失败: {e}")

    @staticmethod
    def _parse_jsonp(text: str) -> Optional[Dict]:
        """解析 JSONP: _Callback({...}); 或纯 JSON

        兼容 QZone 的 malformed JSON + 多回调模式:
        - _Callback({...})  /  QZFL.FeedsCallBack({...}) / QZONE.Feeds 等
        - 结尾无分号只有 \\n
        - 内嵌 JS 单引号字符串 (attach:'' 等)
        - 裸数组 [...]
        """
        if not text:
            return None

        def _fix_js_object(s: str) -> str:
            """把 JS 对象转成合法 JSON"""
            # 1. \\xNN → \\u00NN (JS hex escape → JSON unicode escape)
            s = re.sub(
                r'\\x([0-9a-fA-F]{2})',
                lambda m: '\\u00' + m.group(1).upper(),
                s,
            )
            # 2. Unquoted keys
            s = re.sub(r'([{,]\s*)([a-zA-Z_]\w*)(\s*:)', r'\1"\2"\3', s)
            # 3. Single-quoted values
            def _fix_single_quoted_value(m):
                return m.group(1) + '"' + m.group(2).replace('"', '\\"') + '"'
            s = re.sub(r"(:\s*)'([^']*)'", _fix_single_quoted_value, s)
            # 4. Trailing commas
            s = re.sub(r',\s*([}\]])', r'\1', s)
            # 5. JS literals: 覆盖所有上下文
            s = re.sub(r'([:\[,])\s*undefined\b', r'\1null', s)
            s = re.sub(r'\bundefined\s*([,\]])', r'null\1', s)
            s = re.sub(r'([:\[,])\s*NaN\b', r'\1null', s)
            return s

        def _try_parse_json(json_str: str) -> Optional[Dict]:
            """尝试多种方式解析 JSON 字符串"""
            # 预处理: json5 不支持 JS 字面量 undefined / NaN
            # 覆盖所有上下文: :undefined / [undefined / ,undefined / undefined,
            preprocessed = re.sub(r'([:\[,])\s*undefined\b', r'\1null', json_str)
            preprocessed = re.sub(r'\bundefined\s*([,\]])', r'null\1', preprocessed)
            preprocessed = re.sub(r'([:\[,])\s*NaN\b', r'\1null', preprocessed)
            # 优先用 json5 (原生支持 unquoted keys、trailing commas、单引号)
            try:
                import json5
                result = json5.loads(preprocessed)
                if result is not None:
                    return result
            except Exception as e:
                # 打印错误位置上下文
                err_str = str(e)
                pos_match = re.search(r'column\s+(\d+)', err_str)
                if pos_match:
                    col = int(pos_match.group(1))
                    ctx_start = max(0, col - 60)
                    ctx_end = min(len(preprocessed), col + 60)
                    logger.info(
                        f"_parse_jsonp json5 失败 @col={col}: {err_str[:120]} | "
                        f"上下文[{ctx_start}:{ctx_end}]: {preprocessed[ctx_start:ctx_end]!r}"
                    )
                else:
                    logger.info(
                        f"_parse_jsonp json5 解析失败: {err_str[:120]} | "
                        f"前80字符: {preprocessed[:80]!r}"
                    )
            # 备用: 正则修复后 json.loads
            try:
                fixed = _fix_js_object(json_str)
                return json.loads(fixed)
            except json.JSONDecodeError as e:
                logger.info(
                    f"_parse_jsonp fixed json.loads 失败 @{e.pos}(len={len(json_str)}): "
                    f"{e.msg[:80]} | 上下文: {json_str[max(0,e.pos-40):e.pos+40]!r}"
                )
            except Exception as e:
                logger.warning(f"_parse_jsonp _fix_js_object 异常: {e}")
            # ast.literal_eval
            try:
                import ast
                s2 = _fix_js_object(json_str)
                node = ast.parse(s2, mode='eval')
                if isinstance(node, ast.Expression):
                    return ast.literal_eval(node.body)
            except Exception:
                pass
            return None

        # 多种回调模式
        callback_patterns = [
            r'_Callback\s*\(\s*(\{.*\})\s*\)',
            r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*\s*\(\s*(\{.*\})\s*\)',
            r'window\.[a-zA-Z_]\w*\s*\(\s*(\{.*\})\s*\)',
        ]

        for pattern in callback_patterns:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                result = _try_parse_json(m.group(1))
                if result:
                    return result

        # 兜底: 括号包裹的 JSON
        m = re.search(r'\(\s*(\{.*\})\s*\)', text, re.DOTALL)
        if m:
            result = _try_parse_json(m.group(1))
            if result:
                return result

        # 终极兜底: 纯 JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # ===== 工具方法 =====

    async def _get_cookie_value(self, name: str) -> str:
        if not self._context:
            return ""
        cookies = await self._context.cookies()
        for c in cookies:
            if c["name"] == name:
                return c["value"]
        return ""

    def _save_feed(self, feed_id: str, content: str):
        """保存一条 feed 到本地缓存 (自动去重 + 截断)
        - 按 id 去重, 重复 id 的旧记录会被移除, 新的插到最前
        - 限制单条 content 长度, 缓存最多 50 条
        """
        if not feed_id:
            return
        feeds = self._load_feeds_cache()
        # 按 id 去重 (同 id 视为同一条)
        feeds = [f for f in feeds if f.get("id") != feed_id]
        feeds.insert(0, {
            "id": feed_id,
            "content": (content or "")[:100],
            "time": now_iso(),
        })
        FEEDS_CACHE_FILE.write_text(
            json.dumps(feeds[:50], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _remove_cached_feed(self, feed_id: str):
        feeds = self._load_feeds_cache()
        feeds = [f for f in feeds if not f.get("id", "").startswith(feed_id)]
        FEEDS_CACHE_FILE.write_text(json.dumps(feeds, ensure_ascii=False, indent=2), encoding="utf-8")

    async def get_feed_detail(self, feed_id: str) -> Optional[Dict]:
        """获取某条动态的完整内容 (从缓存或重新抓取)

        优先从本地缓存里查, 找不到再尝试去 QQ 空间详情页抓
        """
        if not feed_id:
            return None
        # 1. 先查本地缓存
        cached = None
        for f in self._load_feeds_cache():
            if f.get("id") == feed_id:
                cached = f
                break
        if cached and cached.get("content"):
            return {
                "id": feed_id,
                "content": cached["content"],
                "time": cached.get("time", ""),
                "images": False,
                "from_cache": True,
            }

        # 2. 重新抓取: 访问详情页
        if not self._page:
            return None
        url = f"https://user.qzone.qq.com/{self._uin}/blog/{feed_id}"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            content = await self._page.evaluate(
                "() => { const el = document.querySelector('.blog_details_text, "
                ".f-info, .f-single-content, .f-ct'); "
                "return el ? (el.innerText || el.textContent || '').trim() : ''; }"
            )
            images = await self._page.evaluate(
                "() => document.querySelectorAll('img').length"
            )
            time_text = await self._page.evaluate(
                "() => { const el = document.querySelector('.blog_details_time, .f-as');"
                " return el ? el.innerText.trim() : ''; }"
            )
            if content:
                self._save_feed(feed_id, content)
                return {
                    "id": feed_id,
                    "content": content[:1000],
                    "time": time_text,
                    "images": images > 0,
                    "from_cache": False,
                }
        except Exception as e:
            logger.error(f"get_feed_detail 抓取失败: {e}")
        return None

    @staticmethod
    def _load_feeds_cache() -> List[Dict]:
        if FEEDS_CACHE_FILE.exists():
            try:
                return json.loads(FEEDS_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []
