"""Streamlit web app -- Green Steel News Generator."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import zipfile
from datetime import datetime
from typing import List

# Fix "Event loop is closed" error on Windows with Python 3.10+
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

st.set_page_config(page_title="Green Steel News", page_icon="\U0001f3ed", layout="wide")

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import CATEGORIES, QUALITY_THRESHOLD
from main import run_pipeline
from models.article import Article

_SAMPLE_TOPICS_PATH = os.path.join(_PROJECT_ROOT, "sample_topics.json")

CATEGORY_COLORS = {
    "Renewable Energy":                        "#2e7d32",
    "Hydrogen Production & Technology":        "#1565c0",
    "Green Iron & Low-Carbon Feedstocks":      "#4e342e",
    "Circular Economy (Scrap)":                "#558b2f",
    "CCS & CCUS":                              "#6a1b9a",
    "Steel Demand, Procurement & End Markets": "#e65100",
    "Steel Prices & Green Premiums":           "#b71c1c",
    "Raw Material Prices":                     "#f57f17",
    "Clean Energy Logistics & Storage":        "#00695c",
    "Project Finance & Investment":            "#1a237e",
    "Trade, Tariffs & Regulations":            "#880e4f",
    "Climate Policy & Environment":            "#1b5e20",
    "Corporate Offtake":                       "#bf360c",
    "Partnerships & M&A":                      "#4a148c",
    "Green Steel Projects & Plant Development":"#0d47a1",
}


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------
def _build_pdf(article: Article) -> bytes:
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=22*mm, bottomMargin=22*mm)
    green = colors.HexColor("#1e6430")
    dark  = colors.HexColor("#111111")
    grey  = colors.HexColor("#555555")
    light = colors.HexColor("#f5f5f5")

    S = lambda name, **kw: ParagraphStyle(name, **kw)
    cat_s  = S("cat",  fontName="Helvetica-Bold",    fontSize=8,  textColor=colors.white, backColor=green, spaceAfter=6, leading=14, leftIndent=4)
    head_s = S("head", fontName="Helvetica-Bold",    fontSize=20, textColor=dark, spaceAfter=6, leading=24)
    date_s = S("date", fontName="Helvetica-Oblique", fontSize=10, textColor=grey, spaceAfter=4)
    body_s = S("body", fontName="Helvetica",         fontSize=11, textColor=dark, spaceAfter=8, leading=16)
    lbl_s  = S("lbl",  fontName="Helvetica-Bold",    fontSize=9,  textColor=dark, spaceAfter=2)
    meta_s = S("meta", fontName="Helvetica",         fontSize=9,  textColor=grey, spaceAfter=3)
    src_s  = S("src",  fontName="Helvetica",         fontSize=8,  textColor=colors.HexColor("#3050b4"), spaceAfter=3)

    story = []
    qs = article.quality_score
    story.append(Paragraph("  " + article.category.upper() + "  ", cat_s))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(article.headline, head_s))
    story.append(Paragraph(article.dateline, date_s))
    story.append(HRFlowable(width="100%", thickness=1.5, color=green, spaceAfter=6))

    for para in article.body.split("\n\n"):
        para = para.strip()
        if para:
            para = para.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            story.append(Paragraph(para, body_s))

    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    story.append(Paragraph("ARTICLE METADATA", lbl_s))
    score_str = f"{qs.overall:.2f}/10  -  {'PASS' if qs.passed else 'FAIL'}" if qs else "N/A"
    t = Table([
        ["Category:", article.category],
        ["Confidence:", f"{article.category_confidence*100:.0f}%"],
        ["Word Count:", str(article.word_count)],
        ["Quality:", score_str],
        ["Created:", article.created_at[:10]],
    ], colWidths=[40*mm, 120*mm])
    t.setStyle(TableStyle([
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("FONTNAME",(1,0),(1,-1),"Helvetica"),
        ("FONTSIZE",(0,0),(-1,-1),9),
        ("TEXTCOLOR",(0,0),(0,-1),dark),
        ("TEXTCOLOR",(1,0),(1,-1),grey),
        ("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("TOPPADDING",(0,0),(-1,-1),3),
        ("BACKGROUND",(0,0),(-1,-1),light),
    ]))
    story.append(t)

    if article.sources:
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph("SOURCES", lbl_s))
        for src in article.sources[:6]:
            src_e = src[:120].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            story.append(Paragraph(src_e, src_s))

    if article.key_players:
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph("KEY PLAYERS", lbl_s))
        story.append(Paragraph("  |  ".join(article.key_players), meta_s))

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Markdown / JSON builders
# ---------------------------------------------------------------------------
def _build_md(article: Article) -> str:
    qs = article.quality_score
    sources_yaml = "\n".join(f'  - "{s}"' for s in article.sources)
    players_yaml = "\n".join(f'  - "{p}"' for p in article.key_players)
    fm = (
        "---\n"
        f'title: "{article.headline}"\n'
        f'category: "{article.category}"\n'
        f'dateline: "{article.dateline}"\n'
        f"word_count: {article.word_count}\n"
        f"quality_score: {qs.overall if qs else 'N/A'}\n"
        f"quality_passed: {qs.passed if qs else 'N/A'}\n"
        f'created_at: "{article.created_at}"\n'
        f"sources:\n{sources_yaml}\n"
        f"key_players:\n{players_yaml}\n"
        "---\n\n"
    )
    return fm + f"# {article.headline}\n\n**{article.dateline}**\n\n---\n\n{article.body}\n"


def _build_json(article: Article) -> str:
    return json.dumps(article.model_dump(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample_topics() -> list:
    if not os.path.exists(_SAMPLE_TOPICS_PATH):
        return []
    with open(_SAMPLE_TOPICS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    result = []
    for t in data.get("topics", []):
        if isinstance(t, str):
            result.append({"category": "", "topic": t})
        else:
            result.append({"category": t.get("category",""), "topic": t.get("topic","")})
    return result


def _category_badge(category: str) -> str:
    color = CATEGORY_COLORS.get(category, "#444444")
    return (
        f'<span style="background:{color};color:white;padding:2px 10px;'
        f'border-radius:12px;font-size:11px;font-weight:600;letter-spacing:0.5px;">'
        f'{category}</span>'
    )


def _quality_badge(qs) -> str:
    if not qs:
        return ""
    color = "#2e7d32" if qs.passed else "#c62828"
    label = f"{'PASS' if qs.passed else 'FAIL'}  {qs.overall:.1f}/10"
    return (
        f'<span style="background:{color};color:white;padding:2px 9px;'
        f'border-radius:12px;font-size:11px;font-weight:600;">{label}</span>'
    )


# ---------------------------------------------------------------------------
# News feed card
# ---------------------------------------------------------------------------
def _news_card(article: Article, index: int):
    qs    = article.quality_score
    slug  = article.slug or f"article_{index}"
    color = CATEGORY_COLORS.get(article.category, "#444444")
    paras = [p.strip() for p in article.body.split("\n\n") if p.strip()]
    lead  = paras[0] if paras else ""

    st.markdown(
        f"""
        <div style="border-left:4px solid {color};padding:0 0 0 16px;margin-bottom:8px;">
            {_category_badge(article.category)}&nbsp;&nbsp;{_quality_badge(qs)}
            <h3 style="margin:10px 0 4px 0;font-size:20px;line-height:1.3;color:#111;">{article.headline}</h3>
            <p style="color:#777;font-size:13px;margin:0 0 10px 0;">{article.dateline}&nbsp;&nbsp;|&nbsp;&nbsp;{article.word_count} words</p>
            <p style="color:#333;font-size:15px;line-height:1.7;margin:0;">{lead}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c_read, c_pdf, c_md, c_json, _ = st.columns([1.4, 0.8, 0.8, 0.8, 4])
    with c_read:
        if st.button("Read full article", key=f"read_{index}"):
            key = f"exp_{index}"
            st.session_state[key] = not st.session_state.get(key, False)
    with c_pdf:
        try:
            st.download_button("PDF", data=_build_pdf(article), file_name=f"{slug}.pdf", mime="application/pdf", key=f"pdf_{index}")
        except Exception:
            pass
    with c_md:
        st.download_button("MD", data=_build_md(article), file_name=f"{slug}.md", mime="text/markdown", key=f"md_{index}")
    with c_json:
        st.download_button("JSON", data=_build_json(article), file_name=f"{slug}.json", mime="application/json", key=f"json_{index}")

    if st.session_state.get(f"exp_{index}", False):
        body_html = "".join(f'<p style="margin:0 0 16px 0;line-height:1.8;">{p}</p>' for p in paras)
        st.markdown(
            f'<div style="background:#fafafa;border:1px solid #e0e0e0;border-radius:8px;padding:28px 32px;margin:8px 0 16px 0;font-size:15px;color:#222;">' +
            f'<p style="color:#777;font-size:13px;font-style:italic;margin:0 0 14px 0;">{article.dateline}</p>' +
            body_html + "</div>",
            unsafe_allow_html=True,
        )
        if qs:
            cols = st.columns(5)
            for col, lbl, val in zip(cols,
                ["Newsworthiness","Specificity","Readability","Structure","Category Fit"],
                [qs.newsworthiness,qs.specificity,qs.readability,qs.structure,qs.category_fit]):
                col.metric(lbl, f"{val:.1f}/10")
        if qs and not qs.passed and qs.feedback:
            st.warning(f"Quality note: {qs.feedback}")

    st.markdown("<hr style='border:none;border-top:1px solid #eee;margin:16px 0;'>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## Green Steel News")
    st.caption("AI-Powered Industry Content")
    st.divider()
    generation_mode = st.radio("Mode", ["Single article", "Batch topics", "Category sweep"])
    st.divider()
    st.markdown("**Filter news feed**")
    filter_cat = st.selectbox("Category", ["All categories"] + CATEGORIES, key="filter_cat")
    st.divider()
    with st.expander("Pipeline"):
        st.markdown("""
1. **Research** -- live Google News + RSS
2. **Classify** -- 1 of 15 categories
3. **Write** -- 500-700 word article
4. **Humanize** -- journalist tone
5. **Quality** -- score & gate at 7.0
6. **Publish** -- MD + JSON + PDF
""")


# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------
tab_generate, tab_feed = st.tabs(["Generate", "News Feed"])


# === TAB 1: GENERATE ========================================================
with tab_generate:
    st.markdown("### Generate New Article")
    sample_topics = _load_sample_topics()

    if generation_mode == "Single article":
        sample_labels = ["-- load a sample topic --"] + [t["topic"] for t in sample_topics]
        col_t, col_c = st.columns([3, 1])
        with col_t:
            sel = st.selectbox("Sample topics", sample_labels)
            prefill = sel if sel != "-- load a sample topic --" else ""
            topic_input = st.text_area("Topic / story angle", value=prefill, height=90,
                placeholder="e.g. SSAB secures green bond for HYBRIT scale-up in Sweden")
        with col_c:
            selected_cat = st.selectbox("Category", ["Auto-detect"] + CATEGORIES)

        if st.button("Generate Article", type="primary"):
            if not topic_input.strip():
                st.warning("Please enter a topic.")
            else:
                cat_override = None if selected_cat == "Auto-detect" else selected_cat
                with st.spinner("Running 6-agent pipeline..."):
                    try:
                        article = asyncio.run(run_pipeline(topic_input.strip(), category_override=cat_override))
                        st.session_state.setdefault("articles", [])
                        st.session_state["articles"] = [article] + st.session_state["articles"]
                        st.success("Article generated -- switch to the News Feed tab to read it.")
                    except Exception as exc:
                        st.error(f"Pipeline error: {exc}")

    elif generation_mode == "Batch topics":
        st.caption("Enter one topic per line.")
        topics_raw = st.text_area("Topics (one per line)", value="", height=180)
        col_c2, _ = st.columns([2, 4])
        with col_c2:
            selected_cat2 = st.selectbox("Category override", ["Auto-detect"] + CATEGORIES)

        if st.button("Generate Batch", type="primary"):
            topics_list = [t.strip() for t in topics_raw.splitlines() if t.strip()]
            if not topics_list:
                st.warning("Please enter at least one topic.")
            else:
                cat_override = None if selected_cat2 == "Auto-detect" else selected_cat2
                st.session_state.setdefault("articles", [])
                prog = st.progress(0, text="Starting batch...")
                for i, topic in enumerate(topics_list):
                    prog.progress(i / len(topics_list), text=f"{i+1}/{len(topics_list)}: {topic[:60]}...")
                    try:
                        art = asyncio.run(run_pipeline(topic, category_override=cat_override))
                        st.session_state["articles"].insert(0, art)
                    except Exception as exc:
                        st.warning(f"Skipped: {exc}")
                prog.progress(1.0, text="Done.")
                st.success("Done -- switch to News Feed to read the articles.")

    else:
        st.caption("Generates one article per selected category.")
        selected_cats = st.multiselect("Categories", options=CATEGORIES, default=CATEGORIES[:5])
        topic_map = {t["category"]: t["topic"] for t in _load_sample_topics() if t.get("category")}

        if st.button("Run Category Sweep", type="primary"):
            if not selected_cats:
                st.warning("Select at least one category.")
            else:
                st.session_state.setdefault("articles", [])
                prog = st.progress(0, text="Starting sweep...")
                for i, cat in enumerate(selected_cats):
                    topic = topic_map.get(cat, f"Latest developments in {cat}")
                    prog.progress(i / len(selected_cats), text=f"{i+1}/{len(selected_cats)} -- {cat}")
                    try:
                        art = asyncio.run(run_pipeline(topic, category_override=cat))
                        st.session_state["articles"].insert(0, art)
                    except Exception as exc:
                        st.warning(f"Skipped {cat}: {exc}")
                prog.progress(1.0, text="Done.")
                st.success("Done -- switch to News Feed to read the articles.")


# === TAB 2: NEWS FEED =======================================================
with tab_feed:
    articles: List[Article] = st.session_state.get("articles", [])
    filter_val = st.session_state.get("filter_cat", "All categories")
    filtered   = [a for a in articles if filter_val == "All categories" or a.category == filter_val]

    if not articles:
        st.markdown(
            """
            <div style="text-align:center;padding:60px 20px;color:#999;">
                <div style="font-size:48px;">&#128240;</div>
                <h3 style="color:#bbb;margin:16px 0 8px;">No articles yet</h3>
                <p>Go to the <strong>Generate</strong> tab and create your first article.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        col_h, col_clr, col_zip = st.columns([4, 1, 2])
        with col_h:
            st.markdown(f"### Published Articles ({len(filtered)} shown)")
        with col_clr:
            if st.button("Clear all"):
                st.session_state["articles"] = []
                st.rerun()
        with col_zip:
            if len(articles) > 1:
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for art in articles:
                        s = art.slug or "article"
                        zf.writestr(f"{s}.md",   _build_md(art))
                        zf.writestr(f"{s}.json", _build_json(art))
                        try:
                            zf.writestr(f"{s}.pdf", _build_pdf(art))
                        except Exception:
                            pass
                st.download_button(
                    f"Download all ({len(articles)}) as ZIP",
                    data=zip_buf.getvalue(),
                    file_name=f"green_steel_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.zip",
                    mime="application/zip",
                )

        # Category stats
        if articles:
            cat_counts: dict = {}
            for a in articles:
                cat_counts[a.category] = cat_counts.get(a.category, 0) + 1
            top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:5]
            stat_cols = st.columns(len(top_cats))
            for col, (cat, count) in zip(stat_cols, top_cats):
                c = CATEGORY_COLORS.get(cat, "#444")
                col.markdown(
                    f'<div style="background:{c}18;border-left:3px solid {c};padding:6px 10px;border-radius:4px;font-size:12px;">' +
                    f'<strong>{count}</strong> {cat}</div>',
                    unsafe_allow_html=True,
                )

        st.markdown("<br>", unsafe_allow_html=True)

        if not filtered:
            st.info(f"No articles in category: {filter_val}")
        else:
            for idx, art in enumerate(filtered):
                _news_card(art, idx)
