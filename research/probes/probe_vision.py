"""P1.4 — Binance Vision probe: inventory, earliest dates, checksums, headers, ts units, publish lag, disk."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _report import Report  # noqa: E402

LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA = "https://data.binance.vision/"
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}


def list_keys(c: httpx.Client, prefix: str) -> list[tuple[str, int, str]]:
    out, marker = [], ""
    while True:
        r = c.get(LIST, params={"delimiter": "/", "prefix": prefix, "marker": marker})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for ct in root.findall("s:Contents", NS):
            out.append((ct.find("s:Key", NS).text, int(ct.find("s:Size", NS).text),
                        ct.find("s:LastModified", NS).text))
        if root.find("s:IsTruncated", NS).text != "true":
            return out
        marker = out[-1][0]


def zips(keys):
    return [k for k in keys if k[0].endswith(".zip")]


def ts_unit(v: int) -> str:
    return "µs" if v > 10**14 else "ms"


def fetch_zip(c: httpx.Client, key: str) -> tuple[bytes, bool]:
    body = c.get(DATA + key).content
    chk = c.get(DATA + key + ".CHECKSUM").text.split()[0]
    return body, hashlib.sha256(body).hexdigest() == chk


def peek_csv(body: bytes, n: int = 3) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        with z.open(z.namelist()[0]) as f:
            rdr = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
            return [row for _, row in zip(range(n), rdr)]


def main() -> None:
    rep = Report("probe_vision", "Binance Vision — historical dumps inventory (P1.4)")
    today = dt.date.today()
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        rep.h("Inventory")
        rows = []
        inventory = [
            ("spot", "monthly", "klines", "BTCUSDT", "1m"), ("spot", "daily", "klines", "BTCUSDT", "1m"),
            ("spot", "monthly", "aggTrades", "BTCUSDT", None), ("spot", "daily", "aggTrades", "BTCUSDT", None),
            ("spot", "monthly", "aggTrades", "ETHUSDT", None),
            ("futures/um", "monthly", "klines", "BTCUSDT", "1m"), ("futures/um", "monthly", "aggTrades", "BTCUSDT", None),
            ("futures/um", "monthly", "aggTrades", "ETHUSDT", None), ("futures/um", "daily", "aggTrades", "XAUUSDT", None),
            ("futures/um", "monthly", "fundingRate", "BTCUSDT", None), ("futures/um", "daily", "metrics", "BTCUSDT", None),
            ("futures/um", "daily", "metrics", "XAUUSDT", None), ("futures/um", "daily", "bookDepth", "BTCUSDT", None),
            ("futures/um", "daily", "bookDepth", "XAUUSDT", None), ("futures/um", "daily", "bookTicker", "BTCUSDT", None),
            ("futures/um", "daily", "klines", "XAUUSDT", "1m"),
        ]
        sizes: dict[str, list[tuple[str, int]]] = {}
        for market, freq, dtype, sym, tf in inventory:
            prefix = f"data/{market}/{freq}/{dtype}/{sym}/" + (f"{tf}/" if tf else "")
            keys = zips(list_keys(c, prefix))
            if not keys:
                rows.append([market, freq, dtype, sym, tf or "", 0, "-", "-", "-"])
                continue
            names = [k[0].rsplit("/", 1)[1] for k in keys]
            first, last = names[0], names[-1]
            lag = ""
            if freq == "daily":
                last_date = dt.date.fromisoformat(last.removesuffix(".zip")[-10:])
                lag = f"{(today - last_date).days} d"
            rows.append([market, freq, dtype, sym, tf or "", len(keys), first, last, lag])
            sizes[f"{market}/{freq}/{dtype}/{sym}"] = [(n, s) for n, (_, s, _) in zip(names, keys)]
        rep.table(["market", "freq", "type", "symbol", "tf", "files", "first", "last", "publish lag"], rows)
        rep.raw["sizes"] = sizes

        rep.h("Disk projection (compressed zip sizes, last 12 months)")
        rows = []
        for key in ("spot/monthly/aggTrades/BTCUSDT", "spot/monthly/aggTrades/ETHUSDT",
                    "futures/um/monthly/aggTrades/BTCUSDT", "futures/um/monthly/aggTrades/ETHUSDT",
                    "futures/um/daily/aggTrades/XAUUSDT", "futures/um/daily/bookDepth/XAUUSDT",
                    "futures/um/daily/metrics/XAUUSDT"):
            lst = sizes.get(key, [])
            span = lst[-12:] if "monthly" in key else lst[-365:]
            total = sum(s for _, s in span)
            rows.append([key, len(span), round(total / 2**30, 2), round(total / max(len(span), 1) / 2**20, 1)])
        rep.table(["dataset", "files", "total GB (zip)", "avg MB/file"], rows)
        rep.p("Zip-compressed CSV sizes; Parquet(zstd) sizes are measured in the storage benchmark (P2.2).")

        rep.h("Format checks (checksum, header, timestamp unit)")
        rows = []
        samples = [
            "data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06-03.zip",
            "data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-" + (today - dt.timedelta(days=3)).isoformat() + ".zip",
            "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-06-03.zip",
            "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-" + (today - dt.timedelta(days=3)).isoformat() + ".zip",
            "data/futures/um/daily/aggTrades/XAUUSDT/XAUUSDT-aggTrades-" + (today - dt.timedelta(days=3)).isoformat() + ".zip",
            "data/futures/um/daily/metrics/XAUUSDT/XAUUSDT-metrics-" + (today - dt.timedelta(days=3)).isoformat() + ".zip",
            "data/futures/um/daily/bookDepth/XAUUSDT/XAUUSDT-bookDepth-" + (today - dt.timedelta(days=3)).isoformat() + ".zip",
            "data/futures/um/monthly/fundingRate/XAUUSDT/XAUUSDT-fundingRate-2026-08.zip",
        ]
        for key in samples:
            try:
                body, ok = fetch_zip(c, key)
                head = peek_csv(body)
            except Exception as exc:  # noqa: BLE001
                rows.append([key.rsplit("/", 1)[1], "ERR", repr(exc)[:60], "", ""])
                continue
            has_header = not head[0][0].lstrip("-").isdigit()
            first_data = head[1] if has_header else head[0]
            ts_col = next((v for v in first_data if v.isdigit() and len(v) >= 13), None)
            unit = ts_unit(int(ts_col)) if ts_col else "n/a"
            rows.append([key.rsplit("/", 1)[1], "OK" if ok else "MISMATCH", "yes" if has_header else "no", unit,
                         ",".join(head[0])[:120]])
            rep.raw.setdefault("samples", {})[key] = head
        rep.table(["file", "sha256", "header", "ts unit", "first line"], rows)
        rep.p("Spot files switched to **microsecond** timestamps from 2025-01-01; futures files stay in milliseconds. "
              "The backfiller must detect the unit per file (value range), never assume.")
    rep.save()


if __name__ == "__main__":
    main()
