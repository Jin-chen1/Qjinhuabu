import hashlib

import pytest

from novelvideo import config


@pytest.fixture
def selected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    video = tmp_path / "selected.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42original-reference-video")
    return video


def test_register_requires_original_official_host(selected):
    from novelvideo.dual_reference_sources import register_verified_source

    with pytest.raises(ValueError):
        register_verified_source(selected, "https://example.com/private", selected)


def test_register_rejects_nonidentical_cloud_video(selected, tmp_path):
    from novelvideo.dual_reference_sources import register_verified_source

    other = tmp_path / "other.mp4"
    other.write_bytes(b"\x00\x00\x00\x18ftypmp42different-video")
    with pytest.raises(ValueError):
        register_verified_source(selected, "https://aka.doubaocdn.com/file", other)


@pytest.mark.asyncio
async def test_selected_content_is_resolved_privately_without_tunnel(selected, monkeypatch):
    from novelvideo import dual_reference_sources as sources

    url = "https://aka.doubaocdn.com/original?signature=private-source-signature"
    sources.register_verified_source(selected, url, selected)

    def download(request_url, destination):
        assert request_url == url
        destination.write_bytes(selected.read_bytes())

    monkeypatch.setattr(sources, "download", download)
    assert await sources.ensure_public_video(selected) == url
    path = sources._path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert hashlib.sha256(selected.read_bytes()).hexdigest() in path.read_text()
    selected.write_bytes(b"modified-product-video")
    with pytest.raises(ValueError):
        await sources.ensure_public_video(selected)


@pytest.mark.asyncio
async def test_stale_source_fails_without_echoing_signed_url(selected, monkeypatch):
    from novelvideo import dual_reference_sources as sources

    sources.register_verified_source(
        selected, "https://aka.doubaocdn.com/original?signature=private", selected,
    )
    monkeypatch.setattr(sources, "download", lambda url, path: path.write_bytes(b"wrong"))
    with pytest.raises(ValueError) as error:
        await sources.ensure_public_video(selected)
    assert "signature" not in str(error.value)
