"""Low-level async WebSocket client for Trade Republic's wire protocol.

TR talks a simple text protocol over WSS:

    Client → Server:
        connect 31 {clientInfo JSON}
        sub <id> {payload JSON}
        unsub <id>
        echo <ts>

    Server → Client:
        connected                    -- on successful connect
        <id> A <full payload JSON>   -- subscription Answer (initial state)
        <id> D <delta>               -- subscription Delta (custom patch format)
        <id> C                       -- subscription Close
        <id> E <error JSON>          -- subscription Error

Authentication: cookies travel in the Upgrade request's `Cookie:` header.
No separate session-token negotiation; if the cookies are valid, the
connection just works.

Delta format: tab-separated chunks where each chunk starts with `+`, `-`,
or `=` followed by a length (for `=`/`-`) or a URL-encoded literal (for
`+`). It's not JSON Patch — TR rolled their own. We implement it so we
can support streaming subscriptions, but for one-shot snapshot fetches
(`fetch_one`) we never need to apply deltas.

This module is intentionally minimal: a `TrWebSocket` class plus a
`fetch_one()` convenience for the snapshot pattern. Higher-level wrappers
(portfolio, cash, transactions) live in separate modules.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import urllib.parse
from http.cookiejar import CookieJar
from typing import Any

import certifi  # ships with requests; safe to depend on
import websockets

from .exceptions import ApiError, SessionExpired

WS_URL = "wss://api.traderepublic.com"

# Protocol version. pytr uses 31 (cookie-authenticated session); 21 is
# the older PIN-token mode. We always use 31.
PROTOCOL_VERSION = 31

# Default client identification. TR's frontend sends similar values; the
# server doesn't appear to enforce specific Chrome versions, but matching
# the shape of a real client is cheap insurance.
DEFAULT_CLIENT_INFO = {
    "platformId": "webtrading",
    "platformVersion": "chrome - 131.0.0",
    "clientId": "app.traderepublic.com",
    "clientVersion": "7000",  # arbitrary but plausible
}


class TrWebSocket:
    """Async WebSocket client for Trade Republic.

    Typical use is via fetch_one() for snapshot subscriptions; use this
    class directly if you need to keep a subscription open and process
    deltas as they arrive.
    """

    def __init__(self, cookie_jar: CookieJar, *, locale: str = "en"):
        self.cookie_jar = cookie_jar
        self.locale = locale
        self._ws: Any | None = None
        self._sub_counter = 0
        self._lock = asyncio.Lock()
        # Stash of last A/D payloads per subscription, so we can apply
        # subsequent D deltas. Keyed by subscription id.
        self._last_payload: dict[str, str] = {}
        # Active subscriptions (id -> request payload).
        self._subs: dict[str, dict[str, Any]] = {}

    # -----------------------------------------------------------------
    # Connect / close
    # -----------------------------------------------------------------
    async def connect(self) -> None:
        """Open the WebSocket and send the initial `connect` frame."""
        cookie_header = self._build_cookie_header()
        if not cookie_header:
            raise SessionExpired(
                "No traderepublic.com cookies to authenticate the WebSocket. "
                "Re-import cookies from Chrome."
            )

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._ws = await websockets.connect(
            WS_URL,
            ssl=ssl_ctx,
            additional_headers={"Cookie": cookie_header},
            user_agent_header=None,  # don't add a `websockets/x.y` UA
            max_size=2**24,           # 16 MiB; some timeline pages are big
        )

        client_info = {"locale": self.locale, **DEFAULT_CLIENT_INFO}
        await self._ws.send(f"connect {PROTOCOL_VERSION} {json.dumps(client_info)}")
        resp = await self._ws.recv()
        if resp != "connected":
            # Most common cause: cookies rejected. Frame it that way.
            raise SessionExpired(
                f"WebSocket auth failed (server said {resp!r}). "
                "Your cookies are likely expired — re-import from Chrome."
            )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> TrWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -----------------------------------------------------------------
    # Subscribe / receive
    # -----------------------------------------------------------------
    async def _next_id(self) -> str:
        async with self._lock:
            self._sub_counter += 1
            return str(self._sub_counter)

    async def subscribe(self, payload: dict[str, Any]) -> str:
        """Send `sub <id> <payload>`. Returns the subscription id."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected; call connect() first")
        sub_id = await self._next_id()
        self._subs[sub_id] = payload
        await self._ws.send(f"sub {sub_id} {json.dumps(payload)}")
        return sub_id

    async def unsubscribe(self, sub_id: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(f"unsub {sub_id}")
        self._subs.pop(sub_id, None)
        self._last_payload.pop(sub_id, None)

    async def recv(self) -> tuple[str, str, str]:
        """Receive the next frame. Returns (sub_id, code, payload_str).

        Code is 'A' (answer), 'D' (delta), 'C' (close), or 'E' (error).
        The caller is responsible for parsing payload_str as JSON when
        appropriate, and for applying deltas via apply_delta().
        """
        if self._ws is None:
            raise RuntimeError("WebSocket not connected; call connect() first")
        raw = await self._ws.recv()
        if not isinstance(raw, str):
            raw = raw.decode("utf-8", errors="replace")
        # Format: "<id> <code> <payload>". Code is one char; payload may be empty.
        first_sp = raw.find(" ")
        if first_sp == -1:
            raise ApiError(f"Unparseable WS frame: {raw!r}")
        sub_id = raw[:first_sp]
        code = raw[first_sp + 1 : first_sp + 2]
        payload = raw[first_sp + 2 :].lstrip()
        return sub_id, code, payload

    # -----------------------------------------------------------------
    # Delta protocol (TR's custom patch format)
    # -----------------------------------------------------------------
    def apply_delta(self, sub_id: str, delta: str) -> str:
        """Apply a D-frame delta to the last A/D payload for this subscription.

        The delta is a tab-separated sequence of operations:

            +<urlencoded literal>  → insert literal at current cursor
            =<n>                   → copy n bytes from previous payload
            -<n>                   → skip n bytes of previous payload

        The cursor advances through the *previous* payload for `=` and `-`.
        `+` does not advance it (the literal is novel).
        """
        prev = self._last_payload.get(sub_id, "")
        i = 0
        out: list[str] = []
        for chunk in delta.split("\t"):
            if not chunk:
                continue
            sign = chunk[0]
            arg = chunk[1:]
            if sign == "+":
                out.append(urllib.parse.unquote_plus(arg))
            elif sign == "=":
                n = int(arg)
                out.append(prev[i : i + n])
                i += n
            elif sign == "-":
                i += int(arg)
            else:
                raise ApiError(f"Unknown delta op {sign!r} in chunk {chunk!r}")
        result = "".join(out)
        self._last_payload[sub_id] = result
        return result

    # -----------------------------------------------------------------
    # High-level helpers
    # -----------------------------------------------------------------
    async def batch_fetch(
        self,
        payloads: list[dict[str, Any]],
        *,
        timeout: float = 60.0,
        ignore_errors: bool = True,
    ) -> list[Any]:
        """Subscribe to many topics on one WS, collect the first Answer for each.

        Returns a list aligned to `payloads`. Each element is the parsed JSON
        body of the Answer frame for that subscription, or — if the
        subscription errored and `ignore_errors=True` — `None`. If
        `ignore_errors=False`, an erroring sub raises ApiError immediately.

        This is what makes "get the whole portfolio with names + prices" fast:
        send 600+ subscribes up-front, then drain the responses as they come
        back. Doing one fetch_one() per ISIN would mean 600+ WS connections
        and is what gets you flagged with WS close 3003 "registered".

        Frames for unknown/already-resolved subscription ids are silently
        dropped (deltas, late closes, etc.). We only look at the first
        Answer per sub.
        """
        if not payloads:
            return []

        if self._ws is None:
            raise RuntimeError("WebSocket not connected; call connect() first")

        # Fire all subscribes (sequential send, but doesn't await answers)
        sub_ids: list[str] = []
        for p in payloads:
            sid = await self.subscribe(p)
            sub_ids.append(sid)

        results: dict[str, Any] = {}
        wanted: set[str] = set(sub_ids)

        async def _drain() -> None:
            while wanted:
                rid, code, body = await self.recv()
                if rid not in wanted:
                    continue
                if code == "A":
                    self._last_payload[rid] = body
                    results[rid] = json.loads(body) if body else {}
                    wanted.discard(rid)
                elif code == "E":
                    err = json.loads(body) if body else {}
                    if ignore_errors:
                        results[rid] = None
                        wanted.discard(rid)
                    else:
                        raise ApiError(
                            f"TR rejected subscription: {err}",
                            body=body,
                        )
                elif code == "C":
                    # Close without Answer — treat as missing.
                    results[rid] = None
                    wanted.discard(rid)
                # 'D' frames before an Answer are ignored (shouldn't happen).

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        finally:
            # Unsubscribe everything to keep TR happy. Errors here don't matter.
            for sid in sub_ids:
                try:
                    await self.unsubscribe(sid)
                except Exception:
                    pass

        return [results.get(sid) for sid in sub_ids]

    async def fetch_one(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 10.0,
    ) -> Any:
        """Subscribe, wait for the first answer, unsubscribe, return parsed JSON.

        Raises ApiError on E-frames or timeout; raises SessionExpired if
        the WS isn't connected (shouldn't happen if you used `async with`).
        """
        sub_id = await self.subscribe(payload)
        try:
            async def _wait_for_answer() -> Any:
                while True:
                    rid, code, body = await self.recv()
                    if rid != sub_id:
                        # Frame for a different subscription — drop it
                        # (we have no other active subs in fetch_one).
                        continue
                    if code == "A":
                        self._last_payload[sub_id] = body
                        return json.loads(body) if body else {}
                    if code == "D":
                        # Shouldn't get a delta before an answer, but tolerate.
                        merged = self.apply_delta(sub_id, body)
                        return json.loads(merged) if merged else {}
                    if code == "E":
                        err = json.loads(body) if body else {}
                        raise ApiError(
                            f"TR rejected subscription {payload!r}: {err}",
                            status_code=None,
                            body=body,
                        )
                    if code == "C":
                        raise ApiError(
                            f"TR closed subscription {payload!r} before answer"
                        )

            return await asyncio.wait_for(_wait_for_answer(), timeout=timeout)
        finally:
            try:
                await self.unsubscribe(sub_id)
            except Exception:
                pass  # closing anyway

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------
    def _build_cookie_header(self) -> str:
        """Serialise all traderepublic.com cookies into one Cookie header."""
        parts: list[str] = []
        for c in self.cookie_jar:
            if c.domain.endswith("traderepublic.com"):
                parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)
