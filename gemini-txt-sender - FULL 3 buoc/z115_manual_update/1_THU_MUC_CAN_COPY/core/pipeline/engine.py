import os
import threading
import time
import concurrent.futures
from typing import Callable, List, Dict, Any
from core.pipeline.file_parser import parse_prompts
from core.pipeline.utils import encode_image_to_base64_data_url, download_file, setup_output_directories
from core.pipeline.api_client import APIClient
import core.pipeline.config as config

MAIN_ATTEMPTS = 1

class PipelineEngine:
    def __init__(self, api_client: APIClient, callbacks: dict):
        self.api = api_client
        self.callbacks = callbacks # {'log': func, 'progress': func, 'completed': func, 'error': func}
        self.is_paused = False
        self.is_stopped = False
        
    def _log(self, message: str, level: str = "info"):
        if 'log' in self.callbacks:
             self.callbacks['log'](message, level)
             
    def _update_progress(self, section: str, current: int, total: int):
        if 'progress' in self.callbacks:
             self.callbacks['progress'](section, current, total)

    def _report_failed_item(
        self,
        section: str,
        index: int,
        name: str,
        prompt: str,
        output_dir: str,
        filepath: str,
        media_type: str,
        reason: str,
        reference_path: str = "",
        item_id: str = "",
    ):
        cb = self.callbacks.get('failed_item')
        if cb:
            cb({
                "id": item_id or f"{section}:{index}:{name}",
                "section": section,
                "index": index,
                "name": name,
                "prompt": prompt,
                "output_dir": output_dir,
                "filepath": filepath,
                "media_type": media_type,
                "reason": reason,
                "reference_path": reference_path,
            })

    def _notify_retry_success(self, item: Dict[str, Any]):
        cb = self.callbacks.get('retry_success')
        if cb:
            cb(item)
             
    def stop(self):
        self.is_stopped = True
        
    def pause(self):
        self.is_paused = True
        
    def resume(self):
        self.is_paused = False
        
    def _wait_if_paused(self):
        while self.is_paused and not self.is_stopped:
            time.sleep(1)

    def _get_worker_count(self, settings: dict, key: str) -> int:
        try:
            return max(1, int(settings.get(key, settings.get('threads', 1))))
        except (TypeError, ValueError):
            return 1

    def _task_error_reason(self, result: Dict[str, Any]) -> str:
        status = result.get("status", "unknown")
        error = result.get("error") or result.get("message") or result.get("detail") or ""
        if "timeout" in str(error).lower():
            return f"Task timeout {config.TASK_TIMEOUT_SECONDS}s"
        if status == "failed":
            return f"Task failed: {error or 'không rõ lỗi'}"
        if status == "error":
            return f"Task error: {error or 'không rõ lỗi'}"
        return f"Task {status}: {error or 'không rõ lỗi'}"

    def run_pipeline(self, whisk_file, veo_file, luot2_file, reference_image, settings):
        """Chạy toàn bộ pipeline trên một luồng riêng để không block UI."""
        thread = threading.Thread(
            target=self._pipeline_logic, 
            args=(whisk_file, veo_file, luot2_file, reference_image, settings),
            daemon=True
        )
        thread.start()

    def run_retry_items(self, items: List[Dict[str, Any]], reference_image: str, settings: dict):
        thread = threading.Thread(
            target=self._retry_items_logic,
            args=(items, reference_image, settings),
            daemon=True
        )
        thread.start()

    def _retry_items_logic(self, items: List[Dict[str, Any]], reference_image: str, settings: dict):
        self.is_stopped = False
        self.is_paused = False
        try:
            self._log(f"[RETRY] Bắt đầu chạy lại {len(items)} mục lỗi.", "INFO")
            if not self.api.check_health():
                self._log("[RETRY] Không thể kết nối Webhook API Server.", "ERROR")
                if 'error' in self.callbacks:
                    self.callbacks['error']("Connection failed")
                return

            shared_ref_b64 = encode_image_to_base64_data_url(reference_image) if reference_image else None

            retry_sections = ("WHISK_IMAGES", "VEO_VIDEOS", "LUOT2_IMAGES")
            retry_totals = {
                section: sum(1 for item in items if item.get("section", "") == section)
                for section in retry_sections
            }
            retry_completed = {section: 0 for section in retry_sections}
            retry_progress_lock = threading.Lock()
            for section, total in retry_totals.items():
                if total:
                    self._update_progress(section, 0, total)

            def mark_retry_done(section: str) -> None:
                if section not in retry_totals or not retry_totals[section]:
                    return
                with retry_progress_lock:
                    retry_completed[section] += 1
                    self._update_progress(section, retry_completed[section], retry_totals[section])

            def retry_one(pos: int, item: Dict[str, Any]) -> None:
                self._wait_if_paused()
                if self.is_stopped:
                    return

                section = item.get("section", "")
                prompt = item.get("prompt", "")
                name = item.get("name", f"retry_{pos}")
                output_dir = item.get("output_dir") or os.getcwd()
                filepath = item.get("filepath") or os.path.join(output_dir, name)
                os.makedirs(output_dir, exist_ok=True)

                if section in ("WHISK_IMAGES", "LUOT2_IMAGES"):
                    ok = self._retry_image_item(item, prompt, filepath, shared_ref_b64, settings)
                elif section == "VEO_VIDEOS":
                    ok = self._retry_video_item(item, prompt, filepath, settings)
                else:
                    ok = False
                    self._log(f"[RETRY] Không nhận diện được loại mục lỗi: {name}", "ERROR")

                if ok:
                    self._notify_retry_success(item)
                    if section == "WHISK_IMAGES":
                        dependent_video = dependent_videos_by_index.get(int(item.get("index", pos - 1)))
                        if dependent_video and not self.is_stopped:
                            _video_pos, video_item = dependent_video
                            video_item["reference_path"] = filepath
                            self._log(
                                f"[RETRY] Anh {int(item.get('index', pos - 1)) + 1} da xong, tu dong chay video cung so.",
                                "INFO",
                            )
                            retry_one(_video_pos, video_item)
                elif not self.is_stopped:
                    self._report_failed_item(
                        section=section,
                        index=int(item.get("index", pos - 1)),
                        name=name,
                        prompt=prompt,
                        output_dir=output_dir,
                        filepath=filepath,
                        media_type=item.get("media_type", "image"),
                        reason=item.get("reason", "Chạy lại vẫn thất bại"),
                        reference_path=item.get("reference_path", ""),
                        item_id=item.get("id", ""),
                    )
                    if section == "WHISK_IMAGES":
                        dependent_video = dependent_videos_by_index.get(int(item.get("index", pos - 1)))
                        if dependent_video:
                            _video_pos, video_item = dependent_video
                            self._report_failed_item(
                                section="VEO_VIDEOS",
                                index=int(video_item.get("index", _video_pos - 1)),
                                name=video_item.get("name", f"retry_{_video_pos}"),
                                prompt=video_item.get("prompt", ""),
                                output_dir=video_item.get("output_dir") or os.getcwd(),
                                filepath=video_item.get("filepath") or os.path.join(video_item.get("output_dir") or os.getcwd(), video_item.get("name", f"retry_{_video_pos}")),
                                media_type="video",
                                reason="Anh cung so chua retry thanh cong",
                                reference_path=video_item.get("reference_path", ""),
                                item_id=video_item.get("id", ""),
                            )
                            mark_retry_done("VEO_VIDEOS")

                mark_retry_done(section)

            image_workers = self._get_worker_count(settings, 'image_threads')
            video_workers = self._get_worker_count(settings, 'video_threads')
            image_items = [
                (pos, item) for pos, item in enumerate(items, start=1)
                if item.get("section", "") in ("WHISK_IMAGES", "LUOT2_IMAGES")
            ]
            video_items = [
                (pos, item) for pos, item in enumerate(items, start=1)
                if item.get("section", "") == "VEO_VIDEOS"
            ]
            image_indexes = {
                int(item.get("index", pos - 1))
                for pos, item in image_items
                if item.get("section", "") == "WHISK_IMAGES"
            }
            dependent_videos_by_index = {
                int(item.get("index", pos - 1)): (pos, item)
                for pos, item in video_items
                if int(item.get("index", pos - 1)) in image_indexes
                and not item.get("reference_path")
            }
            runnable_video_items = [
                (pos, item) for pos, item in video_items
                if int(item.get("index", pos - 1)) not in dependent_videos_by_index
            ]
            other_items = [
                (pos, item) for pos, item in enumerate(items, start=1)
                if item.get("section", "") not in ("WHISK_IMAGES", "LUOT2_IMAGES", "VEO_VIDEOS")
            ]

            self._log(
                f"[RETRY] Chay song song: {len(image_items)} muc anh voi {image_workers} luong, "
                f"{len(video_items)} muc video voi {video_workers} luong.",
                "INFO",
            )

            futures = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=image_workers) as image_executor, \
                 concurrent.futures.ThreadPoolExecutor(max_workers=video_workers) as video_executor:
                for pos, item in image_items:
                    if self.is_stopped:
                        break
                    futures.append(image_executor.submit(retry_one, pos, item))
                for pos, item in runnable_video_items:
                    if self.is_stopped:
                        break
                    futures.append(video_executor.submit(retry_one, pos, item))
                for pos, item in other_items:
                    if self.is_stopped:
                        break
                    futures.append(image_executor.submit(retry_one, pos, item))

                for future in concurrent.futures.as_completed(futures):
                    if self.is_stopped:
                        break
                    future.result()

            if not self.is_stopped:
                self._log("[RETRY] Đã chạy xong danh sách mục lỗi.", "SUCCESS")
                if 'completed' in self.callbacks:
                    self.callbacks['completed']()
        except Exception as e:
            self._log(f"[RETRY] LỖI: {e}", "ERROR")
            if 'error' in self.callbacks:
                self.callbacks['error'](str(e))

    def _retry_image_item(self, item: Dict[str, Any], prompt: str, filepath: str, ref_img_b64: str, settings: dict) -> bool:
        if not ref_img_b64:
            item["reason"] = "Không tìm thấy ảnh đầu vào"
            self._log(f"[RETRY] Thiếu ảnh tham chiếu cho {item.get('name', filepath)}.", "ERROR")
            return False

        for _attempt in range(config.MAX_RETRIES):
            if self.is_stopped:
                return False
            self._wait_if_paused()
            task_id = self.api.generate_image(
                prompt=prompt,
                model=settings.get('image_model', config.DEFAULT_IMAGE_MODEL),
                reference_image_b64=ref_img_b64,
                category=settings.get('category', config.DEFAULT_CATEGORY),
                aspect_ratio=settings.get('aspect_ratio', config.DEFAULT_ASPECT_RATIO)
            )
            if not task_id:
                item["reason"] = "Không nhận được task_id"
                time.sleep(2)
                continue

            result = self.api.wait_for_task(
                task_id,
                poll_interval=max(1, settings.get('poll_interval', int(config.POLL_INTERVAL_SECONDS))),
                timeout=config.TASK_TIMEOUT_SECONDS
            )
            if result.get("status") != "completed":
                item["reason"] = self._task_error_reason(result)
                time.sleep(2)
                continue

            file_results = result.get("results", [])
            file_ref = file_results[0] if file_results and isinstance(file_results, list) else file_results
            if not file_ref:
                item["reason"] = "Không có file_url"
                continue
            file_url = self.api.get_file_url(file_ref)
            if not file_url:
                item["reason"] = "Không có file_url"
                continue
            if file_url and download_file(file_url, filepath):
                self._log(f"[RETRY] ✅ Đã tạo lại ảnh: {item.get('name', os.path.basename(filepath))}", "SUCCESS")
                return True
            item["reason"] = "Download failed"

        self._log(f"[RETRY] ❌ Tạo lại ảnh thất bại: {item.get('name', os.path.basename(filepath))}", "ERROR")
        return False

    def _retry_video_item(self, item: Dict[str, Any], prompt: str, filepath: str, settings: dict) -> bool:
        reference_path = item.get("reference_path", "")
        if not reference_path or not os.path.exists(reference_path):
            item["reason"] = "Không tìm thấy ảnh đầu vào"
            self._log(f"[RETRY] Thiếu ảnh đầu vào cho video: {item.get('name', filepath)}", "ERROR")
            return False

        vid_ref_b64 = encode_image_to_base64_data_url(reference_path)
        if not vid_ref_b64:
            item["reason"] = "Không đọc được ảnh đầu vào"
            self._log(f"[RETRY] Không đọc được ảnh đầu vào video: {reference_path}", "ERROR")
            return False

        for _attempt in range(config.MAX_RETRIES):
            if self.is_stopped:
                return False
            self._wait_if_paused()
            task_id = self.api.generate_video(
                prompt=prompt,
                model=settings.get('video_model', config.DEFAULT_VIDEO_MODEL),
                aspect_ratio=settings.get('aspect_ratio', config.DEFAULT_ASPECT_RATIO),
                mode=settings.get('video_mode', config.DEFAULT_VIDEO_MODE),
                reference_images_b64=[vid_ref_b64],
                resolution=[settings.get('resolution', config.DEFAULT_RESOLUTION)]
            )
            if not task_id:
                item["reason"] = "Không nhận được task_id"
                time.sleep(2)
                continue

            result = self.api.wait_for_task(
                task_id,
                poll_interval=max(1, settings.get('poll_interval', int(config.POLL_INTERVAL_SECONDS))),
                timeout=config.TASK_TIMEOUT_SECONDS
            )
            if result.get("status") != "completed":
                item["reason"] = self._task_error_reason(result)
                time.sleep(2)
                continue

            file_results = result.get("results", [])
            file_ref = file_results[0] if file_results and isinstance(file_results, list) else file_results
            if not file_ref:
                item["reason"] = "Không có file_url"
                continue
            file_url = self.api.get_file_url(file_ref)
            if not file_url:
                item["reason"] = "Không có file_url"
                continue
            if file_url and download_file(file_url, filepath):
                self._log(f"[RETRY] ✅ Đã tạo lại video: {item.get('name', os.path.basename(filepath))}", "SUCCESS")
                return True

        self._log(f"[RETRY] ❌ Tạo lại video thất bại: {item.get('name', os.path.basename(filepath))}", "ERROR")
        item.setdefault("reason", "Download failed")
        return False
        
    def _generate_images_loop(
        self,
        prompts: List[str],
        ref_img_b64: str,
        output_dir: str,
        prefix: str,
        settings: dict,
        section_name: str,
        require_reference: bool = False,
    ) -> List[str]:
         """Hàm chạy loop tạo ảnh và trả về danh sách các đường dẫn ảnh đã tạo, dùng đa luồng"""
         total = len(prompts)
         results = [None] * total
         completed_count = 0
         lock = threading.Lock()

         if require_reference and not ref_img_b64:
             raise Exception(f"{section_name} cần ảnh tham chiếu nhưng chưa đọc được ảnh.")

         ref_mode = "prompt + ảnh tham chiếu" if ref_img_b64 else "prompt only"
         self._log(f"[{section_name}] Chế độ tạo ảnh: {ref_mode}.", "INFO")
         
         def worker(i, prompt):
             nonlocal completed_count
             filename = f"{i+1}_{prefix}.png"
             filepath = os.path.join(output_dir, filename)
             retries = 0
             success = False
             last_error = "Không rõ lỗi"
             
             while retries < MAIN_ATTEMPTS and not success and not self.is_stopped:
                 self._wait_if_paused()
                 if self.is_stopped: break
                 
                 task_id = self.api.generate_image(
                     prompt=prompt, 
                     model=settings.get('image_model', config.DEFAULT_IMAGE_MODEL),
                     reference_image_b64=ref_img_b64,
                     category=settings.get('category', config.DEFAULT_CATEGORY),
                     aspect_ratio=settings.get('aspect_ratio', config.DEFAULT_ASPECT_RATIO)
                 )
                 
                 if not task_id:
                     last_error = "Không nhận được task_id"
                     retries += 1
                     time.sleep(2)
                     continue
                     
                 # Wait for task
                 result = self.api.wait_for_task(
                     task_id, 
                     poll_interval=max(1, settings.get('poll_interval', int(config.POLL_INTERVAL_SECONDS))),
                     timeout=config.TASK_TIMEOUT_SECONDS
                 )
                 
                 if result.get("status") == "completed":
                     file_results = result.get("results", [])
                     if file_results and isinstance(file_results, list):
                         file_ref = file_results[0]
                     else:
                         file_ref = file_results
                     if not file_ref:
                         last_error = "Không có file_url"
                         retries += 1
                         continue
                     file_url = self.api.get_file_url(file_ref)
                     if not file_url:
                         last_error = "Không có file_url"
                         retries += 1
                         continue
                         
                     if file_url and download_file(file_url, filepath):
                         self._log(f"[{section_name}] ✅ Ảnh {i+1}/{total} đã tải xong.", "SUCCESS")
                         results[i] = filepath
                         success = True
                     else:
                         last_error = "Download failed"
                         retries += 1
                 else:
                     last_error = self._task_error_reason(result)
                     retries += 1
                     time.sleep(2)
                     
             if not success and not self.is_stopped:
                 self._log(f"[{section_name}] Lý do lỗi ảnh {i+1}: {last_error}", "ERROR")
                 self._log(f"[{section_name}] ❌ Bỏ qua ảnh {i+1} sau {MAIN_ATTEMPTS} lần thử thất bại.", "ERROR")
                 self._report_failed_item(
                     section=section_name,
                     index=i,
                     name=filename,
                     prompt=prompt,
                     output_dir=output_dir,
                     filepath=filepath,
                     media_type="image",
                     reason=last_error,
                 )
                 
             with lock:
                 completed_count += 1
                 self._update_progress(section_name, completed_count, total)

         max_workers = self._get_worker_count(settings, 'image_threads')
         self._log(f"[{section_name}] Đang chạy {total} ảnh với {max_workers} luồng song song...", "INFO")

         start_index = 0
         if ref_img_b64 and total > 1 and max_workers > 1:
             self._log(f"[{section_name}] Làm nóng cache ảnh tham chiếu bằng ảnh đầu tiên trước khi chạy song song.", "INFO")
             worker(0, prompts[0])
             start_index = 1
          
         with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
             next_index = start_index
             running = set()

             while next_index < total and len(running) < max_workers and not self.is_stopped:
                 running.add(executor.submit(worker, next_index, prompts[next_index]))
                 next_index += 1

             while running and not self.is_stopped:
                 done, running = concurrent.futures.wait(
                     running,
                     return_when=concurrent.futures.FIRST_COMPLETED,
                 )
                 for future in done:
                     future.result()

                 while next_index < total and len(running) < max_workers and not self.is_stopped:
                     running.add(executor.submit(worker, next_index, prompts[next_index]))
                     next_index += 1
             
         return results

    def _pipeline_logic(self, whisk_file, veo_file, luot2_file, reference_image, settings):
        self.is_stopped = False
        self.is_paused = False
        
        try:
             run_step1 = bool(settings.get('run_step1', True))
             run_step2 = bool(settings.get('run_step2', True))
             run_step3 = bool(settings.get('run_step3', True))
             needs_step1 = run_step1 or run_step2

             input_file_for_dir = (whisk_file if needs_step1 else None) or (veo_file if run_step2 else None) or (luot2_file if run_step3 else None)
             if not input_file_for_dir:
                 raise Exception("Không tìm thấy file đầu vào hợp lệ.")
                 
             base_input_dir = os.path.dirname(input_file_for_dir)
             folder_name = os.path.basename(base_input_dir)
             if not folder_name:
                 folder_name = "output"
                 
             # Chúng ta sẽ lưu vào các thư mục con trong thư mục đầu vào
             img_whisk_dir = os.path.join(base_input_dir, "30whisk")
             video_dir = os.path.join(base_input_dir, "30VEO")
             img_luot2_dir = os.path.join(base_input_dir, "luot2")
             
             os.makedirs(img_whisk_dir, exist_ok=True)
             os.makedirs(video_dir, exist_ok=True)
             os.makedirs(img_luot2_dir, exist_ok=True)
             
             # Verify Server Health
             self._log("Đang kiểm tra kết nối server...", "INFO")
             if not self.api.check_health():
                 self._log("Không thể kết nối đến Webhook API Server. Hãy kiểm tra URL và cấu hình.", "ERROR")
                 if 'error' in self.callbacks: self.callbacks['error']("Connection failed")
                 return
                 
             # Đọc dữ liệu
             self._log("Đang đọc dữ liệu đầu vào...", "INFO")
             whisk_prompts = parse_prompts(whisk_file) if needs_step1 else []
             veo_prompts = parse_prompts(veo_file) if run_step2 else []
             luot2_prompts = parse_prompts(luot2_file) if run_step3 and luot2_file else []

             if needs_step1 and not whisk_prompts:
                 raise Exception("Bước 1 hoặc Bước 2 cần file 30WHISK có prompt hợp lệ.")
             if run_step2 and not veo_prompts:
                 raise Exception("Bước 2 cần file 30VEO có prompt hợp lệ.")
             if run_step3 and not luot2_prompts:
                 raise Exception("Bước 3 cần file Luot2 có prompt hợp lệ.")
              
             ref_img_b64 = encode_image_to_base64_data_url(reference_image) if reference_image else None
             if whisk_prompts:
                 if not reference_image:
                     raise Exception("Bước 1 (WHISK) cần ảnh tham chiếu. Vui lòng chọn file ảnh ở ô 'Tham Chiếu Ảnh'.")
                 if not ref_img_b64:
                     raise Exception(f"Không đọc được ảnh tham chiếu cho WHISK: {reference_image}")
                 self._log("Đã nạp ảnh tham chiếu cho WHISK và sẽ gửi trong reference_images.", "SUCCESS")

             if luot2_prompts:
                 if not reference_image:
                     raise Exception("Bước 3 (Lượt 2) cần ảnh tham chiếu. Vui lòng chọn file ảnh ở ô 'Tham Chiếu Ảnh'.")
                 if not ref_img_b64:
                     raise Exception(f"Không đọc được ảnh tham chiếu cho Lượt 2: {reference_image}")
                 self._log("Đã nạp ảnh tham chiếu cho Lượt 2 và sẽ gửi trong reference_images.", "SUCCESS")

             # --- BƯỚC 1: TẠO ẢNH TỪ WHISK ---
             whisk_generated_images = []
             if whisk_prompts:
                 self._log(f"🚀 BƯỚC 1 (WHISK): Bắt đầu tạo {len(whisk_prompts)} ảnh. Thư mục: {folder_name}", "INFO")
                 whisk_generated_images = self._generate_images_loop(
                     whisk_prompts, ref_img_b64, img_whisk_dir, folder_name, settings, "WHISK_IMAGES", require_reference=True
                 )
             
             # --- BƯỚC 2 VÀ BƯỚC 3 CHẠY SONG SONG ---
             video_workers = self._get_worker_count(settings, 'video_threads')
             image_workers = self._get_worker_count(settings, 'image_threads')
             if veo_prompts and luot2_prompts:
                 self._log(
                     f"[PIPELINE] B2/B3 chay song song: {video_workers} luong video + {image_workers} luong anh.",
                     "INFO",
                 )

             def run_step2():
                 if veo_prompts and not self.is_stopped:
                     self._log(f"🚀 BƯỚC 2 (VEO): Bắt đầu tạo {len(veo_prompts)} video.", "INFO")
                     total_v = len(veo_prompts)
                     completed_v = 0
                     v_lock = threading.Lock()
                     
                     def video_worker(i, prompt):
                         nonlocal completed_v
                         self._wait_if_paused()
                         if self.is_stopped: return

                         filename = f"{i+1}_{folder_name}.mp4"
                         filepath = os.path.join(video_dir, filename)
                         reference_path = ""
                          
                         vid_ref_b64 = None
                         if i < len(whisk_generated_images) and whisk_generated_images[i]:
                             reference_path = whisk_generated_images[i]
                             vid_ref_b64 = encode_image_to_base64_data_url(whisk_generated_images[i])
                          
                         if not vid_ref_b64:
                             self._log(f"[VEO_VIDEOS] ⚠️ Không tìm thấy ảnh đầu vào cho video {i+1}. Bỏ qua.", "WARNING")
                             self._report_failed_item(
                                 section="VEO_VIDEOS",
                                 index=i,
                                 name=filename,
                                 prompt=prompt,
                                 output_dir=video_dir,
                                 filepath=filepath,
                                 media_type="video",
                                 reason="Không tìm thấy ảnh đầu vào",
                                 reference_path=reference_path,
                             )
                             with v_lock:
                                 completed_v += 1
                                 self._update_progress("VEO_VIDEOS", completed_v, total_v)
                             return
                              
                         retries = 0
                         success = False
                         last_error = "Không rõ lỗi"
                          
                         while retries < MAIN_ATTEMPTS and not success and not self.is_stopped:
                             task_id = self.api.generate_video(
                                 prompt=prompt,
                                 model=settings.get('video_model', config.DEFAULT_VIDEO_MODEL),
                                 aspect_ratio=settings.get('aspect_ratio', config.DEFAULT_ASPECT_RATIO),
                                 mode=settings.get('video_mode', config.DEFAULT_VIDEO_MODE),
                                 reference_images_b64=[vid_ref_b64],
                                 resolution=[settings.get('resolution', config.DEFAULT_RESOLUTION)]
                             )
                             
                             if not task_id:
                                 last_error = "Không nhận được task_id"
                                 retries += 1; time.sleep(2)
                                 continue
                                 
                             result = self.api.wait_for_task(
                                 task_id, 
                                 poll_interval=max(1, settings.get('poll_interval', int(config.POLL_INTERVAL_SECONDS))),
                                 timeout=config.TASK_TIMEOUT_SECONDS
                             )
                             
                             if result.get("status") == "completed":
                                 file_results = result.get("results", [])
                                 if file_results and isinstance(file_results, list):
                                     file_ref = file_results[0]
                                 else:
                                     file_ref = file_results
                                 if not file_ref:
                                     last_error = "Không có file_url"
                                     retries += 1
                                     continue
                                 file_url = self.api.get_file_url(file_ref)
                                 if not file_url:
                                     last_error = "Không có file_url"
                                     retries += 1
                                     continue
                                     
                                 if file_url and download_file(file_url, filepath):
                                     self._log(f"[VEO_VIDEOS] ✅ Video {i+1}/{total_v} tải xong.", "SUCCESS")
                                     success = True
                                 else:
                                     last_error = "Download failed"
                                     retries += 1
                             else:
                                 last_error = self._task_error_reason(result)
                                 retries += 1
                                 time.sleep(2)

                         if not success and not self.is_stopped:
                             self._log(f"[VEO_VIDEOS] Lý do lỗi video {i+1}: {last_error}", "ERROR")
                             self._log(f"[VEO_VIDEOS] ❌ Bỏ qua video {i+1} sau {MAIN_ATTEMPTS} lần thử thất bại.", "ERROR")
                             self._report_failed_item(
                                 section="VEO_VIDEOS",
                                 index=i,
                                 name=filename,
                                 prompt=prompt,
                                 output_dir=video_dir,
                                 filepath=filepath,
                                 media_type="video",
                                 reason=last_error,
                                 reference_path=reference_path,
                             )
                                  
                         with v_lock:
                             completed_v += 1
                             self._update_progress("VEO_VIDEOS", completed_v, total_v)

                     max_v_workers = self._get_worker_count(settings, 'video_threads')
                     self._log(f"[VEO_VIDEOS] Đang chạy với {max_v_workers} luồng video song song...", "INFO")
                     with concurrent.futures.ThreadPoolExecutor(max_workers=max_v_workers) as executor:
                         v_futures = []
                         for i, prompt in enumerate(veo_prompts):
                             if self.is_stopped: break
                             v_futures.append(executor.submit(video_worker, i, prompt))
                             
                         for future in concurrent.futures.as_completed(v_futures):
                             if self.is_stopped: break
                             future.result()

             def run_step3():
                 if luot2_prompts and not self.is_stopped:
                     self._log(f"🚀 BƯỚC 3 (Lượt 2): Bắt đầu tạo {len(luot2_prompts)} ảnh.", "INFO")
                     self._generate_images_loop(
                         luot2_prompts, ref_img_b64, img_luot2_dir, f"luot2_{folder_name}", settings, "LUOT2_IMAGES", require_reference=True
                     )

             t2 = threading.Thread(target=run_step2)
             t3 = threading.Thread(target=run_step3)
             t2.start()
             t3.start()
             t2.join()
             t3.join()
                 
             if not self.is_stopped:
                 self._log("🎉 PIPELINE ĐÃ HOÀN TẤT THÀNH CÔNG!", "SUCCESS")
                 if 'completed' in self.callbacks: self.callbacks['completed']()
                 
        except Exception as e:
            self._log(f"🚨 LỖI HỆ THỐNG: {e}", "ERROR")
            if 'error' in self.callbacks: self.callbacks['error'](str(e))
