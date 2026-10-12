"""Client for the Apps Script sheet bridge (TelegramSheetBridge.gs).

The Python bot talks to the Google Sheet through the Apps Script project's
/exec URL instead of a Google Cloud service account. Every request carries
the TG_BRIDGE_SECRET.

BridgeSheet mimics the small subset of the gspread Worksheet API that the
store layer uses, so all parsing/business logic stays unchanged.
"""

import httpx

_REDIRECTS = {301, 302, 303, 307, 308}

_LOGIN_HINT = (
    "Apps Script web app asks for Google sign-in. In Apps Script: "
    "Deploy > Manage deployments > edit the Web App > "
    "'Who has access' must be 'Anyone' > New version > Deploy."
)

_NO_BRIDGE_HINT = (
    "Your Apps Script /exec is missing the bridge code. In Apps Script: "
    "add the TelegramSheetBridge.gs file, replace doPost with "
    "Code_gs_doPost_ready_to_paste.txt, set the TG_BRIDGE_SECRET script "
    "property, then Deploy > Manage deployments > edit > New version > Deploy."
)


class BridgeError(RuntimeError):
    pass


class Cell:
    __slots__ = ("row", "col", "value")

    def __init__(self, row, col, value):
        self.row = row
        self.col = col
        self.value = value


class Bridge:
    def __init__(self, base_url, secret, timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, follow_redirects=False,
                                   trust_env=False)
        self._final_url = None

    def _resolve_url(self, payload):
        """POST to /exec, following Apps Script's redirect manually (re-POST)."""
        if self._final_url:
            try:
                return self._post(self._final_url, payload)
            except BridgeError as e:
                if "redirect" not in str(e).lower():
                    raise
                self._final_url = None
        return self._post(self.base_url, payload, follow=True)

    def _post(self, url, payload, follow=False):
        r = self._client.post(url, json=payload)
        if follow and r.status_code in _REDIRECTS and r.headers.get("location"):
            loc = r.headers["location"]
            if "accounts.google.com" in loc:
                raise BridgeError(_LOGIN_HINT)
            r = self._client.post(loc, json=payload)
            if r.status_code == 200:
                # Cache the echo URL only when it actually answered with JSON.
                # Never cache an error page (e.g. a login redirect target).
                try:
                    r.json()
                except Exception:
                    pass
                else:
                    self._final_url = loc
        if r.status_code in _REDIRECTS:
            raise BridgeError("redirect loop talking to Apps Script")
        if "accounts.google.com" in str(getattr(r, "url", "")):
            raise BridgeError(_LOGIN_HINT)
        try:
            data = r.json()
        except Exception:
            raise BridgeError(f"Apps Script returned non-JSON (HTTP {r.status_code})")
        if not isinstance(data, dict) or not data.get("success"):
            err = str(data.get("error", "bridge call failed"))
            if "is not defined" in err:
                # Deployed Apps Script code is missing the bridge (or its own
                # functions) — tell the user exactly what to do.
                raise BridgeError(_NO_BRIDGE_HINT + " (Apps Script said: " + err + ")")
            raise BridgeError(err)
        return data.get("data", {})

    def call(self, op, **kwargs):
        payload = {"action": "tgBridge",
                   "payload": {"op": op, "secret": self.secret, **kwargs}}
        return self._resolve_url(payload)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


class BridgeSheet:
    """Worksheet-like view over one sheet, backed by bridge ops."""

    def __init__(self, bridge, name):
        self._b = bridge
        self._name = name

    def _op(self, op, **kw):
        return self._b.call(op, sheet=self._name, **kw)

    def get_all_values(self):
        return [list(r) for r in self._op("getValues").get("values", [])]

    def get(self, rng):
        return [list(r) for r in self._op("getRange", range=rng).get("values", [])]

    def col_values(self, col):
        vals = self._op("colValues", col=col).get("values", [])
        return [r[0] if r else "" for r in vals]

    def row_values(self, row):
        vals = self._op("rowValues", row=row).get("values", [])
        return list(vals[0]) if vals else []

    def cell(self, row, col):
        vals = self.get(f"{_a1(col)}{row}")
        v = vals[0][0] if vals and vals[0] else ""
        return Cell(row, col, v)

    def update_cell(self, row, col, value):
        self._op("updateCell", row=row, col=col, value=value)

    def update(self, rng, values):
        self._op("updateRange", range=rng, values=values)

    def append_row(self, row):
        return self._op("appendRow", row=list(row))

    def batch_update(self, reqs):
        updates = [{"range": q["range"], "values": q["values"]} for q in reqs]
        self._op("batchUpdate", updates=updates)


def _a1(col):
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s
