#!/usr/bin/env python3
"""highlight.js npm 패키지에서 vendor/highlight.bundle.js 를 만든다.

    npm pack highlight.js@11 && tar xzf highlight.js-*.tgz
    python3 tools/build_hljs.py ./package

lib/core.js 와 lib/languages/*.js 는 require() 가 없는 자립 CommonJS 파일이라
얇은 모듈 셈만 씌우면 그대로 브라우저에서 돈다.

미니파이는 하지 않는다 — 레포에 들어가는 코드는 읽을 수 있어야 한다.
로컬에서만 쓰는 도구라 237KB 는 문제가 되지 않는다.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent

# 세션에서 실제로 쓰인 언어(측정 기준) + 흔한 것 몇 개.
# 별칭(js/sh/zsh/ts/html 등)은 highlight.js 가 알아서 처리한다.
LANGS = [
    "bash", "c", "cpp", "css", "diff", "go", "groovy", "ini", "java",
    "javascript", "json", "markdown", "python", "rust", "sql",
    "typescript", "xml", "yaml", "powershell",
]


def main():
    if len(sys.argv) < 2:
        sys.exit("사용법: build_hljs.py <highlight.js 패키지 디렉토리>")
    src = pathlib.Path(sys.argv[1]).resolve()
    if not (src / "lib" / "core.js").is_file():
        sys.exit("lib/core.js 가 없습니다: %s" % src)

    version = json.loads((src / "package.json").read_text(encoding="utf-8"))["version"]
    missing = [l for l in LANGS if not (src / "lib" / "languages" / (l + ".js")).is_file()]
    if missing:
        sys.exit("없는 언어 파일: %s" % missing)

    def read(p):
        text = p.read_text(encoding="utf-8")
        if "require(" in text:
            sys.exit("%s 에 require() 가 있어 그대로 번들할 수 없습니다" % p.name)
        return text

    out = [
        "// highlight.js v%s — 브라우저 번들 (BSD 3-Clause, (c) 2006 Ivan Sagalaev)\n"
        "// https://github.com/highlightjs/highlight.js  ·  라이선스: vendor/highlight.LICENSE\n"
        "//\n"
        "// npm 패키지의 lib/core.js 와 lib/languages/*.js 를 그대로 이어붙이고 얇은\n"
        "// CommonJS 셈만 씌운 것이다(미니파이 안 함). 재생성: tools/build_hljs.py\n"
        "// 포함 언어: %s\n"
        "(function (global) {\n"
        '  "use strict";\n'
        "  var __mods = {};\n"
        "  function __def(name, factory) {\n"
        "    var module = { exports: {} };\n"
        "    factory(module, module.exports);\n"
        "    __mods[name] = module.exports;\n"
        "  }\n" % (version, ", ".join(LANGS))
    ]

    out += ["  __def('core', function (module, exports) {\n",
            read(src / "lib" / "core.js"), "\n  });\n"]
    for lang in LANGS:
        out += ["  __def(%s, function (module, exports) {\n" % json.dumps(lang),
                read(src / "lib" / "languages" / (lang + ".js")), "\n  });\n"]

    out.append("\n  var hljs = __mods['core'];\n  %s\n"
               "  hljs.configure({ ignoreUnescapedHTML: true });\n"
               "  global.hljs = hljs;\n})(window);\n"
               % "\n  ".join("hljs.registerLanguage(%s, __mods[%s]);"
                             % (json.dumps(l), json.dumps(l)) for l in LANGS))

    dest = HERE / "vendor" / "highlight.bundle.js"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(out), encoding="utf-8")
    (dest.parent / "highlight.LICENSE").write_text(
        (src / "LICENSE").read_text(encoding="utf-8"), encoding="utf-8")

    print("생성: %s (%.0f KB)" % (dest, dest.stat().st_size / 1024))
    print("언어 %d개: %s" % (len(LANGS), ", ".join(LANGS)))


if __name__ == "__main__":
    main()
