import io, re, os, json, requests, streamlit as st
from bs4 import BeautifulSoup

# Optional parsers
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None
try:
    import docx
except Exception:
    docx = None
try:
    from pptx import Presentation
except Exception:
    Presentation = None

st.set_page_config(page_title="Course Load Estimator (Secrets TOML)", page_icon="📚", layout="wide")
st.title("📚 Course Load Estimator — Secrets-driven (no manual keys)")

# --------------------------
# Load from secrets.toml
# --------------------------
from collections.abc import Mapping
import streamlit as st

def get_secret(path: str, default=None):
    cur = st.secrets
    for p in path.split("."):
        if isinstance(cur, Mapping) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

# Usage
canvas_base  = get_secret("canvas.base_url")
canvas_token = get_secret("canvas.token")
use_llm      = bool(get_secret("azure.use_llm", False))
az_endpoint  = get_secret("azure.endpoint", "")
az_key       = get_secret("azure.api_key", "")
az_deployment= get_secret("azure.deployment", "gpt-4o")
max_chars    = int(get_secret("azure.max_chars", 15000))
default_level= (get_secret("app.default_level", "Undergraduate") or "Undergraduate").strip()
max_file_mb  = int(get_secret("app.max_file_mb", 50))

max_file_bytes = max_file_mb * 1024 * 1024

# Hard fail if mandatory secrets are missing
missing = []
if not canvas_base: missing.append("canvas.base_url")
if not canvas_token: missing.append("canvas.token")
if use_llm and (not az_endpoint or not az_key):
    missing.append("azure.endpoint/api_key (required when azure.use_llm=true)")
if missing:
    st.error("Missing required secrets: " + ", ".join(missing))
    st.stop()

# --------------------------
# Sidebar (read-only summary)
# --------------------------
with st.sidebar:
    st.header("Configuration (from secrets.toml)")
    st.write(f"**Canvas Base URL**: {canvas_base}")
    st.write("**Canvas Token**: (loaded from secrets)")
    st.write(f"**Level Default**: {default_level}")
    st.write(f"**Max File Size**: {max_file_mb} MB")
    st.write(f"**LLM Difficulty**: {'Enabled' if use_llm else 'Disabled'}")
    if use_llm:
        st.write(f"**Azure Endpoint**: {az_endpoint}")
        st.write("**Azure Key**: (loaded from secrets)")
        st.write(f"**Deployment**: {az_deployment}")
        st.write(f"**Max Chars**: {max_chars}")
    st.caption("Edit `.streamlit/secrets.toml` to change these values.")

# --------------------------
# Helpers
# --------------------------
def headers():
    return {"Authorization": f"Bearer {canvas_token}"} if canvas_token else {}

def paginate(url: str):
    s = requests.Session()
    s.headers.update(headers())
    while url:
        r = s.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        yield data
        # parse RFC5988 link header
        nxt = None
        link = r.headers.get("link") or r.headers.get("Link")
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    nxt = part[part.find("<")+1:part.find(">")]
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
    return r.json().get("body","") or ""

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

VIDEO_DOMAINS = ("youtube.com","youtu.be","vimeo.com","echo360","kaltura","panopto","video.","player.")

def detect_videos_from_html(html: str):
    out = []
    soup = BeautifulSoup(html or "", "lxml")
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or ""
        title = iframe.get("title") or ""
        if any(dom in src for dom in VIDEO_DOMAINS):
            out.append({"kind":"iframe","src":src,"title":title or "Embedded Video"})
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        text = a.get_text(strip=True) or ""
        if any(dom in href for dom in VIDEO_DOMAINS):
            out.append({"kind":"link","src":href,"title":text or "Video Link"})
    return out

def hhmmss_to_seconds(hhmmss: str) -> int:
    m = re.match(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$", hhmmss.strip())
    if not m: return 0
    h, m_, s = map(int, m.groups())
    return h*3600 + m_*60 + s

# File extraction
def extract_file_text(file_url: str, content_type: str, max_bytes: int) -> tuple[str, int]:
    r = requests.get(file_url, headers=headers(), timeout=60, stream=True)
    r.raise_for_status()
    total, chunks = 0, []
    for chunk in r.iter_content(1024*64):
        total += len(chunk)
        if total > max_bytes: return ("", 0)
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        if "pdf" in content_type.lower() and pdf_extract_text:
            text = pdf_extract_text(io.BytesIO(data)) or ""
            return (text, 0)
        if ("word" in content_type.lower() or "docx" in content_type.lower()) and docx:
            d = docx.Document(io.BytesIO(data))
            text = "\n".join([p.text for p in d.paragraphs])
            return (text, 0)
        if ("powerpoint" in content_type.lower() or "pptx" in content_type.lower()) and Presentation:
            prs = Presentation(io.BytesIO(data))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        parts.append(shape.text)
            return ("\n".join(parts), len(prs.slides))
        # fallback
        return (data.decode("utf-8", errors="ignore"), 0)
    except Exception:
        return ("", 0)

def default_difficulty():
    return {"difficulty_label":"intermediate","difficulty_score":0.5,"wpm_adjustment":1.0,"rationale":"default heuristics"}

def azure_llm_difficulty(text: str, endpoint: str, deployment: str, api_key: str, max_chars: int) -> dict:
    import json, requests

    headers = {"api-key": api_key, "Content-Type": "application/json"}
    sys_msg = (
        "Return only JSON with keys: difficulty_label, difficulty_score (0..1), "
        "wpm_adjustment (0.6..1.2), rationale (<= 3 sentences)."
    )
    user_msg = f"Assess the reading difficulty and return JSON only.\nTEXT:\n{text[:max_chars]}"

    is_cogsvc = "cognitiveservices.azure.com" in endpoint
    api_version = "2024-12-01-preview"  # change here if your tenant requires a different version

    if is_cogsvc:
        # Cognitive Services endpoint REQUIRES deployment in the path + api-version
        url = endpoint.rstrip("/") + f"/openai/deployments/{deployment}/responses?api-version={api_version}"
        body = {
            # for deployment-scoped path, model is implied by deployment; body may omit "model"
            "input": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
        }
    else:
        # Classic Azure OpenAI v1 endpoint: /openai/v1/responses (no api-version), model in body
        url = endpoint.rstrip("/") + "/openai/v1/responses"
        body = {
            "model": deployment,
            "input": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
        }

    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    j = r.json()
    try:
        raw = j["output"]["content"][0]["text"]
        return json.loads(raw)
    except Exception:
        # Minimal, safe fallback
        return {
            "difficulty_label": "intermediate",
            "difficulty_score": 0.5,
            "wpm_adjustment": 1.0,
            "rationale": "default heuristics",
        }


def words_from_text(text: str) -> int:
    import re as _re
    toks = _re.findall(r"\b\w+\b", text or "", flags=_re.UNICODE)
    return len(toks)

def reading_minutes(words: int, base_wpm: int, diff: dict) -> float:
    wpm_adj = max(0.1, float(diff.get("wpm_adjustment", 1.0)))
    adjusted = max(50, base_wpm * wpm_adj)
    score = float(diff.get("difficulty_score", 0.5))
    reread = 1.0 + (0.15 * score)
    return (words / adjusted) * reread

def estimate_quiz_time(quiz: dict) -> float:
    tl = quiz.get("time_limit") if quiz else None
    if tl: return float(tl)
    qc = (quiz or {}).get("question_count") or 0
    return qc * 2.0 if qc else 10.0

# --------------------------
# UI — Scan, Process, Durations, Export
# --------------------------
level = st.selectbox("Learner Level", ["Undergraduate", "Graduate"], index=(0 if default_level.lower().startswith("under") else 1))
base_wpm = 225 if level == "Undergraduate" else 250

st.header("1) Scan a Course")
course_id = st.number_input("Canvas Course ID", min_value=1, step=1, value=12345)
if st.button("🚀 Scan (Modules & Items)"):
    progress = st.progress(0.0, text="Listing modules…")
    items = []
    try:
        # modules → items
        modules = list(list_modules_with_items(int(course_id)))
    except Exception as e:
        st.exception(e); st.stop()
    total_items = sum(len(m.get("items", [])) for m in modules)
    done = 0
    for mod in modules:
        for it in (mod.get("items") or []):
            done += 1
            progress.progress(min(0.99, done/max(1,total_items)), text=f"Fetching item {done}/{total_items}")
            items.append({
                "module_name": mod.get("name",""),
                "position": mod.get("position",0),
                "item_type": it.get("type",""),
                "title": it.get("title",""),
                "html_url": it.get("html_url",""),
                "content_id": it.get("content_id"),
                "page_url": it.get("page_url"),
                "content_details": it.get("content_details",{})
            })
    st.success(f"Collected {len(items)} items from {len(modules)} modules.")
    st.session_state["items"] = items

st.header("2) Extract & Estimate")
colA, colB = st.columns([3,2])
with colA:
    if st.button("🔎 Process Items"):
        if "items" not in st.session_state:
            st.warning("Scan the course first."); st.stop()
        results = []; pending_videos = {}
        for it in st.session_state["items"]:
            item_type, title, html_url = it["item_type"], it["title"], it["html_url"]
            read_min = watch_min = do_min = 0.0
            difficulty = default_difficulty()

            if item_type in ("Page","Assignment","Discussion"):
                try:
                    if item_type == "Page":
                        body = get_page_body(int(course_id), it.get("page_url"))
                    elif item_type == "Assignment":
                        a = get_assignment(int(course_id), it.get("content_id")); body = a.get("description","") or ""
                    else:
                        d = get_discussion(int(course_id), it.get("content_id")); body = d.get("message","") or ""
                except Exception: body = ""
                text = strip_html_to_text(body)
                vids = detect_videos_from_html(body)
                for idx,v in enumerate(vids, start=1):
                    key = f"{html_url}::{v.get('src','')}::{idx}"
                    pending_videos[key] = {"title": v.get("title","Video"), "src": v.get("src",""), "hhmmss": "00:00:00"}
                words = words_from_text(text)
                if words > 0:
                    if use_llm and az_endpoint and az_key:
                        try:
                            difficulty = azure_llm_difficulty(text, az_endpoint, az_deployment, az_key, max_chars)
                        except Exception as e:
                            st.warning(f"LLM difficulty failed for '{title}': {e}")
                            difficulty = default_difficulty()
                    read_min = reading_minutes(words, base_wpm, difficulty)
            elif item_type == "File":
                cd = it.get("content_details") or {}
                file_url, content_type = cd.get("url"), cd.get("content_type","")
                if file_url:
                    text, pages = extract_file_text(file_url, content_type, max_file_bytes)
                    words = words_from_text(text)
                    if words > 0:
                        if use_llm and az_endpoint and az_key:
                            try:
                                difficulty = azure_llm_difficulty(text, az_endpoint, az_deployment, az_key, max_chars)
                            except Exception as e:
                                st.warning(f"LLM difficulty failed for file '{title}': {e}")
                                difficulty = default_difficulty()
                        read_min = reading_minutes(words, base_wpm, difficulty)
                    else:
                        mp = 2.0 if "presentation" in content_type.lower() else 3.5
                        read_min = pages * mp
            elif item_type == "Quiz":
                q = it.get("content_details") or {}
                do_min = estimate_quiz_time(q)
            else:
                cd = it.get("content_details") or {}
                if any(dom in (html_url or "") for dom in ("youtube","vimeo","echo360","panopto","kaltura")):
                    key = f"{html_url}::external"
                    pending_videos[key] = {"title": title or "External Video", "src": html_url, "hhmmss": "00:00:00"}

            total = read_min + watch_min + do_min
            results.append({
                "module": it["module_name"], "title": title, "type": item_type, "url": html_url,
                "read_min": round(read_min,2), "watch_min": round(watch_min,2), "do_min": round(do_min,2),
                "total_min": round(total,2), "difficulty": difficulty
            })

        st.session_state["results"] = results
        st.session_state["pending_videos"] = pending_videos
        st.success(f"Processed {len(results)} items. Videos needing duration: {len(pending_videos)}")

with colB:
    if "results" in st.session_state:
        res = st.session_state["results"]
        total_read = sum(r["read_min"] for r in res)
        total_watch = sum(r["watch_min"] for r in res)
        total_do = sum(r["do_min"] for r in res)
        st.metric("Total Read (min)", f"{total_read:.1f}")
        st.metric("Total Watch (min)", f"{total_watch:.1f}")
        st.metric("Total Do (min)", f"{total_do:.1f}")
        st.metric("Total (hrs)", f"{(total_read+total_watch+total_do)/60:.2f}")

st.header("3) Enter Video Durations (hh:mm:ss)")
pending = st.session_state.get("pending_videos", {})
if pending:
    for key, meta in list(pending.items()):
        with st.expander(f"{meta['title']} — {meta.get('src','')}"):
            hhmmss = st.text_input("Duration (hh:mm:ss)", key=f"dur_{key}", value=meta.get("hhmmss","00:00:00"))
            if st.button("💾 Save", key=f"save_{key}"):
                sec = hhmmss_to_seconds(hhmmss)
                if sec <= 0:
                    st.error("Invalid hh:mm:ss (must be > 00:00:00).")
                else:
                    for r in st.session_state["results"]:
                        if r["url"] and r["url"] in key:
                            r["watch_min"] = round(sec/60.0,2)
                            r["total_min"] = round(r["read_min"] + r["watch_min"] + r["do_min"],2)
                    meta["hhmmss"] = hhmmss
                    st.success("Saved. This session will reuse the value.")
else:
    st.info("No pending videos detected yet.")

st.header("4) Module Rollups & Export")
if "results" in st.session_state and st.session_state["results"]:
    rollups = {}
    for r in st.session_state["results"]:
        m = r["module"] or "(no module)"
        grp = rollups.setdefault(m, {"read":0.0,"watch":0.0,"do":0.0,"total":0.0})
        grp["read"] += r["read_min"]; grp["watch"] += r["watch_min"]; grp["do"] += r["do_min"]; grp["total"] += r["total_min"]
    st.subheader("Module Totals (minutes)")
    st.dataframe([{"module":m, **{k:round(v,2) for k,v in vals.items()}} for m,vals in rollups.items()])

    import csv
    from io import StringIO
    def to_csv(rows):
        cols = ["module","title","type","url","read_min","watch_min","do_min","total_min","difficulty_label","difficulty_score","wpm_adjustment","level"]
        sio = StringIO()
        w = csv.writer(sio); w.writerow(cols)
        for r in rows:
            d = r.get("difficulty",{}) or {}
            w.writerow([r["module"], r["title"], r["type"], r["url"], r["read_min"], r["watch_min"], r["do_min"], r["total_min"],
                        d.get("difficulty_label",""), d.get("difficulty_score",""), d.get("wpm_adjustment",""), level])
        return sio.getvalue().encode("utf-8")
    def to_json(rows):
        return json.dumps({"level": level, "items": rows}, indent=2).encode("utf-8")

    c1, c2 = st.columns(2)
    with c1: st.download_button("⬇️ Download CSV", to_csv(st.session_state["results"]), "estimates.csv", "text/csv")
    with c2: st.download_button("⬇️ Download JSON", to_json(st.session_state["results"]), "estimates.json", "application/json")

st.divider()
st.caption("Configured entirely via `.streamlit/secrets.toml`. No keys entered in the UI.")
