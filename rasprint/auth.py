from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
from pathlib import Path
from secrets import token_bytes, token_urlsafe
from threading import Lock
from time import monotonic

from fastapi import Request, Response

from .i18n import ApiError
from .state import atomic_write

COOKIE_NAME = "rasprint_admin"
SESSION_TTL = 8 * 60 * 60
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 5 * 60
PASSWORD_ITERATIONS = 600_000


def password_digest(password: str, salt: bytes | None) -> bytes:
    data = password.encode("utf-8", errors="surrogatepass")
    if salt is None:
        return sha256(data).digest()
    return pbkdf2_hmac("sha256", data, salt, PASSWORD_ITERATIONS)


class AdminAuth:
    def __init__(self, password: str, secure_cookie: bool, state_dir: Path):
        self.password_file = state_dir / "admin-password"
        try:
            version, salt, digest = self.password_file.read_text(encoding="ascii").split(":")
            self.salt, self.password_hash = bytes.fromhex(salt), bytes.fromhex(digest)
            if version != "1" or len(self.salt) != 16 or len(self.password_hash) != 32:
                raise ValueError("Invalid saved admin credential")
        except FileNotFoundError:
            self.salt = None
            self.password_hash = password_digest(password, None)
        self.secure_cookie = secure_cookie
        self.sessions: dict[str, tuple[float, str]] = {}
        self.attempts: dict[str, list[float]] = {}
        self.lock = Lock()
        self.password_lock = Lock()

    def session(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_NAME, "")
        with self.lock:
            session = self.sessions.get(token)
            if session and session[0] > monotonic():
                return session[1]
            self.sessions.pop(token, None)
        return None

    def require(self, request: Request, *, csrf: bool = False) -> None:
        token = self.session(request)
        if token is None:
            raise ApiError(401, "관리자 로그인이 필요합니다.", "Sign in as an administrator.")
        provided = request.headers.get("X-CSRF-Token", "")
        if csrf and not compare_digest(token.encode("ascii"), provided.encode("utf-8")):
            raise ApiError(403, "세션을 새로고침한 뒤 다시 시도하세요.", "Refresh the page and try again.")

    def check_password(self, request: Request, password: str, *, changing: bool = False) -> None:
        ip = request.client.host if request.client else "local"
        key = f"change:{ip}" if changing else f"login:{ip}"
        now = monotonic()
        self.attempts = {
            host: [attempt for attempt in attempts if attempt > now - ATTEMPT_WINDOW]
            for host, attempts in self.attempts.items()
            if attempts and attempts[-1] > now - ATTEMPT_WINDOW
        }
        recent = self.attempts.setdefault(key, [])
        if len(recent) >= MAX_ATTEMPTS:
            retry = max(1, int(recent[0] + ATTEMPT_WINDOW - now) + 1)
            raise ApiError(
                429, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
                "Too many sign-in attempts. Try again in a few minutes.",
                headers={"Retry-After": str(retry)},
            )
        if not compare_digest(self.password_hash, password_digest(password, self.salt)):
            recent.append(now)
            if changing:
                raise ApiError(400, "현재 비밀번호가 맞지 않습니다.", "The current password is incorrect.")
            raise ApiError(401, "비밀번호가 맞지 않습니다.", "Incorrect password.")
        self.attempts.pop(key, None)

    def login(self, request: Request, response: Response, password: str) -> str:
        with self.password_lock:
            self.check_password(request, password)
            now = monotonic()
            cookie = token_urlsafe(32)
            csrf = token_urlsafe(32)
            with self.lock:
                self.sessions = {sid: value for sid, value in self.sessions.items() if value[0] > now}
                self.sessions.pop(request.cookies.get(COOKIE_NAME, ""), None)
                if len(self.sessions) >= 128:
                    self.sessions.pop(next(iter(self.sessions)))
                self.sessions[cookie] = (now + SESSION_TTL, csrf)
        response.set_cookie(COOKIE_NAME, cookie, max_age=SESSION_TTL, httponly=True,
                            samesite="strict", secure=self.secure_cookie)
        return csrf

    def change_password(self, request: Request, response: Response, current: str, new: str) -> None:
        try:
            size = len(new.encode("utf-8"))
        except UnicodeEncodeError:
            size = 0
        if not 8 <= size <= 1024:
            raise ApiError(422, "새 비밀번호는 UTF-8 기준 8~1024바이트여야 합니다.",
                           "The new password must be between 8 and 1024 UTF-8 bytes.")
        with self.password_lock:
            self.require(request, csrf=True)
            self.check_password(request, current, changing=True)
            if current == new:
                raise ApiError(422, "다른 비밀번호를 입력하세요.", "Choose a different password.")
            salt = token_bytes(16)
            digest = password_digest(new, salt)
            atomic_write(self.password_file, f"1:{salt.hex()}:{digest.hex()}".encode("ascii"))
            self.salt, self.password_hash = salt, digest
            with self.lock:
                self.sessions.clear()
            self.attempts.clear()
        self.logout(request, response)

    def logout(self, request: Request, response: Response) -> None:
        with self.lock:
            self.sessions.pop(request.cookies.get(COOKIE_NAME, ""), None)
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict", secure=self.secure_cookie)
