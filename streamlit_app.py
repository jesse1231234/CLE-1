import io
import json
import math
import os
import re
from typing import Dict, List, Tuple, Optional

import requests
import streamlit as st
from bs4 import BeautifulSoup
import pandas as pd

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from pptx import Presentation
except Exception:
    Presentation = None

try:
    from openai import AzureOpenAI
except Exception:
    AzureOpenAI = None


# -------------------------------------------------------------------
# Config & constants
# -------------------------------------------------------------------

def get_secret(name: str, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)


CANVAS_BASE = get_secret("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = get_secret("CANVAS_API_TOKEN", "")

AZ_ENDPOINT = get_secret("AZURE_OPENAI_ENDPOINT", "")
AZ_API_KEY = get_secret("AZURE_OPENAI_API_KEY", "")
AZ_MODEL = get_secret("AZURE_OPENAI_MODEL", "")
AZ_API_VERSION = get_secret("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB
AZ_MAX_CHARS = 15000

# Sanity caps to prevent runaway counts from garbage extraction
MAX_REASONABLE_WORDS = 200_000          # beyond this, treat extraction as junk
MAX_REASONABLE_MINUTES_PER_FILE = 1_000 # beyond this, treat extraction as junk


# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------

def canvas_headers():
    if not CANVAS_TOKEN:
        raise RuntimeError("Missing CANVAS_API_TOKEN in secrets/env.")
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}


def canvas_get(url: str, params=None) -> List[dict]:
    """Handle Canvas pagination."""
    out = []
    while url:
        r = requests.get(url, headers=canvas_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
        link = r.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    next_url = m.group(1)
        url = next_url
        params = None
    return out


@st.cache_data(show_spinner=False)
def fetch_url_bytes(url: str, max_bytes: int) -> Tuple[bytes, str]:
    """
    Download up to max_bytes from url and return (bytes, detected_content_type).
    We use detected content type from HTTP headers to protect against missing metadata.
    """
    r = requests.get(url, headers=canvas_headers(), stream=True, timeout=60, allow_redirects=True)
    r.raise_for_status()
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    data = r.content[:max_bytes]
    return data, ct


# -------------------------------------------------------------------
# Canvas API helpers
# -------------------------------------------------------------------

def get_modules_with_items(course_id: int) -> List[dict]:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/modules"
    mods = canvas_get(url, params={"include[]": "items", "per_page": 100})
    items = []
    for mod in mods:
        for it in mod.get("items", []):
            items.append(
                {
                    "module_name": mod.get("name", ""),
                    "position": mod.get("position", 0),
                    "item_type": it.get("type", ""),
                    "title": it.get("title", ""),
                    "html_url": it.get("html_url", ""),
                    "content_id": it.get("content_id"),
                    "page_url": it.get("page_url"),
                    "content_details": it.get("content_details", {}),
                    "item_key": f"{it.get('type','')}::{it.get('id')}",
                }
            )
    return items


def get_page_body(course_id: int, page_url: str) -> str:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/pages/{page_url}"
    r = requests.get(url, headers=canvas_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("body", "") or ""


def get_assignment(course_id: int, assignment_id: int) -> dict:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/assignments/{assignment_id}"
    r = requests.get(url, headers=canvas_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_discussion(course_id: int, topic_id: int) -> dict:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
    r = requests.get(url, headers=canvas_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_quiz(course_id: int, quiz_id: int) -> dict:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/quizzes/{quiz_id}"
    r = requests.get(url, headers=canvas_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_file_metadata(course_id: int, file_id: int) -> dict:
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/files/{file_id}"
    r = requests.get(url, headers=canvas_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


# -------------------------------------------------------------------
# Text / HTML parsing
# -------------------------------------------------------------------

def strip_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words_from_text(text: str) -> int:
    """
    IMPORTANT PATCH:
    - Ignore 1-character "words" to avoid PDFs that extract as spaced letters
      turning into millions of tokens.
    """
    if not text:
        return 0
    # count tokens length >= 2 (letters/numbers/underscore/apostrophe)
    return len(re.findall(r"\b[\w']{2,}\b", text))


def detect_videos_from_html(html: str) -> List[dict]:
    videos = []
    if not html:
        return videos
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["iframe", "video", "embed"]):
        src = tag.get("src") or tag.get("data-src") or ""
        if not src:
            continue
        title = tag.get("title") or tag.get("aria-label") or "Embedded Video"
        videos.append({"src": src, "title": title})

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(dom in href for dom in ["youtube.com", "youtu.be", "vimeo.com", "echo360", "panopto", "kaltura"]):
            title = a.get_text(strip=True) or "Linked Video"
            videos.append({"src": href, "title": title})

    return videos


def detect_canvas_file_ids_from_html(html: str) -> List[int]:
    """
    Extract Canvas file IDs from HTML links that contain /files/<id>
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/files/" not in href:
            continue
        m = re.search(r"/files/(\d+)", href)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


# -------------------------------------------------------------------
# File extraction (PATCHED)
# -------------------------------------------------------------------

def is_text_like_content_type(ct: str) -> bool:
    ct = (ct or "").lower()
    return ct.startswith("text/") or any(x in ct for x in ["json", "xml", "html"])


def extract_file_text(file_url: str, content_type_hint: str, max_bytes: int) -> Tuple[str, int, str]:
    """
    Download a Canvas file and extract (text, pages_or_slides, detected_ct).

    PATCHES:
    - Use HTTP Content-Type header if hint is missing/wrong
    - Never decode unknown binary as UTF-8 text
    - PPTX returns empty text + slide_count
    """
    if not file_url:
        return "", 0, ""

    data, detected_ct = fetch_url_bytes(file_url, max_bytes)
    ct = (content_type_hint or detected_ct or "").split(";")[0].strip().lower()

    pages = 0

    # PDF
    if "pdf" in ct and pdf_extract_text:
        try:
            text = pdf_extract_text(io.BytesIO(data))
            pages = text.count("\f") or 0
            return text, pages, ct
        except Exception:
            return "", 0, ct

    # DOCX
    if (("word" in ct) or ("docx" in ct)) and Document:
        try:
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text, 0, ct
        except Exception:
            return "", 0, ct

    # PPTX
    if (("powerpoint" in ct) or ("pptx" in ct)) and Presentation:
        try:
            prs = Presentation(io.BytesIO(data))
            slide_count = len(prs.slides)
            return "", slide_count, ct
        except Exception:
            return "", 0, ct

    # PATCH: Only decode as text if content-type is text-like
    if is_text_like_content_type(ct):
        try:
            text = data.decode("utf-8", errors="ignore")
            return text, 0, ct
        except Exception:
            return "", 0, ct

    # Unknown/binary => no text
    return "", 0, ct


def estimate_minutes_from_pages_or_unknown(pages: int, content_type: str, size_bytes: Optional[int]) -> float:
    """
    Heuristic fallback:
    - PPT/presentation: 2 min per slide
    - Other: 3.5 min per page if pages known
    - If no pages, use size-based minimums (conservative)
    """
    ct = (content_type or "").lower()
    if pages and ("presentation" in ct or "powerpoint" in ct or "pptx" in ct):
        return float(pages) * 2.0
    if pages:
        return float(pages) * 3.5

    # No pages: size-based fallback (very conservative)
    if size_bytes:
        mb = max(1.0, size_bytes / (1024 * 1024))
        # 5 min per MB as a crude proxy, capped to avoid insane values
        return min(120.0, 5.0 * mb)

    return 10.0


# -------------------------------------------------------------------
# Difficulty & LLM
# -------------------------------------------------------------------

def default_difficulty() -> Dict:
    return {"label": "average", "wpm_factor": 1.0, "notes": "default difficulty (no LLM)"}


def reading_minutes(words: int, base_wpm: int, difficulty: Dict) -> float:
    factor = float(difficulty.get("wpm_factor", 1.0) or 1.0)
    wpm = max(80.0, base_wpm * factor)
    return words / wpm


def _coerce_json(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(r"{.*}", raw, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def azure_llm_client(endpoint: str, api_key: str, api_version: str):
    if AzureOpenAI is None:
        raise RuntimeError("openai SDK not installed. pip install openai>=1.52.0")
    return AzureOpenAI(api_key=api_key, azure_endpoint=endpoint.rstrip("/"), api_version=api_version)


def azure_llm_difficulty(text: str, endpoint: str, model: str, api_key: str, max_chars: int, api_version: str) -> Dict:
    client = azure_llm_client(endpoint, api_key, api_version)
    sys_msg = (
        "You are a reading difficulty estimator. Return ONLY JSON with keys:\n"
        "label one of ['very_easy','easy','average','hard','very_hard'], "
        "wpm_factor float, notes string. "
        "Very easy => 1.3, easy => 1.15, average => 1.0, hard => 0.8, very_hard => 0.65."
    )
    user_msg = f"Estimate reading difficulty:\n\n{text[:max_chars]}"

    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(cc.choices[0].message.content)
        return {
            "label": data.get("label", "average"),
            "wpm_factor": float(data.get("wpm_factor", 1.0)),
            "notes": data.get("notes", ""),
        }
    except Exception:
        pass

    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            temperature=0,
        )
        data = _coerce_json(cc.choices[0].message.content) or {}
        return {
            "label": data.get("label", "average"),
            "wpm_factor": float(data.get("wpm_factor", 1.0)),
            "notes": data.get("notes", "parsed without response_format"),
        }
    except Exception as e:
        return {"label": "average", "wpm_factor": 1.0, "notes": f"default (LLM error: {e})"}


def azure_llm_task_time(
    text: str,
    item_type: str,
    level: str,
    endpoint: str,
    model: str,
    api_key: str,
    max_chars: int,
    api_version: str,
) -> Dict:
    client = azure_llm_client(endpoint, api_key, api_version)
    sys_msg = (
        "You are a workload estimator. Return ONLY JSON with keys:\n"
        "do_minutes (float, excluding reading time), rationale (string)."
    )
    user_msg = (
        f"Item type: {item_type}\nStudent level: {level}\n\n"
        "Estimate completion time excluding reading time:\n\n"
        f"{text[:max_chars]}"
    )

    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(cc.choices[0].message.content)
        return {"do_minutes": float(data.get("do_minutes", 0.0)), "rationale": data.get("rationale", "")}
    except Exception:
        pass

    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            temperature=0,
        )
        data = _coerce_json(cc.choices[0].message.content) or {}
        return {"do_minutes": float(data.get("do_minutes", 0.0)), "rationale": data.get("rationale", "")}
    except Exception as e:
        return {"do_minutes": 0.0, "rationale": f"default 0 (LLM unavailable: {e})"}


def heuristic_task_time(words: int, item_type: str, level: str) -> float:
    lvl_factor = 1.0 if level.lower().startswith("under") else 1.25
    it = item_type.lower()
    if it == "assignment":
        base = 30.0 if words < 150 else (60.0 if words < 600 else 120.0)
        return base * lvl_factor
    if it == "discussion":
        return 35.0 * lvl_factor
    return 0.0


def estimate_quiz_time(meta: dict) -> float:
    if not meta:
        return 10.0
    t = meta.get("time_limit")
    if t:
        return float(t)
    qcount = meta.get("question_count") or meta.get("questions") or 5
    try:
        qcount = int(qcount)
    except Exception:
        qcount = 5
    return max(5.0, qcount * 2.0)


# -------------------------------------------------------------------
# Video & KPI formatting
# -------------------------------------------------------------------

def hhmmss_to_seconds(hhmmss: str) -> int:
    parts = hhmmss.strip().split(":")
    if len(parts) != 3:
        return 0
    try:
        h, m, s = [int(x) for x in parts]
    except Exception:
        return 0
    return max(0, h * 3600 + m * 60 + s)


def minutes_to_hhmm(minutes: float) -> str:
    if minutes is None:
        return "00:00"
    try:
        total_minutes = int(round(minutes))
    except Exception:
        return "00:00"
    hours, mins = divmod(total_minutes, 60)
    return f"{hours:02d}:{mins:02d}"


# -------------------------------------------------------------------
# Streamlit app
# -------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Course Load Estimator", layout="wide")
    st.title("📚 Course Load Estimator")

    if "items" not in st.session_state:
        st.session_state["items"] = []
    if "results" not in st.session_state:
        st.session_state["results"] = []
    if "pending_videos" not in st.session_state:
        st.session_state["pending_videos"] = {}

    st.sidebar.header("Configuration")
    course_id = st.sidebar.text_input("Canvas Course ID", value="")
    level = st.sidebar.selectbox("Student Level", ["Undergraduate", "Graduate"])
    base_wpm = st.sidebar.slider("Base Reading Speed (words per minute)", 150, 350, 200, 10)
    use_llm = st.sidebar.checkbox("Use Azure OpenAI for difficulty & DO time", value=True)
    debug_breakdown = st.sidebar.checkbox("Debug read-time breakdown", value=False)

    # KPIs at top (HH:MM)
    if st.session_state.get("results"):
        df_all = pd.DataFrame(st.session_state["results"])
        total_read = df_all.get("read_min", pd.Series(dtype=float)).sum()
        total_watch = df_all.get("watch_min", pd.Series(dtype=float)).sum()
        total_do = df_all.get("do_min", pd.Series(dtype=float)).sum()
        total_total = df_all.get("total_min", pd.Series(dtype=float)).sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Read (hh:mm)", minutes_to_hhmm(total_read))
        c2.metric("Total Watch (hh:mm)", minutes_to_hhmm(total_watch))
        c3.metric("Total Do (hh:mm)", minutes_to_hhmm(total_do))
        c4.metric("Total Workload (hh:mm)", minutes_to_hhmm(total_total))

    st.sidebar.markdown("### Azure OpenAI status")
    if not (AZ_ENDPOINT and AZ_API_KEY and AZ_MODEL):
        st.sidebar.warning("Azure OpenAI secrets missing or incomplete.")
    else:
        st.sidebar.success("Azure OpenAI configured.")

    st.sidebar.markdown("### Canvas status")
    if not (CANVAS_BASE and CANVAS_TOKEN):
        st.sidebar.error("Canvas secrets missing or incomplete.")
    else:
        st.sidebar.success("Canvas configured.")

    st.markdown(
        """
This tool estimates workload per module:

- **READ** – Canvas pages + linked Canvas documents  
- **WATCH** – embedded/linked videos (manual durations)  
- **DO** – assignments/discussions/quizzes
"""
    )

    # 1) Scan
    st.header("1) Scan Course")
    if st.button("Scan course modules & items", type="primary"):
        if not course_id:
            st.error("Enter a Canvas Course ID.")
        elif not (CANVAS_BASE and CANVAS_TOKEN):
            st.error("Canvas configuration not set.")
        else:
            try:
                with st.spinner("Fetching modules and items from Canvas..."):
                    items = get_modules_with_items(int(course_id))
                st.session_state["items"] = items
                st.session_state["results"] = []
                st.session_state["pending_videos"] = {}
                st.success(f"Found {len(items)} module items.")
            except Exception as e:
                st.error(f"Canvas API error: {e}")

    if st.session_state["items"]:
        st.write(f"Total items discovered: **{len(st.session_state['items'])}**")
        with st.expander("Preview raw module items"):
            st.json(st.session_state["items"])

    # 2) Process
    st.header("2) Estimate READ and DO time")

    if st.button("Process items for workload"):
        items = st.session_state.get("items", [])
        if not items:
            st.warning("No items scanned yet. Run 'Scan Course' first.")
        else:
            if use_llm and not (AZ_ENDPOINT and AZ_API_KEY and AZ_MODEL):
                st.error("Azure OpenAI is not configured, or secrets missing.")
            else:
                results = []
                pending_videos = []
                debug_rows = []

                for it in items:
                    item_type = it["item_type"]
                    title = it["title"]
                    html_url = it["html_url"]
                    item_key = it.get("item_key")

                    read_min = 0.0
                    watch_min = 0.0
                    do_min = 0.0
                    difficulty = default_difficulty()

                    # Pages / Assignments / Discussions
                    if item_type in ("Page", "Assignment", "Discussion"):
                        try:
                            if item_type == "Page":
                                body = get_page_body(int(course_id), it.get("page_url"))
                            elif item_type == "Assignment":
                                a = get_assignment(int(course_id), it.get("content_id"))
                                body = a.get("description", "") or ""
                            else:
                                d = get_discussion(int(course_id), it.get("content_id"))
                                body = d.get("message", "") or ""
                        except Exception:
                            body = ""

                        # videos
                        vids = detect_videos_from_html(body)
                        for idx, v in enumerate(vids, start=1):
                            v_key = f"{item_key}::embed::{idx}"
                            st.session_state["pending_videos"][v_key] = {
                                "title": v.get("title", "Video"),
                                "src": v.get("src", ""),
                                "hhmmss": "00:00:00",
                                "seconds": 0,
                                "item_key": item_key,
                            }

                        # page text
                        page_text = strip_html_to_text(body)
                        page_words = words_from_text(page_text)

                        if page_words > 0:
                            if use_llm:
                                try:
                                    difficulty = azure_llm_difficulty(
                                        page_text, AZ_ENDPOINT, AZ_MODEL, AZ_API_KEY, AZ_MAX_CHARS, AZ_API_VERSION
                                    )
                                except Exception:
                                    difficulty = default_difficulty()
                            page_read = reading_minutes(page_words, base_wpm, difficulty)
                            read_min += page_read

                            if debug_breakdown:
                                debug_rows.append({
                                    "item": title,
                                    "component": "page_text",
                                    "name": "(page text)",
                                    "content_type": "text/html",
                                    "size_bytes": None,
                                    "words": page_words,
                                    "minutes": page_read,
                                    "note": ""
                                })

                        # linked Canvas docs
                        file_ids = detect_canvas_file_ids_from_html(body)
                        for fid in file_ids:
                            try:
                                meta = get_file_metadata(int(course_id), fid)
                            except Exception:
                                continue

                            file_url = meta.get("url") or meta.get("download_url")
                            size_bytes = meta.get("size")
                            ct_hint = (meta.get("content-type") or meta.get("content_type") or "").lower()

                            if not file_url:
                                continue

                            text, pages_or_slides, detected_ct = extract_file_text(file_url, ct_hint, MAX_FILE_BYTES)
                            ct = detected_ct or ct_hint

                            f_words = words_from_text(text)

                            # Sanity: treat absurd extraction as junk and fall back
                            minutes_added = 0.0
                            note = ""
                            if f_words > 0:
                                if f_words > MAX_REASONABLE_WORDS:
                                    note = f"sanity-fallback: words>{MAX_REASONABLE_WORDS}"
                                    minutes_added = estimate_minutes_from_pages_or_unknown(pages_or_slides, ct, size_bytes)
                                else:
                                    if use_llm:
                                        try:
                                            f_diff = azure_llm_difficulty(
                                                text, AZ_ENDPOINT, AZ_MODEL, AZ_API_KEY, AZ_MAX_CHARS, AZ_API_VERSION
                                            )
                                        except Exception:
                                            f_diff = default_difficulty()
                                    else:
                                        f_diff = default_difficulty()

                                    minutes_added = reading_minutes(f_words, base_wpm, f_diff)
                                    if minutes_added > MAX_REASONABLE_MINUTES_PER_FILE:
                                        note = f"sanity-fallback: minutes>{MAX_REASONABLE_MINUTES_PER_FILE}"
                                        minutes_added = estimate_minutes_from_pages_or_unknown(pages_or_slides, ct, size_bytes)
                            else:
                                minutes_added = estimate_minutes_from_pages_or_unknown(pages_or_slides, ct, size_bytes)
                                note = "no-text-fallback"

                            read_min += minutes_added

                            if debug_breakdown:
                                debug_rows.append({
                                    "item": title,
                                    "component": "linked_file",
                                    "name": meta.get("display_name") or meta.get("filename") or f"file:{fid}",
                                    "content_type": ct,
                                    "size_bytes": size_bytes,
                                    "words": f_words,
                                    "minutes": minutes_added,
                                    "note": note
                                })

                        # DO time
                        if item_type in ("Assignment", "Discussion"):
                            if page_words > 0:
                                if use_llm:
                                    task = azure_llm_task_time(
                                        page_text, item_type, level, AZ_ENDPOINT, AZ_MODEL, AZ_API_KEY, AZ_MAX_CHARS, AZ_API_VERSION
                                    )
                                    do_min = float(task.get("do_minutes", 0.0))
                                    difficulty["work_rationale"] = task.get("rationale", "")
                                else:
                                    do_min = heuristic_task_time(page_words, item_type, level)

                    # File module items
                    elif item_type == "File":
                        cd = it.get("content_details") or {}
                        file_url = cd.get("url")
                        ct_hint = (cd.get("content_type", "") or "").lower()
                        size_bytes = None  # content_details doesn't always include size

                        if file_url:
                            text, pages_or_slides, detected_ct = extract_file_text(file_url, ct_hint, MAX_FILE_BYTES)
                            ct = detected_ct or ct_hint
                            w = words_from_text(text)

                            if w > 0 and w <= MAX_REASONABLE_WORDS:
                                if use_llm:
                                    try:
                                        difficulty = azure_llm_difficulty(
                                            text, AZ_ENDPOINT, AZ_MODEL, AZ_API_KEY, AZ_MAX_CHARS, AZ_API_VERSION
                                        )
                                    except Exception:
                                        difficulty = default_difficulty()
                                minutes_added = reading_minutes(w, base_wpm, difficulty)
                                if minutes_added > MAX_REASONABLE_MINUTES_PER_FILE:
                                    minutes_added = estimate_minutes_from_pages_or_unknown(pages_or_slides, ct, size_bytes)
                                read_min = minutes_added
                            else:
                                read_min = estimate_minutes_from_pages_or_unknown(pages_or_slides, ct, size_bytes)

                    # Quiz
                    elif item_type == "Quiz":
                        q_meta = it.get("content_details") or {}
                        quiz_id = it.get("content_id")
                        do_min = estimate_quiz_time(q_meta)
                        if use_llm and quiz_id:
                            try:
                                quiz = get_quiz(int(course_id), quiz_id)
                                q_text = strip_html_to_text(quiz.get("description", "") or "")
                                meta_str = (
                                    f"\n\n[Metadata: question_count="
                                    f"{q_meta.get('question_count') or quiz.get('question_count')}, "
                                    f"time_limit={q_meta.get('time_limit') or quiz.get('time_limit')} minutes]"
                                )
                                task = azure_llm_task_time(
                                    q_text + meta_str, "Quiz", level, AZ_ENDPOINT, AZ_MODEL, AZ_API_KEY, AZ_MAX_CHARS, AZ_API_VERSION
                                )
                                do_min = float(task.get("do_minutes", do_min))
                            except Exception:
                                pass

                    # External link video items
                    else:
                        if any(dom in (html_url or "") for dom in ("youtube", "youtu.be", "vimeo", "echo360", "panopto", "kaltura")):
                            v_key = f"{item_key}::external"
                            st.session_state["pending_videos"][v_key] = {
                                "title": title or "External Video",
                                "src": html_url,
                                "hhmmss": "00:00:00",
                                "seconds": 0,
                                "item_key": item_key,
                            }

                    total = read_min + watch_min + do_min
                    results.append(
                        {
                            "module": it["module_name"],
                            "module_position": it.get("position", 0),
                            "title": title,
                            "type": item_type,
                            "url": html_url,
                            "item_key": item_key,
                            "read_min": round(read_min, 2),
                            "watch_min": round(watch_min, 2),
                            "do_min": round(do_min, 2),
                            "total_min": round(total, 2),
                            "difficulty": difficulty,
                        }
                    )

                st.session_state["results"] = results
                st.success(f"Processed {len(results)} items. Videos detected: {len(st.session_state['pending_videos'])}")

                if debug_breakdown and debug_rows:
                    with st.expander("Debug: read-time breakdown (page text + linked files)", expanded=False):
                        dbg = pd.DataFrame(debug_rows)
                        st.dataframe(dbg, use_container_width=True)

    # 3) Video durations (keep per-video entry as requested)
    st.header("3) Enter video durations (hh:mm:ss)")

    pending = st.session_state.get("pending_videos", {})
    if pending:
        for v_key, meta in list(pending.items()):
            with st.expander(f"{meta['title']} — {meta.get('src','')}"):
                hhmmss = st.text_input(
                    "Duration (hh:mm:ss)",
                    key=f"dur_{v_key}",
                    value=meta.get("hhmmss", "00:00:00"),
                )
                if st.button("💾 Save", key=f"save_{v_key}"):
                    sec = hhmmss_to_seconds(hhmmss)
                    if sec <= 0:
                        st.error("Invalid hh:mm:ss (must be > 00:00:00).")
                    else:
                        meta["hhmmss"] = hhmmss
                        meta["seconds"] = sec
                        st.success("Saved. Totals will update below when table is rendered.")

        # Recompute watch_min per item
        item_seconds = {}
        for meta in pending.values():
            ik = meta.get("item_key")
            if not ik:
                continue
            item_seconds[ik] = item_seconds.get(ik, 0) + meta.get("seconds", 0)

        for r in st.session_state.get("results", []):
            ik = r.get("item_key")
            sec_total = item_seconds.get(ik, 0)
            watch_min = sec_total / 60.0
            r["watch_min"] = round(watch_min, 2)
            r["total_min"] = round(r["read_min"] + r["watch_min"] + r["do_min"], 2)

    else:
        st.info("No videos detected yet. They’ll appear here after processing items.")

    # 4) Summary
    st.header("4) Workload summary")

    results = st.session_state.get("results", [])
    if not results:
        st.info("No workload results yet. Process items to see estimates.")
        return

    df = pd.DataFrame(results)

    # Ensure module_position exists
    if "module_position" not in df.columns:
        module_order = {}
        for it in st.session_state.get("items", []):
            mn = it.get("module_name", "")
            pos = it.get("position", 0)
            if mn not in module_order or pos < module_order[mn]:
                module_order[mn] = pos
        df["module_position"] = df["module"].map(lambda m: module_order.get(m, 0))

    mod_summary = (
        df.groupby(["module", "module_position"])[["read_min", "watch_min", "do_min", "total_min"]]
        .sum()
        .reset_index()
        .sort_values("module_position")
    )

    grand_totals = {
        "module": "Grand Total",
        "module_position": mod_summary["module_position"].max() + 1 if len(mod_summary) else 9999,
        "read_min": mod_summary["read_min"].sum(),
        "watch_min": mod_summary["watch_min"].sum(),
        "do_min": mod_summary["do_min"].sum(),
        "total_min": mod_summary["total_min"].sum(),
    }

    mod_summary_with_total = pd.concat([mod_summary, pd.DataFrame([grand_totals])], ignore_index=True)
    mod_summary_display = mod_summary_with_total.drop(columns=["module_position"])

    st.subheader("Per-module totals (minutes)")
    st.dataframe(mod_summary_display, use_container_width=True)

    st.subheader("Item-level details")
    show_cols = ["module", "type", "title", "read_min", "watch_min", "do_min", "total_min", "url"]
    st.dataframe(df[show_cols], use_container_width=True)

    csv = df[show_cols].to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download item-level CSV",
        data=csv,
        file_name="course_load_estimates.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
