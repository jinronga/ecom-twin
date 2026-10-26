import struct
import zlib

import pytest

from ecom_twin.evidence import collect, final_path, prepare, validate_png


def png_1x1() -> bytes:
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body +
                struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def test_prepare_collect_validates_and_never_overwrites(tmp_path):
    source = tmp_path / "mcp-output.png"
    source.write_bytes(png_1x1())
    destination = final_path("T-0001", root=tmp_path / "evidence")
    assert collect(source, destination) == destination
    assert validate_png(destination) == (1, 1)
    with pytest.raises(FileExistsError):
        collect(source, destination)


def test_collect_rejects_fake_png(tmp_path):
    source = tmp_path / "fake.png"
    source.write_text("not an image")
    with pytest.raises(ValueError, match="PNG"):
        collect(source, tmp_path / "out.png")


def test_start_chrome_uses_new_instance_and_skips_busy_port(tmp_path, monkeypatch):
    import ecom_twin.evidence as evidence
    calls = []
    class Probe:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def connect_ex(self, address): return 0 if address[1] == 9222 else 1
    monkeypatch.setattr(evidence.socket, 'socket', Probe)
    monkeypatch.setattr(evidence.subprocess, 'Popen', lambda args, **kwargs: calls.append(args))
    port = evidence.start_chrome(user_data_dir=str(tmp_path / 'profile'), port=9222, url='https://open.feishu.cn/app')
    assert port == 9223
    assert calls and calls[0][:4] == ['/usr/bin/open', '-na', 'Google Chrome', '--args']
    assert '--new-window' in calls[0] and 'https://open.feishu.cn/app' in calls[0]
