
"""
Generic, domain-agnostic statistical primitives.
IMPORTANT: These are the ONLY functions that ever touch raw data directly.
Everything they return to the caller is either aggregated or masked -
this is the privacy boundary between "local execution" and "LLM-visible data".
"""

from typing import Optional, List, Dict, Any
import pandas as pd

# This is only still in use
def mask_value(value: Any, keep_last: int = 2) -> str:
    s = str(value)
    if len(s) <= keep_last:
        return "*" * len(s)
    return "*" * (len(s) - keep_last) + s[-keep_last:]
