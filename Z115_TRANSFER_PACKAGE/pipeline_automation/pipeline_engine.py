import os
import threading
import time
import concurrent.futures
from typing import Callable, List
from file_parser import parse_prompts
from utils import encode_image_to_base64_data_url, download_file, setup_output_directories
from api_client import APIClient
import config

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
             
    def stop(self):
        self.is_stopped = True
        
    def pause(self):
        self.is_paused = True
        
    def resume(self):
        self.is_paused = False
        
    def _wait_if_paused(self):
        while self.is_paused and not self.is_stopped:
            time.sleep(1)

    def run_pipeline(self, whisk_file, veo_file, luot2_file, reference_image, settings):
        """Chạy toàn bộ pipeline trên một luồng riêng để không block UI."""
        thread = threading.Thread(
            target=self._pipeline_logic, 
            args=(whisk_file, veo_file, luot2_file, reference_image, settings),
            daemon=True
        )
        thread.start()
        
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
         self._log(f"[{section_name}] Chế độ tạo ảnh: {ref_mode}.", "info")
         
         def worker(i, prompt):
             nonlocal completed_count
             filename = f"{i+1}_{prefix}.png"
             filepath = os.path.join(output_dir, filename)
             retries = 0
             success = False
             
             while retries < config.MAX_RETRIES and not success and not self.is_stopped:
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
                     retries += 1
                     time.sleep(2)
                     continue
                     
                 # Wait for task
                 result = self.api.wait_for_task(
                     task_id, 
                     poll_interval=max(1, settings.get('poll_interval', 1)),
                     timeout=config.TASK_TIMEOUT_SECONDS
                 )
                 
                 if result.get("status") == "completed":
                     file_results = result.get("results", [])
                     if file_results and isinstance(file_results, list):
                         file_url = self.api.get_file_url(file_results[0])
                     else:
                         file_url = self.api.get_file_url(file_results)
                         
                     if file_url and download_file(file_url, filepath):
                         self._log(f"[{section_name}] ✅ Ảnh {i+1}/{total} đã tải xong.", "success")
                         results[i] = filepath
                         success = True
                     else:
                         retries += 1
                 else:
                     retries += 1
                     time.sleep(2)
                     
             if not success and not self.is_stopped:
                 self._log(f"[{section_name}] ❌ Bỏ qua ảnh {i+1} sau {config.MAX_RETRIES} lần thử thất bại.", "error")
                 
             with lock:
                 completed_count += 1
                 self._update_progress(section_name, completed_count, total)

         max_workers = int(settings.get('threads', 1))
         self._log(f"[{section_name}] Đang chạy {total} ảnh với {max_workers} luồng song song...", "info")
         
         with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
             futures = []
             for i, prompt in enumerate(prompts):
                 if self.is_stopped: break
                 futures.append(executor.submit(worker, i, prompt))
             
             for future in concurrent.futures.as_completed(futures):
                 if self.is_stopped: break
             
         return results

    def _pipeline_logic(self, whisk_file, veo_file, luot2_file, reference_image, settings):
        self.is_stopped = False
        self.is_paused = False
        
        try:
             input_file_for_dir = whisk_file or veo_file or luot2_file
             if not input_file_for_dir:
                 raise Exception("Không tìm thấy đường dẫn thư mục đầu vào hợp lệ.")
                 
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
             self._log("Đang kiểm tra kết nối server...", "info")
             if not self.api.check_health():
                 self._log("Không thể kết nối đến Webhook API Server. Hãy kiểm tra URL và cấu hình.", "error")
                 if 'error' in self.callbacks: self.callbacks['error']("Connection failed")
                 return
                 
             # Đọc dữ liệu
             self._log("Đang đọc dữ liệu đầu vào...", "info")
             whisk_prompts = parse_prompts(whisk_file)
             veo_prompts = parse_prompts(veo_file)
             luot2_prompts = parse_prompts(luot2_file) if luot2_file else []
             
             ref_img_b64 = encode_image_to_base64_data_url(reference_image) if reference_image else None
             if whisk_prompts:
                 if not reference_image:
                     raise Exception("Bước 1 (WHISK) cần ảnh tham chiếu. Vui lòng chọn file ảnh tham chiếu.")
                 if not ref_img_b64:
                     raise Exception(f"Không đọc được ảnh tham chiếu cho WHISK: {reference_image}")
                 self._log("Đã nạp ảnh tham chiếu cho WHISK và sẽ gửi trong reference_images.", "success")

             if luot2_prompts:
                 if not reference_image:
                     raise Exception("Bước 3 (Lượt 2) cần ảnh tham chiếu. Vui lòng chọn file ảnh tham chiếu.")
                 if not ref_img_b64:
                     raise Exception(f"Không đọc được ảnh tham chiếu cho Lượt 2: {reference_image}")
                 self._log("Đã nạp ảnh tham chiếu cho Lượt 2 và sẽ gửi trong reference_images.", "success")

             # --- BƯỚC 1: TẠO ẢNH TỪ WHISK ---
             whisk_generated_images = []
             if whisk_prompts:
                self._log(f"🚀 BƯỚC 1: Bắt đầu tạo {len(whisk_prompts)} ảnh (WHISK). Thư mục: {folder_name}", "info")
                whisk_generated_images = self._generate_images_loop(
                    whisk_prompts, ref_img_b64, img_whisk_dir, folder_name, settings, "WHISK_IMAGES", require_reference=True
                )
             
             # --- BƯỚC 2 VÀ BƯỚC 3 CHẠY SONG SONG ---
             def run_step2():
                 if veo_prompts and not self.is_stopped:
                     self._log(f"🚀 BƯỚC 2: Bắt đầu tạo {len(veo_prompts)} video (VEO).", "info")
                     total_v = len(veo_prompts)
                     completed_v = 0
                     v_lock = threading.Lock()
                     
                     def video_worker(i, prompt):
                         nonlocal completed_v
                         self._wait_if_paused()
                         if self.is_stopped: return
                         
                         vid_ref_b64 = None
                         if i < len(whisk_generated_images) and whisk_generated_images[i]:
                             vid_ref_b64 = encode_image_to_base64_data_url(whisk_generated_images[i])
                         
                         if not vid_ref_b64:
                             self._log(f"[VEO_VIDEOS] ⚠️ Không tìm thấy ảnh đầu vào cho video {i+1}. Bỏ qua.", "warning")
                             with v_lock:
                                 completed_v += 1
                                 self._update_progress("VEO_VIDEOS", completed_v, total_v)
                             return
                             
                         retries = 0
                         success = False
                         filename = f"{i+1}_{folder_name}.mp4"
                         filepath = os.path.join(video_dir, filename)
                         
                         while retries < config.MAX_RETRIES and not success and not self.is_stopped:
                             task_id = self.api.generate_video(
                                 prompt=prompt,
                                 model=settings.get('video_model', config.DEFAULT_VIDEO_MODEL),
                                 aspect_ratio=settings.get('aspect_ratio', config.DEFAULT_ASPECT_RATIO),
                                 mode=settings.get('video_mode', config.DEFAULT_VIDEO_MODE),
                                 reference_images_b64=[vid_ref_b64],
                                 resolution=[settings.get('resolution', config.DEFAULT_RESOLUTION)]
                             )
                             
                             if not task_id:
                                 retries += 1; time.sleep(2)
                                 continue
                                 
                             result = self.api.wait_for_task(
                                 task_id, 
                                 poll_interval=max(1, settings.get('poll_interval', 1)),
                                 timeout=config.TASK_TIMEOUT_SECONDS
                             )
                             
                             if result.get("status") == "completed":
                                 file_results = result.get("results", [])
                                 if file_results and isinstance(file_results, list):
                                     file_url = self.api.get_file_url(file_results[0])
                                 else:
                                     file_url = self.api.get_file_url(file_results)
                                     
                                 if file_url and download_file(file_url, filepath):
                                     self._log(f"[VEO_VIDEOS] ✅ Video {i+1}/{total_v} tải xong.", "success")
                                     success = True
                                 else:
                                     retries += 1
                             else:
                                 retries += 1
                                 time.sleep(2)
                                 
                         with v_lock:
                             completed_v += 1
                             self._update_progress("VEO_VIDEOS", completed_v, total_v)

                     max_v_workers = int(settings.get('threads', 1))
                     self._log(f"[VEO_VIDEOS] Đang chạy với {max_v_workers} luồng video song song...", "info")
                     with concurrent.futures.ThreadPoolExecutor(max_workers=max_v_workers) as executor:
                         v_futures = []
                         for i, prompt in enumerate(veo_prompts):
                             if self.is_stopped: break
                             v_futures.append(executor.submit(video_worker, i, prompt))
                             
                         for future in concurrent.futures.as_completed(v_futures):
                             if self.is_stopped: break

             def run_step3():
                if luot2_prompts and not self.is_stopped:
                    self._log(f"🚀 BƯỚC 3: Bắt đầu tạo {len(luot2_prompts)} ảnh (Lượt 2 Trở Đi).", "info")
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
                 self._log("🎉 PIPELINE ĐÃ HOÀN TẤT THÀNH CÔNG!", "success")
                 if 'completed' in self.callbacks: self.callbacks['completed']()
                 
        except Exception as e:
            self._log(f"🚨 LỖI HỆ THỐNG: {e}", "error")
            if 'error' in self.callbacks: self.callbacks['error'](str(e))
