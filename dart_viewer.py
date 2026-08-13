"""
DART 공시원문(XML) → 읽을 수 있는 HTML 변환기
================================================

[왜 필요한가]
DART가 주는 공시원문은 확장자가 .xml 이지만 표준 XML이 아니다.
자체 스키마(dart4.xsd) 기반의 SGML에 가까운 형식이라 그냥 열면 다음과 같이 된다.

  · 브라우저      : 파싱 에러 또는 태그 트리만 표시
  · 메모장/워드   : 태그가 그대로 보임
  · 엑셀          : 표가 깨짐

실제로 표준 XML 파서(ElementTree)로는 읽히지 않는다. 이유가 두 가지다.
  1) 본문의 'R&D' 처럼 &가 이스케이프되지 않은 채 들어있다 (&amp; 여야 함)
  2) <PGBRK>, <IMG> 처럼 닫는 태그가 없는 SGML식 빈 태그가 있다

다행히 구조 자체는 HTML과 거의 같다(TABLE/TR/TD/TH).
그래서 엄격하게 파싱하지 않고, 태그 이름만 HTML로 바꿔치기한 뒤
브라우저의 관대한 파서에 맡기는 방식이 가장 안전하다.
"""

import io
import os
import re
import html
import zipfile


# ─────────────────────────────────────────────────────────────
# 인코딩 판별
# ─────────────────────────────────────────────────────────────

def decode(raw):
    """
    공시원문 바이트를 문자열로 디코드한다.

    ★ XML 선언에 적힌 인코딩을 믿으면 안 된다.
      2021년 이전 공시는 선언이 encoding="utf-8" 인데 실제 바이트는 CP949다.
      예) 2020년 삼성전자 사업보고서
          선언: <?xml version="1.0" encoding="utf-8"?>
          실제: b'\\xbb\\xe7\\xbe\\xf7\\xba\\xb8\\xb0\\xed\\xbc\\xad'  = CP949의 '사업보고서'

      선언을 믿고 UTF-8로 읽으면 한글이 전부 깨진다(���������).
      그래서 선언은 무시하고 실제로 디코드를 시도해서 되는 것을 쓴다.
    """
    for enc in ("utf-8", "cp949"):     # cp949는 euc-kr을 포함하는 상위집합
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # 둘 다 안 되면 깨진 글자만 대체하고 진행 (전체를 못 읽는 것보다 낫다)
    return raw.decode("utf-8", errors="replace"), "utf-8(일부손실)"


# ─────────────────────────────────────────────────────────────
# 태그 매핑
# ─────────────────────────────────────────────────────────────

# 태그와 내용을 통째로 버릴 것 (문서 메타데이터라 본문에 보일 필요 없음)
DROP_BLOCK = ["SUMMARY", "FORMULA-VERSION"]

# 태그만 없애고 내용은 남길 것 (의미 없는 껍데기)
UNWRAP = {
    "DOCUMENT", "BODY", "LIBRARY", "EXTRACTION", "TABLE-GROUP",
    "CORRECTION", "COVER", "PART", "ARTICLE",
}

# DART 태그 → HTML 태그
TAG_MAP = {
    "COVER-TITLE": "h1",
    "DOCUMENT-NAME": "h1",
    "COMPANY-NAME": "div",
    "TITLE": "h2",
    "SECTION-1": "section",
    "SECTION-2": "section",
    "TE": "td",          # DART 고유 셀 변형
    "TU": "td",          # 단위(금액/기간) 정보를 담은 셀
    "PGBRK": "hr",
    "IMAGE": "figure",
    "IMG-CAPTION": "figcaption",
    "P": "p",
    "SPAN": "span",
}

# 그대로 둘 HTML 표준 태그
KEEP = {
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "colgroup", "col", "a", "br", "hr", "b", "i", "u", "em", "strong",
    "ul", "ol", "li", "sub", "sup", "img", "p", "span", "div",
    "h1", "h2", "h3", "h4", "section", "figure", "figcaption",
}

# 닫는 태그가 없는 태그
VOID = {"br", "hr", "col", "img"}

# 살릴 속성만 화이트리스트로 (DART 고유 속성 ACLASS/AUNIT 등은 버린다.
# 특히 CLASS는 우리 CSS와 충돌하므로 반드시 제거한다.)
KEEP_ATTRS = {"rowspan", "colspan", "align", "valign"}

ATTR_RE = re.compile(r'([A-Za-z][\w\-]*)\s*=\s*"([^"]*)"')
TAG_RE = re.compile(r"<(/?)([A-Za-z][\w\-]*)((?:[^<>\"]|\"[^\"]*\")*)/?>")


def _convert_attrs(raw):
    out = []
    for name, value in ATTR_RE.findall(raw):
        if name.lower() in KEEP_ATTRS:
            out.append(f'{name.lower()}="{html.escape(value, quote=True)}"')
    return (" " + " ".join(out)) if out else ""


def _convert_tag(m):
    closing, name, attrs = m.group(1), m.group(2), m.group(3)
    upper = name.upper()

    if upper in UNWRAP:
        return ""

    target = TAG_MAP.get(upper, name.lower())
    if target not in KEEP:
        target = "div"        # 모르는 태그는 안전하게 div로

    if closing:
        return "" if target in VOID else f"</{target}>"

    return f"<{target}{_convert_attrs(attrs)}>"


CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: "Malgun Gothic", "맑은 고딕", -apple-system, sans-serif;
  font-size: 14px; line-height: 1.7; color: #1a1a1a; background: #fff;
  margin: 0; padding: 0 24px 80px;
}
.wrap { max-width: 1100px; margin: 0 auto; }
.docbar {
  position: sticky; top: 0; z-index: 10; background: #00338d; color: #fff;
  margin: 0 -24px 24px; padding: 12px 24px;
}
.docbar b { font-size: 15px; }
.docbar span { opacity: .85; font-size: 13px; margin-left: 10px; }
h1 { font-size: 22px; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #00338d; }
h2 { font-size: 17px; margin: 28px 0 10px; color: #00338d; }
h3 { font-size: 15px; margin: 20px 0 8px; }
p { margin: 8px 0; }
hr { border: none; border-top: 1px dashed #ccc; margin: 24px 0; }
/* 표: 원문 폭 지정을 무시하고 화면에 맞춘다. 넘치면 표 안에서만 가로 스크롤 */
.tablebox { overflow-x: auto; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
td, th {
  border: 1px solid #d0d5dd; padding: 5px 8px;
  vertical-align: middle; word-break: break-word;
}
th { background: #eef2f8; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfd; }
figure { margin: 16px 0; padding: 12px; background: #f6f7f9; border: 1px dashed #c8ccd4;
         color: #666; font-size: 13px; }
#toc {
  background: #f6f7f9; border: 1px solid #e0e4ea; border-radius: 6px;
  padding: 14px 18px; margin: 20px 0 32px;
}
#toc div { font-weight: 700; margin-bottom: 8px; color: #00338d; }
#toc a { display: block; color: #333; text-decoration: none; padding: 3px 0; font-size: 13px; }
#toc a:hover { text-decoration: underline; }
#toc a.lv2 { padding-left: 16px; color: #555; }
@media (prefers-color-scheme: dark) {
  body { background: #16181d; color: #e6e6e6; }
  th { background: #232833; } tr:nth-child(even) td { background: #1c1f26; }
  td, th { border-color: #333a46; }
  #toc { background: #1c1f26; border-color: #333a46; }
  #toc a { color: #cfd4dc; }
  h2, #toc div { color: #7aa7e8; }
  figure { background: #1c1f26; border-color: #333a46; color: #aaa; }
}
"""


def _meta(text, tag):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def convert(xml_text):
    """DART 원문 XML 문자열 → 완성된 HTML 문자열"""

    doc_name = _meta(xml_text, "DOCUMENT-NAME") or "공시원문"
    company = _meta(xml_text, "COMPANY-NAME")

    body = xml_text

    # 1) XML 선언과 메타데이터 블록 제거
    body = re.sub(r"<\?xml[^>]*\?>", "", body)
    for tag in DROP_BLOCK:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.S)

    # 2) 태그를 HTML로 변환
    body = TAG_RE.sub(_convert_tag, body)

    # 3) 표를 스크롤 가능한 상자로 감싼다 (원문 표는 화면보다 넓은 경우가 많다)
    body = body.replace("<table", '<div class="tablebox"><table')
    body = body.replace("</table>", "</table></div>")

    # 4) 제목에 앵커를 달아 목차를 만든다
    toc = []
    def anchor(m):
        level, attrs, inner = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"<[^>]+>", "", inner).strip()
        if not title:
            return m.group(0)
        aid = f"s{len(toc)}"
        toc.append((level, aid, title))
        return f'<h{level} id="{aid}"{attrs}>{inner}</h{level}>'

    body = re.sub(r"<h([12])([^>]*)>(.*?)</h\1>", anchor, body, flags=re.S)

    toc_html = ""
    if len(toc) > 1:
        links = "".join(
            f'<a href="#{aid}" class="lv{lv}">{html.escape(t)}</a>'
            for lv, aid, t in toc
        )
        toc_html = f'<nav id="toc"><div>목차</div>{links}</nav>'

    title = f"{company} {doc_name}".strip()
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<div class="docbar"><b>{html.escape(doc_name)}</b><span>{html.escape(company)}</span></div>
{toc_html}
{body}
</div></body></html>"""


def convert_file(xml_path, out_path=None):
    """XML 파일 하나를 HTML로 변환해 저장하고 경로를 돌려준다."""
    with open(xml_path, "rb") as f:
        text, _enc = decode(f.read())

    out_path = out_path or os.path.splitext(xml_path)[0] + "_보기.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(convert(text))
    return out_path


def convert_zip(zip_bytes, folder, stem=None):
    """
    다운로드한 zip 안의 XML을 모두 HTML로 변환한다.
    zip 하나에 본문 + 첨부문서가 여러 개 들어있는 경우가 있다.

    stem을 주면 '<stem>_보기.html' 로 저장한다(첨부가 여럿이면 _1, _2 를 붙인다).
    주지 않으면 예전처럼 zip 안의 원래 파일명을 따른다.
    """
    made = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
        for i, name in enumerate(xmls):
            text, _enc = decode(z.read(name))
            if stem:
                base = stem if len(xmls) == 1 else f"{stem}_{i + 1}"
            else:
                base = os.path.splitext(os.path.basename(name))[0]
            out = os.path.join(folder, base + "_보기.html")
            with open(out, "w", encoding="utf-8") as f:
                f.write(convert(text))
            made.append(out)
    return made


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python dart_viewer.py <공시원문.xml 또는 폴더>")
        sys.exit(1)

    target = sys.argv[1]
    if os.path.isdir(target):
        for root, _dirs, files in os.walk(target):
            for fn in files:
                if fn.lower().endswith(".xml"):
                    print(convert_file(os.path.join(root, fn)))
    else:
        print(convert_file(target))
