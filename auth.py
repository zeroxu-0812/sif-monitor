import argparse
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

CACHE_FILE = Path(os.getenv("SIF_COOKIE_CACHE_FILE", PROJECT_DIR / ".sif_cookie_cache"))
TOKEN_CACHE_FILE = Path(os.getenv("SIF_TOKEN_CACHE_FILE", PROJECT_DIR / ".sif_token_cache"))
BASE_URL = os.getenv("SIF_BASE_URL", "https://www.sif.com").rstrip("/")
DEFAULT_COUNTRY = os.getenv("SIF_COUNTRY", "US")
LOGIN_PATH = os.getenv("SIF_LOGIN_PATH", "/api/user/login")
LOGIN_URL = os.getenv("SIF_LOGIN_URL", "").strip()
LOGIN_TIMEOUT = int(os.getenv("SIF_LOGIN_TIMEOUT", "15"))
CACHE_TTL_SECONDS = int(os.getenv("SIF_CACHE_TTL_SECONDS", "21600"))
AUTH_COOKIE_NAME = os.getenv("SIF_AUTH_COOKIE_NAME", "sif_token")
LOGIN_USERNAME_FIELD = os.getenv("SIF_LOGIN_USERNAME_FIELD", "phone").strip() or "phone"
LOGIN_REFERER = os.getenv("SIF_LOGIN_REFERER", f"{BASE_URL}/?country={DEFAULT_COUNTRY}")
USER_AGENT = os.getenv(
    "SIF_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36",
)
PLACEHOLDER_MARKERS = ("replace_with", "your_", "你的")


class SIFAuthError(RuntimeError):
    """SIF 登录失败。"""


def _is_placeholder_value(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value and not _is_placeholder_value(value):
            return value
    return None


def _parse_cookie_header(cookie_header: str) -> dict:
    cookies = {}
    for item in cookie_header.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def _resolve_env_token() -> str | None:
    return _env_value("SIF_AUTH_TOKEN", "AUTHORIZATION", "SIF_TOKEN")


def _resolve_credentials() -> tuple[str, str]:
    username = _env_value("SIF_USERNAME", "SIF_PHONE", "SIF_ACCOUNT", "PHONE") or ""
    password = _env_value("SIF_PASSWORD", "PASSWORD") or ""
    return username, password


def _extract_token(data: object) -> str | None:
    if isinstance(data, dict):
        token_keys = (
            "token",
            "access_token",
            "accessToken",
            "authorization",
            "Authorization",
            "authToken",
            "jwt",
            "sessionToken",
        )
        for key in token_keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            nested = _extract_token(value)
            if nested:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _extract_token(item)
            if nested:
                return nested
    return None


def _build_login_payload(username: str, password: str) -> dict:
    payload = {
        LOGIN_USERNAME_FIELD: username,
        "password": password,
    }
    extra_raw = os.getenv("SIF_LOGIN_EXTRA_JSON", "").strip()
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except json.JSONDecodeError as exc:
            raise SIFAuthError(f"SIF_LOGIN_EXTRA_JSON 不是合法 JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise SIFAuthError("SIF_LOGIN_EXTRA_JSON 必须是 JSON object")
        payload.update(extra)
    return payload


def _is_placeholder_login_url(login_url: str) -> bool:
    return not login_url or "your_login_endpoint" in login_url


def _build_url(path_or_url: str) -> str:
    parsed = urlparse(path_or_url)
    if parsed.scheme and parsed.netloc:
        return path_or_url
    return urljoin(f"{BASE_URL}/", path_or_url.lstrip("/"))


def build_sif_query_params(
    country: str | None = None,
    extra_params: dict | None = None,
    include_country: bool = True,
) -> dict:
    """生成 SIF 接口请求参数：`_t`（毫秒时间戳）+ `_m`（`Sif_<uuid>-<timestamp>` marker）。
    每次调用生成新的 marker，可安全用于任意批次的请求。"""
    timestamp_ms = int(time.time() * 1000)
    marker = f"Sif_{uuid.uuid4()}-{timestamp_ms}"
    params = {"_t": timestamp_ms, "_m": marker}
    if include_country:
        params["country"] = country or DEFAULT_COUNTRY
    if extra_params:
        params.update(extra_params)
    return params


def _resolve_login_target() -> tuple[str, dict | None]:
    if not _is_placeholder_login_url(LOGIN_URL):
        return LOGIN_URL, None
    return _build_url(LOGIN_PATH), build_sif_query_params(include_country=False)


def _base_headers(referer: str | None = None) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": BASE_URL,
        "referer": referer or f"{BASE_URL}/",
        "user-agent": USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _safe_json(resp: requests.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return {}


def _apply_env_cookies(session: requests.Session) -> None:
    env_cookie = os.getenv("SIF_COOKIE", "").strip()
    if env_cookie:
        session.cookies.update(_parse_cookie_header(env_cookie))


def _cookie_domain() -> str:
    parsed = urlparse(BASE_URL)
    return parsed.hostname or "www.sif.com"


def _login() -> tuple[requests.Session, str | None]:
    """
    SIF 登录：
    1) 优先使用账号密码登录，默认接口为 /api/user/login，自动附加 _t/_m
    2) 若未配置账号密码，回退到环境变量 token/cookie
    """
    session = requests.Session()
    _apply_env_cookies(session)

    username, password = _resolve_credentials()
    if username and password:
        login_url, login_params = _resolve_login_target()
        resp = session.post(
            login_url,
            params=login_params,
            json=_build_login_payload(username=username, password=password),
            headers=_base_headers(LOGIN_REFERER),
            timeout=LOGIN_TIMEOUT,
        )
        resp.raise_for_status()

        data = _safe_json(resp)
        token = _extract_token(data) or session.cookies.get(AUTH_COOKIE_NAME)
        if token and not session.cookies.get(AUTH_COOKIE_NAME):
            session.cookies.set(
                AUTH_COOKIE_NAME,
                token,
                domain=_cookie_domain(),
                path="/",
            )
        if not token and not session.cookies.get_dict():
            message = ""
            if isinstance(data, dict):
                message = data.get("msg") or data.get("message") or ""
            detail = f": {message}" if message else ""
            raise SIFAuthError(f"SIF 登录未返回 token/cookie{detail}")
        return session, token

    env_token = _resolve_env_token()
    if env_token and not session.cookies.get(AUTH_COOKIE_NAME):
        session.cookies.set(
            AUTH_COOKIE_NAME,
            env_token,
            domain=_cookie_domain(),
            path="/",
        )

    return session, env_token or session.cookies.get(AUTH_COOKIE_NAME)


def _load_cookie_cache() -> dict | None:
    """读取缓存的 cookies"""
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cookies = cache["session_cookies"]
        if cookies and cache["expires_at"] > time.time() + 60:
            return cookies
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def _save_cookie_cache(session_cookies: dict, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
    """保存 cookies 到缓存"""
    if not session_cookies:
        return
    CACHE_FILE.write_text(
        json.dumps(
            {
                "session_cookies": session_cookies,
                "expires_at": time.time() + ttl_seconds,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def _load_token_cache() -> str | None:
    """读取缓存的 Bearer Token"""
    try:
        cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        token = cache["token"]
        if (
            token
            and not _is_placeholder_value(token)
            and cache["expires_at"] > time.time() + 60
        ):
            return token
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def _save_token_cache(token: str, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
    """保存 Bearer Token 到缓存"""
    if not token:
        return
    TOKEN_CACHE_FILE.write_text(
        json.dumps(
            {
                "token": token,
                "expires_at": time.time() + ttl_seconds,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        TOKEN_CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def _resolve_cached_or_env_token() -> str | None:
    token = _load_token_cache() or _resolve_env_token()
    if token:
        return token
    cookies = _load_cookie_cache() or {}
    return cookies.get(AUTH_COOKIE_NAME)


def _login_and_cache() -> tuple[requests.Session, str | None]:
    session, token = _login()
    _save_cookie_cache(session.cookies.get_dict())
    if token:
        _save_token_cache(token)
    return session, token


def _refresh_session_auth(session: requests.Session) -> None:
    refreshed, _ = _login_and_cache()
    session.cookies.clear()
    session.cookies.update(refreshed.cookies.get_dict())


def _needs_auth_retry(resp: requests.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    try:
        data = resp.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("code") == -7


def get_session(force_login: bool = False) -> requests.Session:
    """获取有效 Session（优先复用缓存的 Cookie）"""
    session = requests.Session()
    if not force_login:
        cookies = _load_cookie_cache()
        if cookies:
            session.cookies.update(cookies)
            return session

    session, _ = _login_and_cache()
    return session


def get_token(force_login: bool = False) -> str:
    """获取 SIF authorization token"""
    token = None if force_login else _resolve_cached_or_env_token()
    if not token:
        _, token = _login_and_cache()
    return token or ""


def get_headers() -> dict:
    """返回携带 SIF token 的请求头"""
    token = get_token()
    if not token:
        return {}
    return {"authorization": token}


def get_default_headers() -> dict:
    """返回调用 SIF API 的默认 headers（cookie 由 session 自动处理）"""
    headers = _base_headers(f"{BASE_URL}/snapshot?country={DEFAULT_COUNTRY}&keyword=")
    token = _resolve_cached_or_env_token()
    if token:
        headers["authorization"] = token
    return headers


def api_get(session: requests.Session, path: str, params: dict = None) -> requests.Response:
    """GET 请求 SIF API，自动加上默认 headers"""
    url = _build_url(path)
    merged_params = build_sif_query_params(extra_params=params or {})
    resp = session.get(url, headers=get_default_headers(), params=merged_params, timeout=10)
    if _needs_auth_retry(resp):
        _refresh_session_auth(session)
        merged_params = build_sif_query_params(extra_params=params or {})
        resp = session.get(url, headers=get_default_headers(), params=merged_params, timeout=10)
    return resp


def api_post(
    session: requests.Session,
    path: str,
    json_data: dict = None,
    params: dict = None,
    timeout: int = 10,
) -> requests.Response:
    """POST 请求 SIF API，自动加上默认 headers"""
    url = _build_url(path)
    merged_params = build_sif_query_params(extra_params=params or {})
    resp = session.post(
        url,
        headers=get_default_headers(),
        params=merged_params,
        json=json_data,
        timeout=timeout,
    )
    if _needs_auth_retry(resp):
        _refresh_session_auth(session)
        merged_params = build_sif_query_params(extra_params=params or {})
        resp = session.post(
            url,
            headers=get_default_headers(),
            params=merged_params,
            json=json_data,
            timeout=timeout,
        )
    return resp


def _redact(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SIF 登录/session 检查工具")
    parser.add_argument(
        "--check",
        action="store_true",
        help="执行一次登录或读取缓存，并只输出脱敏后的状态信息",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="忽略 cookie/token 缓存，强制重新登录",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.check:
        print("用法: python auth.py --check [--force-login]")
        return 0

    if args.force_login:
        session, token = _login_and_cache()
    else:
        session = get_session()
        token = _resolve_cached_or_env_token()
    cookie_names = sorted(session.cookies.get_dict().keys())
    login_url, login_params = _resolve_login_target()

    print(f"[INFO] base_url: {BASE_URL}")
    print(f"[INFO] login_url: {login_url}")
    print(f"[INFO] login_params: {sorted((login_params or {}).keys())}")
    print(f"[INFO] cookie_names: {cookie_names}")
    print(f"[INFO] token: {_redact(token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
