#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
컴퓨터네트워크 과제 - 선택1(구현)  [서버]

소프트웨어학부 20233032 김주한

=====================================================
TCP 소켓 위에 HTTP/2.0 프로토콜을 직접 구현한 서버.
백엔드는 데이터 '파일'들을 간이 데이터베이스처럼 다뤄
사용자 정보(id, 이름, 전화, 이메일, 학번)를 CRUD 한다.

[이번 확장] 교수님 조언(유연한 CREATE) 반영:
  "데이터를 기존 파일에 생성하는 방법 / 새 파일을 만들어 생성하는 방법,
   둘 다 유연하게" → 그래서 '파일 단위 CRUD'를 추가했다.
  (한 파일 = 하나의 사용자 목록/그룹. 예: userdata, friends, classA ...)

  ── 파일 단위 (CREATE·DELETE 를 '파일'에도 적용) ──
  - GET    /files            : 데이터 파일 목록             -> 200
  - POST   /files            : 새 파일 생성(body=파일이름)   -> 201 / 400 / 409
  - DELETE /files/{f}        : 파일 통째 삭제               -> 200 / 404 / 405(기본파일)

  ── 레코드 단위 (선택한 파일 '안'의 데이터) ──
  - GET    /files/{f}        : 그 파일 전체 목록            -> 200 / 404(파일없음)
  - GET    /files/{f}/{id}   : 한 명 조회                   -> 200 / 404
  - POST   /files/{f}        : 데이터 추가(id 자동)          -> 201 / 400 / 404 / 409 / 422
  - PUT    /files/{f}/{id}   : 데이터 수정                  -> 200 / 400 / 404 / 409 / 422
  - DELETE /files/{f}/{id}   : 데이터 삭제                  -> 200 / 404

  ※ 호환용: /users , /users/{id} 는 예전 그대로 동작한다(= /files/userdata 로 라우팅).

처리하는 HTTP 상태코드(총 10종, 과제 최소 5종 + 추가):
  200 OK / 201 Created / 400 Bad Request(형식 오류) /
  404 Not Found(없는 자원·경로·파일) / 405 Method Not Allowed(미지원 메서드) /
  409 Conflict(중복 학번·이메일·파일) / 411 Length Required(Content-Length 없음) /
  422 Unprocessable Entity(값 형식 오류) / 500 Internal Server Error /
  505 HTTP Version Not Supported(버전 미지원)

연결은 '지속(persistent)' 방식: 클라이언트 하나와 연결을 유지하며
여러 요청을 처리하고, 클라이언트가 연결을 닫으면 종료를 감지해 로그를 남긴다.

[다중 클라이언트] 연결마다 '스레드'를 띄워 '동시에' 처리한다(concurrent server).
  - accept() 로 받은 연결을 handle_client 로 넘길 때, 예전처럼 그 자리에서 처리하지 않고
    새 스레드에 맡긴 뒤 곧바로 다음 accept() 로 돌아간다 → 여러 클라이언트를 동시에 서비스.
  - 여러 스레드가 같은 데이터 파일에 '동시에 쓰면' 내용이 깨질 수 있으므로,
    파일에 접근하는 구간을 threading.Lock 으로 감싸 '한 번에 하나만' 들어가게 한다.
  - CRUD 동작 자체는 예전과 100% 동일. (Lock 은 안전장치일 뿐)

폴더 구조(실무형): 소스는 src/, 데이터 파일은 이웃한 data/ 폴더에 쌓인다.

실행:  (프로젝트 루트에서)  python3 src/server.py   (종료: Ctrl + C)
"""

import socket
import os
import re
import datetime
import threading                                          # 다중 클라이언트(스레드) + Lock

# ===== 상수 =====
HOST = "127.0.0.1"
PORT = 8080
HTTP_VERSION = "HTTP/2.0"
ACCEPTED_VERSIONS = ("HTTP/1.0", "HTTP/1.1", "HTTP/2.0")   # 이 외 버전은 505로 거절

# 데이터 폴더 = 이 스크립트(src/server.py)의 '한 단계 위'에 있는 data/ 폴더.
# (어느 위치에서 실행하든 항상 프로젝트의 data/ 를 가리키도록 __file__ 기준으로 계산)
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DATA_EXT = ".txt"                                         # 데이터 파일 확장자
DEFAULT_FILE = "userdata"                                 # 기본 파일 이름 → userdata.txt
FIELDS = ("id", "name", "phone", "email", "studentid")

# 파일 이름 규칙: 영문/숫자/한글/밑줄/하이픈 1~32자.
# ('/', '..', '.' 등을 막아 데이터 폴더 밖으로 나가는 '경로 조작(path traversal)'을 차단)
FILENAME_RE = re.compile(r"^[A-Za-z0-9_가-힣\-]{1,32}$")

STATUS_TEXT = {
    200: "OK", 201: "Created",
    400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 411: "Length Required", 422: "Unprocessable Entity",
    500: "Internal Server Error", 505: "HTTP Version Not Supported",
}


def recv_http_message(sock, buffer):
    """소켓에서 HTTP 메시지 1개(헤더+바디)를 읽어 (메시지bytes, 남은buffer) 반환.
    상대가 연결을 닫으면 (None, b'') 를 돌려준다. (지속 연결의 메시지 경계 처리)"""
    # 1) 헤더 끝(\r\n\r\n) 까지 모은다
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:                 # 상대가 연결을 닫음
            return None, b""
        buffer += chunk
    header_bytes, _, rest = buffer.partition(b"\r\n\r\n")

    # 2) Content-Length 만큼 바디를 더 받는다
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
    leftover = rest[content_length:]               # 다음 요청의 앞부분일 수 있음
    return header_bytes + b"\r\n\r\n" + body, leftover


class RecordStore:
    """데이터 파일 '하나'를 'id,name,phone,email,studentid' 레코드(한 줄=한 명)로 다루는 간이 DB.
    (예전 UserStore. 이제는 특정 파일 하나에만 묶이지 않고, 경로만 주면 그 파일을 다룬다.)"""

    def __init__(self, path):
        self.path = path                # 이 스토어가 다루는 데이터 파일 경로

    def _read_all(self):
        """파일을 읽어 레코드(dict) 리스트로 반환. 파일이 없으면 빈 리스트."""
        records = []
        if not os.path.isfile(self.path):
            return records
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) != len(FIELDS):     # 필드 수 안 맞는 줄은 건너뜀(깨진 줄 방어)
                    continue
                records.append(dict(zip(FIELDS, parts)))
        return records

    def _write_all(self, records):
        """레코드 리스트를 파일에 통째로 다시 쓴다(수정/삭제 후 저장)."""
        with open(self.path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(",".join(r[k] for k in FIELDS) + "\n")

    def list_all(self):
        return self._read_all()

    def find(self, uid):
        for r in self._read_all():
            if r["id"] == uid:
                return r
        return None

    def find_by_field(self, field, value):
        """특정 필드(studentid·email 등) 값이 같은 레코드를 찾는다. (중복 검사용)"""
        for r in self._read_all():
            if r[field] == value:
                return r
        return None

    def next_id(self):
        """현재 파일에서 가장 큰 id + 1. (파일마다 id는 독립적으로 매겨짐)"""
        ids = [int(r["id"]) for r in self._read_all() if r["id"].isdigit()]
        return str(max(ids) + 1) if ids else "1"

    def add(self, name, phone, email, studentid):
        uid = self.next_id()
        records = self._read_all()
        records.append({"id": uid, "name": name, "phone": phone,
                        "email": email, "studentid": studentid})
        self._write_all(records)
        return uid

    def update(self, uid, name, phone, email, studentid):
        records = self._read_all()
        for r in records:
            if r["id"] == uid:
                r["name"], r["phone"], r["email"], r["studentid"] = name, phone, email, studentid
                self._write_all(records)
                return True
        return False

    def delete(self, uid):
        records = self._read_all()
        kept = [r for r in records if r["id"] != uid]
        if len(kept) == len(records):     # 지운 게 없음 = 그 id가 원래 없었음
            return False
        self._write_all(kept)
        return True


class FileManager:
    """DATA_DIR 안의 여러 '데이터 파일'을 관리한다.
    - 파일 = '{이름}.txt'. 한 파일이 하나의 사용자 목록(간이 테이블)이다.
    - 교수님 조언(유연한 CREATE): 데이터를 '기존 파일에 추가'하거나
      '새 파일을 만들어' 담을 수 있도록, 파일 자체도 만들고/지울 수 있게 했다."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)     # data/ 폴더가 없으면 만든다
        # 기본 파일(userdata.txt)이 없으면 시드 3명으로 만들어 둔다(기존 동작 유지).
        if not self.exists(DEFAULT_FILE):
            self._seed_default()

    def _path(self, name):
        """파일 이름 → 실제 경로. (항상 DATA_DIR 안, 확장자 자동 부착)"""
        return os.path.join(self.data_dir, name + DATA_EXT)

    @staticmethod
    def valid_name(name):
        """파일 이름이 규칙에 맞는지 검사. (빈 값·경로 문자·너무 김 → False)"""
        return bool(name) and bool(FILENAME_RE.match(name))

    def _seed_default(self):
        seed = [
            "1,홍길동,010-1234-1234,test1@kookmin.ac.kr,20210001",
            "2,김철수,010-5678-5678,test2@kookmin.ac.kr,20210002",
            "3,이영희,010-9999-8888,test3@kookmin.ac.kr,20210003",
        ]
        with open(self._path(DEFAULT_FILE), "w", encoding="utf-8") as f:
            f.write("\n".join(seed) + "\n")

    def list_files(self):
        """DATA_DIR 안 데이터 파일 이름 목록(확장자 뗀 이름, 정렬)."""
        names = []
        for fn in os.listdir(self.data_dir):
            if fn.endswith(DATA_EXT):
                names.append(fn[:-len(DATA_EXT)])
        return sorted(names)

    def exists(self, name):
        return os.path.isfile(self._path(name))

    def create_file(self, name):
        """빈 데이터 파일 생성. 성공 True / 이미 있으면 False."""
        if self.exists(name):
            return False
        open(self._path(name), "w", encoding="utf-8").close()   # 빈 파일 생성
        return True

    def delete_file(self, name):
        """데이터 파일 통째 삭제. 성공 True / 없으면 False."""
        if not self.exists(name):
            return False
        os.remove(self._path(name))
        return True

    def store(self, name):
        """그 파일을 다루는 RecordStore 반환."""
        return RecordStore(self._path(name))


class HTTPServer:
    """소켓을 열고 HTTP 요청을 받아 파일/레코드를 조작한 뒤 응답을 돌려주는 서버."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.files = FileManager(DATA_DIR)     # 여러 데이터 파일 관리자
        self.lock = threading.Lock()           # 파일 접근 직렬화용(동시 쓰기 충돌 방지)
        self.sock = None

    # ---------- 응답 만들기 ----------
    def make_response(self, status, body="", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        reason = STATUS_TEXT.get(status, "Unknown")
        headers = [
            f"{HTTP_VERSION} {status} {reason}",
            f"Date: {datetime.datetime.now():%a, %d %b %Y %H:%M:%S}",
            "Server: MyHTTP/1.0 (Python socket)",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(body)}",
        ]
        if extra_headers:
            headers += extra_headers
        headers.append("Connection: keep-alive")     # 연결 유지(지속)
        head = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
        return head + body

    # ---------- 메시지 파싱 ----------
    def parse_message(self, message):
        header_bytes, _, body = message.partition(b"\r\n\r\n")
        lines = header_bytes.decode("utf-8", "replace").split("\r\n")
        request_line = lines[0] if lines else ""
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return request_line, headers, body.decode("utf-8", "replace")

    def parse_body(self, body):
        """바디 'name,phone,email,studentid' → [4개] / 구조가 틀리면 None.
        (필드 개수·빈 값 같은 '구조' 검사 → 틀리면 400)"""
        parts = [p.strip() for p in body.strip().split(",")]
        if len(parts) != 4 or any(p == "" for p in parts):
            return None
        return parts

    def validate_fields(self, name, phone, email, studentid):
        """필드 '값'의 형식을 검사한다. 문제 있으면 사유 문자열, 정상이면 None.
        (구조는 맞지만 값이 이상한 경우 → 422 Unprocessable Entity)"""
        if not studentid.isdigit():
            return "학번(studentid)은 숫자만 가능합니다."
        if "@" not in email or "." not in email:
            return "이메일 형식이 올바르지 않습니다. (@ 와 . 필요)"
        if not any(c.isdigit() for c in phone):
            return "전화번호에 숫자가 없습니다."
        return None

    # ---------- 라우팅(메서드 + 경로 → 동작) ----------
    def route(self, method, path, body):
        segments = [s for s in path.split("/") if s]
        if not segments:
            return self.make_response(404, "404 Not Found: 빈 경로입니다.\n")

        # (호환) /users... 는 예전 클라이언트/캡처를 위해 /files/userdata... 로 변환
        if segments[0] == "users":
            segments = ["files", DEFAULT_FILE] + segments[1:]

        if segments[0] != "files":
            return self.make_response(404, "404 Not Found: 알 수 없는 경로입니다. (/files, /users)\n")

        # ── /files : 파일 목록(GET) / 새 파일 생성(POST) ──
        if len(segments) == 1:
            if method == "GET":
                return self.handle_list_files()
            if method == "POST":
                return self.handle_create_file(body)
            return self.make_response(
                405, "405 Method Not Allowed: /files 에는 GET, POST 만 됩니다.\n",
                ["Allow: GET, POST"])

        fname = segments[1]

        # ── /files/{f} : 그 파일의 목록(GET) / 데이터 추가(POST) / 파일 삭제(DELETE) ──
        if len(segments) == 2:
            if method == "GET":
                return self.handle_get(fname, None)
            if method == "POST":
                return self.handle_post(fname, body)
            if method == "DELETE":
                return self.handle_delete_file(fname)
            return self.make_response(
                405, "405 Method Not Allowed: /files/{f} 에는 GET, POST, DELETE 만 됩니다.\n",
                ["Allow: GET, POST, DELETE"])

        # ── /files/{f}/{id} : 한 명 조회(GET) / 수정(PUT) / 삭제(DELETE) ──
        uid = segments[2]
        if method == "GET":
            return self.handle_get(fname, uid)
        if method == "PUT":
            return self.handle_put(fname, uid, body)
        if method == "DELETE":
            return self.handle_delete(fname, uid)
        return self.make_response(
            405, "405 Method Not Allowed: /files/{f}/{id} 에는 GET, PUT, DELETE 만 됩니다.\n",
            ["Allow: GET, PUT, DELETE"])

    # ---------- 파일 단위 핸들러 ----------
    def handle_list_files(self):
        """데이터 파일 목록 + 각 파일의 인원수."""
        names = self.files.list_files()
        lines = []
        for n in names:
            count = len(self.files.store(n).list_all())
            mark = " (기본)" if n == DEFAULT_FILE else ""
            lines.append(f"- {n}{mark} : {count}명")
        body = "[데이터 파일 목록]\n" + ("\n".join(lines) if lines else "(없음)") + "\n"
        return self.make_response(200, body)

    def handle_create_file(self, body):
        """새 파일 생성. (CREATE 의 '새 파일로 데이터 생성' 갈래)"""
        name = body.strip()
        if not self.files.valid_name(name):
            return self.make_response(
                400, "400 Bad Request: 파일 이름은 영문/숫자/한글/_/- 1~32자만 됩니다.\n")
        if not self.files.create_file(name):
            return self.make_response(409, f"409 Conflict: 파일 '{name}' 이(가) 이미 있습니다.\n")
        return self.make_response(
            201, f"201 Created: 파일 '{name}{DATA_EXT}' 생성 완료. (이제 이 파일에 POST 로 데이터 추가)\n",
            [f"Location: /files/{name}"])

    def handle_delete_file(self, fname):
        """파일 통째 삭제. (기본 파일 userdata 는 보호)"""
        if fname == DEFAULT_FILE:
            return self.make_response(
                405, f"405 Method Not Allowed: 기본 파일('{DEFAULT_FILE}')은 삭제할 수 없습니다.\n")
        if self.files.delete_file(fname):
            return self.make_response(200, f"200 OK: 파일 '{fname}{DATA_EXT}' 삭제 완료.\n")
        return self.make_response(404, f"404 Not Found: 파일 '{fname}' 이(가) 없습니다.\n")

    # ---------- 레코드 단위 핸들러 (파일 이름을 받아 그 파일을 다룸) ----------
    def handle_get(self, fname, uid):
        if not self.files.exists(fname):
            return self.make_response(404, f"404 Not Found: 파일 '{fname}' 이(가) 없습니다.\n")
        store = self.files.store(fname)
        if uid is None:                                  # 파일 전체 목록
            records = store.list_all()
            if not records:
                return self.make_response(200, f"(파일 '{fname}' 에 등록된 사용자가 없습니다.)\n")
            body = "\n".join(",".join(r[k] for k in FIELDS) for r in records) + "\n"
            return self.make_response(200, body)
        rec = store.find(uid)                            # 한 명 조회
        if rec is None:
            return self.make_response(404, f"404 Not Found: 파일 '{fname}' 에 id={uid} 사용자가 없습니다.\n")
        return self.make_response(200, ",".join(rec[k] for k in FIELDS) + "\n")

    def handle_post(self, fname, body):
        """선택한 파일에 데이터 추가. (CREATE 의 '기존 파일에 데이터 생성' 갈래)"""
        if not self.files.exists(fname):
            return self.make_response(
                404, f"404 Not Found: 파일 '{fname}' 이(가) 없습니다. (먼저 POST /files 로 파일 생성)\n")
        store = self.files.store(fname)
        parsed = self.parse_body(body)
        if parsed is None:                               # 구조 오류 → 400
            return self.make_response(400, "400 Bad Request: 본문은 'name,phone,email,studentid'(이름,전화,이메일,학번) 형식이어야 합니다.\n")
        name, phone, email, studentid = parsed
        reason = self.validate_fields(name, phone, email, studentid)
        if reason:                                       # 값 오류 → 422
            return self.make_response(422, f"422 Unprocessable Entity: {reason}\n")
        if store.find_by_field("studentid", studentid):  # 같은 파일 안 중복 → 409
            return self.make_response(409, f"409 Conflict: 파일 '{fname}' 에 이미 등록된 학번({studentid})입니다.\n")
        if store.find_by_field("email", email):
            return self.make_response(409, f"409 Conflict: 파일 '{fname}' 에 이미 등록된 이메일({email})입니다.\n")
        uid = store.add(name, phone, email, studentid)
        return self.make_response(201, f"201 Created: 파일 '{fname}' 에 id={uid} 추가 완료.\n",
                                  [f"Location: /files/{fname}/{uid}"])

    def handle_put(self, fname, uid, body):
        """선택한 파일 안 특정 레코드 수정. (UPDATE)"""
        if not self.files.exists(fname):
            return self.make_response(404, f"404 Not Found: 파일 '{fname}' 이(가) 없습니다.\n")
        store = self.files.store(fname)
        parsed = self.parse_body(body)
        if parsed is None:                               # 구조 오류 → 400
            return self.make_response(400, "400 Bad Request: 본문은 'name,phone,email,studentid'(이름,전화,이메일,학번) 형식이어야 합니다.\n")
        name, phone, email, studentid = parsed
        reason = self.validate_fields(name, phone, email, studentid)
        if reason:                                       # 값 오류 → 422
            return self.make_response(422, f"422 Unprocessable Entity: {reason}\n")
        dup = store.find_by_field("studentid", studentid)   # 같은 파일의 '다른 사람' 학번과 충돌 → 409
        if dup and dup["id"] != uid:
            return self.make_response(409, f"409 Conflict: 학번({studentid})은 id={dup['id']} 사용자가 이미 쓰고 있습니다.\n")
        if store.update(uid, name, phone, email, studentid):
            return self.make_response(200, f"200 OK: 파일 '{fname}' 의 id={uid} 수정 완료.\n")
        return self.make_response(404, f"404 Not Found: 파일 '{fname}' 에 id={uid} 사용자가 없습니다.\n")

    def handle_delete(self, fname, uid):
        """선택한 파일 안 특정 레코드 삭제. (DELETE 의 '파일 안 데이터 삭제' 갈래)"""
        if not self.files.exists(fname):
            return self.make_response(404, f"404 Not Found: 파일 '{fname}' 이(가) 없습니다.\n")
        store = self.files.store(fname)
        if store.delete(uid):
            return self.make_response(200, f"200 OK: 파일 '{fname}' 의 id={uid} 삭제 완료.\n")
        return self.make_response(404, f"404 Not Found: 파일 '{fname}' 에 id={uid} 사용자가 없습니다.\n")

    # ---------- 연결 1건(클라이언트 1명) 처리 — 각 연결이 '별도 스레드'에서 돈다 ----------
    def handle_client(self, conn, addr):
        tname = threading.current_thread().name           # 이 연결을 맡은 스레드 이름
        client = f"{addr[0]}:{addr[1]}"
        print(f"[접속]  클라이언트 {client} 연결됨  ({tname})")
        buffer = b""
        try:
            while True:
                message, buffer = recv_http_message(conn, buffer)
                if message is None:                       # 클라이언트가 연결을 닫음
                    print(f"[종료]  클라이언트 {client} 가 종료되었습니다.")
                    break
                request_line, headers, body = self.parse_message(message)
                parts = request_line.split()
                if len(parts) != 3:
                    conn.sendall(self.make_response(400, "400 Bad Request: 요청라인 형식 오류.\n"))
                    continue
                method, path, version = parts

                # 버전 검사 → 505
                if version not in ACCEPTED_VERSIONS:
                    conn.sendall(self.make_response(
                        505, f"505 HTTP Version Not Supported: '{version}'는 지원하지 않습니다. "
                             f"(지원: {', '.join(ACCEPTED_VERSIONS)})\n"))
                    continue
                # 바디가 필요한 메서드인데 Content-Length 헤더가 없음 → 411
                if method in ("POST", "PUT") and "content-length" not in headers:
                    conn.sendall(self.make_response(
                        411, "411 Length Required: POST/PUT 요청에는 Content-Length 헤더가 필요합니다.\n"))
                    continue

                # 라우팅 중 예기치 못한 오류 → 500
                #  ▸ Lock: 여러 스레드가 같은 파일에 동시에 쓰면 내용이 깨지므로,
                #    파일을 실제로 읽고/쓰는 route() 구간은 '한 번에 한 스레드만' 들어가게 한다.
                #    (네트워크 수신·응답 전송은 잠금 밖 → 연결 자체는 여전히 동시 처리)
                try:
                    with self.lock:
                        response = self.route(method, path, body)
                except Exception as e:
                    response = self.make_response(500, f"500 Internal Server Error: {e}\n")
                conn.sendall(response)

                status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace")
                print(f"[{datetime.datetime.now():%H:%M:%S}] {client} ({tname})  {method} {path}  ->  "
                      f"{status_line.replace(HTTP_VERSION + ' ', '')}")
        except ConnectionResetError:
            print(f"[종료]  클라이언트 {client} 연결이 끊겼습니다.")
        except Exception as e:
            print("[처리 오류]", e)
        finally:
            conn.close()

    # ---------- 서버 시작 ----------
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)
        print(f"[서버 시작] {HTTP_VERSION}  http://{self.host}:{self.port}  (다중 클라이언트: 스레드)")
        print(f"[데이터 폴더] {DATA_DIR}  (파일 = *.txt)")
        print("[종료] Ctrl + C\n")
        try:
            while True:
                conn, addr = self.sock.accept()       # 클라이언트 1명 받기(3-way handshake 완료)
                # 예전엔 여기서 바로 처리(handle_client)해 '한 명 끝나야 다음'이었다(iterative).
                # 이제는 처리를 '새 스레드'에 맡기고 곧바로 다음 accept() 로 돌아간다 → 동시 처리.
                t = threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("\n[서버 종료] Ctrl+C 입력으로 서버를 종료합니다.")
        finally:
            self.sock.close()


if __name__ == "__main__":
    HTTPServer(HOST, PORT).start()
