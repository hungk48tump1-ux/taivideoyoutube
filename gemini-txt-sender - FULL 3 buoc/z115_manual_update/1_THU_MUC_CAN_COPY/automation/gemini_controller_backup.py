"""
automation/gemini_controller.py - Điều khiển trình duyệt Gemini bằng Playwright

QUAN TRỌNG: Playwright sync_api phải chạy trên cùng một thread từ đầu đến cuối.
GeminiController phải được tạo và sử dụng hoàn toàn trong cùng một thread.
"""
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from core.logger import get_logger

logger = get_logger()


class SelectorNotFoundError(Exception):
    """Không tìm thấy selector nào hợp lệ."""


class GeminiController:
    """
    Điều khiển Gemini web qua Playwright với persistent Chrome profile.

    THREAD SAFETY: Tất cả method phải được gọi từ cùng một thread.
    Tạo instance, gọi launch(), navigate_to_gemini() và send_chunk()
    đều phải nằm trong cùng một thread context.
    """

    GEMINI_URL = "https://gemini.google.com/app"
    BUSY_STALLED_RETRY = "__GEMINI_BUSY_STALLED_RETRY__"
    MAX_CHUNK_RETRIES = 20

    def __init__(self, config: Dict[str, Any], selectors: Dict[str, Any]):
        self.config = config
        self.selectors = selectors
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ─────────────────────────────────────────────
    # Khởi động / Tắt
    # ─────────────────────────────────────────────

    def launch(self, headless: bool = False, force_close: bool = False, user_data_dir_name: str = "browser_profile") -> None:
        """Khởi động browser với persistent profile."""
        if self._playwright is not None:
            logger.warning("Browser đã được khởi động — không thể launch Lần 2. Gọi reset() trước.")
            return

        user_data_dir = Path(self.config.get("user_data_dir", user_data_dir_name)).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("User data dir: %s", user_data_dir)

        if force_close:
            self._kill_chrome_on_profile(user_data_dir)
            time.sleep(1.5)

        self._playwright = sync_playwright().start()
        channel = self.config.get("browser_channel", "chrome")

        last_err = None
        for attempt_channel in [channel, None]:
            try:
                kwargs = dict(
                    user_data_dir=str(user_data_dir),
                    headless=headless,
                    viewport={"width": 1280, "height": 900},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-popup-blocking",
                    ],
                    ignore_default_args=["--enable-automation"],
                    slow_mo=50,
                )
                if attempt_channel:
                    kwargs["channel"] = attempt_channel

                self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
                if len(self._context.pages) > 0:
                    self._page = self._context.pages[0]
                else:
                    self._page = self._context.new_page()
                logger.info("Browser khởi động thành công (channel=%s).", attempt_channel or "chromium")
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning("Channel '%s' thất bại: %s", attempt_channel, e)

        if last_err:
            raise last_err

        time.sleep(1.5)

    def reset(self) -> None:
        """Đóng browser hiện tại để chuẩn bị launch lại."""
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning("reset() warning: %s", e)
        finally:
            self._context = None
            self._playwright = None
            self._page = None
        logger.info("Controller đã reset, sẵn sàng launch lại.")

    def _kill_chrome_on_profile(self, user_data_dir: Path) -> None:
        """Kill các tiến trình Chrome đang dùng cùng user_data_dir."""
        import subprocess
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 f"name='chrome.exe' and commandline like '%{user_data_dir.name}%'",
                 "get", "ProcessId", "/format:csv"],
                capture_output=True, text=True, timeout=5
            )
            pids = []
            for line in result.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 2 and parts[-1].strip().isdigit():
                    pids.append(int(parts[-1].strip()))

            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=3)
                    logger.info("Đã kill Chrome PID %d", pid)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("_kill_chrome_on_profile: %s", e)

    def navigate_to_gemini(self) -> None:
        """Mở trang Gemini theo URL trong config."""
        url = self.config.get("gemini_url", self.GEMINI_URL)
        logger.info("Điều hướng tới: %s", url)
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._page.wait_for_timeout(2000)
        self._try_close_popups()

    def navigate_to_url(self, url: str) -> None:
        """Diều hướng tới một URL cụ thể."""
        logger.info("Mở URL: %s", url)
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._page.wait_for_timeout(1500)

    def minimize_window(self) -> None:
        """Thu nhỏ cửa sổ Chrome xuống taskbar qua CDP."""
        if not self._page or self._page.is_closed():
            return
        try:
            cdp = self._context.new_cdp_session(self._page)
            result = cdp.send("Browser.getWindowForTarget")
            window_id = result["windowId"]
            cdp.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {"windowState": "minimized"}
            })
            cdp.detach()
            logger.info("Chrome đã được thu nhỏ.")
        except Exception as e:
            logger.debug("minimize_window lỗi: %s", e)

    def restore_window(self) -> None:
        """Hiện lại cửa sổ Chrome và đưa lên trước màn hình qua CDP."""
        if not self._page or self._page.is_closed():
            return
        try:
            cdp = self._context.new_cdp_session(self._page)
            result = cdp.send("Browser.getWindowForTarget")
            window_id = result["windowId"]
            cdp.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {"windowState": "normal", "width": 1280, "height": 900}
            })
            cdp.detach()
            logger.info("Chrome đã được hiện lại.")
        except Exception as e:
            logger.debug("restore_window lỗi: %s", e)

    def close(self) -> None:
        """Đóng browser và giải phóng tài nguyên."""
        try:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.error("Lỗi khi đóng browser: %s", e)
        finally:
            self._context = None
            self._playwright = None
            self._page = None

    # ─────────────────────────────────────────────
    # Kiểm tra trạng thái
    # ─────────────────────────────────────────────

    def is_ready(self) -> Tuple[bool, str]:
        """Kiểm tra xem Gemini đã sẵn sàng nhận input chưa."""
        if self._page is None:
            return False, "Browser chưa được khởi động."
        try:
            self._find_element("chat_input", timeout=8000)
            return True, "Gemini sẵn sàng."
        except SelectorNotFoundError:
            return False, "Không tìm thấy ô chat. Vui lòng kiểm tra Gemini đã mở chưa."

    def is_browser_open(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def test_selectors(
        self, progress_cb: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """Kiểm tra từng selector có tìm thấy trên trang hiện tại không."""
        results = {}
        for name, cfg in self.selectors.items():
            if name.startswith("_"):
                continue
            sels = cfg.get("selectors", [])
            found, matched = False, None
            for sel in sels:
                try:
                    if self._page.locator(sel).count() > 0:
                        found, matched = True, sel
                        break
                except Exception:
                    pass
            results[name] = {"found": found, "matched": matched}
            msg = f"  [{name}]: {'✓ ' + matched if found else '✗ không tìm thấy'}"
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)
        return results

    # ─────────────────────────────────────────────
    # Chọn model
    # ─────────────────────────────────────────────

    def ensure_pro_model(self, model_text: str = "Pro") -> bool:
        """Đảm bảo model hiện tại là model có chứa model_text."""
        logger.info("Đảm bảo model chứa '%s'...", model_text)
        try:
            btn = self._find_element("model_selector_button", timeout=5000)
            current = btn.inner_text()
            if model_text.lower() in current.lower() and "nhanh" not in current.lower():
                logger.info("Model đã đúng: %s", current.strip())
                return True
            logger.info("Model hiện tại '%s', đang đổi sang '%s'...", current.strip(), model_text)
            btn.click()
            self._page.wait_for_timeout(700)
        except SelectorNotFoundError:
            logger.warning("Không tìm thấy nút model. Bỏ qua việc chọn model.")
            return False
        except Exception as e:
            logger.error("Lỗi click model button: %s", e)
            return False

        for sel in self.selectors.get("model_option", {}).get("selectors", []):
            try:
                options = self._page.locator(f"{sel}:visible").all()
                for opt in options:
                    try:
                        if model_text.lower() in opt.inner_text().lower():
                            opt.click()
                            logger.info("Đã CLICK chọn model chứa '%s'.", model_text)
                            self._page.wait_for_timeout(500)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

        try:
            popups = self._page.locator("[role='listbox'], [role='menu']").all()
            if popups:
                last_popup = popups[-1]
                opt = last_popup.locator(f"*:has-text('{model_text}')").last
                if opt.is_visible():
                    opt.click()
                    self._page.wait_for_timeout(500)
                    return True
        except Exception as e:
            logger.debug("Lỗi khi tìm trong popup menu: %s", e)

        try:
            elements = self._page.get_by_text(model_text).all()
            for el in reversed(elements):
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
        except Exception as e:
            logger.debug("Lỗi khi brute-force tìm option: %s", e)

        logger.error("Không click được vào menu chứa '%s'.", model_text)
        self._page.keyboard.press("Escape")
        return False

    # ─────────────────────────────────────────────
    # Gửi chunk
    # ─────────────────────────────────────────────

    def send_chunk(
        self,
        content: str = "",
        attachment_path: Optional[str] = None,
        target_model: str = "Pro",
        expected_lines: Optional[int] = None,
        expected_start_line: Optional[int] = None,
        expected_end_line: Optional[int] = None,
        timeout_seconds: int = 300,
        stable_wait_seconds: int = 5,
        progress_cb: Optional[Callable[[str], None]] = None,
        stop_event: Optional[Any] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Nhập nội dung hoặc đính kèm file vào ô chat, gửi, chờ Gemini phản hồi xong.
        Trả về (success, {"text": ..., "blocks": [...]}).
        """
        def _log(msg: str):
            logger.info(msg)
            if progress_cb:
                progress_cb(msg)

        # ── 1. Tìm ô chat ──
        _log("Tìm ô nhập chat...")
        self._try_close_popups()
        try:
            chat_input = self._find_element("chat_input", timeout=10_000)
        except SelectorNotFoundError as e:
            return False, {"text": str(e), "blocks": []}


        # ── 2. Chọn model ──
        if target_model:
            _log(f"Đang đảm bảo model là '{target_model}'...")
            self.ensure_pro_model(target_model)
            self._page.wait_for_timeout(400)

            # Tìm lại chat_input vì DOM có thể re-render sau khi đổi model
            try:
                chat_input = self._find_element("chat_input", timeout=5_000)
            except SelectorNotFoundError:
                pass  # giữ nguyên chat_input cũ nếu không tìm lại được

        # ── 2.5 Nhập nội dung hoặc đính kèm file ──
        if attachment_path:
            self._clear_chat_input(chat_input)
            _log(f"Đang đính kèm file chunk: {Path(attachment_path).name}")
            if not self._attach_file_to_chat(attachment_path, _log):
                return False, {"text": f"Không thể đính kèm file: {attachment_path}", "blocks": []}
        else:
            _log("Đang nhập nội dung vào ô chat...")
            try:
                self._paste_text_to_chat(chat_input, content)
            except Exception as e:
                return False, {"text": f"Không thể nhập nội dung: {e}", "blocks": []}

        self._random_delay()
        baseline_response_text = self._get_latest_response_text()

        # ── 3. Gửi ──
        _log("Đang gửi...")
        try:
            send_btn = self._wait_for_send_button_ready(timeout_seconds=30, log_cb=_log)
            if send_btn is None:
                return False, {"text": "Nut gui chua san sang sau khi paste chunk", "blocks": []}
            send_btn.click()
            if False and not self._wait_for_send_confirmed(chat_input, content, timeout_seconds=5):
                _log("Chua xac nhan da gui sau lan bam dau, bam gui lai...")
                resent = False
                for resend_attempt in range(1, 3):
                    send_btn = self._wait_for_send_button_ready(timeout_seconds=10, log_cb=_log)
                    if send_btn is None:
                        break
                    send_btn.click()
                    if self._wait_for_send_confirmed(chat_input, content, timeout_seconds=5):
                        resent = True
                        break
                    _log(f"Chua xac nhan da gui sau lan bam lai {resend_attempt}.")
                if not resent:
                    return False, {"text": "Khong xac nhan duoc Gemini da nhan chunk sau khi bam gui", "blocks": []}
        except Exception as e:
            return False, {"text": f"Khong the bam nut gui: {e}", "blocks": []}
        except SelectorNotFoundError:
            logger.warning("Không tìm thấy nút gửi, dùng Enter.")
            try:
                chat_input.press("Enter")
            except Exception as e:
                return False, {"text": f"Không thể gửi: {e}", "blocks": []}

        _log("Đã gửi. Chờ Gemini phản hồi...")

        # ── 4. Chờ Gemini phản hồi + Validate code block ──
        _log("Đã bấm gửi, chờ Gemini phản hồi...")
        final_success = False
        final_response_text = ""
        final_code_blocks: List[str] = []
        t0 = time.time()
        wait_baseline_text = baseline_response_text
        retry_count = 0
        max_retries = int(self.config.get("max_chunk_retries", self.MAX_CHUNK_RETRIES))
        extra_wait_seconds = int(self.config.get("no_codeblock_extra_wait_seconds", 200))

        _log(f"📋 Cấu hình: max_retries={max_retries} | extra_wait={extra_wait_seconds}s | timeout={timeout_seconds}s | stable={stable_wait_seconds}s")

        while retry_count < max_retries:
            # ── 4a. Chờ Gemini viết xong (Stop button biến mất + text ổn định) ──
            retry_count += 1
            _log(f"── Lần thử {retry_count}/{max_retries} | Đã chạy {time.time() - t0:.0f}s ──")

            success, response_text = self._wait_for_response(
                timeout_seconds=timeout_seconds,
                stable_wait_seconds=stable_wait_seconds,
                progress_cb=progress_cb,
                baseline_text=wait_baseline_text,
                stop_event=stop_event,
            )

            if stop_event is not None and stop_event.is_set():
                _log("⏹ Dừng theo yêu cầu user.")
                final_success, final_response_text, final_code_blocks = False, "Stopped by user", []
                break

            if not success:
                _log(f"⚠️ Gemini không phản hồi. Lý do: {response_text}")
                _log(f"   → Bấm Thử lại (lần {retry_count}/{max_retries})...")
                wait_baseline_text = self._retry_current_chunk(
                    reason=response_text, content=content,
                    baseline_text=wait_baseline_text,
                    retry_count=retry_count, log_cb=_log,
                )
                continue

            resp_len = len(response_text)
            _log(f"📝 Gemini đã phản hồi: {resp_len} ký tự.")

            # ── 4b. Đọc code blocks (CHỈ từ response cuối cùng) ──
            code_blocks = self._get_latest_code_blocks(
                expected_lines=expected_lines,
                expected_start_line=expected_start_line,
                expected_end_line=expected_end_line,
            )
            _log(f"📦 Tìm thấy {len(code_blocks)} code block trong response cuối.")

            # Không cần validate → xong
            if expected_lines is None:
                _log("ℹ️ Không yêu cầu validate (expected_lines=None) → chấp nhận kết quả.")
                if self._is_stop_button_visible() or self._is_spinner_visible():
                    _log("⚠️ Gemini vẫn đang chạy dù đã có kết quả. Reload trang...")
                    self._reload_after_valid_busy(_log)
                final_success, final_response_text, final_code_blocks = True, response_text, code_blocks
                break

            # ── 4c. Nếu 0 code block → chờ thêm (Gemini có thể đang viết code block) ──
            if not code_blocks:
                _log(f"⏳ 0 code block — chờ thêm tối đa {extra_wait_seconds}s (poll mỗi 10s)...")
                poll_interval = 10
                waited = 0
                while waited < extra_wait_seconds:
                    if stop_event is not None and stop_event.is_set():
                        break
                    self._page.wait_for_timeout(poll_interval * 1000)
                    waited += poll_interval
                    code_blocks = self._get_latest_code_blocks(
                        expected_lines=expected_lines,
                        expected_start_line=expected_start_line,
                        expected_end_line=expected_end_line,
                    )
                    if code_blocks:
                        _log(f"✅ Code block xuất hiện sau {waited}s chờ thêm! Chờ Gemini hoàn thiện...")
                        self._wait_for_response(
                            timeout_seconds=timeout_seconds,
                            stable_wait_seconds=stable_wait_seconds,
                            progress_cb=progress_cb,
                            baseline_text="",
                            stop_event=stop_event,
                        )
                        response_text = self._get_latest_response_text() or response_text
                        code_blocks = self._get_latest_code_blocks(
                            expected_lines=expected_lines,
                            expected_start_line=expected_start_line,
                            expected_end_line=expected_end_line,
                        )
                        break
                    _log(f"⏳ Đã chờ {waited}/{extra_wait_seconds}s, chưa có code block...")

                if stop_event is not None and stop_event.is_set():
                    _log("⏹ Dừng theo yêu cầu user (trong lúc chờ code block).")
                    final_success, final_response_text, final_code_blocks = False, "Stopped by user", []
                    break

            # ── 4d. Validate code blocks ──
            expected_numbers = None
            if expected_start_line is not None and expected_end_line is not None:
                expected_numbers = list(range(expected_start_line, expected_end_line + 1))

            code_lines_count = 0
            range_valid = False
            best_block_idx = -1
            if code_blocks:
                for bi, block in enumerate(code_blocks):
                    count = self._count_keyframe_lines(block)
                    numbers = self._extract_keyframe_numbers(block)
                    r_ok = numbers == expected_numbers if expected_numbers is not None else True
                    _log(f"   Block {bi+1}/{len(code_blocks)}: {count} dòng keyframe | Range {'OK' if r_ok else 'SAI'}")
                    if count == expected_lines and r_ok:
                        code_lines_count = count
                        range_valid = True
                        best_block_idx = bi
                        break
                    if count > code_lines_count:
                        code_lines_count = count
                        range_valid = r_ok
                        best_block_idx = bi

            parsed_count = self._parse_received_count(response_text)
            range_status = "OK" if range_valid else (f"SAI, cần {expected_start_line}-{expected_end_line}" if expected_numbers else "—")

            # Log chi tiết 3 điều kiện
            cond1 = code_lines_count == expected_lines
            cond2 = parsed_count == expected_lines
            _log(f"🔎 Validate 3 điều kiện (lần {retry_count}/{max_retries}):")
            _log(f"   ① Code block lines: {code_lines_count}/{expected_lines} → {'✅' if cond1 else '❌'}")
            _log(f"   ② Bot báo count:     {parsed_count}/{expected_lines} → {'✅' if cond2 else '❌'}")
            _log(f"   ③ Range:             {range_status}")

            is_valid = cond1 and cond2

            if is_valid:
                elapsed = time.time() - t0
                _log(f"✅ HỢP LỆ sau {retry_count} lần thử ({elapsed:.1f}s)! Code block: {code_lines_count} dòng.")
                final_success, final_response_text, final_code_blocks = True, response_text, code_blocks
                break

            # ── 4e. Sai → Bấm Thử lại ──
            fail_details = []
            if not code_blocks:
                fail_details.append(f"0 code block sau {extra_wait_seconds}s")
            else:
                if not cond1:
                    fail_details.append(f"code={code_lines_count}, cần {expected_lines}")
                if not cond2:
                    fail_details.append(f"bot báo={parsed_count}, cần {expected_lines}")
            fail_summary = " | ".join(fail_details)
            _log(f"⚠️ KHÔNG hợp lệ: {fail_summary}")
            _log(f"   → Bấm Thử lại (lần {retry_count}/{max_retries})...")

            wait_baseline_text = self._retry_current_chunk(
                reason=f"Sai output: {fail_summary}",
                content=content,
                baseline_text=response_text,
                retry_count=retry_count,
                log_cb=_log,
            )
            # continue → quay lại 4a

        else:
            # while kết thúc bình thường = đã hết max_retries
            elapsed = time.time() - t0
            _log(f"❌ ĐÃ THỬ {max_retries} LẦN ({elapsed:.1f}s) VẪN KHÔNG HỢP LỆ. DỪNG CHUNK NÀY.")
            _log(f"   Kết quả cuối: code={code_lines_count} | bot={parsed_count} | cần={expected_lines}")
            final_success = False
            final_response_text = f"Max retries ({max_retries}) exceeded. code={code_lines_count}, bot={parsed_count}, need={expected_lines}"
            final_code_blocks = []

        _log(f"Phản hồi sau {time.time() - t0:.1f}s | {len(final_response_text)} ký tự | {len(final_code_blocks)} code block.")
        return final_success, {"text": final_response_text, "blocks": final_code_blocks}

    def _clear_chat_input(self, chat_input) -> None:
        try:
            chat_input.click()
            self._page.wait_for_timeout(200)
            chat_input.evaluate("""node => {
                node.textContent = '';
                node.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            self._page.wait_for_timeout(200)
        except Exception as e:
            logger.debug("Không xóa được ô chat trước khi gửi: %s", e)

    def _paste_text_to_chat_legacy(self, chat_input, content: str) -> None:
        try:
            self._clear_chat_input(chat_input)
            self._page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", content)
            chat_input.click()
            self._page.wait_for_timeout(100)
            chat_input.press("Control+a")
            chat_input.press("Delete")
            chat_input.press("Control+v")
            self._page.wait_for_timeout(350)

            if len(chat_input.inner_text().strip()) < 5:
                raise Exception("Trống DOM sau khi Ctrl+V")
        except Exception as e:
            logger.warning("Lỗi paste clipboard (%s), thử insert_text...", e)
            self._clear_chat_input(chat_input)
            chat_input.click()
            self._page.wait_for_timeout(100)
            self._page.keyboard.insert_text(content)
            self._page.wait_for_timeout(350)
            if len(chat_input.inner_text().strip()) < 5:
                raise Exception("Không thể nhập text vào ô chat")

    def _paste_text_to_chat(self, chat_input, content: str) -> None:
        if not self._wait_until_ready_for_new_prompt(timeout_seconds=90):
            raise Exception("Gemini chua san sang nhan chunk moi")

        last_error = ""
        for attempt in range(1, 3):
            try:
                self._clear_chat_input(chat_input)
                self._page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", content)
                chat_input.click()
                self._page.wait_for_timeout(100)
                chat_input.press("Control+a")
                chat_input.press("Delete")
                chat_input.press("Control+v")
                self._page.wait_for_timeout(350)

                pasted_text = self._read_locator_text(chat_input)
                if self._text_matches_chunk(pasted_text, content):
                    return
                last_error = f"Noi dung sau Ctrl+V khong khop chunk (lan {attempt})"
                logger.warning(last_error)
            except Exception as e:
                last_error = f"Loi paste clipboard lan {attempt}: {e}"
                logger.warning(last_error)

        logger.warning("Paste clipboard khong xac nhan duoc, thu insert_text...")
        self._clear_chat_input(chat_input)
        chat_input.click()
        self._page.wait_for_timeout(100)
        self._page.keyboard.insert_text(content)
        self._page.wait_for_timeout(350)
        inserted_text = self._read_locator_text(chat_input)
        if not self._text_matches_chunk(inserted_text, content):
            raise Exception(f"Khong the nhap dung chunk vao o chat: {last_error}")

    def _attach_file_to_chat(self, attachment_path: str, log_cb: Callable[[str], None]) -> bool:
        path = Path(attachment_path)
        if not path.exists() or not path.is_file():
            log_cb(f"Không tìm thấy file chunk: {attachment_path}")
            return False

        try:
            chat_input = self._find_element("chat_input", timeout=10_000)
            chat_input.scroll_into_view_if_needed()
            chat_input.click()
            self._page.wait_for_timeout(300)

            if not self._click_add_menu_button(chat_input, log_cb):
                log_cb("Không tìm thấy hoặc không bấm được nút dấu cộng.")
                return False

            self._page.wait_for_timeout(500)
            if not self._click_upload_file_option(chat_input, path, log_cb):
                log_cb("Không bấm được mục Tải tệp lên.")
                return False

            self._page.wait_for_timeout(2000)
            return self._wait_for_attachment_ready(path.name, log_cb)
        except Exception as e:
            log_cb(f"Không thể tải file lên Gemini: {e}")
            return False

    def _get_composer_box(self, chat_input) -> Optional[Dict[str, float]]:
        boxes = []
        try:
            box = chat_input.bounding_box()
            if box:
                boxes.append(box)
        except Exception:
            pass

        try:
            parent_boxes = chat_input.evaluate("""node => {
                const boxes = [];
                let cur = node;
                for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {
                    const r = cur.getBoundingClientRect();
                    if (r.width > 300 && r.height > 40) {
                        boxes.push({x: r.x, y: r.y, width: r.width, height: r.height});
                    }
                }
                return boxes;
            }""")
            boxes.extend(parent_boxes or [])
        except Exception:
            pass

        if not boxes:
            return None
        return max(boxes, key=lambda b: float(b.get("width", 0)) * float(b.get("height", 0)))

    def _click_add_menu_button(self, chat_input, log_cb: Callable[[str], None]) -> bool:
        box = self._get_composer_box(chat_input)
        if box:
            x = float(box["x"]) + 32
            y = float(box["y"]) + float(box["height"]) - 33
            try:
                log_cb(f"Bấm dấu cộng theo tọa độ: x={x:.0f}, y={y:.0f}")
                self._page.mouse.click(x, y)
                self._page.wait_for_timeout(700)
                return True
            except Exception as e:
                logger.debug("Bấm dấu cộng theo tọa độ lỗi: %s", e)

        for sel in self.selectors.get("add_menu_button", {}).get("selectors", []):
            try:
                buttons = self._page.locator(sel).all()
                for btn in reversed(buttons):
                    if btn.is_visible():
                        btn.click()
                        return True
            except Exception as e:
                logger.debug("add_menu_button '%s' lỗi: %s", sel, e)
        return False

    def _click_upload_file_option(self, chat_input, path: Path, log_cb: Callable[[str], None]) -> bool:
        box = self._get_composer_box(chat_input)
        if box:
            x = float(box["x"]) + 95
            y = float(box["y"]) - 135
            try:
                log_cb(f"Bấm Tải tệp lên theo tọa độ: x={x:.0f}, y={y:.0f}")
                with self._page.expect_file_chooser(timeout=5000) as fc_info:
                    self._page.mouse.click(x, y)
                fc_info.value.set_files(str(path))
                log_cb(f"Đã chọn file qua Tải tệp lên: {path.name}")
                return True
            except Exception as e:
                logger.debug("Bấm Tải tệp lên theo tọa độ lỗi: %s", e)

        for sel in self.selectors.get("upload_file_option", {}).get("selectors", []):
            try:
                opts = self._page.locator(sel).all()
                for opt in reversed(opts):
                    if not opt.is_visible():
                        continue
                    try:
                        with self._page.expect_file_chooser(timeout=5000) as fc_info:
                            opt.click()
                        fc_info.value.set_files(str(path))
                        log_cb(f"Đã chọn file qua Tải tệp lên: {path.name}")
                        return True
                    except Exception as e:
                        logger.debug("upload_file_option '%s' không mở file chooser: %s", sel, e)
            except Exception as e:
                logger.debug("upload_file_option '%s' lỗi: %s", sel, e)
        return False

    def _wait_for_attachment_ready(self, filename: str, log_cb: Callable[[str], None]) -> bool:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                body = self._page.inner_text("body")
                if filename in body:
                    log_cb(f"Đã thấy file đính kèm: {filename}")
                    return True
            except Exception:
                pass
            self._page.wait_for_timeout(500)

        log_cb(f"Không xác nhận được tên file '{filename}' trên trang, tiếp tục nếu nút gửi đã sẵn sàng.")
        try:
            send_btn = self._find_element("send_button", timeout=5000)
            return send_btn.is_enabled()
        except Exception:
            return False

    def _parse_received_count(self, response_text: str) -> Optional[int]:
        """
        Parse số keyframe/dòng từ các pattern trong response text của Gemini.
        Ví dụ: 'RECEIVED: 51 content lines → 51 keyframes'  → 51
               '→ 48 keyframes'                              → 48
        Trả về None nếu không tìm thấy.
        """
        import re
        patterns = [
            "Đã xảy ra lỗi",
            "Không thể tạo",
            r'RECEIVED\s*:\s*\d+[^→]*→\s*(\d+)\s*keyframes?',
            r'→\s*(\d+)\s*keyframes?',
            r'(\d+)\s*keyframes?',
            r'RECEIVED\s*:\s*(\d+)\s+content\s+lines?',
        ]
        for pattern in patterns:
            m = re.search(pattern, response_text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    def _extract_keyframe_numbers(self, block_text: str) -> List[int]:
        import re
        numbers: List[int] = []
        for line in block_text.splitlines():
            match = re.match(r'^\s*\[(\d{1,4})\]', line)
            if match:
                numbers.append(int(match.group(1)))
        return numbers

    def _count_keyframe_lines(self, block_text: str) -> int:
        """
        Đếm số dòng keyframe thực sự trong code block.
        Chỉ đếm dòng bắt đầu bằng pattern [NN] (VD: [01], [02], ..., [30]).
        Bỏ qua: dòng trống, dòng header, dòng wrapper (bracket, variable assign...).

        Nếu không tìm thấy pattern [NN] → fallback đếm dòng non-empty có nội dung thực.
        """
        import re
        lines = block_text.split('\n')

        # Primary: đếm dòng bắt đầu bằng [số] — format chuẩn của keyframe/prompt
        keyframe_pattern = re.compile(r'^\s*\[\d{1,4}\]')
        keyframe_count = sum(1 for l in lines if keyframe_pattern.match(l))
        if keyframe_count > 0:
            return keyframe_count

        # Fallback: lọc bỏ các dòng structural rõ ràng, chỉ giữ dòng có nội dung thực
        structural = re.compile(
            r'^\s*('
            r'\[|\]|\{|\}|\(|\)|,$|'           # chỉ có bracket/brace/dấu phẩy
            r'[A-Z_][\w_]*\s*=\s*[\[{(]|'      # variable assignment: VAR = [
            r'#.*|'                              # comment
            r'={3,}|-{3,}|_{3,}'               # separator ===, ---, ___
            r')\s*$'
        )
        content_lines = [l for l in lines if l.strip() and not structural.match(l)]
        return len(content_lines)


    def _retry_current_chunk(
        self,
        reason: str,
        content: str,
        baseline_text: str,
        retry_count: int,
        log_cb: Callable[[str], None],
    ) -> str:
        if reason == self.BUSY_STALLED_RETRY:
            reason = "Gemini dung yen"
        log_cb(f"Retry chunk hien tai lan {retry_count}. Ly do: {reason}")

        if not content:
            log_cb("Khong co noi dung chunk de xac minh/gửi lai.")
            time.sleep(2)
            return self._get_latest_response_text() or baseline_text

        latest_user_prompt = self._get_latest_user_prompt_text()
        if self._text_matches_chunk(latest_user_prompt, content):
            log_cb("Da xac nhan prompt cuoi dung chunk. Bam Thu lai...")
            self._reveal_regenerate_button()
            if self._click_regenerate_button():
                log_cb("Da bam Thu lai, cho phan hoi moi...")
                self._page.wait_for_timeout(1500)
                return self._get_latest_response_text() or baseline_text
            log_cb("Prompt cuoi dung chunk nhung khong bam duoc Thu lai.")
        else:
            if latest_user_prompt:
                log_cb("Prompt cuoi khong khop chunk hien tai. Khong bam Thu lai, se gui lai dung chunk.")
            else:
                log_cb("Khong doc duoc prompt cuoi. Khong regenerate mu, se gui lai dung chunk.")

        log_cb("Thu gui lai prompt hien tai bang copy-paste da verify...")
        if self._click_send_current_prompt(content):
            self._page.wait_for_timeout(1500)
            return self._get_latest_response_text() or baseline_text

        log_cb("Chua retry duoc, cho 2s roi thu lai cung chunk.")
        time.sleep(2)
        return self._get_latest_response_text() or baseline_text

    def _click_regenerate_button(self) -> bool:
        """Tìm và bấm vào nút Thử lại của câu trả lời cuối cùng."""
        try:
            container_sels = self.selectors.get("response_container", {}).get("selectors", [])
            last_container = None
            for sel in container_sels:
                conts = self._page.locator(sel).all()
                if conts:
                    last_container = conts[-1]
                    break

            base_loc = last_container if last_container else self._page

            btn_clicked = False
            for sel in self.selectors.get("regenerate_button", {}).get("selectors", []):
                try:
                    btns = base_loc.locator(sel).all()
                    for btn in reversed(btns):
                        if btn.is_visible():
                            btn.click()
                            self._page.wait_for_timeout(600)
                            btn_clicked = True
                            break
                except Exception:
                    continue
                if btn_clicked:
                    break

            if not btn_clicked:
                logger.warning("Không tìm thấy regenerate_button.")
                return False

            for sel in self.selectors.get("regenerate_option", {}).get("selectors", []):
                try:
                    opts = self._page.locator(sel).all()
                    for opt in reversed(opts):
                        if opt.is_visible():
                            opt.click()
                            self._page.wait_for_timeout(1000)
                            return True
                except Exception:
                    continue

            logger.info("Không thấy menu regenerate_option; coi như nút Thử lại đã chạy trực tiếp.")
            return True

        except Exception as e:
            logger.debug("Lỗi khi bấm Thử lại: %s", e)
            return False

    def _reveal_regenerate_button(self) -> None:
        try:
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._page.wait_for_timeout(1200)
            self._page.mouse.move(640, 750)
            self._page.wait_for_timeout(800)
        except Exception:
            pass

    def _click_stop_button(self) -> bool:
        for sel in self.selectors.get("stop_button", {}).get("selectors", []):
            try:
                buttons = self._page.locator(sel).all()
                for btn in reversed(buttons):
                    if btn.is_visible():
                        btn.click()
                        self._page.wait_for_timeout(1000)
                        logger.info("Đã bấm nút dừng response.")
                        return True
            except Exception:
                pass

        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(500)
            logger.info("Không tìm thấy nút dừng, đã nhấn Escape.")
            return False
        except Exception as e:
            logger.debug("Không bấm được nút dừng response: %s", e)
            return False

    # ─────────────────────────────────────────────
    # Chờ response
    # ─────────────────────────────────────────────

    def _wait_for_response(
        self,
        timeout_seconds: int = 300,
        stable_wait_seconds: int = 5,
        progress_cb: Optional[Callable[[str], None]] = None,
        baseline_text: str = "",
        stop_event: Optional[Any] = None,
    ) -> Tuple[bool, str]:
        """
        Chờ Gemini trả lời xong theo 3 giai đoạn:
          1. Chờ bắt đầu sinh (Stop button xuất hiện hoặc text thay đổi)
          2. Chờ Gemini viết xong (Stop button biến mất)
          3. Chờ text ổn định (stable_wait_seconds)
        KHÔNG tự bấm Dừng khi đang viết — text có thể dừng nhưng code block đang được tạo.
        """
        deadline = time.time() + max(30, timeout_seconds)
        baseline_norm = self._normalize_response_text(baseline_text)

        def _cb(msg: str):
            if progress_cb:
                progress_cb(msg)

        # ── Phase 1: Chờ bắt đầu sinh ──
        phase1_deadline = time.time() + min(45, max(15, timeout_seconds // 4))
        started = False
        while time.time() < phase1_deadline:
            if stop_event is not None and stop_event.is_set():
                return False, "Stopped by user"
            if self._is_stop_button_visible() or self._is_spinner_visible():
                started = True
                _cb("⏳ Gemini đang tạo phản hồi...")
                break
            current_norm = self._normalize_response_text(self._get_latest_response_text())
            if current_norm and current_norm != baseline_norm:
                started = True
                _cb("Đã thấy phản hồi mới, đang chờ Gemini viết xong...")
                break
            error_text = self._get_gemini_error_text()
            if error_text:
                return False, error_text
            time.sleep(0.5)

        if not started:
            logger.warning("Không phát hiện Gemini bắt đầu sinh, tiếp tục chờ bằng heuristic.")

        # ── Phase 2: Chờ Stop button biến mất (Gemini đang viết text + code block) ──
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False, "Stopped by user"

            is_busy = self._is_stop_button_visible() or self._is_spinner_visible()
            raw_current = self._get_latest_response_text()
            cur_len = len(raw_current)

            if is_busy:
                _cb(f"  {cur_len} ký tự | ⏳ Gemini đang viết...")
                time.sleep(0.5)
                continue

            # Stop biến mất → chuyển sang kiểm tra ổn định
            _cb(f"  {cur_len} ký tự | ✅ Gemini đã dừng, chờ ổn định...")
            break
        else:
            return False, f"Hết thời gian chờ Gemini viết ({timeout_seconds}s)"

        # ── Phase 3: Chờ text ổn định (không đổi trong stable_wait_seconds) ──
        stable_since: Optional[float] = None
        last_text = ""

        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False, "Stopped by user"

            # Nếu Stop button xuất hiện lại → quay lại Phase 2
            if self._is_stop_button_visible() or self._is_spinner_visible():
                _cb(f"  ⏳ Gemini viết tiếp, chờ...")
                stable_since = None
                last_text = ""
                time.sleep(0.5)
                continue

            raw_current = self._get_latest_response_text()
            current_norm = self._normalize_response_text(raw_current)

            # Chưa có text mới so với baseline → chờ tiếp
            if not raw_current or current_norm == baseline_norm:
                stable_since = None
                time.sleep(0.5)
                continue

            cur_len = len(raw_current)
            _cb(f"  {cur_len} ký tự | ✅ chờ ổn định...")

            if raw_current == last_text:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_wait_seconds:
                    return True, raw_current
            else:
                stable_since = None
                last_text = raw_current

            time.sleep(0.5)

        # Timeout nhưng có text → trả về
        raw_current = self._get_latest_response_text()
        if raw_current and self._normalize_response_text(raw_current) != baseline_norm:
            return True, raw_current
        return False, f"Hết thời gian chờ ({timeout_seconds}s)"

    # ─────────────────────────────────────────────
    # Helpers nội bộ
    # ─────────────────────────────────────────────

    def _normalize_response_text(self, text: str) -> str:
        return " ".join((text or "").split())

    def _normalize_chunk_text(self, text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in text.strip().split("\n")]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _text_matches_chunk(self, actual: str, expected: str) -> bool:
        expected_norm = self._normalize_chunk_text(expected)
        actual_norm = self._normalize_chunk_text(actual)
        if not expected_norm or not actual_norm:
            return False
        if actual_norm == expected_norm or expected_norm in actual_norm:
            return True

        expected_lines = [line.strip() for line in expected_norm.split("\n") if line.strip()]
        actual_lines = [line.strip() for line in actual_norm.split("\n") if line.strip()]
        if not expected_lines or not actual_lines:
            return False
        if expected_lines[0] not in actual_lines[0] and expected_lines[0] not in actual_norm:
            return False
        if expected_lines[-1] not in actual_lines[-1] and expected_lines[-1] not in actual_norm:
            return False

        pos = 0
        for expected_line in expected_lines:
            found = False
            while pos < len(actual_lines):
                if expected_line == actual_lines[pos] or expected_line in actual_lines[pos]:
                    found = True
                    pos += 1
                    break
                pos += 1
            if not found:
                return False
        return True

    def _read_locator_text(self, locator) -> str:
        try:
            return locator.evaluate("""node => {
                return node.value || node.innerText || node.textContent || "";
            }""")
        except Exception:
            try:
                return locator.inner_text()
            except Exception:
                return ""

    def _get_latest_user_prompt_text(self) -> str:
        selectors = [
            "div[data-message-author-role='user']",
            "[data-message-author-role='user']",
            "user-query",
            "user-message",
            "message-content[data-message-author-role='user']",
            ".user-query",
            ".user-message",
            "div.query-text",
        ]
        for sel in selectors:
            try:
                items = self._page.locator(sel).all()
                if items:
                    text = self._read_locator_text(items[-1]).strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""

    def _wait_until_ready_for_new_prompt(
        self,
        timeout_seconds: int = 90,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> bool:
        deadline = time.time() + timeout_seconds
        last_log_at = 0.0
        while time.time() < deadline:
            if not self._is_stop_button_visible() and not self._is_spinner_visible():
                return True
            if log_cb and time.time() - last_log_at >= 5:
                log_cb("Gemini van dang tra loi, cho truoc khi gui chunk moi...")
                last_log_at = time.time()
            self._page.wait_for_timeout(500)
        return False

    def _reload_after_valid_busy(self, log_cb: Optional[Callable[[str], None]] = None) -> None:
        try:
            self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(2000)
            self._try_close_popups()
            if log_cb:
                log_cb("Da reload trang Gemini sau chunk hop le con dang chay.")
        except Exception as e:
            logger.warning("Reload sau chunk hop le bi loi: %s", e)
            if log_cb:
                log_cb(f"Reload sau chunk hop le bi loi: {e}")

    def _wait_for_send_button_ready(
        self,
        timeout_seconds: int = 30,
        log_cb: Optional[Callable[[str], None]] = None,
    ):
        deadline = time.time() + timeout_seconds
        last_log_at = 0.0
        while time.time() < deadline:
            if self._is_stop_button_visible() or self._is_spinner_visible():
                if log_cb and time.time() - last_log_at >= 5:
                    log_cb("Gemini dang busy, chua bam gui...")
                    last_log_at = time.time()
                self._page.wait_for_timeout(500)
                continue

            for sel in self.selectors.get("send_button", {}).get("selectors", []):
                try:
                    buttons = self._page.locator(sel).all()
                    for btn in reversed(buttons):
                        if btn.is_visible() and btn.is_enabled():
                            return btn
                except Exception:
                    pass

            if log_cb and time.time() - last_log_at >= 5:
                log_cb("Chua thay nut gui san sang, tiep tuc cho...")
                last_log_at = time.time()
            self._page.wait_for_timeout(500)
        return None

    def _wait_for_send_confirmed(self, chat_input, content: str, timeout_seconds: int = 5) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            latest_user_prompt = self._get_latest_user_prompt_text()
            if self._text_matches_chunk(latest_user_prompt, content):
                return True

            if self._is_stop_button_visible() or self._is_spinner_visible():
                return True

            current_text = self._read_locator_text(chat_input).strip()
            if not current_text:
                self._page.wait_for_timeout(500)
                latest_user_prompt = self._get_latest_user_prompt_text()
                if self._text_matches_chunk(latest_user_prompt, content):
                    return True

            self._page.wait_for_timeout(250)
        return False

    def _get_gemini_error_text(self) -> str:
        patterns = [
            "Đã xảy ra lỗi",
            "Da xay ra loi",
            "Something went wrong",
            "There was an error",
            "Try again later",
            "response was blocked",
            "Không thể tạo",
            "Khong the tao",
        ]
        try:
            body = self._page.inner_text("body")[-5000:]
        except Exception:
            return ""
        body_lower = body.lower()
        for pattern in patterns:
            if pattern.lower() in body_lower:
                return f"Gemini báo lỗi: {pattern}"
        return ""


    def _click_send_current_prompt(self, content: str) -> bool:
        try:
            chat_input = self._find_element("chat_input", timeout=5000)
            try:
                current_text = self._read_locator_text(chat_input)
                if not self._text_matches_chunk(current_text, content):
                    self._paste_text_to_chat(chat_input, content)
            except Exception:
                self._paste_text_to_chat(chat_input, content)

            send_btn = self._wait_for_send_button_ready(timeout_seconds=30)
            if send_btn is None:
                logger.warning("Nut gui chua san sang, khong bam gui lai prompt.")
                return False
            send_btn.click()
            return True
        except Exception as e:
            logger.warning("Khong gui lai duoc prompt hien tai: %s", e)
            return False

    def _find_element(self, selector_key: str, timeout: int = 10_000):
        """Thử từng selector theo thứ tự ưu tiên."""
        cfg = self.selectors.get(selector_key, {})
        sels: List[str] = cfg.get("selectors", [])

        if not sels:
            raise SelectorNotFoundError(f"Không có selector cho '{selector_key}'.")

        per_timeout = max(1000, timeout // max(len(sels), 1))

        for sel in sels:
            try:
                loc = self._page.locator(sel).first
                loc.wait_for(state="visible", timeout=per_timeout)
                logger.debug("Tìm thấy '%s' với selector: %s", selector_key, sel)
                return loc
            except PlaywrightTimeoutError:
                continue
            except Exception as e:
                logger.debug("Selector '%s' lỗi: %s", sel, e)
                continue

        raise SelectorNotFoundError(
            f"Không tìm thấy '{selector_key}'. Hãy cập nhật selectors.json."
        )

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

    def _get_latest_response_text(self) -> str:
        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                containers = self._page.locator(sel).all()
                if containers:
                    return containers[-1].inner_text()
            except Exception:
                pass
        try:
            logger.warning("Không tìm thấy response_container, fallback đọc body.")
            return self._page.inner_text("body")[-3000:]
        except Exception:
            return ""

    def _get_latest_code_blocks(
        self,
        expected_lines: Optional[int] = None,
        expected_start_line: Optional[int] = None,
        expected_end_line: Optional[int] = None,
    ) -> List[str]:
        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                containers = self._page.locator(sel).all()
                if containers:
                    script = """
                    (container) => {
                        let blocks = Array.from(container.querySelectorAll('pre'));
                        if (blocks.length === 0) {
                            blocks = Array.from(container.querySelectorAll('code-block'));
                        }
                        let validBlocks = blocks.filter(b1 => {
                            for (let b2 of blocks) {
                                if (b1 !== b2 && b1.contains(b2)) return false;
                            }
                            return true;
                        });
                        return validBlocks.map(b => {
                            let txt = b.innerText || b.textContent || "";
                            txt = txt.replace(/^(Plaintext|Python|Javascript|HTML|C\\+\\+|Java|Copy code|Sao chép mã|\\n)+/i, "");
                            return txt.trim();
                        });
                    }
                    """
                    # CHỈ lấy code block từ response CUỐI CÙNG (mới nhất)
                    # Không duyệt ngược các response cũ để tránh lấy nhầm code block cũ
                    last_container = containers[-1]
                    code_texts = last_container.evaluate(script)
                    if code_texts:
                        return code_texts
                    # Response cuối chưa có code block → trả rỗng
                    return []
            except Exception as e:
                logger.debug("Lỗi lấy code block: %s", e)
        return []

    def get_all_code_blocks_from_page(self) -> List[List[str]]:
        """
        Soát lại TOÀN BỘ trang Gemini, đọc code blocks từ MỌI response container.
        Trả về: [[blocks_response_1], [blocks_response_2], ...]
        Dùng sau khi gửi chunk cuối để xác nhận kết quả.
        """
        extract_script = """
        (container) => {
            let blocks = Array.from(container.querySelectorAll('pre'));
            if (blocks.length === 0) {
                blocks = Array.from(container.querySelectorAll('code-block'));
            }
            let validBlocks = blocks.filter(b1 => {
                for (let b2 of blocks) {
                    if (b1 !== b2 && b1.contains(b2)) return false;
                }
                return true;
            });
            return validBlocks.map(b => {
                let txt = b.innerText || b.textContent || "";
                txt = txt.replace(/^(Plaintext|Python|Javascript|HTML|C\\+\\+|Java|Copy code|Sao chép mã|\\n)+/i, "");
                return txt.trim();
            }).filter(t => t.length > 0);
        }
        """
        all_blocks: List[List[str]] = []

        for sel in self.selectors.get("response_container", {}).get("selectors", []):
            try:
                containers = self._page.locator(sel).all()
                if not containers:
                    continue
                for container in containers:
                    try:
                        code_texts = container.evaluate(extract_script)
                        all_blocks.append(code_texts if code_texts else [])
                    except Exception:
                        all_blocks.append([])
                logger.info(
                    "Soát trang: %d response, %d có code blocks, tổng %d blocks.",
                    len(all_blocks),
                    sum(1 for b in all_blocks if b),
                    sum(len(b) for b in all_blocks),
                )
                return all_blocks
            except Exception as e:
                logger.debug("Lỗi soát code blocks toàn trang: %s", e)

        return all_blocks

    def _try_close_popups(self) -> None:
        for sel in self.selectors.get("popup_close_button", {}).get("selectors", []):
            try:
                loc = self._page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    self._page.wait_for_timeout(500)
                    logger.info("Đã đóng popup: %s", sel)
                    break
            except Exception:
                pass

    def _random_delay(self) -> None:
        mn = self.config.get("random_delay_min", 0.5)
        mx = self.config.get("random_delay_max", 1.5)
        time.sleep(random.uniform(mn, mx))

    def get_page(self) -> Optional[Page]:
        return self._page
