from dataclasses import dataclass
import re


def normalize_cik(value):
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", text):
        raise ValueError("CIK must contain 1 to 10 ASCII digits")
    return text.zfill(10)


@dataclass
class Record:
    company: str
    value: int
