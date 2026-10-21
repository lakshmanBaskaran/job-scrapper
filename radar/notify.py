"""Build the daily digest and send it over SMTP (Gmail app password works)."""
import html
import os
import smtplib
from collections import defaultdict
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

LABEL = {"swe": "SWE", "ai": "AI", "cloud": "Cloud"}


def build_digest(matches, errors):
    """matches: list of (Job, Verdict). Returns (subject, html_body, text_body)."""
    by_co = defaultdict(list)
    for job, v in matches:
        by_co[job.company].append((job, v))

    subject = f"Job radar {date.today():%d %b}: {len(matches)} new entry-level role" + ("s" if len(matches) != 1 else "")
    h = [f"<h2 style='font-family:sans-serif'>{len(matches)} new roles</h2>"]
    t = [subject, ""]
    for co in sorted(by_co):
        h.append(f"<h3 style='font-family:sans-serif;margin-bottom:4px'>{html.escape(co)}</h3><ul>")
        t.append(co)
        for job, v in by_co[co]:
            tag = LABEL.get(v.category, v.category)
            h.append(
                f"<li style='font-family:sans-serif;margin-bottom:6px'>"
                f"<a href='{html.escape(job.url)}'>{html.escape(job.title)}</a> "
                f"<span style='color:#666'>· {html.escape(job.location)} · {tag}</span><br>"
                f"<small style='color:#888'>{html.escape(v.reason)} ({v.source})</small></li>")
            t.append(f"  - {job.title} | {job.location} | {tag}\n    {job.url}")
        h.append("</ul>")
        t.append("")
    if errors:
        h.append("<p style='font-family:sans-serif;color:#a33'><b>Feeds that failed:</b><br>"
                 + "<br>".join(html.escape(e) for e in errors) + "</p>")
        t += ["Feeds that failed:"] + [f"  {e}" for e in errors]
    return subject, "\n".join(h), "\n".join(t)


def send_email(subject, html_body, text_body):
    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)}. Add them to the .env file in the project folder.")
    user = os.environ["SMTP_USER"]
    to = os.environ.get("MAIL_TO", user)
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    pw = os.environ["SMTP_PASSWORD"].replace(" ", "")  # Google shows app passwords with spaces
    try:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, pw)
            s.send_message(msg)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
        raise RuntimeError(
            f"Gmail rejected the login for {user} (password length {len(pw)}; an app password is 16). "
            "Check SMTP_PASSWORD in .env is a Gmail app password from myaccount.google.com/apppasswords."
        ) from e