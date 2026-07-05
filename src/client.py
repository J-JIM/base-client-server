#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
컴퓨터네트워크 과제 - 선택1(구현)  [클라이언트]
=====================================================
사용자가 입력한 '친근한 명령'을 HTTP/2.0 요청으로 바꿔 서버에 보내고
응답을 출력한다. 서버와 연결을 '유지(persistent)'하며 여러 요청을 보낸다.

[이번 확장] 데이터를 담을 '파일'을 여러 개 다룰 수 있다.
  → 교수님 조언대로 '기존 파일에 데이터 추가' 와 '새 파일 만들어 담기' 를 모두 지원.
  - 파일 관리:  FILES / NEWFILE <이름> / DELFILE <이름> / USE <이름>
  - 데이터 관리(현재 선택된 파일 대상):  GET / POST / PUT / DELETE

명령:
  ── 파일 ──
  FILES              파일 목록 보기
  NEWFILE <이름>      새 파일 생성        (C: 파일 Create)
  DELFILE <이름>      파일 삭제           (D: 파일 Delete)
  USE <이름>          작업할 파일 선택     (이후 GET/POST/... 이 파일 대상, 서버 요청 아님)
  ── 데이터(현재 파일) ──
  GET                전체 조회   (R: Read)
  GET <id>           한 명 조회  (R: Read)
  POST <레코드>       추가       (C: Create)
  PUT <id> <레코드>   수정       (U: Update)
  DELETE <id>        삭제       (D: Delete)
  help               안내 다시 보기
  quit / exit        종료

실행:  (프로젝트 루트에서)  python3 src/client.py
       (먼저 다른 터미널에서  python3 src/server.py  실행)
       ※ 여러 터미널에서 client 를 동시에 띄우면 서버가 '동시에' 처리한다(다중 클라이언트).
"""

import socket

HOST = "127.0.0.1"
PORT = 8080
HTTP_VERSION = "HTTP/2.0"
DEFAULT_FILE = "userdata"        # 시작할 때 선택돼 있는 기본 파일


def recv_http_message(sock, buffer):
    """소켓에서 HTTP 응답 1개(헤더+바디)를 읽어 (메시지bytes, 남은buffer) 반환.
    서버가 연결을 닫으면 (None, b'')."""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            return None, b""
        buffer += chunk
    header_bytes, _, rest = buffer.partition(b"\r\n\r\n")

    content_length = 0
    for line in header_bytes.decode("utf-8", "replace").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                content_length = 0
    while len(rest) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            return None, b""
        rest += chunk

    body = rest[:content_length]
    leftover = rest[content_length:]
    return header_bytes + b"\r\n\r\n" + body, leftover


def build_request(cmd, current_file):
    """친근한 명령 → HTTP/2.0 요청 문자열로 변환.
    current_file = 현재 선택된 데이터 파일(= GET/POST/PUT/DELETE 의 대상)."""
    tokens = cmd.split(" ", 1)
    verb = tokens[0].upper()
    arg = tokens[1].strip() if len(tokens) > 1 else ""
    base = "/files/" + current_file        # 데이터 명령은 '현재 파일'을 대상으로 함

    # ── 데이터(레코드) 명령: 현재 선택된 파일 대상 ──
    if verb == "GET":
        http_method, path, body = "GET", (base + "/" + arg if arg else base), ""
    elif verb == "DELETE":
        http_method, path, body = "DELETE", base + "/" + arg, ""
    elif verb == "POST":
        http_method, path, body = "POST", base, arg
    elif verb == "PUT":
        sub = arg.split(" ", 1)                       # "2 김철수,..." → ["2", "김철수,..."]
        http_method, path, body = "PUT", base + "/" + sub[0], (sub[1] if len(sub) > 1 else "")
    # ── 파일 관리 명령 ──
    elif verb == "FILES":                             # 파일 목록
        http_method, path, body = "GET", "/files", ""
    elif verb == "NEWFILE":                           # 새 파일 생성(새 파일로 데이터 생성)
        http_method, path, body = "POST", "/files", arg
    elif verb == "DELFILE":                           # 파일 통째 삭제
        http_method, path, body = "DELETE", "/files/" + arg, ""
    else:                                             # 그 외 → 서버가 판단(405 등)
        http_method, path, body = verb, "/" + arg, ""

    return (
        f"{http_method} {path} {HTTP_VERSION}\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
        f"{body}"
    )


def print_help():
    """시작할 때와 'help' 입력 시 보여줄 명령어 안내."""
    print("=" * 64)
    print("   📁 사용자 정보 관리 클라이언트 (HTTP/2.0) — 파일 + 데이터 CRUD")
    print("=" * 64)
    print("  [파일 관리]")
    print("   FILES              파일 목록 보기")
    print("   NEWFILE <이름>      새 파일 생성     (C: 파일)   예) NEWFILE friends")
    print("   DELFILE <이름>      파일 삭제        (D: 파일)   예) DELFILE friends")
    print("   USE <이름>          작업 파일 선택    (이후 아래 명령이 이 파일 대상)")
    print("  " + "-" * 60)
    print("  [데이터 관리] — 현재 선택된 파일 대상")
    print("   GET               전체 조회   (R)   GET")
    print("   GET <id>          한 명 조회  (R)   GET 2")
    print("   POST <레코드>      추가       (C)   POST 박철,010-1111-1111,a@b.com,20210004")
    print("   PUT <id> <레코드>  수정       (U)   PUT 2 김철수,010-0000-0000,c@d.com,20210002")
    print("   DELETE <id>       삭제       (D)   DELETE 3")
    print("   help / quit / exit")
    print("  " + "-" * 60)
    print("  ※ <레코드> = 이름,전화번호,이메일,학번 (콤마 구분, 공백 없이)")
    print("  ※ CREATE 두 갈래:  NEWFILE(새 파일 만들기) / POST(기존 파일에 추가)")
    print("  ── 오류 케이스 예시(상태코드 확인용) ──")
    print("   POST 박,010-1,a@b.com,abcd      → 422 (학번이 숫자 아님)")
    print("   POST 김중복,010-1,a@b.com,20210001 → 409 (학번 중복)")
    print("   POST 이름만                       → 400 (필드 부족)")
    print("   GET 999 / DELETE 999             → 404 (없는 id)")
    print("   NEWFILE userdata                  → 409 (이미 있는 파일)")
    print("   USE 없는파일 후 GET               → 404 (파일 없음)")
    print("=" * 64)


def main():
    print_help()

    # 서버에 한 번 연결해서 세션 동안 유지한다(지속 연결)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((HOST, PORT))
    except ConnectionRefusedError:
        print("⚠️  서버에 연결할 수 없습니다. 다른 터미널에서 server.py를 먼저 실행하세요.")
        return

    current_file = DEFAULT_FILE      # 현재 작업 중인 파일(클라이언트 쪽 상태)
    buffer = b""
    try:
        while True:
            try:
                cmd = input(f"\n명령[{current_file}]> ").strip()   # 프롬프트에 현재 파일 표시
            except EOFError:                       # 입력 끝(파이프 종료 등)
                break
            if cmd == "":
                continue
            low = cmd.lower()
            if low in ("quit", "exit"):
                break
            if low == "help":
                print_help()
                continue
            # USE 는 서버에 요청을 보내지 않고 '현재 파일'만 바꾼다(클라이언트 상태)
            if low.split(" ", 1)[0] == "use":
                parts = cmd.split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    print("사용법: USE <파일이름>   (예: USE friends)")
                else:
                    current_file = parts[1].strip()
                    print(f"→ 이제 '{current_file}' 파일을 대상으로 합니다. (FILES 로 목록 확인)")
                continue

            request = build_request(cmd, current_file)
            sock.sendall(request.encode("utf-8"))
            message, buffer = recv_http_message(sock, buffer)
            if message is None:                    # 서버가 연결을 닫음
                print("⚠️  서버와의 연결이 끊어졌습니다.")
                break
            print(message.decode("utf-8", "replace"))
    except KeyboardInterrupt:
        print("\n[Ctrl+C] 입력으로 종료합니다.")
    finally:
        sock.close()                               # 연결 닫기 → 서버가 종료를 감지
        print("클라이언트를 종료했습니다.")


if __name__ == "__main__":
    main()
