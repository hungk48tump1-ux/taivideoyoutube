import os
from typing import List

def parse_prompts(filepath: str) -> List[str]:
    """Reads a text file and returns a list of non-empty lines (prompts)."""
    if not filepath or not os.path.exists(filepath):
        return []
        
    prompts = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                clean_line = line.strip()
                if clean_line:
                    prompts.append(clean_line)
    except Exception as e:
        print(f"Error reading file {filepath}: {e}")
        
    return prompts
