import base64
import os
import requests
from typing import Optional
from core.pipeline.config import APP_NAME

def get_base64_mime_type(file_extension: str) -> str:
    """Returns the mime_type for base64 data URL based on file extension."""
    ext = file_extension.lower()
    if ext in ['.jpg', '.jpeg']:
        return "image/jpeg"
    elif ext == '.png':
        return "image/png"
    elif ext == '.webp':
        return "image/webp"
    else:
        return "image/jpeg"  # Default

def encode_image_to_base64_data_url(filepath: str) -> Optional[str]:
    """Reads an image file and converts it to a base64 data URL string."""
    try:
        with open(filepath, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            
        _, ext = os.path.splitext(filepath)
        mime_type = get_base64_mime_type(ext)
        return f"data:{mime_type};base64,{encoded_string}"
    except Exception as e:
        print(f"Error encoding image to base64: {e}")
        return None

def download_file(url: str, output_filepath: str) -> bool:
    """Downloads a file from a URL and saves it to output_filepath."""
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        with open(output_filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192): 
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Error downloading file from {url}: {e}")
        return False

def setup_output_directories(base_dir: str = "output"):
    """Creates the necessary output directories if they don't exist."""
    print("Setting up output directories...")
    images_dir = os.path.join(base_dir, "images")
    videos_dir = os.path.join(base_dir, "videos")
    sessions_dir = os.path.join(base_dir, "sessions")
    
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(videos_dir, exist_ok=True)
    os.makedirs(sessions_dir, exist_ok=True)
    
    return base_dir, images_dir, videos_dir, sessions_dir

def list_files_in_dir(directory: str, extension: str = None) -> list:
    """Helper to list files in a directory, optionally filtered by extension."""
    if not os.path.exists(directory):
        return []
        
    files = os.listdir(directory)
    if extension:
        files = [f for f in files if f.lower().endswith(extension.lower())]
    return sorted(files)
