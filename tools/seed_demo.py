#!/usr/bin/env python3
"""Seed a Cold Storage archive with a LOT of realistic-looking template data,
for recording a product demo. Nothing here is real: every name, message and
photo is synthetic.

Run against an already-initialised archive (``cold init`` first). Honours the
usual env: COLD_HOME (which archive), COLD_PASSPHRASE + COLD_NO_KEYRING (unlock
an encrypted one without touching the OS keychain).

    COLD_HOME=~/ColdStorageDemo COLD_PASSPHRASE=demo COLD_NO_KEYRING=1 \
        .venv/bin/python tools/seed_demo.py
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make the engine importable whether or not the package is installed.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from coldstorage.models import Batch, MediaRef, NormalizedRecord, RecordType  # noqa: E402
from coldstorage.runtime import load_runtime  # noqa: E402

# A deterministic PRNG so re-runs are identical and reviewable (and so we never
# import `random`, keeping this self-contained and reproducible).
_state = 0x9E3779B97F4A7C15


def rnd() -> float:
    global _state
    _state = (_state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
    return ((_state >> 11) & ((1 << 53) - 1)) / float(1 << 53)


def pick(seq):
    return seq[int(rnd() * len(seq)) % len(seq)]


def rint(a: int, b: int) -> int:
    return a + int(rnd() * (b - a + 1))


# --------------------------------------------------------------------------- #
# Tiny pure-Python PNG writer (no Pillow). Vertical two-tone gradient — reads as
# a photo thumbnail and every image is unique, so each becomes its own blob.
# --------------------------------------------------------------------------- #
def _chunk(typ: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + typ
        + data
        + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, seed: int, w: int = 256, h: int = 256) -> None:
    # Two distinct-ish colours derived from the seed.
    def col(k: int):
        return (
            60 + (k * 73) % 190,
            50 + (k * 151) % 200,
            70 + (k * 199) % 180,
        )

    c1, c2 = col(seed), col(seed * 7 + 13)
    raw = bytearray()
    for y in range(h):
        t = y / (h - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        raw.append(0)  # PNG filter type 0 for this scanline
        raw += bytes((r, g, b)) * w
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)


# --------------------------------------------------------------------------- #
# Content pools
# --------------------------------------------------------------------------- #
OWNER = "Alex Rivera"  # "you" — the author that spans every thread (right-aligned)

FIRST = [
    "Maya", "Liam", "Sofia", "Noah", "Emma", "Kai", "Aria", "Ethan", "Zoe", "Leo",
    "Nina", "Omar", "Priya", "Diego", "Hana", "Marcus", "Ivy", "Theo", "Lena", "Sam",
    "Amara", "Felix", "Chloe", "Ravi", "Yuki", "Mateo", "Isla", "Jonas", "Farah", "Bea",
    "Tariq", "Elena", "Nate", "Simone", "Arjun", "Cleo", "Dana", "Gus", "Mira", "Rex",
]
LAST = [
    "Chen", "Patel", "Nguyen", "Okoro", "Silva", "Kim", "Haddad", "Rossi", "Novak", "Diaz",
    "Ford", "Bauer", "Costa", "Reed", "Mensah", "Kaur", "Vega", "Sato", "Blum", "Ito",
    "Cruz", "Abbas", "Lund", "Park", "Moreau", "Singh", "Ali", "Weber", "Flores", "Yamada",
]


def name_pool(n: int) -> list[str]:
    out, seen = [], set()
    i = 0
    while len(out) < n and i < 5000:
        i += 1
        nm = f"{pick(FIRST)} {pick(LAST)}"
        if nm != OWNER and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


LINES = [
    "hey!! did you see the photos from the weekend??", "omg yes they turned out so good",
    "are we still on for friday?", "yeah 7pm works — same place?", "lol that's amazing",
    "wait send me the address again", "just landed, call you in a bit", "how did the interview go?!",
    "it went really well actually 😄", "so proud of you", "can you resend that file when you get a sec",
    "on it", "did you eat yet", "starving lol, let's get tacos", "i'll be 10 min late sorry",
    "no worries take your time", "look at this 😂", "hahaha stop", "happy birthday!!! 🎉",
    "thank you so much ❤️", "you free to talk tonight?", "yeah after 9", "we need to plan the trip",
    "i found flights, sending now", "these are perfect", "book it before it's gone",
    "miss you", "miss you too, home soon", "the meeting got moved to 3", "ok noted thanks",
    "did the package arrive?", "yep just got it!", "let's do dinner next week", "i'm in, wednesday?",
    "check your email", "done — replied", "that concert was unreal", "best night in ages honestly",
    "can you water my plants while i'm away", "of course, have the best time", "sending good vibes ✨",
    "ok this made my whole day", "call me when you land safe", "always do ❤️", "new haircut who dis",
    "looks so good!!", "ugh monday", "coffee? my treat", "yes please, life saver", "guess who got the job",
    "NO WAY congrats!!!", "we should celebrate", "absolutely, this weekend", "did you finish the show",
    "no spoilers!!", "the puppy says hi 🐶", "i can't handle the cuteness", "see you soon",
]
CAPTIONS = [
    "golden hour never misses", "throwback to a good one", "weekend well spent",
    "new city, who dis", "made this from scratch 🍝", "sunday reset", "found my new favourite spot",
    "little moments", "3 years ago today", "we made it to the top", "beach day essentials",
    "studio session", "road trip diaries", "morning light", "this view though",
]


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Per-connector plan. Every platform gets conversations + contacts + posts, so
# whichever one is "connected" on camera already has a rich archive behind it.
# --------------------------------------------------------------------------- #
PLAN = {
    "instagram": dict(threads=16, msgs=(30, 110), followers=680, following=430, posts=40, img_every=6),
    "facebook":  dict(threads=14, msgs=(24, 90),  followers=540, following=0,   posts=32, img_every=7, ctype="friends"),
    "twitter":   dict(threads=10, msgs=(14, 55),  followers=910, following=620, posts=150, img_every=9),
    "whatsapp":  dict(threads=16, msgs=(35, 150), followers=0,   following=0,   posts=0,   img_every=5),
    "google":    dict(threads=9,  msgs=(20, 70),  followers=0,   following=0,   posts=0,   img_every=5),
    "snapchat":  dict(threads=12, msgs=(18, 75),  followers=240, following=0,   posts=0,   img_every=4),
    "telegram":  dict(threads=11, msgs=(24, 100), followers=0,   following=0,   posts=0,   img_every=6),
    "discord":   dict(threads=8,  msgs=(30, 120), followers=0,   following=0,   posts=0,   img_every=7),
    "linkedin":  dict(threads=7,  msgs=(10, 38),  followers=460, following=0,   posts=20,  img_every=11, ctype="connections"),
    "reddit":    dict(threads=5,  msgs=(8, 28),   followers=0,   following=0,   posts=95,  img_every=8),
    "slack":     dict(threads=6,  msgs=(20, 80),  followers=0,   following=0,   posts=0,   img_every=8),
}

NOW = datetime.now(tz=UTC)


def main() -> None:
    home = os.environ.get("COLD_HOME")
    if not home:
        sys.exit("COLD_HOME must be set")
    rt = load_runtime(home=home, passphrase=os.environ.get("COLD_PASSPHRASE"))
    archive = rt.open_archive()

    tmp = Path(tempfile.mkdtemp(prefix="cold-demo-img-"))
    img_n = 0  # global, so every generated image is unique -> its own blob
    totals = {"messages": 0, "media": 0, "contacts": 0, "posts": 0}

    def new_image() -> Path:
        nonlocal img_n
        img_n += 1
        p = tmp / f"img_{img_n:05d}.png"
        write_png(p, img_n)
        return p

    for connector, cfg in PLAN.items():
        uid = 0

        def nid(kind: str) -> str:
            nonlocal uid
            uid += 1
            return f"{kind}-{uid}"

        records: list[NormalizedRecord] = []
        media: list[MediaRef] = []

        # ---- conversations -------------------------------------------------
        contacts = name_pool(cfg["threads"])
        for contact in contacts:
            n = rint(*cfg["msgs"])
            # walk backwards in time so the thread ends "recently"
            t = NOW - timedelta(days=rint(1, 640), minutes=rint(0, 1400))
            for i in range(n):
                t = t + timedelta(minutes=rint(2, 800))
                if t > NOW:
                    t = NOW - timedelta(minutes=rint(1, 200))
                author = OWNER if (i % 2 == 0) else contact
                mid = nid("m")
                rec = NormalizedRecord(
                    connector=connector,
                    type=RecordType.MESSAGE,
                    uid=mid,
                    created_at=iso(t),
                    text=pick(LINES),
                    author=author,
                    thread=contact,
                    extra={"thread_path": contact},
                )
                records.append(rec)
                totals["messages"] += 1
                if cfg["img_every"] and (int(rnd() * cfg["img_every"]) == 0):
                    p = new_image()
                    media.append(
                        MediaRef(owner_uid=mid, kind="image",
                                 source_path=str(p), filename=p.name)
                    )
                    totals["media"] += 1

        # ---- contacts (followers / friends / connections) ------------------
        ctype = cfg.get("ctype", "followers")
        rtype = RecordType.FOLLOWER
        for who in name_pool(cfg["followers"]):
            records.append(NormalizedRecord(
                connector=connector, type=rtype, uid=nid("f"),
                text=who, author=who, extra={"kind": ctype},
            ))
            totals["contacts"] += 1
        for who in name_pool(cfg["following"]):
            records.append(NormalizedRecord(
                connector=connector, type=RecordType.FOLLOWING, uid=nid("g"),
                text=who, author=who,
            ))
            totals["contacts"] += 1

        # ---- posts (with photos) ------------------------------------------
        for _ in range(cfg["posts"]):
            t = NOW - timedelta(days=rint(1, 700))
            pid = nid("p")
            records.append(NormalizedRecord(
                connector=connector, type=RecordType.POST, uid=pid,
                created_at=iso(t), text=pick(CAPTIONS), author=OWNER,
            ))
            totals["posts"] += 1
            for _ in range(rint(1, 3)):
                p = new_image()
                media.append(MediaRef(owner_uid=pid, kind="image",
                                      source_path=str(p), filename=p.name))
                totals["media"] += 1

        added = archive.ingest_batch(Batch(records=records, media=media))
        finished = NOW - timedelta(minutes=rint(3, 320))
        archive.index.record_run(
            connector, iso(finished - timedelta(minutes=rint(1, 6))),
            iso(finished), "ok", added, None,
        )
        print(f"  {connector:10s} +{added:5d} records")

    archive.close()
    print(
        f"\nseeded: {totals['messages']} messages, {totals['contacts']} contacts, "
        f"{totals['posts']} posts, {totals['media']} photos"
    )


if __name__ == "__main__":
    main()
