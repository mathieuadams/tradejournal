"""Reader for Databento's binary format (DBN), uncompressed, streamed: MBP-10 records (the top 10 price levels of
the book after every event, plus the trades).

Layout (little endian):
  file      "DBN" + version (1 byte) + metadata length (u32) + metadata, then the records
  header    length in 4-byte words (u8), rtype (u8), publisher_id (u16), instrument_id (u32), ts_event (u64)  = 16 bytes
  MBP-10    price i64, size u32, action char, side char, flags u8, depth u8, ts_recv u64, ts_in_delta i32,
  (rtype    sequence u32, then 10 levels of {bid_px i64, ask_px i64, bid_sz u32, ask_sz u32, bid_ct u32, ask_ct u32}
   0x0A)    = 368 bytes. Prices are fixed point: 1 = 1e-9; an empty level has the price i64 max."""
import struct

MBP10 = 0x0A
UNDEF = (1 << 63) - 1
_LV = struct.Struct("<" + "qqIIII" * 10)
T, A, B = ord("T"), ord("A"), ord("B")


def _read(f, n):
    out = b""
    while len(out) < n:
        x = f.read(n - len(out))
        if not x:
            raise ValueError("DBN stream ended early")
        out += x
    return out


def records(f, chunk=1 << 22):
    """(rtype, memoryview of the record) for every record of the stream."""
    head = _read(f, 8)
    if head[:3] != b"DBN":
        raise ValueError("not a DBN stream")
    _read(f, int.from_bytes(head[4:8], "little"))     # metadata: not needed (one continuous symbol)
    buf = b""
    while True:
        data = f.read(chunk)
        if not data:
            break
        buf = buf + data if buf else data
        mv, i, n = memoryview(buf), 0, len(buf)
        while i < n:
            L = buf[i] * 4
            if not L:
                raise ValueError("bad DBN record")
            if i + L > n:
                break
            yield buf[i + 1], mv[i:i + L]
            i += L
        buf = buf[i:]


def ts_recv(r):
    return int.from_bytes(r[32:40], "little")


def ts_event(r):
    return int.from_bytes(r[8:16], "little")


def trade(r):
    """(price, size, side 'B' | 'A' | 'N') of a trade record, else None."""
    if r[28] != T:
        return None
    return int.from_bytes(r[16:24], "little", signed=True) / 1e9, int.from_bytes(r[24:28], "little"), chr(r[29])


def levels(r):
    """([(bid price, size)], [(ask price, size)]) of the 10 levels."""
    x = _LV.unpack_from(r, 48)
    bids, asks = [], []
    for k in range(10):
        bp, ap, bs, as_ = x[6 * k], x[6 * k + 1], x[6 * k + 2], x[6 * k + 3]
        if bp != UNDEF and bs:
            bids.append((bp / 1e9, bs))
        if ap != UNDEF and as_:
            asks.append((ap / 1e9, as_))
    return bids, asks


def pack_mbp10(ts, price, size, action, side, bids, asks, rtype=MBP10):
    """One MBP-10 record (for the tests and the fake)."""
    head = struct.pack("<BBHIQ", 368 // 4, rtype, 1, 42, ts)
    body = struct.pack("<qIccBBQiI", int(round(price * 1e9)), size, action.encode(), side.encode(), 0, 0, ts, 0, 0)
    lv = []
    for k in range(10):
        bp, bs = bids[k] if k < len(bids) else (None, 0)
        ap, as_ = asks[k] if k < len(asks) else (None, 0)
        lv += [UNDEF if bp is None else int(round(bp * 1e9)), UNDEF if ap is None else int(round(ap * 1e9)), bs, as_, 1, 1]
    return head + body + _LV.pack(*lv)


def header():
    meta = b"\x00" * 16
    return b"DBN\x02" + struct.pack("<I", len(meta)) + meta
