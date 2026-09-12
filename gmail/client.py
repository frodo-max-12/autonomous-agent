"""
SemiSales AI Agent - Gmail Client
Handles Gmail API connection, reading emails, sending replies, and monitoring inboxes.
"""

import base64
import os
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

# Thread IDs that returned 404 (thread not found) — usually a stale thread_id in the DB from a
# DIFFERENT mailbox (e.g. an old RFQ sent while the agent was on pm@, now that we read intl.sales@).
# We warn once per thread instead of spamming ERROR on every polling cycle.
_WARNED_MISSING_THREADS: set[str] = set()


class GmailClient:
    """Handles all Gmail API operations for a single inbox."""

    def __init__(
        self,
        account_name: str,
        credentials_file: str,
        token_file: str,
        scopes: list[str],
    ):
        self.account_name = account_name
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.scopes = scopes
        self.service = None
        self.user_email = None
        # googleapiclient + httplib2 are NOT thread-safe. Serialize all API calls
        # per client to prevent TLS state corruption (WRONG_VERSION_NUMBER errors)
        # when the main loop, pricing monitor, and follow-up threads run concurrently.
        self._api_lock = threading.RLock()

    def authenticate(self, interactive: bool = False):
        """Authenticate with Gmail API using OAuth 2.0.

        interactive=False (the agent/service path): never opens a browser. If the token is
        missing/expired and cannot be refreshed, raises a clear RuntimeError telling the operator
        to run `python main.py --setup`, instead of blocking forever on a headless server.
        interactive=True (the --setup path): allowed to open the browser consent flow.
        """
        creds = None

        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)

        if not creds or not creds.valid:
            refreshed = False
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info(f"[{self.account_name}] Refreshing expired token...")
                    creds.refresh(Request())
                    refreshed = True
                except Exception as e:
                    logger.error(f"[{self.account_name}] Token refresh failed: {e}")
                    creds = None

            if not refreshed and (not creds or not creds.valid):
                if not interactive:
                    raise RuntimeError(
                        f"[{self.account_name}] Gmail token missing/expired and cannot be refreshed. "
                        f"Run 'python main.py --setup' to (re)authenticate this account before starting the agent."
                    )
                logger.info(f"[{self.account_name}] Starting OAuth flow - browser will open...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, self.scopes
                )
                creds = flow.run_local_server(port=0)

            # Save token for next run
            Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_file, "w") as token:
                token.write(creds.to_json())
            # Best-effort: restrict token file perms (contains a refresh token + client secret).
            try:
                os.chmod(self.token_file, 0o600)
            except Exception:
                pass
            logger.info(f"[{self.account_name}] Token saved to {self.token_file}")

        self.service = build("gmail", "v1", credentials=creds)

        # Get authenticated user email
        profile = self.service.users().getProfile(userId="me").execute()
        self.user_email = profile.get("emailAddress")
        logger.info(f"[{self.account_name}] Authenticated as {self.user_email}")

    def get_unread_emails(self, max_results: int = 20, after_date: str = None) -> list[dict]:
        """Fetch unread emails from inbox.
        after_date: Only fetch emails after this date (format: 'YYYY/MM/DD').
                    Used to skip old emails so the agent only processes today's mail.
        """
        if not self.service:
            raise RuntimeError("Gmail client not authenticated. Call authenticate() first.")

        query = None
        if after_date:
            query = f"after:{after_date}"

        with self._api_lock:
            results = self.service.users().messages().list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                q=query,
                maxResults=max_results,
            ).execute()

            messages = results.get("messages", [])
            emails = []
            for msg_ref in messages:
                email_data = self._get_email_detail(msg_ref["id"])
                if email_data:
                    emails.append(email_data)

        logger.info(f"[{self.account_name}] Found {len(emails)} unread emails")
        return emails

    def get_inbox_emails(self, after_date: str = None, max_results: int = 100,
                         include_read: bool = True) -> list[dict]:
        """Fetch INBOX emails (READ + unread by default) received after `after_date` (YYYY/MM/DD).
        Used for the start-up catch-up sweep: a mail a human already OPENED (marked read) while the
        agent was off would be skipped by the unread-only poll — this picks it up. Dedup is handled
        downstream (process_email skips any gmail_id already in the DB)."""
        if not self.service:
            raise RuntimeError("Gmail client not authenticated. Call authenticate() first.")

        labels = ["INBOX"] if include_read else ["INBOX", "UNREAD"]
        query = f"after:{after_date}" if after_date else None
        with self._api_lock:
            results = self.service.users().messages().list(
                userId="me", labelIds=labels, q=query, maxResults=max_results,
            ).execute()
            messages = results.get("messages", [])
            emails = []
            for msg_ref in messages:
                email_data = self._get_email_detail(msg_ref["id"])
                if email_data:
                    emails.append(email_data)

        logger.info(f"[{self.account_name}] Inbox sweep: {len(emails)} email(s) since {after_date} "
                    f"(include_read={include_read})")
        return emails

    def get_emails_since(self, since_timestamp: str, max_results: int = 50) -> list[dict]:
        """Fetch emails received after a given timestamp. Format: 'YYYY/MM/DD'."""
        query = f"after:{since_timestamp}"
        with self._api_lock:
            results = self.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
            ).execute()

            messages = results.get("messages", [])
            emails = []
            for msg_ref in messages:
                email_data = self._get_email_detail(msg_ref["id"])
                if email_data:
                    emails.append(email_data)

        return emails

    def _get_email_detail(self, message_id: str) -> Optional[dict]:
        """Get full email details including body and attachments info."""
        try:
            with self._api_lock:
                msg = self.service.users().messages().get(
                    userId="me", id=message_id, format="full"
                ).execute()

            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}

            # Extract body
            body_text = ""
            body_html = ""
            attachments = []

            self._extract_parts(msg["payload"], body_text_parts := [], body_html_parts := [], attachments)
            body_text = "\n".join(body_text_parts)
            body_html = "\n".join(body_html_parts)

            return {
                "gmail_id": msg["id"],
                "thread_id": msg.get("threadId"),
                "from_email": headers.get("from", ""),
                "to_email": headers.get("to", ""),
                "cc": headers.get("cc", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "body_text": body_text,
                "body_html": body_html,
                "has_attachments": len(attachments) > 0,
                "attachment_names": [a["filename"] for a in attachments],
                "attachment_refs": attachments,
                "labels": msg.get("labelIds", []),
                "snippet": msg.get("snippet", ""),
            }
        except Exception as e:
            logger.error(f"[{self.account_name}] Error fetching email {message_id}: {e}")
            return None

    def _extract_parts(self, payload: dict, text_parts: list, html_parts: list, attachments: list):
        """Recursively extract text, HTML, and attachment info from email payload."""
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                text_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
        elif mime_type == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                html_parts.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="replace"))
        elif payload.get("filename"):
            attachments.append({
                "filename": payload["filename"],
                "mime_type": mime_type,
                "attachment_id": payload.get("body", {}).get("attachmentId"),
                "size": payload.get("body", {}).get("size", 0),
            })

        # Recurse into parts
        for part in payload.get("parts", []):
            self._extract_parts(part, text_parts, html_parts, attachments)

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download an attachment by its ID."""
        with self._api_lock:
            attachment = self.service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()

        data = attachment.get("data", "")
        return base64.urlsafe_b64decode(data)

    def send_reply(self, thread_id: str, to_email: str, subject: str, body_html: str, in_reply_to: str = None, cc_emails: list[str] = None) -> dict:
        """Send a reply email within a thread. Optional cc_emails list is added as CC."""
        message = MIMEMultipart("alternative")
        message["to"] = to_email
        if cc_emails:
            message["cc"] = ", ".join(cc_emails)
        message["subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to

        html_part = MIMEText(body_html, "html")
        message.attach(html_part)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        with self._api_lock:
            result = self.service.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": thread_id},
            ).execute()

        cc_note = f" (cc: {', '.join(cc_emails)})" if cc_emails else ""
        logger.info(f"[{self.account_name}] Reply sent to {to_email}{cc_note} | Message ID: {result['id']}")
        return result

    def send_new_email(self, to_email: str, subject: str, body_html: str, cc_emails: list[str] = None) -> dict:
        """Send a new email (not a reply). Supports multiple recipients via to_email (comma-separated) and cc_emails list."""
        message = MIMEMultipart("alternative")
        message["to"] = to_email
        if cc_emails:
            message["cc"] = ", ".join(cc_emails)
        message["subject"] = subject

        html_part = MIMEText(body_html, "html")
        message.attach(html_part)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        with self._api_lock:
            result = self.service.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()

        all_recipients = to_email
        if cc_emails:
            all_recipients += ", " + ", ".join(cc_emails)
        logger.info(f"[{self.account_name}] New email sent to {all_recipients} | Message ID: {result['id']}")
        return result

    def get_thread_replies(self, thread_id: str, after_message_id: str = None) -> list[dict]:
        """Get all messages in a thread, optionally only those after a given message ID.
        Used to monitor forwarded threads for pricing replies from Quote Analyst."""
        try:
            with self._api_lock:
                thread = self.service.users().threads().get(
                    userId="me", id=thread_id, format="full"
                ).execute()

            messages = thread.get("messages", [])
            results = []
            found_marker = after_message_id is None  # If no marker, return all

            for msg in messages:
                if not found_marker:
                    if msg["id"] == after_message_id:
                        found_marker = True
                    continue

                # This is a reply after our forwarded message
                headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}

                text_parts = []
                html_parts = []
                attachments = []
                self._extract_parts(msg["payload"], text_parts, html_parts, attachments)

                results.append({
                    "gmail_id": msg["id"],
                    "thread_id": thread_id,
                    "from_email": headers.get("from", ""),
                    "to_email": headers.get("to", ""),
                    "cc": headers.get("cc", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "body_text": "\n".join(text_parts),
                    "body_html": "\n".join(html_parts),
                    "labels": msg.get("labelIds", []),
                })

            return results

        except HttpError as e:
            # 404 = thread not in THIS mailbox (stale cross-mailbox thread_id) — expected, not an
            # error; warn once so the log isn't flooded on every poll cycle.
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                if thread_id not in _WARNED_MISSING_THREADS:
                    _WARNED_MISSING_THREADS.add(thread_id)
                    logger.warning(f"[{self.account_name}] Thread {thread_id} not found in this mailbox "
                                   f"(stale/cross-mailbox thread_id) — skipping; will not re-log.")
                return []
            logger.error(f"[{self.account_name}] Error getting thread {thread_id}: {e}")
            return []
        except Exception as e:
            logger.error(f"[{self.account_name}] Error getting thread {thread_id}: {e}")
            return []

    def mark_as_read(self, message_id: str):
        """Remove UNREAD label from a message."""
        with self._api_lock:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()

    def add_label(self, message_id: str, label_name: str):
        """Add a label to a message (creates label if needed)."""
        label_id = self._get_or_create_label(label_name)
        with self._api_lock:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()

    def _get_or_create_label(self, label_name: str) -> str:
        """Get label ID by name, or create it."""
        with self._api_lock:
            labels = self.service.users().labels().list(userId="me").execute()
            for label in labels.get("labels", []):
                if label["name"] == label_name:
                    return label["id"]

            # Create new label
            new_label = self.service.users().labels().create(
                userId="me",
                body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            ).execute()
        return new_label["id"]
