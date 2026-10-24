"""多场景多难度自适应 AI 虚拟面试智能体 —— v1 主程序

对应 DESIGN.md 第十三节的工程结构。v1 范围：文本面试 + 三级配置 + 三体 Agent + 复盘报告。

运行方式（Windows PowerShell，在项目目录下执行）：
    py -3.12 -m pip install -r requirements.txt
    py -3.12 -m streamlit run app.py
然后在浏览器打开的页面左侧填入 DeepSeek API Key 即可。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import streamlit as st

import calibration
import history_store
import interview_state
import jd_match
import mood
import resume_optimize
import session_store
from agents import (
    advisor_review,
    coach_review,
    evaluate_interview,
    interviewer_turn,
    reset_usage,
    resume_review,
    test_connection,
    usage_snapshot,
)
from avatar import avatar_component
from knowledge import (
    LEVELS,
    MAJORS,
    MOOD_FRIENDLY,
    MOOD_MODES,
    MOOD_ORDER,
    SCENES,
    TIER_LABELS,
    VERDICT_FAIL,
    VERDICT_PARTIAL,
    VERDICT_UNKNOWN,
    aggregate_rubric_check,
    build_profile,
    level_labels,
    profile_summary,
    topic_mix_quotas,
    topic_tier,
)
from resume import ACCEPTED_TYPES, extract_text
from voice import (
    DEFAULT_VOICE,
    VOICES,
    correct_asr,
    is_valid_audio,
    reset_edge_breaker,
    synthesize,
    transcribe,
)

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "history.json"

ACTION_LABELS = {"ask": "换新题", "probe": "追问", "reflect": "反馈", "end": "收尾"}

DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
SILICONFLOW_KEY_ENV = "SILICONFLOW_API_KEY"


def load_api_key(env_name: str) -> str:
    """读取某个 API Key：进程环境变量 → Windows 用户级环境变量。

    加第二层回退是因为：如果应用是从"环境变量写入之前"就已经打开的终端里启动的，
    该进程的环境副本里没有这个变量，这时只能直接读注册表才拿得到。
    """
    key = os.getenv(env_name, "").strip()
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, env_name)
            return str(value).strip()
        except OSError:
            return ""
    return ""


def siliconflow_key() -> str:
    """硅基流动 Key：环境变量优先，其次是侧边栏手填的那个输入框。"""
    return st.session_state.sf_key or st.session_state.get("sf_key_input", "")


def _last_question() -> str:
    """取面试官最近一次提问的原文，作为语音纠错的上下文。"""
    for msg in reversed(st.session_state.history):
        if msg["role"] == "assistant":
            return msg["content"]
    return ""


def _correct_voice(raw: str) -> str:
    """语音识别结果再用 DeepSeek 结合题干轻量纠错；任何失败都回退原文，绝不中断。"""
    api_key = st.session_state.get("api_key", "")
    if not raw or not api_key:
        return raw
    try:
        return correct_asr(raw, _last_question(), api_key)
    except Exception:  # noqa: BLE001
        return raw


def _awaiting_interviewer() -> bool:
    """面试官该说话、但还没说出口——说明上一轮模型调用失败了。

    两种情形都算：
      - history 末尾停在候选人回答（本轮失败）→ 那句回答还在，可直接重试；
      - history 为空（开场就失败）→ 重试即重新开场。
    正常一轮结束时末尾必是 assistant，所以这里返回 False。
    """
    history = st.session_state.get("history") or []
    if not history:
        return True
    return history[-1].get("role") == "user"


st.set_page_config(page_title="知遇 · AI 虚拟面试官", page_icon="🎤", layout="wide")


# =====================================================================
# 全局 CSS —— 知遇 · AI 虚拟面试官
# 风格：暖纸编辑风
#   暖米纸面底 / 墨黑正文 / 柿橙点缀 #C2481B / 衬线标题 /
#   圆角卡片 / 柔和投影 / 宽松留白
# 说明：仅注入外观样式（unsafe_allow_html），不触碰功能类名与 JS。
# =====================================================================
st.markdown(
    """
    <style>
    /* ============================================================
       知遇 · 全局设计系统 ——「暖纸编辑风」
       暖米纸面底 / 墨黑正文 / 柿橙点缀 #C2481B / 衬线标题 /
       圆角卡片 / 柔和投影 / 宽松留白
       ============================================================ */
    :root {
        --zy-paper: #F5F0E8;
        --zy-surface: #FFFFFF;
        --zy-ink: #211D19;
        --zy-muted: #6B6259;
        --zy-faint: #9A9184;
        --zy-accent: #C2481B;
        --zy-accent-deep: #9E3A14;
        --zy-accent-soft: #FBEAE0;
        --zy-line: #E5DCCD;
        --zy-line-strong: #D8CFC0;
        --zy-serif: "Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", "SimSun", serif;
        --zy-sans: "Microsoft YaHei UI", "Noto Sans SC", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
        --zy-shadow-sm: 0 1px 2px rgba(33, 29, 25, 0.05);
        --zy-shadow-md: 0 1px 2px rgba(33, 29, 25, 0.04), 0 12px 32px rgba(33, 29, 25, 0.07);
    }

    /* ============================================================
       0. 全局基础
       ============================================================ */
    html, body,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    [data-testid="stAppViewContainer"] .main {
        background-color: var(--zy-paper) !important;
        color: var(--zy-ink);
        font-family: var(--zy-sans);
        letter-spacing: 0.01em;
    }
    [data-testid="stAppViewContainer"] *:focus-visible {
        outline: 2px solid var(--zy-accent) !important;
        outline-offset: 1px;
    }

    /* ============================================================
       1. 侧边栏 —— 暖白底 / 柔和右线
       ============================================================ */
    [data-testid="stSidebar"] {
        background-color: #F1EADF !important;
        border-right: 1px solid var(--zy-line);
        min-width: 300px;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding: 1.75rem 1.25rem;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarNav"] {
        background-color: #F1EADF !important;
        border-bottom: 1px solid var(--zy-line);
        padding-top: 0.5rem;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarNav"] a {
        color: var(--zy-ink);
        font-weight: 500;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarNav"] a:hover {
        color: var(--zy-accent);
        background-color: var(--zy-surface);
    }
    [data-testid="stSidebar"] [data-testid="stSidebarNav"] a[aria-current="page"] {
        background-color: var(--zy-accent-soft);
        color: var(--zy-accent-deep);
        font-weight: 700;
    }
    [data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        padding: 0 0.25rem;
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
    [data-testid="stSidebar"] [data-testid="stHeading"] h1 {
        font-family: var(--zy-serif);
        font-size: 1.3125rem;
        font-weight: 800;
        letter-spacing: 0.02em;
        color: var(--zy-ink);
        margin: 0.25rem 0 0.875rem;
        padding-bottom: 0.5rem;
        border-bottom: 2px solid var(--zy-accent);
        display: inline-block;
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2,
    [data-testid="stSidebar"] [data-testid="stHeading"] h2 {
        font-size: 0.9375rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        color: var(--zy-ink);
        margin: 1.25rem 0 0.5rem;
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        color: var(--zy-muted);
        font-size: 0.875rem;
        line-height: 1.6;
    }

    /* ============================================================
       2. 按钮 —— 胶囊形 / 柿橙主色 / hover 转墨黑
       ============================================================ */
    [data-testid="stButton"] button,
    [data-testid="stFormSubmitButton"] button,
    [data-testid="stDownloadButton"] button,
    [data-testid="baseButton-primary"],
    [data-testid="baseButton-secondary"],
    [data-testid="baseButton-primaryFormSubmit"],
    [data-testid="baseButton-secondaryFormSubmit"] {
        border-radius: 999px !important;
        font-family: inherit;
        font-weight: 600;
        letter-spacing: 0.02em;
        padding: 0.5rem 1.5rem;
        border-width: 1px;
        border-style: solid;
        box-shadow: none !important;
        transition: background-color 0.18s ease, border-color 0.18s ease,
                    color 0.18s ease, transform 0.18s ease;
    }
    [data-testid="stButton"] button[kind="primary"],
    [data-testid="baseButton-primary"],
    [data-testid="baseButton-primaryFormSubmit"] {
        background-color: var(--zy-accent) !important;
        border-color: var(--zy-accent) !important;
        color: #FFFFFF !important;
    }
    [data-testid="stButton"] button[kind="primary"]:hover:not(:disabled),
    [data-testid="baseButton-primary"]:hover:not(:disabled),
    [data-testid="baseButton-primaryFormSubmit"]:hover:not(:disabled) {
        background-color: var(--zy-ink) !important;
        border-color: var(--zy-ink) !important;
        color: #FFFFFF !important;
    }
    [data-testid="stButton"] button[kind="secondary"],
    [data-testid="baseButton-secondary"],
    [data-testid="baseButton-secondaryFormSubmit"] {
        background-color: var(--zy-surface) !important;
        border-color: var(--zy-line-strong) !important;
        color: var(--zy-ink) !important;
    }
    [data-testid="stButton"] button[kind="secondary"]:hover:not(:disabled),
    [data-testid="baseButton-secondary"]:hover:not(:disabled),
    [data-testid="baseButton-secondaryFormSubmit"]:hover:not(:disabled) {
        border-color: var(--zy-ink) !important;
        color: var(--zy-accent) !important;
    }
    [data-testid="stButton"] button:disabled,
    [data-testid="baseButton-primary"]:disabled,
    [data-testid="baseButton-secondary"]:disabled {
        background-color: #E7E0D3 !important;
        border-color: #E7E0D3 !important;
        color: var(--zy-faint) !important;
    }

    /* ============================================================
       3. 表单控件
       ============================================================ */
    /* --- selectbox / multiselect --- */
    [data-testid="stSelectbox"] [data-baseweb="select"] > div,
    [data-testid="stMultiSelect"] [data-baseweb="select"] > div {
        background-color: var(--zy-surface) !important;
        border: 1px solid var(--zy-line-strong) !important;
        border-radius: 10px !important;
        color: var(--zy-ink);
    }
    [data-testid="stSelectbox"] [data-baseweb="select"] > div:hover,
    [data-testid="stMultiSelect"] [data-baseweb="select"] > div:hover {
        border-color: var(--zy-accent) !important;
    }
    [data-testid="stSelectbox"] [data-baseweb="select"] input,
    [data-testid="stMultiSelect"] [data-baseweb="select"] input,
    [data-testid="stSelectbox"] [data-baseweb="select"] span,
    [data-testid="stMultiSelect"] [data-baseweb="select"] span {
        color: var(--zy-ink);
    }
    [data-baseweb="popover"] [data-baseweb="menu"],
    [data-baseweb="menu"],
    [data-baseweb="popover"] {
        background-color: var(--zy-surface) !important;
        border: 1px solid var(--zy-line) !important;
        border-radius: 12px !important;
        box-shadow: var(--zy-shadow-md) !important;
    }
    [data-baseweb="menu"] li,
    [data-baseweb="menu"] [role="option"] {
        background-color: transparent;
        color: var(--zy-ink);
    }
    [data-baseweb="menu"] li:hover,
    [data-baseweb="menu"] [role="option"]:hover {
        background-color: var(--zy-accent-soft);
    }
    [data-baseweb="menu"] li[aria-selected="true"],
    [data-baseweb="menu"] li[aria-current="true"],
    [data-baseweb="menu"] [role="option"][aria-selected="true"] {
        background-color: var(--zy-accent) !important;
        color: #FFFFFF !important;
    }

    /* --- radio --- */
    [data-testid="stRadio"] [role="radiogroup"] label {
        color: var(--zy-ink);
        font-size: 0.9375rem;
    }
    [data-testid="stRadio"] [data-baseweb="radio"] > span:first-child {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line-strong);
        width: 18px;
        height: 18px;
    }
    [data-testid="stRadio"] [data-baseweb="radio"] input:checked + span {
        background-color: var(--zy-accent);
        border-color: var(--zy-accent);
    }
    [data-testid="stRadio"] [data-baseweb="radio"] input:checked + span > span {
        background-color: #FFFFFF !important;
    }

    /* --- text_input / text_area / number_input --- */
    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input {
        background-color: var(--zy-surface) !important;
        border: 1px solid var(--zy-line-strong) !important;
        border-radius: 10px !important;
        box-shadow: none !important;
        color: var(--zy-ink) !important;
        caret-color: var(--zy-accent);
    }
    [data-testid="stTextInput"] input:focus,
    [data-testid="stTextArea"] textarea:focus,
    [data-testid="stNumberInput"] input:focus,
    [data-testid="stDateInput"] input:focus {
        border-color: var(--zy-accent) !important;
        box-shadow: 0 0 0 3px rgba(194, 72, 27, 0.14) !important;
    }
    [data-testid="stTextInput"] input::placeholder,
    [data-testid="stTextArea"] textarea::placeholder {
        color: var(--zy-faint);
    }
    [data-testid="stTextInput"] [data-baseweb="input"],
    [data-testid="stTextArea"] [data-baseweb="textarea"] {
        background-color: transparent;
    }

    /* --- checkbox --- */
    [data-testid="stCheckbox"] label {
        color: var(--zy-ink);
        font-size: 0.9375rem;
    }
    [data-testid="stCheckbox"] [data-baseweb="checkbox"] > span:first-child {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line-strong);
        border-radius: 5px !important;
    }
    [data-testid="stCheckbox"] [data-baseweb="checkbox"] input:checked + span {
        background-color: var(--zy-accent);
        border-color: var(--zy-accent);
    }

    /* --- file_uploader --- */
    [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] {
        background-color: var(--zy-surface) !important;
        border: 1.5px dashed var(--zy-line-strong) !important;
        border-radius: 14px !important;
        box-shadow: none !important;
        color: var(--zy-ink);
    }
    [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"]:hover {
        border-color: var(--zy-accent) !important;
        background-color: #FFFDF9 !important;
    }
    [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button {
        border: none !important;
        border-radius: 999px !important;
        background-color: var(--zy-accent) !important;
        color: #FFFFFF !important;
        font-weight: 600;
    }
    [data-testid="stFileUploader"] [data-testid="stFileUploaderFile"] {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        border-radius: 10px !important;
    }
    [data-testid="stFileUploader"] [data-testid="stFileUploaderFile"] button {
        border: 1px solid var(--zy-line-strong);
        border-radius: 999px !important;
    }

    /* ============================================================
       4. Expander —— 白色圆角卡片 / 柔和投影
       ============================================================ */
    [data-testid="stExpander"] details {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        border-radius: 14px !important;
        box-shadow: var(--zy-shadow-sm) !important;
        overflow: hidden;
    }
    [data-testid="stExpander"] summary {
        color: var(--zy-ink);
        font-weight: 600;
        letter-spacing: 0.02em;
        padding: 0.8rem 1.1rem;
    }
    [data-testid="stExpander"] summary:hover {
        color: var(--zy-accent);
        background-color: #FFFDF9;
    }
    [data-testid="stExpander"] summary svg {
        color: var(--zy-muted);
    }

    /* ============================================================
       5. Chat 消息气泡 —— 大圆角 / 面试官白卡 / 用户柿橙
       ============================================================ */
    [data-testid="stChatMessage"] {
        background-color: transparent;
    }
    [data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {
        border-radius: 16px !important;
        padding: 0.9rem 1.2rem;
        font-size: 1rem;
        line-height: 1.7;
        max-width: 78%;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"])
    [data-testid="stChatMessageContent"] {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        box-shadow: var(--zy-shadow-sm) !important;
        color: var(--zy-ink);
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
    [data-testid="stChatMessageContent"] {
        background-color: var(--zy-accent);
        border: 1px solid var(--zy-accent);
        box-shadow: 0 6px 18px rgba(194, 72, 27, 0.22) !important;
        color: #FFFFFF;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
    [data-testid="stChatMessageContent"] a {
        color: #FFFFFF;
        text-decoration: underline;
    }
    [data-testid="stChatMessageAvatarUser"],
    [data-testid="stChatMessageAvatarAssistant"] {
        border-radius: 50% !important;
        background-color: var(--zy-accent-soft);
        font-size: 1rem;
    }

    /* ============================================================
       6. Tabs —— 下划线式 / 暖色底线
       ============================================================ */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        background-color: transparent;
        border-bottom: 1px solid var(--zy-line);
        gap: 0.25rem;
        padding: 0;
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] button[role="tab"] {
        background-color: transparent;
        border: none;
        border-bottom: 3px solid transparent;
        color: var(--zy-muted);
        font-weight: 500;
        letter-spacing: 0.02em;
        padding: 0.625rem 1.25rem;
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] button[role="tab"]:hover {
        color: var(--zy-ink);
        border-bottom-color: var(--zy-line-strong);
    }
    [data-testid="stTabs"] [data-baseweb="tab-list"] button[role="tab"][aria-selected="true"] {
        color: var(--zy-accent);
        border-bottom: 3px solid var(--zy-accent);
        font-weight: 700;
    }
    [data-testid="stTabs"] [data-baseweb="tab-highlight"],
    [data-testid="stTabs"] [data-baseweb="tab-border"] {
        display: none;
    }

    /* ============================================================
       7. Spinner
       ============================================================ */
    [data-testid="stSpinner"] {
        color: var(--zy-ink);
        font-weight: 500;
    }
    [data-testid="stSpinner"] [data-testid="stSpinnerText"] {
        color: var(--zy-muted);
    }
    [data-testid="stSpinner"] svg {
        stroke: var(--zy-accent) !important;
        fill: var(--zy-accent) !important;
        color: var(--zy-accent) !important;
    }

    /* ============================================================
       8. 提示框 —— 暖白圆角 / 左侧柿橙竖条
       ============================================================ */
    [data-testid="stAlert"],
    [data-testid="stNotification"] {
        background-color: var(--zy-surface) !important;
        border: 1px solid var(--zy-line) !important;
        border-left: 4px solid var(--zy-accent) !important;
        border-radius: 12px !important;
        box-shadow: var(--zy-shadow-sm) !important;
        color: var(--zy-ink);
        font-size: 0.9375rem;
    }
    [data-testid="stAlert"] svg,
    [data-testid="stNotification"] svg {
        fill: var(--zy-accent);
        color: var(--zy-accent);
    }
    [data-testid="stAlert"] p,
    [data-testid="stNotification"] p {
        color: var(--zy-ink);
    }

    /* ============================================================
       9. 标题层级 —— 衬线标题 / 编辑排版
       ============================================================ */
    [data-testid="stHeading"] h1,
    [data-testid="stTitle"] h1,
    [data-testid="stMarkdownContainer"] h1 {
        font-family: var(--zy-serif);
        font-size: 2.125rem;
        font-weight: 800;
        letter-spacing: 0;
        line-height: 1.22;
        color: var(--zy-ink);
        margin: 0 0 1.375rem;
    }
    [data-testid="stHeading"] h2,
    [data-testid="stHeader"] h2,
    [data-testid="stMarkdownContainer"] h2 {
        font-family: var(--zy-serif);
        font-size: 1.5rem;
        font-weight: 700;
        line-height: 1.3;
        color: var(--zy-ink);
        margin: 2rem 0 0.75rem;
    }
    [data-testid="stHeading"] h3,
    [data-testid="stMarkdownContainer"] h3 {
        font-size: 1rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        color: var(--zy-accent);
        margin: 1.5rem 0 0.625rem;
    }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li {
        color: var(--zy-ink);
        font-size: 1rem;
        line-height: 1.75;
    }
    [data-testid="stMarkdownContainer"] a {
        color: var(--zy-accent);
        font-weight: 500;
        text-decoration: none;
        border-bottom: 1px solid rgba(194, 72, 27, 0.35);
    }
    [data-testid="stMarkdownContainer"] strong {
        color: var(--zy-ink);
        font-weight: 700;
    }

    /* ============================================================
       10. 分割线
       ============================================================ */
    [data-testid="stDivider"] hr,
    [data-testid="stMarkdownContainer"] hr {
        border: none;
        border-top: 1px solid var(--zy-line);
        margin: 1.75rem 0;
    }

    /* ============================================================
       11. Metric 卡片 —— 白卡圆角 / 柿橙大数字
       ============================================================ */
    [data-testid="stMetric"] {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        border-radius: 14px !important;
        box-shadow: var(--zy-shadow-sm) !important;
        padding: 1rem 1.25rem;
    }
    [data-testid="stMetricLabel"] {
        color: var(--zy-muted);
        font-size: 0.8125rem;
        font-weight: 500;
        letter-spacing: 0.06em;
    }
    [data-testid="stMetricValue"] {
        font-family: var(--zy-serif);
        color: var(--zy-accent);
        font-weight: 800;
        font-size: 1.75rem;
    }
    [data-testid="stMetricDelta"] {
        color: var(--zy-ink);
    }

    /* ============================================================
       12. 容器与表单细节
       ============================================================ */
    /* 只给「最外层」内容区留白。以前这里是 [data-testid="stVerticalBlock"] > div，
       它会给每一个元素容器（含消息气泡内层）都加上 2.25rem/3rem 的上下内边距，
       导致每条消息凭空多出 84px 高度、页面被撑高后自动滚到底，顶部内容被挤出视口。
       —— 2026-10-18 修复「点击开始面试后看不到开场白」。
       留白再收一档：面试中的整页高度只差几十像素就超一屏，超出的部分会被
       Streamlit 自动滚到底吃掉。但顶部必须留够 3.75rem —— Streamlit 的
       [data-testid="stHeader"] 是固定在顶部的透明浮层（约 60px），内容是从
       滚动容器的 y=0 开始画、从浮层底下穿过去的；padding-top 给少了，会话栏
       （知遇 · AI 虚拟面试官）就会被那层浮层压住，只剩下面一条分隔线，
       看着像"上面有一行遮挡"（2026-10-18 报障）。 */
    .block-container,
    [data-testid="stMainBlockContainer"],
    [data-testid="stMain"] > div:first-child {
        padding-top: 3.75rem;
        padding-bottom: 1.5rem;
    }
    [data-testid="stForm"] {
        border: 1px solid var(--zy-line);
        border-radius: 16px !important;
        box-shadow: var(--zy-shadow-sm) !important;
        padding: 1.5rem;
        background-color: var(--zy-surface);
    }
    [data-testid="stCaptionContainer"] {
        color: var(--zy-muted);
        font-size: 0.8125rem;
    }
    [data-testid="stWidgetLabel"] p {
        color: var(--zy-ink);
        font-weight: 600;
        font-size: 0.9375rem;
    }

    /* ============================================================
       13. 顶栏与页脚
       ============================================================ */
    [data-testid="stHeader"] {
        background-color: var(--zy-paper) !important;
        border-bottom: 1px solid var(--zy-line);
    }
    [data-testid="stDecoration"] {
        display: none;
    }
    footer {
        visibility: hidden;
    }

    /* ============================================================
       14. 进度条 —— 胶囊轨道 / 柿橙填充
       ============================================================ */
    [data-testid="stProgress"] > div > div > div {
        background-color: #EAE2D6 !important;
        border-radius: 999px !important;
        height: 8px;
    }
    [data-testid="stProgress"] > div > div > div > div {
        background-color: var(--zy-accent) !important;
        border-radius: 999px !important;
    }
    [data-testid="stProgress"] [data-testid="stMarkdownContainer"] p {
        color: var(--zy-muted);
        font-size: 0.875rem;
        white-space: pre-line;
    }

    /* ============================================================
       15. 聊天输入框
       ============================================================ */
    [data-testid="stChatInput"] {
        background-color: var(--zy-paper);
        border-top: 1px solid var(--zy-line);
        padding: 0.75rem 0 0.25rem;
    }
    [data-testid="stChatInput"] textarea {
        background-color: var(--zy-surface) !important;
        border: 1px solid var(--zy-line-strong) !important;
        border-radius: 14px !important;
        box-shadow: var(--zy-shadow-sm) !important;
        color: var(--zy-ink);
        caret-color: var(--zy-accent);
    }
    [data-testid="stChatInput"] textarea:focus {
        border-color: var(--zy-accent) !important;
        box-shadow: 0 0 0 3px rgba(194, 72, 27, 0.14) !important;
    }
    [data-testid="stChatInput"] button {
        color: var(--zy-accent) !important;
    }

    /* ============================================================
       16. 数据表格 / 音频 / 滚动条
       ============================================================ */
    [data-testid="stDataFrame"],
    [data-testid="stTable"] {
        border: 1px solid var(--zy-line);
        border-radius: 12px !important;
        background-color: var(--zy-surface);
        overflow: hidden;
    }
    [data-testid="stAudioInput"] {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line-strong);
        border-radius: 14px !important;
        padding: 0.5rem 0.75rem;
    }
    [data-testid="stAudio"] audio {
        width: 100%;
    }
    ::-webkit-scrollbar {
        width: 10px;
        height: 10px;
    }
    ::-webkit-scrollbar-track {
        background: transparent;
    }
    ::-webkit-scrollbar-thumb {
        background: var(--zy-line-strong);
        border-radius: 999px;
        border: 2px solid var(--zy-paper);
    }
    ::-webkit-scrollbar-thumb:hover {
        background: var(--zy-accent);
    }

    /* ============================================================
       17. 自定义组件 —— Hero / 特性网格 / 会话头部栏
       ============================================================ */
    .zy-hero {
        position: relative;
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        border-radius: 24px;
        box-shadow: var(--zy-shadow-md);
        padding: 2.75rem 2.75rem 2.625rem;
        margin: 0 0 1.375rem;
        overflow: hidden;
    }
    .zy-hero::after {
        content: "";
        position: absolute;
        right: -64px;
        top: -64px;
        width: 200px;
        height: 200px;
        border-radius: 50%;
        background: radial-gradient(circle, var(--zy-accent-soft) 0%, rgba(251, 234, 224, 0) 70%);
    }
    .zy-kicker {
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.2em;
        text-transform: uppercase;
        color: var(--zy-accent);
        margin: 0 0 1rem;
    }
    .zy-title {
        font-family: var(--zy-serif);
        font-size: 4rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        line-height: 1.08;
        color: var(--zy-ink);
        margin: 0;
    }
    .zy-title em {
        font-style: normal;
        color: var(--zy-accent);
    }
    .zy-sub {
        font-size: 1.0625rem;
        line-height: 1.75;
        color: var(--zy-muted);
        max-width: 46em;
        margin: 1.125rem 0 0;
    }
    .zy-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 1.125rem;
        margin: 0 0 1.375rem;
    }
    .zy-cell {
        background-color: var(--zy-surface);
        border: 1px solid var(--zy-line);
        border-radius: 18px;
        box-shadow: var(--zy-shadow-sm);
        padding: 1.375rem 1.5rem 1.5rem;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .zy-cell:hover {
        transform: translateY(-3px);
        box-shadow: var(--zy-shadow-md);
    }
    .zy-num {
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--zy-accent);
    }
    .zy-cell h4 {
        font-family: var(--zy-serif);
        margin: 0.5rem 0 0.375rem;
        font-size: 1.125rem;
        font-weight: 800;
        color: var(--zy-ink);
    }
    .zy-cell p {
        margin: 0;
        font-size: 0.9rem;
        line-height: 1.7;
        color: var(--zy-muted);
    }
    .zy-band {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
        flex-wrap: wrap;
        border-bottom: 1px solid var(--zy-line);
        padding-bottom: 0.75rem;
        margin-bottom: 0.75rem;
    }
    .zy-mark {
        font-family: var(--zy-serif);
        font-size: 1.625rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        line-height: 1.15;
        color: var(--zy-ink);
    }
    .zy-mark em {
        font-style: normal;
        color: var(--zy-accent);
    }
    .zy-meta {
        font-size: 0.875rem;
        letter-spacing: 0.03em;
        color: var(--zy-muted);
        text-align: right;
    }
    @media (max-width: 900px) {
        .zy-grid {
            grid-template-columns: 1fr;
        }
        .zy-hero {
            padding: 2rem 1.375rem;
        }
        .zy-title {
            font-size: 2.75rem;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# 记录落盘
#
# 读写与统计的实现在 history_store.py（与 Streamlit 解耦，可离线单测）。
# 这里保留薄封装，是因为界面代码读起来更顺，也因为下面几个调用点用惯了旧名字。
# ---------------------------------------------------------------------------


def load_records() -> list[dict]:
    return history_store.load_records(HISTORY_FILE)


def save_record(record: dict) -> None:
    history_store.append_record(HISTORY_FILE, record)


def recent_asked_topics(limit: int = 3) -> list[str]:
    """读取最近几场练习问过的考点，开新场时用来避开重复出题。

    历史文件缺失或损坏时返回空列表——去重只是锦上添花，绝不能因此挡住面试。
    """
    seen: list[str] = []
    for record in reversed(load_records()[-limit:]):
        for topic in record.get("asked_topics") or []:
            text = str(topic).strip()
            if text and text not in seen:
                seen.append(text)
    return seen


# ---------------------------------------------------------------------------
# 会话快照（断点续面）
#
# Streamlit 的 session_state 是纯内存态：浏览器一刷新，整场面试就没了。
# 这里把"进行中"的面试存成快照文件，刷新后可选择继续。
#
# 存储是**一场一个文件**（`.snapshots/<会话id>.json`，实现见 session_store.py）：
# 早先所有会话共用一个 `.session_snapshot.json`，开第二个标签页面试就会把
# 第一个标签页的快照顶掉。现在每个标签页写自己的文件，首页再从所有残留场次里
# 挑最近保存的那个来提示"继续上次面试"。旧版单文件仍会被扫到（只读兼容）。
#
# 明确不落盘的东西：
#   - 简历（resume / resume_chunks）：项目硬约束，简历只在内存里，绝不写磁盘
#   - 音频（audio_by_index / avatar_audio）：体积大且无意义，恢复后旧消息不再有声音
#   - API Key：一律从环境变量重读，密钥不进快照文件
# ---------------------------------------------------------------------------

# 需要写进快照的字段（其余字段要么能从环境重建，要么上面已说明不落盘）
_SNAPSHOT_FIELDS = (
    "cur_q",
    "probes_used",
    "reflects_in_a_row",
    "asked_topics",
    "mood_state",
    "mood_trace",
    "mood_choice",
    "voice_on",
    "voice_name",
    "avatar_on",
    "decisions",
    "history",
)


def _session_id() -> str:
    """本浏览器会话的稳定 id。

    Streamlit 每个标签页是一个独立 session，所以这个 id 天然做到"一个标签页一场"。
    存在 session_state 里，刷新（同一标签页）时由 Streamlit 保留；换标签页则不同。
    """
    ss = st.session_state
    if not ss.get(session_store.SESSION_KEY):
        ss[session_store.SESSION_KEY] = uuid.uuid4().hex[:12]
    return str(ss[session_store.SESSION_KEY])


def save_snapshot() -> None:
    """把"进行中的面试"写进**本会话自己的**快照文件。

    只在 status == running 时保存；写失败绝不能影响面试，静默跳过即可。
    """
    if st.session_state.get("status") != "running" or not st.session_state.get("profile"):
        return
    data = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": st.session_state.started_at.isoformat(timespec="seconds"),
        # 简历不落盘：profile 里剔除 resume / resume_chunks，
        # 另记一个"本场是否用过简历"的标记，恢复后好提示用户。
        "profile": {
            k: v
            for k, v in (st.session_state.profile or {}).items()
            if k not in ("resume", "resume_chunks")
        },
        "had_resume": bool(st.session_state.get("resume_text")),
    }
    for field in _SNAPSHOT_FIELDS:
        data[field] = st.session_state.get(field)
    try:
        session_store.write_snapshot(BASE_DIR, _session_id(), data)
    except (OSError, TypeError) as exc:  # noqa: BLE001
        print(f"[快照保存失败] {exc}", file=sys.stderr, flush=True)


def load_snapshot() -> dict | None:
    """读取最近一场未完成的面试（排除本会话自己）；没有则 None。"""
    return session_store.latest_snapshot(BASE_DIR, exclude_session=_session_id())


def clear_snapshot() -> None:
    """删掉本会话自己的快照（面试正常结束、重新开始或用户主动放弃时调用）。"""
    session_store.clear_snapshot(BASE_DIR, _session_id())


def restore_snapshot(data: dict) -> None:
    """把快照恢复进 session_state，并把界面切回"面试进行中"。"""
    ss = st.session_state
    # 接管这一场：内容搬进本会话，然后删掉源快照。
    # 不能直接沿用源会话 id——那个标签页可能还开着，两边同写一个文件会互相覆盖；
    # 删掉源文件则保证"一场面试 = 一个快照文件"，也不会留下孤儿。
    # source 为空说明它来自旧版单文件，同样要清掉，否则续面后横幅会再冒出来。
    source = str(data.get(session_store.SESSION_KEY) or "")
    session_store.clear_snapshot(
        BASE_DIR, source or session_store.LEGACY_SNAPSHOT_NAME
    )
    ss["profile"] = data["profile"]
    for field in _SNAPSHOT_FIELDS:
        if field in data:
            ss[field] = data[field]
    try:
        ss["started_at"] = datetime.fromisoformat(str(data.get("started_at") or ""))
    except ValueError:
        ss["started_at"] = datetime.now()
    # 音频与简历都不可能恢复：清空，避免残留上一场的内容
    ss["audio_by_index"] = {}
    ss["autoplay_index"] = -1
    ss["avatar_audio"] = b""
    ss["avatar_index"] = -1
    ss["voice_signature"] = ""
    ss["evaluation"] = {}
    ss["coach"] = {}
    # 建议与简历优化都不落盘，恢复出来的场次自然也没有它们。
    # 这里显式清空，避免残留上一场（或上一次简历优化）的结果与本场混在一起。
    ss["advice"] = {}
    ss["resume_advice"] = {}
    ss["error"] = ""
    ss["status"] = "running"
    # 原场次用过简历，但简历不落盘 → 恢复后针对性提问会退化，界面上要明确提示
    ss["resume_lost_note"] = bool(data.get("had_resume"))


def render_recovery_banner() -> None:
    """首页：发现未完成的面试快照时，给一个明确的"继续 / 重新开始"入口。

    刻意不做静默自动恢复：让用户自己选，避免他刚想开一场新面试却被旧场次顶掉。
    """
    data = load_snapshot()
    if not data:
        return
    profile = data.get("profile") or {}
    answered = len([m for m in (data.get("history") or []) if m.get("role") == "user"])
    st.warning(
        "检测到一场未完成的面试，可以继续。\n\n"
        f"- 场次：{profile_summary(profile)}\n"
        f"- 已作答：{answered} 道\n"
        f"- 快照时间：{data.get('saved_at', '')}\n\n"
        "简历不会被保存，恢复后本场将按你口述的背景继续提问。"
    )
    col1, col2 = st.columns(2)
    if col1.button("继续上次面试", type="primary", use_container_width=True):
        restore_snapshot(data)
        st.rerun()
    if col2.button("放弃并重新开始", use_container_width=True):
        # 要丢的是**横幅上那一场**（可能来自另一个标签页），不是本会话自己的。
        # 误删本会话快照会让正在进行的面试失去断点续面能力。
        stale = str(data.get(session_store.SESSION_KEY) or "")
        if stale:
            session_store.clear_snapshot(BASE_DIR, stale)
        st.rerun()


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


def init_state() -> None:
    defaults = {
        "profile": None,
        "history": [],
        "decisions": [],
        "status": "idle",  # idle / running / finished
        "cur_q": 0,  # 已经问出的主问题数量
        "probes_used": 0,  # 当前主问题已追问次数
        "reflects_in_a_row": 0,  # 已经连续做了几次"不提问回合"（复述确认 / 澄清 / 反馈）
        "asked_topics": [],  # 本场已经问过的主问题考点，用来避免同场重复出题
        # 面试官情绪：此刻的状态、整场的状态轨迹、开场时间（用来推算时间压力）
        "mood_state": mood.initial(),
        "mood_trace": [],
        "mood_choice": MOOD_ORDER[0],  # 侧边栏选的情绪模式（默认「随机」）
        "started_at": datetime.now(),
        "evaluation": {},
        "coach": {},
        # 提升建议（按评分维度给）+ 简历优化结果。
        # 两者都**不落盘**：建议含逐字稿引用，简历优化含简历原文改写，都只活在本会话内存。
        "advice": {},
        "resume_advice": {},
        "usage": {},  # 本场 token 计量（开局清零，报告生成后取值）
        # 清空历史记录需要两步确认（不可撤销），这个标记记住"已点过一次"
        "confirm_clear_history": False,
        # 本场记录落盘时写下的 time。报告页的"结局回填"靠它定位 history.json 里
        # 那一条记录（而不是靠下标——下标会因为删掉别的场次而整体前移）。
        "record_time": "",
        # API 连通性测试结果。存进 session_state，否则 rerun 一次就消失
        "conn_test": None,
        # 一次性提示：`st.success()` 紧跟 `st.rerun()` 时提示渲染不出来（rerun
        # 会立刻中断本次运行）。要"改完就重跑还要给反馈"的写操作先把话存这里，
        # 由下一轮开头 `_flush_flash()` 显示后清空。
        "flash": "",
        # 注意：`strict_mode` 刻意不在这里设默认值——它由侧边栏那个带 key 的
        # checkbox 自己管理。两边都设会让 Streamlit 报"默认值与 Session State 冲突"。
        "api_key": load_api_key(DEEPSEEK_KEY_ENV),
        "sf_key": load_api_key(SILICONFLOW_KEY_ENV),
        "error": "",
        # 简历只存在于内存：不落盘、不进历史记录，面试结束即清空
        "resume_text": "",
        "resume_name": "",
        "resume_error": "",
        "resume_signature": "",
        "resume_epoch": 0,
        # 从快照恢复时若原场次用过简历，这里置 True，界面上提示"简历无法恢复"
        "resume_lost_note": False,
        # 语音：音频单独存放，绝不进 history（否则落盘 json.dumps 会直接崩）
        "voice_on": True,
        "voice_name": DEFAULT_VOICE,
        "audio_by_index": {},  # 面试官消息下标 -> mp3 字节
        "autoplay_index": -1,  # 需要自动播放的消息下标
        "voice_signature": "",  # 已处理过的录音指纹，防止同一条录音被反复识别
        # 录音控件的实例代号：每消费掉一段录音就 +1，让控件以全新实例重挂载。
        # st.audio_input 复用同一个 key 时，录音结束后会残留一个已失效的
        # MediaRecorder，界面上显示 "An error has occurred, please try again."
        # （2026-10-18 报障：语音输入完就变那样，点控件左边的刷新按钮才好）。
        "voice_widget_gen": 0,
        # 面试官语音动效：开关 + 最新一条语音（组件专用，rerun 时保持不变，避免 iframe 重载打断播放）
        "avatar_on": True,
        "avatar_audio": b"",
        # 动效组件"占用"的那条消息下标：它自己会播这条语音，消息上就不能再挂
        # 一个 st.audio 播放器——同一段 mp3 两套播放器各自计时，永远不同步
        # （2026-10-18 报障：进度条走到 0:03，卡片却已经播完在等回答）。
        "avatar_index": -1,
        # 语音合成连续失败次数 + 停用说明。
        # edge-tts 自 2026-10 起对第三方客户端永久 403（voice.py 里已确认），
        # 兜底的硅基流动也可能返回空音频。每轮都白等好几秒，用户看到的
        # 就是"点了开始面试，页面半天没反应"。连续失败到阈值就停用本会话
        # 的语音合成（文字面试照常），侧边栏给出可一键恢复的说明。
        "tts_failures": 0,
        "tts_note": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def start_interview(profile: dict, api_key: str) -> None:
    """重置状态并让面试官开场。"""
    clear_snapshot()  # 开新场次即丢弃上一场残留的快照，避免首页又提示"继续上次面试"
    reset_usage()  # 本场 token 计量从零开始
    st.session_state.profile = profile
    st.session_state.api_key = api_key
    st.session_state.history = []
    st.session_state.decisions = []
    st.session_state.cur_q = 0
    st.session_state.probes_used = 0
    st.session_state.reflects_in_a_row = 0
    st.session_state.asked_topics = []
    st.session_state.resume_lost_note = False
    # 情绪状态在开局锁定：整场不再换模式，状态轨迹才连续，归因报告才有依据
    st.session_state.mood_state = mood.initial(**profile["mood_init"])
    st.session_state.mood_trace = []
    st.session_state.started_at = datetime.now()
    st.session_state.evaluation = {}
    st.session_state.coach = {}
    st.session_state.advice = {}
    st.session_state.resume_advice = {}
    st.session_state.error = ""
    st.session_state.audio_by_index = {}
    st.session_state.autoplay_index = -1
    st.session_state.voice_signature = ""
    st.session_state.voice_widget_gen += 1
    st.session_state.avatar_audio = b""
    st.session_state.avatar_index = -1
    st.session_state.status = "running"
    advance()


# 语音合成失败到这个次数就停用本会话的语音。
# 阈值取 1：`voice.synthesize()` 只有在**两条通道都失败**时才抛异常
# （edge-tts 已被上游 403 封禁、硅基流动超时或返回坏音频），这已经不是网络抖动，
# 而是本机确实没有可用的合成通道。此前是 2，代价是头两轮各白等一次超时——
# 按 v17 的 25 秒 TTS 超时算，两轮就是 50 秒的冷冻，用户只会觉得页面卡死。
TTS_FAILURE_LIMIT = 1


def _note_tts_failure(reason: str) -> None:
    """记一次语音合成失败；达到阈值后停用本会话语音并留下可恢复的说明。"""
    st.session_state.tts_failures = int(st.session_state.get("tts_failures") or 0) + 1
    if st.session_state.tts_failures < TTS_FAILURE_LIMIT:
        return
    if st.session_state.get("tts_note"):
        return
    st.session_state.tts_note = (
        "语音合成失败，已自动停用本场语音，文字面试照常进行（不影响出题与评分）。\n\n"
        "已知原因：edge-tts 自 2026-10 起被上游拒绝握手（WSS 403），这条通道"
        "只能靠硅基流动兜底；若兜底也没返回音频，本机当前就没有可用的合成通道。\n\n"
        f"失败原因：{reason[:300]}\n\n"
        "处理后可在左侧「🎙️ 语音与动效」点「重试语音合成」恢复，不必重开页面。"
    )


def speak(text: str) -> None:
    """把面试官的话合成语音，挂到刚写入的那条消息上等待播放。

    合成失败绝不能影响面试：宁可没声音，也不能让整场面试报错中断。
    """
    if not (st.session_state.voice_on and text.strip()):
        return
    # 已经确认本机合成不可用：不再每轮都在这里白等好几秒。
    if st.session_state.get("tts_note"):
        return
    try:
        level = (st.session_state.profile or {}).get("level", "B")
        current_mood = mood.mood_of(st.session_state.mood_state)
        audio = synthesize(
            text,
            st.session_state.voice_name,
            level,
            current_mood,
            fallback_key=siliconflow_key(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[TTS 失败] {exc}", file=sys.stderr, flush=True)
        _note_tts_failure(f"{type(exc).__name__}: {exc}")
        return
    # 合成可能"成功"返回空流（例如整段都是 markdown、清完就空了），
    # 这时不能把 b"" 挂进 audio_by_index：界面上的 `if audio:` 会静默跳过，
    # 用户只是听不到声音，也看不出是合成失败还是文本被清空。
    if not is_valid_audio(audio):
        print("[TTS 跳过] 合成结果为空或非 MP3 数据", file=sys.stderr, flush=True)
        _note_tts_failure("合成结果为空或非 MP3 数据")
        return
    st.session_state.tts_failures = 0
    index = len(st.session_state.history) - 1
    st.session_state.audio_by_index[index] = audio
    st.session_state.autoplay_index = index
    # 声波球组件只认"最新一条"：内容变化 = 有新语音 = 该它开口
    st.session_state.avatar_audio = audio
    st.session_state.avatar_index = index


def advance() -> None:
    """让面试官走一轮，并按决策更新计数器。"""
    profile = st.session_state.profile

    # 先推进情绪状态：依据是「候选人刚给的回答」+「面试官上一轮的决策」。
    # 放在调用模型之前，本轮的口播语气与追问上限才是这份状态的产物。
    last_answer, last_decision = "", {}
    for msg in reversed(st.session_state.history):
        if msg["role"] == "user" and not last_answer:
            last_answer = msg.get("content", "")
        elif msg["role"] == "assistant" and not last_decision:
            last_decision = msg.get("decision") or {}
        if last_answer and last_decision:
            break

    elapsed_min = (datetime.now() - st.session_state.started_at).total_seconds() / 60
    # 先备份情绪状态：模型调用失败时要把这一步退回去。mood.update 是确定性的，
    # 失败后若不回滚，重试就会把同一轮的情绪变化重复叠加一次。
    mood_before = dict(st.session_state.mood_state)
    st.session_state.mood_state = mood.update(
        st.session_state.mood_state,
        answer=last_answer,
        decision=last_decision,
        elapsed_min=elapsed_min,
        planned_rounds=int(profile.get("total_rounds", 5)),
        cur_q=st.session_state.cur_q,
    )

    try:
        # 这一轮要等模型 2–4 秒；没有进度提示时页面看起来就是"点了没反应"。
        with st.spinner("面试官正在看你的回答…"):
            data = interviewer_turn(
                st.session_state.history,
                profile,
                st.session_state.api_key,
                cur_q=st.session_state.cur_q,
                probes_used=st.session_state.probes_used,
                reflects_in_a_row=st.session_state.reflects_in_a_row,
                mood_state=st.session_state.mood_state,
                asked_topics=st.session_state.asked_topics,
            )
    except Exception as exc:  # noqa: BLE001
        # 回滚情绪状态，并保留候选人这条回答：界面据此给出「重试本轮」入口，
        # 用户不必重新输入（重新输入会留下两条连续的候选人消息）。
        st.session_state.mood_state = mood_before
        st.session_state.error = f"调用模型失败：{exc}"
        return

    # 本轮成功：清掉上一轮可能残留的错误横幅，否则它会一直挂在页面上。
    st.session_state.error = ""
    action = data["action"]

    # 计数权在代码手里：先按决策推进状态，再记录。
    # 具体规则抽在 interview_state.advance_counters（纯函数，可离线单测）。
    (
        st.session_state.cur_q,
        st.session_state.probes_used,
        st.session_state.reflects_in_a_row,
    ) = interview_state.advance_counters(
        st.session_state.cur_q,
        st.session_state.probes_used,
        st.session_state.reflects_in_a_row,
        action,
    )

    # 记录本轮考点用于同场去重：ask 落一个新考点，probe / reflect 沿用当前考点。
    # 规整后的考点由 decision_row 自行读取 data["topic"]，这里只要新列表。
    st.session_state.asked_topics, _ = interview_state.next_asked_topics(
        st.session_state.asked_topics, data.get("topic")
    )

    st.session_state.history.append(
        {"role": "assistant", "content": data["reply"], "decision": data}
    )
    # TTS 是同步阻塞的，合成一幕可能要好几秒；只在真的要合成时才挂进度提示。
    if st.session_state.voice_on and not st.session_state.get("tts_note"):
        with st.spinner("面试官正在开口…"):
            speak(data["reply"])
    else:
        speak(data["reply"])
    st.session_state.decisions.append(
        interview_state.decision_row(st.session_state.cur_q, data, ACTION_LABELS)
    )

    # 状态轨迹：这一轮面试官是带着什么状态说话的。评估官与教练靠它做归因。
    state = st.session_state.mood_state
    st.session_state.mood_trace.append(
        interview_state.mood_trace_row(
            st.session_state.cur_q, state, mood.mood_of(state)
        )
    )

    if action == "end":
        finish_interview()
    else:
        # 每轮问答后存一次快照：刷新浏览器就能从上一步接着面
        save_snapshot()


def clear_resume() -> None:
    """丢弃内存中的简历，并把上传控件重置为空。

    上传播件自身持有一份状态，只清 session_state 不足以让它变空，
    所以递增 resume_epoch 去换一个新的控件 key，强制重建。
    """
    st.session_state.resume_text = ""
    st.session_state.resume_name = ""
    st.session_state.resume_error = ""
    st.session_state.resume_signature = ""
    st.session_state.resume_epoch += 1


def sync_resume(uploaded) -> None:
    """上传控件有变化时解析简历；控件被清空时同步清掉内存里的简历。"""
    if uploaded is None:
        if st.session_state.resume_name or st.session_state.resume_text:
            clear_resume()
        return

    signature = f"{uploaded.name}:{uploaded.size}"
    if signature == st.session_state.resume_signature:
        return  # 同一个文件，不必每次页面刷新都重新解析

    text, message = extract_text(uploaded.name, uploaded.getvalue())
    st.session_state.resume_signature = signature
    st.session_state.resume_error = message
    st.session_state.resume_text = text
    st.session_state.resume_name = uploaded.name if text else ""


def finish_interview() -> None:
    """面试结束：依次调用评估官和教练，并落盘。"""
    st.session_state.status = "finished"
    history = st.session_state.history
    profile = st.session_state.profile

    try:
        # 至少要有几轮真实问答，否则报告没有意义
        if len([m for m in history if m["role"] == "user"]) < 2:
            st.session_state.error = "有效回答太少，本次不生成报告。"
            return

        try:
            trace = st.session_state.mood_trace
            # 把**本场实际问到**的考点交给评估官：不传的话它会拿到该难度档的
            # 全部 15~17 条参照，把没问过的判成「遗漏」，教练再据此给出
            # "去补某个从没问到的考点"的错误建议。
            evaluation = evaluate_interview(
                history,
                profile,
                st.session_state.api_key,
                mood_trace=trace,
                asked_topics=st.session_state.asked_topics,
            )
            coach = coach_review(
                history, evaluation, profile, st.session_state.api_key, mood_trace=trace
            )
            # 建议官排在教练之后：它要读教练没覆盖的那一层（按维度补什么）。
            # 但它是**可选**的——失败只记一笔降级说明，绝不让整份报告生成失败：
            # 评估与教练已经产出，因为多要一份建议而丢掉整份报告是本末倒置。
            try:
                advisor = advisor_review(
                    history, evaluation, profile, st.session_state.api_key
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[建议官调用失败，报告缺该栏] {exc}", file=sys.stderr, flush=True)
                advisor = {}
        except Exception as exc:  # noqa: BLE001
            st.session_state.error = f"生成报告失败：{exc}"
            return

        st.session_state.evaluation = evaluation
        st.session_state.coach = coach
        st.session_state.advice = advisor
        # 报告生成完再取一次计量，这样本场消耗把评估官与教练的调用也算进去
        st.session_state.usage = usage_snapshot()

        # 记住这场记录的落盘时间：报告页的"结局回填"靠它定位这一条记录
        record_time = datetime.now().isoformat(timespec="seconds")
        st.session_state.record_time = record_time
        save_record(
            {
                "time": record_time,
                # 落盘时剔除简历及其切片索引：简历不进入任何持久化文件
                "profile": {
                    k: v
                    for k, v in profile.items()
                    if k not in ("resume", "resume_chunks")
                },
                "overall_score": evaluation.get("overall_score"),
                "adjusted_score": evaluation.get("adjusted_score"),
                "level": evaluation.get("level"),
                "transcript": history,
                "decisions": st.session_state.decisions,
                "asked_topics": st.session_state.asked_topics,
                "mood_trace": trace,
                "usage": st.session_state.usage,
                "evaluation": evaluation,
                "coach": coach,
                # 「提升建议」刻意不落盘：它的每条建议都引用候选人原话，属于个人化
                # 内容（与简历同级）。留存只在**用户主动导出报告**时发生，
                # 与 README 声明的"简历从不写入磁盘"保持一致口径。
                # 想在这里补一行落盘时，必须同步修改 README 的隐私声明。
            }
        )
    finally:
        # 面试一结束就丢弃简历，无论报告是否生成成功
        clear_resume()
        # 同时清掉快照：这场已经收尾，首页不该再提示"继续上次面试"
        clear_snapshot()


# ---------------------------------------------------------------------------
# 界面 · 侧边栏配置
# ---------------------------------------------------------------------------


def _safe_index(options: list, value) -> int:
    """在 options 里找 value 的下标，找不到就返回 0。

    直接 .index() 会抛 ValueError —— 而这里的取值可能来自**快照恢复**
    （跨版本、跨配置），或者来自用户改过的环境变量。侧边栏因为一个过期取值
    整个崩掉是不可接受的，所以一律退化为第一项。
    """
    try:
        return options.index(value)
    except ValueError:
        return 0


def _render_api_key_section() -> str:
    """DeepSeek API Key：环境变量优先，否则让用户手填。返回当前可用的 key。

    判断"是否来自环境变量"用的是**值比对**而不是"字段非空"：
    `start_interview` 会把生效的 key 写进 `st.session_state.api_key`，
    若只看字段非空，手填的 key 在开局后会被误报成"已从环境变量读取"。
    """
    env_key = load_api_key(DEEPSEEK_KEY_ENV)
    if env_key:
        st.caption("🔑 DeepSeek API Key 已从环境变量读取")
        return env_key

    typed = st.text_input(
        "DeepSeek API Key",
        value="",
        type="password",
        key="api_key_input",
        help="在 platform.deepseek.com 申请，形如 sk-xxxx",
    )
    return (typed or "").strip()


def _render_core_config() -> dict:
    """三级自适应（场景 × 专业 × 难度）—— 本项目的核心配置，默认展开。"""
    scene = st.selectbox("① 面试场景", SCENES, index=1)
    major = st.selectbox("② 专业方向", MAJORS, index=0)

    labels = level_labels(scene)
    level = st.selectbox(
        "③ 难度等级",
        LEVELS,
        index=1,
        format_func=lambda code: f"{code} 级 · {labels[code]}",
    )

    interview_type = st.radio("面试类型", ["技术面", "行为面"], horizontal=True)
    total_rounds = st.slider("主问题数量", 3, 10, 5)

    # 难度档决定的是三档题的配比，不是「整场只出这一档」：
    # 这一行把配比摆出来，用户才能看出 A 档不是「全是高难题」。
    if interview_type == "技术面":
        mix = topic_mix_quotas(level, total_rounds)
        parts = [
            f"{TIER_LABELS[tier]} {mix[tier]} 道"
            for tier in LEVELS
            if mix.get(tier, 0) > 0
        ]
        st.caption(
            f"这 {total_rounds} 道主问题的难度配比：{'、'.join(parts)}。"
            "难度档调的是三档题的权重，不是整场只问同一档。"
        )
    else:
        st.caption("行为面按 STAR 结构出题，难度只影响追问的深度，不改考点。")

    return {
        "scene": scene,
        "major": major,
        "level": level,
        "interview_type": interview_type,
        "total_rounds": total_rounds,
    }


def _render_mood_section(strict: bool) -> bool:
    """附加轴 · 面试官情绪。返回是否满足"可以开始"的知情同意条件。

    刻意不编号：DESIGN 里情绪是**与难度正交的第二条轴**，
    编成"④"会暗示它是三级自适应的下一步，与设计不符。
    """
    st.markdown("**附加轴 · 面试官情绪**")

    if strict:
        # 严格模式锁定友善型。这里直接把控件禁用，而不是"能选但不生效"——
        # 后者会让人以为自己选的模式被采纳了。
        st.selectbox(
            "情绪模式",
            ["友善型（严格模式锁定）"],
            index=0,
            disabled=True,
            label_visibility="collapsed",
            help="严格模式要求每场节奏一致；情绪漂移会改变追问上限与措辞，两者目标冲突，所以锁定。",
        )
        st.caption("严格模式已锁定「友善型」——保证每场节奏一致，情绪不随机也不漂移。")
        return True

    st.session_state.mood_choice = st.selectbox(
        "情绪模式",
        MOOD_ORDER,
        index=_safe_index(MOOD_ORDER, st.session_state.mood_choice),
        label_visibility="collapsed",
        help="与难度正交：难度决定「问什么」，情绪决定「用什么态度问」。"
        "面试官的情绪还会随你的回答实时变化，面试结束后在报告里揭晓。",
    )

    if st.session_state.mood_choice == "随机":
        st.caption(
            "「随机」会在五种模式里抽取，其中包含疲惫型、怀疑型、冷淡型、压力型等高强度模式。"
            "如果只想练正常发挥，请显式选择「友善型」。"
        )

    if not MOOD_MODES[st.session_state.mood_choice]["confirm"]:
        return True

    agreed = st.checkbox(
        "我了解这是为训练目的设计的模拟状态，不是对真实面试官的平均描述",
        value=False,
        help="这类模式会刻意表现得没耐心、少反馈甚至挑刺，用来练高压下的稳定性。"
        "面试过程中可以随时中止。",
    )
    if not agreed:
        st.caption("勾选后即可开始面试。")
    return agreed


def _render_real_questions_section() -> tuple[bool, int]:
    """真题扩展：开关 + 配额，附覆盖范围说明。"""
    use_real = st.checkbox(
        "真题扩展（取材真实企业面试题）",
        value=True,
        help="从开源的真实企业面试题库里挑选素材，每场最多挑几道作为主问题，"
        "让面试更接近真实企业面试；其余主问题仍按上面的考点体系出题。",
    )
    real_quota = st.slider(
        "真题配额（每场最多几道）",
        0,
        3,
        2,
        disabled=not use_real,
        help="设为 0 即完全不使用真题。录演示视频、或想让每场节奏完全一致时，建议设为 0 或关掉。",
    )
    st.caption(
        "覆盖范围：技术面仅「计算机科学与技术 / 软件工程」有可信来源；"
        "人工智能与电子信息 / 自动化 / 通信工程暂无技术面真题，会自动跳过、不影响面试；"
        "行为面真题不限专业。"
    )
    return use_real, real_quota


def _render_candidate_section() -> str:
    """候选人背景 + 简历上传。返回拼好的背景文本。"""
    st.caption("填了之后面试官会围绕你的真实经历提问；不传简历也能问出针对性。")
    target_role = st.text_input(
        "目标岗位",
        placeholder="例：后端开发实习生 / 算法工程师",
        key="bg_role",
    )
    bg_project = st.text_area(
        "项目 / 实习经历",
        height=80,
        placeholder="例：做过校园二手交易小程序，SpringBoot + MySQL，我负责订单与支付模块。",
        key="bg_project",
    )
    bg_skills = st.text_input(
        "技能栈",
        placeholder="例：Java / SpringBoot / MySQL / Redis / Git",
        key="bg_skills",
    )

    parts = []
    if target_role.strip():
        parts.append(f"目标岗位：{target_role.strip()}")
    if bg_project.strip():
        parts.append(f"项目/实习经历：{bg_project.strip()}")
    if bg_skills.strip():
        parts.append(f"技能栈：{bg_skills.strip()}")

    uploaded = st.file_uploader(
        "上传简历（可选）",
        type=ACCEPTED_TYPES,
        key=f"resume_upload_{st.session_state.resume_epoch}",
        help="上传后面试官会围绕你的实习经历、毕业设计、获奖情况等具体条目提问；不传也能正常面试。",
    )
    sync_resume(uploaded)

    if st.session_state.resume_name:
        st.success(f"已读取简历：{st.session_state.resume_name}")
        if st.button("清除简历"):
            clear_resume()
            st.rerun()
    elif st.session_state.resume_error:
        st.warning(st.session_state.resume_error)

    st.caption(
        "简历只在本次会话的内存里暂存，不写入磁盘、不进入历史记录，面试结束后立即清除。"
        "面试中会按当前话题检索相关片段发给 DeepSeek 生成针对性问题；"
        "结束后评估官还会用简历原文核对你的自述。"
    )
    return "\n".join(parts)


def _render_resume_optimize_section(api_key: str) -> None:
    """简历优化入口：独立于面试，上传简历就能用。

    刻意与「候选人背景与简历」分开：那一组是为了**面试**取材（上传后面试官会
    围绕它提问），这一组是为了**改简历**。两者的目标岗位可以不同——用户可能
    想改一份投算法岗的简历，同时不想为它开一场面试。
    """
    st.caption("独立功能：改简历不需要先开一场面试。结果只留在本页内存，不写入历史记录。")

    uploaded = st.file_uploader(
        "在这里上传要优化的简历",
        type=ACCEPTED_TYPES,
        key="optimize_upload",
        help="支持 PDF / Word / TXT。解析后只在内存中处理，不落盘。",
    )

    if uploaded is not None:
        signature = f"opt:{uploaded.name}:{uploaded.size}"
        if signature != st.session_state.get("optimize_signature"):
            text, message = extract_text(uploaded.name, uploaded.getvalue())
            st.session_state.optimize_signature = signature
            st.session_state.optimize_text = text
            st.session_state.optimize_name = uploaded.name if text else ""
            st.session_state.optimize_error = message
            # 换了简历，上一份的结果作废——否则会显示"新简历 + 旧建议"
            st.session_state.resume_advice = {}
    elif st.session_state.get("optimize_name"):
        # 控件被清空：同步丢掉内存里的简历与原建议，与 sync_resume 同款处理
        st.session_state.optimize_text = ""
        st.session_state.optimize_name = ""
        st.session_state.optimize_error = ""
        st.session_state.optimize_signature = ""
        st.session_state.resume_advice = {}

    if st.session_state.get("optimize_name"):
        st.success(f"已读取：{st.session_state.optimize_name}")
    elif st.session_state.get("optimize_error"):
        st.warning(st.session_state.optimize_error)

    st.text_input(
        "目标岗位（用于匹配度建议）",
        key="ro_target_role",
        placeholder="例：后端开发实习生 / 算法工程师",
        help="留空也能生成，只是建议不会针对某个具体岗位。",
    )

    ready = bool(st.session_state.get("optimize_text")) and bool(api_key)
    if st.button("生成优化建议", disabled=not ready, use_container_width=True):
        with st.spinner("正在诊断简历…"):
            try:
                st.session_state.resume_advice = resume_review(
                    st.session_state.optimize_text,
                    api_key,
                    target_role=st.session_state.ro_target_role,
                    major=str((st.session_state.profile or {}).get("major") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                st.session_state.error = f"简历优化失败：{exc}"
        st.rerun()

    if not api_key:
        st.caption("填入 DeepSeek API Key 后即可生成。")

    result = st.session_state.get("resume_advice") or {}
    if result:
        st.divider()
        if result.get("score") is not None:
            st.metric("简历评分", result["score"])
        if result.get("verdict"):
            st.info(result["verdict"])

        problems = result.get("problems") or []
        if problems:
            st.markdown("**诊断**")
            for item in problems:
                icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(
                    item.get("severity", ""), "•"
                )
                st.markdown(f"{icon} **{item.get('problem', '')}**")
                if item.get("evidence"):
                    st.caption(f"原文：{item['evidence']}")
                st.caption(f"怎么改：{item.get('advice', '')}")

        rewrites = result.get("rewrites") or []
        if rewrites:
            st.markdown("**改写对照**")
            for i, item in enumerate(rewrites, start=1):
                with st.expander(f"{i}. {item.get('original', '')[:24]}…"):
                    st.markdown(f"**原句**：{item.get('original', '')}")
                    st.success(f"**改写**：{item.get('improved', '')}")
                    st.caption(f"为什么：{item.get('why', '')}")

        if result.get("missing"):
            st.markdown("**还需要补的内容**")
            for item in result["missing"]:
                st.markdown(f"- {item}")
        if result.get("keywords"):
            st.markdown("**关键词建议**")
            st.caption("只列你真实掌握的方向；不建议堆砌不熟悉的名词。")
            st.markdown(" ".join(f"`{k}`" for k in result["keywords"]))
        if result.get("checklist"):
            st.markdown("**投简历前自查**")
            for item in result["checklist"]:
                st.markdown(f"- [ ] {item}")

        # 如实标注两道本地核对的结果，让人知道"没编造"是被验证过的。
        # 两种被剔除的原因分开说：编造原句 与 凭空补数字 是不同性质的问题。
        dropped = int(result.get("dropped_rewrites") or 0)
        invented = int(result.get("invented_numbers") or 0)
        ungrounded = int(result.get("ungrounded_evidence") or 0)
        if dropped or ungrounded:
            st.warning(
                f"本地核对已剔除 {dropped} 条改写"
                f"（其中 {invented} 条是改写里补了原文没有的数字）、"
                f"{ungrounded} 条诊断引用（对不上原文）——避免出现你没写过的内容。"
            )

        quant = result.get("quantification") or {}
        if quant.get("lines"):
            st.caption(
                f"本地统计：{quant['lines']} 行里 {quant['quantified']} 行含量化结果"
                f"（{quant['ratio']:.0%}）。"
            )

        identity = result.get("identity") or []
        if identity:
            st.markdown("**身份与表述核实**")
            for item in identity:
                icon = "🔴" if item.get("severity") == "high" else "🟠"
                st.markdown(f"{icon} {item.get('signal', '')}")
                st.caption(f"原文：{item.get('quote', '')}")
                st.caption(f"怎么改：{item.get('advice', '')}")

        formatting = result.get("formatting") or {}
        if formatting.get("issues"):
            st.markdown("**格式检查（本地）**")
            for issue in formatting["issues"]:
                st.markdown(f"- {issue}")
        _, btn_col = st.columns([3, 1])
        btn_col.download_button(
            "导出优化建议",
            data=build_resume_advice_markdown(),
            file_name=f"简历优化建议-{datetime.now().strftime('%Y%m%d-%H%M')}.md",
            mime="text/markdown",
            use_container_width=True,
        )


def _jd_bridge_caption(result: dict) -> None:
    """库外技术词在其他专业库里有考点时给一条对照提示，不算本专业命中。"""
    bridges = result.get("bridges") or []
    if not bridges:
        return
    text = "；".join(
        f"{item.get('token', '')} → {item.get('major', '')}「{item.get('topic', '')}」"
        for item in bridges
    )
    st.caption("这些说法在其他专业库里有相关考点（仅供对照，不算本专业命中）：" + text)


def _start_jd_interview(result: dict) -> None:
    """按 JD 命中的考点直接开一场技术面。

    做法：先用左侧「基础配置」的场景/难度/主问题数编出常规 profile，再把
    profile["topics"] 换成 JD 命中的考点——面试官提示词里的「考点参考」就是它，
    所以本场主问题只从这几条里出，评分仍按（专业, 考点原文）取参照，问评同源。

    刻意不带简历：有简历时面试官会被要求「主问题优先针对简历条目」，与
    「只围绕 JD 考点」直接冲突；定向练习要的就是这份聚焦。
    情绪固定友善型：一键开场不能替用户勾「随机/高强度模式」的知情同意。
    真题支线关闭：真题按专业/档位抽，可能抽到与这份 JD 无关的题，把定向题挤掉。
    """
    api_key = st.session_state.get("api_key") or ""
    if not api_key:
        return
    core = st.session_state.get("core_config") or {}
    profile = build_profile(
        scene=core.get("scene") or SCENES[1],
        major=result.get("major") or MAJORS[0],
        level=core.get("level") or "B",
        interview_type="技术面",
        total_rounds=int(core.get("total_rounds") or 5),
        resume="",
        strict=False,
        mood=MOOD_FRIENDLY,
        avoid_topics=recent_asked_topics(),
        use_real=False,
    )
    profile["deep_dive"] = []
    profile["topics"] = [
        str(item.get("topic", "")).strip()
        for item in (result.get("topics") or [])
        if str(item.get("topic", "")).strip()
    ]
    profile["jd_topics"] = list(profile["topics"])
    # 本场考点是 JD 命中的原文，不是按难度配比抽的：档位与配比都要清掉，
    # 否则提示词会给出「高难 3 道、中等 2 道」这种与本场考点对不上的排题指令。
    profile["topic_tiers"] = {}
    profile["topic_mix"] = {}
    start_interview(profile, api_key)
    st.rerun()


def _jd_progress(profile: dict, rubric_check: list[dict]) -> dict:
    """本场是 JD 定向场时，把 rubric_check 对回本场目标考点；否则返回空字典。"""
    jd_topics = [str(t).strip() for t in (profile.get("jd_topics") or []) if str(t).strip()]
    if not jd_topics:
        return {}
    return jd_match.session_progress(jd_topics, rubric_check, profile.get("major", ""))


def _tier_distribution_caption(profile: dict, rubric_check: list[dict]) -> str:
    """本场**实际问到**的考点在 A/B/C 三档上怎么分布，拼成一行 markdown。

    为什么不回读 profile["topic_tiers"]：那是抽池时的构成，不等于这场真问了什么。
    用问过的考点现算，才能回答「这场到底考了几道高难题」。
    行为面 / JD 定向场 / 查不到档位时不输出（不编一个「未知」出来凑数）。
    """
    asked = [
        str(row.get("topic", "")).strip()
        for row in (rubric_check or [])
        if str(row.get("topic", "")).strip()
    ]
    if not asked:
        return ""
    major = str(profile.get("major", ""))
    tiers = [topic_tier(major, topic) for topic in asked]
    if not any(tiers):
        return ""
    counts = Counter(tier for tier in tiers if tier)
    parts = [
        f"{TIER_LABELS[tier]} {counts[tier]} 道"
        for tier in LEVELS
        if counts.get(tier, 0) > 0
    ]
    unknown = len([tier for tier in tiers if not tier])
    line = f"**本场实际档位分布**：{'、'.join(parts)}"
    if unknown:
        line += f"（另有 {unknown} 条考点不在档位表里，未计入）"
    return line + "。"


def _render_rubric_check_block(rubric_check: list[dict], asked_topics: list[str]) -> dict:
    """渲染报告页的「考点级判定」，并按判定依据的来源分区。

    为什么要分区：库里 318 条参照是人工逐条核对过的，评估官为自拟题现场
    现拟的要点不是。两类结论放在同一张表里、用同一个「通过 / 不通过」的说法
    呈现，等于把现拟的结论冒充成核对过的结论——答辩时被问「这条判定依据是
    哪来的」就答不上来。所以分两段显示，并如实写出各几条。

    返回 aggregate 结果，供调用方继续用（JD 收尾块另外算，不复用）。
    """
    profile = st.session_state.profile or {}
    agg = aggregate_rubric_check(profile, rubric_check)
    labels = {t["topic"]: t["label"] for t in agg["topics"]}
    sources = {t["topic"]: t["source"] for t in agg["topics"]}
    counts = agg["counts"]
    st.markdown(
        f"**考点级判定**：共 {agg['total']} 个考点，通过 {agg['passed']} 个，"
        f"部分通过 {counts[VERDICT_PARTIAL]} 个，不通过 {counts[VERDICT_FAIL]} 个，"
        f"本场未问到 {counts[VERDICT_UNKNOWN]} 个。"
    )
    if agg["generated"]:
        st.caption(
            f"其中 {agg['library']} 条的判定依据来自自建考点库（人工核对过），"
            f"{agg['generated']} 条是评委按题面现场现拟的要点（仅供参考）。"
        )

    # 问到了、却连一行判定都没回来的考点——用户有权知道漏了哪几道。
    judged = {str(item.get("topic", "")).strip() for item in rubric_check}
    missing = [topic for topic in asked_topics if topic not in judged]
    if missing:
        st.caption("以下几个考点本场没拿到判定：" + "、".join(missing))

    if agg["weak"]:
        for entry in agg["weak"]:
            tag = "" if entry["source"] == "library" else "（现拟）"
            st.markdown(f"- {entry['label']}｜{entry['topic']}{tag}：{entry['reason']}")
    tier_line = _tier_distribution_caption(profile, rubric_check)
    if tier_line:
        st.markdown(tier_line)
    with st.expander("考点命中情况（评委判分时对照的要点清单）"):
        st.caption(
            "参照是底线不是天花板：清单之外的正确答案同样算分，没命中不等于答错。"
            "「来源」一列写明这条判定是查库得来的，还是评委现场现拟的。"
        )
        st.dataframe(
            [
                {
                    "考点": item.get("topic", ""),
                    "来源": "考点库" if sources.get(item.get("topic", "")) == "library" else "现拟",
                    "判定": labels.get(item.get("topic", ""), ""),
                    "已命中": "；".join(str(x) for x in (item.get("must_hit") or [])),
                    "遗漏": "；".join(str(x) for x in (item.get("missed") or [])),
                    "误区": "；".join(str(x) for x in (item.get("errors") or [])),
                    "说明": item.get("comment", ""),
                }
                for item in rubric_check
            ],
            hide_index=True,
        )
    return agg


def _render_jd_progress_block(profile: dict, rubric_check: list[dict]) -> None:
    """JD 定向场的收尾：本场目标考点逐条判定 + 「只练没过的」下一跳。

    这一步把单向的「JD → 面试」补成闭环：练完一场立刻能看到哪几条还没过，
    并且可以直接把没过的那几条设成下一场的目标。判定与报告页考点级判定同源
    （knowledge.topic_verdict，含 veto），不再叫模型判一次。
    """
    progress = _jd_progress(profile, rubric_check)
    if not progress.get("total"):
        return
    counts = progress["counts"]
    st.subheader("JD 定向场收尾")
    st.markdown(
        f"本场目标 {progress['total']} 条：通过 {progress['passed']} 条，"
        f"部分通过 {counts[VERDICT_PARTIAL]} 条，不通过 {counts[VERDICT_FAIL]} 条，"
        f"本场未问到 {counts[VERDICT_UNKNOWN]} 条。"
    )
    st.dataframe(
        [
            {"考点": item["topic"], "判定": item["label"], "原因": item["reason"]}
            for item in progress["topics"]
        ],
        hide_index=True,
    )

    remaining = progress["remaining"]
    if not remaining:
        st.success(
            "本场目标考点都过了。回到左侧「岗位 JD 对照」重新分析一次，练习情况会跟着更新。"
        )
        return
    unasked = counts[VERDICT_UNKNOWN]
    if unasked:
        note = f"（其中 {unasked} 条本场没问到，也算还没证明）"
    else:
        note = ""
    st.caption(
        f"还剩 {len(remaining)} 条没过或没考到{note}。点下面的按钮，把它们设成下一场的目标"
        "（专业用本场的，场景、难度与主问题数沿用左侧「基础配置」）。"
    )
    api_key_ready = bool(st.session_state.get("api_key"))
    if st.button(
        f"🎯 只练这 {len(remaining)} 条",
        key="jd_retry_weak",
        disabled=not api_key_ready,
        use_container_width=True,
    ):
        with st.spinner("正在为你开场…"):
            _start_jd_interview(
                {
                    "major": profile.get("major", ""),
                    "topics": [{"topic": topic} for topic in remaining],
                }
            )
    if not api_key_ready:
        st.caption("填入 API Key 后即可从这里直接开场。")


def _render_jd_match_section() -> None:
    """岗位 JD 对照：把招聘要求换算成考点与能力维度（纯本地，不调用模型）。

    与「简历优化」并列、也独立于面试，只回答两件事：这个岗位大概率会问哪些
    考点、这些考点你已经练过几条。只读历史记录，不写回任何东西。
    """
    st.caption(
        "独立功能：把招聘要求对照到考点库，看看这个岗位会问什么、你已经练过几条。"
        "纯本地匹配，不调用模型。"
    )

    st.selectbox(
        "目标专业（决定查哪套考点库）",
        MAJORS,
        key="jd_major",
        help="与「基础配置」里的面试专业互不影响，只决定这次对照查哪个专业的考点库。",
    )
    jd_text = st.text_area(
        "岗位 JD",
        key="jd_text",
        height=150,
        placeholder=(
            "粘贴招聘要求。例：负责推荐系统后端开发；熟悉 MySQL 索引优化与慢查询调优；"
            "了解事务隔离级别；熟悉 TCP/IP 与 HTTP；有 Git 协作经验。"
        ),
    )

    if st.button("分析这份 JD", use_container_width=True):
        if not jd_text.strip():
            st.warning("先粘贴一段 JD 再分析。")
        else:
            with st.spinner("正在对照考点库…"):
                analyzed = jd_match.analyze_jd(
                    jd_text,
                    major=st.session_state.get("jd_major") or MAJORS[0],
                    records=load_records(),
                )
            st.session_state.jd_analysis = {
                "jd": jd_text,
                "major": st.session_state.get("jd_major") or MAJORS[0],
                "result": analyzed,
            }

    stored = st.session_state.get("jd_analysis") or {}
    result = stored.get("result") or {}
    if not result:
        return
    if stored.get("jd") and stored["jd"] != jd_text:
        st.caption("下面是上一份 JD 的结果；改完 JD 请重新点「分析这份 JD」。")
    current_major = st.session_state.get("jd_major") or MAJORS[0]
    if stored.get("major") and stored["major"] != current_major:
        st.caption(
            f"上次分析用的是「{stored['major']}」；切换专业后请重新分析。"
        )

    topics = result.get("topics") or []
    if not topics:
        st.info(
            "这份 JD 没对上考点库里的任何考点。可能是写得过于笼统，"
            "或者方向不在当前专业范围内。"
        )
        uncovered = result.get("uncovered") or []
        if uncovered:
            st.caption("考点库没有覆盖的说法：" + "、".join(uncovered))
        _jd_bridge_caption(result)
        return

    practiced = int(result.get("practiced") or 0)
    st.markdown(
        f"**这个岗位大概率会问这些**（命中 {len(topics)} 条，"
        f"其中你练过 {practiced} 条）"
    )
    st.caption(
        f"本专业 {result.get('library_size', 0)} 条考点里，你练过 "
        f"{result.get('practiced_total', 0)} 条；最近一次通过 "
        f"{result.get('passed_total', 0)} 条。"
    )
    for item in topics:
        attempts = int(item.get("attempts") or 0)
        if attempts:
            record = (
                f"练过 {attempts} 次 · 过 {item.get('pass', 0)} / "
                f"部分 {item.get('partial', 0)} / 没过 {item.get('fail', 0)}"
            )
            latest = {"pass": "最近一次通过", "partial": "最近一次部分通过", "fail": "最近一次没过"}
            if item.get("latest") in latest:
                record += f"（{latest[item['latest']]}）"
        else:
            record = "还没练过"
        st.markdown(
            f"- **{item.get('topic', '')}**｜{item.get('dimension', '')}｜{record}"
        )
        evidence = item.get("evidence") or []
        if evidence:
            st.caption("　JD 依据：" + "；".join(evidence))

    dims = result.get("dimensions") or []
    if dims:
        st.caption(
            "对应的能力维度："
            + "；".join(f"{item['name']}（{len(item['topics'])} 条）" for item in dims)
        )
    uncovered = result.get("uncovered") or []
    if uncovered:
        st.caption("JD 里这些说法考点库没有覆盖：" + "、".join(uncovered))
    _jd_bridge_caption(result)
    st.caption("依据是 JD 原文与考点名称/要点的字面重合，只能说明「可能问到」，不代表一定会问。")
    st.caption(
        "练完一场 JD 定向面试后，回到这里再点一次「分析这份 JD」，"
        "「练过几次 / 过没过」会按最新记录重算。"
    )

    st.divider()
    api_key_ready = bool(st.session_state.get("api_key"))
    st.caption(
        f"把这 {len(topics)} 条考点设为下一场的目标（技术面）：专业用「{result.get('major', '')}」，"
        "场景、难度与主问题数沿用左侧「基础配置」。"
    )
    if st.button(
        f"🎯 用这 {len(topics)} 条考点开一场面试",
        key="jd_start_interview",
        disabled=not api_key_ready,
        use_container_width=True,
    ):
        with st.spinner("正在为你开场…"):
            _start_jd_interview(result)
    if not api_key_ready:
        st.caption("填入 API Key 后即可从这里直接开场。")


def _render_voice_section() -> None:
    """语音与动效。关闭语音时整段收起，避免无意义的控件堆在侧边栏。"""
    note = st.session_state.get("tts_note")
    if note:
        st.warning(note)
        if st.button("重试语音合成", use_container_width=True):
            st.session_state.tts_note = ""
            st.session_state.tts_failures = 0
            reset_edge_breaker()
            st.rerun()

    st.session_state.voice_on = st.checkbox(
        "启用语音面试",
        value=st.session_state.voice_on,
        help="打开后可以按住说话作答，面试官也会开口回复；关闭则回到纯文字模式。",
    )
    if not st.session_state.voice_on:
        return

    names = list(VOICES)
    current = _safe_index(
        list(VOICES.values()), st.session_state.voice_name
    )
    picked = st.selectbox("面试官音色", names, index=current)
    st.session_state.voice_name = VOICES[picked]

    st.session_state.avatar_on = st.checkbox(
        "面试官语音动效",
        value=st.session_state.avatar_on,
        help="说话时显示一个随声音起伏的声波球（不出现人脸）。"
        "纯前端渲染，不需要额外配置；关掉就只保留语音与文字。",
    )

    if st.session_state.sf_key:
        st.caption("🎙️ 硅基流动 API Key 已从环境变量读取")
    else:
        st.text_input(
            "硅基流动 API Key",
            type="password",
            key="sf_key_input",
            help="在 siliconflow.cn 申请，用于语音识别；不填仍然可以用文字作答。",
        )

    st.caption(
        "录音会上传到硅基流动做识别，不做保存；面试官的声音默认由 edge-tts 合成，"
        "如不可用会自动改用硅基流动（复用同一把 Key）。识别失败会自动退回文字输入。"
    )


def _render_connection_test(api_key: str) -> None:
    """连通性测试。结果存进 session_state，否则 rerun 一次就消失。"""
    if st.button("测试 API 连通性"):
        if not api_key:
            st.session_state.conn_test = ("warning", "请先填写 API Key。")
        else:
            with st.spinner("正在连接…"):
                try:
                    reply = test_connection(api_key)
                    st.session_state.conn_test = ("success", f"连接成功，模型回复：{reply}")
                except Exception as exc:  # noqa: BLE001
                    st.session_state.conn_test = ("error", f"连接失败：{exc}")

    result = st.session_state.get("conn_test")
    if result:
        level, message = result
        getattr(st, level)(message)


def _render_running_panel() -> None:
    """面试进行中：只显示进度，不再摆一堆改了也没用的配置项。

    两条刻意为之：

    1. **不渲染任何配置控件**。`profile` 在开局时就编译好了，面试中改这些值
       不会生效——摆在那里只会让人以为改了有用。
    2. **不显示面试官的内部状态**（耐心 / 精力 / 时间压力）。DESIGN 明确
       "状态本身不给考生看"：看见了就会去迎合它，训练意义就没了。
       本场的状态轨迹在**面试结束后的报告**里揭晓（含归因）。
    """
    profile = st.session_state.profile or {}
    st.header("面试进行中")

    progress = f"已问 {st.session_state.cur_q}/{profile.get('total_rounds', 5)} 道主问题"
    if st.session_state.probes_used:
        max_probes = mood.effective_max_probes(profile, st.session_state.mood_state)
        progress += f"\n当前题已追问 {st.session_state.probes_used}/{max_probes} 次"
    st.progress(
        min(st.session_state.cur_q / max(profile.get("total_rounds", 5), 1), 1.0),
        text=progress,
    )

    st.divider()
    st.caption(profile_summary(profile))
    st.caption("本场配置在开局时已锁定；如需换配置，请先结束本场。")
    st.caption("面试官的情绪会随你的回答变化，但本场结束前不显示——报告里会揭晓整条状态轨迹。")


def render_sidebar() -> None:
    """侧边栏。

    分两种形态：**面试进行中**只显示进度（配置已锁定，摆出来只会误导）；
    **空闲时**按用途分组折叠，避免两百行控件平铺。
    """
    with st.sidebar:
        if st.session_state.status == "running" and st.session_state.profile:
            _render_running_panel()
            return

        st.header("面试配置")
        api_key = _render_api_key_section()

        # strict 要在情绪那一节之前就已知（严格模式会锁定友善型），
        # 但它的控件在「高级选项」里——所以用带 key 的控件 + 先读 session_state。
        # 这是 Streamlit 里处理"后渲染的控件影响先渲染的内容"的标准做法。
        strict = bool(st.session_state.get("strict_mode", False))

        with st.expander("🎯 基础配置", expanded=True):
            config = _render_core_config()
            # 「JD 对照 → 一键开面试」要和左侧用同一套场景/难度/主问题数，
            # 而配置控件没有 key、值只活在本次 render 的局部变量里，所以存一份。
            st.session_state.core_config = config

        with st.expander("🎭 面试官情绪", expanded=False):
            mood_ok = _render_mood_section(strict=strict)

        with st.expander("👤 候选人背景与简历", expanded=False):
            candidate_bg = _render_candidate_section()

        with st.expander("📝 简历优化（独立功能）", expanded=False):
            _render_resume_optimize_section(api_key)

        with st.expander("🧭 岗位 JD 对照（独立功能）", expanded=False):
            _render_jd_match_section()

        with st.expander("🎙️ 语音与动效", expanded=False):
            _render_voice_section()

        with st.expander("⚙️ 高级选项", expanded=False):
            st.checkbox(
                "严格模式（节奏固定）",
                value=False,
                key="strict_mode",
                help="打开后：追问上限一律收紧为 2 次（不论难度档）、不出现「复述确认/澄清/反馈」"
                "这类非提问回合，每场节奏基本一致，适合录演示视频。关闭则更像真人：追问上限"
                "按难度档放宽——A 档最多 4 次、B 档 3 次、C 档 2 次，面试官会偶尔停下来"
                "确认、澄清或反馈，节奏有呼吸感。",
            )
            st.caption("严格模式会锁定「友善型」情绪：录演示要求每场节奏一致，而情绪漂移会改变追问上限与措辞。")
            use_real, real_quota = _render_real_questions_section()
            st.divider()
            _render_connection_test(api_key)

        st.divider()
        can_start = bool(api_key) and mood_ok
        if st.button("开始面试", type="primary", disabled=not can_start, use_container_width=True):
            profile = build_profile(
                config["scene"],
                config["major"],
                config["level"],
                config["interview_type"],
                config["total_rounds"],
                candidate_bg,
                resume=st.session_state.resume_text,
                strict=strict,
                mood=st.session_state.mood_choice,
                avoid_topics=recent_asked_topics(),
                use_real=use_real,
                real_quota=real_quota,
            )
            start_interview(profile, api_key)
            st.rerun()

        if not api_key:
            st.caption("填入 DeepSeek API Key 后即可开始。")
        elif not mood_ok:
            st.caption("勾选上面的知情同意后即可开始。")


# ---------------------------------------------------------------------------
# 界面 · 面试对话
# ---------------------------------------------------------------------------


def render_page_header(meta: str) -> None:
    """页面顶部的会话栏：左侧字标、右侧场次信息。替代裸的 st.title + st.caption。"""
    st.markdown(
        f'<div class="zy-band">'
        f'<span class="zy-mark">知遇<em> · AI 虚拟面试官</em></span>'
        f'<span class="zy-meta">{meta}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_interview() -> None:
    profile = st.session_state.profile

    render_page_header(profile_summary(profile))

    jd_topics = profile.get("jd_topics") or []
    if jd_topics:
        st.caption("本场按岗位 JD 定向出题：" + "、".join(jd_topics))

    if st.session_state.get("resume_lost_note"):
        st.warning(
            "本场是从快照恢复的：简历只在内存里、不会落盘，所以无法一起恢复。"
            "本场将继续进行，但针对简历的提问会退化为按你口述的背景提问；"
            "如需简历提问，可结束本场后重新上传简历再开一场。"
        )

    # 进度条只在侧边栏渲染（_render_running_panel）。
    # 这里不再重复画一条：主区域每次重跑都会被 Streamlit 自动滚到底，
    # 顶部内容（会话栏 / 进度 / 开场白开头）就被挤出视口——2026-10-18 修复。

    # 面试官语音动效：开启语音时显示声波球，并接管"最新一条"语音的自动播放
    avatar_visible = st.session_state.avatar_on and st.session_state.voice_on
    avatar_html = avatar_component(st.session_state.avatar_audio) if avatar_visible else ""

    for index, msg in enumerate(st.session_state.history):
        role = "assistant" if msg["role"] == "assistant" else "user"
        with st.chat_message(role, avatar="🧑‍💼" if role == "assistant" else "🙋"):
            st.markdown(msg["content"])
            # 动效组件正在播的那一条，消息上不再挂播放器：同一段 mp3 挂两套
            # 播放器就是两个各自计时的进度条，怎么调都不会同步
            # （2026-10-18 报障：进度条停在 0:03，卡片却已经播完写"等待你的回答"）。
            # 语音由动效卡片负责播（它自己要靠这段音频驱动动效），卡片自带
            # 「▶ 重播」；更早的消息仍保留播放器，供回听。
            audio = st.session_state.audio_by_index.get(index)
            owned_by_avatar = bool(avatar_html) and index == st.session_state.avatar_index
            if audio and not owned_by_avatar:
                # 动效组件接管自动播放时，这里的播放器只留手动回听，避免双重出声
                st.audio(
                    audio,
                    format="audio/mp3",
                    autoplay=(index == st.session_state.autoplay_index)
                    and not avatar_html,
                )
    # 只让最新一条自动出声，否则每次刷新都会重播
    st.session_state.autoplay_index = -1

    # 放在对话流末尾、输入框上方：面试官问完话，画面就在你要作答的地方
    if avatar_html:
        _, avatar_col, _ = st.columns([1, 1.4, 1])
        with avatar_col:
            # 165px：330px 会把整页撑高约 250px，而 Streamlit 每次重跑都会把主区域
            # 滚到底，顶部会话栏因此被切掉一条（2026-10-18 报障）。动效组件内部会按
            # 可用高度等比缩放（avatar.py 的 fitCard），这里只是取景框，不必给太大。
            st.iframe(avatar_html, height=165)

    # 上一轮调用失败：面试官还没接上话。
    # 这时**不再渲染输入框**（否则再答一次会留下两条连续的候选人消息），
    # 而是让用户直接重试本轮——已答的那句还在，不用重打。
    if _awaiting_interviewer():
        st.warning(
            "面试官这一轮没有接上话（多半是网络或接口临时故障）。"
            "你的进度已经保留，直接重试即可。"
        )
        col1, col2 = st.columns([1, 4])
        if col1.button("重试本轮", type="primary"):
            with st.chat_message("assistant", avatar="🧑‍💼"):
                with st.spinner("面试官思考中…"):
                    advance()
            st.rerun()
        # 至少答过一轮才有报告可生成；开场就失败时只给重试
        if len([m for m in st.session_state.history if m.get("role") == "user"]) >= 2:
            if col2.button("放弃这一轮，直接生成报告"):
                with st.spinner("评估官与教练正在生成报告…"):
                    finish_interview()
                st.rerun()
        return

    answer = st.chat_input("输入你的回答，按回车发送…")

    sfc_key = siliconflow_key()
    if st.session_state.voice_on and sfc_key and not answer:
        # key 带上实例代号：录音消费完就换一个新控件，避免复用那个残留下
        # 失效 MediaRecorder 的旧实例（表现为控件里显示
        # "An error has occurred, please try again."，得手点刷新按钮）。
        clip = st.audio_input(
            "也可以按住说话作答", key=f"answer_clip_{st.session_state.voice_widget_gen}"
        )
        if clip is not None:
            # 同一段录音只识别一次：靠指纹比对，而不是识别完就去清空控件
            signature = hashlib.md5(clip.getvalue()).hexdigest()
            if signature != st.session_state.voice_signature:
                st.session_state.voice_signature = signature
                # 这段录音已经吃掉了，下一轮换新控件（成功走下面的 rerun，
                # 失败就在异常分支里 rerun，两种结局都让控件恢复干净）
                st.session_state.voice_widget_gen += 1
                with st.spinner("正在识别语音…"):
                    try:
                        answer = transcribe(clip.getvalue(), sfc_key)
                        answer = _correct_voice(answer)
                    except Exception as exc:  # noqa: BLE001
                        st.session_state.error = f"语音识别失败：{exc}"
                        st.rerun()
                if not answer:
                    st.rerun()
    elif st.session_state.voice_on and not answer and not sfc_key:
        st.caption("想用语音作答？在左侧填入硅基流动 API Key 后即可录音。")

    if answer:
        st.session_state.history.append({"role": "user", "content": answer})
        # 先把回答存进快照，再调用模型：万一本轮模型报错，刷新后至少不会丢掉这句回答
        save_snapshot()
        with st.chat_message("user", avatar="🙋"):
            st.markdown(answer)
        with st.chat_message("assistant", avatar="🧑‍💼"):
            with st.spinner("面试官思考中…"):
                advance()
        st.rerun()

    col1, col2 = st.columns([1, 4])
    if col1.button("结束面试并生成报告"):
        with st.spinner("评估官与教练正在生成报告…"):
            finish_interview()
        st.rerun()


# ---------------------------------------------------------------------------
# 复盘报告导出
#
# 界面上看到的内容不落盘，导出成 Markdown 后可以存档、用 VSCode 打开，
# 或直接作为参赛材料 / 演示视频的附件。
# 纯字符串拼接，不调用模型，也不影响面试主流程。
# ---------------------------------------------------------------------------


def build_resume_advice_markdown() -> str:
    """把简历优化建议整理成一份 Markdown 文本（用户主动导出时才产出）。"""
    result = st.session_state.get("resume_advice") or {}
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    add("# 简历优化建议")
    add()
    add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add(f"- 简历文件：{st.session_state.get('optimize_name') or '—'}")
    target = str(st.session_state.get("ro_target_role") or "").strip()
    add(f"- 目标岗位：{target or '未指定'}")
    if result.get("score") is not None:
        add(f"- 简历评分：**{result['score']}**")
    add()
    if result.get("verdict"):
        add(f"> {result['verdict']}")
        add()

    problems = result.get("problems") or []
    if problems:
        add("## 一、诊断")
        add()
        for item in problems:
            add(f"### {item.get('category', '') or '问题'}（{item.get('severity', '')}）")
            add()
            add(item.get("problem", ""))
            add()
            if item.get("evidence"):
                add(f"- 原文：{item['evidence']}")
            add(f"- 怎么改：{item.get('advice', '')}")
            add()

    rewrites = result.get("rewrites") or []
    if rewrites:
        add("## 二、改写对照")
        add()
        add("> 改写只用简历里已有的信息重新组织；本地已逐条核对原句出处，找不到出处的已剔除。")
        add()
        for i, item in enumerate(rewrites, start=1):
            add(f"**{i}.** {item.get('original', '')}")
            add()
            add(f"- 改写：{item.get('improved', '')}")
            if item.get("why"):
                add(f"- 为什么：{item['why']}")
            add()

    if result.get("missing"):
        add("## 三、还需要补的内容")
        add()
        for item in result["missing"]:
            add(f"- {item}")
        add()
    if result.get("keywords"):
        add("## 四、关键词建议")
        add()
        add("> 只列你真实掌握的方向，不建议堆砌不熟悉的名词。")
        add()
        for item in result["keywords"]:
            add(f"- {item}")
        add()
    if result.get("checklist"):
        add("## 五、投简历前自查")
        add()
        for item in result["checklist"]:
            add(f"- [ ] {item}")
        add()

    identity = result.get("identity") or []
    formatting = result.get("formatting") or {}
    if identity or formatting.get("issues"):
        add("## 六、身份与格式核对（本地）")
        add()
        for item in identity:
            add(f"- **{item.get('signal', '')}**")
            add(f"  - 原文：{item.get('quote', '')}")
            add(f"  - 怎么改：{item.get('advice', '')}")
        for issue in formatting.get("issues") or []:
            add(f"- {issue}")
        add()

    quant = result.get("quantification") or {}
    if quant.get("lines"):
        add("---")
        add(f"- 本地统计：{quant['lines']} 行里 {quant['quantified']} 行含量化结果"
            f"（{quant['ratio']:.0%}）。")
    dropped = int(result.get("dropped_rewrites") or 0)
    invented = int(result.get("invented_numbers") or 0)
    ungrounded = int(result.get("ungrounded_evidence") or 0)
    if dropped or ungrounded:
        add(f"- 本地核对剔除：{dropped} 条改写（其中 {invented} 条补了原文没有的数字）、"
            f"{ungrounded} 条诊断引用（对不上原文）。")
    add()
    add("> 本建议由「知遇」AI 虚拟面试智能体的简历优化功能生成。")
    add("> 改写只重组简历里已有的信息，不新增任何未写过的经历或数字；")
    add("> 标注为「缺数字」的地方请用你自己的真实数据补上。")
    return "\n".join(lines)


def build_report_markdown() -> str:
    """把本场报告整理成一份 Markdown 文本。"""
    profile = st.session_state.profile or {}
    evaluation = st.session_state.evaluation or {}
    coach = st.session_state.coach or {}
    trace = st.session_state.mood_trace or []
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    def table(headers: list[str], rows: list[list[str]]) -> None:
        add("| " + " | ".join(headers) + " |")
        add("|" + "---|" * len(headers))
        for row in rows:
            add("| " + " | ".join(str(c).replace("|", "／") for c in row) + " |")
        add()

    add("# 模拟面试复盘报告")
    add()
    add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add(f"- 面试配置：{profile_summary(profile)}")
    if profile.get("jd_topics"):
        add(f"- JD 定向考点：{'、'.join(profile['jd_topics'])}")
    add(f"- 主问题数量：{profile.get('total_rounds', '-')}")
    if profile.get("mood_mode"):
        add(f"- 面试官情绪模式：{profile['mood_mode']}")
    real_items = [
        str(item.get("q", "")).strip()
        for item in (profile.get("real_questions") or [])
        if str(item.get("q", "")).strip()
    ]
    if real_items:
        add(f"- 取材真题（{len(real_items)} 道）：" + "；".join(real_items))
    add()

    if evaluation:
        add("## 一、评分")
        add()
        add(f"- 本场实际表现：**{evaluation.get('overall_score', '-')}**")
        add(
            "- 剥离面试官状态干扰后："
            f"**{evaluation.get('adjusted_score', evaluation.get('overall_score', '-'))}**"
        )
        add(f"- 评委结论：{evaluation.get('level', '-')}")
        # 回填过的结局写进报告：这份报告本身就是参赛/复盘材料，
        # "这场后来被拒了"比一个孤立分数更能说明问题。
        outcome_record = _find_record(st.session_state.get("record_time", ""))
        if outcome_record and calibration.outcome_of(outcome_record):
            add(f"- 真实结局（本人回填）：{calibration.outcome_label(outcome_record)}")
            if calibration.outcome_note(outcome_record):
                add(f"- 结局备注：{calibration.outcome_note(outcome_record)}")
        add()
        if evaluation.get("attribution"):
            add("### 归因：哪些扣分不该算在你头上")
            add()
            add(evaluation["attribution"])
            add()
        dims = evaluation.get("dimensions") or []
        if dims:
            add("### 维度得分")
            add()
            table(
                ["维度", "得分", "评语"],
                [[d.get("name", ""), d.get("score", 0), d.get("comment", "")] for d in dims],
            )
        rubric_check = [x for x in (evaluation.get("rubric_check") or []) if isinstance(x, dict)]
        if rubric_check:
            # 要点粒度的对照结果由代码升成考点粒度（规则见 knowledge.topic_verdict），
            # 不额外叫模型判一次——同一份记录每次得到同一个结论。
            agg = aggregate_rubric_check(profile, rubric_check)
            labels = {t["topic"]: t["label"] for t in agg["topics"]}
            sources = {t["topic"]: t["source"] for t in agg["topics"]}
            add("### 考点命中情况")
            add()
            add("> 参照是底线不是天花板：清单之外的正确答案同样算分，没命中不等于答错。")
            add()
            if agg["generated"]:
                add(
                    f"> 来源：「考点库」= 自建考点库里人工核对过的参照（{agg['library']} 条）；"
                    f"「现拟」= 评委按题面现场现拟的要点，仅供参考（{agg['generated']} 条）。"
                )
                add()
            table(
                ["考点", "来源", "判定", "已命中", "遗漏", "误区"],
                [
                    [
                        item.get("topic", ""),
                        "考点库"
                        if sources.get(item.get("topic", "")) == "library"
                        else "现拟",
                        labels.get(item.get("topic", ""), ""),
                        "；".join(str(x) for x in (item.get("must_hit") or [])) or "—",
                        "；".join(str(x) for x in (item.get("missed") or [])) or "—",
                        "；".join(str(x) for x in (item.get("errors") or [])) or "—",
                    ]
                    for item in rubric_check
                ],
            )
            counts = agg["counts"]
            add()
            add(
                f"**考点级判定**：共 {agg['total']} 个考点，通过 {agg['passed']} 个，"
                f"部分通过 {counts[VERDICT_PARTIAL]} 个，不通过 {counts[VERDICT_FAIL]} 个，"
                f"本场未问到 {counts[VERDICT_UNKNOWN]} 个。"
            )
            if agg["weak"]:
                add()
                for entry in agg["weak"]:
                    tag = "" if entry["source"] == "library" else "（现拟）"
                    add(f"- {entry['label']}｜{entry['topic']}{tag}：{entry['reason']}")
            tier_line = _tier_distribution_caption(profile, rubric_check)
            if tier_line:
                add()
                add(tier_line)
            progress = _jd_progress(profile, rubric_check)
            if progress.get("total"):
                add()
                add("### JD 定向场收尾")
                add()
                table(
                    ["考点", "判定", "原因"],
                    [[i["topic"], i["label"], i["reason"]] for i in progress["topics"]],
                )
                if progress["remaining"]:
                    add(
                        f"**还剩 {len(progress['remaining'])} 条没过或没考到**："
                        + "、".join(progress["remaining"])
                    )
                else:
                    add("本场目标考点都通过了。")
                add()
        else:
            asked_topics = [
                str(t).strip()
                for t in (st.session_state.get("asked_topics") or [])
                if str(t).strip()
            ]
            if asked_topics:
                # 导出件与页面同口径：问过考点却一条判定都没有，就如实写在这里，
                # 而不是让读者以为「报告中本来就没有这一节」。
                add("### 考点级判定")
                add()
                add(
                    f"> 本场未能生成考点级判定。这场问到了 {len(asked_topics)} 个考点"
                    f"（{'、'.join(asked_topics)}），但评委没有给出可用的对照结论。"
                )
                add()
                # 与页面同源：JD 定向场即使判定缺失，也要写清「还剩哪几条、下一场练什么」。
                progress = _jd_progress(profile, [])
                if progress.get("total"):
                    add("### JD 定向场收尾")
                    add()
                    table(
                        ["考点", "判定", "原因"],
                        [[i["topic"], i["label"], i["reason"]] for i in progress["topics"]],
                    )
                    if progress["remaining"]:
                        add(
                            f"**还剩 {len(progress['remaining'])} 条没过或没考到**："
                            + "、".join(progress["remaining"])
                        )
                    else:
                        add("本场目标考点都通过了。")
                    add()
        resume_check = evaluation.get("resume_check")
        if isinstance(resume_check, dict):
            issues = [x for x in (resume_check.get("issues") or []) if isinstance(x, dict)]
            uncovered = [str(x) for x in (resume_check.get("uncovered") or []) if str(x).strip()]
            if issues or uncovered or resume_check.get("comment"):
                add("### 简历一致性核查")
                add()
                add("> 用简历原文核对你在面试里的自述，只标记确有依据的出入。")
                add()
                if issues:
                    table(
                        ["简历里的说法", "面试中的说法", "问题"],
                        [
                            [i.get("claim", ""), i.get("said", ""), i.get("comment", "")]
                            for i in issues
                        ],
                    )
                for item in uncovered:
                    add(f"- 简历里有、本场没问到的经历：{item}")
                if resume_check.get("comment"):
                    add()
                    add(f"> {resume_check['comment']}")
                add()
        for title, key in (("亮点", "highlights"), ("风险", "risks")):
            if evaluation.get(key):
                add(f"### {title}")
                add()
                for item in evaluation[key]:
                    add(f"- {item}")
                add()
        if evaluation.get("summary"):
            add("### 总评")
            add()
            add(evaluation["summary"])
            add()

    if coach:
        add("## 二、教练复盘")
        add()
        if coach.get("weakest"):
            add("### 最需要改的问题")
            add()
            for item in coach["weakest"]:
                add(f"- **{item.get('problem', '')}**")
                add(f"  - 证据：{item.get('evidence', '')}")
                add(f"  - 怎么改：{item.get('fix', '')}")
            add()
        if coach.get("rewrites"):
            add("### 回答改写示范")
            add()
            for i, item in enumerate(coach["rewrites"], start=1):
                add(f"**{i}. {item.get('question', '')}**")
                add()
                add(f"- 你的原答：{item.get('original', '')}")
                add(f"- 示范答案：{item.get('improved', '')}")
                add(f"- 为什么这样改：{item.get('why', '')}")
                add()
        if coach.get("drills"):
            add("### 接下来 3 天练什么")
            add()
            for i, item in enumerate(coach["drills"], start=1):
                add(f"{i}. {item}")
            add()
        resilience = coach.get("resilience") or {}
        if resilience.get("summary"):
            add("### 压力下的稳定性")
            add()
            add(resilience["summary"])
            add()
            if resilience.get("evidence"):
                add(f"- 证据：{resilience['evidence']}")
            if resilience.get("fix"):
                add(f"- 怎么稳住：{resilience['fix']}")
            add()
        if coach.get("encouragement"):
            add(f"> {coach['encouragement']}")
            add()

    advice_result = st.session_state.get("advice") or {}
    if advice_result.get("advice"):
        add("## 三、提升建议（下一场面试前该补什么）")
        add()
        add("> 与上一节的「教练复盘」互补：教练改的是**说法**（这句话该怎么讲），")
        add("> 这一节补的是**能力与经历**（这个维度缺什么、下次面试前做什么）。")
        add()
        if advice_result.get("priority"):
            add("### 如果只改一件事")
            add()
            for i, item in enumerate(advice_result["priority"], start=1):
                add(f"{i}. {item}")
            add()
        for item in advice_result["advice"]:
            add(f"### {item.get('dimension', '')}"
                + (f"（{item['score']} 分）" if item.get("score") is not None else ""))
            add()
            if item.get("missing"):
                add("- 本次未给出该维度的建议。")
                add()
                continue
            if item.get("gap"):
                add(f"- 缺什么：{item['gap']}")
            if item.get("evidence"):
                add(f"- 依据：{item['evidence']}")
            for action in item.get("actions") or []:
                add(f"- 动作：{action}")
            if item.get("self_check"):
                add(f"- 自测：{item['self_check']}")
            add()
        if advice_result.get("quick_wins"):
            add("### 明天就能改")
            add()
            for i, item in enumerate(advice_result["quick_wins"], start=1):
                add(f"{i}. {item}")
            add()
        if advice_result.get("next_setup"):
            add("### 下一场怎么练")
            add()
            add(advice_result["next_setup"])
            add()

    decisions = st.session_state.decisions or []
    if decisions:
        add("## 四、面试官决策轨迹")
        add()
        add("> 这一栏体现「不是念题库」：每一轮追问或换题都有明确依据，「工具调用」列可见面试官是否主动查阅了简历原文。")
        add()
        table(
            ["主问题", "动作", "考点", "考察点", "工具调用", "面试官判断"],
            [[d.get(k, "") for k in ("主问题", "动作", "考点", "考察点", "工具调用", "面试官判断")] for d in decisions],
        )

    if trace:
        mode_label = profile.get("mood_mode", "")
        add(f"## 五、面试官状态轨迹（情绪模式：{mode_label or '—'}）")
        add()
        add(mood.summarize(trace, mode_label))
        add()
        table(
            ["轮次", "状态", "耐心", "精力", "时间压力"],
            [
                [i.get("round", ""), i.get("mood", ""), i.get("patience", ""), i.get("energy", ""), i.get("pressure", "")]
                for i in trace
            ],
        )

    history = st.session_state.history or []
    if history:
        add("## 六、完整文字稿")
        add()
        for msg in history:
            speaker = "面试官" if msg.get("role") == "assistant" else "候选人"
            add(f"**{speaker}**：{msg.get('content', '')}")
            add()

    add("---")
    add()
    usage = st.session_state.get("usage") or {}
    if usage.get("calls"):
        add(
            f"> 本场共调用模型 {usage.get('calls', 0)} 次，"
            f"输入 {usage.get('prompt_tokens', 0)} tokens、"
            f"输出 {usage.get('completion_tokens', 0)} tokens。"
        )
        add()
    add("> 本报告由「知遇」AI 虚拟面试智能体自动生成。面试官的情绪状态是刻意模拟的实验条件，")
    add("> 「剥离干扰后」分数估计的是排除对方状态影响后的真实水平。")
    add()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 界面 · 历史记录与跨场对比
# ---------------------------------------------------------------------------


def _find_record(time_key: str) -> dict | None:
    """按落盘时间在 history.json 里定位一场记录；找不到返回 None。"""
    key = str(time_key or "").strip()
    if not key:
        return None
    for record in load_records():
        if str(record.get("time") or "").strip() == key:
            return record
    return None


def _render_outcome_backfill(time_key: str, key_prefix: str) -> None:
    """结局回填控件 —— 报告页与历史页共用一份实现。

    面试评分是一次**预测**，真实结局才是**标签**；把标签补上之后，
    跨场对照才有意义（对照规则在 calibration.calibrate）。

    刻意只写回 history.json、**不碰评分**：评分口径由「场景 × 专业 × 难度」
    三级配置唯一决定（DESIGN 第三节），若拿历史结局反推调分，
    口径就会随样本漂移，与"提问与评分同源、可控可解释"冲突。
    """
    record = _find_record(time_key)
    if record is None:
        st.caption("这一场在历史记录里找不到了（可能已被删除），无法回填结局。")
        return

    current = calibration.outcome_of(record)
    choice = st.selectbox(
        "这一场的真实结局",
        calibration.OUTCOMES,
        index=calibration.OUTCOMES.index(current),
        # OUTCOMES[0] 是空字符串（"未回填"的内部表示），直接渲染会是一个看不懂的空选项
        format_func=lambda value: value or "（未回填）",
        key=f"outcome_choice_{key_prefix}",
        help="「被拒」只代表这一家没过，不代表你不行。「还没结果」「不适用」不参与校准。",
    )
    note = st.text_input(
        "备注（可选）",
        value=calibration.outcome_note(record),
        key=f"outcome_note_{key_prefix}",
        placeholder="例如：自我介绍太长 / 项目细节答不上来",
    )
    if st.button("保存结局", key=f"outcome_save_{key_prefix}"):
        if history_store.set_outcome(HISTORY_FILE, time_key, choice, note):
            # 不能直接 st.success 再 st.rerun()——rerun 会立刻中断本次运行，
            # 那句提示根本渲染不出来（全仓其余 rerun 都是"状态驱动"的，用户
            # 通过界面变化就能看出成功；这里是表单提交，必须给一句明确反馈）。
            st.session_state.flash = "已回填。下面的校准结论会跟着更新。"
            st.rerun()
        else:
            st.error("没找到这一场记录，可能已经被删掉了。")


def render_history_tab() -> None:
    """历史记录页：跨场趋势 + 维度对比 + 逐场回看 + 删除入口。

    这里补上的是 DESIGN 第七节承诺的「横向对比」——数据本来就全在
    history.json 里，此前只做了逐场回看，没有变成"我这几场有没有进步"。
    """
    records = load_records()

    if not records:
        st.info("还没有面试记录。完成一场面试后，这里会出现你的进步曲线。")
        return

    st.caption(f"共 {len(records)} 场面试记录，保存在 history.json")

    # -- 结局回填与评分校准 ------------------------------------------------
    # 分数只能说明"我这几场有没有进步"；要回答"这个分准不准"就必须有真实结局。
    # 校准结论只做呈现，不反过来改写评分（理由见 _render_outcome_backfill）。
    cal = calibration.calibrate(records)
    st.subheader("评分校准")
    st.caption(
        "面试评分是一次预测，真实结局才是标签。在下面每一场里回填结局，"
        "这里就能回答「我的分数准不准、是偏乐观还是偏保守」。"
    )
    col1, col2, col3 = st.columns(3)
    col1.metric("已回填结局", f"{cal['labeled']} / {cal['scored']}")
    col2.metric("样本（已判定）", cal["decided"], f"另有 {cal['unclear']} 场落在灰区" if cal["unclear"] else None)
    accuracy = cal["accuracy"]
    # 准确率的分母是 decided，样本不足时给出百分数会与旁边的「样本还太少」自相矛盾
    if accuracy is None:
        col3.metric("评分准确率", "—", "还没有可判定的场次")
    elif cal["decided"] < calibration.MIN_SAMPLES_FOR_CONCLUSION:
        col3.metric("评分准确率", "—", f"仅 {cal['decided']} 场可判定，样本不足")
    else:
        col3.metric("评分准确率", f"{round(accuracy * 100)}%", f"{cal['hits']} 对 / {cal['misses']} 错")
    if cal["outcome_counts"]:
        st.caption(
            "结局分布："
            + "；".join(f"{name} {count} 场" for name, count in cal["outcome_counts"].items())
        )
    for note in cal["notes"]:
        st.markdown(f"- {note}")
    if cal["by_level"]:
        with st.expander("按难度档看（不同档位的基线不同，分开看才有意义）"):
            st.dataframe(
                [
                    {
                        "难度档": stats["level"],
                        "校准场次": stats["samples"],
                        "通过": stats["positive"],
                        "被拒": stats["negative"],
                        "均分": stats["avg_score"] if stats["avg_score"] is not None else "—",
                    }
                    for stats in sorted(cal["by_level"].values(), key=lambda s: s["level"])
                ],
                hide_index=True,
            )

    # -- 跨场趋势 ---------------------------------------------------------
    series = history_store.trend_series(records)
    if series["labels"]:
        st.subheader("分数趋势")
        chart: dict[str, list[float]] = {"本场实际表现": series["overall"]}
        # 剥离干扰分只有高强度情绪模式下才有，缺的场次不画这条线
        if any(v is not None for v in series["adjusted"]):
            chart["剥离干扰后"] = [
                v if v is not None else None for v in series["adjusted"]
            ]
        st.line_chart(chart)
        st.caption(
            "按时间从左到右。两条线的差距反映面试官情绪状态造成的干扰——"
            "差距越大，说明那几场你越是被对方状态带着走。"
        )

        # 首末对比：一句话回答"有没有进步"
        if len(series["overall"]) >= 2:
            first, last = series["overall"][0], series["overall"][-1]
            delta = last - first
            trend = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
            st.caption(
                f"从首场 {first:g} 分到最近一场 {last:g} 分，{trend} "
                f"{abs(delta):g} 分（共 {len(series['overall'])} 场）。"
            )

    # -- 维度对比（只在同专业 / 同面试类型内比）---------------------------
    groups = history_store.dimension_groups(records)
    if groups:
        st.subheader("维度对比")
        st.caption(
            "不同专业、不同面试类型的维度口径不同，所以分开对比——"
            "把计算机的「编程基础」和人工智能的「算法能力」放在一张表里比是没有意义的。"
        )
        for key, group in sorted(groups.items()):
            names = history_store.dimension_names(group)
            sessions = group["sessions"]
            with st.expander(f"{key}（{len(sessions)} 场）", expanded=len(groups) == 1):
                weakest = history_store.weakest_dimension(group)
                if weakest and len(sessions) >= 2:
                    st.caption(
                        f"平均分最低的维度是「{weakest[0]}」（{weakest[1]:g} 分）——"
                        "如果只补一项，先补它。"
                    )

                latest = sessions[-1]["dimensions"]
                rows: list[dict[str, str]] = []
                averages: dict[str, float] = {}
                for name in names:
                    avg = history_store.dimension_average(group, name)
                    if avg is not None:
                        averages[name] = avg
                    rows.append(
                        {
                            "维度": name,
                            "最近一场": (
                                f"{latest[name]:g}" if name in latest else "—"
                            ),
                            "平均分": f"{avg:g}" if avg is not None else "—",
                        }
                    )
                st.dataframe(rows, hide_index=True)
                if averages:
                    st.bar_chart(averages)

    # -- 逐场回看与删除 ---------------------------------------------------
    st.subheader("逐场回看")
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        outcome = calibration.outcome_of(record)
        label = history_store.record_label(record)
        # 回填过的场次把结局缀在标题上：一列扫下来就知道哪些还没回填。
        # 控件 key 用落盘时间而不是下标——删掉一场会让后面所有下标前移，
        # 用下标做 key 会把上一场填过的结局错显到这一场上。
        # 但时间戳没有唯一性保证（同一秒内完成两场时会重复），key 里再缀下标
        # 去重；定位记录仍然只用时间（time_key 与 widget key 是两回事）。
        widget_prefix = f"hist_{record.get('time') or 'na'}_{index}"
        with st.expander(f"{label} · {outcome}" if outcome else label):
            st.caption(record.get("level", ""))
            st.json(record.get("evaluation", {}))
            st.divider()
            _render_outcome_backfill(record.get("time"), widget_prefix)
            # 删除入口：DESIGN 的隐私应对写了"提供删除入口"，
            # 而落盘的 history.json 里存着完整逐字稿，必须让用户能自己清掉。
            if st.button("删除这一场", key=f"del_record_{index}"):
                history_store.delete_record(HISTORY_FILE, index)
                st.session_state.flash = "已删除这一场记录。"
                st.rerun()

    # -- 清空全部 ---------------------------------------------------------
    st.divider()
    st.caption(
        "历史记录里保存着完整逐字稿（你的回答原文与自述背景）。"
        "简历本身从不落盘，但逐字稿会——想清掉的话用下面的按钮。"
    )
    if st.session_state.get("confirm_clear_history"):
        st.warning(f"将永久删除全部 {len(records)} 场记录，无法撤销。")
        col1, col2 = st.columns([1, 3])
        if col1.button("确认清空", type="primary"):
            history_store.clear_records(HISTORY_FILE)
            st.session_state.confirm_clear_history = False
            st.session_state.flash = "已清空全部历史记录。"
            st.rerun()
        if col2.button("取消"):
            st.session_state.confirm_clear_history = False
            st.session_state.flash = "已取消，记录没有变动。"
            st.rerun()
    else:
        if st.button("清空全部历史记录"):
            # 两步确认：清空不可撤销，不能一次点击就执行
            st.session_state.confirm_clear_history = True
            st.rerun()


# ---------------------------------------------------------------------------
# 界面 · 复盘报告
# ---------------------------------------------------------------------------


def render_report() -> None:
    render_page_header("本场面试已结束 · 复盘报告")
    st.success(f"面试已结束 · {profile_summary(st.session_state.profile)}")

    jd_topics = st.session_state.profile.get("jd_topics") or []
    if jd_topics:
        st.caption("本场为 JD 定向场：" + "、".join(jd_topics))

    real_items = [
        item
        for item in (st.session_state.profile.get("real_questions") or [])
        if str(item.get("q", "")).strip()
    ]
    if real_items:
        st.caption(
            f"本场取材了 {len(real_items)} 道真实企业面试真题："
            + "；".join(str(item.get("q", "")).strip() for item in real_items)
        )

    if st.session_state.evaluation:
        _, btn_col = st.columns([4, 1])
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        btn_col.download_button(
            "导出报告",
            data=build_report_markdown(),
            file_name=f"模拟面试报告-{stamp}.md",
            mime="text/markdown",
            help="导出为 Markdown 文件，可用 VSCode 打开，也可作为参赛材料或演示视频的附件。",
            use_container_width=True,
        )

    evaluation = st.session_state.evaluation
    coach = st.session_state.coach

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["评分报告", "教练复盘", "提升建议", "面试记录", "历史记录"]
    )

    with tab1:
        if not evaluation:
            st.info("暂未生成评分报告，可点击下方「重新开始面试」。")
        else:
            col1, col2, col3 = st.columns([1, 1, 2])
            col1.metric("本场实际表现", evaluation.get("overall_score", "-"))
            col2.metric(
                "剥离干扰后",
                evaluation.get("adjusted_score", evaluation.get("overall_score", "-")),
                help="面试官的情绪状态是刻意模拟的实验条件。这个分数估计的是排除对方状态影响后，你的真实水平。",
            )
            col3.metric("评委结论", evaluation.get("level", "-"))

            usage = st.session_state.get("usage") or {}
            if usage.get("calls"):
                st.caption(
                    f"本场共调用模型 {usage.get('calls', 0)} 次 · "
                    f"输入 {usage.get('prompt_tokens', 0)} tokens · "
                    f"输出 {usage.get('completion_tokens', 0)} tokens。"
                    "「主动查阅简历」的轮次会多算一次调用，这个数字能看出窗口化压缩省下了多少。"
                )

            if evaluation.get("attribution"):
                st.subheader("归因：哪些扣分不该算在你头上")
                st.info(evaluation["attribution"])

            trace = st.session_state.mood_trace
            if trace:
                mode_label = (st.session_state.profile or {}).get("mood_mode", "")
                with st.expander(f"本场面试官状态轨迹（情绪模式：{mode_label or '—'}）"):
                    st.caption(mood.summarize(trace, mode_label))
                    columns = {
                        "round": "轮次",
                        "mood": "状态",
                        "patience": "耐心",
                        "energy": "精力",
                        "pressure": "时间压力",
                    }
                    st.dataframe(
                        [{label: item.get(key, "") for key, label in columns.items()} for item in trace],
                        hide_index=True,
                    )
                st.warning(
                    "本场面试官的部分表现是为训练目的设计的状态模拟，不代表真实面试的普遍情况。"
                    "如果你在真实面试里遇到这类面试官，问题在对方，不在你。"
                )

            dims = evaluation.get("dimensions", [])
            if dims:
                st.subheader("维度得分")
                st.dataframe(
                    [
                        {
                            "维度": d.get("name", ""),
                            "得分": d.get("score", 0),
                            "评语": d.get("comment", ""),
                        }
                        for d in dims
                    ],
                    hide_index=True,
                )
                st.bar_chart({d.get("name", ""): d.get("score", 0) for d in dims})

            rubric_check = [
                item for item in (evaluation.get("rubric_check") or []) if isinstance(item, dict)
            ]
            asked_topics = [
                str(t).strip()
                for t in (st.session_state.get("asked_topics") or [])
                if str(t).strip()
            ]
            if rubric_check:
                agg = _render_rubric_check_block(rubric_check, asked_topics)
                _render_jd_progress_block(st.session_state.profile or {}, rubric_check)
            elif asked_topics:
                # 三态里的第三态：问过考点、却一条判定都没有。
                # 以前这里是 `if rubric_check:` 直接跳过，用户只看到报告少了半页，
                # 不知道是没判、判了没存、还是根本没参照。
                st.markdown("**考点级判定**：本场未能生成。")
                st.caption(
                    f"这场问到了 {len(asked_topics)} 个考点（{'、'.join(asked_topics)}），"
                    "但评委没有给出可用的对照结论。常见原因是模型这一轮输出被截断或格式不合规，"
                    "重跑一遍通常就能恢复；如果反复出现，请把这份记录反馈给开发者。"
                )
                # JD 定向场的收尾块不能因为 rubric_check 为空就消失：
                # 它带着「只练没过的 K 条」按钮，是闭环的下一跳。判定缺失时
                # session_progress 会把每条都算成 unknown，按钮照样给出——
                # 这正是「这场没判出来」时用户最需要的出口。
                _render_jd_progress_block(st.session_state.profile or {}, rubric_check)

            resume_check = evaluation.get("resume_check")
            if isinstance(resume_check, dict):
                issues = [x for x in (resume_check.get("issues") or []) if isinstance(x, dict)]
                uncovered = [
                    str(x) for x in (resume_check.get("uncovered") or []) if str(x).strip()
                ]
                if issues or uncovered or resume_check.get("comment"):
                    with st.expander("简历一致性核查（你的自述 vs 简历原文）"):
                        st.caption("只标记确有依据的出入；简历里没被问到的经历不算你的问题。")
                        if issues:
                            st.dataframe(
                                [
                                    {
                                        "简历里的说法": i.get("claim", ""),
                                        "面试中的说法": i.get("said", ""),
                                        "问题": i.get("comment", ""),
                                    }
                                    for i in issues
                                ],
                                hide_index=True,
                            )
                        for item in uncovered:
                            st.markdown(f"- 简历里有、本场没问到的经历：{item}")
                        if resume_check.get("comment"):
                            st.info(resume_check["comment"])

            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader("亮点")
                for item in evaluation.get("highlights", []):
                    st.markdown(f"- {item}")
            with col_b:
                st.subheader("风险")
                for item in evaluation.get("risks", []):
                    st.markdown(f"- {item}")

            if evaluation.get("summary"):
                st.info(evaluation["summary"])

            # 结局回填：报告看完正好知道这场实际结果，是收集标签最自然的时机。
            # 回填后历史记录页会把它并进"评分准不准"的校准结论里。
            if st.session_state.get("record_time"):
                st.divider()
                st.subheader("这一场后来怎么样了")
                st.caption(
                    "面试评分是一次预测。回填真实结局之后，历史记录页会告诉你"
                    "你的分数偏乐观还是偏保守。"
                )
                _render_outcome_backfill(st.session_state.get("record_time", ""), "report")

    with tab2:
        if not coach:
            st.info("暂未生成复盘报告。")
        else:
            st.subheader("最需要改的问题")
            for item in coach.get("weakest", []):
                st.markdown(f"**{item.get('problem', '')}**")
                st.caption(f"证据：{item.get('evidence', '')}")
                st.write(f"怎么改：{item.get('fix', '')}")

            rewrites = coach.get("rewrites", [])
            if rewrites:
                st.subheader("回答改写示范")
                for i, item in enumerate(rewrites, start=1):
                    with st.expander(f"{i}. {item.get('question', '')}"):
                        st.markdown(f"**你的原答**：{item.get('original', '')}")
                        st.success(f"**示范答案**：{item.get('improved', '')}")
                        st.caption(f"为什么这样改：{item.get('why', '')}")

            drills = coach.get("drills", [])
            if drills:
                st.subheader("接下来 3 天练什么")
                for i, item in enumerate(drills, start=1):
                    st.markdown(f"{i}. {item}")

            resilience = coach.get("resilience") or {}
            if resilience.get("summary"):
                st.subheader("压力下的稳定性")
                st.markdown(resilience["summary"])
                if resilience.get("evidence"):
                    st.caption(f"证据：{resilience['evidence']}")
                if resilience.get("fix"):
                    st.write(f"怎么稳住：{resilience['fix']}")

            if coach.get("encouragement"):
                st.info(coach["encouragement"])

    with tab3:
        advice_result = st.session_state.get("advice") or {}
        if not advice_result.get("advice"):
            st.info(
                "暂未生成提升建议。它是在评估官与教练之后单独调用的——"
                "若那一轮调用失败，评分与教练复盘不受影响。"
            )
        else:
            st.caption(
                "与「教练复盘」互补：教练改的是**说法**（那句话该怎么讲），"
                "这一节补的是**能力与经历**（这个维度缺什么、下次面试前做什么）。"
            )
            priority = advice_result.get("priority") or []
            if priority:
                st.subheader("如果只改一件事")
                for i, item in enumerate(priority, start=1):
                    st.markdown(f"{i}. {item}")

            for item in advice_result["advice"]:
                score = item.get("score")
                title = item.get("dimension", "")
                if score is not None:
                    title += f"（{score} 分）"
                with st.expander(title, expanded=score is not None and score < 70):
                    if item.get("missing"):
                        st.caption("本次未给出该维度的建议。")
                        continue
                    if item.get("gap"):
                        st.markdown(f"**缺什么**：{item['gap']}")
                    if item.get("evidence"):
                        st.caption(f"依据：{item['evidence']}")
                    for action in item.get("actions") or []:
                        st.markdown(f"- {action}")
                    if item.get("self_check"):
                        st.info(f"自测：{item['self_check']}")

            quick = advice_result.get("quick_wins") or []
            if quick:
                st.subheader("明天就能改")
                for i, item in enumerate(quick, start=1):
                    st.markdown(f"{i}. {item}")

            if advice_result.get("next_setup"):
                st.subheader("下一场怎么练")
                st.markdown(advice_result["next_setup"])

            if advice_result.get("evidence_missing"):
                st.caption(
                    f"其中有 {advice_result['evidence_missing']} 条建议没能引用到你的原话，"
                    "已如实标出——「缺量化结果」这类建议本身就不针对某一句原话。"
                )

    with tab4:
        decisions = st.session_state.decisions
        if decisions:
            st.subheader("面试官决策轨迹")
            st.caption("这一栏体现「不是念题库」：每一轮追问或换题都有明确依据；「工具调用」列显示面试官是否主动查阅了简历原文。")
            st.dataframe(decisions, hide_index=True)

        st.subheader("完整文字稿")
        for msg in st.session_state.history:
            speaker = "面试官" if msg["role"] == "assistant" else "候选人"
            st.markdown(f"**{speaker}**：{msg['content']}")

    with tab5:
        render_history_tab()

    if st.button("重新开始面试", type="primary"):
        clear_snapshot()
        st.session_state.status = "idle"
        st.session_state.profile = None
        st.rerun()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _flush_flash() -> None:
    """显示上一轮存下的一次性提示。见 init_state 里 "flash" 的说明。"""
    message = st.session_state.get("flash") or ""
    if message:
        st.session_state.flash = ""
        st.success(message)


def main() -> None:
    init_state()
    render_sidebar()
    _flush_flash()

    if st.session_state.error:
        st.error(st.session_state.error)

    status = st.session_state.status
    if status == "idle" or st.session_state.profile is None:
        # 有未完成的面试快照时，先给出「继续 / 重新开始」的选择，不做静默恢复
        render_recovery_banner()
        st.markdown(
            """
            <div class="zy-hero">
                <div class="zy-kicker">AI Interview Agent</div>
                <div class="zy-title">知遇<em>。</em></div>
                <p class="zy-sub">多场景 · 多专业 · 多难度自适应的模拟面试智能体。
                他不会念题库——会根据你的回答现场判断继续追问还是换题，
                情绪也随回答实时变化；面试结束后自动生成评分报告与教练复盘。</p>
            </div>
            <div class="zy-grid">
                <div class="zy-cell">
                    <div class="zy-num">01 / 现场判断</div>
                    <h4>不念题库</h4>
                    <p>每一轮追问或换题都有明确依据，完整决策轨迹在复盘页可查。</p>
                </div>
                <div class="zy-cell">
                    <div class="zy-num">02 / 情绪模拟</div>
                    <h4>面试官有状态</h4>
                    <p>耐心、精力、时间压力动态变化，训练你在压力下的稳定性。</p>
                </div>
                <div class="zy-cell">
                    <div class="zy-num">03 / 评估复盘</div>
                    <h4>评分 + 教练</h4>
                    <p>维度评分、归因分析、可以直接背的示范答案，结束即得。</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption("开始：在左侧完成配置（场景 → 专业 → 难度，以及面试官情绪），填好 API Key 后点击「开始面试」。")
        st.info("小提示：技术面和专业方向相关，行为面则主要考 STAR 结构，可分别各练一场。")
    elif status == "running":
        render_interview()
    else:
        render_report()


main()
