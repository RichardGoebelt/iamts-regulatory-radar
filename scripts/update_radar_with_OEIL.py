#!/usr/bin/env python3
"""IAMTS Regulatory & Policy Radar – weekly source retrieval.

Server-side retrieval intended for GitHub Actions. No third-party packages.
The script reads data/state.json as monitoring memory, checks the fixed official
source list below, writes public/radar.json, and updates state only for sources
that were checked successfully.
"""

from __future__ import annotations

import concurrent.futures
import csv
import email.utils
import io
import hashlib
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILE = ROOT / "public" / "radar.json"
STATE_FILE = ROOT / "data" / "state.json"
CURRENT_YEAR = datetime.now(timezone.utc).year

# ============================================================================
# EDITABLE SOURCE CONFIGURATION
# Add, remove or change official sources only in this section.
# No URL is supplied by website users.
# ============================================================================
SOURCES: list[dict[str, Any]] = [
    {
        "id": "unece-grva",
        "region": "UNECE",
        "name": "UNECE GRVA – UN Official Document System",
        "adapter": "unece_official",
        "page_url": "https://unece.org/transport/vehicle-regulations/working-party-automatedautonomous-and-connected-vehicles-introduction",
        # The UN Digital Library provides a documented machine-readable search API.
        # It is used instead of unece.org HTML pages, which return HTTP 403 to
        # GitHub-hosted runners.
        "search_patterns": [
            {"pattern": f"ECE/TRANS/WP.29/GRVA/{CURRENT_YEAR}", "field": "reportnumber"},
            {"pattern": f"ECE/TRANS/WP.29/GRVA/{CURRENT_YEAR}", "field": ""},
        ],
        # Official UNECE Wiki is a supplementary source for current GRVA/ADS
        # informal work. Failure of the Wiki does not invalidate UNDL results.
        "wiki_urls": [
            "https://wiki.unece.org/spaces/trans/pages/63310525/Working%2BParty%2Bon%2BAutomated%2BAutonomous%2Band%2BConnected%2BVehicles%2BGRVA",
            "https://wiki.unece.org/spaces/trans/pages/364708023/ADS%2B21st%2BSession",
        ],
        # Direct ODS symbol scan. Current formal GRVA documents are numbered
        # ECE/TRANS/WP.29/GRVA/YYYY/N. Increase the upper bound if a future
        # year exceeds it.
        "symbol_prefix": "ECE/TRANS/WP.29/GRVA",
        "max_symbol_number": 90,
        "scan_workers": 8,
    },
    {
        "id": "unece-wp29",
        "region": "UNECE",
        "name": "UNECE WP.29 – UN Official Document System",
        "adapter": "unece_official",
        "page_url": "https://unece.org/transport/vehicle-regulations",
        "search_patterns": [
            {"pattern": f"ECE/TRANS/WP.29/{CURRENT_YEAR}", "field": "reportnumber"},
            {"pattern": f"ECE/TRANS/WP.29/{CURRENT_YEAR}", "field": ""},
        ],
        "wiki_urls": [],
        # WP.29 formal documents use ECE/TRANS/WP.29/YYYY/N. The range is
        # intentionally broader than GRVA because WP.29 issues more documents.
        "symbol_prefix": "ECE/TRANS/WP.29",
        "max_symbol_number": 240,
        "scan_workers": 10,
    },
    {
        "id": "eu-grow-publications",
        "region": "EU",
        "name": "European Commission DG GROW – Publications",
        "adapter": "rss",
        "page_url": "https://single-market-economy.ec.europa.eu/rss_en",
        "feed_urls": [
            "https://single-market-economy.ec.europa.eu/node/3/rss_en",
            "https://ec.europa.eu/newsroom/growth/feed?tpa_id=29399",
        ],
    },
    {
        "id": "eu-ccam",
        "region": "EU",
        "name": "European Commission – CCAM / Automotive legislation",
        "adapter": "official_pages",
        "page_url": "https://transport.ec.europa.eu/transport-themes/intelligent-transport-systems/cooperative-connected-and-automated-mobility-ccam_en",
        "urls": [
            "https://transport.ec.europa.eu/transport-themes/intelligent-transport-systems/cooperative-connected-and-automated-mobility-ccam_en",
            "https://single-market-economy.ec.europa.eu/sectors/automotive-industry/legislation_en",
        ],
    },
    {
        "id": "eu-oeil",
        "region": "EU",
        "name": "European Parliament – Legislative Observatory (OEIL)",
        "adapter": "ep_oeil",
        "page_url": "https://oeil.europarl.europa.eu/oeil/en/search",
        # OEIL procedure data are retrieved from the European Parliament's
        # official Open Data Portal. The visible source links still point to
        # the corresponding Legislative Observatory procedure files.
        "years_back": 3,
        "distribution_template": "https://data.europarl.europa.eu/distribution/procedures_{year}_1_en.csv",
        "max_detail_fetch": 30,
    },
    {
        "id": "eu-eurlex-oj",
        "region": "EU",
        "name": "EUR-Lex – Official Journal of the European Union",
        "adapter": "eurlex_oj",
        "page_url": "https://eur-lex.europa.eu/oj/direct-access.html",
        "sparql_url": "https://publications.europa.eu/webapi/rdf/sparql",
        "monitor_days": 14,
        "max_results": 1000,
    },
    {
        "id": "usa-nhtsa",
        "region": "USA",
        "name": "NHTSA – Regulatory Actions (Federal Register)",
        "adapter": "federal_register",
        "page_url": "https://www.nhtsa.gov/vehicle-safety/automated-vehicle-safety",
        "agency": "national-highway-traffic-safety-administration",
        "monitor_days": 30,
        "search_terms": [
            "automated vehicle",
            "automated driving",
            "advanced driver assistance",
            "vehicle cybersecurity",
            "vehicle software",
        ],
    },
    {
        "id": "usa-usdot",
        "region": "USA",
        "name": "U.S. DOT – Regulatory Actions (Federal Register)",
        "adapter": "federal_register",
        "page_url": "https://www.transportation.gov/AV",
        "agency": "transportation-department",
        "monitor_days": 30,
        "search_terms": [
            "automated vehicle",
            "automated driving",
            "connected vehicle",
            "vehicle automation",
            "vehicle cybersecurity",
        ],
    },
    {
        "id": "china-miit",
        "region": "China",
        "name": "China MIIT – Automotive Industry",
        "adapter": "miit",
        "page_url": "https://www.miit.gov.cn/jgsj/zbys/qcgy/index.html",
        "urls": ["https://www.miit.gov.cn/jgsj/zbys/qcgy/index.html"],
    },
    {
        "id": "china-samr",
        "region": "China",
        "name": "China SAMR / SAC – National Standards",
        "adapter": "samr",
        "page_url": "https://openstd.samr.gov.cn/",
        "search_urls": [
            "https://openstd.samr.gov.cn/bzgk/std/std_list?p.p1=0&p.p2=%E6%99%BA%E8%83%BD%E7%BD%91%E8%81%94%E6%B1%BD%E8%BD%A6&p.p90=circulation_date&p.p91=desc",
            "https://openstd.samr.gov.cn/bzgk/std/std_list?p.p1=0&p.p2=%E8%87%AA%E5%8A%A8%E9%A9%BE%E9%A9%B6&p.p90=circulation_date&p.p91=desc",
        ],
    },
]

SCOPE_TERMS = [
    "automated driving", "autonomous driving", "automated vehicle", "autonomous vehicle",
    "connected vehicle", "connected and automated", "cooperative connected",
    "intelligent connected vehicle", "driver assistance", "advanced driver assistance",
    "adas", "automated driving system", "automated driving systems", " ads ", "alks", "dcas",
    "driver control assistance systems", "dssad", "event data recorder", "edr",
    "un regulation no. 155", "un regulation no. 156", "un regulation no. 157", "un regulation no. 171",
    "un regulation no. 185", "un regulation no 185", "un r185", "un r 185",
    "automated lane", "automated parking", "cybersecurity", "software update", "software updates",
    "vehicle software", "over-the-air", "type approval", "homologation", "vehicle approval",
    "vehicle certification", "validation", "verification", "simulation", "virtual testing",
    "scenario-based", "scenario based", "proving ground", "test method", "testing method",
    "safety assessment", "conformity assessment", "data access", "data recorder", "data recording",
    "vehicle connectivity", "v2x", "artificial intelligence", "machine learning",
    "intelligent transport systems", "roadworthiness", "roadworthiness package",
    "vehicle registration data", "vehicle registration documents", "motor vehicle safety",
    "智能网联汽车", "自动驾驶", "组合驾驶辅助", "驾驶辅助", "软件升级", "信息安全", "网络安全",
    "仿真试验", "场地试验", "道路试验", "测试场景", "安全评估", "数据记录",
]

OEIL_SCOPE_TERMS = [
    "automated driving", "automated vehicle", "autonomous driving", "driver assistance",
    "advanced driver assistance", "ADAS", "connected vehicle", "connected and automated",
    "intelligent transport systems", "vehicle cybersecurity", "vehicle software",
    "software update", "type-approval", "type approval", "motor vehicle safety",
    "roadworthiness", "roadworthiness package", "vehicle registration data",
    "vehicle registration documents", "Regulation (EU) 2018/858",
    "Regulation (EU) 2019/2144", "UN Regulation No. 185", "UN R185",
]


def oeil_in_scope(text: str) -> bool:
    """Narrow EP/OEIL filter for vehicle regulation relevant to IAMTS."""
    return text_has_any(text, OEIL_SCOPE_TERMS) or (
        in_scope(text) and text_has_any(text, [
            "vehicle", "driving", "road", "transport", "mobility",
            "type approval", "type-approval", "testing", "certification"
        ])
    )


OJ_SCOPE_TERMS = [
    # Strong connected/automated-driving signals
    "automated driving", "automated vehicle", "automated vehicles", "automated mobility",
    "autonomous driving", "driver assistance", "advanced driver assistance", "ADAS",
    "automated lane", "automated parking", "driving automation",
    "connected vehicle", "connected vehicles", "vehicle connectivity", "V2X",
    "UN Regulation No. 185", "UN R185",
    "cooperative intelligent transport", "intelligent transport systems",
    "vehicle cybersecurity", "vehicle software", "software update", "software updates",
    "vehicle data", "data access", "event data recorder",
    # Vehicle approval/testing terms that can materially affect IAMTS even when
    # the title does not explicitly say 'automated driving'.
    "vehicle type-approval", "vehicle type approval", "type-approval of motor vehicles",
    "type approval of motor vehicles", "motor vehicle safety", "roadworthiness",
    "Regulation (EU) 2018/858", "Regulation (EU) 2019/2144",
]


def oj_in_scope(text: str) -> bool:
    """Narrow Official Journal filter for IAMTS Connected & Automated Driving.

    The OJ contains thousands of unrelated acts. We therefore require a strong
    mobility/vehicle signal instead of treating every general AI/cyber act as
    relevant. This avoids filling the radar with unrelated EU legislation.
    """
    return text_has_any(text, OJ_SCOPE_TERMS) or in_scope(text) and text_has_any(
        text, ["vehicle", "driving", "road", "transport", "mobility", "type approval", "type-approval"]
    )


REGULATORY_TERMS = [
    "regulation", "regulatory", "proposal", "proposed", "draft", "standard", "standards",
    "rule", "rulemaking", "requirements", "requirement", "guidance", "framework", "consultation",
    "approval", "certification", "homologation", "safety", "amendment", "working document",
    "implementation", "specification", "technical", "test", "testing",
    "标准", "要求", "征求意见", "草案", "试验", "测试", "安全",
]

T_AND_C_TERMS = [
    "test", "testing", "validation", "verification", "certification", "homologation", "type approval",
    "approval", "conformity assessment", "simulation", "virtual", "scenario", "proving ground",
    "cybersecurity", "software update", "safety assessment", "试验", "测试", "安全评估", "认证", "批准",
]

TOPIC_RULES = [
    ("Automated Driving Validation", ["validation", "verification", "scenario", "simulation", "virtual testing", "proving ground", "safety assessment", "仿真试验", "测试场景"]),
    ("Cybersecurity & Software Updates", ["cybersecurity", "software update", "ota", "information security", "软件升级", "信息安全", "网络安全"]),
    ("Type Approval & Certification", ["type approval", "homologation", "certification", "conformity assessment", "approval"]),
    ("Vehicle Connectivity & Data", ["connected", "connectivity", "v2x", "data access", "data recorder", "数据记录"]),
    ("AI-based Vehicle Functions", ["artificial intelligence", "machine learning", " ai "]),
    ("Automated Driving Regulation", ["automated driving", "autonomous", "automated vehicle", "automated driving system", "ads", "aleks", "dcas", "自动驾驶"]),
    ("Driver Assistance Systems", ["driver assistance", "adas", "lane keeping", "automated parking", "驾驶辅助", "自动泊车"]),
]

FETCH_HEADERS = {
    # Use a normal browser user agent. Several public-sector sites treat generic
    # Python user agents differently from interactive browsers.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,application/rss+xml,application/atom+xml;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
USER_AGENTS = [
    FETCH_HEADERS["User-Agent"],
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
]
MAX_BYTES = 6_000_000
TIMEOUT = 20
FETCH_RETRIES = 2


class FetchFailure(RuntimeError):
    """Raised with useful diagnostics after all retrieval strategies fail."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def low(value: Any) -> str:
    return norm(value).lower()


def truncate(value: Any, limit: int = 800) -> str:
    s = norm(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def text_has_any(text: str, terms: list[str]) -> bool:
    hay = " " + low(text) + " "
    return any(low(term) in hay for term in terms)


def in_scope(text: str) -> bool:
    return text_has_any(text, SCOPE_TERMS)


def extract_date(text: str) -> str:
    text = norm(text)
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        for token in re.findall(r"[A-Za-z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}\s+[A-Za-z]+\s+20\d{2}", text):
            try:
                return datetime.strptime(token, fmt).date().isoformat()
            except ValueError:
                pass
    return ""


def parse_date_any(value: Any) -> str:
    s = norm(value)
    if not s:
        return ""
    d = extract_date(s)
    if d:
        return d
    try:
        parsed = email.utils.parsedate_to_datetime(s)
        return parsed.date().isoformat()
    except Exception:
        return ""


def resolve_url(href: str, base: str) -> str:
    try:
        return urllib.parse.urljoin(base, html_lib.unescape(href))
    except Exception:
        return base


def _body_snippet(data: bytes) -> str:
    return truncate(data[:500].decode("utf-8", errors="replace").replace("\n", " "), 180)


def _urllib_fetch(url: str, timeout: int, user_agent: str) -> tuple[bytes, str]:
    headers = dict(FETCH_HEADERS)
    headers["User-Agent"] = user_agent
    # A same-origin Referer helps with a few public Drupal/WAF configurations.
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FetchFailure(f"response exceeds {MAX_BYTES // 1_000_000} MB limit")
            return data, content_type
    except urllib.error.HTTPError as exc:
        # Do not surface full WAF/Access Denied HTML in the dashboard.
        if exc.code in {401, 403}:
            raise FetchFailure(f"HTTP {exc.code} {norm(exc.reason)} (automated access refused by source)") from exc
        try:
            snippet = _body_snippet(exc.read(600))
        except Exception:
            snippet = ""
        suffix = f" · {snippet}" if snippet else ""
        raise FetchFailure(f"HTTP {exc.code} {norm(exc.reason)}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise FetchFailure(f"network error: {norm(getattr(exc, 'reason', exc))}") from exc
    except TimeoutError as exc:
        raise FetchFailure("request timed out") from exc


def _curl_fetch(url: str, timeout: int) -> tuple[bytes, str]:
    """Fallback for sites that reject urllib but accept a normal curl client.

    GitHub-hosted Ubuntu runners include curl. This keeps the Python project free
    of third-party packages while giving government sites a second HTTP stack.
    """
    if not shutil.which("curl"):
        raise FetchFailure("curl fallback is not available on this runner")
    with tempfile.TemporaryDirectory(prefix="iamts-radar-") as td:
        body = Path(td) / "body.bin"
        headers = Path(td) / "headers.txt"
        cmd = [
            "curl", "--location", "--silent", "--show-error", "--compressed",
            "--fail-with-body", "--retry", "2", "--retry-delay", "2",
            "--connect-timeout", "12", "--max-time", str(timeout),
            "--user-agent", USER_AGENTS[0],
            "--header", f"Accept: {FETCH_HEADERS['Accept']}",
            "--header", f"Accept-Language: {FETCH_HEADERS['Accept-Language']}",
            "--header", "Cache-Control: no-cache",
            "--output", str(body), "--dump-header", str(headers), url,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 10)
        data = body.read_bytes() if body.exists() else b""
        if len(data) > MAX_BYTES:
            raise FetchFailure(f"curl response exceeds {MAX_BYTES // 1_000_000} MB limit")
        header_text = headers.read_text(encoding="iso-8859-1", errors="replace") if headers.exists() else ""
        statuses = re.findall(r"HTTP/\S+\s+(\d{3})(?:\s+([^\r\n]+))?", header_text)
        final_status = statuses[-1][0] if statuses else ""
        ctype_matches = re.findall(r"(?im)^content-type:\s*([^\r\n]+)", header_text)
        content_type = ctype_matches[-1].strip() if ctype_matches else ""
        if proc.returncode != 0:
            detail = norm(proc.stderr.decode("utf-8", errors="replace"))
            snippet = _body_snippet(data) if data and final_status not in {"401", "403"} else ""
            bits = [f"curl exit {proc.returncode}"]
            if final_status:
                bits.append(f"HTTP {final_status}")
            if final_status in {"401", "403"}:
                bits.append("automated access refused by source")
            elif detail:
                bits.append(detail)
            if snippet:
                bits.append(snippet)
            raise FetchFailure(" · ".join(bits))
        return data, content_type


def fetch_bytes(url: str, timeout: int = TIMEOUT) -> tuple[bytes, str]:
    errors: list[str] = []
    # Retry transient failures and rotate a realistic browser UA.
    for attempt in range(FETCH_RETRIES):
        try:
            return _urllib_fetch(url, timeout, USER_AGENTS[attempt % len(USER_AGENTS)])
        except Exception as exc:
            message = norm(exc)
            errors.append(f"urllib #{attempt + 1}: {message}")
            # Authentication/permission/not-found responses normally will not
            # improve on an immediate retry. Move straight to the curl fallback.
            if re.search(r"HTTP (?:401|403|404|410)\b", message):
                break
            if attempt < FETCH_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    try:
        return _curl_fetch(url, timeout)
    except Exception as exc:
        errors.append(f"curl: {norm(exc)}")
    host = urllib.parse.urlsplit(url).netloc or url
    raise FetchFailure(f"{host} retrieval failed — " + " | ".join(errors[-4:]))


def fetch_text(url: str, timeout: int = TIMEOUT) -> str:
    data, content_type = fetch_bytes(url, timeout)
    charset = "utf-8"
    m = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    if m:
        charset = m.group(1)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _sparql_post_json(url: str, query: str, timeout: int = TIMEOUT) -> dict[str, Any]:
    """POST a SPARQL SELECT query and request the standard JSON result format."""
    payload = urllib.parse.urlencode({"query": query}).encode("utf-8")
    headers = dict(FETCH_HEADERS)
    headers.update({
        "Accept": "application/sparql-results+json, application/json;q=0.9, */*;q=0.5",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Origin": "https://op.europa.eu",
        "Referer": "https://op.europa.eu/",
    })
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    errors: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FetchFailure(f"SPARQL response exceeds {MAX_BYTES // 1_000_000} MB limit")
            return json.loads(data.decode("utf-8", errors="replace"))
    except Exception as exc:
        errors.append(f"urllib POST: {norm(exc)}")

    # GitHub Ubuntu runners include curl; use it as a second HTTP stack.
    if shutil.which("curl"):
        with tempfile.TemporaryDirectory(prefix="iamts-sparql-") as td:
            body = Path(td) / "result.json"
            qfile = Path(td) / "query.txt"
            qfile.write_text(query, encoding="utf-8")
            cmd = [
                "curl", "--location", "--silent", "--show-error", "--fail-with-body",
                "--retry", "2", "--retry-delay", "2", "--connect-timeout", "12",
                "--max-time", str(timeout), "--user-agent", USER_AGENTS[0],
                "--header", "Accept: application/sparql-results+json",
                "--header", "Content-Type: application/x-www-form-urlencoded; charset=utf-8",
                "--data-urlencode", f"query@{qfile}", "--output", str(body), url,
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 10)
                data = body.read_bytes() if body.exists() else b""
                if proc.returncode == 0:
                    return json.loads(data.decode("utf-8", errors="replace"))
                errors.append(f"curl POST exit {proc.returncode}: {norm(proc.stderr.decode('utf-8', errors='replace'))}")
            except Exception as exc:
                errors.append(f"curl POST: {norm(exc)}")
    raise FetchFailure("CELLAR SPARQL request failed — " + " | ".join(errors[-3:]))


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", value)
    value = re.sub(r"(?i)</?(?:p|div|li|tr|td|th|h[1-6]|article|section|br)\b[^>]*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return norm(html_lib.unescape(value))


def error_summary(errors: list[tuple[str, Exception]], limit: int = 4) -> str:
    if not errors:
        return "no successful response"
    parts=[]
    for url, exc in errors[-limit:]:
        p=urllib.parse.urlsplit(url)
        short=(p.netloc + p.path) if p.netloc else url
        parts.append(f"{short}: {truncate(norm(exc), 240)}")
    return " | ".join(parts)


@dataclass
class Anchor:
    text: str
    url: str


@dataclass
class Row:
    text: str
    anchors: list[Anchor] = field(default_factory=list)


class StructuredHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.anchors: list[Anchor] = []
        self.rows: list[Row] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._row_depth = 0
        self._row_parts: list[str] = []
        self._row_anchors: list[Anchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag.lower() == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_parts = []
                self._row_anchors = []
        if tag.lower() == "a" and attrs_d.get("href"):
            self._anchor_href = resolve_url(attrs_d["href"] or "", self.base_url)
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._row_depth:
            self._row_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_href is not None:
            a = Anchor(norm(" ".join(self._anchor_parts)), self._anchor_href)
            if a.text:
                self.anchors.append(a)
                if self._row_depth:
                    self._row_anchors.append(a)
            self._anchor_href = None
            self._anchor_parts = []
        if tag.lower() == "tr" and self._row_depth:
            if self._row_depth == 1:
                text = norm(" ".join(self._row_parts))
                if text:
                    self.rows.append(Row(text, list(self._row_anchors)))
            self._row_depth -= 1


def parse_html(text: str, base_url: str) -> StructuredHTMLParser:
    p = StructuredHTMLParser(base_url)
    p.feed(text)
    return p


def classify_status(text: str, raw_status: str = "") -> str:
    t = low(text + " " + raw_status)
    if re.search(r"\b(in force|entered into force|effective date|effective on|final rule|final regulation|applies from)\b", t) or "现行" in t:
        return "In force"
    if "即将实施" in t:
        return "In progress"
    if re.search(r"\b(draft|proposal|proposed rule|notice of proposed rulemaking|nprm|consultation|public comment|proposed regulation)\b", t) or any(x in t for x in ["征求意见", "草案"]):
        return "Draft"
    if re.search(r"\b(in progress|working document|working party|work item|under development|rulemaking|agenda|programme of work|notice)\b", t) or any(x in t for x in ["研制", "审查", "立项"]):
        return "In progress"
    return "Status unclear"


def relevance_text(title: str, summary: str) -> str:
    t = low(title + " " + summary)
    parts: list[str] = []

    def add(terms: list[str], label: str) -> None:
        if any(low(x) in t for x in terms):
            parts.append(label)

    add(["type approval", "homologation", "certification", "conformity", "approval"], "May affect homologation, certification or conformity assessment")
    add(["validation", "verification", "scenario", "simulation", "virtual", "proving", "test method", "testing", "试验", "测试"], "May require adapted validation, simulation, scenario-based or physical test methods")
    add(["cybersecurity", "software update", "ota", "信息安全", "网络安全", "软件升级"], "May affect cybersecurity and software-update assessment")
    add(["connected", "connectivity", "v2x", "data access", "data recorder", "数据记录"], "May create data-access, data-recording or vehicle-connectivity test requirements")
    add(["automated", "autonomous", "adas", "driver assistance", "aleks", "dcas", "自动驾驶", "驾驶辅助"], "May influence automated-driving or driver-assistance test procedures")
    add(["artificial intelligence", "machine learning", " ai "], "May require evidence and assessment methods for AI-based vehicle functions")
    unique = list(dict.fromkeys(parts))
    if not unique:
        return "Potential Testing & Certification relevance identified from the connected and automated driving scope; detailed implications require source review."
    return "; ".join(unique[:2]) + "."


def strategic_questions(region: str, status: str, title: str, summary: str, relevance: str) -> str:
    t = low(" ".join([title, summary, relevance]))
    q: list[str] = []
    if re.search(r"simulation|scenario|validation|verification|test|testing|试验|测试", t):
        q.append("Could IAMTS develop a common testing or validation approach?")
    if re.search(r"type approval|homologation|certification|conformity|approval", t):
        q.append("Does this create a new certification or homologation service opportunity?")
    if re.search(r"cybersecurity|software|ota|data|connect", t):
        q.append("Are new competencies, tools or testing infrastructure required?")
    if region != "UNECE":
        q.append("Does this create a need for cross-regional harmonization?")
    if status in {"Draft", "In progress"}:
        q.append("Should IAMTS engage in this regulatory or standardization process?")
    if not q:
        q = ["Will this require new testing methods?", "Could IAMTS members collaborate on validation methods?"]
    return " ".join(list(dict.fromkeys(q))[:2])


def score_item(title: str, summary: str, relevance: str, status: str, date: str) -> dict[str, Any]:
    t = low(" ".join([title, summary, relevance]))
    tc = 2 if any(low(x) in t for x in T_AND_C_TERMS) else (1 if in_scope(t) else 0)
    urgency = 2 if status in {"In force", "Draft"} else (1 if status == "In progress" else 0)
    if date:
        try:
            d = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            days = abs((datetime.now(timezone.utc) - d).days)
            if days <= 120:
                urgency = max(urgency, 2)
            elif days <= 365:
                urgency = max(urgency, 1)
        except ValueError:
            pass
    impact_terms = ["automated driving", "autonomous", "type approval", "certification", "cybersecurity", "software update", "simulation", "safety assessment", "自动驾驶", "驾驶辅助"]
    impact = 2 if any(low(x) in t for x in impact_terms) else 1
    total = tc + urgency + impact
    priority = "High" if total >= 5 else "Medium" if total >= 3 else "Low"
    return {"tc": tc, "urgency": urgency, "impact": impact, "total": total, "priority": priority}


def normalize_item(raw: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    title = norm(raw.get("title"))
    summary = truncate(raw.get("summary"), 900)
    if not title or not in_scope(title + " " + summary):
        return None
    status = raw.get("status") or classify_status(title + " " + summary, norm(raw.get("raw_status")))
    date = parse_date_any(raw.get("date"))
    relevance = relevance_text(title, summary)
    questions = strategic_questions(source["region"], status, title, summary, relevance)
    score = score_item(title, summary, relevance, status, date)
    raw_key = norm(raw.get("key"))
    source_url = norm(raw.get("url")) or source["page_url"]
    item_id = stable_hash(f"{source['id']}|{low(raw_key or title)}|{'' if raw_key else source_url}")
    content_hash = stable_hash("|".join([title, date, status, summary]))
    return {
        "id": item_id,
        "region": source["region"],
        "sourceId": source["id"],
        "sourceName": source["name"],
        "sourceUrl": source_url,
        "title": truncate(title, 360),
        "date": date,
        "status": status,
        "summary": summary,
        "relevance": relevance,
        "questions": questions,
        "contentHash": content_hash,
        **score,
    }


def english_china_title(chinese: str, standard_no: str = "") -> str:
    t = norm(chinese)
    translations = [
        ("组合驾驶辅助系统安全要求", "Safety requirements for combined driver assistance systems"),
        ("自动驾驶功能仿真试验方法及要求", "Simulation test methods and requirements for automated driving functions"),
        ("自动驾驶功能道路试验方法及要求", "Road test methods and requirements for automated driving functions"),
        ("自动驾驶功能场地试验方法及要求", "Track testing methods and requirements for automated driving functions"),
        ("自动驾驶系统测试场景", "Automated driving system test scenarios"),
        ("自动驾驶系统设计运行条件", "Design operational conditions for automated driving systems"),
        ("自动驾驶系统通用技术要求", "General technical requirements for automated driving systems"),
        ("自动驾驶数据记录系统", "Automated driving data recording system"),
        ("自动泊车系统性能要求与试验方法", "Performance requirements and test methods for automated parking systems"),
        ("车载操作系统技术要求及试验方法", "Technical requirements and test methods for vehicle information operating systems"),
        ("车控操作系统技术要求及试验方法", "Technical requirements and test methods for vehicle-control operating systems"),
        ("数字身份及认证通用规范", "General specification for digital identity and authentication"),
    ]
    for needle, english in translations:
        if needle in t:
            return f"{standard_no} – {english}" if standard_no else english
    if "自动驾驶" in t:
        topic = "automated driving"
    elif "驾驶辅助" in t:
        topic = "driver assistance"
    elif "智能网联汽车" in t:
        topic = "intelligent and connected vehicles"
    else:
        topic = "connected and automated vehicles"
    return f"{standard_no} – Chinese national standard on {topic}" if standard_no else f"MIIT notice on {topic}"


def _binding_value(binding: dict[str, Any], key: str) -> str:
    value = binding.get(key, {}) if isinstance(binding, dict) else {}
    return norm(value.get("value", "")) if isinstance(value, dict) else ""


def _oj_series_from_uri(uri: str) -> str:
    m = re.search(r"/oj/([LC])[_/]", uri, re.I)
    return m.group(1).upper() if m else ""


def _oj_status(title: str, in_force: str, entry_date: str) -> str:
    if low(in_force) in {"true", "1"}:
        return "In force"
    if entry_date:
        try:
            if datetime.fromisoformat(entry_date).date() > datetime.now(timezone.utc).date():
                return "In progress"
        except ValueError:
            pass
    # Proposals and consultations in the C series should remain Draft; other
    # published acts without explicit legal-status metadata remain uncertain.
    classified = classify_status(title)
    return classified if classified in {"Draft", "In progress"} else "Status unclear"


def _eurlex_oj_via_sparql(source: dict[str, Any]) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, int(source.get("monitor_days", 14))) - 1)
    # Official Publications Office pattern for act-by-act Official Journal data:
    # cdm:official-journal-act_date_publication, plus the English expression title.
    query = f'''\
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?work ?act ?title ?date ?inforce ?entry
WHERE {{
  ?work cdm:official-journal-act_date_publication ?date .
  FILTER (?date >= "{start.isoformat()}"^^xsd:date && ?date <= "{end.isoformat()}"^^xsd:date)
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> ;
        cdm:expression_title ?title .
  OPTIONAL {{ ?work owl:sameAs ?act . FILTER(regex(str(?act), "/oj/|/eli/")) }}
  OPTIONAL {{ ?work cdm:resource_legal_in-force ?inforce . }}
  OPTIONAL {{ ?work cdm:resource_legal_date_entry-into-force ?entry . }}
}}
ORDER BY DESC(?date)
LIMIT {int(source.get("max_results", 1000))}
'''
    payload = _sparql_post_json(source["sparql_url"], query)
    bindings = payload.get("results", {}).get("bindings", [])
    if not isinstance(bindings, list):
        raise FetchFailure("CELLAR SPARQL returned an unexpected result structure")
    out: list[dict[str, Any]] = []
    for b in bindings:
        title = _binding_value(b, "title")
        if not title or not oj_in_scope(title):
            continue
        act = _binding_value(b, "act")
        work = _binding_value(b, "work")
        date = parse_date_any(_binding_value(b, "date"))
        entry = parse_date_any(_binding_value(b, "entry"))
        inforce = _binding_value(b, "inforce")
        uri = act or work or source["page_url"]
        series = _oj_series_from_uri(uri)
        status = _oj_status(title, inforce, entry)
        series_text = f" {series}-series" if series else ""
        detail = f"Official Journal{series_text} act published on {date or 'the monitored date'}"
        if entry:
            detail += f"; recorded entry-into-force date: {entry}"
        detail += "."
        out.append({
            "key": work or act or title,
            "title": title,
            "summary": detail,
            "date": date,
            "url": uri,
            "status": status,
        })
    return out


def _eurlex_date_from_daily_url(url: str) -> str:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        token = (q.get("ojDate") or [""])[0]
        if re.fullmatch(r"\d{8}", token):
            return datetime.strptime(token, "%d%m%Y").date().isoformat()
    except Exception:
        pass
    return ""


def _eurlex_oj_via_daily_pages(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback to the human-facing OJ direct-access page and recent daily views."""
    root_html = fetch_text(source["page_url"])
    root_parser = parse_html(root_html, source["page_url"])
    daily: list[str] = []
    seen: set[str] = set()
    for a in root_parser.anchors:
        if "/oj/daily-view/" not in a.url or a.url in seen:
            continue
        seen.add(a.url)
        daily.append(a.url)
        if len(daily) >= max(10, int(source.get("monitor_days", 14)) * 2):
            break
    if not daily:
        raise FetchFailure("EUR-Lex direct-access page contained no daily-view links")

    out: list[dict[str, Any]] = []
    successful_views = 0
    errors: list[tuple[str, Exception]] = []
    for url in daily:
        try:
            text = fetch_text(url)
            if re.search(r"verify that you(?:'|’)re not a robot|javascript is disabled", text, re.I):
                raise FetchFailure("EUR-Lex anti-bot page returned instead of the OJ daily view")
            parser = parse_html(text, url)
            successful_views += 1
        except Exception as exc:
            errors.append((url, exc))
            continue
        date = _eurlex_date_from_daily_url(url)
        # Rows normally preserve the OJ number plus the full title.
        for row in parser.rows:
            if len(row.text) < 25 or not oj_in_scope(row.text):
                continue
            link = next((a.url for a in row.anchors if a.url != url), url)
            out.append({
                "key": link or row.text,
                "title": truncate(row.text, 360),
                "summary": f"Official Journal act identified in the EUR-Lex daily view for {date or 'the monitored date'}.",
                "date": date,
                "url": link,
                "status": classify_status(row.text),
            })
        # Some EUR-Lex layouts expose titles as links without table rows.
        for a in parser.anchors:
            if len(a.text) < 25 or not oj_in_scope(a.text):
                continue
            if "/oj/daily-view/" in a.url:
                continue
            out.append({
                "key": a.url,
                "title": truncate(a.text, 360),
                "summary": f"Official Journal act identified in the EUR-Lex daily view for {date or 'the monitored date'}.",
                "date": date,
                "url": a.url,
                "status": classify_status(a.text),
            })
    if not successful_views:
        raise FetchFailure("EUR-Lex daily views could not be retrieved. " + error_summary(errors))
    return out


def _oeil_title(value: str) -> str:
    title = norm(value)
    return re.sub(r"@en$", "", title, flags=re.I).strip()


def _oeil_stage_from_page(text: str) -> str:
    plain = norm(text)
    m = re.search(
        r"Stage reached in procedure\s+(.{3,180}?)(?=\s+(?:Committee dossier|Documentation gateway|Legal basis|Procedure reference|Procedure type|$))",
        plain,
        re.I,
    )
    return norm(m.group(1)) if m else ""


def _oeil_status(stage: str) -> str:
    t = low(stage)
    if not t:
        return "In progress"
    if any(k in t for k in ["completed", "procedure completed", "final act", "act published", "enters into force"]):
        return "In force"
    if any(k in t for k in ["preparatory", "parliament", "council", "awaiting", "first reading", "second reading", "conciliation", "committee", "ongoing"]):
        return "In progress"
    return "Status unclear"


def adapter_ep_oeil(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Monitor relevant European Parliament procedures surfaced in OEIL.

    The retrieval uses the European Parliament Open Data CSV distributions,
    which mirror the procedure records behind the Legislative Observatory.
    Relevant entries link back to OEIL procedure files for human review.
    """
    out: list[dict[str, Any]] = []
    errors: list[tuple[str, Exception]] = []
    successful_years = 0
    current_year = datetime.now(timezone.utc).year
    candidates: list[dict[str, str]] = []

    for year in range(current_year - int(source.get("years_back", 3)) + 1, current_year + 1):
        url = source["distribution_template"].format(year=year)
        try:
            payload, _ = fetch_bytes(url)
            text = payload.decode("utf-8-sig", errors="replace")
            sample = text[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            successful_years += 1
            for row in reader:
                title = _oeil_title(row.get("process_title", ""))
                label = norm(row.get("label", ""))
                if not title or not label:
                    continue
                search_text = " ".join([title, label, norm(row.get("subtype", "")), norm(row.get("created_documents", ""))])
                if not oeil_in_scope(search_text):
                    continue
                candidates.append({
                    "title": title,
                    "label": label,
                    "process_id": norm(row.get("process_id", "")),
                    "stage_uri": norm(row.get("current_stage", "")),
                    "year": norm(row.get("year", "")) or str(year),
                })
        except Exception as exc:
            errors.append((url, exc))

    if not successful_years:
        raise RuntimeError("European Parliament procedure data could not be retrieved. " + error_summary(errors))

    # Deduplicate by procedure reference before optional OEIL detail enrichment.
    unique: dict[str, dict[str, str]] = {}
    for c in candidates:
        unique[c["label"]] = c
    candidates = list(unique.values())

    detail_budget = int(source.get("max_detail_fetch", 30))
    for i, c in enumerate(candidates):
        ref = c["label"]
        procedure_url = "https://oeil.europarl.europa.eu/oeil/en/procedure-file?reference=" + urllib.parse.quote(ref, safe="")
        stage = ""
        date = ""
        if i < detail_budget:
            try:
                detail_html = fetch_text(procedure_url)
                detail_text = html_to_text(detail_html)
                stage = _oeil_stage_from_page(detail_text)
                # The procedure page's first explicit date is sufficient as a
                # source date; weekly change detection is driven by content.
                date = extract_date(detail_text)
            except Exception:
                pass
        status = _oeil_status(stage)
        stage_note = stage or (c["stage_uri"].rsplit("/", 1)[-1] if c["stage_uri"] else "stage available in the procedure file")
        summary = (
            f"European Parliament procedure {ref} in the Legislative Observatory. "
            f"Current procedural stage: {stage_note}."
        )
        out.append({
            "key": ref,
            "title": f"{ref} – {c['title']}",
            "summary": summary,
            "date": date,
            "url": procedure_url,
            "raw_status": stage,
            "status": status,
        })
    return out


def adapter_eurlex_oj(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Monitor recent Official Journal acts relevant to IAMTS CAV scope.

    Prefer CELLAR/SPARQL because it is the Publications Office's documented
    machine-to-machine interface. Fall back to the EUR-Lex direct-access/daily
    view pages if the SPARQL endpoint is temporarily unavailable.
    """
    errors: list[str] = []
    try:
        return _eurlex_oj_via_sparql(source)
    except Exception as exc:
        errors.append(f"CELLAR/SPARQL: {norm(exc)}")
    try:
        return _eurlex_oj_via_daily_pages(source)
    except Exception as exc:
        errors.append(f"EUR-Lex daily view: {norm(exc)}")
    raise RuntimeError("Official Journal retrieval failed. " + " | ".join(errors))


def adapter_federal_register(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Retrieve recent regulatory actions from the official Federal Register API.

    The browser-oriented NHTSA/USDOT sites currently return HTTP 403 to GitHub
    runners. Federal Register is the official publication channel for US federal
    rules/notices and exposes a public machine-readable API, so it is the more
    reliable source for this regulatory radar.
    """
    out: list[dict[str, Any]] = []
    successes = 0
    errors: list[tuple[str, Exception]] = []
    monitor_days = max(1, int(source.get("monitor_days", 30)))
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=monitor_days - 1))
    for term in source["search_terms"]:
        params = urllib.parse.urlencode({
            "per_page": "60",
            "order": "newest",
            "conditions[agencies][]": source["agency"],
            "conditions[term]": term,
        })
        url = "https://www.federalregister.gov/api/v1/documents.json?" + params
        try:
            payload = json.loads(fetch_text(url))
            successes += 1
            for r in payload.get("results", []):
                title = norm(r.get("title"))
                summary = norm(r.get("abstract") or r.get("action"))
                pub_date = parse_date_any(r.get("publication_date"))
                if pub_date:
                    try:
                        if datetime.fromisoformat(pub_date).date() < cutoff:
                            continue
                    except ValueError:
                        pass
                if not in_scope(title + " " + summary):
                    continue
                out.append({
                    "key": r.get("document_number") or r.get("html_url") or title,
                    "title": title,
                    "summary": summary or f"Federal Register {norm(r.get('type') or 'document')} published by {source['name']}.",
                    "date": pub_date,
                    "url": r.get("html_url") or source["page_url"],
                    "raw_status": norm(r.get("type")),
                })
        except Exception as exc:
            errors.append((url, exc))
            continue
    if not successes:
        raise RuntimeError("Federal Register API requests failed. " + error_summary(errors))
    return out


def _marc_values(record: ET.Element, tag: str, codes: set[str] | None = None) -> list[str]:
    values: list[str] = []
    for field in record.iter():
        if not field.tag.endswith("datafield") or field.attrib.get("tag") != tag:
            continue
        for sub in field:
            if not sub.tag.endswith("subfield"):
                continue
            code = sub.attrib.get("code", "")
            if codes is None or code in codes:
                value = norm(sub.text)
                if value:
                    values.append(value)
    return values


def _marc_control(record: ET.Element, tag: str) -> str:
    for field in record.iter():
        if field.tag.endswith("controlfield") and field.attrib.get("tag") == tag:
            return norm(field.text)
    return ""


def _undl_record_to_raw(record: ET.Element, source: dict[str, Any]) -> dict[str, Any] | None:
    # MARC 245 is the title field. UN bibliographic records may carry document
    # symbols/subjects in several fields, so scope and symbol detection use all
    # available subfields rather than relying on one catalogue-specific tag.
    title_parts = _marc_values(record, "245", {"a", "b", "n", "p"})
    title = norm(" ".join(title_parts))
    all_values: list[str] = []
    for el in record.iter():
        if el.tag.endswith("subfield") and el.text:
            v = norm(el.text)
            if v:
                all_values.append(v)
    all_text = norm(" | ".join(all_values))
    if not title:
        # Conservative fallback: only use a sufficiently descriptive metadata
        # value; never invent a document title.
        title = next((v for v in all_values if len(v) >= 25 and in_scope(v)), "")
    if not title or not in_scope(title + " " + all_text):
        return None

    if source["id"] == "unece-grva":
        symbol_patterns = [
            r"ECE/TRANS/WP\.29/GRVA/20\d{2}/\d+(?:/Rev\.\d+)?",
            r"GRVA-\d{1,3}-\d+(?:-Rev\.\d+)?",
        ]
    else:
        symbol_patterns = [
            r"ECE/TRANS/WP\.29/20\d{2}/\d+(?:/Rev\.\d+)?",
            r"WP\.29-\d{1,3}-\d+(?:-Rev\.\d+)?",
        ]
    symbol = ""
    for pattern in symbol_patterns:
        m = re.search(pattern, all_text, re.I)
        if m:
            symbol = norm(m.group(0))
            break

    notes = []
    for tag in ["500", "520", "650", "710"]:
        notes.extend(_marc_values(record, tag))
    notes = [n for n in notes if low(n) != low(title)]
    summary = truncate(" ".join(notes), 700) if notes else "Official UN Digital Library metadata for a UNECE vehicle-regulation document matching the IAMTS monitoring scope."

    date_text = " ".join(_marc_values(record, "269") + _marc_values(record, "260") + _marc_values(record, "264"))
    date = extract_date(date_text)
    if not date:
        control_005 = _marc_control(record, "005")
        m = re.match(r"(20\d{2})(\d{2})(\d{2})", control_005)
        if m:
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    recid = _marc_control(record, "001")
    if symbol:
        url = "https://docs.un.org/" + urllib.parse.quote(symbol, safe="/")
    elif recid:
        url = f"https://digitallibrary.un.org/record/{urllib.parse.quote(recid)}"
    else:
        url = source["page_url"]
    return {
        "key": symbol or recid or title,
        "title": title,
        "summary": summary,
        "date": date,
        "url": url,
        "raw_status": "working document",
    }


def _json_strings(value: Any) -> list[str]:
    """Flatten strings from an unknown recjson structure conservatively."""
    out: list[str] = []
    if isinstance(value, str):
        v = norm(value)
        if v:
            out.append(v)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_json_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_json_strings(v))
    return out


def _undl_json_record_to_raw(record: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    """Convert UNDL recjson without depending on one catalogue schema version."""
    strings = _json_strings(record)
    if not strings:
        return None
    all_text = norm(" | ".join(strings))

    if source["id"] == "unece-grva":
        symbol_patterns = [
            r"ECE/TRANS/WP\.29/GRVA/20\d{2}/\d+(?:/Rev\.\d+)?",
            r"ECE/TRANS/WP\.29/GRVA/\d+(?:/Rev\.\d+)?",
        ]
    else:
        symbol_patterns = [
            r"ECE/TRANS/WP\.29/20\d{2}/\d+(?:/Rev\.\d+)?",
            r"ECE/TRANS/WP\.29/\d+(?:/Rev\.\d+)?",
        ]
    symbol = ""
    for pattern in symbol_patterns:
        m = re.search(pattern, all_text, re.I)
        if m:
            symbol = norm(m.group(0))
            break
    if not symbol:
        return None

    # Prefer explicit title-like keys if the JSON representation exposes them.
    title_candidates: list[str] = []
    if isinstance(record, dict):
        for key in ("title", "titles", "title_statement", "document_title", "245"):
            if key in record:
                title_candidates.extend(_json_strings(record.get(key)))
    title = next((x for x in title_candidates if len(x) >= 12 and x != symbol), "")
    if not title:
        # Safe generic fallback: use the longest descriptive string, never invent text.
        candidates = [
            x for x in strings
            if len(x) >= 18 and x != symbol and not x.startswith("http")
            and not re.fullmatch(r"[A-Z0-9./_-]+", x)
        ]
        candidates.sort(key=len, reverse=True)
        title = candidates[0] if candidates else ""
    if not title or not in_scope(title + " " + all_text):
        return None

    date = ""
    if isinstance(record, dict):
        for key in ("creation_date", "publication_date", "date", "release_date"):
            if key in record:
                vals = _json_strings(record.get(key))
                if vals:
                    date = parse_date_any(vals[0]) or extract_date(vals[0])
                    if date:
                        break
    if not date:
        date = extract_date(all_text)

    recid = ""
    if isinstance(record, dict):
        for key in ("recid", "record_id", "id"):
            val = record.get(key)
            if isinstance(val, (str, int)) and str(val).strip():
                recid = str(val).strip()
                break
    url = "https://docs.un.org/en/" + urllib.parse.quote(symbol, safe="/.") + "?direct=true"
    return {
        "key": symbol or recid or title,
        "title": title,
        "summary": "Official United Nations bibliographic metadata for a UNECE vehicle-regulation document matching the IAMTS monitoring scope.",
        "date": date,
        "url": url,
        "raw_status": "working document",
    }


def _undl_search_json(source: dict[str, Any], pattern: str, field: str = "") -> list[dict[str, Any]]:
    params = {"ln": "en", "p": pattern, "of": "recjson", "rg": "50"}
    if field:
        params["f"] = field
    url = "https://digitallibrary.un.org/search?" + urllib.parse.urlencode(params)
    text = fetch_text(url)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchFailure(f"UN Digital Library JSON response could not be parsed: {exc}") from exc
    records: list[Any]
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        candidate = payload.get("records") or payload.get("results") or payload.get("hits") or []
        records = candidate if isinstance(candidate, list) else [payload]
    else:
        records = []
    return [item for rec in records if (item := _undl_json_record_to_raw(rec, source))]


def _undl_search_xml(source: dict[str, Any], pattern: str, field: str = "") -> list[dict[str, Any]]:
    # Request only fields used by the radar. This also avoids catalogue fields
    # that have occasionally produced malformed MARCXML in the public endpoint.
    params = {
        "ln": "en", "p": pattern, "of": "xm", "rg": "50",
        "ot": "001,005,191,245,260,264,269,500,520,650,710",
    }
    if field:
        params["f"] = field
    url = "https://digitallibrary.un.org/search?" + urllib.parse.urlencode(params)
    xml_bytes, _ = fetch_bytes(url)
    records: list[ET.Element] = []
    try:
        root = ET.fromstring(xml_bytes)
        records = [el for el in root.iter() if el.tag.endswith("record")]
    except ET.ParseError as exc:
        # A single malformed catalogue fragment should not discard all valid
        # records. Salvage complete <record> blocks independently.
        text = xml_bytes.decode("utf-8", errors="replace")
        for chunk in re.findall(r"(?is)<record\b[^>]*>.*?</record>", text):
            try:
                records.append(ET.fromstring(chunk))
            except ET.ParseError:
                continue
        if not records:
            raise FetchFailure(f"UN Digital Library returned invalid XML: {exc}") from exc
    return [item for record in records if (item := _undl_record_to_raw(record, source))]


def _undl_search(source: dict[str, Any], pattern: str, field: str = "") -> list[dict[str, Any]]:
    errors: list[str] = []
    try:
        return _undl_search_json(source, pattern, field)
    except Exception as exc:
        errors.append("JSON: " + norm(exc))
    try:
        return _undl_search_xml(source, pattern, field)
    except Exception as exc:
        errors.append("MARCXML: " + norm(exc))
    raise FetchFailure("UN Digital Library search failed — " + " | ".join(errors[-2:]))



def _ods_symbol_access_url(symbol: str, file_type: str = "docx") -> str:
    """Official ODS direct-access URL for an exact UN document symbol."""
    return "https://documents.un.org/api/symbol/access?" + urllib.parse.urlencode({
        "s": symbol, "l": "en", "t": file_type,
    })


def _docx_paragraphs_and_metadata(data: bytes) -> tuple[list[str], str, str]:
    """Extract paragraphs, optional document title and date from an ODS DOCX.

    Uses only Python's standard library. If ODS returns an HTML error/loading
    shell for a symbol that does not exist, this function simply returns no
    paragraphs rather than treating the shell as a document.
    """
    if not data.startswith(b"PK"):
        return [], "", ""
    bio = io.BytesIO(data)
    if not zipfile.is_zipfile(bio):
        return [], "", ""
    bio.seek(0)
    try:
        with zipfile.ZipFile(bio) as zf:
            if "word/document.xml" not in zf.namelist():
                return [], "", ""
            root = ET.fromstring(zf.read("word/document.xml"))
            paragraphs: list[str] = []
            for p in root.iter():
                if not p.tag.endswith("}p") and p.tag != "p":
                    continue
                bits: list[str] = []
                for child in p.iter():
                    if child.tag.endswith("}t") or child.tag == "t":
                        if child.text:
                            bits.append(child.text)
                text = norm("".join(bits))
                if text:
                    paragraphs.append(text)

            core_title = ""
            core_date = ""
            if "docProps/core.xml" in zf.namelist():
                try:
                    core = ET.fromstring(zf.read("docProps/core.xml"))
                    for el in core.iter():
                        local = el.tag.rsplit("}", 1)[-1]
                        if local == "title" and el.text and not core_title:
                            core_title = norm(el.text)
                        elif local in {"modified", "created"} and el.text and not core_date:
                            core_date = parse_date_any(el.text)
                except ET.ParseError:
                    pass
            return paragraphs, core_title, core_date
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return [], "", ""


def _ods_best_title(paragraphs: list[str], core_title: str, symbol: str) -> str:
    """Choose a conservative official title from the DOCX itself."""
    candidates: list[str] = []
    if core_title and len(core_title) >= 12:
        candidates.append(core_title)
    skip = [
        "economic commission for europe", "world forum for harmonization",
        "working party on automated", "working party on automated/autonomous",
        "distr.:", "original: english", "agenda item", "provisional agenda",
    ]
    preferred = re.compile(
        r"^(?:proposal|draft|report|guidance|request|amendment|amendments|"
        r"status report|terms of reference|interpretation|new united nations regulation|"
        r"consolidated|information from)", re.I
    )
    for text in paragraphs[:90]:
        t = norm(text)
        if len(t) < 14 or len(t) > 420:
            continue
        lt = low(t)
        if symbol.lower() == lt or any(x in lt for x in skip):
            continue
        if preferred.search(t):
            candidates.append(t)
    if not candidates:
        for text in paragraphs[:90]:
            t = norm(text)
            if 18 <= len(t) <= 420 and in_scope(t) and not any(x in low(t) for x in skip):
                candidates.append(t)
    if not candidates:
        return ""
    # Prefer an in-scope title, then a regulatory title, preserving document order.
    return next((x for x in candidates if in_scope(x)), candidates[0])


def _ods_symbol_item(source: dict[str, Any], symbol: str) -> tuple[str, dict[str, Any] | None, str]:
    """Probe one exact ODS symbol. Returns (status, item, error).

    status is 'hit', 'miss' or 'error'. A miss is a successful ODS response for
    a symbol that is not issued / has no English DOCX and therefore still proves
    that the official endpoint is reachable.
    """
    url = _ods_symbol_access_url(symbol, "docx")
    try:
        data, content_type = fetch_bytes(url, timeout=14)
    except Exception as exc:
        return "error", None, norm(exc)

    paragraphs, core_title, core_date = _docx_paragraphs_and_metadata(data)
    if not paragraphs:
        # ODS commonly returns a small HTML shell for an unknown/unavailable
        # symbol. That is a normal miss, not a source outage.
        ctype = low(content_type)
        if "html" in ctype or data[:100].lstrip().lower().startswith((b"<!doctype", b"<html")):
            return "miss", None, ""
        return "miss", None, ""

    title = _ods_best_title(paragraphs, core_title, symbol)
    full_text = norm(" ".join(paragraphs[:160]))
    if not title:
        return "hit", None, ""

    # The source is intentionally scoped. A GRVA/WP.29 document is retained
    # only when its own title/body contains a configured CAV signal, including
    # UN R155/R156/R157/R171/R185.
    if not in_scope(title + " " + full_text):
        return "hit", None, ""

    relevant_context = [p for p in paragraphs[:160] if p != title and in_scope(p)]
    summary = truncate(" ".join(relevant_context[:2]), 700) if relevant_context else (
        "Official United Nations document retrieved directly from the UN Official Document System and matching the IAMTS connected and automated driving scope."
    )
    viewer = "https://docs.un.org/en/" + urllib.parse.quote(symbol, safe="/.")
    return "hit", {
        "key": symbol,
        "title": title,
        "summary": summary,
        "date": core_date,
        "url": viewer,
        "raw_status": "working document",
    }, ""


def _ods_symbol_scan_items(source: dict[str, Any]) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Discover current formal UNECE documents by probing exact ODS symbols.

    This avoids the UN Digital Library search export, whose JSON/MARCXML output
    is currently unreliable for GitHub-hosted automation. Only official ODS
    endpoints are queried.
    """
    prefix = source.get("symbol_prefix", "")
    upper = max(1, int(source.get("max_symbol_number", 100)))
    workers = max(1, min(12, int(source.get("scan_workers", 8))))
    years = [CURRENT_YEAR]
    # During the first two months of a year also look at the previous year's
    # formal cycle, because late revisions can still be published then.
    if datetime.now(timezone.utc).month <= 2:
        years.append(CURRENT_YEAR - 1)
    symbols = [f"{prefix}/{year}/{n}" for year in years for n in range(1, upper + 1)]

    out: list[dict[str, Any]] = []
    reachable = 0
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_ods_symbol_item, source, symbol): symbol for symbol in symbols}
        for future in concurrent.futures.as_completed(future_map):
            symbol = future_map[future]
            try:
                status, item, error = future.result()
            except Exception as exc:
                status, item, error = "error", None, norm(exc)
            if status in {"hit", "miss"}:
                reachable += 1
            elif error and len(errors) < 6:
                errors.append(f"{symbol}: {error}")
            if item:
                out.append(item)

    out.sort(key=lambda x: (x.get("date", ""), x.get("key", "")), reverse=True)
    return out, reachable, errors

_ODS_DAY_CACHE: dict[str, tuple[bool, list[Row], str]] = {}


def _ods_day(day: datetime) -> tuple[bool, list[Row], str]:
    """Retrieve one official ODS daily-document page, cached across sources."""
    key = day.date().isoformat()
    if key in _ODS_DAY_CACHE:
        return _ODS_DAY_CACHE[key]
    url = "https://documents.un.org/daily-list?" + urllib.parse.urlencode({"d": key})
    try:
        html_text = fetch_text(url)
        parser = parse_html(html_text, url)
        # ODS can render a loading shell if its backend is unavailable. Treat
        # that as a failed day rather than a successful empty day.
        plain = html_to_text(html_text)
        if "L O A D I N G" in plain and not parser.rows:
            raise FetchFailure("ODS returned a loading shell without document rows")
        result = (True, parser.rows, url)
    except Exception as exc:
        result = (False, [], f"{url}: {norm(exc)}")
    _ODS_DAY_CACHE[key] = result
    return result


def _ods_title_from_row(row_text: str, symbol: str) -> str:
    text = norm(row_text)
    pos = text.lower().find(symbol.lower())
    if pos >= 0:
        text = norm(text[pos + len(symbol):])
    # Daily-list rows place the duty station after the title.
    text = re.split(r"\s+(?:Geneva|New York|Vienna|Bangkok|Nairobi|Addis Ababa)\s+", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"^(?:\||[-–—:]\s*)+", "", text).strip()
    return truncate(text, 360)


def _ods_recent_items(source: dict[str, Any]) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Scan recent UN ODS release lists for WP.29/GRVA document symbols."""
    days = max(14, int(source.get("ods_days", 75)))
    out: list[dict[str, Any]] = []
    success_days = 0
    errors: list[str] = []
    today = datetime.now(timezone.utc)
    symbol_re = re.compile(r"ECE/TRANS/WP\.29(?:/GRVA)?/(?:20\d{2}/\d+|\d+)(?:/Rev\.\d+)?", re.I)
    for offset in range(days):
        day = today - timedelta(days=offset)
        ok, rows, info = _ods_day(day)
        if not ok:
            if len(errors) < 4:
                errors.append(info)
            continue
        success_days += 1
        for row in rows:
            m = symbol_re.search(row.text)
            if not m:
                continue
            symbol = norm(m.group(0))
            is_grva = "/GRVA/" in symbol.upper()
            if source["id"] == "unece-grva" and not is_grva:
                continue
            if source["id"] == "unece-wp29" and is_grva:
                continue
            title = _ods_title_from_row(row.text, symbol)
            # For GRVA, retain its official documents if they carry a CAV signal.
            # For parent WP.29, require the title itself to match the IAMTS scope.
            if not title or not in_scope(title + " " + row.text):
                continue
            link = next((a.url for a in row.anchors if "documents.un.org" in a.url or "docs.un.org" in a.url), "")
            if not link:
                link = "https://docs.un.org/en/" + urllib.parse.quote(symbol, safe="/.") + "?direct=true"
            out.append({
                "key": symbol,
                "title": title,
                "summary": f"Official UN document released through the United Nations Official Document System on {day.date().isoformat()}.",
                "date": day.date().isoformat(),
                "url": link,
                "raw_status": "working document",
            })
    return out, success_days, errors


def _unece_wiki_items(source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    html_text = fetch_text(url)
    parser = parse_html(html_text, url)
    page_text = html_to_text(html_text)
    out=[]
    for a in parser.anchors:
        t=norm(a.text)
        if len(t) < 12 or not in_scope(t):
            continue
        if not re.search(r"(?:GRVA|ADS|ADAS|CS/OTA|ECE[-_/ ]TRANS[-_/ ]WP\.?29).*(?:\d|proposal|guidance|report)|(?:proposal|guidance|testing|validation|cyber|software).*(?:GRVA|ADS|ADAS)", t, re.I):
            continue
        out.append({
            "key": a.url,
            "title": truncate(t, 340),
            "summary": "UNECE informal-working-group document identified in the official UNECE Transport Vehicle Regulations workspace.",
            "date": extract_date(t) or extract_date(page_text),
            "url": a.url,
            "raw_status": "working document",
        })
    return out


def adapter_unece_official(source: dict[str, Any]) -> list[dict[str, Any]]:
    """UNECE monitoring via direct UN Official Document System symbols.

    UNECE's public HTML pages return HTTP 403 to GitHub-hosted runners and the
    UN Digital Library search export currently returns empty JSON / malformed
    MARCXML. This adapter therefore discovers formal WP.29/GRVA documents by
    probing their predictable official UN symbols against ODS directly.
    """
    out: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        ods_items, reachable, ods_errors = _ods_symbol_scan_items(source)
        out.extend(ods_items)
        if not reachable:
            details = " | ".join(ods_errors[:3]) if ods_errors else "no ODS responses received"
            raise RuntimeError("UN ODS direct symbol access failed. " + details)
    except Exception as exc:
        raise RuntimeError("Official UNECE/UN document access failed. " + norm(exc)) from exc

    # Optional official UNECE Wiki supplement for GRVA informal documents.
    # Wiki failure never marks the formal ODS source unavailable.
    for url in source.get("wiki_urls", []):
        try:
            out.extend(_unece_wiki_items(source, url))
        except Exception:
            pass

    unique: dict[str, dict[str, Any]] = {}
    for item in out:
        key = norm(item.get("key")) or norm(item.get("url")) or norm(item.get("title"))
        if key and key not in unique:
            unique[key] = item
    return list(unique.values())

def adapter_rss(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    successes = 0
    errors: list[tuple[str, Exception]] = []
    for url in source["feed_urls"]:
        try:
            xml_bytes, _ = fetch_bytes(url)
            root = ET.fromstring(xml_bytes)
            successes += 1
        except Exception as exc:
            errors.append((url, exc))
            continue
        items = list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        for node in items:
            def text_of(names: list[str]) -> str:
                for name in names:
                    el = node.find(name)
                    if el is not None and el.text:
                        return norm(re.sub(r"<[^>]+>", " ", html_lib.unescape(el.text)))
                return ""
            title = text_of(["title", "{http://www.w3.org/2005/Atom}title"])
            desc = text_of(["description", "summary", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"])
            link = text_of(["link"])
            if not link:
                atom_link = node.find("{http://www.w3.org/2005/Atom}link")
                link = atom_link.attrib.get("href", "") if atom_link is not None else ""
            date = text_of(["pubDate", "date", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"])
            if in_scope(title + " " + desc):
                out.append({"key": link or title, "title": title, "summary": desc, "date": date, "url": link or source["page_url"]})
    if not successes:
        raise RuntimeError("Official RSS feeds could not be retrieved. " + error_summary(errors))
    return out


def adapter_official_pages(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    successes = 0
    errors: list[tuple[str, Exception]] = []
    for url in source["urls"]:
        try:
            html_text = fetch_text(url)
            parser = parse_html(html_text, url)
            successes += 1
        except Exception as exc:
            errors.append((url, exc))
            continue
        for a in parser.anchors:
            t = norm(a.text)
            if len(t) < 18 or not in_scope(t) or not text_has_any(t, REGULATORY_TERMS):
                continue
            if a.url.rstrip("/") == url.rstrip("/"):
                continue
            out.append({
                "key": a.url,
                "title": t,
                "summary": f"Official {source['name']} item matching the configured connected and automated driving regulatory scope.",
                "date": extract_date(t),
                "url": a.url,
            })
        # Some landing pages contain the relevant development in their own
        # headline/body rather than as a separate child link. Do not invent an
        # item, but preserve an explicit dated regulatory headline if present.
        page_text = html_to_text(html_text)
        if in_scope(page_text) and text_has_any(page_text, REGULATORY_TERMS):
            for pattern in [
                r"([^.!?]{15,220}(?:automated vehicle|automated driving|driver assistance|vehicle cybersecurity|software update)[^.!?]{0,220})",
                r"([^.!?]{15,220}(?:AV Framework|AV safety|ADS)[^.!?]{0,220})",
            ]:
                m = re.search(pattern, page_text, re.I)
                if m:
                    candidate = norm(m.group(1))
                    if in_scope(candidate) and text_has_any(candidate, REGULATORY_TERMS):
                        out.append({
                            "key": url + "#page-content",
                            "title": truncate(candidate, 280),
                            "summary": f"Official {source['name']} page content matching the monitoring scope.",
                            "date": extract_date(page_text),
                            "url": url,
                        })
                    break
    if not successes:
        raise RuntimeError("Official source pages could not be retrieved. " + error_summary(errors))
    return out

def _unece_document_from_text(text: str, url: str, source: dict[str, Any], date: str = "") -> dict[str, Any] | None:
    """Create a conservative UNECE record from a document-labelled text block."""
    t = norm(text)
    if not t or not in_scope(t):
        return None
    if source["id"] == "unece-grva":
        symbol_re = r"ECE[/_\s-]*TRANS[/_\s-]*WP\.?29[/_\s-]*GRVA[/_\s-]*20\d{2}[/_\s-]*\d+(?:[/_\s-]*Rev\.?\s*\d+)?|GRVA-\d{1,3}-\d+"
    else:
        symbol_re = r"ECE[/_\s-]*TRANS[/_\s-]*WP\.?29[/_\s-]*20\d{2}[/_\s-]*\d+(?:[/_\s-]*Rev\.?\s*\d+)?|WP\.29-\d{1,3}-\d+"
    m = re.search(symbol_re, t, re.I)
    if not m:
        return None
    symbol = norm(m.group(0)).replace("_", "/")
    return {
        "key": symbol,
        "title": truncate(t, 340),
        "summary": f"UNECE working or informal document identified on an official {source['name']} page.",
        "date": date or extract_date(t),
        "url": url,
        "raw_status": "working document",
    }


def _extract_unece_page(html_text: str, url: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    parser = parse_html(html_text, url)
    page_text = html_to_text(html_text)
    page_date = extract_date(page_text)
    out: list[dict[str, Any]] = []

    # Most UNECE meeting pages expose document metadata in table rows.
    for row in parser.rows:
        item = _unece_document_from_text(row.text, url, source, page_date)
        if not item:
            continue
        link = next((a.url for a in row.anchors if re.search(r"\.pdf(?:$|\?)|\.docx?(?:$|\?)|/transport/documents/", a.url, re.I)), None)
        if link:
            item["url"] = link
        out.append(item)

    # Fallback for lists rendered mostly as links.
    for a in parser.anchors:
        item = _unece_document_from_text(a.text, a.url, source, page_date)
        if item:
            out.append(item)

    # The generic UNECE document index can render title and symbol as separate
    # elements. Extract symbol-centred text windows as a final official-source
    # fallback. Items are still required to match the CAD scope.
    if source["id"] == "unece-grva":
        plain_symbol = r"ECE/TRANS/WP\.29/GRVA/20\d{2}/\d+(?:/Rev\.\d+)?"
    else:
        plain_symbol = r"ECE/TRANS/WP\.29/20\d{2}/\d+(?:/Rev\.\d+)?"
    for m in re.finditer(plain_symbol, page_text, re.I):
        lo = max(0, m.start() - 260)
        hi = min(len(page_text), m.end() + 360)
        window = norm(page_text[lo:hi])
        # Cut at the nearest previous/next document symbol to avoid combining
        # several search results into one pseudo-title.
        prev_matches = list(re.finditer(plain_symbol, window[: m.start()-lo], re.I))
        if prev_matches:
            window = window[prev_matches[-1].end():]
        next_m = re.search(plain_symbol, window[m.end()-lo if m.end()-lo < len(window) else 0:], re.I)
        item = _unece_document_from_text(window, url, source, page_date)
        if item:
            out.append(item)

    # High-level UNECE news/fallback pages may contain a relevant dated headline
    # without a formal document symbol. These are included only when clearly
    # regulatory and in scope, and are explicitly labelled as UNECE page items.
    if not out and in_scope(page_text) and text_has_any(page_text, REGULATORY_TERMS):
        for a in parser.anchors:
            t = norm(a.text)
            if len(t) >= 20 and in_scope(t) and text_has_any(t, REGULATORY_TERMS):
                out.append({
                    "key": a.url,
                    "title": truncate(t, 340),
                    "summary": f"Official UNECE item matching the connected and automated driving regulatory scope.",
                    "date": extract_date(t) or page_date,
                    "url": a.url,
                    "raw_status": "",
                })
    return out


def adapter_unece_events(source: dict[str, Any]) -> list[dict[str, Any]]:
    event_urls = set(source.get("seed_urls", []))
    successful_pages = 0
    errors: list[tuple[str, Exception]] = []
    out: list[dict[str, Any]] = []

    # 1) Discover current meeting pages from the official vehicle-regulations
    # event index. Failure here is not fatal because seed/search fallbacks exist.
    for list_url in source.get("list_urls", []):
        try:
            html_text = fetch_text(list_url)
            parser = parse_html(html_text, list_url)
            successful_pages += 1
            for a in parser.anchors:
                if any(low(n) in low(a.text) for n in source.get("event_needles", [])) and "unece.org" in a.url and ("/event" in a.url or "/events" in a.url):
                    event_urls.add(a.url)
        except Exception as exc:
            errors.append((list_url, exc))

    # 2) Formal event pages / seeded current sessions.
    for url in sorted(event_urls, reverse=True)[: source.get("max_events", 5)]:
        try:
            html_text = fetch_text(url)
            successful_pages += 1
            out.extend(_extract_unece_page(html_text, url, source))
        except Exception as exc:
            errors.append((url, exc))

    # 3) Generic official UNECE document search pages. This uses a different
    # Drupal route and often remains available when event pages reject cloud IPs.
    for url in source.get("document_search_urls", []):
        try:
            html_text = fetch_text(url)
            successful_pages += 1
            out.extend(_extract_unece_page(html_text, url, source))
        except Exception as exc:
            errors.append((url, exc))

    # 4) Official vehicle-regulations/news landing pages. This is a limited
    # fallback for high-level changes; it never fabricates missing formal docs.
    for url in source.get("fallback_urls", []):
        try:
            html_text = fetch_text(url)
            successful_pages += 1
            out.extend(_extract_unece_page(html_text, url, source))
        except Exception as exc:
            errors.append((url, exc))

    if not successful_pages:
        raise RuntimeError("UNECE official pages could not be retrieved. " + error_summary(errors))

    # Deduplicate by the source key before normalization.
    unique: dict[str, dict[str, Any]] = {}
    for item in out:
        key = norm(item.get("key")) or norm(item.get("url")) or norm(item.get("title"))
        if key and key not in unique:
            unique[key] = item
    return list(unique.values())

def adapter_samr(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    successes = 0
    for url in source["search_urls"]:
        try:
            parser = parse_html(fetch_text(url), url)
            successes += 1
        except Exception:
            continue
        for row in parser.rows:
            m = re.search(r"\bGB(?:/T)?\s*\d+(?:\.\d+)?-20\d{2}\b", row.text, re.I)
            if not m or not in_scope(row.text):
                continue
            std_no = norm(m.group(0))
            dates = re.findall(r"20\d{2}-\d{2}-\d{2}", row.text)
            raw_status = "现行" if "现行" in row.text else "即将实施" if "即将实施" in row.text else ""
            detail = next((a.url for a in row.anchors if "newGbInfo" in a.url or "hcno=" in a.url), url)
            title = english_china_title(row.text, std_no)
            if raw_status == "现行":
                summary = f"Chinese national standard in force{f' (implementation date: {dates[1]})' if len(dates) > 1 else ''}."
                status = "In force"
            elif raw_status == "即将实施":
                summary = f"Chinese national standard published and scheduled for implementation{f' on {dates[1]}' if len(dates) > 1 else ''}."
                status = "In progress"
            else:
                summary = "Chinese national standard matching the configured connected and automated driving scope."
                status = "Status unclear"
            out.append({
                "key": std_no,
                "title": title,
                "summary": summary,
                "date": dates[0] if dates else "",
                "url": detail,
                "raw_status": raw_status,
                "status": status,
            })
    if not successes:
        raise RuntimeError("SAMR/SAC standards search pages could not be retrieved")
    return out


def adapter_miit(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    successes = 0
    for url in source["urls"]:
        try:
            parser = parse_html(fetch_text(url), url)
            successes += 1
        except Exception:
            continue
        for a in parser.anchors:
            if len(a.text) < 8 or not in_scope(a.text) or not text_has_any(a.text, REGULATORY_TERMS):
                continue
            raw_status = "征求意见" if any(x in a.text for x in ["征求意见", "草案"]) else ""
            out.append({
                "key": a.url,
                "title": english_china_title(a.text),
                "summary": "Official MIIT automotive-industry notice matching the connected and automated driving regulatory scope. The original source is in Chinese.",
                "date": extract_date(a.text),
                "url": a.url,
                "raw_status": raw_status,
            })
    if not successes:
        raise RuntimeError("MIIT automotive-industry pages could not be retrieved")
    return out


def collect_source(source: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        adapter = source["adapter"]
        if adapter == "ep_oeil":
            raw = adapter_ep_oeil(source)
        elif adapter == "eurlex_oj":
            raw = adapter_eurlex_oj(source)
        elif adapter == "federal_register":
            raw = adapter_federal_register(source)
        elif adapter == "rss":
            raw = adapter_rss(source)
        elif adapter == "unece_official":
            raw = adapter_unece_official(source)
        elif adapter == "unece_events":
            raw = adapter_unece_events(source)
        elif adapter == "samr":
            raw = adapter_samr(source)
        elif adapter == "miit":
            raw = adapter_miit(source)
        else:
            raw = adapter_official_pages(source)
        items = []
        seen = set()
        for r in raw:
            item = normalize_item(r, source)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        return {
            "source": source,
            "status": "success",
            "message": "Successfully checked",
            "entries": items,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "source": source,
            "status": "unavailable",
            "message": truncate(f"Currently unavailable · {type(exc).__name__}: {norm(exc)}", 360),
            "entries": [],
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_changes(fresh: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev = {e.get("id"): e for e in previous}
    result = []
    for item in fresh:
        old = prev.get(item["id"])
        change = "New" if not old else "Updated" if old.get("contentHash") != item.get("contentHash") else "No change"
        result.append({**item, "change": change})
    return result


def dominant_topic(entries: list[dict[str, Any]]) -> str:
    best = "No dominant topic"
    best_count = 0
    for topic, terms in TOPIC_RULES:
        count = sum(1 for e in entries if text_has_any(" ".join([e.get("title", ""), e.get("summary", ""), e.get("relevance", "")]), terms))
        if count > best_count:
            best, best_count = topic, count
    return best if best_count else "No dominant topic"


def run() -> int:
    checked_at = now_iso()
    state = load_json(STATE_FILE, {"schemaVersion": 1, "sources": {}})
    state.setdefault("schemaVersion", 1)
    state.setdefault("sources", {})

    current_entries: list[dict[str, Any]] = []
    source_states: list[dict[str, Any]] = []

    print(f"IAMTS Radar update started at {checked_at}")
    for source in SOURCES:
        result = collect_source(source)
        previous_source_state = state["sources"].get(source["id"], {})
        previous_entries = previous_source_state.get("entries", [])
        if result["status"] == "success":
            changed = apply_changes(result["entries"], previous_entries)
            current_entries.extend(changed)
            state["sources"][source["id"]] = {
                "lastSuccessfulCheck": checked_at,
                "entries": result["entries"],
            }
            print(f"  OK   {source['name']}: {len(changed)} matching item(s)")
        else:
            print(f"  WARN {source['name']}: {result['message']}")

        source_states.append({
            "id": source["id"],
            "region": source["region"],
            "name": source["name"],
            "url": source["page_url"],
            "status": result["status"],
            "message": result["message"],
            "count": len(result["entries"]),
            "checkedAt": checked_at,
            "lastSuccessfulCheck": checked_at if result["status"] == "success" else previous_source_state.get("lastSuccessfulCheck", ""),
            "elapsedMs": result["elapsedMs"],
        })

    current_entries.sort(key=lambda x: ({"High": 0, "Medium": 1, "Low": 2}.get(x["priority"], 3), x.get("date") or "9999-99-99", x["title"]))
    regions = {}
    for region in ["UNECE", "EU", "USA", "China"]:
        r_entries = [e for e in current_entries if e["region"] == region]
        regions[region] = {
            "developments": len(r_entries),
            "highPriority": sum(1 for e in r_entries if e["priority"] == "High"),
            "dominantTopic": dominant_topic(r_entries),
        }

    previous_updated = state.get("lastRun", "")
    state["lastRun"] = checked_at
    radar = {
        "schemaVersion": 1,
        "updatedAt": checked_at,
        "previousMonitoringRun": previous_updated,
        "scope": "Connected & Automated Driving – Regulation / Standardization – Testing & Certification",
        "entries": current_entries,
        "sources": source_states,
        "regionalSignals": regions,
    }

    write_json(STATE_FILE, state)
    write_json(PUBLIC_FILE, radar)
    try:
        display_path = PUBLIC_FILE.relative_to(ROOT)
    except ValueError:
        display_path = PUBLIC_FILE
    print(f"Wrote {len(current_entries)} radar item(s) to {display_path}")
    unavailable = sum(1 for s in source_states if s["status"] == "unavailable")
    print(f"Source status: {len(source_states)-unavailable} successful, {unavailable} unavailable")
    # A partial-source run is still successful; the UI must show unavailable sources.
    return 0


if __name__ == "__main__":
    sys.exit(run())
