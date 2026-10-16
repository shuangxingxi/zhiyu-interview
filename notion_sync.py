"""notion_sync.py — 把知识库内容同步到 Notion 归档页面（对应「AI虚拟面试智能体-知识库」）。

同步内容：
    考点库（6 专业 × A/B/C 三档 × 15 条 = 270 条，含评分要点三元组与教材溯源）
        → 每个专业一个子页面（共 6 页）
    真题库（技术面 1044 条 + 行为面 56 条）
        → 按来源仓库分页（许可证不同分开归档，与项目数据存储约定一致）
    同步完成后在父页面追加「归档内容（自动同步）」索引（link_to_page）

设计原则：
    - 零新增依赖：优先调用系统自带 curl（走系统 TLS 栈，规避个别网络环境对
      Python TLS 握手的干扰，如连接被重置），系统没有 curl 时回退标准库 urllib
    - 先干跑后写入：--dry-run 只在本地构建与统计（不联网），--push 才真正写入
    - 幂等可重跑：同名子页面已存在则跳过，中断后可直接重跑续传
    - 密钥不落盘：token 只从环境变量 NOTION_API_KEY 读取，不写进任何文件

用法（Windows PowerShell，项目目录下）：
    $env:NOTION_API_KEY = "ntn_xxx"      # Notion 内部集成 token
    py -3.12 notion_sync.py --dry-run    # 本地构建 + 统计（不联网）
    py -3.12 notion_sync.py --push       # 写入 Notion
    py -3.12 notion_sync.py --verify     # 读回父页面子页清单，核对结果
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent

# 「AI虚拟面试智能体-知识库」页面 id（取自页面 URL：AI-3f7e9ec4dfef812cbba8ca53a1f5fd95）
PARENT_PAGE_ID = "3f7e9ec4dfef812cbba8ca53a1f5fd95"

NOTION_VERSION = "2022-06-28"
API_BASE = "https://api.notion.com/v1"
REQUEST_INTERVAL = 0.35  # 两次请求之间的间隔，避开 Notion 约 3 次/秒的限流
BATCH_ELEMENTS = 90  # 单次请求的元素上限（Notion 限制 100，留出余量；含嵌套子块）

LEVEL_LABEL = {
    "A": "A 级｜985 高校 · 一线大厂",
    "B": "B 级｜211 高校 · 中型互联网 / 上市公司",
    "C": "C 级｜双非本科 · 中小企业",
}
LEVELS = ["A", "B", "C"]

# CC-BY-SA / CC BY-NC-SA / MIT —— 归档页面里如实署名的来源对照
SOURCE_LICENSE = {
    "doocs/advanced-java": "CC-BY-SA-4.0",
    "CyC2018/CS-Notes": "CC BY-NC-SA 4.0（非商业）",
    "haizlin/fe-interview": "MIT",
}


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def load_rubrics() -> dict[str, dict[str, dict]]:
    """rubrics.json：{专业: {考点: {level, must_hit, bonus, common_errors}}}"""
    return json.loads((BASE / "rubrics.json").read_text(encoding="utf-8"))


def load_trace_table() -> dict[tuple[str, str], dict]:
    """解析《知识库考点溯源表.md》的表格，返回 {(专业, 考点): 行}。

    行内含：档位 / 序号 / 能力维度 / 课程 / 教材 / 章节 / 核对状态 / 备注。
    """
    text = (BASE / "知识库考点溯源表.md").read_text(encoding="utf-8")
    rows: dict[tuple[str, str], dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 10 or cells[1] not in LEVELS:
            continue  # 表头 / 分隔行 / 说明表格
        major, level, no, topic, dim, course, book, chapter, status, note = cells
        rows[(major, topic)] = {
            "major": major,
            "level": level,
            "no": no,
            "dim": dim,
            "course": course,
            "book": book,
            "chapter": chapter,
            "status": status,
            "note": note,
        }
    return rows


def load_questions() -> dict:
    """real_questions.json：技术面按来源去重分页；行为面单独一页。"""
    data = json.loads((BASE / "real_questions.json").read_text(encoding="utf-8"))
    tech_groups: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    dropped = 0
    src_majors: dict[str, list[str]] = {}
    for major, items in (data.get("技术面") or {}).items():
        for item in items or []:
            key = (str(item.get("q", "")).strip(), str(item.get("ref", "")).strip())
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            src = str(item.get("src", "")).strip() or "未标注来源"
            tech_groups.setdefault(src, []).append(item)
            src_majors.setdefault(src, [])
            if major not in src_majors[src]:
                src_majors[src].append(major)
    behavior = list(data.get("行为面") or [])
    return {
        "tech": tech_groups,
        "tech_total": len(seen),
        "tech_dropped": dropped,
        "src_majors": src_majors,
        "behavior": behavior,
    }


# ---------------------------------------------------------------------------
# Notion 块构造
# ---------------------------------------------------------------------------


def rt(text: str, link: str = "", bold: bool = False, gray: bool = False) -> dict:
    obj: dict = {"type": "text", "text": {"content": text}}
    if link:
        obj["text"]["link"] = {"url": link}
    annotations = {}
    if bold:
        annotations["bold"] = True
    if gray:
        annotations["color"] = "gray"
    if annotations:
        obj["annotations"] = annotations
    return obj


def heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [rt(text)]}}


def paragraph(rts: list[dict], color: str = "") -> dict:
    body: dict = {"rich_text": rts}
    if color:
        body["color"] = color
    return {"object": "block", "type": "paragraph", "paragraph": body}


def bullet(rts: list[dict], children: list[dict] | None = None) -> dict:
    body: dict = {"rich_text": rts}
    if children:
        body["children"] = children
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": body}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def link_to_page(page_id: str) -> dict:
    return {
        "object": "block",
        "type": "link_to_page",
        "link_to_page": {"type": "page_id", "page_id": page_id},
    }


def trace_line(row: dict | None) -> str:
    """把溯源表的一行压成一条灰色元信息。"""
    if not row:
        return "能力维度：—　｜　教材溯源：未在溯源表中找到该考点"
    parts = [f"能力维度：{row['dim']}", f"课程：{row['course']}"]
    book, chapter, status = row["book"], row["chapter"], row["status"]
    if status == "不适用":
        parts.append("教材溯源：通用软技能，无对应教材")
    elif book.startswith("—"):
        parts.append(f"教材溯源：工程实践类，白名单教材无对应章节（{status}）")
    else:
        parts.append(f"教材溯源：{book}　{chapter}（{status}）")
    if row["note"] and row["note"] not in ("", "　"):
        parts.append(f"备注：{row['note']}")
    return "　｜　".join(parts)


def entry_blocks(no: int, topic: str, rub: dict, row: dict | None) -> list[dict]:
    """一条考点：标题 + 溯源元信息 + 三组要点（必中 / 加分 / 误区）。"""
    blocks = [
        heading(3, f"{no}. {topic}"),
        paragraph([rt(trace_line(row), gray=True)]),
    ]
    groups = [
        ("必中要点", rub.get("must_hit") or []),
        ("加分项（讲出来是亮点）", rub.get("bonus") or []),
        ("常见误区（踩中会扣分）", rub.get("common_errors") or []),
    ]
    for label, points in groups:
        if not points:
            continue
        blocks.append(
            bullet([rt(label, bold=True)], [bullet([rt(str(p))]) for p in points])
        )
    return blocks


def build_major_pages() -> tuple[list[dict], dict]:
    """考点库：每个专业一页。返回 (页面计划, 统计信息)。"""
    rubrics = load_rubrics()
    traces = load_trace_table()
    pages: list[dict] = []
    stats = {"built": 0, "trace_missing": 0, "rubric_missing": 0, "level_mismatch": 0}

    used_keys: set[tuple[str, str]] = set()
    for major, entries in rubrics.items():
        snippets: list[dict] = [
            paragraph(
                [
                    rt(
                        f"本页为「{major}」专业面试考点库，共 {len(entries)} 条："
                        "A / B / C 三档各 15 条，按 5 个能力维度组织。"
                    )
                ]
            ),
            paragraph(
                [
                    rt(
                        "每条考点的评分参照由三部分组成：必中要点（must_hit）、"
                        "加分项（bonus）、常见误区（common_errors）。"
                        "参照是底线不是天花板——考生讲对了但用词不同照常给分；"
                        "教材溯源见《知识库考点溯源表.md》。",
                        gray=True,
                    )
                ]
            ),
            divider(),
        ]
        for level in LEVELS:
            level_items = [
                (topic, rub)
                for topic, rub in entries.items()
                if str(rub.get("level", "")).strip() == level
            ]
            if not level_items:
                continue
            # 按溯源表序号排序，保证与项目文档中的顺序一致
            level_items.sort(key=lambda kv: int(traces.get((major, kv[0]), {}).get("no", 99)))

            snippets.append(heading(2, f"{LEVEL_LABEL[level]}（{len(level_items)} 条）"))
            for topic, rub in level_items:
                row = traces.get((major, topic))
                if row is None:
                    stats["trace_missing"] += 1
                used_keys.add((major, topic))
                no = int(row["no"]) if row else 0
                snippets.extend(entry_blocks(no, topic, rub, row))
                stats["built"] += 1

        pages.append({"title": f"考点库 · {major}", "blocks": snippets})

    stats["rubric_missing"] = len(set(traces.keys()) - used_keys)
    for (major, topic), row in traces.items():
        rub = (rubrics.get(major) or {}).get(topic)
        if rub and str(rub.get("level", "")).strip() != row["level"]:
            stats["level_mismatch"] += 1
    return pages, stats


def build_question_pages(questions: dict) -> list[dict]:
    """真题库：技术面按来源仓库一页，行为面一页。"""
    pages: list[dict] = []

    def group_bullets(items: list[dict], group_key: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for item in items:
            key = str(item.get(group_key) or item.get("cat") or "未分类").strip()
            groups.setdefault(key, []).append(item)
        blocks: list[dict] = []
        for key, sub in groups.items():
            blocks.append(heading(3, f"{key}（{len(sub)} 条）"))
            for item in sub:
                q = str(item.get("q", "")).strip()
                ref = str(item.get("ref", "")).strip()
                rts = [rt(q)]
                if ref:
                    rts.append(rt("　来源↗", link=ref))
                blocks.append(bullet(rts))
        return blocks

    for src, items in sorted(questions["tech"].items(), key=lambda kv: -len(kv[1])):
        license_note = SOURCE_LICENSE.get(src, "")
        majors = " / ".join(questions["src_majors"].get(src, []))
        snippets = [
            paragraph(
                [
                    rt(f"技术面真题归档 · 来源仓库：{src}（{license_note}）"),
                    rt("　仓库地址↗", link=f"https://github.com/{src}"),
                ]
            ),
            paragraph(
                [
                    rt(
                        f"共 {len(items)} 条，覆盖专业：{majors}。"
                        "仅作面试素材归档，运行时由题库抽样模块按配额注入，不参与评分参照匹配。",
                        gray=True,
                    )
                ]
            ),
            divider(),
            *group_bullets(items, "cat"),
        ]
        pages.append({"title": f"真题库 · 技术面 · {src}（{len(items)} 条）", "blocks": snippets})

    behavior = questions["behavior"]
    if behavior:
        snippets = [
            paragraph(
                [
                    rt("行为面真题归档 · 来源仓库：haizlin/fe-interview（MIT）"),
                    rt("　仓库地址↗", link="https://github.com/haizlin/fe-interview"),
                ]
            ),
            paragraph([rt(f"共 {len(behavior)} 条，6 个专业通用。", gray=True)]),
            divider(),
            *group_bullets(behavior, "topic"),
        ]
        pages.append({"title": f"真题库 · 行为面（{len(behavior)} 条）", "blocks": snippets})

    return pages


def count_elements(blocks: list[dict]) -> int:
    """统计元素总数（含一层嵌套子块），用于分批。"""
    total = 0
    for b in blocks:
        total += 1
        children = (b.get(b["type"]) or {}).get("children") or []
        total += len(children)
    return total


def pack_batches(blocks: list[dict]) -> list[list[dict]]:
    """按元素总数（含嵌套）切成不超过 BATCH_ELEMENTS 的批次。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    count = 0
    for b in blocks:
        size = 1 + len((b.get(b["type"]) or {}).get("children") or [])
        if current and count + size > BATCH_ELEMENTS:
            batches.append(current)
            current, count = [], 0
        current.append(b)
        count += size
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# Notion API
# ---------------------------------------------------------------------------


def _status_hint(code: int) -> str:
    if code == 401:
        return "（token 无效或已撤销）"
    if code == 403:
        return "（集成没有被授权访问该页面：在连接的「内容访问权限」里添加该页面）"
    if code == 404:
        return "（页面不存在，或未授权给集成）"
    return ""


def _request_via_curl(
    method: str, url: str, token: str, payload_text: str | None
) -> tuple[int, str]:
    """用系统 curl 发一次请求，返回 (HTTP 状态码, 响应体)。

    为什么不是全用 urllib：个别网络环境会干扰 Python(OpenSSL) 到 Notion 的
    TLS 握手（表现为 ConnectionResetError），curl 走系统 TLS 栈不受影响。
    token 通过 stdin 的 curl 配置传入，不进入命令行参数、不落盘。
    """
    cfg = [
        f'url = "{url}"',
        f'request = "{method}"',
        f'header = "Authorization: Bearer {token}"',
        f'header = "Notion-Version: {NOTION_VERSION}"',
        f'header = "Content-Type: application/json"',
        "silent",
        "show-error",
        "max-time = 60",
        'write-out = "\\n__HTTP_%{http_code}__"',
    ]
    if payload_text is not None:
        # curl 配置里的双引号值：反斜杠与双引号需要转义
        escaped = payload_text.replace("\\", "\\\\").replace('"', '\\"')
        cfg.append(f'data = "{escaped}"')
    try:
        proc = subprocess.run(
            ["curl", "--config", "-"],
            input="\n".join(cfg) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise urllib.error.URLError("curl 请求超时（90 秒）") from None
    if proc.returncode != 0:
        raise urllib.error.URLError(
            f"curl 退出码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}"
        )
    out = proc.stdout
    idx = out.rfind("__HTTP_")
    if idx < 0:
        raise urllib.error.URLError("curl 响应里没有状态码标记")
    return int(out[idx + 7 : idx + 10]), out[:idx].rstrip("\r\n")


def api(method: str, path: str, payload: dict | None = None) -> dict:
    token = os.environ.get("NOTION_API_KEY", "").strip()
    if not token:
        raise SystemExit(
            "缺少环境变量 NOTION_API_KEY。先执行：$env:NOTION_API_KEY = \"ntn_xxx\" 再运行 --push。"
        )
    url = API_BASE + path
    payload_text = json.dumps(payload) if payload is not None else None
    curl = shutil.which("curl")

    for attempt in range(4):
        try:
            if curl:
                code, body = _request_via_curl(method, url, token, payload_text)
                if code >= 400:
                    if code == 429 and attempt < 3:
                        time.sleep(2.0)  # curl 路径拿不到 Retry-After，用固定退避
                        continue
                    raise SystemExit(
                        f"Notion API {code} {path} {_status_hint(code)}\n{body[:400]}"
                    )
                return json.loads(body)

            req = urllib.request.Request(
                url,
                data=payload_text.encode("utf-8") if payload_text is not None else None,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 3:
                time.sleep(float(exc.headers.get("Retry-After", "1")) + 0.5)
                continue
            raise SystemExit(
                f"Notion API {exc.code} {path} {_status_hint(exc.code)}\n{body[:400]}"
            ) from None
        except urllib.error.URLError as exc:
            if attempt < 3:
                time.sleep(1.5)
                continue
            raise SystemExit(f"网络请求失败：{exc}") from None
    raise SystemExit("Notion API 重试仍未成功。")


def list_all_children(block_id: str) -> list[dict]:
    """分页读取某个块（页面）下的全部子块。"""
    results: list[dict] = []
    cursor = ""
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = api("GET", path)
        results.extend(data.get("results") or [])
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor") or ""


def plain_text(block: dict) -> str:
    btype = block.get("type", "")
    body = block.get(btype) or {}
    if btype == "child_page":  # 子页面块用 title 字段，没有 rich_text
        return str(body.get("title", ""))
    return "".join(t.get("plain_text", "") for t in body.get("rich_text") or [])


def create_page(title: str, batches: list[list[dict]]) -> dict:
    page = api(
        "POST",
        "/pages",
        {
            "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            "children": batches[0],
        },
    )
    page_id = page["id"]
    for batch in batches[1:]:
        time.sleep(REQUEST_INTERVAL)
        api("PATCH", f"/blocks/{page_id}/children", {"children": batch})
    return page


def append_children(block_id: str, blocks: list[dict]) -> None:
    for batch in pack_batches(blocks):
        api("PATCH", f"/blocks/{block_id}/children", {"children": batch})
        time.sleep(REQUEST_INTERVAL)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_all() -> tuple[list[dict], dict]:
    major_pages, stats = build_major_pages()
    questions = load_questions()
    pages = major_pages + build_question_pages(questions)
    for page in pages:
        page["batches"] = pack_batches(page["blocks"])
        page["elements"] = count_elements(page["blocks"])
    stats["questions"] = questions
    return pages, stats


def dry_run() -> None:
    pages, stats = build_all()
    q = stats["questions"]
    print("== 考点库 ==")
    print(
        f"构建 {stats['built']} 条；溯源表缺失 {stats['trace_missing']} 条；"
        f"溯源表多余 {stats['rubric_missing']} 条；档位不一致 {stats['level_mismatch']} 条"
    )
    print()
    print("== 真题库 ==")
    print(
        f"技术面去重后 {q['tech_total']} 条（丢弃重复 {q['tech_dropped']} 条）；"
        f"行为面 {len(q['behavior'])} 条"
    )
    for src, items in sorted(q["tech"].items(), key=lambda kv: -len(kv[1])):
        print(f"  {src}: {len(items)} 条")
    print()
    print("== 页面计划 ==")
    for page in pages:
        print(
            f"  {page['title']}：{page['elements']} 个元素，"
            f"{len(page['batches'])} 批（首屏 {'+' if len(page['batches']) > 1 else ''}"
            f"{max(0, len(page['batches']) - 1)} 次追加）"
        )
    total_elements = sum(p["elements"] for p in pages)
    total_requests = sum(len(p["batches"]) for p in pages) + len(pages) + 2
    print(f"合计 {len(pages)} 页 / {total_elements} 个元素，预计约 {total_requests} 次 API 请求。")


def push() -> None:
    pages, _ = build_all()
    existing = {plain_text(b): b["id"] for b in list_all_children(PARENT_PAGE_ID) if b.get("type") == "child_page"}
    created: list[tuple[str, str]] = []
    print(f"父页面下已有 {len(existing)} 个子页面。")
    for page in pages:
        title = page["title"]
        if title in existing:
            print(f"跳过（已存在）：{title}")
            created.append((title, existing[title]))
            continue
        result = create_page(title, page["batches"])
        created.append((title, result["id"]))
        print(f"已创建：{title} → {result.get('url', '')}")
        time.sleep(REQUEST_INTERVAL)

    # 父页面追加索引（已存在同名标题则跳过，保证幂等重跑）
    parent_blocks = list_all_children(PARENT_PAGE_ID)
    if any(b.get("type") == "heading_2" and plain_text(b) == "归档内容（自动同步）" for b in parent_blocks):
        print("索引已存在，跳过。")
    else:
        index_blocks = [
            divider(),
            heading(2, "归档内容（自动同步）"),
            paragraph(
                [
                    rt(
                        "以下子页面由 notion_sync.py 从项目数据生成："
                        "rubrics.json（考点与评分参照）、real_questions.json（真题素材）、"
                        "知识库考点溯源表.md（教材溯源）。",
                        gray=True,
                    )
                ]
            ),
            *[link_to_page(pid) for _, pid in created],
        ]
        append_children(PARENT_PAGE_ID, index_blocks)
        print("父页面索引已追加。")
    print(f"完成：本次涉及 {len(created)} 个子页面。")


def verify() -> None:
    children = list_all_children(PARENT_PAGE_ID)
    print(f"父页面共 {len(children)} 个子块：")
    for b in children:
        btype = b.get("type")
        if btype == "child_page":
            print(f"  [子页面] {plain_text(b)}  id={b['id']}")
        elif btype in ("heading_1", "heading_2", "heading_3"):
            print(f"  [{btype}] {plain_text(b)}")
        elif btype == "paragraph":
            text = plain_text(b)
            if text:
                print(f"  [段落] {text[:60]}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry-run"
    if mode == "--dry-run":
        dry_run()
    elif mode == "--push":
        push()
    elif mode == "--verify":
        verify()
    else:
        raise SystemExit(f"未知参数：{mode}（可用：--dry-run / --push / --verify）")