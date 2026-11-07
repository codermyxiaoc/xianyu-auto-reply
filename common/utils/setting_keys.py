"""设置键比较，兼容 MySQL 常用的不区分大小写、重音和尾空格排序规则。"""
import unicodedata


def normalize_setting_key(key: str) -> str:
    normalized = "".join(character for character in unicodedata.normalize("NFKD", key)
                         if unicodedata.category(character) not in {"Mn", "Cf"})
    return normalized.casefold().rstrip()
