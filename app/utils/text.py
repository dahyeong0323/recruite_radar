from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher


_COMPANY_LEGAL = re.compile(r"(?:\(주\)|㈜|주식회사)")
_PUNCTUATION = re.compile(r"[^0-9a-zA-Z가-힣]+")
_SPACE = re.compile(r"\s+")
_FINANCE_COMPANY_SUFFIX = re.compile(r"(?:인베스트먼트|파트너스|캐피탈|벤처스|벤처투자|증권|자산운용|투자자문|자산관리|금융|생명|은행)$", re.I)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE.sub(" ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def normalize_company(value: str | None, aliases: dict[str, list[str]] | None = None) -> str:
    value = clean_text(value)
    value = _COMPANY_LEGAL.sub("", value)
    normalized = _PUNCTUATION.sub("", value).casefold()
    if aliases:
        for canonical, variants in aliases.items():
            candidates = [canonical, *variants]
            if normalized in {_PUNCTUATION.sub("", clean_text(candidate)).casefold() for candidate in candidates}:
                return _PUNCTUATION.sub("", clean_text(canonical)).casefold()
    return normalized


def normalize_title(value: str | None) -> str:
    value = clean_text(value)
    value = re.sub(r"\[(?:채용|인턴|경력|신입)[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"(?:채용공고|모집공고|채용|모집|공고)", " ", value, flags=re.I)
    value = _PUNCTUATION.sub("", value).casefold()
    return value


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def short_hash(*parts: str, length: int = 6) -> str:
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def slug(value: str, max_length: int = 80) -> str:
    cleaned = clean_text(value)
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._ -]+", "", cleaned).strip(" .")
    return (cleaned or "job")[:max_length]


def infer_company_from_title(title: str | None) -> str | None:
    """Extract a company only when the title has a strong company-shaped prefix."""
    title = clean_text(title)
    if not title:
        return None
    bracket = re.match(r"^\[([^\]]+)\]", title)
    if bracket:
        candidate = bracket.group(1).strip()
        if candidate.casefold() in {"채용", "모집", "공고", "인턴", "경력", "신입"}:
            return None
        return candidate or None
    prefix = re.split(r"\s+(?=(?:투자|경영|IB|PE|VC|CVC|심사|운용|리스크|재무|회계|전략|기획|인사|관리)\S*)", title, maxsplit=1)[0].strip()
    prefix = _COMPANY_LEGAL.sub("", prefix).strip()
    if _FINANCE_COMPANY_SUFFIX.search(prefix):
        return prefix
    return None
