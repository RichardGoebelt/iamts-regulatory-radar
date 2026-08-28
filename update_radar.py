#!/usr/bin/env python3
"""IAMTS Regulatory & Policy Radar – weekly source retrieval.

Server-side retrieval intended for GitHub Actions. No third-party packages.
The script reads data/state.json as monitoring memory, checks the fixed official
source list below, writes public/radar.json, and updates state only for sources
that were checked successfully.
"""

from __future__ import annotations

import email.utils
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        "name": "UNECE GRVA",
        "adapter": "unece_events",
        "page_url": "https://unece.org/transport/vehicle-regulations/working-party-automatedautonomous-and-connected-vehicles-introduction",
        "list_urls": [
            "https://unece.org/info/events/unece-meetings-and-events/vehicle-regulations"
        ],
        # Alternative official UNECE indexes. These are important because some
        # UNECE event pages intermittently reject requests from cloud runners.
        "document_search_urls": [
            f"https://unece.org/media/documents?key=&symbol=ECE%2FTRANS%2FWP.29%2FGRVA%2F{CURRENT_YEAR}&title=",
            "https://unece.org/media/documents?key=automated&symbol=ECE%2FTRANS%2FWP.29%2FGRVA&title=",
        ],
        "fallback_urls": [
            "https://unece.org/transport/vehicle-regulations",
            "https://unece.org/media/transport/Vehicle-Regulations/news/recent",
        ],
        # Seed pages make the first deployment useful even if the event-list
        # markup changes. New meetings are also discovered from list_urls.
        "seed_urls": [
            "https://unece.org/info/events/event/412104",
            "https://unece.org/info/events/event/411623",
        ],
        "event_needles": ["WP.29/GRVA", "GRVA"],
        "max_events": 6,
    },
    {
        "id": "unece-wp29",
        "region": "UNECE",
        "name": "UNECE WP.29",
        "adapter": "unece_events",
        "page_url": "https://unece.org/transport/vehicle-regulations",
        "list_urls": [
            "https://unece.org/info/events/unece-meetings-and-events/vehicle-regulations"
        ],
        "document_search_urls": [
            f"https://unece.org/media/documents?key=&symbol=ECE%2FTRANS%2FWP.29%2F{CURRENT_YEAR}&title=",
            f"https://unece.org/media/documents?key=GRVA&symbol=ECE%2FTRANS%2FWP.29%2F{CURRENT_YEAR}&title=",
        ],
        "fallback_urls": [
            "https://unece.org/transport/vehicle-regulations",
            "https://unece.org/media/transport/Vehicle-Regulations/news/recent",
        ],
        "seed_urls": [
            "https://unece.org/info/Transport/events/412348",
            "https://unece.org/info/Transport/Vehicle-Regulations/events/412348",
        ],
        "event_needles": [
            "(WP.29) World Forum",
            "World Forum for Harmonization of Vehicle Regulations",
        ],
        "max_events": 5,
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
        "id": "usa-federal-register",
        "region": "USA",
        "name": "Federal Register – NHTSA",
        "adapter": "federal_register",
        "page_url": "https://www.federalregister.gov/agencies/national-highway-traffic-safety-administration",
        "agency": "national-highway-traffic-safety-administration",
        "search_terms": [
            "automated vehicle",
            "automated driving",
            "advanced driver assistance",
            "vehicle cybersecurity",
            "vehicle software",
        ],
    },
    {
        "id": "usa-nhtsa",
        "region": "USA",
        "name": "NHTSA – Automated Vehicle Safety",
        "adapter": "official_pages",
        "page_url": "https://www.nhtsa.gov/vehicle-safety/automated-vehicle-safety",
        "urls": [
            "https://www.nhtsa.gov/vehicle-safety/automated-vehicle-safety",
            "https://www.nhtsa.gov/automated-vehicle-safety/resources",
            "https://www.nhtsa.gov/vehicle-manufacturers/automated-driving-systems",
            "https://www.nhtsa.gov/press-releases",
        ],
    },
    {
        "id": "usa-usdot",
        "region": "USA",
        "name": "U.S. DOT – Automated Vehicles",
        "adapter": "official_pages",
        "page_url": "https://www.transportation.gov/AV",
        "urls": [
            "https://www.transportation.gov/AV",
            "https://www.transportation.gov/taxonomy/term/12981",
            "https://www.transportation.gov/tags/automated-vehicles?page=0",
            "https://www.transportation.gov/newsroom/press-releases?keys=automated%20vehicle",
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
    "adas", "automated driving system", "automated driving systems", " ads ", "aleks", "dcas",
    "automated lane", "automated parking", "cybersecurity", "software update", "software updates",
    "vehicle software", "over-the-air", "type approval", "homologation", "vehicle approval",
    "vehicle certification", "validation", "verification", "simulation", "virtual testing",
    "scenario-based", "scenario based", "proving ground", "test method", "testing method",
    "safety assessment", "conformity assessment", "data access", "data recorder", "data recording",
    "vehicle connectivity", "v2x", "artificial intelligence", "machine learning",
    "intelligent transport systems",
    "智能网联汽车", "自动驾驶", "组合驾驶辅助", "驾驶辅助", "软件升级", "信息安全", "网络安全",
    "仿真试验", "场地试验", "道路试验", "测试场景", "安全评估", "数据记录",
]

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
            snippet = _body_snippet(data) if data else ""
            bits = [f"curl exit {proc.returncode}"]
            if final_status:
                bits.append(f"HTTP {final_status}")
            if detail:
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


def adapter_federal_register(source: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    successes = 0
    errors: list[tuple[str, Exception]] = []
    for term in source["search_terms"]:
        params = urllib.parse.urlencode({
            "per_page": "40",
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
                if not in_scope(title + " " + summary):
                    continue
                out.append({
                    "key": r.get("document_number") or r.get("html_url") or title,
                    "title": title,
                    "summary": summary or f"Federal Register {norm(r.get('type') or 'document')} published by NHTSA.",
                    "date": r.get("publication_date"),
                    "url": r.get("html_url") or source["page_url"],
                    "raw_status": norm(r.get("type")),
                })
        except Exception as exc:
            errors.append((url, exc))
            continue
    if not successes:
        raise RuntimeError("Federal Register API requests failed. " + error_summary(errors))
    return out


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
        if adapter == "federal_register":
            raw = adapter_federal_register(source)
        elif adapter == "rss":
            raw = adapter_rss(source)
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
            "message": truncate(f"Currently unavailable · {type(exc).__name__}: {norm(exc)}", 700),
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
