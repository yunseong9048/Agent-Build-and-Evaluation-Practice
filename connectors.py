"""외부 서비스 커넥터 (Slack / Telegram / Email).

두 곳에서 함께 쓰인다:
  1) 에이전트 도구  — build_messaging_tools() 가 @tool 목록을 만들어 deep agent 에 붙인다.
  2) 공용 게이트웨이 — gateway.py 가 각 Connector 의 send()/수신 메서드를 직접 호출한다.

각 Connector 는 관련 env 가 있을 때만 enabled=True 가 된다. TLS 검사 프록시 환경을 가정해
인증서 검증을 끈다(verify=False / CERT_NONE) — 운영 환경에서는 검증을 켤 것.

필요한 환경변수
  Slack     : SLACK_BOT_TOKEN(쓰기, xoxb-) / SLACK_USER_TOKEN(읽기, xoxp-)
              (게이트웨이 수신은 SLACK_APP_TOKEN(xapp-, Socket Mode) 추가 필요)
  Telegram  : TELEGRAM_BOT_TOKEN, (선택) TELEGRAM_CHAT_ID
  Email(발신): SMTP_HOST, (선택)SMTP_PORT=587, SMTP_USER, SMTP_PASSWORD, (선택)EMAIL_FROM
  Email(수신): IMAP_HOST, (선택)IMAP_PORT=993, IMAP_USER, IMAP_PASSWORD
              (IMAP_USER/PASSWORD 미설정 시 SMTP_USER/PASSWORD 재사용)
"""

import os
import ssl

from curl_cffi import requests as _curl_requests
from langchain_core.tools import tool

# TLS 검사 프록시 환경에서도 동작하도록 인증서 검증을 끈 SSL 컨텍스트(Slack/Email 용).
_insecure_ssl = ssl.create_default_context()
_insecure_ssl.check_hostname = False
_insecure_ssl.verify_mode = ssl.CERT_NONE


# Telegram HTTP:
# 사내망 DPI 가 api.telegram.org 로 가는 '파이썬 TLS' 핸드셰이크를 지문 기반으로
# 간헐적으로 리셋한다([Errno 54] Connection reset). httpx(http2 포함)/urllib 모두 영향받고,
# curl 만 통과한다. 그래서 브라우저 TLS 를 위장하는 curl_cffi(libcurl)로 요청한다.
def _tg_get(url, params=None, timeout=30):
    return _curl_requests.get(
        url, params=params, timeout=timeout, impersonate="chrome", verify=False
    )


def _tg_post(url, json=None, timeout=30):
    return _curl_requests.post(
        url, json=json, timeout=timeout, impersonate="chrome", verify=False
    )


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------
def _decode_mime(value: str | None) -> str:
    """MIME 인코딩된 이메일 헤더를 사람이 읽는 문자열로 디코드."""
    if not value:
        return ""
    from email.header import decode_header

    out = ""
    for text, enc in decode_header(value):
        out += (
            text.decode(enc or "utf-8", errors="replace")
            if isinstance(text, bytes)
            else text
        )
    return out


def _email_plain_body(msg) -> str:
    """이메일 메시지에서 text/plain 본문을 추출."""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        return ""
    payload = msg.get_payload(decode=True)
    return (
        payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if payload
        else ""
    )


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
class SlackConnector:
    """Slack 송수신. 쓰기는 봇 토큰, 읽기는 사용자 토큰을 우선 사용한다.

    수신(게이트웨이)은 Socket Mode 라서 SLACK_APP_TOKEN(xapp-)이 별도로 필요하다.
    """

    def __init__(self) -> None:
        self.bot_token = os.getenv("SLACK_BOT_TOKEN")
        self.user_token = os.getenv("SLACK_USER_TOKEN")
        self.app_token = os.getenv("SLACK_APP_TOKEN")  # Socket Mode(xapp-)
        self._write = None
        self._read = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token or self.user_token)

    def verify(self) -> tuple[bool, str]:
        """봇 토큰으로 auth.test 를 호출해 실제 연결을 확인한다."""
        try:
            write, _ = self._clients()
            r = write.auth_test()
            return True, f"{r['team']}/{r['user']}"
        except Exception as e:
            return False, str(e)

    def _clients(self):
        if self._write is None:
            from slack_sdk import WebClient

            self._write = WebClient(
                token=self.bot_token or self.user_token, ssl=_insecure_ssl
            )
            self._read = WebClient(
                token=self.user_token or self.bot_token, ssl=_insecure_ssl
            )
        return self._write, self._read

    def send(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        from slack_sdk.errors import SlackApiError

        write, _ = self._clients()
        try:
            resp = write.chat_postMessage(
                channel=channel, text=text, thread_ts=thread_ts
            )
            return f"Slack 전송 완료 (channel={resp['channel']}, ts={resp['ts']})"
        except SlackApiError as e:
            return f"Slack 전송 실패: {e.response['error']}"

    def read(self, channel: str, limit: int = 20) -> str:
        from slack_sdk.errors import SlackApiError

        _, read = self._clients()
        try:
            resp = read.conversations_history(channel=channel, limit=limit)
            msgs = resp.get("messages", [])
            if not msgs:
                return "메시지가 없습니다."
            return "\n".join(
                f"[{m.get('user', '?')}] {m.get('text', '')}" for m in reversed(msgs)
            )
        except SlackApiError as e:
            return f"Slack 읽기 실패: {e.response['error']}"

    def tools(self) -> list:
        if not self.enabled:
            return []
        try:
            import slack_sdk  # noqa: F401
        except ImportError:
            print("[connector] slack-sdk 미설치 — Slack 건너뜀")
            return []

        @tool(parse_docstring=True)
        def slack_send_message(channel: str, text: str) -> str:
            """Slack 채널 또는 사용자에게 메시지를 보낸다.

            Args:
                channel: 채널 ID(C…)나 이름(#general), 또는 사용자 ID(U…).
                text: 보낼 메시지 텍스트.

            Returns:
                전송 결과 요약.
            """
            return self.send(channel, text)

        @tool(parse_docstring=True)
        def slack_read_channel(channel: str, limit: int = 20) -> str:
            """Slack 채널의 최근 메시지를 읽는다.

            Args:
                channel: 채널 ID(C…). conversations_history 는 채널 ID 를 요구한다.
                limit: 가져올 최근 메시지 수(기본 20).

            Returns:
                오래된→최신 순으로 정렬한 메시지 목록.
            """
            return self.read(channel, limit)

        print("[connector] Slack 활성화")
        return [slack_send_message, slack_read_channel]


# ---------------------------------------------------------------------------
# Telegram (Bot API, HTTP 직접 호출)
# ---------------------------------------------------------------------------
class TelegramConnector:
    """Telegram 봇 송수신. 수신은 getUpdates 롱폴링을 사용한다."""

    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.default_chat = os.getenv("TELEGRAM_CHAT_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def _api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def verify(self) -> tuple[bool, str]:
        """getMe 로 봇 토큰 유효성과 연결을 확인한다."""
        try:
            r = _tg_get(f"{self._api}/getMe", timeout=15)
            data = r.json()
            if data.get("ok"):
                return True, f"@{data['result'].get('username')}"
            return False, f"getMe 실패: {data.get('description')}"
        except Exception as e:
            return False, str(e)

    def send(self, text: str, chat_id: str = "") -> str:
        target = chat_id or self.default_chat
        if not target:
            return "chat_id 가 없습니다. 인자로 주거나 TELEGRAM_CHAT_ID 를 설정하세요."
        try:
            r = _tg_post(
                f"{self._api}/sendMessage", json={"chat_id": target, "text": text}
            )
            r.raise_for_status()
            return "Telegram 전송 완료"
        except Exception as e:
            return f"Telegram 전송 실패: {e}"

    def post_message(self, text: str, chat_id: str) -> int | None:
        """메시지를 보내고 message_id 를 돌려준다(스트리밍 편집의 시작점).

        일반 send() 와 달리 이후 editMessageText 로 갱신할 수 있도록 id 를 노출한다.
        실패하면 None.
        """
        try:
            r = _tg_post(
                f"{self._api}/sendMessage", json={"chat_id": chat_id, "text": text}
            )
            r.raise_for_status()
            return r.json()["result"]["message_id"]
        except Exception as e:
            print(f"[telegram] sendMessage 실패: {e}")
            return None

    def edit_message(self, chat_id: str, message_id: int, text: str) -> str:
        """editMessageText 로 기존 메시지를 갱신한다(스트리밍용).

        반환:
          - "ok"    : 갱신 성공(또는 '내용 동일'이라 갱신 불필요 — 성공으로 간주)
          - "retry" : 레이트리밋(429)·일시 오류 — 다음 틱에 다시 시도하면 됨
          - "fail"  : 편집 불가(예: 메시지가 너무 오래됨) — 이후는 새 메시지로 보낼 것
        """
        try:
            r = _tg_post(
                f"{self._api}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text},
            )
            if r.status_code == 200:
                return "ok"
            desc = (r.json().get("description") or "").lower()
            # 직전과 동일 텍스트면 텔레그램이 거부한다 → 성공과 동일하게 취급.
            if "not modified" in desc:
                return "ok"
            if r.status_code == 429:  # Too Many Requests
                return "retry"
            print(f"[telegram] editMessageText 실패({desc}) — 이후 새 메시지로 전환")
            return "fail"
        except Exception as e:
            print(f"[telegram] editMessageText 예외: {e}")
            return "retry"

    def get_updates(self, offset: int | None = None, timeout: int = 25, limit: int = 100) -> list:
        """롱폴링으로 새 업데이트를 가져온다(게이트웨이 수신용).

        서버가 timeout 초 동안 대기하므로 httpx timeout 을 그보다 넉넉히 준다.
        """
        params: dict = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        r = _tg_get(
            f"{self._api}/getUpdates", params=params, timeout=timeout + 15
        )
        r.raise_for_status()
        return r.json().get("result", [])

    def tools(self) -> list:
        if not self.enabled:
            return []

        # 주의: 수신은 게이트웨이(gateway.py)가 getUpdates 롱폴링으로 처리한다.
        # 도구에서도 getUpdates 를 쓰면 게이트웨이와 업데이트를 서로 뺏어 충돌하므로,
        # 도구는 '보내기'만 제공한다.
        @tool(parse_docstring=True)
        def telegram_send_message(text: str, chat_id: str = "") -> str:
            """Telegram 봇으로 메시지를 보낸다.

            Args:
                text: 보낼 메시지.
                chat_id: 대상 chat_id. 비우면 TELEGRAM_CHAT_ID 기본값을 사용한다.

            Returns:
                전송 결과.
            """
            return self.send(text, chat_id)

        print("[connector] Telegram 활성화")
        return [telegram_send_message]


# ---------------------------------------------------------------------------
# Email (SMTP 발신 / IMAP 수신, 표준 라이브러리)
# ---------------------------------------------------------------------------
class EmailConnector:
    """이메일 도구. 읽기는 IMAP(읽음 처리 안 함), 발신은 SMTP.

    게이트웨이 자동 응답은 하지 않는다 — 에이전트가 요청받을 때만 읽고/보낸다.
    """

    def __init__(self) -> None:
        self.smtp_host = os.getenv("SMTP_HOST")
        self.imap_host = os.getenv("IMAP_HOST")

    @property
    def enabled(self) -> bool:
        return bool(self.smtp_host or self.imap_host)

    def verify(self, need_imap: bool = True, need_smtp: bool = True) -> tuple[bool, str]:
        """IMAP/SMTP 에 실제 로그인해 연결을 확인한다."""
        if need_imap and self.imap_host:
            m = None
            try:
                import imaplib

                port = int(os.getenv("IMAP_PORT", "993"))
                user, password = self._imap_login()
                m = imaplib.IMAP4_SSL(self.imap_host, port, ssl_context=_insecure_ssl)
                m.login(user, password)
            except Exception as e:
                return False, f"IMAP 실패: {e}"
            finally:
                # 연결 누수 방지(Gmail 동시 연결 한도 대비): 실패해도 반드시 닫는다.
                if m is not None:
                    try:
                        m.logout()
                    except Exception:
                        pass
        if need_smtp and self.smtp_host:
            try:
                import smtplib

                port = int(os.getenv("SMTP_PORT", "587"))
                user, password = self._smtp_login()
                with smtplib.SMTP(self.smtp_host, port, timeout=15) as s:
                    s.starttls(context=_insecure_ssl)
                    if user and password:
                        s.login(user, password)
            except Exception as e:
                return False, f"SMTP 실패: {e}"
        return True, (self.imap_host or self.smtp_host or "")

    def _smtp_login(self) -> tuple[str | None, str | None]:
        return os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")

    def _imap_login(self) -> tuple[str | None, str | None]:
        return (
            os.getenv("IMAP_USER") or os.getenv("SMTP_USER"),
            os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD"),
        )

    def send(self, to: str, subject: str, body: str) -> str:
        import smtplib
        from email.message import EmailMessage

        host = self.smtp_host
        port = int(os.getenv("SMTP_PORT", "587"))
        user, password = self._smtp_login()
        sender = os.getenv("EMAIL_FROM") or user
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=_insecure_ssl)
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
            return f"메일 전송 완료 → {to}"
        except Exception as e:
            return f"메일 전송 실패: {e}"

    def read_recent(self, limit: int = 10, unread_only: bool = False) -> str:
        """받은 편지함(INBOX)을 읽기 전용으로 조회한다(읽음 처리하지 않음).

        각 메일을 [날짜] 발신자 — 제목 + 본문 미리보기로 최신순 요약한다.
        unread_only=True 면 안 읽은 메일만 대상으로 한다.
        """
        import email
        import imaplib
        from email.utils import parseaddr

        if not self.imap_host:
            return "IMAP 설정이 없습니다."
        port = int(os.getenv("IMAP_PORT", "993"))
        user, password = self._imap_login()
        m = None
        try:
            m = imaplib.IMAP4_SSL(self.imap_host, port, ssl_context=_insecure_ssl)
            m.login(user, password)
            m.select("INBOX", readonly=True)  # readonly → 읽음 처리 안 됨
            _, data = m.search(None, "UNSEEN" if unread_only else "ALL")
            ids = data[0].split()
            if not ids:
                return "안 읽은 메일이 없습니다." if unread_only else "메일이 없습니다."
            _, unseen_data = m.search(None, "UNSEEN")  # 안읽음 표시용
            unseen = set(unseen_data[0].split())
            out = []
            for i in reversed(ids[-limit:]):
                _, md = m.fetch(i, "(BODY.PEEK[])")  # 헤더+본문, 읽음 처리 없이
                msg = email.message_from_bytes(md[0][1])
                frm = parseaddr(msg.get("From"))[1] or _decode_mime(msg.get("From"))
                subj = _decode_mime(msg.get("Subject")) or "(제목 없음)"
                date = msg.get("Date", "")
                mark = "🆕 " if i in unseen else ""
                body = " ".join(_email_plain_body(msg).split())
                preview = (body[:500] + "…") if len(body) > 500 else body
                out.append(f"{mark}[{date}] {frm} — {subj}\n{preview}")
            return "\n\n".join(out)
        except Exception as e:
            return f"메일 읽기 실패: {e}"
        finally:
            # 연결 누수 방지(Gmail 동시 연결 한도 대비): 성공/실패 무관하게 닫는다.
            if m is not None:
                try:
                    m.logout()
                except Exception:
                    pass

    def poll_new(self, since_uid: int | None, limit: int = 20) -> tuple[int, list[dict]]:
        """since_uid 보다 큰 UID 의 새 메일을 읽기 전용으로 가져온다(읽음 처리 안 함).

        since_uid=None 이면 '기준점 설정' 모드 — 현재 최대 UID 만 돌려주고 메일은
        가져오지 않는다(감시 시작 이전의 과거 메일은 트리거하지 않기 위함).

        반환: (새 최대 UID, [ {uid, from_addr, from_raw, subject, body, date}, ... ])
        """
        import email
        import imaplib
        from email.utils import parseaddr

        if not self.imap_host:
            return since_uid or 0, []
        port = int(os.getenv("IMAP_PORT", "993"))
        user, password = self._imap_login()
        m = imaplib.IMAP4_SSL(self.imap_host, port, ssl_context=_insecure_ssl)
        mails: list[dict] = []
        try:
            m.login(user, password)
            m.select("INBOX", readonly=True)  # readonly → 안읽음 유지
            if since_uid is None:
                _, data = m.uid("search", None, "ALL")
                uids = data[0].split()
                return (int(uids[-1]) if uids else 0), []
            _, data = m.uid("search", None, f"UID {since_uid + 1}:*")
            # IMAP 의 'N:*' 는 최고 UID 를 항상 포함하는 특성이 있어 명시적으로 걸러낸다.
            uids = sorted(int(x) for x in data[0].split() if int(x) > since_uid)
            max_uid = since_uid
            for uid in uids[:limit]:
                _, md = m.uid("fetch", str(uid), "(BODY.PEEK[])")
                if not md or md[0] is None:
                    continue
                msg = email.message_from_bytes(md[0][1])
                mails.append(
                    {
                        "uid": uid,
                        "from_addr": parseaddr(msg.get("From"))[1],
                        "from_raw": _decode_mime(msg.get("From")),
                        "subject": _decode_mime(msg.get("Subject")),
                        "body": _email_plain_body(msg),
                        "date": msg.get("Date", ""),
                    }
                )
                max_uid = max(max_uid, uid)
            return max_uid, mails
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def tools(self) -> list:
        if not self.enabled:
            return []
        tools: list = []
        if self.smtp_host:

            @tool(parse_docstring=True)
            def send_email(to: str, subject: str, body: str) -> str:
                """이메일을 보낸다 (SMTP).

                Args:
                    to: 받는 사람 이메일 주소.
                    subject: 제목.
                    body: 본문(평문).

                Returns:
                    전송 결과.
                """
                return self.send(to, subject, body)

            tools.append(send_email)

        if self.imap_host:

            @tool(parse_docstring=True)
            def read_recent_emails(limit: int = 10, unread_only: bool = False) -> str:
                """받은 편지함(INBOX)의 메일을 읽는다 (IMAP, 읽음 처리 안 함).

                최신순으로 [날짜] 발신자 — 제목 + 본문 미리보기를 돌려준다.
                "새 메일 있어?" 같은 요청은 unread_only=True 로, 과거 메일을 훑을 땐
                limit 을 늘려 호출한다.

                Args:
                    limit: 가져올 최근 메일 수(기본 10).
                    unread_only: True 면 안 읽은 메일만 대상으로 한다.

                Returns:
                    최신순 메일 요약(발신자/제목/미리보기) 목록.
                """
                return self.read_recent(limit, unread_only)

            tools.append(read_recent_emails)

        print("[connector] Email 활성화")
        return tools


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------
# 게이트웨이(gateway.py)도 같은 클래스를 재사용한다.
ALL_CONNECTORS = (SlackConnector, TelegramConnector, EmailConnector)


def build_messaging_tools() -> list:
    """Slack / Telegram / Email 커넥터의 에이전트 도구를 모두 조립한다.

    설정(env)이 없는 커넥터는 자동으로 빠진다.
    """
    tools: list = []
    for connector_cls in ALL_CONNECTORS:
        if (
            connector_cls is EmailConnector
            and os.getenv("EMAIL_CONNECTOR_ENABLED", "1").lower()
            in {"0", "false", "no", "off"}
        ):
            continue
        tools += connector_cls().tools()
    return tools
