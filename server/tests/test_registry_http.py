"""The registry's HTTP surface, driven as bytes.

Routing and rendering are plain functions over bytes precisely so this file can
exist without a socket -- the same trick `admin.py` plays with its handler dict
and `mdns.py` plays with its codec. The one test that does open a port is there
because "the daemon still serves boards when the registry cannot listen" is not
provable any other way.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mbrelay.config import load as load_config
from mbrelay.httpapi import HttpApi, parse_head, render, route
from mbrelay.registry import NameRegistry


@pytest.fixture
def registry(tmp_path):
    return NameRegistry(load_config(overrides={"state.dir": str(tmp_path)}, environ={}))


def call(registry, raw: bytes) -> tuple[int, dict]:
    """Feed a whole raw request; get back (status, payload)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    return route(registry, parse_head(head), body)


def get(registry, path: str) -> tuple[int, dict]:
    return call(registry, f"GET {path} HTTP/1.1\r\nHost: relay\r\n\r\n".encode())


def put(registry, path: str, body: str) -> tuple[int, dict]:
    return call(registry, (f"PUT {path} HTTP/1.1\r\nHost: relay\r\n"
                           f"Content-Length: {len(body)}\r\n\r\n{body}").encode())


# -- the routes --------------------------------------------------------------
def test_asking_where_a_robot_is_answers_and_records_it(registry):
    status, payload = get(registry, "/names/tovez")
    assert status == 200
    assert (payload["channel"], payload["group"]) == (55, 108)
    assert payload["source"] == "derived" and payload["derived"] is True
    assert get(registry, "/names")[1]["names"][0]["name"] == "tovez"


def test_a_put_moves_a_robot_and_a_delete_puts_it_back(registry):
    assert put(registry, "/names/tovez", '{"channel": 12, "group": 4}') == (
        200, {"name": "tovez", "channel": 12, "group": 4, "source": "registry",
              "derived": False,
              "updated": get(registry, "/names/tovez")[1]["updated"]})
    status, payload = call(registry, b"DELETE /names/tovez HTTP/1.1\r\n\r\n")
    assert status == 200 and (payload["channel"], payload["source"]) == (55, "derived")


def test_the_listing_names_who_shares_a_link(registry):
    put(registry, "/names/tovez", '{"channel": 12, "group": 4}')
    put(registry, "/names/vevov", '{"channel": 12, "group": 4}')
    payload = get(registry, "/names")[1]
    assert payload["conflicts"] == [{"channel": 12, "group": 4,
                                     "names": ["tovez", "vevov"]}]
    assert {r["name"]: r.get("conflict") for r in payload["names"]} == {
        "tovez": ["vevov"], "vevov": ["tovez"]}


def test_status_is_a_liveness_probe_for_tooling(registry):
    registry.resolve("tovez")
    status, payload = get(registry, "/status")
    assert status == 200 and payload["names"] == 1 and payload["version"]


def test_a_trailing_slash_is_the_same_route(registry):
    assert get(registry, "/names/")[0] == get(registry, "/names")[0] == 200


# -- refusals ----------------------------------------------------------------
def test_a_malformed_name_is_a_400_with_a_usable_message(registry):
    status, payload = get(registry, "/names/robot1")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "five letters" in payload["error"]["message"]


def test_shadowing_a_config_pin_is_a_409_that_says_where_to_go(tmp_path):
    import dataclasses
    cfg = load_config(overrides={"state.dir": str(tmp_path)}, environ={})
    cfg = dataclasses.replace(cfg, registry=dataclasses.replace(
        cfg.registry, names={"tovez": "20/7"}))
    registry = NameRegistry(cfg)
    status, payload = put(registry, "/names/tovez", '{"channel": 12, "group": 4}')
    assert status == 409 and payload["error"]["code"] == "pinned"
    assert "[registry.names]" in payload["error"]["message"]


@pytest.mark.parametrize("body,expected", [
    ("", "channel"), ("not json", "JSON"), ("[]", "object"),
    ('{"channel": 12}', "group"), ('{"channel": "x", "group": 4}', "whole numbers"),
    ('{"channel": 999, "group": 4}', "0-83"),
])
def test_a_bad_put_body_says_what_was_wrong(registry, body, expected):
    status, payload = put(registry, "/names/tovez", body)
    assert status == 400 and expected in payload["error"]["message"]


def test_an_unknown_route_is_a_404(registry):
    for path in ("/", "/nope", "/names/tovez/extra"):
        assert get(registry, path)[0] == 404


def test_a_method_the_route_does_not_have_is_a_405(registry):
    assert call(registry, b"DELETE /names HTTP/1.1\r\n\r\n")[0] == 405
    assert call(registry, b"POST /names/tovez HTTP/1.1\r\n\r\n")[0] == 405


# -- the wire ----------------------------------------------------------------
def test_a_response_is_json_and_always_closes_the_connection(registry):
    raw = render(*get(registry, "/names/tovez"))
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: application/json" in head
    assert b"Connection: close" in head
    assert f"Content-Length: {len(body)}".encode() in head
    assert json.loads(body)["name"] == "tovez"


@pytest.mark.parametrize("raw", [b"nonsense\r\n", b"GET\r\n", b"GET / SPDY/1\r\n"])
def test_bytes_that_are_not_a_request_are_refused_not_crashed(raw):
    from mbrelay.httpapi import BadRequest
    with pytest.raises(BadRequest):
        parse_head(raw)


def test_a_query_string_is_ignored_rather_than_making_a_new_route(registry):
    assert parse_head(b"GET /names?all=1 HTTP/1.1").path == "/names"


async def test_the_daemon_still_serves_boards_when_the_port_is_taken(cfg, caplog):
    """A registry that cannot listen is a degraded relay, not a dead one --
    every robot that never moved is still reachable by its derived address."""
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    try:
        import dataclasses
        taken = dataclasses.replace(cfg, registry=dataclasses.replace(
            cfg.registry, bind="127.0.0.1", port=port))
        daemon = type("D", (), {"cfg": taken, "registry": NameRegistry(taken)})()
        api = HttpApi(daemon)
        with caplog.at_level("WARNING"):
            await api.start()
        assert api.problem and not api.to_json()["listening"]
        assert "boards are still served" in caplog.text
        await api.stop()
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_a_real_request_over_a_real_socket_round_trips(cfg):
    """One end-to-end pass, so the asyncio plumbing between the socket and
    route() is not merely assumed to line up."""
    import dataclasses
    listening = dataclasses.replace(cfg, registry=dataclasses.replace(
        cfg.registry, bind="127.0.0.1", port=0))
    daemon = type("D", (), {"cfg": listening, "registry": NameRegistry(listening)})()
    api = HttpApi(daemon)
    await api.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", api.port)
        writer.write(b"GET /names/tovez HTTP/1.1\r\nHost: relay\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK")
        assert json.loads(body)["channel"] == 55
    finally:
        await api.stop()
