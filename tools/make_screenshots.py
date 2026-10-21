#!/usr/bin/env python3
"""README 스크린샷을 데모 데이터로 생성한다.

    python3 tools/make_screenshots.py            # docs/readme_*.png 갱신

실제 세션 대신 가짜 데이터를 쓴다 — 공개 저장소에 올라가는 이미지라
개인 기록(세션 제목·경로·대화 내용)이 새어 나가면 안 된다.
headless Chrome 이 필요하다(google-chrome 또는 chromium).
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent.parent
DOCS = HERE / "docs"
TS = "2026-03-14T10:00:00Z"
TS2 = "2026-03-14T10:42:00Z"


def sess(uuid, title, prompt, cwd, branch, msgs, days_old=0, models=("opus-5",)):
    import time
    return {
        "file": "/home/you/.claude/projects/%s/%s.jsonl" % (cwd.strip("/").replace("/", "-"), uuid),
        "uuid": uuid, "aiTitle": title, "lastPrompt": prompt, "cwd": cwd,
        "gitBranch": branch, "version": "2.0.1", "firstTs": TS, "lastTs": TS2,
        "msgCount": msgs, "models": list(models), "size": msgs * 3200,
        "mtime": time.time() - days_old * 86400, "badLines": 0,
        "parentSessionId": None, "forkedAtMessage": None,
    }


SESSIONS = [
    sess("a1b2c3d4-0001-4000-8000-000000000001", "렌더러 캐시 무효화 버그 추적",
         "캐시 키에 mtime 을 넣어보자", "/home/you/work/renderer", "main", 412),
    sess("a1b2c3d4-0002-4000-8000-000000000002", "빌드 파이프라인 정리",
         "CI 에서 캐시가 안 먹는 이유 확인", "/home/you/work/renderer", "main", 188, 23),
    sess("a1b2c3d4-0003-4000-8000-000000000003", "파서 성능 개선",
         "스트리밍 파싱으로 바꾸면 얼마나 빨라질까", "/home/you/work/parser", "feat/stream", 1204),
    sess("a1b2c3d4-0004-4000-8000-000000000004", "설정 스키마 마이그레이션",
         "v1 설정을 v2 로 옮기는 스크립트", "/home/you/work/parser", "feat/stream", 96, 28),
    sess("a1b2c3d4-0005-4000-8000-000000000005", "문서 사이트 배포",
         "정적 빌드 결과를 어디에 올릴지", "/home/you/docs", "main", 54, 5),
]

MD = """캐시 키에 `mtime` 을 넣으면 파일이 바뀔 때마다 자동으로 무효화됩니다.

## 바꾼 것

- 캐시 키를 `(path, mtime, size)` 로 확장
- 적중 실패 시에만 다시 읽도록 정리
- 키가 늘어나는 건 LRU 로 제한

```python
def scan_meta(path):
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    cached = _meta_cache.get(key)
    if cached is not None:
        return cached          # 파일이 그대로면 재파싱하지 않는다
    meta = _parse(path)
    _meta_cache[key] = meta
    return meta
```

측정 결과입니다.

| 항목 | 이전 | 이후 |
|---|---:|---:|
| 첫 로드 | 2,180ms | 410ms |
| 재방문 | 2,150ms | 12ms |

> 파일이 바뀌면 키가 달라지므로 따로 무효화할 필요가 없습니다.

```bash
# 캐시 적중률 확인
python3 -m tools.cachestat --since 1h | tail -20
```
"""

DETAIL = {
    "meta": SESSIONS[0], "badLines": 0, "fork": None,
    "stats": {"user": 2, "assistant": 3,
              "tokens": {"input": 137500, "output": 4_000_000,
                         "cacheRead": 1_801_700_000, "cacheCreate": 122_600_000},
              "tools": {"Bash": 1236, "Edit": 83, "Read": 73, "Write": 30},
              "firstTs": TS, "lastTs": TS2},
    "events": [
        {"role": "user", "ts": TS, "model": None, "isSidechain": False, "isMeta": False,
         "inject": None, "blocks": [{"kind": "text",
          "text": "렌더러가 같은 파일을 매번 다시 읽는 것 같아. 캐시 키에 mtime 을 넣어보자."}]},
        {"role": "assistant", "ts": TS, "model": "opus-5", "isSidechain": False,
         "isMeta": False, "inject": None,
         "blocks": [{"kind": "thinking", "text": "캐시 키 설계를 먼저 확인한다."}]},
        {"role": "assistant", "ts": TS, "model": "opus-5", "isSidechain": False,
         "isMeta": False, "inject": None,
         "blocks": [{"kind": "tool_use", "name": "Read",
                     "input": {"file_path": "/home/you/work/renderer/cache.py",
                               "limit": 40}}]},
        {"role": "user", "ts": TS, "model": None, "isSidechain": False, "isMeta": False,
         "inject": None, "blocks": [{"kind": "tool_result", "is_error": False,
          "text": "     1\timport time\n     2\tfrom pathlib import Path\n"
                  "     3\t\n     4\t_meta_cache = {}\n"}]},
        {"role": "assistant", "ts": TS2, "model": "opus-5", "isSidechain": False,
         "isMeta": False, "inject": None, "blocks": [{"kind": "text", "text": MD}]},
    ],
}

TRASH = [{
    "id": "a1b2c3d4-0009-4000-8000-000000000009",
    "uuid": "a1b2c3d4-0009-4000-8000-000000000009",
    "origFile": "/home/you/.claude/projects/home-you-work-old/x.jsonl",
    "name": "x.jsonl", "deletedAt": 1773000000.0, "aiTitle": "실험용 임시 세션",
    "lastPrompt": "이건 지워도 됨", "lastTs": TS, "msgCount": 23, "size": 70000,
    "models": ["opus-5"], "gitBranch": "main", "cwd": "/home/you/work/old",
    "file": "/trash/x/x.jsonl",
}]

SETTINGS = {"path": "/home/you/.claude/settings.json", "exists": True, "items": [
    {"path": ["cleanupPeriodDays"], "label": "cleanupPeriodDays", "value": 30,
     "type": "number"},
    {"path": ["showThinkingSummaries"], "label": "showThinkingSummaries", "value": True,
     "type": "bool"},
]}

STUB = """<script>
const MOCK = %s;
window.fetch = (u, o) => {
  const s = String(u);
  let b = {};
  if (s.startsWith("/api/trash")) b = {root:"/trash", items: MOCK.trash};
  else if (s.startsWith("/api/projects")) b = MOCK.projects;
  else if (s.startsWith("/api/settings")) b = MOCK.settings;
  else if (s.startsWith("/api/session")) b = MOCK.detail;
  else if (s.startsWith("/api/search")) b = {results: []};
  return Promise.resolve({json: () => Promise.resolve(b)});
};
localStorage.clear();
localStorage.setItem("ccsv-theme", "__THEME__");
localStorage.setItem("ccsv-favs", JSON.stringify(["%s"]));
window.addEventListener("load", () => setTimeout(async () => {
  applyTheme("__THEME__");
  __ACTION__
}, 150));
</script>
</head>"""


def build(name, theme, action, size, out):
    mock = {"projects": {"version": "3", "root": "/home/you/.claude/projects",
                         "exists": True, "sessions": SESSIONS},
            "detail": DETAIL, "trash": TRASH, "settings": SETTINGS}
    html = (HERE / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="/vendor/highlight.bundle.js"',
                        'src="highlight.bundle.js"', 1)
    stub = (STUB % (json.dumps(mock, ensure_ascii=False), SESSIONS[0]["uuid"])) \
        .replace("__THEME__", theme).replace("__ACTION__", action)
    page = pathlib.Path(out) / (name + ".html")
    page.write_text(html.replace("</head>", stub, 1), encoding="utf-8")
    shutil.copy(HERE / "vendor" / "highlight.bundle.js", out)
    png = DOCS / (name + ".png")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") \
        or shutil.which("chromium-browser")
    if not chrome:
        sys.exit("google-chrome 또는 chromium 이 필요합니다")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--window-size=%d,%d" % size, "--virtual-time-budget=15000",
                    "--screenshot=%s" % png, "file://%s" % page],
                   check=True, capture_output=True)
    print("  %s  (%.0f KB)" % (png.name, png.stat().st_size / 1024))


OPEN_FIRST = """
  const dirs = document.querySelectorAll("#tree .row:not(.favrow)");
  if (dirs[1]) dirs[1].click();
  await new Promise(r => setTimeout(r, 200));
  const c = document.querySelector(".sess"); if (c) c.click();
  await new Promise(r => setTimeout(r, 600));
  document.querySelector("#detail").scrollTop = 0;
"""

SHOTS = [
    # ① 기본 3분할 + 요약 카드
    ("readme_1", "dark", OPEN_FIRST, (1500, 980)),
    # ② 마크다운 렌더 + 코드 하이라이팅 (맨 아래로 — 열면 최신이 보이는 그 화면)
    ("readme_2", "dark", OPEN_FIRST.replace(
        'document.querySelector("#detail").scrollTop = 0;',
        'scrollToBottom(document.querySelector("#detail"));\n'
        '  await new Promise(r => setTimeout(r, 900));'), (1500, 980)),
    # ③ 같은 화면을 라이트로 + 설정 드로어
    ("readme_3", "light", OPEN_FIRST + """
  document.querySelector("#settingsBtn").click();
  await new Promise(r => setTimeout(r, 400));
""", (1500, 760)),
    # ④ 즐겨찾기 · 휴지통 · 기간 필터
    ("readme_4", "dark", """
  document.querySelector("#tree .favrow:not(.trashrow)").click();
  await new Promise(r => setTimeout(r, 300));
  document.querySelector("#dateBtn").click();
  await new Promise(r => setTimeout(r, 200));
""", (1500, 560)),
]


def main():
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ccshot-") as tmp:
        print("스크린샷 생성 (데모 데이터):")
        for name, theme, action, size in SHOTS:
            build(name, theme, action, size, tmp)


if __name__ == "__main__":
    main()
