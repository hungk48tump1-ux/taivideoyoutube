"""
automation/gemini_controller.py - Điều khiển Gemini bằng Playwright
Logic giống y hệt addon Chrome: chờ 15s ổn định, đếm dòng có chữ,
reload & resend khi sai số dòng.

QUAN TRỌNG: Playwright sync_api phải chạy trên cùng một thread.
"""
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.sync_api import (
    BrowserContext, Page, Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from core.logger import get_logger

logger = get_logger()

STABLE_WAIT_SECONDS = 15  # Giống addon: chờ 15 giây ổn định


class SelectorNotFoundError(Exception):
    pass


class GeminiController:
    GEMINI_URL = "https://gemini.google.com/app"

    def __init__(self, config: Dict[str, Any], selectors: Dict[str, Any]):
        self.config = config
        self.selectors = selectors
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ── Khởi động / Tắt ──

    def launch(self, headless=False, force_close=False, user_data_dir_name="browser_profile"):
        if self._playwright is not None:
            logger.warning("Browser đã mở — gọi reset() trước.")
            return
        user_data_dir = Path(self.config.get("user_data_dir", user_data_dir_name)).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        if force_close:
            self._kill_chrome_on_profile(user_data_dir)
            time.sleep(1.5)
        self._playwright = sync_playwright().start()
        channel = self.config.get("browser_channel", "chrome")
        custom_path = self.config.get("custom_browser_path", "").strip()
        last_err = None
        for ch in [channel, None]:
            try:
                kw = dict(
                    user_data_dir=str(user_data_dir), headless=headless,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-popup-blocking"],
                    ignore_default_args=["--enable-automation"], slow_mo=50,
                )
                if custom_path:
                    import os
                    if os.path.exists(custom_path):
                        kw["executable_path"] = custom_path
                        ch = None  # Playwright requires channel to be None when using executable_path
                if ch:
                    kw["channel"] = ch
                self._context = self._playwright.chromium.launch_persistent_context(**kw)
                self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
        time.sleep(1.5)

    def reset(self):
        try:
            if self._context: self._context.close()
            if self._playwright: self._playwright.stop()
        except Exception:
            pass
        finally:
            self._context = self._playwright = self._page = None

    def close(self):
        try:
            if self._context: self._context.close()
            if self._playwright: self._playwright.stop()
        except Exception as e:
            logger.error("Lỗi đóng browser: %s", e)
        finally:
            self._context = self._playwright = self._page = None

    def _kill_chrome_on_profile(self, user_data_dir: Path):
        import subprocess
        try:
            r = subprocess.run(
                ["wmic", "process", "where",
                 f"name='chrome.exe' and commandline like '%{user_data_dir.name}%'",
                 "get", "ProcessId", "/format:csv"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 2 and parts[-1].strip().isdigit():
                    try:
                        subprocess.run(["taskkill", "/F", "/PID", parts[-1].strip()],
                                       capture_output=True, timeout=3)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Điều hướng ──

    def navigate_to_gemini(self):
        url = self.config.get("gemini_url", self.GEMINI_URL)
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._page.wait_for_timeout(2000)
        self._try_close_popups()

    def navigate_to_url(self, url: str):
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._page.wait_for_timeout(1500)

    def reload_page(self, url: str = ""):
        """F5 trang, chờ load + 5 giây — giống addon reloadAndContinue()."""
        try:
            if url:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            else:
                self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(5000)
            self._try_close_popups()
        except Exception as e:
            logger.warning("reload_page lỗi: %s", e)

    # ── CDP Window ──

    def minimize_window(self):
        if not self._page or self._page.is_closed(): return
        try:
            cdp = self._context.new_cdp_session(self._page)
            r = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {"windowId": r["windowId"], "bounds": {"windowState": "minimized"}})
            cdp.detach()
        except Exception:
            pass

    def restore_window(self):
        if not self._page or self._page.is_closed(): return
        try:
            cdp = self._context.new_cdp_session(self._page)
            r = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {"windowId": r["windowId"],
                      "bounds": {"windowState": "normal", "width": 1280, "height": 900}})
            cdp.detach()
        except Exception:
            pass

    # ── Kiểm tra trạng thái ──

    def is_ready(self) -> Tuple[bool, str]:
        if self._page is None:
            return False, "Browser chưa mở."
        try:
            self._find_element("chat_input", timeout=8000)
            return True, "Gemini sẵn sàng."
        except SelectorNotFoundError:
            return False, "Không tìm thấy ô chat."

    def is_browser_open(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def test_selectors(self, progress_cb=None) -> Dict[str, Any]:
        results = {}
        for name, cfg in self.selectors.items():
            if name.startswith("_"): continue
            found, matched = False, None
            for sel in cfg.get("selectors", []):
                try:
                    if self._page.locator(sel).count() > 0:
                        found, matched = True, sel
                        break
                except Exception:
                    pass
            results[name] = {"found": found, "matched": matched}
            msg = f"  [{name}]: {'✓ ' + matched if found else '✗'}"
            if progress_cb: progress_cb(msg)
        return results

    # ── Chọn model ──

    def ensure_pro_model(self, model_text="Pro") -> bool:
        try:
            btn = self._find_element("model_selector_button", timeout=5000)
            current = btn.inner_text()
            if model_text.lower() in current.lower() and "nhanh" not in current.lower():
                return True
            btn.click()
            self._page.wait_for_timeout(700)
        except SelectorNotFoundError:
            return False
        except Exception:
            return False

        for sel in self.selectors.get("model_option", {}).get("selectors", []):
            try:
                for opt in self._page.locator(f"{sel}:visible").all():
                    try:
                        if model_text.lower() in opt.inner_text().lower():
                            opt.click()
                            self._page.wait_for_timeout(500)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

        try:
            popups = self._page.locator("[role='listbox'], [role='menu']").all()
            if popups:
                opt = popups[-1].locator(f"*:has-text('{model_text}')").last
                if opt.is_visible():
                    opt.click()
                    self._page.wait_for_timeout(500)
                    return True
        except Exception:
            pass

        try:
            for el in reversed(self._page.get_by_text(model_text).all()):
                try:
                    if el.is_visible() and el.is_enabled():
                        tag = el.evaluate("e => e.tagName").lower()
                        role = el.get_attribute("role") or ""
                        if tag in ["li", "span", "div", "button"] or "option" in role or "menuitem" in role:
                            el.click()
                            self._page.wait_for_timeout(500)
                            return True
                except Exception:
                    continue
        except Exception:
            pass

        self._page.keyboard.press("Escape")
        return False

    def _get_response_count(self) -> int:
        """Đếm số lượng khung trả lời hiện có trên trang."""
        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                count = self._page.locator(sel).count()
                if count > 0:
                    return count
            except Exception:
                pass
        return 0

    # ── Paste text ──

    def _clear_chat_input(self, chat_input):
        try:
            chat_input.click()
            self._page.wait_for_timeout(200)
            chat_input.evaluate("""node => {
                node.textContent = '';
                node.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            self._page.wait_for_timeout(200)
        except Exception:
            pass

    def _paste_text_to_chat(self, chat_input, content: str):
        """Dán nội dung vào ô chat - GIỐNG HỆT ADDON (DataTransfer/ClipboardEvent)"""
        if not self._wait_until_ready_for_new_prompt(timeout_seconds=90):
            raise Exception("Gemini chưa sẵn sàng nhận chunk mới")

        # 1. Focus và Xóa nội dung cũ (giống addon)
        chat_input.click()
        self._page.wait_for_timeout(200)
        chat_input.evaluate("""node => {
            node.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
        }""")
        self._page.wait_for_timeout(200)

        # 2. Dán giả lập bằng DataTransfer (Giống hệt insertText trong addon)
        chat_input.evaluate("""(node, text) => {
            const dataTransfer = new DataTransfer();
            dataTransfer.setData('text/plain', text);
            const pasteEvent = new ClipboardEvent('paste', {
                clipboardData: dataTransfer,
                bubbles: true,
                cancelable: true
            });
            node.dispatchEvent(pasteEvent);
        }""", content)
        self._page.wait_for_timeout(500)

        # 3. Kiểm tra và Fallback (Nếu dán giả lập không lên chữ)
        pasted_text = self._read_locator_text(chat_input)
        if not self._text_matches_chunk(pasted_text, content):
            logger.warning("Paste giả lập không khớp, dùng fallback execCommand('insertText')...")
            chat_input.evaluate("""(node, text) => {
                document.execCommand('insertText', false, text);
            }""", content)
            self._page.wait_for_timeout(350)
            
            pasted_text = self._read_locator_text(chat_input)
            if not self._text_matches_chunk(pasted_text, content):
                logger.warning("Vẫn không khớp, dùng fallback keyboard.insert_text...")
                self._page.keyboard.insert_text(content)
                self._page.wait_for_timeout(350)

    # ── Chờ phản hồi (giống addon: nút Send hiện + 15s ổn định) ──

    def _wait_for_response_addon_style(
        self,
        timeout_seconds=900,
        progress_cb=None,
        stop_event=None,
        baseline_text="",
        baseline_count=0,
    ) -> str:
        """
        1 vòng lặp duy nhất — giống hệt waitForResponse() trong addon content.js.

        isBusy = isGenerating || !isSendBtnReady
        - isBusy=True  → reset stableSince, cập nhật lastText, KHÔNG đếm 15s
        - isBusy=False nhưng text == baseline_text → vẫn reset (chưa có chữ mới)
        - isBusy=False VÀ text != baseline_text → bắt đầu đếm 15s ổn định
        """
        deadline = time.time() + timeout_seconds

        def _cb(msg):
            if progress_cb: progress_cb(msg)

        last_text = ""
        stable_since = None

        while time.time() < deadline:
            if stop_event and stop_event.is_set():
                raise Exception("Stopped by user")

            is_generating = self._is_stop_button_visible() or self._is_spinner_visible()
            send_btn_ready = self._is_send_button_ready()
            is_busy = is_generating or not send_btn_ready

            current_text = self._get_latest_response_text()

            if is_busy:
                # Đang sinh chữ → reset mọi thứ, không đếm ổn định
                _cb(f"⏳ đang tạo chữ... ({len(current_text)} ký tự)")
                stable_since = None
                last_text = current_text

            elif current_text == baseline_text:
                # Hết busy nhưng text chưa thay đổi so với trước khi gửi
                # → chưa có câu trả lời mới, tiếp tục chờ
                _cb("⏳ chờ Gemini bắt đầu viết...")
                stable_since = None
                last_text = current_text

            else:
                # Hết busy VÀ đã có text mới → bắt đầu đếm 15s ổn định
                _cb(f"✅ kiểm tra ổn định (đợi 15s)... ({len(current_text)} ký tự)")
                if current_text and current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= STABLE_WAIT_SECONDS:
                        return current_text
                else:
                    stable_since = None
                    last_text = current_text

            time.sleep(0.5)

        raise Exception(f"Timeout khi chờ phản hồi ({timeout_seconds}s)")


    # ── Đếm dòng code block (giống addon getCodeLinesCount) ──

    def _count_code_lines(self, blocks: List[str]) -> int:
        """Đếm dòng có chữ trong code block đầu tiên — giống addon."""
        if not blocks:
            return 0
        text = blocks[0]
        lines = [l for l in text.split('\n') if l.strip()]
        return len(lines)

    # ── Gửi chunk (logic giống addon handleSendChunk) ──

    def send_chunk(
        self,
        content: str = "",
        target_model: str = "Pro",
        expected_lines: Optional[int] = None,
        expected_start_line: Optional[int] = None,
        expected_end_line: Optional[int] = None,
        timeout_seconds: int = 900,
        stable_wait_seconds: int = 15,
        progress_cb: Optional[Callable[[str], None]] = None,
        stop_event: Optional[Any] = None,
        attachment_path: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Gửi chunk, chờ phản hồi, đếm dòng.
        Return: (success, {"text": ..., "blocks": [...], "retry": bool})
        - success=True: chunk OK
        - success=False, retry=True: sai số dòng → app_ui sẽ reload & gửi lại
        - success=False, retry=False: lỗi thật sự → dừng
        """
        def _log(msg):
            logger.info(msg)
            if progress_cb: progress_cb(msg)

        empty_result = {"text": "", "blocks": [], "retry": False}

        # 1. Tìm ô chat
        _log("Tìm ô nhập chat...")
        self._try_close_popups()
        try:
            chat_input = self._find_element("chat_input", timeout=10_000)
        except SelectorNotFoundError as e:
            return False, {**empty_result, "text": str(e)}

        # 2. Chọn model
        if target_model:
            _log(f"Đảm bảo model '{target_model}'...")
            self.ensure_pro_model(target_model)
            self._page.wait_for_timeout(400)
            try:
                chat_input = self._find_element("chat_input", timeout=5_000)
            except SelectorNotFoundError:
                pass

        # 3. Paste text
        _log("Đang nhập nội dung vào ô chat...")
        try:
            self._paste_text_to_chat(chat_input, content)
        except Exception as e:
            return False, {**empty_result, "text": f"Không paste được: {e}"}

        self._random_delay()

        # 4. Lấy baseline trước khi bấm gửi
        baseline_text = self._get_latest_response_text()
        baseline_count = self._get_response_count()

        # 5. Gửi bằng Enter (không tìm nút Send)
        _log("Đang gửi...")
        try:
            chat_input.press("Enter")
        except Exception as e:
            return False, {**empty_result, "text": f"Không gửi được: {e}"}

        _log("Đã gửi. Chờ Gemini phản hồi...")

        # 6. Chờ phản hồi (addon style: nút Send + 15s ổn định)
        try:
            response_text = self._wait_for_response_addon_style(
                timeout_seconds=timeout_seconds,
                progress_cb=progress_cb,
                stop_event=stop_event,
                baseline_text=baseline_text,
                baseline_count=baseline_count
            )
        except Exception as e:
            return False, {**empty_result, "text": str(e)}

        # 6. Lấy code blocks
        code_blocks = self._get_latest_code_blocks()
        _log(f"📦 {len(code_blocks)} code block, response {len(response_text)} ký tự.")

        # 6b. Nếu chưa có code block → chờ thêm tối đa 250s
        if not code_blocks and expected_lines is not None:
            _log("⏳ Chưa thấy code block, chờ tối đa 250s...")
            cb_deadline = time.time() + 250
            while time.time() < cb_deadline:
                if stop_event and stop_event.is_set():
                    return False, {**empty_result, "text": "Stopped by user"}
                self._page.wait_for_timeout(5000)
                elapsed = int(250 - (cb_deadline - time.time()))
                code_blocks = self._get_latest_code_blocks()
                if code_blocks:
                    _log(f"✅ Phát hiện code block sau {elapsed}s. Chờ ổn định thêm 15s...")
                    # Chờ ổn định lại sau khi code block xuất hiện
                    try:
                        current_text = self._get_latest_response_text()
                        response_text = self._wait_for_response_addon_style(
                            timeout_seconds=60,
                            progress_cb=progress_cb,
                            stop_event=stop_event,
                            baseline_text=baseline_text,
                            baseline_count=baseline_count,
                        )
                        code_blocks = self._get_latest_code_blocks()
                    except Exception:
                        pass
                    break
                _log(f"⏳ [{elapsed}/{250}s] Chưa có code block, tiếp tục chờ...")
            else:
                # Hết 250s vẫn không có code block → reload & gửi lại
                _log("❌ Hết 250s vẫn không có code block. Reload & gửi lại.")
                return False, {"text": response_text, "blocks": [], "retry": True}

        # 7. Đếm dòng (giống addon)
        if expected_lines is not None:
            parsed_count = self._count_code_lines(code_blocks)
            if parsed_count != expected_lines:
                _log(f"❌ Sai số dòng ({parsed_count}/{expected_lines}). Cần reload & gửi lại.")
                return False, {"text": response_text, "blocks": code_blocks, "retry": True}
            else:
                _log(f"✅ Số dòng khớp: {parsed_count}/{expected_lines}")

        return True, {"text": response_text, "blocks": code_blocks, "retry": False}

    # ── Helpers ──

    def _find_element(self, selector_key: str, timeout: int = 10_000):
        cfg = self.selectors.get(selector_key, {})
        sels = cfg.get("selectors", [])
        if not sels:
            raise SelectorNotFoundError(f"Không có selector cho '{selector_key}'.")
        per_timeout = max(1000, timeout // max(len(sels), 1))
        for sel in sels:
            try:
                loc = self._page.locator(sel).first
                loc.wait_for(state="visible", timeout=per_timeout)
                return loc
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        raise SelectorNotFoundError(f"Không tìm thấy '{selector_key}'.")

    def _is_stop_button_visible(self) -> bool:
        for sel in self.selectors.get("stop_button", {}).get("selectors", []):
            try:
                if self._page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return False

    def _is_spinner_visible(self) -> bool:
        for sel in self.selectors.get("loading_spinner", {}).get("selectors", []):
            try:
                if self._page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return False

    def _is_send_button_ready(self) -> bool:
        """Kiểm tra nút Send đã hiện và enabled — giống addon isSendBtnReady."""
        for sel in self.selectors.get("send_button", {}).get("selectors", []):
            try:
                btns = self._page.locator(sel).all()
                for btn in reversed(btns):
                    if btn.is_visible() and btn.is_enabled():
                        return True
            except Exception:
                pass
        return False

    def _wait_for_send_button_ready_loc(self, timeout_seconds=30):
        """Chờ nút Send sẵn sàng và trả về locator."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._is_stop_button_visible() or self._is_spinner_visible():
                self._page.wait_for_timeout(500)
                continue
            for sel in self.selectors.get("send_button", {}).get("selectors", []):
                try:
                    for btn in reversed(self._page.locator(sel).all()):
                        if btn.is_visible() and btn.is_enabled():
                            return btn
                except Exception:
                    pass
            self._page.wait_for_timeout(500)
        return None

    def _wait_until_ready_for_new_prompt(self, timeout_seconds=90, log_cb=None) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self._is_stop_button_visible() and not self._is_spinner_visible():
                return True
            self._page.wait_for_timeout(500)
        return False

    def _get_latest_response_text(self) -> str:
        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                containers = self._page.locator(sel).all()
                if containers:
                    return containers[-1].inner_text()
            except Exception:
                pass
        return ""

    def _get_latest_code_blocks(self) -> List[str]:
        """Lấy code blocks từ response cuối — giống addon getLatestCodeBlocks()."""
        script = """
        (container) => {
            let blocks = Array.from(container.querySelectorAll('pre'));
            if (blocks.length === 0)
                blocks = Array.from(container.querySelectorAll('code-block'));
            let valid = blocks.filter(b1 => {
                for (let b2 of blocks) { if (b1 !== b2 && b1.contains(b2)) return false; }
                return true;
            });
            return valid.map(b => {
                let txt = b.innerText || b.textContent || "";
                txt = txt.replace(/^(Plaintext|Python|Javascript|HTML|C\\+\\+|Java|Copy code|Sao chép mã|\\n)+/i, "");
                return txt.trim();
            });
        }
        """
        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                containers = self._page.locator(sel).all()
                if containers:
                    result = containers[-1].evaluate(script)
                    return result if result else []
            except Exception:
                pass
        return []

    def _try_close_popups(self):
        for sel in self.selectors.get("popup_close_button", {}).get("selectors", []):
            try:
                loc = self._page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    self._page.wait_for_timeout(500)
                    break
            except Exception:
                pass

    def _random_delay(self):
        mn = self.config.get("random_delay_min", 0.5)
        mx = self.config.get("random_delay_max", 1.5)
        time.sleep(random.uniform(mn, mx))

    def _read_locator_text(self, locator) -> str:
        try:
            return locator.evaluate('node => node.value || node.innerText || node.textContent || ""')
        except Exception:
            try:
                return locator.inner_text()
            except Exception:
                return ""

    def _normalize_chunk_text(self, text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in text.strip().split("\n")]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _text_matches_chunk(self, actual: str, expected: str) -> bool:
        en = self._normalize_chunk_text(expected)
        an = self._normalize_chunk_text(actual)
        if not en or not an:
            return False
        if an == en or en in an:
            return True
        el = [l.strip() for l in en.split("\n") if l.strip()]
        al = [l.strip() for l in an.split("\n") if l.strip()]
        if not el or not al:
            return False
        pos = 0
        for line in el:
            found = False
            while pos < len(al):
                if line == al[pos] or line in al[pos]:
                    found = True; pos += 1; break
                pos += 1
            if not found:
                return False
        return True

    def get_page(self) -> Optional[Page]:
        return self._page
