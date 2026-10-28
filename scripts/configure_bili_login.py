"""Scan with the Bilibili app; save only the read credential locally."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit

import httpx
import qrcode


ROOT = Path(__file__).resolve().parents[1]
PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode/"


class LoginError(RuntimeError):
    """Only fixed, non-secret diagnostics may be displayed to the user."""


def session_from_url(url):
    # Preserve the cookie's original percent encoding, as returned by QR login.
    fields = dict(part.split("=", 1) for part in urlsplit(url).query.split("&") if "=" in part)
    return fields.get("SESSDATA", "")


def session_from_confirmation(client, response, state):
    session = next((cookie.value for cookie in response.cookies.jar if cookie.name == "SESSDATA"), "")
    url = state.get("url", "")
    session = session or session_from_url(url)
    if session:
        return session
    # New WEB QR success responses carry a one-time cross-domain ticket.
    # Its first response (often 302) sets the cookie; do not re-poll the QR key.
    target = urlsplit(url)
    if (target.scheme != "https" or target.hostname not in {"passport.bilibili.com", "passport.biligame.com"}
            or target.path != "/x/passport-login/web/crossDomain"
            or target.port not in {None, 443} or target.username or target.password):
        raise LoginError("手机已确认，但登录响应没有可用凭据或已知的换票地址；请重新扫码。")
    exchanged = client.get(url, headers={"Referer": "https://passport.bilibili.com/"}, follow_redirects=False)
    if not 200 <= exchanged.status_code < 400:
        exchanged.raise_for_status()
    session = next((cookie.value for cookie in exchanged.cookies.jar if cookie.name == "SESSDATA"), "")
    if not session:
        raise LoginError("手机已确认，但换票响应未下发SESSDATA；未修改配置，请重新扫码。")
    return session


def login_json(response, stage):
    try:
        return response.json()
    except ValueError:
        raise LoginError(stage + "接口没有返回JSON，可能收到网络验证页；请稍后重新扫码。") from None


def save_credential(root, sessdata):
    if not isinstance(sessdata, str) or not 0 < len(sessdata) < 4096 or any(
        char in sessdata for char in "\r\n;"
    ):
        raise ValueError("登录没有返回有效的SESSDATA，未修改配置。")
    config_file = root / "config/harness/executor.local.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if config.get("biliHumanism", {}).get("enabled") is not True:
        raise ValueError("请先启用B站读取插件，未修改配置。")
    credential = config_file.with_name("bili-credential.local.json")
    backup = root / "var/backups" / ("bili-login-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True)
    shutil.copy2(config_file, backup / config_file.name)
    if credential.exists():
        shutil.copy2(credential, backup / credential.name)
    temporary = credential.with_name(".bili-credential.tmp.local.json")
    temporary.write_text(json.dumps({"sessdata": sessdata}) + "\n", encoding="utf-8")
    temporary.replace(credential)
    config["biliHumanism"]["credentialPath"] = str(credential.resolve())
    temporary = config_file.with_name(".executor.tmp.local.json")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_file)
    return credential


def login(*, root=ROOT, timeout=180, show=False):
    directory = root / "var/bili-login"
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / ("qrcode-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png")
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    with httpx.Client(headers=headers, timeout=15) as client:
        response = client.get(PASSPORT + "generate", params={"source": "main-fe-header"})
        response.raise_for_status()
        payload = login_json(response, "二维码生成")
        if payload.get("code") != 0:
            raise RuntimeError("B站未返回登录二维码，配置未修改。")
        data = payload["data"]
        target = urlsplit(data["url"])
        if target.scheme != "https" or target.hostname not in {"passport.bilibili.com", "account.bilibili.com"}:
            raise RuntimeError("登录二维码地址异常，配置未修改。")
        qrcode.make(data["url"]).save(image)
        print("二维码已生成，请用B站App扫码并确认登录：", flush=True)
        print(str(image.resolve()), flush=True)
        if show:
            os.startfile(image)
        deadline = time.monotonic() + timeout
        last_state = None
        while time.monotonic() < deadline:
            time.sleep(3)
            response = client.get(PASSPORT + "poll", params={
                "qrcode_key": data["qrcode_key"], "source": "main-fe-header"})
            response.raise_for_status()
            payload = login_json(response, "扫码状态")
            if payload.get("code") != 0:
                raise RuntimeError("登录状态查询失败，配置未修改。")
            state = payload["data"]
            code = state["code"]
            if code == 0:
                session = session_from_confirmation(client, response, state)
                credential = save_credential(root, session)
                print("登录成功，读取凭据已保存：" + str(credential), flush=True)
                print("未启用平台写入。正常重启WARMAGENT后生效。", flush=True)
                return True
            if code == 86038:
                print("二维码已过期，重新运行此脚本即可；配置未修改。", flush=True)
                return False
            if code not in {86101, 86090}:
                raise RuntimeError("B站返回了未识别的登录状态，配置未修改。")
            if code != last_state:
                print("等待扫码。" if code == 86101 else "已扫码，请在B站App确认。", flush=True)
                last_state = code
    print("等待扫码超时；配置未修改。", flush=True)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=180, help="等待手机扫码的秒数")
    parser.add_argument("--show", action="store_true", help="用默认图片应用打开二维码")
    args = parser.parse_args()
    try:
        success = login(timeout=args.timeout, show=args.show)
    except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as error:
        # Login responses and poll URLs can contain secrets; never print them.
        reason = (str(error) if isinstance(error, LoginError) else
                  "HTTP " + str(error.response.status_code) if isinstance(error, httpx.HTTPStatusError)
                  else type(error).__name__)
        print("B站登录未完成（" + reason + "）。不要把Cookie贴到聊天里。", flush=True)
        success = False
    raise SystemExit(0 if success else 1)
