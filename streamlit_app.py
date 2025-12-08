import io
import json
import math
import os
import re
from typing import Dict, List, Tuple

import requests
import streamlit as st
from bs4 import BeautifulSoup
import pandas as pd  # <-- moved to top so we can use it for KPIs and summary

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
# Config & helpers
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
AZ_MAX_CHARS = 15000  # truncate long docs for LLM


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

def minutes_to_hhmm(minutes: float) -> str:
    """Convert a float number of minutes to HH:MM."""
    if minutes is None or math.isnan(minutes):
        return "00:00"
    total_minutes = int(round(minutes))
    hours, mins = divmod(total_minutes, 60)
    return f"{hours:02d}:{mins:02d}"


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
                    "position": mod.get("position", 0),  # module position from Canvas
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


# -------------------------------------------------------------------
# Text / file handling
# -------------------------------------------------------------------


def strip_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words_from_text(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\b\w+\b", text))


def detect_videos_from_html(html: str) -> List[dict]:
    """Find embedded videos via iframe/video tags and known hosts."""
    videos = []
    if not html:
        return videos
    soup = BeautifulSoup(html, "html.parser")

    # iframe / video tags
    for tag in soup.find_all(["iframe", "video", "embed"]):
        src = tag.get("src") or tag.get("data-src") or ""
        if not src:
            continue
        title = tag.get("title") or tag.get("aria-label") or "Embedded Video"
        videos.append({"src": src, "title": title})

    # Hyperlinks to common video hosts
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(dom in href for dom in ["youtube.com", "youtu.be", "vimeo.com", "echo360", "panopto", "kaltura"]):
            title = a.get_text(strip=True) or "Linked Video"
            videos.append({"src": href, "title": title})

    return videos


def extract_file_text(file_url: str, content_type: str, max_bytes: int) -> Tuple[str, int]:
    """
    Download a Canvas file and extract text + page/slide count.
    Returns (text, pages).

    PPTX is treated differently: we return "", slide_count so that
    a slide-based heuristic is used instead of full-text reading.
    """
    if not file_url:
        return "", 0

    r = requests.get(file_url, headers=canvas_headers(), stream=True, timeout=60)
    r.raise_for_status()
    data = r.content[:max_bytes]

    pages = 0
    # PDF
    if "pdf" in (content_type or "").lower() and pdf_extract_text:
        try:
            text = pdf_extract_text(io.BytesIO(data))
            pages = text.count("\f") or 0
            return text, pages
        except Exception:
            pass

    # DOCX
    if ("word" in (content_type or "").lower() or "docx" in (content_type or "").lower()) and Document:
        try:
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text, 0
        except Exception:
            pass

    # PPTX
    if (("powerpoint" in (content_type or "").lower()) or ("pptx" in (content_type or "").lower())) and Presentation:
        try:
            prs = Presentation(io.BytesIO(data))
            slide_count = len(prs.slides)
            # INTENTIONALLY return no text so slide-based heuristic is used.
            return "", slide_count
        except Exception:
            pass

    # Fallback: treat as plain text
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        text = ""
    return text, 0


# -------------------------------------------------------------------
# Difficulty & reading time
# -------------------------------------------------------------------


def default_difficulty() -> Dict:
    return {
        "label": "average",
        "wpm_factor": 1.0,
        "notes": "default difficulty (no LLM)",
    }


def reading_minutes(words: int, base_wpm: int, difficulty: Dict) -> float:
    factor = float(difficulty.get("wpm_factor", 1.0) or 1.0)
    wpm = max(80.0, base_wpm * factor)
    return words / wpm


def _coerce_json(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    # Try to extract JSON object
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


def azure_llm_difficulty(
    text: str,
    endpoint: str,
    model: str,
    api_key: str,
    max_chars: int,
    api_version: str,
) -> Dict:
    """
    Ask Azure OpenAI to estimate reading difficulty and wpm factor.
    Returns dict with keys: label, wpm_factor, notes.
    """
    client = azure_llm_client(endpoint, api_key, api_version)

    sys_msg = (
        "You are a reading difficulty estimator. "
        "Return ONLY a JSON object with keys:\n"
        "  label: one of ['very_easy','easy','average','hard','very_hard']\n"
        "  wpm_factor: a float multiplier relative to base reading speed\n"
        "  notes: short explanation.\n"
        "Very easy => 1.3, easy => 1.15, average => 1.0, hard => 0.8, very_hard => 0.65."
    )

    user_msg = (
        "Estimate reading difficulty for the following course material "
        "for a typical college student.\n\n"
        f"TEXT:\n{text[:max_chars]}"
    )

    # First attempt: JSON mode
    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = cc.choices[0].message.content
        data = json.loads(raw)
        return {
            "label": data.get("label", "average"),
            "wpm_factor": float(data.get("wpm_factor", 1.0)),
            "notes": data.get("notes", ""),
        }
    except Exception:
        pass

    # Fallback: best-effort JSON
    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
        )
        raw = cc.choices[0].message.content
        data = _coerce_json(raw) or {}
        return {
            "label": data.get("label", "average"),
            "wpm_factor": float(data.get("wpm_factor", 1.0)),
            "notes": data.get("notes", "parsed without response_format"),
        }
    except Exception as e:
        return {
            "label": "average",
            "wpm_factor": 1.0,
            "notes": f"default difficulty (LLM error: {e})",
        }


# -------------------------------------------------------------------
# DO-time estimation
# -------------------------------------------------------------------


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
    """
    Ask Azure OpenAI to estimate *DO* time for an item.

    Returns dict:
      {
        "do_minutes": float,
        "rationale": str
      }
    """
    client = azure_llm_client(endpoint, api_key, api_version)

    sys_msg = (
        "You are a workload estimator for university courses. "
        "You MUST respond with ONLY a JSON object that has keys:\n"
        "  do_minutes (float, minutes required to complete the task, "
        "              not including reading time),\n"
        "  rationale (string, short explanation).\n"
        "Assume typical students working at a realistic, not ideal, pace."
    )

    user_msg = (
        f"Item type: {item_type}\n"
        f"Student level: {level}\n\n"
        "Below are the full student-facing instructions and/or description. "
        "Estimate how long it will take the AVERAGE student to complete this item "
        "(writing, problem solving, posting, etc.), EXCLUDING reading time.\n\n"
        f"TEXT:\n{text[:max_chars]}"
    )

    # Try JSON mode
    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = cc.choices[0].message.content
        data = json.loads(raw)
        return {
            "do_minutes": float(data.get("do_minutes", 0.0)),
            "rationale": data.get("rationale", "no rationale provided"),
        }
    except Exception:
        pass

    # Fallback
    try:
        cc = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
        )
        raw = cc.choices[0].message.content
        data = _coerce_json(raw) or {}
        return {
            "do_minutes": float(data.get("do_minutes", 0.0)),
            "rationale": data.get("rationale", "fallback parse"),
        }
    except Exception as e:
        return {
            "do_minutes": 0.0,
            "rationale": f"default 0 (LLM unavailable: {e})",
        }


def heuristic_task_time(words: int, item_type: str, level: str) -> float:
    """
    Rough fallback if LLM is off.

    - Assignments: scale with instructions length.
    - Discussions: base for original post + replies.
    """
    lvl_factor = 1.0 if level.lower().startswith("under") else 1.25

    it = item_type.lower()
    if it == "assignment":
        if words < 150:
            base = 30.0
        elif words < 600:
            base = 60.0
        else:
            base = 120.0
        return base * lvl_factor

    if it == "discussion":
        base = 35.0  # ~20 min post + ~15 replies
        return base * lvl_factor

    return 0.0


def estimate_quiz_time(meta: dict) -> float:
    """
    Basic heuristic for quiz DO time:
    - If time_limit set: use that.
    - Else: 2 minutes per question (fallback 10).
    """
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
# Video helpers
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

    # ---------------- KPIs at top (from grand totals) ----------------
    if st.session_state.get("results"):
        df_all = pd.DataFrame(st.session_state["results"])
        total_read = df_all["read_min"].sum()
        total_watch = df_all["watch_min"].sum()
        total_do = df_all["do_min"].sum()
        total_total = df_all["total_min"].sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Read (hh:mm)", minutes_to_hhmm(total_read))
        c2.metric("Total Watch (hh:mm)", minutes_to_hhmm(total_watch))
        c3.metric("Total Do (hh:mm)", minutes_to_hhmm(total_do))
        c4.metric("Total Workload (hh:mm)", minutes_to_hhmm(total_total))


    st.sidebar.header("Configuration")

    course_id = st.sidebar.text_input("Canvas Course ID", value="")
    level = st.sidebar.selectbox("Student Level", ["Undergraduate", "Graduate"])
    base_wpm = st.sidebar.slider("Base Reading Speed (words per minute)", 150, 350, 200, 10)
    use_llm = st.sidebar.checkbox("Use Azure OpenAI for difficulty & DO time", value=True)

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
This tool estimates student workload per module by breaking it into:

- **READ** – reading Canvas pages, assignment prompts, discussions, and document files  
- **WATCH** – watching embedded / linked videos  
- **DO** – completing assignments, discussions, and quizzes
"""
    )

    # ------------------------------------------------------------------
    # 1) Scan Course
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 2) Process items (READ + DO, plus video detection)
    # ------------------------------------------------------------------
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
                pending_videos = {}
                for it in items:
                    item_type = it["item_type"]
                    title = it["title"]
                    html_url = it["html_url"]
                    item_key = it.get("item_key")

                    read_min = 0.0
                    watch_min = 0.0
                    do_min = 0.0
                    difficulty = default_difficulty()

                    # -------------------
                    # Pages / Assignments / Discussions
                    # -------------------
                    if item_type in ("Page", "Assignment", "Discussion"):
                        try:
                            if item_type == "Page":
                                body = get_page_body(int(course_id), it.get("page_url"))
                            elif item_type == "Assignment":
                                a = get_assignment(int(course_id), it.get("content_id"))
                                body = a.get("description", "") or ""
                            else:  # Discussion
                                d = get_discussion(int(course_id), it.get("content_id"))
                                body = d.get("message", "") or ""
                        except Exception:
                            body = ""

                        text = strip_html_to_text(body)

                        # Detect videos in the HTML
                        vids = detect_videos_from_html(body)
                        for idx, v in enumerate(vids, start=1):
                            v_key = f"{item_key}::embed::{idx}"
                            pending_videos[v_key] = {
                                "title": v.get("title", "Video"),
                                "src": v.get("src", ""),
                                "hhmmss": "00:00:00",
                                "seconds": 0,
                                "item_key": item_key,
                            }

                        words = words_from_text(text)

                        # Reading time via LLM difficulty
                        if words > 0:
                            if use_llm:
                                try:
                                    difficulty = azure_llm_difficulty(
                                        text,
                                        AZ_ENDPOINT,
                                        AZ_MODEL,
                                        AZ_API_KEY,
                                        AZ_MAX_CHARS,
                                        AZ_API_VERSION,
                                    )
                                except Exception as e:
                                    st.warning(f"LLM difficulty failed for '{title}': {e}")
                                    difficulty = default_difficulty()
                            read_min = reading_minutes(words, base_wpm, difficulty)

                        # DO time via LLM / heuristic (assignments & discussions only)
                        if item_type in ("Assignment", "Discussion"):
                            if words > 0:
                                if use_llm:
                                    try:
                                        task = azure_llm_task_time(
                                            text,
                                            item_type,
                                            level,
                                            AZ_ENDPOINT,
                                            AZ_MODEL,
                                            AZ_API_KEY,
                                            AZ_MAX_CHARS,
                                            AZ_API_VERSION,
                                        )
                                        do_min = float(task.get("do_minutes", 0.0))
                                        difficulty["work_rationale"] = task.get("rationale", "")
                                    except Exception as e:
                                        st.warning(f"LLM task-time failed for '{title}', using heuristic: {e}")
                                        do_min = heuristic_task_time(words, item_type, level)
                                else:
                                    do_min = heuristic_task_time(words, item_type, level)

                    # -------------------
                    # Files (PDF / DOCX / PPTX / etc.)
                    # -------------------
                    elif item_type == "File":
                        cd = it.get("content_details") or {}
                        file_url = cd.get("url")
                        content_type = cd.get("content_type", "")
                        if file_url:
                            text, pages = extract_file_text(file_url, content_type, MAX_FILE_BYTES)
                            words = words_from_text(text)
                            if words > 0:
                                if use_llm:
                                    try:
                                        difficulty = azure_llm_difficulty(
                                            text,
                                            AZ_ENDPOINT,
                                            AZ_MODEL,
                                            AZ_API_KEY,
                                            AZ_MAX_CHARS,
                                            AZ_API_VERSION,
                                        )
                                    except Exception as e:
                                        st.warning(f"LLM difficulty failed for file '{title}': {e}")
                                        difficulty = default_difficulty()
                                read_min = reading_minutes(words, base_wpm, difficulty)
                            else:
                                # page-based fallback, esp. PPTX or scans
                                mp = 2.0 if "presentation" in (content_type or "").lower() else 3.5
                                read_min = pages * mp

                    # -------------------
                    # Quizzes
                    # -------------------
                    elif item_type == "Quiz":
                        q_meta = it.get("content_details") or {}
                        quiz_id = it.get("content_id")
                        do_min = estimate_quiz_time(q_meta)
                        # If LLM is enabled, refine using quiz description
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
                                    q_text + meta_str,
                                    "Quiz",
                                    level,
                                    AZ_ENDPOINT,
                                    AZ_MODEL,
                                    AZ_API_KEY,
                                    AZ_MAX_CHARS,
                                    AZ_API_VERSION,
                                )
                                do_min = float(task.get("do_minutes", do_min))
                                difficulty["work_rationale"] = task.get("rationale", "")
                            except Exception as e:
                                st.warning(
                                    f"LLM task-time for quiz '{title}' failed "
                                    f"(using heuristic {do_min:.1f} min): {e}"
                                )

                    # -------------------
                    # External links (videos)
                    # -------------------
                    else:
                        if any(
                            dom in (html_url or "")
                            for dom in ("youtube", "youtu.be", "vimeo", "echo360", "panopto", "kaltura")
                        ):
                            v_key = f"{item_key}::external"
                            pending_videos[v_key] = {
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
                            "module_position": it.get("position", 0),  # <-- carry module order into results
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
                st.session_state["pending_videos"] = pending_videos
                st.success(f"Processed {len(results)} items. Videos detected: {len(pending_videos)}")

    # ------------------------------------------------------------------
    # 3) Manual video durations (UNCHANGED LOGIC)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 4) Summary tables (with module order + Grand Total)
    # ------------------------------------------------------------------
        # ------------------------------------------------------------------
    # 4) Workload summary (with module order + Grand Total)
    # ------------------------------------------------------------------
    st.header("4) Workload summary")

    results = st.session_state.get("results", [])
    if not results:
        st.info("No workload results yet. Process items to see estimates.")
        return

    df = pd.DataFrame(results)

    # --- Ensure module_position exists (for older results or partial runs) ---
    if "module_position" not in df.columns:
        # Build a mapping from module name -> position from the original items
        module_order = {}
        for it in st.session_state.get("items", []):
            mn = it.get("module_name", "")
            pos = it.get("position", 0)
            # keep the smallest position seen for that module
            if mn not in module_order or pos < module_order[mn]:
                module_order[mn] = pos

        df["module_position"] = df["module"].map(lambda m: module_order.get(m, 0))

    # Module-level aggregation with proper ordering
    mod_summary = (
        df.groupby(["module", "module_position"])[["read_min", "watch_min", "do_min", "total_min"]]
        .sum()
        .reset_index()
        .sort_values("module_position")
    )

    # Build Grand Total row
    grand_totals = {
        "module": "Grand Total",
        "module_position": mod_summary["module_position"].max() + 1 if len(mod_summary) else 9999,
        "read_min": mod_summary["read_min"].sum(),
        "watch_min": mod_summary["watch_min"].sum(),
        "do_min": mod_summary["do_min"].sum(),
        "total_min": mod_summary["total_min"].sum(),
    }

    mod_summary_with_total = pd.concat(
        [mod_summary, pd.DataFrame([grand_totals])],
        ignore_index=True,
    )

    # Drop module_position for display, but keep order
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
