import time
import requests
from typing import Callable, Optional, Dict, Any, List, Union

class APIClient:
    def __init__(self, host: str, port: int, api_key: str, log_cb: Optional[Callable[[str, str], None]] = None):
        self.host = host
        self.port = port
        self.api_key = api_key
        self.log_cb = log_cb
        # For simplicity, assuming http
        self.base_url = f"http://{self.host}:{self.port}/api"

    def _log(self, message: str, level: str = "info") -> None:
        if self.log_cb:
            self.log_cb(message, level)
        else:
            print(message)
        
    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key
        }

    def _format_image_references(
        self,
        model: str,
        reference_image_b64: Union[str, List[Union[str, Dict[str, str]]]],
        category: str,
    ) -> List[Union[str, Dict[str, str]]]:
        refs = reference_image_b64 if isinstance(reference_image_b64, list) else [reference_image_b64]
        if model == "imagen4":
            return [
                ref if isinstance(ref, dict) else {"data": ref, "category": category}
                for ref in refs
            ]
        return refs
        
    def check_health(self) -> bool:
        """Kiểm tra server có đang hoạt động không."""
        try:
            url = f"{self.base_url}/health"
            # Health endpoint thường không cần auth, nhưng vẫn cung cấp header cho chắc 
            # hoặc thử không dùng nếu yêu cầu
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                return True
            return False
        except Exception as e:
            print(f"Health check failed: {e}")
            return False
            
    def generate_image(
        self,
        prompt: str,
        model: str,
        reference_image_b64: Union[str, List[str], None],
        category: str = "subject",
        upscale: Optional[List[str]] = None,
        aspect_ratio: str = "16:9",
    ) -> Optional[str]:
        """Gửi yêu cầu tạo ảnh. Trả về task_id nếu thành công, None nếu thất bại."""
        url = f"{self.base_url}/image/generate"
        payload = {
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,
        }
        if reference_image_b64:
            payload["reference_images"] = self._format_image_references(
                model,
                reference_image_b64,
                category,
            )
        if upscale:
            payload["upscale"] = upscale
             
        try:
            refs = payload.get("reference_images", [])
            first_ref = refs[0] if refs else None
            ref_value = first_ref.get("data", "") if isinstance(first_ref, dict) else first_ref
            ref_prefix = ref_value[:32] if ref_value else "NONE"
            self._log(
                f"POST /api/image/generate | model={model} | aspect={aspect_ratio} | "
                f"reference_images={len(refs)} | ref_prefix={ref_prefix}",
                "debug",
            )
            res = requests.post(url, json=payload, headers=self._get_headers(), timeout=30)
            
            if res.status_code == 202:
                data = res.json()
                task_id = data.get("task_id")
                self._log(f"Image task accepted: {task_id} | reference_images={len(refs)}", "debug")
                return task_id
            else:
                self._log(f"API Error ({res.status_code}): {res.text}", "error")
                return None
        except Exception as e:
            self._log(f"Connection error: {e}", "error")
            return None
            
    def generate_video(self, prompt: str, model: str, aspect_ratio: str, mode: str, reference_images_b64: List[str], resolution: List[str]) -> Optional[str]:
        """Gửi yêu cầu tạo video. Trả về task_id nếu thành công, None nếu thất bại."""
        url = f"{self.base_url}/video/generate"
        payload = {
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,
            "mode": mode,
            "reference_images": reference_images_b64,
        }
        if resolution:
            payload["resolution"] = resolution
            
        try:
            res = requests.post(url, json=payload, headers=self._get_headers())
            
            if res.status_code == 202:
                data = res.json()
                return data.get("task_id")
            else:
                 print(f"API Error ({res.status_code}): {res.text}")
                 return None
        except Exception as e:
            print(f"Connection error: {e}")
            return None
            
    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """Lấy trạng thái của một task."""
        url = f"{self.base_url}/status/{task_id}"
        try:
            res = requests.get(url, headers=self._get_headers())
            if res.status_code == 200:
                return res.json()
            else:
                return {"status": "error", "error": f"HTTP {res.status_code}: {res.text}"}
        except Exception as e:
             return {"status": "error", "error": str(e)}
             
    def wait_for_task(self, task_id: str, poll_interval: int = 5, timeout: int = 600, callback=None) -> Dict[str, Any]:
        """Đợi task hoàn thành và trả về kết quả cuối cùng. Cập nhật qua callback nếu có"""
        start_time = time.time()
        while True:
            status_data = self.get_task_status(task_id)
            status = status_data.get("status", "error")
            
            if callback:
                callback(status_data)
                
            if status == "completed" or status == "failed":
                return status_data
            elif status == "error":
                 print(f"Error checking status: {status_data.get('error')}")
                 return status_data
                 
            if (time.time() - start_time) > timeout:
                return {"status": "failed", "error": "Timeout exceeded", "task_id": task_id}
                
            time.sleep(poll_interval)
            
    def get_file_url(self, file_url_from_result: str) -> str:
        """Nếu API trả về file URL dạng tương đối hoặc cần ghép thêm, xử lý ở đây."""
        if file_url_from_result.startswith("http"):
            return file_url_from_result
        return f"{self.base_url.replace('/api', '')}{file_url_from_result}"

    def get_all_tasks(self) -> Optional[List[Dict]]:
        """Lấy danh sách các task trên server"""
        url = f"{self.base_url}/tasks"
        try:
            res = requests.get(url, headers=self._get_headers())
            if res.status_code == 200:
                return res.json()
            return None
        except Exception as e:
            print(f"Error getting tasks: {e}")
            return None
