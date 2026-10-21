#!/usr/bin/env python3
"""Claude Code 세션 뷰어 — 로컬 웹앱 (표준 라이브러리만).

실행:  python3 server.py
브라우저가 자동으로 열린다. 세션 파일은 읽기 전용으로만 접근한다.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 화면(index.html)과 서버가 맞물리는 계약의 버전.
# 엔드포인트·응답 필드가 바뀌면 올린다. index.html 의 APP_VERSION 과 같아야 하며,
# 다르면 화면이 "서버를 재시작하세요" 배너를 띄운다.
# (index.html 은 매 요청마다 디스크에서 새로 읽히는데 서버는 떠 있던 프로세스가
#  그대로라, 코드를 고친 뒤 재시작을 잊으면 조용히 어긋난 상태가 된다.)
APP_VERSION = "4"

HERE = Path(__file__).resolve().parent
SESSIONS_ROOT = (Path.home() / ".claude" / "projects").resolve()
SETTINGS_FILE = (Path.home() / ".claude" / "settings.json").resolve()

# 휴지통은 projects/ '밖'에 둔다. 안에 두면 Claude Code 와 이 뷰어 양쪽이
# 버린 세션을 살아있는 세션으로 다시 집어든다.
TRASH_ROOT = (Path.home() / ".claude" / ".cc-explorer-trash").resolve()
TRASH_INDEX = TRASH_ROOT / "index.json"
TRASH_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")

# 목록에서 무시할 노이즈 이벤트
NOISE_TYPES = {
    "queue-operation", "attachment", "file-history-snapshot",
    "mode", "ai-title", "last-prompt", "summary",
}

# (path, mtime, size) -> meta dict 캐시
_meta_cache = {}


def _iter_lines(path):
    """파일을 줄 단위로 읽어 (json_obj 또는 None) 를 yield. 깨진 줄은 None."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, json.JSONDecodeError):
                yield None


def scan_meta(path):
    """세션 파일에서 목록용 메타만 가볍게 추출."""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    cached = _meta_cache.get(key)
    if cached is not None:
        return cached

    meta = {
        "file": str(path),
        "uuid": path.stem,
        "aiTitle": None,       # Claude 가 자동으로 붙인 제목
        "customTitle": None,   # /rename 으로 직접 붙인 이름 (있으면 이쪽이 우선)
        "lastPrompt": None,
        "cwd": None,
        "gitBranch": None,
        "version": None,
        "firstTs": None,
        "lastTs": None,
        "msgCount": 0,
        "models": [],
        "size": st.st_size,
        "mtime": st.st_mtime,
        "badLines": 0,
        "parentSessionId": None,   # /branch 로 분기된 경우 부모 세션
        "forkedAtMessage": None,
    }
    models = set()
    for obj in _iter_lines(path):
        if obj is None:
            meta["badLines"] += 1
            continue
        if meta["parentSessionId"] is None:
            ff = obj.get("forkedFrom")
            if isinstance(ff, dict) and ff.get("sessionId"):
                meta["parentSessionId"] = ff.get("sessionId")
                meta["forkedAtMessage"] = ff.get("messageUuid")
        t = obj.get("type")
        if t == "ai-title":
            meta["aiTitle"] = obj.get("aiTitle") or meta["aiTitle"]
            continue
        if t == "custom-title":
            # /rename 은 ai-title 을 바꾸지 않고 custom-title 레코드를 덧붙인다.
            # 여러 번 바꿨으면 마지막 것이 현재 이름이다.
            meta["customTitle"] = obj.get("customTitle") or meta["customTitle"]
            continue
        if t == "last-prompt":
            meta["lastPrompt"] = obj.get("lastPrompt") or meta["lastPrompt"]
            continue
        ts = obj.get("timestamp")
        if ts:
            if meta["firstTs"] is None:
                meta["firstTs"] = ts
            meta["lastTs"] = ts
        if t in ("user", "assistant"):
            meta["msgCount"] += 1
            if meta["cwd"] is None and obj.get("cwd"):
                meta["cwd"] = obj.get("cwd")
            if meta["gitBranch"] is None and obj.get("gitBranch"):
                meta["gitBranch"] = obj.get("gitBranch")
            if meta["version"] is None and obj.get("version"):
                meta["version"] = obj.get("version")
            m = (obj.get("message") or {}).get("model")
            if m:
                models.add(m)
    meta["models"] = sorted(models)
    _meta_cache[key] = meta
    return meta


def list_sessions():
    """SESSIONS_ROOT 하위 모든 .jsonl 의 메타 목록."""
    out = []
    if not SESSIONS_ROOT.exists():
        return out
    for sub in sorted(SESSIONS_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        try:
            files = sorted(sub.glob("*.jsonl"))
        except PermissionError:
            continue
        for fp in files:
            try:
                out.append(scan_meta(fp))
            except (PermissionError, OSError):
                continue
    return out


def _normalize_content(content):
    """message.content (문자열 또는 리스트) 를 블록 배열로 정규화."""
    blocks = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
        return blocks
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                blocks.append({"kind": "text", "text": item})
            continue
        it = item.get("type")
        if it == "text":
            txt = item.get("text", "")
            if txt.strip():
                blocks.append({"kind": "text", "text": txt})
        elif it == "thinking":
            blocks.append({"kind": "thinking",
                           "text": item.get("thinking") or item.get("text") or ""})
        elif it == "tool_use":
            blocks.append({"kind": "tool_use",
                           "name": item.get("name", "?"),
                           "input": item.get("input", {})})
        elif it == "tool_result":
            c = item.get("content")
            if isinstance(c, list):
                parts = []
                for p in c:
                    if isinstance(p, dict):
                        parts.append(p.get("text", "") if p.get("type") == "text"
                                     else f"[{p.get('type')}]")
                    else:
                        parts.append(str(p))
                c = "\n".join(parts)
            blocks.append({"kind": "tool_result",
                           "text": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False),
                           "is_error": bool(item.get("is_error"))})
        elif it == "image":
            blocks.append({"kind": "image"})
    return blocks


# user role 이지만 사람이 친 입력이 아닌 것들. Claude Code 는 이걸 전부 user 로 적는다.
#   - task-notification : 백그라운드 작업 완료 알림 (origin/promptSource 로 확실히 구분된다)
#   - command-* / local-command-* : 슬래시 명령 봉투와 그 출력
#   - system-reminder / ide_opened_file : 하니스 주입
# 앞의 둘과 달리 슬래시 명령 계열은 구분용 필드가 없어 선두 태그로 판별할 수밖에 없다.
INJECT_TAG_RE = re.compile(
    r"^\s*<(system-reminder|ide_opened_file|command-name|command-message|command-args"
    r"|local-command-stdout|local-command-caveat|task-notification)\b")


def _inject_kind(obj, blocks):
    """주입된 메시지면 그 종류를, 진짜 사용자 입력이면 None 을 돌려준다."""
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return "task-notification"
    if obj.get("promptSource") == "system":
        return "task-notification"
    if obj.get("isCompactSummary"):      # 컨텍스트가 넘쳐 압축될 때 하니스가 넣는 요약
        return "compact"
    # 태그가 isMeta 보다 구체적이다(local-command-caveat 는 isMeta 도 True 다).
    # 사용자가 친 글 뒤에 <system-reminder> 가 덧붙는 경우가 있어서 첫 텍스트 블록만 본다.
    for b in blocks:
        if b["kind"] == "text":
            m = INJECT_TAG_RE.match(b["text"])
            if m:
                return m.group(1)
            break
    return "meta" if obj.get("isMeta") else None


def _find_session_file(sid):
    """sessionId 로 .jsonl 파일 경로 찾기 (전 디렉토리 탐색)."""
    if not sid:
        return None
    if not SESSIONS_ROOT.exists():
        return None
    for sub in SESSIONS_ROOT.iterdir():
        if not sub.is_dir():
            continue
        cand = sub / f"{sid}.jsonl"
        if cand.is_file():
            return cand
    return None


def _session_line_uuids(path):
    """파일의 모든 라인 uuid 집합 (상속 메시지 식별용)."""
    out = set()
    for obj in _iter_lines(path):
        if obj and obj.get("uuid"):
            out.add(obj["uuid"])
    return out


def parse_session(path):
    """단일 세션 파일을 타임라인 이벤트 배열로 파싱.

    분기(/branch) 세션이면 부모와 uuid를 공유하는 '상속' 메시지를 잘라내고
    분기점 이후 메시지만 반환한다. 토큰/도구 통계도 함께 계산한다.
    """
    meta = scan_meta(path)
    parent_uuids = set()
    parent_meta = None
    if meta.get("parentSessionId"):
        pf = _find_session_file(meta["parentSessionId"])
        if pf:
            parent_uuids = _session_line_uuids(pf)
            try:
                parent_meta = scan_meta(pf)
            except OSError:
                parent_meta = None

    events = []
    bad = 0
    inherited = 0
    started = False  # 분기점 이후 시작 여부
    stats = {"user": 0, "assistant": 0,
             "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheCreate": 0},
             "tools": {}, "firstTs": None, "lastTs": None}
    for obj in _iter_lines(path):
        if obj is None:
            bad += 1
            continue
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        # 분기 세션: 부모와 공유하는(=상속) 메시지는 분기점 전까지만 존재. 건너뛴다.
        if parent_uuids and not started:
            if obj.get("uuid") in parent_uuids:
                inherited += 1
                continue
            started = True
        msg = obj.get("message") or {}
        blocks = _normalize_content(msg.get("content"))
        if not blocks:
            continue
        ts = obj.get("timestamp")
        if ts:
            if stats["firstTs"] is None:
                stats["firstTs"] = ts
            stats["lastTs"] = ts
        stats[t] = stats.get(t, 0) + 1
        if t == "assistant":
            u = msg.get("usage") or {}
            stats["tokens"]["input"] += u.get("input_tokens", 0) or 0
            stats["tokens"]["output"] += u.get("output_tokens", 0) or 0
            stats["tokens"]["cacheRead"] += u.get("cache_read_input_tokens", 0) or 0
            stats["tokens"]["cacheCreate"] += u.get("cache_creation_input_tokens", 0) or 0
        for b in blocks:
            if b["kind"] == "tool_use":
                stats["tools"][b["name"]] = stats["tools"].get(b["name"], 0) + 1
        events.append({
            "role": t,
            "ts": ts,
            "model": msg.get("model"),
            "blocks": blocks,
            "isSidechain": bool(obj.get("isSidechain")),
            "isMeta": bool(obj.get("isMeta")),   # 하니스 주입(스킬 본문/시스템 컨텍스트)
            # user 로 적혀 있지만 사람이 친 게 아닌 것의 종류. None 이면 진짜 사용자 입력.
            "inject": _inject_kind(obj, blocks) if t == "user" else None,
        })

    fork = None
    if meta.get("parentSessionId"):
        fork = {
            "parentSessionId": meta["parentSessionId"],
            "parentTitle": ((parent_meta or {}).get("customTitle")
                            or (parent_meta or {}).get("aiTitle")) if parent_meta else None,
            "parentFile": str(_find_session_file(meta["parentSessionId"]) or ""),
            "inheritedCount": inherited,
            "found": bool(parent_uuids),
        }
    return {"meta": meta, "events": events, "badLines": bad,
            "stats": stats, "fork": fork}


def _iter_visible_messages(path, meta=None):
    """타임라인에 실제로 표시되는 user/assistant 메시지 obj 만 yield.

    분기(/branch) 세션이면 부모와 uuid 를 공유하는 '상속' 메시지를 건너뛴다.
    parse_session 의 트리밍 규칙과 같게 맞춰, 검색 결과가 화면에 보이지도 않는
    메시지를 가리키지 않도록 한다.
    """
    if meta is None:
        meta = scan_meta(path)
    parent_uuids = set()
    if meta.get("parentSessionId"):
        pf = _find_session_file(meta["parentSessionId"])
        if pf:
            parent_uuids = _session_line_uuids(pf)
    started = False
    for obj in _iter_lines(path):
        if obj is None or obj.get("type") not in ("user", "assistant"):
            continue
        if parent_uuids and not started:
            if obj.get("uuid") in parent_uuids:
                continue
            started = True
        yield obj


def _searchable_texts(obj):
    """한 메시지에서 전체 검색이 훑을 텍스트 조각들을 반환.

    타임라인에 실제로 렌더되는 것과 같은 대상을 본다 — 본문 텍스트 · thinking ·
    도구 호출 이름/입력 JSON · 도구 결과.
    """
    texts = []
    for b in _normalize_content((obj.get("message") or {}).get("content")):
        kind = b["kind"]
        if kind in ("text", "thinking", "tool_result"):
            if b.get("text"):
                texts.append(b["text"])
        elif kind == "tool_use":
            if b.get("name"):
                texts.append(b["name"])
            try:
                texts.append(json.dumps(b.get("input", {}), ensure_ascii=False, indent=2))
            except (TypeError, ValueError):
                texts.append(str(b.get("input")))
    return texts


def search_all(term):
    """전 세션 텍스트를 훑어 매칭 세션 + 스니펫 반환.

    건수(hits)는 블록당 1회가 아니라 '출현 횟수'로 센다.
    """
    term_l = term.lower()
    results = []
    for meta in list_sessions():
        hits = 0
        snippet = None
        for obj in _iter_visible_messages(Path(meta["file"]), meta):
            for txt in _searchable_texts(obj):
                low = txt.lower()
                c = low.count(term_l)
                if not c:
                    continue
                hits += c
                if snippet is None:
                    idx = low.find(term_l)
                    start = max(0, idx - 60)
                    snippet = ("…" if start else "") + txt[start:idx + 120].replace("\n", " ")
        if hits:
            r = dict(meta)
            r["hits"] = hits
            r["snippet"] = snippet
            results.append(r)
    results.sort(key=lambda r: r["hits"], reverse=True)
    return results


def _safe_session_path(raw):
    """file 파라미터를 SESSIONS_ROOT 하위로 제한 (경로 탈출 차단).

    쓰기(연장·휴지통 이동) 대상은 반드시 이 함수를 거친다.
    """
    p = Path(raw).resolve()
    try:
        p.relative_to(SESSIONS_ROOT)
    except ValueError:
        return None
    if not p.is_file() or p.suffix != ".jsonl":
        return None
    return p


VENDOR_ROOT = (HERE / "vendor").resolve()


def _safe_vendor_path(name):
    """/vendor/<name> 을 레포의 vendor/ 안으로 제한. 하위 디렉토리는 두지 않는다."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = (VENDOR_ROOT / name).resolve()
    try:
        p.relative_to(VENDOR_ROOT)
    except ValueError:
        return None
    return p if p.is_file() else None


def _safe_readable_path(raw):
    """읽기 전용 경로. 휴지통 항목도 복원 전에 내용을 확인할 수 있어야 한다."""
    p = _safe_session_path(raw)
    if p:
        return p
    p = Path(raw).resolve()
    try:
        p.relative_to(TRASH_ROOT)
    except ValueError:
        return None
    if not p.is_file() or p.suffix != ".jsonl":
        return None
    return p


def touch_sessions(files):
    """세션 파일들의 mtime 을 현재로 갱신(내용 불변) → Claude Code 자동삭제 시계 리셋."""
    results = []
    for raw in files or []:
        p = _safe_session_path(raw)
        if not p:
            results.append({"file": raw, "ok": False, "error": "invalid file"})
            continue
        try:
            os.utime(p, None)  # atime/mtime 을 현재로. 파일 내용은 건드리지 않는다.
            results.append({"file": raw, "ok": True, "mtime": p.stat().st_mtime})
        except OSError as e:
            results.append({"file": raw, "ok": False, "error": str(e)})
    return {"results": results}


# ---------- 휴지통 ----------
# 삭제는 2단계다. ① 휴지통으로 이동(복원 가능) ② 휴지통에서 영구 삭제.
# 항목 하나는 TRASH_ROOT/<id>/ 폴더에 세션 .jsonl 과 사이드카 디렉토리를 함께 담고,
# 원래 경로 등 복원에 필요한 정보는 index.json 에 적어둔다.


def _read_trash_index():
    if not TRASH_INDEX.is_file():
        return []
    try:
        data = json.loads(TRASH_INDEX.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def _write_trash_index(items):
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = TRASH_INDEX.with_name(TRASH_INDEX.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRASH_INDEX)   # 중간에 죽어도 인덱스가 깨지지 않게


def _trash_entry_dir(entry_id):
    """휴지통 항목 폴더. id 를 그대로 경로에 붙이므로 탈출을 막는다."""
    if not entry_id or not TRASH_ID_RE.fullmatch(str(entry_id)):
        return None
    p = (TRASH_ROOT / entry_id).resolve()
    try:
        p.relative_to(TRASH_ROOT)
    except ValueError:
        return None
    return p


def trash_sessions(files):
    """세션을 휴지통으로 옮긴다. 사이드카 디렉토리도 같이 옮겨 복원에 대비한다."""
    items = _read_trash_index()
    known = {it.get("id") for it in items}
    results = []
    for raw in files or []:
        p = _safe_session_path(raw)
        if not p:
            results.append({"file": raw, "ok": False, "error": "invalid file"})
            continue
        try:
            meta = scan_meta(p)
        except OSError:
            meta = {}
        entry_id, n = p.stem, 1
        while entry_id in known or (TRASH_ROOT / entry_id).exists():
            entry_id = "%s-%d" % (p.stem, n)
            n += 1
        dest = TRASH_ROOT / entry_id
        try:
            dest.mkdir(parents=True, exist_ok=False)
            shutil.move(str(p), str(dest / p.name))
            sidecar = p.with_suffix("")          # .../<uuid>.jsonl → .../<uuid>
            if sidecar.is_dir():
                shutil.move(str(sidecar), str(dest / sidecar.name))
        except OSError as e:
            results.append({"file": raw, "ok": False, "error": str(e)})
            continue
        known.add(entry_id)
        items.append({
            "id": entry_id,
            "uuid": meta.get("uuid") or p.stem,
            "origFile": str(p),
            "name": p.name,
            "deletedAt": time.time(),
            "aiTitle": meta.get("aiTitle"),
            "customTitle": meta.get("customTitle"),
            "lastPrompt": meta.get("lastPrompt"),
            "lastTs": meta.get("lastTs"),
            "msgCount": meta.get("msgCount") or 0,
            "size": meta.get("size") or 0,
            "models": meta.get("models") or [],
            "gitBranch": meta.get("gitBranch"),
            "cwd": meta.get("cwd"),
        })
        results.append({"file": raw, "ok": True, "id": entry_id})
    _write_trash_index(items)
    return {"results": results}


def list_trash():
    """휴지통 목록. 실제 파일이 사라진 항목은 인덱스에서 걷어낸다."""
    items = _read_trash_index()
    out, kept, changed = [], [], False
    for it in items:
        d = _trash_entry_dir(it.get("id"))
        f = (d / it["name"]) if (d and it.get("name")) else None
        if not f or not f.is_file():
            changed = True
            continue
        kept.append(it)
        entry = dict(it)
        entry["file"] = str(f)
        out.append(entry)
    if changed:
        _write_trash_index(kept)
    out.sort(key=lambda e: e.get("deletedAt") or 0, reverse=True)
    return out


def restore_trash(ids):
    """휴지통 항목을 원래 경로로 되돌린다. 같은 이름이 이미 있으면 덮어쓰지 않는다."""
    items = _read_trash_index()
    by_id = {it.get("id"): it for it in items}
    results, done = [], set()
    for tid in ids or []:
        it = by_id.get(tid)
        d = _trash_entry_dir(tid)
        if not it or not d or not d.is_dir():
            results.append({"id": tid, "ok": False, "error": "없는 항목"})
            continue
        try:
            dest = Path(it.get("origFile") or "").resolve()
            dest.relative_to(SESSIONS_ROOT)     # 복원 위치는 세션 루트 안이어야 한다
        except (ValueError, OSError):
            results.append({"id": tid, "ok": False, "error": "복원 위치가 올바르지 않음"})
            continue
        if dest.exists():
            results.append({"id": tid, "ok": False, "error": "같은 이름의 세션이 이미 있음"})
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d / it["name"]), str(dest))
            sidecar = d / dest.stem
            if sidecar.is_dir():
                shutil.move(str(sidecar), str(dest.with_suffix("")))
            shutil.rmtree(d, ignore_errors=True)
        except OSError as e:
            results.append({"id": tid, "ok": False, "error": str(e)})
            continue
        done.add(tid)
        results.append({"id": tid, "ok": True, "file": str(dest)})
    if done:
        _write_trash_index([it for it in items if it.get("id") not in done])
    return {"results": results}


def purge_trash(ids=None, purge_all=False):
    """휴지통에서 영구 삭제한다. 여기서 지우면 복구할 수 없다."""
    items = _read_trash_index()
    targets = [it.get("id") for it in items] if purge_all else list(ids or [])
    results, done = [], set()
    for tid in targets:
        d = _trash_entry_dir(tid)
        if not d:
            results.append({"id": tid, "ok": False, "error": "잘못된 항목"})
            continue
        try:
            if d.is_dir():
                shutil.rmtree(d)
            done.add(tid)
            results.append({"id": tid, "ok": True})
        except OSError as e:
            results.append({"id": tid, "ok": False, "error": str(e)})
    if done:
        _write_trash_index([it for it in items if it.get("id") not in done])
    return {"results": results}


# ---------- 설정(settings.json) 읽기/쓰기 ----------
# cc-explorer 가 의미 있게 쓰는 설정: 파일/키에 없어도 드로어에 기본값으로 항상 노출하고
# (파일이 없으면 생성해서) 편집을 허용한다.
#   - cleanupPeriodDays: 세션 자동 삭제 기간(일). 삭제 카운트다운 배지의 기준값. 기본 30.
#   - showThinkingSummaries: thinking 기록 여부(뷰어가 thinking 블록을 표시). 켜기 번거로워 기본 노출.
KNOWN_SETTINGS = [
    {"key": "cleanupPeriodDays", "type": "number", "default": 30},
    {"key": "showThinkingSummaries", "type": "bool", "default": False},
]


def _known_default_items(present):
    """파일에 없는 알려진 설정을 기본값 + absent 로 항목화."""
    out = []
    for spec in KNOWN_SETTINGS:
        if (spec["key"],) not in present:
            out.append({"path": [spec["key"]], "label": spec["key"],
                        "value": spec["default"], "type": spec["type"], "absent": True})
    return out


def _coerce_int(value):
    """number 설정 입력을 정수로 강제. 실패 시 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _flatten_settings(obj, path=None):
    """중첩 dict 를 leaf 리스트로 평탄화. 각 항목: {path(list), label, value, type}."""
    path = path or []
    out = []
    for k, v in obj.items():
        p = path + [k]
        if isinstance(v, dict):
            out.extend(_flatten_settings(v, p))
        else:
            out.append({
                "path": p,
                "label": ".".join(p),
                "value": v,
                "type": ("bool" if isinstance(v, bool)
                         else "number" if isinstance(v, (int, float))
                         else "string" if isinstance(v, str)
                         else "other"),
            })
    return out


def read_settings():
    exists = SETTINGS_FILE.is_file()
    if exists:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            return {"path": str(SETTINGS_FILE), "exists": True,
                    "error": f"파싱 실패: {e}", "items": []}
        if not isinstance(data, dict):
            return {"path": str(SETTINGS_FILE), "exists": True,
                    "error": "최상위가 객체가 아님", "items": []}
        items = _flatten_settings(data)
    else:
        # 파일이 없어도 알려진 설정은 기본값으로 노출한다(생성 허용).
        items = []
    present = {tuple(it["path"]) for it in items}
    items.extend(_known_default_items(present))
    return {"path": str(SETTINGS_FILE), "exists": exists, "items": items}


def set_setting(path_list, value):
    """설정 leaf 를 변경한다.

    - 기존 leaf: 같은 타입(bool/str/number)으로만 변경.
    - 없는 leaf: 알려진 최상위 설정(KNOWN_SETTINGS)만 타입에 맞게 생성.
    - 파일이 없어도 알려진 설정이면 새로 만든다. .bak 백업 후 다른 키를 보존하며 기록.
    """
    if not isinstance(path_list, list) or not path_list:
        return {"ok": False, "error": "잘못된 경로"}
    known = None
    if len(path_list) == 1:
        known = next((s for s in KNOWN_SETTINGS if s["key"] == path_list[0]), None)

    file_exists = SETTINGS_FILE.is_file()
    if file_exists:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": f"읽기 실패: {e}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "최상위가 객체가 아님"}
    elif known:
        data = {}
    else:
        return {"ok": False, "error": "설정 파일 없음"}

    # 경로를 따라가며 leaf 컨테이너 확인
    node = data
    for k in path_list[:-1]:
        if not isinstance(node, dict) or k not in node:
            return {"ok": False, "error": "존재하지 않는 키"}
        node = node[k]
    leaf = path_list[-1]
    if not isinstance(node, dict):
        return {"ok": False, "error": "존재하지 않는 키"}

    if leaf in node:
        existing = node[leaf]
        if isinstance(existing, bool):
            if not isinstance(value, bool):
                return {"ok": False, "error": "boolean 값이어야 함"}
        elif isinstance(existing, str):
            if not isinstance(value, str):
                return {"ok": False, "error": "문자열 값이어야 함"}
        elif isinstance(existing, (int, float)):
            v = _coerce_int(value)
            if v is None:
                return {"ok": False, "error": "정수 값이어야 함"}
            value = v
        else:
            return {"ok": False, "error": "편집 불가 타입"}
    else:
        # 없는 키: 알려진 설정만 타입에 맞게 생성 허용
        if not known:
            return {"ok": False, "error": "존재하지 않는 키"}
        if known["type"] == "bool":
            if not isinstance(value, bool):
                return {"ok": False, "error": "boolean 값이어야 함"}
        elif known["type"] == "number":
            v = _coerce_int(value)
            if v is None:
                return {"ok": False, "error": "정수 값이어야 함"}
            value = v

    # cleanupPeriodDays 범위 검증(1 이상)
    if path_list == ["cleanupPeriodDays"] and (not isinstance(value, int) or value < 1):
        return {"ok": False, "error": "1 이상의 정수여야 함"}

    node[leaf] = value
    try:
        if file_exists:
            # 쓰기 직전 백업 1부
            SETTINGS_FILE.with_suffix(".json.bak").write_text(
                SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"쓰기 실패: {e}"}
    return {"ok": True, "path": path_list, "value": value}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send_file(HERE / "index.html", "text/html; charset=utf-8")
            if u.path.startswith("/vendor/"):
                p = _safe_vendor_path(u.path[len("/vendor/"):])
                if not p:
                    return self.send_error(404)
                ctype = ("application/javascript; charset=utf-8" if p.suffix == ".js"
                         else "text/plain; charset=utf-8")
                return self._send_file(p, ctype)
            if u.path == "/api/projects":
                return self._send_json({
                    "version": APP_VERSION,
                    "root": str(SESSIONS_ROOT),
                    "exists": SESSIONS_ROOT.exists(),
                    "sessions": list_sessions(),
                })
            if u.path == "/api/session":
                raw = (q.get("file") or [""])[0]
                p = _safe_readable_path(raw)   # 휴지통 항목도 열어볼 수 있다
                if not p:
                    return self._send_json({"error": "invalid file"}, 400)
                return self._send_json(parse_session(p))
            if u.path == "/api/trash":
                return self._send_json({"root": str(TRASH_ROOT), "items": list_trash()})
            if u.path == "/api/search":
                term = (q.get("q") or [""])[0].strip()
                if len(term) < 2:
                    return self._send_json({"results": [], "note": "검색어 2자 이상"})
                return self._send_json({"results": search_all(term)})
            if u.path == "/api/settings":
                return self._send_json(read_settings())
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            try:
                body = self._read_json_body()
            except ValueError:
                return self._send_json({"ok": False, "error": "잘못된 JSON"}, 400)
            if u.path == "/api/settings/set":
                res = set_setting(body.get("path"), body.get("value"))
                return self._send_json(res, 200 if res.get("ok") else 400)
            if u.path == "/api/touch":
                return self._send_json(touch_sessions(body.get("files")))
            # 삭제는 휴지통 경유 2단계. 세션 파일을 곧바로 지우는 경로는 두지 않는다.
            if u.path == "/api/trash":
                return self._send_json(trash_sessions(body.get("files")))
            if u.path == "/api/trash/restore":
                return self._send_json(restore_trash(body.get("ids")))
            if u.path == "/api/trash/purge":
                return self._send_json(purge_trash(body.get("ids"), bool(body.get("all"))))
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(e)}, 500)


def main():
    if not SESSIONS_ROOT.exists():
        print(f"[!] 세션 폴더가 없습니다: {SESSIONS_ROOT}", file=sys.stderr)
    port = 8765
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("[!] 사용 가능한 포트를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print(f"Claude Code 세션 뷰어 → {url}")
    print(f"  세션 루트: {SESSIONS_ROOT}")
    print("  종료: Ctrl+C")
    if "--no-open" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
