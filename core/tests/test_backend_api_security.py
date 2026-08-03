import pytest
from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient
from backend.api.security import authentication as auth
from backend.bootstrap import create_app, create_container
from core.config import AppConfig
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore

_MINUTE=60

def _env(mp):
  mp.setenv("RENDER_BACKEND","mock")
  mp.setenv("LLM_ENGINE","none")
  mp.setenv("TTS_ENGINE","tone")
  mp.setenv("SESSION_STORE","memory")
  mp.setenv("DIRECTOR_ENABLED","0")
  mp.setenv("APP_ENV","dev")

def _make_app(config=None,container=None):
  if config is None:
    config=AppConfig(render_backend="mock",app_env="dev")
  if container is None:
    container=create_container(backend=MockRenderBackend(),store=InMemorySessionStore(),config=config)
  return create_app(config=config,container=container)

def test_tokens_match():
  assert auth.tokens_match("abc123","abc123")
  assert not auth.tokens_match("abc123","xyz789")

def test_parse_bearer():
  assert auth.parse_bearer("Bearer t123")=="t123"
  assert auth.parse_bearer(None) is None

def test_auth_disabled():
  cd=AppConfig(render_backend="mock",app_env="dev",backend_api_token="")
  assert auth.auth_disabled_dev(cd,"")

def test_401_missing(monkeypatch):
  _env(monkeypatch)
  with TestClient(_make_app(AppConfig(render_backend="mock",app_env="production",backend_api_token="tkn123",cors_origins="http://localhost"))) as c:
    assert c.post("/api/v1/lite/start",json={}).status_code==401

def test_401_wrong(monkeypatch):
  _env(monkeypatch)
  with TestClient(_make_app(AppConfig(render_backend="mock",app_env="production",backend_api_token="tkn123",cors_origins="http://localhost"))) as c:
    assert c.post("/api/v1/lite/start",json={},headers={"authorization":"Bearer wrong"}).status_code==401

def test_200_valid(monkeypatch):
  _env(monkeypatch)
  with TestClient(_make_app(AppConfig(render_backend="mock",app_env="production",backend_api_token="tkn123",cors_origins="http://localhost"))) as c:
    assert c.post("/api/v1/lite/start",json={},headers={"authorization":"Bearer tkn123"}).status_code!=401

def test_403_on_admin(monkeypatch):
  _env(monkeypatch)
  config=AppConfig(render_backend="mock",app_env="production",backend_api_token="vtkn",admin_api_token="atkn",cors_origins="http://localhost")
  with TestClient(_make_app(config)) as c:
    assert c.get("/api/v1/engines",headers={"authorization":"Bearer vtkn"}).status_code==403

def test_ws_accepts(monkeypatch):
  _env(monkeypatch)
  with TestClient(_make_app(AppConfig(render_backend="mock",app_env="production",backend_api_token="tknv",cors_origins="http://localhost"))) as c:
    with c.websocket_connect("/api/v1/ws/control/test-session?token=tknv") as ws:
      assert ws.receive_json()["type"]=="control.connected"

def test_rate_limit(monkeypatch):
  _env(monkeypatch)
  config=AppConfig(render_backend="mock",app_env="dev",backend_api_token="",api_rate_limit_requests=1,api_rate_limit_window_seconds=_MINUTE,api_rate_limit_max_keys=100)
  with TestClient(_make_app(config)) as c:
    c.post("/api/v1/lite/start",json={})
    assert c.post("/api/v1/lite/start",json={}).status_code==429

def test_ws_rate_limit(monkeypatch):
  _env(monkeypatch)
  config=AppConfig(render_backend="mock",app_env="dev",backend_api_token="",ws_rate_limit_messages=1,ws_rate_limit_window_seconds=_MINUTE,api_rate_limit_max_keys=100)
  with TestClient(_make_app(config)) as c:
    with c.websocket_connect("/api/v1/ws/control/test-session") as ws:
      ws.receive_json()
      ws.send_json({"type":"ping"})
      assert ws.receive_json()["type"]=="pong"
      ws.send_json({"type":"ping"})
      with pytest.raises(WebSocketDisconnect):
        ws.receive_json()

def test_integration(monkeypatch):
  _env(monkeypatch)
  config=AppConfig(render_backend="mock",app_env="production",backend_api_token="tkn",cors_origins="http://localhost")
  with TestClient(_make_app(config)) as c:
    assert c.get("/api/v1/health/live").headers.get("x-content-type-options")=="nosniff"
    assert c.post("/api/v1/lite/start",json={}).status_code==401
    assert c.post("/api/v1/lite/start",json={},headers={"authorization":"Bearer tkn"}).status_code!=401

def test_isolated(monkeypatch):
  _env(monkeypatch)
  config=AppConfig(render_backend="mock",app_env="dev")
  ca=create_container(backend=MockRenderBackend(),store=InMemorySessionStore(),config=config)
  cb=create_container(backend=MockRenderBackend(),store=InMemorySessionStore(),config=config)
  aa=create_app(config=config,container=ca)
  ab=create_app(config=config,container=cb)
  assert aa.state.container is not ab.state.container
