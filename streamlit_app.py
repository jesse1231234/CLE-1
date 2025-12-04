# streamlit_app.py
# Self-contained Streamlit app that:
# - Reads ALL config from Streamlit Cloud "Secrets" (no manual entry in UI)
# - Connects directly to Canvas (admin token) to scan a course
# - Extracts text from Pages/Assignments/Discussions and Files (PDF/DOCX/PPTX)
# - Detects embedded videos and lets you enter manual durations (hh:mm:ss)
# - Estimates reading time (UG/Grad base WPM) with optional Azure LLM difficulty
# - Exports CSV/JSON
#
# Required secrets (Streamlit Cloud -> Settings -> Secrets), TOML:
# [canvas]
# base_url = "https://YOURTENANT.instructure.com"
# token    = "YOUR_CANVAS_ADMIN_TOKEN"
#
# [azure]
# use_llm     = false            # set true to enable LLM difficulty
# endpoint    = "https://YOUR-RESOURCE.cognitiveservices.azure.com"
# api_key     = "YOUR_KEY"
# deployment  = "YOUR_DEPLOYMENT_NAME"
# api_version = "2024-10-01-preview"
# max_chars   = 15000
#
# [app]
# default_level = "Undergraduate"   # or "Graduate"
# max_file_mb   = 50

import io
import re
import os
import json
import requests
import streamlit as st
from collections.abc import Mapping
from bs4 import BeautifulSoup

# Optional parsers (graceful if not present)
try:
    from pdfminer.high_level import extract_text as pdf_extract_text  # pdfminer.six
except Exception:
    pdf_extract_text = None

try:
    import docx  # python-docx
except Exception:
    docx = None

try:
    from pptx import Presentation  # python-pptx
except Exception:
    Presentation = None


# --------------------------
# App setup
# --------------------------
st.set_page_config(page_title="Course Load Estimator (Secrets)", page_icon="📚", layout="wide")
st.title("📚 Course Load Estimator — Secrets-driven (no manual keys)")


# --------------------------
# Secrets helpers & load
# --------------------------
def get_secret(path: str, default=None):
    """Path like 'canvas.base_url'. Works with Streamlit's Mapping-like secrets."""
    cur: Mapping | dict = st.secrets
    for p in path.split("."):
        if isinstance(cur, Mapping) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


# Canvas (required)
canvas_base = (get_secret("canvas.base_url") or "").strip()
canvas_token = (get_secret("canvas.token") or "").strip()

# Azure (optional for LLM difficulty)
use_llm = bool(get_secret("azure.use_llm", False))
az_endpoint = (get_secret("azure.endpoint", "") or "").strip()
az_key = (get_secret("azure.api_key", "") or "").strip()
az_deployment = (get_secret("azure.deployment", "gpt-4o") or "").strip()
az_api_version = (get_secret("azure.api_version", "2024-12-01-preview") or "").strip()
az_max_chars = int(get_secret("azure.max_chars", 15000))

# App defaults
default_level = (get_secret("app.default_level", "Undergraduate") or "Undergraduate").strip()
max_file_mb = int(get_secret("app.max_file_mb", 50))
MAX_FILE_BYTES = max_file_mb * 1024 * 1024

# Validate required secrets
missing = []
if not canvas_base:
    missing.append("canvas.base_url")
if not canvas_token:
    missing.append("canvas.token")
if use_llm and (not az_endpoint or not az_key or not az_deployment):
    missing.append("azure.endpoint/api_key/deployment (required when azure.use_llm=true)")
if missing:
    st.error("Missing required secrets: " + ", ".join(missing))
    st.stop()


# --------------------------
# Sidebar (read-only config + test button)
# --------------------------
with st.sidebar:
    st.header("Configuration (from Secrets)")
    st.write(f"**Canvas Base URL**: {canvas_base}")
    st.write("**Canvas Token**: (loaded)")
    st.write(f"**Default Level**: {default_level}")
    st.write(f"**Max File Size**: {max_file_mb} MB")
    st.write(f"**LLM Difficulty**: {'Enabled' if use_llm else 'Disabled'}")
    if use_llm:
        st.write(f"**Azure Endpoint**: {az_endpoint}")
        st.write(f"**Deployment**: {az_deployment}")
        st.write(f"**API Version**: {az_api_version}")
        st.write("**Azure Key**: (loaded)")
        if st.button("🔧 Test Azure LLM"):
            try:
                d = None
                d = None
                d = None
                sample = "This is a short passage to estimate reading difficulty."
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
                d = None
            except Exception as e:
                st.error(str(e))


# --------------------------
# Canvas helpers
# --------------------------
def headers():
    return {"Authorization": f"Bearer {canvas_token}"} if canvas_token else {}


def paginate(url: str):
    """Yield JSON pages following Canvas RFC5988 Link headers."""
    s = requests.Session()
    s.headers.update(headers())
    while url:
        r = s.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        yield data
        nxt = None
        link = r.headers.get("link") or r.headers.get("Link")
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    nxt = part[part.find("<") + 1 : part.find(">")]
        url = nxt


def list_modules_with_items(course_id: int):
    url = f"{canvas_base.rstrip('/')}/api/v1/courses/{course_id}/modules?include[]=items&include[]=content_details&per_page=100"
    for page in paginate(url):
        for m in page:
            yield m


def get_page_body(course_id: int, url_or_id: str) -> str:
    url = f"{canvas_base.rstrip('/')}/api/v1/courses/{course_id}/pages/{url_or_id}"
    r = requests.get(url, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("body", "") or ""


def get_assignment(course_id: int, assignment_id: int) -> dict:
    url = f"{canvas_base.rstrip('/')}/api/v1/courses/{course_id}/assignments/{assignment_id}"
    r = requests.get(url, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_discussion(course_id: int, topic_id: int) -> dict:
    url = f"{canvas_base.rstrip('/')}/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
    r = requests.get(url, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def strip_html_to_text(html: str):
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text(separator=" ", strip=True)


VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "echo360",
    "kaltura",
    "panopto",
    "video.",
    "player.",
)


def detect_videos_from_html(html: str):
    out = []
    soup = BeautifulSoup(html or "", "lxml")
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or ""
        title = iframe.get("title") or ""
        if any(dom in src for dom in VIDEO_DOMAINS):
            out.append({"kind": "iframe", "src": src, "title": title or "Embedded Video"})
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        text = a.get_text(strip=True) or ""
        if any(dom in href for dom in VIDEO_DOMAINS):
            out.append({"kind": "link", "src": href, "title": text or "Video Link"})
    return out


def hhmmss_to_seconds(hhmmss: str) -> int:
    m = re.match(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$", (hhmmss or "").strip())
    if not m:
        return 0
    h, m_, s = map(int, m.groups())
    return h * 3600 + m_ * 60 + s


def extract_file_text(file_url: str, content_type: str, max_bytes: int) -> tuple[str, int]:
    """Download file and return (text, page_count_est). Pages only for PPTX when possible."""
    r = requests.get(file_url, headers=headers(), timeout=60, stream=True)
    r.raise_for_status()
    total, chunks = 0, []
    for chunk in r.iter_content(1024 * 64):
        total += len(chunk)
        if total > max_bytes:
            return ("", 0)  # size cap
        chunks.append(chunk)
    data = b"".join(chunks)
    # PDF
    if "pdf" in (content_type or "").lower() and pdf_extract_text:
        try:
            text = pdf_extract_text(io.BytesIO(data)) or ""
            return (text, 0)
        except Exception:
            pass
    # DOCX
    if (("word" in (content_type or "").lower()) or ("docx" in (content_type or "").lower())) and docx:
        try:
            d = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in d.paragraphs)
            return (text, 0)
        except Exception:
            pass
    # PPTX
    if (("powerpoint" in (content_type or "").lower()) or ("pptx" in (content_type or "").lower())) and Presentation:
        try:
            prs = Presentation(io.BytesIO(data))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        parts.append(shape.text)
            return ("\n".join(parts), len(prs.slides))
        except Exception:
            pass
    # Fallback to best-effort text
    try:
        return (data.decode("utf-8", errors="ignore"), 0)
    except Exception:
        return ("", 0)


# --------------------------
# Estimation helpers
# --------------------------
def default_difficulty():
    return {
        "difficulty_label": "intermediate",
        "difficulty_score": 0.5,
        "wpm_adjustment": 1.0,
        "rationale": "default heuristics",
    }


def azure_llm_difficulty(
    text: str,
    endpoint: str,
    deployment: str,
    api_key: str,
    max_chars: int,
    api_version: str,
) -> dict:
    """
    Supports both:
    - Cognitive Services domain: /openai/deployments/{deployment}/responses?api-version=...
    - Azure OpenAI v1:           /openai/v1/responses  (no api-version; model in body)
    """
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        # Some CogSvc front doors also accept Ocp-Apim-Subscription-Key; harmless to include:
        "Ocp-Apim-Subscription-Key": api_key,
    }

    sys_msg = (
        "Return only JSON with keys: difficulty_label, difficulty_score (0..1), "
        "wpm_adjustment (0.6..1.2), rationale (<= 3 sentences)."
    )
    user_msg = f"Assess the reading difficulty and return JSON only.\nTEXT:\n{text[:max_chars]}"

    is_cogsvc = "cognitiveservices.azure.com" in (endpoint or "")

    if is_cogsvc:
        # Cognitive Services: deployment in path + api-version
        url = endpoint.rstrip("/") + f"/openai/deployments/{deployment}/responses"
        params = {"api-version": api_version}
        body = {
            "input": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
        }
        r = requests.post(url, headers=headers, params=params, json=body, timeout=60)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
        j = r.json()
        try:
            raw = j["output"]["content"][0]["text"]
            return json.loads(raw)
        except Exception:
            return default_difficulty()
    else:
        # Azure OpenAI v1
        url = endpoint.rstrip("/") + "/openai/v1/responses"
        body = {
            "model": deployment,  # model in body for v1
            "input": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
        }
        r = requests.post(url, headers=headers, json=body, timeout=60)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
        j = r.json()
        try:
            raw = j["output"]["content"][0]["text"]
            return json.loads(raw)
        except Exception:
            return default_difficulty()


def words_from_text(text: str) -> int:
    toks = re.findall(r"\b\w+\b", text or "", flags=re.UNICODE)
    return len(toks)


def reading_minutes(words: int, base_wpm: int, diff: dict) -> float:
    wpm_adj = max(0.1, float(diff.get("wpm_adjustment", 1.0)))
    adjusted = max(50, base_wpm * wpm_adj)
    score = float(diff.get("difficulty_score", 0.5))
    reread = 1.0 + (0.15 * score)  # up to +15% at score=1.0
    return (words / adjusted) * reread


def estimate_quiz_time(quiz: dict) -> float:
    # minutes
    tl = quiz.get("time_limit") if quiz else None
    if tl:
        return float(tl)
    qc = (quiz or {}).get("question_count") or 0
    return qc * 2.0 if qc else 10.0


# --------------------------
# UI: Level & Course controls
# --------------------------
level = st.selectbox(
    "Learner Level",
    ["Undergraduate", "Graduate"],
    index=(0 if default_level.lower().startswith("under") else 1),
)
base_wpm = 225 if level == "Undergraduate" else 250

st.header("1) Scan a Course")
course_id = st.number_input("Canvas Course ID", min_value=1, step=1, value=12345)

if st.button("🚀 Scan (Modules & Items)"):
    progress = st.progress(0.0, text="Listing modules…")
    items = []
    try:
        modules = list(list_modules_with_items(int(course_id)))
    except Exception as e:
        st.exception(e)
        st.stop()
    total_items = sum(len(m.get("items", [])) for m in modules)
    done = 0
    for mod in modules:
        for it in (mod.get("items") or []):
            done += 1
            progress.progress(min(0.99, done / max(1, total_items)), text=f"Fetching item {done}/{total_items}")
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
                }
            )
    st.success(f"Collected {len(items)} items from {len(modules)} modules.")
    st.session_state["items"] = items


# --------------------------
# Process items
# --------------------------
st.header("2) Extract & Estimate")
left, right = st.columns([3, 2])

with left:
    if st.button("🔎 Process Items"):
        if "items" not in st.session_state:
            st.warning("Scan the course first.")
            st.stop()

        results = []
        pending_videos = {}

        for it in st.session_state["items"]:
            item_type, title, html_url = it["item_type"], it["title"], it["html_url"]
            read_min = watch_min = do_min = 0.0
            difficulty = default_difficulty()

            if item_type in ("Page", "Assignment", "Discussion"):
                # Fetch and parse HTML
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

                text = strip_html_to_text(body)
                # Detect videos
                vids = detect_videos_from_html(body)
                for idx, v in enumerate(vids, start=1):
                    key = f"{html_url}::{v.get('src','')}::{idx}"
                    pending_videos[key] = {
                        "title": v.get("title", "Video"),
                        "src": v.get("src", ""),
                        "hhmmss": "00:00:00",
                    }

                words = words_from_text(text)
                if words > 0:
                    if use_llm:
                        try:
                            difficulty = azure_llm_difficulty(
                                text, az_endpoint, az_deployment, az_key, az_max_chars, az_api_version
                            )
                        except Exception as e:
                            st.warning(f"LLM difficulty failed for '{title}': {e}")
                            difficulty = default_difficulty()
                    read_min = reading_minutes(words, base_wpm, difficulty)

            elif item_type == "File":
                cd = it.get("content_details") or {}
                file_url, content_type = cd.get("url"), cd.get("content_type", "")
                if file_url:
                    text, pages = extract_file_text(file_url, content_type, MAX_FILE_BYTES)
                    words = words_from_text(text)
                    if words > 0:
                        if use_llm:
                            try:
                                difficulty = azure_llm_difficulty(
                                    text, az_endpoint, az_deployment, az_key, az_max_chars, az_api_version
                                )
                            except Exception as e:
                                st.warning(f"LLM difficulty failed for file '{title}': {e}")
                                difficulty = default_difficulty()
                        read_min = reading_minutes(words, base_wpm, difficulty)
                    else:
                        # page-based fallback (slides vs scans)
                        mp = 2.0 if "presentation" in (content_type or "").lower() else 3.5
                        read_min = pages * mp

            elif item_type == "Quiz":
                q = it.get("content_details") or {}
                do_min = estimate_quiz_time(q)

            else:
                # External URL/Tool: check if looks like a video link
                if any(dom in (html_url or "") for dom in ("youtube", "vimeo", "echo360", "panopto", "kaltura")):
                    key = f"{html_url}::external"
                    pending_videos[key] = {
                        "title": title or "External Video",
                        "src": html_url,
                        "hhmmss": "00:00:00",
                    }

            total = read_min + watch_min + do_min
            results.append(
                {
                    "module": it["module_name"],
                    "title": title,
                    "type": item_type,
                    "url": html_url,
                    "read_min": round(read_min, 2),
                    "watch_min": round(watch_min, 2),
                    "do_min": round(do_min, 2),
                    "total_min": round(total, 2),
                    "difficulty": difficulty,
                }
            )

        st.session_state["results"] = results
        st.session_state["pending_videos"] = pending_videos
        st.success(f"Processed {len(results)} items. Videos needing duration: {len(pending_videos)}")

with right:
    if "results" in st.session_state:
        res = st.session_state["results"]
        total_read = sum(r["read_min"] for r in res)
        total_watch = sum(r["watch_min"] for r in res)
        total_do = sum(r["do_min"] for r in res)
        st.metric("Total Read (min)", f"{total_read:.1f}")
        st.metric("Total Watch (min)", f"{total_watch:.1f}")
        st.metric("Total Do (min)", f"{total_do:.1f}")
        st.metric("Total (hrs)", f"{(total_read + total_watch + total_do) / 60:.2f}")


# --------------------------
# Manual video durations
# --------------------------
st.header("3) Enter Video Durations (hh:mm:ss)")
pending = st.session_state.get("pending_videos", {})
if pending:
    for key, meta in list(pending.items()):
        with st.expander(f"{meta['title']} — {meta.get('src','')}"):
            hhmmss = st.text_input("Duration (hh:mm:ss)", key=f"dur_{key}", value=meta.get("hhmmss", "00:00:00"))
            if st.button("💾 Save", key=f"save_{key}"):
                sec = hhmmss_to_seconds(hhmmss)
                if sec <= 0:
                    st.error("Invalid hh:mm:ss (must be > 00:00:00).")
                else:
                    # apply to results with matching URL
                    for r in st.session_state["results"]:
                        if r["url"] and r["url"] in key:
                            r["watch_min"] = round(sec / 60.0, 2)
                            r["total_min"] = round(r["read_min"] + r["watch_min"] + r["do_min"], 2)
                    meta["hhmmss"] = hhmmss
                    st.success("Saved. This session will reuse the value.")
else:
    st.info("No pending videos detected yet.")


# --------------------------
# Rollups & Export
# --------------------------
st.header("4) Module Rollups & Export")
if "results" in st.session_state and st.session_state["results"]:
    # Module totals
    rollups = {}
    for r in st.session_state["results"]:
        m = r["module"] or "(no module)"
        grp = rollups.setdefault(m, {"read": 0.0, "watch": 0.0, "do": 0.0, "total": 0.0})
        grp["read"] += r["read_min"]
        grp["watch"] += r["watch_min"]
        grp["do"] += r["do_min"]
        grp["total"] += r["total_min"]

    st.subheader("Module Totals (minutes)")
    st.dataframe(
        [{"module": m, **{k: round(v, 2) for k, v in vals.items()}} for m, vals in rollups.items()],
        use_container_width=True,
    )

    # Exports
    import csv
    from io import StringIO

    def to_csv(rows):
        cols = [
            "module",
            "title",
            "type",
            "url",
            "read_min",
            "watch_min",
            "do_min",
            "total_min",
            "difficulty_label",
            "difficulty_score",
            "wpm_adjustment",
            "level",
        ]
        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(cols)
        for r in rows:
            d = r.get("difficulty", {}) or {}
            w.writerow(
                [
                    r["module"],
                    r["title"],
                    r["type"],
                    r["url"],
                    r["read_min"],
                    r["watch_min"],
                    r["do_min"],
                    r["total_min"],
                    d.get("difficulty_label", ""),
                    d.get("difficulty_score", ""),
                    d.get("wpm_adjustment", ""),
                    level,
                ]
            )
        return sio.getvalue().encode("utf-8")

    def to_json(rows):
        return json.dumps({"level": level, "items": rows}, indent=2).encode("utf-8")

    c1, c2 = st.columns(2)
    with c1:
        st.download_button("⬇️ Download CSV", to_csv(st.session_state["results"]), "estimates.csv", "text/csv")
    with c2:
        st.download_button("⬇️ Download JSON", to_json(st.session_state["results"]), "estimates.json", "application/json")

st.divider()
st.caption(
    "Configured entirely via Streamlit Secrets. Supports Canvas Pages/Assignments/Discussions, Files (PDF/DOCX/PPTX), "
    "manual video durations, quiz heuristic, UG/Grad toggle, and optional Azure LLM difficulty."
)
