# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import base64
import os
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def get_gmail_service(credentials_path: str, token_path: str):
    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Không tìm thấy credentials file: {credentials_path}"
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path,
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        Path(token_path).parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def create_html_message(
    sender: str,
    to: str,
    subject: str,
    html_body: str,
    cc: str | None = None,
    bcc: str | None = None,
):
    message = MIMEText(html_body, "html", "utf-8")
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject

    if cc:
        message["Cc"] = cc

    if bcc:
        message["Bcc"] = bcc

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": raw}


def main():
    parser = argparse.ArgumentParser(
        description="Send CEO Growth Report HTML via Gmail API."
    )
    parser.add_argument("--html", required=True, help="Path to HTML report file")
    parser.add_argument("--to", required=True, help="CEO email")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--sender", default="me", help="Sender email or 'me'")
    parser.add_argument("--cc", default=None)
    parser.add_argument("--bcc", default=None)
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Google OAuth client credentials JSON",
    )
    parser.add_argument(
        "--token",
        default="token_gmail_send.json",
        help="OAuth token file",
    )
    args = parser.parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file HTML: {html_path}")

    html_body = html_path.read_text(encoding="utf-8")

    if "```" in html_body:
        raise ValueError("HTML còn chứa markdown code fence ```; cần sửa trước khi gửi.")

    if "</html>" not in html_body.lower():
        raise ValueError("HTML chưa có thẻ </html>; cần kiểm tra lại file.")

    service = get_gmail_service(args.credentials, args.token)

    message = create_html_message(
        sender=args.sender,
        to=args.to,
        subject=args.subject,
        html_body=html_body,
        cc=args.cc,
        bcc=args.bcc,
    )

    sent = service.users().messages().send(userId="me", body=message).execute()

    print("Sent Gmail successfully.")
    print("Message ID:", sent.get("id"))


if __name__ == "__main__":
    main()