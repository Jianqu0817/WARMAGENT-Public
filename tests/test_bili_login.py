import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("bili_login", Path(__file__).resolve().parents[1]
                                            / "scripts/configure_bili_login.py")
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


def test_qr_session_keeps_original_encoding():
    assert login.session_from_url("https://passport.bilibili.com/a?SESSDATA=test%2C123&bili_jct=not-saved") == "test%2C123"


def test_save_only_session_and_preserve_native_config(tmp_path):
    file = tmp_path / "config/harness/executor.local.json"
    file.parent.mkdir(parents=True)
    config = {"skills": [{"name": "existing"}], "mcpServers": [], "biliHumanism": {
        "enabled": True, "enablePersonalityLearning": False, "enableLikeWrites": False}}
    file.write_text(json.dumps(config), encoding="utf-8")
    credential = login.save_credential(tmp_path, "test%2C123")
    assert json.loads(credential.read_text()) == {"sessdata": "test%2C123"}
    after = json.loads(file.read_text(encoding="utf-8"))
    assert after["biliHumanism"].pop("credentialPath") == str(credential.resolve())
    assert after == config
    assert next((tmp_path / "var/backups").glob("*/executor.local.json")).read_bytes() != b""


def test_invalid_session_does_not_modify_config(tmp_path):
    with pytest.raises(ValueError):
        login.save_credential(tmp_path, "bad;cookie")
    assert not list(tmp_path.iterdir())


def test_qr_flow_writes_only_after_phone_confirmation(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config/harness/executor.local.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"biliHumanism": {"enabled": True}}))
    states = iter([86101, 86090, 0])

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, params):
            if url.endswith("generate"):
                data = {"url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?key=test",
                        "qrcode_key": "test"}
            else:
                assert not (config.parent / "bili-credential.local.json").exists()
                code = next(states)
                data = {"code": code, "url": "https://passport.bilibili.com/a?SESSDATA=test%2C123&bili_jct=not-saved"}
            return login.httpx.Response(200, json={"code": 0, "data": data},
                                        request=login.httpx.Request("GET", url))

    monkeypatch.setattr(login.httpx, "Client", Client)
    monkeypatch.setattr(login.time, "sleep", lambda *_: None)
    assert login.login(root=tmp_path)
    assert json.loads((config.parent / "bili-credential.local.json").read_text()) == {"sessdata": "test%2C123"}
    output = capsys.readouterr().out
    assert "test%2C123" not in output and "not-saved" not in output


@pytest.mark.parametrize('source', ['poll-cookie', 'legacy-url', 'cross-domain'])
def test_confirmation_formats_save_only_read_session(tmp_path, source, capsys):
    file = tmp_path / 'config/harness/executor.local.json'
    file.parent.mkdir(parents=True)
    original = {'biliHumanism': {'enabled': True, 'enableLikeWrites': False}}
    file.write_text(json.dumps(original))
    calls = []
    url = 'https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=private-ticket'

    def transport(request):
        calls.append(request)
        assert str(request.url) == url
        assert request.headers['referer'] == 'https://passport.bilibili.com/'
        assert request.headers['user-agent'] == 'Mozilla/5.0'
        # Cookie must be captured from this 302 without following Location.
        return login.httpx.Response(302, headers=[('Set-Cookie', 'SESSDATA=test%2C123; Path=/; HttpOnly'),
            ('Set-Cookie', 'bili_jct=not-saved; Path=/'), ('Location', 'https://www.bilibili.com/')])

    headers = {'Set-Cookie': 'SESSDATA=test%2C123; Path=/; HttpOnly'} if source == 'poll-cookie' else {}
    response = login.httpx.Response(200, headers=headers, request=login.httpx.Request('GET', login.PASSPORT + 'poll'))
    state = {'url': url if source == 'cross-domain' else
             'https://passport.bilibili.com/a?SESSDATA=test%2C123' if source == 'legacy-url' else ''}
    with login.httpx.Client(transport=login.httpx.MockTransport(transport),
                           headers={'User-Agent': 'Mozilla/5.0'}) as client:
        session = login.session_from_confirmation(client, response, state)
    path = login.save_credential(tmp_path, session)
    assert json.loads(path.read_text()) == {'sessdata': 'test%2C123'}
    assert len(calls) == (1 if source == 'cross-domain' else 0)
    assert not capsys.readouterr().out


@pytest.mark.parametrize('url', [
    'https://example.com/x/passport-login/web/crossDomain?ticket=secret',
    'https://passport.biligame.com.evil.example/x/passport-login/web/crossDomain?ticket=secret',
    'https://passport.biligame.com:8443/x/passport-login/web/crossDomain?ticket=secret',
    '',
])
def test_unknown_exchange_address_is_not_requested(url):
    def unexpected(request):
        pytest.fail('unknown login address was requested')
    with login.httpx.Client(transport=login.httpx.MockTransport(unexpected)) as client:
        with pytest.raises(login.LoginError) as error:
            login.session_from_confirmation(client, login.httpx.Response(200,
                request=login.httpx.Request('GET', login.PASSPORT + 'poll')), {'url': url})
    assert 'secret' not in str(error.value)


def test_missing_exchange_cookie_and_non_json_have_safe_diagnostics():
    with login.httpx.Client(transport=login.httpx.MockTransport(
            lambda _: login.httpx.Response(302, headers={'Location': 'https://www.bilibili.com/'}))) as client:
        with pytest.raises(login.LoginError, match='未下发SESSDATA'):
            login.session_from_confirmation(client, login.httpx.Response(200,
                request=login.httpx.Request('GET', login.PASSPORT + 'poll')), {
                'url': 'https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=secret'})
    with pytest.raises(login.LoginError, match='扫码状态接口没有返回JSON') as error:
        login.login_json(login.httpx.Response(200, text='private response'), '扫码状态')
    assert 'private response' not in str(error.value)
