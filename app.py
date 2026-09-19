from __future__ import annotations

import json
import os
import threading
import uuid
import ctypes
import re
import base64
import html as html_lib
from ctypes import wintypes
from urllib.parse import parse_qs, urljoin, urlsplit
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, X, Y, BooleanVar, Button, Canvas, Checkbutton, Entry, Frame, Label, Menu, OptionMenu, StringVar, TclError, Tk, Toplevel, font as tkfont, messagebox
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import requests
from Crypto.Cipher import AES
from PIL import Image, ImageDraw, ImageFont, ImageTk


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "UBAA Desktop"
CACHE_FILE = APP_DIR / "dashboard.json"
CONFIG_FILE = APP_DIR / "config.json"
CREDENTIAL_FILE = APP_DIR / "credentials.bin"
UI_STATE_FILE = APP_DIR / "ui-state.json"
LOG_FILE = APP_DIR / "debug.log"
DEFAULT_ENDPOINT = "https://ubaa.mofrp.top:2021"
LOGIN_MODES = {
    "服务器中转": "SERVER_RELAY",
    "WebVPN": "WEBVPN",
    "直连": "DIRECT",
}

MODE_LABELS = {value: label for label, value in LOGIN_MODES.items()}


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_crypt(data: bytes, decrypt: bool = False) -> bytes:
    """Protect data for the current Windows user without exposing plaintext."""
    if os.name != "nt":
        raise OSError("凭据安全存储仅支持 Windows")
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), source_buffer)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(output)
        )
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), "UBAA Desktop login", None, None, None, 0x01, ctypes.byref(output)
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def save_credentials(username: str, password: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    plaintext = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
    protected = _dpapi_crypt(plaintext)
    temporary = CREDENTIAL_FILE.with_suffix(".tmp")
    temporary.write_bytes(protected)
    temporary.replace(CREDENTIAL_FILE)


def load_credentials() -> tuple[str, str]:
    try:
        payload = json.loads(_dpapi_crypt(CREDENTIAL_FILE.read_bytes(), decrypt=True).decode("utf-8"))
        return str(payload.get("username", "")), str(payload.get("password", ""))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return "", ""


def load_ui_state() -> dict:
    try:
        payload = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def update_ui_state(**values: object) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**load_ui_state(), **values}
    UI_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def save_window_position(x: int, y: int) -> None:
    update_ui_state(x=int(x), y=int(y))


def load_window_position() -> tuple[int, int]:
    try:
        payload = load_ui_state()
        return int(payload["x"]), int(payload["y"])
    except (ValueError, TypeError, KeyError):
        return 48, 80


def save_transparent_background(enabled: bool) -> None:
    update_ui_state(transparent_background=bool(enabled))


def load_transparent_background() -> bool:
    return bool(load_ui_state().get("transparent_background", False))


def debug_log(event: str, detail: str = "") -> None:
    """Write diagnostics without credentials, tokens, cookies or response bodies."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {event}"
        if detail:
            line += f" | {detail[:240]}"
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


class LocalCampusClient:
    """本科生本地连接适配，按 UBAA 的 CAS/WebVPN 连接路径工作."""

    SSO = "https://sso.buaa.edu.cn/login"
    UC_ACTIVATE = "https://uc.buaa.edu.cn/api/login?target=https%3A%2F%2Fuc.buaa.edu.cn%2F%23%2Fuser%2Flogin"
    UC_STATUS = "https://uc.buaa.edu.cn/api/uc/status"
    CURRENT_USER = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp/api/home/currentUser.do"
    TODAY = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp/api/home/teachingSchedule/detail.do"
    PORTAL = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp/index.html"
    SPOC_CAS = "https://spoc.buaa.edu.cn/spocnewht/cas"
    SPOC_LOGIN = "https://spoc.buaa.edu.cn/spocnewht/sys/casLogin"
    SPOC_TERM = "https://spoc.buaa.edu.cn/spocnewht/inco/ht/queryOne"
    SPOC_COURSES = "https://spoc.buaa.edu.cn/spocnewht/jxkj/queryKclb"
    SPOC_ASSIGNMENTS = "https://spoc.buaa.edu.cn/spocnewht/inco/ht/queryListByPage"
    SPOC_TERM_PARAM = "YHrxtTavu6raCwC0/qdgYffB9evWHBkTng/XS4W6j3f/TPo02iEPSoegscDTRNzIPRG49o3RHl4JiFCXAiBkkA=="
    SPOC_ASSIGNMENTS_SQL = "1713252980496efac7d5d9985e81693116d3e8a52ebf2b"
    SPOC_KEY = b"inco12345678ocni"
    SPOC_IV = b"ocni12345678inco"
    JUDGE_BASE = "https://judge.buaa.edu.cn"
    JUDGE_LOGIN = "https://sso.buaa.edu.cn/login?service=http%3A%2F%2Fjudge.buaa.edu.cn%2F"
    VPN_HOST = "d.buaa.edu.cn"
    VPN_KEY = b"wrdvpnisthebest!"

    def __init__(self, mode: str, cookies: dict | list[dict[str, str]] | None = None):
        self.mode = mode
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "UBAA-Desktop/0.1", "Accept": "application/json, text/javascript, */*; q=0.01"})
        if isinstance(cookies, list):
            for item in cookies:
                if item.get("name") and item.get("value"):
                    self.session.cookies.set(item["name"], item["value"], domain=item.get("domain") or None, path=item.get("path", "/"))
        elif cookies:
            # Backward compatibility with the first local-mode build.
            self.session.cookies.update(cookies)

    def cookie_payload(self) -> list[dict[str, str]]:
        return [{"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"} for c in self.session.cookies]

    def bootstrap_portal(self) -> None:
        # BYXT establishes its own application session through this endpoint.
        # Opening index.html does not perform the CAS hand-off (and currently
        # returns 404), which leaves every schedule API call at HTTP 401.
        url = self.upstream_url(self.CURRENT_USER)
        response = self.session.get(url, timeout=20)
        debug_log(
            "local.portal.bootstrap",
            f"status={response.status_code} redirects={len(response.history)} final_host={urlsplit(response.url).hostname} cookies={len(self.session.cookies)}",
        )
        response.raise_for_status()
        if not response.text.lstrip().startswith(("{", "[")):
            raise RuntimeError("教务系统会话未建立成功，请重新登录")

    @staticmethod
    def _envelope(response: requests.Response, service: str) -> object:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") not in (200, "200"):
            raise RuntimeError(f"{service} 返回异常")
        return payload.get("content")

    @staticmethod
    def _spoc_encrypt(payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw += b"\0" * ((16 - len(raw) % 16) % 16)
        encrypted = AES.new(LocalCampusClient.SPOC_KEY, AES.MODE_CBC, iv=LocalCampusClient.SPOC_IV).encrypt(raw)
        return base64.b64encode(encrypted).decode("ascii")

    def _redirect_url(self, current_url: str, location: str) -> str:
        resolved = urljoin(current_url, location)
        if self.mode == "WEBVPN" and urlsplit(resolved).hostname != self.VPN_HOST:
            return self.upstream_url(resolved)
        return resolved

    def _spoc_tokens(self) -> tuple[str, str | None]:
        current = self.upstream_url(self.SPOC_CAS)
        for _ in range(8):
            response = self.session.get(current, allow_redirects=False, timeout=20)
            for candidate in (response.url, response.headers.get("Location", "")):
                parsed = urlsplit(candidate)
                values = parse_qs(parsed.query)
                token = values.get("token", [""])[0]
                if "/spocnew/cas" in parsed.path and token:
                    return token, values.get("refreshToken", [None])[0]
            location = response.headers.get("Location")
            if not location:
                break
            current = self._redirect_url(response.url, location)
        raise RuntimeError("SPOC 登录跳转未返回令牌")

    @staticmethod
    def _role_code(content: object) -> str | None:
        if not isinstance(content, dict):
            return None
        for key in ("jsdm", "rolecode", "jsdmList"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                first = next((str(item).strip() for item in value if str(item).strip()), "")
                if first:
                    return first
        return None

    def spoc_assignments(self, now: datetime) -> list[Assignment]:
        token, _ = self._spoc_tokens()
        login_headers = {"X-Requested-With": "XMLHttpRequest", "Token": f"Inco-{token}"}
        login = self.session.post(
            self.upstream_url(self.SPOC_LOGIN), json={"token": token}, headers=login_headers, timeout=20
        )
        role = self._role_code(self._envelope(login, "SPOC"))
        if not role:
            raise RuntimeError("SPOC 登录未返回角色信息")
        headers = {**login_headers, "RoleCode": role}
        term_content = self._envelope(
            self.session.post(
                self.upstream_url(self.SPOC_TERM), json={"param": self.SPOC_TERM_PARAM}, headers=headers, timeout=20
            ),
            "SPOC",
        )
        term_code = term_content.get("mrxq") if isinstance(term_content, dict) else None
        if not term_code:
            raise RuntimeError("SPOC 未返回当前学期")
        course_content = self._envelope(
            self.session.get(
                self.upstream_url(self.SPOC_COURSES), params={"kcmc": "", "xnxq": term_code}, headers=headers, timeout=20
            ),
            "SPOC",
        )
        course_names = {
            str(item.get("kcid")): item.get("kcmc")
            for item in (course_content if isinstance(course_content, list) else [])
            if isinstance(item, dict) and item.get("kcid")
        }
        results: list[Assignment] = []
        page_number = 1
        while page_number <= 50:
            request_payload = {
                "pageSize": 15,
                "pageNum": page_number,
                "sqlid": self.SPOC_ASSIGNMENTS_SQL,
                "xnxq": term_code,
                "kcid": "",
                "yzwz": "",
            }
            content = self._envelope(
                self.session.post(
                    self.upstream_url(self.SPOC_ASSIGNMENTS),
                    json={"param": self._spoc_encrypt(request_payload)},
                    headers=headers,
                    timeout=20,
                ),
                "SPOC",
            )
            if not isinstance(content, dict):
                break
            items = content.get("list", [])
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or str(item.get("tjzt", "")).strip() in {"1", "已做", "已提交"}:
                    continue
                due = _parse_time(item.get("zyjzsj"))
                if due and due >= now:
                    course_id = str(item.get("sskcid") or "")
                    results.append(
                        Assignment(
                            item.get("kcmc") or course_names.get(course_id) or "未知课程",
                            item.get("zymc") or "未命名作业",
                            due,
                            "SPOC",
                        )
                    )
            pages = int(content.get("pages") or 1)
            if not content.get("hasNextPage") or page_number >= pages or not items:
                break
            page_number += 1
        debug_log("local.spoc.success", f"pending={len(results)} pages={page_number}")
        return results

    @staticmethod
    def _plain_html(value: str) -> str:
        value = re.sub(r"(?i)<br\s*/?>", " ", value)
        return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", value))).strip()

    @classmethod
    def _judge_courses(cls, body: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r'<a\b[^>]*href\s*=\s*(?:"[^"]*courselist\.jsp\?courseID=(\d+)[^"]*"|\'[^\']*courselist\.jsp\?courseID=(\d+)[^\']*\'|[^\s>]*courselist\.jsp\?courseID=(\d+)[^\s>]*)[^>]*>([\s\S]*?)</a>',
            re.I,
        )
        results: list[tuple[str, str]] = []
        for match in pattern.finditer(body):
            course_id = next((value for value in match.groups()[:3] if value), "")
            name = cls._plain_html(match.group(4))
            if course_id and course_id != "0" and name and (course_id, name) not in results:
                results.append((course_id, name))
        return results

    @classmethod
    def _judge_assignment_links(cls, body: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r'<a\b[^>]*href\s*=\s*(?:"([^"]*assignID=(\d+)[^"]*)"|\'([^\']*assignID=(\d+)[^\']*)\'|([^\s>]*assignID=(\d+)[^\s>]*))[^>]*>([\s\S]*?)</a>',
            re.I,
        )
        results: list[tuple[str, str]] = []
        for match in pattern.finditer(body):
            href = next((value for value in (match.group(1), match.group(3), match.group(5)) if value), "")
            assignment_id = next((value for value in (match.group(2), match.group(4), match.group(6)) if value), "")
            title = cls._plain_html(match.group(7))
            if assignment_id and title and "problemContent" not in href and "judgeDetails" not in href:
                item = (assignment_id, title)
                if item not in results:
                    results.append(item)
        return results

    @classmethod
    def _judge_pending_from_detail(
        cls, body: str, course_name: str, title: str, now: datetime
    ) -> Assignment | None:
        text = cls._plain_html(body)
        time_match = re.search(
            r"作业时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*至\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)",
            text,
        )
        due = _parse_time(time_match.group(2)) if time_match else None
        if not due or due < now:
            return None
        unsubmitted = any(marker in text for marker in ("还未提交代码", "未提交文件", "未提交答案", "未作答", "未提交"))
        submitted = any(
            marker in text
            for marker in ("初次提交时间", "首次提交时间", "最近一次提交时间", "最后一次提交时间", "最后一次修改时间", "已提交", "Accepted")
        )
        if submitted and not unsubmitted:
            return None
        return Assignment(course_name, title, due, "希冀")

    def judge_assignments(self, now: datetime) -> list[Assignment]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/58 Safari/537.3",
        }
        activated = self.session.get(self.upstream_url(self.JUDGE_LOGIN), headers=headers, timeout=20)
        activated.raise_for_status()
        if "name=\"execution\"" in activated.text or urlsplit(activated.url).hostname == "sso.buaa.edu.cn":
            raise RuntimeError("希冀登录状态未建立")
        course_page = self.session.get(
            self.upstream_url(f"{self.JUDGE_BASE}/courselist.jsp?courseID=0"), headers=headers, timeout=20
        )
        course_page.raise_for_status()
        courses = self._judge_courses(course_page.text)
        results: list[Assignment] = []
        assignment_count = 0
        for course_id, course_name in courses:
            self.session.get(
                self.upstream_url(f"{self.JUDGE_BASE}/courselist.jsp?courseID={course_id}"), headers=headers, timeout=20
            ).raise_for_status()
            assignment_page = self.session.get(
                self.upstream_url(f"{self.JUDGE_BASE}/assignment/index.jsp"), headers=headers, timeout=20
            )
            assignment_page.raise_for_status()
            for assignment_id, title in self._judge_assignment_links(assignment_page.text):
                assignment_count += 1
                detail = self.session.get(
                    self.upstream_url(f"{self.JUDGE_BASE}/assignment/index.jsp?assignID={assignment_id}"),
                    headers=headers,
                    timeout=20,
                )
                detail.raise_for_status()
                pending = self._judge_pending_from_detail(detail.text, course_name, title, now)
                if pending:
                    results.append(pending)
        debug_log("local.judge.success", f"courses={len(courses)} assignments={assignment_count} pending={len(results)}")
        return results

    def upstream_url(self, url: str) -> str:
        if self.mode != "WEBVPN":
            return url
        parsed = urlsplit(url)
        protocol = parsed.scheme if parsed.port in (None, 443, 80) else f"{parsed.scheme}-{parsed.port}"
        host = parsed.hostname or ""
        padded = host.encode("utf-8") + b"0" * ((16 - len(host.encode("utf-8")) % 16) % 16)
        encrypted = AES.new(self.VPN_KEY, AES.MODE_CFB, iv=self.VPN_KEY, segment_size=128).encrypt(padded)
        encoded_host = self.VPN_KEY.hex() + encrypted.hex()[: len(host.encode("utf-8")) * 2]
        return f"https://{self.VPN_HOST}/{protocol}/{encoded_host}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")

    @staticmethod
    def _hidden_fields(html: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for match in re.finditer(r"<input\b([^>]*)>", html, re.I):
            attrs = dict(re.findall(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", match.group(1)))
            name = attrs.get("name", "").strip()
            if name and attrs.get("type", "hidden").lower() not in {"submit", "button", "image"}:
                fields[name] = attrs.get("value", "")
        return fields

    def login(self, username: str, password: str) -> dict:
        login_url = self.upstream_url(self.SSO)
        debug_log("local.login.start", f"mode={self.mode} host={urlsplit(login_url).hostname}")
        response = self.session.get(login_url, allow_redirects=False, timeout=20)
        debug_log("local.login.page", f"status={response.status_code} content_type={response.headers.get('Content-Type', '')[:40]}")
        if response.status_code in (301, 302, 303, 307, 308):
            self.session.get(self.upstream_url(self.UC_ACTIVATE), timeout=20)
        elif response.ok:
            # Match UBAA's LocalCasParser: only a concrete captcha config means
            # the current login flow requires a captcha. Generic script/style
            # references to the word "captcha" are not enough.
            if re.search(r"config\.captcha\s*=\s*\{\s*type\s*:", response.text, re.I):
                raise RuntimeError("统一认证要求验证码；请暂时使用服务器中转模式")
            form = self._hidden_fields(response.text)
            execution = form.get("execution", "")
            if not execution:
                raise RuntimeError("无法解析统一认证登录流程")
            form.update({"username": username, "password": password, "execution": execution, "_eventId": "submit", "submit": "登录", "type": "username_password"})
            result = self.session.post(login_url, data=form, allow_redirects=True, timeout=25)
            debug_log("local.login.submit", f"status={result.status_code} final_host={urlsplit(result.url).hostname}")
            if "input name=\"execution\"" in result.text or "账号或密码错误" in result.text:
                raise RuntimeError("账号或密码错误，或统一认证拒绝了本地登录")
            self.session.get(self.upstream_url(self.UC_ACTIVATE), timeout=20)
        else:
            raise RuntimeError(f"统一认证不可用：HTTP {response.status_code}")
        status = self.session.get(self.upstream_url(self.UC_STATUS), timeout=20)
        debug_log("local.login.status", f"status={status.status_code} content_type={status.headers.get('Content-Type', '')[:40]}")
        if not status.ok:
            raise RuntimeError(f"本地会话校验失败：HTTP {status.status_code}")
        payload = status.json()
        if payload.get("code") not in (0, "0") or not payload.get("data"):
            raise RuntimeError("本地会话未建立成功")
        self.bootstrap_portal()
        return self.cookie_payload()

    def dashboard(self) -> Snapshot:
        now = datetime.now()
        # Refresh/establish the service-specific BYXT session before every
        # synchronization. CAS/UC being valid alone is not sufficient.
        self.bootstrap_portal()
        url = self.upstream_url(self.TODAY)
        debug_log("local.schedule.start", f"mode={self.mode} host={urlsplit(url).hostname} path={urlsplit(url).path}")
        response = self.session.get(url, params={"rq": now.strftime("%Y-%m-%d"), "lxdm": "student"}, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20)
        debug_log("local.schedule.response", f"status={response.status_code} content_type={response.headers.get('Content-Type', '')[:40]} bytes={len(response.content)}")
        response.raise_for_status()
        payload = response.json()
        items = payload.get("datas", []) if isinstance(payload, dict) else []
        courses: list[Course] = []
        for item in items:
            window = _time_range(item.get("time"), now)
            if window:
                courses.append(Course(item.get("bizName") or item.get("shortName") or "未命名课程", item.get("place") or "教室待定", *window))
        assignments: list[Assignment] = []
        try:
            assignments.extend(self.spoc_assignments(now))
        except Exception as exc:
            debug_log("local.spoc.error", f"type={type(exc).__name__} message={str(exc)[:140]}")
        try:
            assignments.extend(self.judge_assignments(now))
        except Exception as exc:
            debug_log("local.judge.error", f"type={type(exc).__name__} message={str(exc)[:140]}")
        assignments.sort(key=lambda item: item.due)
        return Snapshot(sorted(courses, key=lambda item: item.start), assignments, now)


def enable_high_dpi() -> None:
    """Keep the frameless Tk widget sharp on Windows display scaling."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass


@dataclass
class Course:
    name: str
    room: str
    start: datetime
    end: datetime


@dataclass
class Assignment:
    course: str
    title: str
    due: datetime
    source: str


@dataclass
class Snapshot:
    courses: list[Course]
    assignments: list[Assignment]
    synced_at: datetime


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    return None


def _time_range(value: str | None, day: datetime) -> tuple[datetime, datetime] | None:
    if not value:
        return None
    import re

    matches = re.findall(r"(\d{1,2}:\d{2})", value)
    if len(matches) < 2:
        return None
    start = datetime.strptime(f"{day:%Y-%m-%d} {matches[0]}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{day:%Y-%m-%d} {matches[1]}", "%Y-%m-%d %H:%M")
    return start, end


class UbaaClient:
    def __init__(self, endpoint: str, token: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.token = token

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> object:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "UBAA-Desktop/0.1"}
        if body:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.endpoint}/{path.lstrip('/')}", data=body, headers=headers, method=method)
        debug_log("relay.request", f"method={method} path=/{path.lstrip('/')}")
        with urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8")
            debug_log("relay.response", f"status={response.status} path=/{path.lstrip('/')} bytes={len(raw)}")
        return json.loads(raw)

    def login(self, username: str, password: str) -> str:
        client_id = str(uuid.uuid4())
        self.request("api/v1/auth/preload", "POST", {"clientId": client_id})
        data = self.request("api/v1/auth/login", "POST", {"username": username, "password": password, "clientId": client_id})
        token = data.get("accessToken")
        if not token:
            raise RuntimeError("登录响应中没有 accessToken，可能需要验证码或服务端拒绝了请求")
        return token

    def dashboard(self) -> Snapshot:
        today_response = self.request("api/v1/schedule/today")
        # UBAA versions have returned both TodayScheduleResponse ({datas: [...]})
        # and the unwrapped list; accept both without weakening validation.
        if isinstance(today_response, list):
            today = today_response
        elif isinstance(today_response, dict):
            today = today_response.get("datas", [])
        else:
            raise RuntimeError("今日课表响应格式无法识别")
        now = datetime.now()
        courses: list[Course] = []
        for item in today:
            window = _time_range(item.get("time"), now)
            if window:
                courses.append(Course(item.get("bizName") or item.get("shortName") or "未命名课程", item.get("place") or "教室待定", *window))

        assignments: list[Assignment] = []
        for path, source in (("api/v1/spoc/assignments", "SPOC"), ("api/v1/judge/assignments", "希冀")):
            try:
                data = self.request(path)
            except Exception:
                continue
            items = data.get("assignments", []) if isinstance(data, dict) else []
            for item in items:
                status = str(item.get("submissionStatus", "")).upper()
                due = _parse_time(item.get("dueTime"))
                if due and due >= now and status not in {"SUBMITTED", "DONE"}:
                    assignments.append(Assignment(item.get("courseName", "未知课程"), item.get("title", "未命名作业"), due, source))
        assignments.sort(key=lambda item: item.due)
        return Snapshot(sorted(courses, key=lambda item: item.start), assignments, now)


def demo_snapshot() -> Snapshot:
    now = datetime.now().replace(second=0, microsecond=0)
    base = now.replace(hour=8, minute=0)
    courses = [Course("软件工程", "主楼 302", base, base + timedelta(minutes=95)), Course("概率论", "知行楼 205", base + timedelta(hours=3), base + timedelta(hours=4, minutes=35))]
    assignments = [Assignment("软件工程", "需求分析报告", now + timedelta(days=1, hours=4), "SPOC"), Assignment("概率论", "第三章习题", now + timedelta(days=3), "希冀")]
    return Snapshot(courses, assignments, now)


def save_snapshot(snapshot: Snapshot) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"synced_at": snapshot.synced_at.isoformat(), "courses": [{**asdict(c), "start": c.start.isoformat(), "end": c.end.isoformat()} for c in snapshot.courses], "assignments": [{**asdict(a), "due": a.due.isoformat()} for a in snapshot.assignments]}
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_snapshot() -> Snapshot | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        courses = [Course(c["name"], c["room"], datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"])) for c in data["courses"]]
        assignments = [Assignment(a["course"], a["title"], datetime.fromisoformat(a["due"]), a["source"]) for a in data["assignments"]]
        return Snapshot(courses, assignments, datetime.fromisoformat(data["synced_at"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class Dashboard(Tk):
    TRANSPARENT = "#f7f8fc"
    BG = "#f7f8fc"
    INK = "#172033"
    MUTED = "#7d8799"
    BLUE = "#76a9ff"
    GREEN = "#75d6b1"
    CARD = "#ffffff"

    def __init__(self):
        super().__init__()
        self.title("BUAA Desktop Widget")
        saved_x, saved_y = load_window_position()
        self.geometry(f"340x500{saved_x:+d}{saved_y:+d}")
        self.overrideredirect(True)
        self.resizable(False, False)
        # Behave like a desktop widget: stay on the desktop instead of
        # covering other applications when the user switches windows.
        self.wm_attributes("-topmost", False)
        self.wm_attributes("-alpha", 0.98)
        try:
            self.wm_attributes("-transparentcolor", self.TRANSPARENT)
        except Exception:
            pass
        self.configure(bg=self.TRANSPARENT)
        self._drag_origin = None
        self._header_overlay: Toplevel | None = None
        self._header_overlay_hwnd: int | None = None
        self._header_overlay_signature: bytes | None = None
        self._header_overlay_visible = False
        self._header_font_cache: dict[tuple, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._render_after_id: str | None = None
        saved_endpoint = DEFAULT_ENDPOINT
        saved_mode = "SERVER_RELAY"
        try:
            saved_config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            saved_endpoint = saved_config.get("endpoint") or DEFAULT_ENDPOINT
            saved_mode = saved_config.get("mode") or "SERVER_RELAY"
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        self.endpoint = StringVar(value=saved_endpoint)
        self.mode = StringVar(value=saved_mode)
        self.transparent_background = BooleanVar(value=load_transparent_background())
        self.status = StringVar(value="演示数据 · 可在设置中连接 UBAA")
        self.current_var = StringVar()
        self.next_var = StringVar()
        self.snapshot = load_snapshot() or demo_snapshot()
        self._build()
        self.render()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        if CONFIG_FILE.exists():
            self.after(300, self.refresh)

    def _build(self):
        self.canvas = Canvas(self, width=340, height=500, bg=self.TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._finish_drag)
        self.canvas.bind("<Button-3>", self._context_menu)
        self.canvas.bind("<Double-Button-1>", lambda _event: self.settings())
        self.canvas.bind("<Button-1>", self._handle_click, add="+")

    def _rounded(self, x1, y1, x2, y2, radius, fill, outline=""):
        # Render only the card shape at 4x resolution, then downsample. Header
        # text remains native Tk so its accepted sizing/rendering is untouched.
        scale = 4
        width, height = int(x2 - x1), int(y2 - y1)
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (0, 0, width * scale - 1, height * scale - 1),
            radius=radius * scale,
            fill=fill,
            outline=outline or None,
            width=scale if outline else 1,
        )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self._image_refs.append(photo)
        self.canvas.create_image(x1, y1, image=photo, anchor="nw")

    def _matched_header_font(
        self,
        names: tuple[str, ...],
        text: str,
        family: str,
        point_size: int,
        weight: str = "normal",
        slant: str = "roman",
        supersample: int = 4,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        key = (names, text, family, point_size, weight, slant, supersample)
        cached = self._header_font_cache.get(key)
        if cached is not None:
            return cached
        target = tkfont.Font(root=self, family=family, size=point_size, weight=weight, slant=slant)
        target_width = target.measure(text)
        target_height = target.metrics("linespace")
        fonts_dir = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        path = next((fonts_dir / name for name in names if (fonts_dir / name).exists()), None)
        if path is None:
            result = ImageFont.load_default(size=max(1, target_height * supersample))
            self._header_font_cache[key] = result
            return result
        best: ImageFont.FreeTypeFont | None = None
        best_score = float("inf")
        for pixel_size in range(max(4, target_height // 2), target_height * 2 + 1):
            candidate = ImageFont.truetype(str(path), pixel_size * supersample)
            box = candidate.getbbox(text)
            width = (box[2] - box[0]) / supersample
            height = (box[3] - box[1]) / supersample
            score = abs(width - target_width) + abs(height - target_height) * 0.35
            if score < best_score:
                best, best_score = candidate, score
        result = best or ImageFont.truetype(str(path), max(1, target_height * supersample))
        self._header_font_cache[key] = result
        return result

    def _build_layered_header_image(self, now: datetime) -> Image.Image:
        scale = 4
        image = Image.new("RGBA", (340 * scale, 82 * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        brand = self._matched_header_font(
            ("seguisbi.ttf", "segoeuiz.ttf", "arialbi.ttf"),
            "BUAA",
            "Segoe UI",
            21,
            "bold",
            "italic",
            scale,
        )
        date_text = f"{now:%m.%d}"
        date_font = self._matched_header_font(
            ("seguisb.ttf", "segoeuib.ttf"), date_text, "Segoe UI Semibold", 15, supersample=scale
        )
        today_font = self._matched_header_font(
            ("segoeuib.ttf", "arialbd.ttf"), "TODAY", "Segoe UI", 7, "bold", supersample=scale
        )
        year_text = f"{now:%Y}  ·  {weekdays[now.weekday()]}"
        year_font = self._matched_header_font(
            ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"), year_text, "Microsoft YaHei UI", 7, supersample=scale
        )
        draw.text((24 * scale, 29 * scale), "BUAA", font=brand, fill=self.BLUE, anchor="lm")
        draw.line((24 * scale, 70 * scale, 78 * scale, 70 * scale), fill=self.BLUE, width=2 * scale)
        draw.text((88 * scale, 70 * scale), "TODAY", font=today_font, fill=self.MUTED, anchor="lm")
        draw.text((316 * scale, 28 * scale), date_text, font=date_font, fill=self.BLUE, anchor="rm")
        draw.text((316 * scale, 52 * scale), year_text, font=year_font, fill=self.INK, anchor="rm")
        return image.resize((340, 82), Image.Resampling.LANCZOS)

    @staticmethod
    def _root_hwnd(widget) -> int:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        return int(user32.GetAncestor(wintypes.HWND(widget.winfo_id()), 2))

    def _ensure_header_overlay(self) -> int:
        if self._header_overlay is None or not self._header_overlay.winfo_exists():
            overlay = Toplevel(self)
            overlay.overrideredirect(True)
            overlay.transient(self)
            # Map once off-screen so Tk creates its final wrapper HWND before
            # layered styles are applied; otherwise Tk replaces that HWND on
            # first show and silently discards the styles.
            overlay.geometry("340x82+-10000+-10000")
            overlay.update()
            hwnd = self._root_hwnd(overlay)
            user32 = ctypes.windll.user32
            get_style = user32.GetWindowLongPtrW
            set_style = user32.SetWindowLongPtrW
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            get_style.restype = ctypes.c_ssize_t
            set_style.restype = ctypes.c_ssize_t
            ex_style = get_style(hwnd, -20)
            set_style(hwnd, -20, ex_style | 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080)
            self._header_overlay = overlay
            self._header_overlay_hwnd = hwnd
        return int(self._header_overlay_hwnd)

    def _show_layered_header(self, image: Image.Image) -> None:
        hwnd = self._ensure_header_overlay()
        signature = image.tobytes()
        if self._header_overlay_visible and signature == self._header_overlay_signature:
            self._position_header_overlay()
            return
        width, height = image.size
        rgba = image.convert("RGBA")
        raw = bytearray(rgba.tobytes("raw", "BGRA"))
        for index in range(0, len(raw), 4):
            alpha = raw[index + 3]
            raw[index] = raw[index] * alpha // 255
            raw[index + 1] = raw[index + 1] * alpha // 255
            raw[index + 2] = raw[index + 2] * alpha // 255

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(_BitmapInfo),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(_Point),
            ctypes.POINTER(_Size),
            wintypes.HDC,
            ctypes.POINTER(_Point),
            wintypes.DWORD,
            ctypes.POINTER(_BlendFunction),
            wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        info = _BitmapInfo()
        info.bmiHeader = _BitmapInfoHeader(
            ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0
        )
        bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        try:
            ctypes.memmove(bits, bytes(raw), len(raw))
            destination = _Point(self.winfo_rootx(), self.winfo_rooty())
            source = _Point(0, 0)
            size = _Size(width, height)
            blend = _BlendFunction(0, 0, 255, 1)
            ok = user32.UpdateLayeredWindow(
                hwnd,
                screen_dc,
                ctypes.byref(destination),
                ctypes.byref(size),
                memory_dc,
                ctypes.byref(source),
                0,
                ctypes.byref(blend),
                2,
            )
            if not ok:
                raise ctypes.WinError()
            if not self._header_overlay_visible:
                user32.ShowWindow(hwnd, 4)
            self._header_overlay_signature = signature
            self._header_overlay_visible = True
        finally:
            gdi32.SelectObject(memory_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)

    def _position_header_overlay(self) -> None:
        if self._header_overlay_hwnd:
            ctypes.windll.user32.SetWindowPos(
                self._header_overlay_hwnd,
                0,
                self.winfo_rootx(),
                self.winfo_rooty(),
                0,
                0,
                0x0001 | 0x0004 | 0x0010,
            )

    def _hide_header_overlay(self) -> None:
        if self._header_overlay_hwnd and self._header_overlay_visible:
            ctypes.windll.user32.ShowWindow(self._header_overlay_hwnd, 0)
            self._header_overlay_visible = False

    def _scheduled_render(self) -> None:
        self._render_after_id = None
        self.render()

    def _schedule_render(self) -> None:
        # render() is also called after sync and settings changes. Keep only
        # one minute timer alive so these calls cannot accumulate redraw loops.
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except TclError:
                pass
        self._render_after_id = self.after(60_000, self._scheduled_render)

    def _start_drag(self, event):
        self._drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag(self, event):
        if self._drag_origin:
            x = event.x_root - self._drag_origin[0]
            y = event.y_root - self._drag_origin[1]
            self.geometry(f"+{x}+{y}")
            self.update_idletasks()
            self._position_header_overlay()

    def _finish_drag(self, _event):
        if self._drag_origin:
            save_window_position(self.winfo_x(), self.winfo_y())
            self._drag_origin = None

    def close_app(self):
        save_window_position(self.winfo_x(), self.winfo_y())
        if self._render_after_id is not None:
            try:
                self.after_cancel(self._render_after_id)
            except TclError:
                pass
            self._render_after_id = None
        if self._header_overlay is not None:
            self._header_overlay.destroy()
        self.destroy()

    def _handle_click(self, event):
        if self.transparent_background.get():
            return
        if event.y < 58 and event.x > 304:
            self.close_app()
        elif event.y < 58 and event.x > 270:
            self.refresh()

    def _context_menu(self, event):
        menu = Menu(self, tearoff=False)
        menu.add_command(label="刷新数据", command=self.refresh)
        menu.add_command(label="设置 / 登录", command=self.settings)
        menu.add_checkbutton(
            label="透明背景",
            variable=self.transparent_background,
            command=self.apply_background_preference,
        )
        menu.add_separator()
        menu.add_command(label="退出 UBAA Desktop", command=self.close_app)
        menu.tk_popup(event.x_root, event.y_root)

    def apply_background_preference(self):
        save_transparent_background(self.transparent_background.get())
        self.render()

    def render(self):
        now = datetime.now()
        active = next((c for c in self.snapshot.courses if c.start <= now < c.end), None)
        upcoming = next((c for c in self.snapshot.courses if c.start > now), None)
        self.canvas.delete("all")
        self._image_refs = []
        transparent = self.transparent_background.get()
        if not transparent:
            self._rounded(8, 8, 332, 492, 22, self.CARD)
        if transparent:
            self.update_idletasks()
            self._show_layered_header(self._build_layered_header_image(now))
        else:
            self._hide_header_overlay()
            self.canvas.create_text(24, 28, text="BUAA", anchor="w", fill=self.BLUE, font=("Segoe UI", 17, "bold italic"))
            self.canvas.create_text(286, 28, text="↻", fill=self.BLUE, font=("Segoe UI Symbol", 17, "bold"), tags="refresh")
            self.canvas.create_text(316, 28, text="×", fill=self.MUTED, font=("Segoe UI", 17), tags="close")
            self.canvas.create_text(24, 70, text=self.status.get(), anchor="w", fill=self.MUTED, font=("Microsoft YaHei UI", 8))
        self._course_card(18, 88, 322, 175, "当前课程", active, now, "当前无课", False, self.BLUE)
        self._course_card(18, 188, 322, 275, "下一课程", upcoming, now, "今日无后续课程", True, self.GREEN)
        if transparent:
            self._rounded(18, 288, 322, 476, 16, "#fbfaff")
            self.canvas.create_rectangle(18, 304, 22, 460, fill=self.BLUE, outline="")
        heading_x = 36 if transparent else 18
        heading_y = 310 if transparent else 300
        self.canvas.create_text(heading_x, heading_y, text="待完成作业", anchor="w", fill=self.INK, font=("Microsoft YaHei UI", 12, "bold"))
        self.canvas.create_text(304 if transparent else 322, heading_y + 2, text="按 DDL", anchor="e", fill=self.MUTED, font=("Microsoft YaHei UI", 8))
        assignments = self.snapshot.assignments[:4]
        if not assignments:
            self.canvas.create_text(36 if transparent else 24, 350 if transparent else 342, text="暂无待完成作业", anchor="w", fill=self.MUTED, font=("Microsoft YaHei UI", 10))
        for index, item in enumerate(assignments):
            y = (336 + index * 34) if transparent else (326 + index * 36)
            dot_x = 34 if transparent else 22
            text_x = 50 if transparent else 40
            self.canvas.create_oval(dot_x, y + 5, dot_x + 8, y + 13, fill=self.BLUE if index == 0 else "#d8deeb", outline="")
            self.canvas.create_text(text_x, y, text=item.title, anchor="w", fill=self.INK, font=("Microsoft YaHei UI", 9, "bold"))
            self.canvas.create_text(text_x, y + 16, text=f"{item.course} · {item.due:%m-%d %H:%M}", anchor="w", fill=self.MUTED, font=("Microsoft YaHei UI", 7))
        if not transparent:
            self.canvas.create_text(18, 474, text="双击设置 · 右键菜单 · 拖动移动", anchor="w", fill="#a7afbd", font=("Microsoft YaHei UI", 7))
        self._schedule_render()

    def _course_card(self, x1, y1, x2, y2, label, course, now, empty, upcoming, color):
        self._rounded(x1, y1, x2, y2, 16, "#f8fbff" if not upcoming else "#f7fffb")
        self.canvas.create_rectangle(x1, y1 + 16, x1 + 4, y2 - 16, fill=color, outline="")
        self.canvas.create_text(x1 + 18, y1 + 20, text=label.upper(), anchor="w", fill=color, font=("Segoe UI", 8, "bold"))
        if not course:
            self.canvas.create_text(x1 + 18, y1 + 55, text=empty, anchor="w", fill=self.MUTED, font=("Microsoft YaHei UI", 13))
            return
        delta = course.start - now if upcoming else course.end - now
        mins = max(0, int(delta.total_seconds() // 60))
        self.canvas.create_text(x1 + 18, y1 + 48, text=course.name, anchor="w", fill=self.INK, font=("Microsoft YaHei UI", 10, "bold"))
        self.canvas.create_text(x1 + 18, y1 + 70, text=f"{course.room}  ·  {'距开课' if upcoming else '剩余'} {mins // 60}小时{mins % 60}分钟", anchor="w", fill=self.MUTED, font=("Microsoft YaHei UI", 8))

    @staticmethod
    def course_text(course: Course | None, now: datetime, empty: str, upcoming: bool = False) -> str:
        if not course:
            return empty
        delta = course.start - now if upcoming else course.end - now
        mins = max(0, int(delta.total_seconds() // 60))
        return f"{course.name}\n{course.room} · {'距开课' if upcoming else '剩余'} {mins // 60}小时{mins % 60}分钟"

    def refresh(self):
        self.status.set("正在同步 UBAA…")
        def work():
            try:
                config = self.load_config()
                mode = config.get("mode", "SERVER_RELAY")
                debug_log("sync.start", f"mode={mode}")
                if mode == "SERVER_RELAY":
                    token = config.get("access_token", "")
                    if not token:
                        raise RuntimeError("尚未登录，当前显示演示/缓存数据")
                    snapshot = UbaaClient(self.endpoint.get(), token).dashboard()
                else:
                    local_client = LocalCampusClient(mode, config.get("cookies"))
                    snapshot = local_client.dashboard()
                    # Keep any service cookies refreshed by the BYXT hand-off.
                    config["cookies"] = local_client.cookie_payload()
                    APP_DIR.mkdir(parents=True, exist_ok=True)
                    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                save_snapshot(snapshot)
                debug_log("sync.success", f"mode={mode} courses={len(snapshot.courses)} assignments={len(snapshot.assignments)}")
                self.after(0, lambda: self.updated(snapshot))
            except Exception as exc:
                debug_log("sync.error", f"type={type(exc).__name__} message={str(exc)[:180]}")
                self.after(0, lambda: self.status.set(f"同步失败，继续显示已有数据：{self.describe_error(exc)}"))
        threading.Thread(target=work, daemon=True).start()

    def updated(self, snapshot):
        self.snapshot = snapshot
        self.status.set(f"已同步 · {snapshot.synced_at:%Y-%m-%d %H:%M}")
        self.render()

    @staticmethod
    def describe_error(exc: Exception) -> str:
        if isinstance(exc, requests.HTTPError):
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                return "本地会话已失效，请重新登录"
            return f"HTTP {status}" if status is not None else "HTTP 请求失败"
        if isinstance(exc, HTTPError):
            if exc.code == 401:
                return "本地会话已失效，请重新登录"
            return f"HTTP {exc.code}"
        if isinstance(exc, URLError):
            return "网络不可用"
        if isinstance(exc, (AttributeError, RuntimeError, ValueError)):
            return str(exc)[:80]
        return type(exc).__name__

    def load_token(self) -> str:
        return self.load_config().get("access_token", "")

    def load_config(self) -> dict:
        try:
            value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def settings(self):
        win = Toplevel(self)
        win.title("BUAA 登录")
        win.geometry("520x620")
        win.minsize(520, 620)
        win.transient(self)
        win.resizable(False, False)
        win.lift()
        win.focus_force()
        Label(win, text="连接模式", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=26, pady=(24, 6))
        mode_value = StringVar(value=next((label for label, value in LOGIN_MODES.items() if value == self.mode.get()), "服务器中转"))
        mode_menu = OptionMenu(win, mode_value, *LOGIN_MODES.keys())
        mode_menu.config(anchor="w", width=18)
        mode_menu.pack(anchor="w", padx=26)
        Label(win, text="三种模式均支持课表和待完成作业；直连/WebVPN 会分别建立教务、SPOC 与希冀会话。", fg=self.MUTED if hasattr(self, "MUTED") else "#6b7280", wraplength=468, justify=LEFT).pack(anchor="w", padx=26, pady=(6, 14))
        appearance = Frame(win, bg="#eef4ff", highlightthickness=1, highlightbackground="#d6e4fb")
        appearance.pack(fill=X, padx=26, pady=(0, 14), ipady=5)
        Label(appearance, text="外观", bg="#eef4ff", fg="#334155", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(5, 0))
        Checkbutton(
            appearance,
            text="透明背景（隐藏小工具外层底板）",
            variable=self.transparent_background,
            command=self.apply_background_preference,
            bg="#eef4ff",
            activebackground="#eef4ff",
            anchor="w",
        ).pack(anchor="w", padx=8, pady=(1, 4))
        Label(win, text="UBAA 服务地址").pack(anchor="w", padx=26, pady=(4, 3)); endpoint = Entry(win); endpoint.insert(0, self.endpoint.get()); endpoint.pack(fill=X, padx=26, ipady=3)
        Label(win, text="学号（凭据使用 Windows DPAPI 加密保存）").pack(anchor="w", padx=26, pady=(14, 3)); user = Entry(win); user.pack(fill=X, padx=26, ipady=3)
        Label(win, text="密码").pack(anchor="w", padx=26, pady=(12, 3)); password = Entry(win, show="*"); password.pack(fill=X, padx=26, ipady=3)
        saved_user, saved_password = load_credentials()
        user.insert(0, saved_user)
        password.insert(0, saved_password)
        def do_login():
            selected_mode = LOGIN_MODES[mode_value.get()]
            try:
                if selected_mode == "SERVER_RELAY":
                    client = UbaaClient(endpoint.get().strip()); token = client.login(user.get().strip(), password.get())
                    config = {**self.load_config(), "endpoint": endpoint.get().strip(), "mode": selected_mode, "access_token": token}
                    config.pop("cookies", None)
                else:
                    local_client = LocalCampusClient(selected_mode)
                    cookies = local_client.login(user.get().strip(), password.get())
                    config = {**self.load_config(), "endpoint": endpoint.get().strip(), "mode": selected_mode, "cookies": cookies}
                    config.pop("access_token", None)
                save_credentials(user.get().strip(), password.get())
                APP_DIR.mkdir(parents=True, exist_ok=True); CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                self.mode.set(selected_mode)
                self.endpoint.set(endpoint.get().strip()); password.delete(0, END); win.destroy(); self.status.set(f"{mode_value.get()}登录成功，正在同步…"); self.refresh()
            except Exception as exc:
                messagebox.showerror("登录失败", str(exc), parent=win)
        buttons = Frame(win, bg="#f7f8fc")
        # Keep the action row well below the password field even on scaled displays.
        buttons.place(x=26, y=540, width=468, height=62)
        Button(buttons, text="登录并同步", command=do_login, width=16, height=2, bg="#4f86e8", fg="white", activebackground="#3d72cf", activeforeground="white", relief="flat", cursor="hand2").pack(side=LEFT, fill=Y)
        Button(buttons, text="清除登录信息", command=self.logout, width=16, height=2, bg="#e9eef7", fg="#334155", activebackground="#dbe4f3", relief="flat", cursor="hand2").pack(side=RIGHT, fill=Y)

    def logout(self):
        try: CONFIG_FILE.unlink()
        except FileNotFoundError: pass
        try: CREDENTIAL_FILE.unlink()
        except FileNotFoundError: pass
        self.status.set("已清除本地令牌 · 当前显示缓存/演示数据")


if __name__ == "__main__":
    enable_high_dpi()
    Dashboard().mainloop()
