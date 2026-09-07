"""
CRM OAuth 登录（RSA 加密 + 多租户）

从公司现有自动化测试方案迁移适配。
流程：
1. 获取初始登录页（跟随重定向拿 session cookie）
2. 获取 RSA 公钥
3. RSA 加密密码
4. POST 登录，拿到租户列表
5. 选择目标租户，获取 authorization code
6. 用 code 换取最终 cookie

依赖：
    pip install requests pycryptodome pyyaml
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import warnings

import requests
import yaml
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

# 忽略 SSL 警告（代理环境）
warnings.filterwarnings("ignore", message="Unverified HTTPS request")


CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "credentials.yaml")
COOKIE_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cookie_cache.json")


class CRMAuth:
    """CRM OAuth 登录，返回 cookie 字符串"""

    def __init__(self):
        self._cookie_jar: dict[str, str] = {}

    def login(self, base_url: str, login_url: str, username: str, password: str, tenant: str) -> str:
        """
        执行完整的 CRM OAuth 登录流程。

        返回: cookie 字符串
        """
        self._cookie_jar = {}

        try:
            # Step 1: 获取初始登录页
            r1 = self._fetch(f"{base_url}/global/login.action", "GET")
            location1 = r1.headers.get("Location")
            if not location1:
                raise Exception("Step 1: 无 Location header")

            # Step 2: 跟随第一次重定向
            r2 = self._fetch(location1, "GET")
            location2 = r2.headers.get("Location")
            if not location2:
                raise Exception("Step 2: 无 Location header")

            # Step 3: 跟随第二次重定向
            r3 = self._fetch(location2, "GET")
            r3_cookie = r3.headers.get("Set-Cookie", "")

            # Step 4: 获取 RSA 公钥
            rsa_url = f"{login_url}/auc/passport/password-key?password={urllib.parse.quote(password)}"
            r4 = self._fetch(rsa_url, "GET", extra_headers={"Cookie": r3_cookie})
            rsa_data = r4.json()
            if not rsa_data.get("result", {}).get("key"):
                raise Exception("Step 4: 无法获取 RSA key")

            rsa_key = rsa_data["result"]["key"]
            encrypted_password = self._rsa_encrypt(password, rsa_key)

            # Step 5: 登录
            r5 = self._fetch(f"{login_url}/auc/login", "POST",
                             extra_headers={"Cookie": r3_cookie},
                             data={
                                 "login_name": username,
                                 "login_type": "web",
                                 "password": encrypted_password,
                             })
            login_data = r5.json()
            tenant_list = login_data.get("result", {}).get("tenant_list", [])
            if not tenant_list:
                raise Exception("Step 5: 无租户列表")

            # Step 6: 匹配目标租户
            target = next((t for t in tenant_list if t["company"] == tenant), None)
            if not target:
                available = [t["company"] for t in tenant_list]
                raise Exception(f"Step 6: 未找到租户 '{tenant}'，可用: {available}")

            # Step 7: 获取 authorization code
            r7 = self._fetch(f"{login_url}/auc/oauth2/authorize-code", "POST",
                             extra_headers={
                                 "Content-Type": "application/json",
                                 "Cookie": r3_cookie,
                             },
                             json_data={
                                 "user_type": 0,
                                 "encryption_key": target["encryptionKey"],
                                 "tenant_id": target["id"],
                                 "passport_id": login_data["result"]["passport_id"],
                                 "login_type": "web",
                             })
            auth_data = r7.json()
            redirect_url = auth_data.get("result", {}).get("redirectUrl", "")
            if not redirect_url:
                raise Exception("Step 7: 无 redirectUrl")

            # 提取 code
            parsed = urllib.parse.urlparse(redirect_url)
            code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0]
            if not code:
                raise Exception("Step 7: 无 authorization code")

            # Step 8: 换取 token/cookie
            token_url = redirect_url if redirect_url else f"{base_url}/neologin/skip/v2/auc/oauth2/token/info?&callback=__jp0&code={code}"
            r8 = self._fetch(token_url, "GET", extra_headers={"Cookie": r3_cookie})

            # 跟随重定向链直到拿到 passport cookie
            max_redirects = 5
            for _ in range(max_redirects):
                location = r8.headers.get("Location", "")
                if not location:
                    break
                if "x-ienterprise-passport" in str(r8.headers.get("Set-Cookie", "")):
                    break
                if "x-ienterprise-passport" in str(self._cookie_jar):
                    break
                r8 = self._fetch(location, "GET")

            # 返回完整 cookie
            full_cookie = self._build_full_cookie_str(r8.headers)
            if full_cookie:
                return full_cookie

            # fallback: passport + tenant
            fallback = self._parse_result_cookies(r8.headers)
            if fallback:
                print("[CRMAuth] warn: 完整 cookie 为空，fallback 到 passport+tenant")
                return fallback

            raise Exception("登录成功但无法获取任何 cookie")

        except Exception as e:
            print(f"[CRMAuth] 登录失败: {e}")
            raise

    def _fetch(self, url: str, method: str, extra_headers: dict = None,
               data: dict = None, json_data: dict = None) -> requests.Response:
        """发送请求，自动管理 cookie"""
        headers = {}
        cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookie_jar.items())
        if cookie_str:
            headers["Cookie"] = cookie_str
        if extra_headers:
            headers.update(extra_headers)

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            data=data,
            json=json_data,
            allow_redirects=False,
            timeout=15,
            verify=False,
        )

        # 存储 cookie
        set_cookie = response.headers.get("Set-Cookie", "")
        if set_cookie:
            for part in set_cookie.split(","):
                kv = part.split(";")[0].strip().split("=", 1)
                if len(kv) == 2:
                    self._cookie_jar[kv[0].strip()] = kv[1].strip()

        return response

    @staticmethod
    def _rsa_encrypt(password: str, rsa_key: str) -> str:
        """RSA 加密密码"""
        key_pem = f"-----BEGIN PUBLIC KEY-----\n{rsa_key}\n-----END PUBLIC KEY-----"
        public_key = RSA.importKey(key_pem.encode("utf-8"))
        cipher = PKCS1_v1_5.new(public_key)
        encrypted = cipher.encrypt(password.encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")

    @staticmethod
    def _parse_result_cookies(headers) -> str:
        """从响应头中提取 passport + tenant cookie"""
        set_cookie = headers.get("Set-Cookie", "")
        passport = re.search(r"x-ienterprise-passport=([^;,\s\"]+)", set_cookie)
        tenant = re.search(r"x-ienterprise-tenant=([^;,\s\"]+)", set_cookie)

        parts = []
        if passport:
            parts.append(f"x-ienterprise-passport={passport.group(1)}")
        if tenant:
            parts.append(f"x-ienterprise-tenant={tenant.group(1)}")

        return ";".join(parts)

    def _build_full_cookie_str(self, last_headers) -> str:
        """构建完整 cookie 字符串"""
        set_cookie = last_headers.get("Set-Cookie", "")
        if set_cookie:
            for part in set_cookie.split(","):
                kv = part.split(";")[0].strip().split("=", 1)
                if len(kv) == 2:
                    k, v = kv[0].strip(), kv[1].strip()
                    if k.lower() not in ("path", "expires", "max-age", "domain", "secure", "httponly", "samesite"):
                        self._cookie_jar[k] = v

        passport = re.search(r"x-ienterprise-passport=([^;,\s\"]+)", set_cookie)
        tenant = re.search(r"x-ienterprise-tenant=([^;,\s\"]+)", set_cookie)
        if passport:
            self._cookie_jar["x-ienterprise-passport"] = passport.group(1)
        if tenant:
            self._cookie_jar["x-ienterprise-tenant"] = tenant.group(1)

        return "; ".join(f"{k}={v}" for k, v in self._cookie_jar.items() if v and v != "\"\"")


# ============================================================
# 对外接口：get_cookie()
# ============================================================

def get_cookie(force_login: bool = False) -> str:
    """
    获取 Cookie，优先使用缓存，过期或强制时重新登录。

    Args:
        force_login: 强制重新登录

    Returns:
        Cookie 字符串
    """
    # 先检查 credentials.yaml 里是否直接配了 cookie
    creds = _load_credentials()
    if creds.get("cookie") and not force_login:
        return creds["cookie"]

    # 检查缓存（4小时有效）
    if not force_login:
        cached = _load_cookie_cache()
        if cached:
            return cached

    # 自动登录
    print("  正在自动登录...")
    auth = CRMAuth()
    cookie = auth.login(
        base_url=creds.get("base_url", "https://crm-cd.xiaoshouyi.com"),
        login_url=creds.get("login_url", "https://login-cd.xiaoshouyi.com"),
        username=creds["username"],
        password=creds["password"],
        tenant=creds["tenant"],
    )

    # 缓存
    _save_cookie_cache(cookie)
    print("  登录成功")
    return cookie


def _load_credentials() -> dict:
    with open(CREDENTIALS_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_cookie_cache(cookie: str):
    cache = {"cookie": cookie, "login_time": time.time()}
    with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def _load_cookie_cache() -> str:
    if not os.path.exists(COOKIE_CACHE_FILE):
        return ""
    try:
        with open(COOKIE_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("login_time", 0) > 4 * 3600:
            return ""
        return cache.get("cookie", "")
    except Exception:
        return ""


if __name__ == "__main__":
    # 直接运行测试登录
    cookie = get_cookie(force_login=True)
    print(f"\nCookie (前100字符):\n{cookie[:100]}...")
