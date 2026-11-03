#!/usr/bin/env python3
"""pages_publish_filter — 공개 리포트 사이트 artifact 조립 (보관 기간 · 크기 예산 · 목록 다시 씀 · 링크 검사).

계기 (2026-11-03 전체 점검 E2 · 사용자 "공개는 최근 90일만, 그 이전은 저장소에만 보관 + 빌드 없는 배포 방식"):
  저장소(NixStock/nixstock-reports)에는 전부 남기고, GitHub Actions 배포 artifact(_site)에는 설정 창 안 날짜 번들 + 날짜 없는 공통 파일만.
  계획서 (프로젝트 저장소): claude/plans/plan_pages_build_wait_90d_actions_20261103.md

설정 = .github/pages_publish.json 하나 (키 없음 = 실패 · 기본값 없음):
  window_days   창 (달력일 · 기준일 = 오늘(tz) − window_days · 그 날짜 이상 공개)
  max_bytes     공개 총량 예산 (null = 없음) — 넘으면 가장 옛 날짜부터 통째 제외 (창보다 우선)
  heavy_window  null 또는 {"days": N, "globs": [...]} — 무거운 파일(그림 · 큰 백데이터)은 최근 N일만 (나머지 창 = 글만)
  tz            날짜 기준 시간대 (예 Asia/Seoul)
  exclude_globs 언제나 제외 (.DS_Store · .github/** …)
  manifest      목록 파일 경로 (reports_manifest.json) — artifact 안에서 창 안 dirs 만 + archive 표기
  min_public_dates  조립 결과 하한 — 공개 날짜가 이보다 적으면 배포하지 않음 (1 이상 정수 · 시계 · tz · 창 · 예산 이상 감지)

날짜 판정 = 경로 세그먼트(폴더 먼저 · 없으면 파일 이름)의 첫 YYYY-MM-DD · 없으면 공통 파일(언제나 공개).
같은 날짜 = 통째로 (반쪽 번들 금지 · 예산 제외도 날짜 단위). 파일 이름 = git ls-tree -z (한글 이름 그대로 · quotepath 영향 없음).

명령:
  measure  --windows 90,30,14,7 [--budgets 1e9]   창별 공개 파일 수 · 크기 (git ls-tree -l · 조립 없음)
  build    --out _site [--sparse on|off] [--link on|off] [--check-links on|warn|off]
           sparse on = CI (blob 없는 부분 clone) — git sparse-checkout 으로 고른 경로 blob 만 받음
           link on   = 하드링크 (같은 파일 시스템 · 로컬 dry-run · CI 둘 다 · upload-pages-artifact 는 tar --hard-dereference)
           조립 결과 검사 (check_site) — 목록 파일이 공개 대상에 있고 · 공개 날짜 ≥ min_public_dates · 날짜 있는 공개 HTML 이 전부 목록에 있어야 함
                                         (하나라도 어긋나면 rc 3 · 이상 사이트를 조용히 배포하지 않음 — no_fallback)
  check-links --site _site                       조립된 사이트 안 상대 · 사이트 절대 링크 → 없는 대상 목록 (rc 1)
  visible  --path <저장소 상대 경로> [--path ...] 번들 폴더 · 파일이 공개 사이트에 실리는가 (build 와 같은 select · rc 1 = 하나라도 비공개)
           창 · 예산 밖 번들은 저장소엔 올라가도 공개 주소가 404 → 배포 스크립트가 push 뒤 공개 URL · X 링크를 내기 전에 부른다

rc: 0 정상 · 1 링크 깨짐 (check-links on) 또는 비공개 경로 있음 (visible) · 2 설정 · 인자 오류 · 3 git 실패 또는 조립 결과 이상
표준 라이브러리만 (CI = ubuntu python3 · 로컬 = .venv).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
SITE_ABS = "https://nixstock.github.io/nixstock-reports/"
LINK_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'<>]+)["']""", re.I)
REQ_KEYS = ("schema", "window_days", "max_bytes", "heavy_window", "tz", "exclude_globs", "manifest", "archive_url",
            "min_public_dates")


def load_cfg(path: str) -> dict:
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    miss = [k for k in REQ_KEYS if k not in c]
    if miss:
        raise ValueError(f"설정 {path} 키 없음: {miss}")
    if c["schema"] != 1:
        raise ValueError(f"설정 schema={c['schema']} — 1 만 지원")
    if not isinstance(c["window_days"], int) or c["window_days"] < 1:
        raise ValueError(f"window_days={c['window_days']!r} — 1 이상 정수")
    if c["max_bytes"] is not None and (not isinstance(c["max_bytes"], (int, float)) or c["max_bytes"] <= 0):
        raise ValueError(f"max_bytes={c['max_bytes']!r} — 양수 또는 null")
    hw = c["heavy_window"]
    if hw is not None and (not isinstance(hw, dict) or not isinstance(hw.get("days"), int) or not hw.get("globs")):
        raise ValueError("heavy_window = null 또는 {\"days\": 정수, \"globs\": [...]}")
    mp = c["min_public_dates"]
    if isinstance(mp, bool) or not isinstance(mp, int) or mp < 1:
        raise ValueError(f"min_public_dates={mp!r} — 1 이상 정수 (공개 날짜 0개 사이트를 막는 하한 · 끌 수 없음)")
    ZoneInfo(c["tz"])
    return c


def git(repo: str, *args: str, inp: bytes | None = None) -> bytes:
    p = subprocess.run(["git", "-C", repo, *args], input=inp, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} rc={p.returncode} — {p.stderr.decode(errors='replace').strip()[:300]}")
    return p.stdout


def ls_tree(repo: str, sizes: bool) -> list:
    """[(path, size|None)] — -z 로 한글 이름 그대로."""
    out = git(repo, "ls-tree", "-r", "-z", *(["-l"] if sizes else ["--name-only"]), "HEAD")
    rows = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        if sizes:
            meta, path = rec.split(b"\t", 1)
            rows.append((path.decode("utf-8"), int(meta.split()[3])))
        else:
            rows.append((rec.decode("utf-8"), None))
    return rows


def _gh_tree(api_repo: str, sha: str, recursive: bool) -> dict:
    gh = os.environ.get("PAGES_GH_BIN") or "gh"
    p = subprocess.run([gh, "api", f"repos/{api_repo}/git/trees/{sha}" + ("?recursive=1" if recursive else "")],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"trees API {sha[:8]} rc={p.returncode} — {p.stderr.decode(errors='replace').strip()[:300]}")
    return json.loads(p.stdout)


def tree_sizes_api(repo: str, api_repo: str) -> list:
    """CI (blob 없는 부분 clone) — 크기를 GitHub trees API 로 (blob 을 받지 않고 예산 판정 · gh = 러너 기본 · GH_TOKEN 필요).
    API 한도 = 응답 100k 항목 / 7 MB (넘으면 truncated) — 2026-11-03 루트 한 번 = 5.6 MB (18.5k 항목 · 약 2~3주 뒤 한도).
    → 루트는 한 단계만 · 그 아래 폴더마다 recursive · 그래도 truncated 면 그 폴더를 한 단계 내려 다시 (잘린 응답을 쓰지 않음)."""
    if not api_repo:
        raise RuntimeError("--api-repo 또는 GITHUB_REPOSITORY 없음")
    root = git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    out: list = []

    def walk(sha: str, prefix: str, recursive: bool) -> None:
        d = _gh_tree(api_repo, sha, recursive)
        if recursive and d.get("truncated"):
            walk(sha, prefix, False)
            return
        if d.get("truncated"):
            raise RuntimeError(f"trees API 한 단계 응답도 잘림 ({prefix or '/'}) — 크기 판정 불가")
        for t in d.get("tree", []):
            path = prefix + t["path"]
            if t.get("type") == "blob":
                out.append((path, int(t["size"])))
            elif t.get("type") == "tree" and not recursive:
                walk(t["sha"], path + "/", True)

    walk(root, "", False)
    return out


def path_date(path: str) -> str | None:
    segs = path.split("/")
    for s in segs[:-1] + segs[-1:]:
        m = DATE_RE.search(s)
        if m:
            try:
                dt.date.fromisoformat(m.group(1))
                return m.group(1)
            except ValueError:
                continue
    return None


def bundle_dir(path: str) -> str | None:
    """첫 날짜 세그먼트까지의 폴더 (예 market/2026-10-30_kr_market_temp_close) · 날짜가 파일 이름에만 있으면 None."""
    segs = path.split("/")
    for i, s in enumerate(segs[:-1]):
        if DATE_RE.search(s):
            return "/".join(segs[: i + 1])
    return None


def matches(path: str, globs) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)


def select(rows: list, cfg: dict, today: dt.date, window_days: int | None = None, budget="cfg") -> dict:
    """공개 선택. 반환 dict(keep=[(path,size)], drop=[...], cutoff, heavy_cutoff, dates_kept, dates_budget_dropped, common)."""
    wd = cfg["window_days"] if window_days is None else window_days
    mb = cfg["max_bytes"] if budget == "cfg" else budget
    cutoff = (today - dt.timedelta(days=wd)).isoformat()
    hw = cfg["heavy_window"]
    h_cut = (today - dt.timedelta(days=hw["days"])).isoformat() if hw else None
    keep, drop, common = [], [], []
    by_date: dict = {}
    for path, size in rows:
        if matches(path, cfg["exclude_globs"]):
            drop.append((path, size, "exclude"))
            continue
        d = path_date(path)
        if d is None:
            common.append((path, size))
            continue
        if d < cutoff:
            drop.append((path, size, "window"))
            continue
        if hw and d < h_cut and matches(path, hw["globs"]):
            drop.append((path, size, "heavy"))
            continue
        by_date.setdefault(d, []).append((path, size))
    budget_dropped = []
    if mb is not None and all(s is not None for _, s in rows):
        total, full = sum(s for _, s in common), False
        for d in sorted(by_date, reverse=True):          # 최근 날짜부터 · 처음 넘는 날짜부터 그 이전 전부 제외 (구멍 난 창 금지)
            z = sum(s for _, s in by_date[d])
            if full or total + z > mb:
                full = True
                budget_dropped.append(d)
            else:
                total += z
        for d in budget_dropped:
            drop.extend((p, s, "budget") for p, s in by_date.pop(d))
    keep = common + [x for d in sorted(by_date) for x in by_date[d]]
    return {"keep": keep, "drop": drop, "cutoff": cutoff, "heavy_cutoff": h_cut, "common": common,
            "dates_kept": sorted(by_date), "dates_budget_dropped": sorted(budget_dropped), "window_days": wd,
            "max_bytes": mb}


def first_public_date(sel: dict) -> str:
    """실제 공개 첫 날짜 (예산 제외가 있으면 공개 날짜 중 가장 옛 날짜 · 없으면 창 기준일) — 목록 archive 표기 · visible 설명이 같이 씀."""
    return sel["dates_kept"][0] if (sel["dates_budget_dropped"] and sel["dates_kept"]) else sel["cutoff"]


def manifest_dirs(src: Path) -> dict:
    m = json.loads(src.read_text(encoding="utf-8"))
    dirs = m.get("dirs")
    if not isinstance(dirs, dict):
        raise ValueError(f"목록 {src} 에 dirs 없음 — schema 확인")
    return dirs


def listed_paths(src: Path) -> set:
    """목록 파일이 싣는 HTML 경로 전부 (dirs 키 "" = 루트 파일)."""
    return {(f"{d}/{f}" if d else f) for d, files in manifest_dirs(src).items() for f in files}


def rewrite_manifest(src: Path, dst: Path, kept: set, sel: dict, cfg: dict, published_at: str) -> dict:
    m = json.loads(src.read_text(encoding="utf-8"))
    dirs = manifest_dirs(src)
    new, rm_dirs, rm_files = {}, 0, 0
    for d, files in dirs.items():
        k = [f for f in files if (f"{d}/{f}" if d else f) in kept]
        rm_files += len(files) - len(k)
        if k:
            new[d] = k
        else:
            rm_dirs += 1
    m["dirs"] = new
    m["count"] = sum(len(v) for v in new.values())
    bd = sel["dates_budget_dropped"]
    first = first_public_date(sel)   # 실제 공개 첫 날짜 (예산 제외가 있으면 그 다음 날짜)
    m["archive"] = {"window_days": sel["window_days"], "cutoff": sel["cutoff"], "max_bytes": sel["max_bytes"],
                    "first_public_date": first, "heavy_cutoff": sel["heavy_cutoff"],
                    "budget_dropped": {"count": len(bd), "from": bd[0] if bd else None, "to": bd[-1] if bd else None},
                    "dirs_removed": rm_dirs, "files_removed": rm_files, "published_at": published_at,
                    "note": f"{first} 이전 리포트 = 저장소 보관 (github.com/{m.get('repo', '')})"}
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, dst)
    return m["archive"]


def sparse_patterns(keep: list) -> list:
    pats, seen = ["/.github/"], set()
    for path, _ in keep:
        b = bundle_dir(path)
        p = f"/{b}/" if b else f"/{path}"
        if p not in seen:
            seen.add(p)
            pats.append(p)
    return pats


def load_rows(a) -> list:
    """크기 원천: git = ls-tree -l (전체 clone · 로컬) · api = GitHub trees API (CI 부분 clone — blob 받기 전에 예산 판정)."""
    if a.sizes == "api":
        return tree_sizes_api(a.repo, a.api_repo or os.environ.get("GITHUB_REPOSITORY", ""))
    return ls_tree(a.sizes_repo or a.repo, sizes=True)


def today_of(a, cfg: dict) -> dt.date:
    return dt.datetime.now(ZoneInfo(cfg["tz"])).date() if not a.today else dt.date.fromisoformat(a.today)


def check_site(repo: str, cfg: dict, sel: dict) -> str:
    """조립 결과 하한 검사 — 이상 사이트를 rc 0 으로 배포하지 않는다 (no_fallback · 조용한 통과 금지). 어긋나면 RuntimeError (rc 3).
      ① 목록 파일이 공개 대상에 있음 (없으면 목록 페이지가 옛 목록 · 빈 목록)
      ② 공개 날짜 ≥ min_public_dates (창 · 예산 · tz · 시계 이상 = 공통 파일만 담은 빈 사이트)
      ③ 날짜 있는 공개 HTML 이 전부 목록에 있음 (이름 판정 불일치 — 예 NFC/NFD — 로 목록에서 조용히 빠지는 것 방지 ·
         날짜 없는 HTML = 사이트 자체 페이지(루트 · 뷰어 index.html …)라 목록 대상 아님 — 목록 생성기가 이미 뺌, 여기에 이름 목록을 복제하지 않음)"""
    kept = {p for p, _ in sel["keep"]}
    man, probs = cfg["manifest"], []
    if man not in kept:
        probs.append(f"목록 파일 {man} 이 공개 대상에 없음 (저장소 HEAD 에 없거나 제외 목록에 걸림)")
    n, floor = len(sel["dates_kept"]), cfg["min_public_dates"]
    if n < floor:
        probs.append(f"공개 날짜 {n}개 < 하한 {floor}개 (min_public_dates) — 창 · 예산 · 날짜 판정 (tz · 시계) 확인")
    if man in kept:
        listed = listed_paths(Path(repo) / man)
        unlisted = sorted(p for p in kept if p.endswith(".html") and path_date(p) and p not in listed)
        if unlisted:
            probs.append(f"날짜 있는 공개 HTML {len(unlisted)}개가 목록에 없음 (예 {unlisted[:3]}) — 목록 생성기와 이름 판정이 어긋남")
    if probs:
        raise RuntimeError("조립 결과 이상 — " + " / ".join(probs))
    return f"🧪 조립 검사 통과 — 목록 파일 있음 · 공개 날짜 {n}개 (하한 {floor}) · 날짜 있는 공개 HTML 전부 목록에 있음"


def build(a, cfg: dict) -> int:
    repo, out = a.repo, Path(a.out)
    today = today_of(a, cfg)
    rows = load_rows(a)
    sel = select(rows, cfg, today)
    if a.sparse == "on":
        pats = sparse_patterns(sel["keep"])
        git(repo, "sparse-checkout", "set", "--no-cone", "--stdin", inp=("\n".join(pats) + "\n").encode("utf-8"))
        print(f"🧩 희소 checkout 패턴 {len(pats)}개 (번들 폴더 + 공통 파일) — 고른 경로 blob 만 받음", flush=True)
        miss = [p for p, _ in sel["keep"] if not (Path(repo) / p).is_file()]
        if miss:
            raise RuntimeError(f"희소 checkout 뒤 없는 파일 {len(miss)}개 (예 {miss[:3]})")
    print(check_site(repo, cfg, sel), flush=True)     # 조립 전에 — 이상이면 _site 를 만들지 않고 rc 3
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    n = tot = 0

    def place(items):
        nonlocal n, tot
        for path, size in items:
            s, d = Path(repo) / path, out / path
            d.parent.mkdir(parents=True, exist_ok=True)
            if a.link == "on":
                os.link(s, d)
            else:
                shutil.copy2(s, d)
            n += 1
            tot += size
    place(sel["keep"])
    kept = {p for p, _ in sel["keep"]}
    # ★경계 링크 = 저장소 보관 주소로 (사용자 "그 이전은 저장소에만 보관" · 깨진 링크 0) — 공개 HTML 이 가리키는 창 밖 대상은
    #   archive_url (github.com 저장소 보기) 로 바꾼다. 날짜 사슬 링크(전날 백데이터 → 그 전날 …)라 통째 추가는 끝이 없다.
    #   하드링크 원본(클론) 을 건드리지 않게 = 새 파일로 쓴다 (unlink → write). 'missing'(저장소에도 없음) = 원래 깨짐 · 그대로 보고.
    repo_paths = {p for p, _ in rows}
    by_src: dict = {}
    for src, raw, t, kind in check_links(out, repo_paths):
        if kind == "window":
            by_src.setdefault(src, []).append((raw, t))
    n_rw = 0
    for src, pairs in by_src.items():
        f = out / src
        html = f.read_text(encoding="utf-8", errors="surrogateescape")
        for raw, t in pairs:
            url = cfg["archive_url"].format(path=quote(t, safe="/"))
            for q in ('"', "'"):
                html = html.replace(f"={q}{raw}{q}", f"={q}{url}{q}")
            n_rw += 1
        f.unlink()
        f.write_text(html, encoding="utf-8", errors="surrogateescape")
    if n_rw:
        print(f"🗄️ 경계 링크 {n_rw}건 ({len(by_src)}파일) → 저장소 보관 주소 (archive_url)", flush=True)
    man = cfg["manifest"]
    arch = None
    if man in kept:
        (out / man).unlink()
        arch = rewrite_manifest(Path(repo) / man, out / man, kept, sel, cfg,
                                dt.datetime.now(ZoneInfo(cfg["tz"])).isoformat(timespec="seconds"))
    why = {}
    for _, _, w in sel["drop"]:
        why[w] = why.get(w, 0) + 1
    print(f"📦 _site 조립 — 파일 {n:,}개 · {tot / 1e9:.3f} GB · 기준일 {sel['cutoff']} (창 {sel['window_days']}일) · "
          f"공개 날짜 {len(sel['dates_kept'])}개 ({sel['dates_kept'][0] if sel['dates_kept'] else '-'} ~ "
          f"{sel['dates_kept'][-1] if sel['dates_kept'] else '-'}) · 제외 {why}", flush=True)
    if sel["dates_budget_dropped"]:
        print(f"⚠️ 예산 {sel['max_bytes'] / 1e9:.2f} GB 초과로 제외한 날짜 {len(sel['dates_budget_dropped'])}개: "
              f"{sel['dates_budget_dropped'][0]} ~ {sel['dates_budget_dropped'][-1]}", flush=True)
    if arch:
        print(f"📋 목록 다시 씀 — 폴더 −{arch['dirs_removed']} · 파일 −{arch['files_removed']} · archive 표기", flush=True)
    if a.check_links != "off":
        bad = check_links(out, {p for p, _ in rows})
        return report_links(bad, a.check_links)
    return 0


def _targets(html: str, rel_dir: PurePosixPath):
    for raw in LINK_RE.findall(html):
        u = raw.strip()
        if u.startswith("+") or " + " in u or "${" in u:   # 스크립트 문자열 조각 (예 href="' + s + '") — 정적 링크 아님
            continue
        if u.startswith(SITE_ABS):
            t = PurePosixPath(unquote(u[len(SITE_ABS):].split("#")[0].split("?")[0]))
        elif re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I) or u.startswith(("#", "//", "{", "$")):
            continue
        else:
            u = unquote(u.split("#")[0].split("?")[0])
            if not u or u.startswith("/"):
                continue
            t = rel_dir / u
        parts = []
        for seg in t.parts:
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg not in (".", ""):
                parts.append(seg)
        if parts:
            yield raw, "/".join(parts)


def check_links(site: Path, repo_paths: set) -> list:
    """[(소스, 링크, 대상, 종류)] — 종류 = window (저장소엔 있으나 창 밖) · missing (저장소에도 없음 = 원래 깨짐)."""
    have = {str(p.relative_to(site)) for p in site.rglob("*") if p.is_file()}
    bad = []
    for f in sorted(have):
        if not f.endswith(".html"):
            continue
        rel_dir = PurePosixPath(f).parent
        html = (site / f).read_text(encoding="utf-8", errors="replace")
        for raw, t in _targets(html, rel_dir):
            if t in have or (t + "/index.html") in have or t.endswith("/"):
                continue
            if t in ("index.html",):
                continue
            kind = "window" if (t in repo_paths or (t + "/index.html") in repo_paths) else "missing"
            bad.append((f, raw, t, kind))
    return bad


def report_links(bad: list, mode: str) -> int:
    w = [b for b in bad if b[3] == "window"]
    m = [b for b in bad if b[3] == "missing"]
    print(f"🔗 링크 검사 — 창 때문에 깨짐 {len(w)}건 · 원래 깨짐 (저장소에도 없음) {len(m)}건", flush=True)
    for f, raw, t, k in (w[:20] + m[:10]):
        print(f"   {'⛔' if k == 'window' else '·'} {f} → {raw} ({k})", flush=True)
    return 1 if (w and mode == "on") else 0


WHY_TEXT = {"window": "창 밖", "budget": "크기 예산 밖", "heavy": "무거운 파일 창 밖", "exclude": "언제나 제외 목록"}


def visible(a, cfg: dict) -> int:
    """--path 마다 공개 여부 한 줄 — build 와 같은 select (창 · 예산 · 무거운 파일 · 제외). rc 1 = 하나라도 완전 공개가 아님.
    창 · 예산 밖 번들은 저장소엔 push 돼도 공개 주소가 404 → 배포 스크립트가 push 뒤 공개 URL · X 요약 링크를 만들기 전에 거른다."""
    rows = load_rows(a)
    sel = select(rows, cfg, today_of(a, cfg))
    kept = {p for p, _ in sel["keep"]}
    why = {p: w for p, _, w in sel["drop"]}
    first = first_public_date(sel)
    print(f"공개 기준 — 창 {sel['window_days']}일 (기준일 {sel['cutoff']}) · 첫 공개 날짜 {first} · 공개 날짜 {len(sel['dates_kept'])}개", flush=True)
    all_paths = [p for p, _ in rows]
    bad = 0
    for raw in a.path:
        base = raw.strip().rstrip("/")
        under = [p for p in all_paths if p == base or p.startswith(base + "/")]
        if not base or not under:
            print(f"⛔ 저장소 HEAD 에 없음  {raw}", flush=True)
            bad += 1
            continue
        k = sum(1 for p in under if p in kept)
        reasons: dict = {}
        for p in under:
            if p not in kept:
                reasons[why[p]] = reasons.get(why[p], 0) + 1
        txt = " · ".join(f"{WHY_TEXT[w]} {c}개" for w, c in sorted(reasons.items(), key=lambda x: -x[1]))
        if k == len(under):
            print(f"✅ 공개  {raw}  ({k}개)", flush=True)
        elif k == 0:
            print(f"⛔ 비공개 (저장소 보관만 — 공개 주소 404)  {raw}  ({txt} · 첫 공개 날짜 {first})", flush=True)
            bad += 1
        else:
            print(f"⚠️ 일부만 공개 ({k}/{len(under)})  {raw}  ({txt})", flush=True)
            bad += 1
    return 1 if bad else 0


def measure(a, cfg: dict) -> int:
    rows = ls_tree(a.repo, sizes=True)
    today = dt.datetime.now(ZoneInfo(cfg["tz"])).date() if not a.today else dt.date.fromisoformat(a.today)
    allb = sum(s for _, s in rows)
    print(f"저장소 HEAD = 파일 {len(rows):,}개 · {allb / 1e9:.3f} GB · 기준 오늘 {today}")
    print("| 창 (일) | 예산 | 기준일 | 공개 날짜 | 파일 | GB | 제외 날짜 (예산) |")
    print("| ---: | ---: | --- | ---: | ---: | ---: | ---: |")
    for w in [int(x) for x in a.windows.split(",")]:
        for b in [None] + [float(x) for x in (a.budgets.split(",") if a.budgets else [])]:
            s = select(rows, cfg, today, window_days=w, budget=b)
            gb = sum(x for _, x in s["keep"]) / 1e9
            print(f"| {w} | {'-' if b is None else f'{b / 1e9:.2f} GB'} | {s['cutoff']} | {len(s['dates_kept'])} | "
                  f"{len(s['keep']):,} | {gb:.3f} | {len(s['dates_budget_dropped'])} |")
    if cfg["heavy_window"]:
        hw = cfg["heavy_window"]
        s = select(rows, cfg, today)
        hb = sum(x for p, x in s["keep"] if matches(p, hw["globs"]))
        print(f"\n무거운 파일 창 {hw['days']}일 ({hw['globs']}) 적용 = 설정 창 {cfg['window_days']}일 · 공개 "
              f"{sum(x for _, x in s['keep']) / 1e9:.3f} GB (그중 무거운 파일 {hb / 1e9:.3f} GB)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("measure", "build", "check-links", "visible"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", default=".")
        sp.add_argument("--config", default=".github/pages_publish.json")
        sp.add_argument("--today", default=None, help="YYYY-MM-DD (시험용 · 기본 = 설정 tz 오늘)")
        if name in ("build", "visible"):
            sp.add_argument("--sizes", choices=["git", "api"], default="git", help="크기 원천 (CI 부분 clone = api)")
            sp.add_argument("--sizes-repo", default=None, help="--sizes git 의 저장소 (기본 --repo · 모의 시험 = 전체 클론)")
            sp.add_argument("--api-repo", default=None, help="--sizes api 의 owner/name (기본 GITHUB_REPOSITORY)")
        if name == "measure":
            sp.add_argument("--windows", default="90,30,14,7")
            sp.add_argument("--budgets", default="")
        if name == "build":
            sp.add_argument("--out", required=True)
            sp.add_argument("--sparse", choices=["on", "off"], default="off")
            sp.add_argument("--link", choices=["on", "off"], default="on")
            sp.add_argument("--check-links", choices=["on", "warn", "off"], default="warn")
        if name == "check-links":
            sp.add_argument("--site", required=True)
        if name == "visible":
            sp.add_argument("--path", action="append", required=True, help="저장소 상대 경로 (번들 폴더 또는 파일 · 여러 번 가능)")
    a = ap.parse_args(argv)
    try:
        cfg = load_cfg(a.config)
        if a.cmd == "measure":
            return measure(a, cfg)
        if a.cmd == "build":
            return build(a, cfg)
        if a.cmd == "visible":
            return visible(a, cfg)
        bad = check_links(Path(a.site), {p for p, _ in ls_tree(a.repo, sizes=False)})
        return report_links(bad, "on")
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"❌ 설정 · 인자 오류 — {e}", flush=True)
        return 2
    except RuntimeError as e:
        print(f"❌ {e}", flush=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
