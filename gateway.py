"""공용 게이트웨이 폴링봇.

Telegram / Slack 어느 채널에서 메시지가 오든 동일한 deep agent 로 처리하고,
받은 채널로 그대로 답장한다. 채널마다 ChannelAdapter 를 두고, 설정(env)이 된 어댑터만
각각의 스레드로 돌린다.

  - Telegram : getUpdates 롱폴링
  - Slack    : Socket Mode 수신(app_mention / DM) → 스레드 답장 (SLACK_APP_TOKEN 필요)

이메일은 개인 메일함에 자동 답장하는 위험을 피하려고 게이트웨이 채널에서 제외했다.
대신 에이전트 '도구'(read_recent_emails / send_email, connectors.py)로만 노출한다.

실행: uv run python gateway.py   (langgraph dev 와는 별개의 상시 프로세스)
"""

import importlib.util
import json
import os
import threading
import time
import traceback
from pathlib import Path

import dotenv

import connectors

dotenv.load_dotenv()

# 에이전트 작업 공간(파일/규칙 파일 위치). langchain-deepagents.py 와 동일 규칙으로 계산.
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "workspace")).expanduser().resolve()


# ---------------------------------------------------------------------------
# deep agent 로드 (파일명이 하이픈이라 일반 import 불가 → importlib 로 로드)
# ---------------------------------------------------------------------------
# 에이전트는 지연 로드한다(임베드 시 langchain-deepagents.py 가 만든 그래프를 재사용).
agent = None

# 에이전트 호출을 직렬화한다(개인용 저부하 가정). 채널이 동시에 들어와도
# 모델/백엔드 상태 충돌 없이 안전하게 처리된다.
_agent_lock = threading.Lock()


def _load_agent():
    """standalone 실행용: langchain-deepagents.py 를 로드해 그래프를 만든다.

    파일명이 하이픈이라 일반 import 가 안 되므로 importlib 로 로드한다.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    spec = importlib.util.spec_from_file_location("agent_app", "langchain-deepagents.py")
    agent_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_app)
    return agent_app.build_agent(checkpointer=InMemorySaver())


def set_agent(agent_instance) -> None:
    """외부(메인 파일)에서 만든 그래프를 게이트웨이에 주입한다."""
    global agent
    agent = agent_instance


def _ensure_agent():
    global agent
    if agent is None:
        agent = _load_agent()
    return agent


def _text_of(msg) -> str:
    """메시지/청크의 content 에서 사람이 읽을 텍스트만 뽑아낸다.

    Anthropic 응답은 content 가 문자열이거나 [{"type":"text","text":...}, ...]
    형태의 블록 리스트일 수 있다(tool_use 블록 등은 걸러낸다).
    """
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type", "text") == "text":
                    out.append(p.get("text", ""))
            else:
                out.append(str(p))
        return "".join(out)
    return str(content)


# 대화 초기화 명령. 채널과 무관하게 answer()/answer_stream() 앞단에서 가로챈다.
# 일반 대화(예: "new feature 얘기하자")를 삼키지 않도록 명시적으로 '/' 를 붙인 경우만 인정한다.
RESET_COMMANDS = {"/new"}
RESET_REPLY = "🆕 대화를 초기화했습니다. 새로 시작할게요."


def _maybe_reset(text: str, conversation_id: str) -> str | None:
    """대화 초기화 명령이면 해당 thread 만 비우고 안내 문구를 돌려준다.

    conversation_id(=chat/스레드) 단위로만 체크포인트를 삭제하므로 다른 대화엔
    영향이 없다. 초기화 명령이 아니면 None 을 반환해 정상 처리로 넘긴다.
    """
    if text.strip().lower() not in RESET_COMMANDS:
        return None
    a = _ensure_agent()
    cp = getattr(a, "checkpointer", None)
    if cp is not None and hasattr(cp, "delete_thread"):
        try:
            cp.delete_thread(conversation_id)
        except Exception:  # 삭제 실패해도 새 대화는 이어갈 수 있으므로 로그만 남긴다
            traceback.print_exc()
    print(f"[gateway] 대화 초기화: {conversation_id}")
    return RESET_REPLY


def answer(text: str, conversation_id: str) -> str:
    """공용 코어: 메시지를 deep agent 로 처리하고 답변 텍스트를 돌려준다.

    conversation_id 별 thread 로 대화 문맥이 이어진다(파일/메모리는 그와 별개로 지속).
    '/new' 는 해당 대화만 초기화한다.
    """
    reset = _maybe_reset(text, conversation_id)
    if reset is not None:
        return reset
    a = _ensure_agent()
    with _agent_lock:
        result = a.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": conversation_id}},
        )
    return _text_of(result["messages"][-1]).strip()


def answer_stream(text: str, conversation_id: str):
    """스트리밍 코어: (세그먼트번호, 누적텍스트) 를 순차적으로 yield 한다.

    LangGraph 의 messages 스트림에서 AI 메시지 토큰만 골라 누적한다. 텍스트가 있는
    AI 메시지(=사설 또는 최종 답변)마다 세그먼트 번호가 1씩 증가한다. 도구 호출만 하는
    턴은 텍스트가 없어 아무것도 yield 하지 않으므로, 보통은 최종 답변이 첫 세그먼트다.
    소비자는 세그먼트가 바뀌면 새 메시지로, 같으면 갱신으로 처리하면 된다.
    '/new' 는 해당 대화만 초기화하고 안내 문구 한 세그먼트만 낸다.
    """
    reset = _maybe_reset(text, conversation_id)
    if reset is not None:
        yield 1, reset
        return
    a = _ensure_agent()
    cur_id = object()  # 첫 청크와 반드시 달라지도록 sentinel 로 시작
    seg = 0
    acc = ""
    with _agent_lock:
        for chunk, _meta in a.stream(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": conversation_id}},
            stream_mode="messages",
        ):
            # AI 메시지 토큰만(도구 결과·사용자 메시지 등 제외).
            if "ai" not in str(getattr(chunk, "type", "")).lower():
                continue
            cid = getattr(chunk, "id", None)
            if cid != cur_id:  # 새 메시지 시작 → 새 세그먼트
                cur_id, acc, seg = cid, "", seg + 1
            piece = _text_of(chunk)
            if piece:
                acc += piece
                yield seg, acc


# ---------------------------------------------------------------------------
# 채널 어댑터
# ---------------------------------------------------------------------------
class ChannelAdapter:
    name = "base"

    def check(self) -> tuple[str, str]:
        """이 채널이 '달려있는지' 판정한다.

        반환 (state, detail):
          - ("ok", 상세)   : env 채워짐 + 실제 연결 확인됨 → 실행 대상
          - ("fail", 사유) : 연결하려 했으나 실패(설정 일부만 있거나 인증 오류 등)
          - ("unset", "")  : 이 채널을 쓸 의도가 없음(관련 env 없음) → 조용히 무시
        """
        raise NotImplementedError

    def run(self) -> None:
        """블로킹 루프. 별도 스레드에서 실행된다."""
        raise NotImplementedError


TG_MSG_LIMIT = 4000  # 텔레그램 메시지 4096자 제한 대비 여유값


class TelegramAdapter(ChannelAdapter):
    name = "telegram"

    def __init__(self) -> None:
        self.c = connectors.TelegramConnector()
        _os = __import__("os")
        # 스트리밍 on/off 및 편집 주기.
        # 텔레그램은 '채팅당 대략 1건/초'가 지속 한도이고 편집(editMessageText)도 여기
        # 포함된다. 그래서 Slack 과 같은 1.0s 로 두되, 429 가 오면 그 틱을 건너뛰고(=retry)
        # 다음에 다시 시도한다. 429 가 잦으면 이 값을 1.5~2.0 으로 올리면 된다.
        self.stream = _os.getenv("TELEGRAM_STREAM", "1") not in ("0", "false", "False")
        self.interval = float(_os.getenv("TELEGRAM_STREAM_INTERVAL", "1.0"))

    def check(self) -> tuple[str, str]:
        if not self.c.enabled:  # TELEGRAM_BOT_TOKEN 없음 → 의도 없음
            return "unset", ""
        ok, detail = self.c.verify()
        return ("ok" if ok else "fail"), detail

    def run(self) -> None:
        offset = None
        while True:
            try:
                for u in self.c.get_updates(offset=offset, timeout=25):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or u.get("channel_post")
                    if not msg:
                        continue
                    text = msg.get("text")
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if not text or not chat_id:
                        continue
                    print(f"[telegram] 수신({chat_id}): {text[:60]}")
                    started = time.monotonic()
                    conv_id = f"telegram:{chat_id}"
                    if self.stream:
                        self._respond_stream(text, conv_id, chat_id)
                    else:
                        try:
                            reply = answer(text, conv_id)
                        except Exception as e:
                            traceback.print_exc()
                            reply = f"에이전트 처리 중 오류: {e}"
                        self.c.send(reply, chat_id=chat_id)
                    print(
                        f"[telegram] 답장 전송 → {chat_id} "
                        f"({time.monotonic() - started:.1f}s)"
                    )
            except Exception as e:
                # 간헐적 네트워크 리셋 등은 전체 트레이스백 대신 한 줄로 로깅 후 재시도.
                print(f"[telegram] 폴링 오류(재시도): {type(e).__name__}: {str(e)[:120]}")
                time.sleep(5)

    def _respond_stream(self, text: str, conv_id: str, chat_id: str) -> None:
        """answer_stream 을 소비하며 editMessageText 로 실시간 갱신한다(Slack 대칭).

        첫 텍스트가 나오는 순간 메시지를 새로 보내고(그때 알림이 울린다), 이후 interval
        마다 같은 메시지를 편집해 채워 넣는다. 세그먼트(AI 메시지)가 바뀌면 새 메시지로
        시작한다. 편집이 막히면(fail) 이후는 새 메시지로 확정한다.
        """
        cur_seg = None
        state = None  # {"mid", "edit_ok", "last", "final", "tried"}
        posted = False
        try:
            for seg, acc in answer_stream(text, conv_id):
                if seg != cur_seg:  # 새 세그먼트 → 이전 것 확정하고 새로 시작
                    posted = self._close_segment(chat_id, state) or posted
                    cur_seg = seg
                    state = {"mid": None, "edit_ok": True, "last": 0.0, "final": "", "tried": False}
                state["final"] = acc
                if not acc.strip():
                    continue
                now = time.monotonic()
                if state["mid"] is None:
                    # 이 세그먼트의 첫 텍스트 → 여기서 메시지 생성(알림 발생 지점).
                    # 전송이 실패하면 tried 로 재전송 폭주를 막고, 내용은 close 에서 확정.
                    if not state["tried"]:
                        state["tried"] = True
                        mid = self.c.post_message(acc[:TG_MSG_LIMIT], chat_id)
                        if mid is not None:
                            state["mid"], state["last"], posted = mid, now, True
                elif state["edit_ok"] and now - state["last"] >= self.interval:
                    status = self.c.edit_message(chat_id, state["mid"], acc[:TG_MSG_LIMIT])
                    if status == "fail":
                        state["edit_ok"] = False
                    else:  # "ok" 또는 "retry" 모두 다음 틱까지 한 박자 쉰다
                        state["last"] = now
        except Exception as e:
            traceback.print_exc()
            self.c.send(f"에이전트 처리 중 오류: {e}", chat_id=chat_id)
            return
        posted = self._close_segment(chat_id, state) or posted
        if not posted:  # 텍스트가 한 번도 안 나온 경우
            self.c.send("(빈 응답)", chat_id=chat_id)

    def _close_segment(self, chat_id: str, state) -> bool:
        """세그먼트 하나를 최종본으로 확정한다. 무언가 내보냈으면 True.

        4096자 초과분은 뒤이어 새 메시지로 이어붙인다.
        """
        if not state:
            return False
        full = state["final"]
        if not full.strip():
            return state["mid"] is not None  # 이미 보낸 게 있으면 True
        head, rest = full[:TG_MSG_LIMIT], full[TG_MSG_LIMIT:]
        if state["mid"] is None:
            self.c.send(head, chat_id=chat_id)  # 처음 전송이 실패했었음 → 새 메시지로
        elif state["edit_ok"]:
            self.c.edit_message(chat_id, state["mid"], head)  # 최종본으로 확정
        else:
            self.c.send(head, chat_id=chat_id)  # 편집 불가 → 전체를 새 메시지로
        while rest:
            self.c.send(rest[:TG_MSG_LIMIT], chat_id=chat_id)
            rest = rest[TG_MSG_LIMIT:]
        return True


# 이메일은 '모든 메일 자동 답장'은 하지 않는다(개인 메일함 위험). 대신 아래 EmailTriggerAdapter
# 가 '규칙에 맞는' 새 메일에만 규칙의 작업을 실행한다(옵트인). 규칙 CRUD 는 스킬로 한다.
def _match_rule(match: dict, mail: dict) -> bool:
    """규칙의 match 조건을 모두 만족하면 True. 빈 조건({})은 모든 메일에 매칭된다."""

    def _contains(hay: str, needles) -> bool:
        if isinstance(needles, str):
            needles = [needles]
        hay = (hay or "").lower()
        return any(str(n).lower() in hay for n in needles)

    if "from" in match and not _contains(
        f"{mail.get('from_raw', '')} {mail.get('from_addr', '')}", match["from"]
    ):
        return False
    if "subject_contains" in match and not _contains(
        mail.get("subject", ""), match["subject_contains"]
    ):
        return False
    if "body_contains" in match and not _contains(
        mail.get("body", ""), match["body_contains"]
    ):
        return False
    return True


class EmailTriggerAdapter(ChannelAdapter):
    """조건에 맞는 새 메일이 오면 규칙의 작업을 에이전트로 실행한다(자동 답장 아님).

    규칙 파일: WORKSPACE/email_triggers.json (스킬 set-email-triggers 로 CRUD).
    매 폴링마다 규칙을 재로드하므로 편집이 재시작 없이 즉시 반영된다.
    IMAP 은 읽기 전용으로 조회하므로 메일함의 안읽음 상태는 유지된다.
    """

    name = "email-trigger"

    def __init__(self) -> None:
        self.c = connectors.EmailConnector()
        self.rules_path = WORKSPACE / "email_triggers.json"
        self.state_path = WORKSPACE / ".email_trigger_state.json"
        self.poll_seconds = int(os.getenv("EMAIL_POLL_SECONDS", "30"))

    def check(self) -> tuple[str, str]:
        # IMAP 이 설정돼 있어야 감시가 가능하다(규칙은 실행 중 언제든 추가/삭제 가능).
        if not self.c.imap_host:
            return "unset", ""
        ok, detail = self.c.verify(need_imap=True, need_smtp=False)
        if ok:
            return "ok", detail
        # Gmail '동시 연결 한도(too many connections)'는 일시적 상태다. 이걸로 세션 내내
        # 제외해버리면 안 되므로, 일단 시작하고 run() 이 폴링마다 재시도하게 둔다.
        if "too many" in detail.lower():
            return "ok", "(일시적 연결 한도 — 감시 시작 후 자동 재시도)"
        return "fail", detail

    def _load_rules(self) -> list:
        if not self.rules_path.exists():
            return []
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[email-trigger] 규칙 파싱 실패(무시): {e}")
            return []

    def _load_last_uid(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))["last_uid"]
        except Exception:
            return None

    def _save_last_uid(self, uid: int) -> None:
        try:
            self.state_path.write_text(json.dumps({"last_uid": uid}), encoding="utf-8")
        except Exception:
            traceback.print_exc()

    def run(self) -> None:
        last_uid = self._load_last_uid()
        if last_uid is None:
            # 첫 실행: 과거 메일은 트리거하지 않도록 현재 최대 UID 로 기준점만 잡는다.
            try:
                last_uid, _ = self.c.poll_new(None)
                self._save_last_uid(last_uid)
                print(f"[email-trigger] 감시 시작 — 기준 UID={last_uid} (이후 새 메일부터)")
            except Exception as e:
                print(f"[email-trigger] 초기화 실패(재시도): {e}")
                time.sleep(self.poll_seconds)
        while True:
            try:
                new_max, mails = self.c.poll_new(last_uid, limit=20)
                rules = self._load_rules()  # 매 폴링마다 재로드 → 스킬 CRUD 즉시 반영
                for mail in mails:
                    for rule in rules:
                        if _match_rule(rule.get("match", {}), mail):
                            self._fire(rule, mail)
                            break  # 메일당 첫 매칭 규칙만 실행
                if new_max and new_max != last_uid:
                    last_uid = new_max
                    self._save_last_uid(last_uid)
            except Exception as e:
                print(f"[email-trigger] 폴링 오류(재시도): {type(e).__name__}: {str(e)[:120]}")
            time.sleep(self.poll_seconds)

    def _fire(self, rule: dict, mail: dict) -> None:
        name = rule.get("name", "(이름없음)")
        print(f"[email-trigger] 매칭 '{name}' ← {mail['from_addr']} / {mail['subject'][:50]}")
        prompt = (
            f"[이메일 트리거 규칙: {name}]\n"
            f"아래 작업을 수행해라:\n{str(rule.get('action', '')).strip()}\n\n"
            f"--- 수신 메일 ---\n"
            f"From: {mail['from_raw']}\nDate: {mail['date']}\nSubject: {mail['subject']}\n\n"
            f"{mail['body']}"
        )
        started = time.monotonic()
        try:
            # 메일마다 새 thread → 트리거 간 문맥이 섞이지 않는다.
            answer(prompt, f"email-trigger:{name}:{mail['uid']}")
        except Exception:
            traceback.print_exc()
        print(f"[email-trigger] '{name}' 처리 완료 ({time.monotonic() - started:.1f}s)")


SLACK_MSG_LIMIT = 3900  # Slack 메시지 4000자 제한 대비 여유값


def _log_recv(event) -> float:
    """이벤트 수신 시각(monotonic)을 반환하고, 전송→수신 지연을 로그로 남긴다.

    event["ts"] 는 사용자가 메시지를 보낸 epoch 초. 실제 시각과의 차이가 크면
    Socket Mode 전달/큐잉 지연(우리 코드 밖)이 병목이라는 뜻이다.
    """
    now_wall, now_mono = time.time(), time.monotonic()
    try:
        delay = now_wall - float(event.get("ts", now_wall))
        print(f"[gateway] slack 이벤트 수신: 전송→수신 {delay:.1f}s")
    except (TypeError, ValueError):
        pass
    return now_mono


class SlackAdapter(ChannelAdapter):
    name = "slack"

    def __init__(self) -> None:
        self.c = connectors.SlackConnector()
        _os = __import__("os")
        # 스트리밍 on/off 및 갱신 주기(chat.update 레이트리밋 대비 최소 간격).
        self.stream = _os.getenv("SLACK_STREAM", "1") not in ("0", "false", "False")
        self.interval = float(_os.getenv("SLACK_STREAM_INTERVAL", "1.0"))

    def _respond(self, text, conv_id, channel, say, thread_ts=None, t_recv=None) -> None:
        """스트림을 받아 처리한다.

        placeholder 를 미리 올리지 않는다. 실제 답변 텍스트가 처음 나오는 순간에
        비로소 댓글을 새로 단다(→ 그 시점에 알림이 울린다). 이후 토큰은 chat.update 로
        같은 댓글에 이어 채운다. 세그먼트(AI 메시지)가 바뀌면 새 댓글로 시작한다.
        """
        write, _ = self.c._clients()
        cur_seg = None
        seg_state = None  # {"ts", "update_ok", "last", "final"}
        posted = False
        logged_first = False
        t0 = time.monotonic()  # answer_stream 시작 직전(진단용)
        try:
            for seg, acc in answer_stream(text, conv_id):
                if seg != cur_seg:  # 새 세그먼트 → 이전 것 확정하고 새로 시작
                    self._close_segment(write, channel, thread_ts, say, seg_state)
                    cur_seg, seg_state = seg, {"ts": None, "update_ok": True, "last": 0.0, "final": ""}
                seg_state["final"] = acc
                if not acc.strip():
                    continue
                now = time.monotonic()
                if seg_state["ts"] is None:
                    # 이 세그먼트의 첫 텍스트 → 여기서 댓글 생성(알림 발생 지점)
                    resp = say(text=acc[:SLACK_MSG_LIMIT], thread_ts=thread_ts)
                    seg_state["ts"], seg_state["last"], posted = resp["ts"], now, True
                    if not logged_first:
                        logged_first = True
                        print(f"[gateway] slack 첫 토큰까지 {now - t0:.1f}s"
                              + (f" (수신→게시 {time.monotonic() - t_recv:.1f}s)" if t_recv else ""))
                elif seg_state["update_ok"] and now - seg_state["last"] >= self.interval:
                    seg_state["update_ok"] = self._update(write, channel, seg_state["ts"], acc)
                    seg_state["last"] = now
        except Exception as e:
            traceback.print_exc()
            say(text=f"에이전트 처리 중 오류: {e}", thread_ts=thread_ts)
            return
        self._close_segment(write, channel, thread_ts, say, seg_state)
        if not posted:  # 텍스트가 한 번도 안 나온 경우
            say(text="(빈 응답)", thread_ts=thread_ts)

    @staticmethod
    def _update(write, channel, ts, text) -> bool:
        """chat.update 로 갱신. 성공하면 True, 실패하면 원인을 로그로 남기고 False."""
        from slack_sdk.errors import SlackApiError

        try:
            write.chat_update(channel=channel, ts=ts, text=text[:SLACK_MSG_LIMIT])
            return True
        except SlackApiError as e:
            err = e.response.get("error", str(e))
            print(f"[gateway] slack chat_update 실패({err}) — 스트리밍 갱신 중단, "
                  f"최종 답변은 새 메시지로 보냅니다. (봇에 chat:write 스코프 필요)")
            return False

    def _close_segment(self, write, channel, thread_ts, say, state) -> None:
        """세그먼트 하나를 최종본으로 확정한다(4000자 초과분은 스레드에 이어붙임)."""
        if not state or state["ts"] is None:
            return
        head, rest = state["final"][:SLACK_MSG_LIMIT], state["final"][SLACK_MSG_LIMIT:]
        if state["update_ok"]:
            self._update(write, channel, state["ts"], head)  # 최종본으로 확정
        else:
            say(text=head, thread_ts=thread_ts)  # 편집 불가 → 전체를 새 댓글로
        while rest:
            say(text=rest[:SLACK_MSG_LIMIT], thread_ts=thread_ts)
            rest = rest[SLACK_MSG_LIMIT:]

    def check(self) -> tuple[str, str]:
        # Socket Mode 수신에는 App-Level Token(xapp-)이 필수다.
        # xapp- 가 있어야 'Slack 수신을 쓰겠다'는 의도로 본다(봇 토큰만 있으면 도구 전용).
        if not self.c.app_token:
            return "unset", ""
        if not self.c.bot_token:
            return "fail", "SLACK_BOT_TOKEN 이 필요합니다(Socket Mode)."
        ok, detail = self.c.verify()
        return ("ok" if ok else "fail"), detail

    def run(self) -> None:
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
            from slack_sdk import WebClient
        except ImportError:
            print("[gateway] slack-bolt 미설치 — Slack 수신 건너뜀 (uv sync 필요)")
            return

        app = App(client=WebClient(token=self.c.bot_token, ssl=connectors._insecure_ssl))

        @app.event("app_mention")
        def _on_mention(event, say):
            t_recv = _log_recv(event)
            text = event.get("text", "")
            channel = event["channel"]
            thread_ts = event.get("thread_ts") or event.get("ts")
            conv_id = f"slack:{channel}:{thread_ts}"
            if self.stream:
                self._respond(text, conv_id, channel, say, thread_ts=thread_ts, t_recv=t_recv)
                return
            try:
                reply = answer(text, conv_id)
            except Exception as e:
                traceback.print_exc()
                reply = f"에이전트 처리 중 오류: {e}"
            say(text=reply, thread_ts=thread_ts)

        @app.event("message")
        def _on_message(event, say):
            # 봇에게 온 1:1 DM 만 처리(봇 자신의 메시지·일반 채널 메시지 제외).
            # 편집/삭제/입장 등 subtype 이벤트는 무시(실제 새 발화만 응답).
            if event.get("subtype"):
                return
            if event.get("channel_type") == "im" and not event.get("bot_id"):
                channel = event["channel"]
                # 스레드로 답하고, 스레드 단위로 맥락을 잇는다.
                #   새 최상위 메시지 → thread_ts = 그 메시지 ts → 새 대화(맥락 끊김)
                #   스레드 안의 답장 → thread_ts = 원본 ts → 같은 대화(맥락 유지)
                thread_ts = event.get("thread_ts") or event["ts"]
                conv_id = f"slack:{channel}:{thread_ts}"
                text = event.get("text", "")
                t_recv = _log_recv(event)
                if self.stream:
                    self._respond(text, conv_id, channel, say, thread_ts=thread_ts, t_recv=t_recv)
                    return
                try:
                    reply = answer(text, conv_id)
                except Exception as e:
                    traceback.print_exc()
                    reply = f"에이전트 처리 중 오류: {e}"
                say(text=reply, thread_ts=thread_ts)

        SocketModeHandler(app, self.c.app_token).start()


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def start_in_background(agent_factory=None) -> list[str]:
    """env 가 채워지고 '실제 연결이 확인된' 채널만 데몬 스레드로 띄운다.

    agent_factory: 호출하면 그래프를 만드는 콜러블(메인 파일이 build_agent 를 넘겨 재사용).
                   None 이면 standalone 로 자체 로드한다. 활성 채널이 하나도 없으면
                   에이전트를 만들지 않는다.

    반환: 실제로 실행된(연결 확인된) 채널 이름 리스트.
    """
    active: list[ChannelAdapter] = []
    adapters: list[ChannelAdapter] = [TelegramAdapter(), SlackAdapter()]
    if os.getenv("EMAIL_CONNECTOR_ENABLED", "1").lower() not in {"0", "false", "no", "off"}:
        adapters.append(EmailTriggerAdapter())
    for a in adapters:
        state, detail = a.check()
        if state == "ok":
            active.append(a)
            print(f"[gateway] {a.name} 연결 확인 → 실행 ({detail})")
        elif state == "fail":
            print(f"[gateway] {a.name} 연결 실패로 제외: {detail}")
        # "unset" 은 조용히 무시(해당 채널을 쓸 의도 없음)

    if not active:
        return []

    if agent_factory is not None:
        set_agent(agent_factory())
    else:
        _ensure_agent()

    for a in active:
        threading.Thread(target=a.run, name=a.name, daemon=True).start()
    return [a.name for a in active]


def main() -> None:
    names = start_in_background()
    if not names:
        print(
            "연결된 채널이 없습니다. .env 를 채우고 정상 연결되는지 확인하세요.\n"
            "  Telegram: TELEGRAM_BOT_TOKEN\n"
            "  Slack   : SLACK_BOT_TOKEN + SLACK_APP_TOKEN(xapp-)\n"
            "  (Email 은 게이트웨이 채널이 아니라 에이전트 도구로만 동작합니다.)"
        )
        return
    print("게이트웨이 시작:", ", ".join(names))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n게이트웨이를 종료합니다.")


if __name__ == "__main__":
    main()
