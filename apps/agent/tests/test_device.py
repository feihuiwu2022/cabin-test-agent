import pytest

from cabin_agent.device.simcar import SimCarAdapter


@pytest.fixture
async def dev():
    d = SimCarAdapter()
    await d.start()
    yield d
    await d.stop()


def find(els, text):
    return next(e for e in els if e.text == text or e.key == text)


async def test_health_and_screenshot(dev):
    h = await dev.health()
    assert h.online and h.ota == "C01-OTA-1.0.0"
    png = await dev.screenshot()
    assert png[:4] == b"\x89PNG"
    assert (await dev.screenshot("jpeg"))[:3] == b"\xff\xd8\xff"


async def test_navigate_to_display_and_truncation(dev):
    await dev.tap(*find(await dev.dump_ui(), "设置").center)
    assert await dev.current_screen() == "设置/通用"
    await dev.tap(*find(await dev.dump_ui(), "nav-显示").center)
    assert await dev.current_screen() == "设置/显示"
    el = find(await dev.dump_ui(), "自动调节亮度")
    assert el.desc == "根据环境光自动调节屏幕亮度"
    assert el.truncated  # 已知缺陷：说明文案截断
    await dev.key("back")
    assert await dev.current_screen() == "home"


async def test_crash_writes_logcat(dev):
    _, cur = await dev.logs()
    await dev.tap(*find(await dev.dump_ui(), "设置").center)
    await dev.tap(*find(await dev.dump_ui(), "nav-声音").center)
    await dev.tap(*find(await dev.dump_ui(), "音效").center)
    txt, _ = await dev.logs(cur)
    assert "FATAL EXCEPTION" in txt and "com.cabin.sim.settings" in txt
    assert await dev.current_screen() == "home"
