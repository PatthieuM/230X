#!/usr/bin/env python3
## @file rit_adaptive.py
# @brief RIT ALGO1 classroom strategy: reference-based LIMIT quoting and adaptive sizing.
# @author Matthieu Pascal
# @details Submission copy with Doxygen comments. Executable logic is unchanged.
# @warning Only the local RIT simulator is supported. No credentials are bundled.

# RIT Adaptive 3.0.1 experimental - self-contained, Python 3.9+, standard library.
# Generated from the adjacent rit_* source modules. No credential is bundled.

# ===== rit_broker.py =====
"""Local RIT simulator transport. Standard library only; LIMIT orders only.

The controller must validate the case, a flat account, and exclusive account use
before enabling live mode. Order fills here are cumulative and owned-order only.
"""
import collections
from concurrent.futures import ThreadPoolExecutor
import email.utils
import http.client
import json
import math
import re
import threading
import time
from urllib.parse import urlencode, urlsplit


_BROKER_TICKERS = frozenset(("CRZY_M", "CRZY_A"))
_BROKER_TERMINAL = frozenset(("TRANSACTED", "FILLED", "CANCELLED", "CANCELED", "REJECTED"))


class BrokerError(RuntimeError):
    pass


class AmbiguousOrder(BrokerError):
    """A mutation may have reached the server. Never repeat it blindly."""


class RejectedOrder(BrokerError):
    """An HTTP 4xx response explicitly rejected the request."""


def _broker_integer(value, name, minimum=0):
    if isinstance(value, bool):
        raise BrokerError("Invalid " + name)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BrokerError("Invalid " + name) from None
    if not math.isfinite(numeric) or numeric != int(numeric) or numeric < minimum:
        raise BrokerError("Invalid " + name)
    return int(numeric)


## @brief LIMIT-only transport and ledger for orders submitted by this process.
# @details Reserves outstanding quantities against worst-case inventory, validates
# cumulative fills, and distinguishes explicit rejection from uncertain execution.
# @warning Closing this client does not cancel orders or liquidate inventory.
class Broker:
    def __init__(self, base_url, api_key, live=False, max_position=200,
                 max_order=100, mutation_rate=4, timeout=1.5, logger=None):
        try:
            url = urlsplit(base_url.rstrip("/"))
            port = url.port
        except (ValueError, TypeError, AttributeError):
            raise BrokerError("Invalid local simulator URL") from None
        if (url.scheme not in ("http", "https") or
                url.hostname not in ("localhost", "127.0.0.1", "::1") or
                url.path != "/v1" or url.query or url.fragment or
                url.username is not None or url.password is not None):
            raise BrokerError("Only a loopback Client REST API URL ending /v1 is allowed")
        if not isinstance(api_key, str) or not api_key or any(
                ord(char) < 33 or ord(char) > 126 for char in api_key):
            raise BrokerError("API key must contain printable ASCII without whitespace")
        self._url, self._port, self._key = url, port, api_key
        self.live = bool(live)
        self.max_position = _broker_integer(max_position, "max_position", 1)
        self.max_order = _broker_integer(max_order, "max_order", 1)
        self.mutation_rate = _broker_integer(mutation_rate, "mutation_rate", 1)
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise BrokerError("Invalid timeout")
        self.logger = logger or (lambda event, **fields: None)
        self.orders = {}
        self.halted = False
        self.position_known = True
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections = []
        self._mutations = collections.deque()
        self._cooldown_until = 0.0
        self._pending = {}
        self._serial = 0
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=4)

    def _log(self, event, **fields):
        # Do not allow logging failures to interrupt execution reconciliation.
        safe = {key: value.replace(self._key, "[REDACTED]")
                if isinstance(value, str) else value for key, value in fields.items()}
        try:
            self.logger(event, **safe)
        except Exception:
            pass

    @property
    def position(self):
        with self._lock:
            return sum((1 if row["action"] == "BUY" else -1) * row["filled"]
                       for row in self.orders.values())

    @property
    def cash_known(self):
        with self._lock:
            return self.position_known and all(
                not row["filled"] or
                (row.get("vwap") is not None and row.get("vwap_filled") == row["filled"])
                for row in self.orders.values())

    @property
    def cash(self):
        with self._lock:
            if not self.cash_known:
                return float("nan")
            return sum((-1 if row["action"] == "BUY" else 1) *
                       row["filled"] * (row["vwap"] or 0.0)
                       for row in self.orders.values())

    @property
    def active_orders(self):
        with self._lock:
            return [row for row in self.orders.values() if not row["terminal"]]

    def mutation_ready(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            while self._mutations and self._mutations[0] <= now - 1.0:
                self._mutations.popleft()
            return now >= self._cooldown_until and len(self._mutations) < self.mutation_rate

    def _take_mutation(self, now):
        with self._lock:
            if not self.mutation_ready(now):
                return False
            self._mutations.append(now)
            return True

    def _disconnect(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
        self._local.connection = None

    def _connection(self):
        if self._closed:
            raise BrokerError("Broker is closed")
        connection = getattr(self._local, "connection", None)
        if connection is None:
            cls = http.client.HTTPSConnection if self._url.scheme == "https" else http.client.HTTPConnection
            connection = cls(self._url.hostname, self._port, timeout=self.timeout)
            self._local.connection = connection
            with self._lock:
                self._connections.append(connection)
        return connection

    def _cooldown(self, response, payload):
        delay = 0.0
        retry = response.getheader("Retry-After")
        if retry:
            try:
                delay = max(delay, float(retry))
            except (TypeError, ValueError):
                try:
                    delay = max(delay, email.utils.parsedate_to_datetime(retry).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        for value in (response.getheader("X-Wait-Until"),
                      payload.get("wait") if isinstance(payload, dict) else None):
            try:
                parsed = float(value)
                if math.isfinite(parsed):
                    delay = max(delay, parsed)
            except (TypeError, ValueError, OverflowError):
                pass
        if response.status == 429:
            delay = max(delay, 1.0)
        if delay > 0 and math.isfinite(delay):
            with self._lock:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)

    def _request(self, method, path, params=None):
        started = time.monotonic()
        status = None
        target = "/v1" + path + (("?" + urlencode(params)) if params else "")
        try:
            connection = self._connection()
            connection.request(method, target, body=b"" if method == "POST" else None,
                               headers={"X-API-Key": self._key, "Accept": "application/json"})
            response = connection.getresponse()
            status = response.status
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                self._disconnect()
                if method != "GET":
                    raise AmbiguousOrder("Mutation response exceeds size limit; outcome unknown")
                raise BrokerError("Response exceeds size limit")
            valid_json = True
            try:
                payload = json.loads(raw.decode("utf-8-sig")) if raw else None
            except (ValueError, UnicodeError):
                payload, valid_json = None, False
            self._cooldown(response, payload)
            # Explicit rejection is distinguished even when its body is HTML.
            if 400 <= status < 500:
                raise RejectedOrder("{} {} HTTP {}".format(method, path, status))
            if method != "GET" and (status >= 500 or not valid_json or not 200 <= status < 300):
                raise AmbiguousOrder("{} {} outcome unknown (HTTP {})".format(method, path, status))
            if not 200 <= status < 300 or not valid_json:
                raise BrokerError("{} {} unreadable/unsuccessful response (HTTP {})".format(method, path, status))
            return payload
        except (OSError, http.client.HTTPException):
            self._disconnect()
            if method != "GET":
                raise AmbiguousOrder("{} {} transport outcome unknown".format(method, path)) from None
            raise BrokerError("GET {} transport failed".format(path)) from None
        finally:
            self._log("http", method=method, path=path, status=status,
                      elapsed_ms=round((time.monotonic() - started) * 1000, 3))

    def get(self, path, params=None):
        params = dict(params or {})
        allowed = {"/case": set(), "/securities": {"ticker"}, "/limits": set(),
                   "/trader": set(), "/securities/book": {"ticker", "limit"},
                   "/securities/tas": {"ticker", "after", "limit"},
                   "/orders": {"status"}}
        if path not in allowed and not re.fullmatch(r"/orders/[0-9]+", path):
            raise BrokerError("Read endpoint is not allowed")
        if set(params) - allowed.get(path, set()):
            raise BrokerError("Read parameters are not allowed")
        if "ticker" in params and params["ticker"] not in _BROKER_TICKERS:
            raise BrokerError("Ticker is not allowed")
        if path in ("/securities/book", "/securities/tas") and "ticker" not in params:
            raise BrokerError("Ticker is required")
        if path == "/orders" and params.get("status") not in ("OPEN", "TRANSACTED", "CANCELLED"):
            raise BrokerError("An explicit allowed order status is required")
        for name in ("limit", "after"):
            if name in params:
                params[name] = _broker_integer(params[name], name, 1 if name == "limit" else 0)
        return self._request("GET", path, params)

    def _halt(self, message, position_unknown=False):
        self.halted = True
        if position_unknown:
            self.position_known = False
        self._log("halt", reason=message, position_known=self.position_known)

    def _apply(self, record, payload, submission=False):
        with self._lock:
            if not isinstance(payload, dict):
                self._halt("Invalid order response", position_unknown=True)
                raise BrokerError("Invalid order response")
            try:
                if str(payload.get("order_id")) != record["id"]:
                    raise BrokerError("Order identifier mismatch")
                for name, expected in (("ticker", record["ticker"]),
                                       ("action", record["action"]), ("type", "LIMIT")):
                    if (name in payload or not submission) and payload.get(name) != expected:
                        raise BrokerError("Order identity/type mismatch")
                if "quantity" in payload and _broker_integer(payload["quantity"], "quantity") != record["quantity"]:
                    raise BrokerError("Order quantity mismatch")
                if "quantity_filled" not in payload:
                    if submission:
                        return record
                    raise BrokerError("Missing cumulative fill quantity")
                filled = _broker_integer(payload["quantity_filled"], "filled quantity")
                if filled < record["filled"] or filled > record["quantity"]:
                    raise BrokerError("Invalid cumulative fill progression")
                status = str(payload.get("status", "OPEN")).upper()
                if status not in _BROKER_TERMINAL and status not in ("OPEN", "PARTIAL", "PENDING"):
                    raise BrokerError("Unknown order status")
            except BrokerError as error:
                self._halt(str(error), position_unknown=True)
                raise
            previous_filled = record["filled"]
            record["filled"] = filled
            record["terminal"] = status in _BROKER_TERMINAL or filled == record["quantity"]
            record["status"] = status
            vwap = None
            try:
                candidate = float(payload.get("vwap"))
                if math.isfinite(candidate) and candidate > 0:
                    vwap = candidate
            except (TypeError, ValueError, OverflowError):
                pass
            if not filled:
                record["vwap"], record["vwap_filled"] = None, 0
            elif vwap is not None:
                record["vwap"], record["vwap_filled"] = vwap, filled
                if ((record["action"] == "BUY" and vwap > record["price"] + 1e-7) or
                        (record["action"] == "SELL" and vwap < record["price"] - 1e-7)):
                    self._halt("Executed VWAP violates LIMIT price")
                    raise BrokerError("Executed VWAP violates LIMIT price")
            elif filled != previous_filled:
                record["vwap"], record["vwap_filled"] = None, None
            if filled > previous_filled:
                self._log("fill", order_id=record["id"], ticker=record["ticker"],
                          action=record["action"], quantity_delta=filled - previous_filled,
                          cumulative_filled=filled, vwap=record["vwap"], role=record["role"])
            if record["terminal"] and filled and record["vwap"] is None:
                self._halt("Final fill VWAP unavailable; cash is unknown")
                raise BrokerError("Final fill VWAP unavailable; cash is unknown")
            return record

    ## @brief Validate and submit one LIMIT order to the local simulator.
    # @param ticker CRZY_M or CRZY_A.
    # @param action BUY or SELL.
    # @param quantity Positive integer share count.
    # @param price Positive price aligned to the one-cent tick.
    # @param role Strategy label used to attribute fills.
    # @param now Optional decision timestamp; rate limiting uses the actual clock.
    # @return Owned order record, or None in read-only mode or when rate-limited.
    # @exception AmbiguousOrder Submission may have reached the server.
    # @warning An uncertain submission is never blindly retried.
    def place(self, ticker, action, quantity, price, role, now=None):
        if ticker not in _BROKER_TICKERS or action not in ("BUY", "SELL"):
            raise BrokerError("Ticker or order action is not allowed")
        quantity = _broker_integer(quantity, "order quantity", 1)
        try:
            price = float(price)
        except (TypeError, ValueError, OverflowError):
            raise BrokerError("Invalid LIMIT price") from None
        if (not math.isfinite(price) or price <= 0 or
                abs(price * 100 - round(price * 100)) > 1e-6):
            raise BrokerError("LIMIT price must be positive, finite and cent-aligned")
        if quantity > self.max_order:
            raise BrokerError("Order exceeds max_order")
        if not self.live:
            self._log("read_only_order_suppressed", ticker=ticker, action=action,
                      quantity=quantity, price=price, role=str(role))
            return None
        # A caller's quote timestamp must not weaken the real-time rate budget.
        decision_at = now
        now = time.monotonic()
        with self._lock:
            if self.halted or not self.position_known or self._closed:
                raise BrokerError("Broker halted/closed; placement is disabled")
            # A rate-blocked or racing cancellation leaves the old order real.
            # Never replace it with an opposite order that could self-execute.
            for row in self.active_orders + list(self._pending.values()):
                if row["ticker"] == ticker and row["action"] != action:
                    crosses = price >= row["price"] if action == "BUY" else price <= row["price"]
                    if crosses:
                        raise BrokerError("Refusing an order crossing our own outstanding LIMIT")
            position = self.position
            reserved = {"BUY": 0, "SELL": 0}
            for row in self.active_orders:
                reserved[row["action"]] += row["quantity"] - row["filled"]
            for row in self._pending.values():
                reserved[row["action"]] += row["quantity"]
            reserved[action] += quantity
            if position + reserved["BUY"] > self.max_position or position - reserved["SELL"] < -self.max_position:
                raise BrokerError("Worst-case inventory would exceed max_position")
            if not self._take_mutation(now):
                return None
            self._serial += 1
            token = self._serial
            self._pending[token] = {"ticker": ticker, "action": action,
                                    "quantity": quantity, "price": price}
        try:
            submit_started = time.monotonic()
            payload = self._request("POST", "/orders", {"ticker": ticker, "action": action,
                                    "quantity": quantity, "price": "{:.2f}".format(price), "type": "LIMIT"})
        except RejectedOrder:
            with self._lock:
                self._pending.pop(token, None)
            raise
        except Exception:
            self._halt("Order submission outcome unknown; do not resubmit", position_unknown=True)
            raise
        order_id = str(payload.get("order_id", "")) if isinstance(payload, dict) else ""
        with self._lock:
            if not re.fullmatch(r"[0-9]+", order_id) or order_id in self.orders:
                self._halt("Submission has no unique owned order ID", position_unknown=True)
                raise AmbiguousOrder("Submission has no unique owned order ID")
            record = {"id": order_id, "ticker": ticker, "action": action, "quantity": quantity,
                      "price": price, "role": str(role), "filled": 0, "vwap": None,
                      "vwap_filled": 0, "terminal": False, "created": now, "status": "OPEN",
                      "decision_at": decision_at,
                      "lasttiming": {"submit_started": submit_started,
                                     "submit_ms": (time.monotonic() - submit_started) * 1000}}
            self.orders[order_id] = record
            self._pending.pop(token, None)
        self._apply(record, payload, submission=True)
        self._log("order_submitted", order_id=order_id, ticker=ticker, action=action,
                  quantity=quantity, price=price, type="LIMIT", role=str(role))
        return record

    ## @brief Refresh active owned orders concurrently and update cumulative fills.
    # @return Updated order records.
    # @warning A polling failure halts placement without assuming that orders vanished.
    def poll(self):
        pending = [(row, time.monotonic(), self._pool.submit(self.get, "/orders/" + row["id"]))
                   for row in self.active_orders]
        updated, errors = [], []
        for row, started, future in pending:
            try:
                updated.append(self._apply(row, future.result()))
                row["lasttiming"] = {"poll_started": started,
                                     "poll_ms": (time.monotonic() - started) * 1000}
            except BrokerError as error:
                errors.append(error)
        if errors:
            self._halt("Owned order polling failed; outstanding orders remain reserved")
            raise errors[0]
        return updated

    ## @brief Request cancellation and reconcile the final cumulative fill.
    # @param order_id Identifier of an order owned by this broker.
    # @return True when terminal, False on confirmation timeout, or None if suppressed.
    # @details A cancellation acknowledgement is not proof of cancellation.
    # Unfilled quantities remain reserved until the final order state is confirmed.
    def cancel(self, order_id):
        order_id = str(order_id)
        with self._lock:
            if order_id not in self.orders:
                raise BrokerError("Refusing cancellation of an unowned order")
            record = self.orders[order_id]
            if record["terminal"]:
                return True
            if not self.live:
                return None
            if not self._take_mutation(time.monotonic()):
                return None
        ambiguous = False
        started = time.monotonic()
        try:
            self._request("DELETE", "/orders/" + order_id)
        except RejectedOrder:
            # It may already be fully executed: confirm using the owned ID.
            pass
        except BrokerError:
            ambiguous = True
        try:
            self._apply(record, self.get("/orders/" + order_id))
        except BrokerError:
            self._halt("Cancellation could not be reconciled; order remains reserved")
            raise
        if ambiguous:
            self._halt("Ambiguous cancellation reconciled; no new entries")
        # RIT can acknowledge DELETE before GET exposes its terminal status.
        # Keep the order and its unfilled quantity reserved; never resend DELETE
        # or replace the order while waiting for definitive cumulative fills.
        deadline = started + 2.0
        if not record["terminal"]:
            self._log("cancel_pending", order_id=order_id,
                      note="Waiting for final status; remaining quantity still reserved")
        while not record["terminal"] and time.monotonic() < deadline:
            time.sleep(.05)
            try:
                self._apply(record, self.get("/orders/" + order_id))
            except BrokerError:
                self._halt("Cancellation confirmation failed; order remains reserved")
                raise
        if not record["terminal"]:
            self._halt("Cancellation not confirmed after 2 seconds; order remains reserved")
            return False
        record["lasttiming"] = {"cancel_started": started,
                                "cancel_ms": (time.monotonic() - started) * 1000}
        self._log("order_cancelled", order_id=order_id, final_filled=record["filled"])
        return True

    def cancel_all(self):
        pending = [self._pool.submit(self.cancel, row["id"]) for row in self.active_orders]
        errors = []
        for future in pending:
            try:
                future.result()
            except BrokerError as error:
                errors.append(error)
        if errors:
            raise errors[0]
        return not self.active_orders

    def close(self):
        # Closing the HTTP client does not cancel orders or liquidate positions.
        self._closed = True
        self._pool.shutdown(wait=True)
        for connection in self._connections:
            connection.close()


# ===== rit_credentials.py =====
"""Hidden API-key entry with an optional Windows per-user DPAPI cache.

No key is bundled with the program. Set RIT_API_KEY for a session override,
or enter the key once on the Windows computer running RIT.
"""

import ctypes as _credential_ctypes
import getpass as _credential_getpass
import os as _credential_os
from pathlib import Path as _CredentialPath
import sys as _credential_sys
import tempfile as _credential_tempfile


def _credential_is_windows():
    return _credential_os.name == "nt"


def _credential_validate_key(value):
    if not isinstance(value, str) or not value or any(
        ord(char) < 33 or ord(char) > 126 for char in value
    ):
        raise ValueError("API key must contain only non-space ASCII characters.")
    return value


def _credential_cache_path():
    local = _credential_os.environ.get("LOCALAPPDATA")
    if not _credential_is_windows() or not local:
        return None
    root = _CredentialPath(local)
    return root / "RITAdaptive" / "api_key.dpapi" if root.is_absolute() else None


def _credential_warn(message):
    print(message, file=_credential_sys.stderr)


def _credential_dpapi(data, protect):
    """Windows current-user protection; plaintext never reaches the filesystem."""
    class Blob(_credential_ctypes.Structure):
        _fields_ = [("size", _credential_ctypes.c_uint32),
                    ("data", _credential_ctypes.POINTER(_credential_ctypes.c_ubyte))]

    crypt = _credential_ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = _credential_ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = _credential_ctypes.POINTER(Blob)
    api = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    api.argtypes = [ptr, _credential_ctypes.c_wchar_p if protect else
                    _credential_ctypes.POINTER(_credential_ctypes.c_wchar_p),
                    ptr, _credential_ctypes.c_void_p, _credential_ctypes.c_void_p,
                    _credential_ctypes.c_uint32, ptr]
    api.restype = _credential_ctypes.c_int
    kernel.LocalFree.argtypes = [_credential_ctypes.c_void_p]
    kernel.LocalFree.restype = _credential_ctypes.c_void_p
    buffer = _credential_ctypes.create_string_buffer(data)
    source = Blob(len(data), _credential_ctypes.cast(
        buffer, _credential_ctypes.POINTER(_credential_ctypes.c_ubyte)))
    result = Blob()
    description = "RITAdaptive API credential" if protect else None
    if not api(_credential_ctypes.byref(source), description, None, None, None,
               1, _credential_ctypes.byref(result)):  # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("Windows credential protection failed.")
    try:
        return _credential_ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(_credential_ctypes.cast(result.data, _credential_ctypes.c_void_p))


def _credential_save(path, key):
    encrypted = _credential_dpapi(key.encode("ascii"), True)
    if not encrypted:
        raise OSError("Windows credential protection failed.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with _credential_tempfile.NamedTemporaryFile(
            mode="wb", prefix=".api_key.", dir=str(path.parent), delete=False
        ) as handle:
            temporary = handle.name
            handle.write(encrypted)
            handle.flush()
            _credential_os.fsync(handle.fileno())
        _credential_os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                _credential_os.unlink(temporary)
            except FileNotFoundError:
                pass


## @brief Load the API key from the environment, encrypted cache, or hidden input.
# @param reset Request fresh credentials instead of reusing the cache.
# @return API key for local authentication.
# @note No credential is included in this submission.
def load_key(reset=False):
    """Return a key; reset skips the cache, but RIT_API_KEY always takes priority."""
    override = _credential_os.environ.get("RIT_API_KEY")
    if override is not None:
        return _credential_validate_key(override)
    path = _credential_cache_path()
    if path is not None and not reset:
        try:
            with path.open("rb") as handle:
                encrypted = handle.read(65537)
            if len(encrypted) > 65536:
                raise ValueError("Invalid credential cache.")
            return _credential_validate_key(_credential_dpapi(encrypted, False).decode("ascii"))
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError):
            _credential_warn("Saved API credential could not be read; enter it again.")
    key = _credential_validate_key(_credential_getpass.getpass("RIT API key (hidden): "))
    if path is not None:
        try:
            _credential_save(path, key)
        except (OSError, ValueError):
            _credential_warn("API credential was not saved; it remains in memory for this run only.")
    elif _credential_is_windows():
        _credential_warn("Credential cache unavailable; key remains in memory for this run only.")
    return key


def forget_key():
    """Remove only this application's DPAPI cache; do not change environment keys."""
    path = _credential_cache_path()
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ===== rit_signals.py =====
"""Conservative, deterministic reference signals for the RIT classroom simulator.

Book churn is instability, not proof of spoofing. No displayed order is assumed
executable; this module never submits orders and never reserves liquidity.
"""
import math
import statistics


def _signal_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


## @brief Estimate a robust price reference from persistent quotes and recent prints.
# @details Own orders are excluded. Quote disappearance contributes to a churn
# score; this is a heuristic, not proof that another trader is spoofing.
class SignalModel:
    def __init__(self, tickers, tick_size=.01, persistence=.30, warmup=1.0,
                 max_spread=.30, max_disagreement=.15, max_jump=.10,
                 max_age_ticks=3):
        self.tickers = tuple(tickers)
        if not self.tickers or len(set(self.tickers)) != len(self.tickers):
            raise ValueError("tickers must be nonempty and unique")
        params = (tick_size, persistence, warmup, max_spread,
                  max_disagreement, max_jump, max_age_ticks)
        if any(_signal_number(v) is None or v <= 0 for v in params):
            raise ValueError("signal thresholds must be positive finite numbers")
        self.tick_size, self.persistence, self.warmup = tick_size, persistence, warmup
        self.max_spread, self.max_disagreement = max_spread, max_disagreement
        self.max_jump, self.max_age_ticks = max_jump, max_age_ticks
        self.started = self.last_now = self.last_tick = None
        self.orders, self.prints, self.history = {}, {}, []
        self.churn = 0.0

    def _rows(self, raw, side, own_ids, trader):
        if not isinstance(raw, dict):
            return []
        rows = raw.get(side, raw.get(side + "s", []))
        if not isinstance(rows, list):
            return []
        parsed, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            ident = row.get("order_id", row.get("id"))
            # Per-order continuity cannot be inferred from anonymous levels.
            if ident is None or str(ident) in own_ids:
                continue
            if trader is not None and str(row.get("trader_id")) == trader:
                continue
            if str(row.get("status", "OPEN")).upper() not in ("OPEN", "PARTIAL", "PARTIALLY_FILLED"):
                continue
            action = str(row.get("action", "BUY" if side == "bid" else "SELL")).upper()
            if action != ("BUY" if side == "bid" else "SELL"):
                continue
            price = _signal_number(row.get("price"))
            quantity = _signal_number(row.get("quantity_remaining"))
            if quantity is None and "quantity_remaining" not in row:
                total, filled = _signal_number(row.get("quantity")), _signal_number(row.get("quantity_filled", 0))
                quantity = total - filled if total is not None and filled is not None and filled >= 0 else None
            if price is None or price <= 0 or quantity is None or quantity <= 0:
                continue
            # One server order id counts only once in a snapshot.
            if str(ident) not in seen:
                parsed.append((str(ident), price, quantity))
                seen.add(str(ident))
        return parsed

    @staticmethod
    def _touch(rows, side):
        if not rows:
            return (0.0, 0)
        price = (max if side == "bid" else min)(r[1] for r in rows)
        return (price, sum(r[2] for r in rows if r[1] == price))

    def _trades(self, trades, tick):
        for ticker in self.tickers:
            rows = trades.get(ticker, []) if isinstance(trades, dict) else []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                price, stamp = _signal_number(row.get("price")), _signal_number(row.get("tick"))
                qty = _signal_number(row.get("quantity", 1))
                if price is None or price <= 0 or stamp is None or not tick - self.max_age_ticks <= stamp <= tick or qty is None or qty <= 0:
                    continue
                ident = row.get("id", row.get("trade_id"))
                key = (ticker, str(ident)) if ident is not None else (ticker, stamp, price, qty)
                self.prints[key] = (stamp, price)
        self.prints = {k: v for k, v in self.prints.items() if tick - self.max_age_ticks <= v[0] <= tick}
        # Bounded history even if a server returns an unexpectedly huge page.
        if len(self.prints) > 2000:
            self.prints = dict(sorted(self.prints.items(), key=lambda kv: kv[1][0])[-2000:])

    ## @brief Update quote persistence, churn, and the valuation interval.
    # @param books Per-venue order books.
    # @param trades Recent time-and-sales records by venue.
    # @param now Monotonic observation time in seconds.
    # @param tick Current simulation tick.
    # @param own_ids Owned order identifiers to exclude.
    # @param own_trader_id Trader identifier to exclude when available.
    # @return Snapshot containing fair value, bounds, depth, and eligibility flags.
    # @details Let T be the median recent execution price and M the median
    # persistent-book midpoint. The implemented reference is:
    # @f[ \widehat V = 0.60T + 0.40M. @f]
    # Uncertainty is the maximum of one tick, the largest deviation of a venue
    # midpoint from T, and the median absolute deviation of execution prices from T.
    # The interval is [max(tick_size, fair - uncertainty), fair + uncertainty].
    # Wide spreads, inconsistent venues, rapid reference moves, or high churn
    # prevent new entries; these filters do not guarantee profitable fills.
    def update(self, books, trades, now, tick, own_ids=(), own_trader_id=None):
        if _signal_number(now) is None or _signal_number(tick) is None:
            raise ValueError("now and tick must be finite")
        if self.last_tick is not None and (tick < self.last_tick or now < self.last_now):
            self.started = None
            self.orders, self.prints, self.history = {}, {}, []
            self.churn = 0.0
        if self.started is None:
            self.started = now
        elapsed = max(0, now - self.last_now) if self.last_now is not None else 0
        self.churn *= math.exp(-elapsed / 1.0)
        self.last_tick, self.last_now = tick, now
        owned = {str(i) for i in own_ids}
        trader = str(own_trader_id) if own_trader_id is not None else None
        current, levels, stable = {}, {}, {}
        result = {"safe": False, "reason": "warmup", "fair": None,
                  "bid": {}, "ask": {}, "stable_bid": {}, "stable_ask": {},
                  "pressure": {}, "suspicious": 0.0, "fair_low": None,
                  "fair_high": None, "tick_size": self.tick_size}
        removed, previous_touch, removed_large, previous_large = 0., 0., 0., 0.
        for ticker in self.tickers:
            raw = books.get(ticker, {}) if isinstance(books, dict) else {}
            for side in ("bid", "ask"):
                rows = self._rows(raw, side, owned, trader)
                levels[ticker, side] = rows
                stable[ticker, side] = []
                for ident, price, qty in rows:
                    key = (ticker, side, ident)
                    old = self.orders.get(key)
                    since = old[0] if old and old[1] == price and qty <= old[2] else now
                    current[key] = (since, price, qty)
                    if now - since + 1e-9 >= self.persistence:
                        stable[ticker, side].append((ident, price, qty))
                prior = [(k, v) for k, v in self.orders.items() if k[:2] == (ticker, side) and k[2] not in owned]
                if prior:
                    touch = (max if side == "bid" else min)(v[1] for _, v in prior)
                    median_qty = statistics.median(v[2] for _, v in prior)
                    for key, old in prior:
                        vanished = key not in current or current[key][1] != old[1]
                        if old[1] == touch:
                            previous_touch += min(old[2], 1000)
                            removed += min(old[2], 1000) if vanished else 0
                        if old[2] >= max(1000, 3 * median_qty):
                            previous_large += 1
                            removed_large += int(vanished)
                result[side][ticker] = self._touch(rows, side)
                result["stable_" + side][ticker] = self._touch(stable[ticker, side], side)
            # Cap each order's influence; use only stable near-touch depth.
            volume = {}
            for side in ("bid", "ask"):
                touch = result["stable_" + side][ticker][0]
                volume[side] = sum(min(q, 1000) for _, p, q in stable[ticker, side]
                                   if abs(p - touch) <= 2 * self.tick_size + 1e-9)
            total = volume["bid"] + volume["ask"]
            result["pressure"][ticker] = (volume["bid"] - volume["ask"]) / total if total else 0.
        self.orders = current
        event = max(removed / previous_touch if previous_touch else 0.,
                    removed_large / previous_large if previous_large else 0.)
        self.churn = min(1., self.churn + .65 * event)
        result["suspicious"] = self.churn
        self._trades(trades, tick)
        reason, mids, spreads = None, [], []
        for ticker in self.tickers:
            bid, ask = result["bid"][ticker][0], result["ask"][ticker][0]
            sbid, sask = result["stable_bid"][ticker][0], result["stable_ask"][ticker][0]
            if not bid or not ask:
                reason = reason or "empty_or_unusable_book"
            elif bid >= ask:
                reason = reason or "internally_crossed_book"
            elif ask - bid > self.max_spread + 1e-9:
                reason = reason or "spread_too_wide"
            spreads.append(max(0., ask - bid))
            if sbid and sask:
                mids.append((sbid + sask) / 2)
            else:
                reason = reason or "insufficient_persistent_depth"
        prices = [p for _, p in self.prints.values()]
        if not prices:
            reason = reason or "no_fresh_executions"
        if len(mids) == len(self.tickers) and prices:
            trade_mid = statistics.median(prices)
            book_mid = statistics.median(mids)
            fair = .60 * trade_mid + .40 * book_mid
            uncertainty = max(self.tick_size, max(abs(m - trade_mid) for m in mids),
                              statistics.median(abs(p - trade_mid) for p in prices))
            result.update(fair=fair, fair_low=max(self.tick_size, fair - uncertainty), fair_high=fair + uncertainty)
            self.history = [(t, p) for t, p in self.history if now - t <= 1.0]
            self.history.append((now, fair))
            if max(mids) - min(mids) > self.max_disagreement + 1e-9:
                reason = reason or "venue_disagreement"
            if abs(trade_mid - book_mid) > self.max_disagreement + 1e-9:
                reason = reason or "executions_disagree_with_book"
            if max(p for _, p in self.history) - min(p for _, p in self.history) > self.max_jump + 1e-9:
                reason = reason or "fast_reference_movement"
        if now - self.started + 1e-9 < self.warmup:
            reason = reason or "warmup"
        if self.churn >= .60:
            reason = reason or "high_book_churn"
        result["reason"] = reason or "stable_reference"
        result["safe"] = reason is None
        return result


# ===== rit_sizing.py =====
"""Bounded sizing heuristic, not learned profitability or a guaranteed loss cap."""
import math


_SIZING_TICKERS = ("CRZY_M", "CRZY_A")


def _sizing_number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric market input")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite sizing input")
    return result


## @brief Compute a conservative integer order quantity.
# @param snapshot Validated market snapshot with valuation bounds and stable depth.
# @param trades Recent executed trades, deduplicated by venue and trade ID.
# @param tick Current simulation tick; volume uses prints no more than three ticks old.
# @param kind Entry sizing or optional probe sizing.
# @param max_quantity Configured per-order strategy cap (25 by default).
# @param position Signed inventory from owned fills.
# @param max_position Absolute inventory cap.
# @param risk_budget Heuristic monetary risk allocation.
# @param exit_loss_per_share Per-share exit allowance, not a guaranteed loss cap.
# @param volume_fraction Maximum fraction of recent confirmed volume.
# @return Dictionary with quantity, unit_risk, quality, reason, and binding caps.
# @details With suspicion s clipped to [0,1], quality is (1-s)^2.
# Unit risk is max(exit_loss_per_share, 0.02, fair_high-fair_low).
# @f[
# q = \left\lfloor (1-s)^2 \min\left(
# q_{\max}, B/r, fV, 0.10D, I_{\max}-|I|
# \right) \right\rfloor.
# @f]
# D is the smaller of the best available stable bid and ask depths across venues.
# Probe sizing additionally caps quantity at three times absolute inventory.
# Invalid or unsafe inputs produce quantity zero.
def adaptive_size(snapshot, trades, tick, *, kind="entry", max_quantity=25,
                  position=0, max_position=150, risk_budget=2,
                  exit_loss_per_share=.08, volume_fraction=.10):
    result = dict(quantity=0, unit_risk=0.0, quality=0.0, reason="invalid_input", caps={})
    try:
        if not isinstance(snapshot, dict) or snapshot.get("safe") is not True:
            result["reason"] = "unsafe_snapshot"
            return result
        maximum, held, inventory_limit, stamp = map(_sizing_number,
                                                   (max_quantity, position, max_position, tick))
        budget, exit_risk, fraction = map(_sizing_number,
                                         (risk_budget, exit_loss_per_share, volume_fraction))
        if (kind not in ("entry", "probe") or maximum <= 0 or inventory_limit <= 0 or
                any(value != int(value) for value in (maximum, held, inventory_limit, stamp)) or
                stamp < 0 or budget <= 0 or exit_risk <= 0 or not 0 < fraction <= 1):
            return result
        low, high, suspicion = map(_sizing_number,
                                   (snapshot["fair_low"], snapshot["fair_high"], snapshot["suspicious"]))
        if low <= 0 or high < low:
            return result
        depth, touch = {}, {}
        for side in ("bid", "ask"):
            quantities = []
            for ticker in _SIZING_TICKERS:
                price, quantity = snapshot["stable_" + side][ticker]
                price, quantity = _sizing_number(price), _sizing_number(quantity)
                if price <= 0 or quantity <= 0 or quantity != int(quantity):
                    return result
                touch[ticker, side] = price
                quantities.append(quantity)
            depth[side] = max(quantities)
        for ticker in _SIZING_TICKERS:
            if touch[ticker, "bid"] >= touch[ticker, "ask"]:
                return result
        unit_risk = max(exit_risk, .02, high - low)
        quality = (1 - max(0.0, min(1.0, suspicion))) ** 2
        result.update(unit_risk=unit_risk, quality=quality)
        if kind == "probe" and not held:
            result["reason"] = "probe_requires_inventory"
            return result
        if abs(held) >= inventory_limit:
            result["reason"] = "inventory_limit"
            return result
        if not isinstance(trades, dict):
            return result
        prints = {}
        for ticker in _SIZING_TICKERS:
            rows = trades.get(ticker, [])
            if not isinstance(rows, list):
                return result
            for row in rows:
                try:
                    if not isinstance(row, dict) or row.get("ticker", ticker) != ticker:
                        continue
                    ident = row.get("id", row.get("trade_id"))
                    if ident is None or isinstance(ident, bool) or not str(ident):
                        continue
                    trade_tick, quantity, price = map(_sizing_number,
                                                       (row["tick"], row["quantity"], row["price"]))
                    if (not stamp - 3 <= trade_tick <= stamp or trade_tick < 0 or
                            trade_tick != int(trade_tick) or quantity <= 0 or
                            quantity != int(quantity) or price <= 0):
                        continue
                    key, value = (ticker, str(ident)), (trade_tick, quantity, price)
                    if key in prints and prints[key] != value:
                        prints[key] = None  # Inconsistent duplicate is not reliable volume.
                    else:
                        prints[key] = value
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        confirmed_volume = sum(row[1] for row in prints.values() if row is not None)
        caps = dict(configured=maximum, risk_budget=budget / unit_risk,
                    recent_flow=fraction * confirmed_volume,
                    stable_depth=.10 * min(depth["bid"], depth["ask"]),
                    inventory_headroom=inventory_limit - abs(held))
        if kind == "probe":
            caps["probe_inventory"] = 3 * abs(held)
        if any(not math.isfinite(value) or value < 0 for value in caps.values()):
            return result
        quantity = math.floor(min(caps.values()) * quality + 1e-12)
        reason = "sized" if quantity else ("no_recent_confirmed_flow" if not confirmed_volume
                                            else "below_one_share_after_caps")
        result.update(quantity=quantity, reason=reason, caps=caps)
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return result


# ===== rit_app.py =====
"""Experimental ALGO1 classroom bot. LIMIT orders only; observation by default.

No claim of profitability or reliable spoof detection. A displayed probe is a
real, fillable order. Bounded LIMIT exits may leave inventory requiring manual
attention. Run only this bot on a flat, dedicated RIT account.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import signal
import threading
import time







ADAPTIVE_VERSION = "3.0.1-experimental"
ADAPTIVE_TICKERS = ("CRZY_M", "CRZY_A")


def _app_number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite simulator field")
    return result


def _app_round(price, action, tick=.01):
    units = price / tick
    return round((math.floor(units + 1e-8) if action == "BUY" else math.ceil(units - 1e-8)) * tick, 8)


class CaseChanged(RuntimeError):
    pass


class EventLog:
    def __init__(self, directory, secret):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / ("rit_v3_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".txt")
        self.file = self.path.open("x", encoding="utf-8")
        self.secret = secret
        self.lock = threading.Lock()

    def __call__(self, event, **fields):
        row = dict(event=event, utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **fields)
        # Serializing the secret first also handles quote/backslash characters.
        token = json.dumps(self.secret)[1:-1]
        data = json.dumps(row, ensure_ascii=True, default=str).replace(token, "[REDACTED]")
        with self.lock:
            self.file.write(data + "\n")
            self.file.flush()
            if event in {"START", "STATUS", "EPISODE", "PROBE_DISABLED", "STOP_REQUEST", "FINAL", "ERROR"}:
                print(data, flush=True)

    def close(self):
        self.file.close()


## @brief Coordinate market snapshots, quotes, inventory exits, and shutdown.
# @details The guarded mode used for the write-up disables optional probe orders.
# All modes preserve the LIMIT-only execution policy.
class AdaptiveBot:
    def __init__(self, broker, args, logger, clock=time.monotonic):
        self.broker, self.args, self.log, self.clock = broker, args, logger, clock
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.model = SignalModel(ADAPTIVE_TICKERS, persistence=args.persistence)
        self.stop_requested = False
        self.validated = False
        self.had_error = False
        self.final_flat = False
        self.context_valid = True
        self.ever_active = False
        self.last_case = None
        self.started = None
        self.stop_started = None
        self.initial_nlv = None
        self.last_account = self.last_heartbeat = -1e9
        self.account_mismatch_since = None
        self.trader_id = None
        self.trade_tick = None
        self.trades = {t: [] for t in ADAPTIVE_TICKERS}
        self.latest = None
        self.quote_plan = None
        self.quote_reference = None
        self.episode = None
        self.episode_count = 0
        self.probes_disabled = False
        self.next_entry = 0.0
        self.stats = {"control": [], "probe": []}
        self.min_order = {t: 1 for t in ADAPTIVE_TICKERS}

    def parallel_get(self, requests):
        futures = [self.pool.submit(self.broker.get, path, params) for path, params in requests]
        return [future.result() for future in futures]

    def case_check(self, case):
        name = "".join(c for c in str(case.get("name", "")).upper() if c.isalnum())
        if "ALGO1" not in name and "ALGORITHMICARBITRAGE" not in name:
            self.context_valid = False
            raise CaseChanged("Not the expected ALGO1 simulator case")
        period, tick = int(case["period"]), int(case["tick"])
        if self.last_case and (period != self.last_case[0] or tick < self.last_case[1]):
            self.context_valid = False
            raise CaseChanged("Case reset/period changed: no mutation of possibly reused IDs")
        self.last_case = (period, tick)
        active = str(case.get("status", "")).upper() in ("ACTIVE", "RUNNING")
        if active:
            self.ever_active = True
            if self.started is None:
                self.started = self.clock()
            if tick >= int(case.get("ticks_per_period", 300)) - self.args.end_buffer:
                self.request_stop(reason="end_buffer")
        elif self.ever_active:
            self.request_stop(reason="case_not_active")
        return active

    @staticmethod
    def _limit(rows):
        if isinstance(rows, dict):
            rows = rows.get("limits", [])
        selected = [r for r in rows if r.get("name") == "LIMIT-STOCK"]
        if len(selected) != 1:
            raise ValueError("Expected LIMIT-STOCK risk data")
        return selected[0]

    ## @brief Require the expected zero-fee ALGO1 case and a flat, dedicated account.
    # @details Checks authoritative net/gross exposure and existing open orders.
    # The two CRZY tickers share a base security; reported positions are not summed.
    # @exception ValueError Account state or configuration fails validation.
    # @note Residual inventory blocks startup even if no orders remain open.
    def validate(self):
        case, securities, risks, trader, opened = self.parallel_get([
            ("/case", None), ("/securities", None), ("/limits", None),
            ("/trader", None), ("/orders", {"status": "OPEN"})])
        self.case_check(case)
        rows = {r["ticker"]: r for r in securities}
        risk = self._limit(risks)
        if opened or any(abs(_app_number(risk[k])) > 1e-8 for k in ("net", "gross")):
            raise ValueError("Startup requires a flat account with no open orders")
        for ticker in ADAPTIVE_TICKERS:
            r = rows[ticker]
            if r.get("base_security") != "CRZY_M" or abs(_app_number(r["position"])) > 1e-8:
                raise ValueError("Expected flat, aggregated CRZY instruments")
            if abs(_app_number(r.get("trading_fee", 0))) > 1e-8:
                raise ValueError("This experimental model requires the zero-fee ALGO1 case")
            if r.get("is_tradeable") is False or r.get("is_shortable") is False:
                raise ValueError("Expected tradeable and shortable CRZY instruments")
            if int(r.get("quoted_decimals", 2)) != 2:
                raise ValueError("This configuration requires a 0.01 price tick")
            memberships = [m for m in r.get("limits", []) if m.get("name") == "LIMIT-STOCK"]
            if len(memberships) != 1 or abs(_app_number(memberships[0]["units"]) - 1) > 1e-8:
                raise ValueError("Expected one risk unit per CRZY share")
            cap = int(_app_number(r["max_trade_size"]))
            if self.args.max_order > cap:
                raise ValueError("Configured order cap exceeds simulator cap")
            self.min_order[ticker] = max(1, int(r.get("min_trade_size", 0)))
            rate = _app_number(r.get("api_orders_per_second", 10))
            if self.args.mutation_rate > rate:
                raise ValueError("Configured mutation rate exceeds simulator order rate")
        if self.args.max_position > min(_app_number(risk[k]) for k in ("net_limit", "gross_limit")):
            raise ValueError("Configured inventory bound exceeds simulator limit")
        self.initial_nlv = _app_number(trader["nlv"])
        self.trader_id = trader.get("trader_id")
        self.validated = True
        self.log("START", version=ADAPTIVE_VERSION, mode=self.args.mode,
                 live=self.args.live, orders="LIMIT_ONLY", max_position=self.args.max_position,
                 quote_size=self.args.quote_size, probe_size=self.args.probe_size,
                 key_saved_in_log=False)

    def request_stop(self, *_unused, reason="Ctrl+C"):
        if not self.stop_requested:
            self.log("STOP_REQUEST", reason=reason)
            self.stop_requested = True
            self.stop_started = self.clock()

    def account_check(self, now):
        if now - self.last_account < 1:
            return self.account_mismatch_since is None
        self.last_account = now
        risks, trader = self.parallel_get([("/limits", None), ("/trader", None)])
        risk = self._limit(risks)
        nlv = _app_number(trader["nlv"])
        net, gross = _app_number(risk["net"]), _app_number(risk["gross"])
        self.log("ACCOUNT", net=net, gross=gross, nlv_change=nlv - self.initial_nlv,
                 ledger_position=self.broker.position, gross_cash=self.broker.cash)
        if self.initial_nlv - nlv >= self.args.max_drawdown:
            self.request_stop(reason="account_loss_threshold")
        if gross > self.args.max_position or abs(net) > self.args.max_position:
            self.request_stop(reason="inventory_limit")
        if abs(net - self.broker.position) > 1e-8:
            self.broker.poll()
            # Account and fills are separate snapshots; freeze entries until they agree.
            if abs(net - self.broker.position) > 1e-8:
                if self.account_mismatch_since is None:
                    self.account_mismatch_since = now
                elif now - self.account_mismatch_since > 2:
                    raise ValueError("Account/owned-fill mismatch: possible manual or external trading")
                return False
        self.account_mismatch_since = None
        return True

    ## @brief Read books and recent executions and construct an age-checked signal.
    # @return Snapshot augmented with case activity, tick, and observation timing.
    # @note Separate HTTP responses are not an atomic market snapshot.
    def snapshot(self):
        started = self.clock()
        case, main, alt = self.parallel_get([
            ("/case", None), ("/securities/book", {"ticker": "CRZY_M", "limit": 20}),
            ("/securities/book", {"ticker": "CRZY_A", "limit": 20})])
        active = self.case_check(case)
        stamp = (case["period"], case["tick"])
        if active and self.trade_tick != stamp:
            groups = self.parallel_get([("/securities/tas", {"ticker": t, "limit": 3}) for t in ADAPTIVE_TICKERS])
            self.trades = dict(zip(ADAPTIVE_TICKERS, groups))
            self.trade_tick = stamp
        books = dict(zip(ADAPTIVE_TICKERS, (main, alt)))
        now = self.clock()
        snapshot = self.model.update(books, self.trades, now, int(case["tick"]),
                                     own_ids=self.broker.orders.keys(), own_trader_id=self.trader_id)
        snapshot.update(active=active, observed=now, case_tick=int(case["tick"]), duration_ms=1000 * (now - started))
        if now - started > self.args.max_snapshot_age:
            snapshot.update(safe=False, reason="slow_snapshot")
        self.log("SNAPSHOT", case_tick=case["tick"], snapshot=snapshot,
                 books=books if self.args.record_books else None,
                 trades=self.trades if self.args.record_books else None)
        self.latest = snapshot
        return snapshot

    def _fresh(self, snapshot):
        return snapshot and snapshot["active"] and self.clock() - snapshot["observed"] <= self.args.max_snapshot_age

    def cancel_active(self):
        if not self.context_valid:
            return False
        return self.broker.cancel_all()

    def new_episode(self, now):
        self.episode_count += 1
        probe = not self.probes_disabled and (self.args.mode == "influence" or
                 (self.args.mode == "experiment" and self.episode_count % 2 == 0))
        self.episode = dict(number=self.episode_count, arm="probe" if probe else "control",
                            cash_start=self.broker.cash, started=now, inventory_since=None,
                            probe_attempted=False, probe_fill=False)

    def finish_episode(self, now):
        if not self.episode:
            return
        episode = self.episode
        pnl = self.broker.cash - episode["cash_start"]
        self.stats[episode["arm"]].append(pnl)
        self.log("EPISODE", number=episode["number"], arm=episode["arm"],
                 gross_cash_pnl=pnl, probe_attempted=episode["probe_attempted"],
                 probe_fill=episode["probe_fill"], elapsed=now - episode["started"])
        if episode["arm"] == "probe" and sum(self.stats["probe"]) <= -self.args.probe_loss_stop:
            self.probes_disabled = True
            self.log("PROBE_DISABLED", reason="probe_episode_loss_threshold")
        self.episode = self.quote_plan = None
        self.next_entry = now + self.args.cooldown
        if pnl <= -self.args.max_episode_loss:
            self.request_stop(reason="episode_loss_threshold")

    ## @brief Construct passive buy and sell quotes across the two venues.
    # @param snapshot Fresh, eligible market snapshot.
    # @return Two order tuples, or None if sizing or signal checks fail.
    # @details For each venue with bid b and ask a, quotes before tick rounding are:
    # @f[ p_b=\min(V_{\mathrm{low}}-\delta,b+0.01,a-0.01), @f]
    # @f[ p_s=\max(V_{\mathrm{high}}+\delta,a-0.01,b+0.01). @f]
    # Buy prices round down and sell prices round up. Select the highest candidate
    # buy and lowest candidate sell, requiring buy < sell.
    # @warning The two orders do not execute atomically; one-sided fills create exposure.
    def maker_plan(self, snapshot):
        if not snapshot["safe"] or not self._fresh(snapshot):
            return None
        sizing = self.sizing(snapshot, "entry")
        quantity = sizing["quantity"]
        if quantity <= 0:
            return None
        buys, sells = [], []
        for ticker in ADAPTIVE_TICKERS:
            bid, ask = snapshot["bid"][ticker][0], snapshot["ask"][ticker][0]
            buy = _app_round(min(snapshot["fair_low"] - self.args.edge, bid + .01, ask - .01), "BUY")
            sell = _app_round(max(snapshot["fair_high"] + self.args.edge, ask - .01, bid + .01), "SELL")
            if buy > 0:
                buys.append((buy, ticker))
            sells.append((sell, ticker))
        if not buys or not sells:
            return None
        buy, sell = max(buys), min(sells)
        if buy[0] >= sell[0] or quantity < max(self.min_order[buy[1]], self.min_order[sell[1]]):
            return None
        return [(buy[1], "BUY", quantity, buy[0], "maker"),
                (sell[1], "SELL", quantity, sell[0], "maker")]

    ## @brief Apply configured risk, volume, depth, and inventory caps and log the result.
    # @param snapshot Current market signal.
    # @param kind Entry or optional probe.
    # @return Diagnostic sizing dictionary from adaptive_size().
    def sizing(self, snapshot, kind):
        tick = int(snapshot.get("case_tick", self.last_case[1] if self.last_case else 0))
        result = adaptive_size(snapshot, self.trades, tick, kind=kind,
                    max_quantity=self.args.quote_size if kind == "entry" else self.args.probe_size,
                    position=self.broker.position, max_position=self.args.max_position,
                    risk_budget=self.args.entry_risk_budget if kind == "entry" else self.args.probe_risk_budget,
                    exit_loss_per_share=self.args.exit_loss_per_share,
                    volume_fraction=self.args.volume_fraction)
        self.log("SIZE", kind=kind, **result)
        return result

    ## @brief Manage resting entry quotes while the owned-fill inventory is flat.
    # @param snapshot Current signal and price bounds.
    # @param now Monotonic time in seconds.
    # @details Withdraw stale or unstable quotes and wait for confirmed cancellation.
    # Read-only mode logs a plan without inventing fills or profits.
    def flat_step(self, snapshot, now):
        if self.episode:
            # Keep entry quotes resting, but withdraw on instability, age or a shifted anchor.
            reference_moved = (snapshot["safe"] and self.quote_reference is not None
                               and abs(snapshot["fair"] - self.quote_reference) >= self.args.edge)
            if (self.quote_plan is None or now - self.episode["started"] >= self.args.quote_ttl
                    or not snapshot["safe"] or reference_moved):
                if self.cancel_active() and not self.broker.position:
                    self.finish_episode(now)
                return
            if not self.broker.active_orders:
                self.finish_episode(now)
            return
        if now < self.next_entry or self.stop_requested:
            return
        plan = self.maker_plan(snapshot)
        if plan is None:
            return
        if not self.args.live:
            self.log("PAPER_PLAN", orders=plan, reference=snapshot["fair"],
                     note="No synthetic fills or simulated profits assumed")
            self.next_entry = now + self.args.quote_ttl
            return
        self.new_episode(now)
        self.quote_plan = plan
        self.quote_reference = snapshot["fair"]
        for ticker, action, quantity, price, role in plan:
            if not self._fresh(snapshot) or self.broker.position:
                break
            record = self.broker.place(ticker, action, quantity, price, role)
            if record is not None:
                record["episode"] = self.episode["number"]
        if not self.broker.active_orders and not self.broker.position:
            self.finish_episode(now)

    ## @brief Select a LIMIT-only exit for the current signed inventory.
    # @param snapshot Current per-venue bids and asks.
    # @param emergency Whether stopping or holding-time conditions require urgency.
    # @return Exit order tuple, or None when no valid exit is available.
    # @warning Emergency prices remain bounded relative to the episode cash basis.
    # An exit may therefore remain unfilled; neither flattening nor a loss cap is guaranteed.
    def exit_plan(self, snapshot, emergency):
        position = self.broker.position
        if not position or self.episode is None:
            return None
        basis = -(self.broker.cash - self.episode["cash_start"]) / position
        action = "SELL" if position > 0 else "BUY"
        candidates = []
        for ticker in ADAPTIVE_TICKERS:
            bid, ask = snapshot["bid"][ticker][0], snapshot["ask"][ticker][0]
            if bid <= 0 or ask <= bid:
                continue
            if action == "SELL":
                price = max(basis - self.args.exit_loss_per_share, bid) if emergency else max(basis + self.args.edge, ask - .01, bid + .01)
            else:
                price = min(basis + self.args.exit_loss_per_share, ask) if emergency else min(basis - self.args.edge, bid + .01, ask - .01)
            if price > 0:
                candidates.append((_app_round(price, action), ticker))
        if not candidates:
            return None
        if emergency:
            price, ticker = (max(candidates) if action == "SELL" else min(candidates))
        else:
            price, ticker = (min(candidates) if action == "SELL" else max(candidates))
        quantity = min(abs(position), self.args.max_order)
        if quantity < self.min_order[ticker]:
            return None
        return ticker, action, quantity, price, "exit"

    ## @brief Build an optional experimental, fillable display order.
    # @param snapshot Fresh market snapshot.
    # @return Probe order tuple or None.
    # @note Disabled in guarded mode. This branch is retained from the original script,
    # not presented as an executed part of the strategy described in the write-up.
    # @warning A probe can fill and increase inventory; it is not a fictitious order.
    def probe_plan(self, snapshot):
        position = self.broker.position
        if (not position or not snapshot["safe"] or not self._fresh(snapshot)
                or self.probes_disabled or self.stop_requested or self.episode["arm"] != "probe"
                or self.episode["probe_attempted"]):
            return None
        action = "BUY" if position > 0 else "SELL"
        candidates = []
        for ticker in ADAPTIVE_TICKERS:
            if action == "BUY":
                touch, depth = snapshot["stable_bid"][ticker]
                price = min(touch - self.args.probe_offset, snapshot["fair_low"] - self.args.edge)
                price = min(price, snapshot["ask"][ticker][0] - .01)
            else:
                touch, depth = snapshot["stable_ask"][ticker]
                price = max(touch + self.args.probe_offset, snapshot["fair_high"] + self.args.edge)
                price = max(price, snapshot["bid"][ticker][0] + .01)
            if price > 0 and depth > 0:
                candidates.append((depth, ticker, _app_round(price, action)))
        if not candidates:
            return None
        depth, ticker, price = min(candidates)
        quantity = self.sizing(snapshot, "probe")["quantity"]
        # A tiny display against enormous depth is not an influence experiment.
        if quantity < max(self.min_order[ticker], abs(position) * 2) or quantity < .05 * depth:
            return None
        for existing in self.broker.active_orders:
            if existing["ticker"] == ticker and existing["action"] != action:
                if (action == "BUY" and price >= existing["price"] - 1e-8) or (action == "SELL" and price <= existing["price"] + 1e-8):
                    return None
        return ticker, action, quantity, price, "probe"

    ## @brief Cancel entry quotes, reconcile late fills, and manage LIMIT exits.
    # @param snapshot Current market snapshot.
    # @param now Monotonic time in seconds.
    # @details Re-evaluate exit quantity after cancellations to account for late fills.
    # Optional probes remain subject to the configured mode and risk checks.
    def inventory_step(self, snapshot, now):
        if self.episode is None:
            raise ValueError("Inventory appeared outside an owned episode")
        if self.episode["inventory_since"] is None:
            self.episode["inventory_since"] = now
        own_episode = [r for r in self.broker.orders.values() if r.get("episode") == self.episode["number"]]
        if any(r["role"] == "probe" and r["filled"] for r in own_episode):
            if not self.episode["probe_fill"]:
                self.log("PROBE_DISABLED", reason="displayed_probe_filled")
            self.episode["probe_fill"] = self.probes_disabled = True
        age = now - self.episode["inventory_since"]
        emergency = self.stop_requested or age >= self.args.max_hold or self.episode["probe_fill"]
        if age >= self.args.max_hold + self.args.cleanup_seconds:
            self.request_stop(reason="limit_exit_not_filled")
        # Entry orders must be definitively cancelled before their replacement.
        for record in list(self.broker.active_orders):
            if (record["role"] == "maker" or (record["role"] == "probe" and
                    (emergency or not snapshot["safe"] or now - record["created"] >= self.args.probe_ttl))):
                self.broker.cancel(record["id"])
        if any(r["role"] == "maker" for r in self.broker.active_orders):
            return
        if emergency and any(r["role"] == "probe" for r in self.broker.active_orders):
            return
        if not self.broker.position:
            return
        plan = self.exit_plan(snapshot, emergency)
        for record in list(self.broker.active_orders):
            if record["role"] != "exit":
                continue
            changed_side = plan is None or record["action"] != plan[1]
            remaining = record["quantity"] - record["filled"]
            urgent_reprice = emergency and plan is not None and (record["ticker"] != plan[0] or abs(record["price"] - plan[3]) >= .01 - 1e-8)
            if changed_side or urgent_reprice or remaining > abs(self.broker.position) or now - record["created"] >= self.args.quote_ttl:
                self.broker.cancel(record["id"])
        if not self._fresh(snapshot):
            return
        if plan and not any(r["role"] == "exit" for r in self.broker.active_orders):
            # Re-evaluate after cancellations, which can include late fills.
            plan = self.exit_plan(snapshot, emergency)
            if plan:
                crossing = [r for r in self.broker.active_orders if r["ticker"] == plan[0]
                            and r["action"] != plan[1] and
                            ((plan[1] == "BUY" and plan[3] >= r["price"] - 1e-8) or
                             (plan[1] == "SELL" and plan[3] <= r["price"] + 1e-8))]
                if crossing:
                    for record in crossing:
                        self.broker.cancel(record["id"])
                    return
                record = self.broker.place(*plan)
                if record is not None:
                    record["episode"] = self.episode["number"]
                    if not self.broker.position:
                        self.cancel_active()
                        return
        if self.broker.position and not emergency and any(r["role"] == "exit" for r in self.broker.active_orders):
            probe = self.probe_plan(snapshot)
            if probe:
                record = self.broker.place(*probe)
                if record is not None:
                    record["episode"] = self.episode["number"]
                    self.episode["probe_attempted"] = True

    def step(self):
        self.broker.poll()
        if self.broker.halted or not self.broker.position_known or not self.broker.cash_known:
            raise ValueError("Execution state uncertain: halt, inspect owned orders and inventory")
        snapshot = self.snapshot()
        now = self.clock()
        if self.started is not None and now - self.started >= self.args.duration:
            self.request_stop(reason="test_duration")
        coherent = self.account_check(now)
        if now - self.last_heartbeat >= 1:
            self.last_heartbeat = now
            self.log("STATUS", active=snapshot["active"], reference=snapshot["fair"],
                     signal=snapshot["reason"], suspicion=round(snapshot["suspicious"], 3),
                     inventory=self.broker.position, gross_cash=self.broker.cash,
                     open_orders=len(self.broker.active_orders), stopping=self.stop_requested)
        if self.stop_requested:
            for record in list(self.broker.active_orders):
                if record["role"] != "exit":
                    self.broker.cancel(record["id"])
            if not snapshot["active"] or now - self.stop_started >= self.args.cleanup_seconds:
                return False
            if not self.broker.position and not self.broker.active_orders:
                self.finish_episode(now)
                return False
        if not snapshot["active"] or not coherent:
            return True
        if self.broker.position:
            self.inventory_step(snapshot, now)
        else:
            # A filled exit must not leave a still-fillable display behind.
            if self.episode and self.episode["inventory_since"] is not None:
                if self.cancel_active() and not self.broker.position:
                    self.finish_episode(now)
            elif not self.stop_requested:
                self.flat_step(snapshot, now)
        return True

    ## @brief Cancel owned orders and report authoritative final account exposure.
    # @details Never cancel potentially reused IDs after a detected case reset.
    # Final flatness requires validated zero net/gross and no unresolved owned orders.
    # @warning Shutdown does not guarantee liquidation; gross cash is not portfolio
    # value while residual inventory remains.
    def shutdown(self):
        # No new order on shutdown failure. Cancel only IDs proven ours, and
        # never touch IDs after a detected case reset.
        if self.context_valid and self.args.live:
            until = self.clock() + self.args.cleanup_seconds
            try:
                current = self.broker.get("/case")
                self.case_check(current)
                while self.broker.active_orders and self.clock() < until:
                    self.case_check(self.broker.get("/case"))
                    self.cancel_active()
                    if self.broker.active_orders:
                        time.sleep(.10)
            except Exception as exc:
                self.log("ERROR", reason="cleanup_incomplete", detail=type(exc).__name__)
        authoritative_flat = False
        final_net = final_gross = "UNKNOWN"
        if self.context_valid and self.validated:
            try:
                self.case_check(self.broker.get("/case"))
                risk = self._limit(self.broker.get("/limits"))
                final_net, final_gross = _app_number(risk["net"]), _app_number(risk["gross"])
                authoritative_flat = abs(final_net) < 1e-8 and abs(final_gross) < 1e-8
            except Exception as exc:
                self.log("ERROR", reason="final_account_unverified", detail=type(exc).__name__)
        self.pool.shutdown(wait=True)
        position_known = self.broker.position_known
        unresolved = [r["id"] for r in self.broker.active_orders]
        flat = (self.context_valid and self.validated and authoritative_flat
                and position_known and self.broker.position == 0 and not unresolved)
        self.final_flat = flat
        self.log("FINAL", flat=flat, inventory=self.broker.position if position_known and self.context_valid else "UNKNOWN",
                 gross_cash=self.broker.cash if self.broker.cash_known and self.context_valid else "UNKNOWN",
                 open_order_ids=unresolved, manual_action_required=not flat,
                 authoritative_net=final_net, authoritative_gross=final_gross,
                 control=self.stats["control"], probe=self.stats["probe"],
                 note="LIMIT-only exits do not guarantee flattening; cash is not NLV while inventory remains")
        self.broker.close()

    ## @brief Validate, run the event loop, and always perform shutdown checks.
    # @return True only when no handled error occurred and final flatness is confirmed.
    def run(self):
        try:
            self.validate()
            while self.step():
                time.sleep(self.args.poll)
        except (BrokerError, ValueError, KeyError, CaseChanged) as exc:
            self.had_error = True
            self.log("ERROR", detail=str(exc))
        finally:
            self.shutdown()
        return not self.had_error and self.final_flat


## @brief Define command-line configuration without placing orders.
# @return Argument parser.
# @note Read-only is the default. Mode defaults to experiment in the original
# code; the session described in the report used guarded explicitly.
def adaptive_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version="RIT Adaptive " + ADAPTIVE_VERSION)
    p.add_argument("--base-url", default="http://localhost:18029/v1")
    p.add_argument("--live", action="store_true", help="Enable LIMIT orders inside the local RIT simulator")
    p.add_argument("--mode", choices=("guarded", "experiment", "influence"), default="experiment")
    p.add_argument("--duration", type=float, default=60, help="Seconds after case first becomes active")
    p.add_argument("--quote-size", type=int, default=25)
    p.add_argument("--probe-size", type=int, default=75)
    p.add_argument("--max-order", type=int, default=100)
    p.add_argument("--max-position", type=int, default=150)
    p.add_argument("--entry-risk-budget", type=float, default=2)
    p.add_argument("--probe-risk-budget", type=float, default=6)
    p.add_argument("--volume-fraction", type=float, default=.10)
    p.add_argument("--mutation-rate", type=int, default=6)
    p.add_argument("--edge", type=float, default=.02)
    p.add_argument("--probe-offset", type=float, default=.02)
    p.add_argument("--probe-ttl", type=float, default=.60)
    p.add_argument("--quote-ttl", type=float, default=1.20)
    p.add_argument("--persistence", type=float, default=.30)
    p.add_argument("--max-snapshot-age", type=float, default=.35)
    p.add_argument("--max-hold", type=float, default=6)
    p.add_argument("--cleanup-seconds", type=float, default=4)
    p.add_argument("--exit-loss-per-share", type=float, default=.08)
    p.add_argument("--probe-loss-stop", type=float, default=5)
    p.add_argument("--max-episode-loss", type=float, default=10)
    p.add_argument("--max-drawdown", type=float, default=30)
    p.add_argument("--poll", type=float, default=.15)
    p.add_argument("--cooldown", type=float, default=.50)
    p.add_argument("--end-buffer", type=int, default=15)
    p.add_argument("--no-record-books", action="store_false", dest="record_books", default=True)
    p.add_argument("--log-dir", default=str(Path(__file__).resolve().parent / "rit_v3_logs"))
    p.add_argument("--forget-key", action="store_true", help="Remove only this bot's local encrypted key cache and exit")
    p.add_argument("--reset-key", action="store_true", help="Ask for and save a new key on Windows")
    return p


## @brief Parse parameters, authenticate locally, and run the controller.
# @param argv Optional command-line argument sequence.
# @return Zero on verified successful shutdown, otherwise two.
# @note Importing this file does not launch the trading loop.
def adaptive_main(argv=None):
    args = adaptive_parser().parse_args(argv)
    if args.forget_key:
        forget_key()
        print("Encrypted cache removed if present. Environment variable, if any, is unchanged.")
        return 0
    positive = ("duration", "quote_size", "probe_size", "max_order", "max_position", "mutation_rate",
                "edge", "probe_offset", "probe_ttl", "quote_ttl", "persistence", "max_snapshot_age",
                "max_hold", "cleanup_seconds", "exit_loss_per_share", "probe_loss_stop",
                "max_episode_loss", "max_drawdown", "poll", "cooldown")
    positive += ("entry_risk_budget", "probe_risk_budget", "volume_fraction")
    if any(not math.isfinite(float(getattr(args, k))) or getattr(args, k) <= 0 for k in positive):
        raise SystemExit("All size/time/risk parameters must be finite and positive")
    if not (args.quote_size <= args.max_order <= 10000 and args.probe_size <= args.max_order
            and args.max_position <= 25000 and args.quote_size + args.probe_size <= args.max_position
            and args.volume_fraction <= 1 and 1 <= args.mutation_rate <= 10 and 1 <= args.end_buffer < 300):
        raise SystemExit("Inconsistent order sizes, inventory bound, rate or end buffer")
    key = load_key(reset=args.reset_key)
    logger = EventLog(args.log_dir, key)
    print("Journal a joindre ici :", logger.path)
    try:
        broker = Broker(args.base_url, key, live=args.live, max_position=args.max_position,
                        max_order=args.max_order, mutation_rate=args.mutation_rate, logger=logger)
        bot = AdaptiveBot(broker, args, logger)
        signal.signal(signal.SIGINT, bot.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, bot.request_stop)
        successful = bot.run()
    finally:
        logger.close()
    return 0 if successful else 2


if __name__ == "__main__":
    raise SystemExit(adaptive_main())
