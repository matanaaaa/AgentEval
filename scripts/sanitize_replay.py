"""
对 fixtures/replay/real_sanitized 下的回放数据做一致性脱敏。

目标目录里虽然名为 real_sanitized，但 cases.json 与 .sse 里仍残留真实 PII
（手机/座机/邮箱/姓名）与真实企业全称、真实客户/记录 ID。本脚本对这些内容值
做**跨文件同值替换**，保证 cases.json 断言与 .sse 快照对应值保持一致，从而
回放六层评测结果不受影响。

两类替换：
1. 整值替换（apply_map）：对整个文件文本按映射表 str.replace。命中 .sse 中
   结构化 JSON（快照 extracted[].value/label、CRM 卡片 records[].name、
   SearchSourcePanel items 等）与 cases.json 里的断言值。
2. delta 分片替换（sanitize_reasoning_delta）：Agent 的 reasoning_narration
   是逐字流式输出的，敏感值被切成多个 TEXT_MESSAGE_CONTENT 的 "delta" 片段
   （如手机号 139032+08506、企业名 中铁+十二局+集团、ID 443+522117+886714+2）。
   整值替换吃不到这些碎片，但 SSEParser 拼接后仍会还原成完整敏感文本。
   因此把同一 message_id 的连续 delta 拼成整段、按映射表替换、再按原各片长度
   切回写入——保证脱敏彻底且不改变 SSE 事件结构。

绝不触碰 schema 结构键（XdMDItm.* / labelKey / apikey / entityType 枚举
整数 / message_id / trace_url 等），映射表里的值都不与这些键冲突（已逐一核对）。

用法：
    python scripts/sanitize_replay.py            # 就地脱敏
    python scripts/sanitize_replay.py --dry-run  # 只打印将发生的替换，不写文件
"""

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(REPO_ROOT, "fixtures", "replay", "real_sanitized")

# 原值 -> 脱敏值。脚本内部一律按"原值长度降序"应用，保证长串先于其子串替换
# （如"中铁十二局集团…分公司"先于"中铁十二局集团"先于"中铁十二局"）。
REPLACEMENTS = {
    # --- 真实企业全称 / 分公司名（长的在前，靠长度排序自动保证顺序） ---
    "中铁十二局集团有限公司陕西第一分公司": "示例建工集团有限公司示范第一分公司",
    "中铁十二局集团第一工程有限公司永济装备科技分公司": "示例建工集团第一工程有限公司示范装备科技分公司",
    "好买财富管理股份有限公司": "示例财富管理股份有限公司",
    "中铁十二局集团": "示例建工集团",
    "中铁十二局": "示例建工局",
    # --- 真实记录/客户 ID ---
    "4435221178867142": "4400000000000001",
    "4484712456511084": "4400000000000002",
    "4435231327477312": "4400000000000003",
    "4435154820030040": "4400000000000004",
    # --- 邮箱（整体替换，local part 不保留姓名拼音） ---
    "liuling@tianke.com": "zhangwei@example.com",
    # --- 手机号 ---
    "13903208506": "13800000001",
    "15901627483": "13800000002",
    # --- 座机 ---
    "90286789": "80000001",
    # --- 姓名 ---
    "刘玲": "张伟",
    "王玲": "李娜",
    "毛振": "钱勇",
}

# 长度降序的 (old, new) 列表，供整段/整值替换共用
ORDERED = sorted(REPLACEMENTS.items(), key=lambda kv: len(kv[0]), reverse=True)

EXTS = (".sse", ".json")

# 匹配一行 SSE data 里的 reasoning_narration delta。用它把碎片拼起来再切回。
_DATA_PREFIX = "data: "


def apply_map(text):
    """整段/整值替换，返回 (新文本, {old: 次数})"""
    hits = {}
    for old, new in ORDERED:
        c = text.count(old)
        if c:
            text = text.replace(old, new)
            hits[old] = hits.get(old, 0) + c
    return text, hits


def _redistribute(new_full, lengths):
    """
    把脱敏后的整段 new_full 按原各分片长度 lengths 重新切分。
    长度可能因替换而变化：前 n-1 片尽量保持原长，最后一片吸收剩余。
    这样各分片仍是合法字符串，拼接结果 == new_full，SSE 事件数量不变。
    """
    pieces = []
    idx = 0
    for i, ln in enumerate(lengths):
        if i == len(lengths) - 1:
            pieces.append(new_full[idx:])
        else:
            pieces.append(new_full[idx:idx + ln])
            idx += ln
    return pieces


def sanitize_reasoning_deltas(lines):
    """
    处理 .sse 的 reasoning delta 分片。

    SSE 里每个 data 行之间夹着空行和 event: 行，因此不能按"相邻行"聚合。
    改为按 message_id 全局聚合：同一 message_id 的所有 delta 行本就是一次
    流式输出被切成的碎片，按文件出现顺序拼接 -> 映射替换 -> 按原各片长度切回。
    返回 (新行列表, {old: 次数})。
    """
    out = list(lines)
    total_hits = {}

    # message_id -> [(line_index, obj, delta), ...]，保持文件出现顺序
    groups = {}
    order = []
    for idx, line in enumerate(lines):
        parsed = _parse_content_delta(line)
        if parsed is None:
            continue
        delta, obj = parsed
        mid = obj.get("message_id")
        if mid not in groups:
            groups[mid] = []
            order.append(mid)
        groups[mid].append((idx, obj, delta))

    for mid in order:
        group = groups[mid]
        full = "".join(g[2] for g in group)
        new_full, hits = apply_map(full)
        if not hits:
            continue
        for k, v in hits.items():
            total_hits[k] = total_hits.get(k, 0) + v
        lengths = [len(g[2]) for g in group]
        new_pieces = _redistribute(new_full, lengths)
        for (line_idx, obj, _old_delta), new_delta in zip(group, new_pieces):
            obj["delta"] = new_delta
            out[line_idx] = _DATA_PREFIX + json.dumps(obj, ensure_ascii=False)

    return out, total_hits


def _parse_content_delta(line):
    """
    若该行是 `data: {...TEXT_MESSAGE_CONTENT...delta...}`，返回 (delta, obj)，
    否则返回 None。只认 reasoning_narration 的内容分片。
    """
    if not line.startswith(_DATA_PREFIX):
        return None
    payload = line[len(_DATA_PREFIX):]
    if '"TEXT_MESSAGE_CONTENT"' not in payload or '"delta"' not in payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if obj.get("type") != "TEXT_MESSAGE_CONTENT" or "delta" not in obj:
        return None
    return obj["delta"], obj


def iter_target_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(EXTS):
                yield os.path.join(dirpath, name)


def process_file(path, dry_run):
    with open(path, encoding="utf-8") as f:
        original = f.read()

    hits_total = {}

    if path.endswith(".sse"):
        # 先处理 delta 分片（按行），再对整体做整值替换（吃 JSON 结构里的完整值）
        lines = original.splitlines()
        lines, delta_hits = sanitize_reasoning_deltas(lines)
        text = "\n".join(lines)
        # splitlines 丢掉末尾换行，若原文件以换行结尾则补回
        if original.endswith("\n"):
            text += "\n"
        text, value_hits = apply_map(text)
        for d in (delta_hits, value_hits):
            for k, v in d.items():
                hits_total[k] = hits_total.get(k, 0) + v
    else:
        text, hits_total = apply_map(original)

    if hits_total and not dry_run and text != original:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    return hits_total


def main():
    parser = argparse.ArgumentParser(description="脱敏 real_sanitized 回放数据")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    args = parser.parse_args()

    if not os.path.isdir(TARGET_DIR):
        print(f"目标目录不存在: {TARGET_DIR}", file=sys.stderr)
        return 1

    total_files = 0
    changed_files = 0
    grand = {}

    for path in sorted(iter_target_files(TARGET_DIR)):
        total_files += 1
        hits = process_file(path, args.dry_run)
        if not hits:
            continue
        changed_files += 1
        for k, v in hits.items():
            grand[k] = grand.get(k, 0) + v
        rel = os.path.relpath(path, REPO_ROOT)
        summary = ", ".join(f"{old}×{n}" for old, n in hits.items())
        print(f"[{'DRY' if args.dry_run else 'FIX'}] {rel}: {summary}")

    print("-" * 60)
    print(f"扫描 {total_files} 个文件，命中 {changed_files} 个。")
    if grand:
        print("各敏感值总替换次数：")
        for old, _new in ORDERED:
            if old in grand:
                print(f"  {old!r} -> {REPLACEMENTS[old]!r}: {grand[old]}")
    else:
        print("未发现任何待脱敏内容（可能已脱敏完成）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
