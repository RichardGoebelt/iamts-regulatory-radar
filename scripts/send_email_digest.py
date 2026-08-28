#!/usr/bin/env python3
"""Send an IAMTS Regulatory & Policy Radar email digest.

No third-party Python packages are required. SMTP credentials and recipients are
read exclusively from environment variables / GitHub Actions secrets.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RADAR_JSON = ROOT / "public" / "radar.json"


def norm(value: Any) -> str:
    return " ".join(str(value or "").split())


def esc(value: Any) -> str:
    return html.escape(norm(value), quote=True)


def get(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = norm(entry.get(key))
        if value:
            return value
    return ""


def parse_recipients(raw: str) -> list[str]:
    values = []
    for token in raw.replace(";", ",").split(","):
        address = token.strip()
        if address and "@" in address and address not in values:
            values.append(address)
    return values


def load_radar() -> dict[str, Any]:
    if not RADAR_JSON.exists():
        raise RuntimeError(f"Radar data not found: {RADAR_JSON}")
    with RADAR_JSON.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise RuntimeError("radar.json has an unexpected structure")
    payload["entries"] = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    return payload


def changed_entries(radar: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        e for e in radar["entries"]
        if isinstance(e, dict) and get(e, "change") in {"New", "Updated"}
    ]
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    change_rank = {"New": 2, "Updated": 1}
    items.sort(
        key=lambda e: (
            priority_rank.get(get(e, "priority"), 0),
            change_rank.get(get(e, "change"), 0),
            get(e, "date"),
            get(e, "title"),
        ),
        reverse=True,
    )
    return items


def format_updated_at(value: str) -> str:
    if not value:
        return "latest monitoring cycle"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    except ValueError:
        return value


def build_messages(radar: dict[str, Any], items: list[dict[str, Any]], site_url: str) -> tuple[str, str, str]:
    new_count = sum(get(e, "change") == "New" for e in items)
    updated_count = sum(get(e, "change") == "Updated" for e in items)
    high_count = sum(get(e, "priority") == "High" for e in items)
    subject = f"[IAMTS Radar] {new_count} new · {updated_count} updated · {high_count} high priority"
    updated_at = format_updated_at(norm(radar.get("updatedAt")))
    max_items = max(1, int(os.getenv("RADAR_EMAIL_MAX_ITEMS", "15")))
    shown = items[:max_items]

    text_lines = [
        "IAMTS Regulatory & Policy Radar – Connected & Automated Driving",
        "",
        f"Monitoring update: {updated_at}",
        f"Changes: {new_count} new · {updated_count} updated · {high_count} high priority",
        "",
    ]
    for i, entry in enumerate(shown, 1):
        title = get(entry, "title") or "Untitled development"
        source = get(entry, "sourceName", "source")
        source_url = get(entry, "sourceUrl", "url")
        text_lines.extend([
            f"{i}. [{get(entry, 'change')}] [{get(entry, 'priority')}] {title}",
            f"   {get(entry, 'region')} · {get(entry, 'status')} · {get(entry, 'date') or 'Date not stated'} · {source}",
            f"   {get(entry, 'summary')}",
            f"   Testing & Certification: {get(entry, 'relevance')}",
            f"   IAMTS: {get(entry, 'questions')}",
            f"   Source: {source_url}" if source_url else "",
            "",
        ])
    if len(items) > len(shown):
        text_lines.append(f"{len(items) - len(shown)} additional changed items are available in the full Radar.")
        text_lines.append("")
    text_lines.extend([
        f"Open the full IAMTS Radar: {site_url}",
        "",
        "This is an automated monitoring notification. Always verify important items against the linked official source.",
    ])

    cards = []
    for entry in shown:
        title = esc(get(entry, "title") or "Untitled development")
        source = esc(get(entry, "sourceName", "source"))
        source_url = get(entry, "sourceUrl", "url")
        source_link = f'<a href="{esc(source_url)}" style="color:#0e6095">{source}</a>' if source_url else source
        cards.append(f'''<div style="border:1px solid #d9e0e6;border-radius:7px;padding:12px 14px;margin:10px 0;background:#fff">
<div style="font-size:12px;margin-bottom:5px"><strong>{esc(get(entry,'change'))}</strong> · <strong>{esc(get(entry,'priority'))} priority</strong> · {esc(get(entry,'region'))} · {esc(get(entry,'status'))} · {esc(get(entry,'date') or 'Date not stated')}</div>
<div style="font-size:16px;font-weight:700;margin-bottom:6px">{title}</div>
<div style="font-size:13px;color:#475560;margin-bottom:6px">{esc(get(entry,'summary'))}</div>
<div style="font-size:13px"><strong>Testing &amp; Certification:</strong> {esc(get(entry,'relevance'))}</div>
<div style="font-size:13px;margin-top:4px"><strong>IAMTS:</strong> {esc(get(entry,'questions'))}</div>
<div style="font-size:12px;margin-top:7px">Source: {source_link}</div>
</div>''')

    more = ""
    if len(items) > len(shown):
        more = f'<p style="color:#667482">{len(items)-len(shown)} additional changed items are available in the full Radar.</p>'
    html_body = f'''<!doctype html><html><body style="margin:0;background:#f5f7f9;font-family:Arial,Helvetica,sans-serif;color:#1b2732">
<div style="max-width:820px;margin:0 auto;padding:20px">
<div style="background:#113b59;color:#fff;padding:18px 22px;border-radius:8px 8px 0 0"><div style="font-size:22px;font-weight:700">IAMTS Regulatory &amp; Policy Radar</div><div style="color:#d9e7ef;margin-top:3px">Connected &amp; Automated Driving</div></div>
<div style="background:#fff;border:1px solid #d9e0e6;border-top:0;padding:18px 22px;border-radius:0 0 8px 8px">
<p style="margin-top:0">Monitoring update: <strong>{esc(updated_at)}</strong></p>
<p><strong>{new_count} new</strong> · <strong>{updated_count} updated</strong> · <strong>{high_count} high priority</strong></p>
{''.join(cards)}{more}
<p style="margin-top:18px"><a href="{esc(site_url)}" style="display:inline-block;background:#155f8d;color:#fff;text-decoration:none;padding:10px 14px;border-radius:6px;font-weight:700">Open full IAMTS Radar</a></p>
<p style="font-size:11px;color:#667482;margin-bottom:0">Automated monitoring notification. Verify important items against the linked official source.</p>
</div></div></body></html>'''
    return subject, "\n".join(line for line in text_lines if line is not None), html_body


def send_email(subject: str, text_body: str, html_body: str, recipients: list[str]) -> None:
    host = norm(os.getenv("SMTP_HOST"))
    port_raw = norm(os.getenv("SMTP_PORT")) or "587"
    username = norm(os.getenv("SMTP_USERNAME"))
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = norm(os.getenv("SMTP_FROM")) or username
    reply_to = norm(os.getenv("SMTP_REPLY_TO"))

    if not host or not from_addr:
        raise RuntimeError("SMTP_HOST and SMTP_FROM (or SMTP_USERNAME) must be configured")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a number") from exc

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "undisclosed-recipients:;"
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)


def main() -> int:
    recipients = parse_recipients(os.getenv("RADAR_EMAIL_RECIPIENTS", ""))
    if not recipients:
        print("Email notifications are not configured: RADAR_EMAIL_RECIPIENTS is empty. Skipping.")
        return 0

    radar = load_radar()
    items = changed_entries(radar)
    send_empty = norm(os.getenv("RADAR_EMAIL_SEND_EMPTY")).lower() in {"1", "true", "yes"}
    if not items and not send_empty:
        print("No New or Updated radar entries. No email sent.")
        return 0

    site_url = norm(os.getenv("RADAR_SITE_URL")) or "https://richardgoebelt.github.io/iamts-regulatory-radar/"
    subject, text_body, html_body = build_messages(radar, items, site_url)
    send_email(subject, text_body, html_body, recipients)
    print(f"IAMTS Radar email notification sent to {len(recipients)} configured recipient(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Email notification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
